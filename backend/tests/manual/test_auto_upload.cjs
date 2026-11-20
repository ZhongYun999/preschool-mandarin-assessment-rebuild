'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

async function setup() {
 const elements = new Map();
 const document = {getElementById(id) { if (!elements.has(id)) elements.set(id, {disabled:false, textContent:'', options:[], addEventListener(_event, fn){this.click=fn;}, replaceChildren(){}, files:[]}); return elements.get(id); }, createElement(){return {};}};
 const cache = new Map();
 const storage={getItem:k=>cache.get(k) || null,setItem:(k,v)=>cache.set(k,v),removeItem:k=>cache.delete(k)};
 let uploadCalls=0, starts=0, rejectUpload=false, release;
 const track={addEventListener(_name, fn){this.ended=fn;},stop(){}};
 class Recorder {
  static isTypeSupported(t){return t.startsWith('audio/webm');}
  constructor(){this.mimeType='audio/webm;codecs=opus';this.state='inactive';}
  start(){this.state='recording';}
  stop(){this.state='inactive'; this.ondataavailable({data:new Blob(['raw-bytes'],{type:this.mimeType})}); this.finished=this.onstop();}
 }
 const fetch = async (url, options={})=>{
  if(url.endsWith('/provider-config')) return {ok:true,text:async()=>'{"configured":false}'};
  if(url.endsWith('/sessions')) return {ok:true,text:async()=> '[]'};
  if(url.endsWith('/recordings')) {starts++; return {ok:true,text:async()=>JSON.stringify({recording_id:'recording-1'})};}
  if(url.endsWith('/content')) {
   uploadCalls++; assert.equal(options.headers['X-Attempt-Id'],'attempt-1'); assert.equal(options.headers['Content-Type'],'audio/webm;codecs=opus'); assert.equal(await options.body.text(),'raw-bytes');
   await new Promise(resolve=>{release=resolve;});
   if(rejectUpload) return {ok:false,status:503,text:async()=>'{"detail":"offline"}'};
   return {ok:true,text:async()=>'{"recording_id":"recording-1","status":"SAVED"}'};
  }
  throw Error('unexpected request '+url);
 };
 const context=vm.createContext({document,localStorage:storage,fetch,MediaRecorder:Recorder,Blob,URL:{createObjectURL:()=> 'blob:test'},navigator:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[track]})}},crypto:{randomUUID:()=> 'request-1'},console,confirm:()=>true});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'app.js'),'utf8'),context);
 await new Promise(resolve=>setImmediate(resolve));
 vm.runInContext("sid='session-1';token='token';task={session_task_id:'task-1'};attempt='attempt-1';refresh=async()=>{}",context);
 return {context,elements,track,get uploads(){return uploadCalls;},get starts(){return starts;}, fail(v){rejectUpload=v;}, release(){release();}};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function start(env){await vm.runInContext('startRecording()',env.context); return vm.runInContext('recorder',env.context);}
(async()=>{
 const a=await setup(); const first=await start(a);
 vm.runInContext("$('stopRecord').click()",a.context); await tick();
 assert.equal(a.uploads,1); assert.equal(a.elements.get('submit').disabled,true); assert.equal(a.elements.get('upload').disabled,true);
 await vm.runInContext('uploadPending()',a.context); assert.equal(a.uploads,1);
 a.release(); await first.finished; assert.equal(vm.runInContext('pending.size',a.context),0); assert.equal(a.starts,1);

 const b=await setup(); const second=await start(b); b.fail(true);
 vm.runInContext("$('stopRecord').click()",b.context); await tick(); b.release(); await second.finished;
 assert.equal(vm.runInContext('pending.size',b.context),1); assert.equal(b.starts,1);
 b.fail(false); const retry=vm.runInContext('uploadPending()',b.context); await tick(); b.release(); await retry;
 assert.equal(b.uploads,2); assert.equal(b.starts,1); assert.equal(vm.runInContext('pending.size',b.context),0);

 const c=await setup(); const third=await start(c); third.onerror(); vm.runInContext("$('stopRecord').click()",c.context); await tick(); await third.finished; assert.equal(c.uploads,0);
 const d=await setup(); const fourth=await start(d); d.track.ended(); fourth.stop(); await fourth.finished; assert.equal(d.uploads,0);
 console.log('Automatic upload: normal stop, in-flight lock, retry, error and interruption passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
