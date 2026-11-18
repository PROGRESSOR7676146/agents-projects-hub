# Проверки и ограничения реализации панели тем

Дата: 2026-11-15. Статус: обязательная часть [плана реализации](TOPIC_PANEL_IMPLEMENTATION_PLAN.ru.md), не evidence выполненных проверок. Прочитать вместе с основным планом до первого изменения. При изменении плана синхронизировать только затронутые критерии этого файла.

## 1. Обязательная матрица тестов

Новые имена модулей тестов определить по итоговой структуре. Следующие сценарии обязательны; тесты проверяют наблюдаемые эффекты независимыми ожиданиями, а не вызывают renderer для вычисления expected.

| Группа | Сценарии и требуемые доказательства |
| --- | --- |
| Проекция | Idle, queued, leased, accepted, process-only, approval, local writer, completed, failed, stop pending/terminal, delivery pending/unknown/confirmed; несколько состояний одновременно |
| Семантика | Result не равен delivery; receipt не равен read; worker heartbeat не равен прогрессу; stale quota не равно fresh; selected model не подменяет модель running satellite |
| Отображение | Русский текст, длинные имена, `<>&`, emoji, invalid links, отсутствие timestamps; bounds и валидный HTML, никакой секретной/сырой информации |
| Очередь | Batching/correction membership, held отдельно, stable pagination при добавлении/завершении, counts без full history scan, same-root blocker не из другой случайной темы |
| Команды | Duplicate update, invalid args, attachments, forwards/quotes, bot-addressed command, private/unbound/disabled/legacy scope; отказ не вызывает модель и не импортирует материалы |
| Provenance | Reply на панель не выдаётся за provider final, не создаёт assessment/recovery authority; обычный новый текст сохраняет действующие правила маршрутизации |
| Callbacks | Не-owner, чужая тема/чат/бот/message, stale instance/session/catalog, malformed/oversized data, два query IDs для одного ticket, ошибка ответа callback |
| Stop races | Render → старая задача завершается → новая начинается → старый tap; двойной tap; два владельца; stop + provider completion; restart после intent; rollback при записи notice |
| Stop effects | Один durable request, сохранённая coverage, новая задача не затронута stale ticket, held и другие темы прежние, отсутствие automatic Allow и повторных provider invocations |
| Telegram faults | Reject before-send, 429, timeout after-send, malformed receipt, потеря receipt commit, late receipt, удалённое сообщение, отсутствующие pin rights; create не повторяется при unknown |
| Edit races | Старый HTTP ещё выполняется после lease expiry; coalesced revision; замена панели во время edit; поздний receipt не активирует старые controls |
| Restart/disable | Confirmed mapping сохранён; unknown не стал confirmed; off до/после send; старые кнопки после off и последующего enable; неизменные задания/результаты/uncertainty |
| Изоляция | Panel failure не портит worker health и не задерживает final/stop; cooldown общий по bot; bounded scheduling нескольких тем и отсутствие busy polling/write-lock в idle |
| Миграция | Populated previous schema, атомарный rollback DDL, backup, foreign keys, retained queues/outboxes/unknowns; совместимый runtime rollback без restore |

Существующие regression suites для выбора focused-пакета: `tests.test_controller_commands`, `tests.test_command_dispatch_characterization`, `tests.test_service_integration`, `tests.test_command_menu`, `tests.test_routing`, `tests.test_telegram`, `tests.test_telegram_rejection_certainty`, `tests.test_outbox_sender`, `tests.test_stop_certainty`, `tests.test_stop_certainty_state`, `tests.test_task_activity`, `tests.test_task_notice_sender`, `tests.test_delivery_wait_notices`, `tests.test_migrations`. Перепроверить имена на актуальном base и добавить затронутые suites; этот список не заменяет полный gate.

Использовать репозиторный Python и установленные зависимости, не системный случайный interpreter. Примеры при существующей `.venv`:

```bash
PYTHONPATH=src .venv/bin/python scripts/validate.py --profile focused tests.test_controller_commands tests.test_command_menu tests.test_stop_certainty
PYTHONPATH=src .venv/bin/python -m hermes_codex_router.privacy_scan . --history
PYTHONPATH=src .venv/bin/python scripts/validate.py
```

