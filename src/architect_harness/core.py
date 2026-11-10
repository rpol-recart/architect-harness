# SPDX-License-Identifier: Apache-2.0
"""Версионируемый граф и архитектурные операции без зависимости от MCP.

The host is the trust boundary: approve_change and record_captured_observation
must not be exposed as tools to an untrusted model. SQLite provides local
transactional integrity, not OS-level isolation or authentication.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator
from uuid import uuid4


NODE_KINDS = frozenset({
    "system", "actor", "external_system", "component", "data", "scenario",
    "constraint", "decision", "claim", "evidence", "artifact", "question",
})
EDGE_KINDS = frozenset({
    "contains", "interacts_with", "depends_on", "owns", "implements", "constrains",
    "addresses", "supports", "contradicts", "affects", "derived_from",
})
STATUSES = {
    "decision": ("proposed", "accepted", "superseded"),
    "claim": ("assumed", "supported", "contested"),
    "question": ("open", "answered", "deferred"),
}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
TERMINAL_EVENTS = {"cancel": "cancelled", "supersede": "superseded"}


class HarnessError(Exception):
    """Стабильная машинно читаемая ошибка доменного сервиса."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    def valid(item: Any, depth: int = 0) -> None:
        if depth > 64:
            raise ValueError("JSON nesting exceeds 64 levels")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise TypeError("JSON object keys must be strings")
            for child in item.values():
                valid(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                valid(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise TypeError("Value is not a JSON type")
    try:
        valid(value)
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        serialized.encode("utf-8")
        return serialized
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise HarnessError("invalid_input", "Значение должно быть корректным JSON.") from exc


def _text(value: Any, name: str, limit: int = 20000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise HarnessError("invalid_input", f"{name}: требуется непустая строка до {limit} символов.")
    return value


def _id(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise HarnessError("invalid_input", "ID должен содержать 1–128 допустимых ASCII-символов.")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise HarnessError("invalid_input", "Ревизия должна быть целым неотрицательным числом.")
    return value


def _keys(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if not isinstance(value, dict):
        raise HarnessError("invalid_input", "Ожидался объект JSON.")
    if any(not isinstance(key, str) for key in value):
        raise HarnessError("invalid_input", "Ключи объекта должны быть строками.")
    missing = required - value.keys()
    unknown = value.keys() - required - (optional or set())
    if missing or unknown:
        raise HarnessError("invalid_input", "Неверные поля объекта.", {
            "missing": sorted(missing), "unknown": sorted(unknown),
        })


def _node(value: dict) -> dict:
    _keys(value, {"id", "kind", "title", "data"})
    _id(value["id"])
    if value["id"].startswith("change_"):
        raise HarnessError("invalid_input", "Префикс change_ зарезервирован для предложений.")
    if not isinstance(value["kind"], str) or value["kind"] not in NODE_KINDS:
        raise HarnessError("invalid_input", "Неизвестный тип узла.")
    _text(value["title"], "title", 1000)
    if not isinstance(value["data"], dict):
        raise HarnessError("invalid_input", "data узла должен быть объектом.")
    result = json.loads(_json(value))
    data = result["data"]
    if result["kind"] in STATUSES:
        statuses = STATUSES[result["kind"]]
        data.setdefault("status", statuses[0])
        if data["status"] not in statuses:
            raise HarnessError("invalid_input", "Недопустимый статус узла.")
    if "tags" in data and (
        not isinstance(data["tags"], list)
        or any(not isinstance(tag, str) or not tag for tag in data["tags"])
    ):
        raise HarnessError("invalid_input", "data.tags должен быть массивом непустых строк.")
    if result["kind"] == "constraint":
        data.setdefault("scope", "local")
        if data["scope"] not in ("local", "project"):
            raise HarnessError("invalid_input", "Неизвестная область ограничения.")
        if "rule" in data:
            rule = data["rule"]
            _keys(rule, {"type", "source", "target"})
            if rule["type"] != "forbid_dependency":
                raise HarnessError("invalid_input", "Этот машинный тип правила не поддерживается.")
            _id(rule["source"])
            _id(rule["target"])
    return result


def _edge(value: dict) -> dict:
    _keys(value, {"id", "source", "target", "kind", "data"})
    for key in ("id", "source", "target"):
        _id(value[key])
    if value["id"].startswith("change_"):
        raise HarnessError("invalid_input", "Префикс change_ зарезервирован для предложений.")
    if not isinstance(value["kind"], str) or value["kind"] not in EDGE_KINDS:
        raise HarnessError("invalid_input", "Неизвестный тип связи.")
    if not isinstance(value["data"], dict):
        raise HarnessError("invalid_input", "data связи должен быть объектом.")
    return json.loads(_json(value))


def _validate_graph(graph: dict) -> None:
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    if len(nodes) != len(graph["nodes"]) or len(edges) != len(graph["edges"]) or nodes.keys() & edges.keys():
        raise HarnessError("invalid_graph", "ID узлов и связей должны быть уникальны.")
    children: dict[str, list[str]] = {key: [] for key in nodes}
    owners: dict[str, str] = {}
    for node in nodes.values():
        _node(node)
        rule = node["data"].get("rule") if node["kind"] == "constraint" else None
        if rule and (rule["source"] not in nodes or rule["target"] not in nodes):
            raise HarnessError("invalid_graph", "Ограничение ссылается на отсутствующий узел.", {"constraint": node["id"]})
    for edge in edges.values():
        _edge(edge)
        source, target, kind = edge["source"], edge["target"], edge["kind"]
        if source not in nodes or target not in nodes:
            raise HarnessError("invalid_graph", "Связь ссылается на отсутствующий узел.", {"edge": edge["id"]})
        if source == target:
            raise HarnessError("invalid_graph", "Связь узла с самим собой запрещена.", {"edge": edge["id"]})
        source_kind, target_kind = nodes[source]["kind"], nodes[target]["kind"]
        valid = True
        if kind == "owns":
            valid = source_kind in ("system", "component") and target_kind == "data"
            if target in owners:
                raise HarnessError("invalid_graph", "У данных может быть только один владелец.", {"data": target})
            owners[target] = source
        elif kind in ("supports", "contradicts"):
            valid = source_kind == "evidence" and target_kind in ("claim", "decision")
        elif kind == "implements":
            valid = source_kind == "artifact" and target_kind in ("system", "component", "decision", "scenario")
        elif kind == "constrains":
            valid = source_kind == "constraint" and target_kind != "evidence"
        if not valid:
            raise HarnessError("invalid_graph", "Типы узлов несовместимы с типом связи.", {"edge": edge["id"]})
        if kind == "contains":
            children[source].append(target)
    # Iterative DFS avoids a recursion limit on deeply nested architectural views.
    state: dict[str, int] = {}
    for start in nodes:
        if state.get(start) == 2:
            continue
        stack = [(start, False)]
        while stack:
            current, leave = stack.pop()
            if leave:
                state[current] = 2
                continue
            if state.get(current) == 1:
                raise HarnessError("invalid_graph", "Иерархия contains содержит цикл.")
            if state.get(current) == 2:
                continue
            state[current] = 1
            stack.append((current, True))
            stack.extend((child, False) for child in reversed(children[current]))
    for node in nodes.values():
        if node["kind"] != "constraint" or "rule" not in node["data"]:
            continue
        rule = node["data"]["rule"]
        forbidden = [edge["id"] for edge in edges.values()
                     if edge["kind"] in ("depends_on", "interacts_with")
                     and edge["source"] == rule["source"] and edge["target"] == rule["target"]]
        if forbidden:
            raise HarnessError("constraint_violation", "Нарушено ограничение зависимости.", {
                "constraint": node["id"], "edges": forbidden,
            })


def _transform(before: dict, operations: list[dict]) -> dict:
    if not isinstance(operations, list) or not operations or len(operations) > 1000:
        raise HarnessError("invalid_input", "Требуется от 1 до 1000 операций.")
    nodes = {node["id"]: deepcopy(node) for node in before["nodes"]}
    edges = {edge["id"]: deepcopy(edge) for edge in before["edges"]}
    original_node_ids, original_edge_ids = set(nodes), set(edges)
    original_evidence = {key for key, node in nodes.items() if node["kind"] == "evidence"}
    protected_links = {key for key, edge in edges.items()
                       if edge["source"] in original_evidence and edge["data"].get("origin") == "observation"}
    for operation in operations:
        if not isinstance(operation, dict):
            raise HarnessError("invalid_input", "Операция должна быть объектом.")
        action = operation.get("op")
        if action == "upsert_node":
            _keys(operation, {"op", "node"})
            node = _node(operation["node"])
            if node["id"] in original_edge_ids:
                raise HarnessError("invalid_graph", "Нельзя переиспользовать ID связи для узла в одном изменении.")
            if node["kind"] == "evidence" or node["id"] in original_evidence:
                raise HarnessError("immutable_evidence", "Свидетельства доступны только через запись наблюдений.")
            nodes[node["id"]] = node
        elif action == "upsert_edge":
            _keys(operation, {"op", "edge"})
            edge = _edge(operation["edge"])
            if edge["id"] in original_node_ids:
                raise HarnessError("invalid_graph", "Нельзя переиспользовать ID узла для связи в одном изменении.")
            if edge["id"] in protected_links:
                raise HarnessError("immutable_evidence", "Исходную связь наблюдения нельзя менять.")
            if edge["data"].get("origin") == "observation":
                raise HarnessError("invalid_input", "origin=observation зарезервирован для сервиса.")
            edges[edge["id"]] = edge
        elif action in ("remove_node", "remove_edge"):
            _keys(operation, {"op", "id"})
            ident = _id(operation["id"])
            collection = nodes if action == "remove_node" else edges
            if ident in original_evidence or ident in protected_links:
                raise HarnessError("immutable_evidence", "Свидетельство и его исходные связи неизменяемы.")
            if ident not in collection:
                raise HarnessError("not_found", "Удаляемый элемент не существует.", {"id": ident})
            del collection[ident]
        else:
            raise HarnessError("invalid_input", "Неизвестная операция.")
    after = {**before, "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
             "edges": sorted(edges.values(), key=lambda e: e["id"])}
    _validate_graph(after)
    return after


def _diff(before: dict, after: dict) -> list[dict]:
    differences = []
    for collection in ("nodes", "edges"):
        old = {item["id"]: item for item in before[collection]}
        new = {item["id"]: item for item in after[collection]}
        for ident in sorted(old.keys() | new.keys()):
            if old.get(ident) != new.get(ident):
                differences.append({"collection": collection, "id": ident,
                                    "before": old.get(ident), "after": new.get(ident)})
    return differences


def _review_reasons(diff: list[dict], before: dict, after: dict) -> list[str]:
    reasons = set()
    protected_sources = {node["id"] for graph in (before, after) for node in graph["nodes"]
                         if node["kind"] == "constraint"
                         or (node["kind"] == "decision" and node["data"].get("status") == "accepted")}
    for item in diff:
        old, new, ident = item["before"], item["after"], item["id"]
        candidates = [candidate for candidate in (old, new) if candidate]
        if old and new and old["kind"] != new["kind"]:
            reasons.add(f"Изменение типа элемента: {ident}")
        if item["collection"] == "nodes":
            if any(candidate["kind"] in ("external_system", "data", "constraint") for candidate in candidates):
                reasons.add(f"Изменение внешней системы, данных или ограничения: {ident}")
            if any(candidate["kind"] == "decision" and candidate["data"].get("status") == "accepted"
                   for candidate in candidates):
                reasons.add(f"Принятие или изменение принятого решения: {ident}")
        else:
            if any(candidate["kind"] in ("depends_on", "interacts_with", "owns") for candidate in candidates):
                reasons.add(f"Изменение зависимости, взаимодействия или владения: {ident}")
            if any(candidate["source"] in protected_sources for candidate in candidates):
                reasons.add(f"Изменение области принятого решения или ограничения: {ident}")
    return sorted(reasons)


class GraphService:
    """Один локальный проект на файл SQLite; каждый экземпляр потокобезопасен."""

    def __init__(self, db_path: str | Path):
        path = str(db_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS project (singleton INTEGER PRIMARY KEY CHECK(singleton=1), snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions (revision INTEGER PRIMARY KEY, snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                kind TEXT NOT NULL, actor TEXT NOT NULL, revision INTEGER NOT NULL, details TEXT NOT NULL);
        """)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                yield
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _snapshot(self) -> dict:
        row = self._db.execute("SELECT snapshot FROM project WHERE singleton=1").fetchone()
        if row is None:
            raise HarnessError("not_initialized", "Сначала создайте проект командой init.")
        return json.loads(row[0])

    def _check_revision(self, expected: int, graph: dict) -> None:
        _revision(expected)
        if graph["revision"] != expected:
            raise HarnessError("stale_revision", "Граф изменился; загрузите актуальный контекст.", {
                "expected": expected, "actual": graph["revision"],
            })

    def _save(self, graph: dict) -> None:
        payload = _json(graph)
        self._db.execute("UPDATE project SET snapshot=? WHERE singleton=1", (payload,))
        self._db.execute("INSERT INTO revisions(revision,snapshot) VALUES (?,?)", (graph["revision"], payload))

    def _event(self, kind: str, actor: str, revision: int, details: dict) -> None:
        self._db.execute("INSERT INTO events(at,kind,actor,revision,details) VALUES (?,?,?,?,?)",
                         (_now(), kind, actor, revision, _json(details)))

    def _get_change(self, ident: str) -> dict:
        row = self._db.execute("SELECT payload FROM proposals WHERE id=?", (_id(ident),)).fetchone()
        if row is None:
            raise HarnessError("not_found", "Предложение не найдено.", {"id": ident})
        return json.loads(row[0])

    def _put_change(self, change: dict) -> None:
        self._db.execute("UPDATE proposals SET payload=? WHERE id=?", (_json(change), change["id"]))

    def _effective_status(self, proposal: dict) -> str:
        row = self._db.execute(
            "SELECT kind FROM events WHERE kind IN ('cancel','supersede') "
            "AND json_extract(details,'$.change_id')=? ORDER BY id DESC LIMIT 1",
            (proposal["id"],),
        ).fetchone()
        return TERMINAL_EVENTS[row[0]] if row else proposal["status"]

    def _check_active(self, proposal: dict) -> None:
        status = self._effective_status(proposal)
        if status in TERMINAL_EVENTS.values():
            raise HarnessError(f"proposal_{status}", "Предложение находится в терминальном состоянии.")

    def _check_supersedes_target(self, change_id: str) -> None:
        target = self._get_change(change_id)
        self._check_active(target)
        if target["status"] == "applied":
            raise HarnessError("supersedes_applied", "Нельзя заместить уже применённое предложение.")

    def cancel_change(self, change_id: str, expected_revision: int, reviewer: str) -> dict:
        """Host-only: append a terminal event without changing graph or payload."""
        _text(reviewer, "reviewer", 200)
        with self._transaction():
            graph = self._snapshot()
            self._check_revision(expected_revision, graph)
            change = self._get_change(change_id)
            self._check_active(change)
            if change["status"] == "applied":
                raise HarnessError("already_applied", "Предложение уже применено.")
            self._event("cancel", reviewer, graph["revision"], {"change_id": change_id})
            return {**change, "effective_status": self._effective_status(change)}

    def initialize(self, project_id: str, title: str) -> dict:
        _id(project_id)
        _text(title, "title", 1000)
        with self._transaction():
            row = self._db.execute("SELECT snapshot FROM project WHERE singleton=1").fetchone()
            if row:
                existing = json.loads(row[0])
                if existing["project_id"] != project_id:
                    raise HarnessError("project_exists", "Этот файл уже принадлежит другому проекту.")
                return existing
            graph = {"schema_version": "0.1", "methodology_version": "0.1", "project_id": project_id,
                     "title": title, "revision": 0, "nodes": [], "edges": []}
            self._db.execute("INSERT INTO project(singleton,snapshot) VALUES (1,?)", (_json(graph),))
            self._db.execute("INSERT INTO revisions(revision,snapshot) VALUES (0,?)", (_json(graph),))
            self._event("initialize", "host", 0, {"project_id": project_id})
            return graph

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot()

    def propose_change(self, base_revision: int, operations: list[dict], rationale: str, actor: str = "agent",
                       supersedes: str | None = None) -> dict:
        _text(rationale, "rationale")
        _text(actor, "actor", 200)
        with self._transaction():
            before = self._snapshot()
            self._check_revision(base_revision, before)
            after = _transform(before, operations)
            diff = _diff(before, after)
            if not diff:
                raise HarnessError("empty_change", "Предложение не изменяет граф.")
            reasons = _review_reasons(diff, before, after)
            change = {"id": "change_" + uuid4().hex, "base_revision": base_revision,
                      "status": "proposed", "operations": json.loads(_json(operations)),
                      "rationale": rationale, "actor": actor, "review_required": bool(reasons),
                      "review_reasons": reasons, "diff": diff, "created_at": _now()}
            if supersedes is not None:
                _id(supersedes)
                if supersedes == change["id"]:
                    raise HarnessError("invalid_input", "Предложение не может замещать само себя.")
                self._check_supersedes_target(supersedes)
                change["supersedes"] = supersedes
                # Superseding another proposal always requires the host's approval.
                reasons.append(f"Замещение предложения: {supersedes}")
                change["review_required"] = True
            self._db.execute("INSERT INTO proposals(id,payload) VALUES (?,?)", (change["id"], _json(change)))
            self._event("propose", actor, before["revision"], {"change_id": change["id"], "review_required": bool(reasons)})
            return deepcopy(change)

    def get_change(self, change_id: str) -> dict:
        with self._lock:
            change = self._get_change(change_id)
            return {**change, "effective_status": self._effective_status(change)}

    @classmethod
    def list_changes(cls, db_path: str | Path) -> dict:
        """Read proposal lifecycle in one snapshot, without initializing or writing a DB."""
        reader = cls.__new__(cls)
        reader._db = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", isolation_level=None)
        try:
            reader._db.execute("BEGIN")
            revision = reader._snapshot()["revision"]
            changes = []
            for (payload,) in reader._db.execute("SELECT payload FROM proposals ORDER BY rowid"):
                change = json.loads(payload)
                changes.append({**change, "effective_status": reader._effective_status(change)})
            return {"revision": revision, "changes": changes}
        finally:
            reader._db.close()

    def approve_change(self, change_id: str, expected_revision: int, reviewer: str) -> dict:
        """Host-only operation. Identity authentication belongs to the calling host."""
        _text(reviewer, "reviewer", 200)
        with self._transaction():
            graph = self._snapshot()
            self._check_revision(expected_revision, graph)
            change = self._get_change(change_id)
            self._check_active(change)
            if change["base_revision"] != expected_revision:
                raise HarnessError("stale_revision", "Предложение относится к другой ревизии.")
            if change["status"] == "applied":
                raise HarnessError("already_applied", "Предложение уже применено.")
            if change["status"] == "approved":
                return change
            change.update(status="approved", reviewer=reviewer, approved_at=_now())
            self._put_change(change)
            self._event("approve", reviewer, graph["revision"], {"change_id": change_id})
            return change

    def apply_change(self, change_id: str, expected_revision: int) -> dict:
        _revision(expected_revision)
        with self._transaction():
            change = self._get_change(change_id)
            self._check_active(change)
            if change["status"] == "applied":
                if expected_revision != change["base_revision"]:
                    raise HarnessError("stale_revision", "Ожидаемая ревизия не совпадает с основанием предложения.")
                row = self._db.execute("SELECT snapshot FROM revisions WHERE revision=?", (change["applied_revision"],)).fetchone()
                return json.loads(row[0])
            before = self._snapshot()
            self._check_revision(expected_revision, before)
            if change.get("supersedes") is not None:
                self._check_supersedes_target(change["supersedes"])
            if expected_revision != change["base_revision"]:
                raise HarnessError("stale_revision", "Ожидаемая ревизия не совпадает с основанием предложения.")
            if change["review_required"] and change["status"] != "approved":
                raise HarnessError("review_required", "Для этого изменения требуется решение пользователя.", {
                    "change_id": change_id, "reasons": change["review_reasons"],
                })
            after = _transform(before, change["operations"])
            after["revision"] = before["revision"] + 1
            self._save(after)
            change.update(status="applied", applied_revision=after["revision"], applied_at=_now())
            self._put_change(change)
            self._event("apply", change["actor"], after["revision"], {"change_id": change_id})
            if change.get("supersedes") is not None:
                self._event("supersede", change["actor"], after["revision"],
                            {"change_id": change["supersedes"], "by": change_id})
            return after

    def record_observation(self, title: str, source: str, conditions: str, result: str,
                           related_ids: list[str], actor: str = "agent") -> dict:
        """Record a submitted statement; never upgrade it to a host capture."""
        return self._record_observation(title, source, conditions, result, related_ids, actor, None)

    def record_captured_observation(self, title: str, source: str, conditions: str, result: str,
                                    related_ids: list[str], capture: dict, actor: str = "host") -> dict:
        """Host-only: caller must actually acquire the supplied capture."""
        if not isinstance(capture, dict) or not capture:
            raise HarnessError("invalid_input", "capture должен быть непустым объектом.")
        return self._record_observation(title, source, conditions, result, related_ids, actor, json.loads(_json(capture)))

    def _record_observation(self, title: str, source: str, conditions: str, result: str,
                            related_ids: list[str], actor: str, capture: dict | None) -> dict:
        for value, name, limit in ((title, "title", 1000), (source, "source", 4096),
                                   (conditions, "conditions", 20000), (result, "result", 100000), (actor, "actor", 200)):
            _text(value, name, limit)
        if not isinstance(related_ids, list) or not related_ids or len(related_ids) > 100:
            raise HarnessError("invalid_input", "related_ids должен содержать от 1 до 100 ID узлов.")
        related_ids = list(dict.fromkeys(_id(ident) for ident in related_ids))
        with self._transaction():
            graph = self._snapshot()
            nodes = {node["id"]: node for node in graph["nodes"]}
            if any(ident not in nodes for ident in related_ids):
                raise HarnessError("not_found", "Предмет наблюдения отсутствует в графе.")
            ident = "evidence_" + uuid4().hex
            data = {"source": source, "conditions": conditions, "result": result,
                    "provenance": "captured" if capture is not None else "reported",
                    "actor": actor, "recorded_at": _now(), "related_ids": related_ids,
                    "recorded_against_revision": graph["revision"]}
            if capture is not None:
                data["capture"] = capture
            evidence = {"id": ident, "kind": "evidence", "title": title, "data": data}
            graph["nodes"].append(evidence)
            for number, related in enumerate(related_ids):
                graph["edges"].append({"id": f"{ident}_rel_{number}", "source": ident,
                                       "target": related, "kind": "affects", "data": {"origin": "observation"}})
            graph["nodes"].sort(key=lambda node: node["id"])
            graph["edges"].sort(key=lambda edge: edge["id"])
            _validate_graph(graph)
            graph["revision"] += 1
            self._save(graph)
            self._event("observation", actor, graph["revision"], {"evidence_id": ident, "provenance": data["provenance"]})
            return {"revision": graph["revision"], "evidence": evidence}

    def inspect(self, ref: str) -> dict:
        _id(ref)
        with self._lock:
            graph = self._snapshot()
            for collection in ("nodes", "edges"):
                for item in graph[collection]:
                    if item["id"] == ref:
                        return deepcopy(item)
            return self.get_change(ref)

    def history(self) -> list[dict]:
        with self._lock:
            self._snapshot()
            rows = self._db.execute("SELECT id,at,kind,actor,revision,details FROM events ORDER BY id").fetchall()
            return [{"id": row[0], "at": row[1], "kind": row[2], "actor": row[3],
                     "revision": row[4], "details": json.loads(row[5])} for row in rows]
