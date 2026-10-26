"""Run Coval simulations against Remy running in this process.

Two ways to run, both using the Agent SDK for the audio:

    python run_simulations.py --template <run-template-id>
        coval-sdk reads the run template (persona, test set, metrics, sample size), launches the
        run against your SDK WebSocket agent and reports each conversation's metrics.
        coval-agent-sdk attaches Remy to each conversation with client.connect().

    python run_simulations.py --persona <id> --test-set <id> --simple
        coval-agent-sdk's client.run() launches the run and attaches every conversation itself.
        It runs the whole test set with the agent's own metrics.

Environment: COVAL_API_KEY, COVAL_WORKSPACE_ID, COVAL_AGENT_ID (from coval_setup.py),
CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN (Workers AI).
"""

import argparse
import asyncio
import os
import sys
import time

import coval_agent_sdk
from coval_agent_sdk.tracing import CovalTraceExporter
from coval_sdk import (
    CovalClient,
    CovalRunsAPILaunchOptions,
    CovalRunsAPILaunchRunRequest,
    paginate,
)
from coval_sdk.exceptions import NotFoundException
from dotenv import load_dotenv
from loguru import logger
from pipecat.utils.tracing.setup import setup_tracing

from remy.agent import Cloudflare, run_conversation

# Simulation states before a conversation is ready to connect, and after it has ended.
QUEUED = {"PENDING", "IN QUEUE"}
FINISHED = {"COMPLETED", "FAILED", "CANCELLED", "SKIPPED", "DELETED"}
POLL_SECONDS = 2
# coval-sdk calls have no timeout by default; a poll must not hang the run.
REQUEST_TIMEOUT_SECONDS = 30


def _text(status) -> str:
    """coval-sdk statuses are string enums; print their values."""
    return getattr(status, "value", status)


def _clients(api_key: str, workspace_id: str):
    # One exporter is shared by Pipecat's OpenTelemetry provider and the SDK client: Pipecat
    # writes the spans, the SDK uploads each conversation's spans when it ends.
    exporter = CovalTraceExporter()
    setup_tracing(service_name="remy-agent-sdk", exporter=exporter)
    voice = coval_agent_sdk.CovalVoiceClient(
        api_key=api_key, workspace_id=workspace_id, trace_exporter=exporter
    )
    return CovalClient(api_key), voice


def _launch(coval: CovalClient, args, workspace_id: str) -> str:
    """Launch a run with the template's persona, test set, metrics and sample size."""
    template = coval.run_templates.get_run_template(args.template).run_template
    request = CovalRunsAPILaunchRunRequest(
        agent_id=args.agent_id,
        persona_id=args.persona or template.persona_ids[0],
        test_set_id=args.test_set or template.test_set_ids[0],
        metric_ids=template.metric_ids or None,
        options=CovalRunsAPILaunchOptions(
            iteration_count=template.iteration_count or 1,
            concurrency=args.concurrency,
            sub_sample_size=args.sample or template.sub_sample_size,
            sub_sample_seed=args.seed,
        ),
    )
    run = coval.runs.launch_run(request).run
    logger.info(f"Launched run {run.run_id} from template {template.display_name!r}")
    return run.run_id


async def _attach(voice, cloudflare: Cloudflare, simulation_id: str):
    async with voice.connect(simulation_id) as connection:
        await run_conversation(connection, cloudflare)
    result = connection.result
    if result.trace:
        logger.info(
            f"{simulation_id}: uploaded {result.trace.uploaded_spans} spans {result.trace.error or ''}"
        )
    return result


async def _warn_when_blocked(threshold: float = 0.25):
    """Logs event-loop stalls. A stall stops every conversation's audio, and long ones drop calls."""
    while True:
        started = time.monotonic()
        await asyncio.sleep(0.1)
        lag = time.monotonic() - started - 0.1
        if lag > threshold:
            logger.warning(f"Event loop was blocked for {lag:.2f}s; audio stalls while it is")


