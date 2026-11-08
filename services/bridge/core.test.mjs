import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign } from 'node:crypto';
import {
  AppError, ISSUER, MAX_VIDEO_BODY_BYTES, MAX_VIDEO_FRAME_BYTES, MAX_MEMORY_OUTPUT_CHARS, MAX_MEMORY_HIGHLIGHTS_OUTPUT_CHARS,
  captionPayload, parseVideoRequest, videoSummaryPayload, parseVideoMemoryResponse, readCaptionStream, verifyIDToken,
} from './core.mjs';

const now = 1_800_000_000;
const options = { clientID: 'test-client', nonce: 'test-nonce', now };
const claims = {
  iss: ISSUER, aud: options.clientID, sub: 'test-user',
  iat: now - 60, exp: now + 300, nonce: options.nonce,
};
const rsa = generateKeyPairSync('rsa', { modulusLength: 2048 });
const ec = generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
const jwk = (pair, alg) => ({
  ...pair.publicKey.export({ format: 'jwk' }), kid: alg, alg, use: 'sig',
});

const frameJPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0xff, 0xd9]);
const videoFrame = timestamp_seconds => ({ timestamp_seconds, jpeg_base64: frameJPEG.toString('base64') });
const videoBody = value => Buffer.from(JSON.stringify(value));

test('video validation accepts boundaries and preserves timestamped JPEG bytes', () => {
  const frames = Array.from({ length: 15 }, (_, i) => videoFrame(i * 2));
  const video = parseVideoRequest(videoBody({ duration_seconds: 30, frames }));
  assert.equal(video.duration_seconds, 30);
  assert.equal(video.frames.length, 15);
  assert.deepEqual(video.frames[14], { timestamp_seconds: 28, jpeg: frameJPEG });
  for (const duration of [0.01, 31, 300, 301]) {
    assert.equal(parseVideoRequest(videoBody({ duration_seconds: duration, frames: [videoFrame(duration)] })).frames.length, 1);
  }
  const maxJPEG = Buffer.alloc(MAX_VIDEO_FRAME_BYTES); frameJPEG.copy(maxJPEG);
  assert.equal(parseVideoRequest(videoBody({ duration_seconds: 1, frames: [
    { ...videoFrame(0), jpeg_base64: maxJPEG.toString('base64') },
  ] })).frames[0].jpeg.length, MAX_VIDEO_FRAME_BYTES);
});

test('video validation rejects malformed, unbounded, unordered, and non-JPEG inputs', () => {
  const invalid = error => error instanceof AppError && error.status === 400 && error.code === 'invalid_video';
  assert.throws(() => parseVideoRequest(Buffer.from('{bad json')), invalid);
  for (const value of [null, [], {}, ...[0, -1, 301.01, '300', null].map(duration_seconds => ({ duration_seconds, frames: [videoFrame(0)] })),
    { duration_seconds: 30, frames: [] },
    { duration_seconds: 300, frames: Array.from({ length: 151 }, (_, i) => videoFrame(i * 2)) },
    { duration_seconds: 30, frames: [videoFrame(1), videoFrame(0)] },
    { duration_seconds: 30, frames: [videoFrame(1), videoFrame(1)] },
    ...[-1, 31, '2', null].map(timestamp => ({ duration_seconds: 30, frames: [videoFrame(timestamp)] })),
    ...['', 'hello', '/9j/4AAQ/9k=\n', 'data:image/jpeg;base64,/9j/4AAQ/9k=', 'YWJjZA==', '/9j/4AAQ/9l='].map(jpeg_base64 => ({
      duration_seconds: 30, frames: [{ timestamp_seconds: 0, jpeg_base64 }],
    })),
  ]) assert.throws(() => parseVideoRequest(videoBody(value)), invalid, JSON.stringify(value));
  const largeJPEG = Buffer.alloc(MAX_VIDEO_FRAME_BYTES + 1); frameJPEG.copy(largeJPEG);
  assert.throws(() => parseVideoRequest(videoBody({ duration_seconds: 30, frames: [{
    timestamp_seconds: 0, jpeg_base64: largeJPEG.toString('base64'),
  }] })), error => error.status === 413);
  assert.throws(() => parseVideoRequest(Buffer.alloc(MAX_VIDEO_BODY_BYTES + 1)), error => error.status === 413);
});

