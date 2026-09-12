# Замена Codex-сессии в существующей Telegram-теме

Статус: план, не реализовано. Дополняет
[CLI session adoption](CLI_SESSION_ADOPTION_PLAN.md).

Задача: подключить сохранённый локальный Codex thread к теме с существующей
Codex-сессией, явно закрыв прежнюю привязку и сохранив её историю. Пользователь
продолжает в той же Telegram-теме; новый Codex thread сохраняет свой CLI-контекст.
Содержимое двух provider conversations не объединяется автоматически.

## 1. Пользовательский сценарий

1. В новой CLI-сессии закончить ход и закрыть CLI.
2. Локально выбрать source thread и существующую тему. Preview показывает
   прежний Hub session ID/generation, выбранный incoming thread, project,
   target model/effort и наличие работы/недоставленных результатов.
3. Подтвердить замену **конкретной** прежней сессии. Одним commit SQLite она
   архивируется в Hub, а новая получает отдельный session ID/generation и
   writer=`local`. Telegram topic, её title и сообщения сохраняются.
4. В той же теме отправить `/return`. Это явно открывает приём новых задач
   для подключённой сессии. Ответ Hub сообщает, что тема продолжает выбранную
   локальную сессию, прежняя сессия сохранена в архиве.
5. Написать следующую задачу. Worker делает exact-thread resume, как задано
   основным планом. Старые сообщения Telegram остаются видимы человеку.

Закрыть прежнюю сессию здесь означает сделать её недоступной для новых Hub turns
в этой теме. Provider thread не удаляется и не получает archive/delete RPC.
OS-процесс не уничтожается. Активный turn/pending approval сначала должен быть
завершён обычным способом; explicit `/stop` — отдельное действие владельца.
Ни apply, ни `/return` не запускают новую модель сами по себе.

Для первого расширения поддерживается замена **активной Codex-сессии**.
Других провайдеров и их satellite sessions автоматически не закрывать.
Если активен другой provider, preview объясняет эту границу; владелец может
явно выбрать Codex обычным `/model` и повторить preview. Это не скрытая смена
провайдера и не массовый reset темы.

## 2. CLI и подтверждение

Использовать ту же будущую команду `session attach-codex`, добавив:

```text
--replace-session EXPECTED_CURRENT_HUB_SESSION_ID
```

Этот аргумент — compare-and-swap precondition, не `--force`. В preview вернуть
точный текущий session ID и generation. Apply с указанным ID атомарно убеждается,
что в теме всё ещё активна именно эта Codex-сессия. При `/new`, смене поколения
или другой замене после preview — `target_changed`, ни одна session не меняется.

Без аргумента остаётся прежний strict empty-topic mode. Аргумент не отменяет
проверок root, owner assertion, backend, source identity, supported runtime,
уникальности thread или запрета busy work. Попытку replace на пустой теме
отклонить как несовпавшее ожидание. Incoming provider thread, уже привязанный
к этой же текущей сессии, возвращает `already_attached` без архивации/генерации
и без сброса writer. Не создавать «новую сессию» с тем же provider thread.

Source thread, принадлежащий другой active/satellite/archived binding, по-прежнему
нельзя использовать. Восстановление архивной Hub-сессии — отдельная будущая
операция, не скрытое исключение в adoption.

## 3. До какой границы старую работу нужно завершить

Apply повторно проверяет в transaction:

- прежняя активная Codex-session writer=`telegram`; при local/terminal сначала
  обычный возврат владения; command не утверждает, что закрыл чужой CLI;
- нет queued/leased/executing/retry_wait/result_ready jobs или running dispatch
  в этой теме, включая её satellite providers;
- нет недоставленных/retry/leased final, progress или stop/control сообщений;
- нет unresolved indeterminate jobs в теме. Их явно классифицируют имеющейся
  локальной процедурой; replace не помечает их resolved и не повторяет работу;
- другие темы того же project/root не держат известную Hub исполняющуюся работу
  или local/terminal writer, согласно основному adoption contract;
- source thread сохранён, его CLI закрыт по явному утверждению владельца,
  stored root и backend проверены до transaction.

Завершённый исторический диалог, старые completed/failed/cancelled jobs,
доставленные ответы и idle Telegram-owned satellites не мешают replace.
Сохранять все их rows. Не удалять undelivered result, чтобы искусственно сделать
тему idle. Не запускать drain loop или автоматический `/stop`; вернуть safe
reason и следующий шаг. Provider stop acknowledgement сам по себе не заменяет
повторную проверку durable terminal state и доставки.

## 4. Атомарная замена и evidence

