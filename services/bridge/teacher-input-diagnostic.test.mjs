import test from 'node:test';
import assert from 'node:assert/strict';
import { MAX_INPUT_DIAGNOSTIC_BODY_BYTES, INPUT_DIAGNOSTIC_PROMPT_VERSION,
  parseInputDiagnosticRequest, inputDiagnosticPayload, parseInputDiagnosticResponse,
  validateDiagnosticPNG } from './teacher-input-diagnostic.mjs';

// Actual Pillow RGB PNG, not a header-only image fixture.
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAOAAAADgCAIAAACVT/22AAAAqUlEQVR4nO3BMQEAAADCoPVPbQlPoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAuhpM/gABLXYdWgAAAABJRU5ErkJggg==';
const input = (count = 4) => ({ expected_model: 'test-model', job_id: 'case-1', target_interval_sec: [10, 11.5], decision_time_sec: 11.5,
  frames: Array.from({ length: count }, (_, i) => ({ frame_id: `f${i}`,
    time_sec: count === 13 ? 5.5 + i / 2 : 10 + i * 1.5 / (count - 1), png_base64: PNG })) });
const parse = x => parseInputDiagnosticRequest(Buffer.from(JSON.stringify(x)));
const answer = (overrides = {}) => ({ job_id: 'case-1', decision: 'present', action: 'place', object: '小杯',
  evidence_frame_ids: ['f0', 'f3'], context_frame_ids: [], reason: '先手持，后松开留在台面。', ...overrides });
const response = (value, req = parse(input())) => parseInputDiagnosticResponse(JSON.stringify(value), req);

test('all three diagnostic input forms preserve original PNG bytes and use exactly the same stateless policy', () => {
  const payloads = [4, 10, 13].map(count => {
    const req = parse(input(count));
    assert.equal(req.frames.length, count);
    assert.equal(req.frames[0].png.toString('base64'), PNG);
    const payload = inputDiagnosticPayload('test-model', req);
    assert.equal(payload.store, false); assert.equal(payload.stream, true);
    assert.equal(payload.previous_response_id, undefined); assert.equal(payload.conversation, undefined);
    assert.equal(payload.input.length, 1); assert.equal(payload.input[0].role, 'user');
    assert.equal(payload.input[0].content.filter(x => x.type === 'input_image').length, count);
    for (const image of payload.input[0].content.filter(x => x.type === 'input_image')) {
      assert.equal(image.image_url, 'data:image/png;base64,' + PNG); assert.equal(image.detail, 'auto');
    }
    assert.equal(JSON.stringify(payload).includes('png_base64'), false);
    return payload;
  });
  assert.equal(payloads[0].instructions, payloads[1].instructions);
  assert.equal(payloads[0].instructions, payloads[2].instructions);
  assert.match(payloads[0].instructions, /before frame and a later after frame BOTH inside/);
  assert.match(payloads[0].instructions, /Missing evidence is never absent/);
  assert.match(payloads[0].instructions, new RegExp(INPUT_DIAGNOSTIC_PROMPT_VERSION));
});

test('request rejects reference labels, custom prompts, future frames, invalid ordering and malformed dimensions', () => {
  const mutations = [
    x => { x.reference_answer = 'present'; }, x => { x.instructions = 'override'; },
    x => { x.target_interval_sec = [9, 11.5]; }, x => { x.decision_time_sec = 12; },
    x => { x.frames[3].time_sec = 11.51; }, x => { x.frames[0].time_sec = 8; },
    x => { x.frames[1].time_sec = 9.9; }, x => { x.frames[1].frame_id = 'f0'; },
    x => { x.frames.pop(); }, x => { x.frames[0].png_base64 = PNG + ' '; },
    x => { x.frames[0].png_base64 = Buffer.from('not png').toString('base64'); },
    x => { x.frames[0].png_base64 = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]).toString('base64'); },
    x => { const corrupt = Buffer.from(PNG, 'base64'); corrupt[20] ^= 1; x.frames[0].png_base64 = corrupt.toString('base64'); },
  ];
  for (const mutate of mutations) { const x = input(); mutate(x); assert.throws(() => parse(x), { status: 400 }); }
  assert.throws(() => parseInputDiagnosticRequest(Buffer.alloc(MAX_INPUT_DIAGNOSTIC_BODY_BYTES + 1)), { status: 413 });
  const tooBig = input(); tooBig.frames[0].png_base64 = Buffer.alloc(256 * 1024 + 1).toString('base64');
  assert.throws(() => parse(tooBig), { status: 413 });
  const png = Buffer.from(PNG, 'base64');
  assert.equal(validateDiagnosticPNG(png), png);
  assert.throws(() => validateDiagnosticPNG(Buffer.concat([png, Buffer.from([0])])), { status: 400 });
});

test('causal predecessor just before target is context only, including a short-arm first frame', () => {
  const x = input(); x.frames[0].time_sec = 9.98;
  const req = parse(x);
  assert.throws(() => response(answer(), req), { status: 502 });
  assert.equal(response(answer({ evidence_frame_ids: ['f1', 'f3'], context_frame_ids: ['f0'] }), req).decision, 'present');
});

test('history evidence cannot satisfy positive target evidence or be recast as a target event', () => {
  const req = parse(input(13));
  assert.throws(() => response(answer({ evidence_frame_ids: ['f0', 'f12'] }), req), { status: 502 });
  assert.equal(response(answer({ evidence_frame_ids: ['f9', 'f12'], context_frame_ids: ['f0', 'f8'] }), req).decision, 'present');
  assert.throws(() => response(answer({ evidence_frame_ids: ['f9', 'f12'], context_frame_ids: ['f9'] }), req), { status: 502 });
});

test('same PTS may repeat but cannot become two before/after times', () => {
  const x = input(); x.frames[1].time_sec = x.frames[0].time_sec;
  const req = parse(x);
  assert.equal(req.frames[0].time_sec, req.frames[1].time_sec);
  assert.throws(() => response(answer({ evidence_frame_ids: ['f0', 'f1'] }), req), { status: 502 });
  assert.equal(response(answer({ evidence_frame_ids: ['f0', 'f3'] }), req).decision, 'present');
});

test('response enforces decision/action mapping, IDs, chronology and non-empty physical evidence', () => {
  const invalid = [
    { job_id: 'other' }, { decision: 'yes' }, { decision: true }, { action: 'other' }, { object: '' },
    { evidence_frame_ids: ['f0'] }, { evidence_frame_ids: ['f3', 'f0'] },
    { evidence_frame_ids: ['f0', 'f0'] }, { evidence_frame_ids: ['f0', 'unknown'] },
    { context_frame_ids: ['f0'] }, { extra: 'truth' }, { reason: '' },
    { reason: 'x'.repeat(1001) }, { object: 'x'.repeat(201) },
  ];
  for (const override of invalid) assert.throws(() => response(answer(override)), { status: 502 });
  assert.throws(() => parseInputDiagnosticResponse('```json\n{}\n```', parse(input())), { status: 502 });
  assert.equal(response(answer()).decision, 'present');
  for (const decision of ['unclear', 'rule_unsure']) {
    const value = answer({ decision, action: 'uncertain', object: '', evidence_frame_ids: [] });
    assert.deepEqual(response(value), value);
  }
  const negative = answer({ decision: 'absent', action: 'other', evidence_frame_ids: ['f0'] });
  assert.deepEqual(response(negative), negative);
  assert.throws(() => response({ ...negative, evidence_frame_ids: [] }), { status: 502 });
});