test('five-minute requests accept 150 frames within the 12 MiB JPEG and 18 MiB JSON budgets', () => {
  const totalJPEGBudget = 12 * 1024 * 1024;
  const perFrameBudget = Math.min(MAX_VIDEO_FRAME_BYTES, Math.floor(totalJPEGBudget / 150));
  const jpeg = Buffer.alloc(perFrameBudget); frameJPEG.copy(jpeg);
  const frames = Array.from({ length: 150 }, (_, index) => ({
    timestamp_seconds: index * 2, jpeg_base64: jpeg.toString('base64'),
  }));
  const body = videoBody({ duration_seconds: 300, frames, include_memory: true, include_highlights: true });
  assert.ok(body.length < MAX_VIDEO_BODY_BYTES);
  const parsed = parseVideoRequest(body);
  assert.equal(parsed.frames.length, 150);
  assert.equal(parsed.duration_seconds, 300);
  assert.equal(parsed.frames.reduce((sum, frame) => sum + frame.jpeg.length, 0), perFrameBudget * 150);
  assert.ok(perFrameBudget * 150 <= totalJPEGBudget);
  assert.deepEqual(parsed.frames[149], { timestamp_seconds: 298, jpeg });
  assert.throws(() => parseVideoRequest(videoBody({ duration_seconds: 300, frames: [videoFrame(300.01)] })),
    error => error.status === 400);
  assert.throws(() => parseVideoRequest(videoBody({ duration_seconds: 301, frames: [videoFrame(301.01)] })),
    error => error.status === 400);
});

test('video payload uses one ordered, streaming, non-stored multimodal request with bounded English summary instructions', () => {
  const video = parseVideoRequest(videoBody({ duration_seconds: 3, frames: [videoFrame(0), videoFrame(2)] }));
  const payload = videoSummaryPayload('existing-model', video);
  assert.equal(payload.model, 'existing-model');
  assert.equal(payload.store, false);
  assert.equal(payload.stream, true);
  assert.equal(payload.input.length, 1);
  assert.match(payload.instructions, /English/);
  assert.match(payload.instructions, /90 words/);
  assert.match(payload.instructions, /No audio/);
  assert.match(payload.instructions, /untrusted/);
  assert.match(payload.instructions, /between frames/);
  const content = payload.input[0].content;
  assert.deepEqual(content.map(item => item.type), ['input_text', 'input_text', 'input_image', 'input_text', 'input_image']);
  assert.match(content[1].text, /0 seconds/);
  assert.match(content[3].text, /2 seconds/);
  assert.equal(content[2].image_url, `data:image/jpeg;base64,${frameJPEG.toString('base64')}`);
});

test('include_memory is optional and boolean, with false preserving the existing parsed request and payload', () => {
  const input = { duration_seconds: 3, frames: [videoFrame(0), videoFrame(2)] };
  const legacy = parseVideoRequest(videoBody(input));
  const disabled = parseVideoRequest(videoBody({ ...input, include_memory: false }));
  assert.deepEqual(disabled, legacy);
  assert.deepEqual(videoSummaryPayload('existing-model', disabled), videoSummaryPayload('existing-model', legacy));
  assert.equal(parseVideoRequest(videoBody({ ...input, include_memory: true })).include_memory, true);
  for (const include_memory of [null, 0, 1, '', 'true', [], {}]) {
    assert.throws(() => parseVideoRequest(videoBody({ ...input, include_memory })),
      error => error instanceof AppError && error.status === 400 && error.code === 'invalid_video');
  }
});

