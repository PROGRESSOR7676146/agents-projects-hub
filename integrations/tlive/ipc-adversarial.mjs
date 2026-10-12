import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,readFileSync,chmodSync,renameSync,symlinkSync,unlinkSync} from 'node:fs';
import {randomBytes,randomUUID,createHmac,createHash} from 'node:crypto';
import {connect} from 'node:net';
import {setTimeout as delay} from 'node:timers/promises';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const selected = process.env.TLIVE_SOURCE;
if (!selected) throw Error('TLIVE_SOURCE is required');
const fromSource = path => import(pathToFileURL(join(selected,path)).href);
const {bootstrapDaemon} = await fromSource('src/kernel/daemon/bootstrap.ts');
const {TelegramAdapter} = await fromSource('src/adapters/im/telegram.ts');
const {request} = await fromSource('src/kernel/ipc/client.ts');
import {bots} from './mock-grammy.mjs';
const {default:WebSocket} = await fromSource('node_modules/ws/index.js');
const mac = (key,domain,...values) => createHmac('sha256',key).update(JSON.stringify([domain,...values])).digest('hex');
let passes=0;
const test = async (name, fn) => {await fn(); passes++; console.log('PASS',name);};
const until = async (fn) => {for(let i=0;i<300;i++){if(fn())return;await delay(5);} throw Error('wait expired');};
const cfg={version:1,chatId:'12345',ownerId:'67890',requestKey:randomBytes(32).toString('hex'),resultKey:randomBytes(32).toString('hex')};
const reqKey=Buffer.from(cfg.requestKey,'hex'), resKey=Buffer.from(cfg.resultKey,'hex');
const home=mkdtempSync('/tmp/hub-tlive-candidate-offline-');
writeFileSync(join(home,'config.json'),JSON.stringify({web:{enabled:true,bind:'127.0.0.1',port:0},mode:'all',allowedSenders:[{channel:'telegram',userId:cfg.ownerId}],adapters:{telegram:{token:'FICTIONAL_OFFLINE_TOKEN',chatIdAllowList:[cfg.chatId]}},approvals:{autoApprove:'readonly',approvalGraceSec:0}}),{mode:0o600});
writeFileSync(join(home,'protected-permissions.json'),JSON.stringify(cfg),{mode:0o600});
let handle, bot, epoch;

