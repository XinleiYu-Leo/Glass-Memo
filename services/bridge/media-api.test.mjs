import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';
import { MAX_VIDEO_BODY_BYTES, RESOURCE } from './core.mjs';
import { createMediaAPI } from './media-api.mjs';

const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0xff, 0xd9]);
const video = Buffer.from(JSON.stringify({ duration_seconds: 30, frames: [0, 15, 29].map(timestamp_seconds => ({
  timestamp_seconds, jpeg_base64: jpeg.toString('base64'),
})) }));
const memoryVideo = Buffer.from(JSON.stringify({ ...JSON.parse(video), include_memory: true }));
const memoryOutput = JSON.stringify({ summary: 'A laptop and a desk are visible.', observations: [
  { frame_index: 0, text: 'A laptop is open on a desk.' },
  { frame_index: 2, text: 'A red cup stands on the desk.' },
] });
const highlightsVideo = Buffer.from(JSON.stringify({ ...JSON.parse(memoryVideo), include_highlights: true }));
const highlightsOutput = JSON.stringify({ ...JSON.parse(memoryOutput), highlights: [
  { text: 'Two visible details in sequence.', frame_indices: [0, 2] },
] });
function request(url = '/v1/video-summary', body = video, overrides = {}) {
  const req = Readable.from([body]);
  Object.assign(req, { method: 'POST', url, headers: {
    authorization: 'Bearer test-pairing', 'content-type': url === '/v1/caption' ? 'image/jpeg' : 'application/json',
    ...overrides,
  } });
  return req;
}
class ResponseStub extends EventEmitter {
  destroyed = false;
  writableEnded = false;
  writeHead(status, headers) { this.status = status; this.headers = headers; }
  end(body) { this.body = JSON.parse(body); this.writableEnded = true; }
  disconnect() { this.destroyed = true; this.emit('close'); }
}
function completed(text = 'A laptop is visible, then the camera view turns toward a desk.') {
  return { ok: true, body: Readable.from([Buffer.from(`data: ${JSON.stringify({
    type: 'response.completed', response: { status: 'completed', output: [{ content: [{ type: 'output_text', text }] }] },
  })}\n\n`)]) };
}
function setup(overrides = {}) {
  const calls = [];
  const api = createMediaAPI({
    getPairingCode: () => 'test-pairing', getStatus: () => ({ ready: true, model: 'existing-model' }),
    getModel: () => 'existing-model', getAccessToken: async () => 'mock-token', isAuthActive: () => false,
    fetchImpl: async (url, options) => { calls.push({ url, options }); return completed(); },
    ...overrides,
  });
  return { api, calls };
}
async function run(api, req = request()) { const res = new ResponseStub(); await api.handler(req, res); return res; }

