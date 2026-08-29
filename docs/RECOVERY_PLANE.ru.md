# Hermes Telegram и tlive: контур восстановления

## Роль

Существующий личный Telegram-чат с Hermes — административный и аварийный
интерфейс всей установки. Это не project group: у него нет `project_id`, Git-root
или topic binding. tlive — отдельный транспорт наблюдения и подтверждений Codex,
а не агент и не часть Hermes Gateway.

Project Hub, Hermes Gateway и tlive не должны образовывать цепочку обязательных
зависимостей:

- отказ Project Hub не останавливает личный чат Hermes;
- отказ Hermes не останавливает проектных Codex/OpenCode/Antigravity-ботов;
- отказ `codex-multi-auth` переводит Codex-бота на официальный stdio app-server;
- отказ tlive не разрешает действия автоматически: approval остаётся локальным
  и fail-closed.

## Что хранится где

- В Git: systemd-шаблоны, интеграционный Hermes plugin/hook, проверки, alert
  policy и этот runbook.
- Локально: `~/.hermes/config.yaml`, `~/.hermes/auth.json`,
  `~/.tlive/config.json`, Telegram tokens, web token, provider OAuth и SQLite.
- В Telegram нельзя передавать локальные пути, OAuth tokens или дампы окружения.

## Проверка

```bash
agents-projects-hub doctor ~/.config/agents-projects-hub/hub.json
agents-projects-hub monitor ~/.config/agents-projects-hub/hub.json
systemctl --user is-active hermes-gateway.service tlive.service
tlive status
```

`doctor` показывает два независимых check: `recovery:hermes` и
`recovery:tlive`. Один неработающий канал даёт warning мониторинга, оба — error.
Monitor делает отдельные cooldown claims для Codex project groups и домашнего
канала Hermes (`hermes_notify_target`), поэтому сбой одной доставки не помечает
вторую как выполненную.

## Восстановление

1. Если Project Hub не отвечает, писать Hermes в существующий личный чат.
2. Проверить user services и последние журналы без вывода секретов.
3. Исправлять только отказавшийся компонент; не перезапускать исправные каналы.
4. Для Codex сначала восстановить официальный standalone login; multi-auth можно
   чинить отдельно после возврата основного канала.
5. tlive в `full` переносит approval на телефон. Неотвеченный запрос ничего не
   разрешает автоматически.

## Antigravity и два Google-аккаунта

Текущий `agy` предоставляет интерактивные `/logout` и sign-in flow, но не
публикует стабильный headless account-pool API. Поэтому Hermes может удалённо
провести ручную смену аккаунта в PTY и переслать пользователю OAuth-ссылку, но
автоматическое копирование credential-файлов и ротация при quota error пока
запрещены. Аккаунты разрешены только `7676146@gmail.com` и
`prgrssr@gmail.com`. Автоматизацию добавлять после capability probe новой версии
CLI либо появления поддерживаемого upstream account-pool интерфейса.
