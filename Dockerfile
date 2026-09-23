FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/uploads /app/.paddlex && chown -R app:app /app/uploads /app/.paddlex
USER app
EXPOSE 8002

CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8002"]
