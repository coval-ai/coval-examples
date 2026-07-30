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

## Connection reuse

Pooled connections idle for longer than `max_idle_seconds` (default `5.0`) are
closed rather than reused. Some network paths silently drop an idle connection
without closing it; reusing one of those stalls the request until your timeout
expires, because the request never reaches the server at all.

```python
# Tune the idle bound, or pass None to reuse connections indefinitely.
coval = CovalClient(api_key, max_idle_seconds=2.0)
```

If you submit infrequently, connection pooling saves you little — the handshake
is amortised over requests you are not making — so a lower bound costs almost
nothing.

## Diagnosing timeouts

A request that dies in transit leaves no server-side trace, so the client keeps
counters describing how the pool has behaved:

```python
coval.connection_stats.as_dict()
# {'opened': 1, 'reused': 12, 'expired': 3}
```

Each counter records the outcome of one attempt to take a connection from the
pool: `opened` when none was available, `reused` when a pooled one was still
within the idle bound, and `expired` when one was discarded for being too old.
Exactly one is incremented per attempt.

A high `expired` relative to `reused` means connections are usually going stale
between requests, which is expected if you call the API infrequently.

`opened` is deliberately not the number of TCP connections established — an
expired connection is closed and then transparently reconnects, so it costs a
handshake as well:

```python
coval.connection_stats.connections_established  # opened + expired
```

Include these when reporting intermittent timeouts. For per-connection detail:

```python
import logging
logging.getLogger("coval_sdk.client").setLevel(logging.DEBUG)
```

Counters are unavailable when `max_idle_seconds=None`, since the pool is then
left entirely to urllib3.

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

## Malformed list resources

List responses validate each resource independently. If one resource does not
match the published schema, the SDK omits that item, returns the remaining
typed resources, and emits `InvalidListItemWarning`. The warning identifies the
response field and list index but does not include resource contents.

Applications that prefer strict failure can enable it on the client:

```python
from coval_sdk import CovalClient

coval = CovalClient(api_key, strict_response_validation=True)
```

Single-resource and malformed top-level responses still raise Pydantic
validation errors.

## What's included

- 25 typed API classes, including agents, conversations, runs, run templates,
  metrics, monitors, reports, tags, webhooks, and organization configuration
- Pydantic v2 request and response models
- Lowercase API-key authentication and connection pooling
- Safe retry and token-pagination helpers
- Warning-backed isolation for malformed resources in list responses

## Development

From the repository root:

```bash
COVAL_SPECS_DIR=../docs/api-reference/v1 node scripts/bundle-spec.mjs
bash scripts/generate-sdks.sh
python -m pytest python-sdk/tests
```

## License

MIT.
