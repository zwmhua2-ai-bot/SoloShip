import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from p00_bridge import SoloShipP00, validate_task, canonical_sha  # noqa: E402


def task(req="p00_test_12345678"):
    return {
        "schema_version": "1.0", "request_id": req, "project_id": "demo-app",
        "title": "P00 test", "goal": "check handoff", "requested_paths": ["src/**"],
        "constraints": [], "non_goals": [],
        "acceptance": [
            {"id": "AC-01", "description": "accepted", "verification": "automated"}
        ],
    }


class P00Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bridge = SoloShipP00(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, obj):
        p = self.bridge.inbox / f"{obj['request_id']}.ready.json"
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return p

    def test_valid_receipt_report(self):
        obj = task()
        self.submit(obj)
        self.assertEqual(self.bridge.poll(), 1)
        key = "demo-app__" + obj["request_id"]
        receipt = json.loads((self.bridge.receipts / (key + ".receipt.json")).read_text(encoding="utf-8"))
        report = json.loads((self.bridge.reports / (key + ".report.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "ACCEPTED")
        self.assertEqual(receipt["spec_sha256"], canonical_sha(obj))
        self.assertEqual(report["status"], "P00_HANDOFF_OK")
        self.assertFalse(report["real_codex_run"])
        self.assertEqual(len(list(self.bridge.archive.iterdir())), 1)

    def test_duplicate_is_idempotent(self):
        obj = task()
        self.submit(obj)
        self.bridge.poll()
        key = "demo-app__" + obj["request_id"]
        first = json.loads((self.bridge.receipts / (key + ".receipt.json")).read_text(encoding="utf-8"))
        self.submit(obj)
        self.bridge.poll()
        second = json.loads((self.bridge.receipts / (key + ".receipt.json")).read_text(encoding="utf-8"))
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["status"], "DUPLICATE")

    def test_conflicting_duplicate_rejected(self):
        obj = task()
        self.submit(obj)
        self.bridge.poll()
        obj["goal"] = "different"
        self.submit(obj)
        self.bridge.poll()
        self.assertEqual(len(list(self.bridge.rejected.iterdir())), 1)
        self.assertTrue(any(self.bridge.receipts.glob("*.error.json")))

    def test_pending_not_read(self):
        p = self.bridge.inbox / "p00_test_12345678.pending.json"
        p.write_text(json.dumps(task()), encoding="utf-8")
        self.assertEqual(self.bridge.poll(), 0)

    def test_unknown_project_rejected(self):
        obj = task()
        obj["project_id"] = "arbitrary-project"
        self.submit(obj)
        self.bridge.poll()
        self.assertEqual(len(list(self.bridge.rejected.iterdir())), 1)

    def test_extra_fields_rejected(self):
        obj = task()
        obj["shell_command"] = "anything"
        self.assertTrue(validate_task(obj))


if __name__ == "__main__":
    unittest.main()
