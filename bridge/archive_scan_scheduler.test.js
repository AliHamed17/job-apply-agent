const test = require('node:test');
const assert = require('node:assert/strict');

const { createArchiveScanScheduler } = require('./archive_scan_scheduler');

function fakeTimers() {
  const queue = [];
  return {
    setTimeout(callback, delay) {
      const handle = { callback, delay, cancelled: false };
      queue.push(handle);
      return handle;
    },
    clearTimeout(handle) {
      if (handle) handle.cancelled = true;
    },
    next() {
      const handle = queue.find(item => !item.cancelled);
      assert.ok(handle, 'expected a scheduled callback');
      handle.cancelled = true;
      return handle.callback();
    },
    pending() {
      return queue.filter(item => !item.cancelled);
    },
  };
}

test('runs bounded archive passes and then polls at the configured interval', async () => {
  const timers = fakeTimers();
  const scanned = [];
  const scheduler = createArchiveScanScheduler({
    enabled: true,
    initialDelayMs: 5,
    initialAttempts: 1,
    intervalMs: 60_000,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    listGroups: async () => [
      { id: 'group-a', isGroup: true },
      { id: 'group-b', isGroup: true },
    ],
    shouldWatchGroup: group => group.isGroup,
    scanGroup: async group => {
      scanned.push(group.id);
    },
  });

  scheduler.start();
  assert.deepEqual(timers.pending().map(item => item.delay), [5]);
  await timers.next();
  assert.deepEqual(scanned, ['group-a', 'group-b']);
  assert.deepEqual(timers.pending().map(item => item.delay), [60_000]);

  await timers.next();
  assert.deepEqual(scanned, ['group-a', 'group-b', 'group-a', 'group-b']);
  assert.deepEqual(timers.pending().map(item => item.delay), [60_000]);

  scheduler.stop();
  assert.equal(timers.pending().length, 0);
});

test('does not create timers or scan when archive polling is disabled', async () => {
  const timers = fakeTimers();
  let scans = 0;
  const scheduler = createArchiveScanScheduler({
    enabled: false,
    initialDelayMs: 5,
    initialAttempts: 2,
    intervalMs: 60_000,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    listGroups: async () => [{ id: 'group-a', isGroup: true }],
    shouldWatchGroup: () => true,
    scanGroup: async () => { scans += 1; },
  });

  scheduler.start();
  assert.equal(timers.pending().length, 0);
  assert.equal(scans, 0);
});
