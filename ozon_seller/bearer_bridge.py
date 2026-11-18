"""Expose one read-only Ozon MCP behind a per-store bearer credential."""

import os
from collections import deque
from secrets import compare_digest
from time import monotonic

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from ozon_seller import config
from ozon_seller.server import create_server


secret = os.environ["OZON_BRIDGE_TOKEN"]
if len(secret) < 40:
    raise ValueError("Use a strong per-store bridge token")

server = create_server(config.config_path(), "Europe/Moscow")
inner = server.streamable_http_app(
    json_response=True,
    stateless_http=True,
    max_request_body_size=32768,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*"],
        allowed_origins=[],
    ),
)
recent = deque()


async def app(scope, receive, send):
    if scope["type"] != "http":
        return await inner(scope, receive, send)
    headers = dict(scope.get("headers", []))
    authorized = compare_digest(
        headers.get(b"authorization", b""), ("Bearer " + secret).encode()
    )
    response = None
    if not authorized:
        response = JSONResponse({"error": "unauthorized"}, status_code=401)
    elif scope["path"] != "/mcp" or scope["method"] != "POST":
        response = JSONResponse({"error": "not_found"}, status_code=404)
    else:
        now = monotonic()
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= 60:
            response = JSONResponse({"error": "rate_limited"}, status_code=429)
        else:
            recent.append(now)
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
        return await response(scope, receive, send)
    return await inner(scope, receive, send)
