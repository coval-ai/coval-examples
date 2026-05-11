// Auth middleware for the generated openapi-fetch client.
//
// Coval's API requires the header `x-api-key`. The header name is
// case-sensitive on the gateway — uppercase `X-API-Key` is rejected with
// "Missing API Key". We always lower-case it here so callers don't have to
// know about that quirk.

import type { Middleware, RequestContext } from './generated/runtime.js';

export interface ApiKeyAuthOptions {
  apiKey: string;
}

export function apiKeyAuthMiddleware(options: ApiKeyAuthOptions): Middleware {
  const { apiKey } = options;
  if (!apiKey) {
    throw new Error('apiKey is required');
  }
  return {
    async pre(context: RequestContext) {
      const headers = new Headers(context.init.headers);
      headers.set('x-api-key', apiKey);
      return {
        url: context.url,
        init: { ...context.init, headers },
      };
    },
  };
}
