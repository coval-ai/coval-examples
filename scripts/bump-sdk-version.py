#!/usr/bin/env python3
"""Bump the patch version of the Python and/or TypeScript SDK.

Used by the weekly regen workflow so a regenerated SDK arrives already
releasable. Only touches the hand-maintained manifests and the one pinned
assertion in the Python tests -- the version strings inside the generated tree
are derived from pyproject.toml by generate-sdks.sh, so codegen must be re-run
after this script for them to match.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "python-sdk" / "pyproject.toml"
PY_TESTS = REPO_ROOT / "python-sdk" / "tests" / "test_client.py"
TS_PACKAGE = REPO_ROOT / "typescript-sdk" / "package.json"
TS_LOCKFILE = REPO_ROOT / "typescript-sdk" / "package-lock.json"

VERSION_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


def next_patch(version: str) -> str:
  match = VERSION_RE.match(version)
  if match is None:
    raise SystemExit(f"Cannot bump non-semver version: {version!r}")
  return f"{match['major']}.{match['minor']}.{int(match['patch']) + 1}"


def bump_python() -> str:
  contents = PYPROJECT.read_text()
  current = re.search(r'^version = "([^"]+)"', contents, re.MULTILINE)
  if current is None:
    raise SystemExit("No version field in python-sdk/pyproject.toml")
  new = next_patch(current.group(1))

  PYPROJECT.write_text(
    contents.replace(f'version = "{current.group(1)}"', f'version = "{new}"', 1)
  )

  # The test pins __version__, so it has to move in lockstep or CI fails.
  tests = PY_TESTS.read_text()
  pinned = f'coval_sdk.__version__ == "{current.group(1)}"'
  if pinned not in tests:
    raise SystemExit(f"Expected {pinned!r} in {PY_TESTS.name}; version pin moved?")
  PY_TESTS.write_text(tests.replace(pinned, f'coval_sdk.__version__ == "{new}"', 1))
  return new


def bump_typescript() -> str:
  package = json.loads(TS_PACKAGE.read_text())
  new = next_patch(package["version"])
  package["version"] = new
  TS_PACKAGE.write_text(json.dumps(package, indent=2) + "\n")

  if TS_LOCKFILE.exists():
    lock = json.loads(TS_LOCKFILE.read_text())
    lock["version"] = new
    if "" in lock.get("packages", {}):
      lock["packages"][""]["version"] = new
    TS_LOCKFILE.write_text(json.dumps(lock, indent=2) + "\n")
  return new


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--python", action="store_true", help="bump the Python SDK")
  parser.add_argument("--typescript", action="store_true", help="bump the TypeScript SDK")
  args = parser.parse_args()

  if not (args.python or args.typescript):
    parser.error("nothing to do: pass --python and/or --typescript")

  if args.python:
    print(f"python-sdk -> {bump_python()}")
  if args.typescript:
    print(f"typescript-sdk -> {bump_typescript()}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
