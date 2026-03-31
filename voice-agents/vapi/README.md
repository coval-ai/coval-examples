# Vapi Voice Agent — Coval OTel Bridge

A FastAPI webhook server that receives Vapi webhook events and builds OpenTelemetry spans post-hoc from the end-of-call-report payload, then exports them to Coval.

Unlike real-time agents (Pipecat, LiveKit), this approach requires no changes to your Vapi assistant configuration — the server intercepts call lifecycle events and reconstructs the span tree from Vapi's artifact messages.

## How it works

Vapi sends webhook events to this server throughout a call. The server:

1. Captures the Coval simulation ID from any event (stored in `assistantOverrides.variableValues["coval-simulation-id"]`) and caches it by call ID.
2. Responds to `assistant-request` with the assistant ID, and to `tool-calls` with mock tool results.
3. On `end-of-call-report`, builds a full OTel span tree from the call artifact and exports it to Coval.

## Simulation ID flow

Coval injects `X-Coval-Simulation-Id` as a SIP header when it calls your Vapi phone number. Vapi auto-lowercases `X-` headers and surfaces the value in:

```
message.call.assistantOverrides.variableValues["coval-simulation-id"]
```

This key is present in all webhook event types, so the server captures it on the first event it sees for a given call.

## OTel span schema

```
conversation (root)   call.ended_reason, call.duration_seconds
  stt                 stt.transcription, metrics.ttfb, stt.confidence (synthetic 0.95)
    stt.provider.vapi stt.providerName, stt.confidence, metrics.ttfb
  llm                 metrics.ttfb, llm.finish_reason
  tts                 metrics.ttfb
  tool_call           tool.name, tool.call_id, tool.arguments
  tool_call_result    tool.name, tool.call_id, tool.result
```

`stt.confidence` is synthetic (0.95). Vapi does not expose per-utterance ASR confidence in the end-of-call-report webhook. Real-time agents like Pipecat can emit the true Deepgram value.

`llm.finish_reason` is derived from the message sequence: `"tool_calls"` when an LLM turn is immediately followed by a `tool_calls` message, otherwise `"stop"`.

## Prerequisites

- Python 3.12+
- Vapi account with a phone number and assistant configured
- Coval account

## Environment variables

| Variable | Description |
|----------|-------------|
| `COVAL_API_KEY` | Your Coval API key |
| `VAPI_ASSISTANT_ID` | The Vapi assistant ID to return on `assistant-request` events |

## Local development

```bash
pip install -r requirements.txt
COVAL_API_KEY=... VAPI_ASSISTANT_ID=... uvicorn server:app --port 8000 --reload
```

Expose your local server with ngrok or a similar tool, then set the webhook URL in Vapi.

## Deploy to Fly.io

```bash
# Set secrets
fly secrets set COVAL_API_KEY=... VAPI_ASSISTANT_ID=...

# Deploy
fly deploy
```

After deploying, configure two webhook URLs in Vapi to point at `https://<your-app>.fly.dev/webhook`:

1. The phone number's **Server URL** — handles `assistant-request` events (returns the assistant ID for inbound calls)
2. The assistant's **Server URL** — handles `tool-calls` and `end-of-call-report` events

## Vapi webhook setup

In the Vapi dashboard:

- Phone number settings → Server URL: `https://<your-app>.fly.dev/webhook`
- Assistant settings → Server URL: `https://<your-app>.fly.dev/webhook`

Both URLs can be the same endpoint — the server routes by `message.type`.
