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
const telegram = {
  onProtectedCallback(handler) { callback = handler; },
  onProtectedDisconnect(handler) { disconnect = handler; },
  async sendProtectedCard(card) {
    cards.push(card);
    return { messageId: String(cards.length) };
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
assert.equal(result.decision, 'allow');
assert.equal(result.tag,
  mac(resultKey, 'result', capability.epoch, JSON.stringify(first), 'allow', result.actor));
assert.equal((await send(first)).decision, 'deny');
assert.equal((await send(binding({ password: 'example-secret' }))).decision, 'deny');
assert.equal((await send(binding({ content: 'x'.repeat(2600) }))).decision, 'deny');
assert.equal((await send({ ...binding(), toolName: 'Bash' })).decision, 'deny');
const second = send(binding());
await new Promise(done => setImmediate(done));
disconnect();
assert.equal((await second).decision, 'deny');
assert.equal((await send(binding())).decision, 'deny');
console.log('protected host offline smoke: passed');
