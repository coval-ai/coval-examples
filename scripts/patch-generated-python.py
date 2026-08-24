#!/usr/bin/env python3
"""Apply deterministic fixes and exports to the regenerated Python package."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT = REPO_ROOT / "python-sdk" / "src" / "coval_sdk" / "__init__.py"
API_CLIENT = REPO_ROOT / "python-sdk" / "src" / "coval_sdk" / "api_client.py"
API_INIT = REPO_ROOT / "python-sdk" / "src" / "coval_sdk" / "api" / "__init__.py"
CLIENT = REPO_ROOT / "python-sdk" / "src" / "coval_sdk" / "client.py"
MODELS = REPO_ROOT / "python-sdk" / "src" / "coval_sdk" / "models"
MODELS_INIT = MODELS / "__init__.py"

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

API_IMPORT = re.compile(
  r"^from coval_sdk\.api\.(?P<module>[a-z0-9_]+) import (?P<class_name>[A-Za-z0-9]+Api)$",
  re.MULTILINE,
)

COMPATIBILITY_MODEL_ALIASES = (
  (
    "coval_metrics_api_error_response_error_details_inner",
    "CovalMetricsAPIErrorResponseErrorDetailsInner",
    "coval_alerts_api_error_response_error_details_inner",
    "CovalAlertsAPIErrorResponseErrorDetailsInner",
  ),
  (
    "coval_monitors_api_error_response_error",
    "CovalMonitorsAPIErrorResponseError",
    "coval_alerts_api_error_response_error",
    "CovalAlertsAPIErrorResponseError",
  ),
  (
    "coval_monitors_api_monitor_event_resource_condition_results_inner",
    "CovalMonitorsAPIMonitorEventResourceConditionResultsInner",
    "coval_alerts_api_alert_event_resource_condition_results_inner",
    "CovalAlertsAPIAlertEventResourceConditionResultsInner",
  ),
  (
    "coval_monitors_api_monitor_event_resource_condition_results_inner_computed_value",
    "CovalMonitorsAPIMonitorEventResourceConditionResultsInnerComputedValue",
    "coval_alerts_api_alert_event_resource_condition_results_inner_computed_value",
    "CovalAlertsAPIAlertEventResourceConditionResultsInnerComputedValue",
  ),
  (
    "coval_monitors_api_monitor_event_resource_dispatched_channels_inner",
    "CovalMonitorsAPIMonitorEventResourceDispatchedChannelsInner",
    "coval_alerts_api_alert_event_resource_dispatched_channels_inner",
    "CovalAlertsAPIAlertEventResourceDispatchedChannelsInner",
  ),
)

# Preserve omission for optional partial-update fields, regardless of OpenAPI defaults.
UPDATE_REQUEST_OMISSION_FIELDS = (
  ("coval_reviews_api_update_review_project_request.py", "enforced_collaboration"),
)


def replace_marked_block(contents: str, start_marker: str, end_marker: str, lines: list[str]) -> str:
  if contents.count(start_marker) != 1 or contents.count(end_marker) != 1:
    raise RuntimeError(f"Expected exactly one {start_marker!r}/{end_marker!r} block")

  marker_start = contents.index(start_marker)
  body_start = contents.index("\n", marker_start) + 1
  body_end = contents.index(end_marker, body_start)
  body = "\n".join(lines)
  return f"{contents[:body_start]}{body}\n{contents[body_end:]}"


def patch_client_api_surface() -> int:
  apis = []
  for match in API_IMPORT.finditer(API_INIT.read_text()):
    module = match.group("module")
    if not module.endswith("_api"):
      raise RuntimeError(f"Generated API module does not end in _api: {module}")
    apis.append((module.removesuffix("_api"), match.group("class_name")))

  if not apis:
    raise RuntimeError("No generated API imports found")
  if len({property_name for property_name, _ in apis}) != len(apis):
    raise RuntimeError("Generated API modules produced duplicate client property names")

  contents = CLIENT.read_text()
  contents = replace_marked_block(
    contents,
    "  # sdk-api-imports:start",
    "  # sdk-api-imports:end",
    [f"  {class_name}," for _, class_name in apis],
  )
  contents = replace_marked_block(
    contents,
    "  # sdk-api-property-names:start",
    "  # sdk-api-property-names:end",
    [f'  "{property_name}",' for property_name, _ in apis],
  )
  contents = replace_marked_block(
    contents,
    "    # sdk-api-properties:start",
    "    # sdk-api-properties:end",
    [
      f"    self.{property_name} = {class_name}(self.api_client)"
      for property_name, class_name in apis
    ],
  )
  CLIENT.write_text(contents)
  return len(apis)


def add_top_level_export(contents: str, name: str, import_line: str) -> str:
  if f'    "{name}",' not in contents:
    marker = "__all__ = [\n"
    if marker not in contents:
      raise RuntimeError("Generated coval_sdk.__init__ no longer defines __all__")
    contents = contents.replace(marker, f'{marker}    "{name}",\n', 1)
  if import_line not in contents:
    contents = f"{contents.rstrip()}\n\n{import_line}\n"
  return contents


def patch_compatibility_model_aliases() -> int:
  models_init_contents = MODELS_INIT.read_text()
  top_level_contents = INIT.read_text()
  patched = 0

  for old_module, old_name, new_module, new_name in COMPATIBILITY_MODEL_ALIASES:
    old_path = MODELS / f"{old_module}.py"
    new_path = MODELS / f"{new_module}.py"
    alias_import = f"from coval_sdk.models.{new_module} import {new_name} as {old_name}"

    if old_path.exists():
      old_contents = old_path.read_text()
      if alias_import not in old_contents and f"class {old_name}(" not in old_contents:
        raise RuntimeError(f"Existing compatibility model has an unexpected shape: {old_path}")
    else:
      if not new_path.exists():
        raise RuntimeError(f"Compatibility target does not exist: {new_path}")
      old_path.write_text(
        f'"""Backward-compatible alias for :class:`{new_name}`."""\n\n'
        f"{alias_import}\n\n"
        f'__all__ = ["{old_name}"]\n'
      )
      patched += 1

    models_import = f"from coval_sdk.models.{old_module} import {old_name}"
    if models_import not in models_init_contents:
      models_init_contents = f"{models_init_contents.rstrip()}\n{models_import}\n"

    top_level_contents = add_top_level_export(top_level_contents, old_name, models_import)

  MODELS_INIT.write_text(models_init_contents)
  INIT.write_text(top_level_contents)
  return patched


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

  if replacement in contents:
    if import_line not in contents:
      raise RuntimeError("Patched ApiClient is missing invalid_list_item_policy import")
    return
  if contents.count(import_marker) != 1 or contents.count(return_line) != 1:
    raise RuntimeError("Generated ApiClient deserialization anchors changed")
  if import_line not in contents:
    contents = contents.replace(import_marker, f"{import_marker}{import_line}", 1)
  contents = contents.replace(return_line, replacement, 1)
  API_CLIENT.write_text(contents)


def patch_response_model_lists() -> int:
  handled = 0
  for path in sorted(MODELS.glob("*.py")):
    contents = path.read_text()
    class_match = re.search(r"^class (?P<name>[A-Za-z0-9_]+)\(BaseModel\):", contents, re.MULTILINE)
    if class_match is None:
      continue

    response_model = class_match.group("name")
    if "List" not in response_model and "History" not in response_model:
      continue

    existing_calls = contents.count("deserialize_model_list(")
    if existing_calls:
      handled += existing_calls
      continue

    def replace(match: re.Match[str], response_model: str = response_model) -> str:
      nonlocal handled
      handled += 1
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

  if handled == 0:
    raise RuntimeError("No generated response-model list deserializers were patched")
  return handled


def patch_missing_list_import() -> int:
  # The generator derives the typing import from field types, but __properties always
  # annotates ClassVar[List[str]] -- models with no list-typed field end up using List
  # without importing it. Harmless at import time (annotations are deferred and pydantic
  # skips ClassVar) but get_type_hints() raises NameError.
  patched = 0
  for path in sorted(MODELS.glob("*.py")):
    contents = path.read_text()
    if "ClassVar[List[" not in contents:
      continue
    if re.search(r"^from typing import .*\bList\b", contents, re.MULTILINE):
      continue

    match = re.search(r"^from typing import (.+)$", contents, re.MULTILINE)
    if match is None:
      raise RuntimeError(f"Generated model uses ClassVar[List] with no typing import: {path}")

    names = sorted({name.strip() for name in match.group(1).split(",")} | {"List"})
    contents = f"{contents[: match.start()]}from typing import {', '.join(names)}{contents[match.end() :]}"
    path.write_text(contents)
    patched += 1
  return patched


def patch_update_request_omission_fields() -> int:
  ensured = 0
  for filename, field_name in UPDATE_REQUEST_OMISSION_FIELDS:
    path = MODELS / filename
    if not path.exists():
      raise RuntimeError(f"Generated update-request model is missing: {path}")

    contents = path.read_text()
    default_pattern = re.compile(
      rf"^(?P<prefix>\s*{re.escape(field_name)}: .* = Field\(default=)"
      r"(?P<default>False|None)(?P<suffix>, description=.*)$",
      re.MULTILINE,
    )
    default_match = default_pattern.search(contents)
    if default_match is None:
      raise RuntimeError(
        f"Generated update-request default anchor changed: {path}:{field_name}"
      )
    if default_match.group("default") == "False":
      contents = (
        f"{contents[:default_match.start()]}{default_match.group('prefix')}None"
        f"{default_match.group('suffix')}{contents[default_match.end():]}"
      )

    fallback = f'"{field_name}": obj.get("{field_name}") if obj.get("{field_name}") is not None else False'
    direct = f'"{field_name}": obj.get("{field_name}")'
    if fallback in contents:
      contents = contents.replace(fallback, direct, 1)
    elif direct not in contents:
      raise RuntimeError(
        f"Generated update-request deserialization anchor changed: {path}:{field_name}"
      )

    path.write_text(contents)
    ensured += 1
  return ensured


def patch_update_run_response_compatibility() -> None:
  path = MODELS / "update_run200_response_run.py"
  if not path.exists():
    raise RuntimeError(f"Generated update-run response union is missing: {path}")

  contents = path.read_text()
  delegate = (
    "    def __getattr__(self, name: str) -> Any:\n"
    "        actual_instance = self.__dict__.get(\"actual_instance\")\n"
    "        if actual_instance is not None:\n"
    "            return getattr(actual_instance, name)\n"
    "        raise AttributeError(name)\n\n"
  )
  from_dict = (
    "    @classmethod\n"
    "    def from_dict(cls, obj: Union[str, Dict[str, Any]]) -> Self:\n"
    "        if isinstance(obj, str):\n"
    "            obj = json.loads(obj)\n"
    "        if not isinstance(obj, dict):\n"
    "            raise TypeError(\"UpdateRun200ResponseRun must be an object\")\n"
    "        if \"status\" in obj or \"create_time\" in obj:\n"
    "            return cls(CovalRunsAPIRunResource.from_dict(obj))\n"
    "        return cls(CovalRunsAPIMonitoringRunTagUpdateResource.from_dict(obj))\n\n"
  )
  from_json = (
    "    @classmethod\n"
    "    def from_json(cls, json_str: str) -> Self:\n"
    "        return cls.from_dict(json.loads(json_str))\n\n"
  )
  original_from_dict = (
    "    @classmethod\n"
    "    def from_dict(cls, obj: Union[str, Dict[str, Any]]) -> Self:\n"
    "        return cls.from_json(json.dumps(obj))\n\n"
  )
  if delegate not in contents:
    if original_from_dict not in contents:
      raise RuntimeError(f"Generated update-run response from_dict anchor changed: {path}")
    contents = contents.replace(original_from_dict, f"{delegate}{from_dict}", 1)
  elif from_dict not in contents:
    raise RuntimeError(f"Generated update-run response compatibility shape changed: {path}")

  if from_json not in contents:
    pattern = re.compile(
      r"    @classmethod\n    def from_json\(cls, json_str: str\) -> Self:\n.*?(?=    def to_json)",
      re.DOTALL,
    )
    contents, substitutions = pattern.subn(from_json, contents, count=1)
    if substitutions != 1:
      raise RuntimeError(f"Generated update-run response from_json anchor changed: {path}")

  path.write_text(contents)


def main() -> None:
  patch_api_client()
  patched_lists = patch_response_model_lists()
  patched_imports = patch_missing_list_import()
  patched_update_fields = patch_update_request_omission_fields()
  patch_update_run_response_compatibility()
  api_count = patch_client_api_surface()
  compatibility_aliases = patch_compatibility_model_aliases()
  contents = INIT.read_text()
  for name, import_line in EXPORTS:
    contents = add_top_level_export(contents, name, import_line)

  INIT.write_text(contents)
  print(f"  Ensured ApiClient and {patched_lists} collection-response list deserializers are patched.")
  print(f"  Added the missing List import to {patched_imports} generated models.")
  print(f"  Preserved omission for {patched_update_fields} generated update-request fields.")
  print(f"  Synchronized {api_count} CovalClient API properties.")
  print(f"  Added {compatibility_aliases} generated-model compatibility alias modules.")
  print("  Exported CovalClient, InvalidListItemWarning, and paginate from coval_sdk.")


if __name__ == "__main__":
  main()
