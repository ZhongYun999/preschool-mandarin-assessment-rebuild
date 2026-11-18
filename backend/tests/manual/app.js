'use strict';
const $ = id => document.getElementById(id);
let sid = '', token = '', task = null, attempt = null, plays = [], recs = [], blob = null;
let player = null, activePlay = null, recorder = null, stream = null, jobs = [];
const key = suffix => `qa-workbench:${sid}:${task?.session_task_id || ''}:${attempt || ''}:${suffix}`;
const remember = name => { const k = key(name); let v = localStorage.getItem(k); if (!v) { v = crypto.randomUUID(); localStorage.setItem(k, v); } return v; };
const forget = name => localStorage.removeItem(key(name));
const api = async (path, options = {}) => {
  const headers = {...options.headers}; if (token && path.startsWith('/assessment/')) headers.Authorization = `Bearer ${token}`;
  if (options.json !== undefined) {headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(options.json); delete options.json;}
  const response = await fetch(path, {...options, headers}); const raw = await response.text();
  let data; try { data = JSON.parse(raw); } catch { data = raw; }
  if (!response.ok) throw Error(`${response.status}: ${typeof data === 'string' ? data : JSON.stringify(data.detail || data)}`);
  return data;
};
const post = (path, json) => api(path, {method:'POST', json});
const root = () => `/assessment/sessions/${sid}`;
const base = () => `${root()}/tasks/${task.session_task_id}`;
const inform = value => $('message').textContent = value;
const showError = async e => { inform(`操作未确认：${e.message}。请刷新状态后使用原操作重试。`); try { await refresh(); } catch (_) {} };
const bind = (id, handler) => $(id).addEventListener('click', () => Promise.resolve().then(handler).catch(showError));
async function sessions() { const rows = await api('/dev/qa/sessions'); $('sessions').replaceChildren(...rows.map(row => { const o=document.createElement('option'); o.value=row.session_id; o.textContent=`${row.session_id} (${row.status})`; return o; })); if (sid) $('sessions').value=sid; }
async function openSession(id) { sid=id; localStorage.setItem('qa-workbench:last-session',sid); token=(await post(`/dev/qa/sessions/${sid}/token`,{})).token; blob=null; await refresh(); }
async function refresh() {
 if (!sid || !token) return;
 const state=await api(root()); task=state.tasks[0]; attempt=task.attempt_id;
 const content=await api(`${base()}/content`); const current=await api(`${base()}/attempt`); plays=current.playbacks;
 recs=attempt ? await api(`${base()}/recordings`) : [];
 $('session').textContent=`Session: ${sid} | ${state.status} | Attempt: ${attempt || '尚未开始'}`;
 $('state').textContent=JSON.stringify({task, recordings:recs.map(r=>({...r,browser_declared_mime:localStorage.getItem(key(`mime:${r.recording_id}`))})), playbacks:plays},null,2);
 $('prompt').textContent=content.prompt_text;
 const diagnostic=await api(`/dev/qa/sessions/${sid}/diagnostics`); jobs=diagnostic.jobs;
 $('diagnostics').textContent=JSON.stringify(diagnostic,null,2);
 $('jobs').replaceChildren(...jobs.filter(j=>['FAILED','INVALID','UNKNOWN'].includes(j.status)).map(j=>{const o=document.createElement('option'); o.value=j.job_id; o.textContent=`${j.source}: ${j.status}`; return o;}));
 $('retryJob').disabled=!$('jobs').options.length;
 for (const [id,action] of Object.entries({start:'START_ATTEMPT',guide:'START_GUIDE',initial:'START_INITIAL_PROMPT',replay:'START_USER_REPLAY',retry:'RETRY_PLAYBACK',record:'START_RECORDING',submit:'SUBMIT_QA',redo:'REDO_ATTEMPT'})) $(id).disabled=!task.allowed_actions.includes(action);
 $('recoverPlay').disabled=!task.playback_in_progress; $('cancel').disabled=!task.recording_in_progress;
 $('upload').disabled=!blob || !recs.some(r=>r.status==='STARTED');
 $('stopPlay').disabled=!activePlay; $('stopRecord').disabled=!recorder || recorder.state!=='recording';
}
async function finishPlayback(play, completed) {
 const k=key(`receipt:${play.playback_id}`); localStorage.setItem(k,JSON.stringify({attempt_id:play.attempt_id,completed}));
 await post(`${base()}/playbacks/${play.playback_id}/finish`,{attempt_id:play.attempt_id,completed}); localStorage.removeItem(k);
}
async function play(kind, retryOf=null) {
 const operation=`play:${kind}:${retryOf || ''}`;
 const p=await post(`${base()}/playbacks`,{attempt_id:attempt,request_id:remember(operation),kind,retry_of_id:retryOf});
 forget(operation); activePlay=p; await refresh();
 let complete = false;
 try {
  const response=await fetch(p.audio_url,{headers:{Authorization:`Bearer ${token}`}}); if(!response.ok) throw Error(`音频下载失败 ${response.status}`);
  const url=URL.createObjectURL(await response.blob()); player=new Audio(url);
  complete=await new Promise((resolve,reject)=>{player.onended=()=>resolve(true); player.onerror=()=>reject(Error('播放失败')); player.onpause=()=>{if(!player.ended) resolve(false);}; player.play().catch(reject);});
  URL.revokeObjectURL(url);
 } catch(e) { try { await finishPlayback(p,false); } finally { player=null; activePlay=null; await refresh(); } throw e; }
 try { await finishPlayback(p,complete); inform(complete?'播放完成已确认':'播放中断已记录'); }
 finally {player=null; activePlay=null; await refresh();}
}
async function startRecording() {
 const request=remember('recording'); const row=await post(`${base()}/recordings`,{attempt_id:attempt,request_id:request}); forget('recording');
 try {
  stream=await navigator.mediaDevices.getUserMedia({audio:true});
  const types=['audio/webm;codecs=opus','audio/ogg;codecs=opus','audio/mp4']; const type=types.find(t=>MediaRecorder.isTypeSupported(t));
  recorder=type ? new MediaRecorder(stream,{mimeType:type}) : new MediaRecorder(stream);
  const chunks=[]; recorder.ondataavailable=e=>{if(e.data.size) chunks.push(e.data);};
  recorder.onstop=()=>{blob=new Blob(chunks,{type:recorder.mimeType}); $('preview').src=URL.createObjectURL(blob); $('audioInfo').textContent=`浏览器录音 MIME: ${blob.type}, ${blob.size} 字节`; stream.getTracks().forEach(t=>t.stop()); refresh().catch(showError);};
  recorder.start(); inform(`录音中：${row.recording_id}`); await refresh();
 } catch(e) { await post(`${base()}/recordings/${row.recording_id}/cancel`,{attempt_id:attempt}); if(stream) stream.getTracks().forEach(t=>t.stop()); throw e; }
}
bind('create',async()=>{const row=await post('/dev/qa/sessions',{}); await sessions(); await openSession(row.session_id);});
bind('open',()=>openSession($('sessions').value)); bind('refresh',refresh);
bind('start',async()=>{await post(`${base()}/attempt`,{}); await refresh();});
bind('guide',()=>play('GUIDE')); bind('initial',()=>play('INITIAL_PROMPT')); bind('replay',()=>play('USER_REPLAY'));
bind('retry',()=>{const completed=new Set(plays.filter(p=>p.kind==='TECHNICAL_RETRY' && p.status==='COMPLETED').map(p=>p.retry_of_id)); const p=plays.find(p=>p.status==='FAILED' && ['GUIDE','INITIAL_PROMPT','USER_REPLAY'].includes(p.kind) && !completed.has(p.playback_id) && (p.kind==='USER_REPLAY' || (p.kind==='GUIDE' && !task.guide_complete) || (p.kind==='INITIAL_PROMPT' && !task.initial_prompt_complete))); if(!p) throw Error('没有可恢复的播放'); return play('TECHNICAL_RETRY',p.playback_id);});
bind('stopPlay',()=>{if(player) player.pause();});
bind('recoverPlay',async()=>{for(const p of plays.filter(p=>p.status==='STARTED')) {const stored=localStorage.getItem(key(`receipt:${p.playback_id}`)); const receipt=stored ? JSON.parse(stored) : {attempt_id:p.attempt_id,completed:false}; await finishPlayback(p,receipt.completed);} await refresh();});
bind('redo',async()=>{if(!confirm('确定整题重做？旧提交将退出当前候选。')) return; await post(`${base()}/redo`,{redo_request_id:remember('redo')}); forget('redo'); blob=null; await refresh();});
bind('record',startRecording); bind('stopRecord',()=>recorder.stop());
bind('useFile',async()=>{const f=$('file').files[0]; if(!f || !f.type.startsWith('audio/')) throw Error('请选择具有 audio MIME 类型的音频文件'); blob=f; $('preview').src=URL.createObjectURL(blob); $('audioInfo').textContent=`文件 MIME: ${blob.type}, ${blob.size} 字节`; if(!task.recording_in_progress){await post(`${base()}/recordings`,{attempt_id:attempt,request_id:remember('recording')}); forget('recording');} await refresh();});
bind('upload',async()=>{const row=recs.find(r=>r.status==='STARTED'); if(!row || !blob) throw Error('没有待上传录音'); localStorage.setItem(key(`mime:${row.recording_id}`),blob.type); const result=await api(`${base()}/recordings/${row.recording_id}/content`,{method:'POST',headers:{'X-Attempt-Id':attempt,'Content-Type':blob.type},body:blob}); inform(`录音 ${result.status}：${result.recording_id}`); await refresh();});
bind('cancel',async()=>{const row=recs.find(r=>r.status==='STARTED'); await post(`${base()}/recordings/${row.recording_id}/cancel`,{attempt_id:attempt}); await refresh();});
bind('submit',async()=>{const row=recs.find(r=>r.status==='SAVED'); if(!row) throw Error('没有已保存录音'); const id=remember(`submit:${row.recording_id}`); const result=await post(`${base()}/submissions/qa`,{attempt_id:attempt,recording_id:row.recording_id,submit_request_id:id}); inform(`正式提交 ${result.status}：${result.submission_id}`); await refresh();});
bind('run',async()=>{await post(`/dev/qa/sessions/${sid}/run-offline`,{}); await refresh();});
bind('retryJob',async()=>{const j=jobs.find(x=>x.job_id===$('jobs').value); if(!j) throw Error('请选择失败作业'); const unknown=j.status==='UNKNOWN'; if(!confirm(unknown?'结果未知；确认接受不确定性并授权重试？':'确认授权重试该作业？')) return; await post(`/dev/qa/sessions/${sid}/jobs/${j.job_id}/retry`,{accept_unknown:unknown}); await refresh();});
(async()=>{await sessions(); const last=localStorage.getItem('qa-workbench:last-session'); if(last && [...$('sessions').options].some(o=>o.value===last)) await openSession(last);})().catch(showError);
