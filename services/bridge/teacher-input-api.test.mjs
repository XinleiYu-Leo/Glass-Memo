import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';
import { RESOURCE } from './core.mjs';
import { createMediaAPI } from './media-api.mjs';
import { INPUT_DIAGNOSTIC_PROMPT_VERSION, MAX_INPUT_DIAGNOSTIC_BODY_BYTES } from './teacher-input-diagnostic.mjs';

const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAOAAAADgCAIAAACVT/22AAAAqUlEQVR4nO3BMQEAAADCoPVPbQlPoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAuhpM/gABLXYdWgAAAABJRU5ErkJggg==';
const input = { expected_model: 'test-model', job_id: 'case-1', target_interval_sec: [10, 11.5], decision_time_sec: 11.5,
  frames: Array.from({ length: 4 }, (_, i) => ({ frame_id: `f${i}`, time_sec: 10 + i / 2, png_base64: PNG })) };
const decision = { job_id: 'case-1', decision: 'present', action: 'place', object: '小杯',
  evidence_frame_ids: ['f0', 'f3'], context_frame_ids: [], reason: '先手持，后松开留在台面。' };
const output = JSON.stringify(decision);
function request(value = input, path = '/v1/teacher-input-diagnostic', headers = {}) {
  const req = Readable.from([Buffer.isBuffer(value) ? value : Buffer.from(JSON.stringify(value))]);
  Object.assign(req, { method: 'POST', url: path,
    headers: { authorization: 'Bearer test-pairing', 'content-type': 'application/json', ...headers } });
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

test('diagnostic route uses existing authenticated transport and returns bounded decision plus provenance', async () => {
  const current = setup(), res = await run(current.api);
  assert.equal(res.status, 200);
  assert.deepEqual({ ...res.body, request_id: undefined, model: undefined, prompt_version: undefined, raw_output: undefined },
    { ...decision, request_id: undefined, model: undefined, prompt_version: undefined, raw_output: undefined });
  assert.equal(res.body.prompt_version, INPUT_DIAGNOSTIC_PROMPT_VERSION); assert.equal(res.body.model, 'test-model');
  assert.equal(res.body.raw_output, output); assert.equal(current.calls.length, 1);
  assert.equal(current.calls[0].url, `${RESOURCE}/responses`);
  assert.equal(current.calls[0].options.headers.Authorization, 'Bearer fake-token');
  assert.equal(current.calls[0].options.redirect, 'error');
  const payload = JSON.parse(current.calls[0].options.body);
  assert.equal(payload.store, false); assert.equal(payload.stream, true);
  assert.equal(payload.previous_response_id, undefined); assert.equal(current.api.isBusy(), false);
});

test('diagnostic auth, reference leakage, future input and model mismatch fail before token or network', async () => {
  const current = setup();
  for (const headers of [{ authorization: 'Bearer wrong' }, { origin: 'https://example.invalid' }])
    assert.equal((await run(current.api, request(input, undefined, headers))).status, 401);
  assert.equal((await run(current.api, request(input, undefined, { 'content-type': 'image/png' }))).status, 415);
  assert.equal((await run(current.api, request(input, undefined, { 'content-length': String(MAX_INPUT_DIAGNOSTIC_BODY_BYTES + 1) }))).status, 413);
  assert.equal((await run(current.api, request({ ...input, answer: 'present' }))).status, 400);
  assert.equal((await run(current.api, request({ ...input, expected_model: 'changed' }))).status, 409);
  assert.equal(current.calls.length, 0); assert.equal(current.tokenCalls(), 0); assert.equal(current.api.isBusy(), false);
  assert.equal((await run(setup({ getStatus: () => ({ ready: false, message: 'Connect first.' }) }).api)).status, 503);
  assert.equal((await run(setup({ isAuthActive: () => true }).api)).status, 429);
});

test('diagnostic invalid evidence or incomplete response is never accepted or retried', async () => {
  for (const upstream of [completed('not JSON'), completed(JSON.stringify({ ...decision, evidence_frame_ids: ['f0', 'unknown'] })),
    { ok: true, body: Readable.from([Buffer.from(`data: ${JSON.stringify({ type: 'response.output_text.delta', delta: output })}\n\n`)]) },
    { ok: false, status: 429, json: async () => ({ error: { code: 'usage_limit' } }) }]) {
    let calls = 0; const current = setup({ fetchImpl: async () => { calls += 1; return upstream; } });
    const res = await run(current.api); assert.notEqual(res.status, 200);
    assert.equal(res.body.decision, undefined); assert.equal(res.body.raw_output, undefined);
    assert.equal(calls, 1); assert.equal(current.api.isBusy(), false);
  }
});

test('diagnostic shares lock with legacy teacher and other routes', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const current = setup({ fetchImpl: async () => { entered.resolve(); await release.promise; return completed(); } });
  const pending = run(current.api); await entered.promise;
  for (const path of ['/v1/teacher-input-diagnostic', '/v1/teacher-labels', '/v1/teacher-presence', '/v1/caption', '/v1/video-summary'])
    assert.equal((await run(current.api, request(input, path))).status, 429);
  release.resolve(); assert.equal((await pending).status, 200); assert.equal(current.api.isBusy(), false);
});

test('disconnect during token refresh prevents late inference and releases lock', async () => {
  const entered = Promise.withResolvers(), token = Promise.withResolvers();
  const current = setup({ getAccessToken: () => { entered.resolve(); return token.promise; } });
  const res = new ResponseStub(), pending = current.api.handler(request(), res);
  await entered.promise; res.disconnect(); await pending; token.resolve('late-fake-token'); await Promise.resolve();
  assert.equal(current.calls.length, 0); assert.equal(current.api.isBusy(), false); assert.equal(res.body, undefined);
});

test('diagnostic pins model before refresh and cannot switch model during the call', async () => {
  let model = 'test-model';
  const entered = Promise.withResolvers(), token = Promise.withResolvers();
  const current = setup({ getModel: () => model, getAccessToken: () => { entered.resolve(); return token.promise; } });
  const pending = run(current.api); await entered.promise; model = 'new-model'; token.resolve('fake-token');
  const res = await pending;
  assert.equal(res.status, 200); assert.equal(res.body.model, 'test-model');
  assert.equal(JSON.parse(current.calls[0].options.body).model, 'test-model');
});

test('diagnostic retains media timeout and disconnect behavior without retry', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  for (const disconnect of [false, true]) {
    const entered = Promise.withResolvers(); let signal;
    const current = setup({ fetchImpl: async (_, options) => { signal = options.signal; entered.resolve();
      await new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true })); } });
    const res = new ResponseStub(), pending = current.api.handler(request(), res); await entered.promise;
    t.mock.timers.tick(299999); assert.equal(signal.aborted, false);
    if (disconnect) res.disconnect(); else t.mock.timers.tick(1);
    await pending; assert.equal(current.api.isBusy(), false); assert.equal(signal.aborted, true);
    if (disconnect) assert.equal(res.body, undefined); else assert.equal(res.status, 504);
  }
});
