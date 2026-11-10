# SPDX-License-Identifier: Apache-2.0
"""Actual MCP SDK stdio lifecycle/schema regression."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from architect_harness.core import GraphService


@unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP extra is not installed")
class LifecycleMCPTests(unittest.TestCase):
    def test_supersedes_stdio(self):
        asyncio.run(self.roundtrip())

    async def roundtrip(self):
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        def unpack(result):
            self.assertFalse(result.is_error, result.content)
            if isinstance(result.structured_content, dict):
                return result.structured_content
            return json.loads(next(item.text for item in result.content if hasattr(item, "text")))

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "mcp.sqlite"
            host = GraphService(db)
            try:
                host.initialize("mcp", "MCP")
                operation = [{"op": "upsert_node", "node": {
                    "id": "root", "kind": "system", "title": "Root", "data": {}}}]
                target = host.propose_change(0, operation, "Original")
                params = StdioServerParameters(command=sys.executable,
                    args=["-m", "architect_harness", "--db", str(db), "serve"], env=dict(os.environ))
                async with Client(params, read_timeout_seconds=20) as client:
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    self.assertFalse({"cancel", "cancel_change", "approve", "approve_change"} & tools.keys())
                    schema = tools["propose_change"].input_schema
                    self.assertIn("supersedes", schema["properties"])
                    self.assertIn("approve+apply", schema["properties"]["supersedes"]["description"])
                    self.assertNotIn("supersedes", schema.get("required", []))
                    self.assertIn("superseded", tools["propose_change"].description)
                    replacement = unpack(await client.call_tool("propose_change", {
                        "base_revision": 0, "operations": operation, "rationale": "Replacement",
                        "supersedes": target["id"]}))
                    self.assertEqual(replacement["supersedes"], target["id"])
                    self.assertTrue(replacement["review_required"])
                    self.assertEqual(host.get_change(target["id"])["effective_status"], "proposed")
                    denied = await client.call_tool("apply_change", {
                        "change_id": replacement["id"], "expected_revision": 0})
                    self.assertTrue(denied.is_error)
                    self.assertEqual(host.get_change(target["id"])["effective_status"], "proposed")
                    host.approve_change(replacement["id"], 0, "Test")
                    applied = unpack(await client.call_tool("apply_change", {
                        "change_id": replacement["id"], "expected_revision": 0}))
                    self.assertEqual(applied["revision"], 1)
                    self.assertEqual(host.get_change(target["id"])["effective_status"], "superseded")
            finally:
                host.close()
