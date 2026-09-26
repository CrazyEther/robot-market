# Робот Маркет

Русскоязычная платформа для подбора роботизированных решений для склада, аэропорта и медицинского учреждения. Приложение хранит проекты и исходные параметры, импортирует каталог оборудования с сохранением происхождения каждой записи и показывает отдельные предложения одного производителя или модели.

## Запуск

Нужны Python 3.11+, разрешённая к использованию копия `catalog_export_v4.csv` и проверенный реестр первичных сведений `verified_claims.json`. Исходные файлы не входят в репозиторий.

### Python

```sh
python -m venv .venv
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py import_catalog /absolute/path/to/catalog_export_v4.csv --expected-sha256 VERIFIED_SOURCE_SHA256
python manage.py import_evidence /absolute/path/to/verified_claims.json --expected-sha256 VERIFIED_CLAIMS_SHA256
python manage.py runserver
```

Адрес приложения: `http://127.0.0.1:8000/`; каталог: `/catalog/`. Пользователь создаёт учётную запись через раздел «Войти» → «Создать учётную запись», затем открывает «Мои проекты». Администратора можно создать командой `python manage.py createsuperuser`.

### Docker Compose

Задайте `CATALOG_SOURCE_FILE` и `RESEARCH_CLAIMS_FILE` как абсолютные пути к разрешённым файлам, `CATALOG_SOURCE_SHA256` и `RESEARCH_CLAIMS_SHA256` как их проверенные контрольные суммы, `DB_PASSWORD` как пароль PostgreSQL и `DJANGO_SECRET_KEY` как секретный ключ Django. Для локального HTTP задайте `DEBUG=1`; для внешнего HTTPS настройте домен, TLS proxy и переменные `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `ENFORCE_HTTPS=1`, `TRUST_PROXY_SSL_HEADER=1` только при доверенном прокси.

```sh
docker compose up --build
```

Контейнер читает оба источника из томов только для чтения и импортирует их при запуске. Он завершится с ошибкой, если файл недоступен или не соответствует ожидаемой версии. Адрес по умолчанию: `http://localhost:8000/`; порт задаётся через `WEB_PORT`.

Для внешнего окружения вместо томов задайте `CATALOG_SOURCE_URL` и `RESEARCH_CLAIMS_URL` — закрытые HTTPS-адреса разрешённых файлов — и обе контрольные суммы. При запуске источники скачиваются, проверяются по SHA-256 и затем импортируются. Срок действия доступа к URL должен покрывать перезапуски сервиса. Сырые данные не включаются в образ. Процедура получения сведений описана в [документации источников](docs/SOURCE_PIPELINE.md).

## Проверка

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
CATALOG_SOURCE_PATH=/absolute/path/to/catalog_export_v4.csv RESEARCH_CLAIMS_PATH=/absolute/path/to/verified_claims.json SUPPLEMENT_MANIFEST_PATH=/absolute/path/to/supplement_products.json SUPPLEMENT_ASSET_DIR=/absolute/path/to/archived_sources python manage.py test
```

Проверки импорта используют исходный файл, указанный в `CATALOG_SOURCE_PATH`, отдельно проверенный реестр первичных утверждений `RESEARCH_CLAIMS_PATH` и закрытое дополнение производителей с точными первичными снимками. Файлы предоставляются вне Git. Загружать медицинские данные с персональными сведениями нельзя.
