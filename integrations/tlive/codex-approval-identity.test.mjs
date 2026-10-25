import {test} from 'node:test';
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
if (!process.env.TLIVE_SOURCE) throw new Error('TLIVE_SOURCE must select verified unpacked source');
const source = resolve(process.env.TLIVE_SOURCE);
const {startCompanion} = await import(pathToFileURL(resolve(source, 'src/kernel/codex/companion.ts')).href);
const {PermissionRouter} = await import(pathToFileURL(resolve(source, 'src/kernel/daemon/permission-router.ts')).href);

export async function rig(t, policy = 'hold') {
  t.mock.timers.enable({apis:['setTimeout','setInterval']});
  let events;
  const cards=[], responses=[], resolved=[], monitor=[], resume=[], prompts=[], nativeResolved=[];
  const router=new PermissionRouter({
    configuredChats:()=>[{channel:'test',chatId:'fictional'}],
    sendToChat:async(_target,card)=>{cards.push(card);},
    isMuted:()=>false,hasWebClients:()=>false,hasLocalAnswerPath:()=>true,
    policyDecide:()=>({decision:'ask'}),renderCard:()=>({title:'Approval',body:'fictional input'}),
    graceSec:()=>10,onResolved:p=>resolved.push(p),
  });
  const rpc={call:async(method)=>method==='thread/loaded/list'?{data:[]}:{},notify(){},close(){}};
  const companion=startCompanion({connect:async e=>{events=e;return rpc;},permissionRouter:router,onMonitor:(...p)=>monitor.push(p),onResumePrompt:p=>resume.push(p),approvalPolicy:()=>policy,onNativePrompt:p=>prompts.push(p),onNativePromptResolved:p=>nativeResolved.push(p),windowSec:()=>600});
  await Promise.resolve();await Promise.resolve();
  t.after(()=>{events.onClose();companion.stop();t.mock.timers.reset();});
  return {events,router,cards,responses,resolved,monitor,resume,prompts,nativeResolved,
    request(id,item='pending',thread='fictional-thread',method='item/commandExecution/requestApproval',turn='fictional-turn'){
      events.onServerRequest(id,method,{threadId:thread,turnId:turn,itemId:item,command:'fictional command'},r=>responses.push({id,...r}));
    },async tick(){t.mock.timers.tick(10000);await Promise.resolve();await Promise.resolve();}
  };
}

test('unrelated resolved request must not erase approval before Telegram grace',async t=>{
 const h=await rig(t);h.request(7);
 h.events.onNotify('serverRequest/resolved',{threadId:'fictional-thread',requestId:6});
 await h.tick();assert.equal(h.cards.length,1);assert.equal(h.router.pendingCount(),1);assert.deepEqual(h.responses,[]);
});

test('unrelated command completion must not erase approval before Telegram grace',async t=>{
 const h=await rig(t);h.request(7);
 h.events.onNotify('item/completed',{threadId:'fictional-thread',turnId:'fictional-turn',item:{id:'earlier-command',type:'commandExecution'}});
 await h.tick();assert.equal(h.cards.length,1);assert.equal(h.router.pendingCount(),1);assert.deepEqual(h.responses,[]);
});


test('exact item completion retires only its own card without answering Codex',async t=>{
 const h=await rig(t);h.request(7,'first');h.request(8,'second');
 h.events.onNotify('item/completed',{threadId:'fictional-thread',turnId:'fictional-turn',item:{id:'first',type:'commandExecution'}});
 await h.tick();assert.equal(h.cards.length,1);assert.equal(h.router.pendingCount(),1);assert.deepEqual(h.responses,[]);
});

test('exact resolved request retires only its own card',async t=>{
 const h=await rig(t);h.request(7,'first');h.request(8,'second');
 h.events.onNotify('serverRequest/resolved',{threadId:'fictional-thread',requestId:7});
 await h.tick();assert.equal(h.cards.length,1);assert.equal(h.router.pendingCount(),1);assert.deepEqual(h.responses,[]);
});

test('numeric and string request IDs are distinct',async t=>{
 const h=await rig(t);h.request(7);
 h.events.onNotify('serverRequest/resolved',{threadId:'fictional-thread',requestId:'7'});
 await h.tick();assert.equal(h.cards.length,1);assert.equal(h.router.pendingCount(),1);
});

test('missing native identities cannot cancel a held card',async t=>{
 const h=await rig(t);h.request(7);
 h.events.onNotify('serverRequest/resolved',{threadId:'fictional-thread'});
 h.events.onNotify('item/completed',{threadId:'fictional-thread',item:{type:'commandExecution'}});
 await h.tick();assert.equal(h.cards.length,1);assert.equal(h.router.pendingCount(),1);
});

test('same request ID in another thread cannot cancel',async t=>{
 const h=await rig(t);h.request(7);
 h.events.onNotify('serverRequest/resolved',{threadId:'other-thread',requestId:7});
 await h.tick();assert.equal(h.cards.length,1);assert.equal(h.router.pendingCount(),1);
});

test('file change request also uses exact native ID',async t=>{
 const h=await rig(t);h.request(7,'file','fictional-thread','item/fileChange/requestApproval');h.request(8);
 h.events.onNotify('serverRequest/resolved',{threadId:'fictional-thread',requestId:7});
 await h.tick();assert.equal(h.cards.length,1);assert.equal(h.cards[0].toolName,'Bash');assert.deepEqual(h.responses,[]);
});

test('genuine turn completion retires pending cards without approval',async t=>{
 const h=await rig(t);h.request(7);
 h.events.onNotify('turn/completed',{threadId:'fictional-thread',turn:{id:'fictional-turn',status:'interrupted'}});
 await h.tick();assert.equal(h.cards.length,0);assert.equal(h.router.pendingCount(),0);assert.deepEqual(h.responses,[]);
});