test('include_highlights validates its type and memory dependency while false preserves plain and v1 payloads', () => {
  const input = { duration_seconds: 3, frames: [videoFrame(0), videoFrame(2)] };
  for (const include_memory of [undefined, false, true]) {
    const baseline = parseVideoRequest(videoBody({ ...input, include_memory }));
    const disabled = parseVideoRequest(videoBody({ ...input, include_memory, include_highlights: false }));
    assert.deepEqual(disabled, baseline);
    assert.deepEqual(videoSummaryPayload('model', disabled), videoSummaryPayload('model', baseline));
  }
  const enabled = parseVideoRequest(videoBody({ ...input, include_memory: true, include_highlights: true }));
  assert.equal(enabled.include_highlights, true);
  const invalid = error => error instanceof AppError && error.status === 400 && error.code === 'invalid_video';
  for (const include_highlights of [null, 0, 1, '', 'true', [], {}]) {
    assert.throws(() => parseVideoRequest(videoBody({ ...input, include_memory: true, include_highlights })), invalid);
  }
  for (const include_memory of [undefined, false]) {
    assert.throws(() => parseVideoRequest(videoBody({ ...input, include_memory, include_highlights: true })), invalid);
  }
});

test('five-minute v2 adds grounded chronological highlights to one ordered 150-frame payload', () => {
  const input = { duration_seconds: 301, include_memory: true, include_highlights: true,
    frames: Array.from({ length: 150 }, (_, index) => videoFrame(index * 2)) };
  const parsed = parseVideoRequest(videoBody(input));
  const payload = videoSummaryPayload('model-snapshot', parsed);
  assert.equal(payload.model, 'model-snapshot');
  assert.equal(payload.store, false);
  assert.equal(payload.stream, true);
  assert.equal(payload.input.length, 1);
  assert.equal(payload.input[0].content.filter(item => item.type === 'input_image').length, 150);
  assert.ok(payload.instructions.includes('"highlights":[{"text":string,"frame_indices":[integer]}]'));
  for (const phrase of ['Preserve all useful detailed observations', 'opening frame', '3–5', 'at most 5 highlights',
    'existing observation', 'not positions in the observations array', 'without overlap or interleaving',
    'Partial or obscured text and numbers', 'complete reading', 'planting time', 'intended use', 'untrusted',
    'No audio', 'State uncertainty', 'summary, observations, and highlights', 'at most 150 observations']) assert.ok(payload.instructions.includes(phrase), phrase);
  parsed.frames.forEach((frame, index) => {
    assert.ok(payload.input[0].content[1 + index * 2].text.includes(`frame_index: ${index}; timestamp_seconds: ${frame.timestamp_seconds}.`));
  });
  assert.throws(() => parseVideoRequest(videoBody({ ...input, duration_seconds: 301.01 })), error => error.status === 400);
  assert.throws(() => parseVideoRequest(videoBody({ ...input, frames: [...input.frames, videoFrame(300)] })), error => error.status === 400);
});

test('v2 preserves all observations and maps sparse highlight references by frame index to the first real timestamp', () => {
  const frames = [0, 0.75, 2.5, 4.25, 5.5, 7].map(timestamp_seconds => ({ timestamp_seconds }));
  const value = { summary: 'A sequence of visible details.', observations: [0, 2, 3, 4, 5].map(frame_index => ({
    frame_index, text: `Visible detail ${frame_index}.`,
  })), highlights: [
    { text: ' The first group of details. ', frame_indices: [0, 2] },
    { text: 'A later detail.', frame_indices: [5] },
  ] };
  const memory = parseVideoMemoryResponse(JSON.stringify(value), frames, { includeHighlights: true });
  assert.equal(memory.observations.length, 5);
  assert.deepEqual(memory.observations.map(item => item.frame_index), [0, 2, 3, 4, 5]);
  assert.deepEqual(memory.highlights, [
    { text: 'The first group of details.', frame_indices: [0, 2], timestamp_seconds: 0 },
    { text: 'A later detail.', frame_indices: [5], timestamp_seconds: 7 },
  ]);
  assert.deepEqual(parseVideoMemoryResponse(JSON.stringify({ ...value, observations: [], highlights: [] }), frames,
    { includeHighlights: true }), { summary: value.summary, observations: [], highlights: [] });
  assert.deepEqual(parseVideoMemoryResponse(JSON.stringify({ ...value, highlights: [] }), frames,
    { includeHighlights: true }).highlights, []);
  assert.equal(parseVideoMemoryResponse(JSON.stringify({ ...value, highlights: [value.highlights[0]] }), frames,
    { includeHighlights: true }).highlights.length, 1);
});

