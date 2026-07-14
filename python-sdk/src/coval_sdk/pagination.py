"""Pagination helpers for generated Coval list operations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, TypeVar


T = TypeVar("T")


def _field(value: Any, name: str) -> Any:
  if isinstance(value, dict):
    return value.get(name)
  return getattr(value, name, None)


def paginate(
  fetch_page: Callable[..., Any],
  *,
  items_field: str,
  token_field: str = "next_page_token",
  token_parameter: str = "page_token",
  **kwargs: Any,
) -> Iterator[T]:
  """Yield items across token-paginated generated API responses."""
  request = dict(kwargs)
  seen_tokens: set[str] = set()

  while True:
    page = fetch_page(**request)
    yield from (_field(page, items_field) or [])

    token = _field(page, token_field)
    if not token:
      return
    token_string = str(token)
    if token_string in seen_tokens:
      raise RuntimeError(f"Pagination token repeated: {token_string}")
    seen_tokens.add(token_string)
    request[token_parameter] = token
