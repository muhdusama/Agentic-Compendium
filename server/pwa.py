"""Single-owner, LAN-hosted PWA and versioned personal-state sync.

Runs in the existing Delivery Service process and SQLite database. Published
deliveries keep their original API, tokens and immutable table unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookies
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet, InvalidToken

from server.app import (SQLITE_TIMEOUT_SECONDS, ConflictError, ValidationError, canonical_delivery_json,
                        validate_delivery)
from server.hermes import HermesAdapter, HermesError, checked_hermes_origin
from server.notifications import Notifications

STATE_KINDS = {"channel", "section", "note", "route", "job", "placement", "hidden-delivery",
               "setting", "source", "delivery"}
KEY = re.compile(r"^[A-Za-z0-9._:@|/-]{1,250}$")
OWNER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{2,31}$")
PASSWORD_ITERATIONS = 600_000
MAX_STATE_BODY = 4 * 1024 * 1024
# Native exports retain 64 MiB; base64 PWA envelopes have a bounded larger limit.
MAX_IMPORT_BODY = 64 * 1024 * 1024
MAX_PWA_ARCHIVE_BODY = 256 * 1024 * 1024
MAX_FAILED_LOGIN_IPS = 1024
LOGIN_FAILURE_WINDOW = 300
LOGIN_FAILURE_LIMIT = 8
STATIC = {
    "/app/": ("index.html", "text/html; charset=utf-8"),
    "/app/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/app/rich-text.js": ("rich-text.js", "application/javascript; charset=utf-8"),
    "/app/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/app/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/app/icon.svg": ("icon.svg", "image/svg+xml"),
    "/app/icon-192.png": ("icon-192.png", "image/png"),
    "/app/icon-512.png": ("icon-512.png", "image/png"),
}


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def checked_string(value, name, limit=250):
    if not isinstance(value, str) or len(value) > limit or not KEY.fullmatch(value):
        raise ValidationError(f"invalid {name}")
    return value


def checked_record(value):
    if not isinstance(value, dict) or set(value) != {"sourceId", "kind", "key", "baseVersion", "deleted", "value"}:
        raise ValidationError("invalid record")
    source = checked_string(value["sourceId"], "sourceId")
    kind = value["kind"]
    if not isinstance(kind, str) or kind not in STATE_KINDS:
        raise ValidationError("invalid kind")
    key = checked_string(value["key"], "key")
    version = value["baseVersion"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValidationError("invalid baseVersion")
    if not isinstance(value["deleted"], bool) or not isinstance(value["value"], dict):
        raise ValidationError("invalid record payload")
    if len(json_bytes(value["value"])) > MAX_STATE_BODY:
        raise ValidationError("record is too large")
    return source, kind, key, version, value["deleted"], value["value"]


class PersonalState:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS app_state (
                  source_id TEXT NOT NULL, kind TEXT NOT NULL, record_key TEXT NOT NULL,
                  version INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                  value_json TEXT NOT NULL, PRIMARY KEY(source_id, kind, record_key)
                );
                CREATE TABLE IF NOT EXISTS app_changes (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                  kind TEXT NOT NULL, record_key TEXT NOT NULL, version INTEGER NOT NULL,
                  deleted INTEGER NOT NULL, value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_imports (
                  digest TEXT PRIMARY KEY, imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_native_archives (
                  digest TEXT PRIMARY KEY REFERENCES app_imports(digest),
                  payload BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_config (
                  name TEXT PRIMARY KEY, value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_sessions (
                  token_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_push_subscriptions (
                  id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, subscription_json TEXT NOT NULL, generation TEXT NOT NULL,
                  session_hash TEXT, cursor INTEGER NOT NULL, published_cursor INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, last_accepted_at TEXT, last_error TEXT,
                  next_attempt_at INTEGER NOT NULL DEFAULT 0, retry_delay INTEGER NOT NULL DEFAULT 0,
                  test_at INTEGER NOT NULL DEFAULT 0
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=SQLITE_TIMEOUT_SECONDS)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def as_record(row):
        return {"sourceId": row["source_id"], "kind": row["kind"],
                "key": row["record_key"], "version": row["version"],
                "deleted": bool(row["deleted"]), "value": json.loads(row["value_json"]),
                **({"seq": row["seq"]} if "seq" in row.keys() else {})}

    def find(self, source, kind, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM app_state WHERE source_id=? AND kind=? AND record_key=?",
                             (source, kind, key)).fetchone()
            return self.as_record(row) if row else None

    def all_kind(self, kind, source=None):
        with self.connect() as db:
            if source is None:
                rows = db.execute("SELECT * FROM app_state WHERE kind=? AND deleted=0", (kind,))
            else:
                rows = db.execute("SELECT * FROM app_state WHERE source_id=? AND kind=? AND deleted=0",
                                  (source, kind))
            return [self.as_record(row) for row in rows]

    def changes(self, cursor, limit):
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0 or cursor > 2**63 - 1:
            raise ValidationError("invalid cursor")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationError("invalid limit")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM app_changes WHERE seq>? ORDER BY seq LIMIT ?",
                              (cursor, limit + 1)).fetchall()
        page = rows[:limit]
        return {"changes": [self.as_record(row) for row in page],
                "cursor": page[-1]["seq"] if page else cursor, "hasMore": len(rows) > limit}

    @staticmethod
    def _upsert(db, source, kind, key, value, deleted, version):
        encoded = json_bytes(value).decode()
        db.execute("""INSERT INTO app_state(source_id,kind,record_key,version,deleted,value_json)
                      VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,kind,record_key)
                      DO UPDATE SET version=excluded.version, deleted=excluded.deleted,
                                    value_json=excluded.value_json""",
                   (source, kind, key, version, int(deleted), encoded))
        db.execute("""INSERT INTO app_changes(source_id,kind,record_key,version,deleted,value_json)
                      VALUES(?,?,?,?,?,?)""", (source, kind, key, version, int(deleted), encoded))

    def put(self, incoming):
        source, kind, key, base, deleted, value = checked_record(incoming)
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM app_state WHERE source_id=? AND kind=? AND record_key=?",
                             (source, kind, key)).fetchone()
            version = row["version"] if row else 0
            if base != version:
                return {"conflict": True, "current": self.as_record(row) if row else None}
            self._upsert(db, source, kind, key, value, deleted, version + 1)
            return {"record": {"sourceId": source, "kind": kind, "key": key,
                               "version": version + 1, "deleted": deleted, "value": value}}

    def config(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value_json FROM app_config WHERE name=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_config(self, key, value):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO app_config(name,value_json) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json",
                       (key, json_bytes(value).decode()))

    @staticmethod
    def _owner_verifier(username, password):
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
        return {"username": username, "salt": salt.hex(),
                "verifier": digest.hex(), "iterations": PASSWORD_ITERATIONS}

    @staticmethod
    def _owner_password_matches(record, candidate):
        if not isinstance(candidate, str) or len(candidate) > 256:
            return False
        try:
            expected = bytes.fromhex(record["verifier"])
            actual = hashlib.pbkdf2_hmac("sha256", candidate.encode(),
                                         bytes.fromhex(record["salt"]), record["iterations"])
            return hmac.compare_digest(expected, actual)
        except (TypeError, ValueError, KeyError):
            return False

    def initialise_owner(self, bootstrap_password):
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM app_config WHERE name='owner_credentials'").fetchone():
                return
            record = self._owner_verifier("owner", bootstrap_password)
            db.execute("INSERT INTO app_config(name,value_json) VALUES('owner_credentials',?)",
                       (json_bytes(record).decode(),))

    def owner_username(self):
        return self.config("owner_credentials")["username"]

    def verify_owner(self, username, password):
        record = self.config("owner_credentials")
        # Always pay the hashing cost so response time does not reveal the username.
        password_ok = self._owner_password_matches(record, password)
        return (isinstance(username, str) and OWNER_NAME.fullmatch(username) is not None and
                hmac.compare_digest(username, record["username"]) and password_ok)

    @staticmethod
    def _check_owner_update(username, new_password):
        if not isinstance(username, str) or not OWNER_NAME.fullmatch(username):
            raise ValidationError("Username must be 3–32 letters, digits, dots, underscores or hyphens")
        if new_password is not None and (not isinstance(new_password, str)
                                         or not 14 <= len(new_password) <= 256):
            raise ValidationError("New password must be 14–256 characters")

    def reset_owner(self, username, new_password):
        """Host-side recovery: replace the owner credentials without the old password."""
        self._check_owner_update(username, new_password)
        if new_password is None:
            raise ValidationError("New password must be 14–256 characters")
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO app_config(name,value_json) VALUES('owner_credentials',?) "
                       "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json",
                       (json_bytes(self._owner_verifier(username, new_password)).decode(),))
            db.execute("DELETE FROM app_sessions")
            db.execute("DELETE FROM app_push_subscriptions")

    def change_owner(self, current_password, username, new_password):
        self._check_owner_update(username, new_password)
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            entry = db.execute("SELECT value_json FROM app_config WHERE name='owner_credentials'").fetchone()
            current = json.loads(entry[0])
            if not self._owner_password_matches(current, current_password):
                raise PermissionError("Current owner password is incorrect")
            updated = self._owner_verifier(username, new_password) if new_password else {
                **current, "username": username}
            db.execute("UPDATE app_config SET value_json=? WHERE name='owner_credentials'",
                       (json_bytes(updated).decode(),))
            db.execute("DELETE FROM app_sessions")
            db.execute("DELETE FROM app_push_subscriptions")

    def create_session(self):
        token = secrets.token_urlsafe(32)
        expiry = int(time.time()) + 12 * 3600
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM app_sessions WHERE expires_at <= ?", (int(time.time()),))
            db.execute("INSERT INTO app_sessions(token_hash,expires_at) VALUES(?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), expiry))
        return token

    def has_session(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT expires_at FROM app_sessions WHERE token_hash=?", (digest,)).fetchone()
        return row is not None and row["expires_at"] > time.time()

    def revoke_session(self, token, notification_endpoint=None):
        if not isinstance(token, str):
            return
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            digest = hashlib.sha256(token.encode()).hexdigest()
            db.execute("DELETE FROM app_sessions WHERE token_hash=?",
                       (digest,))
            db.execute("DELETE FROM app_push_subscriptions WHERE session_hash=?", (digest,))
            if notification_endpoint:
                db.execute("DELETE FROM app_push_subscriptions WHERE endpoint=?", (notification_endpoint,))

    def import_archive(self, contents):
        if not isinstance(contents, (bytes, bytearray)):
            raise ValidationError("invalid portable archive")
        contents = bytes(contents)
        if len(contents) > MAX_PWA_ARCHIVE_BODY:
            raise ValidationError("portable archive exceeds the PWA import limit")
        try:
            payload = json.loads(contents)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError("invalid portable archive") from exc
        if isinstance(payload, dict) and payload.get("format") == "agentic-compendium-pwa":
            return self._import_pwa_archive(payload)
        if len(contents) > MAX_IMPORT_BODY:
            raise ValidationError("native archive exceeds the import limit")
        digest = hashlib.sha256(contents).hexdigest()
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValidationError("unsupported portable archive version")
        files, prefs, previews = payload.get("files"), payload.get("preferences"), payload.get("drawingPreviews")
        if not all(isinstance(item, list) for item in (files, prefs, previews)):
            raise ValidationError("missing portable archive data")
        originals = {}
        for entry in files:
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str) or path in originals or not (
                path in {"personal-notes.json", "deliveries-v1.json"}
                or re.fullmatch(r"archives/[0-9a-f]{64}\.json", path)
            ):
                raise ValidationError("invalid archive file path")
            try:
                raw = base64.b64decode(entry["contents"], validate=True)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValidationError("invalid archived file encoding") from exc
            if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
                raise ValidationError("archived file checksum mismatch")
            originals[path] = raw

        settings = {}
        for entry in prefs:
            if not isinstance(entry, dict) or entry.get("key") in settings:
                raise ValidationError("invalid archive preference")
            if entry.get("key") not in {"compendiumChannelLibraryV1", "hermesDeliveryPreferences",
                                         "hermesSourceIdentityAliases", "hermesDeliveryJobIDs",
                                         "customDeliverySections", "deliverySectionNavigationOrder",
                                         "hiddenPersonalDeliverySections", "hiddenDeliverySections"}:
                raise ValidationError("unrecognised archive preference")
            try:
                encoded = base64.b64decode(entry["contents"], validate=True)
                settings[entry["key"]] = json.loads(encoded)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValidationError("invalid archive preference encoding") from exc

        preview_map = {}
        for preview in previews:
            if not isinstance(preview, dict) or not isinstance(preview.get("noteID"), str):
                raise ValidationError("invalid drawing preview")
            if preview["noteID"] in preview_map:
                raise ValidationError("duplicate drawing preview")
            try:
                png = base64.b64decode(preview["png"], validate=True)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValidationError("invalid drawing preview") from exc
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValidationError("invalid PNG preview")
            preview_map[preview["noteID"]] = preview["png"]

        candidates = {}
        def add(source, kind, key, value):
            checked_record({"sourceId": source, "kind": kind, "key": key,
                            "baseVersion": 0, "deleted": False, "value": value})
            index = (source, kind, key)
            if index in candidates and candidates[index] != value:
                raise ValidationError("conflicting entries in archive")
            candidates[index] = value

        library = settings.get("compendiumChannelLibraryV1") or {}
        if library:
            if not isinstance(library, dict) or library.get("version") != 1:
                raise ValidationError("unsupported channel library")
            for index, channel in enumerate(library.get("channels", [])):
                add("library", "channel", channel["id"], {
                    "name": channel["name"], "archived": channel.get("isArchived", False), "order": index})
            for route, channel_id in library.get("jobRoutes", {}).items():
                if "|" in route and channel_id:
                    source, job_id = route.split("|", 1)
                    add(source, "route", job_id, {"channelId": channel_id})
            for placement, channel_id in library.get("placements", {}).items():
                if "|" in placement and channel_id:
                    source, delivery_id = placement.split("|", 1)
                    add(source, "placement", delivery_id, {"channelId": channel_id})
            for marker in library.get("deletedDeliveryIDs", []):
                if "|" in marker:
                    source, delivery_id = marker.split("|", 1)
                    add(source, "hidden-delivery", delivery_id, {"hidden": True})
            add("library", "setting", "timezone", {"identifier": library.get("timezoneIdentifier", "UTC")})

        for path, raw in originals.items():
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValidationError("invalid stored archive content")
            if path == "personal-notes.json":
                if data.get("version") not in (1, 2, 3) or not isinstance(data.get("notes"), list):
                    raise ValidationError("unsupported notes archive")
                for note in data["notes"]:
                    note_id = note["deliveryID"]
                    if note_id.startswith("source:") and "|" in note_id:
                        source, delivery_id = note_id[7:].split("|", 1)
                    else:
                        source, delivery_id = "legacy", note_id
                    value = dict(note)
                    value.pop("deliveryID", None)
                    # Raw PencilKit drawingData stays byte-identical; browser
                    # strokes are a separate layer, never an implicit conversion.
                    if note_id in preview_map:
                        value["drawingPreview"] = preview_map[note_id]
                    add(source, "note", delivery_id, value)
            else:
                if data.get("version") != 1 or not isinstance(data.get("deliveries"), list):
                    raise ValidationError("unsupported delivery archive")
                sync = data.get("syncState") or {}
                source = sync.get("remoteSourceIdentity") or ("legacy" if path == "deliveries-v1.json" else None)
                source = checked_string(source, "saved source")
                for delivery in data["deliveries"]:
                    item = dict(delivery)
                    if isinstance(item.get("deliveredAt"), (int, float)):
                        item["deliveredAt"] = datetime.fromtimestamp(
                            float(item["deliveredAt"]) + 978307200, tz=timezone.utc
                        ).isoformat().replace("+00:00", "Z")
                    # Preserve Hermes metadata and archive identity.
                    if "hermesJobID" not in item:
                        item["hermesJobID"] = None
                    add(source, "delivery", item["id"], item)

        aliases = settings.get("hermesSourceIdentityAliases") or {}
        if not isinstance(aliases, dict):
            raise ValidationError("invalid source identities")
        for source in set(aliases.values()):
            checked_string(source, "source identity")
            add(source, "source", "self", {"name": "Imported Hermes archive", "migrated": True})

        # Migrate enabled job subscriptions without changing Hermes schedules.
        preferences = settings.get("hermesDeliveryPreferences") or {}
        if not isinstance(preferences, dict):
            raise ValidationError("invalid job preferences")
        for source, rules in preferences.items():
            checked_string(source, "job source")
            if not isinstance(rules, dict):
                raise ValidationError("invalid job routing")
            for job_id in rules.get("enabledJobIDs", []):
                add(source, "job", job_id, {"enabled": True, "name": job_id})
            for job_id, group in rules.get("jobGroups", {}).items():
                group_id = rules.get("groupIDs", {}).get(group)
                if group_id and (source, "route", job_id) not in candidates:
                    add(source, "route", job_id, {"channelId": group_id})
        if aliases:
            add("library", "setting", "source-aliases", {"aliases": aliases})

        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM app_imports WHERE digest=?", (digest,)).fetchone():
                return {"imported": 0, "alreadyImported": True}
            for source, kind, key in candidates:
                existing = db.execute("SELECT value_json,deleted FROM app_state WHERE source_id=? AND kind=? AND record_key=?",
                                      (source, kind, key)).fetchone()
                if existing and (existing["deleted"] or json.loads(existing["value_json"]) != candidates[source, kind, key]):
                    raise ConflictError("archive conflicts with existing data; nothing imported")
            created = 0
            for (source, kind, key), value in candidates.items():
                exists = db.execute("SELECT 1 FROM app_state WHERE source_id=? AND kind=? AND record_key=?",
                                    (source, kind, key)).fetchone()
                if not exists:
                    self._upsert(db, source, kind, key, value, False, 1)
                    created += 1
            db.execute("INSERT INTO app_imports(digest,imported_at) VALUES(?,?)",
                       (digest, datetime.now(timezone.utc).isoformat()))
            # Keep the complete original export, not only the fields understood
            # by this PWA version. Unknown native fields remain recoverable.
            db.execute("INSERT INTO app_native_archives(digest,payload) VALUES(?,?)",
                       (digest, contents))
        return {"imported": created, "alreadyImported": False}

    def export_archive(self):
        """Return only portable owner data from one SQLite read transaction."""
        with self.lock, self.connect() as db:
            db.execute("BEGIN")
            records = [self.as_record(row) for row in db.execute(
                "SELECT * FROM app_state ORDER BY source_id,kind,record_key").fetchall()]
            published = [json.loads(row["payload_json"]) for row in db.execute(
                "SELECT payload_json FROM deliveries ORDER BY id").fetchall()]
            native_archives = [{"digest": row["digest"], "contents": base64.b64encode(row["payload"]).decode()}
                               for row in db.execute("SELECT digest,payload FROM app_native_archives ORDER BY digest").fetchall()]
        archive = {"format": "agentic-compendium-pwa", "version": 1, "records": records,
                   "published": published, "nativeArchives": native_archives}
        if len(json_bytes(archive)) > MAX_PWA_ARCHIVE_BODY:
            raise ValidationError("portable archive exceeds the PWA import limit")
        return archive

    def _import_pwa_archive(self, payload):
        if set(payload) != {"format", "version", "records", "published", "nativeArchives"}:
            raise ValidationError("invalid PWA archive")
        if payload["version"] != 1 or not all(isinstance(payload[name], list)
                                                for name in ("records", "published", "nativeArchives")):
            raise ValidationError("unsupported PWA archive version")
        records = {}
        for item in payload["records"]:
            if not isinstance(item, dict) or set(item) != {"sourceId", "kind", "key", "version", "deleted", "value"}:
                raise ValidationError("invalid PWA record")
            source, kind, key, version, deleted, value = checked_record({
                "sourceId": item["sourceId"], "kind": item["kind"], "key": item["key"],
                "baseVersion": item["version"], "deleted": item["deleted"], "value": item["value"]})
            if version < 1:
                raise ValidationError("invalid PWA record version")
            index = (source, kind, key)
            if index in records:
                raise ValidationError("duplicate PWA record")
            records[index] = (deleted, value)
        published = {}
        for item in payload["published"]:
            delivery = validate_delivery(item)
            if delivery["id"] in published:
                raise ValidationError("duplicate published delivery")
            published[delivery["id"]] = delivery
        native_archives = {}
        for item in payload["nativeArchives"]:
            if not isinstance(item, dict) or set(item) != {"digest", "contents"}:
                raise ValidationError("invalid native archive")
            digest = item["digest"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValidationError("invalid native archive digest")
            try:
                raw = base64.b64decode(item["contents"], validate=True)
            except (ValueError, TypeError) as exc:
                raise ValidationError("invalid native archive encoding") from exc
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValidationError("native archive checksum mismatch")
            if digest in native_archives:
                raise ValidationError("duplicate native archive")
            native_archives[digest] = raw
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for (source, kind, key), (deleted, value) in records.items():
                existing = db.execute("SELECT deleted,value_json FROM app_state WHERE source_id=? AND kind=? AND record_key=?",
                                      (source, kind, key)).fetchone()
                if existing and (bool(existing["deleted"]) != deleted or json.loads(existing["value_json"]) != value):
                    raise ConflictError("archive conflicts with existing data; nothing imported")
            for delivery_id, delivery in published.items():
                existing = db.execute("SELECT payload_json FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
                if existing and json.loads(existing["payload_json"]) != delivery:
                    raise ConflictError("archive conflicts with existing published delivery; nothing imported")
            for digest, raw in native_archives.items():
                existing = db.execute("SELECT payload FROM app_native_archives WHERE digest=?", (digest,)).fetchone()
                if existing and existing["payload"] != raw:
                    raise ConflictError("archive conflicts with existing native archive; nothing imported")
            created = 0
            for (source, kind, key), (deleted, value) in records.items():
                if not db.execute("SELECT 1 FROM app_state WHERE source_id=? AND kind=? AND record_key=?",
                                  (source, kind, key)).fetchone():
                    self._upsert(db, source, kind, key, value, deleted, 1); created += 1
            for delivery_id, delivery in published.items():
                if not db.execute("SELECT 1 FROM deliveries WHERE id=?", (delivery_id,)).fetchone():
                    db.execute("INSERT INTO deliveries(id,payload_json,updated_at) VALUES(?,?,?)",
                               (delivery_id, canonical_delivery_json(delivery), time.time_ns())); created += 1
            for digest, raw in native_archives.items():
                if not db.execute("SELECT 1 FROM app_native_archives WHERE digest=?", (digest,)).fetchone():
                    db.execute("INSERT OR IGNORE INTO app_imports(digest,imported_at) VALUES(?,?)",
                               (digest, datetime.now(timezone.utc).isoformat()))
                    db.execute("INSERT INTO app_native_archives(digest,payload) VALUES(?,?)", (digest, raw)); created += 1
        return {"imported": created, "alreadyImported": created == 0}


class OwnerWeb:
    def __init__(self, state, owner_password, public_origin, static_dir=None, hermes_allowed=None,
                 trusted_proxies=None, secret_key=""):
        if len(owner_password) < 20:
            raise ValueError("APP_OWNER_PASSWORD must be at least 20 characters")
        self.state = state
        self.state.initialise_owner(owner_password)
        self.origin = public_origin.rstrip("/")
        parsed = urlparse(self.origin)
        if (parsed.scheme not in ("https", "http") or not parsed.netloc or
                parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or
                (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"})):
            raise ValueError("invalid APP_PUBLIC_ORIGIN")
        self.static = Path(static_dir or Path(__file__).parent / "web")
        self.allowed_hermes = set(hermes_allowed or [])
        try:
            self.trusted_proxies = [ipaddress.ip_network(item, strict=False) for item in trusted_proxies or []]
        except ValueError as exc:
            raise ValueError("invalid APP_TRUSTED_PROXIES entry") from exc
        # Optional at-rest encryption of the Hermes password; any random secret works.
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())) if secret_key else None
        self._seal_stored_hermes_password()
        self.auth_lock = threading.Lock()
        self.failed_logins = {}
        self.hermes_probe_positions = {}
        self.poll_lock = threading.Lock()
        self.notifications = Notifications(state)
        self._notification_started = False

    @staticmethod
    def _session_token(handler):
        try:
            cookie = http.cookies.SimpleCookie(handler.headers.get("Cookie", ""))
            return cookie["compendium_sid"].value
        except (ValueError, KeyError, TypeError, http.cookies.CookieError):
            return None

    def _session(self, handler):
        return self.state.has_session(self._session_token(handler))

    def _csrf(self, handler):
        return handler.headers.get("Origin") == self.origin

    def _cookie(self, token=None):
        value = token or ""
        secure = "; Secure" if self.origin.startswith("https://") else ""
        return f"compendium_sid={value}; HttpOnly; SameSite=Strict; Path=/app/; Max-Age={43200 if token else 0}{secure}"

    def _client_ip(self, handler):
        """Throttle by the real client behind an explicitly trusted proxy, never a spoofed header."""
        def trusted(address):
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                return False
            return any(ip in network for network in self.trusted_proxies)
        client = handler.client_address[0]
        if not trusted(client):
            return client
        for hop in reversed(handler.headers.get("X-Forwarded-For", "").split(",")):
            hop = hop.strip()
            if not trusted(hop):
                # The first untrusted hop from the right was added by a trusted proxy.
                try:
                    return str(ipaddress.ip_address(hop))
                except ValueError:
                    return client
            client = hop
        return client

    def _login_blocked(self, ip, now):
        for address, failures in list(self.failed_logins.items()):
            current = [stamp for stamp in failures if now - stamp < LOGIN_FAILURE_WINDOW]
            if current:
                self.failed_logins[address] = current
            else:
                self.failed_logins.pop(address, None)
        failures = self.failed_logins.get(ip, [])
        if len(failures) >= LOGIN_FAILURE_LIMIT:
            return True
        if ip not in self.failed_logins:
            while len(self.failed_logins) >= MAX_FAILED_LOGIN_IPS:
                oldest = min(self.failed_logins, key=lambda address: self.failed_logins[address][-1])
                self.failed_logins.pop(oldest, None)
        return False

    def _json(self, handler, code, data, cookie=None):
        body = json_bytes(data)
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        if cookie:
            handler.send_header("Set-Cookie", cookie)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _body(handler, max_size=MAX_STATE_BODY):
        try:
            return json.loads(OwnerWeb._body_bytes(handler, max_size))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValidationError("invalid JSON") from exc

    @staticmethod
    def _body_bytes(handler, max_size=MAX_STATE_BODY):
        try:
            length = int(handler.headers.get("Content-Length", "-1"))
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if not 0 < length <= max_size:
            raise ValidationError("invalid request body size")
        return handler.rfile.read(length)

    def _static(self, handler, path):
        if path == "/app":
            handler.send_response(302)
            handler.send_header("Location", "/app/")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        name, mime = STATIC[path]
        body = (self.static / name).read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        handler.end_headers()
        handler.wfile.write(body)

    def dispatch(self, handler, method):
        path = urlparse(handler.path).path
        if path == "/":
            handler.send_response(302)
            handler.send_header("Location", "/app/")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        if method == "GET" and (path in STATIC or path == "/app"):
            self._static(handler, path)
            return True
        if not path.startswith("/app/api/"):
            return False
        if method not in {"GET", "POST", "PUT"}:
            self._json(handler, 405, {"error": "method not allowed"})
            return True
        if method != "GET" and not self._csrf(handler):
            self._json(handler, 403, {"error": "origin not allowed"})
            return True
        if path == "/app/api/login" and method == "POST":
            try:
                payload = self._body(handler, 1024)
                username = payload.get("username", "") if isinstance(payload, dict) else ""
                supplied = payload.get("password", "") if isinstance(payload, dict) else ""
                if not isinstance(supplied, str):
                    supplied = ""
                with self.auth_lock:
                    ip = self._client_ip(handler)
                    now = time.time()
                    if self._login_blocked(ip, now):
                        self._json(handler, 429, {"error": "invalid sign-in"})
                        return True
                    if not self.state.verify_owner(username, supplied):
                        failures = self.failed_logins.setdefault(ip, [])
                        failures.append(now)
                        self._json(handler, 429 if len(failures) >= LOGIN_FAILURE_LIMIT else 401,
                                   {"error": "invalid sign-in"})
                        return True
                    self.failed_logins.pop(ip, None)
                    token = self.state.create_session()
                self._json(handler, 200, {"authenticated": True}, self._cookie(token))
            except ValidationError as exc:
                self._json(handler, 400, {"error": str(exc)})
            except (OSError, sqlite3.DatabaseError):
                self._json(handler, 503, {"error": "sign-in storage unavailable"})
            return True
        if not self._session(handler):
            self._json(handler, 401, {"error": "sign in required"})
            return True
        try:
            self._api(handler, method, path)
        except ConflictError as exc:
            self._json(handler, 409, {"error": str(exc)})
        except PermissionError as exc:
            self._json(handler, 403, {"error": str(exc)})
        except (ValidationError, ValueError) as exc:
            self._json(handler, 400, {"error": str(exc)})
        except HermesError as exc:
            self._json(handler, 502, {"error": str(exc)})
        except (KeyError, TypeError, IndexError, OverflowError, AttributeError, RecursionError) as exc:
            self._json(handler, 400, {"error": "invalid archive or request data"})
        except (OSError, sqlite3.DatabaseError) as exc:
            print(f"Compendium request failure: {type(exc).__name__}", flush=True)
            self._json(handler, 503, {"error": "storage temporarily unavailable"})
        return True

    def _api(self, handler, method, path):
        query = parse_qs(urlparse(handler.path).query)
        if method == "GET" and path == "/app/api/session":
            return self._json(handler, 200, {"authenticated": True,
                                            "username": self.state.owner_username(),
                                            "hermes": self._public_hermes()})
        if method == "POST" and path == "/app/api/account":
            payload = self._body(handler, 2048)
            if (not isinstance(payload, dict) or
                    set(payload) - {"currentPassword", "username", "newPassword"}):
                raise ValidationError("invalid account details")
            with self.auth_lock:
                if not self._session(handler):
                    raise PermissionError("Sign in again")
                self.state.change_owner(payload.get("currentPassword"), payload.get("username"),
                                        payload.get("newPassword") or None)
            return self._json(handler, 200, {"changed": True}, self._cookie())
        if method == "POST" and path == "/app/api/logout":
            payload = self._body(handler, 8192)
            if not isinstance(payload, dict) or set(payload) - {"notificationEndpoint"}:
                raise ValidationError("invalid logout details")
            endpoint = payload.get("notificationEndpoint")
            if endpoint is not None and (not isinstance(endpoint, str) or len(endpoint) > 4096):
                raise ValidationError("invalid push endpoint")
            with self.auth_lock:
                self.state.revoke_session(self._session_token(handler), endpoint)
            return self._json(handler, 200, {"authenticated": False}, self._cookie())
        if method == "GET" and path == "/app/api/sync":
            try:
                cursor = int(query.get("cursor", ["0"])[0]); limit = int(query.get("limit", ["250"])[0])
            except ValueError as exc:
                raise ValidationError("invalid cursor or limit") from exc
            return self._json(handler, 200, self.state.changes(cursor, limit))
        if method == "PUT" and path == "/app/api/state":
            result = self.state.put(self._body(handler))
            return self._json(handler, 409 if result.get("conflict") else 200, result)
        if method == "GET" and path == "/app/api/published":
            cursor = query.get("cursor", [None])[0]
            deliveries, next_cursor, more = self.delivery_db.sync(cursor, 250)
            return self._json(handler, 200, {"deliveries": deliveries, "cursor": next_cursor, "hasMore": more})
        if method == "GET" and path == "/app/api/export":
            return self._json(handler, 200, self.state.export_archive())
        if method == "POST" and path == "/app/api/import":
            raw = self._body_bytes(handler, MAX_PWA_ARCHIVE_BODY)
            return self._json(handler, 200, self.state.import_archive(raw))
        if method == "GET" and path == "/app/api/hermes":
            return self._json(handler, 200, self._public_hermes())
        if method == "GET" and path == "/app/api/notifications":
            endpoint = query.get("device", [None])[0]
            return self._json(handler, 200, self.notifications.status(endpoint))
        if method == "POST" and path == "/app/api/notifications/subscribe":
            subscription = self._body(handler, 8192)
            with self.auth_lock:
                if not self._session(handler):
                    raise PermissionError("Sign in again")
                identifier = self.notifications.subscribe(subscription, self._session_token(handler))
            return self._json(handler, 200, {"id": identifier, "subscribed": True})
        if method == "POST" and path == "/app/api/notifications/unsubscribe":
            data = self._body(handler, 8192)
            if not isinstance(data, dict) or set(data) != {"endpoint"}: raise ValidationError("invalid push request")
            self.notifications.unsubscribe(data["endpoint"])
            return self._json(handler, 200, {"subscribed": False})
        if method == "POST" and path == "/app/api/notifications/test":
            data = self._body(handler, 8192)
            if not isinstance(data, dict) or set(data) != {"endpoint"}: raise ValidationError("invalid push request")
            return self._json(handler, 200, {"accepted": self.notifications.test(data["endpoint"])})
        if method == "POST" and path == "/app/api/hermes":
            data = self._body(handler, 4096)
            if not isinstance(data, dict):
                raise ValidationError("invalid connection")
            endpoint = checked_hermes_origin(data.get("url"), self.allowed_hermes)
            user, password = data.get("username"), data.get("password")
            if not isinstance(user, str) or not user.strip() or len(user) > 200 or not isinstance(password, str) or not password:
                raise ValidationError("Hermes username and password required")
            source = data.get("sourceId") or str(uuid.uuid4())
            checked_string(source, "sourceId")
            if not self.poll_lock.acquire(blocking=False):
                raise HermesError("A Hermes check is already running. Try again shortly.", "busy")
            try:
                # Validate a candidate before replacing the working connection.
                HermesAdapter(endpoint, user, password).jobs()
                config = self._sealed({"url": endpoint, "username": user, "password": password, "sourceId": source})
                existing = self.state.find(source, "source", "self")
                self.state.set_config("hermes", config)
                self.hermes_probe_positions.clear()
                self.state.put({"sourceId": source, "kind": "source", "key": "self",
                                "baseVersion": existing["version"] if existing else 0,
                                "deleted": False, "value": {"name": user + " · Hermes", "url": endpoint}})
                now = datetime.now(timezone.utc).isoformat()
                self._save_hermes_health(config, state="healthy", lastCheckAt=now, lastSuccessAt=now, error=None)
            finally:
                self.poll_lock.release()
            return self._json(handler, 200, self._public_hermes())
        if method == "POST" and path == "/app/api/hermes/check":
            self.check_hermes_jobs()
            return self._json(handler, 200, self._public_hermes())
        if method == "GET" and path == "/app/api/hermes/jobs":
            jobs = self.check_hermes_jobs()
            return self._json(handler, 200, {"jobs": [
                {"id": j["id"], "name": j.get("name", j["id"]), "profile": j.get("profile")}
                for j in jobs if isinstance(j, dict) and isinstance(j.get("id"), str)
            ]})
        if method == "POST" and path == "/app/api/hermes/pull":
            return self._json(handler, 200, self.pull_hermes())
        self._json(handler, 404, {"error": "not found"})

    def _public_hermes(self):
        config = self.state.config("hermes")
        if not config:
            return {"connected": False, "health": self._hermes_health(None)}
        return {"connected": True, "url": config["url"], "username": config["username"],
                "sourceId": config["sourceId"], "health": self._hermes_health(config)}

    @staticmethod
    def _hermes_fingerprint(config):
        return hashlib.sha256(json_bytes(config)).hexdigest()

    def _hermes_health(self, config):
        saved = self.state.config("hermes_health") or {}
        if not config or saved.get("fingerprint") != self._hermes_fingerprint(config):
            saved = {}
        status = saved.get("state", "unchecked" if config else "not_configured")
        if status == "healthy":
            last = max(filter(None, [saved.get("lastPullAt"), saved.get("lastCheckAt")]), default=None)
            if last:
                try:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))
                    if age.total_seconds() > getattr(self, "poll_seconds", 300) * 3:
                        status = "stale"
                except (TypeError, ValueError):
                    status = "stale"
        return {"state": status, "checking": self.poll_lock.locked(),
                **{key: saved.get(key) for key in ("lastCheckAt", "lastSuccessAt", "lastPullAt", "lastPullSuccessAt", "lastResult", "error")},
                "pollSeconds": getattr(self, "poll_seconds", 300)}

    def _save_hermes_health(self, config, **values):
        # Keep the fingerprint check and write together with other config mutations.
        with self.state.lock:
            if self.state.config("hermes") != config:
                return
            prior = self.state.config("hermes_health") or {}
            fingerprint = self._hermes_fingerprint(config)
            if prior.get("fingerprint") != fingerprint:
                prior = {"fingerprint": fingerprint}
            self.state.set_config("hermes_health", {**prior, **values})

    def check_hermes_jobs(self):
        if not self.poll_lock.acquire(blocking=False):
            raise HermesError("A Hermes check is already running. Try again shortly.", "busy")
        config = None
        try:
            config = self.state.config("hermes")
            if not config:
                return []
            jobs = self.hermes(config).jobs()
            now = datetime.now(timezone.utc).isoformat()
            self._save_hermes_health(config, state="healthy", lastCheckAt=now, lastSuccessAt=now, error=None)
            return jobs
        except HermesError as exc:
            if config:
                self._save_hermes_health(config, state="error", lastCheckAt=datetime.now(timezone.utc).isoformat(),
                                         error={"code": exc.category, "message": str(exc)})
            raise
        finally:
            self.poll_lock.release()

    def _sealed(self, config):
        if not self.cipher or "password" not in config:
            return config
        sealed = {key: value for key, value in config.items() if key != "password"}
        sealed["passwordEncrypted"] = self.cipher.encrypt(config["password"].encode()).decode()
        return sealed

    def _seal_stored_hermes_password(self):
        """Encrypt a plaintext stored password once a key is configured, keeping its health record."""
        with self.state.lock:
            config = self.state.config("hermes")
            sealed = self._sealed(config) if config else config
            if sealed == config:
                return
            health = self.state.config("hermes_health")
            self.state.set_config("hermes", sealed)
            if health and health.get("fingerprint") == self._hermes_fingerprint(config):
                self.state.set_config("hermes_health", {**health, "fingerprint": self._hermes_fingerprint(sealed)})

    def hermes(self, config):
        password = config.get("password")
        if password is None:
            try:
                password = self.cipher.decrypt(config["passwordEncrypted"].encode()).decode()
            except (AttributeError, KeyError, InvalidToken) as exc:
                raise HermesError("The stored Hermes password cannot be decrypted. Check APP_SECRET_KEY "
                                  "or reconnect Hermes in Settings.", "auth") from exc
        return HermesAdapter(config["url"], config["username"], password)

    def pull_hermes(self):
        if not self.poll_lock.acquire(blocking=False):
            return {"imported": 0, "busy": True}
        config = None
        try:
            config = self.state.config("hermes")
            if not config:
                return {"imported": 0, "connected": False}
            enabled = {entry["key"] for entry in self.state.all_kind("job", config["sourceId"])
                       if entry["value"].get("enabled")}
            if not enabled:
                result = {"imported": 0, "enabledJobs": 0}
                self._save_hermes_health(config, state="idle", lastPullAt=datetime.now(timezone.utc).isoformat(), lastResult=result)
                return result
            client = self.hermes(config)
            connection = (config["url"], config["sourceId"])
            positions = {job_id: self.hermes_probe_positions[(connection[0], connection[1], job_id)]
                         for job_id in enabled if (connection[0], connection[1], job_id) in self.hermes_probe_positions}
            result = client.pull(self.state, config["sourceId"], enabled, positions)
            for job_id, run_id in positions.items():
                self.hermes_probe_positions[(connection[0], connection[1], job_id)] = run_id
            now = datetime.now(timezone.utc).isoformat()
            self._save_hermes_health(config, state="healthy", lastCheckAt=now, lastSuccessAt=now, lastPullAt=now, lastPullSuccessAt=now, lastResult=result, error=None)
            return result
        except HermesError as exc:
            if config:
                now = datetime.now(timezone.utc).isoformat()
                self._save_hermes_health(config, state="error", lastCheckAt=now, lastPullAt=now,
                                         error={"code": exc.category, "message": str(exc)})
            raise
        finally:
            self.poll_lock.release()

    def start_poller(self, interval=300):
        self.poll_seconds = interval
        def loop():
            while True:
                time.sleep(interval)
                try:
                    self.pull_hermes()
                except Exception as exc:
                    print(f"Compendium Hermes poll failed: {type(exc).__name__}", flush=True)
                    if not isinstance(exc, HermesError):
                        try:
                            config = self.state.config("hermes")
                            if config:
                                self._save_hermes_health(config, state="error", error={"code": "unavailable",
                                    "message": "The import check failed. Retry Sync; if this continues, check the server logs."})
                        except Exception:
                            pass
        threading.Thread(target=loop, daemon=True, name="app01-hermes-poll").start()
        if not self._notification_started:
            self._notification_started = True
            def notification_loop():
                while True:
                    time.sleep(30)
                    try:
                        self.notifications.poll()
                    except Exception as exc:
                        print(f"Compendium notification poll failed: {type(exc).__name__}", flush=True)
            threading.Thread(target=notification_loop, daemon=True, name="app01-webpush-poll").start()