test('v2 rejects malformed highlights, missing evidence, range errors, duplicate or crossing groups, and forged timestamps', () => {
  const frames = Array.from({ length: 15 }, (_, timestamp_seconds) => ({ timestamp_seconds }));
  const observation = { frame_index: 0, text: 'Visible evidence.' };
  const value = { summary: 'Visible details.', observations: [0, 2, 3, 4, 5, 6, 7].map(frame_index => ({ ...observation, frame_index })) };
  const highlight = { text: 'A visible detail.', frame_indices: [0] };
  const badGroups = [null, {}, [null], [[]], [{}], [{ text: 'No evidence.' }],
    ...['', '  ', null, 42, 'x'.repeat(301)].map(text => [{ ...highlight, text }]),
    ...[null, {}, [], [1], [-1], [15], [0.5], ['0'], [null], [0, 0], [2, 0]].map(frame_indices => [{ ...highlight, frame_indices }]),
    [{ ...highlight, timestamp_seconds: 99 }],
    [highlight, highlight],
    [{ ...highlight, frame_indices: [0, 3] }, { ...highlight, frame_indices: [2, 4] }],
    [{ ...highlight, frame_indices: [3] }, { ...highlight, frame_indices: [2] }],
    [0, 2, 3, 4, 5, 6].map(index => ({ ...highlight, frame_indices: [index] })),
  ];
  const invalid = error => error instanceof AppError && error.status === 502 && error.code === 'invalid_memory_response';
  for (const highlights of badGroups) {
    assert.throws(() => parseVideoMemoryResponse(JSON.stringify({ ...value, highlights }), frames,
      { includeHighlights: true }), invalid, JSON.stringify(highlights));
  }
  assert.throws(() => parseVideoMemoryResponse(JSON.stringify(value), frames, { includeHighlights: true }), invalid);
  assert.throws(() => parseVideoMemoryResponse(JSON.stringify({ ...value, highlights: [], timestamp_seconds: 1 }), frames,
    { includeHighlights: true }), invalid);
  assert.throws(() => parseVideoMemoryResponse(JSON.stringify({ ...value, observations: [], highlights: [highlight] }), frames,
    { includeHighlights: true }), invalid);
  // V1 continues to reject unsolicited v2 output instead of silently accepting it.
  assert.throws(() => parseVideoMemoryResponse(JSON.stringify({ ...value, highlights: [] }), frames), invalid);
});

