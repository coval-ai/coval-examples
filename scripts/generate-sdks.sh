#!/usr/bin/env bash
# Run openapi-generator-cli against the bundled Coval spec to produce
# TypeScript + Python clients.
#
# Output layout (after this script):
#   typescript-sdk/src/generated/{apis,models,runtime.ts,index.ts}
#   python-sdk/src/coval_sdk/{api,models,...}
#
# We strip the generator's own package.json / tsconfig.json — the outer
# typescript-sdk owns those. Generated output is then a pure source-only tree
# that the outer package compiles together with the hand-written wrapper.
#
# Prerequisites: Node 22+, Java 11+ (openapi-generator-cli requires a JRE).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC="$REPO_ROOT/dist/coval-openapi.yaml"

if [[ ! -f "$SPEC" ]]; then
  echo "✗ Bundled spec not found: $SPEC"
  echo "  Run 'node scripts/bundle-spec.mjs' first."
  exit 1
fi

# Brew-installed openjdk is keg-only — put it on PATH for this script.
if command -v brew >/dev/null 2>&1; then
  BREW_JDK="$(brew --prefix openjdk 2>/dev/null || true)"
  if [[ -n "$BREW_JDK" && -d "$BREW_JDK/bin" ]]; then
    export PATH="$BREW_JDK/bin:$PATH"
    export JAVA_HOME="$BREW_JDK"
  fi
fi

if ! command -v java >/dev/null 2>&1; then
  echo "✗ Java not found on PATH. openapi-generator-cli requires a JRE."
  echo "  On macOS: brew install openjdk"
  exit 1
fi

echo "Using $(java -version 2>&1 | head -1)"

TS_OUT="$REPO_ROOT/typescript-sdk/src/generated"
PY_OUT="$REPO_ROOT/python-sdk/src"
TMP_TS="$(mktemp -d -t coval-sdk-ts.XXXXXX)"
TMP_PY="$(mktemp -d -t coval-sdk-py.XXXXXX)"
PY_HANDWRITTEN="$(mktemp -d -t coval-sdk-py-handwritten.XXXXXX)"
trap 'rm -rf "$TMP_TS" "$TMP_PY" "$PY_HANDWRITTEN"' EXIT

echo
echo "→ TypeScript (typescript-fetch) into $TS_OUT"
npx --yes @openapitools/openapi-generator-cli@2.13.4 generate \
  -i "$SPEC" \
  -g typescript-fetch \
  -o "$TMP_TS" \
  -c "$REPO_ROOT/.openapi-generator-config/typescript-fetch.yaml" \
  --skip-validate-spec \
  >/tmp/coval-sdk-ts-gen.log 2>&1 || {
    echo "✗ TypeScript codegen failed. Tail of log:"
    tail -30 /tmp/coval-sdk-ts-gen.log
    exit 1
  }

rm -rf "$TS_OUT"
mkdir -p "$TS_OUT"
cp -r "$TMP_TS/src/." "$TS_OUT/"
# Bring across openapi-generator's marker file so downstream tooling can detect
# generated code (e.g., for codeowners or 'do-not-edit' linters).
cp "$TMP_TS/.openapi-generator/VERSION" "$TS_OUT/.openapi-generator-version" 2>/dev/null || true

# Post-process the TS output to inject inline-enum declarations that
# openapi-generator forgets to emit for properties in oneOf/anyOf models.
echo "  Patching generated TS for known enum gaps..."
node "$REPO_ROOT/scripts/patch-generated-ts.mjs"

echo "✓ $TS_OUT ($(find "$TS_OUT" -type f | wc -l | xargs) files)"

echo
echo "→ Python into $PY_OUT"
PY_PACKAGE_VERSION="$(awk -F '"' '/^version = "/ { print $2; exit }' "$REPO_ROOT/python-sdk/pyproject.toml")"
if [[ -z "$PY_PACKAGE_VERSION" ]]; then
  echo "✗ Could not read Python package version from python-sdk/pyproject.toml."
  exit 1
fi
npx --yes @openapitools/openapi-generator-cli@2.13.4 generate \
  -i "$SPEC" \
  -g python \
  -o "$TMP_PY" \
  -c "$REPO_ROOT/.openapi-generator-config/python.yaml" \
  --additional-properties="packageVersion=$PY_PACKAGE_VERSION" \
  --skip-validate-spec \
  >/tmp/coval-sdk-py-gen.log 2>&1 || {
    echo "✗ Python codegen failed. Tail of log:"
    tail -30 /tmp/coval-sdk-py-gen.log
    exit 1
  }

for handwritten in client.py deserialization.py pagination.py; do
  if [[ -f "$PY_OUT/coval_sdk/$handwritten" ]]; then
    cp "$PY_OUT/coval_sdk/$handwritten" "$PY_HANDWRITTEN/$handwritten"
  fi
done
rm -rf "$PY_OUT/coval_sdk"
mkdir -p "$PY_OUT"
cp -r "$TMP_PY/coval_sdk" "$PY_OUT/"
for handwritten in "$PY_HANDWRITTEN"/*.py; do
  if [[ -f "$handwritten" ]]; then
    cp "$handwritten" "$PY_OUT/coval_sdk/"
  fi
done
cp "$TMP_PY/README.md" "$REPO_ROOT/python-sdk/GENERATED_README.md" 2>/dev/null || true
python3 "$REPO_ROOT/scripts/patch-generated-python.py"
echo "✓ $PY_OUT ($(find "$PY_OUT/coval_sdk" -type f | wc -l | xargs) files)"

echo
echo "Done. Hand-written Python modules were preserved and generated exports were synchronized."
