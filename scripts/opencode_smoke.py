#!/usr/bin/env python3
"""Проверка загрузки харнесса настоящим OpenCode 2.2.5 без обращения к LLM.

Запуск: python scripts/opencode_smoke.py [--output /path/evidence.json]
Нужны установленный architect-harness с MCP-зависимостью и бинарник opencode.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


EXPECTED_VERSION = "opencode v2.2.5"
EXPECTED_AGENTS = {"architect", "architect-researcher", "architect-auditor"}
EXPECTED_COMMANDS = {"arch-start", "arch-focus", "arch-review", "arch-resume"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def isolated_environment(root: Path) -> dict[str, str]:
    """Не наследует токены, конфигурацию провайдеров и адрес фонового сервиса."""
    env = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "OPENCODE_TEST_HOME": str(root / "home"),
        "OPENCODE_CONFIG_DIR": str(root / "config" / "opencode"),
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE": "true",
        "OPENCODE_DISABLE_FILEWATCHER": "true",
        "OPENCODE_DISABLE_FFF": "true",
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            {"update": "disable", "share": "disabled", "warming": False}
        ),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    for variable, directory in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        target = root / directory
        target.mkdir(mode=0o700)
        env[variable] = str(target)
    (root / "home").mkdir(mode=0o700)
    return env


def stop_server(process: subprocess.Popen) -> None:
    """Закрывает созданную группу, включая дочерний stdio MCP-процесс."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    # Родитель мог завершиться раньше дочернего процесса.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def install_probe(workspace: Path, result: Path) -> None:
    """Одноразовый plugin проверяет callbacks с настоящим ctx, без модели."""
    config_path = workspace / ".opencode" / "opencode.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    production = next(item for item in config["plugins"] if isinstance(item, dict)
                      and item["package"].endswith("/architect-harness"))
    config["agents"]["orchestrator"] = {
        "mode": "primary", "system": "Isolated smoke fixture; no model calls.",
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
    }
    probe = workspace / ".opencode" / "smoke-probe"
    probe.mkdir()
    settings = json.dumps({"source": Path(production["package"]).joinpath("index.js").as_uri(),
                           "options": production["options"], "result": str(result)})
    source = r'''
import { writeFile } from "node:fs/promises";
const settings = __SETTINGS__;
export default {
  id: "architect-harness-smoke-probe",
  async setup(ctx) {
    const { default: harness } = await import(settings.source);
    const callbacks = new Map();
    const toolCallbacks = new Map();
    const expectedTools = ["apply_change", "get_checkpoint", "get_context", "get_methodology", "inspect",
      "list_details", "propose_change", "read_detail", "record_observation", "save_checkpoint", "search_detail"];
    let compactCalls = 0;
    const cleanup = await harness.setup({
      ...ctx, options: settings.options,
      session: { ...ctx.session, hook: async (name, callback) => {
        callbacks.set(name, callback);
        return ctx.session.hook(name, callback);
      }, compact: async () => {
        compactCalls++;
        throw new Error("Smoke must never start a model-backed compaction");
      } },
      tool: { ...ctx.tool, hook: async (name, callback) => {
        toolCallbacks.set(name, callback);
        return ctx.tool.hook(name, callback);
      } },
    });
    let task;
    const timer = setTimeout(() => {
      task = (async () => {
        let sessionID;
        let orchestratorID;
        const report = { status: "failed", callbacks: [] };
        try {
          const created = await ctx.session.create({ agent: "architect", title: "Harness smoke probe" });
          sessionID = created.id;
          const fetched = await ctx.session.get({ sessionID });
          if (fetched.location.directory !== ctx.location.directory) throw new Error("Unexpected session location");
          report.session_response = "location.directory (Promise plugin API unwraps REST data envelope)";
          for (let attempt = 0; attempt < 50; attempt++) {
            const tools = await ctx.tool.list();
            report.architect_tools = tools.filter(tool => tool.id?.includes("architect") || tool.options?.namespace === "architect")
              .map(tool => ({ id: tool.id, name: tool.name }));
            if (report.architect_tools.length === expectedTools.length) break;
            await new Promise(resolve => setTimeout(resolve, 100));
          }
          if (report.architect_tools.length !== expectedTools.length || expectedTools.some(name =>
            !report.architect_tools.some(tool => tool.id === `architect_${name}`))) {
            throw new Error("Expected eleven named architect tools");
          }
          for (const name of ["context", "compaction", "generate"]) {
            const event = { agent: "architect", sessionID, system: [], tools: {}, messages: [],
              model: { providerID: "smoke-unconfigured", id: "smoke-no-llm" } };
            await callbacks.get(name)(event);
            const text = event.system.map(part => part.text ?? "").join("\n");
            const match = text.match(/<architecture_context>(.*?)<\/architecture_context>/s);
            if (!match) throw new Error(`${name}: missing architecture context`);
            const payload = JSON.parse(match[1]);
            if (payload.context.revision !== 0) throw new Error(`${name}: wrong revision`);
            if (!payload.trust) throw new Error(`${name}: missing trust boundary`);
            report.callbacks.push({ name, revision: payload.context.revision });
          }
          const before = toolCallbacks.get("execute.before");
          if (!before) throw new Error("Missing fresh-subagent execution guard");
          const toolEvent = { tool: "subagent", agent: "architect", sessionID,
            input: { agent: "architect-researcher", description: "Smoke research", prompt: "Inspect a bounded issue", sessionID } };
          await before(toolEvent);
          if (toolEvent.input.sessionID !== undefined) throw new Error("Subagent session reuse was not prevented");
          report.fresh_subagent_guard = "sessionID removed before execution";
          const idle = callbacks.get("experimental.idle");
          if (!idle) throw new Error("Missing periodic compaction idle hook");
          await idle({ sessionID, background: [{ kind: "subagent", sessionID, childSessionID: sessionID,
            agent: "architect-researcher", description: "Smoke background guard" }] });
          await idle({ sessionID, background: [] });
          if (compactCalls !== 0) throw new Error("Background or empty sessions must not compact");
          report.idle_guards = "No compaction with pending background work or empty context";
          const coordinator = await ctx.session.create({ agent: "orchestrator", title: "Orchestrator smoke probe" });
          orchestratorID = coordinator.id;
          for (const name of ["context", "compaction", "generate"]) {
            const event = { agent: "orchestrator", sessionID: orchestratorID, system: [], tools: {}, messages: [] };
            await callbacks.get(name)(event);
            const text = event.system.map(part => part.text ?? "").join("\n");
            if (!text.includes("<architecture_context>")) throw new Error(`${name}: orchestrator was excluded`);
            if (name === "compaction" && !text.includes("заново прочитать полный действующий")) {
              throw new Error("Orchestrator compaction lost the contract readback obligation");
            }
          }
          await idle({ sessionID: orchestratorID, background: [] });
          if (compactCalls !== 0) throw new Error("Empty orchestrator must not compact");
          report.orchestrator = "Real session: context, compaction archive, generate and idle guards verified; no LLM";
          report.status = "passed";
        } catch (error) {
          report.error = String(error);
        } finally {
          try {
            for (const id of [sessionID, orchestratorID]) {
              if (id) await ctx.session.remove({ sessionID: id });
            }
          } catch (error) { report.status = "failed"; report.cleanup_error = String(error); }
          await writeFile(settings.result, JSON.stringify(report));
          await cleanup?.();
        }
      })();
      task.catch(async error => {
        await writeFile(settings.result, JSON.stringify({ status: "failed", error: String(error) }));
      });
    }, 0);
    return async () => { clearTimeout(timer); await task; await cleanup?.(); };
  },
};
'''.replace("__SETTINGS__", settings)
    (probe / "index.js").write_text(source, encoding="utf-8")
    (probe / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    config["plugins"].append(str(probe))
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_smoke(binary: str, timeout: float, evidence: dict) -> None:
    require(sys.platform == "linux", "Изоляция этой проверки рассчитана на Linux.")
    # CLI 2.2.5 не предоставляет флага отключения системной конфигурации.
    require(not Path("/etc/opencode").exists(), "Проверка остановлена: обнаружена системная /etc/opencode.")
    with tempfile.TemporaryDirectory(prefix="architect-opencode-smoke-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        for parent in workspace.parents:
            for name in ("opencode.json", "opencode.jsonc", ".opencode", ".agents"):
                require(not (parent / name).exists(), f"Внешняя конфигурация мешает изоляции: {parent / name}")
        env = isolated_environment(root)
        password = secrets.token_urlsafe(32)
        env["OPENCODE_SERVER_PASSWORD"] = password
        version = subprocess.run(
            [binary, "--version"], cwd=workspace, env=env,
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout.strip()
        evidence["opencode_version"] = version
        require(version == EXPECTED_VERSION, f"Нужен {EXPECTED_VERSION}, получен {version!r}.")

        from architect_harness.opencode import install

        install(workspace, project_id="opencode-smoke", title="Проверка OpenCode 2.2.5")
        require((workspace / ".architecture" / "architecture.sqlite").is_file(), "Установщик не создал БД.")
        probe_result = root / "probe-result.json"
        install_probe(workspace, probe_result)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        query = urlencode({"location[directory]": str(workspace)})
        opener = build_opener(ProxyHandler({}))
        authorization = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()

        def get(path: str):
            request = Request(f"{base_url}{path}?{query}", headers={"Authorization": authorization})
            with opener.open(request, timeout=5) as response:
                return json.load(response)

        with (root / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                deadline = time.monotonic() + timeout
                last_error = None
                while True:
                    require(process.poll() is None, f"OpenCode завершился с кодом {process.returncode}.")
                    try:
                        agents = get("/api/agent")["data"]
                        break
                    except (URLError, TimeoutError, ConnectionError) as error:
                        last_error = error
                    require(time.monotonic() < deadline, f"OpenCode не запустился: {last_error}")
                    time.sleep(0.2)

                by_id = {agent["id"]: agent for agent in agents}
                require(EXPECTED_AGENTS <= by_id.keys(), "OpenCode не загрузил всех трёх агентов.")
                require(by_id["architect"]["mode"] == "primary", "architect должен быть primary.")
                for name in EXPECTED_AGENTS - {"architect"}:
                    require(by_id[name]["mode"] == "subagent", f"{name} должен быть subagent.")
                evidence["agents"] = {name: by_id[name]["mode"] for name in sorted(EXPECTED_AGENTS)}

                entries = get("/api/config")
                installed = [entry["info"] for entry in entries if entry.get("type") == "document"
                             and entry.get("info", {}).get("default_agent") == "architect"]
                require(bool(installed), "OpenCode не загрузил default_agent=architect.")
                config = installed[-1]
                require(EXPECTED_COMMANDS <= config.get("commands", {}).keys(), "Команды отсутствуют в конфигурации хоста.")
                command_names = {item["name"] for item in get("/api/command")["data"]}
                require(EXPECTED_COMMANDS <= command_names, "Команды отсутствуют в реестре хоста.")
                compaction = config.get("compaction", {})
                require(compaction.get("auto") is True and compaction.get("buffer") == "30%"
                        and compaction.get("keep", {}).get("tokens") == 6000,
                        "Настройки compaction не соответствуют auto/30%/6000.")
                evidence["compaction"] = compaction
                server_config = config.get("mcp", {}).get("servers", {}).get("architect", {})
                require(server_config.get("protocol") == "2026-07-28", "Неверный протокол MCP в конфигурации хоста.")
                evidence["default_agent"] = "architect"
                evidence["commands"] = sorted(EXPECTED_COMMANDS)
                evidence["mcp_protocol_configured"] = server_config["protocol"]

                plugins = get("/api/plugin")["data"]
                plugin = next((item for item in plugins if item.get("id") == "architect-harness"), None)
                require(plugin is not None and plugin["state"]["status"] == "active", f"Плагин не активен: {plugin}")
                evidence["plugin"] = {"id": plugin["id"], "state": plugin["state"]}

                while True:
                    servers = get("/api/mcp")["data"]
                    server = next((item for item in servers if item.get("name") == "architect"), None)
                    if server and server["status"]["status"] == "connected":
                        break
                    require(time.monotonic() < deadline, f"MCP не подключился: {server}")
                    time.sleep(0.2)
                evidence["mcp"] = server
                catalog = get("/api/mcp/resource")["data"]
                resources = [item["uri"] for item in catalog["resources"] if item["server"] == "architect"]
                require(bool(resources), "OpenCode не обнаружил ресурсов архитектурного MCP.")
                evidence["mcp_resources"] = sorted(resources)
                while not probe_result.exists():
                    require(time.monotonic() < deadline, "Проверка hydration callbacks не завершилась.")
                    time.sleep(0.2)
                evidence["hydration_probe"] = json.loads(probe_result.read_text(encoding="utf-8"))
                require(evidence["hydration_probe"]["status"] == "passed", "Ошибка реального plugin API: " + str(evidence["hydration_probe"]))
                evidence["status"] = "passed"
            except Exception:
                log.flush()
                log.seek(0)
                evidence["server_log_tail"] = log.read()[-6000:].replace(password, "[redacted]")
                raise
            finally:
                stop_server(process)
                evidence["server_stopped"] = process.poll() is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", default="opencode", help="Путь к OpenCode 2.2.5")
    parser.add_argument("--timeout", type=float, default=60, help="Время запуска в секундах")
    parser.add_argument("--output", type=Path, help="Дополнительно сохранить JSON-свидетельство")
    args = parser.parse_args()
    evidence = {
        "status": "failed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "environment": "Linux, temporary workspace and XDG directories, private loopback server",
        "limitations": [
            "Область проверки: загрузка конфигурации, регистрация агентов/плагина и discovery MCP реальным хостом.",
            "Hydration callbacks вызываются тестовым plugin с настоящим ctx.session API; автоматический dispatch перед моделью не проверяется.",
            "Модель не вызывается; поведение агента в диалоге этой проверкой не подтверждается.",
            "MCP resources/list проверяется через OpenCode; tools/call и resources/read не проверяются.",
            "Изоляция конфигурации не является сетевой или файловой песочницей OpenCode.",
        ],
    }
    try:
        require(args.timeout > 0, "--timeout должен быть положительным.")
        binary = shutil.which(args.opencode)
        require(binary is not None, "Бинарник opencode не найден.")
        run_smoke(str(Path(binary).resolve()), args.timeout, evidence)
    except Exception as error:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(error).__name__}: {error}"
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
