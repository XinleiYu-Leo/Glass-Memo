import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import { createAutoPipeline, AUTO_PROMPT_VERSION } from '../auto-pipeline.mjs';
import { search } from '../core.mjs';

const model = 'fixture-vision';
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
const jsonResponse = (data, status = 200) => new Response(JSON.stringify(data), { status });
function observation(frame, { object_id = 'keys', state = 'visible', location_zh = '桌面', ...rest } = {}) {
  return { object_id, frame_id: frame.frame_id, state, location_zh, observation_zh: '该帧可看到目标物品。',
    visibility: 'clear', identity: 'match', hand_relation: state === 'held' ? 'held' : 'free', needs_review: false, ...rest };
}
function response(frames, observations = [], refine_frame_ids = []) {
  return { request_id: 'fixture-request', model, prompt_version: AUTO_PROMPT_VERSION,
    observations, refine_frame_ids, raw_output: JSON.stringify({ observations, refine_frame_ids }) };
}
async function fixture({ duration = 4.2, backend, ...options } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'robomemo-auto-test-'));
  await mkdir(join(directory, 'data'));
  const reference = JSON.stringify({ originalReviewedIndex: true });
  await writeFile(join(directory, 'data/index.json'), reference);
  const source = join(directory, 'fixture.mov'); await writeFile(source, 'local video fixture');
  const calls = [], extraction = [];
  const extractImpl = async ({ output, interval, ranges, signal }) => {
    signal.throwIfAborted(); extraction.push({ interval, ranges });
    await mkdir(join(output, 'images'), { recursive: true });
    const pts = Array.from({ length: Math.floor(duration * 4) + 1 }, (_, i) => ({
      frame_id: `f${String(i + 1).padStart(6, '0')}`, output_pts_seconds: i / 4,
    })).filter(f => f.output_pts_seconds <= duration);
    const selected = pts.filter(f => ranges ? ranges.some(r => f.output_pts_seconds >= r.start && f.output_pts_seconds <= r.end)
      : Math.abs(f.output_pts_seconds / interval - Math.round(f.output_pts_seconds / interval)) < 0.00001);
    const frames = [];
    for (const f of selected) {
      const image_file = `images/${f.frame_id}.jpg`;
      await writeFile(join(output, image_file), JPEG);
      frames.push({ ...f, source_frame_index: Number(f.frame_id.slice(1)), image_file, width: 4, height: 4 });
    }
    return { version: 1, duration_seconds: duration, decoded_frames: pts.length,
      width: 4, height: 4, sampling_version: 'fixture-sampling-v1', frames, all_frame_pts: pts };
  };
  const fetchImpl = async (url, init) => {
    assert.equal(url.hostname, '127.0.0.1');
    assert.equal(init.headers.Authorization, 'Bearer fixture-private-pairing');
    assert.equal(init.redirect, 'error');
    if (url.pathname === '/v1/status') return jsonResponse({ ready: true, model, object_prompt_version: AUTO_PROMPT_VERSION });
    assert.equal(url.pathname, '/v1/object-observations');
    const body = JSON.parse(init.body); calls.push(body);
    assert.equal(body.expected_model, model);
    assert.ok(body.frames.length <= 40);
    assert.deepEqual(Object.keys(body).sort(), ['duration_seconds', 'expected_model', 'frames']);
    return backend ? backend(body, calls.length, init) : jsonResponse(response(body.frames));
  };
  const remuxImpl = async ({ video, output, reportPath, signal }) => {
    signal.throwIfAborted(); await writeFile(output, await readFile(video));
    const report = { version: 1, passed: true, reencoded: false,
      payloads_and_timestamps_identical: true, frames: Math.floor(duration * 4) + 1, duration_seconds: duration };
    await writeFile(reportPath, JSON.stringify(report)); return report;
  };
  const engine = createAutoPipeline({ directory, bridgePairingCode: 'fixture-private-pairing',
    extractImpl, remuxImpl, fetchImpl, ...options });
  const input = { sourcePath: source, startedAt: '2026-01-01T00:00:00Z', label: '自动测试' };
  const run = async (extra = {}) => { const started = engine.start({ ...input, ...extra }); return engine.wait(started.job_id); };
  return { directory, reference, source, input, engine, run, calls, extraction, extractImpl, remuxImpl, fetchImpl };
}
async function readIndex(f) { return JSON.parse(await readFile(join(f.directory, 'data/auto-index.json'), 'utf8')); }
async function assertUnpublished(f) {
  await assert.rejects(readFile(join(f.directory, 'data/auto-index.json')), { code: 'ENOENT' });
  assert.equal(await readFile(join(f.directory, 'data/index.json'), 'utf8'), f.reference);
  try { assert.deepEqual(await readdir(join(f.directory, '.auto-work/imports')), []); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
}

test('automatic results need two distinct real frames; a held object keeps its destination unknown', async () => {
  const f = await fixture({ backend: async body => {
    const frames = body.frames;
    return jsonResponse(response(frames, [observation(frames[0]), observation(frames[1]),
      observation(frames[2], { object_id: 'earbud_case', state: 'held', location_zh: '手中' }),
      observation(frames[3], { object_id: 'earbud_case', state: 'held', location_zh: '手中' }),
      observation(frames[4], { object_id: 'wallet' })]));
  } });
  const status = await f.run(); assert.equal(status.status, 'completed');
  assert.equal(status.result.observation_count, 5); assert.equal(status.result.clear_candidate_count, 4);
  const index = await readIndex(f);
  assert.equal(index.provenance, 'vision_pipeline'); assert.equal(index.clips.length, 1);
  assert.ok(index.observations.every(o => o.provenance === 'vision_pipeline' && o.review_status === 'auto_checked'
    && o.human_ground_truth === null && o.state !== 'placed'));
  const wallet = index.observations.find(o => o.object_id === 'wallet');
  assert.equal(wallet.use_for_last_seen, false); assert.deepEqual(wallet.support_frame_ids, []);
  const answer = search(index, { query: '白色小盒在哪' });
  assert.equal(answer.status, 'found'); assert.match(answer.answers[0].headline, /手中.*尚未确认/u);
  assert.equal(await readFile(join(f.directory, 'data/index.json'), 'utf8'), f.reference);
  assert.ok(!JSON.stringify(status).includes('fixture-private-pairing'));
  assert.ok(!JSON.stringify(status).includes(f.directory));
});

test('same video and model versions resume cached windows and never duplicate index entries', async () => {
  const f = await fixture({ backend: async body => jsonResponse(response(body.frames,
    [observation(body.frames[0]), observation(body.frames[1])])) });
  const first = await f.run(); const second = await f.run();
  assert.equal(first.status, 'completed'); assert.equal(second.status, 'completed');
  assert.equal(second.calls, 0); assert.equal(second.cached_windows, 1); assert.equal(f.calls.length, 1);
  assert.equal(first.run_key, second.run_key);
  const index = await readIndex(f); assert.equal(index.clips.length, 1); assert.equal(index.observations.length, 2);
});

async function readMetrics(f, status) {
  return JSON.parse(await readFile(join(f.directory, '.auto-work/runs', status.run_key,
    `execution-metrics-${status.job_id}.json`), 'utf8'));
}
test('execution audit distinguishes fresh requests, cached inputs, and unsuccessful attempts', async () => {
  const f = await fixture();
  const first = await f.run(), cold = await readMetrics(f, first);
  assert.equal(cold.outcome, 'completed');
  assert.equal(cold.attempted_submission_images, f.calls[0].frames.length);
  assert.equal(cold.successful_response_images, cold.attempted_submission_images);
  assert.ok(cold.windows[0].request_wall_ms >= 0); assert.ok(cold.elapsed_wall_ms >= 0);
  const second = await f.run(), cached = await readMetrics(f, second);
  assert.equal(cached.attempted_submission_images, 0); assert.equal(cached.cached_window_count, 1);
  assert.equal(cached.planned_window_input_images, cold.planned_window_input_images);
  assert.equal(cached.windows[0].request_wall_ms, null);
  assert.equal(cached.token_usage, null); assert.equal(cached.dollar_cost, null);
  assert.ok(!JSON.stringify(cached).includes('fixture-private-pairing'));
  const fail = await fixture({ backend: async () => jsonResponse({ error: 'failed' }, 503) });
  const failure = await fail.run(), failed = await readMetrics(fail, failure);
  assert.equal(failed.outcome, 'interrupted_before_completion');
  assert.equal(failed.attempted_submission_images, fail.calls[0].frames.length);
  assert.equal(failed.successful_response_images, 0);
});

test('context dedup is opt-in, preserves sampled coverage, and uses a distinct policy and cache key', async () => {
  const backend = async body => jsonResponse(response(body.frames, body.frames.map(f => observation(f)),
    body.frames.filter((_, i) => i === 1 || i === 3).map(f => f.frame_id)));
  const normal = await fixture({ backend });
  const standard = await normal.run(), standardIndex = await readIndex(normal);
  const candidate = createAutoPipeline({ directory: normal.directory, bridgePairingCode: 'fixture-private-pairing',
    extractImpl: normal.extractImpl, remuxImpl: normal.remuxImpl, fetchImpl: normal.fetchImpl,
    refinementMode: 'context-dedup-v1' });
  const started = candidate.start(normal.input), optimized = await candidate.wait(started.job_id);
  assert.equal(optimized.status, 'completed'); assert.notEqual(standard.run_key, optimized.run_key);
  assert.equal(optimized.cached_windows, 0);
  const index = await readIndex(normal);
  assert.equal(index.clips[0].sampled_frames, standardIndex.clips[0].sampled_frames);
  assert.equal(index.clips[0].policy_version, 'bounded-refinement-context-dedup-v1');
  assert.ok(index.observations.every(o => o.policy_version === index.clips[0].policy_version));
  const a = await readMetrics(normal, standard), b = await readMetrics(normal, optimized);
  assert.ok(b.attempted_submission_images < a.attempted_submission_images);
  assert.ok(b.attempted_submission_unique_images === a.attempted_submission_unique_images);
  const repeated = candidate.start(normal.input), cached = await candidate.wait(repeated.job_id);
  assert.equal(cached.calls, 0); assert.ok(cached.cached_windows > 0);
  assert.throws(() => createAutoPipeline({ directory: normal.directory, refinementMode: 'typo' }), /配置/u);
});

test('a failed second window publishes nothing; retry reuses the validated first window', async () => {
  let failOnce = true;
  const f = await fixture({ duration: 30, backend: async (body, call) => {
    if (call === 2 && failOnce) { failOnce = false; return jsonResponse({ error: 'temporary' }, 503); }
    return jsonResponse(response(body.frames));
  } });
  const first = await f.run(); assert.equal(first.status, 'failed'); assert.equal(first.completed_windows, 1);
  await assertUnpublished(f);
  const second = await f.run(); assert.equal(second.status, 'completed'); assert.equal(second.cached_windows, 1);
  assert.equal(second.calls, 1); assert.equal(f.calls.length, 3);
});

test('unknown frame IDs, guessed placement, and mismatched model versions fail closed', async () => {
  for (const mode of ['frame', 'placed', 'model', 'time']) {
    const f = await fixture({ backend: async body => {
      const r = response(body.frames, [observation(body.frames[0])]);
      if (mode === 'frame') r.observations[0].frame_id = 'not-sent';
      if (mode === 'placed') r.observations[0].state = 'placed';
      if (mode === 'model') r.model = 'other-model';
      if (mode === 'time') r.refine_frame_ids = ['not-sent'];
      return jsonResponse(r);
    } });
    assert.equal((await f.run()).status, 'failed'); await assertUnpublished(f);
  }
});

test('coarse/dense disagreement on the same real frame is retained as uncertain and never promoted', async () => {
  const f = await fixture({ backend: async (body, call) => {
    if (call === 1) return jsonResponse(response(body.frames, [observation(body.frames[1])], [body.frames[1].frame_id]));
    const shared = body.frames.find(fr => fr.frame_id === 'f000005');
    assert.ok(shared);
    return jsonResponse(response(body.frames, [observation(shared, { state: 'held', location_zh: '手中' })]));
  } });
  const status = await f.run(); assert.equal(status.status, 'completed'); assert.equal(f.extraction.length, 2);
  const index = await readIndex(f); assert.equal(index.observations.length, 1);
  const o = index.observations[0]; assert.equal(o.state, 'uncertain'); assert.equal(o.use_for_last_seen, false);
  assert.equal(o.conflicting_observations.length, 2); assert.equal(o.output_pts_seconds, 1);
});

test('excessive refinement keeps a bounded explicit plan and never promotes unrefined observations', async () => {
  const f = await fixture({ duration: 75, maxRefineRanges: 2, backend: async body => {
    const ids = body.frames.filter(fr => fr.timestamp_seconds % 10 === 0).map(fr => fr.frame_id);
    return jsonResponse(response(body.frames, body.frames.map(frame => observation(frame)), ids));
  } });
  const status = await f.run(); assert.equal(status.status, 'completed');
  assert.ok(status.result.unrefined_range_count > 0); assert.ok(status.calls <= 30);
  const index = await readIndex(f), clip = index.clips[0];
  assert.equal(clip.coverage_status, 'bounded_with_unrefined_ranges');
  assert.ok(clip.unrefined_ranges.some(r => r.start === 0));
  const unrefined = index.observations.filter(o => clip.unrefined_ranges.some(r => o.output_pts_seconds >= r.start && o.output_pts_seconds <= r.end));
  assert.ok(unrefined.length > 0);
  assert.ok(unrefined.every(o => o.is_object_visible && o.needs_review && !o.use_for_last_seen && !o.usable_as_clear_evidence));
  assert.equal(await readFile(join(f.directory, 'data/index.json'), 'utf8'), f.reference);
});

test('single active task, cancellation and timeout never publish partial work', async () => {
  let enteredResolve; const entered = new Promise(r => { enteredResolve = r; });
  const f = await fixture({ backend: async (_body, _n, init) => {
    enteredResolve(); return new Promise((_resolve, reject) => init.signal.addEventListener('abort',
      () => reject(new DOMException('Aborted', 'AbortError')), { once: true }));
  } });
  const started = f.engine.start(f.input);
  assert.throws(() => f.engine.start(f.input), { code: 'busy' });
  await entered; f.engine.cancel(started.job_id);
  assert.equal((await f.engine.wait(started.job_id)).status, 'cancelled'); await assertUnpublished(f);
  const timeout = await fixture({ requestTimeoutMs: 20, backend: async (_body, _n, init) =>
    new Promise((_resolve, reject) => { const timer = setTimeout(() => {}, 100);
      init.signal.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); }, { once: true }); }) });
  const failed = await timeout.run(); assert.equal(failed.error_code, 'request_timeout'); await assertUnpublished(timeout);
});

