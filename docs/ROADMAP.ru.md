# Roadmap

Roadmap содержит только переносимые продуктовые задачи. Состояние конкретной
установки, имена проектов, аккаунты и результаты live-проверок хранятся вне Git.

## Ближайшие задачи

### Активный checkpoint: нативная передача сессии Codex

Работа выполняется строго в следующем порядке; следующий пункт не означает, что
предыдущий принят без указанного evidence:

1. Восстановить зелёный baseline: formatter, lint, полный test suite и privacy
   scan; сохранить checkpoint отдельным commit и push.
2. Реализовать первую фазу
   [ADR 0012](decisions/0012-verifiable-immutable-deployments.md): каждый процесс
   сообщает точный clean Git revision, а monitor обнаруживает mixed/unknown
   deployment. Остальной engineering debt вести по
   [baseline backlog](operations/ENGINEERING_BASELINE.md), не смешивая его с
   передачей сессии.
3. Сделать SQLite-consistent backup, развернуть ровно проверенный revision во
   всех Controller/Sender/provider-worker процессах и пройти bounded Telegram
   smoke без потери или дублирования turn.
4. Разобрать повторяющиеся Telegram transport errors либо доказать корректный
   retry под контролируемым сетевым сбоем.
5. Выполнить Codex feasibility canary: Hub thread → `/local` → native
   `codex resume` → локальный turn → закрытие CLI → `/return` → следующий
   Telegram turn в том же provider thread.
6. Реализовать [ADR 0011](decisions/0011-explicit-native-session-ownership-transfer.md):
   удалить автоматический summary из `/return`, сохранить строгий one-writer
   lease и не добавлять PID discovery, PTY mirroring или active-turn migration.
7. Добавить fault/contract tests и deployment-local E2E для Codex. Расширять
   контракт на OpenCode и Antigravity только после принятого Codex E2E.

### Последующие задачи

1. Реализовать и принять Telegram Interaction Contract v2 как основную
   продуктовую функцию, а не текстовую подсказку внутри пользовательского
   сообщения:
   - Codex получает контракт через штатный `developerInstructions` при
     `thread/start` и `thread/resume`;
   - OpenCode и Antigravity получают channel-specific agent/profile там, где
     runtime предоставляет поддерживаемый интерфейс, с безопасным prompt
     fallback только при отсутствии такого интерфейса;
   - для сложной задачи агент кратко сообщает понимание и подход, после чего
     продолжает без искусственной задержки; Hub предлагает Start/Clarify/Cancel
     только когда действительно требуется решение пользователя, а простые
     однозначные задачи выполняются сразу;
   - транспорт отдельно владеет semantic message splitting, copyable blocks,
     inline choices, progress/activity и прикреплением артефактов;
   - E2E behavioural eval проверяет фактические ответы каждого provider на
     короткие, неоднозначные, длительные и artifact-producing задачи. Проверки
     наличия строки контракта в prompt недостаточно.
2. Завершить E2E естественного исчерпания лимита Codex; выбор
   provider/model/effort уже покрыт выделенным Telegram acceptance actor.
3. Поддерживать уже добавленные contract tests при обновлении Codex app-server,
   Hermes Gateway hook, OpenCode/Antigravity CLI и Antigravity statusline.
4. Реализовать автоматическую ротацию Antigravity только после появления
   поддерживаемого headless account-pool интерфейса.
5. Расширять terminal backends только argv-безопасными адаптерами.
6. Реализовать команду `/steer`: передача агенту накопленных сообщений пользователя
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
