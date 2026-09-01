const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');

const { ARCHIVE_SCAN_MODE, archiveScanAllowsPagination } = require('./archive_scan_modes');

test('only the explicit startup pass may request historical pagination', () => {
  assert.equal(archiveScanAllowsPagination(ARCHIVE_SCAN_MODE.STARTUP), true);
  assert.equal(archiveScanAllowsPagination(ARCHIVE_SCAN_MODE.PERIODIC_CACHE), false);
  assert.equal(archiveScanAllowsPagination('unknown'), false);
});

test('the bridge wires periodic passes to cache-only mode', () => {
  const source = fs.readFileSync(path.join(__dirname, 'whatsapp_bridge.js'), 'utf8');
  assert.match(
    source,
    /scanArchiveGroup\(group,\s*\{\s*mode: ARCHIVE_SCAN_MODE\.PERIODIC_CACHE/,
  );
  assert.match(source, /if \(shouldPaginate\) \{/);
  assert.match(source, /max: limit, allowPagination/);
  assert.match(source, /ConversationMsgs\.loadEarlierMsgs\(chat, chat\.msgs\)/);
  assert.match(source, /function invalidateArchiveRescans\(\)/);
  assert.match(source, /if \(generation !== _archiveSchedulerGeneration\) return/);
});
