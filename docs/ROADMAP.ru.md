# План разработки и тестирования (устаревший v0.1)

> Канонический план Project Hub перенесён в `PROJECT_HUB_SPEC.ru.md`.

## Главная рекомендация

Не дорабатывать старый tmux/TUI bridge. Первый рабочий релиз должен использовать уже установленный Hermes topic mode, Codex app-server и tlive. CLI остаётся интерактивным представлением thread, но не машинным протоколом.

## Этап 0 — foundation (выполнен в текущем проекте)

- выделен отдельный проект вне Babelfish;
- описаны архитектура и модель угроз;
- создан локальный JSON registry contract;
- реализованы canonical path/Git-root/allowlist/policy проверки;
- реализована безопасная argv-сборка для ручного Codex attach;
- добавлены unit tests.

Выход: можно обсуждать интеграцию без запуска bot/gateway и без изменения credentials.

## Этап 1 — Router core без Telegram

Реализовать:

- SQLite migrations и repositories;
- topic/project/session/dispatch state machines;
- per-project lease и bounded queue;
- idempotency/replay receipts;
- structured event redaction;
- `hcr project add/list/disable/validate` (только локально);
- `hcr doctor` и capability report.

Тесты:

- path traversal/symlink escape;
- duplicate IDs/names/roots;
- unsafe sandbox/approval;
- duplicate update exactly once;
- crash between enqueue/start/complete;
- queue ordering, timeout and interrupt;
- state DB permissions.

Gate: 100% переходов state machine покрыты, ни один тест не запускает реальный Codex.

## Этап 2 — Codex app-server adapter

Реализовать typed RPC client, version/capability probe, thread creation/resume, `turn/start`, interrupt и bounded events. Сохранять thread ID и подтверждать возвращённый cwd/project metadata. Добавить fake app-server для contract tests.

Тесты:

- два проекта никогда не разделяют thread;
- resume с другим root отклоняется;
- один turn на lane;
- server disconnect/reconnect;
- malformed/unknown event;
- approval остаётся внешним для router;
- итог и ошибка коррелируют с request ID.

Gate: реальный локальный smoke на двух временных Git-репозиториях; команды только безвредные и read-only. Никакого Telegram.

## Этап 3 — Hermes tool/plugin

Добавить узкий tool `codex_project_router` и инструкции Hermes:

- origin `chat_id/thread_id` берётся из gateway context, не из текста модели;
- tool принимает только project ID, operation, text, request ID;
- неизвестная тема получает инструкцию перейти в System/выполнить локальный bind;
- ответы всегда возвращаются в origin topic;
- штатные `/topic` и Hermes session recovery сохраняются.

Тесты с fake Hermes gateway: auth, origin preservation, duplicate delivery, topic rename/delete, prompt injection в аргументы, недоступный router.

Gate: локальная recorded rehearsal без сети и bot token.

## Этап 4 — Telegram topics canary

После явного разрешения владельца:

1. В BotFather включить Threaded Mode.
2. Запустить Hermes gateway и выполнить `/topic`.
3. Создать только один canary-проект в отдельном временном Git repo.
4. Проверить topic creation/binding/restart.
5. Отправить read-only задачу, затем безопасную запись в canary.
6. Проверить deny и approve через tlive.
7. Перезапустить gateway/router и продолжить тот же Codex thread.

Gate: нет межтопиковой утечки; duplicate update не дублирует turn; timeout не становится согласием; root и thread видимы в `/codex_status`.

## Этап 5 — реальные проекты

Подключать по одному: сначала Babelfish, затем Pythia и остальные. Для каждого:

- локально подтвердить canonical Git root и `AGENTS.md`;
- выбрать policy profile;
- проверить clean/dirty status без изменения;
- создать named topic и показать пользователю binding receipt;
- провести read-only задачу, bounded edit, approval deny/allow, restart/resume;
- включить очередь только после canary.

Никакой автоматической регистрации всех каталогов домашней директории.

## Этап 6 — эксплуатация

- user-level service для router и app-server companion;
- health checks без вложенного model turn;
- локальный kill switch;
- rotation/redaction audit;
- backup только registry/state metadata, не credentials;
- upgrade test matrix Hermes/Codex/tlive/Telegram API;
- уведомление о зависшей lane, rate limit и заполненной очереди.

## Критерии приёмки v1

- сообщение из темы A физически не может попасть в root/session B;
- Codex cwd равен зарегистрированному canonical root;
- несуществующий/disabled проект не стартует;
- повтор update создаёт ровно один turn;
- simultaneous prompts сериализуются;
- Hermes не может auto-approve;
- approval timeout/restart = deny/no action;
- после restart binding и thread восстанавливаются без выбора «последней сессии»;
- пользователь видит project, cwd, thread, queue и последнее событие;
- выключение router не удаляет проектные файлы или Codex history.

## Что можно добавить после v1

- worktree lanes и отдельные темы для параллельных задач;
- краткие voice-команды через Babelfish как ещё один frontend к тому же router contract;
- web dashboard с тем же read-only состоянием;
- project presets для модели/профиля, но без снижения sandbox;
- handoff между Hermes planning session и несколькими Codex lanes;
- автоматическое архивирование завершённых временных тем без удаления sessions.
