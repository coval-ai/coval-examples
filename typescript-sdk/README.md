# @coval/sdk

Typed TypeScript client for the [Coval](https://coval.dev) evaluation platform API.

The bulk of this SDK is **generated** from Coval's public OpenAPI specs (the
same ones that power [docs.coval.dev/api-reference](https://docs.coval.dev/api-reference)).
A small hand-written layer adds auth, retries with backoff, pagination
iteration, and typed errors so the generated client is ergonomic for
production use.

## Install

```bash
npm install @coval/sdk
```

Requires Node 18+.

## Quick start

```ts
import { CovalClient } from '@coval/sdk';

const coval = new CovalClient({ apiKey: process.env.COVAL_API_KEY! });

// List agents
const { agents } = await coval.agents.listAgents({ pageSize: 25 });

// Submit a production conversation for evaluation
await coval.conversations.submitConversation({
  covalConversationsAPISubmitConversationRequest: {
    agent_id: 'my-agent-id',
    external_conversation_id: 'call-2026-05-11-abc',
    occurred_at: new Date(),
    transcript: [
      { role: 'agent', content: 'Hi, thanks for calling.' },
      { role: 'user', content: "I'd like to update my account." },
    ],
  },
});
```

## What the wrapper adds

### Auth — `x-api-key` (lowercase)

Coval's gateway is case-sensitive about the API-key header. Uppercase
`X-API-Key` returns `Missing API Key`. The `CovalClient` always sends
lowercase `x-api-key`, so you don't have to think about it.

### Retries with exponential backoff

For idempotent `GET`, `HEAD`, and `OPTIONS` requests, the client retries
automatically on `408`, `429`, and `5xx` responses, plus network errors.
Defaults: 3 attempts, 200ms base delay with jitter, 5s cap. `Retry-After`
headers (both seconds and HTTP-date) are respected. Mutating requests are not
retried unless you explicitly add their method to `retryableMethods`.

```ts
const coval = new CovalClient({
  apiKey: process.env.COVAL_API_KEY!,
  retry: { maxAttempts: 5, baseDelayMs: 500, maxDelayMs: 10_000 },
});

// Disable retries entirely:
const coval2 = new CovalClient({ apiKey, retry: false });
```

### Typed errors

Every non-2xx HTTP response becomes a `CovalApiError` after retries are
exhausted. Network failures become `CovalNetworkError`. Both extend `Error`,
so plain `try/catch` works.

```ts
import { CovalApiError } from '@coval/sdk';

try {
  await coval.agents.getAgent({ agentId: 'unknown' });
} catch (err) {
  if (err instanceof CovalApiError) {
    console.error(err.status, err.code, err.message, err.requestId);
  }
}
```

### Pagination iterator

List endpoints return a single page plus a `next_page_token`. The `paginate`
helper turns that into an async iterator so you don't have to manage tokens.

```ts
import { paginate, collectAll } from '@coval/sdk';

for await (const agent of paginate({
  fetchPage: (pageToken) => coval.agents.listAgents({ pageToken, pageSize: 50 }),
  items: (page) => page.agents,
  nextToken: (page) => page.next_page_token,
})) {
  console.log(agent.id, agent.display_name);
}

// Or collect everything into an array:
const all = await collectAll({
  fetchPage: (pageToken) => coval.simulations.listSimulations({ pageToken }),
  items: (p) => p.simulations,
  nextToken: (p) => p.next_page_token,
});
```

## Customization

### Override base URL (staging, self-host)

```ts
const coval = new CovalClient({
  apiKey,
  baseUrl: 'https://staging.api.coval.dev/v1',
});
```

### Plug in custom middleware

```ts
const coval = new CovalClient({
  apiKey,
  middleware: [
    {
      async pre({ url, init }) {
        console.log('→', init.method, url);
        return undefined;
      },
    },
  ],
});
```

### Override fetch (e.g., undici with keep-alive)

```ts
import { fetch as undiciFetch } from 'undici';

const coval = new CovalClient({ apiKey, fetch: undiciFetch });
```

## Examples

Runnable in this repo. Set `COVAL_API_KEY` (and `COVAL_AGENT_ID` for the
submit example) and run:

```bash
npm run example:list-agents
npm run example:submit-conversation
```

## Versioning

The generated surface tracks the underlying Coval API. Spec changes that
break the generated client increment the **minor** version. Hand-written
wrapper changes that break the public TypeScript API also increment the
**minor** version. Bug fixes increment the **patch** version.

## Regenerating

If you want to regenerate the client locally:

```bash
# From the repo root, with the docs repo checked out as a sibling:
node scripts/bundle-spec.mjs
bash scripts/generate-sdks.sh
```

A weekly CI job (`.github/workflows/regenerate-sdks.yml`) does this
automatically and opens a PR if the generated output has drifted.

## License

MIT.
