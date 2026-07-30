// Transport counters for diagnosing intermittent failures.
//
// A request that dies in transit never reaches the API, so there is no
// server-side trace of it. These counters are often the only evidence available
// when a caller reports sporadic timeouts. Mirrors `connection_stats` in the
// Python SDK.

export interface CovalTransportStats {
  /** Logical API calls started. */
  calls: number;
  /** fetch invocations, including retries — exceeds `calls` when retries fire. */
  requests: number;
  /** Retry attempts beyond the initial request. */
  retries: number;
  /** fetch rejections: DNS failure, reset, abort, or a stranded pooled socket. */
  networkErrors: number;
}

export function createTransportStats(): CovalTransportStats {
  return { calls: 0, requests: 0, retries: 0, networkErrors: 0 };
}

export function formatTransportStats(stats: CovalTransportStats): string {
  return `CovalTransportStats(calls=${stats.calls}, requests=${stats.requests}, retries=${stats.retries}, networkErrors=${stats.networkErrors})`;
}
