#!/usr/bin/env node
// Post-process typescript-fetch output for two upstream generator bugs:
// missing inline-enum declarations and empty aliases for inline oneOf fields.
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
const APIS_INDEX = resolve(GENERATED_ROOT, 'apis/index.ts');
const MODELS_DIR = resolve(GENERATED_ROOT, 'models');
const MODELS_INDEX = resolve(MODELS_DIR, 'index.ts');
const CLIENT = resolve(repoRoot, 'typescript-sdk/src/CovalClient.ts');

const COMPATIBILITY_MODEL_ALIASES = [
  [
    'CovalMetricsAPIErrorResponseErrorDetailsInner',
    'CovalAlertsAPIErrorResponseErrorDetailsInner',
  ],
  ['CovalMonitorsAPIErrorResponseError', 'CovalAlertsAPIErrorResponseError'],
  [
    'CovalMonitorsAPIMonitorEventResourceConditionResultsInner',
    'CovalAlertsAPIAlertEventResourceConditionResultsInner',
  ],
  [
    'CovalMonitorsAPIMonitorEventResourceConditionResultsInnerComputedValue',
    'CovalAlertsAPIAlertEventResourceConditionResultsInnerComputedValue',
  ],
  [
    'CovalMonitorsAPIMonitorEventResourceDispatchedChannelsInner',
    'CovalAlertsAPIAlertEventResourceDispatchedChannelsInner',
  ],
];

if (!existsSync(SPEC) || !existsSync(MODELS_DIR)) {
  console.error('✗ Run bundle + codegen before patch-generated-ts.');
  process.exit(1);
}

function replaceMarkedBlock(contents, startMarker, endMarker, lines) {
  if (contents.split(startMarker).length !== 2 || contents.split(endMarker).length !== 2) {
    throw new Error(`Expected exactly one ${startMarker}/${endMarker} block`);
  }

  const markerStart = contents.indexOf(startMarker);
  const bodyStart = contents.indexOf('\n', markerStart) + 1;
  const bodyEnd = contents.indexOf(endMarker, bodyStart);
  return `${contents.slice(0, bodyStart)}${lines.join('\n')}\n${contents.slice(bodyEnd)}`;
}

function apiClassToProperty(className) {
  const base = className.replace(/Api$/, '');
  if (base.startsWith('API')) return `api${base.slice(3)}`;
  return `${base.charAt(0).toLowerCase()}${base.slice(1)}`;
}

function patchClientApiSurface() {
  const apiIndex = readFileSync(APIS_INDEX, 'utf8');
  const classNames = [...apiIndex.matchAll(/export \* from '\.\/(\w+Api)\.js';/g)].map(
    (match) => match[1],
  );
  if (classNames.length === 0) throw new Error('No generated TypeScript API exports found');

  const apis = classNames.map((className) => [apiClassToProperty(className), className]);
  if (new Set(apis.map(([propertyName]) => propertyName)).size !== apis.length) {
    throw new Error('Generated TypeScript APIs produced duplicate client property names');
  }

  let contents = readFileSync(CLIENT, 'utf8');
  contents = replaceMarkedBlock(
    contents,
    '  // sdk-api-imports:start',
    '  // sdk-api-imports:end',
    apis.map(([, className]) => `  ${className},`),
  );
  contents = replaceMarkedBlock(
    contents,
    '  // sdk-api-property-names:start',
    '  // sdk-api-property-names:end',
    apis.map(([propertyName]) => `  '${propertyName}',`),
  );
  contents = replaceMarkedBlock(
    contents,
    '  // sdk-api-properties:start',
    '  // sdk-api-properties:end',
    apis.map(([propertyName, className]) => `  readonly ${propertyName}: ${className};`),
  );
  contents = replaceMarkedBlock(
    contents,
    '    // sdk-api-assignments:start',
    '    // sdk-api-assignments:end',
    apis.map(
      ([propertyName, className]) =>
        `    this.${propertyName} = new ${className}(this.configuration);`,
    ),
  );
  writeFileSync(CLIENT, contents);
  return apis.length;
}

