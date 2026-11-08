import test from 'node:test';
import assert from 'node:assert/strict';
import { validateDataset, search, catalog, parseRange, InputError } from '../core.mjs';

export function fixture() {
  const clips = ['old', 'new'].map((id, i) => ({ id, label: id, started_at: `2026-10-23T05:0${i}:00Z`,
    duration_seconds: 60, video_file: `${id}.mp4`, poster_file: `${id}.jpg` }));
  const o = (id, clip_id, object_id, time, state, extra = {}) => ({ id, clip_id, object_id,
    label_zh: object_id === 'keys' ? '钥匙' : '钱包', output_pts_seconds: time, state,
    observation_zh: state === 'held' ? '右手拿着钥匙。' : '测试台 A 上的钥匙。', location_zh: state === 'held' ? '右手中' : '测试台 A 上',
    evidence_file: `${id}.jpg`, provenance: 'codex_visual_review', review_status: 'ai_reviewed',
    is_object_visible: true, use_for_last_seen: true, usable_as_clear_evidence: true,
    limitations: ['后续位置未确认。'], ...extra });
  return { schema_version: 1, objects: [
    { id: 'keys', label_zh: '钥匙', aliases: ['钥匙', '鑰匙', 'key', 'keys', 'keyring'] },
    { id: 'wallet', label_zh: '钱包', aliases: ['钱包', '錢包', 'wallet'] },
  ], clips, observations: [
    o('placed-old', 'old', 'keys', 55, 'placed'),
    o('placed-new', 'new', 'keys', 20, 'placed'),
    o('held-new', 'new', 'keys', 30, 'held'),
    o('uncertain-new', 'new', 'keys', 50, 'uncertain', { use_for_last_seen: false }),
    o('empty-new', 'new', 'keys', 55, 'visible', { is_object_visible: false, use_for_last_seen: false }),
    o('wallet-new', 'new', 'wallet', 31, 'held', { observation_zh: '左手拿着钱包。' }),
  ] };
}

