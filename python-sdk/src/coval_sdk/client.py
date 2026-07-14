"""Ergonomic entry point for the Coval Python SDK."""

from __future__ import annotations

from typing import Optional, Union

from urllib3.util import Retry

from coval_sdk.api import (
  APIKeysApi,
  AgentsApi,
  AudioApi,
  ConversationsApi,
  DashboardsApi,
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
)
from coval_sdk.api_client import ApiClient
from coval_sdk.configuration import Configuration


DEFAULT_BASE_URL = "https://api.coval.dev/v1"
RetryConfig = Union[Retry, int, bool]


def _default_retry_policy() -> Retry:
  return Retry(
    total=3,
    connect=3,
    read=3,
    status=3,
    backoff_factor=0.2,
    status_forcelist=(408, 429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
    respect_retry_after_header=True,
  )


class CovalClient:
  """Authenticated client exposing every public Coval v1 API surface."""

  def __init__(
    self,
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    retries: Optional[RetryConfig] = None,
  ) -> None:
    if not api_key:
      raise ValueError("CovalClient: api_key is required")

    self.configuration = Configuration(
      host=base_url.rstrip("/"),
      retries=_default_retry_policy() if retries is None else retries,
    )
    self.api_client = ApiClient(self.configuration)
    self.api_client.set_default_header("x-api-key", api_key)

    self.api_keys = APIKeysApi(self.api_client)
    self.agents = AgentsApi(self.api_client)
    self.audio = AudioApi(self.api_client)
    self.conversations = ConversationsApi(self.api_client)
    self.dashboards = DashboardsApi(self.api_client)
    self.metric_outputs = MetricOutputsApi(self.api_client)
    self.metrics = MetricsApi(self.api_client)
    self.monitor_events = MonitorEventsApi(self.api_client)
    self.monitors = MonitorsApi(self.api_client)
    self.mutations = MutationsApi(self.api_client)
    self.organization_conversations_config = OrganizationConversationsConfigApi(self.api_client)
    self.personas = PersonasApi(self.api_client)
    self.reports = ReportsApi(self.api_client)
    self.review_annotations = ReviewAnnotationsApi(self.api_client)
    self.review_projects = ReviewProjectsApi(self.api_client)
    self.run_templates = RunTemplatesApi(self.api_client)
    self.runs = RunsApi(self.api_client)
    self.scheduled_runs = ScheduledRunsApi(self.api_client)
    self.simulations = SimulationsApi(self.api_client)
    self.tags = TagsApi(self.api_client)
    self.test_cases = TestCasesApi(self.api_client)
    self.test_sets = TestSetsApi(self.api_client)
    self.traces = TracesApi(self.api_client)
    self.webhooks = WebhooksApi(self.api_client)
    self.widgets = WidgetsApi(self.api_client)

  def close(self) -> None:
    """Release pooled HTTP connections."""
    self.api_client.rest_client.pool_manager.clear()

  def __enter__(self) -> "CovalClient":
    return self

  def __exit__(self, exc_type, exc_value, traceback) -> None:
    self.close()
