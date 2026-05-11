#!/usr/bin/env node
// Post-process the typescript-fetch generator output to fix a known bug:
// inline enum properties inside `oneOf` models reference a type
// `<ModelName><PropName>Enum` that the generator declares neither in the
// owning file nor anywhere else, causing TS2304 "Cannot find name" errors.
//
// We walk the bundled spec, find any inline string enums on object properties
// whose owning schema participates in a `oneOf`/`anyOf`, and emit the missing
// enum declarations into the corresponding generated model file. The patch is
// idempotent (skips files that already declare the enum).

import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'yaml';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const repoRoot = resolve(__dirname, '..');
const SPEC = resolve(repoRoot, 'dist/coval-openapi.yaml');
const GENERATED_ROOT = resolve(repoRoot, 'typescript-sdk/src/generated');
const MODELS_DIR = resolve(GENERATED_ROOT, 'models');

if (!existsSync(SPEC) || !existsSync(MODELS_DIR)) {
  console.error('✗ Run bundle + codegen before patch-generated-ts.');
  process.exit(1);
}

// --- ESM extension patch ----------------------------------------------------
// openapi-generator emits extensionless relative imports (e.g., `from './runtime'`).
// Node's strict ESM resolver requires .js extensions at runtime, so we append
// them after generation. Compile-time TS doesn't care (we use moduleResolution
// Bundler), but the published package is consumed at runtime by Node.
const IMPORT_RX = /(from\s+['"])(\.[^'"]+?)(['"])/g;
const HAS_EXTENSION_RX = /\.(js|mjs|cjs|json|ts)$/;

function* walkTsFiles(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walkTsFiles(full);
    else if (full.endsWith('.ts')) yield full;
  }
}

let extPatched = 0;
for (const file of walkTsFiles(GENERATED_ROOT)) {
  const src = readFileSync(file, 'utf8');
  const patched = src.replace(IMPORT_RX, (match, lead, spec, trail) => {
    if (HAS_EXTENSION_RX.test(spec)) return match;
    return `${lead}${spec}.js${trail}`;
  });
  if (patched !== src) {
    writeFileSync(file, patched);
    extPatched += 1;
  }
}
if (extPatched > 0) {
  console.log(`  Added .js extensions to ${extPatched} generated file${extPatched === 1 ? '' : 's'}.`);
}
// --- end ESM extension patch ------------------------------------------------

const doc = parse(readFileSync(SPEC, 'utf8'));
const schemas = doc?.components?.schemas ?? {};

// Convert a property name like `comparison_operator` to PascalCase
// (`ComparisonOperator`). openapi-generator uses this for enum type names.
const propToPascal = (s) =>
  s.split(/[_-]/).map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join('');

// Convert a bundled schema name like `Coval_Metrics_API_TargetCondition` to
// the openapi-generator file/identifier (`CovalMetricsAPITargetCondition`).
// Each segment is already PascalCase or upper-case, so we just strip underscores.
const schemaToFileName = (s) => s.replace(/_/g, '');

const enumValueIdent = (v) =>
  v
    .toString()
    .replace(/[^a-zA-Z0-9]/g, '_')
    .replace(/^([0-9])/, '_$1');
const enumMemberName = (v) => propToPascal(enumValueIdent(v));

let patches = 0;

for (const [schemaName, schema] of Object.entries(schemas)) {
  if (!schema || typeof schema !== 'object') continue;
  if (!Array.isArray(schema.oneOf) && !Array.isArray(schema.anyOf)) continue;
  const props = schema.properties ?? {};
  for (const [propName, prop] of Object.entries(props)) {
    if (!prop || typeof prop !== 'object') continue;
    const isEnum = Array.isArray(prop.enum) && prop.type === 'string';
    if (!isEnum) continue;

    const modelIdent = schemaToFileName(schemaName);
    const enumTypeName = `${modelIdent}${propToPascal(propName)}Enum`;
    const modelFile = join(MODELS_DIR, `${modelIdent}.ts`);
    if (!existsSync(modelFile)) continue;
    let contents = readFileSync(modelFile, 'utf8');
    if (contents.includes(`export const ${enumTypeName}`) || contents.includes(`export enum ${enumTypeName}`)) {
      continue;
    }

    const block =
      `\n\n/** @export */\nexport const ${enumTypeName} = {\n` +
      prop.enum
        .map((v) => `    ${enumMemberName(v)}: ${JSON.stringify(v)} as const,`)
        .join('\n') +
      `\n} as const;\nexport type ${enumTypeName} = typeof ${enumTypeName}[keyof typeof ${enumTypeName}];\n`;

    contents += block;
    writeFileSync(modelFile, contents);
    patches += 1;
    console.log(`  + ${schemaName}.${propName} → ${enumTypeName}`);
  }
}

console.log(`\n✓ Applied ${patches} enum patch${patches === 1 ? '' : 'es'}.`);
