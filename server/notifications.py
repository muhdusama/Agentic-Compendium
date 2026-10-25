"""Private, generic Web Push subscriptions for the single owner service."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from server.app import ValidationError

MAX_SUBSCRIPTIONS = 32
_PUSH_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}


def _b64(value, name, length=None):
    if not isinstance(value, str) or len(value) > 512:
        raise ValidationError("invalid " + name)
    if not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise ValidationError("invalid " + name)
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("invalid " + name) from exc
    if length is not None and len(decoded) != length:
        raise ValidationError("invalid " + name)
    return decoded


def subscription_id(endpoint):
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def checked_subscription(value):
    if not isinstance(value, dict) or set(value) - {"endpoint", "keys", "expirationTime"}:
        raise ValidationError("invalid push subscription")
    endpoint, keys = value.get("endpoint"), value.get("keys")
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 4096:
        raise ValidationError("invalid push endpoint")
    if any(ord(char) < 32 for char in endpoint):
        raise ValidationError("invalid push endpoint")
    parsed = urlparse(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("invalid push endpoint") from exc
    if (parsed.scheme != "https" or not parsed.hostname or port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment or parsed.hostname.lower() not in _PUSH_HOSTS):
        raise ValidationError("unsupported push endpoint")
    if not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
        raise ValidationError("invalid push keys")
    public = _b64(keys["p256dh"], "p256dh", 65)
    if not public or public[0] != 4:
        raise ValidationError("invalid p256dh")
    try:
        from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey, SECP256R1
        EllipticCurvePublicKey.from_encoded_point(SECP256R1(), public)
    except ValueError as exc:
        raise ValidationError("invalid p256dh") from exc
    _b64(keys["auth"], "auth", 16)
    return {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}


class Notifications:
    def __init__(self, state, subject="mailto:owner@localhost"):
        self.state, self.subject = state, subject
        # No lock is held while a push provider is contacted, so sign-out never waits
        # on the network. Result writes match the snapshot generation instead.
        self.lock, self.vapid_lock = threading.RLock(), threading.Lock()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def available(self):
        try:
            import pywebpush  # noqa: F401
            import py_vapid  # noqa: F401
            return True
        except ImportError:
            return False

    def public_key(self):
        if not self.available():
            return None
        from py_vapid import Vapid
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        with self.vapid_lock:
            saved = self.state.config("webpush_vapid_private")
            if saved:
                private = base64.b64decode(saved["pem"])
                vapid = Vapid.from_pem(private)
            else:
                vapid = Vapid(); vapid.generate_keys()
                self.state.set_config("webpush_vapid_private", {"pem": base64.b64encode(vapid.private_pem()).decode()})
        raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def status(self, identifier=None):
        result = {"available": self.available(), "publicKey": self.public_key() if self.available() else None,
                  "subscribed": False, "lastAcceptedAt": None, "lastError": None, "nextAttemptAt": None}
        if identifier:
            if not isinstance(identifier, str) or len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
                raise ValidationError("invalid device")
            with self.state.connect() as db:
                row = db.execute("SELECT last_accepted_at,last_error,next_attempt_at FROM app_push_subscriptions WHERE id=?", (identifier,)).fetchone()
            if row:
                result.update({"subscribed": True, "lastAcceptedAt": row["last_accepted_at"],
                               "lastError": row["last_error"], "nextAttemptAt": row["next_attempt_at"] or None})
        return result

    def subscribe(self, value, session_token):
        sub = checked_subscription(value); identifier = subscription_id(sub["endpoint"])
        with self.lock, self.state.lock, self.state.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT cursor,published_cursor FROM app_push_subscriptions WHERE id=?", (identifier,)).fetchone()
            if not row and db.execute("SELECT COUNT(*) FROM app_push_subscriptions").fetchone()[0] >= MAX_SUBSCRIPTIONS:
                raise ValidationError("too many push subscriptions")
            cursor = row["cursor"] if row else db.execute("SELECT COALESCE(MAX(seq),0) FROM app_changes").fetchone()[0]
            # Deliveries are immutable and never deleted. SQLite rowid orders arrivals even
            # when wall-clock timestamps tie or the clock moves backwards.
            published = row["published_cursor"] if row else db.execute("SELECT COALESCE(MAX(rowid),0) FROM deliveries").fetchone()[0]
            db.execute("""INSERT INTO app_push_subscriptions(id,endpoint,subscription_json,generation,session_hash,cursor,published_cursor,created_at)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET endpoint=excluded.endpoint,
                        subscription_json=excluded.subscription_json,generation=excluded.generation,session_hash=excluded.session_hash""",
                       (identifier, sub["endpoint"], json.dumps(sub, separators=(",", ":")), uuid.uuid4().hex,
                        hashlib.sha256((session_token or "").encode()).hexdigest(), cursor, published, self._now()))
        return identifier

    def unsubscribe(self, endpoint):
        if not isinstance(endpoint, str) or len(endpoint) > 4096:
            raise ValidationError("invalid push endpoint")
        with self.lock, self.state.lock, self.state.connect() as db:
            db.execute("DELETE FROM app_push_subscriptions WHERE id=?", (subscription_id(endpoint),))

    def revoke_session(self, token):
        with self.lock, self.state.lock, self.state.connect() as db:
            db.execute("DELETE FROM app_push_subscriptions WHERE session_hash=?", (hashlib.sha256((token or "").encode()).hexdigest(),))

    def revoke_all(self):
        with self.lock, self.state.lock, self.state.connect() as db:
            db.execute("DELETE FROM app_push_subscriptions")

    def _send(self, row, payload):
        if not self.available():
            return False, "disabled", None
        from pywebpush import webpush, WebPushException
        from py_vapid import Vapid
        saved = self.state.config("webpush_vapid_private")
        if not saved: self.public_key(); saved = self.state.config("webpush_vapid_private")
        requests = __import__("requests")
        class NoRedirectSession(requests.Session):
            def request(self, method, url, **kwargs):
                kwargs["allow_redirects"] = False
                return super().request(method, url, **kwargs)
        session = NoRedirectSession(); session.trust_env = False
        try:
            response = webpush(json.loads(row["subscription_json"]), data=json.dumps(payload, separators=(",", ":")),
                vapid_private_key=Vapid.from_pem(base64.b64decode(saved["pem"])), vapid_claims={"sub": self.subject},
                ttl=60, timeout=10, requests_session=session)
            ok = 200 <= response.status_code < 300
            return ok, None if ok else "unavailable", response.status_code
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            return False, "rejected" if status in (404, 410) else "unavailable", status
        except Exception:
            return False, "unavailable", None
        finally:
            session.close()

    def _record(self, row, ok, error, status, advance=None, retry_delay=None):
        """Store a provider result unless the device was revoked or re-registered meanwhile."""
        match = (row["id"], row["generation"])
        with self.lock, self.state.lock, self.state.connect() as db:
            if ok:
                db.execute("UPDATE app_push_subscriptions SET last_accepted_at=?,last_error=NULL,next_attempt_at=0,retry_delay=0 WHERE id=? AND generation=?",
                           (self._now(), *match))
                if advance:
                    db.execute("UPDATE app_push_subscriptions SET cursor=?,published_cursor=? WHERE id=? AND generation=?",
                               (*advance, *match))
            elif status in (404, 410):
                db.execute("DELETE FROM app_push_subscriptions WHERE id=? AND generation=?", match)
            else:
                delay = retry_delay or min(3600, max(60, row["retry_delay"] * 2 or 60))
                db.execute("UPDATE app_push_subscriptions SET last_error=?,next_attempt_at=?,retry_delay=? WHERE id=? AND generation=?",
                           (error, int(time.time()) + delay, delay, *match))

    def _current(self, row):
        with self.state.connect() as db:
            current = db.execute("SELECT generation FROM app_push_subscriptions WHERE id=?", (row["id"],)).fetchone()
        return current is not None and current["generation"] == row["generation"]

    def _pending(self, row):
        with self.state.connect() as db:
            latest = db.execute("SELECT MAX(seq) FROM app_changes WHERE seq>? AND kind='delivery' AND deleted=0 AND version=1", (row["cursor"],)).fetchone()[0]
            published = db.execute("SELECT MAX(rowid) FROM deliveries WHERE rowid>?", (row["published_cursor"],)).fetchone()[0]
        return latest, published

    def test(self, endpoint):
        if not isinstance(endpoint, str) or len(endpoint) > 4096:
            raise ValidationError("invalid push endpoint")
        identifier = subscription_id(endpoint)
        with self.lock, self.state.lock, self.state.connect() as db:
            row = db.execute("SELECT * FROM app_push_subscriptions WHERE id=?", (identifier,)).fetchone()
            if not row: raise ValidationError("push subscription not found")
            if row["test_at"] + 30 > time.time(): raise ValidationError("push test is cooling down")
            db.execute("UPDATE app_push_subscriptions SET test_at=? WHERE id=?", (int(time.time()), identifier))
        ok, error, status = self._send(row, {"type": "test"})
        self._record(row, ok, error, status, retry_delay=60)
        return ok

    def poll(self):
        now = int(time.time())
        with self.state.connect() as db:
            rows = db.execute("SELECT * FROM app_push_subscriptions WHERE next_attempt_at<=?", (now,)).fetchall()
        for row in rows:
            latest, published = self._pending(row)
            if not latest and not published or not self._current(row):
                continue
            ok, error, status = self._send(row, {"type": "deliveries"})
            self._record(row, ok, error, status,
                         (latest or row["cursor"], published or row["published_cursor"]))
