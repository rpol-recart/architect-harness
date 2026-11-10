# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
import tempfile
import unittest

from architect_harness.core import GraphService, HarnessError
from architect_harness.opencode import install, read_context


class OpenCodeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harness space '")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_install_and_reinstall_preserve_memory_and_custom_config(self):
        config_file = self.root / ".opencode/opencode.json"
        config_file.parent.mkdir()
        original = {"model": "local/example", "default_agent": "build",
                    "agents": {"existing": {"system": "Keep me"}},
                    "plugins": ["unrelated-plugin"], "mcp": {"servers": {"existing": {
                        "type": "local", "command": ["existing-server"]}}}}
        config_file.write_text(json.dumps(original))
        installed = install(self.root, "test", "Test")
        service = GraphService(installed["database"])
        self.addCleanup(service.close)
        proposal = service.propose_change(0, [{"op": "upsert_node", "node": {
            "id": "root", "kind": "system", "title": "Actual project", "data": {}}}], "Actual scenario")
        service.apply_change(proposal["id"], 0)
        before = config_file.read_bytes()
        again = install(self.root, "test", "Ignored replacement title")
        self.assertEqual(again["revision"], 1)
        self.assertEqual(service.snapshot()["title"], "Test")
        self.assertEqual(config_file.read_bytes(), before)
        config = json.loads(before)
        self.assertEqual(config["default_agent"], "build")
        self.assertEqual(config["agents"]["existing"], original["agents"]["existing"])
        self.assertEqual(config["mcp"]["servers"]["existing"], original["mcp"]["servers"]["existing"])
        self.assertEqual(config["plugins"][0], "unrelated-plugin")
        self.assertEqual(json.loads(config_file.with_name("opencode.json.before-architect").read_text()), original)
        # Quoted/spaced paths are argv entries, never shell snippets.
        self.assertEqual(config["mcp"]["servers"]["architect"]["command"][-2], str(self.root / ".architecture/architecture.sqlite"))

    def test_conflict_does_not_create_database_or_replace_user_file(self):
        path = self.root / ".opencode/opencode.json"
        path.parent.mkdir()
        original = '{"agents":{"architect":{"system":"My architect"}}}'
        path.write_text(original)
        with self.assertRaisesRegex(HarnessError, "agents.architect.system"):
            install(self.root)
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.root / ".architecture").exists())

    def test_jsonc_is_not_silently_shadowed(self):
        path = self.root / ".opencode/opencode.jsonc"
        path.parent.mkdir()
        path.write_text("{// keep comment\n}")
        with self.assertRaises(HarnessError):
            install(self.root)
        self.assertFalse(path.with_suffix(".json").exists())

    def test_modified_plugin_is_preserved(self):
        install(self.root)
        path = self.root / ".opencode/architect-harness/index.js"
        path.write_text("// user modification")
        with self.assertRaises(HarnessError):
            install(self.root)
        self.assertEqual(path.read_text(), "// user modification")

    def test_missing_context_does_not_create_database(self):
        database = self.root / "missing.sqlite"
        with self.assertRaises(FileNotFoundError):
            read_context(database)
        self.assertFalse(database.exists())

    def test_context_reads_committed_revision_and_keeps_global_constraint(self):
        installed = install(self.root)
        service = GraphService(installed["database"])
        self.addCleanup(service.close)
        proposal = service.propose_change(0, [{"op": "upsert_node", "node": {
            "id": "constraint", "kind": "constraint", "title": "No invented API", "data": {"scope": "project"}}}], "Global rule")
        service.approve_change(proposal["id"], 0, "test-operator")
        service.apply_change(proposal["id"], 0)
        before = service.history()
        context = read_context(installed["database"])["context"]
        self.assertEqual(context["revision"], 1)
        self.assertIn("No invented API", json.dumps(context))
        self.assertEqual(service.history(), before)


if __name__ == "__main__":
    unittest.main()
