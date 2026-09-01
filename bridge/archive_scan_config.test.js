const test = require('node:test');
const assert = require('node:assert/strict');

const {
  DEFAULT_ARCHIVE_RESCAN_INTERVAL_MS,
  MIN_ARCHIVE_RESCAN_INTERVAL_MS,
  archiveRescanIntervalDefault,
  parseArchiveRescanInterval,
} = require('./archive_scan_config');

test('uses the safe default when the interval is omitted', () => {
  assert.equal(parseArchiveRescanInterval(undefined), DEFAULT_ARCHIVE_RESCAN_INTERVAL_MS);
});

test('preserves explicit startup-only configuration from older deployments', () => {
  assert.equal(archiveRescanIntervalDefault({ attemptsRaw: '0' }), 0);
  assert.equal(
    archiveRescanIntervalDefault({ attemptsRaw: '0', intervalRaw: '60000' }),
    DEFAULT_ARCHIVE_RESCAN_INTERVAL_MS,
  );
});

test('allows explicit zero to disable periodic scans', () => {
  assert.equal(parseArchiveRescanInterval('0'), 0);
});

test('rejects malformed and too-short intervals instead of creating a tight loop', () => {
  assert.equal(parseArchiveRescanInterval(''), 0);
  assert.equal(parseArchiveRescanInterval('1junk'), 0);
  assert.equal(parseArchiveRescanInterval('1'), 0);
  assert.equal(parseArchiveRescanInterval(String(MIN_ARCHIVE_RESCAN_INTERVAL_MS - 1)), 0);
  assert.equal(
    parseArchiveRescanInterval(String(MIN_ARCHIVE_RESCAN_INTERVAL_MS)),
    MIN_ARCHIVE_RESCAN_INTERVAL_MS,
  );
});

test('caps very large intervals', () => {
  assert.equal(parseArchiveRescanInterval(String(Number.MAX_SAFE_INTEGER)), 3_600_000);
});