test('five-minute memory accepts 150 maximum observations and escaped JSON with at most five highlights', async () => {
  const frames = Array.from({ length: 150 }, (_, index) => ({ timestamp_seconds: index * 2 }));
  const value = { summary: 's'.repeat(1500),
    observations: frames.map((_, frame_index) => ({ frame_index, text: 't'.repeat(300) })),
    highlights: Array.from({ length: 5 }, (_, index) => ({ text: 'h'.repeat(300), frame_indices: Array.from({ length: 30 }, (_, offset) => index * 30 + offset) })),
  };
  const text = JSON.stringify(value).replace(/[a-z_]/g, character => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
  assert.ok(text.length > 60000 && text.length < MAX_MEMORY_HIGHLIGHTS_OUTPUT_CHARS);
  const output = await readCaptionStream(bytes([event({ type: 'response.completed', response: {
    status: 'completed', output: [{ content: [{ type: 'output_text', text }] }],
  } })]), { maxTextLength: MAX_MEMORY_HIGHLIGHTS_OUTPUT_CHARS });
  const parsed = parseVideoMemoryResponse(output, frames, { includeHighlights: true });
  assert.equal(parsed.observations.length, 150);
  assert.equal(parsed.highlights.length, 5);
  assert.equal(parsed.highlights[4].text.length, 300);
  assert.equal(parsed.highlights[4].timestamp_seconds, 240);
  const v1 = JSON.stringify({ summary: value.summary, observations: value.observations })
    .replace(/[a-z_]/g, character => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
  const v1Output = await readCaptionStream(bytes([event({ type: 'response.completed', response: {
    status: 'completed', output: [{ content: [{ type: 'output_text', text: v1 }] }],
  } })]), { maxTextLength: MAX_MEMORY_OUTPUT_CHARS });
  assert.equal(parseVideoMemoryResponse(v1Output, frames).observations.length, 150);
  const raw = JSON.stringify(value);
  const padded = `${' '.repeat(MAX_MEMORY_OUTPUT_CHARS + 1 - raw.length)}${raw}`;
  const stream = () => bytes([event({ type: 'response.completed', response: {
    status: 'completed', output: [{ content: [{ type: 'output_text', text: padded }] }],
  } })]);
  assert.ok(padded.length < MAX_MEMORY_HIGHLIGHTS_OUTPUT_CHARS);
  await assert.rejects(readCaptionStream(stream(), { maxTextLength: MAX_MEMORY_OUTPUT_CHARS }), modelError);
  assert.equal(parseVideoMemoryResponse(await readCaptionStream(stream(), { maxTextLength: MAX_MEMORY_HIGHLIGHTS_OUTPUT_CHARS }),
    frames, { includeHighlights: true }).highlights.length, 5);
});

test('memory payload labels each image with its zero-based index and real timestamp in one ordered request', () => {
  const input = { duration_seconds: 3.25, include_memory: true, frames: [0.125, 1.75, 3.25].map((timestamp_seconds, index) => ({
    timestamp_seconds, jpeg_base64: Buffer.concat([frameJPEG, Buffer.from([index])]).toString('base64'),
  })) };
  const video = parseVideoRequest(videoBody(input));
  const payload = videoSummaryPayload('model-snapshot', video);
  assert.deepEqual(Object.keys(payload).sort(), ['input', 'instructions', 'model', 'store', 'stream']);
  assert.equal(payload.model, 'model-snapshot');
  assert.equal(payload.store, false);
  assert.equal(payload.stream, true);
  assert.equal(payload.input.length, 1);
  for (const phrase of ['strict JSON', '90 words', '1500 characters', '300 characters', 'strictly increasing',
    'empty observations array', 'English', 'untrusted', 'between frames', 'No audio', 'guess place names', 'identify people']) {
    assert.ok(payload.instructions.includes(phrase), phrase);
  }
  const content = payload.input[0].content;
  assert.equal(content.length, 1 + video.frames.length * 2);
  video.frames.forEach((frame, index) => {
    assert.equal(content[1 + index * 2].type, 'input_text');
    assert.ok(content[1 + index * 2].text.includes(`frame_index: ${index}; timestamp_seconds: ${frame.timestamp_seconds}.`));
    assert.deepEqual(content[2 + index * 2], {
      type: 'input_image', image_url: `data:image/jpeg;base64,${frame.jpeg.toString('base64')}`, detail: 'auto',
    });
  });
});

test('memory parsing maps sparse observations to input timestamps and accepts an empty observation list', () => {
  const frames = [0.125, 1.75, 3.25].map(timestamp_seconds => ({ timestamp_seconds }));
  assert.deepEqual(parseVideoMemoryResponse(JSON.stringify({ summary: ' A blue door is visible. ', observations: [
    { frame_index: 0, text: ' A blue door with a silver handle. ' },
    { frame_index: 2, text: 'The word EXIT is readable above a door.' },
  ] }), frames), { summary: 'A blue door is visible.', observations: [
    { frame_index: 0, timestamp_seconds: 0.125, text: 'A blue door with a silver handle.' },
    { frame_index: 2, timestamp_seconds: 3.25, text: 'The word EXIT is readable above a door.' },
  ] });
  assert.deepEqual(parseVideoMemoryResponse('{"summary":"The frames are blurred.","observations":[]}', frames),
    { summary: 'The frames are blurred.', observations: [] });
  assert.equal(parseVideoMemoryResponse(JSON.stringify({ summary: 's'.repeat(1500), observations: [
    { frame_index: 0, text: 't'.repeat(300) },
  ] }), frames).observations[0].text.length, 300);
});

test('memory parsing rejects invalid JSON, shape, bounds, ordering, duplicate indices, and oversized text with 502', () => {
  const frames = [0, 1, 2].map(timestamp_seconds => ({ timestamp_seconds }));
  const summary = 'A table is visible.';
  const observation = { frame_index: 0, text: 'A red cup on a table.' };
  const badValues = [null, [], {}, { summary }, { summary, observations: null }, { summary, observations: {} },
    { summary, observations: [], timestamp_seconds: 99 },
    ...['', '   ', null, 42, 's'.repeat(1501)].map(summary => ({ summary, observations: [] })),
    ...[null, [], {}, { ...observation, timestamp_seconds: 999 },
      ...[-1, 3, 0.5, '0', null].map(frame_index => ({ ...observation, frame_index })),
      ...['', '  ', null, 42, 't'.repeat(301)].map(text => ({ ...observation, text })),
    ].map(value => ({ summary, observations: [value] })),
    { summary, observations: [observation, observation] },
    { summary, observations: [{ ...observation, frame_index: 2 }, observation] },
    { summary, observations: Array.from({ length: 4 }, (_, frame_index) => ({ ...observation, frame_index })) },
  ];
  const invalid = error => error instanceof AppError && error.status === 502 && error.code === 'invalid_memory_response';
  for (const text of ['not json', '```json\n{"summary":"A table.","observations":[]}\n```', ...badValues.map(JSON.stringify)]) {
    assert.throws(() => parseVideoMemoryResponse(text, frames), invalid, text);
  }
  const excessiveFrames = Array.from({ length: 151 }, (_, timestamp_seconds) => ({ timestamp_seconds }));
  assert.throws(() => parseVideoMemoryResponse(JSON.stringify({ summary, observations: excessiveFrames.map((_, frame_index) => ({
    ...observation, frame_index,
  })) }), excessiveFrames), invalid);
});

test('memory stream limit permits JSON escaping overhead while the legacy caption limit stays unchanged', async () => {
  const frames = Array.from({ length: 15 }, (_, timestamp_seconds) => ({ timestamp_seconds }));
  const value = { summary: 's'.repeat(1500), observations: frames.map((_, frame_index) => ({ frame_index, text: 't'.repeat(300) })) };
  const text = JSON.stringify(value).replace(/[st]/g, character => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
  assert.ok(text.length > 6000 && text.length < MAX_MEMORY_OUTPUT_CHARS);
  const stream = () => bytes([event({ type: 'response.completed', response: {
    status: 'completed', output: [{ content: [{ type: 'output_text', text }] }],
  } })]);
  assert.equal(parseVideoMemoryResponse(await readCaptionStream(stream(), { maxTextLength: MAX_MEMORY_OUTPUT_CHARS }), frames).observations.length, 15);
  await assert.rejects(readCaptionStream(stream()), modelError);
});
const keys = [jwk(rsa, 'RS256'), jwk(ec, 'ES256')];
const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');
function token(payload = claims, alg = 'RS256', header = {}) {
  const signingInput = `${encode({ alg, kid: alg, ...header })}.${encode(payload)}`;
  const pair = alg === 'ES256' ? ec : rsa;
  const signature = sign('sha256', Buffer.from(signingInput), {
    key: pair.privateKey, dsaEncoding: 'ieee-p1363',
  }).toString('base64url');
  return `${signingInput}.${signature}`;
}
const identityError = error => error instanceof AppError
  && error.status === 401 && error.code === 'invalid_identity';
const modelError = error => error instanceof AppError
  && error.status === 502 && error.code === 'model_request_failed';
const event = value => `data: ${JSON.stringify(value)}\n\n`;
async function* bytes(parts) { for (const part of parts) yield Buffer.from(part); }
async function* splitEveryByte(text) {
  for (const byte of Buffer.from(text)) yield Uint8Array.of(byte);
}

test('ID tokens verify RSA and P-256 signatures and binding to the login request', () => {
  for (const algorithm of ['RS256', 'ES256']) {
    assert.deepEqual(verifyIDToken(token(claims, algorithm), keys, options), claims);
  }
  const multiAudience = { ...claims, aud: ['another-app', options.clientID], azp: options.clientID };
  assert.equal(verifyIDToken(token(multiAudience), keys, options).sub, 'test-user');
  assert.equal(verifyIDToken(token(), keys, { ...options, subject: 'test-user' }).sub, 'test-user');
});

test('ID tokens reject modified signatures or payloads, unsupported algorithms, and unknown keys', () => {
  const [header, payload, signature] = token().split('.');
  const modifiedSignature = Buffer.from(signature, 'base64url');
  modifiedSignature[0] ^= 1;
  const altered = [
    `${header}.${payload}.${modifiedSignature.toString('base64url')}`,
    `${header}.${encode({ ...claims, sub: 'attacker' })}.${signature}`,
    token(claims, 'HS256'),
    `${encode({ alg: 'none' })}.${payload}.`,
    token(claims, 'RS256', { kid: 'unknown-key' }),
    token(claims, 'RS256', { crit: ['unrecognized-extension'] }),
    'not.a.valid.token',
  ];
  for (const value of altered) assert.throws(() => verifyIDToken(value, keys, options), identityError);
  assert.throws(() => verifyIDToken(token(), [{ ...keys[0], use: 'enc' }], options), identityError);
  assert.throws(() => verifyIDToken(token(), [{ ...keys[0], alg: 'ES256' }], options), identityError);
});

test('ID tokens reject the wrong issuer, audience, authorized party, nonce, subject, and expiry', () => {
  const invalid = [
    { iss: 'https://attacker.invalid' },
    { aud: 'another-client' },
    { aud: [options.clientID, 'another-client'], azp: undefined },
    { azp: 'another-client' },
    { nonce: undefined },
    { nonce: 'other-login-request' },
    { sub: '' },
    { sub: 42 },
    { exp: now },
    { exp: now - 1 },
    { exp: String(now + 300) },
    { exp: undefined },
    { iat: now + 31 },
    { nbf: now + 31 },
  ];
  for (const overrides of invalid) {
    assert.throws(() => verifyIDToken(token({ ...claims, ...overrides }), keys, options), identityError,
      `accepted invalid claims: ${JSON.stringify(overrides)}`);
  }
  assert.throws(() => verifyIDToken(token(), keys, { ...options, subject: 'different-user' }), identityError);
});

test('ID tokens reject missing or malformed issued-at and malformed not-before values', () => {
  for (const overrides of [
    { iat: undefined }, { iat: 'not-a-time' }, { iat: String(now - 60) }, { iat: null },
    { nbf: 'not-a-time' }, { nbf: String(now - 60) }, { nbf: null },
  ]) {
    assert.throws(() => verifyIDToken(token({ ...claims, ...overrides }), keys, options), identityError,
      `accepted invalid time claim: ${JSON.stringify(overrides)}`);
  }
});

test('SSE handles byte-split UTF-8 and CRLF separators without losing caption content', async () => {
  const text = [
    ': keep-alive\n\n',
    event({ type: 'response.output_text.delta', delta: 'A café ' }),
    event({ type: 'response.output_text.delta', delta: 'table.' }),
    event({ type: 'response.completed', response: { status: 'completed' } }),
    'data: [DONE]\n\n',
  ].join('').replaceAll('\n', '\r\n');
  assert.equal(await readCaptionStream(splitEveryByte(text)), 'A café table.');
});

test('SSE uses the completed response text and supports a final event without a blank terminator', async () => {
  const stream = event({ type: 'response.output_text.delta', delta: 'Partial' })
    + event({ type: 'response.completed', response: {
      status: 'completed', output: [{ type: 'message', content: [
        { type: 'output_text', text: ' A desk with a laptop. ' },
      ] }],
    } }).trimEnd();
  assert.equal(await readCaptionStream(bytes([stream])), 'A desk with a laptop.');
});

test('SSE never returns partial caption text when the model fails or becomes incomplete', async () => {
  for (const failure of [
    { type: 'response.failed', response: { error: { code: 'server_error' } } },
    { type: 'response.incomplete', response: { incomplete_details: { reason: 'max_output_tokens' } } },
    { type: 'error', error: { code: 'server_error' } },
    { type: 'response.completed', response: { status: 'incomplete' } },
  ]) {
    await assert.rejects(readCaptionStream(bytes([
      event({ type: 'response.output_text.delta', delta: 'This must never be returned.' }),
      event(failure),
    ])), modelError);
  }
});

test('SSE rejects truncated streams, empty output, malformed JSON, and oversized output', async () => {
  const delta = event({ type: 'response.output_text.delta', delta: 'Partial caption' });
  await assert.rejects(readCaptionStream(bytes([delta])), modelError);
  await assert.rejects(readCaptionStream(bytes([delta, 'data: [DONE]\n\n'])), modelError);
  await assert.rejects(readCaptionStream(bytes([event({ type: 'response.completed' })])), modelError);
  await assert.rejects(readCaptionStream(bytes(['data: {bad-json}\n\n'])));
  await assert.rejects(readCaptionStream(bytes([
    event({ type: 'response.output_text.delta', delta: 'x'.repeat(6001) }),
    event({ type: 'response.completed' }),
  ])), modelError);
  await assert.rejects(readCaptionStream(bytes([Buffer.alloc(4 * 1024 * 1024 + 1)])), modelError);
});

test('SSE maps plan usage failures without treating prior output as success', async () => {
  await assert.rejects(readCaptionStream(bytes([
    event({ type: 'response.output_text.delta', delta: 'Partial' }),
    event({ type: 'response.failed', response: { error: { code: 'usage_limit_reached' } } }),
  ])), error => error instanceof AppError && error.status === 429 && error.code === 'usage_limit');
});

test('photo payload preserves JPEG bytes and uses the supported streaming, non-stored contract', () => {
  const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0xff, 0xd9]);
  const payload = captionPayload('a-verified-vision-model', jpeg);
  assert.deepEqual(Object.keys(payload).sort(), ['input', 'instructions', 'model', 'store', 'stream']);
  assert.equal(payload.model, 'a-verified-vision-model');
  assert.equal(payload.store, false);
  assert.equal(payload.stream, true);
  assert.match(payload.instructions, /English/);
  assert.match(payload.instructions, /untrusted/);
  assert.equal(payload.input.length, 1);
  assert.equal(payload.input[0].role, 'user');
  const image = payload.input[0].content.find(item => item.type === 'input_image');
  assert.equal(image.image_url, `data:image/jpeg;base64,${jpeg.toString('base64')}`);
  assert.deepEqual(Buffer.from(image.image_url.split(',')[1], 'base64'), jpeg);
  assert.equal(image.detail, 'auto');
  for (const unsupported of ['max_output_tokens', 'max_tokens', 'temperature', 'previous_response_id']) {
    assert.equal(Object.hasOwn(payload, unsupported), false);
  }
});
