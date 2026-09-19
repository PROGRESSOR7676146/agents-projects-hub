# P0: полнота входящих материалов

Статус: repository checkpoint реализован в schema 33; deployment и live E2E не выполнялись.
База исследования: `6f4f0e2e383711d9c595e5cdda4ebe381c29d300`.

## Результат

Каждая допущенная часть пользовательского сообщения либо доступна выбранному
агенту в правильном поручении, либо имеет явное объяснение недоступности.
«Получен файл» и «агент получил содержимое» — разные состояния. Имя файла,
metadata или одна caption не доказывают передачу содержимого. Альбом и серия
приложенных материалов не должны создавать отдельный платный turn на каждый
файл. Пересланный материал остаётся данными, а не авторизацией.

## Подтверждённые точки и карта чтения

| Участок | Что проверить и сохранить |
| --- | --- |
| `telegram.py`: `TopicMessage`, `parse_topic_message`, `parse_direct_message` | Оба parser читают `message.text` и возвращают `None`, если строки нет. Caption и документ без text сейчас не входят в productive ingress. Модель сообщения не содержит полноценного входящего attachment contract. |
| `service.py`: `_handle_update`, `_queue_provider_turn`, polling loop | Авторизация, маршрутизация, приём в очередь и подтверждение offset; существующий payload limit 20 000 символов и усечение требуют проверки на потерю текущего ввода, отдельно от ограничения старого контекста. |
| `state.py`: `record_forwarded_quote`, `unseen_forwarded_context`, `enqueue_or_append_provider_job`, `provider_job_inputs`, steering methods | Идемпотентность каждой части, bounded burst, сохранение target/session/model, пассивные пересылки и отсутствие повторного исполнения при неоднозначном исходе. |
| `external_worker.py`, embedded consumer в `service.py`, `external_runtime.py`, `codex_appserver.py` | Проверить реальные структуры provider input и поддерживаемый способ передачи текста/изображений/документов. Для каждого runtime подтвердить capability либо явно отказать. |
| `artifacts.py`, `artifact_delivery.py`, `telegram.py::send_document` | Это существующая исходящая доставка. Её наличие не означает поддержку входящих файлов. Не смешивать incoming storage с `.hub/staging/<job_id>` для результатов. |
| `acceptance_actor.py` и существующие parser/routing/ingress/fault tests | Расширить существующую систему приёмки; не заводить второго Telegram actor или отдельный framework. |

## Первый проверяемый пакет

Сначала добавить failing regressions на вымышленных Telegram updates:
caption + документ без text; альбом с caption лишь на одной части; документы,
поступившие после начала текстового turn. Зафиксировать, где update исчезает и
какой input реально получает fake provider. Не использовать реальные файлы,
переписки, credentials или активные поручения для воспроизведения.

Затем подготовить компактный контракт нормализации и durable-состояний. До
реализации явно определить:

- принадлежность материала к numeric topic, пользователю, target provider и
  поколению сессии; Reply, mention, selected quote и forwarded origin;
- начало и завершение сбора альбома, максимальное ожидание, поздние части и
  неполный альбом; один caption не означает, что остальные файлы доступны;
- поведение file-only сообщения без нового поручения и материалов, пришедших
  во время active turn: сохранить их, не запускать второй writer, не считать
  постфактум увиденными. Без доказанной поддержки same-turn input использовать
  существующий FIFO/ожидающий контекст с понятным уведомлением;
- запись приёма до продвижения Telegram offset, идентичность каждого
  `(chat_id, message_id)` и связь частей альбома, границы download/retry/commit;
- состояния «принято», «сохранено», «подготовлено для provider», «передано»,
  «отклонено/недоступно» и восстановление при сбое на каждой границе;
- invalidation при `/new`, смене provider, `/connect`, переносе проекта,
  смене writer, `/stop`, истечении срока хранения. Не отдавать материал новой
  сессии или другому root только потому, что старая завершилась.

Это вопросы проектирования, а не предписание создавать отдельную таблицу на
каждое состояние. Предпочесть минимальное расширение существующей очереди и
журнала. Новое долговечное состояние требует additive migration после схемы 32,
тестов upgrade/fault recovery и совместимого runtime rollback.

## Безопасная доставка содержимого