test('photo and video use the same authorized upstream path and preserve photo response compatibility', async () => {
  const { api, calls } = setup();
  const videoRes = await run(api);
  assert.equal(videoRes.status, 200);
  assert.deepEqual(Object.keys(videoRes.body).sort(), ['caption', 'frame_count', 'request_id']);
  assert.equal(videoRes.body.frame_count, 3);
  assert.match(videoRes.body.request_id, /^[a-f0-9-]{36}$/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${RESOURCE}/responses`);
  assert.equal(calls[0].options.headers.Authorization, 'Bearer mock-token');
  assert.equal(JSON.parse(calls[0].options.body).input[0].content.filter(i => i.type === 'input_image').length, 3);
  const photoRes = await run(api, request('/v1/caption', jpeg));
  assert.equal(photoRes.status, 200);
  assert.deepEqual(Object.keys(photoRes.body).sort(), ['caption', 'request_id']);
  assert.equal(calls.length, 2);
  assert.equal(JSON.parse(calls[1].options.body).input[0].content.filter(i => i.type === 'input_image').length, 1);
  assert.equal(api.isBusy(), false);
});

test('explicit false preserves the legacy video response and upstream payload exactly', async () => {
  const { api, calls } = setup();
  const legacy = await run(api);
  const disabled = await run(api, request('/v1/video-summary', Buffer.from(JSON.stringify({ ...JSON.parse(video), include_memory: false }))));
  assert.equal(disabled.status, 200);
  assert.deepEqual(Object.keys(disabled.body).sort(), ['caption', 'frame_count', 'request_id']);
  assert.equal(disabled.body.caption, legacy.body.caption);
  assert.equal(disabled.body.frame_count, legacy.body.frame_count);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.body, calls[1].options.body);
});

test('disabled highlights preserve both plain and v1 payloads and responses', async () => {
  for (const body of [video, memoryVideo]) {
    const calls = [];
    const { api } = setup({ fetchImpl: async (_, options) => { calls.push(options.body); return completed(body === memoryVideo ? memoryOutput : 'Visible details.'); } });
    const original = await run(api, request('/v1/video-summary', body));
    const disabled = await run(api, request('/v1/video-summary', Buffer.from(JSON.stringify({ ...JSON.parse(body), include_highlights: false }))));
    assert.equal(disabled.status, 200);
    assert.deepEqual({ ...disabled.body, request_id: undefined }, { ...original.body, request_id: undefined });
    assert.equal(disabled.body.highlights, undefined);
    assert.equal(calls.length, 2);
    assert.equal(calls[0], calls[1]);
  }
});

test('v2 uses one authorized request, retains all observations, and returns real timestamps with the request model snapshot', async () => {
  let model = 'original-model', modelReads = 0;
  const calls = [], tokenStarted = Promise.withResolvers(), token = Promise.withResolvers();
  const { api } = setup({ getModel: () => { modelReads += 1; return model; },
    getAccessToken: () => { tokenStarted.resolve(); return token.promise; },
    fetchImpl: async (url, options) => { calls.push({ url, options }); return completed(highlightsOutput); },
  });
  const pending = run(api, request('/v1/video-summary', highlightsVideo));
  await tokenStarted.promise;
  model = 'changed-model';
  token.resolve('mock-token');
  const res = await pending;
  assert.equal(res.status, 200);
  assert.equal(res.body.prompt_version, 'recent-memory-v2-5m');
  assert.equal(res.body.model, 'original-model');
  assert.equal(res.body.caption, JSON.parse(memoryOutput).summary);
  assert.equal(res.body.frame_count, 3);
  assert.equal(res.body.observations.length, 2);
  assert.deepEqual(res.body.highlights, [{ text: 'Two visible details in sequence.', frame_indices: [0, 2], timestamp_seconds: 0 }]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${RESOURCE}/responses`);
  assert.equal(calls[0].options.headers.Authorization, 'Bearer mock-token');
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.model, res.body.model);
  assert.equal(payload.input[0].content.filter(item => item.type === 'input_image').length, 3);
  assert.ok(payload.instructions.includes('"highlights"'));
  assert.equal(modelReads, 1);
  assert.equal(api.isBusy(), false);
});

test('invalid highlight switches fail before inference', async () => {
  const { api, calls } = setup();
  for (const flags of [
    ...[null, 0, 1, 'true', [], {}].map(include_highlights => ({ include_memory: true, include_highlights })),
    { include_highlights: true }, { include_memory: false, include_highlights: true },
  ]) {
    const res = await run(api, request('/v1/video-summary', Buffer.from(JSON.stringify({ ...JSON.parse(video), ...flags }))));
    assert.equal(res.status, 400);
    assert.equal(res.body.error, 'invalid_video');
    assert.equal(api.isBusy(), false);
  }
  assert.equal(calls.length, 0);
});

