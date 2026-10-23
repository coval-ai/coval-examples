"""One simulation call per Durable Object. The object lives as long as Coval's WebSocket."""

import uuid
from urllib.parse import parse_qs, urlparse

from workers import DurableObject, Response

from agent.engine import CovalEngine
from verticals.catalog import get_vertical


class CovalCall(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.engine = None

    async def fetch(self, request):
        parsed = urlparse(request.url)
        parts = [part for part in parsed.path.split("/") if part]
        vertical = get_vertical(parts[1] if len(parts) > 1 else "")
        if vertical is None:
            return Response("Unknown agent", status=404)
        if self.engine is not None:
            return Response("This call already has a connection", status=409)
        org = parse_qs(parsed.query).get("org", [""])[0]
        call_id = uuid.uuid4().hex
        self.engine = CovalEngine(self.ctx, self.env, vertical, call_id=call_id, org=org)
        return self.engine.accept()
