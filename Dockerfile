FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Secrets are never baked in — pass them at runtime:
#   docker run --env-file .env <image>
# or via docker-compose environment / secrets.

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
