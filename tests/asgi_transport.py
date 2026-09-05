"""Sync httpx transport that dispatches to the FastAPI app in-process.

Avoids coupling tests to starlette's TestClient internals (its transport
changed shape across starlette 1.2 -> 1.6 and now targets httpx 2.x).
Used by the client / CLI / MCP test suites.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx


class InProcessASGITransport(httpx.BaseTransport):
    """Runs the ASGI app with anyio.run() per request — no network, no portals."""

    def __init__(self, app: Any):
        self.app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        import anyio

        body = b"".join(request.stream)
        scope: Dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path,
            "query_string": bytes(request.url.query, "ascii") if isinstance(request.url.query, str) else request.url.query,
            "root_path": "",
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "client": ("testclient", 50000),
            "server": (request.url.host or "testserver", request.url.port or 80),
        }

        status: Dict[str, int] = {}
        response_headers: List[Any] = []
        chunks: List[bytes] = []

        async def receive() -> Dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                response_headers.extend(message.get("headers", []))
            elif message["type"] == "http.response.body":
                if message.get("body"):
                    chunks.append(message["body"])

        anyio.run(lambda: self.app(scope, receive, send))

        if "code" not in status:
            raise RuntimeError("ASGI app did not send http.response.start")
        return httpx.Response(
            status_code=status["code"],
            headers=response_headers,
            content=b"".join(chunks),
            request=request,
        )

    def close(self) -> None:
        pass
