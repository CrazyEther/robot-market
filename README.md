# Робот Маркет

Русскоязычная платформа для подбора роботизированных решений для склада, аэропорта и медицинского учреждения. Приложение хранит проекты и исходные параметры, импортирует каталог оборудования с сохранением происхождения каждой записи и показывает отдельные предложения одного производителя или модели.

## Локальный запуск

Полный перечень пользовательских полей, форматов CSV и порядок заполнения: [Справочник ввода данных](docs/INPUT_REFERENCE.md). Для каталога нужны разрешённая к использованию копия `catalog_export_v4.csv` и проверенный реестр `verified_claims.json`. Эти источники намеренно не хранятся в Git и не включаются в Docker-образ; получите их отдельно и держите вне репозитория.

Рекомендуемый вариант для Windows — Docker Desktop с включённым Docker Compose v2. Альтернатива без Docker требует Python 3.11+.

### Docker Compose (Windows PowerShell)

1. Установите и запустите Docker Desktop. Убедитесь, что Docker Engine работает.
2. В корне репозитория создайте локальный файл окружения:

```powershell
Copy-Item .env.example .env
notepad .env
```

3. В `.env` замените все примеры на свои значения:

- `DJANGO_SECRET_KEY` и `DB_PASSWORD` — локальные секреты. Сгенерировать случайную строку можно командой `python -c "import secrets; print(secrets.token_urlsafe(48))"`; запустите её отдельно для каждого значения.
- `CATALOG_SOURCE_FILE` и `RESEARCH_CLAIMS_FILE` — абсолютные пути к разрешённым локальным файлам. В Windows используйте прямые слеши, например `D:/private/catalog_export_v4.csv`. Если путь содержит пробелы, заключите значение в кавычки.
- `CATALOG_SOURCE_SHA256` и `RESEARCH_CLAIMS_SHA256` — ожидаемые контрольные суммы из доверенного источника/манифеста. Для проверки локальной копии используйте `Get-FileHash <путь> -Algorithm SHA256`; не считайте неизвестный файл доверенным только потому, что вычислили для него хеш.
- `DEBUG=1` оставьте для локального HTTP. `WEB_PORT=8000` задаёт порт браузера; если он занят, замените, например, на `8001`.

Файл `.env` уже исключён из Git и Docker build context. Не добавляйте в репозиторий `.env`, каталоги исходников, экспортированные базы, пароли или реальные персональные данные.

4. Проверьте переменные и конфигурацию Compose, не запуская контейнеры:

```powershell
docker compose config --quiet
```

5. Соберите и запустите приложение:

```powershell
docker compose up --build -d
docker compose ps
```

При первом запуске Compose поднимет PostgreSQL, дождётся его health-check, проверит оба подключённых источника по SHA-256, выполнит миграции и импортирует каталог с первичными сведениями. Если источник отсутствует, недоступен или checksum не совпадает, web-контейнер остановится с ошибкой вместо запуска с неподтверждёнными данными.

6. Дождитесь готовности приложения:

```powershell
(Invoke-WebRequest -UseBasicParsing http://localhost:8000/ready).StatusCode
```

Ответ `200` означает, что приложение доступно, БД подключена и нужные версии каталога опубликованы. Откройте `http://localhost:8000/`. Поменяли `WEB_PORT` на `8001` — используйте `http://localhost:8001/`.

7. Зарегистрируйте пользователя через «Войти» → «Создать учётную запись». Для административного доступа создайте отдельного пользователя:

```powershell
docker compose exec web python manage.py createsuperuser
```

Полезные команды:

```powershell
docker compose logs --tail 100 web
docker compose logs -f web
docker compose down
```

`docker compose down` останавливает сервисы, но сохраняет данные PostgreSQL в Docker volume. **Не добавляйте `-v` без резервной копии и явного намерения удалить все локальные проекты и версии каталога.** Новые источники при последующих запусках должны соответствовать заданным SHA-256.

В Compose используются локальные bind mounts с read-only доступом. Переменные `CATALOG_SOURCE_URL` и `RESEARCH_CLAIMS_URL` предназначены для развёртывания с закрытыми HTTPS-источниками в хостинге; текущий `compose.yaml` не заменяет ими обязательные локальные файлы. Для внешнего HTTPS, доверенного TLS-прокси и Render см. [документацию развёртывания](docs/DEPLOY.md) и [описание источников](docs/SOURCE_PIPELINE.md).

### Python и SQLite

Этот режим подходит для разработки без Docker. PowerShell-команды из корня репозитория:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Если политика PowerShell запрещает активацию окружения, не меняйте её ради проекта: запускайте `.\.venv\Scripts\python.exe` вместо `python` в следующих командах.

Укажите пути к внешним файлам и их **заранее утверждённые** контрольные суммы. Пример ниже только иллюстрирует формат; замените значения на свои:

```powershell
$catalog = 'D:\private\catalog_export_v4.csv'
$claims = 'D:\private\verified_claims.json'
$catalogSha = 'APPROVED_CATALOG_SHA256'
$claimsSha = 'APPROVED_CLAIMS_SHA256'

Get-FileHash $catalog -Algorithm SHA256
Get-FileHash $claims -Algorithm SHA256
python manage.py migrate
python manage.py import_catalog $catalog --expected-sha256 $catalogSha
python manage.py import_evidence $claims --expected-sha256 $claimsSha
python manage.py runserver 127.0.0.1:8000
```

При запуске без `DB_HOST` приложение использует `local.sqlite3` в корне проекта; этот файл исключён из Git и сохраняется между перезапусками. Откройте `http://127.0.0.1:8000/`. Создание учётной записи и импорт данных работают так же, как в Docker.

## Проверка

Быстрые проверки без закрытых исходников:

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test projects.test_matching_units
```

Полный тестовый набор требует разрешённую копию v4, проверенный реестр первичных утверждений и закрытое дополнение производителей с исходными снимками. Перед запуском задайте `CATALOG_SOURCE_PATH`, `CATALOG_SOURCE_SHA256`, `RESEARCH_CLAIMS_PATH`, `RESEARCH_CLAIMS_SHA256`, `SUPPLEMENT_MANIFEST_PATH` и `SUPPLEMENT_ASSET_DIR` в текущей PowerShell-сессии, затем выполните:

```powershell
python manage.py test
```

Исходные файлы предоставляются отдельно от Git. Медицинские данные с персональной информацией в локальные и демонстрационные проекты не загружайте.
