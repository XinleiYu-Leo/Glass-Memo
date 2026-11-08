import test from 'node:test';
import assert from 'node:assert/strict';
import { MAX_VIDEO_FRAME_BYTES } from './core.mjs';
import { MAX_TEACHER_BODY_BYTES, MAX_TEACHER_OUTPUT_CHARS, TEACHER_PROMPT_VERSION,
  parseTeacherRequest, teacherLabelsPayload, parseTeacherLabelsResponse } from './teacher-labels.mjs';

const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0xff, 0xd9]);
export const requestInput = { expected_model: 'test-model', segment_id: 'clip:segment-1',
  frames: Array.from({ length: 6 }, (_, i) => ({ frame_id: `f-${i}`, time_sec: 10 + i / 2, jpeg_base64: jpeg.toString('base64') })),
  windows: [
    { sample_id: 'clip:23', frame_ids: ['f-0', 'f-1', 'f-2', 'f-3'] },
    { sample_id: 'clip:24', frame_ids: ['f-1', 'f-2', 'f-3', 'f-4'] },
    { sample_id: 'clip:25', frame_ids: ['f-2', 'f-3', 'f-4', 'f-5'] },
  ] };
export const labelsOutput = { segment_id: requestInput.segment_id, windows: [
  { sample_id: 'clip:23', label: 1, action: 'pickup', object: '杯子', evidence_frame_ids: ['f-0', 'f-2'], reason: '先在桌面上，后离开桌面被手持。' },
  { sample_id: 'clip:24', label: 0, action: 'other', object: '杯子', evidence_frame_ids: ['f-2', 'f-4'], reason: '持续握持并转动杯子。' },
  { sample_id: 'clip:25', label: null, action: 'uncertain', object: '', evidence_frame_ids: [], reason: '手部遮挡，无法确认新的支撑转移。' },
] };
const parsed = () => parseTeacherRequest(Buffer.from(JSON.stringify(requestInput)));
const serialize = value => Buffer.from(JSON.stringify(value));
const badRequest = value => assert.throws(() => parseTeacherRequest(serialize(value)), { status: 400, code: 'invalid_teacher_request' });

test('teacher request strips encoding, preserves original times and windows, and sends only requested evidence', () => {
  const value = parsed();
  assert.equal(value.expected_model, 'test-model');
  assert.deepEqual(value.windows, requestInput.windows);
  assert.equal(value.frames[0].time_sec, 10);
  assert.equal(Buffer.compare(value.frames[0].jpeg, jpeg), 0);
  assert.equal(value.frames[0].jpeg_base64, undefined);
  const payload = teacherLabelsPayload('selected-model', value);
  assert.equal(payload.model, 'selected-model'); assert.equal(payload.store, false); assert.equal(payload.stream, true);
  assert.equal(payload.input.length, 1); assert.equal(payload.input[0].role, 'user');
  assert.equal(payload.input[0].content.filter(item => item.type === 'input_image').length, 6);
  assert.deepEqual(JSON.parse(payload.input[0].content[0].text), { segment_id: value.segment_id, windows: value.windows });
  assert.match(payload.instructions, /support transfer/); assert.match(payload.instructions, /MUST NOT supply evidence/);
  assert.match(payload.instructions, /untrusted/); assert.match(payload.instructions, new RegExp(TEACHER_PROMPT_VERSION));
  assert.equal(JSON.stringify(payload).includes('expected_model'), false);
});

test('teacher request rejects reference leakage, invalid identities, unsafe encodings and unordered/duplicate frames', () => {
  for (const value of [null, [], { ...requestInput, expected_model: '' }, { ...requestInput, segment_id: '../escape' },
    { ...requestInput, reference_label: 1 }, { ...requestInput, frames: requestInput.frames.slice(0, 3) },
    { ...requestInput, frames: Array(18).fill(requestInput.frames[0]) }]) badRequest(value);
  for (const change of [ { frame_id: requestInput.frames[1].frame_id }, { frame_id: ' bad' }, { time_sec: null },
    { time_sec: -1 }, { time_sec: 11 }, { time_sec: 86401 }, { jpeg_base64: 'not base64' },
    { jpeg_base64: 'abcd===' }, { jpeg_base64: Buffer.from('not a jpeg').toString('base64') }, { label: 1 } ]) {
    const value = structuredClone(requestInput); Object.assign(value.frames[0], change); badRequest(value);
  }
  const gap = structuredClone(requestInput); gap.frames[5].time_sec = 20; badRequest(gap);
  assert.throws(() => parseTeacherRequest(Buffer.from('{bad')), { code: 'invalid_teacher_request' });
});

