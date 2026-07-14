import json
import warnings
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from coval_sdk import CovalClient, InvalidListItemWarning
from coval_sdk.deserialization import deserialize_model_list
from coval_sdk.models.list_test_sets200_response import ListTestSets200Response
from coval_sdk.models.test_sets_api_test_set_resource import (
  TestSetsAPITestSetResource as ResourceModel,
)


def _test_sets_payload():
  return {
    "test_sets": [
      {"id": "12345678", "display_name": "valid"},
      {"id": "short", "display_name": "invalid"},
      {"id": "87654321", "display_name": "also valid"},
    ],
    "next_page_token": "next",
  }


def _deserialize_test_sets(payload, *, strict=False):
  raw_response = SimpleNamespace(
    data=json.dumps(payload).encode(),
    status=200,
    headers={"content-type": "application/json"},
  )
  client = CovalClient("test-key", strict_response_validation=strict)
  try:
    return client.api_client.response_deserialize(
      response_data=raw_response,
      response_types_map={"200": "ListTestSets200Response"},
    )
  finally:
    client.close()


def test_api_client_isolates_invalid_list_resource_without_exposing_its_value() -> None:
  payload = _test_sets_payload()
  payload["test_sets"][1]["id"] = "DO_NOT_EXPOSE_THIS_VALUE"
  with warnings.catch_warnings(record=True) as seen:
    warnings.simplefilter("always")
    response = _deserialize_test_sets(payload)

  assert [test_set.id for test_set in response.data.test_sets] == ["12345678", "87654321"]
  assert len(seen) == 1
  assert issubclass(seen[0].category, InvalidListItemWarning)
  assert "DO_NOT_EXPOSE_THIS_VALUE" not in str(seen[0].message)


def test_invalid_single_resource_still_raises_validation_error() -> None:
  with pytest.raises(ValidationError):
    ResourceModel.from_dict({"id": "short"})


def test_strict_response_validation_restores_fail_fast_behavior() -> None:
  with pytest.raises(ValidationError):
    _deserialize_test_sets(_test_sets_payload(), strict=True)


def test_invalid_top_level_response_still_raises_validation_error() -> None:
  with pytest.raises(ValidationError):
    ListTestSets200Response.from_dict([])


@pytest.mark.parametrize("invalid_items", ["not-a-list", {"id": "12345678"}])
def test_invalid_list_container_still_raises_validation_error(invalid_items) -> None:
  payload = _test_sets_payload()
  payload["test_sets"] = invalid_items
  with pytest.raises(ValidationError):
    _deserialize_test_sets(payload)


def test_unexpected_deserializer_error_is_not_masked() -> None:
  class BrokenModel:
    @classmethod
    def from_dict(cls, _item):
      raise TypeError("broken generated model")

  with pytest.raises(TypeError, match="broken generated model"):
    deserialize_model_list(
      [{}],
      BrokenModel,
      response_model="BrokenResponse",
      field="items",
    )


def test_warning_can_restore_strict_behavior() -> None:
  with warnings.catch_warnings():
    warnings.simplefilter("error", InvalidListItemWarning)
    with pytest.raises(InvalidListItemWarning):
      _deserialize_test_sets(_test_sets_payload())
