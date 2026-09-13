# Единое задание: подключение CLI-сессии к Telegram

Статус: реализовано в репозитории, офлайн-проверки добавлены; живая приёмка и
production rollback artifact остаются отдельными задачами. Решения и границы:
[ADR 0022](../decisions/0022-explicit-codex-session-adoption.md).

План ниже сохранён как контракт реализации. Проверки находятся в
`test_codex_session_metadata.py`, `test_codex_session_adoption.py` и
`test_session_adoption_{state,ingress,worker,transport,journey,migrations}.py`.
Имитаторы provider/Telegram не являются доказательством живой приёмки.

Это основной документ для следующего исполнителя. Он объединяет два сценария:
подключение сохранённого Codex thread к свободной теме и замена текущей
Codex-сессии в существующей теме. Выполнить этапы последовательно, подготовить
результат для отдельного review. Не начинать другие пункты roadmap.

Технические контракты, на которые опирается задание:

- [Базовое подключение](CLI_SESSION_ADOPTION_PLAN.md) — metadata, CLI,
  atomic binding, origin, worker, schema и rollback.
- [Замена существующей сессии](CLI_SESSION_REPLACEMENT_PLAN.md) — explicit
  replacement, archival, activation boundary, old-context isolation.

Порядок работы определяется этим документом; технические требования обоих
документов обязательны. «Только свободная тема» относится исключительно к
режиму без `--replace-session`. Завершение только свободного attach не является
завершением всего задания.

## 1. Итоговый пользовательский результат

**Свободная тема:** пользователь завершает CLI turn, закрывает CLI, локально
проверяет и подключает сохранённую сессию к теме зарегистрированного проекта,
отправляет в теме `/return`, затем продолжает тот же разговор через Telegram.

**Существующая тема:** пользователь выбирает точную текущую Codex-сессию для
замены. Hub атомарно архивирует её привязку и подключает выбранный CLI thread.
Переписка Telegram и прежняя provider history сохраняются. После `/return`
новые сообщения идут в подключённый разговор. Истории не смешиваются автоматически.

Общие свойства: exact Codex thread ID; один владелец; никаких минутных задержек,
summary-вызовов, фоновой inference, автоматического `/stop` или скрытого создания
нового provider thread. Ошибка до commit оставляет прежнюю привязку целой.

## 2. До начала реализации

1. Прочитать `AGENTS.md`, private profile и обязательные документы в установленном
   порядке. Private profile не копировать в отчёты, fixtures, prompts или Git.
2. Прочитать оба технических контракта выше полностью. Не заменять их чтение
   кратким handoff. Затем читать relevant source и tests, указанные в них.
3. Проверить `git status`, текущую ветку, HEAD и diff. Пакет A1–A2 прошёл отдельное
   локальное review; его результат описан в [плане A](NEXT_DEVELOPMENT_SESSION.md).
   Проверить наличие принятых исправлений в своём baseline: отчёт другой ветки
   не доказывает, что они уже интегрированы сюда.
4. Работать в отдельной development branch и worktree. Если реализация начата
   параллельно review A, интегрировать принятые исправления перед итоговой
   проверкой, внимательно сверив пересечения state/migrations/tests. Сохранить
   чужие изменения; не reset, не возвращаться на старый tag, не присваивать
   авторство существующему diff.
5. Запустить baseline `.venv/bin/python scripts/validate.py`; записать failures,
   skips и фактический SHA. Нерелевантные проблемы не исправлять молча.

Этот этап не требует deployment. Не работать поверх одновременно меняющегося
чужого checkout. Итоговая приёмка требует проверки объединённой ревизии после
интеграции A, а не суммы отдельных отчётов о двух ветках.

## 3. Зафиксированные решения

