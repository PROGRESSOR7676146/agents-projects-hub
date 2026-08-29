# Модель безопасности

## Активы

- исходный код и документы проектов;
- Codex/Hermes/Telegram credentials;
- право запускать команды и подтверждать опасные действия;
- соответствие Telegram-темы правильному каталогу;
- приватный текст задач и результаты Codex.

## Границы доверия

Telegram — удалённый транспорт, не файловая ACL. Hermes аутентифицирует разрешённый Telegram user/chat; router повторно проверяет origin metadata. Только локальный реестр определяет доступные каталоги. Codex sandbox ограничивает фактический файловый доступ, а approval policy — действия, требующие человека. tlive переносит запрос решения, но не делегирует его Hermes.

## Обязательные меры

1. Allowlist точных Telegram user ID и private chat ID; group fallback — только приватная группа.
2. Topic identity — numeric ID. Совпадения/переименования строк недостаточно для rebind.
3. Проект выбирается только по `project_id`; путь из сообщения или tool argument запрещён.
4. `realpath` проекта должен лежать внутри локального `allowed_root` и быть Git-root; symlink escape отклоняется.
5. Session сохраняет immutable project/root binding. Resume из другого root запрещён.
6. Только argv API; shell interpolation, `eval`, `sh -c` и `tmux send-keys` запрещены.
7. Разрешены `read-only` и `workspace-write`; `danger-full-access` запрещён. MVP использует `on-request`.
8. Hermes не может отвечать на approval автоматически. Решение принимает пользователь в Codex/tlive; timeout/неопределённость означает deny/no action.
9. Telegram `update_id` и router `request_id` обеспечивают exactly-once dispatch при повторной доставке.
10. Один активный turn на lane; очередь имеет размер/TTL, `/stop` не означает delete.
11. Логи не содержат токены, environment dump, hidden reasoning и полный terminal buffer. Prompt хранится как digest, если полный текст не нужен для recovery.
12. State/config имеют `0600`; service запускается непривилегированным пользователем без sudo.
13. App-server слушает stdio или Unix socket. TCP — только loopback либо authenticated encrypted tunnel; capability token не пишется в Telegram.
14. Все внешние сетевые и destructive действия по-прежнему проходят политику Codex.

## Основные угрозы и ответы

| Угроза | Ответ |
|---|---|
| Prompt injection просит открыть другой каталог | Tool не принимает путь; registry lookup fail-closed |
| Пользователь создаёт тему с именем проекта | Имя не binding; неизвестный numeric thread ID не маршрутизируется |
| Повтор Telegram update | Уникальный update/request ID, возврат прежнего receipt |
| Подмена/переименование темы | Требуется локально подтверждённый rebind |
| Инъекция через имя проекта/session ID | Regex + argv array, без shell |
| Hermes решает approval словом «да» | Router вообще не имеет approval tool; tlive/Codex — единственный канал |
| Два Codex одновременно правят один worktree | Lease одной lane; параллельность только через отдельный worktree |
| Router перезапущен во время approval | Pending approval не восстанавливается как разрешённый; fail closed |
| App-server protocol изменился | Version/capability probe; остановка вместо fallback к screen scraping |
| Компрометация Telegram | Sandbox/approval всё ещё ограничивают ущерб; локальный kill switch и отзыв bot token |

## Kill switch и recovery

- локальная команда останавливает router независимо от Telegram;
- loss of Telegram auth → остановить gateway/router, отозвать token, не resume pending turns;
- mismatch state/root → `NEEDS_LOCAL_REBIND`, который нельзя снять удалённо;
- удаление проекта из registry блокирует новые turns, но не удаляет Git или Codex history;
- архивирование и удаление — разные операции; router не выполняет permanent session delete в MVP.
