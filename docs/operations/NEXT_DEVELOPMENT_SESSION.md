# Следующая сессия: release gate и владение SQLite-ресурсами

Статус: пакет A реализован и прошёл локальное review; hosted Actions ещё не подтверждены.

Это подробная инструкция и review к пакету A из
[RELIABILITY_PLAN.md](RELIABILITY_PLAN.md). Пакет A реализован в репозитории:
failing regressions подтвердили SQLite failure boundaries, затем минимальные
исправления и reusable workflow прошли полный canonical gate на Python 3.11,
3.12 и 3.13. Следующий агент не должен повторять A; B–F остаются в
плане и не запускаются автоматически. Деплой, публикация, изменение тегов,
live-вызовы и управление сервисами в эту работу не входят.

Review дополнительно исправило порядок cleanup неудачного backup и сохранность
destination, который текущий вызов не смог открыть. Новые CI regressions
отвергают skipped/error-tolerant validation; временный Git fixture выполняет
реальный revision guard с совпадающими и различающимися HEAD/event/annotated tag.

Python 3.13 с ResourceWarning и tracemalloc обнаружил четыре незакрытых
соединения именно в `tests/test_execution_journal.py`; fixtures теперь явно
закрывают соединения, сохраняя транзакционные границы. Аналогичная утечка при
read-only чтении metadata в `alerts.py` подтверждена failing regression на
успех и ошибку SQL и исправлена. Это не доказательство того, что исходные
warnings имели единственный источник в `HubState.open()`.

Разделы ниже сохраняют исходную критику и последовательность выполнения;
описания старых дефектов в них относятся к baseline до пакета A, а не к
текущему состоянию реализации. Уровень приёмки — repository-only: static,
unit/adapter и synthetic fault tests; hosted Actions и live E2E не заявляются.

## 1. Критика исходного плана

Проверка выполнена по исходникам v0.7.0. Новая сессия должна перепроверить
точки входа; номера строк не являются контрактом.

| Проблема | Доказательство и последствие | Исправление плана |
| --- | --- | --- |
| Шесть направлений выданы за одну последовательную сессию. | CI, удаление интеграции, рефакторинг, canary, callbacks и конкурентность имеют разные риски и критерии завершения. Ошибка в конце затруднит проверку всего пакета. | Отдельные пакеты с завершением и проверкой после каждого; следующая сессия только A. |
| «Release gates complete» можно прочитать как защищённую публикацию. | `.github/workflows/release.yml` проверяет metadata и вызывает `gh release create`, не ожидая CI. | Уточнить старую формулировку и поставить release job в зависимость от общей матрицы. |
| CI и локальный gate уже расходятся. | `scripts/validate.py` вызывает `check_release_lock()`, а ручной список стадий в `ci.yml` этого не делает. | Матрица вызывает канонический скрипт целиком. Не создавать третий список проверок. |
| Источник Python 3.13 warning принят за установленный факт. | В `HubState.open()` ошибка после `sqlite3.connect()` оставляет соединение открытым; временный fault-injection опыт подтвердил, что на нём затем работает `SELECT 1`. Но именно исходный warning на 3.13 в этой проверке не воспроизводился. | Начать с отдельного failing regression на владение ресурсом, затем проверить настоящий warning на 3.13. Не приписывать все warnings одному месту. |
| Удаление multi-auth описано как удаление неиспользуемого кода. | Интеграция всё ещё имеет статус Implemented в REQ-AUTH и связана с каталогами, quota, supervisor и ADR 0017 об approval-only Hub sessions. | До удаления определить сохраняемые транспорты, источники telemetry и миграцию конфигурации. Не удалить поддержку shared socket вместе с account rotation. |
| Onboarding может продублировать существующую систему. | Уже есть `acceptance_actor.py`, `e2e-validate`, `e2e-run`, project/registry validation и тесты multi-project isolation. | Добавить недостающую композицию и evidence contract, переиспользуя эти механизмы. Offline success не означает принятую live-интеграцию. |
| Кнопки названы, но протокол решения не определён. | REQ-UX-006 не задаёт источник pending decision; REQ-UX-007 всё ещё описывает задержку, отменённую ADR 0010. | Сначала один конкретный сценарий и переходы состояния, включая crash между выбором и постановкой работы в очередь. |
| Конкурентность сведена к числу worker slots. | `lease_provider_job()` исключает более раннюю незавершённую задачу в той же теме, но не соседнюю тему с тем же root. Worker владеет SQLite connection, client/supervisor и текущей health identity. | Нужны lane/root exclusion и отдельное владение ресурсами слотов; обычный thread pool недостаточен. |
| Извлечение модулей не имеет предмета и границы. | В проверенном дереве `state.py` — 3508 строк, `service.py` — 2458, `handle_update` — 686. Это стоимость изменения, но не самостоятельное доказательство дефекта. | Извлекать один модуль ради конкретного изменения B/C; не начинать с массового разрезания файлов. |

