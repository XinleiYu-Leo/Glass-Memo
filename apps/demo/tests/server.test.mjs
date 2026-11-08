import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { request } from 'node:http';
import { createDemoServer } from '../server.mjs';
const data = { schema_version: 1,
  objects: [{ id: 'keys', label_zh: '钥匙', aliases: ['钥匙', 'keys'] }],
  clips: [{ id: 'clip', label: 'Test', started_at: '2026-10-23T05:00:00Z', duration_seconds: 60,
    video_file: 'clip.mp4', poster_file: 'poster.jpg' }],
  observations: [{ id: 'o1', object_id: 'keys', label_zh: '钥匙', clip_id: 'clip', state: 'held',
    output_pts_seconds: 50, observation_zh: '手中拿着钥匙。', location_zh: '右手中',
    is_object_visible: true, use_for_last_seen: true, usable_as_clear_evidence: true,
    provenance: 'codex_visual_review', review_status: 'ai_reviewed', limitations: ['后续位置未知。'], evidence_file: 'poster.jpg' }],
};

async function directory() {
  const root = await mkdtemp(join(tmpdir(), 'robomemo-demo-test-'));
  await mkdir(join(root, 'web'));
  for (const f of ['index.html', 'style.css', 'app.js']) await writeFile(join(root, 'web', f), 'test');
  await writeFile(join(root, 'clip.mp4'), Buffer.from('0123456789abcdefghij'));
  await writeFile(join(root, 'poster.jpg'), 'image');
  return root;
}
test('real HTTP: grounded query, playable byte-range routes, host/origin restriction and no arbitrary files', async t => {
  const root = await directory();
  const server = await createDemoServer({ directory: root, data });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  t.after(async () => { await new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }); await rm(root, { recursive: true, force: true }); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const catalog = await fetch(`${base}/api/catalog`);
  assert.equal(catalog.status, 200);
  assert.equal((await catalog.json()).clips.length, 1);
  const result = await fetch(`${base}/api/search`, { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: base }, body: JSON.stringify({ query: '钥匙在哪' }) });
  assert.match((await result.json()).answers[0].headline, /手中/);
  const media = await fetch(`${base}/media/clip`, { headers: { Range: 'bytes=3-7' } });
  assert.equal(media.status, 206);
  assert.equal(media.headers.get('content-range'), 'bytes 3-7/20');
  assert.equal(media.headers.get('content-type'), 'video/mp4');
  assert.equal(await media.text(), '34567');
  assert.equal((await fetch(`${base}/media/clip`, { headers: { Range: 'bytes=30-' } })).status, 416);
  const head = await fetch(`${base}/media/clip`, { method: 'HEAD' });
  assert.equal(head.headers.get('content-length'), '20');
  assert.equal(await head.text(), '');
  const invalidHostStatus = await new Promise((resolve, reject) => {
    const req = request(`${base}/api/catalog`, { headers: { Host: 'evil.example' } }, res => {
      res.resume(); res.once('end', () => resolve(res.statusCode));
    });
    req.on('error', reject); req.end();
  });
  assert.equal(invalidHostStatus, 403);
  assert.equal((await fetch(`${base}/api/search`, { method: 'POST', headers: { Origin: 'https://evil.example', 'Content-Type': 'application/json' }, body: '{"query":"keys"}' })).status, 403);
  for (const path of ['/data/index.json', '/server.mjs', '/%2e%2e/secret', '/evidence/missing'])
    assert.equal((await fetch(base + path)).status, 404);
  assert.equal((await fetch(`${base}/api/search`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{' })).status, 400);
});
test('a dataset symlink outside the demo cannot be served', async () => {
  const root = await directory();
  const outside = await mkdtemp(join(tmpdir(), 'robomemo-outside-'));
  try {
    await writeFile(join(outside, 'secret'), 'private');
    await rm(join(root, 'poster.jpg'));
    await symlink(join(outside, 'secret'), join(root, 'poster.jpg'));
    await assert.rejects(createDemoServer({ directory: root, data }), /目录外/);
  } finally { await rm(root, { recursive: true, force: true }); await rm(outside, { recursive: true, force: true }); }
});