Расширить atomic adoption operation основного плана параметром expected old
session ID. Порядок: повторная проверка → archive old → insert new → origin /
replacement receipt → topic active binding → commit. RPC, Telegram и subprocess
внутри SQL transaction запрещены. При любой ошибке старый active binding остаётся.

Новая session имеет новый ID/generation, exact incoming provider thread и
writer=`local`. Прежние jobs, outbox и session references не переписывать на неё.
Прежняя session получает status=`archived`; provider ID, origin, contract
provenance и сохранённые results остаются. Остальные provider bindings темы
сохраняются. Не вызывать существующий `/new` и затем attach: два отдельных
перехода могут потерять прежнюю привязку при ошибке между ними.

Origin record новой session дополнить минимумом durable evidence:

| Поле | Назначение |
| --- | --- |
| `replaces_session_id` nullable | Ссылка на прежнюю Hub session для exact repeat и локальной диагностики. |
| `activation_message_id` nullable | Numeric Telegram message ID первого успешного `/return`; граница допуска новых сообщений. |
| `context_floor_turn_id` | Нижняя граница topic journal для автоматических forwarded/context механизмов новой session. |

Если базовая schema ещё не выпущена, проектировать полную additive таблицу
сразу. Если migration уже вошла в принятую историю, добавить следующую migration,
а не менять семантику прежней schema version. Реальные IDs/private metadata
не включать в Git или общий monitor output.

Repeat после commit-before-stdout находит origin с совпадающими incoming thread,
topic, project и `replaces_session_id`, возвращает прежний результат и не
архивирует новую session. После `/return` repeat не сбрасывает writer local.
После последующего `/new` или replacement возвращает `binding_superseded`, не
воскрешает старую generation.

Пустой attach из основного плана должен пользоваться теми же activation fields:
это убирает разницу admission safety между двумя способами подключения.

## 5. Граница сообщений: старое не становится новой задачей

Нового session ID недостаточно: сообщение может лежать у Telegram и впервые
попасть в Controller после замены. Тогда Controller увидит уже новую session.

Использовать существующий `/return` как точную границу активации:

1. До первого успешного `/return` adopted session writer=`local`; productive
   input для неё не enqueue. Не сохранять rejected input для автоматического
   исполнения после возврата.
2. Первый успешный `/return` в одной transaction меняет writer, фиксирует свой
   `message_id` как activation boundary и current journal high-watermark как
   context floor. Receipt `/return` и эти поля должны быть атомарными.
3. Input, адресованный adopted Codex session с тем же numeric chat/topic и
   `message_id <= activation_message_id`, не может начать provider turn даже
   при первом запоздавшем поступлении после `/return`. Зафиксировать bounded
   receipt/rejection без replay и без переноса в очередь следующей session.
4. Новое сообщение после boundary проходит обычную idempotent admission.
   Сравнивать numeric message IDs внутри правильного chat/topic, не timestamps,
   arrival order, изменяемые названия или `update_id` другого бота.
5. Проверку применять ко всем productive входам к adopted Codex: ordinary,
   mention, Reply, `/context` и batching/steering admission. Не интерпретировать
   локальные negative synthetic menu message IDs как productive Telegram input.
   Commands/callbacks, способные создать работу или изменить selected session,
   должны проверять соответствующую current identity; старые reset/model callbacks
   не получают права действовать над новой generation.
6. Attach против already-read ingress защищён archive old/new session ID и
   transaction admission checks. `/return` против нового ingress также проверять
   с независимыми SQLite connections. Проверка только в Python до transaction
   оставляет окно гонки.

