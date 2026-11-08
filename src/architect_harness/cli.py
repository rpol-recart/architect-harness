"""Operator CLI. The approve command belongs to a trusted host/user boundary."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .context import get_context
from .core import GraphService, HarnessError
from .presentation import mermaid


def read_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Архитектурный граф, решения и свидетельства")
    cli.add_argument("--db", default="architecture.sqlite", help="SQLite-файл проекта")
    actions = cli.add_subparsers(dest="command", required=True)
    p = actions.add_parser("init"); p.add_argument("--project-id", default="project"); p.add_argument("--title", default="Новый проект")
    p = actions.add_parser("context"); p.add_argument("--focus"); p.add_argument("--depth", type=int, default=1); p.add_argument("--max-nodes", type=int, default=30); p.add_argument("--profile", action="append")
    p = actions.add_parser("propose"); p.add_argument("file", help="JSON: base_revision, operations, rationale")
    p = actions.add_parser("show-change"); p.add_argument("id")
    p = actions.add_parser("inspect"); p.add_argument("id")
    p = actions.add_parser("approve"); p.add_argument("id"); p.add_argument("--revision", type=int, required=True); p.add_argument("--reviewer", required=True)
    p = actions.add_parser("apply"); p.add_argument("id"); p.add_argument("--revision", type=int, required=True)
    p = actions.add_parser("observe"); p.add_argument("file", help="JSON report; provenance is always reported")
    p = actions.add_parser("probe-http"); p.add_argument("url"); p.add_argument("--allow-origin", action="append", required=True); p.add_argument("--related", action="append", required=True)
    p = actions.add_parser("inspect-python"); p.add_argument("root")
    p = actions.add_parser("opencode-install", help="Подключить харнесс к проекту OpenCode 2.2.5")
    p.add_argument("workspace", nargs="?", default="."); p.add_argument("--project-id", default="project")
    p.add_argument("--title", default="Новый проект"); p.add_argument("--profile", action="append", choices=["core", "python"])
    p.add_argument("--model", help="Необязательный provider/model; иначе используется модель OpenCode")
    p = actions.add_parser("opencode-doctor", help="Проверить локальную установку")
    p.add_argument("workspace", nargs="?", default="."); p.add_argument("--executable", default="opencode")
    p = actions.add_parser("opencode-context", help="Прочитать память без изменения БД (для plugin)")
    p.add_argument("--profile", action="append", choices=["core", "python"])
    p.add_argument("--max-bytes", type=int, default=16000)
    p = actions.add_parser("opencode-archive", help="Сохранить точный JSON из stdin перед compaction")
    p.add_argument("--session", required=True); p.add_argument("--kind", default="compaction-transcript")
    for name in ("snapshot", "history", "mermaid", "serve"):
        actions.add_parser(name)
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "opencode-install":
            from .opencode import install
            result = install(Path(args.workspace), args.project_id, args.title, args.profile, args.model)
        elif args.command == "opencode-doctor":
            from .opencode import doctor
            result = doctor(Path(args.workspace), args.executable)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 2
        elif args.command == "opencode-context":
            from .opencode import read_context
            result = read_context(args.db, args.profile, args.max_bytes)
        elif args.command == "opencode-archive":
            from .memory import ContextMemory, DETAIL_BYTES, memory_path
            raw = sys.stdin.buffer.read(DETAIL_BYTES + 1)
            if len(raw) > DETAIL_BYTES:
                raise HarnessError("detail_too_large", "Архив больше 32 MiB; сжатие отменено, исходные данные сохранены в OpenCode.")
            result = ContextMemory(memory_path(args.db)).archive(json.loads(raw), args.session, args.kind)
        elif args.command == "inspect-python":
            from .inspection import inspect_python
            result = inspect_python(args.root)
        else:
            service = GraphService(args.db)
            if args.command == "init":
                result = service.initialize(args.project_id, args.title)
            elif args.command == "context":
                result = get_context(service.snapshot(), args.focus, args.depth, args.max_nodes, args.profile)
            elif args.command == "propose":
                payload = read_json(args.file)
                result = service.propose_change(payload["base_revision"], payload["operations"], payload["rationale"], actor="operator")
            elif args.command == "show-change": result = service.get_change(args.id)
            elif args.command == "inspect": result = service.inspect(args.id)
            elif args.command == "approve": result = service.approve_change(args.id, args.revision, args.reviewer)
            elif args.command == "apply": result = service.apply_change(args.id, args.revision)
            elif args.command == "observe":
                data = read_json(args.file)
                allowed = {"title", "source", "conditions", "result", "related_ids"}
                if set(data) != allowed:
                    raise HarnessError("invalid_report", "Отчёт должен содержать только title, source, conditions, result, related_ids.")
                result = service.record_observation(**data, actor="operator")
            elif args.command == "probe-http":
                from .observations import probe_http
                result = probe_http(service, args.url, args.allow_origin, args.related)
            elif args.command == "snapshot": result = service.snapshot()
            elif args.command == "history": result = service.history()
            elif args.command == "mermaid":
                print(mermaid(service.snapshot()), end=""); return 0
            elif args.command == "serve":
                from .mcp_server import serve
                serve(args.db); return 0
            else: raise ValueError("Unknown command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except HarnessError as error:
        print(json.dumps({"error": error.code, "message": error.message, "details": error.details}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as error:
        print(json.dumps({"error": "input_or_runtime_error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
