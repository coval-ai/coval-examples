# coval-sdk (Python)

Typed Python client for the [Coval](https://coval.dev) evaluation platform
API. The API classes and Pydantic v2 models are generated from the same public
OpenAPI specs that power the API reference. `CovalClient` adds authentication,
safe retries, pagination, and one entry point for every public v1 resource.

## Install

```bash
pip install coval-sdk
```

Requires Python 3.9+.

## Quick start

```python
import os

from coval_sdk import CovalClient, paginate

with CovalClient(os.environ["COVAL_API_KEY"]) as coval:
  for agent in paginate(
    coval.agents.list_agents,
    items_field="agents",
    page_size=50,
  ):
    print(agent.id, agent.display_name)
```

The client sends the required lowercase `x-api-key` header and defaults to
`https://api.coval.dev/v1`.

## Retries

`GET`, `HEAD`, and `OPTIONS` requests retry `408`, `429`, and transient `5xx`
responses up to three total attempts with exponential backoff. Mutating
requests are not retried by default, which avoids duplicating side effects.

```python
from coval_sdk import CovalClient

# Disable transport retries.
coval = CovalClient(api_key, retries=False)

# Use a custom base URL.
staging = CovalClient(api_key, base_url="https://staging.api.coval.dev/v1")
```

## Generated client

All generated API classes, models, `ApiClient`, `Configuration`, and typed
exceptions remain available for lower-level usage:

```python
from coval_sdk import AgentsApi, ApiClient, Configuration

config = Configuration(host="https://api.coval.dev/v1")
with ApiClient(config) as client:
  client.set_default_header("x-api-key", api_key)
  page = AgentsApi(client).list_agents(page_size=50)
```

## What's included

- 25 typed API classes, including agents, conversations, runs, run templates,
  metrics, monitors, reports, tags, webhooks, and organization configuration
- Pydantic v2 request and response models
- Lowercase API-key authentication and connection pooling
- Safe retry and token-pagination helpers

## Development

From the repository root:

```bash
COVAL_SPECS_DIR=../docs/api-reference/v1 node scripts/bundle-spec.mjs
bash scripts/generate-sdks.sh
python -m pytest python-sdk/tests
```

## License

MIT.
