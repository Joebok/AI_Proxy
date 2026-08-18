from __future__ import annotations

import asyncio
from pathlib import Path
import socket

from aiohttp import ClientSession, web
import pytest

from file_proxy.engine import Proxy
from file_proxy.http_proxy import HttpProxyConfig, HttpProxyService, parse_bypass_route
from file_proxy.registry import Registry
from file_proxy.scheduler import HttpQueue


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_proxy(tmp_path: Path) -> Proxy:
    proxy = Proxy(tmp_path / "File_Proxy", Registry({}))
    proxy.ensure_layout()
    return proxy


def test_http_configuration_validation() -> None:
    with pytest.raises(ValueError, match="same address and port"):
        HttpProxyConfig(
            listen_host="127.0.0.1",
            listen_port=11434,
            upstream="http://localhost:11434",
        ).validate()
    assert parse_bypass_route("get /health") == ("GET", "/health")
    with pytest.raises(ValueError, match="METHOD /path"):
        parse_bypass_route("/health")


@pytest.mark.parametrize(
    ("path", "content_type", "first_chunk", "second_chunk"),
    [
        (
            "/api/chat",
            "application/x-ndjson",
            b'{"response":"one","done":false}\n',
            b'{"response":"two","done":true}\n',
        ),
        (
            "/v1/chat/completions",
            "text/event-stream",
            b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n',
            b"data: [DONE]\n\n",
        ),
    ],
)
def test_streaming_request_is_forwarded_without_response_buffering(
    tmp_path: Path,
    path: str,
    content_type: str,
    first_chunk: bytes,
    second_chunk: bytes,
) -> None:
    async def scenario() -> None:
        captured: dict[str, object] = {}
        release = asyncio.Event()

        async def upstream_handler(request: web.Request) -> web.StreamResponse:
            captured.update(
                method=request.method,
                path=request.path,
                query=request.query_string,
                header=request.headers.get("X-Test"),
                body=await request.read(),
            )
            response = web.StreamResponse(
                status=201,
                headers={"Content-Type": content_type, "X-Upstream": "yes"},
            )
            await response.prepare(request)
            await response.write(first_chunk)
            await release.wait()
            await response.write(second_chunk)
            await response.write_eof()
            return response

        upstream_app = web.Application()
        upstream_app.router.add_post(path, upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_port = unused_port()
        await web.TCPSite(upstream_runner, "127.0.0.1", upstream_port).start()

        queue = HttpQueue()
        proxy = make_proxy(tmp_path)
        proxy_port = unused_port()
        service = HttpProxyService(
            HttpProxyConfig(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                upstream=f"http://127.0.0.1:{upstream_port}",
                continuation_grace_seconds=0,
            ),
            queue,
            lambda _message: None,
        )
        await service.start()
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0)
        )
        try:
            async with ClientSession() as client:
                async with client.post(
                    f"http://127.0.0.1:{proxy_port}{path}?mode=test",
                    data=b'{"model":"test"}',
                    headers={"Content-Type": "application/json", "X-Test": "forwarded"},
                ) as response:
                    assert response.status == 201
                    assert response.headers["X-Upstream"] == "yes"
                    assert response.headers["X-AI-Proxy-Request-ID"]
                    first = await asyncio.wait_for(response.content.readline(), 1)
                    if content_type == "text/event-stream":
                        first += await asyncio.wait_for(response.content.readline(), 1)
                    assert first == first_chunk
                    release.set()
                    second = await asyncio.wait_for(response.content.readline(), 1)
                    if content_type == "text/event-stream":
                        second += await asyncio.wait_for(response.content.readline(), 1)
                    assert second == second_chunk
            assert captured == {
                "method": "POST",
                "path": path,
                "query": "mode=test",
                "header": "forwarded",
                "body": b'{"model":"test"}',
            }
        finally:
            release.set()
            scheduler.cancel()
            try:
                await scheduler
            except asyncio.CancelledError:
                pass
            await service.stop()
            await upstream_runner.cleanup()

    asyncio.run(scenario())


