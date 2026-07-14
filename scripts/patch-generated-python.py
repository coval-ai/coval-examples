#!/usr/bin/env python3
"""Apply deterministic fixes and exports to the regenerated Python package."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INIT = REPO_ROOT / "python-sdk" / "src" / "coval_sdk" / "__init__.py"
API_CLIENT = REPO_ROOT / "python-sdk" / "src" / "coval_sdk" / "api_client.py"
MODELS = REPO_ROOT / "python-sdk" / "src" / "coval_sdk" / "models"

EXPORTS = (
  ("CovalClient", "from coval_sdk.client import CovalClient"),
  (
    "InvalidListItemWarning",
    "from coval_sdk.deserialization import InvalidListItemWarning",
  ),
  ("paginate", "from coval_sdk.pagination import paginate"),
)

MODEL_LIST = re.compile(
  r'\[(?P<model>[A-Za-z0-9_]+)\.from_dict\(_item\) for _item in obj\['
  r'(?P<quote>["\'])(?P<field>[^"\']+)(?P=quote)\]\]'
)


def patch_api_client() -> None:
  contents = API_CLIENT.read_text()
  import_line = "from coval_sdk.deserialization import invalid_list_item_policy\n"
  import_marker = "from coval_sdk.configuration import Configuration\n"
  return_line = "        return klass.from_dict(data)\n"
  replacement = (
    "        with invalid_list_item_policy(\n"
    "            strict=getattr(self.configuration, 'strict_response_validation', False)\n"
    "        ):\n"
    "            return klass.from_dict(data)\n"
  )

  if contents.count(import_marker) != 1 or contents.count(return_line) != 1:
    raise RuntimeError("Generated ApiClient deserialization anchors changed")
  contents = contents.replace(import_marker, f"{import_marker}{import_line}", 1)
  contents = contents.replace(return_line, replacement, 1)
  API_CLIENT.write_text(contents)


def patch_response_model_lists() -> int:
  patched = 0
  for path in sorted(MODELS.glob("*.py")):
    contents = path.read_text()
    class_match = re.search(r"^class (?P<name>[A-Za-z0-9_]+)\(BaseModel\):", contents, re.MULTILINE)
    if class_match is None:
      continue

    response_model = class_match.group("name")
    if "List" not in response_model and "History" not in response_model:
      continue

    def replace(match: re.Match[str]) -> str:
      nonlocal patched
      patched += 1
      field = match.group("field")
      quote = match.group("quote")
      model = match.group("model")
      return (
        f"deserialize_model_list(obj[{quote}{field}{quote}], {model}, "
        f'response_model="{response_model}", field="{field}")'
      )

    updated = MODEL_LIST.sub(replace, contents)
    if updated == contents:
      continue

    import_line = "from coval_sdk.deserialization import deserialize_model_list\n"
    if import_line not in updated:
      marker = "from __future__ import annotations\n"
      if marker not in updated:
        raise RuntimeError(f"Generated response model is missing future import: {path}")
      updated = updated.replace(marker, f"{marker}{import_line}", 1)
    path.write_text(updated)

  if patched == 0:
    raise RuntimeError("No generated response-model list deserializers were patched")
  return patched


def main() -> None:
  patch_api_client()
  patched_lists = patch_response_model_lists()
  contents = INIT.read_text()
  for name, import_line in EXPORTS:
    if f'    "{name}",' not in contents:
      marker = "__all__ = [\n"
      if marker not in contents:
        raise RuntimeError("Generated coval_sdk.__init__ no longer defines __all__")
      contents = contents.replace(marker, f'{marker}    "{name}",\n', 1)
    if import_line not in contents:
      contents = f"{contents.rstrip()}\n\n{import_line}\n"

  INIT.write_text(contents)
  print(f"  Patched ApiClient and {patched_lists} collection-response list deserializers.")
  print("  Exported CovalClient, InvalidListItemWarning, and paginate from coval_sdk.")


if __name__ == "__main__":
  main()
