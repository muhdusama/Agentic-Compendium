import base64
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from server.app import DeliveryDatabase, DeliveryService, make_handler, validate_delivery
from server.notifications import Notifications, checked_subscription, subscription_id
from server.pwa import OwnerWeb, PersonalState
from http.server import ThreadingHTTPServer


def subscription(endpoint="https://fcm.googleapis.com/fcm/send/example"):
    from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    public = generate_private_key(SECP256R1()).public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return {"endpoint": endpoint, "keys": {
        "p256dh": base64.urlsafe_b64encode(public).decode().rstrip("="),
        "auth": base64.urlsafe_b64encode(b"a" * 16).decode().rstrip("=")}}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "state.sqlite"
        self.delivery = DeliveryDatabase(path)
        self.state = PersonalState(path)
        self.push = Notifications(self.state)

    def tearDown(self): self.temp.cleanup()

    def test_rejects_arbitrary_endpoints_and_bad_keys(self):
        for endpoint in ("http://fcm.googleapis.com/x", "https://127.0.0.1/x", "https://fcm.googleapis.com:444/x", "https://evil.example/x"):
            with self.assertRaises(Exception): checked_subscription(subscription(endpoint))
        bad = subscription(); bad["keys"]["p256dh"] = "AA"
        with self.assertRaises(Exception): checked_subscription(bad)

    def test_baseline_prevents_replay_and_only_generic_payload_is_sent(self):
        self.state.put({"sourceId": "a", "kind": "delivery", "key": "old", "baseVersion": 0, "deleted": False, "value": {"private": "old"}})
        identifier = self.push.subscribe(subscription(), "session")
        self.assertEqual(identifier, subscription_id(subscription()["endpoint"]))
        with mock.patch.object(self.push, "_send", return_value=(True, None, 201)) as send:
            self.push.poll(); send.assert_not_called()
            self.state.put({"sourceId": "a", "kind": "delivery", "key": "new", "baseVersion": 0, "deleted": False, "value": {"private": "new"}})
            self.push.poll()
        self.assertEqual(send.call_args.args[1], {"type": "deliveries"})
        self.assertNotIn("private", str(send.call_args))

    def test_session_revoke_prevents_delivery_and_export_has_no_secrets(self):
        self.push.subscribe(subscription(), "session")
        self.push.revoke_session("session")
        self.state.put({"sourceId": "a", "kind": "delivery", "key": "new", "baseVersion": 0, "deleted": False, "value": {}})
        with mock.patch.object(self.push, "_send") as send:
            self.push.poll(); send.assert_not_called()
        self.push.public_key()
        self.assertNotIn("webpush", str(self.state.export_archive()))

    def test_published_delivery_baseline_and_retry_cleanup(self):
        self.delivery.ingest(validate_delivery({"id": "old", "title": "x", "publisher": "x", "section": "x", "summary": "", "systemImage": "x", "body": "", "deliveredAt": "2026-01-01T00:00:00Z", "needsAttention": False}))
        self.push.subscribe(subscription(), "session")
        with mock.patch.object(self.push, "_send", return_value=(True, None, 201)) as send:
            self.push.poll(); send.assert_not_called()
            self.delivery.ingest(validate_delivery({"id": "new", "title": "x", "publisher": "x", "section": "x", "summary": "", "systemImage": "x", "body": "", "deliveredAt": "2026-01-01T00:00:00Z", "needsAttention": False}))
            self.push.poll(); self.assertEqual(send.call_args.args[1], {"type": "deliveries"})
        self.push.subscribe(subscription(), "new-session")
        with mock.patch.object(self.push, "_send", return_value=(False, "rejected", 410)):
            self.state.put({"sourceId": "a", "kind": "delivery", "key": "later", "baseVersion": 0, "deleted": False, "value": {}})
            self.push.poll()
        self.assertFalse(self.push.status(subscription_id(subscription()["endpoint"]))["subscribed"])

    def test_retry_is_bounded_and_cursor_only_advances_after_acceptance(self):
        item = subscription(); identifier = self.push.subscribe(item, "session")
        self.state.put({"sourceId": "a", "kind": "delivery", "key": "new", "baseVersion": 0, "deleted": False, "value": {}})
        with mock.patch.object(self.push, "_send", return_value=(False, "unavailable", None)):
            self.push.poll()
        with self.state.connect() as db:
            first = db.execute("SELECT cursor,next_attempt_at,retry_delay FROM app_push_subscriptions WHERE id=?", (identifier,)).fetchone()
            self.assertEqual(first["cursor"], 0); self.assertEqual(first["retry_delay"], 60)
            db.execute("UPDATE app_push_subscriptions SET next_attempt_at=0 WHERE id=?", (identifier,))
        with mock.patch.object(self.push, "_send", return_value=(False, "unavailable", None)):
            self.push.poll()
        with self.state.connect() as db:
            self.assertEqual(db.execute("SELECT retry_delay FROM app_push_subscriptions WHERE id=?", (identifier,)).fetchone()[0], 120)
            db.execute("UPDATE app_push_subscriptions SET next_attempt_at=0 WHERE id=?", (identifier,))
        with mock.patch.object(self.push, "_send", return_value=(True, None, 204)):
            self.push.poll()
        with self.state.connect() as db:
            self.assertGreater(db.execute("SELECT cursor FROM app_push_subscriptions WHERE id=?", (identifier,)).fetchone()[0], 0)

    def test_provider_send_encrypts_payload_disables_redirects_and_treats_3xx_as_failure(self):
        item = subscription(); identifier = self.push.subscribe(item, "session")
        self.push.public_key()
        with self.state.connect() as db:
            row = db.execute("SELECT * FROM app_push_subscriptions WHERE id=?", (identifier,)).fetchone()
        class Response:
            status_code = 302
            headers = {}
            content = b""
            reason = "redirect"
            url = "https://fcm.googleapis.com/fcm/send/example"
            text = ""
        with mock.patch("requests.Session.send", return_value=Response()) as send:
            accepted, error, status = self.push._send(row, {"type": "test", "private": "do-not-send"})
        self.assertFalse(accepted); self.assertEqual((error, status), ("unavailable", 302))
        request = send.call_args.args[0]
        self.assertNotIn(b"do-not-send", request.body)
        self.assertFalse(send.call_args.kwargs.get("allow_redirects", True))

    def test_snapshot_generation_blocks_stale_send_after_unsubscribe_and_resubscribe(self):
        item = subscription(); self.push.subscribe(item, "first")
        self.state.put({"sourceId": "a", "kind": "delivery", "key": "new", "baseVersion": 0, "deleted": False, "value": {}})
        pending = self.push._pending
        def replace_device_after_snapshot(row):
            self.push.unsubscribe(item["endpoint"]); self.push.subscribe(item, "second")
            return pending(row)
        with mock.patch.object(self.push, "_pending", side_effect=replace_device_after_snapshot), \
                mock.patch.object(self.push, "_send", return_value=(True, None, 201)) as send:
            self.push.poll()
        send.assert_not_called()

    def test_published_rowid_detects_equal_and_backward_timestamps(self):
        item = subscription()
        self.delivery.ingest(validate_delivery({"id": "old", "title": "x", "publisher": "x", "section": "x", "summary": "", "systemImage": "x", "body": "", "deliveredAt": "2026-01-01T00:00:00Z", "needsAttention": False}))
        self.push.subscribe(item, "session")
        with mock.patch("server.app.time.time_ns", side_effect=[100, 50]):
            for identifier in ("equal", "backward"):
                self.delivery.ingest(validate_delivery({"id": identifier, "title": "x", "publisher": "x", "section": "x", "summary": "", "systemImage": "x", "body": "", "deliveredAt": "2026-01-01T00:00:00Z", "needsAttention": False}))
        with mock.patch.object(self.push, "_send", return_value=(True, None, 201)) as send:
            self.push.poll()
        self.assertEqual(send.call_args.args[1], {"type": "deliveries"})

    def _stalled_poll(self, result):
        """Start a poll whose provider call blocks until released."""
        self.state.put({"sourceId": "a", "kind": "delivery", "key": "new", "baseVersion": 0, "deleted": False, "value": {}})
        active, release = threading.Event(), threading.Event()
        def stalled_send(_row, _payload):
            active.set(); release.wait(5); return result
        patcher = mock.patch.object(self.push, "_send", side_effect=stalled_send); patcher.start()
        self.addCleanup(patcher.stop)
        poller = threading.Thread(target=self.push.poll); poller.start()
        self.assertTrue(active.wait(1))
        return poller, release

    def test_reregistration_during_inflight_410_does_not_wait_and_survives(self):
        original, replacement = subscription(), subscription()
        identifier = self.push.subscribe(original, "old-session")
        poller, release = self._stalled_poll((False, "rejected", 410))
        updater = threading.Thread(target=self.push.subscribe, args=(replacement, "new-session")); updater.start()
        updater.join(1); self.assertFalse(updater.is_alive(), "re-registration must not wait for the provider")
        release.set(); poller.join(1); self.assertFalse(poller.is_alive())
        with self.state.connect() as db:
            row = db.execute("SELECT subscription_json,session_hash FROM app_push_subscriptions WHERE id=?", (identifier,)).fetchone()
        self.assertIsNotNone(row); self.assertIn(replacement["keys"]["p256dh"], row["subscription_json"])
        self.assertNotEqual(row["session_hash"], __import__("hashlib").sha256(b"old-session").hexdigest())

    def test_revocation_does_not_wait_for_provider_and_is_not_undone(self):
        identifier = self.push.subscribe(subscription(), "session")
        poller, release = self._stalled_poll((True, None, 201))
        revoker = threading.Thread(target=self.push.revoke_session, args=("session",)); revoker.start()
        revoker.join(1); self.assertFalse(revoker.is_alive(), "sign-out must not wait for the provider")
        release.set(); poller.join(1); self.assertFalse(poller.is_alive())
        self.assertFalse(self.push.status(identifier)["subscribed"])

    def test_inflight_failure_does_not_touch_a_replaced_registration(self):
        identifier = self.push.subscribe(subscription(), "old")
        poller, release = self._stalled_poll((False, "unavailable", 503))
        self.push.subscribe(subscription(), "new")
        release.set(); poller.join(1)
        with self.state.connect() as db:
            row = db.execute("SELECT last_error,next_attempt_at,cursor FROM app_push_subscriptions WHERE id=?", (identifier,)).fetchone()
        self.assertEqual((row["last_error"], row["next_attempt_at"], row["cursor"]), (None, 0, 0))


