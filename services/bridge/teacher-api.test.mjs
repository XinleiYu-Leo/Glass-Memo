import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';
import { RESOURCE } from './core.mjs';
import { MAX_TEACHER_BODY_BYTES } from './teacher-labels.mjs';
import { createMediaAPI } from './media-api.mjs';

const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0xff, 0xd9]);
const input = { expected_model: 'test-model', segment_id: 'seg-1',
  frames: Array.from({ length: 4 }, (_, i) => ({ frame_id: `f-${i}`, time_sec: 10 + i / 2, jpeg_base64: jpeg.toString('base64') })),
  windows: [{ sample_id: 'clip:23', frame_ids: ['f-0', 'f-1', 'f-2', 'f-3'] }] };
const label = { sample_id: 'clip:23', label: 1, action: 'pickup', object: 'cup', evidence_frame_ids: ['f-0', 'f-3'], reason: 'Visible support transfer from table to hand.' };
const output = JSON.stringify({ segment_id: 'seg-1', windows: [label] });
function request(value = input, path = '/v1/teacher-labels', headers = {}) {
  const req = Readable.from([Buffer.isBuffer(value) ? value : Buffer.from(JSON.stringify(value))]);
  Object.assign(req, { method: 'POST', url: path, headers: { authorization: 'Bearer test-pairing', 'content-type': 'application/json', ...headers } });
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
  const api = createMediaAPI({ getPairingCode: () => 'test-pairing', getStatus: () => ({ ready: true }), getModel: () => 'test-model',
    getAccessToken: async () => { tokenCalls += 1; return 'fake-token'; }, isAuthActive: () => false,
    fetchImpl: async (url, options) => { calls.push({ url, options }); return completed(); }, ...overrides });
  return { api, calls, tokenCalls: () => tokenCalls };
}
async function run(api, req = request()) { const res = new ResponseStub(); await api.handler(req, res); return res; }

test('teacher API makes one public Responses call and returns validated labels with audit provenance', async () => {
  const { api, calls } = setup(); const res = await run(api);
  assert.equal(res.status, 200); assert.deepEqual(res.body.windows, [label]);
  assert.deepEqual(Object.keys(res.body).sort(), ['model', 'prompt_version', 'raw_output', 'request_id', 'segment_id', 'windows']);
  assert.equal(res.body.model, 'test-model'); assert.equal(res.body.prompt_version, 'pick-place-evidence-v2');
  assert.equal(res.body.raw_output, output); assert.match(res.body.request_id, /^[a-f0-9-]{36}$/);
  assert.equal(calls.length, 1); assert.equal(calls[0].url, `${RESOURCE}/responses`);
  assert.equal(calls[0].options.redirect, 'error'); assert.equal(calls[0].options.headers.Authorization, 'Bearer fake-token');
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.model, 'test-model'); assert.equal(payload.store, false); assert.equal(payload.stream, true);
  assert.equal(api.isBusy(), false);
});

test('teacher route rejects unauthenticated, oversized, unready or malformed inputs before token or inference', async () => {
  const configured = setup();
  for (const headers of [{ authorization: 'Bearer wrong' }, { origin: 'https://example.invalid' }])
    assert.equal((await run(configured.api, request(input, undefined, headers))).status, 401);
  assert.equal((await run(configured.api, request(input, undefined, { 'content-type': 'image/jpeg' }))).status, 415);
  assert.equal((await run(configured.api, request(input, undefined, { 'content-length': String(MAX_TEACHER_BODY_BYTES + 1) }))).status, 413);
  assert.equal((await run(configured.api, request(Buffer.alloc(MAX_TEACHER_BODY_BYTES + 1)))).status, 413);
  assert.equal((await run(configured.api, request({ ...input, source_label: 1 }))).status, 400);
  assert.equal((await run(configured.api, request({ ...input, expected_model: 'changed-model' }))).status, 409);
  assert.equal(configured.calls.length, 0); assert.equal(configured.tokenCalls(), 0); assert.equal(configured.api.isBusy(), false);
  const offline = setup({ getStatus: () => ({ ready: false, message: 'Connect first.' }) });
  assert.equal((await run(offline.api)).status, 503); assert.equal(offline.tokenCalls(), 0);
  assert.equal((await run(setup({ isAuthActive: () => true }).api)).status, 429);
});