Focused-запуск добавить новыми тестами панели после их создания. Последняя команда — canonical для публикации; не запускать её повторно без изменений/ошибки, если обязательный hook уже проверил тот же exact head. Не отключать hooks, privacy или tests. Независимый review требуется из-за Controller/state, даже если diff маленький; точную модель и CPA route брать из актуального [runbook](CLAUDE_SOURCE_REVIEW.md). При review blocker исправить и проверить; не просить менее строгую модель подтвердить тот же diff.

## 2. Live сценарий и готовность к передаче

Подготовить автоматизированный сценарий на одной разрешённой тестовой теме обычной production-группы. Сначала без inference проверить открытие, редактирование, пагинацию, pin/refusal, off, повторные команды и независимость `/status`. Проверку отображения выполнения и Stop проводить только в отдельно разрешённом live-сценарии с безопасной задачей и без посторонней работы. Fake transport не доказывает показ сообщения в Telegram-клиенте.

Проверить на актуальном мобильном клиенте читаемость и доступность кнопок, переход из закрепления, отсутствие потока новых статусных сообщений, поведение старой панели. Для продуктового суждения владельцу показать готовый сценарий; повторяемые технические действия и сбор evidence выполняет агент. Production faults не создавать ради теста, если их можно доказать offline; не прерывать рабочие задачи для Stop canary.

Перед включением подготовить точные candidate/rollback revisions, совместимость schema, backup и способ отключить панель без остановки работы провайдеров. После выключения late callbacks должны отказать, а основной Hub продолжать работать. Никакого автоматического backup restore, смены профиля безопасности или добавления прав боту.

Финальный отчёт исполнителя: где открывается панель; какие кнопки реально работают; какие режимы отказаны; exact SHA; выполненные проверки и их уровень; PR/review findings; факт включения или «не включено»; rollback; оставшиеся ограничения. Обновить этот план, owning status/acceptance records и документацию использования без копирования live фактов в Git.

## 3. Категорические запреты для исполнителя

- Не выполнять этот план как свободное поручение «улучшить Telegram UX». Не добавлять dashboards, scoring, agent handoffs, общую очередь проектов или универсальный scheduler.
- Не вызывать LLM для панели, названий задач, обновления статуса, выбора кнопок, health probes или тестов. Не читать native transcripts/terminal buffers для прогресса.
- Не давать model-generated choice buttons полномочия panel controls. Existing `ch:` — обычный пользовательский ответ; panel actions — отдельная детерминированная поверхность.
- Не утверждать успех по отсутствию ошибки, heartbeat, PID, elapsed time или карточке. Не менять факты execution/delivery при рендере.
- Не дублировать lifecycle SQL, обходить facade, писать в production SQLite вручную или добавлять транзакции вокруг HTTP.
- Не отправлять через provider bots от имени Hub, не передавать credentials workers, не запускать второй group poller.
- Не повторять unknown send/provider work, не освобождать leases/holds по возрасту, не удалять jobs/history ради чистой панели.
- Не делать Telegram API вызов или прямую CLI-команду из callback payload. Никаких shell strings, `eval`, произвольных путей или unit names.
- Не реализовывать остановку через `pkill`, kill по найденному PID или отключение сервиса. Только существующий проверенный stop path.
- Не публиковать личные проекты, numeric deployment IDs, account hints из реальной установки, приватные URL, пути, screenshots, конфигурацию и acceptance logs. Примеры только вымышленные.
- Не обновлять требования/документационные hashes лишь ради зелёного gate, не переписывать выпущенные миграции и не повышать hotspot budget без обязательного решения.
- Не выдавать локальные тесты за live-приёмку. Не включать функцию и не менять права/аккаунты только потому, что код опубликован.

## 4. Короткое поручение следующему агенту

> Реализуй `docs/operations/TOPIC_PANEL_IMPLEMENTATION_PLAN.ru.md` и обязательные проверки/ограничения `docs/operations/TOPIC_PANEL_ACCEPTANCE.ru.md` по этапам, начав с проверки текущего main и обязательного read order. Нужна панель обычным сообщением с inline-кнопками в проектных темах Hub, без Widgets и веб-приложения. Сохрани существующие routing, writer, stop, approval и delivery boundaries. Сначала тесты поведения, затем минимальная реализация. Все технические проверки выполняй сам. Отмечай этапы, exact SHA и evidence; не называй всю задачу готовой после частичного UI. При конфликте с актуальным контрактом укажи конкретный конфликт и предложи минимальное решение, не обходи gate. Deployment и live acceptance выполняй только в пределах отдельно имеющейся авторизации.
