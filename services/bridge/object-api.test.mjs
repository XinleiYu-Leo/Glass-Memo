import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';
import { MAX_VIDEO_BODY_BYTES, RESOURCE } from './core.mjs';
import { createMediaAPI } from './media-api.mjs';

const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0xff, 0xd9]);
const input = { duration_seconds: 30, frames: [0, 15, 29].map((timestamp_seconds, i) => ({
  frame_id: `f-${i}`, timestamp_seconds, jpeg_base64: jpeg.toString('base64'),
})) };
const observation = { object_id: 'keys', frame_id: 'f-1', state: 'held', location_zh: '手中', observation_zh: '手正握着钥匙。',
  visibility: 'clear', identity: 'match', hand_relation: 'held', needs_review: false };
const output = JSON.stringify({ observations: [observation], refine_frame_ids: ['f-1'] });
function request(value = input, path = '/v1/object-observations', headers = {}) {
  const req = Readable.from([Buffer.isBuffer(value) ? value : Buffer.from(JSON.stringify(value))]);
  Object.assign(req, { method: 'POST', url: path, headers: {
    authorization: 'Bearer test-pairing', 'content-type': path === '/v1/caption' ? 'image/jpeg' : 'application/json', ...headers,
  } });
  return req;
}
class ResponseStub extends EventEmitter {
  destroyed = false; writableEnded = false;
  writeHead(status, headers) { this.status = status; this.headers = headers; }
  end(body) { this.body = JSON.parse(body); this.writableEnded = true; }
  disconnect() { this.destroyed = true; this.emit('close'); }
}
const completed = (text = output) => ({ ok: true, body: Readable.from([Buffer.from(`data: ${JSON.stringify({
  type: 'response.completed', response: { status: 'completed', output: [{ content: [{ type: 'output_text', text }] }] },
})}\n\n`)]) });
function setup(overrides = {}) {
  const calls = []; let tokenCalls = 0;
  const api = createMediaAPI({ getPairingCode: () => 'test-pairing', getStatus: () => ({ ready: true }),
    getModel: () => 'test-model', getAccessToken: async () => { tokenCalls += 1; return 'fake-token'; }, isAuthActive: () => false,
    fetchImpl: async (url, options) => { calls.push({ url, options }); return completed(); }, ...overrides });
  return { api, calls, tokenCalls: () => tokenCalls };
}
async function run(api, req = request()) { const res = new ResponseStub(); await api.handler(req, res); return res; }

test('paired status advertises the object prompt version without token access or inference', async () => {
  const configured = setup({ getStatus: () => ({ ready: true, model: 'test-model', message: 'Connected.' }) });
  const req = request(Buffer.alloc(0), '/v1/status'); req.method = 'GET';
  const res = await run(configured.api, req);
  assert.equal(res.status, 200);
  assert.deepEqual(res.body, { ready: true, model: 'test-model', message: 'Connected.', object_prompt_version: 'object-observations-v1' });
  assert.equal(configured.tokenCalls(), 0); assert.equal(configured.calls.length, 0);
});