test('teacher request rejects untrusted, noncausal, gapped, repeated or oversized windows', () => {
  for (const windows of [ [], Array(15).fill(requestInput.windows[0]),
    [requestInput.windows[0], requestInput.windows[0]],
    [{ ...requestInput.windows[0], sample_id: 'bad/id' }],
    [{ ...requestInput.windows[0], frame_ids: ['f-0', 'f-1', 'f-2'] }],
    [{ ...requestInput.windows[0], frame_ids: ['f-0', 'f-1', 'f-2', 'unknown'] }],
    [{ ...requestInput.windows[0], frame_ids: ['f-0', 'f-1', 'f-2', 'f-2'] }],
    [{ ...requestInput.windows[0], frame_ids: ['f-3', 'f-2', 'f-1', 'f-0'] }],
    [{ ...requestInput.windows[0], frame_ids: ['f-0', 'f-1', 'f-2', 'f-4'] }],
    [{ ...requestInput.windows[0], answer: 1 }],
    [requestInput.windows[0], { ...requestInput.windows[0], sample_id: 'different' }],
  ]) badRequest({ ...requestInput, windows });
});

test('teacher request bounds entire body and decoded image bytes', () => {
  assert.throws(() => parseTeacherRequest(Buffer.alloc(MAX_TEACHER_BODY_BYTES + 1)), { status: 413 });
  const value = structuredClone(requestInput);
  value.frames[0].jpeg_base64 = Buffer.alloc(MAX_VIDEO_FRAME_BYTES + 1).toString('base64');
  assert.throws(() => parseTeacherRequest(serialize(value)), { status: 413 });
});

test('teacher response preserves null and restores trusted window order', () => {
  const output = structuredClone(labelsOutput); output.windows.reverse();
  assert.deepEqual(parseTeacherLabelsResponse(JSON.stringify(output), parsed()), labelsOutput);
  const place = structuredClone(labelsOutput); place.windows[0].action = 'place';
  assert.equal(parseTeacherLabelsResponse(JSON.stringify(place), parsed()).windows[0].action, 'place');
});

test('teacher response rejects invalid evidence atomically instead of assigning negatives', () => {
  const cases = [null, {}, { ...labelsOutput, segment_id: 'unknown' }, { ...labelsOutput, windows: labelsOutput.windows.slice(0, 2) },
    { ...labelsOutput, windows: [labelsOutput.windows[0], labelsOutput.windows[0], labelsOutput.windows[2]] },
    { ...labelsOutput, new_field: true }];
  for (const change of [ { sample_id: 'unknown' }, { label: false }, { label: 2 }, { label: '1' },
    { label: null, action: 'pickup' }, { action: 'other' }, { object: '' }, { object: 'a'.repeat(201) },
    { reason: '' }, { reason: '\u0000unsafe' }, { reason: 'a'.repeat(1001) }, { invented_time: 10 },
    { evidence_frame_ids: ['f-1'] }, { evidence_frame_ids: ['f-1', 'f-4'] },
    { evidence_frame_ids: ['f-2', 'f-1'] }, { evidence_frame_ids: ['f-1', 'f-1'] },
    { evidence_frame_ids: ['f-0', 'unknown'] }, { evidence_frame_ids: 'f-0' },
  ]) {
    const value = structuredClone(labelsOutput); Object.assign(value.windows[0], change); cases.push(value);
  }
  for (const change of [{ label: 0, action: 'pickup' }, { evidence_frame_ids: [] }, { evidence_frame_ids: ['f-0'] }]) {
    const value = structuredClone(labelsOutput); Object.assign(value.windows[1], change); cases.push(value);
  }
  for (const value of cases)
    assert.throws(() => parseTeacherLabelsResponse(JSON.stringify(value), parsed()), { status: 502, code: 'invalid_teacher_response' });
  for (const value of ['```json\n{}\n```', '{bad', 'a'.repeat(MAX_TEACHER_OUTPUT_CHARS + 1)])
    assert.throws(() => parseTeacherLabelsResponse(value, parsed()), { code: 'invalid_teacher_response' });
});
