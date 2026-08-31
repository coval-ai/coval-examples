import importlib
import logging
import socket
import threading
import time
from typing import Union

import pytest
from urllib3.util import Retry

import coval_sdk
from coval_sdk import CovalClient
from coval_sdk import api as generated_apis
from coval_sdk import models as generated_models
from coval_sdk.client import (
  API_PROPERTY_NAMES,
  DEFAULT_MAX_IDLE_SECONDS,
  _IdleExpiryPoolMixin,
)
from coval_sdk.models.submit_conversation200_response import SubmitConversation200Response
from coval_sdk.models.update_run200_response import UpdateRun200Response


def test_client_exposes_every_generated_api() -> None:
  client = CovalClient("test-key")
  try:
    assert client.configuration.host == "https://api.coval.dev/v1"
    assert client.api_client.default_headers["x-api-key"] == "test-key"
    assert all(getattr(client, name) is not None for name in API_PROPERTY_NAMES)
    exposed_api_names = {type(getattr(client, name)).__name__ for name in API_PROPERTY_NAMES}
    generated_api_names = {name for name in dir(generated_apis) if name.endswith("Api")}
    assert exposed_api_names == generated_api_names
  finally:
    client.close()


@pytest.mark.parametrize(
  ("old_module", "old_name", "new_name"),
  (
    (
      "coval_metrics_api_error_response_error_details_inner",
      "CovalMetricsAPIErrorResponseErrorDetailsInner",
      "CovalAlertsAPIErrorResponseErrorDetailsInner",
    ),
    (
      "coval_monitors_api_error_response_error",
      "CovalMonitorsAPIErrorResponseError",
      "CovalAlertsAPIErrorResponseError",
    ),
    (
      "coval_monitors_api_monitor_event_resource_condition_results_inner",
      "CovalMonitorsAPIMonitorEventResourceConditionResultsInner",
      "CovalAlertsAPIAlertEventResourceConditionResultsInner",
    ),
    (
      "coval_monitors_api_monitor_event_resource_condition_results_inner_computed_value",
      "CovalMonitorsAPIMonitorEventResourceConditionResultsInnerComputedValue",
      "CovalAlertsAPIAlertEventResourceConditionResultsInnerComputedValue",
    ),
    (
      "coval_monitors_api_monitor_event_resource_dispatched_channels_inner",
      "CovalMonitorsAPIMonitorEventResourceDispatchedChannelsInner",
      "CovalAlertsAPIAlertEventResourceDispatchedChannelsInner",
    ),
  ),
)
def test_renamed_models_keep_compatibility_aliases(
  old_module: str, old_name: str, new_name: str
) -> None:
  new_model = getattr(coval_sdk, new_name)
  assert getattr(coval_sdk, old_name) is new_model
  assert getattr(generated_models, old_name) is new_model

  compatibility_module = importlib.import_module(f"coval_sdk.models.{old_module}")
  assert getattr(compatibility_module, old_name) is new_model


def test_generated_apis_share_the_canonical_v1_base_path() -> None:
  client = CovalClient("test-key")
  serialize_args = {
    "filter": None,
    "page_size": None,
    "page_token": None,
    "order_by": None,
    "tag_filters": None,
    "_request_auth": None,
    "_content_type": None,
    "_headers": None,
    "_host_index": 0,
  }
  try:
    agents_request = client.agents._list_agents_serialize(**serialize_args)
    test_sets_request = client.test_sets._list_test_sets_serialize(**serialize_args)
    assert agents_request[1] == "https://api.coval.dev/v1/agents"
    assert test_sets_request[1] == "https://api.coval.dev/v1/test-sets"
  finally:
    client.close()


def test_default_retries_only_idempotent_methods() -> None:
  client = CovalClient("test-key")
  try:
    retries = client.configuration.retries
    assert isinstance(retries, Retry)
    assert retries.total == 2
    assert retries.allowed_methods == frozenset({"GET", "HEAD", "OPTIONS"})
    assert retries.status_forcelist == (408, 429, 500, 502, 503, 504)
  finally:
    client.close()


