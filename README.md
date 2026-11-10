# architect-harness

Архитектурная память для совместной работы программиста и агента в **OpenCode 2.2.5**.
Харнесс хранит границы системы, решения, их основания и открытые вопросы в графе,
чтобы после новой сессии не восстанавливать проект по обрывкам переписки.
Он помогает обсуждать компромиссы и замечать значимые изменения до применения,
но не выбирает архитектуру за пользователя и не контролирует произвольные правки кода.

Текущий пакет — **0.3.0**. Python-ядро независимо от MCP, LLM-провайдера и UI;
OpenCode отвечает за модель, диалог, инструменты и делегирование.

## Возможности

- Граф в локальном SQLite: компоненты, окружение, решения, ограничения, свидетельства
  и привязки к реализации. Решение, поддержанное утверждение и наблюдение — разные записи.
- Неизменный diff предложения, операторское рассмотрение существенных изменений,
  атомарное применение к ожидаемой ревизии (CAS) и безопасный повтор применения.
- Отмена и замещение неприменённых предложений без удаления истории.
- Три роли: `architect`, `architect-researcher`, `architect-auditor`; команды `/arch-*`.
- Plugin загружает свежий контекст перед запросом модели и при сжатии истории.
  Checkpoint хранит текущую работу, точные архивы доступны страницами и поиском.
- CLI и MCP stdio; статический инспектор Python без исполнения исследуемого кода.

## Требования

- Python **3.11+**, Git, модуль `venv` и pip.
- Для интеграции — **OpenCode 2.2.5** и настроенная пользователем модель с tools.
- MCP extra использует **`mcp==2.3.0`**, не SDK v1. Ядро без MCP работает на стандартной библиотеке.
- Примеры ниже — для POSIX shell. В Windows замените `.venv/bin/python` на
  `.venv\Scripts\python.exe` и адаптируйте пути/синтаксис shell.

Plugin не требует npm-зависимостей; Node нужен только для его отдельного теста.
Локальные проверки выполнялись на Linux с Python 3.12.3. Ключи и модель установщик не настраивает.

## Установка из исходников

```bash
git clone https://github.com/rpol-recart/architect-harness.git
cd architect-harness
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[mcp]"
HARNESS="$PWD"
```

Подключите харнесс к своему репозиторию. Для первого знакомства можно использовать
новый пустой каталог рядом с исходниками:

```bash
mkdir ../my-project
.venv/bin/python -m architect_harness opencode-install ../my-project --project-id my-project --title "Мой проект" --profile python
.venv/bin/python -m architect_harness opencode-doctor ../my-project
cd ../my-project
opencode
```

Вместо `../my-project` можно передать абсолютный путь к существующему репозиторию.
Ожидаемый результат: `.opencode/opencode.json`, plugin в `.opencode/architect-harness/`,
пустой граф `.architecture/architecture.sqlite` с ревизией 0 и операционная память
`.architecture/architecture.context.sqlite`. У `opencode-doctor` ожидается `"ok": true`;
это проверка локальной установки, не качества модели.

