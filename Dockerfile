FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "python manage.py init-db && exec gunicorn --bind 0.0.0.0:8000 --workers 3 app:app"]
