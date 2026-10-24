"""Agentic Compendium: compact SQLite delivery and PWA HTTP service.

This public release contains no private deployment settings or project adapters.
"""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DELIVERY_FIELDS = ("id", "title", "publisher", "section", "summary", "systemImage", "body", "deliveredAt", "needsAttention")
MAX_BODY_BYTES = 1_048_576
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")

class ValidationError(ValueError):
    pass


class ConflictError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty ISO 8601 string")
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return isoformat(parsed)


def validate_delivery(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("delivery must be a JSON object")
    missing = [field for field in DELIVERY_FIELDS if field not in payload]
    extra = sorted(set(payload) - set(DELIVERY_FIELDS))
    if missing:
        raise ValidationError(f"missing fields: {', '.join(missing)}")
    if extra:
        raise ValidationError(f"unknown fields: {', '.join(extra)}")

    delivery_id = payload["id"]
    if not isinstance(delivery_id, str) or not ID_PATTERN.fullmatch(delivery_id):
        raise ValidationError("id must be 1-200 safe identifier characters")

    result: dict[str, Any] = {"id": delivery_id}
    for field in ("title", "publisher", "section", "summary", "systemImage", "body"):
        value = payload[field]
        if not isinstance(value, str):
            raise ValidationError(f"{field} must be a string")
        if field in {"title", "publisher", "section", "systemImage"} and not value.strip():
            raise ValidationError(f"{field} must not be empty")
        result[field] = value

    if not isinstance(payload["needsAttention"], bool):
        raise ValidationError("needsAttention must be a boolean")
    result["deliveredAt"] = parse_timestamp(payload["deliveredAt"], "deliveredAt")
    result["needsAttention"] = payload["needsAttention"]
    return result


def canonical_delivery_json(delivery: dict[str, Any]) -> str:
    return json.dumps(delivery, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_cursor(updated_at: int, delivery_id: str) -> str:
    raw = json.dumps([updated_at, delivery_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[0], int)
            or not isinstance(value[1], str)
        ):
            raise ValueError
        return value[0], value[1]
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("cursor is invalid") from exc


class DeliveryDatabase:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_deliveries_updated
                    ON deliveries(updated_at, id);
                """
            )

    def ingest(self, delivery: dict[str, Any]) -> tuple[dict[str, Any], bool, int]:
        payload_json = canonical_delivery_json(delivery)
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT payload_json, updated_at FROM deliveries WHERE id = ?", (delivery["id"],)
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload_json:
                    raise ConflictError("delivery id already exists with different content")
                return json.loads(existing["payload_json"]), False, existing["updated_at"]

            updated_at = time.time_ns()
            connection.execute(
                "INSERT INTO deliveries(id, payload_json, updated_at) VALUES (?, ?, ?)",
                (delivery["id"], payload_json, updated_at),
            )
            return delivery, True, updated_at

    def sync(self, cursor: str | None, limit: int) -> tuple[list[dict[str, Any]], str | None, bool]:
        if cursor:
            cursor_updated, cursor_id = decode_cursor(cursor)
        else:
            cursor_updated, cursor_id = -1, ""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, payload_json, updated_at
                FROM deliveries
                WHERE updated_at > ? OR (updated_at = ? AND id > ?)
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                (cursor_updated, cursor_updated, cursor_id, limit + 1),
            ).fetchall()

        has_more = len(rows) > limit
        page = rows[:limit]
        deliveries = [json.loads(row["payload_json"]) for row in page]
        next_cursor = cursor
        if page:
            last = page[-1]
            next_cursor = encode_cursor(last["updated_at"], last["id"])
        return deliveries, next_cursor, has_more

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])


class DeliveryService:
    def __init__(
        self,
        database: DeliveryDatabase,
        read_token: str,
        publish_token: str,
        project_source: Any | None = None,
    ):
        if not read_token or not publish_token:
            raise ValueError("read and publish bearer tokens are required")
        if hmac.compare_digest(read_token, publish_token):
            raise ValueError("read and publish bearer tokens must be different")
        self.database = database
        self.read_token = read_token
        self.publish_token = publish_token
        self.project_source = project_source
        self.web = None

    def authorised(self, header: str | None, token: str) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:], token)


def make_handler(service: DeliveryService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgenticCompendiumDelivery/1"

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

        def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def require_token(self, token: str) -> bool:
            if service.authorised(self.headers.get("Authorization"), token):
                return True
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorised"})
            return False

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if service.web is not None and service.web.dispatch(self, "GET"):
                return
            if parsed.path == "/healthz":
                self.send_json(
                    HTTPStatus.OK,
                    {"status": "ok", "service": "agentic-compendium-delivery"},
                )
                return
            if parsed.path == "/v1/status":
                if not self.require_token(service.read_token):
                    return
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "agentic-compendium-delivery",
                        "deliveryCount": service.database.count(),
                    },
                )
                return
            if parsed.path == "/v1/deliveries":
                if not self.require_token(service.read_token):
                    return
                try:
                    params = parse_qs(parsed.query, keep_blank_values=True)
                    cursor = params.get("cursor", [None])[0] or None
                    raw_limit = params.get("limit", [str(DEFAULT_PAGE_SIZE)])[0]
                    limit = int(raw_limit)
                    if not 1 <= limit <= MAX_PAGE_SIZE:
                        raise ValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
                    deliveries, next_cursor, has_more = service.database.sync(cursor, limit)
                except (ValueError, ValidationError) as exc:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                now = isoformat(utc_now())
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "version": 1,
                        "deliveries": deliveries,
                        "syncState": {
                            "source": "remote",
                            "cachedAt": now,
                            "lastSuccessfulRefreshAt": now,
                            "cursor": next_cursor,
                        },
                        "hasMore": has_more,
                    },
                )
                return
            if parsed.path == "/v1/projects":
                if not self.require_token(service.read_token):
                    return
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "project source unavailable"})
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if service.web is not None and service.web.dispatch(self, "POST"):
                return
            if urlparse(self.path).path != "/v1/deliveries":
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self.require_token(service.publish_token):
                return
            try:
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise ValidationError("Content-Length is required")
                length = int(raw_length)
                if length < 1 or length > MAX_BODY_BYTES:
                    raise ValidationError("request body size is invalid")
                payload = json.loads(self.rfile.read(length))
                delivery = validate_delivery(payload)
                stored, created, updated_at = service.database.ingest(delivery)
            except ConflictError as exc:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self.send_json(
                HTTPStatus.CREATED if created else HTTPStatus.OK,
                {
                    "delivery": stored,
                    "created": created,
                    "cursor": encode_cursor(updated_at, stored["id"]),
                },
            )

        def do_PUT(self) -> None:
            if service.web is not None and service.web.dispatch(self, "PUT"):
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


def database_from_environment() -> DeliveryDatabase:
    return DeliveryDatabase(os.environ.get("APP_DB_PATH", "./data/deliveries.sqlite3"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agentic Compendium")
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--host", default=os.environ.get("APP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "8790")))
    args = parser.parse_args(argv)

    database = database_from_environment()
    service = DeliveryService(database, os.environ.get("APP_READ_TOKEN", ""),
                              os.environ.get("APP_PUBLISH_TOKEN", ""))
    owner_password = os.environ.get("APP_OWNER_PASSWORD", "")
    if owner_password:
        from server.pwa import OwnerWeb, PersonalState
        origin = os.environ.get("APP_PUBLIC_ORIGIN", "")
        allowed = [item.strip() for item in os.environ.get("APP_HERMES_ALLOWED_ORIGINS", "").split(",") if item.strip()]
        web = OwnerWeb(PersonalState(database.path), owner_password, origin,
                       hermes_allowed=allowed)
        web.delivery_db = database
        service.web = web
        web.start_poller(max(60, int(os.environ.get("APP_HERMES_POLL_SECONDS", "300"))))
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    print(f"Agentic Compendium listening on {args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
