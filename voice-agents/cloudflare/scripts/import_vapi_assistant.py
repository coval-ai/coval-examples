"""Copy a live Vapi assistant's prompt, greeting and tool schemas into src/verticals/data/<name>.json.

The Vapi assistant is the source of truth for what the demo agent says; tool *behaviour*
is ported by hand from the agent's webhook repo.

  python3 scripts/import_vapi_assistant.py <vapi-assistant-id> <name>

Reads VAPI_API_KEY, or the key from ~/.vapi-cli.yaml (the Vapi CLI login).
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path


def vapi_key() -> str:
    if os.environ.get("VAPI_API_KEY"):
        return os.environ["VAPI_API_KEY"]
    text = Path(os.path.expanduser("~/.vapi-cli.yaml")).read_text()
    keys = re.findall(r'api_?key:\s*"?([A-Za-z0-9-]{20,})"?', text, re.I)
    if not keys:
        sys.exit("no Vapi API key in ~/.vapi-cli.yaml")
    return keys[-1]


def main() -> None:
    assistant_id, slug = sys.argv[1], sys.argv[2]
    request = urllib.request.Request(
        f"https://api.vapi.ai/assistant/{assistant_id}",
        headers={"Authorization": f"Bearer {vapi_key()}", "User-Agent": "coval-import"},
    )
    assistant = json.load(urllib.request.urlopen(request))
    model = assistant.get("model") or {}
    system = next(
        (m["content"] for m in model.get("messages") or [] if m.get("role") == "system"), ""
    )
    tools = [
        {"type": "function", "function": tool["function"]}
        for tool in model.get("tools") or []
        if tool.get("type") == "function" and tool.get("function")
    ]
    data = {
        "source": {
            "ported_from": f"Vapi assistant {assistant.get('name')}",
            "vapi_llm": f"{model.get('provider')}/{model.get('model')}",
            "vapi_voice": assistant.get("voice"),
        },
        "systemPrompt": system,
        "firstMessage": assistant.get("firstMessage") or "",
        "tools": tools,
        "maxDurationSeconds": assistant.get("maxDurationSeconds") or 600,
        "silenceTimeoutSeconds": assistant.get("silenceTimeoutSeconds") or 30,
    }
    out = Path(__file__).resolve().parent.parent / "src" / "verticals" / "data" / f"{slug}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out} ({len(system)} prompt chars, {len(tools)} tools)")


if __name__ == "__main__":
    main()
