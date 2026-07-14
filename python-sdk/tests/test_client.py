from urllib3.util import Retry

import pytest

import coval_sdk
from coval_sdk import CovalClient
from coval_sdk import api as generated_apis


API_PROPERTIES = (
  "api_keys",
  "agents",
  "audio",
  "conversations",
  "dashboards",
  "metric_outputs",
  "metrics",
  "monitor_events",
  "monitors",
  "mutations",
  "organization_conversations_config",
  "personas",
  "reports",
  "review_annotations",
  "review_projects",
  "run_templates",
  "runs",
  "scheduled_runs",
  "simulations",
  "tags",
  "test_cases",
  "test_sets",
  "traces",
  "webhooks",
  "widgets",
)


def test_client_exposes_every_generated_api() -> None:
  client = CovalClient("test-key")
  try:
    assert client.configuration.host == "https://api.coval.dev/v1"
    assert client.api_client.default_headers["x-api-key"] == "test-key"
    assert all(getattr(client, name) is not None for name in API_PROPERTIES)
    exposed_api_names = {type(getattr(client, name)).__name__ for name in API_PROPERTIES}
    generated_api_names = {name for name in dir(generated_apis) if name.endswith("Api")}
    assert exposed_api_names == generated_api_names
  finally:
    client.close()


def test_default_retries_only_idempotent_methods() -> None:
  client = CovalClient("test-key")
  try:
    retries = client.configuration.retries
    assert isinstance(retries, Retry)
    assert retries.allowed_methods == frozenset({"GET", "HEAD", "OPTIONS"})
    assert retries.status_forcelist == (408, 429, 500, 502, 503, 504)
  finally:
    client.close()


def test_client_requires_api_key() -> None:
  with pytest.raises(ValueError, match="api_key is required"):
    CovalClient("")


def test_top_level_exports_and_version_match() -> None:
  assert coval_sdk.CovalClient is CovalClient
  assert coval_sdk.__version__ == "0.3.0"
