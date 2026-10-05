#!/usr/bin/env node
// Bundle Coval's per-resource OpenAPI v1 specs into a single combined spec.
//
// Source: docs/api-reference/v1/*-v1.yaml in the public docs repo
//         (override via COVAL_SPECS_DIR), plus the frozen legacy specs in
//         legacy-specs/ (override via COVAL_LEGACY_SPECS_DIR).
// Output: dist/coval-openapi.yaml in the SDK repo.
//
// Legacy specs describe paths the API still serves but the public catalog no
// longer publishes (see legacy-specs/README.md). They keep the published SDK
// surfaces for those paths. Canonical specs win: a legacy path or tag
// definition that a canonical spec also defines is dropped from the legacy
// spec. Legacy operations keep their tags, so they stay in the same API classes.
//
// Workflow:
//   1. Read each *-v1.yaml from the specs dir, then each legacy spec.
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

import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { basename, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { parse, stringify } from 'yaml';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const repoRoot = resolve(__dirname, '..');
const SOURCE_DIR =
  process.env.COVAL_SPECS_DIR || resolve(repoRoot, '../coval/docs/api-reference/v1');
const LEGACY_DIR = process.env.COVAL_LEGACY_SPECS_DIR || resolve(repoRoot, 'legacy-specs');
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

// A missing legacy dir would silently drop published SDK surfaces, so it is
// an error rather than an empty list.
if (!existsSync(LEGACY_DIR) || !statSync(LEGACY_DIR).isDirectory()) {
  console.error(`✗ Legacy specs directory not found: ${LEGACY_DIR}`);
  process.exit(1);
}

const listSpecs = (dir) => readdirSync(dir).filter((f) => f.endsWith('-v1.yaml')).sort();
const canonicalFiles = listSpecs(SOURCE_DIR);
const legacyFiles = listSpecs(LEGACY_DIR);

if (canonicalFiles.length === 0) {
  console.error(`✗ No *-v1.yaml files found in ${SOURCE_DIR}`);
  process.exit(1);
}

console.log(`Found ${canonicalFiles.length} specs in ${SOURCE_DIR}`);
console.log(`Found ${legacyFiles.length} legacy specs in ${LEGACY_DIR}`);

// Legacy sources keep their docs filename, so the slug used for operationId
// conflict renames (for example simulations_listMetrics) matches the names the
// SDKs published before those specs left the catalog.
const sources = [
  ...canonicalFiles.map((filename) => ({
    key: filename,
    filename,
    filepath: join(SOURCE_DIR, filename),
    legacy: false,
  })),
  ...legacyFiles.map((filename) => ({
    key: `legacy/${filename}`,
    filename,
    filepath: join(LEGACY_DIR, filename),
    legacy: true,
  })),
];

const HTTP_METHODS = new Set([
  'get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace',
]);

const slugFromFile = (filename) => basename(filename, '.yaml').replace(/-v1$/, '');

const seenOperationIds = new Map();
const renamed = [];
const normalizedPaths = [];
const canonicalPaths = new Set();
const canonicalTags = new Set();
const droppedLegacyPaths = [];
const droppedLegacyTags = [];
const tmpFiles = [];

const tmpRoot = join(tmpdir(), `coval-sdk-bundle-${process.pid}`);
rmSync(tmpRoot, { recursive: true, force: true });
mkdirSync(tmpRoot, { recursive: true });

for (const { key, filename, filepath, legacy } of sources) {
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
      console.error(`Path collision in ${key}: ${pathKey} normalizes to ${normalized}`);
      process.exit(1);
    }
    if (legacy && canonicalPaths.has(normalized)) {
      droppedLegacyPaths.push({ key, path: normalized });
      continue;
    }
    if (!legacy) canonicalPaths.add(normalized);
    paths[normalized] = pathItem;
    if (normalized !== pathKey) {
      normalizedPaths.push({ key, from: pathKey, to: normalized });
    }
  }
  doc.paths = paths;
  doc.servers = [{ url: CANONICAL_SERVER }];

  const tags = [];
  for (const tag of doc.tags ?? []) {
    if (legacy && canonicalTags.has(tag.name)) {
      droppedLegacyTags.push({ key, tag: tag.name });
      continue;
    }
    if (!legacy) canonicalTags.add(tag.name);
    tags.push(tag);
  }
  if (doc.tags) doc.tags = tags;

  for (const [pathKey, pathItem] of Object.entries(doc.paths ?? {})) {
    if (!pathItem || typeof pathItem !== 'object') continue;
    for (const [method, operation] of Object.entries(pathItem)) {
      if (!HTTP_METHODS.has(method)) continue;
      const opId = operation?.operationId;
      if (!opId) continue;

      const previous = seenOperationIds.get(opId);
      if (previous === undefined) {
        seenOperationIds.set(opId, key);
        continue;
      }
      if (previous === key) continue;

      const newId = `${slug}_${opId}`;
      operation.operationId = newId;
      seenOperationIds.set(newId, key);
      renamed.push({ key, from: opId, to: newId, pathKey, method });
    }
  }

  const tmpFile = join(tmpRoot, legacy ? `legacy-${filename}` : filename);
  writeFileSync(tmpFile, stringify(doc));
  tmpFiles.push(tmpFile);
}

if (renamed.length > 0) {
  console.log('\nResolved operationId conflicts:');
  for (const r of renamed) {
    console.log(`  ${r.key}: ${r.method.toUpperCase()} ${r.pathKey} → ${r.from} → ${r.to}`);
  }
}

if (droppedLegacyPaths.length > 0) {
  console.log('\nDropped legacy paths that a canonical spec defines:');
  for (const dropped of droppedLegacyPaths) {
    console.log(`  ${dropped.key}: ${dropped.path}`);
  }
}

if (droppedLegacyTags.length > 0) {
  console.log('\nDropped legacy tag definitions that a canonical spec defines:');
  for (const dropped of droppedLegacyTags) {
    console.log(`  ${dropped.key}: ${dropped.tag}`);
  }
}

if (normalizedPaths.length > 0) {
  console.log('\nNormalized version-prefixed paths for the canonical /v1 server:');
  for (const path of normalizedPaths) {
    console.log(`  ${path.key}: ${path.from} -> ${path.to}`);
  }
}

mkdirSync(OUT_DIR, { recursive: true });

const redocly = join(repoRoot, 'scripts', 'node_modules', '.bin', 'redocly');
if (!existsSync(redocly)) {
  console.error('Redocly CLI is not installed. Run `npm ci --prefix scripts`.');
  process.exit(1);
}
// An argument array, not a shell string, so no path needs quoting or escaping.
const args = [
  'join',
  ...tmpFiles,
  '-o',
  OUTPUT,
  '--prefix-components-with-info-prop=title',
  // Legacy specs reuse canonical tag names (Audio, Metric Outputs) so their
  // operations stay in the same API classes. x-tagGroups cannot hold one tag in
  // two groups, and openapi-generator ignores them.
  '--without-x-tag-groups',
];

try {
  execFileSync(redocly, args, { stdio: 'inherit', cwd: repoRoot });
} catch (err) {
  console.error(`\n✗ redocly join failed (exit ${err.status})`);
  rmSync(tmpRoot, { recursive: true, force: true });
  process.exit(err.status ?? 1);
}

rmSync(tmpRoot, { recursive: true, force: true });
console.log(`\n✓ Bundled spec → ${OUTPUT}`);
