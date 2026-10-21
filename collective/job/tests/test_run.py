import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run  # noqa: E402

NAMES = {"US": "United States", "US-IL": "Illinois", "GB": "United Kingdom", "account:gmail": "Gmail"}


class LabelTest(unittest.TestCase):
    def doc(self, *pods, by_address=()):
        return {"k": 50, "city": {"players": 100, "byAddress": list(by_address)}, "pods": list(pods)}

    def test_labels(self):
        doc = run.add_labels(self.doc(
            {"id": "us-il-chicago", "level": "city", "country": "US", "region": "US-IL", "city": "Chicago", "rest": False},
            {"id": "us-il-rest", "level": "region", "country": "US", "region": "US-IL", "rest": True},
            {"id": "gb", "level": "country", "country": "GB", "rest": False, "includesWorld": True},
            {"id": "us-rest", "level": "country", "country": "US", "rest": True},
            {"id": "fr", "level": "country", "country": "FR", "rest": False},
            {"id": "world-rest", "level": "world", "rest": True},
            by_address=[{"id": "gmail"}, {"id": "hotmail"}]), NAMES)
        labels = [p["label"] for p in doc["pods"]]
        self.assertEqual(labels, ["Chicago", "Rest of Illinois", "United Kingdom & the rest of the world",
                                  "Rest of United States", "FR", "Rest of the World"])
        self.assertEqual([a["label"] for a in doc["city"]["byAddress"]], ["Gmail", "hotmail"])

    def test_load_names(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "n.tsv")
            with open(p, "w", encoding="utf-8") as f:
                f.write("US\tUnited States\n\n# comment\nUS-IL\tIllinois\nBR-SP\tS\u00e3o Paulo\n")
            self.assertEqual(run.load_names(p), {"US": "United States", "US-IL": "Illinois", "BR-SP": "S\u00e3o Paulo"})


    def test_names_tsv_labels_only_email_address_accounts(self):
        # byAddress carries email-address accounts only (build.sql allowlist).
        names = run.load_names(str(Path(run.__file__).resolve().parent / "names.tsv"))
        accts = {k[len("account:"):] for k in names if k.startswith("account:")}
        self.assertEqual(accts, {"gmail", "outlook", "icloud", "yahoo", "protonmail",
                                 "google", "apple_id", "microsoft"})
        sql = (Path(run.__file__).resolve().parent / "sql" / "build.sql").read_text()
        for a in accts:
            self.assertIn(f"'{a}'", sql)


class WriteTest(unittest.TestCase):
    def test_write_atomic_replaces_and_leaves_no_temp(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "collective.json")
        run.write_atomic(p, '{"a":1}')
        run.write_atomic(p, '{"a":2}')
        with open(p) as f:
            self.assertEqual(json.load(f), {"a": 2})
        self.assertEqual(os.listdir(d), ["collective.json"])

    def test_validate_rejects_missing_city(self):
        with self.assertRaises(ValueError):
            run.validate({"k": 50, "pods": []})
        with self.assertRaises(ValueError):
            run.validate({"k": 50, "city": {}, "pods": "nope"})
        run.validate({"k": 50, "city": {}, "pods": []})

    def test_failed_run_keeps_previous_file(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "collective.json")
        run.write_atomic(p, '{"previous":true}')
        code = run.main(["--out", p, "--names", os.devnull, "--dry-sql-output", "not json"], report=lambda m: None)
        self.assertEqual(code, 1)
        with open(p) as f:
            self.assertEqual(json.load(f), {"previous": True})


GOOD_DOC = json.dumps({"k": 50, "city": {"players": 60, "byAddress": []}, "pods": []})


class FailurePathTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "collective.json")
        run.write_atomic(self.p, '{"previous":true}')
        self.saved = (run.run_sql, run.refresh_geo, run.record_run)
        self.addCleanup(lambda: (setattr(run, "run_sql", self.saved[0]), setattr(run, "refresh_geo", self.saved[1]),
                                 setattr(run, "record_run", self.saved[2])))
        run.run_sql = lambda: GOOD_DOC + "\n"

    def test_geo_failure_leaves_previous_file_byte_identical(self):
        def boom(d):
            raise RuntimeError("docker cp failed")
        run.refresh_geo = boom
        run.record_run = lambda ok, detail: None
        reported = []
        code = run.main(["--out", self.p, "--names", os.devnull, "--geo-dir", self.d], report=reported.append)
        self.assertEqual(code, 1)
        with open(self.p, "rb") as f:
            self.assertEqual(f.read(), b'{"previous":true}')
        self.assertEqual(len(reported), 1)

    def test_report_called_when_record_run_raises(self):
        def boom(d):
            raise RuntimeError("geo")
        def hung(ok, detail):
            raise FileNotFoundError("docker")
        run.refresh_geo = boom
        run.record_run = hung
        reported = []
        code = run.main(["--out", self.p, "--names", os.devnull, "--geo-dir", self.d], report=reported.append)
        self.assertEqual(code, 1)
        self.assertEqual(len(reported), 1)

    def test_success_publishes_after_geo(self):
        order = []
        run.refresh_geo = lambda d: order.append("geo")
        run.record_run = lambda ok, detail: order.append("record")
        self.assertEqual(run.main(["--out", self.p, "--names", os.devnull, "--geo-dir", self.d], report=lambda m: None), 0)
        self.assertEqual(order, ["geo", "record"])
        with open(self.p) as f:
            self.assertIn("asOf", json.load(f))


class RecordRunTest(unittest.TestCase):
    def capture(self, detail, returncode=0, stderr=""):
        calls = []
        orig = run.subprocess.run
        def fake(cmd, **kw):
            calls.append(kw["input"])
            return type("R", (), {"returncode": returncode, "stderr": stderr, "stdout": ""})()
        run.subprocess.run = fake
        self.addCleanup(lambda: setattr(run.subprocess, "run", orig))
        run.record_run(True, detail)
        run.record_run(False, detail)
        return calls

    def test_detail_is_truncated_and_escaped(self):
        calls = self.capture("x" * 3000 + "'")
        self.assertIn("x" * 2000, calls[0])
        self.assertNotIn("x" * 2001, calls[0])
        self.assertIn("''", calls[0])

    def test_ok_is_true_or_false(self):
        calls = self.capture("x")
        self.assertIn("true", calls[0])
        self.assertIn("false", calls[1])
