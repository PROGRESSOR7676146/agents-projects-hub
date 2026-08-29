# Hermes Project Hub — спецификация и план

Дата актуализации: 2026-08-29. Pythia acceptance пройден, operational hardening
v0.4 реализован.

## Целевая модель

Каждая постоянная Telegram-supergroup соответствует одному локальному проекту
из allowlist. Форумная тема определяется числовой парой
`(chat_id, message_thread_id)` и содержит независимые сессии агентов. Название
темы — изменяемая подпись, а не ключ.

Один агент темы активен. Обычный текст идёт активному агенту; явное упоминание
`@Codex`, `@OpenCode`, `@Antigravity`, `@Hermes` создаёт/продолжает satellite session и не меняет
активного агента. Смена активного агента или модели создаёт новую provider
session с handoff предыдущего контекста. `/new` сбрасывает активную сессию,
`/new all` — все сессии темы.

Один центральный ingress (Codex/Project Hub) получает сообщения группы и
маршрутизирует локально управляемые provider-боты. Настоящий Telegram Reply
адресуется боту-автору; упоминание — указанному агенту; вручную выбранная цитата
остаётся контекстом для активного агента. Provider-боты не запускают параллельные
group poller и поэтому не конкурируют за update.

Каждый завершённый видимый turn записывается в ограниченный журнал темы. При
следующем продуктивном обращении к другому агенту непрочитанный фрагмент журнала
автоматически добавляется в его контекст с явной разметкой адресата. Наблюдение
само по себе не вызывает модель, не тратит токены и не порождает ответ главного.

## Подтверждённая вертикаль

`Telegram topic → Project Hub → Codex app-server → Pythia → Telegram reply`

Работают:

- строгая привязка пилотной группы и темы по числовым `chat_id/thread_id`;
- проверка owner ID, supergroup/forum topic и локального project allowlist;
- отдельная SQLite-сессия Codex для темы;
- настоящий Codex thread и повторное сохранение provider thread ID;
- `gpt-5.6-sol/high`, `workspace-write/on-request`, без auto-approval;
- ответы и expandable metadata: модель, effort, контекст, лимиты и reset time;
- systemd user service с автозапуском и приватными state/socket/token;
- idempotency входящих Telegram message ID;
- первый end-to-end ответ отправлен в тему сообщением №89.

## Writer lease и Terminal

Экспериментально подтверждено: два frontend-клиента не могут одновременно
писать в один Codex thread (`already has an active writer`). Правильная модель:

1. По умолчанию lease принадлежит Telegram bridge.
2. `/terminal` завершает текущий Telegram turn, освобождает provider writer и
   открывает именованную tmux/Windows Terminal вкладку через `codex resume`.
3. Пока действует takeover, бот показывает состояние и не отправляет turns.
4. `/release` закрывает/отсоединяет интерактивный writer, перечитывает thread с
   диска и возвращает lease Telegram.
5. Незавершённый turn никогда не мигрирует между writer-режимами.

Для нескольких одновременно активных тем runtime supervisor должен быть
per-topic либо использовать подтверждённый multiplex API провайдера. Глобальный
рестарт всех Codex-сессий ради одной темы запрещён.

## Команды: статус

| Команда | Пилот | Цель |
| --- | --- | --- |
| `/pilot` | работает | показать/создать привязку темы |
| обычный текст / `@Codex` | работает | продолжить Codex thread |
| `/new` | работает в state | новая активная provider session на следующем turn |
| `/new all` | работает в state | сбросить active + satellites |
| `/model MODEL EFFORT` | реализовано | live validation + новая session + handoff |
| `/agent`, `/agent AGENT` | двусторонний Codex ↔ Hermes | inline-выбор; Codex summary и bounded visible Hermes excerpts |
| `/terminal`, `/release` | реализовано | явная передача writer lease |
| `/status` | реализовано | active agent/session/writer/schema без hidden данных |

Локальный CLI дополнен командами `doctor`, `status`, `backup`, `migrate`,
`project add/list/enable/disable`, `lane create/list/bind/archive/cleanup` и
`monitor`. Telegram по-прежнему не принимает локальные пути и не создаёт
worktree самостоятельно. Binding требует локального подтверждения точной пары
`chat_id:thread_id`; cleanup — архивной lane и подтверждения её точного ID.

## Следующая очередь

1. Провести Telegram E2E-приёмку центрального ingress: Reply автору,
   mention-сабагенту, автоматический видимый контекст главного и отсутствие
   дублей/лишних provider turns.
2. Сохранить ручную смену двух Google-аккаунтов через Hermes как fallback;
   автоматическую ротацию Antigravity отложить до появления стабильного API.
3. Настроить GitHub ruleset и private vulnerability reporting после
   восстановления административной GitHub CLI-сессии.

## Operational hardening v0.4

- CI на Python 3.11–3.13, Ruff, Pyright, unit/integration tests и CodeQL;
- Dependabot для pip и GitHub Actions;
- MIT license, security policy, changelog и acknowledgments upstream-проектам;
- versioned SQLite schema v6, automatic pre-migration backup и integrity check;
- dispatch status для queued/running/completed/failed и диагностический snapshot;
- fail-closed `doctor`, systemd user templates и installer без auto-enable;
- configurable WSL/Linux/macOS/tmux-only terminal launchers;
- локальные project administration, подтверждённый lane binding и безопасный
  cleanup с сохранением branch;
- OpenCode/Antigravity CLI adapters через structured output без auto-approval;
- независимый recovery plane Hermes Telegram + tlive и Codex stdio fallback;
- cooldown-alerts о deployment health, Codex quota/account и зависших dispatch;
- Hermes native runtime footer и публичный `agent:end` visible-turn hook.

## Acceptance Pythia

- два последовательных сообщения продолжают один provider thread — пройдено;
- `/new` меняет provider thread ID только активной сессии;
- ответ чужого пользователя и незарегистрированной группы игнорируется;
- ни один approval не разрешается автоматически;
- после рестарта systemd состояние и offset сохраняются;
- takeover/release не теряет историю и не оставляет двух writers — пройдено
  живым циклом для `Pythia / General`, thread `01a04a01…`;
- при активном Codex обычный текст не будит Hermes, а `@Hermes` остаётся
  отдельной satellite-сессией;
- после `/agent Hermes` первый Hermes turn однократно получает handoff Codex —
  пройдено; обратный Hermes → Codex handoff также пройден;
- Privacy Mode отключён либо ingress-бот назначен администратором, чтобы он
  получал обычный текст; у provider identity bots Privacy Mode может оставаться
  включённым, потому что они не запускают собственные group poller.
