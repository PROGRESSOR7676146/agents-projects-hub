"""Real local HTTP: no proxy/redirect/retry and a total response deadline."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from aiohttp import web

from hermes_codex_router import consult


class ConsultTransportTests(unittest.IsolatedAsyncioTestCase):
    async def server(self, handler) -> str:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        return f"http://127.0.0.1:{port}/v1/responses"

    async def test_explicit_post_ignores_environment_proxies(self) -> None:
        seen = []

        async def handler(request):
            seen.append((request.method, request.path, await request.json()))
            self.assertEqual(request.headers["Authorization"], "Bearer fictional")
            return web.json_response({"visible": "fixture"})

        endpoint = await self.server(handler)
        with patch.dict(
            "os.environ",
            {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "http_proxy": "http://127.0.0.1:1",
                "NO_PROXY": "",
                "no_proxy": "",
            },
        ):
            raw = await consult._request(
                {"endpoint": endpoint, "timeout_seconds": 5}, {"model": "example"}, "fictional"
            )
        self.assertIn(b"fixture", raw)
        self.assertEqual(seen, [("POST", "/v1/responses", {"model": "example"})])

    async def test_redirect_never_gets_a_second_request(self) -> None:
        seen = []

        async def handler(request):
            seen.append(request.path)
            raise web.HTTPFound("/elsewhere")

        endpoint = await self.server(handler)
        with self.assertRaises(consult.ConsultError):
            await consult._request({"endpoint": endpoint, "timeout_seconds": 5}, {}, "fictional")
        self.assertEqual(seen, ["/v1/responses"])

    async def test_trickled_bytes_cannot_extend_total_deadline(self) -> None:
        seen = []

        async def handler(request):
            seen.append(request.path)
            response = web.StreamResponse()
            await response.prepare(request)
            try:
                for _ in range(100):
                    await response.write(b" ")
                    await asyncio.sleep(0.02)
            except ConnectionResetError:
                pass
            return response

        endpoint = await self.server(handler)
        with self.assertRaises(TimeoutError):
            await consult._request({"endpoint": endpoint, "timeout_seconds": 0.05}, {}, "fictional")
        self.assertEqual(seen, ["/v1/responses"])

    async def test_response_limit_closes_without_retry(self) -> None:
        seen = []

        async def handler(request):
            seen.append(request.path)
            return web.Response(body=b"x" * 100)

        endpoint = await self.server(handler)
        with patch.object(consult, "_RESPONSE_LIMIT", 20), self.assertRaises(consult.ConsultError):
            await consult._request({"endpoint": endpoint, "timeout_seconds": 5}, {}, "fictional")
        self.assertEqual(seen, ["/v1/responses"])