test('object route uses the existing authorized upstream once and returns only validated observations plus audit output', async () => {
  const { api, calls } = setup();
  const res = await run(api);
  assert.equal(res.status, 200);
  assert.match(res.body.request_id, /^[a-f0-9-]{36}$/);
  assert.deepEqual(Object.keys(res.body).sort(), ['model', 'observations', 'prompt_version', 'raw_output', 'refine_frame_ids', 'request_id']);
  assert.equal(res.body.model, 'test-model');
  assert.equal(res.body.prompt_version, 'object-observations-v1');
  assert.deepEqual(res.body.observations, [observation]);
  assert.deepEqual(res.body.refine_frame_ids, ['f-1']);
  assert.equal(res.body.raw_output, output);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${RESOURCE}/responses`);
  assert.equal(calls[0].options.headers.Authorization, 'Bearer fake-token');
  assert.equal(calls[0].options.redirect, 'error');
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.model, res.body.model);
  assert.equal(payload.input[0].content.filter(item => item.type === 'input_image').length, 3);
  assert.equal(api.isBusy(), false);
});

test('expected model mismatch fails with 409 before token refresh or inference and model is snapshotted once', async () => {
  const mismatch = setup();
  const rejected = await run(mismatch.api, request({ ...input, expected_model: 'other-model' }));
  assert.equal(rejected.status, 409); assert.equal(rejected.body.error, 'model_changed');
  assert.equal(mismatch.tokenCalls(), 0); assert.equal(mismatch.calls.length, 0); assert.equal(mismatch.api.isBusy(), false);
  let model = 'first-model', reads = 0;
  const token = Promise.withResolvers(), entered = Promise.withResolvers();
  const current = setup({ getModel: () => { reads += 1; return model; }, getAccessToken: () => { entered.resolve(); return token.promise; } });
  const pending = run(current.api, request({ ...input, expected_model: model }));
  await entered.promise; model = 'changed-model'; token.resolve('fake-token');
  const res = await pending;
  assert.equal(res.status, 200); assert.equal(res.body.model, 'first-model'); assert.equal(reads, 1);
  assert.equal(JSON.parse(current.calls[0].options.body).model, 'first-model');
});

test('object route enforces pairing, origin, content, frame IDs, time order, body bounds and offline readiness before inference', async () => {
  const configured = setup();
  for (const headers of [{ authorization: 'Bearer wrong' }, { origin: 'https://example.invalid' }])
    assert.equal((await run(configured.api, request(input, undefined, headers))).status, 401);
  assert.equal((await run(configured.api, request(input, undefined, { 'content-type': 'image/jpeg' }))).status, 415);
  assert.equal((await run(configured.api, request(input, undefined, { 'content-length': String(MAX_VIDEO_BODY_BYTES + 1) }))).status, 413);
  assert.equal((await run(configured.api, request(Buffer.alloc(MAX_VIDEO_BODY_BYTES + 1)))).status, 413);
  for (const body of [Buffer.from('{bad'), { ...input, frames: [{ ...input.frames[0], frame_id: '../bad' }] },
    { ...input, frames: [input.frames[0], input.frames[0]] },
    { ...input, frames: [input.frames[1], input.frames[0]] }, { ...input, expected_model: null }]) {
    const res = await run(configured.api, request(body));
    assert.equal(res.status, 400); assert.equal(res.body.error, 'invalid_object_request');
  }
  assert.equal(configured.calls.length, 0); assert.equal(configured.tokenCalls(), 0); assert.equal(configured.api.isBusy(), false);
  const offline = setup({ getStatus: () => ({ ready: false, message: 'Connect ChatGPT first.' }) });
  assert.equal((await run(offline.api)).status, 503); assert.equal(offline.calls.length, 0); assert.equal(offline.tokenCalls(), 0);
  assert.equal((await run(setup({ isAuthActive: () => true }).api)).status, 429);
});

test('object route rejects invalid model evidence atomically with no retry, partial answer or fallback', async () => {
  for (const value of [
    'not JSON', JSON.stringify({ observations: [observation], refine_frame_ids: ['not-sent'] }),
    JSON.stringify({ observations: [{ ...observation, frame_id: 'unknown' }], refine_frame_ids: [] }),
    JSON.stringify({ observations: [{ ...observation, hand_relation: 'touching' }], refine_frame_ids: [] }),
    JSON.stringify({ observations: [observation, observation], refine_frame_ids: [] }),
  ]) {
    let count = 0;
    const { api } = setup({ fetchImpl: async () => { count += 1; return completed(value); } });
    const res = await run(api);
    assert.equal(res.status, 502); assert.equal(res.body.error, 'invalid_object_response');
    assert.equal(res.body.observations, undefined); assert.equal(res.body.raw_output, undefined);
    assert.equal(count, 1); assert.equal(api.isBusy(), false);
  }
});

test('object analysis shares the photo/video lock in both directions and releases it on completion', async () => {
  for (const firstPath of ['/v1/object-observations', '/v1/video-summary', '/v1/caption']) {
    const entered = Promise.withResolvers(), release = Promise.withResolvers(); let count = 0;
    const { api } = setup({ fetchImpl: async () => { count += 1; entered.resolve(); await release.promise;
      return completed(firstPath === '/v1/object-observations' ? output : 'A visible object.'); } });
    const pending = run(api, request(firstPath === '/v1/caption' ? jpeg : input, firstPath));
    await entered.promise;
    for (const path of ['/v1/object-observations', '/v1/video-summary', '/v1/caption'])
      assert.equal((await run(api, request(path === '/v1/caption' ? jpeg : input, path))).status, 429);
    release.resolve(); assert.equal((await pending).status, 200);
    assert.equal(count, 1); assert.equal(api.isBusy(), false);
  }
});

test('object analysis has the production 300 second timeout and shares caller cancellation', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  for (const disconnect of [false, true]) {
    const entered = Promise.withResolvers(); let signal;
    const { api } = setup({ fetchImpl: async (_, options) => {
      signal = options.signal; entered.resolve();
      await new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
    } });
    const res = new ResponseStub(), pending = api.handler(request(), res);
    await entered.promise;
    t.mock.timers.tick(299999); assert.equal(signal.aborted, false); assert.equal(api.isBusy(), true);
    if (disconnect) res.disconnect(); else t.mock.timers.tick(1);
    await pending;
    assert.equal(signal.aborted, true); assert.equal(api.isBusy(), false);
    if (disconnect) assert.equal(res.body, undefined);
    else { assert.equal(res.status, 504); assert.equal(res.body.error, 'timeout'); }
  }
});

test('cancellation during token refresh prevents later object inference and frees the shared lock', async () => {
  const entered = Promise.withResolvers(), token = Promise.withResolvers();
  const configured = setup({ getAccessToken: () => { entered.resolve(); return token.promise; } });
  const res = new ResponseStub(), pending = configured.api.handler(request(), res);
  await entered.promise; res.disconnect(); await pending;
  assert.equal(configured.api.isBusy(), false); assert.equal(res.body, undefined);
  token.resolve('late-fake-token'); await Promise.resolve();
  assert.equal(configured.calls.length, 0);
});

test('object route exposes safe upstream errors without accepting a truncated stream as observations', async () => {
  for (const upstream of [
    { ok: false, status: 429, json: async () => ({ error: { code: 'usage_limit' } }) },
    { ok: true, body: Readable.from([Buffer.from('data: {"type":"response.output_text.delta","delta":"partial"}\n\n')]) },
  ]) {
    const { api } = setup({ fetchImpl: async () => upstream });
    const res = await run(api);
    assert.notEqual(res.status, 200); assert.equal(res.body.observations, undefined); assert.equal(api.isBusy(), false);
  }
});
