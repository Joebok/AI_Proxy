from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import pytest

from file_proxy.engine import Proxy
from file_proxy.http_proxy import COMFYUI_PROFILE, OLLAMA_PROFILE, HttpProxyConfig, HttpRuntimeState, run_http_proxies
from file_proxy.registry import Registry
from file_proxy.runtime import RuntimeConfig

from conftest import assert_process_dead, unused_port, wait_for


def _assert_proxy_stopped(config: RuntimeConfig) -> None:
    path = Path(config.runtime_dir) / "proxy.lock"
    if not path.is_file():
        return
    try:
        owner = json.loads(path.read_text(encoding="utf-8"))
        pid = int(owner["pid"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        pytest.fail(f"Cannot verify the installed proxy lock at {path}: {exc}")
    try:
        assert_process_dead(pid)
    except AssertionError:
        pytest.fail(f"A proxy still owns {path} (PID {pid}); stop the normal proxy before the live check")


async def _request_json(session: aiohttp.ClientSession, method: str, url: str, **kwargs):
    async with session.request(method, url, **kwargs) as response:
        body = await response.read()
        assert response.status < 300, f"{method} {url} returned {response.status}: {body[:500]!r}"
        return json.loads(body) if response.content_type == "application/json" else body


@pytest.mark.live_backends
def test_installed_ollama_and_managed_comfyui_lifecycle(pytestconfig: pytest.Config, tmp_path: Path) -> None:
    model = pytestconfig.getoption("--ollama-test-model")
    assert model, "Supply --ollama-test-model with an installed model that fits the available GPU/VRAM"
    config_path = Path(pytestconfig.getoption("--live-runtime-config")).resolve()
    assert config_path.is_file(), f"Live runtime config does not exist: {config_path}"
    configured = RuntimeConfig.load(config_path).resolved(config_path.parent)
    comfy = configured.managed_comfyui
    assert comfy.enabled, "Live runtime config must enable managed ComfyUI"
    assert comfy.executable and Path(comfy.executable).is_file(), f"ComfyUI executable is missing: {comfy.executable}"
    assert comfy.working_directory and Path(comfy.working_directory).is_dir(), f"ComfyUI working directory is missing: {comfy.working_directory}"
    assert comfy.script and Path(comfy.script).is_file(), f"ComfyUI script is missing: {comfy.script}"
    assert comfy.model_config and Path(comfy.model_config).is_file(), f"ComfyUI model-path configuration is missing: {comfy.model_config}"
    _assert_proxy_stopped(configured)

    async def scenario() -> None:
        ollama_upstream = "http://127.0.0.1:11434"
        original = comfy.arguments
        arguments = list(original)
        for option, path in (("--input-directory", tmp_path / "input"), ("--output-directory", tmp_path / "output")):
            assert option in arguments and arguments.index(option) + 1 < len(arguments), f"managed ComfyUI needs {option}"
            index = arguments.index(option) + 1
            path.mkdir(parents=True, exist_ok=True)
            arguments[index] = str(path)
        local_comfy = replace(
            comfy,
            arguments=tuple(arguments),
            upstream=f"http://127.0.0.1:{urlsplit(comfy.upstream).port or 8188}",
            startup_deadline_seconds=max(120.0, comfy.startup_deadline_seconds),
            idle_shutdown_seconds=2.0,
        )
        runtime = replace(configured, runtime_dir=str(tmp_path / "runtime"), managed_comfyui=local_comfy)
        runtime.validate()
        ollama_port, comfy_listener_port = unused_port(), unused_port()
        configs = [
            HttpProxyConfig(listen_port=ollama_port, upstream=ollama_upstream, profile=OLLAMA_PROFILE, continuation_grace_seconds=0),
            HttpProxyConfig(listen_port=comfy_listener_port, upstream=local_comfy.upstream, profile=COMFYUI_PROFILE, continuation_grace_seconds=0),
        ]
        proxy = Proxy(tmp_path / "queue", Registry({}), local_runtime_dir=Path(runtime.runtime_dir))
        # Check installed state before launching the isolated proxy or changing residency.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as preflight:
            tags = await _request_json(preflight, "GET", f"{ollama_upstream}/api/tags")
            installed = {item.get("name") for item in tags.get("models", []) if isinstance(item, dict)}
            assert model in installed, f"{model!r} is not installed in Ollama; available models: {sorted(installed)}"
            before = await _request_json(preflight, "GET", f"{ollama_upstream}/api/ps")
            assert not before["models"], f"Ollama already has resident models; unload them before testing: {before['models']}"
        state = HttpRuntimeState()
        task = asyncio.create_task(run_http_proxies(proxy, configs, 0.1, runtime_config=runtime, runtime_state=state))
        owned_pid: int | None = None
        test_may_own_model = False
        try:
            await asyncio.wait_for(state.ready.wait(), 10)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as session:
                test_may_own_model = True
                preload = await _request_json(session, "POST", f"http://127.0.0.1:{ollama_port}/api/generate", json={"model": model, "stream": False, "keep_alive": "5m"})
                assert preload.get("model") == model
                resident = await _request_json(session, "GET", f"{ollama_upstream}/api/ps")
                assert any(item.get("name") == model for item in resident["models"]), f"Ollama did not report {model!r} resident: {resident}"

                info = await _request_json(session, "GET", f"http://127.0.0.1:{comfy_listener_port}/object_info/CheckpointLoaderSimple")
                inputs = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
                assert inputs, "ComfyUI checkpoint model discovery returned no configured checkpoints"
                cleared = await _request_json(session, "GET", f"{ollama_upstream}/api/ps")
                assert cleared["models"] == [], f"ComfyUI started while Ollama models remained resident: {cleared}"
                manager = state.backend_manager
                assert manager is not None and manager.ownership == "proxy" and manager.status()["pid"]
                owned_pid = int(manager.status()["pid"])

                second_load = await _request_json(session, "POST", f"http://127.0.0.1:{ollama_port}/api/generate", json={"model": model, "stream": False, "keep_alive": "5m"})
                assert second_load.get("model") == model
                await wait_for(lambda: manager.status()["pid"] is None, timeout=30, description="ComfyUI stop before Ollama handoff")
                assert_process_dead(owned_pid)
                resident_again = await _request_json(session, "GET", f"{ollama_upstream}/api/ps")
                assert any(item.get("name") == model for item in resident_again["models"]), f"Ollama did not reload {model!r}: {resident_again}"

                unload = await _request_json(session, "POST", f"http://127.0.0.1:{ollama_port}/api/generate", json={"model": model, "keep_alive": 0, "stream": False})
                assert unload.get("model") == model
                await wait_for(lambda: _ollama_empty(session, ollama_upstream), timeout=30, description="Ollama model unload")
                await _request_json(session, "GET", f"http://127.0.0.1:{comfy_listener_port}/object_info/CheckpointLoaderSimple")
                next_pid = int(manager.status()["pid"])
                assert next_pid != owned_pid
                await wait_for(lambda: manager.status()["pid"] is None, timeout=15, description="ComfyUI idle shutdown")
                assert_process_dead(next_pid)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Never stop an external/manual ComfyUI. run_http_proxies closes only its owned process.
            if test_may_own_model:
                try:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as cleanup:
                        async with cleanup.post(f"{ollama_upstream}/api/generate", json={"model": model, "keep_alive": 0, "stream": False}) as response:
                            await response.read()
                        await wait_for(lambda: _ollama_empty(cleanup, ollama_upstream), timeout=30, description="live test Ollama cleanup")
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    pytest.fail(f"Could not unload the test-owned Ollama model during cleanup: {exc}")

    asyncio.run(scenario())


async def _ollama_empty(session: aiohttp.ClientSession, upstream: str) -> bool:
    async with session.get(f"{upstream}/api/ps") as response:
        if response.status >= 300:
            return False
        payload = await response.json()
    return isinstance(payload, dict) and payload.get("models") == []
