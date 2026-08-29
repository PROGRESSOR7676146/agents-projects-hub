# Hermes Project Hub — спецификация и план

Дата актуализации: 2026-08-28. Pythia acceptance пройден.

## Целевая модель

Каждая постоянная Telegram-supergroup соответствует одному локальному проекту
из allowlist. Форумная тема определяется числовой парой
`(chat_id, message_thread_id)` и содержит независимые сессии агентов. Название
темы — изменяемая подпись, а не ключ.

Один агент темы активен. Обычный текст идёт активному агенту; явное упоминание
`@Codex`, `@Gemini`, `@Hermes` создаёт/продолжает satellite session и не меняет
активного агента. Смена активного агента или модели создаёт новую provider
session с handoff предыдущего контекста. `/new` сбрасывает активную сессию,
`/new all` — все сессии темы.

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

## Следующая очередь

1. Проверить постоянный long-polling реальным `@Codex` сообщением в `main thia`.
2. Проверить `/terminal`/`/release` реальной темой и закрытием Terminal вручную.
3. Проверить inline-кнопки `/model` в реальной теме: модели и efforts уже
   валидируются через provider API, новая session получает handoff без hidden reasoning.
4. Проверить двусторонний Hermes adapter живой темой; admission plugin, общая
   state DB и экспорт контекста подключены без второго long-poller. Затем
   добавить унифицированный metadata footer Hermes.
5. Добавить Gemini/OpenCode adapters; неизвестные лимиты обозначать
   `unavailable`, а не оценивать.
6. Сделать создание/переименование темы и выбор initial active agent удобным,
   сохраняя числовой ключ.
7. Добавить Babelfish и Robots только после прохождения Pythia acceptance.

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
- Privacy Mode отключён либо бот назначен администратором, если активный бот
  должен получать обычный текст без упоминания — отключён для обоих пилотных
  ботов, после изменения они повторно добавлены в группу.
