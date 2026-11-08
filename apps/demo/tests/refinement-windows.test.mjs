import test from 'node:test';
import assert from 'node:assert/strict';
import { contextDeduplicatedWindows } from '../refinement-windows.mjs';

const frame = n => ({ frame_id: `f${n}`, output_pts_seconds: n });
const ids = windows => windows.map(w => w.map(f => f.frame_id));
test('dedup retains fresh images and the nearest already-seen context on both sides', () => {
  const frames = Array.from({ length: 7 }, (_, i) => frame(i));
  const result = contextDeduplicatedWindows([frames], ['f0', 'f1', 'f3', 'f5', 'f6']);
  assert.deepEqual(ids(result), [['f1', 'f2', 'f4', 'f5']]);
  assert.deepEqual(frames.map(f => f.frame_id), ['f0', 'f1', 'f2', 'f3', 'f4', 'f5', 'f6']);
});
test('skip all-reused windows, preserve every new frame, and reuse earlier windows as context', () => {
  const windows = [[0, 1], [0, 1, 2, 3], [2, 3, 4, 5], [0, 1]] .map(w => w.map(frame));
  const result = contextDeduplicatedWindows(windows, ['f0', 'f1']);
  assert.deepEqual(ids(result), [['f1', 'f2', 'f3'], ['f3', 'f4', 'f5']]);
  const before = new Set(['f0', 'f1', ...result.flat().map(f => f.frame_id)]);
  assert.deepEqual([...before].sort(), ['f0', 'f1', 'f2', 'f3', 'f4', 'f5']);
});