test('invalid v2 evidence returns 502 without retry, fallback, or partial observations', async () => {
  const valid = JSON.parse(highlightsOutput), group = valid.highlights[0];
  for (const output of [memoryOutput, ...[
    [{ ...group, frame_indices: [1] }], [{ ...group, frame_indices: [3] }],
    [{ ...group, frame_indices: [0, 0] }], [{ ...group, frame_indices: [2, 0] }],
    [group, { ...group, frame_indices: [2] }],
    [{ ...group, timestamp_seconds: 99 }], [{ ...group, text: 'x'.repeat(301) }],
  ].map(highlights => JSON.stringify({ ...valid, highlights }))]) {
    let count = 0;
    const { api } = setup({ fetchImpl: async () => { count += 1; return completed(output); } });
    const res = await run(api, request('/v1/video-summary', highlightsVideo));
    assert.equal(res.status, 502);
    assert.equal(res.body.error, 'invalid_memory_response');
    assert.equal(res.body.caption, undefined);
    assert.equal(res.body.observations, undefined);
    assert.equal(res.body.highlights, undefined);
    assert.equal(count, 1);
    assert.equal(api.isBusy(), false);
  }
});

test('v2 accepts empty highlights and maximum detailed output without additional model calls', async () => {
  const frames = Array.from({ length: 15 }, (_, index) => ({ timestamp_seconds: index * 2, jpeg_base64: jpeg.toString('base64') }));
  const body = Buffer.from(JSON.stringify({ duration_seconds: 30, frames, include_memory: true, include_highlights: true }));
  const observations = frames.map((_, frame_index) => ({ frame_index, text: 't'.repeat(300) }));
  const highlights = Array.from({ length: 5 }, (_, index) => ({ text: 'h'.repeat(300), frame_indices: [index * 3, index * 3 + 1, index * 3 + 2] }));
  for (const output of [
    { summary: 's'.repeat(1500), observations, highlights },
    { summary: 'Visible details.', observations, highlights: [] },
    { summary: 'No useful visible evidence.', observations: [], highlights: [] },
  ]) {
    let count = 0;
    const { api } = setup({ fetchImpl: async () => { count += 1; return completed(JSON.stringify(output)); } });
    const res = await run(api, request('/v1/video-summary', body));
    assert.equal(res.status, 200);
    assert.equal(res.body.observations.length, output.observations.length);
    assert.equal(res.body.highlights.length, output.highlights.length);
    assert.equal(res.body.prompt_version, 'recent-memory-v2-5m');
    if (res.body.highlights.length) assert.equal(res.body.highlights[4].timestamp_seconds, 24);
    assert.equal(count, 1);
  }
});

test('five-minute plain and memory routes submit 150 ordered frames once and preserve complete long-video evidence', async () => {
  const frames = Array.from({ length: 150 }, (_, index) => ({ timestamp_seconds: index * 2, jpeg_base64: jpeg.toString('base64') }));
  const observations = frames.map((_, frame_index) => ({ frame_index, text: `Visible detail ${frame_index}.` }));
  const highlights = Array.from({ length: 5 }, (_, index) => ({ text: `Visible segment ${index}.`,
    frame_indices: Array.from({ length: 30 }, (_, offset) => index * 30 + offset),
  }));
  for (const mode of ['plain', 'memory', 'highlights']) {
    const input = { duration_seconds: 300, frames, include_memory: mode !== 'plain', include_highlights: mode === 'highlights' };
    const output = mode === 'plain' ? 'Visible details over five minutes.' : JSON.stringify({
      summary: 'Visible details over five minutes.', observations, ...(mode === 'highlights' ? { highlights } : {}),
    });
    const calls = [];
    const { api } = setup({ fetchImpl: async (url, options) => { calls.push({ url, options }); return completed(output); } });
    const res = await run(api, request('/v1/video-summary', Buffer.from(JSON.stringify(input))));
    assert.equal(res.status, 200, mode);
    assert.equal(res.body.frame_count, 150);
    assert.equal(calls.length, 1);
    const payload = JSON.parse(calls[0].options.body);
    const content = payload.input[0].content;
    assert.equal(content.filter(item => item.type === 'input_image').length, 150);
    assert.ok(content[299].text.includes('298'));
    if (mode === 'plain') {
      assert.deepEqual(Object.keys(res.body).sort(), ['caption', 'frame_count', 'request_id']);
    } else {
      assert.equal(res.body.observations.length, 150);
      assert.deepEqual(res.body.observations[149], { frame_index: 149, timestamp_seconds: 298, text: 'Visible detail 149.' });
      assert.equal(res.body.model, payload.model);
      assert.equal(res.body.prompt_version, mode === 'highlights' ? 'recent-memory-v2-5m' : 'recent-memory-v1-5m');
      assert.ok(payload.instructions.includes('at most 150 observations'));
      if (mode === 'highlights') {
        assert.equal(res.body.highlights.length, 5);
        assert.equal(res.body.highlights[4].timestamp_seconds, 240);
        assert.equal(res.body.highlights[4].frame_indices.at(-1), 149);
      } else assert.equal(res.body.highlights, undefined);
    }
    assert.equal(api.isBusy(), false);
  }
});

