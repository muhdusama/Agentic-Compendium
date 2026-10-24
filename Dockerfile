FROM python:3.13-alpine

WORKDIR /app
COPY server/ /app/server/

RUN addgroup -g 1000 app && adduser -D -u 1000 -G app app \
    && mkdir -p /data && chown -R app:app /app /data

USER app
ENV APP_DB_PATH=/data/deliveries.sqlite3 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8790 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8790
CMD ["python", "-m", "server.app", "serve"]
