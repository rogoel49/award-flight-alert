import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestManage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.alerts = os.path.join(self.dir, "alerts.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_cli(self, args):
        env = dict(os.environ, AWARD_ALERTS=self.alerts)
        return subprocess.run([sys.executable, os.path.join(ROOT, "manage.py"), *args],
                              capture_output=True, text=True, env=env, cwd=ROOT)

    def read(self):
        with open(self.alerts) as f:
            return json.load(f)

    def raw(self):
        with open(self.alerts) as f:
            return f.read()

    def test_add_valid(self):
        r = self.run_cli(["add", "--name", "SFO Nov", "--from", "sfo", "--to", "NRT,HND",
                          "--cabin", "business", "--max-miles", "90000", "--min-seats", "2",
                          "--start", "2026-11-01", "--end", "2026-11-30"])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(out["alert"]["_resolved"]["max_miles"], 90000)
        data = self.read()
        self.assertEqual(len(data["alerts"]), 1)
        self.assertEqual(data["alerts"][0]["origins"], ["SFO"])  # upcased
        self.assertTrue(data["alerts"][0]["id"])

    def test_bad_airport_leaves_file_unchanged(self):
        self.run_cli(["add", "--name", "seed", "--from", "SFO", "--to", "NRT"])
        before = self.raw()
        r = self.run_cli(["add", "--name", "bad", "--from", "XX", "--to", "NRT"])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(json.loads(r.stdout)["ok"])
        self.assertEqual(self.raw(), before)

    def test_bad_date_rejected(self):
        r = self.run_cli(["add", "--name", "x", "--from", "SFO", "--to", "NRT",
                          "--start", "11/01/2026"])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(json.loads(r.stdout)["ok"])

    def test_bad_cabin_rejected(self):
        r = self.run_cli(["add", "--name", "x", "--from", "SFO", "--to", "NRT",
                          "--cabin", "busines"])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(json.loads(r.stdout)["ok"])
        self.assertFalse(os.path.exists(self.alerts))

    def test_cabin_normalized_to_lowercase(self):
        r = self.run_cli(["add", "--name", "x", "--from", "SFO", "--to", "NRT",
                          "--cabin", "First"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read()["alerts"][0]["cabin"], "first")

    def test_injection_name_stored_as_data(self):
        evil = '"; rm -rf ~ #`whoami`$(id)'
        r = self.run_cli(["add", "--name", evil, "--from", "SFO", "--to", "NRT"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read()["alerts"][0]["name"], evil)  # verbatim, not executed

    def test_add_same_content_is_idempotent(self):
        args = ["add", "--name", "n", "--from", "SFO", "--to", "NRT",
                "--max-miles", "90000", "--start", "2026-11-01", "--end", "2026-11-30"]
        self.run_cli(args)
        first_id = self.read()["alerts"][0]["id"]
        self.run_cli(args)  # same content -> same id -> replace, not duplicate
        data = self.read()
        self.assertEqual(len(data["alerts"]), 1)
        self.assertEqual(data["alerts"][0]["id"], first_id)

    def test_disable_then_rm_then_list(self):
        self.run_cli(["add", "--name", "x", "--from", "SFO", "--to", "NRT"])
        aid = self.read()["alerts"][0]["id"]
        r = self.run_cli(["disable", "--id", aid])
        self.assertTrue(json.loads(r.stdout)["ok"])
        self.assertFalse(self.read()["alerts"][0]["enabled"])
        r = self.run_cli(["list", "--json"])
        self.assertEqual(json.loads(r.stdout)["count"], 1)
        r = self.run_cli(["rm", "--id", aid])
        self.assertTrue(json.loads(r.stdout)["ok"])
        self.assertEqual(self.read()["alerts"], [])

    def test_rm_unknown_id_fails(self):
        r = self.run_cli(["rm", "--id", "nope"])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(json.loads(r.stdout)["ok"])

    def test_concurrent_adds_all_land(self):
        dests = ["NRT", "HND", "KIX", "ICN", "SIN"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(dests)) as ex:
            results = list(ex.map(
                lambda d: self.run_cli(["add", "--name", d, "--from", "SFO", "--to", d]),
                dests))
        for r in results:
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.read()["alerts"]), 5)  # lock serialized, none lost


if __name__ == "__main__":
    unittest.main()
