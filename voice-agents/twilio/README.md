# Twilio ConversationRelay Agent — Coval OTel Bridge

A FastAPI webhook server that handles Twilio Programmable Voice calls via ConversationRelay, calls OpenAI for responses, and builds OpenTelemetry spans post-hoc at call end, then exports them to Coval.

Spans are reconstructed from a turn log accumulated during the call — the same post-hoc approach used by the Vapi example, rather than emitting spans inline as Pipecat and LiveKit do. LLM TTFB is real (measured from the moment Twilio sends the transcription to the moment the first token is sent back). STT and TTS TTFBs are synthetic — Twilio handles those providers internally without exposing per-utterance timing.

## How it works

1. A phone call arrives at your Twilio number
2. Twilio calls `POST /webhook` — the server returns TwiML that opens a ConversationRelay WebSocket to `/ws`
3. Twilio sends a `"setup"` event with the call SID — the server pops the pending simulation ID from its queue
4. The caller speaks — Twilio transcribes the audio and sends a `"prompt"` event with the transcribed text
5. The server calls OpenAI with streaming, accumulates tool calls, executes them if needed (agentic loop), and streams tokens back to Twilio
6. Each turn (user/assistant/tool) is appended to a turn log with real timestamps
7. When the call ends and the WebSocket closes, the server builds OTLP spans from the turn log and POSTs them to `https://api.coval.dev/v1/traces`

## Simulation ID flow

Twilio Programmable Voice routes calls over PSTN, which strips all custom SIP headers — including `X-Coval-Simulation-Id`. To correlate traces with Coval simulations, configure `pre_call_webhook_url` on your Coval agent:

```json
{
  "pre_call_webhook_url": "https://your-agent.example.com/register-simulation",
  "pre_call_webhook_headers": {"x-api-key": "<COVAL_API_KEY>"}
}
```

Before placing each simulation call, Coval will POST `{"simulation_output_id": "..."}` to `/register-simulation`. The server queues these IDs FIFO and pops the oldest one when the next call arrives. IDs expire after 5 minutes if unused.

See the [Coval docs](https://docs.coval.dev/guides/simulations/twilio-conversationrelay) for full setup instructions.

## OTel span schema

```
conversation (root)   call.duration_seconds
  stt                 stt.transcription, metrics.ttfb (synthetic), stt.confidence (synthetic 0.95)
    stt.provider.twilio  stt.providerName, stt.confidence, metrics.ttfb
    stt.provider_selection
  llm                 metrics.ttfb (real), llm.finish_reason
  tts                 metrics.ttfb (synthetic 0.1s)
  tool_call           tool.name, tool.call_id, tool.arguments
  tool_call_result    tool.name, tool.call_id, tool.result
```

`metrics.ttfb` on `llm` is real — measured from when the "prompt" event arrives to when the first text token is sent to Twilio.

`metrics.ttfb` on `stt` and `tts` is **synthetic** — a meaningful limitation. Twilio manages those providers internally and does not expose per-utterance timing to the application. Values are estimated from turn durations rather than measured. Do not use these for latency analysis.

`stt.confidence` is **synthetic** (0.95) — Twilio's ConversationRelay does not expose per-utterance ASR confidence scores.

`llm.finish_reason` is inferred from the turn log: `"tool_calls"` if the next turn is a tool call, otherwise `"stop"`. Only these two values are possible — other OpenAI finish reasons (`"length"`, `"content_filter"`) are not captured and would appear as `"stop"`.

## Prerequisites

- Python 3.12+
- Twilio account with a phone number and ConversationRelay enabled
- Coval account with `pre_call_webhook_url` configured on the agent (see [docs](https://docs.coval.dev/guides/simulations/twilio-conversationrelay))

## Environment variables

| Variable | Description | Required |
|----------|-------------|----------|
| `OPENAI_API_KEY` | OpenAI API key | Yes |
| `COVAL_API_KEY` | Coval API key — used to authenticate `/register-simulation` and export traces | Yes |
| `TWILIO_ACCOUNT_SID` | Twilio account SID | Yes (for webhook validation, optional in dev) |
| `TWILIO_AUTH_TOKEN` | Twilio auth token | Yes (for webhook validation, optional in dev) |

## Local development

```bash
pip install -r requirements.txt
OPENAI_API_KEY=... COVAL_API_KEY=... uvicorn server:app --port 8000 --reload
```

Expose your local server publicly (Twilio requires a public HTTPS URL):

```bash
ngrok http 8000
```

Then set your Twilio phone number's Voice webhook to `https://<ngrok-id>.ngrok.io/webhook`.

## Deploy to Fly.io

```bash
# Create the app (first time only — choose a unique name)
fly launch --no-deploy

# Set secrets
fly secrets set \
  OPENAI_API_KEY=... \
  COVAL_API_KEY=... \
  TWILIO_ACCOUNT_SID=... \
  TWILIO_AUTH_TOKEN=...

# Deploy
fly deploy
```

## Twilio webhook setup

In the [Twilio console](https://console.twilio.com/), navigate to your phone number and set:

- **Voice webhook (A call comes in):** `https://<your-deployed-server>/webhook` — POST

## Coval agent setup

In the Coval dashboard, open your agent's settings and add to the agent metadata:

```json
{
  "pre_call_webhook_url": "https://<your-deployed-server>/register-simulation",
  "pre_call_webhook_headers": {"x-api-key": "<COVAL_API_KEY>"}
}
```

This tells Coval to notify your agent before each simulation call with the simulation output ID, enabling trace correlation despite PSTN stripping SIP headers.