def _simulations(coval: CovalClient, run_id: str) -> list:
    return list(
        paginate(
            coval.simulations.list_simulations,
            items_field="simulations",
            filter=f'run_id="{run_id}"',
            page_size=100,
            _request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
    )


async def _run_attached(coval: CovalClient, voice, cloudflare: Cloudflare, run_id: str, timeout: float):
    """Connect Remy to each of the run's conversations as soon as it is ready.

    coval-sdk's client is synchronous, so its calls run in a thread. On the event loop, one slow
    API call would stop every live conversation from reading its audio, and Coval's relay closes
    a conversation whose unread audio passes 64 KiB.
    """
    attached: dict[str, asyncio.Task] = {}
    deadline = time.monotonic() + timeout
    status = "PENDING"
    while time.monotonic() < deadline:
        try:
            simulations = await asyncio.to_thread(_simulations, coval, run_id)
            run = await asyncio.to_thread(
                coval.runs.get_run, run_id, _request_timeout=REQUEST_TIMEOUT_SECONDS
            )
        except Exception as error:  # A failed poll is retried on the next one.
            logger.warning(f"Polling run {run_id} failed: {error!r}")
            await asyncio.sleep(POLL_SECONDS)
            continue
        for simulation in simulations:
            sid = simulation.simulation_id
            if sid not in attached and simulation.status not in QUEUED | FINISHED:
                attached[sid] = asyncio.create_task(_attach(voice, cloudflare, sid))
        status = run.run.status
        if status in FINISHED and all(task.done() for task in attached.values()):
            break
        await asyncio.sleep(POLL_SECONDS)
    results = await asyncio.gather(*attached.values(), return_exceptions=True)
    for sid, result in zip(attached, results, strict=True):
        if isinstance(result, BaseException):
            logger.error(f"{sid}: {result!r}")
    return status


def _metric_name(coval: CovalClient, metric_id: str) -> str:
    try:
        return coval.metrics.get_metric(metric_id).metric.metric_name
    except NotFoundException:
        return metric_id  # A built-in metric, which the metrics API does not list.


def _report(coval: CovalClient, run_id: str):
    """Each conversation's metric results, read back with coval-sdk."""
    names: dict[str, str] = {}
    for simulation in _simulations(coval, run_id):
        print(f"\n{simulation.simulation_id}  {_text(simulation.status)}")
        outputs = coval.metric_outputs.simulations_list_metrics(simulation.simulation_id)
        for output in outputs.metrics or []:
            if output.metric_id not in names:
                names[output.metric_id] = _metric_name(coval, output.metric_id)
            # Metric values are a typed union in coval-sdk; actual_instance is the value itself.
            value = getattr(output.value, "actual_instance", None)
            if value is None:
                value = _text(output.status)
            print(f"  {names[output.metric_id]:<44} {value}")


async def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--agent-id", default=os.environ.get("COVAL_AGENT_ID"))
    parser.add_argument("--template", help="Run template to take persona, test set and metrics from")
    parser.add_argument("--persona")
    parser.add_argument("--test-set")
    parser.add_argument("--sample", type=int, help="Number of test cases to sample")
    parser.add_argument("--seed", type=int)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Conversations at once (default 1; the hosted relay dropped calls at 3 in our tests)",
    )
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--simple", action="store_true", help="Use client.run() instead")
    parser.add_argument("--verbose", action="store_true", help="Show Pipecat's debug logs")
    args = parser.parse_args()
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "INFO")
    if not args.agent_id:
        parser.error("Set COVAL_AGENT_ID or pass --agent-id (create one with coval_setup.py)")

    workspace_id = os.environ["COVAL_WORKSPACE_ID"]
    coval, voice = _clients(os.environ["COVAL_API_KEY"], workspace_id)
    cloudflare = Cloudflare.from_env()
    watchdog = asyncio.create_task(_warn_when_blocked())

    if args.simple:
        if not (args.persona and args.test_set):
            parser.error("--simple needs --persona and --test-set")
        config = coval_agent_sdk.RunConfig(
            agent_id=args.agent_id,
            persona_id=args.persona,
            test_set_id=args.test_set,
            concurrency=args.concurrency,
            run_timeout_seconds=args.timeout,
        )
        result = await voice.run(config, on_connection=lambda c: run_conversation(c, cloudflare))
        run_id, status = result.run_id, result.status
    else:
        if not args.template:
            parser.error("Pass --template, or --simple with --persona and --test-set")
        run_id = await asyncio.to_thread(_launch, coval, args, workspace_id)
        status = await _run_attached(coval, voice, cloudflare, run_id, args.timeout)

    watchdog.cancel()
    print(f"\nRun {run_id}: {_text(status)}")
    _report(coval, run_id)
    return 0 if status == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
