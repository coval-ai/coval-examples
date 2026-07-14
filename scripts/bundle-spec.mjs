#!/usr/bin/env node
// Bundle Coval's per-resource OpenAPI v1 specs into a single combined spec.
//
// Source: docs/api-reference/v1/*-v1.yaml in the public docs repo
//         (override via COVAL_SPECS_DIR).
// Output: dist/coval-openapi.yaml in the SDK repo.
//
// Workflow:
//   1. Read each *-v1.yaml from the specs dir.
//   2. Scan for duplicate operationIds across specs (OpenAPI requires uniqueness).
//      When duplicates appear, rename the conflicting operations by prefixing
//      with the spec's slug (e.g. listMetrics in simulations-v1 becomes
//      simulations_listMetrics). The first occurrence wins; later occurrences
//      get renamed.
//   3. Write the (possibly rewritten) specs to a temp dir.
//   4. Invoke `redocly join` on the temp specs to merge paths, components,
//      tags into a single bundle. Components get prefixed by the source title
//      to avoid schema-name collisions.
//
// Each source spec is self-contained (no cross-file $refs), so step 4 is
// primarily a paths + components consolidation.

import { execSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { basename, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { parse, stringify } from 'yaml';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const repoRoot = resolve(__dirname, '..');
const SOURCE_DIR =
  process.env.COVAL_SPECS_DIR || resolve(repoRoot, '../coval/docs/api-reference/v1');
const OUT_DIR = resolve(repoRoot, 'dist');
const OUTPUT = join(OUT_DIR, 'coval-openapi.yaml');
const CANONICAL_SERVER = 'https://api.coval.dev/v1';
const ALLOWED_SOURCE_SERVERS = new Set([
  'https://api.coval.dev',
  CANONICAL_SERVER,
]);

if (!existsSync(SOURCE_DIR) || !statSync(SOURCE_DIR).isDirectory()) {
  console.error(`✗ Specs directory not found: ${SOURCE_DIR}`);
  console.error('  Set COVAL_SPECS_DIR or check the docs repo is on disk.');
  process.exit(1);
}

const sourceFiles = readdirSync(SOURCE_DIR)
  .filter((f) => f.endsWith('-v1.yaml'))
  .sort();

if (sourceFiles.length === 0) {
  console.error(`✗ No *-v1.yaml files found in ${SOURCE_DIR}`);
  process.exit(1);
}

console.log(`Found ${sourceFiles.length} specs in ${SOURCE_DIR}`);

const HTTP_METHODS = new Set([
  'get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace',
]);

const slugFromFile = (filename) => basename(filename, '.yaml').replace(/-v1$/, '');

const seenOperationIds = new Map();
const renamed = [];
const normalizedPaths = [];

const tmpRoot = join(tmpdir(), `coval-sdk-bundle-${process.pid}`);
rmSync(tmpRoot, { recursive: true, force: true });
mkdirSync(tmpRoot, { recursive: true });

for (const filename of sourceFiles) {
  const filepath = join(SOURCE_DIR, filename);
  const doc = parse(readFileSync(filepath, 'utf8'));
  const slug = slugFromFile(filename);

  const sourceServers = (doc.servers ?? []).map((server) => server?.url).filter(Boolean);
  const unexpectedServers = sourceServers.filter((url) => !ALLOWED_SOURCE_SERVERS.has(url));
  if (unexpectedServers.length > 0) {
    console.error(`Unexpected server URL in ${filename}: ${unexpectedServers.join(', ')}`);
    process.exit(1);
  }

  const paths = {};
  for (const [pathKey, pathItem] of Object.entries(doc.paths ?? {})) {
    const normalized = pathKey === '/v1'
      ? '/'
      : pathKey.startsWith('/v1/')
        ? pathKey.slice(3)
        : pathKey;
    if (Object.hasOwn(paths, normalized)) {
      console.error(`Path collision in ${filename}: ${pathKey} normalizes to ${normalized}`);
      process.exit(1);
    }
    paths[normalized] = pathItem;
    if (normalized !== pathKey) {
      normalizedPaths.push({ filename, from: pathKey, to: normalized });
    }
  }
  doc.paths = paths;
  doc.servers = [{ url: CANONICAL_SERVER }];

  for (const [pathKey, pathItem] of Object.entries(doc.paths ?? {})) {
    if (!pathItem || typeof pathItem !== 'object') continue;
    for (const [method, operation] of Object.entries(pathItem)) {
      if (!HTTP_METHODS.has(method)) continue;
      const opId = operation?.operationId;
      if (!opId) continue;

      const previous = seenOperationIds.get(opId);
      if (previous === undefined) {
        seenOperationIds.set(opId, filename);
        continue;
      }
      if (previous === filename) continue;

      const newId = `${slug}_${opId}`;
      operation.operationId = newId;
      seenOperationIds.set(newId, filename);
      renamed.push({ filename, from: opId, to: newId, pathKey, method });
    }
  }

  writeFileSync(join(tmpRoot, filename), stringify(doc));
}

if (renamed.length > 0) {
  console.log('\nResolved operationId conflicts:');
  for (const r of renamed) {
    console.log(`  ${r.filename}: ${r.method.toUpperCase()} ${r.pathKey} → ${r.from} → ${r.to}`);
  }
}

if (normalizedPaths.length > 0) {
  console.log('\nNormalized version-prefixed paths for the canonical /v1 server:');
  for (const path of normalizedPaths) {
    console.log(`  ${path.filename}: ${path.from} -> ${path.to}`);
  }
}

mkdirSync(OUT_DIR, { recursive: true });

const specPaths = sourceFiles.map((f) => join(tmpRoot, f));
const quoted = (s) => `"${s.replace(/"/g, '\\"')}"`;
const redocly = join(repoRoot, 'scripts', 'node_modules', '.bin', 'redocly');
if (!existsSync(redocly)) {
  console.error('Redocly CLI is not installed. Run `npm ci --prefix scripts`.');
  process.exit(1);
}
const cmd = [
  quoted(redocly),
  'join',
  ...specPaths.map(quoted),
  '-o',
  quoted(OUTPUT),
  '--prefix-components-with-info-prop=title',
].join(' ');

try {
  execSync(cmd, { stdio: 'inherit', cwd: repoRoot });
} catch (err) {
  console.error(`\n✗ redocly join failed (exit ${err.status})`);
  rmSync(tmpRoot, { recursive: true, force: true });
  process.exit(err.status ?? 1);
}

rmSync(tmpRoot, { recursive: true, force: true });
console.log(`\n✓ Bundled spec → ${OUTPUT}`);
