// Public entry point for @coval/sdk.

export { CovalClient } from './CovalClient.js';
export type { CovalClientOptions } from './CovalClient.js';
export { CovalApiError, CovalNetworkError } from './errors.js';
export type { CovalApiErrorBody } from './errors.js';
export { paginate, collectAll } from './pagination.js';
export type { PaginateOptions } from './pagination.js';
export { apiKeyAuthMiddleware } from './auth.js';
export { createRetryingFetch } from './retry.js';
export type { RetryOptions } from './retry.js';

// Re-export the entire generated surface so callers can reach for request
// types, model types, and individual API classes if they want to bypass the
// CovalClient wrapper.
export * from './generated/index.js';
