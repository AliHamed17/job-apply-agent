'use strict';

const DEFAULT_ARCHIVE_RESCAN_INTERVAL_MS = 600_000;
const MIN_ARCHIVE_RESCAN_INTERVAL_MS = 60_000;
const MAX_ARCHIVE_RESCAN_INTERVAL_MS = 3_600_000;

function archiveRescanIntervalDefault({ attemptsRaw, intervalRaw } = {}) {
  if (
    intervalRaw === undefined
    && attemptsRaw !== undefined
    && /^0$/.test(String(attemptsRaw).trim())
  ) {
    // Preserve the pre-periodic behavior when an existing deployment
    // explicitly configured ARCHIVE_RESCAN_ATTEMPTS=0.
    return 0;
  }
  return DEFAULT_ARCHIVE_RESCAN_INTERVAL_MS;
}

/**
 * Parse a periodic archive interval without ever allowing a tight loop.
 * Explicit zero disables periodic scans. Invalid or sub-minimum values are
 * rejected as disabled so an unsafe deployment fails closed.
 */
function parseArchiveRescanInterval(
  rawValue,
  {
    defaultValue = DEFAULT_ARCHIVE_RESCAN_INTERVAL_MS,
    minValue = MIN_ARCHIVE_RESCAN_INTERVAL_MS,
    maxValue = MAX_ARCHIVE_RESCAN_INTERVAL_MS,
  } = {},
) {
  if (rawValue === undefined || rawValue === null) {
    return defaultValue;
  }
  const text = String(rawValue).trim();
  if (text === '0') return 0;
  if (!/^\d+$/.test(text)) return 0;
  const parsed = Number(text);
  if (!Number.isSafeInteger(parsed) || parsed < minValue) return 0;
  return Math.min(parsed, maxValue);
}

module.exports = {
  DEFAULT_ARCHIVE_RESCAN_INTERVAL_MS,
  MIN_ARCHIVE_RESCAN_INTERVAL_MS,
  MAX_ARCHIVE_RESCAN_INTERVAL_MS,
  archiveRescanIntervalDefault,
  parseArchiveRescanInterval,
};