test('long-video route rejects duration, count, and timestamp violations before inference', async () => {
  const frames = Array.from({ length: 150 }, (_, index) => ({ timestamp_seconds: index * 2, jpeg_base64: jpeg.toString('base64') }));
  const { api, calls } = setup();
  for (const value of [
    { duration_seconds: 301.01, frames },
    { duration_seconds: 300, frames: [...frames, { ...frames[0], timestamp_seconds: 300 }] },
    { duration_seconds: 300, frames: [...frames.slice(0, 149), { ...frames[149], timestamp_seconds: 300.01 }] },
  ]) {
    const res = await run(api, request('/v1/video-summary', Buffer.from(JSON.stringify(value))));
    assert.equal(res.status, 400);
    assert.equal(res.body.error, 'invalid_video');
    assert.equal(api.isBusy(), false);
  }
  assert.equal(calls.length, 0);
});

test('production video timeout is 300 seconds, photos remain 60 seconds, and timeoutMs overrides both', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  for (const scenario of [
    { path: '/v1/caption', body: jpeg, expected: 60000 },
    { path: '/v1/video-summary', body: highlightsVideo, expected: 300000 },
    { path: '/v1/caption', body: jpeg, expected: 7, timeoutMs: 7 },
    { path: '/v1/video-summary', body: highlightsVideo, expected: 7, timeoutMs: 7 },
  ]) {
    const entered = Promise.withResolvers(); let signal;
    const { api } = setup({ timeoutMs: scenario.timeoutMs, fetchImpl: async (_, options) => {
      signal = options.signal; entered.resolve();
      await new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
    } });
    const pending = run(api, request(scenario.path, scenario.body));
    await entered.promise;
    if (scenario.expected === 300000) {
      t.mock.timers.tick(60000);
      assert.equal(signal.aborted, false);
      t.mock.timers.tick(239999);
    } else t.mock.timers.tick(scenario.expected - 1);
    assert.equal(signal.aborted, false);
    assert.equal(api.isBusy(), true);
    t.mock.timers.tick(1);
    const res = await pending;
    assert.equal(signal.aborted, true);
    assert.equal(res.status, 504);
    assert.equal(res.body.error, 'timeout');
    assert.equal(api.isBusy(), false);
  }
});