Исходный handoff полезно сохраняет запрет replay неопределённой работы,
test-first, приватность, feature boundaries и отдельную live-приёмку. Эти части
сохраняются. Неподтверждённые утверждения handoff о работающем deployment не
переносятся в репозиторий и не заменяют текущую runtime-проверку.

## 2. Что можно поручить менее сильному агенту

Пакет A подходит: проблема ограничена, архитектура ниже определена, результат
проверяем. От агента требуется аккуратная реализация и проверка, а не выбор
новой модели исполнения. Для E/F сначала нужен отдельный design review более
сильного агента: детальная инструкция не заменяет отсутствующий контракт.

Разрешённые основные файлы:

- `.github/workflows/ci.yml`, `.github/workflows/release.yml` и новый
  `.github/workflows/validate.yml`;
- `src/hermes_codex_router/state.py`, при подтверждённом дефекте также
  `src/hermes_codex_router/migrations.py`;
- соответствующие тесты SQLite и новый узкий тест release workflow;
- `docs/testing/README.md`, `docs/status/PROJECT_STATUS.md` и этот план.

`scripts/validate.py` уже содержит нужные стадии: изменять только при
конкретной доказанной необходимости. Новая dev-зависимость для YAML-проверки
допустима, если существующего инструмента нет; обновлять её lock-файлы штатным
способом. Не добавлять runtime-зависимость ради теста workflow.

Не менять schema 24, provider execution/retry policy, routing, writer leases,
multi-auth integration, Telegram UX, package version или release tags в A.
Не делать общий рефакторинг. Не перезапускать прошлые reliability milestones.

## 3. Вход и фиксация исходного состояния

1. Прочитать `AGENTS.md`, optional private profile и обязательные product
   modules в указанном порядке, затем status, index, security. Private profile
   не копировать в отчёт, файлы, prompts помощников или Git.
2. Прочитать этот план, workflows, `scripts/validate.py`, relevant SQLite code,
   `tests/test_state.py`, `tests/test_migrations.py`,
   `tests/test_schema_20_21_rehearsal.py`, engineering baseline и testing guide.
3. Выполнить read-only команды:

   ```bash
   git status --short
   git branch --show-current
   git rev-parse HEAD
   git rev-parse 'refs/tags/v0.7.0^{commit}'
   git diff --stat
   git diff -- docs/operations/RELIABILITY_PLAN.md
   ```

   Annotated tag имеет собственный object ID: сравнивать именно peeled commit.
   Remote-tracking ref — локальный кэш, не доказательство текущего remote.
4. Сохранить все пользовательские правки. При неизменной release-базе создать
   новую development branch от текущего HEAD с сохранением плановых правок.
   Если уже есть более новая development branch, проверить её изменения и
   продолжить там; не переключаться автоматически на старый tag, не делать
   reset/stash/checkout для очистки. При несовместимых правках запросить решение.
5. Запустить `.venv/bin/python scripts/validate.py`. Если окружение отсутствует,
   установить зависимости штатным способом с соблюдением sandbox permissions.
   Записать исходные failures/skips. Не исправлять несвязанные дефекты молча.