test('first human answer wins after unrelated command completion',async t=>{
 const h=await rig(t);h.request(7);
 h.events.onNotify('item/completed',{threadId:'fictional-thread',item:{id:'other',type:'commandExecution'}});
 await h.tick();const id=h.cards[0].requestId;
 assert.equal(h.router.answer(id,true),true);assert.equal(h.router.answer(id,false),false);
 await Promise.resolve();await Promise.resolve();
 assert.deepEqual(h.responses,[{id:7,decision:'accept'}]);
});

test('disconnect retires cards without accepting or declining',async t=>{
 const h=await rig(t);h.request(7);h.events.onClose();
 await h.tick();assert.equal(h.cards.length,0);assert.equal(h.router.pendingCount(),0);assert.deepEqual(h.responses,[]);
});

test('file-change approval retains exact item identity for passive visibility',async t=>{
 const h=await rig(t);
 h.request(7,'file-item','fictional-thread','item/fileChange/requestApproval');
 const snapshot=h.router.approvalSnapshot();
 assert.equal(snapshot.truncated,false);
 assert.equal(snapshot.entries.length,1);
 assert.equal(snapshot.entries[0].itemId,'file-item');
});


test('router cancel turn filter is exact including absent ownership',async t=>{
 const h=await rig(t);h.request(7,'first','fictional-thread',undefined,'old-turn');h.request('7','second');
 assert.equal(h.router.cancel({key:'codex:fictional-thread',providerTurnId:'old-turn'}),1);
 assert.equal(h.router.pendingCount(),1);assert.deepEqual(h.responses,[]);
});

test('missing or malformed completed turn cannot affect current cards or presentation',async t=>{
 const h=await rig(t);h.request(7);
 for(const turn of [undefined,{}, {id:''},{id:7},{id:true}]) h.events.onNotify('turn/completed',{threadId:'fictional-thread',turn});
 await h.tick();assert.equal(h.router.pendingCount(),1);assert.equal(h.cards.length,1);
 assert.deepEqual(h.monitor,[]);assert.deepEqual(h.resume,[]);assert.deepEqual(h.responses,[]);
});

test('late old completion preserves newer card, error and completion presentation',async t=>{
 const h=await rig(t);h.request(7,'first','fictional-thread',undefined,'old-turn');
 h.events.onNotify('turn/started',{threadId:'fictional-thread',turn:{id:'fictional-turn'}});
 h.request('7','second');
 h.events.onNotify('error',{threadId:'fictional-thread',turnId:'fictional-turn',error:{message:'current failure'}});
 const before=h.monitor.length;
 h.events.onNotify('turn/completed',{threadId:'fictional-thread',turn:{id:'old-turn',status:'completed'}});
 assert.equal(h.router.pendingCount(),1);assert.equal(h.monitor.length,before);assert.deepEqual(h.resume,[]);
 h.events.onNotify('turn/completed',{threadId:'fictional-thread',turn:{id:'fictional-turn',status:'interrupted'}});
 await h.tick();assert.equal(h.router.pendingCount(),0);assert.equal(h.resume.length,1);
 assert.equal(h.resume[0].outcome,'failed');assert.equal(h.resume[0].errorMessage,'current failure');
 assert.deepEqual(h.responses,[]);
});

test('file completion retires only exact file item and turn',async t=>{
 const h=await rig(t);h.request(7,'same','fictional-thread','item/fileChange/requestApproval','old-turn');
 h.request('7','same','fictional-thread','item/fileChange/requestApproval');h.request(9,'other');
 h.events.onNotify('item/completed',{threadId:'fictional-thread',turnId:'old-turn',item:{id:'same',type:'fileChange'}});
 await h.tick();assert.equal(h.router.pendingCount(),2);assert.equal(h.cards.length,2);
 h.events.onNotify('item/completed',{threadId:'fictional-thread',turnId:'fictional-turn',item:{id:'same',type:'fileChange'}});
 assert.equal(h.router.pendingCount(),1);assert.deepEqual(h.responses,[]);
});

test('native prompt retirement follows latest item and turn ownership',async t=>{
 const h=await rig(t,'notify');h.request(7,'old','fictional-thread',undefined,'old-turn');
 h.events.onNotify('turn/started',{threadId:'fictional-thread',turn:{id:'fictional-turn'}});
 h.request('7','file','fictional-thread','item/fileChange/requestApproval');
 for(const params of [
  {turnId:'fictional-turn',item:{type:'fileChange'}},
  {turnId:'fictional-turn',item:{id:'old',type:'commandExecution'}},
  {turnId:'old-turn',item:{id:'file',type:'fileChange'}},
 ]) h.events.onNotify('item/completed',{threadId:'fictional-thread',...params});
 h.events.onNotify('turn/completed',{threadId:'fictional-thread',turn:{id:'old-turn'}});
 h.events.onNotify('turn/completed',{threadId:'fictional-thread',turn:{}});
 assert.deepEqual(h.nativeResolved,[]);
 h.events.onNotify('item/completed',{threadId:'fictional-thread',turnId:'fictional-turn',item:{id:'file',type:'fileChange'}});
 assert.equal(h.nativeResolved.length,1);assert.deepEqual(h.responses,[]);
});

test('native resolved request ID retains its type',async t=>{
 const h=await rig(t,'notify');h.request(7);
 h.events.onNotify('serverRequest/resolved',{threadId:'fictional-thread',requestId:'7'});
 assert.deepEqual(h.nativeResolved,[]);
 h.events.onNotify('serverRequest/resolved',{threadId:'fictional-thread',requestId:7});
 assert.equal(h.nativeResolved.length,1);
});