| Вопрос | Решение для этой задачи |
| --- | --- |
| Провайдер | Только Codex. В занятой теме заменяется активная Codex-сессия; другие providers не закрываются автоматически. |
| Runtime | Внешний Codex worker и внешний outbox. При persisted adoption state неподдерживаемые execution modes должны fail closed. |
| Source | Exact persisted local thread, доступный backend/store worker. Не JSONL import, не копирование transcript и не поиск последней сессии. |
| Root | Exact canonical allowlisted registered Git root. Lane-bound topic отклоняется; путь из Telegram не принимается. |
| Выбор | Локальная команда с preview и явным apply. Подключение само не отправляет Telegram messages и не вызывает модель. |
| Активация | Apply оставляет writer=`local`; существующий `/return` переводит в Telegram. |
| Busy work | Replace отклоняется до завершения очереди, исполнения и доставок. Unresolved indeterminate work не скрывается новой сессией. |
| Старая сессия | Архивируется только Hub binding; provider thread, journal, jobs и results сохраняются. |
| Ошибка resume | Видимый failure без replacement thread, fork, summary или productive replay. |
| Повторы | Exactly one binding/replacement; повтор после `/return` не возвращает writer в local. |
| Environment | CLI history сохраняется у Codex; идентичность MCP/plugins/model/effort не обещается. Target settings видны в preview. |
| Полномочия | Repository implementation и offline tests. Никаких реальных attach, provider turns, Telegram, services, deployment DB migrations, credentials, push, tags или публикации. |

## 4. Этап I — безопасный preview

**Файлы:** новый `codex_session_adoption.py`, узкое расширение
`codex_appserver.py`, CLI parser и новые focused tests.

Добавить будущую команду:

```text
agents-projects-hub session attach-codex CONFIG
    --project PROJECT_ID
    --chat-id CHAT_ID
    --thread-id TELEGRAM_THREAD_ID
    --codex-thread-id CODEX_THREAD_ID
    [--model MODEL] [--effort EFFORT]
    [--replace-session EXPECTED_CURRENT_HUB_SESSION_ID]
    [--apply --confirm-cli-closed] [--json]
```

Сначала реализовать только preview с явным отказом записи до готовности этапа II.
Config/registry/topic/schema проверяются без token reads и без implicit migration.
Inspector использует handshake и bounded `thread/read(includeTurns=false)`;
не запускает model/catalog discovery, resume, новый thread или managed daemon.
Все owned connections/temporary stdio children закрываются при success/failure.

Тесты сначала должны доказать: no writes, no inference, exact source/root,
безопасные ошибки malformed metadata/timeout, правильный cleanup и отсутствие
raw private content в выводе. Fake metadata не считается evidence поддержки
настоящей установленной версии протокола.

**Готовность этапа:** preview выдаёт bounded понятный результат или конкретный
precondition/capability error; сохраняет DB и provider conversation неизменными.

## 5. Этап II — origin, atomic attach и execution protection

**Файлы:** migrations/schema compatibility, узкий state method/repository,
`external_worker.py`, mode guards и соответствующие tests.

1. Спроектировать additive origin table сразу с полями обоих сценариев:
   session ID, unique provider thread ID, project/root/backend, created_at,
   nullable `replaces_session_id`, nullable `activation_message_id` и
   `context_floor_turn_id`. Для forwarded rows предусмотреть source message
   provenance из replacement contract. Номер schema — следующий после реального
   baseline; опубликованные migrations не переписывать.
2. Написать failing tests для пустого attach, duplicate binding и rollback
   каждого промежуточного изменения. Реализовать один `BEGIN IMMEDIATE`.
   Пустой placeholder заменить новым Hub session ID/generation; иначе stale
   ingress snapshot с provider ID=None может попасть в импортированный разговор.
3. Сохранить origin и writer local атомарно. Не вызывать последовательно
   transaction-owning activate/bind/set-writer APIs как замену transaction.
4. До включения apply защитить execution: adopted session всегда resumes exact
   thread, в том числе через `stdio-fallback`. Текущий fallback с `thread/start`
   и bounded context не применять. Проверить stored root до resume overrides.
5. При unsupported mode, missing origin proof или source mismatch отказать до
   productive invocation. Не трогать прежний fallback Hub-created sessions.