@pytest.mark.parametrize(("configured", "total"), [(True, 2), (4, 4)])
def test_retry_shortcuts_preserve_safe_methods(configured: Union[bool, int], total: int) -> None:
  client = CovalClient("test-key", retries=configured)
  try:
    retries = client.configuration.retries
    assert isinstance(retries, Retry)
    assert retries.total == total
    assert retries.allowed_methods == frozenset({"GET", "HEAD", "OPTIONS"})
  finally:
    client.close()


def test_retry_count_must_be_non_negative() -> None:
  with pytest.raises(ValueError, match="retries must be non-negative"):
    CovalClient("test-key", retries=-1)


def test_client_requires_api_key() -> None:
  with pytest.raises(ValueError, match="api_key is required"):
    CovalClient("")


def test_client_can_restore_strict_response_validation() -> None:
  client = CovalClient("test-key", strict_response_validation=True)
  try:
    assert client.configuration.strict_response_validation is True
  finally:
    client.close()


def test_top_level_exports_and_version_match() -> None:
  assert coval_sdk.CovalClient is CovalClient
  assert coval_sdk.__version__ == "0.7.0"


def test_update_run_response_preserves_simulation_run_fields() -> None:
  response = UpdateRun200Response.from_dict(
    {
      "run": {
        "name": "runs/5BhqoFdXSuk6IcugFvkXeU",
        "run_id": "5BhqoFdXSuk6IcugFvkXeU",
        "status": "COMPLETED",
        "create_time": "2025-10-14T12:00:00Z",
        "tags": ["baseline"],
      }
    }
  )

  assert response is not None
  assert response.run.status.value == "COMPLETED"


def test_update_run_response_handles_monitoring_tag_confirmation() -> None:
  response = UpdateRun200Response.from_dict(
    {
      "run": {
        "name": "runs/6CirpGeYTvl7JdvhGwlYfV",
        "run_id": "6CirpGeYTvl7JdvhGwlYfV",
        "tags": ["monitoring"],
      }
    }
  )

  assert response is not None
  assert response.run.tags == ["monitoring"]


def test_submit_conversation_response_preserves_conversation_fields() -> None:
  response = SubmitConversation200Response.from_dict(
    {
      "conversation": {
        "name": "conversations/5BhqoFdXSuk6IcugFvkXeU",
        "conversation_id": "5BhqoFdXSuk6IcugFvkXeU",
        "status": "IN_QUEUE",
      }
    }
  )

  assert response.conversation.conversation_id == "5BhqoFdXSuk6IcugFvkXeU"
  assert isinstance(
    response.actual_instance,
    generated_models.CovalConversationsAPISubmitConversationResponse,
  )


def test_submit_conversation_response_surfaces_simulation_filter_result() -> None:
  response = SubmitConversation200Response.from_dict(
    {
      "filtered": True,
      "reason": "Submit matched the organization's simulation filter.",
      "simulation_id": "6CirpGeYTvl7JdvhGwlYfV",
    }
  )

  assert response.filtered is True
  assert response.simulation_id == "6CirpGeYTvl7JdvhGwlYfV"
  assert isinstance(
    response.actual_instance,
    generated_models.CovalConversationsAPIFilteredSubmitResponse,
  )


def test_review_project_update_preserves_omitted_collaboration_setting() -> None:
  update_model = generated_models.CovalReviewsAPIUpdateReviewProjectRequest

  assert update_model(display_name="Renamed project").to_dict() == {"display_name": "Renamed project"}
  assert "enforced_collaboration" not in update_model.from_dict(
    {"display_name": "Renamed project"}
  ).to_dict()
  assert update_model(enforced_collaboration=False).to_dict() == {"enforced_collaboration": False}
  assert update_model.from_dict({"enforced_collaboration": False}).to_dict()["enforced_collaboration"] is False


def _pool_for(client: CovalClient, url: str):
  return client.api_client.rest_client.pool_manager.connection_from_url(url)