test('non-loopback bridge URLs and malformed starts are rejected before IO or network', async () => {
  const directory = '/tmp/never-used';
  for (const bridgeURL of ['https://example.com', 'http://localhost:8766', 'http://fixture-user:fixture-pass@127.0.0.1', 'http://127.0.0.1/path'])
    assert.throws(() => createAutoPipeline({ directory, bridgeURL }), { code: 'configuration' });
  const pipeline = createAutoPipeline({ directory });
  assert.equal(pipeline.getStatus().status, 'idle');
  for (const input of [{ sourcePath: 'relative.mov', startedAt: '2026-01-01' },
    { sourcePath: '/tmp/ok.mov', startedAt: 'invalid' }, { sourcePath: '/tmp/ok.mov', startedAt: '2026-01-01', clipId: '../escape' }])
    assert.throws(() => pipeline.start(input), { code: 'invalid_input' });
});

test('a later failed job preserves an already published automatic index byte-for-byte', async () => {
  let fail = false;
  const f = await fixture({ backend: async body => fail ? jsonResponse({}, 503) : jsonResponse(response(body.frames)) });
  assert.equal((await f.run()).status, 'completed');
  const path = join(f.directory, 'data/auto-index.json'), original = await readFile(path, 'utf8');
  await writeFile(f.source, 'different local video fixture'); fail = true;
  assert.equal((await f.run()).status, 'failed'); assert.equal(await readFile(path, 'utf8'), original);
});

