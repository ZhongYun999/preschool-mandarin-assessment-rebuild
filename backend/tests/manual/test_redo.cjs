'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const elements = new Map(), values = new Map(), requests = [];
const document = {getElementById(id){if(!elements.has(id)) elements.set(id,{addEventListener(_event,fn){this.click=fn;}, textContent:''}); return elements.get(id);}};
const localStorage = {getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
const fetch = async (url, options={}) => {
 if(url==='/auth/me') return {ok:false,status:401,text:async()=>'{}'};
 if(url.endsWith('/redo')) {requests.push(JSON.parse(options.body)); if(requests.length===1) throw Error('response lost'); return {ok:true,text:async()=>'{"attempt_id":"new"}'};}
 throw Error('unexpected request');
};
const context=vm.createContext({document,localStorage,fetch,crypto:{randomUUID:()=> 'redo-operation'},confirm:()=>true,console});
vm.runInContext(fs.readFileSync(path.join(__dirname,'app.js'),'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await tick();
 vm.runInContext("sid='session';token='child-token';task={session_task_id:'task'};attempt='old-attempt';refresh=async()=>{}",context);
 elements.get('redo').click(); await tick(); await tick();
 assert.equal(requests.length,1);
 vm.runInContext("attempt='new-attempt'",context);
 elements.get('redo').click(); await tick(); await tick();
 assert.deepEqual(requests,[{redo_request_id:'redo-operation',expected_attempt_id:'old-attempt'},{redo_request_id:'redo-operation',expected_attempt_id:'old-attempt'}]);
 assert.equal(values.has('qa-workbench:session:task:pending-redo'),false);
 console.log('Redo retry preserves the original operation and expected attempt');
})().catch(e=>{console.error(e);process.exitCode=1;});
