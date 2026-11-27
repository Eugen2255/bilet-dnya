# HTTPS-стенд «Билет дня»

## Запуск на VPS

Нужны Linux, Docker Compose v2, домен с DNS-записью на VPS и входящие TCP-порты 80 и 443. Caddy выпускает сертификат и перенаправляет HTTP на HTTPS. Compose публикует только порты прокси. [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https) · [Docker Compose merge](https://docs.docker.com/reference/compose-file/merge/)

Из корня репозитория создайте приватный файл окружения и заполните значения для тестового бота и модели. Согласуйте `POSTGRES_PASSWORD` со строкой `DATABASE_URL`. Для пароля с буквами и цифрами подойдёт `openssl rand -hex 32`. `MINI_APP_URL` должен содержать HTTPS-адрес на домене `BILET_DOMAIN` с суффиксом `/app/`.

```sh
umask 077
cp deploy/env.example .env.deploy
chmod 600 .env.deploy
```

Для команд ниже определите функцию в текущем shell.

```sh
stack() {
    docker compose --env-file .env.deploy -f compose.yaml -f compose.deploy.yaml "$@"
}
stack up -d --build
stack ps --all
stack exec -T worker python -m app.operations init-ledger
```

Сервис `migrate` применяет схему до запуска API и воркера. Проверьте `https://<домен>/health` и `https://<домен>/app/`. Зарегистрируйте webhook тестового бота, подставив домен. Команда вызывается один раз для каждого адреса webhook. [Telegram Bot API](https://core.telegram.org/bots/api#setwebhook)

```sh
stack exec -T api python -m app.telegram https://study.example.org/telegram/webhook
```

Затем добавьте бота администратором в тестовую группу, выполните `/bind`, откройте `/app` в личке и создайте тестовый курс. Секреты хранятся в `.env.deploy` с ограниченным доступом.

## Данные и наблюдаемость

- `/delete_me` выдаёт персональное подтверждение на 10 минут. Удаление убирает аккаунт, сессии, личные задания, ответы, жалобы, состояние повторов и псевдоним из сохранённого рейтинга. Уже отправленные сообщения остаются в Telegram.
- Воркер очищает ответы и свободный текст разбора по достижении 7 дней после экзамена по часовому поясу курса. Баллы и агрегаты хранятся до удаления курса через 30 дней. Организатор может удалить курс раньше из Mini App. Временные оригиналы ограничены 24 часами.
- `operation_errors` хранит внутренний ID задания, код ошибки и время до 7 дней. Для stdout/stderr долгоживущих контейнеров Compose использует драйвер Docker `none`. Вывод миграции доступен через `stack logs migrate`.

```sh
stack exec -T worker python -m app.operations metrics
```

`acknowledgement_p95_ms` и `grade_delivery_p95_ms` измеряются от приёма ответа до первого успешного вызова Telegram для подтверждения и оценки. Отчёт также включает объём выборки, возраст готовой очереди, ошибки провайдеров и недоставки за последние 7 дней. `null` означает, что успешных измерений за это окно ещё нет.

## Резервное копирование

На VPS запускайте `sh deploy/backup.sh` ежедневно. Команда сохраняет PostgreSQL в `data/backups/bilet-<UTC>.backup/db.dump` и записывает ID моделей в `settings.json`. Для отдельного диска задайте `BILET_BACKUP_DIR`. `sh deploy/backup.sh --prune` удаляет файлы копий по достижении 7 дней. Запланируйте её ежечасно через cron или таймер VPS. [PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)

Том `privacy_ledger` хранится отдельно от PostgreSQL. Для восстановления на другом сервере сохраните актуальную SQLite-копию журнала в защищённом хранилище отдельно от дампа. SQLite создаёт согласованную копию во время работы по следующей команде. Ограничьте доступ к журналу и дампу владельцем стенда.

```sh
umask 077
mkdir -p data
stack exec -T worker python -c 'import os, sqlite3; db=sqlite3.connect(os.environ["PRIVACY_LEDGER_PATH"]); dst=sqlite3.connect("/srv/bilet/privacy/ledger-copy.sqlite3"); db.backup(dst); dst.close(); db.close()'
stack cp worker:/srv/bilet/privacy/ledger-copy.sqlite3 ./data/ledger-copy.sqlite3
chmod 600 ./data/ledger-copy.sqlite3
stack exec -T worker rm -f /srv/bilet/privacy/ledger-copy.sqlite3
```

## Восстановление

Используйте архив за последние 7 дней и актуальный журнал удалений. При переносе на другой VPS сначала восстановите журнал в том `privacy_ledger` по пути `/srv/bilet/privacy/deletions.sqlite3` с правами записи пользователя воркера. На том же VPS команда использует существующий том.

После запуска PostgreSQL и сборки образа выполните из корня репозитория команду с путём к своему архиву и свободным именем БД.

```sh
sh deploy/restore.sh data/backups/bilet-20261127T030000Z.backup bilet_restore
```

Скрипт создаёт новую БД, загружает дамп, применяет миграции и повторно выполняет подтверждённые удаления. Он погашает старые сессии и задания доставки из бэкапа. Для переключения на проверенную БД остановите API, воркер и прокси, повторите финальный прогон журнала и поменяйте имя БД в `POSTGRES_DB` и `DATABASE_URL` файла `.env.deploy`.

```sh
stack stop proxy api worker
stack run --rm --no-deps -T worker python -m app.operations replay-restored --database-name bilet_restore
# Обновите POSTGRES_DB и DATABASE_URL в .env.deploy.
stack up -d
```