test('memory returns summary, real timestamps, and the original payload model despite a model switch', async () => {
  let model = 'model-before-refresh', modelReads = 0;
  const tokenStarted = Promise.withResolvers(), token = Promise.withResolvers(), calls = [];
  const { api } = setup({ getModel: () => { modelReads += 1; return model; },
    getAccessToken: () => { tokenStarted.resolve(); return token.promise; },
    fetchImpl: async (url, options) => { calls.push({ url, options }); model = 'model-after-inference'; return completed(memoryOutput); },
  });
  const pending = run(api, request('/v1/video-summary', memoryVideo));
  await tokenStarted.promise;
  model = 'model-during-refresh';
  token.resolve('mock-token');
  const res = await pending;
  assert.equal(res.status, 200);
  assert.match(res.body.request_id, /^[a-f0-9-]{36}$/);
  assert.deepEqual({ ...res.body, request_id: undefined }, {
    caption: 'A laptop and a desk are visible.', request_id: undefined, frame_count: 3,
    model: 'model-before-refresh', prompt_version: 'recent-memory-v1-5m', observations: [
      { frame_index: 0, timestamp_seconds: 0, text: 'A laptop is open on a desk.' },
      { frame_index: 2, timestamp_seconds: 29, text: 'A red cup stands on the desk.' },
    ],
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${RESOURCE}/responses`);
  assert.equal(calls[0].options.headers.Authorization, 'Bearer mock-token');
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(res.body.model, payload.model);
  assert.equal(modelReads, 1);
  const content = payload.input[0].content;
  [0, 15, 29].forEach((timestamp, index) => {
    assert.ok(content[1 + index * 2].text.includes(`frame_index: ${index}; timestamp_seconds: ${timestamp}.`));
    assert.equal(content[2 + index * 2].type, 'input_image');
  });
  assert.equal(api.isBusy(), false);
});

test('memory permits a valid summary with no useful observations', async () => {
  const { api } = setup({ fetchImpl: async () => completed('{"summary":"The frames are blurred.","observations":[]}') });
  const res = await run(api, request('/v1/video-summary', memoryVideo));
  assert.equal(res.status, 200);
  assert.equal(res.body.caption, 'The frames are blurred.');
  assert.deepEqual(res.body.observations, []);
  assert.equal(res.body.prompt_version, 'recent-memory-v1-5m');
});

test('invalid structured memory returns a clear 502 with no fallback or retry and releases the lock', async () => {
  const observation = { frame_index: 0, text: 'A desk.' };
  const invalidOutputs = ['not JSON', ...[
    { summary: '', observations: [] },
    { summary: 's'.repeat(1501), observations: [] },
    { summary: 'A desk.', observations: [{ ...observation, frame_index: 3 }] },
    { summary: 'A desk.', observations: [observation, observation] },
    { summary: 'A desk.', observations: [{ ...observation, frame_index: 1 }, observation] },
    { summary: 'A desk.', observations: [{ ...observation, text: 't'.repeat(301) }] },
    { summary: 'A desk.', observations: [{ ...observation, timestamp_seconds: 100 }] },
  ].map(JSON.stringify)];
  for (const output of invalidOutputs) {
    let count = 0;
    const { api } = setup({ fetchImpl: async () => { count += 1; return completed(output); } });
    const res = await run(api, request('/v1/video-summary', memoryVideo));
    assert.equal(res.status, 502, output);
    assert.equal(res.body.error, 'invalid_memory_response');
    assert.match(res.body.message, /invalid structured memory/);
    assert.equal(res.body.caption, undefined);
    assert.equal(res.body.observations, undefined);
    assert.equal(count, 1);
    assert.equal(api.isBusy(), false);
  }
});

test('memory mode shares the photo/video single-flight lock', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  let count = 0;
  const { api } = setup({ fetchImpl: async () => { count += 1; entered.resolve(); await release.promise; return completed(memoryOutput); } });
  const pending = run(api, request('/v1/video-summary', memoryVideo));
  await entered.promise;
  assert.equal((await run(api, request('/v1/caption', jpeg))).status, 429);
  assert.equal((await run(api)).status, 429);
  assert.equal((await run(api, request('/v1/video-summary', memoryVideo))).status, 429);
  release.resolve();
  assert.equal((await pending).status, 200);
  assert.equal(count, 1);
  assert.equal(api.isBusy(), false);
});

test('memory cancellation and timeout abort the same upstream request and release the lock', async () => {
  for (const body of [memoryVideo, highlightsVideo]) for (const disconnect of [false, true]) {
    const entered = Promise.withResolvers();
    let signal;
    const { api } = setup({ timeoutMs: disconnect ? 60000 : 10, fetchImpl: async (_, options) => {
      signal = options.signal; entered.resolve();
      await new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
    } });
    const res = new ResponseStub();
    const pending = api.handler(request('/v1/video-summary', body), res);
    await entered.promise;
    if (disconnect) res.disconnect();
    await pending;
    assert.equal(signal.aborted, true);
    if (disconnect) assert.equal(res.body, undefined);
    else { assert.equal(res.status, 504); assert.equal(res.body.error, 'timeout'); }
    assert.equal(api.isBusy(), false);
  }
});

test('pairing, origin, readiness, content type, and JSON limits are enforced before inference', async () => {
  const { api, calls } = setup();
  for (const path of ['/v1/video-summary', '/v1/caption']) {
    for (const headers of [{ authorization: 'Bearer wrong' }, { origin: 'https://example.invalid' }]) {
      assert.equal((await run(api, request(path, path.endsWith('caption') ? jpeg : video, headers))).status, 401);
    }
  }
  assert.equal((await run(api, request('/v1/video-summary', video, { 'content-type': 'image/jpeg' }))).status, 415);
  assert.equal((await run(api, request('/v1/video-summary', Buffer.from('{bad')))).status, 400);
  assert.equal((await run(api, request('/v1/video-summary', video, { 'content-length': String(MAX_VIDEO_BODY_BYTES + 1) }))).status, 413);
  assert.equal((await run(api, request('/v1/video-summary', Buffer.alloc(MAX_VIDEO_BODY_BYTES + 1)))).status, 413);
  assert.equal((await run(api, request('/v1/caption', Buffer.from('bad')))).status, 400);
  assert.equal(calls.length, 0);
  assert.equal(api.isBusy(), false);
  const unavailable = setup({ getStatus: () => ({ ready: false, message: 'Sign in first.' }) });
  assert.equal((await run(unavailable.api)).status, 503);
  assert.equal(unavailable.calls.length, 0);
  assert.equal((await run(setup({ isAuthActive: () => true }).api)).status, 429);
});

test('a video and a photo cannot run concurrently, and the lock releases after completion', async () => {
  let unblock;
  const entered = Promise.withResolvers();
  const { api } = setup({ fetchImpl: async () => { entered.resolve(); await new Promise(resolve => { unblock = resolve; }); return completed(); } });
  const first = run(api);
  await entered.promise;
  assert.equal(api.isBusy(), true);
  assert.equal((await run(api, request('/v1/caption', jpeg))).status, 429);
  assert.equal((await run(api)).status, 429);
  unblock();
  assert.equal((await first).status, 200);
  assert.equal(api.isBusy(), false);
});

test('client disconnect aborts the upstream request and releases single-flight state', async () => {
  const entered = Promise.withResolvers(); let upstreamSignal;
  const { api } = setup({ fetchImpl: async (_, { signal }) => {
    upstreamSignal = signal; entered.resolve();
    await new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
  } });
  const res = new ResponseStub();
  const pending = api.handler(request(), res);
  await entered.promise;
  res.disconnect();
  await pending;
  assert.equal(upstreamSignal.aborted, true);
  assert.equal(res.body, undefined);
  assert.equal(api.isBusy(), false);
});

test('timeout aborts inference, returns an actionable error, and releases the lock', async () => {
  const { api } = setup({ timeoutMs: 10, fetchImpl: async (_, { signal }) => {
    await new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
  } });
  const res = await run(api);
  assert.equal(res.status, 504);
  assert.equal(res.body.error, 'timeout');
  assert.equal(api.isBusy(), false);
});

test('timeout during a shared token refresh prevents any later inference request', async () => {
  const token = Promise.withResolvers();
  const { api, calls } = setup({ timeoutMs: 10, getAccessToken: () => token.promise });
  const res = await run(api);
  assert.equal(res.status, 504);
  assert.equal(api.isBusy(), false);
  token.resolve('late-token');
  await Promise.resolve();
  assert.equal(calls.length, 0);
});

test('upstream failure or incomplete stream never returns a successful video summary', async () => {
  for (const upstream of [
    { ok: false, status: 429, json: async () => ({ error: { code: 'usage_limit' } }) },
    { ok: true, body: Readable.from([Buffer.from('data: {"type":"response.output_text.delta","delta":"partial"}\n\n')]) },
  ]) {
    const { api } = setup({ fetchImpl: async () => upstream });
    const res = await run(api);
    assert.notEqual(res.status, 200);
    assert.equal(res.body.caption, undefined);
    assert.equal(api.isBusy(), false);
  }
});
