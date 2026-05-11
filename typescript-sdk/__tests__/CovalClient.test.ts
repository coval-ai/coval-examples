import { describe, expect, it, vi } from 'vitest';
import {
  CovalApiError,
  CovalClient,
  CovalNetworkError,
  createRetryingFetch,
  paginate,
} from '../src/index.js';

describe('apiKeyAuthMiddleware', () => {
  it('attaches lowercase x-api-key to every request', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fakeFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      calls.push({ url, init });
      return new Response(JSON.stringify({ agents: [], next_page_token: null }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }) as unknown as typeof fetch;

    const coval = new CovalClient({ apiKey: 'test-key', fetch: fakeFetch });
    await coval.agents.listAgents({});

    expect(calls).toHaveLength(1);
    const headers = new Headers(calls[0]?.init?.headers);
    expect(headers.get('x-api-key')).toBe('test-key');
    // It must be lowercase — uppercase X-API-Key is rejected by the gateway.
    expect(Array.from(headers.entries()).map(([k]) => k)).toContain('x-api-key');
  });

  it('throws if apiKey is missing', () => {
    expect(() => new CovalClient({ apiKey: '' })).toThrow(/apiKey is required/);
  });
});

describe('CovalApiError', () => {
  it('wraps non-2xx responses with status/code/message/requestId', async () => {
    const fakeFetch = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          error: {
            message: 'agent not found',
            code: 'NOT_FOUND',
            request_id: 'req-abc',
          },
        }),
        {
          status: 404,
          headers: {
            'content-type': 'application/json',
            'x-request-id': 'req-abc',
          },
        },
      );
    }) as unknown as typeof fetch;

    const coval = new CovalClient({ apiKey: 'test-key', fetch: fakeFetch, retry: false });
    await expect(coval.agents.getAgent({ agentId: 'does-not-exist' })).rejects.toMatchObject({
      name: 'CovalApiError',
      status: 404,
      message: 'agent not found',
      requestId: 'req-abc',
      code: 'NOT_FOUND',
    });
  });
});

describe('createRetryingFetch', () => {
  it('retries 503 responses up to maxAttempts and surfaces the last one', async () => {
    let attempts = 0;
    const fakeFetch = vi.fn(async () => {
      attempts += 1;
      return new Response('upstream', { status: 503 });
    }) as unknown as typeof fetch;

    const retrying = createRetryingFetch(
      { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 2, jitter: () => 0 },
      { fetch: fakeFetch, sleep: async () => {} },
    );

    const response = await retrying('https://api.coval.dev/v1/agents', { method: 'GET' });
    expect(response.status).toBe(503);
    expect(attempts).toBe(3);
  });

  it('returns the first non-retryable response without retrying', async () => {
    let attempts = 0;
    const fakeFetch = vi.fn(async () => {
      attempts += 1;
      return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
    }) as unknown as typeof fetch;

    const retrying = createRetryingFetch(
      { maxAttempts: 3, baseDelayMs: 1, jitter: () => 0 },
      { fetch: fakeFetch, sleep: async () => {} },
    );

    const response = await retrying('https://api.coval.dev/v1/agents', { method: 'GET' });
    expect(response.status).toBe(200);
    expect(attempts).toBe(1);
  });

  it('honors Retry-After in seconds', async () => {
    let attempts = 0;
    let lastSleep = 0;
    const fakeFetch = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1) {
        return new Response('rate limited', { status: 429, headers: { 'retry-after': '2' } });
      }
      return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
    }) as unknown as typeof fetch;

    const retrying = createRetryingFetch(
      { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 10_000, jitter: () => 0 },
      {
        fetch: fakeFetch,
        sleep: async (ms) => {
          lastSleep = ms;
        },
      },
    );

    const response = await retrying('https://api.coval.dev/v1/agents', { method: 'GET' });
    expect(response.status).toBe(200);
    expect(lastSleep).toBe(2_000);
  });

  it('wraps network errors as CovalNetworkError after maxAttempts', async () => {
    const fakeFetch = vi.fn(async () => {
      throw new TypeError('fetch failed');
    }) as unknown as typeof fetch;

    const retrying = createRetryingFetch(
      { maxAttempts: 2, baseDelayMs: 1, jitter: () => 0 },
      { fetch: fakeFetch, sleep: async () => {} },
    );

    await expect(
      retrying('https://api.coval.dev/v1/agents', { method: 'GET' }),
    ).rejects.toBeInstanceOf(CovalNetworkError);
  });
});

describe('paginate', () => {
  it('yields items across multiple pages and stops when next_page_token is null', async () => {
    const pages = [
      { agents: ['a', 'b'], next_page_token: 'token-2' },
      { agents: ['c'], next_page_token: 'token-3' },
      { agents: ['d', 'e'], next_page_token: null },
    ];

    let calls = 0;
    const collected: string[] = [];
    for await (const item of paginate({
      fetchPage: async (token) => {
        const page = pages[calls]!;
        if (calls === 0) expect(token).toBeUndefined();
        else expect(token).toBe(pages[calls - 1]?.next_page_token ?? undefined);
        calls += 1;
        return page;
      },
      items: (page) => page.agents,
      nextToken: (page) => page.next_page_token ?? undefined,
    })) {
      collected.push(item);
    }

    expect(collected).toEqual(['a', 'b', 'c', 'd', 'e']);
    expect(calls).toBe(3);
  });

  it('respects maxPages cap', async () => {
    let calls = 0;
    const collected: number[] = [];
    for await (const item of paginate({
      fetchPage: async () => {
        calls += 1;
        return { items: [calls], next_page_token: 'always-more' };
      },
      items: (p) => p.items,
      nextToken: (p) => p.next_page_token,
      maxPages: 4,
    })) {
      collected.push(item);
    }

    expect(collected).toEqual([1, 2, 3, 4]);
    expect(calls).toBe(4);
  });
});