def _pooled_conn(pool, monkeypatch):
  """A pooled connection that looks live, so urllib3's own dropped-connection
  check leaves it alone and only the idle-expiry logic can close it."""
  conn = pool._get_conn()  # take a slot; the pool starts full of placeholders
  ours, peer = socket.socketpair()
  monkeypatch.setattr(conn, "sock", ours, raising=False)
  closed = []
  monkeypatch.setattr(conn, "close", lambda: closed.append(True))
  return conn, closed, (ours, peer)


def test_idle_expiry_is_wired_by_default() -> None:
  client = CovalClient("test-key")
  try:
    pool = _pool_for(client, "https://api.coval.dev/v1")
    assert isinstance(pool, _IdleExpiryPoolMixin)
    assert pool.max_idle_seconds == DEFAULT_MAX_IDLE_SECONDS
  finally:
    client.close()


def test_idle_expiry_can_be_disabled() -> None:
  client = CovalClient("test-key", max_idle_seconds=None)
  try:
    assert not isinstance(_pool_for(client, "https://api.coval.dev/v1"), _IdleExpiryPoolMixin)
  finally:
    client.close()


@pytest.mark.parametrize("value", [0, -1.0])
def test_non_positive_max_idle_seconds_is_rejected(value: float) -> None:
  with pytest.raises(ValueError, match="max_idle_seconds"):
    CovalClient("test-key", max_idle_seconds=value)


def test_pool_discards_a_connection_idle_past_the_bound(monkeypatch) -> None:
  client = CovalClient("test-key", max_idle_seconds=5.0)
  try:
    pool = _pool_for(client, "https://api.coval.dev/v1")
    now = 1000.0
    monkeypatch.setattr("coval_sdk.client.time.monotonic", lambda: now)

    conn, closed, socks = _pooled_conn(pool, monkeypatch)
    pool._put_conn(conn)

    now += 5.5  # idle longer than max_idle_seconds
    assert pool._get_conn() is conn
    assert closed == [True]
    for sock in socks:
      sock.close()
  finally:
    client.close()


def test_pool_keeps_a_connection_still_within_the_bound(monkeypatch) -> None:
  client = CovalClient("test-key", max_idle_seconds=5.0)
  try:
    pool = _pool_for(client, "https://api.coval.dev/v1")
    now = 1000.0
    monkeypatch.setattr("coval_sdk.client.time.monotonic", lambda: now)

    conn, closed, socks = _pooled_conn(pool, monkeypatch)
    pool._put_conn(conn)

    now += 1.0  # still fresh
    assert pool._get_conn() is conn
    assert closed == []
    for sock in socks:
      sock.close()
  finally:
    client.close()


def test_connection_stats_start_at_zero_and_are_exposed() -> None:
  client = CovalClient("test-key")
  try:
    assert client.connection_stats.as_dict() == {"opened": 0, "reused": 0, "expired": 0}
    assert "opened=0" in repr(client.connection_stats)
  finally:
    client.close()


def test_connection_stats_are_absent_when_expiry_is_disabled() -> None:
  client = CovalClient("test-key", max_idle_seconds=None)
  try:
    assert client.connection_stats is None
  finally:
    client.close()


def test_connection_stats_count_each_outcome(monkeypatch) -> None:
  client = CovalClient("test-key", max_idle_seconds=5.0)
  try:
    pool = _pool_for(client, "https://api.coval.dev/v1")
    now = 1000.0
    monkeypatch.setattr("coval_sdk.client.time.monotonic", lambda: now)

    conn, _, socks = _pooled_conn(pool, monkeypatch)
    assert client.connection_stats.as_dict()["opened"] == 1

    pool._put_conn(conn)
    now += 1.0  # fresh
    pool._get_conn()
    assert client.connection_stats.as_dict()["reused"] == 1

    pool._put_conn(conn)
    now += 5.5  # stale
    pool._get_conn()
    assert client.connection_stats.as_dict() == {"opened": 1, "reused": 1, "expired": 1}
    for sock in socks:
      sock.close()
  finally:
    client.close()


