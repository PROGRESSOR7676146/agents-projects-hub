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
- отказ общего Codex app-server переводит Codex-бота на официальный stdio
  app-server (интеграция `codex-multi-auth` выведена, ADR 0047);
- отказ tlive не разрешает действия автоматически: approval остаётся локальным
  и fail-closed.

Независимость каналов не отменяет совместимость optional Hub plugin/hook:
Gateway импортирует Hub-код и потребляет SQLite. Source/environment pin и schema
нужно согласовать при rollout/rollback по
[recovery capsules](operations/RECOVERY_CAPSULES.md#schema-consumers-and-optional-model-proxies).
Hermes сохраняет native providers/tools/channels и участие в проектах.
По [ADR 0072](decisions/0072-trusted-personal-installation.md) взаимный ремонт
доверенными ассистентами сохраняется, но не даёт права одобрять друг друга.

Шаблон monitor уже использует `--notify --repair`: разрешённый установкой
детерминированный repair может синхронизировать policy и рестартовать Gateway
с cooldown, без модели. Это operational coupling, а не обязательная service
dependency. В recovery drill записывать реальное состояние timer и его
вмешательство; изменение repair policy требует соответствующего разрешения.

## Что хранится где

- В Git: systemd-шаблоны, интеграционный Hermes plugin/hook, проверки, alert
  policy и этот runbook.
- Локально: `~/.hermes/config.yaml`, `~/.hermes/auth.json`,
  `~/.tlive/config.json`, Telegram tokens, web token, provider OAuth и SQLite.
- В Telegram нельзя передавать локальные пути, OAuth tokens или дампы окружения.

## Взаимные recovery capsules

Каждый проект владеет только собственной инструкцией восстановления и после
чистого принятого изменения публикует её в нейтральный локальный каталог
`recovery-capsules`. Публикация содержит Git SHA, время, хеши manifest/runbook и
атомарный указатель `current`. Hermes читает Hub-owned capsule, но не правит её;
Hub аналогично читает Hermes-owned capsule. Установленная копия остаётся
доступной при поломке исходного checkout или venv и не создаёт service
dependency между каналами.

Hub публикует свою capsule командой:

```bash
python scripts/publish-recovery-capsule.py
```

Dirty tree не публикуется. Capsule не содержит credentials, deployment paths,
Telegram identities или private state и не заменяет immutable artifacts,
backup либо deployment-local acceptance.

## Проверка

```bash
agents-projects-hub doctor ~/.config/agents-projects-hub/hub.json
agents-projects-hub monitor ~/.config/agents-projects-hub/hub.json
systemctl --user is-active hermes-gateway.service tlive.service
tlive status
```

`doctor` показывает два независимых check: `recovery:hermes` и
`recovery:tlive`. Один неработающий канал даёт warning мониторинга, оба — error.
Поле `service` различает `active`, подтверждённый `inactive` (exit code 3) и
`unavailable`, когда supervisor bus/команда недоступны или результат probe не
доказывает состояние unit. Свежий Hermes heartbeat или bounded `tlive status`
может независимо подтвердить healthy runtime даже при `service=unavailable`;
эти факты показываются одновременно, а не подменяют друг друга.
Monitor доставляет Operations-уведомления Hub-ботом. При сбое транспорта
существующий Hermes CLI fallback использует тот же Operations topic.
`hermes_notify_target` хранится для совместимости, но отдельного домашнего
push и его cooldown claims в runtime нет.

## Инциденты и обновления стека (план, ADR 0048)

Hub ведёт пассивный журнал инцидентов и сверяет версии стека с приватным
манифестом. Hermes по журналу присылает владельцу карточку без вызова модели;
разбор моделью начинается только по нажатию «Разобрать». Переключение и откат
версий выполняет отдельный детерминированный инструмент и только после
подтверждения владельцем кнопкой. Первым отдельным исходным этапом подготовлены
[уведомления и «Скрыть»](operations/HERMES_INCIDENT_NOTICES.md), default-off;
Analyze и обновления остаются планом. Карточки не отключают существующий
fallback в Operations topic. Активация и live-приёмка требуют отдельной задачи.
Подробности — в
[ADR 0048](decisions/0048-out-of-band-update-and-incident-plane.md).

## Восстановление

1. Если Project Hub не отвечает, писать Hermes в существующий личный чат.
2. Проверить user services и последние журналы без вывода секретов.
3. Исправлять только отказавшийся компонент; не перезапускать исправные каналы.
4. Для Codex восстановить официальный standalone login.
5. tlive в `full` переносит approval на телефон. Неотвеченный запрос ничего не
   разрешает автоматически.

## Antigravity и два Google-аккаунта

Текущий `agy` предоставляет интерактивные `/logout` и sign-in flow, но не
публикует стабильный headless account-pool API. Поэтому Hermes может удалённо
провести ручную смену аккаунта в PTY и переслать пользователю OAuth-ссылку, но
автоматическое копирование credential-файлов и ротация при quota error пока
запрещены. Используются только локально allowlisted account profiles; их имена
и email не записываются в Git. Автоматизацию добавлять после capability probe новой версии
CLI либо появления поддерживаемого upstream account-pool интерфейса.
