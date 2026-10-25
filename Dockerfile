FROM python:3.13.17-alpine3.24@sha256:bf12ecc0bea3f171d55145dd02c78b5eeb09f30ee2ac9cc2b4c9e4e8f5e30741

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY server/__init__.py server/app.py server/hermes.py server/notifications.py server/pwa.py /app/server/
COPY server/web/app.js server/web/icon-192.png server/web/icon-512.png server/web/icon.svg server/web/index.html server/web/manifest.webmanifest server/web/rich-text.js server/web/styles.css server/web/sw.js /app/server/web/

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
