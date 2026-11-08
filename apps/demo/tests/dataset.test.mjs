// All fixture content below is generated and fictional; no user recording is loaded.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import { validateDataset, search, catalog } from '../core.mjs';
const root = new URL('../', import.meta.url);
const data = validateDataset(JSON.parse(await readFile(new URL('data/index.json', root), 'utf8')));
test('share dataset is explicitly synthetic and has only generated geometric media', async () => {
  assert.equal(data.provenance,'synthetic_fixture');
  assert.equal(data.clips.length,1); assert.equal(data.observations.length,4);
  for (const c of data.clips) {
    assert.match(c.video_file,/^data\/synthetic\//);
    assert.ok((await stat(new URL(c.video_file,root))).size>0);
  }
  for (const o of data.observations) {
    assert.equal(o.provenance,'synthetic_fixture');
    assert.match(o.evidence_file,/^data\/synthetic\//);
    assert.ok((await stat(new URL(o.evidence_file,root))).size>0);
  }
  assert.match(catalog(data).provenance_label,/合成/);
});
test('synthetic latest holding invalidates earlier settled placement', () => {
  const answer=search(data,{query:'钥匙在哪'}).answers[0];
  assert.equal(answer.observation.id,'keys-held');
  assert.equal(answer.latest_placement.id,'keys-surface-a');
  assert.match(answer.headline,/手中.*尚未确认/);
  assert.match(answer.caveat,/合成示例/);
});
test('independent object remains at a fictional visible surface', () => {
  const answer=search(data,{query:'wallet'}).answers[0];
  assert.equal(answer.observation.id,'wallet-surface-b');
  assert.match(answer.headline,/测试台 B/);
  assert.match(answer.observation.provenance_label,/合成/);
});
