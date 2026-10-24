"""Stable delivery contract independent of web owner-state changes."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from server.app import ConflictError, DeliveryDatabase, ValidationError, validate_delivery


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

    def test_invalid_extra_fields_rejected(self):
        item = delivery("sample-1")
        item["unexpected"] = "no"
        with self.assertRaises(ValidationError):
            validate_delivery(item)

    def test_delivery_store_starts_empty(self):
        self.assertEqual(self.database.count(), 0)
        self.assertEqual(self.database.sync(None, 10)[0], [])