function patchCompatibilityModelAliases() {
  let modelsIndex = readFileSync(MODELS_INDEX, 'utf8');
  let patched = 0;

  for (const [oldName, newName] of COMPATIBILITY_MODEL_ALIASES) {
    const oldFile = join(MODELS_DIR, `${oldName}.ts`);
    const newFile = join(MODELS_DIR, `${newName}.ts`);
    const indexExport = `export * from './${oldName}.js';`;

    if (!existsSync(oldFile)) {
      if (!existsSync(newFile)) {
        throw new Error(`Compatibility target does not exist: ${newFile}`);
      }

      const targetContents = readFileSync(newFile, 'utf8');
      const typeExports = new Set(
        [...targetContents.matchAll(/export (?:interface|type) (\w+)/g)].map(
          (match) => match[1],
        ),
      );
      const valueExports = new Set(
        [...targetContents.matchAll(/export (?:const|function) (\w+)/g)].map(
          (match) => match[1],
        ),
      );
      const rename = (symbol) => symbol.replaceAll(newName, oldName);
      const relevantValues = [...valueExports].filter((symbol) => symbol.includes(newName));
      const relevantTypes = [...typeExports].filter(
        (symbol) => symbol.includes(newName) && !valueExports.has(symbol),
      );
      if (relevantValues.length === 0 && relevantTypes.length === 0) {
        throw new Error(`No compatibility exports found in ${newFile}`);
      }

      const lines = [
        '/* tslint:disable */',
        '/* eslint-disable */',
        `/** Backward-compatible aliases for ${newName}. */`,
      ];
      if (relevantTypes.length > 0) {
        lines.push(
          'export type {',
          ...relevantTypes.map((symbol) => `  ${symbol} as ${rename(symbol)},`),
          `} from './${newName}.js';`,
        );
      }
      if (relevantValues.length > 0) {
        lines.push(
          'export {',
          ...relevantValues.map((symbol) => `  ${symbol} as ${rename(symbol)},`),
          `} from './${newName}.js';`,
        );
      }
      writeFileSync(oldFile, `${lines.join('\n')}\n`);
      patched += 1;
    }

    if (!modelsIndex.includes(indexExport)) {
      modelsIndex = `${modelsIndex.trimEnd()}\n${indexExport}\n`;
    }
  }

  writeFileSync(MODELS_INDEX, modelsIndex);
  return patched;
}

// --- update_run oneOf ordering patch ----------------------------------------
// update_run returns either a full run resource or, for monitored
// conversations, a minimal tag-write confirmation whose fields are a subset of
// the run resource's. The generated guards test the confirmation first, so a
// tagged run would be misclassified and lose its fields; test the run resource
// first instead.
const UPDATE_RUN_ONE_OF_FILE = join(MODELS_DIR, 'UpdateRun200ResponseRun.ts');

