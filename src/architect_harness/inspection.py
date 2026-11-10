# SPDX-License-Identifier: Apache-2.0
"""Bounded, read-only AST inspection. Findings are hints, never runtime evidence."""

import ast
import hashlib
import os
from pathlib import Path

EXCLUDED = {".git", ".venv", "venv", "env", "build", "dist", "deps", "__pycache__", "node_modules"}
BLOCKING = ("requests.", "urllib.request.", "http.client.")


def _call_name(node, aliases):
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value, aliases)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return "<dynamic>"


class _ImportTime(ast.NodeVisitor):
    def __init__(self, aliases):
        self.aliases, self.calls = aliases, []

    def visit_Call(self, node):
        self.calls.append({"call": _call_name(node.func, self.aliases), "line": node.lineno})
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        # Function bodies are deferred; decorators and argument defaults are not.
        for value in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if value is not None:
                self.visit(value)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        for value in [*node.args.defaults, *node.args.kw_defaults]:
            if value is not None:
                self.visit(value)


class _AsyncBody(ast.NodeVisitor):
    def __init__(self, function, aliases):
        self.function, self.aliases, self.hints = function, aliases, []

    def visit_Call(self, node):
        name = _call_name(node.func, self.aliases)
        if name == "time.sleep" or name.startswith(BLOCKING):
            self.hints.append({"function": self.function, "call": name, "line": node.lineno,
                               "confidence": "heuristic", "reason": "Возможный блокирующий вызов в async-функции"})
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        pass

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef


def inspect_python(root: str, max_files: int = 500) -> dict:
    """Inspect source without importing it. Symlink directories are never traversed."""
    base = Path(root).resolve(strict=True)
    if not base.is_dir():
        raise ValueError("root должен быть каталогом")
    if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= 10000:
        raise ValueError("max_files должен быть целым числом от 1 до 10000")
    paths, skipped, truncated = [], [], False
    for directory, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not Path(directory, d).is_symlink())
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = Path(directory, name)
            try:
                path.resolve(strict=True).relative_to(base)
            except (ValueError, OSError, RuntimeError):
                skipped.append({"path": str(path.relative_to(base)), "reason": "outside_root_or_broken_link"})
                continue
            if len(paths) >= max_files:
                truncated = True
                break
            paths.append(path)
        if truncated:
            break
    files, digest = [], hashlib.sha256()
    for path in sorted(paths):
        relative = path.relative_to(base).as_posix()
        parts = list(path.relative_to(base).with_suffix("").parts)
        if parts[0] == "src":
            parts.pop(0)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        item = {"path": relative, "module": module, "imports": [], "import_time_calls": [], "async_blocking_hints": []}
        files.append(item)
        try:
            with path.open("rb") as stream:
                raw = stream.read(2_000_001)
            if len(raw) > 2_000_000:
                item["error"] = "file_exceeds_2MB"
                digest.update(relative.encode() + b"\0file_exceeds_2MB\0")
                continue
            item["sha256"] = hashlib.sha256(raw).hexdigest()
            digest.update(relative.encode() + b"\0" + raw + b"\0")
            tree = ast.parse(raw, filename=relative)
        except (OSError, SyntaxError, ValueError) as error:
            item["error"] = f"{type(error).__name__}: {error}"
            continue
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    parent = node.module or "" if isinstance(node, ast.ImportFrom) else ""
                    level = node.level if isinstance(node, ast.ImportFrom) else 0
                    item["imports"].append({"module": parent, "name": alias.name, "alias": alias.asname,
                                            "line": node.lineno, "level": level, "form": type(node).__name__})
                    local = alias.asname or (alias.name if parent else alias.name.split(".")[0])
                    aliases[local] = f"{parent}.{alias.name}" if parent else (alias.name if alias.asname else local)
        visitor = _ImportTime(aliases)
        visitor.visit(tree)
        item["import_time_calls"] = sorted(visitor.calls, key=lambda x: x["line"])
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                visitor = _AsyncBody(node.name, aliases)
                for statement in node.body:
                    visitor.visit(statement)
                item["async_blocking_hints"].extend(visitor.hints)
        item["imports"].sort(key=lambda x: (x["line"], x["name"]))
    known = {item["module"] for item in files if item["module"]}
    edges = []
    for item in files:
        for imp in item["imports"]:
            parent = imp["module"]
            if imp["level"]:
                package = item["module"].split(".")
                if not item["path"].endswith("/__init__.py"):
                    package = package[:-1]
                climb = imp["level"] - 1
                if climb >= len(package):
                    continue
                parent = ".".join((package[:-climb] if climb else package) + ([parent] if parent else []))
            candidate = f"{parent}.{imp['name']}" if parent else imp["name"]
            targets = [candidate, parent] if imp["form"] == "ImportFrom" else [candidate]
            target = next((p for p in targets if p in known), None)
            if target and target != item["module"]:
                edges.append({"source": item["module"], "target": target, "line": imp["line"], "kind": "import"})
    return {"root": str(base), "provenance": "static_source_inspection", "files": files,
            "relationships": edges, "digest": digest.hexdigest(), "truncated": truncated, "skipped": skipped,
            "limitations": ["Код не исполнялся; AST не подтверждает поведение при запуске.",
                            "Динамические импорты, sys.path, плагины и побочные связи не восстанавливаются.",
                            "Подсказки о вызовах эвристические: не учитывают полную область видимости, ветвления и подмену имён.",
                            "Вызовы при импорте — потенциальные: условия ветвей не вычисляются; аннотации не исследуются.",
                            "Применены лимиты числа файлов и 2 MB на файл; digest относится только к прочитанному набору."]}
