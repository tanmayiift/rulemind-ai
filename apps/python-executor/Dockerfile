FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY apps/python-executor/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./alembic.ini
COPY apps/python-executor/app ./app
COPY apps/python-executor/alembic ./alembic
COPY apps/python-executor/package.json ./package.json
RUN mkdir -p /app/apps/python-executor \
  && ln -s /app/app /app/apps/python-executor/app \
  && ln -s /app/alembic /app/apps/python-executor/alembic

EXPOSE 8080

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "4"]
