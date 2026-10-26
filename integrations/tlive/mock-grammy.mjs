export const bots = [];
export class Bot {
  constructor(token) {
    this.handlers = new Map(); this.sent = []; this.edits = []; this.nextId = 1; this.sendFailure = null;
    this.editFailure = null;
    bots.push(this);
    this.api = {
      setMyCommands: async () => {},
      sendMessage: async (chatId, text, opts) => {
        if (this.sendFailure) throw new Error(this.sendFailure);
        const sent = {chatId, text, opts, message_id: this.nextId++};
        this.sent.push(sent); return sent;
      },
      editMessageText: async (chatId, messageId, text, opts) => {
        this.edits.push({chatId, messageId, text, opts});
        if (this.editFailure) throw new Error(this.editFailure);
      },
    };
  }
  on(kind, handler) { this.handlers.set(kind, handler); }
  async start() {}
  async stop() {}
  fire(kind, ctx) { return this.handlers.get(kind)?.(ctx); }
}
