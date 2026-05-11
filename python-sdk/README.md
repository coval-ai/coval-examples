# coval-sdk (Python)

Generated Python client for the [Coval](https://coval.dev) evaluation
platform API. Built from the public OpenAPI specs via
[openapi-generator](https://openapi-generator.tech/docs/generators/python).

This package is currently **demo-grade**: it's the generated client without a
hand-written ergonomic wrapper layer. The TypeScript SDK (`@covalai/sdk`)
ships with auth/retry/pagination helpers — those are still in flight on the
Python side and will follow in a subsequent release.

## Install

```bash
pip install -e ./python-sdk          # from the coval-examples repo
# or, once published to PyPI:
# pip install coval-sdk
```

Requires Python 3.9+.

## Usage

```python
import os
from coval_sdk import AgentsApi, ApiClient, Configuration

config = Configuration(host="https://api.coval.dev")
with ApiClient(config) as client:
    # Coval's gateway requires lowercase x-api-key. The bundled spec splits
    # the auth scheme per tag, so the cleanest pattern is to set the header
    # directly on the client:
    client.set_default_header("x-api-key", os.environ["COVAL_API_KEY"])

    agents_api = AgentsApi(client)
    page = agents_api.list_agents(page_size=50)
    for a in page.agents:
        print(a.id, a.display_name)
```

## Run the example

```bash
COVAL_API_KEY=... python examples/list_agents.py
COVAL_API_KEY=... python examples/list_agents.py --raw   # bypass pydantic validation
```

### Why `--raw` exists

The generated models enforce strict pydantic validation on responses
(including regex patterns on IDs). If your org has any historical data that
predates the current spec — for instance, an `agent_id` that doesn't match
the `^[A-Za-z0-9]{22}$` regex — pydantic will raise a `ValidationError`
mid-stream. The `--raw` flag falls back to
`list_agents_without_preload_content()` which returns the raw HTTP response,
so you can parse it manually.

For production use, prefer the typed call. If you hit ValidationErrors,
file a docs/spec issue so we can either fix the data or relax the
constraint.

## What's in the box

- 21 typed API classes (`AgentsApi`, `ConversationsApi`, `MetricsApi`, …)
- Pydantic v2 models for every request and response shape
- `urllib3`-based transport

## Regenerating

From the repo root:

```bash
node scripts/bundle-spec.mjs
bash scripts/generate-sdks.sh
```

## License

MIT.
