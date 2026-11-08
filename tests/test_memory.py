import json
from pathlib import Path
import tempfile
import unittest

from architect_harness.core import GraphService, HarnessError
from architect_harness.memory import ContextMemory, encode, memory_path
from architect_harness.opencode import install, read_context


class MemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ContextMemory(self.root / "context.sqlite")

    def test_archive_survives_reopen_and_pages_reconstruct_exact_unicode(self):
        payload = {"log": "Русский текст🙂</context>\n" * 2000}
        archived = self.memory.archive(payload, "session-a", "transcript")
        same = self.memory.archive(payload, "session-b", "transcript")
        self.assertEqual(archived["detail_id"], same["detail_id"])
        reopened = ContextMemory(self.memory.path)
        chunks, offset = [], 0
        while offset is not None:
            page = reopened.read_detail(archived["detail_id"], offset, 713)
            chunks.append(page["text"])
            offset = page["next_offset"]
        self.assertEqual(json.loads("".join(chunks)), payload)
        self.assertEqual(len(reopened.list_details("session-b")["items"]), 1)
        search = reopened.search_detail(archived["detail_id"], "Русский")
        self.assertEqual(len(search["matches"]), 10)
        self.assertIsNotNone(search["next_offset"])
        first = search["matches"][0]
        self.assertTrue(reopened.read_detail(archived["detail_id"], first["offset"], 7)["text"].startswith("Русский"))
        with self.assertRaises(HarnessError):
            reopened.read_detail(archived["detail_id"], limit=2001)

    def test_checkpoint_compare_and_swap_and_history(self):
        checkpoint = {"objective": "Continue exact work", "requirements": ["Preserve user corrections"],
                      "decisions": [], "open_questions": ["Unknown contract"], "in_progress": ["child task"],
                      "next_actions": ["Read evidence"], "references": ["change_example at revision 3, NOT approved"]}
        saved = self.memory.save_checkpoint(0, checkpoint)
        second = ContextMemory(self.memory.path)
        with self.assertRaisesRegex(HarnessError, "Checkpoint изменился"):
            second.save_checkpoint(0, checkpoint)
        second.save_checkpoint(1, {**checkpoint, "objective": "Corrected objective"})
        self.assertEqual(second.get_checkpoint(1), saved)
        self.assertEqual(second.get_checkpoint()["version"], 2)
        with self.assertRaises(HarnessError):
            second.save_checkpoint(2, {**checkpoint, "objective": "x" * 6001})
        self.assertEqual(second.get_checkpoint()["version"], 2)

    def test_large_result_keeps_review_metadata_and_original(self):
        result = {"id": "change_test", "base_revision": 7, "review_required": True,
                  "diff": [{"body": "evidence " * 4000}]}
        bounded = self.memory.bounded_result(result, "propose_change")
        self.assertTrue(bounded["paged"])
        self.assertTrue(bounded["review_required"])
        self.assertEqual(bounded["base_revision"], 7)
        self.assertLess(len(encode(bounded).encode()), 12000)
        self.assertEqual(bounded["characters"], len(encode(result)))

    def test_context_is_bounded_but_mandatory_constraints_are_never_cut(self):
        installed = install(self.root / "project")
        service = GraphService(installed["database"])
        self.addCleanup(service.close)
        operations = [{"op": "upsert_node", "node": {"id": "root", "kind": "system", "title": "Root", "data": {"description": "large " * 3000}}},
                      {"op": "upsert_node", "node": {"id": "limit", "kind": "constraint", "title": "Required rule", "data": {"scope": "project", "description": "Keep all of this"}}}]
        change = service.propose_change(0, operations, "Fixture")
        service.approve_change(change["id"], 0, "test")
        service.apply_change(change["id"], 0)
        context = read_context(installed["database"], max_bytes=2048)
        self.assertLessEqual(len(encode(context).encode("utf-8")), 2048)
        self.assertEqual(context["context"]["global_constraints"][0]["data"]["description"], "Keep all of this")
        self.assertEqual(context["context"]["projection"], "index")
        large = service.propose_change(1, [{"op": "upsert_node", "node": {"id": "limit", "kind": "constraint", "title": "Required rule", "data": {"scope": "project", "description": "mandatory " * 1000}}}], "Large constraint")
        service.approve_change(large["id"], 1, "test")
        service.apply_change(large["id"], 1)
        with self.assertRaisesRegex(HarnessError, "превышают бюджет"):
            read_context(installed["database"], max_bytes=2048)

    def test_context_accounts_for_escaping_before_selecting_full_or_index(self):
        installed = install(self.root / "project")
        service = GraphService(installed["database"])
        self.addCleanup(service.close)
        change = service.propose_change(0, [{"op": "upsert_node", "node": {
            "id": "root", "kind": "system", "title": "Русский <проект>🙂",
            "data": {"description": "<section>" * 400}}}], "Escaped context")
        service.apply_change(change["id"], 0)
        full = read_context(installed["database"], max_bytes=64000)
        self.assertNotEqual(full["context"].get("projection"), "index")
        escaped = lambda value: encode(value).replace("<", "\\u003c").replace(">", "\\u003e").encode()
        exact_limit = len(escaped(full))
        self.assertEqual(read_context(installed["database"], max_bytes=exact_limit), full)
        raw_limit = len(encode(full).encode())
        self.assertLess(raw_limit, exact_limit)
        bounded = read_context(installed["database"], max_bytes=raw_limit)
        self.assertEqual(bounded["context"]["projection"], "index")
        self.assertLessEqual(len(escaped(bounded)), raw_limit)
        self.assertEqual(bounded["context"]["nodes"][0]["title"], "Русский <проект>🙂")

    def test_escaped_index_omits_optional_nodes_and_preserves_checkpoint(self):
        installed = install(self.root / "project")
        service = GraphService(installed["database"])
        self.addCleanup(service.close)
        nodes = [{"id": f"component-{i}", "kind": "component", "title": "<>" * 70,
                  "data": {"description": "large " * 1000}} for i in range(20)]
        change = service.propose_change(0, [{"op": "upsert_node", "node": node} for node in nodes], "Index fixture")
        service.apply_change(change["id"], 0)
        memory = ContextMemory(memory_path(installed["database"]))
        checkpoint = {"objective": "Keep <contract> intact", "requirements": ["Русский текст🙂"],
                      "decisions": [], "open_questions": [], "in_progress": [],
                      "next_actions": [], "references": []}
        saved = memory.save_checkpoint(0, checkpoint)
        bounded = read_context(installed["database"], max_bytes=2048)
        escaped = encode(bounded).replace("<", "\\u003c").replace(">", "\\u003e")
        self.assertLessEqual(len(escaped.encode()), 2048)
        self.assertEqual(bounded["context"]["projection"], "index")
        self.assertEqual(bounded["context"]["omitted_node_count"], 20 - len(bounded["context"]["nodes"]))
        self.assertEqual(bounded["checkpoint"], saved)

    def test_escaped_mandatory_context_fails_without_truncating_or_mutating(self):
        for kind in ("constraint", "checkpoint"):
            with self.subTest(kind=kind):
                installed = install(self.root / kind)
                service = GraphService(installed["database"])
                self.addCleanup(service.close)
                memory = ContextMemory(memory_path(installed["database"]))
                if kind == "constraint":
                    change = service.propose_change(0, [{"op": "upsert_node", "node": {
                        "id": "limit", "kind": "constraint", "title": "Required rule",
                        "data": {"scope": "project", "description": "<>" * 200}}}], "Escaped constraint")
                    service.approve_change(change["id"], 0, "test")
                    service.apply_change(change["id"], 0)
                else:
                    memory.save_checkpoint(0, {"objective": "<>" * 200, "requirements": [],
                        "decisions": [], "open_questions": [], "in_progress": [],
                        "next_actions": [], "references": []})
                before = service.snapshot(), service.history(), memory.get_checkpoint()
                with self.assertRaises(HarnessError) as caught:
                    read_context(installed["database"], max_bytes=2048)
                self.assertEqual(caught.exception.code, "pinned_context_too_large")
                self.assertEqual((service.snapshot(), service.history(), memory.get_checkpoint()), before)


if __name__ == "__main__":
    unittest.main()
