from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import sys
import uuid

from aiohttp import ClientSession, web
import pytest

from file_proxy.engine import Proxy
from file_proxy.backend import BackendManager, BackendUnavailable
from file_proxy.http_proxy import COMFYUI_PROFILE, HttpProxyConfig, HttpProxyService, _prepare_prompt_id, comfyui_resource_key
from file_proxy.models import JobManifest
from file_proxy.registry import Registry
from file_proxy.runtime import ManagedComfyUIConfig, RuntimeConfig
from file_proxy.scheduler import HttpQueue
from file_proxy.store import ExecutionStore


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_prompt_ids_are_canonical_and_invalid_supplied_ids_are_rejected() -> None:
    body, prompt_id, changed = _prepare_prompt_id(b'{"prompt":{"1":{"inputs":{}}}}', "POST", "/prompt")
    assert changed and str(uuid.UUID(prompt_id)) == prompt_id
    assert json.loads(body)["prompt_id"] == prompt_id
    supplied = str(uuid.uuid4())
    _, preserved, changed = _prepare_prompt_id(json.dumps({"prompt": {}, "prompt_id": supplied}).encode(), "POST", "/prompt")
    assert preserved == supplied and not changed
    with pytest.raises(ValueError, match="canonical UUID"):
        _prepare_prompt_id(b'{"prompt":{},"prompt_id":"old-history"}', "POST", "/prompt")


def test_comfyui_signature_and_legacy_resource_normalization() -> None:
    body = json.dumps({"prompt": {"2": {"inputs": {"lora_name": "style.safetensors"}}, "1": {"inputs": {"ckpt_name": "base.safetensors", "vae_name": "vae.safetensors"}}}}).encode()
    assert comfyui_resource_key("POST", "/prompt", body) == "comfyui:ckpt_name=base.safetensors|vae_name=vae.safetensors|lora_name=style.safetensors"
    manifest = JobManifest.from_dict({"protocol_version": 1, "job_id": "j", "subscriber_id": "s", "worker": "w", "created_at": "2026-01-01T00:00:00Z", "resource_key": "image:comfyui:model"})
    assert manifest.resource_key == "comfyui:model"


def test_execution_store_rejects_prompt_reuse_and_survives_reopen(tmp_path: Path) -> None:
    prompt_id = str(uuid.uuid4())
    store = ExecutionStore(tmp_path / "runtime")
    store.create("request-1", "comfyui", prompt_id)
    with pytest.raises(FileExistsError):
        store.create("request-2", "comfyui", prompt_id)
    history = json.dumps({prompt_id: {"outputs": {}}}).encode()
    store.update("request-1", "completed", outcome="succeeded", history=history)
    reopened = ExecutionStore(tmp_path / "runtime")
    assert reopened.history(prompt_id) == history
    assert reopened.get("request-1")["outcome"] == "succeeded"


def test_terminal_comfyui_error_is_not_recorded_as_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        prompt_id = str(uuid.uuid4())
        store = ExecutionStore(tmp_path / "runtime")
        store.create("request-error", "comfyui", prompt_id)
        service = HttpProxyService(
            HttpProxyConfig(listen_port=unused_port(), profile=COMFYUI_PROFILE),
            HttpQueue(),
            lambda _message: None,
            store=store,
        )
        history = json.dumps(
            {prompt_id: {"outputs": {}, "status": {"status_str": "error", "completed": False}}}
        ).encode()
        await service._archive_completion("request-error", prompt_id, history)
        record = store.get("request-error")
        assert record["state"] == "failed"
        assert record["outcome"] == "upstream_error"
        assert store.history(prompt_id) == history

    asyncio.run(scenario())


