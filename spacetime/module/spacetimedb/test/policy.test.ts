import test from 'node:test';
import assert from 'node:assert/strict';
import { canonical, cents, hash, journalLines, payloadFrom, sumCents, taskTransition, type Json } from '../src/policy';

test('canonical JSON and SHA-256 are deterministic and match the known vector', () => {
  assert.equal(canonical({ z: ['x', { y: 2, x: 1 }], a: true }), '{"a":true,"z":["x",{"x":1,"y":2}]}');
  assert.equal(hash('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  assert.equal(hash(canonical({ a: 1, b: 2 })), hash(canonical({ b: 2, a: 1 })));
});

test('money uses exact cents, rejects fractional cents, NaN and unsafe amounts', () => {
  for (const value of [0, 0.01, 0.29, 1850, 9400, 123456.78]) assert.equal(cents(value), Math.round(value * 100));
  for (const value of [-1, 0.001, 1.005, Infinity, NaN, Number.MAX_SAFE_INTEGER]) assert.throws(() => cents(value));
  assert.equal(cents(-30.25, true), -3025);
  assert.equal(sumCents([0.1, 0.2, 0.3]), 60);
});

test('journal validation rejects zero, mixed-sided and unbalanced lines', () => {
  assert.deepEqual(journalLines([{ account: 'Cash', debit: 0.3 }, { account: 'AP', credit: 0.1 }, { account: 'AP', credit: 0.2 }]), [
    { account: 'Cash', debit: 0.3, credit: 0 }, { account: 'AP', debit: 0, credit: 0.1 }, { account: 'AP', debit: 0, credit: 0.2 },
  ]);
  const invalid: Json[] = [[], [{ account: 'a', debit: 1 }], [{ account: 'a' }, { account: 'b' }], [{ account: 'a', debit: 1, credit: 1 }, { account: 'b', credit: 1 }], [{ account: 'a', debit: 100 }, { account: 'b', credit: 99.99 }]];
  for (const lines of invalid) assert.throws(() => journalLines(lines));
});

test('payload size and shape boundaries reject malformed inputs', () => {
  for (const raw of ['null', '[]', '42', '"text"', '{', JSON.stringify({ large: 'x'.repeat(262144) })]) assert.throws(() => payloadFrom(raw));
  assert.deepEqual(payloadFrom('{"write_id":"test"}'), { write_id: 'test' });
});

test('terminal tasks cannot reopen or return to submitted', () => {
  assert.doesNotThrow(() => taskTransition('submitted', 'working'));
  assert.doesNotThrow(() => taskTransition('input-required', 'working'));
  assert.throws(() => taskTransition('completed', 'working'));
  assert.throws(() => taskTransition('working', 'submitted'));
});