def test_connections_established_counts_expiry_replacements() -> None:
  """An expired connection reconnects, so it costs a handshake like a fresh one.
  Asserted against the sockets a real server accepts, not just the counters."""
  response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\n{}"
  accepted = []

  def handle(conn: socket.socket) -> None:
    while True:
      try:
        if not conn.recv(65535):
          return
      except OSError:
        return
      conn.sendall(response)

  def serve(sock: socket.socket) -> None:
    while True:
      try:
        conn, addr = sock.accept()
      except OSError:
        return
      accepted.append(addr)
      threading.Thread(target=handle, args=(conn,), daemon=True).start()

  server = socket.socket()
  server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  server.bind(("127.0.0.1", 0))
  server.listen(8)
  port = server.getsockname()[1]
  threading.Thread(target=serve, args=(server,), daemon=True).start()

  client = CovalClient(
    "test-key", base_url=f"http://127.0.0.1:{port}/v1", retries=False, max_idle_seconds=0.25
  )
  try:
    url = f"http://127.0.0.1:{port}/v1/ping"
    rest = client.api_client.rest_client
    rest.request("GET", url, _request_timeout=5.0).read()
    time.sleep(0.5)  # let the pooled connection go stale
    rest.request("GET", url, _request_timeout=5.0).read()

    stats = client.connection_stats
    assert stats.as_dict() == {"opened": 1, "reused": 0, "expired": 1}
    # opened alone would under-report; the server saw two sockets.
    assert stats.connections_established == len(accepted) == 2
  finally:
    client.close()
    server.close()


def test_expiry_emits_a_debug_log(monkeypatch, caplog) -> None:
  client = CovalClient("test-key", max_idle_seconds=5.0)
  try:
    pool = _pool_for(client, "https://api.coval.dev/v1")
    now = 1000.0
    monkeypatch.setattr("coval_sdk.client.time.monotonic", lambda: now)
    conn, _, socks = _pooled_conn(pool, monkeypatch)
    pool._put_conn(conn)
    now += 9.0

    with caplog.at_level(logging.DEBUG, logger="coval_sdk.client"):
      pool._get_conn()

    messages = [r.getMessage() for r in caplog.records]
    assert any("discarding pooled connection" in m for m in messages)
    assert any("9.00s idle" in m for m in messages)
    for sock in socks:
      sock.close()
  finally:
    client.close()


def test_a_silently_stranded_connection_is_not_reused() -> None:
  """Regression: a peer that stops answering without closing used to stall the
  next request until the read timeout instead of yielding a fresh connection."""
  response = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    b"Content-Length: 2\r\nConnection: keep-alive\r\n\r\n{}"
  )
  accepted = []

  def handle(conn: socket.socket) -> None:
    served = 0
    while True:
      try:
        if not conn.recv(65535):
          return
      except OSError:
        return
      served += 1
      if served == 1:
        conn.sendall(response)
      else:
        return  # strand: answer nothing further

  def serve(sock: socket.socket) -> None:
    while True:
      try:
        conn, addr = sock.accept()
      except OSError:
        return
      accepted.append(addr)
      threading.Thread(target=handle, args=(conn,), daemon=True).start()

  server = socket.socket()
  server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  server.bind(("127.0.0.1", 0))
  server.listen(8)
  port = server.getsockname()[1]
  threading.Thread(target=serve, args=(server,), daemon=True).start()

  client = CovalClient(
    "test-key", base_url=f"http://127.0.0.1:{port}/v1", retries=False, max_idle_seconds=0.25
  )
  try:
    url = f"http://127.0.0.1:{port}/v1/ping"
    rest = client.api_client.rest_client
    rest.request("GET", url, _request_timeout=5.0).read()
    assert len(accepted) == 1

    time.sleep(0.5)  # exceed max_idle_seconds so the pooled socket is stale
    rest.request("GET", url, _request_timeout=5.0).read()
    assert len(accepted) == 2, "stale pooled connection was reused instead of replaced"
  finally:
    client.close()
    server.close()
