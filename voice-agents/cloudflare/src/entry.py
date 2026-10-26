"""Routes:

GET /health
GET /agents                    list of demo agents
GET /agents/<slug>/ws?org=...  Coval WebSocket (Voice) connection; one Durable Object per call
"""

import hmac
import uuid
from urllib.parse import urlparse

from workers import WorkerEntrypoint

from server.call import CovalCall as CovalCall
from server.http import reply
from verticals.catalog import VERTICALS, get_vertical


def _authorized(request, env) -> bool:
    expected = getattr(env, "AGENT_AUTH_TOKEN", "") or ""
    if not expected:
        return True
    header = request.headers.get("Authorization") or ""
    token = header.removeprefix("Bearer ").strip()
    return hmac.compare_digest(token, expected)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        parsed = urlparse(request.url)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/health":
            return reply({"status": "ok", "agents": sorted(VERTICALS)})
        if parts == ["agents"]:
            return reply([{"slug": v.slug, "name": v.agent_name} for v in VERTICALS.values()])
        if len(parts) != 3 or parts[0] != "agents" or parts[2] != "ws":
            return reply({"error": "not_found"}, 404)
        if get_vertical(parts[1]) is None:
            return reply({"error": "unknown_agent"}, 404)
        if (request.headers.get("Upgrade") or "").lower() != "websocket":
            return reply({"error": "expected_websocket"}, 426)
        if not _authorized(request, self.env):
            return reply({"error": "unauthorized"}, 401)
        return await self.env.CALLS.getByName(uuid.uuid4().hex).fetch(request)
