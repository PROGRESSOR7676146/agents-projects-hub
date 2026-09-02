# Roadmap

Roadmap содержит только переносимые продуктовые задачи. Состояние конкретной
установки, имена проектов, аккаунты и результаты live-проверок хранятся вне Git.

## Ближайшие задачи

1. Завершить E2E естественного исчерпания лимита Codex; выбор
   provider/model/effort уже покрыт выделенным Telegram acceptance actor.
2. Сохранять стабильные contract tests для Codex, Hermes, OpenCode и
   Antigravity при обновлении их CLI/API.
3. Реализовать автоматическую ротацию Antigravity только после появления
   поддерживаемого headless account-pool интерфейса.
4. Расширять terminal backends только argv-безопасными адаптерами.

## Отложено

- provider-neutral Session Bridge;
- полное восстановление незавершённого turn после потери машины;
- дополнительные providers до прохождения acceptance текущего набора.

## Не планируется

- TUI/screen scraping;
- автоматические approvals или ослабление sandbox;
- message-by-message зеркалирование нативного CLI;
- выбор локального пути из Telegram.
