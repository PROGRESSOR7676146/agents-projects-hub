import assert from 'node:assert/strict';
import { test } from 'node:test';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// Explicit offline source selection: never fall back to an installed daemon.
if (!process.env.TLIVE_SOURCE) throw new Error('TLIVE_SOURCE is required');
const source = resolve(process.env.TLIVE_SOURCE);
const { startCompanion } = await import(pathToFileURL(resolve(source, 'src/kernel/codex/companion.ts')));
const { PermissionRouter } = await import(pathToFileURL(resolve(source, 'src/kernel/daemon/permission-router.ts')));

const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function fixture() {
  const cards = [], calls = [], replies = [], connections = [];
  const router = new PermissionRouter({
    configuredChats: () => [{ channel: 'telegram', chatId: 'example-chat' }],
    sendToChat: async (_target, card) => { cards.push(card); },
    isMuted: () => false, hasWebClients: () => false, hasLocalAnswerPath: () => false,
    policyDecide: () => ({ decision: 'ask' }),
    renderCard: () => ({ title: 'Example approval', body: 'Fictional input' }),
    graceSec: () => 0,
  });
  const comp = startCompanion({
    connect: async (events) => {
      connections.push(events);
      return {
        call: async (method, params) => {
          calls.push({ method, params });
          if (method === 'thread/loaded/list') return { data: ['large-history-thread'] };
          if (method === 'thread/resume') {
            // Model a history response that cannot finish within the transport
            // budget. Metadata-only resume still subscribes to live requests.
            if (params.excludeTurns !== true) return new Promise(() => {});
            queueMicrotask(() => events.onServerRequest(72, 'item/commandExecution/requestApproval', {
              threadId: 'large-history-thread', turnId: 'turn-2', itemId: 'item-3',
              command: 'example-action', cwd: '/example/project',
            }, (reply) => replies.push(reply)));
            return { cwd: '/example/project', thread: { id: params.threadId, turns: [] } };
          }
          return {};
        },
        notify() {}, close() {},
      };
    },
    permissionRouter: router, onMonitor() {}, onResumePrompt() {}, windowSec: () => 120,
  });
  return { comp, router, cards, calls, replies, connections };
}

test('large-history subscription reaches a card and waits for a decision', async () => {
  const f = fixture();
  try {
    await flush();
    assert.equal(f.cards.length, 1, 'the approval must reach an answer surface');
    assert.equal(f.router.pendingCount(), 1);
    assert.deepEqual(f.replies, [], 'subscription must never approve the operation');
    assert.equal(f.router.answer(f.cards[0].requestId, false), true);
    await flush();
    assert.deepEqual(f.replies, [{ decision: 'decline' }]);
    assert.equal(f.router.pendingCount(), 0);
    assert.equal(f.router.answer(f.cards[0].requestId, true), false);
    assert.deepEqual(f.replies, [{ decision: 'decline' }]);
  } finally { f.comp.stop(); }
});

test('observer changes neither execution policy nor provider configuration', async () => {
  const f = fixture();
  try {
    await flush();
    assert.equal(f.router.pendingCount(), 1);
    assert.deepEqual(f.calls.map((c) => c.method), ['thread/loaded/list', 'thread/resume']);
    const params = f.calls.find((c) => c.method === 'thread/resume').params;
    assert.equal(params.threadId, 'large-history-thread');
    assert.equal(Object.hasOwn(params, 'approvalPolicy'), false);
    assert.equal(Object.hasOwn(params, 'sandbox'), false);
    assert.equal(Object.hasOwn(params, 'model'), false);
    assert.equal(Object.hasOwn(params, 'cwd'), false);
    assert.deepEqual(f.replies, []);
  } finally { f.comp.stop(); }
});

test('disconnect retires the pending card without sending an approval', async () => {
  const f = fixture();
  try {
    await flush();
    assert.equal(f.cards.length, 1);
    const id = f.cards[0].requestId;
    f.connections[0].onClose();
    await flush();
    assert.equal(f.router.pendingCount(), 0);
    assert.equal(f.router.answer(id, true), false);
    assert.deepEqual(f.replies, []);
  } finally { f.comp.stop(); }
});
