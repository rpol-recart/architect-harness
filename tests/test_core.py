"""Проверки границ доверия, ревизий и атомарности архитектурного графа."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest

from architect_harness.core import GraphService, HarnessError


def node(ident, kind="component", **data):
    return {"op": "upsert_node", "node": {"id": ident, "kind": kind, "title": ident, "data": data}}


def edge(ident, source, target, kind="depends_on", **data):
    return {"op": "upsert_edge", "edge": {"id": ident, "source": source, "target": target,
                                           "kind": kind, "data": data}}


class GraphServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "architecture.sqlite3"
        self.service = GraphService(self.path)
        self.service.initialize("project", "Проект")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def commit(self, operations):
        revision = self.service.snapshot()["revision"]
        proposal = self.service.propose_change(revision, operations, "Основание изменения")
        if proposal["review_required"]:
            self.service.approve_change(proposal["id"], revision, "Пользователь")
        return self.service.apply_change(proposal["id"], revision)

    def assert_error(self, code, callable_, *args, **kwargs):
        with self.assertRaises(HarnessError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_project_initialization_is_idempotent_and_persists(self):
        self.commit([node("api")])
        graph = self.service.initialize("project", "Название не перезаписывается")
        self.assertEqual(graph["revision"], 1)
        self.assertEqual(graph["title"], "Проект")
        self.assert_error("project_exists", self.service.initialize, "other", "Другой")
        second = GraphService(self.path)
        try:
            self.assertEqual(second.snapshot(), graph)
        finally:
            second.close()

    def test_failed_batch_is_atomic_and_does_not_create_proposal(self):
        before = self.service.snapshot()
        self.assert_error("invalid_graph", self.service.propose_change, 0,
                          [node("api"), edge("broken", "api", "missing")], "Неверная связь")
        self.assertEqual(self.service.snapshot(), before)
        self.assertEqual([event["kind"] for event in self.service.history()], ["initialize"])

    def test_proposal_inputs_and_return_values_cannot_mutate_saved_proposal(self):
        operations = [node("api", description="Исходное описание")]
        proposal = self.service.propose_change(0, operations, "Локальная реализация")
        operations[0]["node"]["data"]["description"] = "Подмена"
        proposal["operations"].clear()
        graph = self.service.apply_change(proposal["id"], 0)
        self.assertEqual(graph["nodes"][0]["data"]["description"], "Исходное описание")

    def test_review_is_required_and_bound_to_exact_proposal(self):
        first = self.service.propose_change(0, [node("provider", "external_system")], "Интеграция")
        second = self.service.propose_change(0, [node("storage", "data")], "Хранение")
        self.assertTrue(first["review_required"])
        self.assert_error("review_required", self.service.apply_change, first["id"], 0)
        self.service.approve_change(first["id"], 0, "Пользователь")
        self.assert_error("review_required", self.service.apply_change, second["id"], 0)
        graph = self.service.apply_change(first["id"], 0)
        self.assertEqual(graph["revision"], 1)

    def test_approved_change_cannot_apply_after_another_graph_mutation(self):
        proposal = self.service.propose_change(0, [node("provider", "external_system")], "Интеграция")
        self.service.approve_change(proposal["id"], 0, "Пользователь")
        self.commit([node("api")])
        self.assert_error("stale_revision", self.service.apply_change, proposal["id"], 0)
        self.assert_error("stale_revision", self.service.approve_change, proposal["id"], 1, "Пользователь")
        self.assertEqual([n["id"] for n in self.service.snapshot()["nodes"]], ["api"])

    def test_safe_apply_retry_returns_historical_snapshot(self):
        proposal = self.service.propose_change(0, [node("api")], "Каркас")
        applied = self.service.apply_change(proposal["id"], 0)
        self.commit([node("worker")])
        self.assertEqual(self.service.apply_change(proposal["id"], 0), applied)
        self.assertEqual(self.service.snapshot()["revision"], 2)
        self.assert_error("stale_revision", self.service.apply_change, proposal["id"], 2)
        self.assertEqual(len([e for e in self.service.history() if e["kind"] == "apply"]), 2)

    def test_concurrent_connections_cannot_overwrite_each_other(self):
        other = GraphService(self.path)
        barrier = threading.Barrier(2)
        try:
            first = self.service.propose_change(0, [node("api")], "Первый исполнитель")
            second = other.propose_change(0, [node("worker")], "Второй исполнитель")

            def apply(service, change_id):
                barrier.wait(timeout=5)
                try:
                    service.apply_change(change_id, 0)
                    return "applied"
                except HarnessError as exc:
                    return exc.code

            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(apply, self.service, first["id"])
                b = pool.submit(apply, other, second["id"])
                self.assertCountEqual([a.result(timeout=10), b.result(timeout=10)], ["applied", "stale_revision"])
            self.assertEqual(self.service.snapshot()["revision"], 1)
            self.assertEqual(len(self.service.snapshot()["nodes"]), 1)
        finally:
            other.close()

    def test_cycle_dangling_endpoint_and_cross_kind_id_collisions_are_rejected(self):
        self.assert_error("invalid_graph", self.service.propose_change, 0,
                          [node("a"), node("b"), edge("ab", "a", "b", "contains"),
                           edge("ba", "b", "a", "contains")], "Цикл")
        self.assert_error("invalid_graph", self.service.propose_change, 0,
                          [node("a"), node("b"), edge("a", "a", "b")], "Повтор ID")
        self.assert_error("invalid_graph", self.service.propose_change, 0,
                          [node("a"), edge("aa", "a", "a", "affects")], "Петля")

    def test_final_state_validation_allows_forward_refs_but_rejects_second_owner(self):
        self.commit([edge("owner", "api", "records", "owns"), node("api"), node("records", "data")])
        self.assert_error("invalid_graph", self.service.propose_change, 1,
                          [node("worker"), edge("owner2", "worker", "records", "owns")], "Два владельца")
        self.assert_error("invalid_graph", self.service.propose_change, 1,
                          [node("api", "actor")], "Владелец стал участником")

    def test_dependency_constraint_is_enforced_on_propose(self):
        self.commit([node("api"), node("db", "data"),
                     node("boundary", "constraint", scope="project",
                          rule={"type": "forbid_dependency", "source": "api", "target": "db"})])
        self.assert_error("constraint_violation", self.service.propose_change, 1,
                          [edge("direct", "api", "db")], "Обход границы")
        self.assert_error("constraint_violation", self.service.propose_change, 1,
                          [edge("direct", "api", "db", "interacts_with")], "Взаимодействие")
        self.assertEqual(self.service.snapshot()["revision"], 1)

    def test_removing_constraint_and_adding_dependency_requires_explicit_review(self):
        self.commit([node("api"), node("db", "data"),
                     node("boundary", "constraint",
                          rule={"type": "forbid_dependency", "source": "api", "target": "db"})])
        change = self.service.propose_change(1, [{"op": "remove_node", "id": "boundary"},
                                                edge("direct", "api", "db")], "Пересмотр ограничения")
        self.assertTrue(change["review_required"])
        self.assertTrue(any("boundary" in reason for reason in change["review_reasons"]))
        self.assert_error("review_required", self.service.apply_change, change["id"], 1)
        self.service.approve_change(change["id"], 1, "Пользователь")
        graph = self.service.apply_change(change["id"], 1)
        self.assertEqual(graph["revision"], 2)
        self.assertIn("direct", [e["id"] for e in graph["edges"]])

    def test_deletion_and_downgrade_inspect_old_state_for_review(self):
        self.commit([node("api"), node("provider", "external_system"),
                     edge("call", "api", "provider"), node("decision", "decision", status="accepted")])
        change = self.service.propose_change(1, [node("decision", "decision", status="proposed")], "Пересмотр")
        self.assertTrue(change["review_required"])
        deleted = self.service.propose_change(1, [{"op": "remove_edge", "id": "call"},
                                                 {"op": "remove_node", "id": "provider"}], "Удаление интеграции")
        self.assertTrue(deleted["review_required"])
        self.assertTrue(any("provider" in reason for reason in deleted["review_reasons"]))
        self.assertTrue(any("call" in reason for reason in deleted["review_reasons"]))

    def test_reported_evidence_has_provenance_and_does_not_answer_question(self):
        self.commit([node("api"), node("q", "question"), node("claim", "claim")])
        observation = self.service.record_observation("Документация", "https://example.invalid/spec", "Источник прочитан",
                                                      "Сообщено о поддержке повторов", ["api", "q"], actor="agent")
        evidence = observation["evidence"]
        self.assertEqual(observation["revision"], 2)
        self.assertEqual(evidence["data"]["provenance"], "reported")
        self.assertEqual(evidence["data"]["related_ids"], ["api", "q"])
        self.assertNotIn("capture", evidence["data"])
        self.assertEqual(self.service.inspect("q")["data"]["status"], "open")
        self.assertEqual(self.service.inspect("claim")["data"]["status"], "assumed")

    def test_evidence_cannot_be_forged_overwritten_deleted_or_reattributed(self):
        self.commit([node("api"), node("worker")])
        self.assert_error("immutable_evidence", self.service.propose_change, 1,
                          [node("fake", "evidence", provenance="captured")], "Подмена происхождения")
        evidence = self.service.record_observation("Наблюдение", "ручной отчёт", "Условия", "Результат", ["api"])["evidence"]
        for operation in (node(evidence["id"]), {"op": "remove_node", "id": evidence["id"]}):
            self.assert_error("immutable_evidence", self.service.propose_change, 2, [operation], "Изменение свидетельства")
        link = next(e for e in self.service.snapshot()["edges"] if e["source"] == evidence["id"])
        self.assert_error("immutable_evidence", self.service.propose_change, 2,
                          [{"op": "remove_edge", "id": link["id"]}], "Удаление происхождения")
        updated = deepcopy(link)
        updated["target"] = "worker"
        self.assert_error("immutable_evidence", self.service.propose_change, 2,
                          [{"op": "upsert_edge", "edge": updated}], "Перепривязка")

    def test_host_capture_is_distinct_and_invalid_observation_does_not_write(self):
        self.commit([node("api")])
        self.assert_error("not_found", self.service.record_observation, "Наблюдение", "источник", "условия",
                          "результат", ["missing"])
        self.assertEqual(self.service.snapshot()["revision"], 1)
        observation = self.service.record_captured_observation("Ответ", "http://127.0.0.1", "Локальный пример",
                                                              "HTTP 200", ["api"], {"status": 200})
        self.assertEqual(observation["evidence"]["data"]["provenance"], "captured")
        self.assertEqual(observation["evidence"]["data"]["actor"], "host")

    def test_strict_input_schema_rejects_unknown_fields_and_non_json_values(self):
        self.assert_error("invalid_input", self.service.propose_change, True, [node("a")], "Boolean не ревизия")
        bad = node("a")
        bad["node"]["approved"] = True
        self.assert_error("invalid_input", self.service.propose_change, 0, [bad], "Неизвестное поле")
        bad = node("a")
        bad["node"]["kind"] = []
        self.assert_error("invalid_input", self.service.propose_change, 0, [bad], "Не строка")
        for value in (float("nan"), float("inf"), object(), {1: "non-string key"}):
            self.assert_error("invalid_input", self.service.propose_change, 0,
                              [node("a", value=value)], "Не JSON")
        self.assert_error("invalid_input", self.service.propose_change, 0,
                          [node("d", "decision", status="verified")], "Несуществующий статус")
        self.assert_error("invalid_input", self.service.propose_change, 0,
                          [node("c", "constraint", rule={"type": "eval", "source": "a", "target": "b"})], "Правило")

    def test_proposal_namespace_cannot_be_shadowed_by_graph_entities(self):
        proposal = self.service.propose_change(0, [node("a")], "Компонент")
        self.assert_error("invalid_input", self.service.propose_change, 0,
                          [node(proposal["id"])], "Подмена ID предложения")
        self.assert_error("invalid_input", self.service.propose_change, 0,
                          [node("a"), node("b"), edge(proposal["id"], "a", "b", "contains")], "Подмена связью")
        self.assertEqual(self.service.inspect(proposal["id"])["status"], "proposed")

    def test_ids_cannot_change_between_node_and_edge_categories_in_one_batch(self):
        self.commit([node("a"), node("b"), node("c"), edge("ab", "a", "b", "contains")])
        self.assert_error("invalid_graph", self.service.propose_change, 1,
                          [{"op": "remove_node", "id": "c"}, edge("c", "a", "b", "contains")], "Смена категории")
        self.assert_error("invalid_graph", self.service.propose_change, 1,
                          [{"op": "remove_edge", "id": "ab"}, node("ab")], "Смена категории")

    def test_accepted_decision_scope_changes_require_review_but_evidence_does_not(self):
        self.commit([node("d", "decision", status="accepted"), node("s1", "scenario"), node("s2", "scenario"),
                     edge("scope", "d", "s1", "addresses")])
        proposal = self.service.propose_change(1, [edge("scope", "d", "s2", "addresses")], "Новая область решения")
        self.assertTrue(proposal["review_required"])
        self.assert_error("review_required", self.service.apply_change, proposal["id"], 1)
        evidence = self.service.record_observation("Сведения", "источник", "условия", "результат", ["d"])["evidence"]
        support = self.service.propose_change(2, [edge("support", evidence["id"], "d", "supports")], "Основание")
        self.assertFalse(support["review_required"])

    def test_invalid_unicode_and_excessive_nesting_return_domain_errors(self):
        nested = "value"
        for _ in range(100):
            nested = {"nested": nested}
        for value in ("\ud800", nested):
            self.assert_error("invalid_input", self.service.propose_change, 0,
                              [node("a", description=value)], "Некорректные данные")
        self.assertEqual(self.service.snapshot()["revision"], 0)


if __name__ == "__main__":
    unittest.main()