## 4. A1 — воспроизведение и исправление SQLite cleanup

Цель: после неуспешного открытия state не остаётся соединения без владельца;
успешно возвращённый state продолжает владеть рабочим соединением до `close()`.

1. Создать regression на временной SQLite-базе без deployment config. Подменить
   `state.migrate_connection` контролируемой ошибкой после открытия соединения.
   Перехватить созданное **настоящее** соединение, сохранив исходный
   `sqlite3.connect` до mock. После ошибки `HubState.open` проверять, что запрос
   на этом соединении вызывает `sqlite3.ProgrammingError` из-за закрытия.
   Cleanup самого теста закрывает его и при провале assertion.
2. Убедиться, что regression падает на текущем коде именно из-за незакрытого
   ресурса. Не заменять это assertion о тексте реализации или числе `close()`.
3. Исправить владение минимально: пока connection ещё не передан успешно
   созданному `HubState`, любой выход через исключение должен его закрывать и
   сохранять исходную ошибку. Успешное открытие не закрывать в `finally`.
   Проверить также ошибку после connect, но до миграции, например chmod.
   Не менять SQL, ordering, migration transaction и rollback semantics.
4. Проверить аналогичные failure boundaries в `backup_database` и
   `migrate_database`. В частности, открытие destination до защитного блока
   может оставить source открытым; ошибка backup/reconnect после закрытия
   первого connection может быть замаскирована rollback на закрытом объекте.
   Это дополнительные гипотезы: исправлять только после самостоятельного
   failing regression, без общего переписывания migration module.
5. Прогнать узкие проверки:

   ```bash
   .venv/bin/python -m unittest discover -s tests -p 'test_state.py' -q
   .venv/bin/python -m unittest discover -s tests -p 'test_migrations.py' -q
   .venv/bin/python -m unittest discover -s tests -p 'test_schema_20_21_rehearsal.py' -q
   ```

6. Найти доступный Python 3.13 и повторить failure paths с отображением
   ResourceWarning и tracemalloc. Не утверждать, что Python 3.11 проверяет
   поведение warning из 3.13. Если 3.13 недоступен, продолжить A2 и оставить
   этот пункт явно непроверенным до локального/CI запуска, без live-публикации.
7. Regression должен реально падать при возврате утечки. Warning из деструктора
   может стать unraisable exception, поэтому одного `-W error::ResourceWarning`
   недостаточно: предпочтительна явная проверка закрытого real connection.
   Для проверки самого warning собирать его в ограниченной области с GC внутри
   неё; не устанавливать глобальное подавление warnings и не добавлять sleeps.

