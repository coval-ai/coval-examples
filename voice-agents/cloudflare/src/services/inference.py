"""Workers AI requests: classified failures and one retry within one deadline."""

import asyncio
import json
import re

from pipecat.utils.errors import (
    ErrorCategory,
    classify_http_exception,
    classify_http_status_code,
    extract_http_status_code,
)

from server.observability import exception_fields

REQUEST_TIMEOUT_SECONDS = 25
RETRY_DELAY_SECONDS = 0.3


class ProviderError(RuntimeError):
    """An error response, with the message the AI binding gives the errors it raises."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class InferenceError(RuntimeError):
    def __init__(self, category, *, status=None, code=None):
        super().__init__("Workers AI inference failed")
        self.category, self.status, self.code = category, status, code

    @property
    def retryable(self):
        return (
            self.category in {ErrorCategory.CONNECTIVITY, ErrorCategory.SERVER} or self.code == 3040
        )


def type_name(value):
    """Names a JavaScript or Python value's type without reading its contents."""
    try:
        return str(value.constructor.name)[:40]
    except Exception:
        return type(value).__name__


async def provider_error(native, status):
    """Reads an error response the way the binding does before it raises.

    The message only classifies the failure. It is never logged.
    """
    message = ""
    try:
        async with asyncio.timeout(2):
            message = str(await native.text())[:4096]
        body = json.loads(message)
        if body.get("internalCode"):
            message = f"{body['internalCode']}: {body.get('description', '')}"
        elif body.get("errors"):
            message = f"{body['errors'][0].get('code')}: {body['errors'][0].get('message', '')}"
    except Exception:
        pass
    return ProviderError(message, status)


def inference_error(exc):
    if isinstance(exc, InferenceError):
        return exc
    native = getattr(exc, "js_error", exc)
    status = extract_http_status_code(native) or extract_http_status_code(exc)
    category = (
        classify_http_status_code(status) if status is not None else classify_http_exception(native)
    )
    # The AI binding puts its internal code at the start of the JS error message.
    # Do not consult binding.lastRequest* fields: concurrent requests share them.
    message = str(getattr(native, "message", exc))[:4096]
    match = re.match(r"(?:\w+Error:\s*)?(\d{3,5}):", message)
    code = int(match[1]) if match else None
    if code == 3040:
        category = ErrorCategory.RATE_LIMIT
    elif code == 3036:
        category = ErrorCategory.QUOTA
    elif status is None and code is not None and 400 <= code < 600:
        status = code
        category = classify_http_status_code(status)
    elif category == ErrorCategory.UNKNOWN:
        category = {
            "network": ErrorCategory.CONNECTIVITY,
            "timeout": ErrorCategory.CONNECTIVITY,
            "internal": ErrorCategory.SERVER,
        }.get(exception_fields(exc)["error_category"], ErrorCategory.UNKNOWN)
    return InferenceError(category, status=status, code=code)


async def run_inference(
    ai, model, inputs, options, *, log, is_current, timeout=REQUEST_TIMEOUT_SECONDS, raw=False
):
    """Retry the request, never audio playback, model parsing, or tool execution.

    Returns the JSON body, or with raw=True the HTTP response for streaming.
    """
    # The binding keeps the options of the request that started last and reads
    # them again when each response arrives. One request without this flag can
    # turn another's response into parsed JSON or a bare stream. Flux's websocket
    # option counts as raw too, so every caller gets the result it expects.
    options = {**options, "returnRawResponse": True}
    try:
        async with asyncio.timeout(timeout):
            for attempt in range(2):
                if not is_current():
                    raise asyncio.CancelledError()
                try:
                    response = await ai.run(model, inputs, options)
                    native = getattr(response, "js_response", response)
                    status = getattr(native, "status", None)
                    if status is None:
                        # Something other than an HTTP response. Retry it like a
                        # server error instead of failing the call.
                        log.warning("provider_unexpected_response", response_type=type_name(native))
                        raise InferenceError(ErrorCategory.SERVER)
                    status = int(status)
                    if status != 200:
                        raise await provider_error(native, status)
                    if raw:
                        return response
                    return json.loads(str(await native.text()))
                except Exception as exc:
                    error = inference_error(exc)
                    if attempt or not error.retryable:
                        if error is exc:
                            raise
                        raise error from exc
                    log.warning(
                        "provider_retry",
                        attempt=2,
                        status=error.status,
                        error_code=error.category.value,
                        provider_error_code=error.code,
                    )
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
    except TimeoutError as exc:
        raise InferenceError(ErrorCategory.CONNECTIVITY) from exc