test('newer holding evidence overrides an earlier placement, and uncertainty never becomes a last sighting', () => {
  const data = validateDataset(fixture());
  const result = search(data, { query: '我的钥匙最后放在哪里了？' });
  assert.equal(result.status, 'found');
  assert.equal(result.answers[0].observation.id, 'held-new');
  assert.match(result.answers[0].headline, /手中.*尚未确认/);
  assert.equal(result.answers[0].latest_placement.id, 'placed-new');
  assert.match(result.answers[0].caveat, /无法确认/);
});
test('recording chronology wins over within-clip timestamps or index insertion order', () => {
  const data = fixture(); data.observations.reverse();
  assert.equal(search(data, { query: 'keys' }).answers[0].observation.clip_id, 'new');
  assert.equal(search(data, { query: '钥匙', clip_id: 'old' }).answers[0].observation.id, 'placed-old');
});
test('a clear visible location followed by unclear handling is not presented as a settled location', () => {
  const data = fixture();
  data.objects = [{ id: 'earbud_case', label_zh: '白色耳机盒', aliases: ['耳机盒'] }];
  const template = data.observations[0];
  data.observations = [
    { ...template, id: 'clear-case', clip_id: 'new', object_id: 'earbud_case', label_zh: '白色耳机盒',
      output_pts_seconds: 20, state: 'visible', observation_zh: '白色耳机盒在测试台 A 上。' },
    { ...template, id: 'unclear-handling', clip_id: 'new', object_id: 'earbud_case', label_zh: '白色耳机盒',
      output_pts_seconds: 30, state: 'held', usable_as_clear_evidence: false,
      observation_zh: '手中有被遮挡的白色盒状物，细节不清楚。' },
  ];
  const answer = search(validateDataset(data), { query: '耳机盒在哪' }).answers[0];
  assert.equal(answer.observation.id, 'clear-case');
  assert.match(answer.headline, /较早清楚.*后续去向未确认/);
  assert.equal(answer.history[0].id, 'unclear-handling');
  data.observations.pop();
  assert.equal(search(data, { query: '耳机盒在哪' }).answers[0].headline,
    '白色耳机盒的最新清楚画面：测试台 A 上。');
});
test('Chinese, traditional Chinese, and English whole-word aliases work; keyboard does not mean keys', () => {
  for (const query of ['钥匙', '鑰匙在哪', 'Where are my KEYS?', 'my keyring'])
    assert.equal(search(fixture(), { query }).answers[0].object_id, 'keys');
  for (const query of ['keyboard', 'monkeys', '手机在哪里', '<script>alert(1)</script>'])
    assert.equal(search(fixture(), { query }).status, 'no_evidence');
});
test('two requested objects return independent evidence, not an invented joint location', () => {
  const result = search(fixture(), { query: '钥匙和钱包在哪里' });
  assert.equal(result.answers.length, 2);
  assert.deepEqual(result.answers.map(a => a.observation.id), ['held-new', 'wallet-new']);
});
test('empty, negated, and missing evidence requests are not guessed', () => {
  assert.equal(search(fixture(), { query: ' ' }).status, 'choose_object');
  assert.equal(search(fixture(), { query: '不是钥匙，是钱包' }).status, 'choose_object');
  assert.equal(search(fixture(), { query: 'not my keys' }).status, 'choose_object');
  assert.equal(search(fixture(), { query: '钱包', clip_id: 'old' }).status, 'no_evidence');
  const data = fixture(); data.observations = data.observations.filter(o => o.state === 'uncertain');
  assert.equal(search(data, { query: '钥匙' }).status, 'no_evidence');
});
test('available-object guidance follows the catalog, including newly indexed objects', () => {
  const data = fixture();
  data.objects.push(
    { id: 'paper-roll', label_zh: '纸巾卷', aliases: ['纸巾卷', '紙巾卷', 'paper towel roll'] },
    { id: 'earbud-case', label_zh: '白色耳机盒', aliases: ['耳机盒', '耳機盒', 'earbud case', 'earbuds case'] },
  );
  const result = search(data, { query: '手机在哪里' });
  assert.equal(result.status, 'no_evidence');
  for (const object of data.objects) assert.ok(result.message.includes(object.label_zh));
  const onlyNew = { ...data, objects: data.objects.slice(2), observations: [] };
  const narrowed = search(onlyNew, { query: '钥匙在哪里' });
  assert.ok(narrowed.message.includes('纸巾卷、白色耳机盒'));
  assert.ok(!narrowed.message.includes('钱包'));
});
test('negated new-object aliases request an explicit choice without answering another object', () => {
  const data = fixture();
  data.objects.push(
    { id: 'paper-roll', label_zh: '纸巾卷', aliases: ['纸巾卷', '紙巾卷', 'paper towel roll'] },
    { id: 'earbud-case', label_zh: '白色耳机盒', aliases: ['耳机盒', '耳機盒', 'earbud case', 'earbuds case'] },
  );
  for (const query of ['不是纸巾卷，是钱包', '不是白色耳机盒，是钱包', '不要找耳机盒，找钥匙', '不用找耳機盒',
    'not my paper towel roll, find my keys', 'NOT THE EARBUD CASE', 'not the earbuds case']) {
    const result = search(data, { query });
    assert.equal(result.status, 'choose_object', query);
    assert.deepEqual(result.answers, [], query);
  }
  assert.equal(search(data, { query: 'notebook and wallet' }).status, 'found');
  assert.equal(search(data, { query: 'not paper towel roller, find keys' }).status, 'found');
});
test('invalid filters, oversized queries, and malformed structures fail explicitly', () => {
  for (const input of [{ query: '钥匙', clip_id: '../private' }, { query: 'x'.repeat(301) }, {}, null])
    assert.throws(() => search(fixture(), input), InputError);
  for (const mutate of [
    d => { d.observations[0].output_pts_seconds = 61; },
    d => { d.observations[0].evidence_file = '../secret'; },
    d => { d.observations[0].review_status = 'human_confirmed'; },
    d => { delete d.observations[0].usable_as_clear_evidence; },
    d => { d.observations[0].is_object_visible = 'true'; },
    d => { d.clips[0].started_at = 'invalid'; },
    d => { d.observations[1].id = d.observations[0].id; },
  ]) { const d = fixture(); mutate(d); assert.throws(() => validateDataset(d), InputError); }
});
test('public API provides local evidence routes and never filesystem paths', () => {
  const result = catalog(fixture());
  assert.equal(result.clips[0].video_url, '/media/old');
  assert.equal(result.observations[0].evidence_url, '/evidence/placed-old');
  assert.equal(result.clips[0].video_file, undefined);
  assert.equal(result.observations[0].evidence_file, undefined);
});
test('browser byte ranges support start/end and suffix, clamp EOF, reject ambiguous or invalid requests', () => {
  assert.deepEqual(parseRange('bytes=0-9', 100), { start: 0, end: 9 });
  assert.deepEqual(parseRange('bytes=90-', 100), { start: 90, end: 99 });
  assert.deepEqual(parseRange('bytes=-20', 100), { start: 80, end: 99 });
  assert.deepEqual(parseRange('bytes=90-200', 100), { start: 90, end: 99 });
  for (const v of ['bytes=100-', 'bytes=8-4', 'bytes=-0', 'bytes=0-1,4-5', 'items=0-9', 'bytes=-'])
    assert.throws(() => parseRange(v, 100), InputError);
});
