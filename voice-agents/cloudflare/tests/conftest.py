"""Stand-ins for the Workers runtime modules so agent code imports under plain CPython."""

import sys
import types


def _stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules.setdefault(name, module)
    return module


class _Response:
    def __init__(self, body=None, status=200, **kwargs):
        self.body, self.status, self.kwargs = body, status, kwargs

    @classmethod
    def json(cls, data, status=200, headers=None):
        return cls(data, status, headers=headers)


_stub("js", WebSocketPair=None)
_stub("pyodide")
_stub("pyodide.ffi", create_proxy=lambda value: value)
_stub(
    "workers",
    Response=_Response,
    DurableObject=object,
    WorkerEntrypoint=object,
    fetch=None,
)
_stub("workers.asgi", acquire_js_buffer=None)
