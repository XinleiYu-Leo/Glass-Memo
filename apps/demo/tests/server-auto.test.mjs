import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, realpath, readdir, rm, symlink, rename, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { request } from 'node:http';
import { createDemoServer } from '../server.mjs';
import { PipelineError } from '../auto-pipeline.mjs';

const reviewed = { schema_version: 1, objects: [{ id: 'keys', label_zh: '钥匙', aliases: ['钥匙', 'keys'] }],
  clips: [{ id: 'clip', label: '已有录像', started_at: '2026-10-23T05:00:00Z', duration_seconds: 60,
    video_file: 'clip.mp4', poster_file: 'poster.jpg' }],
  observations: [{ id: 'o1', object_id: 'keys', clip_id: 'clip', state: 'held', output_pts_seconds: 50,
    observation_zh: '手中拿着钥匙。', location_zh: '右手中', is_object_visible: true, use_for_last_seen: true,
    usable_as_clear_evidence: true, provenance: 'codex_visual_review', review_status: 'ai_reviewed',
    limitations: ['后续位置未知。'], evidence_file: 'poster.jpg' }],
};
function autoData(id = 'auto-clip') {
  return { schema_version: 1, provenance: 'vision_pipeline', objects: structuredClone(reviewed.objects),
    clips: [{ id, label: '自动录像', started_at: '2026-10-23T06:00:00Z', duration_seconds: 60,
      video_file: `data/auto/${id}.mp4`, poster_file: `data/auto/${id}.jpg`, run_key: `${id}-run`,
      model: 'mock-model', prompt_version: 'test-prompt', sampling_version: 'test-sampling' }],
    observations: [1, 2].map(n => ({ ...reviewed.observations[0], id: `${id}-o${n}`, clip_id: id, state: 'visible',
      output_pts_seconds: n === 1 ? 50 : 49, frame_id: `f${n}`, run_key: `${id}-run`,
      location_zh: '桌面上', observation_zh: '桌面上可见钥匙。', provenance: 'vision_pipeline', review_status: 'auto_checked',
      human_ground_truth: null, model: 'mock-model', prompt_version: 'test-prompt', sampling_version: 'test-sampling',
      support_frame_ids: ['f1', 'f2'], needs_review: false, identity: 'match', hand_relation: 'free',
      evidence_file: `data/auto/${id}.jpg`, limitations: ['自动提取，尚需核验。'] })),
  };
}
function fakeAnalysis() {
  const starts = [], cancels = [], jobs = new Map();
  let current = { status: 'idle', phase: 'idle', message: '等待开始。' };
  function finish(status = 'completed') {
    current = { ...current, status, phase: status };
    jobs.get(current.job_id)?.resolve({ ...current });
    return current;
  }
  return { starts, cancels,
    getStatus: () => ({ ...current, sourcePath: '/not-public/private.mov', observations: ['not public'] }),
    start(input) {
      if (current.status === 'running') throw new PipelineError('busy', '任务正在进行。');
      starts.push(input); current = { job_id: `job-${starts.length}`, status: 'running', phase: 'queued', calls: 0, progress: 0 };
      jobs.set(current.job_id, Promise.withResolvers());
      return { job_id: current.job_id, status: current.status, sourcePath: input.sourcePath };
    },
    cancel(id) {
      if (id !== current.job_id) throw new PipelineError('unknown_job', '找不到任务。');
      cancels.push(id); return finish('cancelled');
    },
    finish,
    wait: id => {
      if (!jobs.has(id)) throw new PipelineError('unknown_job', '找不到任务。');
      return jobs.get(id).promise;
    },
  };
}
async function fixture(t, analysis = fakeAnalysis(), { now, beforeServer } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'robomemo-auto-http-'));
  await mkdir(join(root, 'web'));
  for (const name of ['index.html', 'app.js', 'style.css']) await writeFile(join(root, 'web', name), 'test');
  await writeFile(join(root, 'clip.mp4'), 'reviewed-video'); await writeFile(join(root, 'poster.jpg'), 'reviewed-image');
  if (beforeServer) await beforeServer(root);
  const server = await createDemoServer({ directory: root, data: reviewed, now, ...(analysis !== undefined ? { analysis } : {}) });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  t.after(async () => {
    await new Promise(resolve => { server.close(resolve); server.closeAllConnections(); });
    await rm(root, { recursive: true, force: true });
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const post = (path, body, headers = {}) => fetch(base + path, { method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: base, ...headers }, body: JSON.stringify(body) });
  return { root, server, base, analysis, post };
}
async function publish(root, value, { files = true } = {}) {
  await mkdir(join(root, 'data/auto'), { recursive: true });
  if (files) for (const clip of value.clips) {
    await writeFile(join(root, clip.video_file), `video:${clip.id}`);
    await writeFile(join(root, clip.poster_file), `image:${clip.id}`);
  }
  const temporary = join(root, 'data/auto-index.next.json');
  await writeFile(temporary, JSON.stringify(value));
  await rename(temporary, join(root, 'data/auto-index.json'));
}
function rawRequest(url, { headers = {}, body, end = true } = {}) {
  let req;
  const response = new Promise((resolve, reject) => {
    req = request(url, { method: 'POST', headers: { Connection: 'close', ...headers } }, res => {
      const chunks = []; res.on('data', chunk => chunks.push(chunk));
      res.once('end', () => resolve({ status: res.statusCode, body: JSON.parse(Buffer.concat(chunks).toString()) }));
    });
    req.on('error', reject);
    if (body !== undefined) req.write(body);
    if (end) req.end();
  });
  return { req, response };
}
async function waitMissing(path) {
  for (let i = 0; i < 100; i++) {
    if (!await stat(path).catch(error => error.code === 'ENOENT' ? null : Promise.reject(error))) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.fail('Temporary upload was not removed.');
}
async function upload(base, name = 'recording.mov') {
  const response = await fetch(`${base}/api/analysis/import?filename=${encodeURIComponent(name)}`, {
    method: 'POST', headers: { Origin: base, 'Content-Type': 'application/octet-stream' }, body: 'video-bytes',
  });
  assert.equal(response.status, 201);
  return response.json();
}

test('HTTP auto source starts empty, never falls back to reviewed observations, and keeps the default unchanged', async t => {
  const { base, post } = await fixture(t);
  assert.equal((await (await fetch(`${base}/api/catalog`)).json()).clips[0].video_url, '/media/clip');
  const auto = await (await fetch(`${base}/api/catalog?source=auto`)).json();
  assert.equal(auto.clips.length, 0); assert.equal(auto.observations.length, 0);
  assert.equal((await (await post('/api/search', { query: 'keys', source: 'auto' })).json()).status, 'no_evidence');
  assert.equal((await (await post('/api/search', { query: 'keys' })).json()).answers[0].observation.id, 'o1');
  for (const suffix of ['?source=other', '?source=auto&source=reviewed'])
    assert.equal((await fetch(`${base}/api/catalog${suffix}`)).status, 400);
  assert.equal((await post('/api/search?source=reviewed', { query: 'keys', source: 'auto' })).status, 400);
  assert.equal((await post('/api/search', { query: 'keys', source: null })).status, 400);
});

test('HTTP analysis exposes only public status and maps existing clip IDs to trusted files', async t => {
  const { base, post, root, analysis } = await fixture(t);
  const status = await (await fetch(`${base}/api/analysis/status`)).json();
  assert.equal(status.status, 'idle'); assert.equal(status.enabled, true);
  assert.deepEqual(status.available_clips, [{ id: 'clip', label: '已有录像', started_at: '2026-10-23T05:00:00Z', duration_seconds: 60 }]);
  assert.equal(status.sourcePath, undefined); assert.equal(status.observations, undefined);
  assert.equal(JSON.stringify(status).includes(root), false);
  const started = await post('/api/analysis/start', { clip_id: 'clip' });
  assert.equal(started.status, 202); assert.deepEqual(await started.json(), { job_id: 'job-1', status: 'running' });
  assert.deepEqual(analysis.starts, [{ sourcePath: await realpath(join(root, 'clip.mp4')),
    playbackPath: await realpath(join(root, 'clip.mp4')), clipId: 'clip', startedAt: reviewed.clips[0].started_at, label: '已有录像' }]);
  assert.equal((await post('/api/analysis/start', { clip_id: 'clip' })).status, 409);
  assert.equal((await post('/api/analysis/cancel', { job_id: 'unknown' })).status, 404);
  assert.equal((await post('/api/analysis/cancel', { job_id: 'job-1' })).status, 200);
  assert.deepEqual(analysis.cancels, ['job-1']);
});

test('HTTP status exposes coverage limits but excludes internal result paths', async t => {
  const analysis = fakeAnalysis();
  analysis.getStatus = () => ({ status: 'completed', result: { clip_id: 'auto-clip',
    unrefined_range_count: 2, coverage_status: 'partial', sourcePath: '/private/source.mov' } });
  const { base } = await fixture(t, analysis);
  const result = (await (await fetch(`${base}/api/analysis/status`)).json()).result;
  assert.deepEqual(result, { clip_id: 'auto-clip', unrefined_range_count: 2, coverage_status: 'partial' });
});

test('HTTP analysis mutations require exact origin, strict IDs and bounded JSON without accepting arbitrary paths', async t => {
  const { base, post, analysis } = await fixture(t);
  for (const path of ['/api/analysis/start', '/api/analysis/cancel']) {
    const body = path.endsWith('start') ? { clip_id: 'clip' } : { job_id: 'job-1' };
    assert.equal((await fetch(base + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).status, 403);
    assert.equal((await post(path, body, { Origin: 'https://example.invalid' })).status, 403);
  }
  for (const value of [null, [], {}, { clip_id: 'unknown' }, { sourcePath: '/tmp/secret.mov' },
    { clip_id: 'clip', sourcePath: '/tmp/secret.mov' }, { clip_id: 123 }])
    assert.equal((await post('/api/analysis/start', value)).status, 400);
  assert.equal((await post('/api/analysis/start', { clip_id: 'clip' }, { 'Content-Type': 'text/plain' })).status, 415);
  assert.equal((await fetch(`${base}/api/analysis/start`, { method: 'POST', headers: { Origin: base, 'Content-Type': 'application/json' }, body: '{' })).status, 400);
  assert.equal((await post('/api/analysis/start', { clip_id: 'x'.repeat(5000) })).status, 413);
  assert.equal(analysis.starts.length, 0);
});

test('HTTP auto index is atomically replaced only after full validation, including evidence file targets', async t => {
  const { base, root, post } = await fixture(t);
  await publish(root, autoData());
  let auto = await (await fetch(`${base}/api/catalog?source=auto`)).json();
  assert.equal(auto.clips[0].video_url, '/auto/media/auto-clip');
  assert.equal(auto.clips[0].poster_url, '/auto/poster/auto-clip');
  assert.equal(auto.observations[0].evidence_url, '/auto/evidence/auto-clip-o1');
  const answer = await (await post('/api/search', { query: 'keys', source: 'auto' })).json();
  assert.equal(answer.answers[0].observation.id, 'auto-clip-o1');
  assert.equal(answer.answers[0].observation.video_url, '/auto/media/auto-clip');
  const ranged = await fetch(`${base}/auto/media/auto-clip`, { headers: { Range: 'bytes=0-4' } });
  assert.equal(ranged.status, 206); assert.equal(await ranged.text(), 'video');
  assert.equal((await fetch(`${base}/auto/evidence/auto-clip-o1`)).status, 200);
  assert.equal((await fetch(`${base}/auto/poster/auto-clip`, { method: 'HEAD' })).status, 200);
  assert.equal((await fetch(`${base}/media/auto-clip`)).status, 404);
  assert.equal((await fetch(`${base}/auto/media/clip`)).status, 404);
  const invalid = autoData('candidate'); invalid.observations[0].evidence_file = 'data/auto/missing.jpg';
  await publish(root, invalid);
  auto = await (await fetch(`${base}/api/catalog?source=auto`)).json();
  assert.equal(auto.clips[0].id, 'auto-clip');
  assert.equal((await fetch(`${base}/auto/media/candidate`)).status, 404);
  assert.ok((await (await fetch(`${base}/api/analysis/status`)).json()).auto_index.refresh_error);
  await publish(root, autoData('next'));
  auto = await (await fetch(`${base}/api/catalog?source=auto`)).json();
  assert.equal(auto.clips[0].id, 'next');
  assert.equal((await fetch(`${base}/auto/media/auto-clip`)).status, 404);
  assert.equal((await fetch(`${base}/auto/media/next`)).status, 200);
  assert.equal((await (await post('/api/search', { query: 'keys' })).json()).answers[0].observation.id, 'o1');
});

test('HTTP invalid or symlinked auto indexes cannot expose outside files or contaminate the reviewed source', async t => {
  const { base, root } = await fixture(t);
  const outside = await mkdtemp(join(tmpdir(), 'robomemo-auto-outside-'));
  t.after(() => rm(outside, { recursive: true, force: true }));
  await writeFile(join(outside, 'secret'), 'private');
  await publish(root, autoData());
  await fetch(`${base}/api/catalog?source=auto`);
  const bad = autoData('outside');
  await publish(root, bad);
  await rm(join(root, bad.clips[0].poster_file));
  await symlink(join(outside, 'secret'), join(root, bad.clips[0].poster_file));
  assert.equal((await (await fetch(`${base}/api/catalog?source=auto`)).json()).clips[0].id, 'auto-clip');
  assert.equal((await fetch(`${base}/auto/poster/outside`)).status, 404);
  await writeFile(join(root, 'data/auto-index.json'), JSON.stringify({ ...autoData('wrong'), observations: [reviewed.observations[0]] }));
  assert.equal((await (await fetch(`${base}/api/catalog?source=auto`)).json()).clips[0].id, 'auto-clip');
  await rm(join(root, 'data/auto-index.json'));
  await symlink(join(outside, 'secret'), join(root, 'data/auto-index.json'));
  assert.equal((await (await fetch(`${base}/api/catalog?source=auto`)).json()).clips[0].id, 'auto-clip');
  assert.equal((await fetch(`${base}/data/auto-index.json`)).status, 404);
  await rm(join(root, 'data/auto/auto-clip.jpg'));
  await symlink(join(outside, 'secret'), join(root, 'data/auto/auto-clip.jpg'));
  assert.equal((await fetch(`${base}/auto/evidence/auto-clip-o1`)).status, 404);
  assert.equal((await (await fetch(`${base}/api/catalog`)).json()).clips[0].id, 'clip');
});

test('HTTP video import keeps private UUID paths, starts only with explicit recording time and never serves uploads', async t => {
  const { base, root, post, analysis } = await fixture(t);
  const bytes = Buffer.from('test-video-bytes');
  const uploaded = await fetch(`${base}/api/analysis/import?filename=recording.MOV`, {
    method: 'POST', headers: { Origin: base, 'Content-Type': 'application/octet-stream' }, body: bytes,
  });
  assert.equal(uploaded.status, 201);
  const result = await uploaded.json();
  assert.match(result.upload_id, /^[a-f0-9-]{36}$/);
  assert.deepEqual(Object.keys(result).sort(), ['filename', 'size_bytes', 'upload_id']);
  assert.equal(result.size_bytes, bytes.length);
  const privatePath = join(root, '.auto-work/uploads', `${result.upload_id}.mov`);
  assert.deepEqual(await readFile(privatePath), bytes);
  assert.equal((await stat(privatePath)).mode & 0o777, 0o600);
  assert.equal((await stat(join(root, '.auto-work/uploads'))).mode & 0o777, 0o700);
  assert.equal((await fetch(`${base}/.auto-work/uploads/${result.upload_id}.mov`)).status, 404);
  for (const value of [{ upload_id: 'unknown', started_at: '2026-10-23T05:00:00Z' },
    { upload_id: result.upload_id }, { upload_id: result.upload_id, started_at: 'today' },
    { upload_id: result.upload_id, started_at: '2026-10-23T05:00:00' },
    { upload_id: result.upload_id, started_at: '2026-10-23T05:00:00Z', sourcePath: '/secret.mov' }])
    assert.equal((await post('/api/analysis/start', value)).status, 400);
  assert.equal(analysis.starts.length, 0);
  assert.equal((await post('/api/analysis/start', { upload_id: result.upload_id, started_at: '2026-10-23T05:00:00Z', label: '新录像' })).status, 202);
  assert.deepEqual(analysis.starts[0], { sourcePath: await realpath(privatePath), startedAt: '2026-10-23T05:00:00Z', label: '新录像' });
  const status = await (await fetch(`${base}/api/analysis/status`)).json();
  assert.equal(JSON.stringify(status).includes('.auto-work'), false);
  assert.deepEqual(status.available_clips.map(c => c.id), ['clip']);
});

test('HTTP imports enforce origin, filename/type, declared size and single concurrency; aborted uploads are deleted', async t => {
  const { base, root } = await fixture(t);
  const endpoint = `${base}/api/analysis/import?filename=test.mp4`;
  const headers = { Origin: base, 'Content-Type': 'application/octet-stream' };
  assert.equal((await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: 'x' })).status, 403);
  assert.equal((await fetch(endpoint, { method: 'POST', headers: { ...headers, Origin: 'https://evil.example' }, body: 'x' })).status, 403);
  assert.equal((await fetch(endpoint, { method: 'POST', headers: { ...headers, 'Content-Type': 'image/jpeg' }, body: 'x' })).status, 415);
  for (const filename of ['wrong.txt', '../test.mp4', 'folder\\test.mov', '', 'x'.repeat(161) + '.mp4'])
    assert.equal((await fetch(`${base}/api/analysis/import?filename=${encodeURIComponent(filename)}`, { method: 'POST', headers, body: 'x' })).status, 400);
  const noLength = rawRequest(endpoint, { headers: { ...headers, 'Transfer-Encoding': 'chunked' }, body: 'x' });
  assert.equal((await noLength.response).status, 411);
  const oversize = rawRequest(endpoint, { headers: { ...headers, 'Content-Length': String(1024 ** 3 + 1) } });
  assert.equal((await oversize.response).status, 413);
  const empty = rawRequest(endpoint, { headers: { ...headers, 'Content-Length': '0' } });
  assert.equal((await empty.response).status, 413);
  const slow = rawRequest(endpoint, { headers: { ...headers, 'Content-Length': '1000' }, body: 'partial', end: false });
  const rejectedSlow = slow.response.catch(() => {});
  // Wait for observable file creation, without invoking any decoder or inference.
  let privateFiles = [];
  for (let i = 0; i < 100; i++) {
    privateFiles = await readdir(join(root, '.auto-work/uploads')).catch(() => []);
    if (privateFiles.length) break;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.equal(privateFiles.length, 1);
  assert.equal((await fetch(endpoint, { method: 'POST', headers, body: 'x' })).status, 409);
  slow.req.destroy(); await rejectedSlow;
  for (let i = 0; i < 100; i++) {
    privateFiles = await readdir(join(root, '.auto-work/uploads'));
    if (!privateFiles.length) break;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.equal(privateFiles.length, 0);
  assert.equal((await fetch(endpoint, { method: 'POST', headers, body: 'second-upload' })).status, 201);
});

test('HTTP automatic analysis can be disabled for supplied-data users without importing or starting files', async t => {
  const { base, post } = await fixture(t, null);
  const status = await (await fetch(`${base}/api/analysis/status`)).json();
  assert.equal(status.enabled, false); assert.equal(status.status, 'idle');
  assert.equal((await post('/api/analysis/start', { clip_id: 'clip' })).status, 503);
  assert.equal((await fetch(`${base}/api/analysis/import?filename=test.mov`, { method: 'POST',
    headers: { Origin: base, 'Content-Type': 'application/octet-stream' }, body: 'x' })).status, 503);
  assert.equal((await fetch(`${base}/api/catalog`)).status, 200);
});

test('HTTP uploaded source is removed after every confirmed terminal job while a busy start preserves it', async t => {
  const { base, root, analysis, post } = await fixture(t);
  await post('/api/analysis/start', { clip_id: 'clip' });
  const pendingUpload = await upload(base);
  const pendingPath = join(root, '.auto-work/uploads', `${pendingUpload.upload_id}.mov`);
  assert.equal((await post('/api/analysis/start', { upload_id: pendingUpload.upload_id, started_at: '2026-10-23T05:00:00Z' })).status, 409);
  assert.equal(await readFile(pendingPath, 'utf8'), 'video-bytes');
  await post('/api/analysis/cancel', { job_id: 'job-1' });
  for (const terminal of ['failed', 'completed', 'cancelled']) {
    const imported = terminal === 'failed' ? pendingUpload : await upload(base);
    const path = join(root, '.auto-work/uploads', `${imported.upload_id}.mov`);
    const result = await post('/api/analysis/start', { upload_id: imported.upload_id, started_at: '2026-10-23T05:00:00Z' });
    assert.equal(result.status, 202);
    assert.equal((await post('/api/analysis/start', { upload_id: imported.upload_id, started_at: '2026-10-23T05:00:00Z' })).status, 409);
    assert.equal(await readFile(path, 'utf8'), 'video-bytes');
    analysis.finish(terminal);
    await waitMissing(path);
    assert.equal((await post('/api/analysis/start', { upload_id: imported.upload_id, started_at: '2026-10-23T05:00:00Z' })).status, 400);
  }
  assert.equal(await readFile(join(root, 'clip.mp4'), 'utf8'), 'reviewed-video');
});

test('HTTP unstarted uploads expire in one hour while active sources survive expiry and server close', async t => {
  let time = 1000;
  const { base, root, analysis, server, post } = await fixture(t, fakeAnalysis(), { now: () => time });
  const active = await upload(base), waiting = await upload(base);
  const activePath = join(root, '.auto-work/uploads', `${active.upload_id}.mov`);
  const waitingPath = join(root, '.auto-work/uploads', `${waiting.upload_id}.mov`);
  assert.equal((await post('/api/analysis/start', { upload_id: active.upload_id, started_at: '2026-10-23T05:00:00Z' })).status, 202);
  time += 60 * 60 * 1000;
  await fetch(`${base}/api/analysis/status`);
  await waitMissing(waitingPath);
  assert.equal(await readFile(activePath, 'utf8'), 'video-bytes');
  assert.equal((await post('/api/analysis/start', { upload_id: waiting.upload_id, started_at: '2026-10-23T05:00:00Z' })).status, 400);
  const closePending = await upload(base), closePath = join(root, '.auto-work/uploads', `${closePending.upload_id}.mov`);
  await new Promise(resolve => { server.close(resolve); server.closeAllConnections(); });
  await waitMissing(closePath);
  assert.equal(await readFile(activePath, 'utf8'), 'video-bytes');
  analysis.finish('completed');
  await waitMissing(activePath);
});

test('startup cleanup removes only UUID video regular files and never crosses directory or file symlinks', async t => {
  const outside = await mkdtemp(join(tmpdir(), 'robomemo-upload-preserve-'));
  t.after(() => rm(outside, { recursive: true, force: true }));
  const filename = '11111111-1111-4111-8111-111111111111.mov';
  const symlinkName = '22222222-2222-4222-8222-222222222222.mp4';
  const directoryName = '33333333-3333-4333-8333-333333333333.mov';
  await writeFile(join(outside, filename), 'outside-original');
  const first = await fixture(t, fakeAnalysis(), { beforeServer: async root => {
    await mkdir(join(root, '.auto-work/uploads'), { recursive: true });
    await mkdir(join(root, '.auto-work/imports'), { recursive: true });
    await writeFile(join(root, '.auto-work/uploads', filename), 'restart-leftover');
    await writeFile(join(root, '.auto-work/uploads', 'user-recording.mov'), 'keep-non-generated-name');
    await writeFile(join(root, '.auto-work/imports', filename), 'pipeline-source');
    await symlink(join(outside, filename), join(root, '.auto-work/uploads', symlinkName));
    await mkdir(join(root, '.auto-work/uploads', directoryName));
  } });
  await waitMissing(join(first.root, '.auto-work/uploads', filename));
  const remaining = await readdir(join(first.root, '.auto-work/uploads'));
  assert.deepEqual(remaining.sort(), [symlinkName, directoryName, 'user-recording.mov'].sort());
  assert.equal(await readFile(join(first.root, '.auto-work/imports', filename), 'utf8'), 'pipeline-source');
  assert.equal(await readFile(join(outside, filename), 'utf8'), 'outside-original');
  const second = await fixture(t, fakeAnalysis(), { beforeServer: async root => {
    await mkdir(join(root, '.auto-work'));
    await symlink(outside, join(root, '.auto-work/uploads'));
  } });
  assert.equal(await readFile(join(outside, filename), 'utf8'), 'outside-original');
  const rejected = await fetch(`${second.base}/api/analysis/import?filename=new.mov`, {
    method: 'POST', headers: { Origin: second.base, 'Content-Type': 'application/octet-stream' }, body: 'bytes',
  });
  assert.equal(rejected.status, 500);
  assert.deepEqual(await readdir(outside), [filename]);
});
