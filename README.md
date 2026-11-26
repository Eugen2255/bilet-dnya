# Билет дня

Telegram-бот для подготовки группы к экзамену. Бот публикует общий билет по расписанию. В личке студент отвечает на вопросы своей порции и получает проверку по утверждённому эталону. Вечером группа видит сводку. Слабые билеты возвращаются в следующих порциях.

- [Задачи команды](docs/backlog.md) — владельцы, зависимости и первый сквозной сценарий.

Контракты, алгоритмы и критерии приёмки описаны в отдельном документе «Архитектура и ТЗ — Билет дня».

`/health` проверяет процесс API. PostgreSQL проверяется отдельным healthcheck в Compose. Схема и демонстрационные данные лежат в `db/`. Подключение API к БД, воркер, Telegram и адаптер модели запланированы в бэклоге.

## Локальный запуск

С запущенным Docker Engine выполните в корне проекта. В PowerShell `cp` работает как псевдоним `Copy-Item`.

```sh
cp .env.example .env
docker compose up --build -d
docker compose ps
```

Откройте [проверку API](http://127.0.0.1:8000/health) и [OpenAPI](http://127.0.0.1:8000/docs). PostgreSQL доступен с хоста на `127.0.0.1:5433`, из Compose — на `db:5432`. `docker compose down` останавливает сервисы с сохранением локальной БД.

### Миграции и тестовые данные

После запуска PostgreSQL примените схему. Вторая команда добавляет синтетический курс в локальную БД.

```sh
docker compose exec -T db sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /db/migrate.sql'
docker compose exec -T db sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /db/seed.sql'
```

Обе команды можно повторять. `db/migrate.sql` регистрирует применённые версии в `schema_migrations`. Для изменения схемы добавляйте новый SQL-файл и шаг в `db/migrate.sql`. `db/seed.sql` заполняет только отсутствующие тестовые записи и не перезаписывает уже созданный курс.

Тестовый курс «Тестовый курс по базам данных» содержит три билета с утверждёнными рубриками, организатора и студента. Их тестовые Telegram ID указаны в `db/seed.sql`. Для общей фикстуры используйте билет №1 «Что такое первичный ключ?» и учебную дату `2035-01-20`. Учебный день и задания создаёт планировщик из CORE-02.

В схеме `assignments.group_id` связывает участника с группой курса. Закреплённая версия рубрики хранится в `assignments.rubric_id`. Балл в `attempts.result.score` хранится как целое число от 0 до 5. При создании курса достаточно группы и названия. Активация проверяет расписание, дату экзамена и утверждённую рубрику каждого билета.

После загрузки тестового курса проверьте ограничения на локальной БД. Скрипт откатывает свои изменения.

```sh
docker compose exec -T db sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /db/tests/core_01.sql'
```

Для запуска API через Python 3.12 создайте виртуальное окружение.

```sh
python -m venv .venv
```

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

```sh
# Linux / macOS
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --reload
```

## Работа в команде

### Проверка модели AI-01

В локальном `.env` задайте URL Open WebUI и ключ аккаунта. Для `MODEL_PRIMARY_URL` укажите полный endpoint, например `https://openwebui.example.edu/api/chat/completions`. Если администратор разрешил маршруты с префиксом `/api/v1`, укажите `/api/v1/chat/completions`. Ключ храните в `MODEL_PRIMARY_API_KEY`. [Open WebUI API](https://docs.openwebui.com/reference/api-endpoints/)

Получите доступные ID и укажите нужный в `MODEL_PRIMARY_ID`. Затем прогоните три синтетических ответа. Команды запускаются из корня проекта в Python-окружении с `requirements.txt`.

```sh
python -m app.ai --list-models
python -m app.ai correct
python -m app.ai partial
python -m app.ai wrong
```

Для тестов с подставной моделью выполните `python -m unittest discover -s tests`. Чтобы включить проверку очереди, задайте `BILET_TEST_DATABASE_URL` на отдельную тестовую PostgreSQL с миграцией и тестовым курсом.

**Контракт воркера.** Захватите ключ `grade:{attemptId}` с типом `grade` и JSON `{"attemptId":"…"}`, увеличьте `tries` и передайте ID и `lease_until` в `app.ai.jobs.handle_grade_job` вместе с подключением к БД без открытой транзакции. Аренда должна покрывать основной и резервный HTTP-вызовы. Обработчик сохраняет итог и ставит задачу типа `deliver_grade` с полем `attemptId` в одной транзакции. Доставка читает актуальный результат попытки.

Временные сбои возвращают задачу в очередь через 15 и 60 секунд с учётом `Retry-After`. После исчерпания попыток результат отмечается `failed`. Повтор по кнопке должен вернуть то же задание в `queued` и сбросить `tries`. Подключение обработчика к захвату задач и доставка в Telegram входят в CORE-02 и TG-02. Резерв можно задать через `MODEL_FALLBACK_URL`, `MODEL_FALLBACK_ID` и `MODEL_FALLBACK_API_KEY`.

### Ветки и PR

Возьмите задачу из бэклога и назовите ветку по её ID, например `feat/core-01-schema`. В PR укажите ID, как проверить критерии приёмки и какие контракты изменились. Первую миграцию ведёт владелец CORE. Для интеграции используйте тестовый курс, тестовый Telegram-чат и обезличенные ответы. Реальные токены и ключи задаются через локальное окружение или хранилище секретов стенда.