test('teacher snapshots selected model before token refresh', async () => {
  let selected = 'test-model', reads = 0;
  const entered = Promise.withResolvers(), token = Promise.withResolvers();
  const current = setup({ getModel: () => { reads += 1; return selected; }, getAccessToken: () => { entered.resolve(); return token.promise; } });
  const pending = run(current.api); await entered.promise; selected = 'new-model'; token.resolve('fake-token');
  const res = await pending;
  assert.equal(res.status, 200); assert.equal(res.body.model, 'test-model'); assert.equal(reads, 1);
});

test('teacher rejects incomplete streams and malformed labels with no retry or partial success', async () => {
  for (const upstream of [
    completed('not JSON'), completed(JSON.stringify({ segment_id: 'seg-1', windows: [{ ...label, evidence_frame_ids: ['unknown', 'f-3'] }] })),
    { ok: true, body: Readable.from([Buffer.from(`data: ${JSON.stringify({ type: 'response.output_text.delta', delta: output })}\n\n`)]) },
    { ok: true, body: Readable.from([Buffer.from('data: {"type":"response.incomplete","response":{"status":"incomplete"}}\n\n')]) },
    { ok: false, status: 429, json: async () => ({ error: { code: 'usage_limit' } }) },
  ]) {
    let count = 0; const { api } = setup({ fetchImpl: async () => { count += 1; return upstream; } });
    const res = await run(api);
    assert.notEqual(res.status, 200); assert.equal(res.body.windows, undefined); assert.equal(res.body.raw_output, undefined);
    assert.equal(count, 1); assert.equal(api.isBusy(), false);
  }
});

test('teacher shares the established single-flight lock in both directions', async () => {
  for (const firstPath of ['/v1/teacher-labels', '/v1/caption']) {
    const entered = Promise.withResolvers(), release = Promise.withResolvers();
    const { api } = setup({ fetchImpl: async () => { entered.resolve(); await release.promise;
      return completed(firstPath === '/v1/teacher-labels' ? output : 'Visible cup.'); } });
    const pending = run(api, request(firstPath === '/v1/caption' ? jpeg : input, firstPath,
      { 'content-type': firstPath === '/v1/caption' ? 'image/jpeg' : 'application/json' }));
    await entered.promise;
    for (const path of ['/v1/teacher-labels', '/v1/caption', '/v1/video-summary', '/v1/object-observations'])
      assert.equal((await run(api, request(input, path))).status, 429);
    release.resolve(); assert.equal((await pending).status, 200); assert.equal(api.isBusy(), false);
  }
});

test('teacher timeout or caller disconnect releases lock without accepting output', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  for (const disconnect of [false, true]) {
    const entered = Promise.withResolvers(); let signal;
    const { api } = setup({ fetchImpl: async (_, options) => { signal = options.signal; entered.resolve();
      await new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true })); } });
    const res = new ResponseStub(), pending = api.handler(request(), res); await entered.promise;
    t.mock.timers.tick(299999); assert.equal(signal.aborted, false);
    if (disconnect) res.disconnect(); else t.mock.timers.tick(1);
    await pending; assert.equal(api.isBusy(), false); assert.equal(signal.aborted, true);
    if (disconnect) assert.equal(res.body, undefined); else assert.equal(res.status, 504);
  }
});

test('teacher cancellation during refresh cannot start later inference', async () => {
  const entered = Promise.withResolvers(), token = Promise.withResolvers();
  const current = setup({ getAccessToken: () => { entered.resolve(); return token.promise; } });
  const res = new ResponseStub(), pending = current.api.handler(request(), res);
  await entered.promise; res.disconnect(); await pending; token.resolve('late-fake-token'); await Promise.resolve();
  assert.equal(current.api.isBusy(), false); assert.equal(current.calls.length, 0); assert.equal(res.body, undefined);
});