В Python 3.13 предупреждение выдаётся за отсутствие явного `close()`. Контекст
`with connection` управляет транзакцией и не закрывает connection. Это разные
контракты: [официальная документация SQLite](https://docs.python.org/3.13/library/sqlite3.html).

## 5. A2 — единый gate для CI и публикации

Принятая для этой задачи схема:

```text
CI (push main / pull_request) ────> validate.yml: Python 3.11 / 3.12 / 3.13
Release (push v*) ──> validation ─> та же validate.yml на commit события
                         │
                         └── success всех версий ──> release job
```

1. Добавить reusable workflow `.github/workflows/validate.yml` с
   `on: workflow_call`, `contents: read`, матрицей строк `"3.11"`, `"3.12"`,
   `"3.13"`, `fail-fast: false`. Каждая строка матрицы выполняет полный
   `python scripts/validate.py`; не поддерживать отдельные списки стадий.
2. Сохранить необходимые setup steps из CI: полный checkout истории
   (`fetch-depth: 0`), Python, uv и editable install с dev dependencies.
   Не обновлять версии Actions и dependency policy без отдельной причины.
   Не принимать произвольную shell-команду или ref от вызывающего workflow.
3. Обычный CI вызывает `./.github/workflows/validate.yml` на job level.
   Сохранить triggers и cancel policy обычного CI. Не использовать
   `pull_request_target`, `workflow_run`, поиск последнего зелёного main или
   shell polling статуса другого workflow.
4. Release получает validation job с тем же локальным `uses`. Job публикации
   имеет `needs: validation` и получает `contents: write` только на своём
   уровне. Workflow по умолчанию имеет `contents: read`; GH_TOKEN доступен
   шагу публикации. Не передавать `secrets: inherit` тестовому workflow.
5. И validation, и publication checkout должны использовать commit события,
   без checkout main/двигающейся ветки. Перед публикацией сверить `HEAD` с
   `github.sha` и peeled commit тега, заново проверить release metadata.
   Не интерполировать tag или другие значения события в shell source:
   передавать через env и корректно заключённые аргументы.
6. Нельзя ставить `continue-on-error`, `always()` или другое условие обхода
   успешной validation перед публикацией. Не добавлять общую concurrency group,
   из-за которой reusable workflow отменит вызывающий его release workflow.
   Сохранить `gh release create --verify-tag --generate-notes`, но не запускать
   его локально и не создавать тестовый тег в этом репозитории.
7. Ветка публикации зависит от успеха всей матрицы, а не одной версии Python.
   Различать гарантию данного workflow и защиту от ручной публикации человеком
   с правами: branch/tag protection и GitHub settings этой задачей не меняются.

Локальный reusable workflow берётся из commit вызывающего workflow;
`needs` блокирует downstream job при failed/skipped upstream. Основание:
[GitHub: reuse workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)
и [GitHub: workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

## 6. Проверка release gate без публикации

Добавить узкий структурный contract test workflow, потому что обход gate ведёт
к публикации непроверенного кода. Не писать собственный YAML parser/эмулятор
Actions и не считать поиск подстроки `needs` достаточным доказательством.

Проверить семантически разобранный YAML:

- CI и Release вызывают один reusable workflow; у него все три Python версии
  и каноническая команда без условного пропуска;
- publication job зависит от validation, не имеет разрешающего обхода;
- write permission находится только у publication job;
- checkout не переключает validation/publication на другую ветку;
- privacy/history, release-lock, docs и registry не вырезаны из canonical gate.

Использовать имеющийся YAML tooling, либо добавить одну dev-only зависимость.
Если выбран PyYAML, учитывать YAML 1.1: обычный loader может превратить ключ
`on` в boolean. Тест должен проверять настоящий trigger. По возможности дополнить
`actionlint`, если доступен, но не объявлять его обязательным установленным
инструментом без проверки окружения.

Убедиться на временных копиях данных, что тест обнаруживает удалённый `needs`,
разрешённый обход ошибки и удалённую Python 3.13. Не мутировать рабочий release
workflow ради negative test и не создавать self-contained fake GitHub runner.
Структурный тест доказывает wiring; реальную семантику hosted Actions подтверждает
только запуск Actions. Локальные проверки не называть hosted CI success.

## 7. Завершение и формат отчёта

1. Запустить узкие SQLite/workflow tests, затем полный
   `.venv/bin/python scripts/validate.py`. Выполнить доступные версии Python
   3.11–3.13; явно перечислить недоступные и пропущенные проверки.
2. Проверить `git diff --check` и полный diff. Убедиться, что изменение cleanup
   не стало рефакторингом очереди и schema version по-прежнему 24.
3. Обновить testing guide и PROJECT_STATUS: какие failure boundaries теперь
   проверены и чем защищена публикация. Не изменять нормативные product hashes
   без изменения соответствующего требования. Для A новая ADR не обязательна:
   новый runtime или продуктовая политика здесь не вводятся.
4. Если коммиты входят в действующее поручение, сделать отдельные проверенные
   commits для cleanup и CI; непосредственно перед каждым выполнить
   `.venv/bin/python -m hermes_codex_router.privacy_scan . --history`.
   Не включать чужие файлы через `git add .`, не переписывать release history.
   Если коммиты не поручены, оставить обозримый diff и указать это в отчёте.
5. Финальный отчёт: изменённое поведение; tests и версии Python; exact commit
   либо base SHA + dirty diff; наивысший доказанный evidence level; известные
   ограничения. Hosted CI и live deployment не выдавать за локальный результат.
6. Завершить сессию после A. Следующий полезный шаг — migration/transport contract
   для B, без начала удаления интеграции в оставшееся время.

Критерий успеха A: воспроизведённые failure paths больше не теряют SQLite
connections, успешное открытие и rollback сохраняют поведение; один canonical
gate применяется в CI и tag workflow; publication имеет проверенную зависимость
от всех версий Python. Недоступный 3.13/hosted CI — явно оставшийся acceptance
пункт, а не разрешение написать «всё проверено».

## 8. Условия готовности следующих пакетов

**B — multi-auth retirement.** Составить keep/remove/migrate таблицу для
`hub_config.py`, `codex_accounts.py`, `codex_proxy_health.py`, `provider_catalog.py`,
`catalog_refresh.py`, `monitoring.py`, `diagnostics.py`, `service.py`,
`external_worker.py`, `supervisor.py`, telemetry/alerts, CLI, examples и unit
templates. Для retired keys задать политику и для `null`, и для non-null значений;
отклонять до чтения paths/запуска helpers, без вывода значений. Тестировать
официальный stdio, generic shared socket с approval companion, headless deny,
configured-model fallback, неизвестные telemetry вместо выдуманных quota и
отсутствие вызовов удалённой интеграции. Обновить REQ-AUTH, acceptance/status и
ADR supersession вместе с изменением; historical rationale сохранить. Schema
rollback и config rollback проверять отдельно. Не удалять host credentials/units.

**C — onboarding.** Начать с gap matrix существующих registry/project tools,
`acceptance_actor.py`, `test_acceptance_actor.py`, `test_multi_project_isolation.py`,
`test_project_admin.py`, `test_routing.py`, `test_fault_injection_matrix.py`.
Read-only offline command не должен случайно открыть deployment DB через
мигрирующий `HubState.open`. Synthetic state — только temp fixtures. Report
содержит version, source revision, check ID, evidence level, pass/fail/skipped/
not-run и safe reason code. Root preflight, simulated restart и live restart —
разные evidence. Любой required live check без запуска оставляет live acceptance
неполной. Live path расширяет существующего scoped actor, не заводит нового бота
или framework; fixed scenarios, contamination rejection и fail-fast сохраняются.

**D — extraction.** Обосновать один seam фактической работой B/C. Зафиксировать
public API, transaction owner и ошибки до переноса. Не совмещать extraction с
schema/behavior change; сохранить embedded/external recovery и outbox contracts.

**E — decisions.** Сначала согласовать один сценарий: кто создаёт pending
decision, какой payload frozen, что именно делает Start, как Clarify принимает
следующий текст и как Cancel завершает pending state без остановки чужой работы.
Определить TTL, retention, queued/executing boundary, инвалидирование при `/new`,
смене provider и writer ownership. Проверить callback-to-enqueue atomicity и
crash после commit до Telegram acknowledgement. Opaque callback не содержит
пути/команды; действуют owner/topic/session/generation checks. Лишь после этого
additive schema migration, race tests и compatible rollback. Никакой timer-based
authority, automatic approval или parsing произвольных model commands.

**F — concurrency.** Сначала рассмотреть минимальное расширение: независимые
проекты с разными roots; same-project parallelism — только явные worktree lanes.
Весь provider output потенциально меняет файлы: не определять безопасность по
тексту prompt. Проверять один lane/root lock между разными темами, провайдерами
и local writer; lease fencing, per-slot SQLite/client/process ownership,
heartbeat identity, targeted stop и точное recovery после смерти одного слота.
Задать проверяемую fairness и поведение при уменьшении capacity до 1 во время
работы. FIFO включает существующую границу final outbox; отдельно решить, когда
можно отпустить filesystem lock. Написать contention matrix до реализации.
Schema 24 сохранять только если доказанно достаточно её контракта; не использовать
generic state поле для скрытой новой схемы. Для этого пакета требуется отдельный
архитектурный review, а затем синтетическая fault-приёмка.
