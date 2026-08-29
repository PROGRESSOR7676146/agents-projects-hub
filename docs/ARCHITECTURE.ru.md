# Архитектура Hermes Codex Project Router (устаревшая v0.1)

> Этот документ описывает ранний DM-topic прототип. Актуальная архитектура
> постоянных project supergroups находится в `PROJECT_HUB_SPEC.ru.md`.

**Статус:** implementation-ready proposal, 2026-08-27
**Граница:** отдельный локальный продукт; не компонент Babelfish и не часть памяти/планирования Pythia.

## 1. Что строим

Один Hermes-бот в приватном Telegram-чате работает в Threaded Mode:

- корневая тема `System` остаётся диспетчерской Hermes;
- для каждого разрешённого локального проекта существует тема с тем же отображаемым именем;
- каждую тему однозначно связываем с каталогом проекта и одним активным Codex thread;
- естественное сообщение в проектной теме передаётся в соответствующий Codex thread;
- прогресс, запросы разрешений и итог возвращаются в ту же тему;
- после перезапуска связь восстанавливается из локального состояния.

Название темы удобно человеку, но идентичность задаёт пара чисел `(telegram_chat_id, message_thread_id)`. Переименование темы не должно переключать проект.

## 2. Почему темы, а не отдельные группы

Hermes уже реализует `/topic`: отдельная тема приватного Telegram-чата получает независимую Hermes-сессию, root DM становится системным lobby, а `/topic <session-id>` восстанавливает сессию. Telegram Bot API также адресует тему через `message_thread_id`.

Это позволяет не создавать отдельного бота, токена и группы на каждый проект. Если private threaded mode окажется недоступен конкретному аккаунту/боту, совместимый fallback — одна приватная forum-supergroup, где бот имеет только необходимые права на темы.

## 3. Компоненты

```text
┌──────────────────────────────────────────────────────────┐
│ Telegram: private Hermes bot chat                        │
│ System | Babelfish | Pythia | …                          │
└───────────────────────┬──────────────────────────────────┘
                        │ chat_id + message_thread_id
┌───────────────────────▼──────────────────────────────────┐
│ Hermes Gateway                                             │
│ штатные auth, /topic, доставка ответа в origin topic      │
└───────────────────────┬──────────────────────────────────┘
                        │ typed tool call, not shell
┌───────────────────────▼──────────────────────────────────┐
│ Hermes Codex Project Router                               │
│ registry · topic binding · dedup · queue · audit          │
└───────────────┬────────────────────────┬──────────────────┘
                │ Codex protocol        │ status/approvals
┌───────────────▼───────────────┐  ┌─────▼─────────────────┐
│ Codex app-server             │  │ tlive companion       │
│ thread/start · turn/start    │  │ Telegram approval UX  │
│ interrupt · resume           │  │ live web terminal     │
└───────────────┬──────────────┘  └───────────────────────┘
                │ cwd fixed at thread creation
      ┌─────────▼────────┐  ┌──────────────────┐
      │ /…/Babelfish     │  │ /…/Pythia       │
      │ AGENTS.md + Git  │  │ AGENTS.md + Git │
      └──────────────────┘  └──────────────────┘
```

### Hermes

Hermes — conversational dispatcher. Он понимает просьбу пользователя, но выполняет маршрутизацию только через узкий tool-контракт:

```json
{
  "project_id": "babelfish",
  "operation": "dispatch",
  "text": "Проверь и исправь тесты",
  "request_id": "telegram-update-derived-id"
}
```

В контракте намеренно нет `cwd`, shell-команды, режима sandbox или флага auto-approve.

### Router

Router — детерминированный control plane, а не второй агент. Он:

1. проверяет отправителя и origin topic;
2. разрешает `project_id` через локальный реестр;
3. отклоняет несовпадение зарегистрированного topic binding;
4. дедуплицирует Telegram update/request;
5. сериализует turns одной lane;
6. создаёт/возобновляет Codex thread с зафиксированным `cwd`;
7. публикует bounded progress/final events обратно в origin topic;
8. хранит routing metadata и аудит, но не долговременную память агента.

### Codex

Авторитетная сущность — Codex thread, а не tmux pane. Программное управление выполняется через app-server (`thread/start`, `turn/start`, resume/interrupt и события). Интерактивный Codex CLI остаётся способом локально посмотреть ту же сессию:

```text
codex resume <thread-id> -C <registered-root> \
  --sandbox workspace-write --ask-for-approval on-request
```

Router формирует argv массивом без shell. Параметр `-C` делает проект рабочим корнем, поэтому Codex читает его `AGENTS.md` и получает запись только в границах разрешённого workspace. Запуск Codex из домашнего каталога не является ошибкой сам по себе, но для изоляции и правильного контекста каждая проектная сессия обязана получать точный `-C`.

### tlive

tlive остаётся адаптером наблюдения и удалённых разрешений Codex. В режиме `full` запрос можно решить из Telegram или локального TUI; действует first-answer-wins. Router не создаёт второй approval channel, не переводит слова «да»/«approve» в нажатие клавиши и не подтверждает запрос от имени пользователя.

