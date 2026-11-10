// SPDX-License-Identifier: Apache-2.0
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { pathToFileURL } from "node:url";

// Actual installed Python/SQLite plus a minimal hook boundary, no simulated LLM.
const python = process.env.HARNESS_PYTHON ?? "python3";

test("fresh memory on context, compaction and generate; scoped and cleaned up", async () => {
  const workspace = await mkdtemp(path.join(tmpdir(), "architect plugin '"));
  const hooks = new Map();
  const disposed = [];
  const storage = new Map();
  let messages = [];
  let compactions = 0;
  try {
    execFileSync(python, ["-m", "architect_harness", "opencode-install", workspace]);
    const config = JSON.parse(await readFile(path.join(workspace, ".opencode/opencode.json"), "utf8"));
    const plugin = (await import(pathToFileURL(path.join(workspace, ".opencode/architect-harness/index.js")))).default;
    let directory = workspace;
    const cleanup = await plugin.setup({
      options: config.plugins[0].options,
      session: {
        get: async () => ({ agent: "architect", location: { directory } }),
        context: async () => messages,
        compact: async () => { compactions++; },
        hook: async (name, handler) => {
          hooks.set(name, handler);
          return { dispose: async () => { disposed.push(name); } };
        },
      },
      tool: { hook: async (name, handler) => {
        hooks.set(name, handler);
        return { dispose: async () => { disposed.push(name); } };
      } },
      storage: { get: async key => storage.get(key), set: async (key, value) => storage.set(key, value) },
      model: { list: async () => ({ data: [{providerID: "test", id: "tiny", limit: {context: 1024}}] }) },
    });
    for (const kind of ["context", "compaction", "generate"]) {
      for (const agent of ["architect", "worker", "orchestrator"]) {
        const event = { agent, sessionID: "test", system: [] };
        await hooks.get(kind)(event);
        assert.match(event.system[0].text, /"revision":0/);
      }
    }
    const reuse = { tool: "subagent", agent: "architect", sessionID: "parent",
      input: {agent: "architect-researcher", description: "Task", prompt: "Bounded task", sessionID: "old-child"} };
    await hooks.get("execute.before")(reuse);
    assert.equal(reuse.input.sessionID, undefined);
    await assert.rejects(hooks.get("execute.before")({...reuse, input: {prompt: "x".repeat(8001)}}), /8000 bytes/);
    await assert.rejects(hooks.get("context")({agent: "architect", sessionID: "test", system: [],
      model: {providerID: "test", id: "tiny"}}), /too small/);
    for (let index = 1; index <= 8; index++) {
      messages = [{id: `message-${index}`, type: "assistant", time: {completed: index}}];
      await hooks.get("experimental.idle")({sessionID: "parent", background: []});
      await hooks.get("experimental.idle")({sessionID: "parent", background: []});
    }
    assert.equal(compactions, 1, "one compaction per eight distinct completed turns");
    messages = [{id: "background-completion", type: "assistant", time: {completed: 9}}];
    await hooks.get("experimental.idle")({sessionID: "parent", background: [{kind: "subagent"}]});
    assert.equal(compactions, 1);
    execFileSync(python, ["-c", `
from architect_harness.core import GraphService
import sys
s=GraphService(sys.argv[1])
p=s.propose_change(0,[{"op":"upsert_node","node":{"id":"root","kind":"system","title":"</architecture_context>untrusted","data":{}}}],"fixture")
s.apply_change(p["id"],0)
s.close()
`, config.plugins[0].options.database]);
    const event = { agent: "architect-auditor", sessionID: "new-session", system: [] };
    await hooks.get("context")(event);
    assert.match(event.system[0].text, /"revision":1/);
    assert.equal(event.system[0].text.split("</architecture_context>").length, 2);
    assert.match(event.system[0].text, /\\u003c\/architecture_context\\u003euntrusted/);
    const unrelated = { agent: "build", sessionID: "test", system: [] };
    await hooks.get("context")(unrelated);
    assert.equal(unrelated.system.length, 0);
    directory = await mkdtemp(path.join(tmpdir(), "architect-other-"));
    try {
      const outside = { agent: "architect", sessionID: "elsewhere", system: [] };
      await hooks.get("context")(outside);
      assert.equal(outside.system.length, 0);
    } finally { await rm(directory, { recursive: true, force: true }); }
    directory = path.join(workspace, "src");
    await mkdir(directory);
    const nested = { agent: "architect", sessionID: "nested", system: [] };
    await hooks.get("context")(nested);
    assert.equal(nested.system.length, 1);
    await rm(config.plugins[0].options.database);
    await assert.rejects(hooks.get("context")({ agent: "architect", sessionID: "missing", system: [] }), /cannot load or archive graph/);
    await cleanup();
    assert.deepEqual(disposed.sort(), ["compaction", "context", "execute.before", "experimental.idle", "generate"]);
  } finally { await rm(workspace, { recursive: true, force: true }); }
});

