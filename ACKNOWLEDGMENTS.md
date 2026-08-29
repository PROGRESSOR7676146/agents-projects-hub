# Acknowledgments

Agents Projects Hub is original integration and orchestration code. It does not
vendor source code from the projects below, but it would not exist without
their protocols, command-line interfaces, libraries, and communities.

With sincere thanks to:

- [OpenAI Codex](https://github.com/openai/codex) for the open agent harness,
  persistent threads, app-server interface, sandbox, and approval model. Codex
  is distributed under Apache-2.0.
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research
  for its Telegram gateway, plugin/hook architecture, and topic-scoped agent
  sessions. Hermes Agent is distributed under MIT.
- [Telegram Bot API](https://core.telegram.org/bots/api) for forum topics,
  callback queries, and the transport used by the pilot. Telegram is a service
  and protocol dependency; no Telegram source code is included here.
- [aiohttp](https://github.com/aio-libs/aiohttp) and its maintainers for the
  asynchronous HTTP/WebSocket foundation. aiohttp is distributed under
  Apache-2.0.
- [tmux](https://github.com/tmux/tmux) and the terminal emulator projects used
  by local takeover backends for making one persistent interactive writer
  practical.
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) for its documented
  headless JSON and session-resume interfaces. Gemini CLI is distributed under
  Apache-2.0.
- [OpenCode](https://github.com/anomalyco/opencode) for its open session and CLI
  interfaces. OpenCode is distributed under MIT.
- The Python, SQLite, Git, Ruff, Pyright, and GitHub Actions communities for the
  language and engineering tools that make this small control plane reliable.

Product and project names are used for identification and interoperability.
They remain trademarks of their respective owners. This project is independent
and is not endorsed by OpenAI, Nous Research, Google, Telegram, or Anomaly.

If a future contribution incorporates third-party source rather than merely
calling a documented interface, its copyright and license notice must be added
to this file or to a dedicated `NOTICE` file before release.
