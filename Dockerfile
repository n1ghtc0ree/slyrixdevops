FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY scripts/ /app/scripts/
COPY loop.sh /app/loop.sh

CMD ["sh", "/app/loop.sh"]
