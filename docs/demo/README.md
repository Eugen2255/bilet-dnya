# Сценарий демонстрации «Билета дня»

Подготовьте отдельную учебную группу, организатора и трёх студентов. Три ответа по одному билету позволяют показать типичную ошибку в сводке. Для ручного показа возьмите [вопрос](questions.txt) и [материалы](materials.txt). Вопрос можно экспортировать в PDF, а один из материалов — сфотографировать для проверки OCR.

## Показ в Telegram · около 10 минут

| Этап | Действие и ожидаемый результат |
| --- | --- |
| Подключение | Организатор выполняет `/bind`, подтверждает привязку и открывает `/app`. Три студента подключаются через «Ответить». |
| Импорт | Организатор создаёт курс и загружает PDF с вопросом и фото с материалом. В распознанном вопросе уточняет формулировку, открывает цитату, утверждает эталон. |
| Запуск | Организатор задаёт расписание и нажимает «Начать тренировку». Для показа живой публикации заранее настройте день и время в тестовом чате. |
| Ответ | Откройте утренний пост и личную порцию. Отправьте ответ «Ключ идентифицирует строку» через Reply на вопрос. Покажите подтверждение, балл, недостающие критерии и эталон. |
| Карта | Откройте `/app` как студент и покажите карточку билета. Затем переключитесь на организатора и покажите число ответивших. |
| Сводка | Покажите вечерний пост после ответов трёх участников. В сводке используются итоги на время среза. При поддержке минимум трёх ответов модель может выбрать типичную ошибку. |
| Повтор | Покажите даты личного и группового повтора. Для демонстрации выдачи первого и второго повтора запустите локальный сценарий ниже с управляемой датой. |

Путь от установки до рабочего тестового бота описан в [инструкции HTTPS-стенда](../../deploy/README.md). Подробности для участников собраны в [руководстве пользователя](../user-guide.md).

## Воспроизводимое локальное демо

Тест запускается с пустой PostgreSQL, подставными Telegram и моделью. Он проходит HTTP webhook, авторизацию Mini App, импорт PDF и JPEG, OCR, проверку цитаты, утверждение, расписание, ответы, баллы, сводку и два повтора. Для OCR установите Tesseract с английским языком и добавьте его в `PATH` или задайте `TESSERACT_BIN`. Команды выполняются из корня репозитория в Python-окружении с `requirements-test.txt`.

```powershell
python -m pip install -r requirements-test.txt
$env:POSTGRES_PORT = '15447'
$env:BILET_TEST_DATABASE_URL = 'postgresql://bilet:bilet_local@127.0.0.1:15447/bilet'
$demoProject = 'bilet-demo-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$demoCompose = @('--env-file', '.env.example', '-p', $demoProject)
try {
    docker compose @demoCompose up -d --wait db
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL не запустилась' }
    docker compose @demoCompose exec -T db psql -X -v ON_ERROR_STOP=1 -U bilet -d bilet -f /db/migrate.sql
    if ($LASTEXITCODE -ne 0) { throw 'Ошибка миграции' }
    python -m unittest discover -s tests -p test_demo_flow.py -v
    if ($LASTEXITCODE -ne 0) { throw 'Демо завершилось ошибкой' }
} finally {
    docker compose @demoCompose down --volumes
}
```

На Linux и macOS выполните тот же прогон отдельным проектом Compose.

```sh
(
  set -e
  export POSTGRES_PORT=15447
  export BILET_TEST_DATABASE_URL=postgresql://bilet:bilet_local@127.0.0.1:15447/bilet
  demo_project="bilet-demo-$(date +%s)-$$"
  demo() { docker compose --env-file .env.example -p "$demo_project" "$@"; }
  trap 'demo down --volumes' EXIT
  demo up -d --wait db
  demo exec -T db psql -X -v ON_ERROR_STOP=1 -U bilet -d bilet -f /db/migrate.sql
  python -m unittest discover -s tests -p test_demo_flow.py -v
)
```

В успешном прогоне есть строка `Demo verified: PDF + OCR, 3/3 answers, mean 2/5` и две даты повтора. Первый повтор выдаётся через два дня от исходного билета, второй — через семь дней от первого повтора. В тестовом курсе учебные дни включают всю неделю. Результаты проверки записаны в [отчёте](../pilot-report.md).
