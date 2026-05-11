// Typed error classes for the Coval SDK.
//
// All non-2xx HTTP responses are turned into a CovalApiError so callers can
// distinguish API-level failures from network/transport failures (which raise
// CovalNetworkError instead). Both extend Error and preserve a `cause`
// chain for ergonomic try/catch.

export interface CovalApiErrorBody {
  message?: string;
  code?: string;
  request_id?: string;
  details?: unknown;
}

export class CovalApiError extends Error {
  readonly status: number;
  readonly code: string | undefined;
  readonly requestId: string | undefined;
  readonly body: CovalApiErrorBody | string | undefined;
  readonly url: string;
  readonly method: string;

  constructor(args: {
    status: number;
    message: string;
    url: string;
    method: string;
    body?: CovalApiErrorBody | string;
    code?: string;
    requestId?: string;
  }) {
    super(args.message);
    this.name = 'CovalApiError';
    this.status = args.status;
    this.url = args.url;
    this.method = args.method;
    this.body = args.body;
    this.code = args.code;
    this.requestId = args.requestId;
  }
}

export class CovalNetworkError extends Error {
  readonly url: string;
  readonly method: string;
  readonly attempts: number;

  constructor(args: { message: string; url: string; method: string; attempts: number; cause?: unknown }) {
    super(args.message, { cause: args.cause });
    this.name = 'CovalNetworkError';
    this.url = args.url;
    this.method = args.method;
    this.attempts = args.attempts;
  }
}

export async function parseErrorResponse(response: Response): Promise<CovalApiErrorBody | string | undefined> {
  const contentType = response.headers.get('content-type') ?? '';
  try {
    if (contentType.includes('application/json')) {
      return (await response.json()) as CovalApiErrorBody;
    }
    const text = await response.text();
    return text.length > 0 ? text : undefined;
  } catch {
    return undefined;
  }
}
