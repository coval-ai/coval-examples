// Retry-with-backoff wrapper for the generated client's `fetchApi`.
//
// We wrap fetch instead of using middleware because middleware fires once per
// request, and retrying needs to re-run the whole fetch. The retry semantics
// mirror our internal Python client (LiveKitModelManager._generate_token):
// 3 attempts, exponential backoff with jitter, retryable statuses 429/500/
// 502/503/504, honor Retry-After.

import { CovalNetworkError } from './errors.js';
import type { FetchAPI } from './generated/runtime.js';

const RETRYABLE_STATUSES = new Set([408, 429, 500, 502, 503, 504]);
const RETRYABLE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

export interface RetryOptions {
  /** Max total attempts including the first. Default 3. */
  maxAttempts?: number;
  /** Base backoff in ms. Effective delay = base * 2^(attempt-1) + jitter. Default 200. */
  baseDelayMs?: number;
  /** Cap on backoff delay in ms. Default 5000. */
  maxDelayMs?: number;
  /** Override the random jitter (0..1) — useful for tests. */
  jitter?: () => number;
  /** Statuses considered retryable. Defaults to {408,429,500,502,503,504}. */
  retryableStatuses?: Set<number>;
  /** HTTP methods eligible for retry. Defaults to idempotent GET/HEAD/OPTIONS only. */
  retryableMethods?: Set<string>;
}

export interface RetryDeps {
  fetch?: FetchAPI;
  sleep?: (ms: number) => Promise<void>;
}

export function createRetryingFetch(options: RetryOptions = {}, deps: RetryDeps = {}): FetchAPI {
  const maxAttempts = options.maxAttempts ?? 3;
  const baseDelay = options.baseDelayMs ?? 200;
  const maxDelay = options.maxDelayMs ?? 5_000;
  const jitter = options.jitter ?? Math.random;
  const retryable = options.retryableStatuses ?? RETRYABLE_STATUSES;
  const retryableMethods = options.retryableMethods ?? RETRYABLE_METHODS;
  const innerFetch: FetchAPI = deps.fetch ?? (globalThis.fetch as FetchAPI);
  const sleep = deps.sleep ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));

  return async function retryingFetch(input, init) {
    let lastError: unknown;
    const method = (init?.method ?? 'GET').toUpperCase();
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;

    if (!retryableMethods.has(method)) {
      try {
        return await innerFetch(input, init);
      } catch (err) {
        throw new CovalNetworkError({
          message: `Coval API request failed: ${stringifyError(err)}`,
          url,
          method,
          attempts: 1,
          cause: err,
        });
      }
    }

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        const response = await innerFetch(input, init);
        if (!retryable.has(response.status) || attempt === maxAttempts) {
          return response;
        }
        const wait = computeDelay({ attempt, baseDelay, maxDelay, jitter, response });
        await sleep(wait);
      } catch (err) {
        lastError = err;
        if (attempt === maxAttempts) {
          throw new CovalNetworkError({
            message: `Coval API request failed after ${attempt} attempt${attempt > 1 ? 's' : ''}: ${stringifyError(err)}`,
            url,
            method,
            attempts: attempt,
            cause: err,
          });
        }
        const wait = computeDelay({ attempt, baseDelay, maxDelay, jitter });
        await sleep(wait);
      }
    }

    throw new CovalNetworkError({
      message: `Coval API request fell through retry loop: ${stringifyError(lastError)}`,
      url,
      method,
      attempts: maxAttempts,
      cause: lastError,
    });
  };
}

function computeDelay(args: {
  attempt: number;
  baseDelay: number;
  maxDelay: number;
  jitter: () => number;
  response?: Response;
}): number {
  const { attempt, baseDelay, maxDelay, jitter, response } = args;
  if (response) {
    const retryAfter = response.headers.get('retry-after');
    if (retryAfter) {
      const seconds = Number(retryAfter);
      if (Number.isFinite(seconds) && seconds >= 0) {
        return Math.min(maxDelay, seconds * 1000);
      }
      const when = Date.parse(retryAfter);
      if (Number.isFinite(when)) {
        return Math.max(0, Math.min(maxDelay, when - Date.now()));
      }
    }
  }
  const exp = Math.min(maxDelay, baseDelay * 2 ** (attempt - 1));
  return Math.floor(exp * (0.5 + jitter() * 0.5));
}

function stringifyError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}
