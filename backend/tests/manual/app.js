'use strict';
const $ = id => document.getElementById(id);
let sid = '', token = '', task = null, attempt = null, plays = [], recs = [], blob = null;
let player = null, activePlay = null, recorder = null, stream = null, jobs = [];
let selectedTask = '', capture = null, uploading = false, starting = false;
let providerState = null;
let currentMode = 'mock';
const pending = new Map();
const pendingKey = () => `${sid}:${task?.session_task_id}:${attempt}`;
const taskQuery = () => `?task_id=${encodeURIComponent(task.session_task_id)}`;
const busy = () => Boolean(capture || uploading || starting || activePlay);
function lockControls() { for (const id of ['create','createTwo','open','tasks','start','guide','initial','replay','retry','record','useFile','upload','cancel','submit','redo','run','authorizePaid','runPaid','retryJob']) $(id).disabled=true; }
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
async function providerStatus(){ providerState=await api('/dev/qa/provider-config'); $('providerKey').value=''; $('providerStatus').textContent=providerState.configured ? `已配置：${providerState.provider} / ${providerState.region} / ${providerState.workspace}` : '未配置或凭据不可用'; if(providerState.region) $('region').value=providerState.region; if(providerState.workspace) $('workspace').value=providerState.workspace; $('disableProvider').disabled=!providerState.credential_id; }
bind('saveProvider',async()=>{const secret=$('providerKey').value; const retain=$('retainKey').checked; try {if(secret && providerState?.credential_id && !confirm('确认轮换为新凭据？旧凭据会保留供已有任务使用。')) return; await post('/dev/qa/provider-config',{region:$('region').value,workspace:$('workspace').value,api_key:secret || null,retain_credential_id:retain ? providerState?.credential_id : null}); await providerStatus();} finally {$('providerKey').value='';} });
bind('disableProvider',async()=>{if(!providerState?.credential_id || !confirm('停用当前凭据？已有任务将无法使用它。')) return; await post(`/dev/qa/provider-config/${providerState.credential_id}/disable`,{}); await providerStatus();});
async function sessions() { const rows = await api('/dev/qa/sessions'); $('sessions').replaceChildren(...rows.map(row => { const o=document.createElement('option'); o.value=row.session_id; o.textContent=`${row.session_id} (${row.status})`; return o; })); if (sid) $('sessions').value=sid; }
async function openSession(id) { if(busy()) throw Error('请先结束当前操作'); sid=id; $('sessions').value=sid; selectedTask=localStorage.getItem(`qa-workbench:task:${sid}`) || ''; localStorage.setItem('qa-workbench:last-session',sid); token=(await post(`/dev/qa/sessions/${sid}/token`,{})).token; blob=null; $('preview').removeAttribute('src'); $('audioInfo').textContent=''; await refresh(); }
async function refresh() {
 if (!sid || !token) return;
 lockControls();
 const state=await api(root()); task=state.tasks.find(t=>t.session_task_id===selectedTask) || state.tasks[0]; selectedTask=task.session_task_id; attempt=task.attempt_id; localStorage.setItem(`qa-workbench:task:${sid}`,selectedTask);
 $('tasks').replaceChildren(...state.tasks.map(t=>{const o=document.createElement('option'); o.value=t.session_task_id; o.textContent=`第 ${t.position} 题：${t.submission_exists ? '已提交' : '未提交'}`; return o;})); $('tasks').value=selectedTask;
 const savedPending=pending.get(pendingKey()); blob=savedPending?.blob || null;
 const content=await api(`${base()}/content`); const current=await api(`${base()}/attempt`); plays=current.playbacks;
 recs=attempt ? await api(`${base()}/recordings`) : [];
 $('session').textContent=`Session: ${sid} | ${state.status} | Attempt: ${attempt || '尚未开始'}`;
 $('state').textContent=JSON.stringify({task, recordings:recs.map(r=>({...r,browser_declared_mime:localStorage.getItem(key(`mime:${r.recording_id}`))})), playbacks:plays},null,2);
 $('prompt').textContent=content.prompt_text;
 const diagnostic=await api(`/dev/qa/sessions/${sid}/diagnostics${taskQuery()}`); jobs=diagnostic.jobs;
 currentMode=diagnostic.mode; $('run').disabled=currentMode!=='mock'; $('authorizePaid').disabled=currentMode!=='qwen'; $('runPaid').disabled=currentMode!=='qwen';
 $('diagnostics').textContent=JSON.stringify(diagnostic,null,2);
 $('jobs').replaceChildren(...jobs.filter(j=>['FAILED','INVALID','UNKNOWN'].includes(j.status)).map(j=>{const o=document.createElement('option'); o.value=j.job_id; o.textContent=`${j.source}: ${j.status}`; return o;}));
 $('retryJob').disabled=currentMode==='qwen' || !$('jobs').options.length;
 for (const [id,action] of Object.entries({start:'START_ATTEMPT',guide:'START_GUIDE',initial:'START_INITIAL_PROMPT',replay:'START_USER_REPLAY',retry:'RETRY_PLAYBACK',record:'START_RECORDING',submit:'SUBMIT_QA',redo:'REDO_ATTEMPT'})) $(id).disabled=!task.allowed_actions.includes(action);
 $('recoverPlay').disabled=!task.playback_in_progress || !!activePlay; $('cancel').disabled=!task.recording_in_progress;
 $('upload').disabled=!blob || !savedPending || !recs.some(r=>r.recording_id===savedPending.id && r.status==='STARTED');
 if(savedPending && recs.some(r=>r.recording_id===savedPending.id && r.status==='SAVED')) {localStorage.setItem(key('selected-recording'),savedPending.id); pending.delete(pendingKey()); blob=null;}
 for (const id of ['create','createTwo','open','tasks','useFile']) $(id).disabled=busy();
 $('run').disabled=busy() || currentMode!=='mock'; $('authorizePaid').disabled=busy() || currentMode!=='qwen'; $('runPaid').disabled=busy() || currentMode!=='qwen';
 if(busy()) { for(const id of ['record','upload','cancel','submit','redo']) $(id).disabled=true; }
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
async function uploadPending() {
 const item=pending.get(pendingKey());
 if(!item || !item.blob) throw Error('没有可重试的原始录音');
 if(uploading) return;
 uploading=true; lockControls(); inform('正在上传录音，请等待服务端确认…');
 try {
  localStorage.setItem(key(`mime:${item.id}`),item.blob.type);
  const result=await api(`${base()}/recordings/${item.id}/content`,{method:'POST',headers:{'X-Attempt-Id':item.attempt,'Content-Type':item.blob.type},body:item.blob});
  if(result.status!=='SAVED') throw Error('服务端尚未确认录音保存');
  localStorage.setItem(key('selected-recording'),item.id); pending.delete(pendingKey()); blob=null; inform(`录音 SAVED：${result.recording_id}`);
 } finally { uploading=false; await refresh(); }
}
async function startRecording() {
 starting=true; lockControls();
 let row;
 try {
  const request=remember('recording'); row=await post(`${base()}/recordings`,{attempt_id:attempt,request_id:request}); forget('recording');
  stream=await navigator.mediaDevices.getUserMedia({audio:true});
  const types=['audio/webm;codecs=opus','audio/ogg;codecs=opus','audio/mp4']; const type=types.find(t=>MediaRecorder.isTypeSupported(t));
  recorder=type ? new MediaRecorder(stream,{mimeType:type}) : new MediaRecorder(stream);
  const ctx={id:row.recording_id,attempt,chunks:[],requested:false,failed:false}; capture=ctx;
  recorder.ondataavailable=e=>{if(e.data.size) ctx.chunks.push(e.data);};
  recorder.onerror=()=>{ctx.failed=true; inform('录音发生异常，未自动上传。请取消录音后重试。');};
  stream.getTracks().forEach(track=>track.addEventListener('ended',()=>{if(!ctx.finished) ctx.failed=true;}));
  recorder.onstop=async()=>{
   const activeStream=stream; ctx.finished=true; stream=null; recorder=null; capture=null;
   activeStream.getTracks().forEach(t=>t.stop());
   if(!ctx.requested || ctx.failed) {inform('录音异常或被中断，未上传；可取消服务端录音。'); await refresh().catch(showError); return;}
   blob=new Blob(ctx.chunks,{type:ctx.mime});
   if(!blob.size || !blob.type.startsWith('audio/')) {inform('录音为空或缺少音频格式，未上传。'); await refresh().catch(showError); return;}
   pending.set(pendingKey(),{id:ctx.id,attempt:ctx.attempt,blob});
   $('preview').src=URL.createObjectURL(blob); $('audioInfo').textContent=`浏览器录音 MIME: ${blob.type}, ${blob.size} 字节`;
   try { await uploadPending(); } catch(e) { await showError(e); }
  };
  ctx.mime=recorder.mimeType; recorder.start(); inform(`录音中：${row.recording_id}`);
 } catch(e) {
  if(stream) stream.getTracks().forEach(t=>t.stop()); stream=null; recorder=null; capture=null;
  if(row) await post(`${base()}/recordings/${row.recording_id}/cancel`,{attempt_id:attempt});
  throw e;
 } finally { starting=false; await refresh(); }
}
bind('create',async()=>{const row=await post(`/dev/qa/sessions?mode=${encodeURIComponent($('mode').value)}`,{}); await sessions(); await openSession(row.session_id);});
bind('createTwo',async()=>{const row=await post(`/dev/qa/sessions?count=2&mode=${encodeURIComponent($('mode').value)}`,{}); await sessions(); await openSession(row.session_id);});
$('tasks').addEventListener('change',()=>{if(busy()){ $('tasks').value=selectedTask; return; } selectedTask=$('tasks').value; $('preview').removeAttribute('src'); $('audioInfo').textContent=''; refresh().catch(showError);});
bind('open',()=>openSession($('sessions').value)); bind('refresh',refresh);
bind('start',async()=>{await post(`${base()}/attempt`,{}); await refresh();});
bind('guide',()=>play('GUIDE')); bind('initial',()=>play('INITIAL_PROMPT')); bind('replay',()=>play('USER_REPLAY'));
bind('retry',()=>{const completed=new Set(plays.filter(p=>p.kind==='TECHNICAL_RETRY' && p.status==='COMPLETED').map(p=>p.retry_of_id)); const p=plays.find(p=>p.status==='FAILED' && ['GUIDE','INITIAL_PROMPT','USER_REPLAY'].includes(p.kind) && !completed.has(p.playback_id) && (p.kind==='USER_REPLAY' || (p.kind==='GUIDE' && !task.guide_complete) || (p.kind==='INITIAL_PROMPT' && !task.initial_prompt_complete))); if(!p) throw Error('没有可恢复的播放'); return play('TECHNICAL_RETRY',p.playback_id);});
bind('stopPlay',()=>{if(player) player.pause();});
bind('recoverPlay',async()=>{for(const p of plays.filter(p=>p.status==='STARTED')) {const stored=localStorage.getItem(key(`receipt:${p.playback_id}`)); const receipt=stored ? JSON.parse(stored) : {attempt_id:p.attempt_id,completed:false}; await finishPlayback(p,receipt.completed);} await refresh();});
bind('redo',async()=>{if(!confirm('确定整题重做？旧提交将退出当前候选。')) return; await post(`${base()}/redo`,{redo_request_id:remember('redo')}); forget('redo'); blob=null; await refresh();});
bind('record',startRecording); bind('stopRecord',()=>{if(!capture || !recorder || recorder.state!=='recording') return; capture.requested=true; lockControls(); recorder.stop();});
bind('useFile',async()=>{const f=$('file').files[0]; if(!f || !f.type.startsWith('audio/')) throw Error('请选择具有 audio MIME 类型的音频文件'); let row=recs.find(r=>r.status==='STARTED'); if(!row){row=await post(`${base()}/recordings`,{attempt_id:attempt,request_id:remember('recording')}); forget('recording');} blob=f; pending.set(pendingKey(),{id:row.recording_id,attempt,blob}); $('preview').src=URL.createObjectURL(blob); $('audioInfo').textContent=`文件 MIME: ${blob.type}, ${blob.size} 字节`; await refresh();});
bind('upload',uploadPending);
bind('cancel',async()=>{const row=recs.find(r=>r.status==='STARTED'); await post(`${base()}/recordings/${row.recording_id}/cancel`,{attempt_id:attempt}); pending.delete(pendingKey()); blob=null; await refresh();});
bind('submit',async()=>{const selected=localStorage.getItem(key('selected-recording')); const row=recs.find(r=>r.status==='SAVED' && r.recording_id===selected) || recs.find(r=>r.status==='SAVED'); if(!row) throw Error('没有已保存录音'); const id=remember(`submit:${row.recording_id}`); const result=await post(`${base()}/submissions/qa`,{attempt_id:attempt,recording_id:row.recording_id,submit_request_id:id}); inform(`正式提交 ${result.status}：${result.submission_id}`); await refresh();});
bind('run',async()=>{await post(`/dev/qa/sessions/${sid}/run-offline${taskQuery()}`,{}); await refresh();});
bind('authorizePaid',async()=>{if(!confirm('确认录音已获授权且不是未经授权的儿童录音？')) return; if(!confirm('确认本次最多一次 ASR 和一次 LLM 的可能收费调用？')) return; await post(`/dev/qa/sessions/${sid}/authorize-paid${taskQuery()}`,{authorized_audio:true,confirm_paid:true}); await refresh();});
bind('runPaid',async()=>{await post(`/dev/qa/sessions/${sid}/run-candidate${taskQuery()}`,{}); await refresh();});
bind('retryJob',async()=>{const j=jobs.find(x=>x.job_id===$('jobs').value); if(!j) throw Error('请选择失败作业'); const unknown=j.status==='UNKNOWN'; if(!confirm(unknown?'结果未知；确认接受不确定性并授权重试？':'确认授权重试该作业？')) return; await post(`/dev/qa/sessions/${sid}/jobs/${j.job_id}/retry${taskQuery()}`,{accept_unknown:unknown}); await refresh();});
(async()=>{await providerStatus(); await sessions(); const last=localStorage.getItem('qa-workbench:last-session'); if(last && [...$('sessions').options].some(o=>o.value===last)) await openSession(last);})().catch(showError);
