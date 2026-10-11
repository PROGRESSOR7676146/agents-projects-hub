# Claude Code: чтение и правка проекта через Hub

Дата пересмотра: 2026-10-11. Статус: первый файловый этап принят;
native/Telegram приёмка остаётся открытой. Владелец решения: владелец репозитория.
Решение об объёме: [ADR 0067](../decisions/0067-claude-editor-first.md).
Последняя проверенная исходная основа: `112c503cd9fb29d5eaf378c0fdf1d8044324af41`.
Это source/test checkpoint, не deployment или live acceptance.

Путь сохранён для существующих ссылок. Этот план заменяет прежний порядок
полной Claude parity. Роли ведущего/советника сняты по
[ADR 0065](../decisions/0065-retire-hub-lead-advisor.md) и не возвращаются.

## Принятый продуктовый объём

Первый результат — Claude читает, ищет и правит файлы зарегистрированного
проекта из Telegram. Используем существующий CLI worker и защищённый
Read/Glob/Grep/Write/Edit, точную Hub-owned сессию, очередь, один writer,
видимые ожидания/ошибки/результаты, stop и восстановление сохранённых результатов.
Полномочия и подписочный маршрут определены один раз в
[REQ-AUTH-009 и REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Text-only остаётся default до прохождения существующих live gates.

Это ограниченный редактор: shell, запуск тестов/сборки, Git-команды, skills,
MCP и дочерние агенты не входят в этап. Конфигурируемый выбор модели/effort
сохраняется; недоказанные effective effort, entitlement и quota остаются неизвестными.
Текущий файловый wrapper требует обычный Git root и не поддерживает linked
worktree с `.git`-файлом. Ограничение проверяется до установки, а не после отказа.

Claude `/local` → `/return`, подключение старых сессий, включение изображений
и полная parity отложены. Расширение начинается только по подтверждённой
потребности и новому решению владельца. Существующие ветки, тесты и код
сохраняются; завершение editor не запускает отложенные работы автоматически.

Общий outcome/usage journal, другие providers, Hermes updates/incidents,
off-machine backup/restore, рефакторинг и выпуск остаются отдельными этапами
[общего goal](STABILIZATION_PLAN.md#revised-delivery-scope). Они не отменены
и не превращаются в новые возможности Claude-editor. Скоринг, модель-судья,
параллельные writers и Hub-managed review остаются вне goal.

## Предел усложнения

Повторно используем Controller, workers, SQLite queue/outbox, namespace и
существующий protected permission host. Не добавляем broker, БД, общий launcher
или agent framework ради этого этапа. Если текущая custody требует платформенной
миграции или переделки внешних запусков, фиксируем конкретный blocker и стоимость
для отдельного решения владельца. Уменьшение объёма не ослабляет trust model.

## Следующая граница разрешений

Проверить применимость уже созданной границы на поддерживаемом filesystem,
обычном Git root, immutable runtime и отдельном provider home по
[file-tool runbook](CLAUDE_FILE_PERMISSIONS.md) и
[existing-host-first custody](CLAUDE_CUSTODY.md). Source fixtures не доказывают
installed custody, реальный human callback или upstream billing.

Решающий native сценарий: Read → предложение изменения → Write/Edit request →
точное новое решение человека → ровно разрешённая правка. Deny не меняет файл.
Настройки manual mode и искусственный hook round trip сами по себе не доказывают,
что native CLI всегда запрашивает разрешение записи. Read/search могут идти без
отдельной карточки внутри технической границы проекта. Авторазрешения, старые
receipts, trust policy и второй reply-to-continue канал не заменяют человека.

Пакет приёмки готовится заранее с точным candidate/rollback и конечным списком
вызовов/эффектов. Model calls, Telegram, установка, live state и рестарты требуют
отдельной авторизации. Пассивные проверки не вызывают модель.
SDK не является автоматическим обходом подписочной/CPA границы.

## Конечная матрица первого этапа

| Проверка | Критерий принятия |
| --- | --- |
| Применимость | Текущий host и поддерживаемые roots подходят существующему wrapper; конкретные blockers названы до установки |
| Чтение и правка | Целевые файлы Read/search ограничены проектом; Deny сохраняет исходные bytes; свежий human Allow применяет точный Write/Edit |
| Полномочия | Отрицательные project/private-state/key/runtime/`.git` проверки; stale/duplicate receipt, timeout, missing host, restart и stop не дают permission |
| Выполнение и сессия | Новая Hub-owned сессия и exact resume; один writer; stop; completed/partial результаты сохранены; unknown outcome не replay |
| Telegram и маршрут | Владелец видит запрос, ожидание, результат и отказ; реальный подписочный route исключает paid/API/extra-usage fallback |
| Завершение | Независимое review, обязательные source gates и отдельно разрешённая приёмка трёх проектов на точной чистой ревизии с compatible rollback |

Для каждой строки сохранять одно из состояний: проверено offline, проверено
native/live, blocker или неизвестно. Эти уровни не складываются в субъективный
процент готовности. Source publication не закрывает native/Telegram строки.
Новые функции не добавляются в эту матрицу как условия завершения.

Текущий исходный [native fixture](../testing/CLAUDE_NATIVE_EDITOR.md)
проверил Write/Edit Allow/Deny, повторную запись того же файла после exact resume
с сохранённой историей и четыре свежих фиктивных receipts. Это закрывает узкий
offline CLI вопрос строки «Чтение и правка»; human Telegram, installed custody
и подписочный route остаются отдельными непроверенными условиями.

## Что означает сопоставимая поддержка с Codex

Полная сопоставимость остаётся отложенным направлением, а не критерием первого
этапа. Прежние native-transfer, saved-session и image investigations сохраняются
как evidence для возможного расширения. Не продолжать PTY startup/witness или
saved-dialogue corpus автоматически после возобновления прерванной сессии.
Следующий trigger — конечная файловая матрица выше.

## Закрытие

Editor milestone открыт до прохождения конечной матрицы. Закрытие указывает
точную ревизию, разрешённые ограничения и отдельно доказанные live результаты.
После этого дальнейший Claude объём выбирает владелец по практическому опыту.
Роли закрыты отменой; остальные этапы goal сохраняют собственные критерии.
Merge и deployment остаются отдельными owner actions.
