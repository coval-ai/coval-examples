from types import SimpleNamespace

import pytest

from coval_sdk import paginate


def test_paginate_handles_model_responses() -> None:
  calls = []

  def fetch_page(**kwargs):
    calls.append(kwargs)
    if not kwargs:
      return SimpleNamespace(agents=[1, 2], next_page_token="next")
    return SimpleNamespace(agents=[3], next_page_token=None)

  assert list(paginate(fetch_page, items_field="agents")) == [1, 2, 3]
  assert calls == [{}, {"page_token": "next"}]


def test_paginate_handles_dict_responses_and_custom_fields() -> None:
  def fetch_page(**kwargs):
    if kwargs.get("cursor") == "two":
      return {"rows": ["b"], "cursor": None}
    return {"rows": ["a"], "cursor": "two"}

  assert list(
    paginate(
      fetch_page,
      items_field="rows",
      token_field="cursor",
      token_parameter="cursor",
    )
  ) == ["a", "b"]


def test_paginate_rejects_repeated_tokens() -> None:
  def fetch_page(**kwargs):
    return {"items": [], "next_page_token": "same"}

  with pytest.raises(RuntimeError, match="Pagination token repeated"):
    list(paginate(fetch_page, items_field="items"))