test('malformed manifest timestamps or escaping image paths never reach the backend', async () => {
  for (const mode of ['pts', 'escape']) {
    const directory = await mkdtemp(join(tmpdir(), 'robomemo-auto-manifest-'));
    const sourcePath = join(directory, 'source.mov'); await writeFile(sourcePath, 'fixture');
    let posted = false;
    const engine = createAutoPipeline({ directory, bridgePairingCode: 'fixture', fetchImpl: async url => {
      if (url.pathname !== '/v1/status') posted = true;
      return jsonResponse({ ready: true, model });
    }, extractImpl: async ({ output }) => {
      await mkdir(output, { recursive: true });
      const frame = { frame_id: 'f1', output_pts_seconds: mode === 'pts' ? 1 : 0,
        source_frame_index: 1, image_file: '../secret.jpg' };
      return { version: 1, duration_seconds: 2, decoded_frames: 1, sampling_version: 'fixture',
        frames: [frame], all_frame_pts: [{ frame_id: 'f1', output_pts_seconds: 0 }] };
    } });
    const { job_id } = engine.start({ sourcePath, startedAt: '2026-01-01' });
    assert.equal((await engine.wait(job_id)).status, 'failed'); assert.equal(posted, false);
  }
});


test('new model or prompt builds a separate complete run and replaces only that video current run', async () => {
  const f = await fixture({ backend: async body => jsonResponse(response(body.frames,
    [observation(body.frames[0]), observation(body.frames[1])])) });
  const initial = await f.run(); assert.equal(initial.status, 'completed');
  let previousKey = initial.run_key;
  for (const variant of [{ model: 'fixture-vision-v2', prompt: AUTO_PROMPT_VERSION },
    { model: 'fixture-vision-v2', prompt: 'object-observations-v2-test' }]) {
    let networkCalls = 0;
    const engine = createAutoPipeline({ directory: f.directory, bridgePairingCode: 'fixture-private-pairing',
      extractImpl: f.extractImpl, remuxImpl: f.remuxImpl, promptVersion: variant.prompt,
      fetchImpl: async (url, init) => {
        if (url.pathname === '/v1/status') return jsonResponse({ ready: true, model: variant.model, object_prompt_version: variant.prompt });
        networkCalls++; const body = JSON.parse(init.body);
        assert.equal(body.expected_model, variant.model);
        return jsonResponse({ ...response(body.frames), model: variant.model, prompt_version: variant.prompt });
      } });
    const started = engine.start({ ...f.input, clipId: 'changed-display-name' });
    const completed = await engine.wait(started.job_id);
    assert.equal(completed.status, 'completed'); assert.equal(completed.cached_windows, 0);
    assert.equal(networkCalls, 1); assert.notEqual(completed.run_key, previousKey);
    const index = await readIndex(f); assert.equal(index.clips.length, 1); assert.equal(index.observations.length, 0);
    assert.equal(index.clips[0].run_key, completed.run_key);
    assert.ok(index.clips[0].video_file.includes(completed.run_key));
    previousKey = completed.run_key;
  }
  assert.equal((await readdir(join(f.directory, '.auto-work/runs'))).length, 3);
  assert.equal(await readFile(join(f.directory, 'data/index.json'), 'utf8'), f.reference);
});