Приём файлов выполняет транспорт с Telegram credentials; provider worker не
получает токен. Скачивание ограничивается Telegram file API с безопасным
формированием запроса; пользовательские URL, filename и путь не становятся
полномочием на сеть или файловую систему. Токен в download URL не попадает в
ошибки или логи. Проверить ограничения размера и срок действия Telegram file
reference по текущей официальной документации перед реализацией.

Использовать приватные управляемые каталоги и имена, проверять фактический
размер, тип, canonical path, symlinks, digest и частично скачанные файлы.
Определить per-file/per-message/per-album/aggregate limits, retention и cleanup.
Не исполнять документы, не распаковывать архивы автоматически и не разрешать
filename/path traversal. Отдельно определить ограниченное извлечение текста
и поддержку изображений; неподдерживаемый формат должен дать явный отказ.

Материалы — lower-priority quoted data. Команды, mentions и инструкции внутри
пересылки или файла не меняют root, routing, sandbox, approval или количество
provider turns. Прямая публикация другого бота и пересылка человеком — разные
Telegram случаи. Недоступную историю и protected content не обходить.

## Обязательная матрица acceptance

| Сценарий | Доказательство |
| --- | --- |
| Text, caption/entities, документ, фото, album, quote, forward | Уникальные маркеры из каждой допустимой части присутствуют в фактическом fake-provider input; недопустимые части перечислены в явном ответе. |
| Reply/mention/обычное сообщение и пересылки от человека, бота, канала | Сохраняется существующий routing; пересылки не запускают команды и не создают самостоятельных turns. |
| Материалы до, во время и после active turn | Нет потерь, двойного writer, подмены session/root или повторного запуска исходного поручения; поздний input сохранён и обработан согласно выбранному контракту. |
| Дубликат update, повтор album part, restart при сборе/download/enqueue | Один durable receipt на часть; нет повторного provider invocation. Неоднозначная передача не replay автоматически. |
| Ошибка загрузки, истёкшая ссылка, размер/тип/число частей, длинный текст | Явная неполнота/отказ вместо молчаливого отбрасывания, усечения поручения или заявления об успешном чтении. |
| `/new`, `/connect`, provider/writer switch, `/stop`, root relocation | Старые материалы не переходят к новой binding generation; сохраняются root/lane exclusion и existing activation boundaries. |
| Crafted names, symlink, archive, forged metadata, cross-project references | Нет выхода из storage/root, раскрытия токена, исполнения вложения или доступа к чужому материалу. |
| Разные runtime capabilities | Каждый поддержанный адаптер получает проверенное содержимое; metadata-only и неподдержанные форматы обозначаются честно. |

Прогнать narrow parser/routing/ingress/adapter tests, затем существующую fault
matrix и `python scripts/validate.py`, включая privacy/history. Обновить product
requirements/status и manifest вместе с новым поведением. Не заменять тест
фактического provider input поиском строки в prompt builder.

Live Telegram/provider E2E — отдельный разрешённый этап: выделенный canary,
вымышленные файлы и уникальные маркеры, exact clean revision всех требуемых
компонентов, действующий rollback, fail-fast и проверка фактического доступа
агента к содержимому. Offline success не объявлять live-приёмкой. Не запускать
старые пользовательские задачи и не просить переслать их вслепую.

## Вне этого пакета

Исправление context/quota labels идёт следующим. Универсальный document service,
новые providers, автоматическое исполнение файлов, расширение approvals и
фоновый LLM-анализ входящих материалов не входят в P0.

## Итог repository checkpoint

Реализация использует одну additive migration 33 и не изменяет миграции 1–32.
Cloud Bot API остаётся ограничен скачиванием файла до 20 MB; Premium-статус
пользовательского аккаунта этот bot boundary не меняет. Hub поэтому не пытается
пересылать большие файлы через acceptance-аккаунт и не заявляет, что способен
разбить байты, которых Bot API ему не передал. Возможный отдельный rollout
официального local Bot API server, который допускает download без size limit,
остаётся будущей deployment-задачей с собственной trust boundary.

Offline acceptance покрывает caption/file-only parsing, фактический input fake
provider, bounded album, FIFO material во время active turn, passive forward,
selected quote, duplicate update, явный oversized отказ, native Codex
`localImage`, schema-32 upgrade и symlink/integrity refusal. Repository checks
не являются доказательством работающей установки; live Telegram/provider E2E
по-прежнему требует отдельной разрешённой задачи и exact-revision evidence.