class NotificationHTTPTests(unittest.TestCase):
    origin, password = "https://compendium.example.test", "long-owner-password-not-reused-for-publish"
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); path = Path(self.temp.name) / "state.sqlite"
        delivery = DeliveryDatabase(path); state = PersonalState(path)
        service = DeliveryService(delivery, "read", "publish"); self.web = OwnerWeb(state, self.password, self.origin); self.web.delivery_db = delivery; service.web = self.web
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service)); self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start(); self.cookie = ""
    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()
    def request(self, method, path, payload=None, origin=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1]); headers = {"Cookie": self.cookie}
        if method != "GET" and origin: headers["Origin"] = self.origin
        if payload is not None: headers["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(payload).encode() if payload is not None else None, headers); response = conn.getresponse(); raw = response.read(); result = (response.status, json.loads(raw), dict(response.getheaders())); conn.close(); return result
    def login(self):
        status, _, headers = self.request("POST", "/app/api/login", {"username": "owner", "password": self.password}); self.assertEqual(status, 200); self.cookie = headers["Set-Cookie"].split(";", 1)[0]
    def test_notification_routes_require_auth_and_origin_and_logout_revokes(self):
        sub = subscription(); self.assertEqual(self.request("GET", "/app/api/notifications")[0], 401)
        self.login(); self.assertEqual(self.request("POST", "/app/api/notifications/subscribe", sub, origin=False)[0], 403)
        status, reply, _ = self.request("POST", "/app/api/notifications/subscribe", sub); self.assertEqual(status, 200); self.assertTrue(reply["subscribed"])
        self.assertTrue(self.request("GET", "/app/api/notifications?device=" + reply["id"])[1]["subscribed"])
        with mock.patch.object(self.web.notifications, "test", return_value=True): self.assertEqual(self.request("POST", "/app/api/notifications/test", {"endpoint": sub["endpoint"]})[1], {"accepted": True})
        self.assertEqual(self.request("POST", "/app/api/logout", {"notificationEndpoint": sub["endpoint"]})[0], 200)
        self.assertFalse(self.web.notifications.status(reply["id"])["subscribed"])

    def test_account_and_logout_revocation_are_atomic_with_sessions_and_subscriptions(self):
        self.login(); sub = subscription()
        _, reply, _ = self.request("POST", "/app/api/notifications/subscribe", sub)
        with self.web.state.connect() as db:
            db.execute("CREATE TRIGGER reject_push_delete BEFORE DELETE ON app_push_subscriptions BEGIN SELECT RAISE(ABORT, 'blocked'); END")
        status, _, _ = self.request("POST", "/app/api/account", {"username": "newowner", "currentPassword": self.password, "newPassword": "updated-owner-password-123"})
        self.assertEqual(status, 503)
        self.assertTrue(self.web.state.verify_owner("owner", self.password)); self.assertTrue(self.web.state.has_session(self.cookie.split("=", 1)[1]))
        self.assertTrue(self.web.notifications.status(reply["id"])["subscribed"])
        self.assertEqual(self.request("POST", "/app/api/logout", {"notificationEndpoint": sub["endpoint"]})[0], 503)
        self.assertTrue(self.web.state.has_session(self.cookie.split("=", 1)[1]))
        with self.web.state.connect() as db: db.execute("DROP TRIGGER reject_push_delete")
        status, _, _ = self.request("POST", "/app/api/account", {"username": "newowner", "currentPassword": self.password, "newPassword": "updated-owner-password-123"})
        self.assertEqual(status, 200); self.assertFalse(self.web.notifications.status(reply["id"])["subscribed"])

    def test_logout_returns_while_a_push_send_is_stalled(self):
        self.login(); sub = subscription()
        _, reply, _ = self.request("POST", "/app/api/notifications/subscribe", sub)
        self.web.state.put({"sourceId": "a", "kind": "delivery", "key": "new", "baseVersion": 0, "deleted": False, "value": {}})
        active, release = threading.Event(), threading.Event()
        def stalled_send(_row, _payload):
            active.set(); release.wait(5); return True, None, 201
        with mock.patch.object(self.web.notifications, "_send", side_effect=stalled_send):
            poller = threading.Thread(target=self.web.notifications.poll); poller.start()
            self.assertTrue(active.wait(1))
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
                conn.request("POST", "/app/api/logout", json.dumps({"notificationEndpoint": sub["endpoint"]}).encode(),
                             {"Cookie": self.cookie, "Origin": self.origin, "Content-Type": "application/json"})
                self.assertEqual(conn.getresponse().status, 200); conn.close()
            finally:
                release.set(); poller.join(1)
        self.assertFalse(self.web.notifications.status(reply["id"])["subscribed"])
