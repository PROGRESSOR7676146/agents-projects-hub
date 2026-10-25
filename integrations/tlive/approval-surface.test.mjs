import {test} from 'node:test';
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const source=process.env.TLIVE_SOURCE;
if(!source) throw new Error('TLIVE_SOURCE required');
const {PermissionRouter}=await import(pathToFileURL(resolve(source,'src/kernel/daemon/permission-router.ts')));
const {ensureCodexAppServer}=await import(pathToFileURL(resolve(source,'src/kernel/codex/spawn.ts')));
function setup(t,send=async()=>({messageId:'42'})) {
 t.mock.timers.enable({apis:['setTimeout']});
 const r=new PermissionRouter({configuredChats:()=>[{channel:'telegram',chatId:'fictional'}],sendToChat:send,isMuted:()=>false,hasWebClients:()=>false,hasLocalAnswerPath:()=>true,policyDecide:()=>({decision:'ask'}),renderCard:()=>({title:'fictional',body:'SECRET INPUT'}),graceSec:()=>10});
 t.after(()=>{r.settleAllPending();t.mock.timers.reset();});
 const request=(id=7,item='item')=>r.requestPermission({key:'codex:thread',cwd:'/home/example',sessionId:'thread',providerRequestId:id,itemIdentity:item,toolName:'Bash',input:{secret:'SECRET INPUT'}});
 return {r,request,async tick(){t.mock.timers.tick(10000);await Promise.resolve();await Promise.resolve();}};
}
test('snapshot is bounded metadata; grace is pending, returned receipt proves sent',async t=>{
 const h=setup(t);void h.request();const a=h.r.approvalSnapshot();assert.equal(a.entries[0].delivery,'pending');assert.equal(JSON.stringify(a).includes('SECRET'),false);
 await h.tick();const b=h.r.approvalSnapshot();assert.equal(b.entries[0].delivery,'sent');assert.equal(b.entries[0].providerRequestId,7);assert.equal(b.entries[0].itemId,'item');assert.equal(b.epoch,a.epoch);
});
test('send in progress is distinct from delivery; unrelated resolution cannot erase it',async t=>{
 let done;const h=setup(t,()=>new Promise(r=>{done=r;}));void h.request();await h.tick();assert.equal(h.r.approvalSnapshot().entries[0].delivery,'sending');
 h.r.cancel({key:'codex:thread',providerRequestId:8});assert.equal(h.r.approvalSnapshot().entries.length,1);done({messageId:'42'});await Promise.resolve();await Promise.resolve();assert.equal(h.r.approvalSnapshot().entries[0].delivery,'sent');
});
for(const [label,send] of [['throw',async()=>{throw Error('SECRET');}],['no reference',async()=>{}],['invalid reference',async()=>({messageId:'0'})]]) test(`ambiguous ${label} is unknown`,async t=>{
 const h=setup(t,send);void h.request();await h.tick();assert.equal(h.r.approvalSnapshot().entries[0].delivery,'unknown');
});
test('exact resolve during send cannot resurrect evidence; new daemon has new epoch',async t=>{
 let done;const h=setup(t,()=>new Promise(r=>{done=r;}));void h.request();await h.tick();const epoch=h.r.approvalSnapshot().epoch;
 h.r.cancel({key:'codex:thread',providerRequestId:7});done({messageId:'42'});await Promise.resolve();assert.equal(h.r.approvalSnapshot().entries.length,0);
 t.mock.timers.reset();const second=setup(t);assert.notEqual(second.r.approvalSnapshot().epoch,epoch);assert.deepEqual(second.r.approvalSnapshot().entries,[]);
});
test('snapshot truncates and rejects oversized identifiers without leaking input',async t=>{
 const h=setup(t);for(let i=0;i<140;i++)void h.request(i,'item'+i);void h.request('x'.repeat(1000));const s=h.r.approvalSnapshot();assert.equal(s.entries.length,128);assert.equal(s.truncated,true);assert.ok(JSON.stringify(s).length<100000);
});
test('attach-only never spawns while unavailable, on reconnect or after client restart',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});let alive=false,spawns=0;const states=[];
 const options={logPath:'/tmp/example.log',attachOnly:true,probe:async()=>alive,hasCodex:()=>true,spawnFn:()=>{spawns++;throw Error('must not spawn');},onStateChange:s=>states.push(s)};
 const a=await ensureCodexAppServer(options);assert.equal(spawns,0);assert.equal(states.at(-1),'degraded');alive=true;t.mock.timers.tick(30000);await Promise.resolve();await Promise.resolve();assert.equal(states.at(-1),'running');a.stop();assert.equal(alive,true);
 const b=await ensureCodexAppServer(options);assert.equal(b.adopted,true);b.stop();assert.equal(alive,true);assert.equal(spawns,0);t.mock.timers.reset();
});

test('client custody stop and restart leave an independently owned process alive',async t=>{
 const {spawn}=await import('node:child_process');
 const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});
 await new Promise((ok,bad)=>{child.once('spawn',ok);child.once('error',bad);});
 t.after(async()=>{child.kill('SIGTERM');await new Promise(done=>child.once('exit',done));});
 let spawnCalls=0;
 const options={logPath:'/tmp/example.log',attachOnly:true,probe:async()=>child.exitCode===null,spawnFn:()=>{spawnCalls++;throw Error('competing server');}};
 const first=await ensureCodexAppServer(options);first.stop();
 assert.equal(child.exitCode,null);
 const second=await ensureCodexAppServer(options);assert.equal(second.adopted,true);second.stop();
 assert.equal(child.exitCode,null);assert.equal(spawnCalls,0);
});
