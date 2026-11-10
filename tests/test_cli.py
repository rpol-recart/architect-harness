# SPDX-License-Identifier: Apache-2.0
"""Operator lifecycle commands, exercised through the real module entry point."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from architect_harness.core import GraphService


class LifecycleCLITests(unittest.TestCase):
    def test_list_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "graph.sqlite"
            service = GraphService(db)
            service.initialize("cli", "CLI")
            change = service.propose_change(0, [{"op": "upsert_node", "node": {
                "id": "root", "kind": "system", "title": "Root", "data": {}}}], "CLI test")
            service.close()

            def run(*args):
                return subprocess.run([sys.executable, "-m", "architect_harness", "--db", str(db), *args],
                                      capture_output=True, text=True)

            before = (db.stat().st_mtime_ns, db.stat().st_size, db.read_bytes())
            listed = run("list-changes")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn("revision=0", listed.stdout)
            self.assertIn(f"{change['id']}|proposed|proposed|0|-|", listed.stdout)
            self.assertEqual(before, (db.stat().st_mtime_ns, db.stat().st_size, db.read_bytes()))
            for missing in (("cancel", change["id"], "--revision", "0"),
                            ("cancel", change["id"], "--reviewer", "Test")):
                self.assertEqual(run(*missing).returncode, 2)
            cancelled = run("cancel", change["id"], "--revision", "0", "--reviewer", "Test")
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            self.assertEqual(json.loads(cancelled.stdout)["effective_status"], "cancelled")
            self.assertIn(f"{change['id']}|proposed|cancelled|", run("list-changes").stdout)
            for command, extra in (("cancel", ["--reviewer", "Test"]),
                                   ("approve", ["--reviewer", "Test"]), ("apply", [])):
                denied = run(command, change["id"], "--revision", "0", *extra)
                self.assertEqual(denied.returncode, 2)
                self.assertEqual(json.loads(denied.stderr)["error"], "proposal_cancelled")

    def test_listing_missing_database_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "missing.sqlite"
            with self.assertRaises(sqlite3.OperationalError):
                GraphService.list_changes(db)
            self.assertFalse(db.exists())
