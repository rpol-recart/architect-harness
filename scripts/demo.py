"""Run a real local HTTP observation and persist it across service instances.

This is a transport/lifecycle demonstration, not validation of a production API.
"""
import argparse
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from architect_harness.context import get_context
from architect_harness.core import GraphService
from architect_harness.observations import probe_http


class LocalPeer(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"local-demonstration","contract_verified":false}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def run() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        database = str(Path(directory) / "demo.sqlite")
        service = GraphService(database)
        service.initialize("local-demo", "Локальный сквозной сценарий")
        payload = json.loads((Path(__file__).resolve().parents[1] / "examples/bootstrap.json").read_text())
        proposal = service.propose_change(**payload, actor="demo-host")
        service.approve_change(proposal["id"], 0, "demo-operator")
        service.apply_change(proposal["id"], 0)
        peer = ThreadingHTTPServer(("127.0.0.1", 0), LocalPeer)
        thread = threading.Thread(target=peer.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{peer.server_port}"
        try:
            observation = probe_http(service, url + "/health", [url], ["host"])
        finally:
            peer.shutdown()
            peer.server_close()
            thread.join(timeout=2)
        resumed_service = GraphService(database)
        restored = resumed_service.snapshot()
        resumed_service.close()
        evidence = [n for n in restored["nodes"] if n["kind"] == "evidence"]
        assert observation["capture"]["status"] == 200
        assert len(evidence) == 1 and evidence[0]["data"]["provenance"] == "captured"
        context = get_context(restored, "system", profiles=["core", "python"])
        service.close()
        return {"scenario": "local-http-observation-and-session-resume", "success": True,
                "revision": restored["revision"], "evidence_count": len(evidence),
                "http": observation["capture"], "context_keys": sorted(context),
                "limits": ["Только локальный демонстрационный HTTP-сервис.",
                           "Реальный production-контракт и пользовательская работа с LLM не проверялись."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
