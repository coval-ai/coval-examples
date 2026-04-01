# LiveKit Voice Agent — Coval OTel Tracing

A real-time LiveKit Cloud voice agent that emits OpenTelemetry spans during live calls via SIP dispatch. Spans are exported to Coval as each STT/LLM/TTS stage completes using LiveKit's `metrics_collected` session events.

## How it works

The agent registers event handlers on the `AgentSession` to observe timing metrics:

- `user_input_transcribed` — buffers the final transcript for correlation with STT metrics
- `metrics_collected` — emits `stt`, `llm`, and `tts` spans from `STTMetrics`, `LLMMetrics`, and `TTSMetrics` events
- `function_tools_executed` — flushes the pending LLM span with `llm.finish_reason = "tool_calls"`

LLM spans are deferred: each `LLMMetrics` event buffers a pending span that is flushed either when `TTSMetrics` fires (finish_reason `"stop"`) or when `function_tools_executed` fires (finish_reason `"tool_calls"`).

A `DynamicCovalExporter` buffers all spans until the simulation ID is known, then flushes them.

## Simulation ID flow

Coval calls your LiveKit SIP trunk. LiveKit surfaces the `X-Coval-Simulation-Id` SIP header as a participant attribute:

```
sip.h.X-Coval-Simulation-Id
```

The agent checks this attribute when a SIP participant joins the room (both from `ctx.room.remote_participants` at startup and via the `participant_connected` event). It also checks at the audio input stage via the `noise_cancellation` callback, which provides an additional extraction point.

Fallback: set `COVAL_SIMULATION_ID` in the environment for local dev or non-SIP testing.

## OTel span schema

```
stt                     stt.transcription, metrics.ttfb, stt.confidence (synthetic 0.95)
  stt.provider.deepgram stt.providerName, stt.confidence, metrics.ttfb
llm                     metrics.ttfb, llm.finish_reason,
                        gen_ai.usage.input_tokens, gen_ai.usage.output_tokens
tts                     metrics.ttfb
```

`stt.confidence` is synthetic (0.95). LiveKit's metrics API does not expose per-utterance ASR confidence. To get real confidence scores you would need to hook directly into the Deepgram WebSocket response.

## Prerequisites

- Python 3.12+, `uv` package manager
- LiveKit Cloud account with a SIP trunk and dispatch rule configured
- Deepgram API key (used for both STT `nova-3` and TTS `aura-asteria-en`)
- OpenAI API key
- Coval account

## Environment variables

| Variable | Description |
|----------|-------------|
| `COVAL_API_KEY` | Your Coval API key |
| `OPENAI_API_KEY` | OpenAI API key for the LLM |
| `DEEPGRAM_API_KEY` | Deepgram API key for STT and TTS |
| `LIVEKIT_URL` | Your LiveKit Cloud WebSocket URL (`wss://...`) |
| `LIVEKIT_API_KEY` | LiveKit API key |
| `LIVEKIT_API_SECRET` | LiveKit API secret |

## Local development

```bash
cp .env.example .env   # fill in the variables above
uv run agent.py dev
```

The `dev` command connects the agent to LiveKit Cloud and waits for dispatch jobs. You can test by calling the SIP number or by dispatching a job manually via the LiveKit CLI.

## Deploy to LiveKit Cloud

Update `livekit.toml` with your project subdomain and Docker image, then:

```bash
lk agent deploy
```

On the first deploy, `lk agent deploy` will populate the `[agent]` section with the assigned agent ID. You can then remove the commented-out `id` placeholder.

## SIP trunk setup

The agent uses LiveKit's SIP dispatch model. You need:

1. A SIP trunk with `allowed_addresses` including both `0.0.0.0/0` (IPv4) and `::/0` (IPv6)
2. A dispatch rule that routes inbound calls to `agent_name = "livekit-voice-agent"`

Point the Coval agent configuration at your SIP URI (`sip:agent@<subdomain>.sip.livekit.cloud`).

## Cold start note

LiveKit Cloud scales down containers after inactivity. If cold start latency is a problem, configure `min_instances` in your LiveKit Cloud project settings to keep a warm instance running.
