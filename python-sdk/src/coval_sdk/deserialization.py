"""Resilient helpers for generated response-model deserialization."""

from __future__ import annotations

import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Iterator, List, Type, TypeVar, cast

from pydantic import BaseModel, ValidationError


ModelT = TypeVar("ModelT", bound=BaseModel)
_strict_response_validation: ContextVar[bool] = ContextVar(
  "coval_sdk_strict_response_validation",
  default=True,
)


class InvalidListItemWarning(UserWarning):
  """A malformed resource was omitted from a typed list response."""


@contextmanager
def invalid_list_item_policy(*, strict: bool) -> Iterator[None]:
  """Set list-item validation behavior for one response deserialization."""
  token = _strict_response_validation.set(strict)
  try:
    yield
  finally:
    _strict_response_validation.reset(token)


def _warn_invalid_item(model: Type[ModelT], response_model: str, field: str, index: int) -> None:
  warnings.warn(
    (
      f"Omitted invalid {model.__name__} from "
      f"{response_model}.{field}[{index}] after validation failed"
    ),
    InvalidListItemWarning,
    stacklevel=3,
  )


def deserialize_model_list(
  items: Iterable[object],
  model: Type[ModelT],
  *,
  response_model: str,
  field: str,
) -> List[ModelT]:
  """Deserialize model items independently so one malformed resource is isolated."""
  if not isinstance(items, list):
    return cast(List[ModelT], items)

  deserialized: List[ModelT] = []
  for index, item in enumerate(items):
    try:
      value = model.from_dict(item)
    except ValidationError:
      if _strict_response_validation.get():
        raise
      _warn_invalid_item(model, response_model, field, index)
      continue

    if value is None and not _strict_response_validation.get():
      _warn_invalid_item(model, response_model, field, index)
      continue
    deserialized.append(value)
  return deserialized
