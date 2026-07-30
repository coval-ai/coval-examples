"""Ergonomic entry point for the Coval Python SDK."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional, Union

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

logger = logging.getLogger(__name__)


class ConnectionStats:
  """Counts of how the connection pool has been used.

  Transport problems (a stranded socket, a reaped keep-alive) are invisible
  server-side because the request never arrives, so these counters are often the
  only evidence available when diagnosing a report of intermittent timeouts.
  Read via ``client.connection_stats``.

  Each counter records the outcome of one attempt to take a connection from the
  pool, so exactly one of the three is incremented per attempt and together they
  total the number of attempts.

  Note that ``opened`` is not the number of TCP connections established: an
  expired connection is closed and then transparently reconnects, which costs a
  fresh handshake too. Use ``connections_established`` for that.
  """

  __slots__ = ("_lock", "opened", "reused", "expired")

  def __init__(self) -> None:
    self._lock = threading.Lock()
    self.opened = 0  # no pooled connection was available, so a new one was made
    self.reused = 0  # a pooled connection was still within max_idle_seconds
    self.expired = 0  # a pooled connection was too old and was discarded

  def _record(self, outcome: str) -> None:
    with self._lock:
      setattr(self, outcome, getattr(self, outcome) + 1)

  @property
  def connections_established(self) -> int:
    """TCP connections actually opened: fresh ones plus expiry replacements."""
    with self._lock:
      return self.opened + self.expired

  def as_dict(self) -> Dict[str, int]:
    with self._lock:
      return {"opened": self.opened, "reused": self.reused, "expired": self.expired}

  def __repr__(self) -> str:
    counts = self.as_dict()
    return "ConnectionStats({})".format(", ".join(f"{k}={v}" for k, v in counts.items()))


class _IdleExpiryPoolMixin:
  """Close pooled connections that have been idle longer than max_idle_seconds."""

  max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS
  connection_stats: Optional[ConnectionStats] = None

  def _put_conn(self, conn: Any) -> None:
    if conn is not None:
      setattr(conn, _RETURNED_AT, time.monotonic())
    super()._put_conn(conn)

  def _get_conn(self, timeout: Optional[float] = None) -> Any:
    conn = super()._get_conn(timeout=timeout)
    returned_at = getattr(conn, _RETURNED_AT, None)

    if returned_at is None:
      self._record("opened")
      return conn

    idle_seconds = time.monotonic() - returned_at
    if idle_seconds > self.max_idle_seconds:
      self._record("expired")
      logger.debug(
        "coval-sdk: discarding pooled connection to %s after %.2fs idle (max_idle_seconds=%.2f)",
        self.host,
        idle_seconds,
        self.max_idle_seconds,
      )
      # Same idiom urllib3 uses for a dropped connection: close it and hand it
      # back. http.client reconnects on the next request when sock is None.
      conn.close()
    else:
      self._record("reused")
    return conn

  def _record(self, outcome: str) -> None:
    if self.connection_stats is not None:
      self.connection_stats._record(outcome)


def _expiring_pool_classes(max_idle_seconds: float, stats: Optional[ConnectionStats] = None) -> dict:
  attrs = {"max_idle_seconds": max_idle_seconds, "connection_stats": stats}
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
    self.connection_stats: Optional[ConnectionStats] = None
    if max_idle_seconds is not None:
      self.connection_stats = ConnectionStats()
      self.api_client.rest_client.pool_manager.pool_classes_by_scheme = _expiring_pool_classes(
        max_idle_seconds, self.connection_stats
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