def test_runtime_config_is_opt_in_and_validates_unknown_keys(tmp_path: Path) -> None:
    assert not RuntimeConfig.load(None).managed_comfyui.enabled
    path = tmp_path / "runtime.json"
    path.write_text('{"unexpected": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown runtime"):
        RuntimeConfig.load(path)


def test_prompt_holds_shared_slot_until_keyed_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        settled = asyncio.Event()
        seen: list[str] = []

        async def prompt(request: web.Request) -> web.Response:
            payload = await request.json()
            seen.append(payload["prompt_id"])
            return web.json_response({"prompt_id": payload["prompt_id"]})

        async def history(request: web.Request) -> web.Response:
            prompt_id = request.match_info["prompt_id"]
            return web.json_response({prompt_id: {"outputs": {}}} if settled.is_set() else {})

        upstream = web.Application()
        upstream.router.add_post("/prompt", prompt)
        upstream.router.add_get("/history/{prompt_id}", history)
        runner = web.AppRunner(upstream)
        await runner.setup()
        upstream_port = unused_port()
        await web.TCPSite(runner, "127.0.0.1", upstream_port).start()
        queue = HttpQueue()
        proxy = Proxy(tmp_path / "queue", Registry({}), local_runtime_dir=tmp_path / "runtime")
        proxy.ensure_layout()
        service = HttpProxyService(HttpProxyConfig(listen_port=unused_port(), upstream=f"http://127.0.0.1:{upstream_port}", profile=COMFYUI_PROFILE, settle_poll_seconds=0.01, upstream_timeout_seconds=2), queue, lambda _message: None)
        await service.start()
        scheduler = asyncio.create_task(proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0))
        try:
            async with ClientSession() as client:
                first = await client.post(f"http://127.0.0.1:{service.config.listen_port}/prompt", json={"prompt": {}})
                assert first.status == 200
                second_task = asyncio.create_task(client.post(f"http://127.0.0.1:{service.config.listen_port}/prompt", json={"prompt": {}}))
                await asyncio.sleep(0.08)
                assert len(seen) == 1
                settled.set()
                second = await asyncio.wait_for(second_task, 1)
                assert second.status == 200
                second.release()
                first.release()
        finally:
            scheduler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scheduler
            await service.stop()
            await runner.cleanup()

    asyncio.run(scenario())


def test_managed_backend_starts_hidden_and_stops_disposable_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        port = unused_port()
        script = tmp_path / "fake_comfy.py"
        script.write_text(
            "from http.server import BaseHTTPRequestHandler,HTTPServer\n"
            "import sys\n"
            "class H(BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  self.send_response(200 if self.path == '/system_stats' else 404); self.end_headers(); self.wfile.write(b'{}')\n"
            " def log_message(self,*args): pass\n"
            "HTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()\n",
            encoding="utf-8",
        )
        manager = BackendManager(
            ManagedComfyUIConfig(
                enabled=True,
                executable=sys.executable,
                script=str(script),
                arguments=(str(port),),
                working_directory=str(tmp_path),
                upstream=f"http://127.0.0.1:{port}",
                startup_deadline_seconds=5,
                shutdown_deadline_seconds=2,
            ),
            tmp_path / "runtime",
            lambda _message: None,
        )
        await manager.ensure_comfyui()
        pid = manager.status()["pid"]
        assert isinstance(pid, int) and manager.state == "ready"
        await manager.stop_comfyui()
        assert manager.status()["pid"] is None and manager.state == "stopped"

    asyncio.run(scenario())


def test_managed_backend_never_adopts_manual_listener(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def stats(_request: web.Request) -> web.Response:
            return web.json_response({})

        app = web.Application()
        app.router.add_get("/system_stats", stats)
        runner = web.AppRunner(app)
        await runner.setup()
        port = unused_port()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        manager = BackendManager(
            ManagedComfyUIConfig(enabled=True, executable=sys.executable, working_directory=str(tmp_path), upstream=f"http://127.0.0.1:{port}"),
            tmp_path / "runtime",
            lambda _message: None,
        )
        try:
            with pytest.raises(BackendUnavailable, match="manually started"):
                await manager.ensure_comfyui()
            assert manager.ownership == "external" and manager.state == "blocked"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
