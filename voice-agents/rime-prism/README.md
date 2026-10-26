# Rime Prism voice agent with Coval tracing

An authenticated WebSocket bridge between Coval's direct voice transport and Rime
Prism (speech-to-speech), with synthetic account tools and correlated OTLP JSON
traces. The business fixture is fictional Cedar Demo Lending. It does not connect
to a lending system. You need a Rime API key with Prism access.

## Run locally

Requires Python 3.12 and uv. From this directory:

```sh
uv sync --frozen
uv run ruff check .
uv run pytest -q
uv run uvicorn server:app --host 127.0.0.1 --port 8877 --no-access-log
```

Provide `RIME_API_KEY` and `BRIDGE_TOKEN` through the process environment. An empty
bridge token fails closed. For trace export also provide `COVAL_API_KEY`,
`COVAL_TRACES_ENDPOINT` (`https://api.coval.dev/v1/traces`), and optionally
`SOURCE_REVISION` and `DEPLOYMENT_ENVIRONMENT`. Keep keys out of shell history,
source and request logs.

`smoke.py` replays a 16 kHz PCM16 question through a running bridge with real
playback pacing: `BRIDGE_TOKEN=... uv run python smoke.py question.pcm`.

## Protocol

1. Connect to `/ws` with `Authorization: Bearer <BRIDGE_TOKEN>`.
2. Send `{"simulation_id":"<actual Coval simulation ID>"}`. The bridge waits for
   Prism's session update acknowledgement, then returns `session_ready` and begins
   a greeting.
3. Send `{"type":"audio_chunk","data":"<base64 PCM16 mono at 16000 Hz>"}`.
   The bridge sends 40 ms chunks and silence to Prism continuously during replies,
   on an absolute schedule so caller audio does not drift behind real time.
4. Receive the same audio envelope with PCM16 mono at **24000 Hz**. Pace playback
   at that rate. Echo every `mark` message after the preceding audio has played.
5. On `clear`, discard queued playback and echo the pending marks as required by
   the Coval transport. The bridge invalidates those old marks before sending
   clear, so they do not acknowledge audio the caller never heard.

The bridge translates playback acknowledgements into Prism playback-drained
events. Generation completion is distinct from playback completion. Tool results
continue only after both result acknowledgement and the parent response's
completion; a newer caller turn suppresses stale continuation.

Configure the Coval agent as `MODEL_TYPE_WEBSOCKET`, direct connection, PCM,
16000 Hz send / 24000 Hz receive / one channel. Put the WSS URL in
`metadata.endpoint`, authorization in `metadata.authorization_header`, and use:

```json
{
  "initialization_json": {"simulation_id": "{{simulation_id}}"},
  "message_type_path": "type",
  "audio_message_type_value": "audio_chunk",
  "audio_data_path": "data",
  "send_audio_template": "{\"type\":\"audio_chunk\",\"data\":\"{{audio_data}}\"}",
  "handshake_ready_message_type": "session_ready",
  "handshake_requires_session_id": true,
  "marks_enabled": true,
  "speech_activity_enabled": true,
  "tracing_enabled": true
}
```

## Fixtures and traces

`DEMO-1042` returns Alex Sample's synthetic $125 payment due November 1, 2026.
`DEMO-500` deliberately returns `demo_service_unavailable`. Other codes fail.
The handoff requires the exact enum `consent: "confirmed"`, after explicit caller
consent, and returns synthetic ticket `DEMO-H1042`. No live transfer occurs.

Spans are `conversation`, `vad`, `prism.input_transcription`, `prism.response`,
`llm_tool_call` and `prism.error`. They carry the actual simulation ID, bridge
source revision, Rime session and response IDs, and Rime's upgrade `x-request-id`
(`rime.request_id`) for support reports. Cancelled replies export with status UNSET
and message `cancelled: <reason>`, which Coval's judges recognize as a cancellation
rather than an error. Tool spans include
arguments and results. These are client observations; the bridge does not invent
separate internal STT, LLM or TTS timings. Prism input transcripts are display
output rather than the model's reasoning input. Usage is unavailable.

Recommended metrics: a composite judge with `include_traces: true`, tool count,
end reason, audio latency, interruption rate, a custom trace average of
`prism.response` / `prism.speech_stop_to_first_audio_ms` (ms), and a custom trace
count of `prism.error`. Inspect actual error spans and cancelled responses even
when a run is marked completed or its scenario judge passes. The custom latency
starts at receipt of the speech-stop event and excludes the endpointer wait.

## Adapting the trace export

The tracing does not depend on the Cedar Demo fixture. To trace another Prism
agent the same way:

1. Get Coval's simulation ID for the call. This bridge reads it from the first
   WebSocket frame; Coval's tracing docs cover SIP, LiveKit and outbound calls.
   Create one `Trace` per call (`telemetry.py`).
2. Turn Prism events into spans as `server.py` does:
   - `input_audio_buffer.speech_started` to `speech_stopped`: `vad`
   - `conversation.item.input_audio_transcription.completed`: `prism.input_transcription`
   - `response.created` to `response.done`: `prism.response`, with status, reason
     and time to first audio
   - `response.function_call_arguments.done` and the result you return: `llm_tool_call`
   - `error`: `prism.error`, with the correlation ID
   - session start and end: `conversation`, with the Prism session ID
3. Export OTLP JSON to Coval's traces endpoint with the `x-api-key` and
   `X-Simulation-Id` headers.

Times are taken when the bridge receives each event, so they include network
time. Keep the Prism session ID on the spans so a call can be matched with Rime's
records.

## Deployment and limits

`fly.toml` runs one shared CPU / 512 MB machine that starts on demand and stops
when idle. Set your app name in `fly.toml`, import `RIME_API_KEY`, `BRIDGE_TOKEN`
and `COVAL_API_KEY` with `fly secrets import` through standard input (never in
`fly.toml`), and deploy from this directory with a committed revision:

```sh
fly deploy --ha=false --remote-only --env SOURCE_REVISION=<committed SHA>
```

`GET /health` reports the live source revision, prompt fingerprint and active
sessions. `GET /sessions/<simulation_id>` requires the same bearer token and
returns bounded diagnostic events without raw audio. Reports are held only in
memory and disappear after autostop/restart. Coval traces persist independently.

The bridge permits **three simultaneous sessions total**. Each session lasts at most
`BRIDGE_MAX_SESSION_SECONDS` (default 900, bounded to 60-3600); `/health` reports
the value in effect. Use a single evaluation batch with concurrency one or two;
parallel batches share that limit. Capacity rejection during the HTTP WebSocket
handshake currently appears as HTTP 403. It can resemble an auth failure.

This bridge was written against Prism's API before release `20261023a`. It does
not yet record the newer `version`, `usage`, `prism_end_reason` or
`prism_response_metadata` fields. It is a test fixture, not a production agent.
