'use strict';

// Schedules bounded scans of already-hydrated WhatsApp archive messages. The
// scheduler deliberately knows nothing about message contents; callers provide
// the metadata listing and a per-group scan function. This keeps diagnostics
// limited to counts and fixed reason codes.

const DEFAULT_REASON_CODE = 'ARCHIVE_SCAN_FAILED';

function boundedReasonCode(value) {
  if (typeof value !== 'string') return DEFAULT_REASON_CODE;
  const code = value.trim().toUpperCase();
  return /^[A-Z][A-Z0-9_]{0,63}$/.test(code) ? code : DEFAULT_REASON_CODE;
}

function createArchiveScanScheduler({
  enabled = false,
  initialDelayMs = 30_000,
  initialAttempts = 0,
  intervalMs = 0,
  setTimeout: scheduleTimeout = setTimeout,
  clearTimeout: cancelTimeout = clearTimeout,
  listGroups,
  shouldWatchGroup,
  scanGroup,
  onState,
  now = () => new Date().toISOString(),
}) {
  if (typeof listGroups !== 'function') throw new TypeError('listGroups must be a function');
  if (typeof shouldWatchGroup !== 'function') throw new TypeError('shouldWatchGroup must be a function');
  if (typeof scanGroup !== 'function') throw new TypeError('scanGroup must be a function');

  const delay = Math.max(0, Number(initialDelayMs) || 0);
  const attempts = Math.max(0, Math.floor(Number(initialAttempts) || 0));
  const interval = Math.max(0, Number(intervalMs) || 0);
  let active = false;
  let running = false;
  let timer = null;
  let remainingAttempts = attempts;
  let state = {
    enabled: Boolean(enabled),
    active: false,
    running: false,
    phase: 'idle',
    lastStartedAt: null,
    lastFinishedAt: null,
    lastGroupCount: 0,
    lastMessageCount: 0,
    lastErrorCode: null,
    lastPaginationAvailable: null,
  };

  function publish(patch) {
    state = { ...state, ...patch };
    if (typeof onState === 'function') {
      try {
        onState({ ...state });
      } catch {
        // Diagnostics must never stop archive processing.
      }
    }
  }

  function scheduleNext() {
    if (!active || timer) return;
    const nextDelay = remainingAttempts > 0 ? delay : interval;
    if (nextDelay <= 0) {
      publish({ active: false, phase: 'idle' });
      active = false;
      return;
    }
    timer = scheduleTimeout(() => {
      timer = null;
      return runPass();
    }, nextDelay);
  }

  async function runPass() {
    if (!active || running) return;
    running = true;
    if (remainingAttempts > 0) remainingAttempts -= 1;
    publish({
      active: true,
      running: true,
      phase: 'scanning',
      lastStartedAt: now(),
      lastErrorCode: null,
    });

    let groupCount = 0;
    let messageCount = 0;
    let paginationAvailable = true;
    let errorCode = null;
    try {
      const metadata = await listGroups();
      const groups = Array.isArray(metadata)
        ? metadata.filter(group => {
          try {
            return shouldWatchGroup(group);
          } catch {
            return false;
          }
        })
        : [];
      groupCount = groups.length;
      for (const group of groups) {
        if (!active) break;
        try {
          const result = await scanGroup(group);
          if (result && typeof result === 'object') {
            if (Number.isFinite(result.messageCount)) {
              messageCount += Math.max(0, Math.floor(result.messageCount));
            }
            if (result.paginationAvailable === false) paginationAvailable = false;
            if (result.errorCode) errorCode = boundedReasonCode(result.errorCode);
          }
        } catch (error) {
          errorCode = boundedReasonCode(error?.code);
        }
      }
    } catch (error) {
      errorCode = boundedReasonCode(error?.code);
    } finally {
      running = false;
      publish({
        active,
        running: false,
        phase: 'idle',
        lastFinishedAt: now(),
        lastGroupCount: groupCount,
        lastMessageCount: messageCount,
        lastErrorCode: errorCode,
        lastPaginationAvailable: paginationAvailable,
      });
      scheduleNext();
    }
  }

  return {
    start() {
      if (!state.enabled || active) return;
      active = true;
      remainingAttempts = attempts;
      publish({ active: true, phase: 'scheduled' });
      scheduleNext();
    },
    stop() {
      active = false;
      if (timer) {
        cancelTimeout(timer);
        timer = null;
      }
      publish({ active: false, running, phase: running ? 'scanning' : 'idle' });
    },
    runNow() {
      if (!state.enabled) return Promise.resolve();
      if (!active) active = true;
      return runPass();
    },
    getState() {
      return { ...state, active, running };
    },
  };
}

module.exports = { boundedReasonCode, createArchiveScanScheduler };
