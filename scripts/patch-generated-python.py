#!/usr/bin/env python3
"""Expose hand-written helpers from the regenerated Python package."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INIT = REPO_ROOT / "python-sdk" / "src" / "coval_sdk" / "__init__.py"

EXPORTS = (
  ("CovalClient", "from coval_sdk.client import CovalClient"),
  ("paginate", "from coval_sdk.pagination import paginate"),
)


def main() -> None:
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
  print("  Exported CovalClient and paginate from coval_sdk.")


if __name__ == "__main__":
  main()
