# Roadmap

Roadmap содержит только переносимые продуктовые задачи. Состояние конкретной
установки, имена проектов, аккаунты и результаты live-проверок хранятся вне Git.

## Ближайшие задачи

### Активный checkpoint: безопасный split product requirements

Сначала подготовить test-first план: инвентаризировать requirement IDs и ссылки,
добавить проверки полноты/уникальности/ссылочной целостности и прогнать privacy
history gate. Массовый перенос выполнять только если эти проверки доказывают
безопасность; иначе оставить документ на месте и зафиксировать точный blocker.

План и pre-split guardrails готовы: inventory содержит 88 IDs и hashes всех 20
нормативных секций, Markdown audit проверяет files/anchors. Следующий шаг —
отдельный механический move при зелёных canonical и privacy/history gates.

### Telegram Interaction Contract v2

Работа выполняется по
[ADR 0013](decisions/0013-native-provider-interaction-instructions.md).
Контракт v2 становится основной продуктовой функцией, а не текстовой
подсказкой внутри пользовательского сообщения:

- Codex получает контракт через штатный `developerInstructions` при
  `thread/start` и `thread/resume`;
- OpenCode и Antigravity получают channel-specific agent/profile там, где
  runtime предоставляет поддерживаемый интерфейс, с безопасным prompt fallback
  только при отсутствии такого интерфейса;
- для сложной задачи агент кратко сообщает понимание и подход, после чего
  продолжает без искусственной задержки; Hub предлагает Start/Clarify/Cancel
  только когда действительно требуется решение пользователя, а простые
  однозначные задачи выполняются сразу;
- транспорт отдельно владеет semantic message splitting, copyable blocks,
  inline choices, progress/activity и прикреплением артефактов;
- E2E behavioural eval проверяет фактические ответы каждого provider на
  короткие, неоднозначные, длительные и artifact-producing задачи. Проверки
  наличия строки контракта в prompt недостаточно.

Codex repository wiring, bounded behavioural runner и локальная doctor-
provenance принятой версии по текущим provider sessions реализованы. Обычный
мобильный `/status` не расширен. Для принятия Codex v2 всё ещё требуется
отдельный разрешённый deployment-local прогон; проверка native channels других
providers остаётся вне текущего scope.

### Последующие задачи

1. Завершить E2E естественного исчерпания лимита Codex; выбор
   provider/model/effort уже покрыт выделенным Telegram acceptance actor.
2. Поддерживать уже добавленные contract tests при обновлении Codex app-server,
   Hermes Gateway hook, OpenCode/Antigravity CLI и Antigravity statusline.
3. Реализовать автоматическую ротацию Antigravity только после появления
   поддерживаемого headless account-pool интерфейса.
4. Расширять terminal backends только argv-безопасными адаптерами.
5. Реализовать команду `/steer`: передача агенту накопленных сообщений пользователя
   с приостановкой/прерыванием выполнения предыдущих инструкций.

## Резервирование и восстановление WSL

1. Считать весь WSL-дистрибутив критичными данными: исходники, Git worktrees,
   provider session stores, Hub SQLite, OAuth/configuration state, Hermes/tlive,
   локальные инструменты и пользовательские файлы не должны оставаться в одном
   экземпляре на системном диске.
2. Сделать два независимых слоя:
   - частый зашифрованный инкрементальный backup критичных каталогов во внешнее
     off-machine хранилище с retention и проверкой целостности;
   - периодический полный cold image/export WSL-дистрибутива после согласованной
     остановки WSL, также вне физического диска ноутбука.
3. Не считать синхронизацию, backup на том же SSD или единственный cloud mirror
   резервной копией. Ключ восстановления хранить отдельно от backup и ноутбука.
4. Автоматизировать расписание, bounded logs, уведомление о первом сбое и
   контроль возраста последней успешной копии без постоянного спама.
5. Документировать bare-machine restore и регулярно выполнять тестовое
   восстановление в отдельный WSL-дистрибутив с проверкой Hub, session UUID,
   секретов, Git-состояния и provider logins. Backup без restore drill не
   считается принятым.

## Недавно завершено

- Canonical gate проверяет согласованность package version, changelog, project
  status и Git tags. Отсутствующие tags видны как debt, но audit не создаёт и не
  переписывает Git history; deployed SHA остаётся отдельным доказательством.
- Recovery diagnostics различают недоступный supervisor bus, подтверждённо
  inactive unit и независимо healthy Hermes/tlive runtime; probe failure больше
  не подписывается как `service=inactive`.
- Schema 21 ограничивает `runtime_events` одновременно 30 сутками и 10 000
  newest rows. Миграция и runtime pruning атомарны, детерминированы и не меняют
  health/alert state или provider work.
- Нативная передача Codex принята на immutable release: `/local` и model-free
  `/return` сохраняют provider thread и режим единственного writer.
- Автоматический межагентный handoff и фоновая инъекция непрочитанного диалога
  удалены. Сохраняемый журнал доступен провайдеру только по явной ограниченной
  команде пользователя `/context [agent_id] [1..20]`; команда намеренно не
  загромождает основное Telegram-меню.

## Отложено

- provider-neutral Session Bridge;
- постоянная supervisor-managed `tlive run` сессия: она не нужна для принятой
  попеременной передачи владения Telegram ↔ native CLI;
- полное восстановление незавершённого turn после потери машины;
- дополнительные providers до прохождения acceptance текущего набора.

## Не планируется

- TUI/screen scraping;
- автоматические approvals или ослабление sandbox;
- message-by-message зеркалирование нативного CLI;
- выбор локального пути из Telegram.