function patchUpdateRunOneOfOrdering() {
  if (!existsSync(UPDATE_RUN_ONE_OF_FILE)) return 0;

  const swaps = [
    [
      `    if (instanceOfCovalRunsAPIMonitoringRunTagUpdateResource(json)) {
        return CovalRunsAPIMonitoringRunTagUpdateResourceFromJSONTyped(json, true);
    }
    if (instanceOfCovalRunsAPIRunResource(json)) {
        return CovalRunsAPIRunResourceFromJSONTyped(json, true);
    }`,
      `    if (instanceOfCovalRunsAPIRunResource(json)) {
        return CovalRunsAPIRunResourceFromJSONTyped(json, true);
    }
    if (instanceOfCovalRunsAPIMonitoringRunTagUpdateResource(json)) {
        return CovalRunsAPIMonitoringRunTagUpdateResourceFromJSONTyped(json, true);
    }`,
    ],
    [
      `    if (instanceOfCovalRunsAPIMonitoringRunTagUpdateResource(value)) {
        return CovalRunsAPIMonitoringRunTagUpdateResourceToJSON(value as CovalRunsAPIMonitoringRunTagUpdateResource);
    }
    if (instanceOfCovalRunsAPIRunResource(value)) {
        return CovalRunsAPIRunResourceToJSON(value as CovalRunsAPIRunResource);
    }`,
      `    if (instanceOfCovalRunsAPIRunResource(value)) {
        return CovalRunsAPIRunResourceToJSON(value as CovalRunsAPIRunResource);
    }
    if (instanceOfCovalRunsAPIMonitoringRunTagUpdateResource(value)) {
        return CovalRunsAPIMonitoringRunTagUpdateResourceToJSON(value as CovalRunsAPIMonitoringRunTagUpdateResource);
    }`,
    ],
  ];

  let contents = readFileSync(UPDATE_RUN_ONE_OF_FILE, 'utf8');
  let patched = 0;
  for (const [monitoringFirst, runFirst] of swaps) {
    if (contents.includes(monitoringFirst)) {
      contents = contents.replace(monitoringFirst, runFirst);
      patched += 1;
    } else if (!contents.includes(runFirst)) {
      throw new Error('Generated UpdateRun200ResponseRun oneOf ordering changed');
    }
  }

  if (patched > 0) writeFileSync(UPDATE_RUN_ONE_OF_FILE, contents);
  return patched;
}
// --- end update_run oneOf ordering patch ------------------------------------

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

const apiCount = patchClientApiSurface();

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
let unionPatches = 0;

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

for (const [schemaName, schema] of Object.entries(schemas)) {
  if (!schema || typeof schema !== 'object') continue;
  const props = schema.properties ?? {};
  for (const [propName, prop] of Object.entries(props)) {
    if (!prop || typeof prop !== 'object' || !Array.isArray(prop.oneOf)) continue;
    const members = prop.oneOf
      .map((member) => member?.$ref)
      .filter(Boolean)
      .map((ref) => schemaToFileName(ref.split('/').at(-1)));
    if (members.length === 0) continue;

    const aliasName = `${schemaToFileName(schemaName)}${propToPascal(propName)}`;
    const modelFile = join(MODELS_DIR, `${aliasName}.ts`);
    if (!existsSync(modelFile)) continue;
    let contents = readFileSync(modelFile, 'utf8');
    const emptyAlias = `export type ${aliasName} = ;`;
    if (!contents.includes(emptyAlias)) continue;

    const missingImports = members.filter(
      (member) => !contents.includes(`import type { ${member} }`),
    );
    if (missingImports.length > 0) {
      console.error(
        `Cannot repair ${aliasName}; generated file is missing imports for ${missingImports.join(', ')}`,
      );
      process.exit(1);
    }

    contents = contents.replace(
      emptyAlias,
      `export type ${aliasName} = ${members.join(' | ')};`,
    );
    writeFileSync(modelFile, contents);
    unionPatches += 1;
    console.log(`  + ${schemaName}.${propName} -> ${members.join(' | ')}`);
  }
}

const emptyAliases = [];
for (const file of walkTsFiles(GENERATED_ROOT)) {
  const contents = readFileSync(file, 'utf8');
  if (/export type [A-Za-z0-9_]+\s*=\s*;/.test(contents)) {
    emptyAliases.push(file);
  }
}
if (emptyAliases.length > 0) {
  console.error(`Unrepaired empty generated aliases:\n${emptyAliases.join('\n')}`);
  process.exit(1);
}

const compatibilityAliases = patchCompatibilityModelAliases();
const updateRunOneOfPatches = patchUpdateRunOneOfOrdering();

console.log(
  `\n✓ Applied ${patches} enum patch${patches === 1 ? '' : 'es'} and ` +
  `${unionPatches} union patch${unionPatches === 1 ? '' : 'es'}, ` +
  `synchronized ${apiCount} CovalClient API properties, added ` +
  `${compatibilityAliases} compatibility alias modules, and applied ` +
  `${updateRunOneOfPatches} update-run oneOf ordering patch${updateRunOneOfPatches === 1 ? '' : 'es'}.`,
);
