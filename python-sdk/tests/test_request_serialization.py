"""Request bodies carry only what the caller set.

Update endpoints are PATCH, so any field that serializes without the caller setting it
overwrites the saved value. These tests pin that for the review project and metric
requests, including fields the weekly regeneration adds.
"""

import pytest

from coval_sdk import CovalClient
from coval_sdk.models import (
  CovalMetricsAPIAudioFilterConfig,
  CovalMetricsAPICreateMetricRequest,
  CovalMetricsAPIJudgeMode,
  CovalMetricsAPIMetricType,
  CovalMetricsAPIUpdateMetricRequest,
  CovalReviewsAPIUpdateReviewProjectRequest,
)

USER_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
AUDIO_FILTER = {"include": [{"type": "speaker", "speakers": ["persona"]}]}


def test_review_project_rename_sends_only_the_name() -> None:
  update = CovalReviewsAPIUpdateReviewProjectRequest
  client = CovalClient("test-key")
  try:
    assert update().to_dict() == {}
    assert update(display_name="Renamed project").to_dict() == {"display_name": "Renamed project"}
    # The body the client actually puts on the wire is built from to_dict().
    assert client.api_client.sanitize_for_serialization(update(display_name="Renamed project")) == {
      "display_name": "Renamed project"
    }
  finally:
    client.close()


def test_review_project_update_from_dict_does_not_default_omitted_booleans() -> None:
  body = CovalReviewsAPIUpdateReviewProjectRequest.from_dict({"display_name": "Renamed project"}).to_dict()

  assert body["display_name"] == "Renamed project"
  assert "allow_metric_skipping" not in body
  assert "enforced_collaboration" not in body


@pytest.mark.parametrize("field", ["allow_metric_skipping", "enforced_collaboration", "review_label_enabled"])
@pytest.mark.parametrize("value", [True, False])
def test_review_project_update_sends_explicit_booleans(field: str, value: bool) -> None:
  assert CovalReviewsAPIUpdateReviewProjectRequest(**{field: value}).to_dict() == {field: value}


def test_review_project_update_review_label_enabled_separates_omitted_from_null() -> None:
  update = CovalReviewsAPIUpdateReviewProjectRequest

  assert "review_label_enabled" not in update(display_name="Renamed project").to_dict()
  assert update(review_label_enabled=None).to_dict() == {"review_label_enabled": None}


def test_review_project_update_assignee_ids_are_sent_only_when_set() -> None:
  update = CovalReviewsAPIUpdateReviewProjectRequest
  renamed = update(display_name="Renamed project").to_dict()

  assert "assignee_user_ids" not in renamed
  assert "opted_out_assignee_user_ids" not in renamed
  assert update(assignee_user_ids=[USER_ID]).to_dict() == {"assignee_user_ids": [USER_ID]}
  assert update(opted_out_assignee_user_ids=[USER_ID]).to_dict() == {"opted_out_assignee_user_ids": [USER_ID]}
  # An empty list clears the opt-outs, so it has to reach the wire instead of being dropped like None.
  assert update(opted_out_assignee_user_ids=[]).to_dict() == {"opted_out_assignee_user_ids": []}


def test_metric_update_omits_unset_audio_filter_and_dependencies() -> None:
  assert CovalMetricsAPIUpdateMetricRequest().to_dict() == {}
  assert CovalMetricsAPIUpdateMetricRequest(metric_name="Renamed").to_dict() == {"metric_name": "Renamed"}


def test_metric_update_audio_filter_separates_omitted_null_and_value() -> None:
  update = CovalMetricsAPIUpdateMetricRequest

  # Omitted keeps the saved filter; null removes it; a value replaces it.
  assert "audio_filter" not in update(metric_name="Renamed").to_dict()
  assert update(audio_filter=None).to_dict() == {"audio_filter": None}
  assert update(audio_filter=CovalMetricsAPIAudioFilterConfig.from_dict(AUDIO_FILTER)).to_dict() == {
    "audio_filter": AUDIO_FILTER
  }
  assert update.from_dict({"audio_filter": AUDIO_FILTER}).to_dict()["audio_filter"] == AUDIO_FILTER


def test_metric_update_dependency_ids_separate_omitted_empty_and_value() -> None:
  update = CovalMetricsAPIUpdateMetricRequest

  # Omitted preserves the saved dependencies; [] clears them; a list replaces them.
  assert "dependency_metric_ids" not in update(metric_name="Renamed").to_dict()
  assert update(dependency_metric_ids=[]).to_dict() == {"dependency_metric_ids": []}
  assert update(dependency_metric_ids=["metric-a", "metric-b"]).to_dict() == {
    "dependency_metric_ids": ["metric-a", "metric-b"]
  }


def test_metric_create_sends_audio_filter_and_dependencies_only_when_set() -> None:
  create = CovalMetricsAPICreateMetricRequest
  base = {
    "metric_name": "Caller volume",
    "description": "Caller loudness",
    "metric_type": CovalMetricsAPIMetricType.METRIC_VOLUME,
    # Set explicitly: the generated default is the bare string 'STANDARD', which pydantic warns about.
    "judge_mode": CovalMetricsAPIJudgeMode.STANDARD,
  }

  plain = create(**base).to_dict()
  assert "audio_filter" not in plain
  assert "dependency_metric_ids" not in plain

  scoped = create(
    **base,
    audio_filter=CovalMetricsAPIAudioFilterConfig.from_dict(AUDIO_FILTER),
    dependency_metric_ids=["metric-a"],
  ).to_dict()
  assert scoped["audio_filter"] == AUDIO_FILTER
  assert scoped["dependency_metric_ids"] == ["metric-a"]
