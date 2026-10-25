import tempfile
import unittest
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server.app import DeliveryDatabase
from server.hermes import HermesAdapter, HermesError
from server.pwa import OwnerWeb, PersonalState


class HermesHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); path = Path(self.temp.name) / "state.sqlite"
        DeliveryDatabase(path); self.state = PersonalState(path)
        self.web = OwnerWeb(self.state, "long-owner-password-not-reused-for-publish", "https://compendium.example.test")
        self.config = {"url": "https://hermes.example.test", "username": "owner", "password": "secret", "sourceId": "source"}
        self.state.set_config("hermes", self.config)
    def tearDown(self): self.temp.cleanup()

    def test_missing_configuration_and_stale_connection_health(self):
        self.state.set_config("hermes", None)
        self.assertEqual(self.web._public_hermes()["health"]["state"], "not_configured")
        self.state.set_config("hermes", self.config)
        self.web.poll_seconds = 10
        old = (datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()
        self.web._save_hermes_health(self.config, state="healthy", lastPullAt=old)
        self.assertEqual(self.web._public_hermes()["health"]["state"], "stale")

    def test_fingerprint_discards_inflight_old_connection_result(self):
        replacement = dict(self.config, sourceId="new-source")
        self.state.set_config("hermes", replacement)
        self.web._save_hermes_health(self.config, state="healthy", lastSuccessAt="old")
        self.assertEqual(self.web._public_hermes()["health"]["state"], "unchecked")

    def test_pull_error_is_sanitised_and_idle_does_not_clear_it(self):
        class Broken:
            def pull(self, *args): raise HermesError("Hermes request was rejected", "auth")
        with mock.patch.object(self.web, "hermes", return_value=Broken()):
            self.state.put({"sourceId": "source", "kind": "job", "key": "daily", "baseVersion": 0, "deleted": False, "value": {"enabled": True}})
            with self.assertRaises(HermesError): self.web.pull_hermes()
        health = self.web._public_hermes()["health"]
        self.assertEqual(health["error"], {"code": "auth", "message": "Hermes request was rejected"})
        self.state.put({"sourceId": "source", "kind": "job", "key": "daily", "baseVersion": 1, "deleted": False, "value": {"enabled": False}})
        self.web.pull_hermes()
        health = self.web._public_hermes()["health"]
        self.assertEqual(health["state"], "idle"); self.assertEqual(health["error"]["code"], "auth")

    def test_check_success_clears_stale_and_failure_is_persisted_without_get_network(self):
        self.web.poll_seconds = 1
        old = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        self.web._save_hermes_health(self.config, state="healthy", lastPullAt=old)
        self.assertEqual(self.web._public_hermes()["health"]["state"], "stale")
        class Good:
            def jobs(self): return [{"id": "daily"}]
        with mock.patch.object(self.web, "hermes", return_value=Good()):
            self.assertEqual(self.web.check_hermes_jobs(), [{"id": "daily"}])
        self.assertEqual(self.web._public_hermes()["health"]["state"], "healthy")
        class Broken:
            def jobs(self): raise HermesError("Hermes authentication failed", "auth")
        with mock.patch.object(self.web, "hermes", return_value=Broken()):
            with self.assertRaises(HermesError): self.web.check_hermes_jobs()
            self.web.hermes.reset_mock()
            self.assertEqual(self.web._public_hermes()["health"]["error"]["code"], "auth")
            self.web.hermes.assert_not_called()  # GET health is storage-only.


class HermesAdapterFailureTests(unittest.TestCase):
    class Opener:
        def __init__(self, result): self.result = result
        def open(self, *_args, **_kwargs):
            if isinstance(self.result, BaseException): raise self.result
            return self.result
    class Response:
        def __init__(self, raw, url="https://hermes.example.test/api/cron/jobs?profile=all"): self.raw, self.url = raw, url
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _): return self.raw
    def client(self, result):
        client = HermesAdapter("https://hermes.example.test", "owner", "private"); client.authed = True; client.opener = self.Opener(result); return client
    def test_http_errors_have_actionable_safe_categories(self):
        from urllib.error import HTTPError
        for code, category, text in ((401, "auth", "authentication"), (429, "rate_limit", "rate limited"), (503, "unavailable", "unavailable")):
            error = HTTPError("https://hermes.example.test/private?token=secret", code, "raw secret", {}, BytesIO(b"secret-body"))
            with self.assertRaises(HermesError) as caught: self.client(error).jobs()
            self.assertEqual(caught.exception.category, category); self.assertIn(text, str(caught.exception)); self.assertNotIn("secret", str(caught.exception))
    def test_invalid_json_timeout_and_invalid_shapes_are_malformed_or_timeout(self):
        with self.assertRaises(HermesError) as caught: self.client(self.Response(b"not-json")).jobs()
        self.assertEqual(caught.exception.category, "malformed")
        from urllib.error import URLError
        with self.assertRaises(HermesError) as caught: self.client(URLError(TimeoutError())).jobs()
        self.assertEqual(caught.exception.category, "timeout")
        for raw in (b'{"unexpected":[]}', b'{"jobs":"not-a-list"}', b'42'):
            with self.assertRaises(HermesError) as caught: self.client(self.Response(raw)).jobs()
            self.assertEqual(caught.exception.category, "malformed")
    def test_invalid_message_container_is_not_silently_treated_as_empty(self):
        client = self.client(self.Response(b'{"messages":"bad"}', "https://hermes.example.test/api/sessions/x/messages"))
        with self.assertRaises(HermesError) as caught: client.last_message("x", None)
        self.assertEqual(caught.exception.category, "malformed")
