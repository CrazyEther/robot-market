FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY manage.py ./
COPY robotmarket/ robotmarket/
COPY demo/ demo/
COPY templates/ templates/
COPY static/ static/
RUN DJANGO_SECRET_KEY=build-only-secret DEBUG=0 python manage.py collectstatic --noinput
RUN chmod -R a+rX /app && useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py seed_demo && exec gunicorn robotmarket.wsgi:application --bind 0.0.0.0:8000 --workers 2 --access-logfile -"]