test('new MOV uses verified MP4 playback and rejects mismatched remux frame counts before inference', async () => {
  const good = await fixture(); assert.equal((await good.run()).status, 'completed');
  assert.match((await readIndex(good)).clips[0].video_file, /\.mp4$/u);
  const invalid = await fixture({ remuxImpl: async () => ({ version: 1, passed: true, reencoded: false,
    payloads_and_timestamps_identical: true, frames: 9999, duration_seconds: 4.2 }) });
  const failed = await invalid.run(); assert.equal(failed.status, 'failed'); assert.equal(failed.error_code, 'invalid_remux');
  assert.equal(invalid.calls.length, 0); await assertUnpublished(invalid);
});


test('extract subprocess has a bounded timeout and cancellation waits until it exits', async () => {
  async function sleepingExtractor(directory) {
    const script = join(directory, 'sleeping-extractor.mjs');
    const pidFile = join(directory, 'extractor.pid');
    await writeFile(script, `#!${process.execPath}\nimport {writeFileSync} from 'node:fs';\nwriteFileSync(${JSON.stringify(pidFile)},String(process.pid));\nsetInterval(()=>{},1000);\n`);
    await chmod(script, 0o700); return { script, pidFile };
  }
  const timeout = await fixture({ extractImpl: undefined, extractTimeoutMs: 1000 });
  const first = await sleepingExtractor(timeout.directory);
  const timed = createAutoPipeline({ directory: timeout.directory, bridgePairingCode: 'fixture-private-pairing',
    fetchImpl: timeout.fetchImpl, extractBinary: first.script, extractTimeoutMs: 1000 });
  const t = timed.start(timeout.input); const failure = await timed.wait(t.job_id);
  assert.equal(failure.error_code, 'extract_timeout');
  const pid = Number(await readFile(first.pidFile, 'utf8'));
  assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' }); await assertUnpublished(timeout);

  const cancelled = await fixture(); const second = await sleepingExtractor(cancelled.directory);
  const cancelEngine = createAutoPipeline({ directory: cancelled.directory, bridgePairingCode: 'fixture-private-pairing',
    fetchImpl: cancelled.fetchImpl, extractBinary: second.script });
  const c = cancelEngine.start(cancelled.input);
  let cancelPID;
  for (let i = 0; i < 100; i++) {
    try { cancelPID = Number(await readFile(second.pidFile, 'utf8')); break; }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.ok(cancelPID); cancelEngine.cancel(c.job_id);
  assert.equal((await cancelEngine.wait(c.job_id)).status, 'cancelled');
  assert.throws(() => process.kill(cancelPID, 0), { code: 'ESRCH' }); await assertUnpublished(cancelled);
});


test('a selected dense window failure does not publish a bounded partial job', async () => {
  const f = await fixture({ duration: 75, maxCalls: 5, backend: async (body, call) => {
    if (call === 5) return jsonResponse({ error: 'temporary' }, 503);
    return jsonResponse(response(body.frames, [], body.frames.filter(fr => fr.timestamp_seconds % 10 === 0).map(fr => fr.frame_id)));
  } });
  const status = await f.run(); assert.equal(status.status, 'failed'); assert.equal(status.calls, 5);
  await assertUnpublished(f);
});

test('validated legacy coarse windows migrate to the new policy run without repeating inference', async () => {
  const f = await fixture(); const initial = await f.run();
  const index = await readIndex(f), clip = index.clips[0];
  const legacyKey = createHash('sha256').update(JSON.stringify({ sourceHash: clip.source_video_sha256,
    backend: 'http://127.0.0.1:8766', model, promptVersion: AUTO_PROMPT_VERSION,
    samplingVersion: 'fixture-sampling-v1', mergeVersion: 'two-frame-conservative-v1' })).digest('hex');
  const runs = join(f.directory, '.auto-work/runs'), oldRun = join(runs, initial.run_key), legacy = join(runs, legacyKey);
  await mkdir(legacy, { recursive: true });
  let cacheFilename;
  for (const name of await readdir(oldRun)) {
    const data = JSON.parse(await readFile(join(oldRun, name), 'utf8'));
    if (data.response && data.stage === 'coarse') {
      cacheFilename = name; await writeFile(join(legacy, name), JSON.stringify({ ...data, key: legacyKey }));
    }
  }
  assert.ok(cacheFilename); await rm(oldRun, { recursive: true });
  const retried = await f.run(); assert.equal(retried.status, 'completed'); assert.equal(retried.calls, 0);
  assert.equal(retried.cached_windows, 1); assert.notEqual(retried.run_key, legacyKey);
  const migrated = JSON.parse(await readFile(join(runs, retried.run_key, cacheFilename), 'utf8'));
  assert.equal(migrated.key, retried.run_key); assert.equal(migrated.cache_source_run_key, legacyKey);
  assert.equal((await readIndex(f)).clips.length, 1);
  // A valid response cannot excuse different input hashes.
  await rm(join(runs, retried.run_key), { recursive: true });
  const corrupt = JSON.parse(await readFile(join(legacy, cacheFilename), 'utf8'));
  corrupt.input_frames[0].sha256 = 'different'; await writeFile(join(legacy, cacheFilename), JSON.stringify(corrupt));
  const failed = await f.run(); assert.equal(failed.error_code, 'invalid_cache'); assert.equal(failed.calls, 0);
});

test('all outcomes remove only their own transient import directory', async () => {
  for (const shouldFail of [false, true]) {
    const f = await fixture({ backend: async body => shouldFail ? jsonResponse({}, 503) : jsonResponse(response(body.frames)) });
    const unrelated = join(f.directory, '.auto-work/imports/unrelated-job');
    await mkdir(unrelated, { recursive: true }); await writeFile(join(unrelated, 'keep.txt'), 'keep');
    const status = await f.run(); assert.equal(status.status, shouldFail ? 'failed' : 'completed');
    assert.deepEqual(await readdir(join(f.directory, '.auto-work/imports')), ['unrelated-job']);
    assert.equal(await readFile(join(unrelated, 'keep.txt'), 'utf8'), 'keep');
  }
});

test('oversized publication fails before marking an audit completed or replacing the current index', async () => {
  const f = await fixture(); const initial = await f.run(); assert.equal(initial.status, 'completed');
  const indexPath = join(f.directory, 'data/auto-index.json'), previous = await readIndex(f);
  previous.clips[0].label = 'x'.repeat(16 * 1024 * 1024);
  const previousText = JSON.stringify(previous); await writeFile(indexPath, previousText);
  await writeFile(f.source, 'different video for size check');
  const failed = await f.run(); assert.equal(failed.error_code, 'index_too_large');
  assert.equal(await readFile(indexPath, 'utf8'), previousText);
  await assert.rejects(readFile(join(f.directory, '.auto-work/runs', failed.run_key, 'completed.json')), { code: 'ENOENT' });
});
