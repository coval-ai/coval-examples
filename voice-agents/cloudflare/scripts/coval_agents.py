"""Create or update a Coval agent (MODEL_TYPE_WEBSOCKET) for each deployed demo agent.

  python3 scripts/coval_agents.py https://coval-demo-voice-agents.<subdomain>.workers.dev \
      --manifest coval-agents.json [--only <org>] [--dry-run]

Each manifest entry names a Coval org and, optionally, an existing agent to copy metrics, prompt
and descriptive metadata from (for example the Vapi agent this one replaces). Environment:
  AGENT_AUTH_TOKEN  the same value set as the Worker secret
  COVAL_API_KEYS    JSON map of org -> Coval API key (the same shape as the Worker secret)
Created agent IDs are written back to the manifest. See coval-agents.example.json.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.coval.dev/v1"


def coval_key(org: str) -> str:
    keys = json.loads(os.environ["COVAL_API_KEYS"])
    if org not in keys:
        sys.exit(f"COVAL_API_KEYS has no key for org {org!r}")
    return keys[org]


def call(key: str, method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        API + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"x-api-key": key, "Content-Type": "application/json"},
    )
    try:
        return json.load(urllib.request.urlopen(request, timeout=60))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{method} {path} -> {exc.code} {exc.read()[:400].decode(errors='replace')}"
        ) from None


def websocket_metadata(base_url: str, slug: str, org: str, token: str) -> dict:
    ws = base_url.rstrip("/").replace("https://", "wss://")
    return {
        "endpoint": f"{ws}/agents/{slug}/ws?org={org}",
        "connection_mode": "direct",
        "authorization_header": f"Bearer {token}",
        "initialization_json": {"simulation_id": "{{simulation_id}}"},
        "send_sample_rate_hertz": 16000,
        # The agent's Pipecat output runs at 48 kHz.
        "receive_sample_rate_hertz": 48000,
        "receive_audio_channels": 1,
        "handshake_ready_message_type": "",
        "handshake_requires_session_id": False,
        "send_audio_template": '{"action":"audio_message","payload":{},"audio_bytes":"{{audio_data}}","sender":"USER"}',
        "message_type_path": "action",
        "audio_message_type_value": "audio_message",
        "audio_data_path": "audio_bytes",
        "audio_encoding": "pcm",
    }


def twin_name(source_name: str) -> str:
    for marker in (" (Vapi)", " (LiveKit)", " (Pipecat)"):
        if marker in source_name:
            return source_name.replace(marker, " (Cloudflare)")
    return f"{source_name} (Cloudflare)"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--manifest", type=Path, default=Path("coval-agents.json"))
    parser.add_argument("--only")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())
    token = os.environ["AGENT_AUTH_TOKEN"]
    for twin in manifest["twins"]:
        if args.only and twin["org"] != args.only:
            continue
        key = coval_key(twin["org"])
        source = {"display_name": twin.get("display_name") or twin["slug"]}
        if twin.get("source_agent_id"):
            source = call(key, "GET", f"/agents/{twin['source_agent_id']}")
            source = source.get("agent", source)
        source_meta = source.get("metadata") or {}
        metadata = {
            **websocket_metadata(args.base_url, twin["slug"], twin["org"], token),
            "framework": "Pipecat on Cloudflare Workers (Durable Object per call)",
            "llm": "Workers AI @cf/openai/gpt-oss-120b",
            "stt": "Workers AI @cf/deepgram/flux",
            "tts": "Workers AI @cf/deepgram/aura-2-en",
            "vertical": source_meta.get("vertical"),
            "brand": source_meta.get("brand"),
            "failure_mode": source_meta.get("failure_mode"),
            "agent_persona": source_meta.get("agent_persona"),
            "twin_of_agent_id": twin.get("source_agent_id"),
            "source_repo": "coval-ai/coval-examples voice-agents/cloudflare",
        }
        metadata = {k: v for k, v in metadata.items() if v is not None}
        body = {
            "display_name": twin_name(source["display_name"]),
            "model_type": "MODEL_TYPE_WEBSOCKET",
            "prompt": source.get("prompt") or "",
            "metadata": metadata,
            "metric_ids": source.get("metric_ids") or [],
            "tags": [t for t in source.get("tags") or [] if not t.startswith("stack:")]
            + ["stack:cloudflare"],
        }
        if args.dry_run:
            shown = {
                **body,
                "metadata": {**metadata, "authorization_header": "Bearer <redacted>"},
                "prompt": body["prompt"][:60],
            }
            print(twin["org"], json.dumps(shown)[:600])
            continue
        if twin.get("cloudflare_agent_id"):
            agent = call(key, "PATCH", f"/agents/{twin['cloudflare_agent_id']}", body)
        else:
            agent = call(key, "POST", "/agents", body)
        agent = agent.get("agent", agent)
        twin["cloudflare_agent_id"] = agent["id"]
        print(f"{twin['org']:18} {twin['slug']:16} {agent['id']}  {body['display_name']}")
        args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
