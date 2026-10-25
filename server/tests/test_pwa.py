from __future__ import annotations

import base64
import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from server.app import DeliveryDatabase, DeliveryService, make_handler, validate_delivery
from server.hermes import HermesAdapter, HermesError, checked_hermes_origin
from server.pwa import OwnerWeb, PersonalState


ORIGIN = "https://compendium.example.test"
OWNER = "long-owner-password-not-reused-for-publish"


class PWAServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "deliveries.sqlite3"
        self.delivery = DeliveryDatabase(self.path)
        self.state = PersonalState(self.path)
        self.service = DeliveryService(self.delivery, "read-only", "publisher-only")
        self.web = OwnerWeb(self.state, OWNER, ORIGIN)
        self.web.delivery_db = self.delivery
        self.service.web = self.web
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.cookie = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def request(self, method, path, payload=None, origin=True, cookie=True, bearer=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        if method != "GET" and origin:
            headers["Origin"] = ORIGIN
        if cookie and self.cookie:
            headers["Cookie"] = self.cookie
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        body = json.dumps(payload).encode() if payload is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        code, header = response.status, dict(response.getheaders())
        conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return code, data, header

    def sign_in(self, username="owner", password=OWNER):
        code, data, headers = self.request("POST", "/app/api/login",
                                           {"username": username, "password": password})
        self.assertEqual(code, 200, data)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_auth_origin_protects_private_apis_and_legacy_tokens_remain_separate(self):
        self.assertEqual(self.request("GET", "/app/api/sync")[0], 401)
        self.assertEqual(self.request("POST", "/app/api/login", {"username": "owner", "password": OWNER}, origin=False)[0], 403)
        self.assertEqual(self.request("POST", "/app/api/login", {"username": "owner", "password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/app/api/login", {"username": "invalid", "password": OWNER})[0], 401)
        self.assertEqual(self.request("POST", "/app/api/login", {"username": "bad☠", "password": OWNER})[0], 401)
        self.sign_in()
        self.assertEqual(self.request("GET", "/app/api/session")[1]["authenticated"], True)
        self.assertEqual(self.request("GET", "/app/api/session")[1]["username"], "owner")
        self.assertEqual(self.request("PUT", "/app/api/state", {"x": 1}, origin=False)[0], 403)
        self.assertEqual(self.request("GET", "/v1/status")[0], 401)
        self.assertEqual(self.request("GET", "/v1/status", bearer="read-only")[0], 200)
        self.assertEqual(self.request("GET", "/v1/deliveries", bearer="publisher-only")[0], 401)
        self.assertEqual(self.request("GET", "/app/api/sync", bearer="publisher-only", cookie=False)[0], 401)
        previous_cookie = self.cookie
        self.sign_in()
        self.assertNotEqual(self.cookie, previous_cookie)
        self.assertEqual(self.request("POST", "/app/api/logout", {})[0], 200)
        self.assertEqual(self.request("GET", "/app/api/session")[0], 401)
        self.cookie = previous_cookie
        self.assertEqual(self.request("GET", "/app/api/session")[0], 200)
        self.cookie = ""
        self.assertEqual(self.request("GET", "/app/api/session")[0], 401)

    def test_owner_username_password_update_revokes_all_sessions_and_survives_restart(self):
        self.sign_in()
        previous_cookie = self.cookie
        self.assertEqual(self.request("POST", "/app/api/account", {
            "username": "new.owner", "currentPassword": "wrong", "newPassword": "a-very-long-updated-password"
        })[0], 403)
        self.assertEqual(self.request("GET", "/app/api/session")[0], 200)
        self.assertEqual(self.request("POST", "/app/api/account", {
            "username": "new.owner", "currentPassword": OWNER, "newPassword": "short"
        })[0], 400)
        code, response, headers = self.request("POST", "/app/api/account", {
            "username": "new.owner", "currentPassword": OWNER,
            "newPassword": "a-very-long-updated-password"
        })
        self.assertEqual(code, 200, response)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/app/api/session")[0], 401)
        self.cookie = ""
        self.assertEqual(self.request("POST", "/app/api/login", {"username": "owner", "password": OWNER})[0], 401)
        self.assertEqual(self.request("POST", "/app/api/login", {
            "username": "new.owner", "password": "a-very-long-updated-password"
        })[0], 200)
        restarted = OwnerWeb(self.state, OWNER, ORIGIN)
        self.assertEqual(restarted.state.owner_username(), "new.owner")
        self.assertFalse(restarted.state.verify_owner("owner", OWNER))
        self.assertTrue(restarted.state.verify_owner("new.owner", "a-very-long-updated-password"))
        with self.state.connect() as db:
            saved = db.execute("SELECT value_json FROM app_config WHERE name='owner_credentials'").fetchone()[0]
        self.assertNotIn("a-very-long-updated-password", saved)
        self.assertNotIn(OWNER, saved)

    def test_username_only_change_retains_password_and_revokes_session(self):
        self.sign_in()
        status, _, _ = self.request("POST", "/app/api/account", {
            "username": "second_owner", "currentPassword": OWNER, "newPassword": ""
        })
        self.assertEqual(status, 200)
        self.assertTrue(self.state.verify_owner("second_owner", OWNER))
        self.assertFalse(self.state.verify_owner("owner", OWNER))
        self.assertEqual(self.request("GET", "/app/api/session")[0], 401)

    def test_sessions_are_not_shared_and_expired_sessions_are_rejected(self):
        token = self.state.create_session()
        self.assertTrue(self.state.has_session(token))
        self.assertFalse(self.state.has_session(token + "not-a-real-token"))
        self.assertFalse(self.state.has_session(""))
        self.state.revoke_session(token)
        self.assertFalse(self.state.has_session(token))
        with self.state.connect() as db:
            db.execute("UPDATE app_sessions SET expires_at=0")
        token = self.state.create_session()
        with self.state.connect() as db:
            db.execute("UPDATE app_sessions SET expires_at=0")
        self.assertFalse(self.state.has_session(token))

    def test_login_failures_are_capped_and_expire_without_lockout_extension(self):
        for _ in range(7):
            self.assertEqual(self.request("POST", "/app/api/login", {"username": "owner", "password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/app/api/login", {"username": "owner", "password": "wrong"})[0], 429)
        failures = list(self.web.failed_logins["127.0.0.1"])
        self.assertEqual(self.request("POST", "/app/api/login", {"username": "owner", "password": "wrong"})[0], 429)
        self.assertEqual(self.web.failed_logins["127.0.0.1"], failures)
        self.web.failed_logins["127.0.0.1"] = [0] * 8
        self.sign_in()
        self.web.failed_logins = {f"192.0.2.{index}": [time.time()] for index in range(1100)}
        self.assertFalse(self.web._login_blocked("198.51.100.1", time.time()))
        self.assertLessEqual(len(self.web.failed_logins), 1024)

    def test_no_fixtures_and_versioned_offline_sync_conflicts_and_tombstones(self):
        self.sign_in()
        code, initial, _ = self.request("GET", "/app/api/sync")
        self.assertEqual((code, initial["changes"]), (200, []))
        channel = {"sourceId": "library", "kind": "channel", "key": "fixed-channel",
                   "baseVersion": 0, "deleted": False, "value": {"name": "Research", "order": 0}}
        self.assertEqual(self.request("PUT", "/app/api/state", channel)[1]["record"]["version"], 1)
        self.assertEqual(self.request("PUT", "/app/api/state", channel)[0], 409)
        revised = dict(channel, baseVersion=1, value={"name": "Daily", "order": 0})
        self.assertEqual(self.request("PUT", "/app/api/state", revised)[1]["record"]["version"], 2)
        deleted = dict(revised, baseVersion=2, deleted=True)
        self.assertEqual(self.request("PUT", "/app/api/state", deleted)[0], 200)
        page = self.request("GET", "/app/api/sync?cursor=0&limit=2")[1]
        self.assertTrue(page["hasMore"])
        self.assertEqual([r["version"] for r in page["changes"]], [1, 2])
        final = self.request("GET", f"/app/api/sync?cursor={page['cursor']}")[1]
        self.assertEqual(len(final["changes"]), 1)
        self.assertTrue(final["changes"][0]["deleted"])
        self.assertEqual(self.state.all_kind("channel"), [])

    def test_owner_can_read_legacy_deliveries_without_changing_publish_api(self):
        self.sign_in()
        delivery = {"id": "legacy-1", "title": "Brief", "publisher": "IPC", "section": "Test",
                    "summary": "summary", "systemImage": "doc.text", "body": "old data",
                    "deliveredAt": "2026-10-24T10:00:00Z", "needsAttention": False}
        self.assertEqual(self.request("POST", "/v1/deliveries", delivery, bearer="publisher-only")[0], 201)
        owner_page = self.request("GET", "/app/api/published")[1]
        self.assertEqual(owner_page["deliveries"][0], validate_delivery(delivery))
        self.assertEqual(self.delivery.count(), 1)

    def test_pwa_assets_served_with_csp_and_no_content_leak(self):
        code, html, headers = self.request("GET", "/app/")
        self.assertEqual(code, 200)
        self.assertIn(b"My typed notes", html)
        self.assertNotIn(b"drawingCanvas", html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/app/sw.js")[0], 200)
        self.assertEqual(self.request("GET", "/app/rich-text.js")[0], 200)
        self.assertEqual(self.request("GET", "/app/icon-192.png")[0], 200)
        self.assertEqual(self.request("GET", "/app/../.env")[0], 404)

    def test_authenticated_pwa_export_round_trips_records_deliveries_and_native_bytes(self):
        self.assertEqual(self.request("GET", "/app/api/export")[0], 401)
        self.sign_in()
        native = PortableImportTests.archive()
        self.state.import_archive(native)
        self.state.set_config("hermes", {"url": "https://hermes.example.test", "username": "owner",
                                         "password": "do-not-export-this", "sourceId": "account-a"})
        tombstone = {"sourceId": "library", "kind": "section", "key": "retired",
                     "baseVersion": 0, "deleted": True, "value": {"name": "Retired"}}
        self.assertEqual(self.request("PUT", "/app/api/state", tombstone)[0], 200)
        delivery = {"id": "published-1", "title": "Résumé", "publisher": "IPC", "section": "Test",
                    "summary": "summary", "systemImage": "doc.text", "body": "published body ☕",
                    "deliveredAt": "2026-10-24T10:00:00Z", "needsAttention": False}
        self.assertEqual(self.request("POST", "/v1/deliveries", delivery, bearer="publisher-only")[0], 201)
        code, archive, _ = self.request("GET", "/app/api/export")
        self.assertEqual(code, 200)
        self.assertEqual((archive["format"], archive["version"]), ("agentic-compendium-pwa", 1))
        self.assertTrue(any(record["deleted"] for record in archive["records"]))
        self.assertIn("published-1", [item["id"] for item in archive["published"]])
        self.assertNotIn(OWNER, json.dumps(archive))
        self.assertNotIn("do-not-export-this", json.dumps(archive))
        self.assertNotIn("owner_credentials", json.dumps(archive))
        self.assertNotIn("verifier", json.dumps(archive))
        self.assertEqual(base64.b64decode(archive["nativeArchives"][0]["contents"]), native)
        target_path = Path(self.tmp.name) / "restored.sqlite3"
        target_delivery = DeliveryDatabase(target_path)
        target = PersonalState(target_path)
        restored = target.import_archive(json.dumps(archive).encode())
        self.assertGreater(restored["imported"], 0)
        self.assertEqual(target.import_archive(json.dumps(archive).encode())["imported"], 0)
        self.assertTrue(target.find("library", "section", "retired")["deleted"])
        self.assertEqual(base64.b64decode(target.find("account-a", "note", "hermes-direct:daily:run-1")["value"]["drawingData"]),
                         b"PencilKit-original-opaque-binary-bytes")
        self.assertEqual(target_delivery.count(), 1)
        self.assertFalse(target_delivery.ingest(validate_delivery(delivery))[1])
        broken = json.loads(json.dumps(archive)); broken["nativeArchives"][0]["contents"] = "bm90LXRoaXM="
        before = target.changes(0, 500)
        with self.assertRaisesRegex(Exception, "checksum"):
            target.import_archive(json.dumps(broken).encode())
        self.assertEqual(before, target.changes(0, 500))
        conflict_path = Path(self.tmp.name) / "conflict.sqlite3"
        DeliveryDatabase(conflict_path)
        conflict = PersonalState(conflict_path)
        conflict.put({"sourceId": "library", "kind": "channel", "key": "fixed-uuid", "baseVersion": 0,
                      "deleted": False, "value": {"name": "Different"}})
        with self.assertRaisesRegex(Exception, "conflicts"):
            conflict.import_archive(json.dumps(archive).encode())
        with conflict.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        published_conflict_path = Path(self.tmp.name) / "published-conflict.sqlite3"
        published_conflict_delivery = DeliveryDatabase(published_conflict_path)
        published_conflict_delivery.ingest(dict(delivery, body="different"))
        published_conflict = PersonalState(published_conflict_path)
        with self.assertRaisesRegex(Exception, "published delivery"):
            published_conflict.import_archive(json.dumps(archive).encode())
        self.assertFalse(published_conflict.changes(0, 500)["changes"])

    def test_malformed_import_structures_return_400_not_dropped_connections(self):
        self.sign_in()
        def native(name, data):
            raw = json.dumps(data).encode()
            return {"version": 1, "preferences": [], "drawingPreviews": [], "files": [
                {"path": name, "contents": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}]}
        library = base64.b64encode(json.dumps({"version": 1, "jobRoutes": []}).encode()).decode()
        cases = [native("personal-notes.json", {"version": 1, "notes": [{"deliveryID": 5}]}),
                 {"version": 1, "files": [], "drawingPreviews": [],
                  "preferences": [{"key": "compendiumChannelLibraryV1", "contents": library}]}]
        for archive in cases:
            code, data, _ = self.request("POST", "/app/api/import", archive)
            self.assertEqual(code, 400, data)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("POST", "/app/api/import", body=b"[" * 200_000,
                     headers={"Origin": ORIGIN, "Cookie": self.cookie, "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400); conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("POST", "/v1/deliveries", body=b"[" * 200_000,
                     headers={"Authorization": "Bearer publisher-only", "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400); conn.close()
        self.assertEqual(self.request("GET", "/app/api/sync")[1]["changes"], [])

    def test_export_refuses_archive_larger_than_the_import_limit(self):
        self.state.put({"sourceId": "library", "kind": "note", "key": "large", "baseVersion": 0,
                        "deleted": False, "value": {"contents": "x" * 200}})
        with mock.patch("server.pwa.MAX_PWA_ARCHIVE_BODY", 100):
            with self.assertRaisesRegex(Exception, "import limit"):
                self.state.export_archive()


class PortableImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PersonalState(Path(self.temp.name) / "compendium.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def archive(changed=False):
        identity = "account-a"
        raw_drawing = b"PencilKit-original-opaque-binary-bytes"
        png = b"\x89PNG\r\n\x1a\nview-only-png"
        note = {"deliveryID": f"source:{identity}|hermes-direct:daily:run-1",
                "text": "Owner's private annotations", "drawingData": base64.b64encode(raw_drawing).decode(),
                "isRead": True, "updatedAt": 803000000.0}
        snapshots = {"version": 1, "syncState": {"source": "hermes", "remoteSourceIdentity": identity},
                     "deliveries": [{"id": "hermes-direct:daily:run-1", "title": "Daily news",
                                     "publisher": "Hermes", "section": "Briefings", "summary": "digest",
                                     "systemImage": "doc.text", "body": "Full content", "deliveredAt": 803000000.0,
                                     "needsAttention": False, "hermesJobID": "daily"}]}
        originals = {
            "personal-notes.json": json.dumps({"version": 3, "notes": [note]}).encode(),
            "archives/" + "a" * 64 + ".json": json.dumps(snapshots).encode()
        }
        library = {"version": 1, "channels": [{"id": "fixed-uuid", "name": "Briefings" if not changed else "Other", "isArchived": False}],
                   "jobRoutes": {identity + "|daily": "fixed-uuid"},
                   "placements": {identity + "|hermes-direct:daily:run-1": "fixed-uuid"},
                   "deletedDeliveryIDs": [identity + "|hidden-run"], "timezoneIdentifier": "Australia/Sydney"}
        prefs = {"account-a": {"enabledJobIDs": ["daily"], "jobGroups": {"daily": "Briefings"},
                               "groupIDs": {"Briefings": "fixed-uuid"}}}
        def pref(key, value):
            return {"key": key, "encoding": "data" if key == "compendiumChannelLibraryV1" else "utf8",
                    "contents": base64.b64encode(json.dumps(value).encode()).decode()}
        return json.dumps({"version": 1, "exportedAt": "2026-10-24T00:00:00Z",
                           "files": [{"path": name, "contents": base64.b64encode(data).decode(),
                                      "sha256": hashlib.sha256(data).hexdigest()}
                                     for name, data in originals.items()],
                           "preferences": [pref("compendiumChannelLibraryV1", library),
                                           pref("hermesDeliveryPreferences", prefs),
                                           pref("hermesSourceIdentityAliases", {"old-origin": identity})],
                           "drawingPreviews": [{"noteID": note["deliveryID"], "png": base64.b64encode(png).decode()}]}).encode()

    def test_lossless_drawing_recovery_import_idempotence_routes_and_history(self):
        original = self.archive()
        result = self.store.import_archive(original)
        self.assertFalse(result["alreadyImported"])
        self.assertGreater(result["imported"], 5)
        self.assertTrue(self.store.import_archive(original)["alreadyImported"])
        with self.store.connect() as db:
            archived = db.execute("SELECT payload FROM app_native_archives").fetchone()
        self.assertEqual(archived[0], original)
        note = self.store.find("account-a", "note", "hermes-direct:daily:run-1")
        self.assertEqual(base64.b64decode(note["value"]["drawingData"]), b"PencilKit-original-opaque-binary-bytes")
        self.assertEqual(note["value"]["drawingPreview"], base64.b64encode(b"\x89PNG\r\n\x1a\nview-only-png").decode())
        self.assertTrue(note["value"]["isRead"])
        self.assertEqual(self.store.find("library", "channel", "fixed-uuid")["value"]["name"], "Briefings")
        self.assertEqual(self.store.find("account-a", "route", "daily")["value"]["channelId"], "fixed-uuid")
        self.assertTrue(self.store.find("account-a", "job", "daily")["value"]["enabled"])
        self.assertTrue(self.store.find("account-a", "hidden-delivery", "hidden-run"))
        self.assertEqual(self.store.find("account-a", "delivery", "hermes-direct:daily:run-1")["value"]["title"], "Daily news")
        self.assertEqual(len(self.store.all_kind("note", "other-account")), 0)

    def test_conflicting_import_does_not_overwrite_or_partially_apply(self):
        self.store.import_archive(self.archive())
        previous = self.store.changes(0, 500)
        with self.assertRaisesRegex(Exception, "conflicts"):
            self.store.import_archive(self.archive(changed=True))
        self.assertEqual(previous, self.store.changes(0, 500))

    def test_corrupt_archive_rejected_without_writes(self):
        payload = json.loads(self.archive())
        payload["files"][0]["sha256"] = "bad"
        with self.assertRaisesRegex(Exception, "checksum"):
            self.store.import_archive(json.dumps(payload).encode())
        self.assertFalse(self.store.changes(0, 100)["changes"])

    def test_pwa_archive_rejects_primitive_record_fields_without_writes(self):
        archive = {"format": "agentic-compendium-pwa", "version": 1,
                   "records": [{"sourceId": "library", "kind": [], "key": "bad", "version": 1,
                                "deleted": False, "value": {}}], "published": [], "nativeArchives": []}
        with self.assertRaisesRegex(Exception, "invalid kind"):
            self.store.import_archive(json.dumps(archive).encode())
        self.assertFalse(self.store.changes(0, 100)["changes"])

    def test_pwa_envelope_can_exceed_native_limit_but_remains_bounded(self):
        path = Path(self.temp.name) / "pwa-limit.sqlite3"
        DeliveryDatabase(path)
        source = PersonalState(path)
        original = b"opaque-native-bytes" * 8
        archive = {"format": "agentic-compendium-pwa", "version": 1, "records": [], "published": [],
                   "nativeArchives": [{"digest": hashlib.sha256(original).hexdigest(),
                                       "contents": base64.b64encode(original).decode()}]}
        with mock.patch("server.pwa.MAX_IMPORT_BODY", 100), mock.patch("server.pwa.MAX_PWA_ARCHIVE_BODY", 1000):
            self.assertGreater(len(json.dumps(archive).encode()), 100)
            source.import_archive(json.dumps(archive).encode())
            exported = source.export_archive()
            self.assertGreater(len(json.dumps(exported).encode()), 100)
            target_path = Path(self.temp.name) / "pwa-limit-restored.sqlite3"
            DeliveryDatabase(target_path)
            target = PersonalState(target_path)
            target.import_archive(json.dumps(exported).encode())
            with target.connect() as db:
                self.assertEqual(db.execute("SELECT payload FROM app_native_archives").fetchone()[0], original)
            with self.assertRaisesRegex(Exception, "native archive exceeds"):
                source.import_archive(json.dumps({"version": 1, "files": [], "preferences": [],
                                                  "drawingPreviews": [], "padding": "x" * 100}).encode())


class HermesIntegrationTests(unittest.TestCase):
    def test_login_accepts_empty_success_response(self):
        class EmptyResponse:
            def __init__(self, url): self.url = url
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _): return b""
        class FakeOpener:
            def open(self, request, timeout=None): return EmptyResponse(request.full_url)
        client = HermesAdapter("https://hermes.example.test", "owner", "private")
        client.opener = FakeOpener()
        client.login()
        self.assertTrue(client.authed)

    def test_only_explicit_https_allowlisted_origin_can_receive_credentials(self):
        allowed = {"https://hermes.example.test"}
        self.assertEqual(checked_hermes_origin("https://hermes.example.test/", allowed),
                         "https://hermes.example.test")
        for unsafe in ("http://hermes.example.test", "https://evil.example",
                       "https://hermes.example.test@evil.example", "https://hermes.example.test/private"):
            with self.assertRaises(ValueError):
                checked_hermes_origin(unsafe, allowed)

    def test_hermes_bounded_import_idempotence_and_script_preview(self):
        class Fake(HermesAdapter):
            def __init__(self):
                pass
            def jobs(self):
                return [{"id": "daily", "name": "Daily summary", "profile": "prime"},
                        {"id": "disabled", "name": "Disabled"}]
            def runs(self, job_id, profile):
                return [{"id": "run-1", "ended_at": 1760000000, "started_at": 1759999900,
                         "source": "cron_output", "preview": "Script output"},
                        {"id": "run-2", "ended_at": 1760000001, "started_at": 1759999901}]
            def last_message(self, session_id, profile):
                return {"role": "assistant", "content": "Final assistant result", "timestamp": 1760000001}
        with tempfile.TemporaryDirectory() as tmp:
            store = PersonalState(Path(tmp) / "pwa.sqlite3")
            result = Fake().pull(store, "source-1", {"daily"})
            self.assertEqual(result["imported"], 2)
            self.assertEqual(Fake().pull(store, "source-1", {"daily"})["imported"], 0)
            script = store.find("source-1", "delivery", "hermes-direct:daily:run-1")
            self.assertIn("Script-only cron output preview", script["value"]["body"])
            self.assertEqual(store.find("source-1", "delivery", "hermes-direct:daily:run-2")["value"]["body"], "Final assistant result")

    def test_probe_rotation_reaches_later_runs_and_retries_missing_without_overprobing(self):
        class Fake(HermesAdapter):
            def __init__(self): self.ready = False
            calls = None
            def jobs(self): return [{"id": "daily", "name": "Daily"}]
            def runs(self, *_):
                return ([{"id": f"empty-{index}", "ended_at": index} for index in range(20)] +
                        [{"id": "valid", "ended_at": 30}])
            def last_message(self, session_id, profile):
                self.calls.append(session_id)
                if session_id == "valid": return {"content": "later valid", "timestamp": 30}
                return {"content": "recovered", "timestamp": 31} if self.ready and session_id == "empty-0" else None
        with tempfile.TemporaryDirectory() as tmp:
            store, fake = PersonalState(Path(tmp) / "pwa.sqlite3"), Fake()
            fake.calls, positions = [], {}
            self.assertEqual(fake.pull(store, "source-1", {"daily"}, positions)["imported"], 0)
            self.assertEqual(fake.calls, [f"empty-{index}" for index in range(20)])
            self.assertIsNone(store.find("source-1", "delivery", "hermes-direct:daily:empty-0"))
            fake.calls = []
            self.assertEqual(fake.pull(store, "source-1", {"daily"}, positions)["imported"], 1)
            self.assertEqual(fake.calls[0], "valid")
            self.assertLessEqual(len(fake.calls), 20)
            fake.ready = True
            fake.calls = []
            self.assertEqual(fake.pull(store, "source-1", {"daily"}, positions)["imported"], 1)
            self.assertNotIn("valid", fake.calls)

    def test_one_hundred_missing_runs_use_at_most_twenty_probes_per_poll(self):
        class Fake(HermesAdapter):
            def __init__(self): self.calls = []
            def jobs(self): return [{"id": "daily", "name": "Daily"}]
            def runs(self, *_): return [{"id": f"run-{index}", "ended_at": index} for index in range(100)]
            def last_message(self, session_id, profile): self.calls.append(session_id)
        with tempfile.TemporaryDirectory() as tmp:
            store, fake, positions = PersonalState(Path(tmp) / "pwa.sqlite3"), Fake(), {}
            fake.pull(store, "source-1", {"daily"}, positions)
            first = list(fake.calls)
            fake.calls = []
            fake.pull(store, "source-1", {"daily"}, positions)
            self.assertEqual(len(first), 20)
            self.assertEqual(len(fake.calls), 20)
            self.assertEqual(first, [f"run-{index}" for index in range(20)])
            self.assertEqual(fake.calls, [f"run-{index}" for index in range(20, 40)])

    def test_new_head_run_precedes_existing_backlog_after_window_rotation(self):
        class Fake(HermesAdapter):
            def __init__(self): self.calls = []; self.new_head = False
            def jobs(self): return [{"id": "daily", "name": "Daily"}]
            def runs(self, *_):
                backlog = [{"id": f"run-{index}", "ended_at": index} for index in range(100)]
                return ([{"id": "new-head", "ended_at": 101}] + backlog[:99]) if self.new_head else backlog
            def last_message(self, session_id, profile): self.calls.append(session_id)
        with tempfile.TemporaryDirectory() as tmp:
            store, fake, positions = PersonalState(Path(tmp) / "pwa.sqlite3"), Fake(), {}
            fake.pull(store, "source-1", {"daily"}, positions)
            fake.new_head, fake.calls = True, []
            fake.pull(store, "source-1", {"daily"}, positions)
            self.assertEqual(fake.calls[0], "new-head")
            self.assertIn("run-20", fake.calls)
            self.assertLessEqual(len(fake.calls), 20)


if __name__ == "__main__":
    unittest.main()
