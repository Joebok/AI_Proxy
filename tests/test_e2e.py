from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

from aiohttp import ClientSession, web

from file_proxy.engine import Proxy
from file_proxy.http_proxy import COMFYUI_PROFILE, OLLAMA_PROFILE, HttpProxyConfig, HttpRuntimeState, run_http_proxies
from file_proxy.runtime import ManagedComfyUIConfig, RuntimeConfig
from file_proxy.supervisor import RuntimeSupervisor
from file_proxy.tui import ProxyDashboard

from conftest import (
    REPOSITORY_ROOT,
    assert_process_dead,
    publish_job,
    seed_running,
    unused_port,
    wait_for,
    write_inventory,
    write_registry,
    write_worker,
)


async def start_server(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


def run_cli(root: Path, registry: Path) -> dict[str, object]:
    completed = __import__("subprocess").run(
        [sys.executable, "-m", "file_proxy.cli", "--root", str(root), "--registry-dir", str(registry), "once"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return json.loads(completed.stdout)


def test_file_jobs_wait_for_complete_inputs_and_publish_terminal_results(tmp_path: Path) -> None:
    async def scenario() -> None:
        counter = tmp_path / "worker-runs.txt"
        worker = write_worker(tmp_path, counter=counter)
        registry_dir = tmp_path / "registry"
        registry = write_registry(registry_dir, worker, counter=counter)
        queue = tmp_path / "queue"
        proxy = Proxy(queue, registry, sync_grace_seconds=10)

        staging_parent = proxy.ask_root / "demo"
        staging_parent.mkdir(parents=True)
        staging = staging_parent / ".complete.staging"
        staging.mkdir()
        inventory = write_inventory(staging / "input.txt", b"synced input")
        manifest = {"protocol_version": 1, "job_id": "complete", "subscriber_id": "demo", "worker": "success", "created_at": "2026-01-01T00:00:00Z", "files": [inventory]}
        (staging / "job.json").write_text(json.dumps(manifest), encoding="utf-8")
        (staging / "input.txt").unlink()
        ready = staging_parent / "complete"
        staging.replace(ready)
        assert run_cli(queue, registry_dir) == {"processed": None}
        assert ready.is_dir() and not (proxy.answer_root / "demo" / "complete").exists()
        (ready / "input.txt").write_bytes(b"synced input")
        run_cli(queue, registry_dir)
        success = proxy.answer_root / "demo" / "complete"
        result = json.loads((success / "proxy_result.json").read_text(encoding="utf-8"))
        assert result["status"] == "SUCCEEDED"
        assert (success / "output.txt").read_text(encoding="utf-8") == "processed:complete"
        assert {item["path"] for item in result["output_files"]} == {"input.txt", "job.json", "output.txt"}

        publish_job(proxy.root, "failure", worker="failure", created_at="2026-01-02T00:00:00Z")
        processed = run_cli(queue, registry_dir)
        failed = proxy.answer_root / "demo" / "failure"
        assert processed.get("processed") and "failure" in str(processed["processed"]), processed
        assert json.loads((failed / "proxy_result.json").read_text())["status"] == "FAILED"
        publish_job(proxy.root, "unsafe", worker="unsafe", created_at="2026-01-03T00:00:00Z")
        run_cli(queue, registry_dir)
        invalid = proxy.answer_root / "demo" / "unsafe"
        assert json.loads((invalid / "proxy_result.json").read_text())["status"] == "FAILED"
        assert "unsafe referenced path" in json.loads((invalid / "proxy_result.json").read_text())["error_message"]
        assert counter.read_text(encoding="utf-8").splitlines() == ["complete", "failure", "unsafe"]

    asyncio.run(scenario())


def test_dashboard_waits_for_workers_and_recovers_without_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime_dir = tmp_path / "runtime"
        queue = tmp_path / "queue"
        counter, gate = tmp_path / "runs.txt", tmp_path / "release-worker"
        worker = write_worker(tmp_path, counter=counter, gate=gate)
        registry_dir = tmp_path / "registry"
        registry = write_registry(registry_dir, worker, counter=counter, gate=gate)
        proxy = Proxy(queue, registry, local_runtime_dir=runtime_dir)
        proxy.ensure_layout()
        seed_running(proxy.root, "interrupted")
        publish_job(proxy.root, "held", created_at="2026-01-02T00:00:00Z")

        async def ps(_request: web.Request) -> web.Response:
            return web.json_response({"models": []})

        fake = web.Application()
        fake.router.add_get("/api/ps", ps)
        fake_runner, upstream_port = await start_server(fake)
        listen_port = unused_port()
        config = HttpProxyConfig(listen_port=listen_port, upstream=f"http://127.0.0.1:{upstream_port}", profile=OLLAMA_PROFILE, continuation_grace_seconds=0)
        supervisor = RuntimeSupervisor(proxy, [config], RuntimeConfig(runtime_dir=str(runtime_dir)), poll_seconds=0.01)
        app = ProxyDashboard(supervisor)
        try:
            async with app.run_test(size=(110, 30)) as pilot:
                await wait_for(lambda: supervisor.state in {"ready", "error"} and (proxy.answer_root / "demo" / "interrupted").is_dir(), description="runtime startup and interrupted recovery")
                assert supervisor.state == "ready", supervisor.error
                recovered = json.loads((proxy.answer_root / "demo" / "interrupted" / "proxy_result.json").read_text())
                assert recovered["status"] == "FAILED" and recovered["error_type"] == "PROXY_INTERRUPTED"
                assert not counter.exists() or counter.read_text(encoding="utf-8").splitlines() == []
                assert (runtime_dir / "proxy.lock").is_file()
                await wait_for(lambda: (proxy.running_root / "demo" / "held").is_dir(), description="worker subprocess start")
                await wait_for(lambda: counter.is_file() and counter.read_text(encoding="utf-8").splitlines() == ["held"], description="worker records its execution")
                await pilot.press("s")
                await wait_for(lambda: supervisor.state == "stopping", description="dashboard stop request")
                await asyncio.sleep(0.05)
                assert (proxy.running_root / "demo" / "held").is_dir()
                assert not (proxy.answer_root / "demo" / "held").exists()
                gate.write_text("continue", encoding="utf-8")
                await wait_for(lambda: supervisor.state == "stopped", description="worker-safe runtime stop")
                held_answer = proxy.answer_root / "demo" / "held"
                assert (held_answer / "output.txt").is_file() and not (runtime_dir / "proxy.lock").exists()
                rebound_port = supervisor.configs[0].listen_port
                await pilot.press("r")
                await wait_for(lambda: supervisor.state == "ready", description="runtime restart and listener bind")
                assert supervisor._http_state is not None
                assert supervisor._http_state.listener_states["ollama"] == "listening"
                publish_job(proxy.root, "after-restart", created_at="2026-01-03T00:00:00Z")
                await wait_for(lambda: (proxy.answer_root / "demo" / "after-restart").is_dir(), description="post-restart job")
                await wait_for(lambda: counter.is_file() and len(counter.read_text(encoding="utf-8").splitlines()) == 2, description="worker executes after restart")
                await pilot.press("q")
                await wait_for(lambda: supervisor.state == "stopped", description="dashboard exit")
                assert not (runtime_dir / "proxy.lock").exists()
                assert rebound_port == listen_port
        finally:
            await supervisor.stop()
            await fake_runner.cleanup()

    asyncio.run(scenario())


def test_file_and_http_work_share_a_non_preemptive_streaming_scheduler(tmp_path: Path) -> None:
    async def scenario() -> None:
        counter, gate = tmp_path / "runs.txt", tmp_path / "release-worker"
        worker = write_worker(tmp_path, counter=counter, gate=gate)
        registry_dir = tmp_path / "registry"
        registry = write_registry(registry_dir, worker, counter=counter, gate=gate)
        proxy = Proxy(tmp_path / "queue", registry, local_runtime_dir=tmp_path / "runtime")
        proxy.ensure_layout()
        publish_job(proxy.root, "file", created_at="2026-01-01T00:00:00Z")
        first_chunk = asyncio.Event()
        finish_stream = asyncio.Event()
        received: list[bytes] = []
        dispatches = 0

        async def chat(request: web.Request) -> web.StreamResponse:
            nonlocal dispatches
            received.append(await request.read())
            dispatches += 1
            if dispatches == 1:
                response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
                await response.prepare(request)
                await response.write(b'{"response":"first"}\n')
                first_chunk.set()
                await finish_stream.wait()
                await response.write(b'{"response":"done","done":true}\n')
                await response.write_eof()
                return response
            return web.json_response({"done": True})

        async def tags(_request: web.Request) -> web.Response:
            return web.json_response({"models": []})

        upstream_app = web.Application()
        upstream_app.router.add_post("/api/chat", chat)
        upstream_app.router.add_get("/api/tags", tags)
        upstream_runner, upstream_port = await start_server(upstream_app)
        listener_port = unused_port()
        state = HttpRuntimeState()
        runtime = RuntimeConfig(runtime_dir=str(tmp_path / "runtime"))
        config = HttpProxyConfig(listen_port=listener_port, upstream=f"http://127.0.0.1:{upstream_port}", profile=OLLAMA_PROFILE, continuation_grace_seconds=0)
        service_task = asyncio.create_task(run_http_proxies(proxy, [config], 0.01, runtime_config=runtime, runtime_state=state))
        try:
            await asyncio.wait_for(state.ready.wait(), 3)
            await wait_for(lambda: (proxy.running_root / "demo" / "file").is_dir(), description="file worker occupying shared slot")
            async with ClientSession() as client:
                first_task = asyncio.create_task(client.post(f"http://127.0.0.1:{listener_port}/api/chat", json={"model": "controlled"}))
                await wait_for(lambda: state.queue.snapshot()["queued"] == 1, description="first HTTP request queued behind file worker")
                gate.write_text("continue", encoding="utf-8")
                await asyncio.wait_for(first_chunk.wait(), 3)
                first = await asyncio.wait_for(first_task, 3)
                request_id = first.headers.get("X-AI-Proxy-Request-ID")
                assert request_id
                assert await asyncio.wait_for(first.content.readline(), 3) == b'{"response":"first"}\n'
                second_task = asyncio.create_task(client.post(f"http://127.0.0.1:{listener_port}/api/chat", json={"model": "controlled"}))
                await wait_for(lambda: state.queue.snapshot()["queued"] == 1, description="second HTTP request queued behind stream")
                async with client.get(f"http://127.0.0.1:{listener_port}/api/tags") as bypass:
                    assert await bypass.json() == {"models": []}
                assert dispatches == 1
                finish_stream.set()
                assert await asyncio.wait_for(first.content.readline(), 3) == b'{"response":"done","done":true}\n'
                await first.read()
                async with await asyncio.wait_for(second_task, 3) as second:
                    assert second.status == 200 and await second.json() == {"done": True}
                    assert second.headers.get("X-AI-Proxy-Request-ID")
                assert dispatches == 2 and len(received) == 2
                assert json.loads(received[0])["model"] == "controlled"
                assert (proxy.answer_root / "demo" / "file" / "output.txt").is_file()
        finally:
            finish_stream.set()
            service_task.cancel()
            try:
                await service_task
            except asyncio.CancelledError:
                pass
            await upstream_runner.cleanup()

    asyncio.run(scenario())


def test_reasoning_policy_reload_and_preservation_through_http(tmp_path: Path) -> None:
    async def scenario() -> None:
        captured: list[tuple[str, dict[str, object]]] = []

        async def chat(request: web.Request) -> web.Response:
            captured.append((request.path, await request.json()))
            return web.json_response({"done": True})

        fake = web.Application()
        fake.router.add_post("/v1/chat/completions", chat)
        fake.router.add_post("/api/chat", chat)
        runner, upstream_port = await start_server(fake)
        policy = tmp_path / "policy.json"

        def write_policy(operation: str = "remove", value: str | None = None) -> None:
            mapping: dict[str, object] = {"operation": operation}
            if value is not None:
                mapping["value"] = value
            policy.write_text(json.dumps({"schema_version": 1, "aliases": {
                "analysis": {"client_default": "high", "mode": "mapped", "mappings": {
                    effort: ({"operation": "remove"} if effort == "none" else {"operation": "set", "value": "medium"})
                    for effort in ("none", "low", "medium")
                } | {"high": mapping}}
            }}), encoding="utf-8")

        write_policy()
        before_stat = policy.stat()
        runtime_dir = tmp_path / "runtime"
        runtime = RuntimeConfig(runtime_dir=str(runtime_dir), ollama_reasoning_policy_path=str(policy))
        proxy = Proxy(tmp_path / "queue", write_registry(tmp_path / "registry", write_worker(tmp_path, counter=tmp_path / "runs.txt"), counter=tmp_path / "runs.txt"), local_runtime_dir=runtime_dir)
        listener_port = unused_port()
        config = HttpProxyConfig(listen_port=listener_port, upstream=f"http://127.0.0.1:{upstream_port}", profile=OLLAMA_PROFILE, continuation_grace_seconds=0)
        state = HttpRuntimeState()
        service_task = asyncio.create_task(run_http_proxies(proxy, [config], 0.01, runtime_config=runtime, runtime_state=state))
        try:
            await asyncio.wait_for(state.ready.wait(), 3)
            async with ClientSession() as client:
                original = {"model": "analysis:latest", "messages": [{"role": "user", "content": "keep this"}]}
                async with await client.post(f"http://127.0.0.1:{listener_port}/v1/chat/completions", json=original) as response:
                    assert response.status == 200
                assert captured[-1][1] == original

                write_policy("set", "max")
                current = policy.stat()
                new_mtime = max(current.st_mtime_ns, before_stat.st_mtime_ns + 2_000_000_000)
                os.utime(policy, ns=(current.st_atime_ns, new_mtime))
                assert policy.stat().st_mtime_ns > before_stat.st_mtime_ns
                async with await client.post(f"http://127.0.0.1:{listener_port}/v1/chat/completions", json=original) as response:
                    assert response.status == 200
                assert captured[-1][1] == {**original, "reasoning_effort": "max"}
                assert captured[-1][1]["messages"] == original["messages"]

                policy.write_text("{", encoding="utf-8")
                native = {"model": "analysis", "messages": [{"role": "user", "content": "native"}], "think": "high"}
                async with await client.post(f"http://127.0.0.1:{listener_port}/api/chat", json=native) as response:
                    assert response.status == 200
                assert captured[-1][1] == native
                async with client.get(f"http://127.0.0.1:{listener_port}/_proxy/status") as response:
                    status = await response.json()
                assert status["reasoning_policy"]["last_known_good"] is True
                assert "invalid reasoning policy" in status["reasoning_policy"]["last_reload_error"]
        finally:
            service_task.cancel()
            try:
                await service_task
            except asyncio.CancelledError:
                pass
            await runner.cleanup()

    asyncio.run(scenario())


def test_comfyui_settlement_handoff_and_cached_result_survive_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        comfy_port = unused_port()
        output_dir, runtime_dir = tmp_path / "output", tmp_path / "runtime"
        output_dir.mkdir()
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        settlement, prompt_marker = tmp_path / "settled", tmp_path / "prompt-id.txt"
        comfy_script = tmp_path / "fake_comfy.py"
        comfy_script.write_text(
            "import argparse, http.server, json, pathlib\n"
            "p=argparse.ArgumentParser(); p.add_argument('--port',type=int,required=True); p.add_argument('--input-directory',required=True); p.add_argument('--output-directory',required=True); p.add_argument('--history-gate',required=True); p.add_argument('--prompt-marker',required=True); a=p.parse_args()\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            " def log_message(self,*args): pass\n"
            " def send(self,body,kind='application/json'):\n"
            "  b=body if isinstance(body,bytes) else json.dumps(body).encode(); self.send_response(200); self.send_header('Content-Type',kind); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)\n"
            " def do_GET(self):\n"
            "  if self.path=='/queue': self.send({'queue_running':[],'queue_pending':[]})\n"
            "  elif self.path.startswith('/history/'):\n"
            "   pid=self.path.rsplit('/',1)[1]; gate=pathlib.Path(a.history_gate); output=pathlib.Path(a.output_directory)/'render.png'\n"
            "   self.send({pid:{'status':{'completed':True,'status_str':'success'},'outputs':{'1':{'images':[{'filename':'render.png','subfolder':'','type':'output'}]}}}} if gate.exists() else {})\n"
            "  elif self.path.startswith('/view'):\n"
            "   self.send((pathlib.Path(a.output_directory)/'render.png').read_bytes(),'image/png')\n"
            "  elif self.path.startswith('/object_info'): self.send({'CheckpointLoaderSimple':{'input':{'required':{'ckpt_name':[['tiny.safetensors']]}}}})\n"
            "  else: self.send({})\n"
            " def do_POST(self):\n"
            "  n=int(self.headers.get('Content-Length','0')); data=json.loads(self.rfile.read(n) or b'{}')\n"
            "  if self.path=='/prompt': pathlib.Path(a.prompt_marker).write_text(data['prompt_id']); self.send({'prompt_id':data['prompt_id']})\n"
            "  else: self.send({})\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1',a.port),H).serve_forever()\n",
            encoding="utf-8",
        )
        ollama_calls: list[str] = []

        async def chat(request: web.Request) -> web.Response:
            ollama_calls.append(request.path)
            return web.json_response({"done": True})

        async def ps(_request: web.Request) -> web.Response:
            return web.json_response({"models": []})

        ollama_app = web.Application()
        ollama_app.router.add_post("/api/chat", chat)
        ollama_app.router.add_get("/api/ps", ps)
        ollama_runner, ollama_port = await start_server(ollama_app)
        comfy_config = ManagedComfyUIConfig(
            enabled=True, executable=sys.executable, script=str(comfy_script),
            arguments=("--port", str(comfy_port), "--input-directory", str(input_dir), "--output-directory", str(output_dir), "--history-gate", str(settlement), "--prompt-marker", str(prompt_marker)),
            working_directory=str(tmp_path), upstream=f"http://127.0.0.1:{comfy_port}",
            startup_deadline_seconds=5, startup_cooldown_seconds=0, idle_shutdown_seconds=30, shutdown_deadline_seconds=2,
        )
        runtime = RuntimeConfig(runtime_dir=str(runtime_dir), managed_comfyui=comfy_config, generation_settlement_seconds=5)
        comfy_listener, ollama_listener = unused_port(), unused_port()
        configs = [
            HttpProxyConfig(listen_port=comfy_listener, upstream=comfy_config.upstream, profile=COMFYUI_PROFILE, settle_poll_seconds=0.01, continuation_grace_seconds=0),
            HttpProxyConfig(listen_port=ollama_listener, upstream=f"http://127.0.0.1:{ollama_port}", profile=OLLAMA_PROFILE, continuation_grace_seconds=0),
        ]
        proxy = Proxy(tmp_path / "queue", write_registry(tmp_path / "registry", write_worker(tmp_path, counter=tmp_path / "runs.txt"), counter=tmp_path / "runs.txt"), local_runtime_dir=runtime_dir)

        async def start_runtime() -> tuple[HttpRuntimeState, asyncio.Task[None]]:
            state = HttpRuntimeState()
            task = asyncio.create_task(run_http_proxies(proxy, configs, 0.01, runtime_config=runtime, runtime_state=state))
            await asyncio.wait_for(state.ready.wait(), 4)
            return state, task

        state = HttpRuntimeState()
        task: asyncio.Task[None] | None = None
        owned_pid: int | None = None
        try:
            state, task = await start_runtime()
            async with ClientSession() as client:
                # Discovery itself starts the owned backend and proves its API is ready.
                async with client.get(f"http://127.0.0.1:{comfy_listener}/object_info/CheckpointLoaderSimple") as discovery:
                    assert discovery.status == 200 and "CheckpointLoaderSimple" in await discovery.text()
                manager = state.backend_manager
                assert manager is not None and manager.ownership == "proxy"
                owned_pid = manager.status()["pid"]
                output = output_dir / "render.png"
                output.write_bytes(b"small controlled image")
                prompt_task = asyncio.create_task(client.post(f"http://127.0.0.1:{comfy_listener}/prompt", json={"prompt": {"1": {"inputs": {}}}}))
                await wait_for(lambda: prompt_marker.is_file(), description="ComfyUI prompt dispatch")
                prompt_response = await asyncio.wait_for(prompt_task, 3)
                prompt_id = (await prompt_response.json())["prompt_id"]
                ollama_task = asyncio.create_task(client.post(f"http://127.0.0.1:{ollama_listener}/api/chat", json={"model": "controlled"}))
                await wait_for(lambda: state.queue.snapshot()["queued"] == 1, description="Ollama work waiting for keyed ComfyUI history")
                assert ollama_calls == [] and manager.status()["pid"] == owned_pid

                settlement.write_text("complete", encoding="utf-8")
                ollama_response = await asyncio.wait_for(ollama_task, 4)
                assert ollama_response.status == 200
                assert await ollama_response.json() == {"done": True}
                await wait_for(lambda: manager.status()["pid"] is None, description="managed ComfyUI stopped before Ollama work")
                assert not output.exists()
                cached_path = state.store.artifact(prompt_id, "render.png", "", "output")
                assert cached_path is not None and cached_path.read_bytes() == b"small controlled image"
                assert state.store.history(prompt_id) is not None

                assert task is not None
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                task = None
                state, task = await start_runtime()
                async with client.get(f"http://127.0.0.1:{comfy_listener}/history/{prompt_id}") as history:
                    assert json.loads(await history.read())[prompt_id]["status"]["status_str"] == "success"
                async with client.get(f"http://127.0.0.1:{comfy_listener}/view", params={"filename": "render.png", "prompt_id": prompt_id}) as view:
                    assert await view.read() == b"small controlled image"
                assert state.store is not None and state.store.artifact(prompt_id, "render.png", "", "output") is not None
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await ollama_runner.cleanup()
            if owned_pid is not None:
                # The proxy owns this disposable process and must have stopped it before task cleanup.
                assert_process_dead(owned_pid)

    asyncio.run(scenario())

