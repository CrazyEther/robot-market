FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY manage.py ./
COPY robotmarket/ robotmarket/
COPY demo/ demo/
COPY projects/ projects/
COPY catalog/ catalog/
COPY templates/ templates/
COPY static/ static/
RUN DJANGO_SECRET_KEY=build-only-secret DEBUG=0 python manage.py collectstatic --noinput
RUN chmod -R a+rX /app && useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "python -m catalog.bootstrap_source && python -m catalog.bootstrap_evidence && python manage.py migrate --noinput && python manage.py import_catalog \"${CATALOG_SOURCE_PATH:-/tmp/robot-market-catalog-v4.csv}\" --expected-sha256 \"$CATALOG_SOURCE_SHA256\" && python manage.py import_evidence \"${RESEARCH_CLAIMS_PATH:-/tmp/robot-market-verified-claims.json}\" --expected-sha256 \"$RESEARCH_CLAIMS_SHA256\" && exec gunicorn robotmarket.wsgi:application --bind 0.0.0.0:8000 --workers 2 --access-logfile -"]
