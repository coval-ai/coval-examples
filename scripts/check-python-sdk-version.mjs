#!/usr/bin/env node
// Verify the Python SDK's two version sources are in lock-step.
//
// The Python SDK has two places where the version string lives:
//   1. python-sdk/pyproject.toml          → drives PyPI metadata
//   2. .openapi-generator-config/python.yaml -> packageVersion
//                                          → drives the generated
//                                            coval_sdk.__version__ constant
//
// Historically these drifted: pyproject.toml went to 0.2.0 but the generator
// config stayed at 0.1.0, so the wheel published to PyPI advertised the right
// version in metadata while baking the stale 0.1.0 into __version__.
//
// This check runs in the regen workflow (see .github/workflows/regenerate-sdks.yml)
// and exits non-zero with a clear message if they ever drift again.

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse as parseYaml } from 'yaml';

const repoRoot = resolve(fileURLToPath(new URL('.', import.meta.url)), '..');

const yamlPath = resolve(repoRoot, '.openapi-generator-config/python.yaml');
const tomlPath = resolve(repoRoot, 'python-sdk/pyproject.toml');

const yamlVer = parseYaml(readFileSync(yamlPath, 'utf8')).packageVersion;

// Tiny TOML scrape — avoids pulling in a TOML parser just for one line. Matches
// the first `version = "..."` under [project], which is the only `version` key
// in this file.
const tomlText = readFileSync(tomlPath, 'utf8');
const tomlMatch = tomlText.match(/^version\s*=\s*"([^"]+)"/m);
const tomlVer = tomlMatch ? tomlMatch[1] : null;

if (!yamlVer) {
  console.error(`✗ Could not read packageVersion from ${yamlPath}`);
  process.exit(1);
}
if (!tomlVer) {
  console.error(`✗ Could not read [project].version from ${tomlPath}`);
  process.exit(1);
}

if (yamlVer !== tomlVer) {
  console.error(`✗ Python SDK version drift:
  .openapi-generator-config/python.yaml packageVersion: ${yamlVer}
  python-sdk/pyproject.toml version: ${tomlVer}

These must match. Update both, then re-run the codegen.`);
  process.exit(1);
}

console.log(`✓ Python SDK versions aligned: ${yamlVer}`);
