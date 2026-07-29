#!/usr/bin/env node
/**
 * Enforce the command palette's bundle budget (R2, PROJECT_STRUCTURE.md §3.2).
 *
 * The palette must open in well under 100 ms. That is a structural constraint,
 * not something to optimise toward later: the moment its entry chunk pulls in
 * the chat renderer, the memory explorer or the monitor charts, the budget is
 * gone and no amount of tuning wins it back.
 *
 * This check is why the constraint holds — reviewer vigilance would not.
 * Run after `vite build`.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const DIST = new URL('../apps/desktop/dist/assets', import.meta.url).pathname;

/** Bytes, uncompressed. The palette's own code, excluding the shared vendor chunk. */
const PALETTE_ENTRY_BUDGET = 12_000;

/**
 * Modules the palette must never reach. Each would drag in a subtree far larger
 * than the palette itself.
 */
const FORBIDDEN_MARKERS = [
  { marker: 'bindTransport', reason: 'transport/state sync' },
  { marker: 'TransportClient', reason: 'websocket client' },
  { marker: 'MainWindow', reason: 'main window shell' },
];

function fail(message) {
  console.error(`✗ ${message}`);
  process.exitCode = 1;
}

let files;
try {
  files = readdirSync(DIST);
} catch {
  fail(`No build output at ${DIST} — run "npm run build" first.`);
  process.exit(1);
}

const paletteChunk = files.find((name) => /^palette-.*\.js$/.test(name));
if (paletteChunk === undefined) {
  fail('No palette entry chunk found in the build output.');
  process.exit(1);
}

const path = join(DIST, paletteChunk);
const size = statSync(path).size;
const source = readFileSync(path, 'utf8');

console.log(`palette entry: ${paletteChunk} — ${size} bytes (budget ${PALETTE_ENTRY_BUDGET})`);

if (size > PALETTE_ENTRY_BUDGET) {
  fail(
    `Palette entry chunk is ${size} bytes, over the ${PALETTE_ENTRY_BUDGET} byte budget. ` +
      'Something heavy was imported into the palette path.',
  );
}

for (const { marker, reason } of FORBIDDEN_MARKERS) {
  if (source.includes(marker)) {
    fail(`Palette bundle contains "${marker}" (${reason}) — it must not import that.`);
  }
}

if (process.exitCode !== 1) {
  console.log('✓ palette budget respected');
}
