'use strict';

const crypto = require('crypto');

const DEFAULT_MAX_ENTRIES = 10_000;
const DEFAULT_TTL_MS = 24 * 60 * 60 * 1_000;

function digest(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex');
}

/**
 * A bounded, TTL-based deduper. Only SHA-256 digests are retained, so URLs
 * and message bodies are never kept in the long-lived in-process index.
 */
function createBoundedDeduper({
  maxEntries = DEFAULT_MAX_ENTRIES,
  ttlMs = DEFAULT_TTL_MS,
  now = () => Date.now(),
} = {}) {
  const capacity = Math.max(1, Math.floor(Number(maxEntries) || DEFAULT_MAX_ENTRIES));
  const ttl = Math.max(1, Math.floor(Number(ttlMs) || DEFAULT_TTL_MS));
  const entries = new Map();

  function prune(currentTime) {
    for (const [key, seenAt] of entries) {
      if (currentTime - seenAt >= ttl) entries.delete(key);
    }
    while (entries.size > capacity) {
      const oldest = entries.keys().next().value;
      if (oldest === undefined) break;
      entries.delete(oldest);
    }
  }

  return {
    has(value) {
      const currentTime = Number(now()) || 0;
      prune(currentTime);
      const key = digest(value);
      const seenAt = entries.get(key);
      if (seenAt === undefined) return false;
      // Refresh recency while preserving a bounded TTL window.
      entries.delete(key);
      entries.set(key, seenAt);
      return true;
    },
    add(value) {
      const currentTime = Number(now()) || 0;
      prune(currentTime);
      const key = digest(value);
      entries.delete(key);
      entries.set(key, currentTime);
      prune(currentTime);
    },
    size() {
      prune(Number(now()) || 0);
      return entries.size;
    },
    // Exposed only for tests/diagnostics; values are digests, never inputs.
    keys() {
      prune(Number(now()) || 0);
      return Array.from(entries.keys());
    },
  };
}

module.exports = {
  DEFAULT_MAX_ENTRIES,
  DEFAULT_TTL_MS,
  createBoundedDeduper,
};
