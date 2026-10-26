# Cloudflare Workers Voice Agents

Voice agents that run entirely on Cloudflare and connect to Coval over Coval's **WebSocket (Voice)** agent type, with no phone numbers. This is the stack behind Coval's demo organizations. Each call runs a [Pipecat](https://docs.pipecat.ai/) pipeline inside its own [Durable Object](https://developers.cloudflare.com/durable-objects/):

```text
Coval simulator ⇄ WebSocket (JSON audio) ⇄ Durable Object
    caller audio → Flux (speech-to-text + turn detection) → LLM (+ tools) → Aura (speech) → agent audio
                                                                    ↓
                                                    OTel spans → POST /v1/traces
```

| Piece | Workers AI model |
|-------|------------------|
| Speech-to-text and end-of-turn detection | `@cf/deepgram/flux` |
| Language model with tool calling | `@cf/openai/gpt-oss-120b` |
| Text-to-speech | `@cf/deepgram/aura-2-en` |

The voice loop (turn handling, interruptions, the conversation history the caller actually heard, tool deadlines) comes from Cloudflare's [support-voice-agent](https://github.com/korinne/support-voice-agent) template, MIT licensed; see [NOTICE.md](NOTICE.md). This example replaces that template's browser/WebRTC transport with a WebSocket Coval connects to directly, and adds Coval trace export.

## Agents

One Worker serves every agent at `/agents/<slug>/ws`. Each agent is a module in [`src/verticals/`](src/verticals): a prompt, greeting and tool schemas copied from a live assistant ([`data/`](src/verticals/data)), plus mock tool behaviour with deliberate failure modes, so Coval's metrics have something to catch.

| Slug | Agent | Deliberate failure modes |
|------|-------|--------------------------|
| `retail` | Avery, Bronmart | Hot SKUs report low-confidence stock (phantom inventory); inventory service outages |
| `banking` | Cassidy, Bronchase Bank | Core-banking outages on account lookup; mistranscribed account numbers |
| `healthcare` | Nurse Bronancy, Brontemoor Medical | Lab results service always unavailable |
| `insurance` | Morgan, Bronstate Auto Insurance | Roadside dispatch is slow and sometimes times out |
| `restaurant` | Tony, Bron and Sons Italian Eats | Reservation write failures; silently shifted booking dates |
| `hospitality` | Remy, Bronwood Hotels & Resorts | Silent room downgrades; premium sell-outs; booking write failures |
| `transport` | Riley, Bronride | Fare breakdown service always unavailable |
| `ibm-cvs` | Ava, CVS Pharmacy | Insurance copay lookup always unavailable |
| `nurse-bronancy` | Nurse Bronancy, Wellness Alliance Medical Group | None (no tools) |

## Coval connection

Create one Coval agent per slug with model type **WebSocket (Voice)**, connection mode **direct**, and:

| Setting | Value |
|---------|-------|
| `endpoint` | `wss://<worker-host>/agents/<slug>/ws?org=<org>` |
| `authorization_header` | `Bearer <AGENT_AUTH_TOKEN>` |
| `initialization_json` | `{"simulation_id": "{{simulation_id}}"}` |
| `send_audio_template` | `{"action":"audio_message","payload":{},"audio_bytes":"{{audio_data}}","sender":"USER"}` |
| `message_type_path` / `audio_message_type_value` / `audio_data_path` | `action` / `audio_message` / `audio_bytes` |
| `send_sample_rate_hertz` / `receive_sample_rate_hertz` | `16000` / `48000` |
| `receive_audio_channels` | `1` |
| `handshake_ready_message_type` | empty (no ready message) |

[`scripts/coval_agents.py`](scripts/coval_agents.py) creates these agents from a manifest (see [`coval-agents.example.json`](coval-agents.example.json)), optionally copying metrics and prompt from an agent each one replaces.

The agent speaks first, so use personas whose conversation initiator is the agent.

## Traces

Coval puts the simulation ID in the first WebSocket message, so every span is correlated exactly, with no caller-number matching or pre-call webhook. When the call ends the agent posts OTLP/JSON to `https://api.coval.dev/v1/traces` with `X-Simulation-Id`, using the key for `?org=` from the `COVAL_API_KEYS` secret.

| Span | Key attributes |
|------|----------------|
| `conversation` (root) | `coval.simulation_id`, `call.ended_reason`, `tool.call.count`, `tool.failure.count`, `workflow.completed`, `workflow.dependency_blocked`, plus each agent's failure-mode counts |
| `turn` | `turn.role`, `turn.user` / `turn.assistant`, `turn.index` |
| `stt` | `transcript`, `metrics.ttfb` (endpointing delay after the last word) |
| `llm` | `metrics.ttfb`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `llm.finish_reason` |
| `tts` | `metrics.ttfb` (time to first audio), `tts.text_length` |
| `llm_tool_call` | `function.name`, `function.arguments` (sensitive keys redacted), `tool.error`, `tool.dependency_unavailable`, `tool.latency_ms` |
| `<vertical>.workflow.<tool>` | `workflow.step`, `tool.error` |

All timings are measured, not synthetic. For a streamed text reply, `llm` time to first byte is the first token; for a reply that only calls tools, it is the whole reply.

## Deploy

You need a Cloudflare account on the Workers Paid plan, Node.js 22+, Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
npm install
npx wrangler login
npm run setup          # installs the Python packages that ship with the Worker
npm run deploy
npx wrangler secret put AGENT_AUTH_TOKEN
npx wrangler secret put COVAL_API_KEYS   # {"<org>": "<coval api key>", ...}
```

Then create the Coval agents:

```bash
AGENT_AUTH_TOKEN=... COVAL_API_KEYS='{"my-retail-org":"..."}' \
  python3 scripts/coval_agents.py https://coval-demo-voice-agents.<subdomain>.workers.dev --manifest coval-agents.json
```

## Try a call without Coval

[`scripts/fake_coval_call.py`](scripts/fake_coval_call.py) speaks scripted caller lines over the same protocol Coval uses and reports how fast the agent answers:

```bash
CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=... AGENT_AUTH_TOKEN=... \
  uv run python scripts/fake_coval_call.py wss://coval-demo-voice-agents.<subdomain>.workers.dev retail \
  "Hi, is the B M twenty two forty one TV in stock?" "Okay, thanks, bye."
```

## Compare against another agent

[`scripts/parity.py`](scripts/parity.py) launches the same seeded test cases, persona and metrics against an existing agent and its Cloudflare replacement, then compares the results metric by metric:

```bash
COVAL_API_KEYS=... python3 scripts/parity.py launch --manifest coval-agents.json --cases 6
COVAL_API_KEYS=... python3 scripts/parity.py report scripts/out/parity-<stamp>.json
```

## Add an agent

1. Import a Vapi assistant's prompt and tools (or write `src/verticals/data/<name>.json` by hand): `python3 scripts/import_vapi_assistant.py <assistant-id> <name>`.
2. Write `src/verticals/<name>.py` with its handlers, following [`retail.py`](src/verticals/retail.py). Handlers take `(arguments, state)` and return a dict; an `"error"` key marks a failed call, and `SERVICE_UNAVAILABLE` or `TIMEOUT` marks a dependency outage.
3. Add the module to [`src/verticals/catalog.py`](src/verticals/catalog.py), then `npm run deploy`.

Every agent also gets an `end_call` tool: the agent says its goodbye line and hangs up once it has played.

## Limits

- English only: Flux turn detection and the Aura voices are English (Aura also has Spanish).
- Replies stream from the model and are spoken a sentence at a time. The Workers build of Pipecat has no sentence tokenizer, so `services/llm.py` cuts sentences itself.
- A turn that needs a tool waits for two model rounds before speaking; there are no fillers, matching the Vapi agents these replace.
- Workers AI latency varies by hour. If `gpt-oss-120b` sends nothing within 2 seconds, the reply is requested from `gpt-oss-20b` instead (`FALLBACK_LLM_MODEL` in `src/agent/config.py`); the `llm` span's `llm.model` records which model answered.
- The bundled Pipecat build supports hosted services and Flux turn detection only, not local VAD or Smart Turn. See [vendor/README.md](vendor/README.md).
- A call ends after its agent's maximum duration (10 minutes by default) or 30 seconds of silence.

## Tests

```bash
uv sync
uv run pytest -q
uv run ruff check src scripts tests
```

Python tests can't prove the code runs on Workers; verify a deployed call with `scripts/fake_coval_call.py`.
