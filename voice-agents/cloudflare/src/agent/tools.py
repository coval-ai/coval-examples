"""A vertical's tools as Pipecat functions, plus end_call. Pipecat owns execution and cancellation.

Tool behaviour is mock data with deliberate failure modes (see verticals/). Every call is
recorded on the owner so the call's trace gets llm_tool_call and workflow spans.
"""

import asyncio
import inspect
import json
import time

from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams, FunctionCallResultProperties

from server.observability import exception_fields, log

DEFAULT_TIMEOUT_SECONDS = 10

END_CALL = {
    "name": "end_call",
    "description": "Hang up the phone call. Use only after the caller's needs are handled and "
    "you have said goodbye, or if the caller asks to end the call.",
    "parameters": {"type": "object", "properties": {}},
}


def _clean(arguments) -> dict:
    # Models sometimes send the string "null" for an omitted optional field.
    if not isinstance(arguments, dict):
        return {}
    return {key: value for key, value in arguments.items() if value not in (None, "null")}


async def _run_handler(handler, arguments, state):
    result = handler(arguments, state)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {"result": result}
    return result if isinstance(result, dict) else {"result": result}


def _register(owner, spec: dict) -> FunctionSchema:
    vertical = owner.vertical
    name = spec["name"]
    handler = vertical.handlers.get(name)
    parameters = spec.get("parameters") or {}

    @tool_options(timeout_secs=vertical.tool_timeouts.get(name, DEFAULT_TIMEOUT_SECONDS))
    async def execute(params: FunctionCallParams):
        arguments = _clean(params.arguments)
        started = time.time()
        try:
            if handler is None:
                result = {"error": "UNKNOWN_TOOL", "message": f"Unknown tool: {name}"}
            else:
                result = await _run_handler(handler, arguments, owner.tool_state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.bind(call_id=owner.call_id).error("tool_failed", tool=name, **exception_fields(exc))
            result = {"error": "TOOL_EXCEPTION", "message": "The action failed and did not happen."}
        owner.record_tool(name, params.tool_call_id, arguments, result, started, time.time())
        await params.result_callback(result)

    return FunctionSchema(
        name=name,
        description=spec.get("description", ""),
        properties=parameters.get("properties", {}),
        required=parameters.get("required", []),
        handler=execute,
    )


def _end_call(owner) -> FunctionSchema:
    async def execute(params: FunctionCallParams):
        # A tool reply carries no spoken text, so the goodbye is spoken here rather than by
        # another model turn, and the call hangs up once it has played.
        await params.result_callback(
            {"success": True}, properties=FunctionCallResultProperties(run_llm=False)
        )
        await owner.hang_up_after(owner.vertical.end_call_message)

    return FunctionSchema(
        name=END_CALL["name"],
        description=END_CALL["description"],
        properties={},
        required=[],
        handler=execute,
    )


def build_tools(owner) -> list[FunctionSchema]:
    specs = [tool.get("function", tool) for tool in owner.vertical.tools]
    return [_register(owner, spec) for spec in specs] + [_end_call(owner)]