def test_discovery_route_bypasses_active_http_job(tmp_path: Path) -> None:
    async def scenario() -> None:
        inference_started = asyncio.Event()
        release = asyncio.Event()

        async def chat(request: web.Request) -> web.Response:
            await request.read()
            inference_started.set()
            await release.wait()
            return web.json_response({"done": True})

        async def tags(_request: web.Request) -> web.Response:
            return web.json_response({"models": []})

        upstream_app = web.Application()
        upstream_app.router.add_post("/api/chat", chat)
        upstream_app.router.add_get("/api/tags", tags)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_port = unused_port()
        await web.TCPSite(upstream_runner, "127.0.0.1", upstream_port).start()

        queue = HttpQueue()
        proxy = make_proxy(tmp_path)
        proxy_port = unused_port()
        service = HttpProxyService(
            HttpProxyConfig(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                upstream=f"http://127.0.0.1:{upstream_port}",
                continuation_grace_seconds=0,
            ),
            queue,
            lambda _message: None,
        )
        await service.start()
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0)
        )
        try:
            async with ClientSession() as client:
                chat_task = asyncio.create_task(
                    client.post(f"http://127.0.0.1:{proxy_port}/api/chat", json={"model": "test"})
                )
                await asyncio.wait_for(inference_started.wait(), 1)
                async with client.get(f"http://127.0.0.1:{proxy_port}/api/tags") as response:
                    assert response.status == 200
                    assert await response.json() == {"models": []}
                release.set()
                chat_response = await asyncio.wait_for(chat_task, 1)
                await chat_response.release()
        finally:
            release.set()
            scheduler.cancel()
            try:
                await scheduler
            except asyncio.CancelledError:
                pass
            await service.stop()
            await upstream_runner.cleanup()

    asyncio.run(scenario())


def test_upstream_failure_and_body_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        queue = HttpQueue()
        proxy = make_proxy(tmp_path)
        proxy_port = unused_port()
        service = HttpProxyService(
            HttpProxyConfig(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                upstream=f"http://127.0.0.1:{unused_port()}",
                max_body_bytes=4,
                continuation_grace_seconds=0,
            ),
            queue,
            lambda _message: None,
        )
        await service.start()
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0)
        )
        try:
            async with ClientSession() as client:
                async with client.post(
                    f"http://127.0.0.1:{proxy_port}/api/chat", data=b"12345"
                ) as response:
                    assert response.status == 413
                async with client.post(
                    f"http://127.0.0.1:{proxy_port}/api/chat", data=b"1234"
                ) as response:
                    assert response.status == 502
        finally:
            scheduler.cancel()
            try:
                await scheduler
            except asyncio.CancelledError:
                pass
            await service.stop()

    asyncio.run(scenario())


def test_upstream_timeout_returns_504(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def slow(_request: web.Request) -> web.Response:
            await asyncio.sleep(1)
            return web.json_response({"done": True})

        upstream_app = web.Application()
        upstream_app.router.add_post("/api/chat", slow)
        upstream_runner = web.AppRunner(upstream_app, handler_cancellation=True)
        await upstream_runner.setup()
        upstream_port = unused_port()
        await web.TCPSite(upstream_runner, "127.0.0.1", upstream_port).start()

        queue = HttpQueue()
        proxy = make_proxy(tmp_path)
        proxy_port = unused_port()
        service = HttpProxyService(
            HttpProxyConfig(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                upstream=f"http://127.0.0.1:{upstream_port}",
                upstream_timeout_seconds=0.05,
                continuation_grace_seconds=0,
            ),
            queue,
            lambda _message: None,
        )
        await service.start()
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0)
        )
        try:
            async with ClientSession() as client:
                async with client.post(
                    f"http://127.0.0.1:{proxy_port}/api/chat", data=b"{}"
                ) as response:
                    assert response.status == 504
        finally:
            scheduler.cancel()
            try:
                await scheduler
            except asyncio.CancelledError:
                pass
            await service.stop()
            await upstream_runner.cleanup()

    asyncio.run(scenario())


def test_client_disconnect_closes_running_upstream(tmp_path: Path) -> None:
    async def scenario() -> None:
        upstream_cancelled = asyncio.Event()

        async def streaming(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
            await response.prepare(request)
            await response.write(b'{"done":false}\n')
            try:
                await asyncio.Event().wait()
            finally:
                upstream_cancelled.set()
            return response

        upstream_app = web.Application()
        upstream_app.router.add_post("/api/chat", streaming)
        upstream_runner = web.AppRunner(upstream_app, handler_cancellation=True)
        await upstream_runner.setup()
        upstream_port = unused_port()
        await web.TCPSite(upstream_runner, "127.0.0.1", upstream_port).start()

        queue = HttpQueue()
        proxy = make_proxy(tmp_path)
        proxy_port = unused_port()
        service = HttpProxyService(
            HttpProxyConfig(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                upstream=f"http://127.0.0.1:{upstream_port}",
                continuation_grace_seconds=0,
            ),
            queue,
            lambda _message: None,
        )
        await service.start()
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0)
        )
        try:
            async with ClientSession() as client:
                response = await client.post(
                    f"http://127.0.0.1:{proxy_port}/api/chat", data=b"{}"
                )
                assert await asyncio.wait_for(response.content.readline(), 1) == b'{"done":false}\n'
                response.close()
                await asyncio.wait_for(upstream_cancelled.wait(), 1)
        finally:
            scheduler.cancel()
            try:
                await scheduler
            except asyncio.CancelledError:
                pass
            await service.stop()
            await upstream_runner.cleanup()

    asyncio.run(scenario())