Установщик сохраняет сторонние настройки и существующий `default_agent`, отказывается
затирать конфликтующие файлы и не сбрасывает граф при повторной установке с тем же
project-id. В существующем графе используйте его ID. Пути к Python и проекту абсолютные;
после переноса потребуется обновление конфигурации. Подробности —
[в руководстве OpenCode](docs/OPENCODE.md#установка-и-перенос).

## Пример работы

В интерфейсе OpenCode:

```text
/arch-start Спроектируем сервис обработки заявок. Сначала изучи существующий код и окружение; неизвестное не выдумывай.
/arch-focus system
```

Ожидаемый ход работы: агент восстанавливает границы и один сценарий, уточняет
существенные неизвестности, предлагает варианты и diff графа. Это пример диалога,
не запись проверенного ответа LLM. ID `system` используйте, если такой узел уже создан;
иначе передайте вопрос или фактический ID.

```text
/arch-review change_ID
/arch-resume
```

Подставьте ID предложения. Аудитор проверяет diff, но **не согласовывает его**.
Существенное изменение оператор рассматривает и согласовывает в отдельном терминале:

```bash
"$HARNESS/.venv/bin/python" -m architect_harness --db .architecture/architecture.sqlite show-change CHANGE_ID
"$HARNESS/.venv/bin/python" -m architect_harness --db .architecture/architecture.sqlite approve CHANGE_ID --revision N --reviewer "Владелец проекта"
```

`HARNESS` — абсолютный путь к установленным исходникам; в другом терминале задайте
его заново. `N` — базовая ревизия рассмотренного предложения, которая должна совпадать
с текущей ревизией графа. Затем попросите агента применить согласованное предложение.
Если граф изменился, нужна новая подготовка diff, а не перенос старого approval.
Reviewer обозначает авторство, **не аутентификацию**. `approve` и `cancel` не доступны через MCP.

## Проверить память без LLM

Из каталога исходников, в отдельной демонстрационной БД:

```bash
.venv/bin/python -m architect_harness --db architecture-demo.sqlite init --project-id demo --title "Демонстрация"
.venv/bin/python -m architect_harness --db architecture-demo.sqlite propose examples/bootstrap.json
# Подставьте ID из предыдущего ответа:
.venv/bin/python -m architect_harness --db architecture-demo.sqlite show-change CHANGE_ID
.venv/bin/python -m architect_harness --db architecture-demo.sqlite approve CHANGE_ID --revision 0 --reviewer "Владелец проекта"
.venv/bin/python -m architect_harness --db architecture-demo.sqlite apply CHANGE_ID --revision 0
.venv/bin/python -m architect_harness --db architecture-demo.sqlite context --focus system --profile core --profile python
.venv/bin/python -m architect_harness --db architecture-demo.sqlite snapshot
```

Пример описывает сам харнесс, а не вашу систему. Ожидаемый эффект: ревизия 1,
сохранённые узлы и ограничения; новый процесс читает их из SQLite. Для своего проекта
замените `operations` и `rationale`, а `base_revision` возьмите из актуального графа.

### Lifecycle предложений в 0.3.0

```bash
.venv/bin/python -m architect_harness --db architecture-demo.sqlite list-changes
.venv/bin/python -m architect_harness --db architecture-demo.sqlite cancel CHANGE_ID --revision N --reviewer "Владелец проекта"
```

Отменять можно только неприменённое предложение; `N` здесь — **текущая** ревизия
из заголовка `list-changes`. Отмена добавляет событие, не меняя граф и payload предложения.
Список показывает исходный и эффективный статусы, ревизии, время и автора.

Для замены агент передаёт optional `supersedes` в MCP `propose_change` при подготовке
нового diff на текущей ревизии (CLI `propose` этот параметр не передаёт). Прежнее предложение
становится `superseded` лишь при успешном применении замены после отдельного операторского
approve, атомарно. Старое согласование не переносится. `approve`/`apply` терминального
предложения возвращают `proposal_cancelled` или `proposal_superseded`; CAS сохраняется.
Отмена или замещение не делают устаревший diff применимым.

## Ограничения

- Политика защищает операции **графа**, а не произвольные файлы, shell или инфраструктуру.
  Permissions OpenCode — не OS sandbox; процесс с прямым доступом к БД/CLI может их обойти.
- Нет GUI, remote authentication, multi-tenancy и собственного автономного LLM-цикла.
  `worker` и `orchestrator` — необязательные внешние профили, не часть штатной тройки.
- Свежая сессия для каждого задания — процедура; технический hook удаления `sessionID`
  и ограничения brief 8000 байт действует только на вызовы от `architect`.
- Checkpoint и summary не являются approval. Напоминание после compaction не доказывает
  чтение полного контракта; для `orchestrator` такое чтение обязательно до зависимой работы.
- Наблюдение (`reported` или `captured`) не принимает решение автоматически. Текст графа
  и внешних источников — данные, а не новые инструкции. Не помещайте в память секреты.
- Нет автоматического переоткрытия вопросов по зависимостям и полной синхронизации графа с кодом.
  `forbid_dependency` проверяет прямые связи графа, не транзитивные пути и не исходники.
- Каждая ревизия хранит полный снимок; масштабирование на большие графы не измерено.
  Технические проверки не подтверждают практическую приёмку с выбранной LLM и реальным проектом.

## Документация и разработка

- [OpenCode: роли, MCP, рассмотрение diff, перенос и проверки](docs/OPENCODE.md).
- [Контекст: compaction, checkpoint и точные архивы](docs/CONTEXT_POLICY.md).
- [Проектная основа v0.1](docs/PROJECT.md), [ADR-001–009](docs/DECISIONS.md),
  [текущий статус и следующие этапы](docs/ROADMAP.md), [источники методологии](docs/SOURCES.md).
- [Процедура архитектора](src/architect_harness/assets/architect-workflow.md),
  [онтология](src/architect_harness/assets/ontology.json), [каталог вопросов](src/architect_harness/assets/questions.json).
- [Пример другого MCP-клиента](examples/mcp-client.json) требует замены абсолютных путей;
  это не формат OpenCode V2. Сервер `serve` работает через stdio, не HTTP.
- [Многопользовательская концепция](docs/MULTIUSER_AGENT_CONCEPT.md) — предложение, не реализованная возможность.

Перед изменениями прочитайте [правила проекта](AGENTS.md). Основные проверки:

```bash
.venv/bin/python -m unittest discover -s tests -v
HARNESS_PYTHON=.venv/bin/python node --test tests/opencode-plugin.test.mjs
.venv/bin/python scripts/demo.py
.venv/bin/python scripts/mcp_roundtrip.py
.venv/bin/python scripts/opencode_smoke.py --output /tmp/opencode-smoke.json
```

`demo.py` проверяет память и настоящий HTTP GET к временному локальному сервису;
MCP roundtrip — официальный клиент и отдельный stdio-процесс. Smoke запускает отдельный
OpenCode без ключей и вызова LLM. Эти сценарии не доказывают контракт production-сервиса.

## История версий

В доступной git-истории: [0.2.0](https://github.com/rpol-recart/architect-harness/commit/74a3ceb)
— исходный публичный commit с ядром, MCP и интеграцией OpenCode;
[0.3.0](https://github.com/rpol-recart/architect-harness/commit/97f8cee)
— lifecycle cancel/supersedes и подготовка упаковки/лицензирования.
Документы v0.1 — историческая проектная основа, не подтверждение отдельного опубликованного релиза.
Версия пакета **0.3.0**, схема графа и методология **0.1**, ревизия графа проекта — отдельный счётчик.
Для lifecycle 0.3.0 миграция существующей БД не требуется.

## Лицензия

[Apache License 2.0](LICENSE).