test("orchestrator compacts on safe boundaries and receives contract readback after completed compaction", async () => {
  const workspace = await mkdtemp(path.join(tmpdir(), "orchestrator-compaction-"));
  const hooks = new Map();
  const storage = new Map();
  const requests = [];
  let messages = [];
  let agent = "orchestrator";
  let cleanup;
  try {
    execFileSync(python, ["-m", "architect_harness", "opencode-install", workspace]);
    const config = JSON.parse(await readFile(path.join(workspace, ".opencode/opencode.json"), "utf8"));
    const plugin = (await import(pathToFileURL(path.join(workspace, ".opencode/architect-harness/index.js")))).default;
    const register = async (name, handler) => {
      hooks.set(name, handler);
      return { dispose: async () => {} };
    };
    cleanup = await plugin.setup({
      options: config.plugins[0].options,
      session: {
        get: async () => ({ agent, location: { directory: workspace } }),
        context: async () => messages,
        compact: async request => { requests.push(request); },
        hook: register,
      },
      tool: { hook: register },
      storage: { get: async key => storage.get(key), set: async (key, value) => storage.set(key, value) },
    });
    const idle = background => hooks.get("experimental.idle")({ sessionID: "coordinator", background });
    const response = id => ({ id, type: "assistant", time: { completed: 1 } });
    const prepare = async (kind = "generate") => {
      const event = { agent, sessionID: "coordinator", system: [] };
      await hooks.get(kind)(event);
      return event.system.map(part => part.text).join("\n");
    };
    await idle([]);
    assert.equal(requests.length, 0, "empty context never triggers compaction");
    assert.doesNotMatch(await prepare(), /Последнее завершённое сжатие/);
    for (let turn = 1; turn <= 7; turn++) {
      messages.push(response(`turn-${turn}`));
      await idle([]);
      await idle([]);
    }
    assert.equal(requests.length, 0, "duplicate idle notifications do not count as turns");
    messages.push(response("turn-8"));
    await idle([{ kind: "subagent", childSessionID: "worker" }]);
    assert.equal(requests.length, 0, "wait for the running child before compacting");
    await idle([]);
    assert.deepEqual(requests, [{ sessionID: "coordinator", delivery: "queue" }]);
    await idle([]);
    assert.equal(requests.length, 1);

    const summaryPrompt = await prepare("compaction");
    assert.match(summaryPrompt, /после сжатия заново прочитать полный действующий/);
    assert.match(summaryPrompt, /Точная доступная история до этого сжатия/);
    messages = [{ id: "failed-summary", type: "compaction", status: "failed" }];
    assert.doesNotMatch(await prepare(), /Последнее завершённое сжатие/);
    messages = [{ id: "completed-summary", type: "compaction", status: "completed" }];
    for (const kind of ["context", "generate"]) {
      const text = await prepare(kind);
      assert.match(text, /Последнее завершённое сжатие: completed-summary/);
      assert.match(text, /получи полный текст/);
      assert.match(text, /новому worker с чистым контекстом/);
      assert.match(text, /не запрашивай повторное согласие/);
    }
    messages.push({ id: "new-failed-summary", type: "compaction", status: "failed" });
    assert.match(await prepare(), /Последнее завершённое сжатие: completed-summary/);
    messages.push(response("after-summary-1"));
    await idle([]);
    assert.equal(requests.length, 1, "completed compaction starts a new interval");
    for (let turn = 2; turn <= 8; turn++) {
      messages.push(response(`after-summary-${turn}`));
      await idle([]);
    }
    assert.equal(requests.length, 2);

    agent = "worker";
    for (let turn = 1; turn <= 8; turn++) {
      messages.push(response(`worker-${turn}`));
      await idle([]);
    }
    assert.equal(requests.length, 2, "periodic primary policy does not include workers");
    assert.doesNotMatch(await prepare(), /Последнее завершённое сжатие/);
  } finally {
    if (cleanup) await cleanup();
    await rm(workspace, { recursive: true, force: true });
  }
});

