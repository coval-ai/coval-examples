# Coval Agent SDK Voice Agent

Remy, the Bronwood Hotels & Resorts reservations agent from Coval's hospitality demo organization, running as an ordinary Python process and connected to Coval simulations with Coval's two SDKs:

- [**coval-agent-sdk**](https://github.com/coval-ai/coval-agent-sdk) carries each simulated call's audio. The agent opens an outbound WebSocket to Coval, so it needs no public endpoint, phone number or tunnel. The SDK also uploads the agent's OpenTelemetry traces for each conversation.
- [**coval-sdk**](https://github.com/coval-ai/coval-sdk) is the typed API client. It creates the Coval agent, launches runs from a run template and reads back each conversation's metric results.

The voice pipeline is [Pipecat](https://docs.pipecat.ai/) on the same Workers AI models as the [Cloudflare](../cloudflare/) version of this agent, with the same prompt, tools and deliberate failure modes. Only the hosting and the connection to Coval differ.

```text
                ┌──────────────────────── this process ────────────────────────┐
Coval simulator │ AudioConnection ─► Flux (STT + turns) ─► gpt-oss (+ tools)   │
  (caller)  ◄──►│  (coval-agent-sdk)  ◄──────────── Aura ◄──────────┘          │
                │  Pipecat OpenTelemetry spans ─► CovalTraceExporter ─► Coval  │
                └──────────────────────────────────────────────────────────────┘
```

| File | What it shows |
|------|---------------|
| [`remy/transport.py`](remy/transport.py) | A Pipecat transport over an SDK `AudioConnection` |
| [`remy/agent.py`](remy/agent.py) | One conversation: the pipeline, tools and trace spans |
| [`run_simulations.py`](run_simulations.py) | Launching runs and attaching to conversations with both SDKs |
| [`coval_setup.py`](coval_setup.py) | Creating an SDK WebSocket agent with coval-sdk |
| [`remy/hospitality.py`](remy/hospitality.py) | Remy's tools and failure modes |
| [`remy/workers_ai.py`](remy/workers_ai.py) | Pipecat services for Workers AI's Flux, gpt-oss and Aura |

## Set up

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), a Coval API key, and a Cloudflare account with an API token that has the **Workers AI** permission.

```bash
uv sync
cp env.example .env   # then fill it in
```

This example requires Agent SDK 0.1.3 or newer. It keeps individual connection failures isolated when using `CovalVoiceClient.run()` and reports abrupt socket loss as a failure. After updating an existing checkout, run `uv sync` and restart the agent process to load the new version.

In Coval, pick a workspace with a voice persona and a test set (and optionally a run template), then create the agent:

```bash
uv run coval_setup.py
```

This creates an agent with the **SDK WebSocket** connection type and prints its ID for `COVAL_AGENT_ID`. Pass `--copy-from <agent-id>` to copy the prompt and metrics from an existing agent, as we did from the Cloudflare twin.

## Run simulations

Launch a run from a run template. coval-sdk reads the template's persona, test set, metrics and sample size, launches the run against your SDK agent, and prints every conversation's metric results at the end:

```bash
uv run run_simulations.py --template <run-template-id>
uv run run_simulations.py --template <run-template-id> --sample 1   # a single test case
```

Or let the Agent SDK launch the run itself, for the whole test set with the agent's own metrics:

```bash
uv run run_simulations.py --simple --persona <persona-id> --test-set <test-set-id>
```

Either way Remy runs in this process and each conversation gets its own pipeline. Conversations run one at a time by default. `--concurrency` runs several at once, but in our tests at three concurrent conversations Coval's hosted relay sometimes closed all of them together with code 1013, so keep it low while the Agent SDK relay is in preview.

## How the SDKs fit together

**Launching and attaching.** `CovalVoiceClient.run()` is the shortest path: it launches a run and calls your handler with an `AudioConnection` for each conversation. Its `RunConfig` takes an agent, persona and test set but not metrics or a sample size, so for a template-shaped run this example launches with coval-sdk instead and attaches to each conversation itself:

```python
run = coval.runs.launch_run(CovalRunsAPILaunchRunRequest(
    agent_id=agent_id, persona_id=..., test_set_id=..., metric_ids=template.metric_ids,
    options=CovalRunsAPILaunchOptions(sub_sample_size=template.sub_sample_size, ...),
)).run

# For each of the run's simulations once it leaves the queue:
async with voice.connect(simulation_id) as connection:
    await run_conversation(connection, cloudflare)
```

**Audio.** Both directions are 16 kHz mono PCM16, so the pipeline runs at 16 kHz and never resamples. Three things matter when bridging an `AudioConnection` to a real-time pipeline:

- Drain `receive_audio()` without waiting on your own output. The SDK fails a conversation once 64 KiB of input is unread, so a loop that awaits `send_audio()` between reads (an echo, say) can fail as soon as the caller speaks while the agent does.
- Coval sends caller audio only while the simulated caller is speaking. Speech-to-text with turn detection measures pauses on a continuous stream, so [`transport.py`](remy/transport.py) feeds the pipeline from a buffer on a 20 ms clock and fills the gaps with silence, as a phone line would.
- `send_audio()` paces output to real time, so the output transport awaits it instead of sleeping.

**Ending the call.** The agent cannot hang up; Coval ends the conversation, usually when the simulated caller says goodbye. The handler must not return before then, or the SDK reports a failure. Remy's `end_call` tool says goodbye and leaves the line open.

**Traces.** Pipecat's built-in OpenTelemetry tracing names its root span `conversation` and sets `conversation.id` to the `conversation_id` you give the `PipelineTask`. Passing the simulation ID there is all `CovalTraceExporter` needs to upload each conversation's spans to the right simulation. Share one exporter between Pipecat's tracer provider and `CovalVoiceClient`:

```python
exporter = CovalTraceExporter()
setup_tracing(service_name="remy-agent-sdk", exporter=exporter)
voice = CovalVoiceClient(api_key=..., workspace_id=..., trace_exporter=exporter)
```

| Span | Key attributes |
|------|----------------|
| `conversation` (root) | `conversation.id` (the simulation ID) |
| `turn` | `turn.number`, `turn.duration_seconds`, `turn.user_bot_latency_seconds`, `turn.was_interrupted` |
| `stt` | `transcript`, `metrics.ttfb` |
| `llm` | `metrics.ttfb`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| `tts` | `metrics.ttfb`, `voice_id` |
| `llm_tool_call` | `function.name`, `function.arguments` (loyalty IDs redacted), `tool.error` (0 or 1, with the error code as the span status), `tool.dependency_unavailable`, `tool.latency_ms`, plus the failure-mode ground truth such as `reservation.room_downgraded` |
| `hospitality.workflow.<tool>` | `workflow.step` and the same outcome attributes, as a child of `llm_tool_call` |

## Remy's failure modes

The tools are mock backends that fail on purpose, so Coval's metrics have something to catch. The odds are the Cloudflare agent's:

| Tool | Failure |
|------|---------|
| `book_room` | The property-management write fails with `ROOM_INVENTORY_SYNC_FAILED` 25% of the time |
| `book_room`, `modify_reservation` | Otherwise the room is silently booked one tier down 40% of the time; Remy should catch it on the read-back |
| `check_availability` | A requested suite sells out 45% of the time |
| Any dated tool | A check-in date before today returns `DATE_IN_PAST` |
| `apply_loyalty` | Member numbers shorter than eight characters are not found |

## Differences from the Cloudflare agent

Both agents use the same models and request settings, prompt, voice rules, tools, failure modes, greeting, spoken-text cleanup (codes spelled out, stage directions dropped), silence check-in and gpt-oss-20b fallback. This one differs in a few places:

- **No hang-up.** The Agent SDK has no way for the agent to end the call, so `end_call` only says goodbye.
- **Stock Pipecat behaviour.** It uses Pipecat's own turn handling, sentence splitting and interruption handling. The Cloudflare version replaces those to fit Durable Objects, and adds a short holding line when the model is slow.
- **Fewer trace attributes.** Pipecat's built-in spans plus tool and workflow spans. The Cloudflare version also writes per-call summaries such as `workflow.completed` on its root span.

## Parity with the Cloudflare agent

The same five test cases from the Bronwood regression template (sub-sample 5, seed 7), run against this agent and against the Cloudflare Workers agent, October 26, 2026:

| Metric | Agent SDK | Cloudflare |
|--------|-----------|------------|
| Trace-aware tool invocation discipline | 5/5 | 5/5 |
| Trace-aware workflow completion | 5/5 | 5/5 |
| Availability verified before booking | 5/5 | 5/5 |
| Booking accuracy and read-back | 4/5 | 4/5 |
| Conversation success | 2/5 | 2/5 |
| Accessibility and special-request handling | 3/5 | 4/5 |
| Pause anomalies (mean) | 1.65 | 1.69 |
| Latency (mean, s) | 2.81 | 6.68 |
| Syllable rate (mean) | 2.09 | 2.08 |
| Repetition loops (mean) | 0.6 | 0.4 |

Five cases is a small sample, so read single-case differences as noise.

## Tests

```bash
uv run pytest
```
