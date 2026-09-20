# Следующая сессия: deployment и live acceptance P0/P1

Статус: P0 и P1 реализованы как repository checkpoints; deployment и live E2E остаются отдельными gates.
Дата: 2026-09-20

## Откуда продолжать

Проверенная интеграционная база `main`:
`f6542afbe6ed6285e7388c8dc8b5d9760d7380f9`.
PR #46–47 и разработка из PR #39 включены в её историю; интеграция завершена
через PR #48. Эта основа была сверена с `origin/main` перед созданием отдельной
ветки P0. Итоговая validation P0 относится только к exact revision его
repository checkpoint и не является доказательством текущего deployment.

Реализованы редактирование проектов, подключение сохранённых Codex-сессий,
явный provider route, native `/local` с сохранением настроек, root exclusion,
ограниченная параллельность независимых roots/worktree lanes и полнота входящих
Telegram-материалов. Схема — 33; миграции 1–30 сохранены, 31–33 добавлены.
Параллельность по умолчанию — один.
При интеграции дополнительно проверены согласованный перенос execution scope
с проектом, crash recovery и повтор admission для динамической группы после
ошибки SQLite без пропуска сообщения.

## Завершённая repository-работа

1. **P1:** Codex app-server `last.totalTokens` используется как текущая
   заполненность контекста; накопительный `total.totalTokens` больше не создаёт
   ложный ноль. Последний snapshot учитывает compaction, отсутствие snapshot
   очищает старое значение до unknown. Quota labels строятся из reported
   duration, а passive account snapshot сохраняет duration и freshness.
   Неизвестные окна называются Primary/Secondary window.

P0/P1 закрыты только на repository evidence; deployment и live E2E требуют
точной чистой опубликованной ревизии, manifest/backup/rollback gate и проверки
каждого обязательного long-running component.
Не возобновлять старые пользовательские поручения и не выдавать отправленные
ранее файлы за уже полученные агентом. Продолжение таких задач требует
отдельного согласования.

## Входные проверки

Прочитать AGENTS.md, optional private operator profile и нормативные модули
в установленном порядке, затем status, index, security и этот план. Проверить
`git status`, текущую ветку, HEAD и актуальную `origin/main`; старый worktree
или приватная запись не заменяют проверку источников. Сохранить чужие правки.
Начинать реализацию в отдельной ветке/worktree от актуальной основной линии.

[Roadmap](../ROADMAP.ru.md) задаёт приоритеты.
[План надёжности](RELIABILITY_PLAN.md) и
[review release gate](RELEASE_GATE_IMPLEMENTATION_REVIEW.md) сохраняют rationale
предыдущих пакетов; последовательность A–F не является текущим заданием.
Не повторять завершённую интеграцию и не запускать удаление multi-auth как
автоматический следующий шаг.

## Граница завершения

Закончить проверяемый repository checkpoint: focused regressions, canonical
validation, privacy/history, обновлённые требования/status и ADR для новых
долговечных решений. Если потребуется новая схема, использовать миграцию после
33 и определить совместимый rollback; не переписывать существующие версии.

Работающая установка может оставаться на другой ревизии и схеме. В этой сессии
сервисы и live БД не обновлялись; перед deployment нужны exact-revision evidence,
резервная копия и совместимый rollback artifact. Запуск сервисов, live Telegram
или provider E2E не следует автоматически из подготовки или реализации плана.
Приватные deployment-данные и журналы остаются вне репозитория.
