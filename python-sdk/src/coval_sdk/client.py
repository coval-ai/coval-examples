"""Ergonomic entry point for the Coval Python SDK."""

from __future__ import annotations

import time
from typing import Any, Optional, Union

from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util import Retry

from coval_sdk.api import (
  APIKeysApi,
  AgentsApi,
  AudioApi,
  ConversationsApi,
  DashboardsApi,
  IntegrationsApi,
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

# urllib3 only discards a pooled connection when the peer actively closed it --
# _get_conn polls for readability, which sees a FIN/RST but not a connection an
# intermediary dropped silently. Reusing one of those stalls the whole request
# until the read timeout. httpx avoids this by expiring idle connections after
# 5s; match that.
DEFAULT_MAX_IDLE_SECONDS = 5.0
_RETURNED_AT = "_coval_returned_at"


class _IdleExpiryPoolMixin:
  """Close pooled connections that have been idle longer than max_idle_seconds."""

  max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS

  def _put_conn(self, conn: Any) -> None:
    if conn is not None:
      setattr(conn, _RETURNED_AT, time.monotonic())
    super()._put_conn(conn)

  def _get_conn(self, timeout: Optional[float] = None) -> Any:
    conn = super()._get_conn(timeout=timeout)
    returned_at = getattr(conn, _RETURNED_AT, None)
    if returned_at is not None and time.monotonic() - returned_at > self.max_idle_seconds:
      # Same idiom urllib3 uses for a dropped connection: close it and hand it
      # back. http.client reconnects on the next request when sock is None.
      conn.close()
    return conn


def _expiring_pool_classes(max_idle_seconds: float) -> dict:
  attrs = {"max_idle_seconds": max_idle_seconds}
  return {
    "http": type("CovalHTTPConnectionPool", (_IdleExpiryPoolMixin, HTTPConnectionPool), attrs),
    "https": type("CovalHTTPSConnectionPool", (_IdleExpiryPoolMixin, HTTPSConnectionPool), attrs),
  }


def _retry_policy(total_retries: int = 2) -> Retry:
  return Retry(
    total=total_retries,
    connect=total_retries,
    read=total_retries,
    status=total_retries,
    backoff_factor=0.2,
    status_forcelist=(408, 429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
    respect_retry_after_header=True,
  )


def _normalize_retries(retries: Optional[RetryConfig]) -> Union[Retry, bool]:
  if retries is None or retries is True:
    return _retry_policy()
  if retries is False:
    return False
  if isinstance(retries, int):
    if retries < 0:
      raise ValueError("CovalClient: retries must be non-negative")
    return _retry_policy(retries)
  return retries


class CovalClient:
  """Authenticated client exposing every public Coval v1 API surface."""

  def __init__(
    self,
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    retries: Optional[RetryConfig] = None,
    strict_response_validation: bool = False,
    max_idle_seconds: Optional[float] = DEFAULT_MAX_IDLE_SECONDS,
  ) -> None:
    if not api_key:
      raise ValueError("CovalClient: api_key is required")
    if max_idle_seconds is not None and max_idle_seconds <= 0:
      raise ValueError("CovalClient: max_idle_seconds must be positive, or None to disable")

    self.configuration = Configuration(
      host=base_url.rstrip("/"),
      retries=_normalize_retries(retries),
    )
    self.configuration.strict_response_validation = strict_response_validation
    self.api_client = ApiClient(self.configuration)
    self.api_client.set_default_header("x-api-key", api_key)

    # Must happen before the first request: PoolManager creates pools lazily and
    # caches them, so a pool built under the default classes would never expire.
    if max_idle_seconds is not None:
      self.api_client.rest_client.pool_manager.pool_classes_by_scheme = _expiring_pool_classes(
        max_idle_seconds
      )

    self.api_keys = APIKeysApi(self.api_client)
    self.agents = AgentsApi(self.api_client)
    self.audio = AudioApi(self.api_client)
    self.conversations = ConversationsApi(self.api_client)
    self.dashboards = DashboardsApi(self.api_client)
    self.integrations = IntegrationsApi(self.api_client)
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
