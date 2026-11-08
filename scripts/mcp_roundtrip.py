"""Exercise the actual stdio process using the official MCP client, not a protocol mock."""
import argparse
import asyncio
import importlib.metadata
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from architect_harness.core import GraphService


def unpack(result) -> dict:
    if result.is_error:
        raise RuntimeError(str(result.content))
    value = result.structured_content
    if isinstance(value, dict):
        return value
    for item in result.content:
        if hasattr(item, "text"):
            return json.loads(item.text)
    raise RuntimeError("No structured MCP result")


async def resolve_paged(client, value: dict) -> dict:
    if not value.get("paged"):
        return value
    offset, chunks = 0, []
    while offset is not None:
        page = unpack(await client.call_tool("read_detail", {"detail_id": value["detail_id"], "offset": offset}))
        chunks.append(page["text"])
        offset = page["next_offset"]
    serialized = "".join(chunks)
    assert hashlib.sha256(serialized.encode("utf-8")).hexdigest() == value["sha256"]
    return json.loads(serialized)


async def run() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        database = str(Path(directory) / "mcp.sqlite")
        host = GraphService(database)
        host.initialize("mcp-demo", "Реальный MCP roundtrip")
        params = StdioServerParameters(command=sys.executable,
                    args=["-m", "architect_harness", "--db", database, "serve"], env=dict(os.environ))
        async with Client(params, read_timeout_seconds=20) as client:
            listing = await client.list_tools()
            names = sorted(tool.name for tool in listing.tools)
            assert "approve_change" not in names
            paged_methodology = unpack(await client.call_tool("get_methodology"))
            assert paged_methodology["paged"]
            methodology = await resolve_paged(client, paged_methodology)
            search = unpack(await client.call_tool("search_detail", {"detail_id": paged_methodology["detail_id"], "query": "workflow"}))
            assert search["matches"]
            initial = await resolve_paged(client, unpack(await client.call_tool("get_context")))
            operations = [{"op":"upsert_node","node":{"id":"root","kind":"system","title":"Система","data":{}}},
                          {"op":"upsert_node","node":{"id":"external","kind":"external_system","title":"Внешняя граница","data":{}}}]
            proposal = unpack(await client.call_tool("propose_change", {
                "base_revision":0,"operations":operations,"rationale":"Проверка реального протокола и границы полномочий"}))
            assert proposal["review_required"]
            denied = await client.call_tool("apply_change", {"change_id":proposal["id"],"expected_revision":0})
            assert denied.is_error
            error_text = next(item.text for item in denied.content if hasattr(item, "text"))
            # Official SDK prefixes anticipated ToolError messages with the tool name.
            error_payload = json.loads(error_text[error_text.index("{"):])
            assert error_payload["code"] == "review_required"
            assert host.snapshot()["revision"] == 0
            host.approve_change(proposal["id"], 0, "demo-operator")
            applied = unpack(await client.call_tool("apply_change", {"change_id":proposal["id"],"expected_revision":0}))
            repeated = unpack(await client.call_tool("apply_change", {"change_id":proposal["id"],"expected_revision":0}))
            assert applied["revision"] == repeated["revision"] == 1
            report = unpack(await client.call_tool("record_observation", {
                "title":"Отчёт клиента","source":"demo-client","conditions":"MCP subprocess",
                "result":"Заявление клиента; не автоматическое подтверждение внешнего контракта.","related_ids":["external"]}))
            resumed = unpack(await client.call_tool("get_context", {"focus":"root"}))
            assert resumed["revision"] == 2
            checkpoint = {"objective": "Continue after compaction", "requirements": ["Retain exact source links"],
                          "decisions": [], "open_questions": ["External contract unknown"], "in_progress": [],
                          "next_actions": ["Inspect external"], "references": [paged_methodology["detail_id"]]}
            saved = unpack(await client.call_tool("save_checkpoint", {"expected_version": 0, "checkpoint": checkpoint}))
            assert saved["version"] == 1
            conflict = await client.call_tool("save_checkpoint", {"expected_version": 0, "checkpoint": checkpoint})
            assert conflict.is_error
        async with Client(params, read_timeout_seconds=20) as client:
            restored = unpack(await client.call_tool("get_checkpoint"))
            assert restored["checkpoint"] == checkpoint
            assert restored["version"] == 1
            assert (await resolve_paged(client, paged_methodology)) == methodology
        evidence = [n for n in host.snapshot()["nodes"] if n["kind"] == "evidence"]
        assert evidence[0]["data"]["provenance"] == "reported"
        final_revision = host.snapshot()["revision"]
        host.close()
        return {"scenario":"official-sdk-stdio-roundtrip", "success":True,
                "mcp_sdk":importlib.metadata.version("mcp"), "tools":names,
                "methodology_version":methodology["version"],"initial_context_keys":sorted(initial),
                "final_revision":final_revision,"review_boundary_exercised":True,
                "idempotent_retry_exercised":True,"reported_provenance_preserved":True,
                "actionable_error_code_preserved":True,
                "paged_detail_roundtrip_hash_verified":True,
                "archive_search_exercised":True,
                "checkpoint_conflict_and_restart_exercised":True,
                "limits":"Проверен локальный SDK клиент/сервер; конкретный пользовательский host и его LLM не подключались."}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--output"); args=parser.parse_args()
    text=json.dumps(asyncio.run(run()),ensure_ascii=False,indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True,exist_ok=True)
        Path(args.output).write_text(text+"\n",encoding="utf-8")
    print(text)
