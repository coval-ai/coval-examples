# Voice Agent OTel Examples

Three reference implementations for adding OpenTelemetry tracing to voice agents connected to Coval. Each agent emits structured spans that Coval ingests to power latency metrics, STT accuracy scoring, and conversation analytics.

## Implementations

| Framework | Approach | Deployment | Simulation ID source |
|-----------|----------|------------|----------------------|
| [Vapi](./vapi/) | Webhook bridge — builds spans post-hoc from end-of-call-report | Fly.io HTTP server | `assistantOverrides.variableValues["coval-simulation-id"]` |
| [Pipecat](./pipecat/) | Real-time — emits spans during the live call via frame processors | Pipecat Cloud | `body.coval.simulationOutputId` (PCC start request) |
| [LiveKit](./livekit/) | Real-time — emits spans during the live call via session events | LiveKit Cloud + SIP dispatch | `sip.h.X-Coval-Simulation-Id` participant attribute |
| [Twilio](./twilio/) | Webhook server — builds spans post-hoc at call end from ConversationRelay events | Fly.io HTTP server | Pre-call webhook (`POST /register-simulation`) via `pre_call_webhook_url` agent config |

## When to use each

**Vapi** — you are already on Vapi and want the simplest possible integration. No pipeline changes required; spans are reconstructed from the webhook payload after the call ends. The trade-off is that stt.confidence is synthetic (Vapi does not expose per-utterance ASR confidence).

**Pipecat** — you want real-time spans and are using or open to Pipecat Cloud. Pipecat's frame processor model makes it straightforward to instrument STT, LLM, and TTS stages inline. Provides real Deepgram confidence values.

**LiveKit** — you want real-time spans and prefer LiveKit's SIP dispatch model with the LiveKit Agents SDK. Uses LiveKit's metrics events rather than frame processors.

**Twilio** — you are using Twilio Programmable Voice with ConversationRelay and want the simplest integration. Spans are built post-hoc from a turn log at call end. LLM TTFB is real; STT and TTS TTFBs are synthetic (Twilio handles those providers internally). Because Twilio routes over PSTN and strips SIP headers, trace correlation uses a pre-call webhook instead of `X-Coval-Simulation-Id`.

## Common setup

All four agents require:

```bash
COVAL_API_KEY=<your Coval API key>
```

Coval injects a simulation ID into each call so it can correlate spans with the simulation run. The mechanism differs by framework (see the table above). Vapi, Pipecat, and LiveKit receive the ID via SIP header or request payload. The Twilio agent uses a pre-call webhook (`pre_call_webhook_url` on the Coval agent) because PSTN strips SIP headers. All agents ultimately call `POST https://api.coval.dev/v1/traces` with an `X-Simulation-Id` header.

## OTel span schema

All three agents emit spans following the same Coval schema:

| Span name | Key attributes |
|-----------|----------------|
| `stt` | `stt.transcription`, `metrics.ttfb`, `stt.confidence` |
| `stt.provider.<name>` | `stt.providerName`, `stt.confidence`, `metrics.ttfb` |
| `llm` | `metrics.ttfb`, `llm.finish_reason` |
| `tts` | `metrics.ttfb` |

Vapi and Twilio also emit `conversation` (root), `tool_call`, and `tool_call_result` spans.

`stt.confidence` is the real Deepgram confidence value in Pipecat (from `frame.result.channel.alternatives[0].confidence`) and synthetic 0.95 in Vapi, LiveKit, and Twilio.

`metrics.ttfb` on `llm` is real in Twilio (measured wall-clock from prompt receipt to first token sent). STT and TTS TTFBs in Twilio are synthetic — Twilio manages those providers internally.

`llm.finish_reason` is `"tool_calls"` when the LLM invoked tools during the turn, otherwise `"stop"`.

## Coval docs

See the Coval documentation for how to configure your agent in the Coval dashboard and set up a SIP trunk or webhook URL to point at your deployment.
