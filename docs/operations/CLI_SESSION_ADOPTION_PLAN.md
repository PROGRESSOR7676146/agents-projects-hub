# Подключение существующей Codex CLI-сессии к Hub

Статус: подробный план реализации; функция ещё не реализована.

Дополнение к scope: после базовой привязки свободной темы выполнить
[замену сессии в существующей теме](CLI_SESSION_REPLACEMENT_PLAN.md).
Ограничение «только свободная тема» ниже относится к базовому режиму без
`--replace-session`; дополнение определяет единственный разрешённый режим
замены. Сначала реализовать и проверить базовый этап, затем замену; оба этапа
передать на review. Не снимать проверки свободной темы для обычного attach.

Цель: разговор, начатый в обычном локальном Codex CLI, можно явно связать с
темой зарегистрированного проекта и затем продолжить через Telegram с тем же
Codex thread ID. Перенос выполняется между завершёнными ходами. Минутной
задержки, summary-вызова и автоматического запуска следующего хода нет.

План предназначен для последовательной реализации агентом с ограниченной
способностью принимать архитектурные решения. Зафиксированные ниже границы
не расширять самостоятельно. После реализации подготовить результат для review
более сильным агентом; не выполнять deployment или live acceptance.

## 1. Место в текущей работе

Сначала должен быть завершён и проверен пакет A из
[плана следующей сессии](NEXT_DEVELOPMENT_SESSION.md): CI и SQLite cleanup.
Эта работа затрагивает `state.py` и migration tests, поэтому выполнять её
параллельно с A в том же checkout нельзя. Не менять текущий handoff агенту A.

После интеграции A создать отдельную development branch от принятого результата,
сохранить пользовательские правки. Не возвращаться автоматически к старому
release tag. Перед реализацией прочитать `AGENTS.md`, private profile и все
обязательные документы в установленном порядке. Private profile не копировать.

Эта задача не включает multi-auth retirement, worker concurrency, универсальный
Session Bridge, новые кнопки, управление процессами CLI или изменение сервисов.
Разрешены исходники, документация и offline tests с fictional fixtures.
Не запускать реальные provider turns, Telegram, live probes, release publication
или миграцию deployment DB. В проверке плана использовались исходники v0.7.0;
следующий агент обязан сверить изменившиеся после A точки входа.

## 2. Продуктовый контракт v1

Поддерживается один узкий маршрут:

```text
Сохранённый разговор в CLI; ход завершён, CLI закрыт
    → локальный preview точной пары «Codex thread / Hub topic»
    → локальный apply: атомарная привязка, writer остаётся local
    → /return в выбранной Telegram-теме
    → следующее обычное сообщение: resume того же Codex thread
    → ответ и дальнейшие /local → CLI → /return
```

`apply` создаёт привязку, но не передаёт незаметно право исполнения Telegram.
Последний шаг передачи использует уже существующий явный `/return`.
Локальный вывод после apply объясняет этот следующий шаг. Команда подключения
сама ничего не отправляет в Telegram и не вызывает модель.

Границы v1:

- Только локальный Codex, зарегистрированный canonical project root и уже
  известная Hub numeric project-group topic. Тему по Telegram API не создавать.
- Только внешний Codex worker: `dispatch_mode=queue`, `queue_runtime=external`,
  Codex включён в external workers, `outbox_runtime=external`. Совместимость с
  inline/embedded не обещать; ниже задан обязательный guard при смене режима.
- Только точный thread ID. Автоматический поиск «последней сессии», выбор по
  названию, импорт JSONL, transcript upload и перенос из другого хранилища не
  входят в v1. Локальный `codex resume` может помочь владельцу найти разговор,
  но команда Hub не должна его запускать ради поиска.
- Только свободная тема: нет существующего содержательного диалога, provider
  binding, satellite sessions, queued/running work или недоставленных сообщений.
  Пустой Codex placeholder, созданный локальными командами Hub, допустим при
  доказанном отсутствии productive/context evidence. Занятую тему не затирать:
  выбрать новую тему того же проекта. `--force` и автоматический `/new` запрещены.