**Готовность этапа:** свободная тема подключается без половинчатого state;
worker не может незаметно заменить imported thread; restart сохраняет политику.
CLI binding без этой worker-защиты не считать готовым этапом.

## 6. Этап III — замена и граница старых сообщений

**Файлы:** тот же atomic state operation, `/return`, admission/context/callback
проверки, CLI output и tests. Детали — в replacement contract.

1. `--replace-session` проверяет exact active old session ID. Без флага сохранить
   strict empty-topic behavior. Не поддерживать `--force` и implicit `/new`.
2. Проверить отсутствие busy jobs, pending deliveries, чужого local writer и
   unresolved indeterminate work. Idle history допускается и сохраняется.
3. Archive old + create/bind new + replacement receipt — один commit. Error
   до commit возвращает прежний active binding; repeat после commit не создаёт
   ещё одну generation. Уже совпадающий source/current thread — no-op.
4. Первый `/return` атомарно фиксирует Telegram message boundary, context floor
   и writer change. Запоздавший input до boundary не должен enqueue в новую
   generation. Проверку проводить внутри admission transaction, не только
   до неё в Controller.
5. Старые callbacks не reset/switch новую generation. Reply к старому Codex
   message выбирает текущую Codex-session, не оживляет архивную.
6. Старый forwarded context не inject автоматически. У delayed forwarded row
   новый journal ID, поэтому одного context floor недостаточно: проверять также
   исходный Telegram message ID. Explicit `/context` остаётся явным выбором
   пользователя; чужие provider cursors не изменять.

**Готовность этапа:** existing-topic replacement имеет ту же безопасность, что
пустой attach, и отдельно доказанную границу старых inputs/context.

## 7. Этап IV — жизненный цикл, rollback и финальные проверки

1. Выполнить synthetic journey: fake saved CLI thread → preview/apply → `/return`
   → real queue logic/fake provider → durable sender → `/local` → fake CLI append
   → `/return`. И для free topic, и для replacement; socket и stdio отдельно.
2. Проверить independent SQLite connections: два attach, два replacements,
   attach/replace против ingress, `/new`, model switch и `/return`. Barriers
   вместо sleeps. Assertions: exact identity, no duplicate invocation,
   сохранность old rows, no mixed/half-bound state.
3. Проверить crash до/после commit, до/после turn acceptance, delivery retry и
   restart. Existing uncertainty/recovery policy сохраняется; stop не delete.
4. Проверить migration и rollback. Runtime rollback должен уважать origin и
   activation policy либо явно отказывать до исполнения. Одной совместимости
   schema недостаточно. Не стирать новую evidence ради запуска старого binary.
5. Обновить adoption ADR, relevant product requirements/status, tests/docs и
   только reviewed requirements hashes. Отдельно указать исключение adopted
   exact-thread sessions из прежнего new-thread fallback contract.
6. Запустить narrow tests из обоих contracts и полный canonical validator:

   ```bash
   .venv/bin/python scripts/validate.py
   git diff --check
   ```

   Перед каждым порученным commit — privacy scan с `--history`. Сохранить
   no-network/no-live test discipline и не скрывать skips.

**Готовность всей работы:** оба сценария и их state/worker/admission protection
реализованы, tests проходят, rollback boundary доказана, документы согласованы.
Live portability остаётся отдельной приёмкой конкретного deployment.

## 8. Что принести на review

- Base/final SHA или base SHA + dirty diff, список изменённых файлов.
- Таблица «требование → тест»: exact thread в stdio; atomic replacement;
  delayed input; delayed quote; stale callback; idempotency; crash; rollback.
- Фактические команды/results/skips, версии Python и evidence level.
- Отличия от плана с причиной и тестами; нерешённые protocol/runtime gaps.
- Подтверждение, что live state, сервисы, credentials, Telegram и provider
  inference не использовались для разработки/проверки.

После этого закончить работу и передать на review. Не переходить к deployment,
другим providers, автоматическому session discovery или новым roadmap features.
Если необходимое metadata отсутствует либо меняется доверительная граница,
сообщить точный blocker; не заменять source thread новым разговором и summary.
