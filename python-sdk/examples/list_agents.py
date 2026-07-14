"""Sample: list all agents in the org.

Run with:

    COVAL_API_KEY=... python python-sdk/examples/list_agents.py

This sample uses the typed `agents_api.list_agents(...)` call by default. If
your org has data that violates the spec's strict pydantic validation
(e.g., a legacy malformed agent_id), pass `--raw` to fall back to the
without_preload_content variant, which returns the raw response so you can
parse it yourself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from coval_sdk import AgentsApi, ApiClient, Configuration


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Bypass pydantic validation by parsing the raw HTTP response.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("COVAL_API_KEY")
    if not api_key:
        sys.stderr.write("Set COVAL_API_KEY in the environment.\n")
        return 1

    # Coval's gateway requires the header `x-api-key`. After bundling the
    # spec, the security scheme is split per-tag (Coval_Agents_API_ApiKeyAuth,
    # Coval_Conversations_API_ApiKeyAuth, …), so the cleanest pattern is to
    # set the header directly on the client.
    config = Configuration(host="https://api.coval.dev/v1")

    with ApiClient(config) as client:
        client.set_default_header("x-api-key", api_key)
        agents_api = AgentsApi(client)

        agents: list[dict] = []
        page_token: str | None = None
        while True:
            if args.raw:
                resp = agents_api.list_agents_without_preload_content(
                    page_size=50, page_token=page_token
                )
                body = json.loads(resp.data.decode("utf-8"))
                agents.extend(body.get("agents", []))
                page_token = body.get("next_page_token")
            else:
                page = agents_api.list_agents(page_size=50, page_token=page_token)
                for a in page.agents:
                    agents.append({
                        "id": a.id,
                        "display_name": a.display_name,
                        "model_type": a.model_type,
                    })
                page_token = page.next_page_token
            if not page_token:
                break

        print(f"Found {len(agents)} agent(s).\n")
        for a in agents:
            print(f"  {a.get('id') or '(no id)':24}  {a.get('model_type') or '-':28}  {a.get('display_name') or '(unnamed)'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
