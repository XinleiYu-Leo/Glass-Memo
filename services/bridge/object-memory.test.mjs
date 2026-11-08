import test from 'node:test';
import assert from 'node:assert/strict';
import { AppError, MAX_VIDEO_BODY_BYTES, MAX_VIDEO_FRAME_BYTES } from './core.mjs';
import { OBJECT_PROMPT_VERSION, MAX_OBJECT_FRAMES, MAX_OBJECT_OUTPUT_CHARS,
  parseObjectRequest, objectObservationsPayload, parseObjectObservationsResponse } from './object-memory.mjs';

const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0xff, 0xd9]);
const encode = value => Buffer.from(JSON.stringify(value));
const frame = (frame_id = 'f-000', timestamp_seconds = 0) => ({ frame_id, timestamp_seconds, jpeg_base64: jpeg.toString('base64') });
const input = () => ({ duration_seconds: 30, frames: [frame(), frame('f-001', 15), frame('f-002', 29)] });
const observation = (overrides = {}) => ({ object_id: 'keys', frame_id: 'f-000', state: 'visible',
  location_zh: '桌面上', observation_zh: '一串钥匙清楚可见。', visibility: 'clear', identity: 'match', hand_relation: 'free', needs_review: false, ...overrides });
const output = (overrides = {}) => ({ observations: [observation()], refine_frame_ids: [], ...overrides });
const invalidRequest = error => error instanceof AppError && error.status === 400 && error.code === 'invalid_object_request';
const invalidResponse = error => error instanceof AppError && error.status === 502 && error.code === 'invalid_object_response';

test('object request preserves bounded frame IDs, timestamps and JPEG bytes with optional expected model', () => {
  const value = { ...input(), expected_model: 'selected/model-v1' };
  const parsed = parseObjectRequest(encode(value));
  assert.equal(parsed.duration_seconds, 30);
  assert.equal(parsed.expected_model, value.expected_model);
  assert.deepEqual(parsed.frames.map(({ jpeg: image, ...rest }) => rest), value.frames.map(({ jpeg_base64, ...rest }) => rest));
  parsed.frames.forEach(item => assert.deepEqual(item.jpeg, jpeg));
  assert.equal(Object.hasOwn(parseObjectRequest(encode(input())), 'expected_model'), false);
  const maximum = { duration_seconds: 301, frames: Array.from({ length: MAX_OBJECT_FRAMES }, (_, i) => frame(`f:${i}`, i * 7)) };
  assert.equal(parseObjectRequest(encode(maximum)).frames.length, 40);
});

test('object request rejects malformed IDs, duplicates, unknown fields, invalid model and bad time order', () => {
  const valid = input();
  const invalidFrames = [
    [null], [1], [{}], [frame(), frame()],
    ...['', 'has space', '../escape', 'x\ntext', '_starts', 'f'.repeat(81), 7, null].map(frame_id => [frame(frame_id)]),
    [frame(), frame('f-001', 0)], [frame('f-001', 1), frame()],
    [frame('f-000', -1)], [frame('f-000', 30.1)], [frame('f-000', '1')],
    [{ ...frame(), extra: 'forged' }], [{ ...frame(), jpeg_base64: 'bad' }],
    Array.from({ length: 41 }, (_, i) => frame(`f-${i}`, i / 2)),
  ];
  for (const value of [null, [], {}, { ...valid, include_memory: true },
    ...[0, -1, 301.1, '30', null].map(duration_seconds => ({ ...valid, duration_seconds })),
    ...['', 'model with space', 'x'.repeat(201), null, 4].map(expected_model => ({ ...valid, expected_model })),
    ...invalidFrames.map(frames => ({ ...valid, frames })),
  ]) assert.throws(() => parseObjectRequest(encode(value)), invalidRequest, JSON.stringify(value));
  assert.throws(() => parseObjectRequest(Buffer.from('{bad')), invalidRequest);
});

test('object input enforces 18 MiB body and 256 KiB decoded JPEG limits', () => {
  const tooLarge = error => error instanceof AppError && error.status === 413;
  assert.throws(() => parseObjectRequest(Buffer.alloc(MAX_VIDEO_BODY_BYTES + 1)), tooLarge);
  const maximum = Buffer.alloc(MAX_VIDEO_FRAME_BYTES); jpeg.copy(maximum);
  assert.equal(parseObjectRequest(encode({ duration_seconds: 1,
    frames: [{ ...frame(), jpeg_base64: maximum.toString('base64') }] })).frames[0].jpeg.length, MAX_VIDEO_FRAME_BYTES);
  assert.throws(() => parseObjectRequest(encode({ duration_seconds: 1,
    frames: [{ ...frame(), jpeg_base64: Buffer.concat([maximum, Buffer.from([1])]).toString('base64') }] })), tooLarge);
});

