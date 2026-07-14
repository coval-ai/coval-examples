// CovalClient — the entry point for the Coval TypeScript SDK.
//
// Wraps the generated openapi-fetch client with:
//   - x-api-key auth (lowercase header — gateway is case-sensitive)
//   - exponential-backoff retry on 408/429/5xx and on network errors
//   - typed CovalApiError raised for non-2xx after retries are exhausted
//   - a default base URL (https://api.coval.dev/v1) — override for staging
//
// Each API surface from the generated client (AgentsApi, ConversationsApi,
// SimulationsApi, …) is constructed eagerly and exposed as a property so
// usage reads as `client.agents.listAgents({})`.

import { apiKeyAuthMiddleware } from './auth.js';
import { CovalApiError, parseErrorResponse } from './errors.js';
import { createRetryingFetch, type RetryOptions } from './retry.js';
import {
  AgentsApi,
  APIKeysApi,
  AudioApi,
  Configuration,
  ConversationsApi,
  DashboardsApi,
  type FetchAPI,
  type Middleware,
  MetricOutputsApi,
  MetricsApi,
  MonitorEventsApi,
  MonitorsApi,
  MutationsApi,
  OrganizationConversationsConfigApi,
  PersonasApi,
  ReportsApi,
  ReviewAnnotationsApi,
  ReviewProjectsApi,
  RunsApi,
  RunTemplatesApi,
  ScheduledRunsApi,
  SimulationsApi,
  TagsApi,
  TestCasesApi,
  TestSetsApi,
  TracesApi,
  WebhooksApi,
  WidgetsApi,
} from './generated/index.js';

export interface CovalClientOptions {
  /** Coval API key. Required. Sent as `x-api-key` (lowercase). */
  apiKey: string;
  /** Base URL. Defaults to https://api.coval.dev/v1. */
  baseUrl?: string;
  /** Retry configuration. Defaults to 3 attempts, 200ms base, 5s cap. */
  retry?: RetryOptions | false;
  /** Additional middlewares to attach (run after auth). */
  middleware?: Middleware[];
  /** Override the global fetch (e.g., for tests, undici-with-keepalive, etc). */
  fetch?: FetchAPI;
}

const DEFAULT_BASE_URL = 'https://api.coval.dev/v1';

export class CovalClient {
  readonly agents: AgentsApi;
  readonly apiKeys: APIKeysApi;
  readonly audio: AudioApi;
  readonly conversations: ConversationsApi;
  readonly dashboards: DashboardsApi;
  readonly metrics: MetricsApi;
  readonly metricOutputs: MetricOutputsApi;
  readonly monitors: MonitorsApi;
  readonly monitorEvents: MonitorEventsApi;
  readonly mutations: MutationsApi;
  readonly organizationConversationsConfig: OrganizationConversationsConfigApi;
  readonly personas: PersonasApi;
  readonly reports: ReportsApi;
  readonly reviewAnnotations: ReviewAnnotationsApi;
  readonly reviewProjects: ReviewProjectsApi;
  readonly runs: RunsApi;
  readonly runTemplates: RunTemplatesApi;
  readonly scheduledRuns: ScheduledRunsApi;
  readonly simulations: SimulationsApi;
  readonly tags: TagsApi;
  readonly testCases: TestCasesApi;
  readonly testSets: TestSetsApi;
  readonly traces: TracesApi;
  readonly webhooks: WebhooksApi;
  readonly widgets: WidgetsApi;

  readonly configuration: Configuration;

  constructor(options: CovalClientOptions) {
    if (!options.apiKey) {
      throw new Error('CovalClient: apiKey is required');
    }

    const innerFetch: FetchAPI = options.fetch ?? (globalThis.fetch as FetchAPI);
    const fetchApi: FetchAPI =
      options.retry === false
        ? innerFetch
        : createRetryingFetch(options.retry ?? {}, { fetch: innerFetch });

    const middleware: Middleware[] = [
      apiKeyAuthMiddleware({ apiKey: options.apiKey }),
      errorMiddleware(),
      ...(options.middleware ?? []),
    ];

    this.configuration = new Configuration({
      basePath: options.baseUrl ?? DEFAULT_BASE_URL,
      fetchApi,
      middleware,
    });

    this.agents = new AgentsApi(this.configuration);
    this.apiKeys = new APIKeysApi(this.configuration);
    this.audio = new AudioApi(this.configuration);
    this.conversations = new ConversationsApi(this.configuration);
    this.dashboards = new DashboardsApi(this.configuration);
    this.metrics = new MetricsApi(this.configuration);
    this.metricOutputs = new MetricOutputsApi(this.configuration);
    this.monitors = new MonitorsApi(this.configuration);
    this.monitorEvents = new MonitorEventsApi(this.configuration);
    this.mutations = new MutationsApi(this.configuration);
    this.organizationConversationsConfig = new OrganizationConversationsConfigApi(this.configuration);
    this.personas = new PersonasApi(this.configuration);
    this.reports = new ReportsApi(this.configuration);
    this.reviewAnnotations = new ReviewAnnotationsApi(this.configuration);
    this.reviewProjects = new ReviewProjectsApi(this.configuration);
    this.runs = new RunsApi(this.configuration);
    this.runTemplates = new RunTemplatesApi(this.configuration);
    this.scheduledRuns = new ScheduledRunsApi(this.configuration);
    this.simulations = new SimulationsApi(this.configuration);
    this.tags = new TagsApi(this.configuration);
    this.testCases = new TestCasesApi(this.configuration);
    this.testSets = new TestSetsApi(this.configuration);
    this.traces = new TracesApi(this.configuration);
    this.webhooks = new WebhooksApi(this.configuration);
    this.widgets = new WidgetsApi(this.configuration);
  }
}

function errorMiddleware(): Middleware {
  return {
    async post(context) {
      const { response } = context;
      if (response.ok) return;
      const cloned = response.clone();
      const body = await parseErrorResponse(cloned);
      const requestId =
        response.headers.get('x-request-id') ??
        response.headers.get('x-amzn-requestid') ??
        getErrorField(body, 'request_id') ??
        undefined;
      const errorMessage = getErrorField(body, 'message');
      const message =
        errorMessage ||
        response.statusText ||
        `Coval API error ${response.status}`;
      throw new CovalApiError({
        status: response.status,
        message: String(message),
        url: context.url,
        method: (context.init.method ?? 'GET').toUpperCase(),
        body,
        code: getErrorField(body, 'code'),
        requestId,
      });
    },
  };
}

function getErrorField(
  body: unknown,
  field: 'message' | 'code' | 'request_id',
): string | undefined {
  if (!isRecord(body)) return undefined;
  const direct = body[field];
  if (typeof direct === 'string') return direct;
  const nested = body.error;
  if (isRecord(nested) && typeof nested[field] === 'string') {
    return nested[field];
  }
  return undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