Для v1 numeric boundary опирается на отдельную возрастающую последовательность
message IDs supergroup. Editing не меняет ID; это описано в
[Telegram: Message ID sequences](https://core.telegram.org/api/updates#message-id-sequences).
Проверить mapping Bot API → `TopicMessage` и fixtures. Не распространять этот
контракт на secret chats или unsent scheduled message IDs. Edited/redelivered
старое сообщение не становится новым поручением. Callback содержит отдельную
identity; не переносить на него правило сравнения message ID без проверки
session/catalog provenance.

Граница фиксируется при первой активации adopted generation и не стирается
при дальнейших `/local`/`/return`. Не менять произвольно семантику последующих
возвратов или всех прочих сессий в этой задаче.

## 6. История и маршрутизация после замены

Telegram messages, reply-author mappings и topic journal не очищаются.
Однако новая Codex-session получает свой сохранённый CLI-контекст и только
новые productive inputs; старая topic history не inject автоматически.

`context_floor_turn_id` фильтрует автоматическую передачу старых forwarded
quotes именно для adopted Codex-session. Нельзя ради этого фальсифицировать
успешный result/context acknowledgement или двигать общий cursor другого
provider. Journal rows после apply, но до первого `/return`, тоже не должны
неожиданно стать новым поручением; поэтому floor фиксируется при активации.
Явный `/context` после boundary сохраняет документированное право пользователя
попросить старую видимую историю темы; этот выбор не должен блокироваться floor.

Один journal high-watermark не исключает старую forwarded quote, впервые
полученную уже после `/return`: у неё будет новый journal row ID. Для forwarded
rows сохранять исходный Telegram message ID отдельным nullable provenance полем
(например, `source_message_id` в `external_turn_excerpts`, если подходящего поля
нет в актуальной схеме). Автоматическая передача adopted session требует и
`turn_id > context_floor_turn_id`, и `source_message_id > activation_message_id`.
Unknown provenance не inject автоматически в adopted session; explicit history
request сохраняется. Старую quote не выкидывать из общего журнала — она может
оставаться релевантной другим providers. Existing non-adopted behavior не менять.

Новый реальный Reply к старому сообщению Codex выбирает Codex по автору,
но не оживляет архивную session: запрос идёт текущей adopted Codex-session.
Ей не приписывать знание старого ответа, если его содержание не было передано
существующим разрешённым quote/context механизмом. Reply к другому provider
по-прежнему адресует его сохранённую session. Replacement не меняет эти правила.

Локальный apply output и `/return` acknowledgement ясно сообщают: прежняя
сессия архивирована, активна подключённая; старые Telegram messages сохранены,
контексты не объединены. Если Telegram acknowledgement потерян после commit,
повторная команда/status показывают ту же identity без нового переключения.
Не обещать автоматический возврат старого binding: это отдельная операция.

## 7. Дополнительные failing regressions

Основная adoption test matrix остаётся обязательной. Добавить:

| Сценарий | Acceptance |
| --- | --- |
| Замена idle существующего диалога | Old archived; new exact provider ID, новый Hub ID/generation, writer local; вся старая история/jobs/results сохранены. |
| Старый provider/placeholder не соответствует ожиданию | Reject; никакой implicit provider switch, `/new` или overwrite. |
| Preview → `/new`/model switch/replacement → apply | Expected session mismatch; apply ничего не меняет. |
| Работа/недоставленные результаты/неопределённый исход | Каждое blocking state проверено отдельно; replace не cancel/drain/resolve. |
| Idle satellites | Их bindings/history сохранены; busy/local-owned satellite блокирует замену. |
| Fault после archive и перед commit | Полный rollback, прежняя session всё ещё active и продолжает обычный маршрут. |
| Commit → crash до stdout; repeat до/после `/return` | Exactly one replacement; IDs/generation/writer не меняются повторно. |
| Incoming source совпадает с current provider thread | Idempotent no-op; conversation не архивируется сама себе. |
| Late old message после активации | Old numeric ID не создаёт job/provider invocation; новый ID после `/return` создаёт ровно один. |
| Same IDs в другой теме/чате | Scope mismatch не использует чужую activation boundary; project isolation сохранена. |
| Old edit, duplicate update, old `/context`, old callback | Не запускают работу и не reset/switch новую generation по старому ожиданию. |
| Stale cached ingress/session snapshot | После replace не enqueue в adopted session; independent connections/barriers, не sleeps. |
| Forwarded quote до и после boundary | Старый, включая впервые доставленный после `/return`, не inject автоматически; новый работает штатно; explicit `/context` после boundary доступен. |
| Reply к старому Codex / другому provider | Текущая Codex-session / прежний другой provider соответственно; archived Codex не оживает. |
| Restart и schema rollback | Receipt/origin/boundary/floor сохраняются; rollback executable уважает replacement policy либо отказывает до обработки работы. |

Source exact-thread socket/stdio и no-replay fault tests основного плана не
сокращать. Проверить реальный worker invocation, а не только новую DB row.

## 8. Реализация, review и граница полномочий

Выполнить после базового attach и в отдельных обозримых изменениях: state/schema
и replacement transaction; activation/admission/context boundaries; CLI/UX и
tests. Relevant файлы дополняются `routing.py`, callback/catalog code и
`return_codex_local_writer` только в необходимых точках. Не превращать это в
общий рефакторинг Controller.

Обновить adoption ADR, REQ-WRITER/identity/context/queue acceptance, status и
reviewed requirements hashes. В product prose различать archival Hub binding
и provider archive/delete. Сохранить все no-model/privacy/approval invariants.

Пройти narrow adoption/replacement/routing/model-selection/local-transfer/
queue/context/migration tests, затем canonical validator и privacy/history gate
перед порученным commit. Fictional IDs и временные stores обязательны.
Live замена настоящей session, `/stop` в действующей теме, credential/service
changes и публикация не разрешены этим планом. На review принести отдельную
evidence по delayed-message boundary и old-context isolation.