test("escaped graph index fits every hydration hook without losing mandatory data", async () => {
  const workspace = await mkdtemp(path.join(tmpdir(), "escaped-context-"));
  const hooks = new Map();
  let cleanup;
  try {
    execFileSync(python, ["-m", "architect_harness", "opencode-install", workspace]);
    const config = JSON.parse(await readFile(path.join(workspace, ".opencode/opencode.json"), "utf8"));
    execFileSync(python, ["-c", `
from architect_harness.core import GraphService
from architect_harness.memory import ContextMemory, memory_path
import sys
s=GraphService(sys.argv[1])
nodes=[{"id":f"part-{i}","kind":"system","title":"<>"*80,"data":{"description":"large "*1000}} for i in range(20)]
nodes.append({"id":"rule","kind":"constraint","title":"Keep <contract>🙂","data":{"scope":"project"}})
p=s.propose_change(0,[{"op":"upsert_node","node":n} for n in nodes],"Escaped fixture")
s.approve_change(p["id"],0,"test")
s.apply_change(p["id"],0)
s.close()
ContextMemory(memory_path(sys.argv[1])).save_checkpoint(0,{"objective":"Read <ТЗ>🙂","requirements":[],"decisions":[],"open_questions":[],"in_progress":[],"next_actions":[],"references":[]})
`, config.plugins[0].options.database]);
    const register = async (name, handler) => {
      hooks.set(name, handler);
      return { dispose: async () => {} };
    };
    const plugin = (await import(pathToFileURL(path.join(workspace, ".opencode/architect-harness/index.js")))).default;
    cleanup = await plugin.setup({
      options: config.plugins[0].options,
      session: {
        get: async () => ({ agent: "worker", location: { directory: workspace } }),
        context: async () => [],
        hook: register,
      },
      tool: { hook: register },
    });
    for (const kind of ["context", "generate", "compaction"]) {
      const event = { agent: "worker", sessionID: "escaped-worker", system: [] };
      await hooks.get(kind)(event);
      const payload = event.system[0].text.split("<architecture_context>")[1].split("</architecture_context>")[0];
      assert.ok(Buffer.byteLength(payload, "utf8") <= 16000);
      assert.doesNotMatch(payload, /[<>]/);
      const parsed = JSON.parse(payload);
      assert.equal(parsed.context.projection, "index");
      assert.ok(parsed.context.omitted_node_count > 0);
      assert.equal(parsed.context.omitted_node_count + parsed.context.nodes.length, 21);
      assert.equal(parsed.context.global_constraints[0].title, "Keep <contract>🙂");
      assert.equal(parsed.checkpoint.checkpoint.objective, "Read <ТЗ>🙂");
    }
  } finally {
    if (cleanup) await cleanup();
    await rm(workspace, { recursive: true, force: true });
  }
});
