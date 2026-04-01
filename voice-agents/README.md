# Voice Agent OTel Examples

Three reference implementations for adding OpenTelemetry tracing to voice agents connected to Coval. Each agent emits structured spans that Coval ingests to power latency metrics, STT accuracy scoring, and conversation analytics.

## Implementations

| Framework | Approach | Deployment | Simulation ID source |
|-----------|----------|------------|----------------------|
| [Vapi](./vapi/) | Webhook bridge — builds spans post-hoc from end-of-call-report | Fly.io HTTP server | `assistantOverrides.variableValues["coval-simulation-id"]` |
| [Pipecat](./pipecat/) | Real-time — emits spans during the live call via frame processors | Pipecat Cloud | `body.coval.simulationOutputId` (PCC start request) |
| [LiveKit](./livekit/) | Real-time — emits spans during the live call via session events | LiveKit Cloud + SIP dispatch | `sip.h.X-Coval-Simulation-Id` participant attribute |

## When to use each

**Vapi** — you are already on Vapi and want the simplest possible integration. No pipeline changes required; spans are reconstructed from the webhook payload after the call ends. The trade-off is that stt.confidence is synthetic (Vapi does not expose per-utterance ASR confidence).

**Pipecat** — you want real-time spans and are using or open to Pipecat Cloud. Pipecat's frame processor model makes it straightforward to instrument STT, LLM, and TTS stages inline. Provides real Deepgram confidence values.

**LiveKit** — you want real-time spans and prefer LiveKit's SIP dispatch model with the LiveKit Agents SDK. Uses LiveKit's metrics events rather than frame processors.

## Common setup

All three agents require:

```bash
COVAL_API_KEY=<your Coval API key>
```

Coval injects a simulation ID into each call so it can correlate spans with the simulation run. The mechanism differs by framework (see the table above), but all three ultimately call `POST https://api.coval.dev/v1/traces` with an `X-Simulation-Id` header.

## OTel span schema

All three agents emit spans following the same Coval schema:

| Span name | Key attributes |
|-----------|----------------|
| `stt` | `stt.transcription`, `metrics.ttfb`, `stt.confidence` |
| `stt.provider.<name>` | `stt.providerName`, `stt.confidence`, `metrics.ttfb` |
| `llm` | `metrics.ttfb`, `llm.finish_reason` |
| `tts` | `metrics.ttfb` |

The Vapi implementation also emits `conversation` (root), `tool_call`, and `tool_call_result` spans from the end-of-call-report artifact.

`stt.confidence` is the real Deepgram confidence value in Pipecat (from `frame.result.channel.alternatives[0].confidence`) and synthetic 0.95 in Vapi and LiveKit.

`llm.finish_reason` is `"tool_calls"` when the LLM invoked tools during the turn, otherwise `"stop"`.

## Coval docs

See the Coval documentation for how to configure your agent in the Coval dashboard and set up a SIP trunk or webhook URL to point at your deployment.
