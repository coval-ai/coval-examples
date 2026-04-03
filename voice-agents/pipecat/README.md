# Pipecat Voice Agent — Coval OTel Tracing

A real-time Pipecat Cloud voice agent that emits OpenTelemetry spans during live calls. Spans are exported to Coval as each STT/LLM/TTS stage completes, giving Coval sub-second visibility into your pipeline's latency.

## How it works

The agent uses custom `FrameProcessor` subclasses inserted into the Pipecat pipeline to observe timing frames and emit spans:

- `STTSpanProcessor` — emits `stt` and `stt.provider.deepgram` spans on each final transcription
- `LLMPreSpanProcessor` + `LLMPostSpanProcessor` — bracket the LLM service to compute TTFB and detect tool calls
- `TTSSpanProcessor` — emits `tts` spans around each synthesis

A `DynamicCovalExporter` buffers spans until the simulation ID is known, then flushes the buffer and exports subsequent spans in real time.

## Simulation ID flow

Coval calls your Pipecat Cloud agent's SIP endpoint. The simulation ID is passed in the PCC start request body:

```
body.coval.simulationOutputId
```

When this field is present, `set_simulation_id()` is called immediately and all buffered spans are flushed. As a fallback, the agent also checks `body.dialin_settings.sip_headers["X-Coval-Simulation-Id"]` and the `COVAL_SIMULATION_ID` env var (useful for local testing).

## OTel span schema

```
stt                     stt.transcription, metrics.ttfb, stt.confidence
  stt.provider.deepgram stt.providerName, stt.confidence, metrics.ttfb
llm                     metrics.ttfb, llm.finish_reason
tts                     metrics.ttfb
```

`stt.confidence` is the real Deepgram value from `frame.result.channel.alternatives[0].confidence`, with a fallback to synthetic 0.95.

`llm.finish_reason` is `"tool_calls"` when `FunctionCallsStartedFrame` is observed before `LLMFullResponseEndFrame`, otherwise `"stop"`. Only these two values are possible — other finish reasons (`"length"`, `"content_filter"`) are not surfaced by Pipecat's frame events and would appear as `"stop"`.

## Prerequisites

- Python 3.12+
- Pipecat Cloud account (`pcc` CLI installed)
- Deepgram API key (STT + TTS)
- OpenAI API key
- Coval account

## Secrets

Set all secrets in Pipecat Cloud before deploying:

```bash
pcc secrets set your-pipecat-secrets \
  COVAL_API_KEY=... \
  OPENAI_API_KEY=... \
  DEEPGRAM_API_KEY=... \
  DAILY_API_KEY=...
```

Update `secret_set` in `pcc-deploy.toml` to match the name you used above.

## Build and deploy

```bash
# Build the Docker image
docker buildx build --platform linux/arm64 --load \
  -t YOUR_DOCKER_HUB_USERNAME/coval-pipecat-agent:latest .

# Push to Docker Hub
docker push YOUR_DOCKER_HUB_USERNAME/coval-pipecat-agent:latest

# Deploy to Pipecat Cloud
pcc deploy --force --no-credentials
```

Update `image` and `agent_name` in `pcc-deploy.toml` before deploying.

## Local development

For local testing, set `DAILY_ROOM_URL` (and optionally `COVAL_SIMULATION_ID`) in `.env.local`, then:

```bash
python bot.py
```

The agent joins the Daily room directly. If `COVAL_SIMULATION_ID` is set, tracing activates immediately without waiting for a SIP dial-in.

## Cold start note

`min_agents = 1` in `pcc-deploy.toml` keeps one warm instance always running. With `min_agents = 0` (scale-to-zero), cold start takes ~10 minutes — longer than Coval's simulator idle timeout, which causes simulations to fail.