let state={jobId:'example-job',sessionId:randomUUID(),generation:1,rootDigest:createHash('sha256').update('/home/example/project').digest('hex'),leaseId:randomUUID(),launchEpoch:randomUUID(),writer:'telegram'};
const ipc=msg=>request(msg,{socketPath:handle.ipcSocketPath,timeoutMs:3000});
async function boot(){
  handle=await bootstrapDaemon({home,imAdapters:[new TelegramAdapter({token:'FICTIONAL_OFFLINE_TOKEN',allowedChatIds:[cfg.chatId]})],ensureAppServer:async()=>null,desktopNotifier:{notify:async()=>{}}});
  bot=bots.at(-1);
}
async function hello(version=1){
  const challenge=randomUUID();
  const r=await ipc({kind:'hub.permission.hello',version,challenge});
  assert.equal(r.kind,'hub.permission.capability'); assert.equal(r.version,1);
  assert.equal(r.tag,mac(resKey,'capability',challenge,r.epoch,1));
  epoch=r.epoch; return r;
}
function launch(overrides={}){
  const b={version:1,...state,requestNonce:randomUUID(),toolName:'Read',input:{file_path:'/home/example/file'},expiresAt:Date.now()+10000,...overrides};
  const payload=JSON.stringify(b);
  const msg={kind:'hub.permission.request',epoch,payload,tag:mac(reqKey,'request',epoch,payload)};
  const start=bot.sent.length;
  const result=ipc(msg); result.catch(()=>{});
  return {b,payload,msg,result,start};
}
async function card(t){await until(()=>bot.sent.length>t.start);return bot.sent[t.start];}
function callback(c,overrides={}){
  const data=c.opts.reply_markup.inline_keyboard[0][0].callback_data;
  return {callbackQuery:{id:randomUUID(),from:{id:Number(cfg.ownerId),is_bot:false},message:{message_id:c.message_id,chat:{id:Number(cfg.chatId),type:'private'}},data},answerCallbackQuery:async()=>{},...overrides};
}
function fire(c,mutate){const ctx=callback(c);mutate?.(ctx);bot.fire('callback_query:data',ctx);}
function verifyReceipt(t,r){
  assert.equal(r.kind,'hub.permission.result');assert.equal(r.decision,'allow'); assert.equal(r.payload,t.payload);assert.equal(r.epoch,epoch);
  assert.equal(r.tag,mac(resKey,'result',r.epoch,r.payload,r.decision,r.actor));
  assert.equal(r.actor.userId,cfg.ownerId);assert.equal(r.actor.chatId,cfg.chatId);assert.ok(r.actor.callbackId);
  return r;
}
try {
  await boot();
  await test('authenticated version/capability handshake',()=>hello());
  await test('mixed protocol version refuses capability',async()=>assert.equal((await ipc({kind:'hub.permission.hello',version:2,challenge:randomUUID()})).decision,'deny'));
  await test('first authenticated owner callback allows exact synchronous invocation once',async()=>{const t=launch();const c=await card(t);fire(c);const r=await t.result;assert.equal(verifyReceipt(t,r).decision,'allow');fire(c);});
  await test('generic IPC answer cannot approve protected request',async()=>{const t=launch();const c=await card(t);const id=c.opts.reply_markup.inline_keyboard[0][0].callback_data.split(':')[1];await ipc({kind:'hook.permission.answer',requestId:id,approved:true,message:'human'});assert.equal(handle.permissionRouter.answer(id,true,undefined,{answers:{example:'yes'}}),false);fire(c);verifyReceipt(t,await t.result);});
  await test('authenticated dashboard approve/always-tool/ask/reply/inject cannot approve protected request',async()=>{
    const t=launch();const c=await card(t);const id=t.b.requestNonce;
    const u=new URL(handle.webUrl);u.protocol='ws:';u.pathname='/ws/events';
    const ws=new WebSocket(u);await new Promise((res,rej)=>{ws.once('open',res);ws.once('error',rej);});
    for(const action of [{type:'approve',requestId:id,approved:true,alwaysAllowTool:'Bash'},{type:'ask',requestId:id,picks:[],skip:true},{type:'ask',requestId:id,picks:[0],text:'allow'},{type:'reply',requestId:id,text:'allow'},{type:'inject',id:state.sessionId,text:'allow'}])ws.send(JSON.stringify(action));
    let done=false;t.result.then(()=>done=true);await delay(30);assert.equal(done,false);
    ws.close();await new Promise(res=>ws.once('close',res));fire(c);verifyReceipt(t,await t.result);
  });
  await test('legacy IM approve/pause/allowtool/askskip cannot settle protected entry',async()=>{
    const t=launch();const c=await card(t);
    for(const data of [`approve:${t.b.requestNonce}`,`pause:${t.b.requestNonce}`,`allowtool:${t.b.requestNonce}`,`askskip:${t.b.requestNonce}`])fire(c,x=>x.callbackQuery.data=data);
    let done=false;t.result.then(()=>done=true);await delay(25);assert.equal(done,false);fire(c);verifyReceipt(t,await t.result);
  });
  await test('policy trust and always-tool grant cannot approve protected request',async()=>{await ipc({kind:'daemon.set',key:'trust',enabled:true});const t=launch({toolName:'Read',input:{file_path:'/home/example/project/file'}});const c=await card(t);let done=false;t.result.then(()=>done=true);await delay(20);assert.equal(done,false);fire(c);verifyReceipt(t,await t.result);});
  await test('text claiming protected callback is not an approving surface',async()=>{const t=launch();const c=await card(t);bot.fire('message:text',{chat:{id:Number(cfg.chatId)},from:{id:Number(cfg.ownerId)},message:{message_id:9000,text:c.opts.reply_markup.inline_keyboard[0][0].callback_data}});let done=false;t.result.then(()=>done=true);await delay(20);assert.equal(done,false);fire(c);verifyReceipt(t,await t.result);});
  for(const [name,mutate] of [
    ['wrong owner',x=>x.callbackQuery.from.id++],['bot principal',x=>x.callbackQuery.from.is_bot=true],['wrong chat',x=>x.callbackQuery.message.chat.id++],['group chat',x=>x.callbackQuery.message.chat.type='group'],['wrong sent card',x=>x.callbackQuery.message.message_id++],['missing callback id',x=>x.callbackQuery.id=''],['missing message',x=>delete x.callbackQuery.message],['stale nonce',x=>x.callbackQuery.data=`hp:${randomUUID()}:a`]]) {
    await test(name+' refuses allow',async()=>{const t=launch();const c=await card(t);fire(c,mutate);let done=false;t.result.then(()=>done=true);await delay(15);assert.equal(done,false);fire(c);verifyReceipt(t,await t.result);});
  }
  await test('identical concurrent input remains two independent invocations',async()=>{const a=launch();const ca=await card(a);const b=launch();const cb=await card(b);assert.notEqual(ca.opts.reply_markup.inline_keyboard[0][0].callback_data,cb.opts.reply_markup.inline_keyboard[0][0].callback_data);fire(ca);verifyReceipt(a,await a.result);let done=false;b.result.then(()=>done=true);await delay(15);assert.equal(done,false);fire(cb);verifyReceipt(b,await b.result);});
  await test('owner Deny has signed deny with no updated input or permissions',async()=>{const t=launch();const c=await card(t);fire(c,x=>x.callbackQuery.data=c.opts.reply_markup.inline_keyboard[0][1].callback_data);const r=await t.result;assert.equal(r.decision,'deny');assert.equal(r.tag,mac(resKey,'result',r.epoch,r.payload,'deny',r.actor));assert.equal(r.updatedInput,undefined);assert.equal(r.updatedPermissions,undefined);});
  await test('malformed/unauthenticated request cannot create card',async()=>{const start=bot.sent.length;assert.equal((await ipc({kind:'hub.permission.request',epoch,payload:'{}',tag:'0'.repeat(64)})).decision,'deny');assert.equal(bot.sent.length,start);});
  await test('modified input invalidates request MAC',async()=>{const b={...state,version:1,requestNonce:randomUUID(),toolName:'Read',input:{file_path:'/home/example/file'},expiresAt:Date.now()+10000};const original=JSON.stringify(b);const payload=JSON.stringify({...b,input:{command:'echo different'}});const r=await ipc({kind:'hub.permission.request',epoch,payload,tag:mac(reqKey,'request',epoch,original)});assert.equal(r.decision,'deny');});
  await test('authenticated subagent/invalid schema/oversized payload denied without card',async()=>{
    for(const override of [{agentId:'example-child'},{generation:0},{writer:'local'},{rootDigest:'wrong'},{expiresAt:Date.now()+700000},{requestNonce:42}]){
      const t=launch(override);assert.equal((await t.result).decision,'deny');assert.equal(bot.sent.length,t.start);
    }
    const payload='x'.repeat(65537);assert.equal((await ipc({kind:'hub.permission.request',epoch,payload,tag:mac(reqKey,'request',epoch,payload)})).decision,'deny');
  });
  await test('card cannot silently omit bytes, fields or masked secrets from authenticated input',async()=>{
    for(const override of [{toolName:'Write',input:{file_path:'/home/example/project/file',content:'x'.repeat(2600)}},{toolName:'Edit',input:{file_path:'/home/example/project/file',old_string:'x'.repeat(2600),new_string:'new'}},{toolName:'Read',input:{password:'FICTIONAL_SECRET'}},{requestNonce:'x'.repeat(49)}]){
      const t=launch(override);assert.equal((await t.result).decision,'deny');assert.equal(bot.sent.length,t.start);
    }
    const input=Object.fromEntries(Array.from({length:10},(_,i)=>['field_'+i,'value_'+i]));const t=launch({toolName:'Read',input});const c=await card(t);assert.ok(c.text.includes('field_9'));assert.ok(c.text.includes('value_9'));fire(c);verifyReceipt(t,await t.result);
  });
  await test('expired request denied without card',async()=>{const t=launch({expiresAt:Date.now()-1});assert.equal((await t.result).decision,'deny');assert.equal(bot.sent.length,t.start);});
  await test('timeout settles explicit deny and late callback is stale',async()=>{const t=launch({expiresAt:Date.now()+100});const c=await card(t);assert.equal((await t.result).decision,'deny');fire(c);});
  await test('unknown delivery outcome denies with no automatic retry',async()=>{bot.sendFailure='connection reset after send';const t=launch();assert.equal((await t.result).decision,'deny');assert.equal(bot.sent.length,t.start);bot.sendFailure=null;});
  await test('protected parse error has no legacy plain-text retry',async()=>{bot.sendFailure='parse entity error';const t=launch();assert.equal((await t.result).decision,'deny');assert.equal(bot.sent.length,t.start);bot.sendFailure=null;});
  await test('caller disconnect retires request and late callback cannot resurrect it',async()=>{
    const b={version:1,...state,requestNonce:randomUUID(),toolName:'Read',input:{file_path:'/home/example/file'},expiresAt:Date.now()+10000};const payload=JSON.stringify(b);
    const msg={kind:'hub.permission.request',epoch,payload,tag:mac(reqKey,'request',epoch,payload)};
    const start=bot.sent.length;const sock=connect(handle.ipcSocketPath);sock.on('error',()=>{});await new Promise(res=>sock.once('connect',res));sock.write(JSON.stringify(msg)+'\n');await until(()=>bot.sent.length>start);const c=bot.sent[start];sock.destroy();await delay(15);fire(c);assert.equal((await ipc(msg)).decision,'deny');
  });
  await test('duplicate authenticated nonce denied before second card',async()=>{const t=launch();const c=await card(t);const r=await ipc(t.msg);assert.equal(r.decision,'deny');assert.equal(bot.sent.length,t.start+1);fire(c);verifyReceipt(t,await t.result);assert.equal((await ipc(t.msg)).decision,'deny');});
  await test('ordinary policy allow remains unchanged',async()=>assert.deepEqual(await ipc({kind:'hook.permission.request',cwd:'/home/example/project',sessionId:state.sessionId,toolName:'Read',input:{file_path:'/home/example/project/file'}}),{kind:'hook.permission.result',decision:'allow'}));
  await test('protected request does not enter legacy registry/continuation/PTY',async()=>{const before=JSON.stringify(handle.sessions.list());const t=launch();const c=await card(t);assert.equal(JSON.stringify(handle.sessions.list()),before);assert.equal(handle.permissionRouter.pendingCount(),0);fire(c);verifyReceipt(t,await t.result);});
  await test('shutdown denies pending and restart invalidates old launch epoch',async()=>{const t=launch();const c=await card(t);const oldEpoch=epoch;await handle.shutdown();handle=null;assert.equal((await t.result).decision,'deny');await boot();await hello();assert.notEqual(epoch,oldEpoch);assert.equal((await ipc(t.msg)).decision,'deny');fire(c);});
  await test('missing protected config rejects capability, keeps ordinary daemon available',async()=>{
    await handle.shutdown();handle=null;const p=join(home,'protected-permissions.json');renameSync(p,p+'.saved');
    try{await boot();assert.equal((await ipc({kind:'hub.permission.hello',version:1,challenge:randomUUID()})).kind,'error');assert.equal((await ipc({kind:'daemon.status'})).kind,'daemon.status');await handle.shutdown();handle=null;}finally{renameSync(p+'.saved',p);}
  });
  await test('mixed adapter version fails before starting a protected daemon',async()=>{
    const a=new TelegramAdapter({token:'FICTIONAL_OFFLINE_TOKEN',allowedChatIds:[cfg.chatId]});a.onProtectedCallback=undefined;
    await assert.rejects(()=>bootstrapDaemon({home,imAdapters:[a],ensureAppServer:async()=>null}),/capability missing/);
  });
  await test('public permissions or symlink config fails closed before daemon start',async()=>{
    const p=join(home,'protected-permissions.json');chmodSync(p,0o644);
    try{await assert.rejects(()=>boot(),/unsafe protected configuration/);}finally{chmodSync(p,0o600);}
    renameSync(p,p+'.saved');symlinkSync(p+'.saved',p);
    try{await assert.rejects(()=>boot());}finally{unlinkSync(p);renameSync(p+'.saved',p);}
  });
  console.log('TOTAL',passes,'PASS; offline release-source/IPC/Telegram-mock only');
} finally {await handle?.shutdown();}
