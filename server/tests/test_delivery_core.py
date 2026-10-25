"""Stable delivery contract independent of web owner-state changes."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from server.app import (ConflictError, DeliveryDatabase, ValidationError, check_secrets_replaced,
                        encode_cursor, validate_delivery)


def delivery(identifier):
    return {
        "id": identifier,
        "title": "Test delivery",
        "publisher": "Test publisher",
        "section": "Inbox",
        "summary": "Synthetic test message",
        "systemImage": "doc.text",
        "body": "No personal or production information",
        "deliveredAt": "2026-01-01T12:00:00Z",
        "needsAttention": False,
    }


class DeliveryContractTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = DeliveryDatabase(Path(self.temp.name) / "test.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_idempotent_published_history_and_conflict(self):
        item = validate_delivery(delivery("sample-1"))
        self.assertTrue(self.database.ingest(item)[1])
        self.assertFalse(self.database.ingest(item)[1])
        changed = dict(item, title="Different content")
        with self.assertRaises(ConflictError):
            self.database.ingest(changed)
        self.assertEqual(self.database.count(), 1)

    def test_cursor_pages_are_complete(self):
        for item in ["one", "two", "three"]:
            self.database.ingest(validate_delivery(delivery(item)))
        first, cursor, more = self.database.sync(None, 2)
        self.assertEqual(len(first), 2)
        self.assertTrue(more)
        second, final, more = self.database.sync(cursor, 2)
        self.assertEqual(len(second), 1)
        self.assertFalse(more)
        self.assertNotEqual(final, cursor)
        self.assertEqual({x["id"] for x in first + second}, {"one", "two", "three"})

    def test_cursor_survives_equal_and_backward_clock(self):
        with mock.patch("server.app.time.time_ns", side_effect=[200, 200, 100]):
            self.database.ingest(validate_delivery(delivery("first")))
            _, cursor, _ = self.database.sync(None, 10)
            self.database.ingest(validate_delivery(delivery("equal")))
            self.database.ingest(validate_delivery(delivery("backward")))
        page, _, more = self.database.sync(cursor, 10)
        self.assertEqual([x["id"] for x in page], ["equal", "backward"])
        self.assertFalse(more)

    def test_legacy_wall_clock_cursor_resumes_without_skipping(self):
        import base64, json
        with mock.patch("server.app.time.time_ns", side_effect=[200, 300, 100]):
            for item in ("seen", "later", "skewed"):
                self.database.ingest(validate_delivery(delivery(item)))
        legacy = base64.urlsafe_b64encode(json.dumps([200, "seen"]).encode()).decode().rstrip("=")
        page, cursor, _ = self.database.sync(legacy, 10)
        self.assertEqual([x["id"] for x in page], ["later", "skewed"])
        self.assertEqual(self.database.sync(cursor, 10)[0], [])
        caught_up = base64.urlsafe_b64encode(json.dumps([300, "later"]).encode()).decode().rstrip("=")
        self.assertEqual(self.database.sync(caught_up, 10)[0], [])

    def test_ingest_returns_resumable_position_for_new_and_repeat(self):
        self.database.ingest(validate_delivery(delivery("one")))
        _, _, position = self.database.ingest(validate_delivery(delivery("two")))
        self.assertEqual(self.database.ingest(validate_delivery(delivery("two")))[2], position)
        self.assertEqual(self.database.sync(encode_cursor(position), 10)[0], [])
        for bad in ("not-base64!", encode_cursor(1)[:-1] + "x", "W10", "WyJhIl0", "W3RydWVd"):
            with self.assertRaises(ValidationError):
                self.database.sync(bad, 10)

    def test_example_placeholder_secrets_refuse_startup(self):
        example = {"APP_READ_TOKEN": "a" * 48, "APP_PUBLISH_TOKEN": "b" * 48,
                   "APP_OWNER_PASSWORD": "replace-with-a-third-unique-random-64-hex-string"}
        with self.assertRaises(SystemExit) as raised:
            check_secrets_replaced(example)
        self.assertIn("APP_OWNER_PASSWORD", str(raised.exception))
        check_secrets_replaced(dict(example, APP_OWNER_PASSWORD="c" * 48))

    def test_invalid_extra_fields_rejected(self):
        item = delivery("sample-1")
        item["unexpected"] = "no"
        with self.assertRaises(ValidationError):
            validate_delivery(item)

    def test_delivery_store_starts_empty(self):
        self.assertEqual(self.database.count(), 0)
        self.assertEqual(self.database.sync(None, 10)[0], [])
