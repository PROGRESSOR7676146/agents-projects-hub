// Run only with a verified patched source: TLIVE_SOURCE=/tmp/example-tlive/package
// node --experimental-transform-types --loader ./ts-source-loader.mjs ./host-smoke.mjs
import assert from 'node:assert/strict';
import { createHmac, randomBytes, randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

if (!process.env.TLIVE_SOURCE) throw new Error('TLIVE_SOURCE is required');
const source = resolve(process.env.TLIVE_SOURCE);
const moduleUrl = pathToFileURL(`${source}/src/kernel/daemon/protected-permissions.ts`);
const { ProtectedPermissions } = await import(moduleUrl.href);
const requestKey = randomBytes(32).toString('hex');
const resultKey = randomBytes(32).toString('hex');
const mac = (key, ...data) => createHmac('sha256', Buffer.from(key, 'hex'))
  .update(JSON.stringify(data)).digest('hex');

let callback;
let disconnect;
const cards = [];
const settlements = [];
const telegram = {
  onProtectedCallback(handler) { callback = handler; },
  onProtectedDisconnect(handler) { disconnect = handler; },
  async sendProtectedCard(card) {
    cards.push(card);
    return { messageId: String(cards.length) };
  },
  async settleProtectedCard(card, chatId, messageId) {
    settlements.push({ card, chatId, messageId });
    // A deliberately never-resolving edit must not delay the actual decision.
    return new Promise(() => {});
  },
};
const host = new ProtectedPermissions({
  version: 1, chatId: '1', ownerId: '2', requestKey, resultKey,
}, telegram);
const challenge = randomUUID();
const capability = await host.handle({ kind: 'hub.permission.hello', version: 1, challenge });
assert.equal(capability.tag,
  mac(resultKey, 'capability', challenge, capability.epoch, 1));

function binding(input = { file_path: '/home/example/file' }) {
  return {
    version: 1, requestNonce: randomUUID(), jobId: 'example-job',
    sessionId: randomUUID(), generation: 1, rootDigest: 'a'.repeat(64),
    leaseId: randomUUID(), launchEpoch: randomUUID(), writer: 'telegram',
    toolName: 'Read', input, expiresAt: Date.now() + 5000,
  };
}
function send(value) {
  const payload = JSON.stringify(value);
  return host.handle({ kind: 'hub.permission.request', epoch: capability.epoch,
    payload, tag: mac(requestKey, 'request', capability.epoch, payload) });
}
function actor(messageId, data, chatId = '1') {
  return { callbackId: randomUUID(), userId: '2', isBot: false,
    chatId, chatType: 'private', messageId, data };
}

const first = binding();
const pending = send(first);
await new Promise(done => setImmediate(done));
assert.equal(cards.length, 1);
assert.match(cards[0].body, /Complete tool input:/);
const allowData = cards[0].buttons[0].id;
callback(actor('1', allowData, '3'));
assert.equal(await Promise.race([
  pending.then(() => true), new Promise(done => setTimeout(() => done(false), 10)),
]), false);
callback(actor('1', allowData));
const result = await pending;
assert.equal(settlements.length, 1);
assert.equal(settlements[0].messageId, '1');
assert.equal(settlements[0].card.buttons, undefined);
assert.match(settlements[0].card.title, /^Allowed once/);
assert.equal(result.decision, 'allow');
assert.equal(result.tag,
  mac(resultKey, 'result', capability.epoch, JSON.stringify(first), 'allow', result.actor));
assert.equal((await send(first)).decision, 'deny');
assert.equal((await send(binding({ password: 'example-secret' }))).decision, 'deny');
assert.equal((await send(binding({ content: 'x'.repeat(2600) }))).decision, 'deny');
assert.equal((await send({ ...binding(), toolName: 'Agent' })).decision, 'deny');
// A broad grant is a distinct, authenticated human decision, restricted to Bash.
const ordinary = binding();
const ordinaryWait = send(ordinary);
await new Promise(done => setImmediate(done));
const ordinaryCard = cards.at(-1);
assert.equal(ordinaryCard.buttons.length, 2);
callback(actor(String(cards.length), `hp:${ordinary.requestNonce}:b`));
assert.equal(await Promise.race([
  ordinaryWait.then(() => true), new Promise(done => setTimeout(() => done(false), 10)),
]), false);
callback(actor(String(cards.length), ordinaryCard.buttons[1].id));
assert.equal((await ordinaryWait).decision, 'deny');
const bash = { ...binding({ command: 'git status --short' }), toolName: 'Bash' };
const bashWait = send(bash);
await new Promise(done => setImmediate(done));
const bashCard = cards.at(-1);
assert.equal(bashCard.buttons.length, 3);
assert.match(bashCard.body, /ALL Bash commands/);
const bashActor = actor(String(cards.length), bashCard.buttons[2].id);
callback({ ...bashActor, isBot: true });
callback({ ...bashActor, userId: '9' });
callback({ ...bashActor, messageId: '999' });
assert.equal(await Promise.race([
  bashWait.then(() => true), new Promise(done => setTimeout(() => done(false), 10)),
]), false);
callback(bashActor);
const bashResult = await bashWait;
assert.equal(bashResult.decision, 'allow_bash_until_restart');
assert.match(settlements.at(-1).card.title, /^Bash allowed until restart/);
assert.equal(bashResult.tag, mac(resultKey, 'result', capability.epoch,
  JSON.stringify(bash), 'allow_bash_until_restart', bashActor));
callback({ ...bashActor, data: `hp:${bash.requestNonce}:a` });
assert.equal((await send(bash)).decision, 'deny');
const second = send(binding());
await new Promise(done => setImmediate(done));
disconnect();
assert.equal((await second).decision, 'deny');
assert.equal((await send(binding())).decision, 'deny');
// A send receipt arriving after timeout must also lose its buttons.
let deliver;
const lateEdits = [];
const lateHost = new ProtectedPermissions({
  version: 1, chatId: '1', ownerId: '2', requestKey, resultKey,
}, {
  onProtectedCallback() {}, onProtectedDisconnect() {},
  sendProtectedCard() { return new Promise(resolve => { deliver = resolve; }); },
  async settleProtectedCard(card, chatId, messageId) { lateEdits.push({ card, chatId, messageId }); },
});
const lateBinding = { ...binding(), expiresAt: Date.now() + 50 };
const latePayload = JSON.stringify(lateBinding);
const lateResult = lateHost.handle({ kind: 'hub.permission.request', epoch: lateHost.epoch,
  payload: latePayload, tag: mac(requestKey, 'request', lateHost.epoch, latePayload) });
// The production timer is unref'ed; keep this isolated witness alive until it fires.
const keepAlive = setTimeout(() => {}, 1000);
assert.equal((await lateResult).decision, 'deny');
deliver({ messageId: '99' });
await new Promise(done => setImmediate(done));
assert.equal(lateEdits.length, 1);
assert.equal(lateEdits[0].messageId, '99');
assert.match(lateEdits[0].card.title, /^Closed without approval/);
clearTimeout(keepAlive);
lateHost.close();
console.log('protected host offline smoke: passed');