- V1 не поддерживает привязку к worktree lane: worker сейчас использует
  `project.root`, а не произвольный путь lane. Отклонять lane-bound topic и
  несовпадающий source cwd; отдельно зарегистрированный разрешённый Git worktree
  допустим как самостоятельный project root.
- Сохраняется provider thread, а не живая память процесса или полный набор
  возможностей прежнего клиента. Старые сообщения не публикуются в Telegram
  и не записываются автоматически в Hub journal.
- MCP, dynamic tools, plugins, модель, effort и инструкции определяются runtime
  Hub. Не обещать идентичную среду. Preview явно показывает выбранные model/effort
  и предупреждает о различии среды; не показывать секретные конфигурации.
- Передача работает без multi-auth. Активный ход, pending approval и процессы
  других клиентов не переносить и не завершать автоматически.

Один открытый CLI вне Hub невозможно надёжно исключить по `thread/read` другого
app-server. Как в ADR 0011, здесь используется явное утверждение владельца о
закрытии CLI. `idle`/`notLoaded` — дополнительная диагностика, не OS-level lock.
Не объявлять эту границу автоматической защитой от независимого запуска CLI.

## 3. Подтверждённые точки риска в текущем коде

| Точка | Что важно для реализации |
| --- | --- |
| `cli.py::_parser` | Есть project/lane commands, но нет команды adoption. Добавить локальную команду, не Telegram command. |
| `state.py::bind_provider_session` | Простой UPDATE; он не доказывает origin/root, отсутствие старой работы, уникальность binding или writer ownership. Не использовать как готовую безопасную операцию adoption. |
| `state.py::activate_agent` | Может переключить существующий диалог и имеет собственный transaction context. Нельзя вызывать из нового atomic transaction без проверки преждевременного commit. |
| `state.py::enqueue_provider_job` | Проверяет session ID/generation/status внутри transaction. При `requested_provider_session=None` не требует совпадения provider ID. При adoption пустой placeholder надо заменить новым Hub session ID, чтобы stale ingress snapshot не попал в импортированный разговор. |
| `external_worker.py::_execute_codex` | Сейчас любой сохранённый provider ID при `transport_mode=stdio-fallback` приводит к новому `thread/start` с bounded visible context. Для adopted thread этот путь должен быть запрещён. |
| `codex_appserver.py::resume_thread` | Проверяет возвращённые ID/cwd/sandbox/approval, но сам передаёт новый cwd. Это не заменяет проверку исходного stored cwd до resume. |
| `codex_appserver.py::read_completed_turn` | Уже есть `thread/read`, но с полной историей ради recovery. Для adoption нужен отдельный bounded metadata method без полной истории. |
| `supervisor.py::client/start/stop` | Может владеть daemon, переключить транспорт и запускать fallback. Inspector не должен стать вторым владельцем server socket или остановить worker-owned server. |
| `service.py` и `state.py::return_codex_local_writer` | Существующий `/return` даёт нужную детерминированную передачу. Его атомарность и no-model semantics сохраняются. |