test('object payload uses one non-stored supported request with original frame labels and no answer hints', () => {
  const media = parseObjectRequest(encode(input()));
  const payload = objectObservationsPayload('test-model', media);
  assert.deepEqual(Object.keys(payload).sort(), ['input', 'instructions', 'model', 'store', 'stream']);
  assert.equal(payload.model, 'test-model');
  assert.equal(payload.store, false); assert.equal(payload.stream, true);
  assert.equal(OBJECT_PROMPT_VERSION, 'object-observations-v1');
  assert.equal(payload.input.length, 1);
  for (const phrase of ['strict JSON', 'untrusted', 'not continuous video', 'never output placed', 'Touching is not holding',
    'empty drawer', 'destination after an object leaves view', 'refine_frame_ids', 'white small case']) assert.ok(payload.instructions.includes(phrase), phrase);
  const content = payload.input[0].content;
  assert.equal(content.length, 7);
  media.frames.forEach((item, index) => {
    assert.equal(content[1 + index * 2].text, `frame_id: ${item.frame_id}; timestamp_seconds: ${item.timestamp_seconds}.`);
    assert.equal(content[2 + index * 2].image_url, `data:image/jpeg;base64,${jpeg.toString('base64')}`);
  });
});

test('object response maps only real frame references, trims text and normalizes request ordering', () => {
  const frames = parseObjectRequest(encode(input())).frames;
  const parsed = parseObjectObservationsResponse(JSON.stringify(output({ observations: [
    observation({ object_id: 'wallet', frame_id: 'f-002' }),
    observation({ frame_id: 'f-001', state: 'held', hand_relation: 'held' }),
    observation({ location_zh: ' 桌面上 ' }),
    observation({ object_id: 'earbud_case', frame_id: 'f-001', state: 'uncertain', visibility: 'partial', identity: 'uncertain',
      hand_relation: 'unclear', needs_review: true }),
  ], refine_frame_ids: ['f-002', 'f-001'] })), frames);
  assert.deepEqual(parsed.observations.map(item => `${item.frame_id}/${item.object_id}`), ['f-000/keys', 'f-001/keys', 'f-001/earbud_case', 'f-002/wallet']);
  assert.deepEqual(parsed.refine_frame_ids, ['f-001', 'f-002']);
  assert.equal(parsed.observations[0].location_zh, '桌面上');
  assert.equal(Object.hasOwn(parsed.observations[0], 'timestamp_seconds'), false);
  assert.deepEqual(parseObjectObservationsResponse('{"observations":[],"refine_frame_ids":[]}', frames), { observations: [], refine_frame_ids: [] });
});

test('object response rejects forged references, action/state contradictions, duplicate pairs and malformed shapes atomically', () => {
  const frames = parseObjectRequest(encode(input())).frames;
  const badItems = [null, [], {},
    ...['new-frame', '', null, 0].map(frame_id => observation({ frame_id })),
    observation({ object_id: 'phone' }), observation({ state: 'placed' }),
    observation({ state: 'held', hand_relation: 'touching' }), observation({ state: 'held', hand_relation: 'free' }),
    observation({ state: 'visible', hand_relation: 'held' }),
    observation({ visibility: 'partial' }), observation({ identity: 'uncertain' }), observation({ state: 'uncertain' }),
    observation({ hand_relation: 'touching' }), observation({ hand_relation: 'unclear' }), observation({ needs_review: 'false' }),
    observation({ visibility: 'excellent' }), observation({ identity: 'guess' }), observation({ hand_relation: 'nearby' }),
    observation({ timestamp_seconds: 999 }), observation({ confidence: 1 }),
    ...['', '  ', null, 3, 'x'.repeat(121), 'bad\0text'].map(location_zh => observation({ location_zh })),
    ...['', '  ', null, 3, 'x'.repeat(301)].map(observation_zh => observation({ observation_zh })),
  ];
  const values = [null, [], {}, { observations: [] }, { ...output(), extra: true },
    ...badItems.map(item => output({ observations: [item] })),
    output({ observations: [observation(), observation()] }),
    ...[null, {}, ['f-999'], ['f-000', 'f-000'], [0]].map(refine_frame_ids => output({ refine_frame_ids })),
  ];
  for (const value of values) assert.throws(() => parseObjectObservationsResponse(JSON.stringify(value), frames), invalidResponse, JSON.stringify(value));
  for (const value of ['not JSON', '```json\n{}\n```', ' '.repeat(MAX_OBJECT_OUTPUT_CHARS + 1)])
    assert.throws(() => parseObjectObservationsResponse(value, frames), invalidResponse);
});

test('object output accepts every bounded target/frame and rejects excessive total observations', () => {
  const frames = Array.from({ length: 40 }, (_, i) => ({ frame_id: `f-${i}`, timestamp_seconds: i }));
  const observations = frames.flatMap(({ frame_id }) => ['keys', 'wallet', 'earbud_case'].map(object_id => observation({
    frame_id, object_id, location_zh: '位'.repeat(120), observation_zh: '物'.repeat(300),
  })));
  const value = output({ observations, refine_frame_ids: frames.map(frame => frame.frame_id) });
  const escaped = JSON.stringify(value).replace(/[\u4e00-\u9fff]/g, character => `\\u${character.charCodeAt(0).toString(16)}`);
  assert.ok(escaped.length < MAX_OBJECT_OUTPUT_CHARS);
  assert.equal(parseObjectObservationsResponse(escaped, frames).observations.length, 120);
  assert.throws(() => parseObjectObservationsResponse(JSON.stringify({ ...value, observations: [...observations, observation()] }), frames), invalidResponse);
});
