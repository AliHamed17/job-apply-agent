const test = require('node:test');
const assert = require('node:assert/strict');

const { createBoundedDeduper } = require('./archive_dedup');

test('deduplicates values without retaining the original value', () => {
  const deduper = createBoundedDeduper({ maxEntries: 2, ttlMs: 60_000, now: () => 100 });
  const privateText = 'a private message with a job URL https://example.test/secret';

  deduper.add(privateText);
  assert.equal(deduper.has(privateText), true);
  assert.equal(deduper.size(), 1);
  assert.equal(JSON.stringify(deduper), '{}');
  assert.equal(deduper.keys().some(key => key.includes('private') || key.includes('example')), false);
});

test('expires old entries and bounds the number of retained hashes', () => {
  let now = 0;
  const deduper = createBoundedDeduper({ maxEntries: 2, ttlMs: 10, now: () => now });

  deduper.add('one');
  deduper.add('two');
  deduper.add('three');
  assert.equal(deduper.size(), 2);
  assert.equal(deduper.has('one'), false);

  now = 11;
  assert.equal(deduper.has('two'), false);
  assert.equal(deduper.size(), 0);
});
