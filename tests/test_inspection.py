# SPDX-License-Identifier: Apache-2.0
import tempfile
import unittest
from pathlib import Path

from architect_harness.inspection import inspect_python


class InspectionTests(unittest.TestCase):
    def test_imports_edges_hints_and_no_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src" / "app"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("from . import service\n")
            (package / "service.py").write_text(
                "import time as clock\nfrom .db import save\n"
                "raise RuntimeError('must not execute')\n"
                "client = open('never-created', 'w')\n"
                "async def work():\n    clock.sleep(1)\n"
                "    def nested():\n        clock.sleep(2)\n"
                "def normal():\n    open('also-never-created', 'w')\n")
            (package / "db.py").write_text("def save():\n    pass\n")
            result = inspect_python(directory)
            service = next(x for x in result["files"] if x["module"] == "app.service")
            self.assertEqual([x["call"] for x in service["async_blocking_hints"]], ["time.sleep"])
            self.assertEqual([x["call"] for x in service["import_time_calls"]], ["RuntimeError", "open"])
            self.assertIn(("app.service", "app.db"), {(e["source"], e["target"]) for e in result["relationships"]})
            self.assertIn(("app", "app.service"), {(e["source"], e["target"]) for e in result["relationships"]})
            self.assertEqual(result["digest"], inspect_python(directory)["digest"])
            self.assertFalse((root / "never-created").exists())

    def test_scope_limits_and_parse_error(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (root / "a.py").write_text("broken syntax !\n")
            (root / "b.py").write_text("x = 1\n")
            (root / "venv").mkdir()
            (root / "venv" / "hidden.py").write_text("x=1\n")
            external = Path(outside) / "secret.py"
            external.write_text("SECRET = 'must not read'\n")
            (root / "external.py").symlink_to(external)
            (root / "outside").symlink_to(outside, target_is_directory=True)
            result = inspect_python(directory)
            self.assertEqual([x["path"] for x in result["files"]], ["a.py", "b.py"])
            self.assertIn("SyntaxError", result["files"][0]["error"])
            self.assertEqual(result["skipped"][0]["path"], "external.py")
            self.assertTrue(inspect_python(directory, max_files=1)["truncated"])
            self.assertNotIn("SECRET", str(result))

    def test_defaults_are_import_time_but_function_body_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "a.py").write_text("def f(value=build()):\n    defer()\n")
            calls = inspect_python(directory)["files"][0]["import_time_calls"]
            self.assertEqual([x["call"] for x in calls], ["build"])


if __name__ == "__main__":
    unittest.main()