## 4. Реестр и состояние

### Статическая конфигурация

Редактируется только локально:

```text
Project {
  project_id          stable machine id
  display_name        human label
  topic_name          desired Telegram title
  real_root           canonical allowlisted Git root
  sandbox             read-only | workspace-write
  approval_policy     on-request
  enabled             bool
}
```

Telegram-команда не может добавить путь. Первичный bootstrap делается локально через будущую команду `hcr project add`; из Telegram разрешается только синхронизация уже одобренных записей.

### Динамическое состояние SQLite

```text
topic_binding(project_id PK, chat_id, thread_id UNIQUE, observed_title,
              created_at, verified_at)
codex_session(session_id PK, project_id, codex_thread_id UNIQUE,
              state, created_at, last_turn_at)
dispatch(request_id PK, telegram_update_id UNIQUE, project_id,
         codex_thread_id, status, prompt_sha256, created_at)
event(event_id PK, request_id, type, bounded_payload, created_at)
```

Файл создаётся с правами `0600`. Текст запроса и полный ответ не обязаны дублироваться в router DB: достаточно идентификаторов, digest, статуса и bounded error. История остаётся в Hermes/Codex согласно их политикам.

## 5. Жизненный цикл

### Bootstrap

1. Локально создать Hermes project (`hermes project create ...`) при необходимости.
2. Добавить Git-root в локальный router registry и провалидировать.
3. В BotFather включить Threads Settings / Threaded Mode.
4. Запустить Hermes gateway и в приватном чате выполнить `/topic`.
5. `hcr sync-topics` создаёт или связывает темы только для enabled-проектов.
6. Пользователь подтверждает таблицу `project → realpath → topic`.

### Первое сообщение

1. Telegram/Hermes передаёт origin metadata и текст.
2. Router находит binding по numeric thread ID.
3. Если active thread отсутствует, app-server создаёт его с `cwd=real_root` и фиксированной policy.
4. Router ставит turn в очередь и сразу отвечает `Принято · Babelfish · queued/running`.
5. Codex events сворачиваются в редкие status updates; final отправляется полностью в допустимых пределах Telegram.

### Повторное сообщение

- если turn не запущен, сообщение добавляется следующим turn;
- если turn идёт, по умолчанию сообщение ставится в очередь, а не инъецируется в текущий reasoning;
- явная `/interrupt <текст>` требует отдельного подтверждения и вызывает протокольный interrupt;
- duplicate Telegram update возвращает прежний receipt и не создаёт второй turn.

### Перезапуск

Router сверяет SQLite с Hermes topic binding и доступными Codex threads. Сессия возобновляется только если сохранённый root совпадает с текущим canonical root проекта. Иначе lane переходит в `NEEDS_LOCAL_REBIND`.

## 6. Пользовательские команды

В `System`:

- `/projects` — проекты, topic, cwd, состояние Codex;
- `/sync` — темы для уже разрешённых локальных проектов;
- `/doctor` — Hermes/tlive/app-server/registry без изменения состояния;
- `/pause <project>` / `/unpause <project>` — приём новых turns;
- `/shutdown-router` — запрос с явным подтверждением.

В теме проекта:

- обычный текст — следующая команда Codex;
- `/status` — cwd, thread id, очередь, время последнего события;
- `/new` — новый Codex thread после подтверждения;
- `/sessions` и `/resume <id>` — только sessions того же project_id/root;
- `/stop` — interrupt текущего turn, но не удаление session;
- `/terminal` — локальный/ tlive live-terminal link, если включён;
- `/archive` — архивировать lane после подтверждения.

Имена команд следует реализовать в router/Hermes tool, не перехватывая штатные `/topic` и `/status` Hermes без namespace. Практический вариант — `/codex_status`, `/codex_new`, `/codex_stop`.

## 7. Параллельная работа

MVP: одна lane и один активный turn на проект. Это предотвращает конфликтующие правки в одном worktree. Позже параллельная задача создаёт отдельный Git worktree и отдельную тему `Babelfish · <task>`; root новой lane снова локально регистрируется и имеет собственный Codex thread. Несколько агентов не пишут в один worktree одновременно.

## 8. Что не переносим из старого HCB

- screen scraping Codex TUI;
- `tmux send-keys` для текста и разрешений;
- распознавание approval prompt регулярными выражениями;
- пересылку hidden reasoning;
- общий JSONL inbox/outbox для всех сессий;
- отдельный Telegram token/gateway;
- строковую сборку shell-команд.

tmux допустим только как необязательная локальная оболочка для наблюдения, но не как API.

## 9. Внешние интерфейсы

- Telegram Bot API: forum/private topics и `message_thread_id`.
- Hermes Gateway: существующий `/topic`, origin-topic delivery и новый narrow router tool.
- Codex app-server: единственный программный session/turn/event API.
- Codex CLI: ручной attach/diagnostics через `resume` и `-C`.
- tlive: существующая companion-интеграция для progress/approval/live terminal.

Необходимо зафиксировать версии и добавить capability probe: если нужный app-server method отсутствует, daemon не должен откатываться к TUI scraping.