Официальные основания: `thread/read` читает сохранённый thread без resume;
`thread/resume` продолжает его по `thread.id`. `thread.sessionId` нельзя
подставлять вместо `thread.id`. Эти API не доказывают, что прежний CLI закрыт,
и не гарантируют доступ к thread из другого runtime/storage.
[OpenAI: App Server](https://learn.chatgpt.com/docs/app-server).

## 4. Интерфейс команды

Добавить подкоманду `session attach-codex` в существующий CLI. Форма:

```text
agents-projects-hub session attach-codex CONFIG
    --project PROJECT_ID
    --chat-id CHAT_ID
    --thread-id TELEGRAM_THREAD_ID
    --codex-thread-id CODEX_THREAD_ID
    [--model MODEL] [--effort EFFORT]
    [--apply --confirm-cli-closed]
    [--json]
```

Пока команда не реализована, эту запись нельзя публиковать как рабочую инструкцию.
В примерах/tests использовать только документированные fictional identities.

Без `--apply` выполняется preview: Hub DB открывается read-only, без создания
файла, migration, routing updates, writer changes или model/catalog discovery.
`--apply` без `--confirm-cli-closed` отклоняется до записи. Пропущенные обязательные
аргументы не угадывать. Произвольные root/socket/CODEX_HOME/transcript paths
команда не принимает. CONFIG — локальный путь к существующей private config.

Ни preview, ни apply не мигрируют DB неявно. Apply работает только с уже
подготовленной текущей schema; неподходящая schema даёт safe precondition error.
Schema migration выполняется обычным контролируемым release/deployment процессом,
а не побочным эффектом открытия через `HubState.open`.

Model/effort берутся из явных аргументов либо defaults Codex в Hub config;
проверяются по существующему cache/config contract без provider model discovery.
Не выводить «сохранена прежняя модель», если исходная модель не установлена.

Ограниченный JSON result: `format_version`, `ok`, `action` (`preview`, `attached`,
`already_attached`), `reason_code`, numeric topic identity, project ID, exact
Codex thread ID, Hub session ID при наличии, target model/effort, writer mode и
`next_action`. Это локальный private результат; реальные IDs не идут в Git,
monitor aggregates или общий operational alert. Не выводить source preview,
заголовок диалога, prompt, history, raw exception, account/config/env dumps.

Exit codes: 0 — successful preview/apply/idempotent repeat; 2 — rejected input,
binding conflict, identity/root/mode/schema precondition; 3 — temporary metadata
transport failure/timeout. Existing argparse behavior может сохранять код 2.
RPC error sanitization должна давать фиксированный safe reason, а не текст сервера.

Preview не выдаёт capability token и не резервирует тему. Apply заново выполняет
проверки; устаревший preview не даёт права на запись. Для exact idempotent repeat
достаточно persisted binding после проверки config/root; недоступность provider
не должна заставлять повторно создавать binding или менять writer mode.

## 5. Metadata inspector и граница доверия

Добавить маленький `codex_session_adoption.py`, отдельно от общего service.
RPC parsing остаётся в `codex_appserver.py`; CLI orchestration — в новом модуле.

1. Загрузить config через существующий режим без чтения Telegram credentials
   (проверить подходящий loader в актуальном `hub_config.py`). Не конструировать
   `ProjectHubService`, Telegram client, monitor или provider worker.
2. Проверить registry allowlist, enabled project, соответствие chat binding,
   существующую numeric topic и root. Проверить настоящий Git toplevel через
   argv subprocess, без shell, после allowlist validation. Учесть canonical
   symlinks, Git worktrees и одинаковые directory names в разных roots.
3. Читать точный thread через тот же доступный backend/store, которым сможет
   пользоваться worker. Допустим существующий configured socket как клиент;
   для official stdio — короткоживущий дочерний `codex app-server` с тем же
   configured executable/runtime context. Не создавать socket daemon, не
   управлять сервисом, не запускать multi-auth и не искать чужие home directories.
4. Операции inspector ограничены handshake и `thread/read(includeTurns=false)`.
   Нельзя `thread/start`, `thread/resume`, `turn/start`, list-models, compaction,
   summarization, thread import/fork или account health probes. Native app-server
   initialization может иметь служебные эффекты самого Codex; read-only claim
   относится к Hub state и provider conversation, а не ко всему filesystem.
5. Все соединения/processes закрываются, включая initialization/read failure.
   Inspector имеет общий bounded deadline, например 10 секунд, а не только
   timeout каждого чтения. Не оставлять background thread/child при timeout.
   Stderr не выводить и не сохранять бесконтрольно.
6. Проверить exact thread ID, persisted/non-ephemeral source, исходный canonical
   cwd и поддерживаемый `modelProvider`. V1 — официальный OpenAI Codex backend;
   неизвестный/custom backend, remote-only session, ephemeral session,
   неподдерживаемая history storage capability — fail closed. Если нужное поле
   отсутствует, не заменять его переданным root или thread ID.
7. Известный active thread/pending approval/system error отклоняется. Для idle
   или notLoaded apply всё равно требует owner assertion. Не читать transcript
   ради угадывания, завершилась ли задача. Unsupported response shape — bounded
   capability error и запрос технического review, не regex по JSONL.

Подтвердить shape metadata по поддерживаемой установленной версии и официальной
схеме без inference. Не изобретать поля по этому плану. Если SDK/protocol не
экспонирует обязательные данные, завершить проверяемую adapter-часть и сообщить
конкретную capability gap, не объявлять adoption безопасной по fake fixtures.

## 6. Сохранение origin и атомарная привязка

Нужен durable признак adopted session, чтобы worker после restart сохранял
запрет смены thread. Не кодировать его в model name, session ID, prompt marker,
runtime_events или другом поле с иной семантикой.

Предпочтительная минимальная схема — одна additive таблица `codex_session_origins`:

| Поле | Контракт |
| --- | --- |
| `session_id` | Primary key, ссылка на конкретный Hub session. |
| `provider_thread_id` | Exact bounded ID; UNIQUE внутри Hub state. |
| `project_id` | Зарегистрированный immutable project ID. |
| `canonical_root` | Проверенный source root; private local state. |
| `model_provider` | Проверенный backend identity, не account identity. |
| `created_at` | UTC timestamp. |

Строка immutable и сохраняется после `/new`/архивации. V1 не даёт перенести
этот же provider thread в другую тему или снять reservation через reset.
Новой schema присвоить следующий номер после актуального baseline; не зашивать
25, если после A номер уже занят. Обновить migrations и schema compatibility.

Новый метод state, например `attach_codex_session`, выполняет один
`BEGIN IMMEDIATE`, без RPC/network/subprocess внутри transaction:

1. Повторно проверяет существующие project/topic identities и точный expected
   placeholder/session snapshot. Config/root proof перепроверяется непосредственно
   перед transaction; это не заявляется атомарной транзакцией filesystem+SQLite.
2. Находит origin row с тем же thread ID. Если это ровно та же привязка и
   параметры — возвращает существующую, ничего не меняя. Если writer уже Telegram,
   не возвращает его в local. Если session архивирована, выдаёт явный conflict;
   повтор apply не воскрешает её.
3. Проверяет, что thread не занят другим active/satellite/archived Hub session,
   immutable queued snapshot или execution checkpoint. Совпадение в старой
   незавершённой/indeterminate работе не лечить новой привязкой. При конфликте
   не менять и не удалять исходную evidence.
4. Проверяет отсутствие productive/context evidence и active/satellite provider
   binding в целевой теме, running dispatch, nonterminal job, pending final,
   progress, control/stop delivery и local/terminal writer. Составить точные
   predicates по актуальной схеме; не сводить всё к `active_session is None`.
   Также отклоняет известную Hub выполняющуюся/ожидающую работу и local/terminal
   writer в других темах того же project/root: adoption не должна пересекаться
   с уже существующим владельцем файлов. Это проверка на момент привязки, не
   новая общая гарантия root-locking для всех будущих turns; не расширять её до
   пакета worker concurrency.
5. Если существует действительно пустой Codex placeholder — архивирует только
   его и создаёт **новый Hub session ID/generation**. Не перепривязывает прежнюю
   строку: ingress мог уже прочитать её до transaction. Stale enqueue должен
   отклониться штатной проверкой session status/generation.
6. Создаёт активную Codex session с source provider ID, выбранными model/effort,
   writer_mode=`local`, origin row и active_agent_id=`codex` у темы в одной
   transaction. Telegram contract остаётся unacknowledged; known context/quota
   telemetry не выдумывать. Старую историю не копировать в журнал.
7. При любом исключении откатывает все изменения. После commit команда только
   печатает результат. Crash до stdout устраняется idempotent repeat.

Не вызывать transaction-owning `activate_agent`/`bind_provider_session`/
`set_writer_mode` последовательно и не рассчитывать, что это одна transaction.
Вынести минимальные SQL primitives или реализовать один узкий state method.

## 7. Worker: доказать продолжение того же разговора

Для сессии с origin row worker перед каждым resume:

- сверяет session/provider ID, registered project, canonical root и origin;
- читает bounded stored metadata через фактически выбранный client и проверяет
  source cwd/provider до передачи overrides в `thread/resume`;
- вызывает только `resume_thread` с exact source thread ID, разрешёнными
  sandbox/approval и Hub Telegram developer instructions;
- использует существующие checkpoint, result/outbox и failure paths.

Для adopted session `stdio-fallback` означает попытку безопасного resume того
же thread через официальный stdio. Запретить `fallback_transfer` с созданием
нового thread и bounded context. Если thread недоступен, writer-locked, root
не совпал или resume вернул другой ID — видимый bounded failure, ноль
`turn/start`, ноль `thread/start`/fork. Не снимать origin и не менять binding.

До попытки `turn/start` такое нарушение относится к preparation failure. После
возможной acceptance действуют существующие indeterminate/recovery правила;
не добавлять productive retry из-за слова «resume» или смены транспорта.
Binding и успешный read не являются подтверждением готовности всех CLI tools.

Незатронутые Hub-created sessions сохраняют текущий fallback contract. Не чинить
в этой задаче все ранее существовавшие transport policies. Зато `/local`,
`/return`, provider/model switches, restart и `/new` не должны случайно снять
origin protection с импортированной сессии; новая session после `/new` имеет
свою отдельную identity и не наследует origin прежней.

Startup/CLI validation обязаны отвергать переход на неподдерживаемый inline/
embedded режим при сохранённых origin rows. Проверить все execution entrypoints,
а не только новый CLI. Старый режим не должен молча проигнорировать новую
политику и создать replacement thread. Никакой автоматической очистки origin
таблицы ради запуска старого режима.

## 8. Обязательная матрица тестов

Сначала failing tests, затем минимальная реализация. Использовать настоящие
temporary Git roots/SQLite и fake app-server/Telegram; не реальные credentials.
Новые test modules ориентировочно `test_codex_session_adoption.py` и
`test_session_adoption_state.py`. Existing fixtures переиспользовать.

| Группа | Сценарии и обязательные assertions |
| --- | --- |
| Input/config | Missing fields, bool вместо numeric ID, invalid/oversized thread ID, disabled/unknown project, unbound/wrong chat/topic, unsupported mode, invalid cached model/effort. Zero state writes/provider turns/Telegram calls. |
| Root | Другой проект с тем же basename, symlink escape, cwd внутри root вместо exact root, missing source cwd, missing Git root, lane-bound topic. Reject до resume; passed cwd не может легализовать неправильный stored cwd. |
| Metadata | Missing/mismatched ID, malformed object/status, active/approval/systemError, ephemeral/custom provider, unavailable backend, timeout/disconnect, wrong storage. Safe error, bounded cleanup, no raw payload. |
| Preview | Missing/old DB не создаётся и не мигрирует; валидная DB остаётся неизменной. Только handshake/read RPC. Placeholder, writer, context cursor и receipts не меняются. |
| Apply | `--confirm-cli-closed` обязателен; attach создаёт exactly one binding и origin, новый session ID при placeholder, writer local, unacknowledged contract, no jobs/results/context injection. |
| Busy target | Каждый nonterminal job state, running dispatch, pending final/progress/stop delivery, existing dialogue/forwarded context/satellite, local or terminal writer. Никакого overwrite или implicit reset. |
| Idempotency | Повтор до/после `/return`, после restart и commit-before-stdout; IDs/generation не меняются, writer не сбрасывается. Archived binding или changed tuple не воскресают и не перезаписываются. |
| Races | Два SQLite connections attach один thread в разные темы; разные threads в одну тему; attach против ingress enqueue, `/new`, model switch. Exactly one compatible transition, no half-binding, stale snapshot не исполняется в adopted thread. Использовать barriers, не sleeps. |
| Transaction fault | Ошибки после placeholder archive, session insert, origin insert и topic update: всё rollback, исходные rows сохранены. |
| First Telegram turn | После `/return` ровно один `thread/resume(source ID)` и один `turn/start(source ID)`; fake provider сохраняет прошлую историю; новый thread/fork/summary не вызываются. Проверить socket и stdio-fallback. |
| Failure certainty | Failed metadata/resume → zero productive starts и safe preparation notice. Disconnect после acceptance → indeterminate/exact-turn recovery без replay и без replacement thread. |
| Lifecycle | `/local` → fake CLI append → close → `/return` → next turn сохраняет thread, после restart тоже. `/new` не удаляет origin evidence старой session, model/provider switch не теряет protection. |
| Privacy/policy | Secret-bearing fake response/error не попадает в stdout/log/Telegram. CLI не читает bot token files. workspace-write; shared on-request; isolated stdio never; никаких new approvals authority или background live probes. |
| Compatibility | Unsupported mode с origin rows fail closed. Старые DB без origin rows и Hub-created sessions ведут себя как до изменения. Migration сохраняет jobs/outbox/indeterminate evidence. |

Race tests должны упражнять существующие admission/reset APIs с независимыми
соединениями, а не только новый метод в изоляции. Если выявлен конфликт
старых transaction boundaries, исправить узкую необходимую границу с regression;
не начинать общий рефакторинг state/router.

## 9. Порядок реализации и проверок

1. **Baseline и contract.** После завершения A проверить Git state и пройти
   canonical validator. Прочитать relevant ADR 0011, 0017, 0019, queue recovery,
   immutable deployment и current schema. Подтвердить metadata protocol shape.
2. **Read-only metadata + preview.** Реализовать typed safe DTO, inspector и
   preview CLI; tests for no mutation/no inference/root/cleanup. Не добавлять
   apply stub, который пишет state до готовности atomic operation.
3. **Durable origin + apply.** Написать migration/state/race tests, реализовать
   atomic binding и protected startup modes. Новый schema номер согласован
   с реальным HEAD. Проверить bounds, private permissions и migration rollback.
4. **Execution protection.** Написать failing stdio-fallback regression,
   затем реализовать adopted same-thread resume и lifecycle guards. Без этого
   CLI attach нельзя объявлять реализованной функцией или публиковать в help
   как завершённый сценарий.
5. **Synthetic end-to-end.** Fake CLI saved thread → preview/apply → existing
   `/return` → queue worker → durable sender → `/local` → fake local continuation
   → `/return`. Проверить identities, отсутствие duplicate invocation и history
   dump. Отдельно crash/restart и targeted failure tests.
6. **Документы и общий gate.** Обновить требования/status/ADR и пройти проверки
   ниже. Подготовить review diff; не deploy и не выполнять live canary.

Основные файлы: `cli.py`, новый `codex_session_adoption.py`, узкое расширение
`codex_appserver.py`, новый state repository либо узкий метод `state.py`,
`migrations.py`, `schema_compatibility.py`, `external_worker.py`, relevant
startup validation и tests. `supervisor.py` менять только если иначе нельзя
обеспечить client-only inspection и корректное cleanup; не вводить новый daemon.

Наряду с новыми тестами запустить relevant existing suites:

```bash
.venv/bin/python -m unittest discover -s tests -p '*session_adoption*.py' -q
.venv/bin/python -m unittest discover -s tests -p 'test_codex_appserver.py' -q
.venv/bin/python -m unittest discover -s tests -p 'test_external_worker.py' -q
.venv/bin/python -m unittest discover -s tests -p 'test_codex_worker.py' -q
.venv/bin/python -m unittest discover -s tests -p 'test_local_transfer.py' -q
.venv/bin/python -m unittest discover -s tests -p 'test_service_integration.py' -q
.venv/bin/python -m unittest discover -s tests -p 'test_provider_job_queue.py' -q
.venv/bin/python -m unittest discover -s tests -p 'test_migrations.py' -q
.venv/bin/python -m unittest discover -s tests -p 'test_release_dry_run.py' -q
.venv/bin/python scripts/validate.py
git diff --check
```

Имена новых modules должны соответствовать командам; не выдавать запуск с нулём
найденных тестов за успех. Full gate включает privacy/history, formatting,
typing, docs, lock и release policy. Не устранять sandbox skips подавлением
тестов и не запускать synthetic live probes. Перед каждым порученным commit:
`.venv/bin/python -m hermes_codex_router.privacy_scan . --history`.
Не push, не tag и не включать чужие изменения в commit.

## 10. Требования, rollback и приёмка

Добавить новую ADR о CLI-origin adoption, сохранив ADR 0011 как основу writer
transfer. Обновить relevant product modules: inbound binding, immutable root,
no-replacement-thread contract, origin persistence, supported execution modes
и acceptance. Уточнить REQ-AUTH-004: existing fallback с новым thread остаётся
для прежнего пути, adopted exact-thread sessions имеют явное исключение.
Нормативные manifest IDs/hashes обновлять только для реально изменённых разделов.
Status помечает repository implementation и отдельную pending live acceptance.

Schema compatibility недостаточна для rollback: старый executable, который
открывает новую таблицу, но игнорирует origin и делает новый thread в stdio,
**не является допустимым rollback**. Нужен clean rollback artifact, который
либо поддерживает origin enforcement, либо до обработки задач отказывает при
наличии origin rows. Проверить manifest gate и offline rollout/rollback
rehearsal. Не стирать binding/evidence и не откатывать живую DB на старый backup
для совместимости: это может потерять принятую работу.

При необходимости подготовить отдельный compatibility checkpoint до feature
activation: он распознаёт новую schema и запрещает execution с adoption state,
которую ещё не поддерживает. Этот путь означает явный отказ работы при rollback,
а не обещание бесперебойного сервиса. Review должен принять именно такую границу.

После repository review отдельная авторизованная live-приёмка должна доказать:
обычный CLI сохраняет нейтральный marker в thread; после закрытия CLI и attach
Telegram продолжает его без подсказки marker в новом prompt; exact thread ID
не меняется; root и provider identity верны; `/local`/`/return` и restart работают.
Marker recall — дополнительная evidence, а не замена exact ID/root checks.
Source/session/tool compatibility проверяется на конкретном runtime. Candidate
SHA и required component revisions фиксируются в private evidence вне Git.

## 11. Когда закончить и что отдать на review

Готово для review, когда выполнен весь v1: preview, atomic apply, existing
`/return`, same-thread socket/stdio execution, restart/race/failure coverage,
schema/policy-compatible rollback и документация. Один CLI binding без worker
protection — незавершённая работа.

Финальный отчёт должен содержать:

- base/final SHA либо base SHA + dirty diff; список изменённых файлов;
- реализованный пользовательский маршрут и явно исключённые случаи;
- фактические test commands/results/skips и evidence level;
- результат exact-thread stdio regression, races и migration/rollback;
- ограничения source storage, tooling, CLI-close assertion и pending live E2E;
- конкретные вопросы reviewer, если реальные protocol/state данные вынудили
  отклониться от этого плана.

Пауза требуется при недоступных обязательных metadata, необходимости менять
другой provider/storage или продуктовый сценарий, пересечении с незавершённой
работой A, либо необходимости deployment authority. Обычные implementation
choices и исправление доказанного дефекта в нужной границе решать самостоятельно.
Не подменять отсутствие evidence новым thread, transcript summary или ручной
правкой deployment SQLite.
