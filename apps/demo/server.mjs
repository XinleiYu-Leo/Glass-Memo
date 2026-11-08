import http from 'node:http';
import { readFile, realpath, stat, lstat, readdir, mkdir, chmod, open, unlink } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { resolve, sep, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { pipeline } from 'node:stream/promises';
import { Transform } from 'node:stream';
import { createHash, randomUUID } from 'node:crypto';
import { InputError, validateDataset, catalog, search, parseRange } from './core.mjs';
import { createAutoPipeline, PipelineError } from './auto-pipeline.mjs';

const root = fileURLToPath(new URL('.', import.meta.url));
const MAX_UPLOAD_BYTES = 1024 ** 3;
const UPLOAD_TTL_MS = 60 * 60 * 1000;
const uploadFilename = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.(?:mov|mp4)$/u;
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
  '.mp4': 'video/mp4', '.mov': 'video/quicktime' };
function send(res, status, value) {
  if (res.destroyed) return;
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(value));
}

class HTTPError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
async function readJSON(req) {
  if (req.headers['content-type']?.split(';')[0].trim() !== 'application/json')
    throw new HTTPError(415, '请求需要 JSON 格式。');
  if (Number(req.headers['content-length']) > 4096) throw new HTTPError(413, '请求过长，请缩短后重试。');
  const chunks = []; let bytes = 0;
  for await (const chunk of req) {
    bytes += chunk.length;
    if (bytes > 4096) throw new HTTPError(413, '请求过长，请缩短后重试。');
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new HTTPError(400, '请求格式无效。'); }
}
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
function oneField(input, key) {
  if (!record(input) || Object.keys(input).length !== 1 || typeof input[key] !== 'string'
    || !input[key] || input[key].length > 160) throw new InputError('请选择已有录像或分析任务。');
  return input[key];
}
function sourceFor(url, input) {
  const params = url.searchParams.getAll('source');
  if (params.length > 1) throw new InputError('索引来源无效。');
  const bodySource = record(input) && Object.hasOwn(input, 'source') ? input.source : undefined;
  if (params.length && bodySource !== undefined && bodySource !== params[0]) throw new InputError('索引来源不一致。');
  const source = bodySource !== undefined ? bodySource : params[0] ?? 'reviewed';
  if (!['reviewed', 'auto'].includes(source)) throw new InputError('请选择核验索引或自动索引。');
  return source;
}
function publicAnalysisStatus(value) {
  const result = {};
  for (const key of ['job_id', 'status', 'phase', 'message', 'progress', 'calls', 'cached_windows', 'completed_windows',
    'total_windows', 'model', 'run_key', 'error_code', 'started_at', 'updated_at']) {
    if (['string', 'number', 'boolean'].includes(typeof value?.[key])) result[key] = value[key];
  }
  if (record(value?.result)) {
    result.result = {};
    for (const key of ['clip_id', 'observation_count', 'clear_candidate_count', 'needs_review_count',
      'duration_seconds', 'sampled_frames', 'decoded_frames', 'unrefined_range_count', 'coverage_status'])
      if (['string', 'number'].includes(typeof value.result[key])) result.result[key] = value.result[key];
  }
  return result;
}

export async function createDemoServer({ directory = root, data: suppliedData, analysis: suppliedAnalysis,
  bridgeConnectionFile = process.env.ROBOMEMO_BRIDGE_CONNECTION_FILE,
  bridgePairingCode = process.env.ROBOMEMO_BRIDGE_PAIRING_CODE,
  enableAuto = process.env.ROBOMEMO_ENABLE_AUTO === '1', extractBinary,
  now = () => Date.now() } = {}) {
  const base = await realpath(directory);
  const data = validateDataset(suppliedData || JSON.parse(await readFile(resolve(base, 'data/index.json'), 'utf8')));
  async function checkedPath(relative) {
    const path = await realpath(resolve(base, relative));
    if (!path.startsWith(base + sep)) throw new Error('索引引用了演示目录外的文件。');
    if (!(await stat(path)).isFile()) throw new Error('索引引用的文件不可读。');
    return path;
  }
  async function mediaWhitelist(index, prefix = '', includeWeb = false) {
    const paths = new Map(includeWeb ? [['/', 'web/index.html'], ['/app.js', 'web/app.js'], ['/style.css', 'web/style.css']] : []);
    for (const clip of index.clips) {
      paths.set(`${prefix}/media/${encodeURIComponent(clip.id)}`, clip.video_file);
      paths.set(`${prefix}/poster/${encodeURIComponent(clip.id)}`, clip.poster_file);
    }
    for (const o of index.observations) paths.set(`${prefix}/evidence/${encodeURIComponent(o.id)}`, o.evidence_file);
    return new Map(await Promise.all([...paths].map(async ([route, relative]) => [route, await checkedPath(relative)])));
  }
  const allowed = await mediaWhitelist(data, '', true);
  const emptyAuto = validateDataset({ schema_version: 1, title: 'RoboMemo · 自动识别', provenance: 'vision_pipeline',
    objects: structuredClone(data.objects.filter(object => ['keys', 'wallet', 'earbud_case'].includes(object.id))),
    clips: [], observations: [], limitations: ['尚无已完成的自动分析。自动结果与核验记录分开显示。'] });
  let autoSnapshot = { data: emptyAuto, allowed: new Map(), digest: null };
  let autoRefreshError = null, refreshPending;
  async function refreshAutoSnapshot() {
    if (refreshPending) return refreshPending;
    refreshPending = (async () => {
      try {
        const indexPath = await checkedPath('data/auto-index.json');
        if ((await stat(indexPath)).size > 16 * 1024 * 1024) throw new Error('Automatic index exceeds the limit.');
        const bytes = await readFile(indexPath);
        if (bytes.length > 16 * 1024 * 1024) throw new Error('Automatic index exceeds the limit.');
        const digest = createHash('sha256').update(bytes).digest('hex');
        if (digest === autoSnapshot.digest) { autoRefreshError = null; return true; }
        const candidate = validateDataset(JSON.parse(bytes.toString('utf8')));
        if (candidate.provenance !== 'vision_pipeline'
          || candidate.observations.some(o => o.provenance !== 'vision_pipeline' || o.review_status !== 'auto_checked'))
          throw new Error('Automatic index has a different source.');
        const candidateAllowed = await mediaWhitelist(candidate, '/auto');
        // Both the dataset and its complete file allowlist become visible in one assignment.
        autoSnapshot = { data: candidate, allowed: candidateAllowed, digest };
        autoRefreshError = null;
        return true;
      } catch (error) {
        if (error.code === 'ENOENT') {
          // A missing referenced media file must not invalidate the last complete snapshot.
          try { await stat(resolve(base, 'data/auto-index.json')); }
          catch (indexError) {
            if (indexError.code === 'ENOENT') {
              autoSnapshot = { data: emptyAuto, allowed: new Map(), digest: null };
              autoRefreshError = null; return true;
            }
          }
        }
        autoRefreshError = '新的自动索引未通过完整校验，暂时保留上次有效结果。';
        return false;
      }
    })();
    try { return await refreshPending; } finally { refreshPending = null; }
  }
  await refreshAutoSnapshot();
  const analysis = suppliedAnalysis !== undefined ? suppliedAnalysis : suppliedData || !enableAuto ? null : createAutoPipeline({
    directory: base, bridgeConnectionFile, bridgePairingCode,
    bridgeURL: process.env.ROBOMEMO_BRIDGE_URL || 'http://127.0.0.1:8766', extractBinary: extractBinary || resolve(base, 'tools/auto-extract'),
    onPublished: async () => { if (!await refreshAutoSnapshot()) throw new Error('Automatic index refresh failed.'); },
  });
  const availableClips = data.clips.map(({ id, label, started_at, duration_seconds }) => ({ id, label, started_at, duration_seconds }));
  const disabledStatus = { status: 'idle', phase: 'idle', message: '此演示未启用自动分析。' };
  const uploads = new Map(); let uploadActive = false;
  async function uploadDirectory({ create = true } = {}) {
    let current = base;
    for (const part of ['.auto-work', 'uploads']) {
      current = resolve(current, part);
      if (create) {
        try { await mkdir(current, { mode: 0o700 }); } catch (error) { if (error.code !== 'EEXIST') throw error; }
      }
      let info;
      try { info = await lstat(current); }
      catch (error) { if (!create && error.code === 'ENOENT') return null; throw error; }
      // Do not traverse even an in-directory symlink when deleting temporary uploads.
      if (info.isSymbolicLink() || !info.isDirectory() || await realpath(current) !== current
        || !current.startsWith(base + sep))
        throw new HTTPError(500, '导入目录无效，请检查本机演示文件。');
      if (create) await chmod(current, 0o700);
    }
    return current;
  }
  async function deleteUploadFile(path, identity) {
    try {
      const directory = await uploadDirectory({ create: false });
      if (!directory || !path.startsWith(`${directory}${sep}`)) return false;
      const filename = path.slice(directory.length + 1);
      if (!uploadFilename.test(filename)) return false;
      const info = await lstat(path);
      if (!info.isFile() || info.isSymbolicLink()
        || (identity && (identity.dev !== info.dev || identity.ino !== info.ino))) return false;
      await unlink(path);
      return true;
    } catch (error) { return error.code === 'ENOENT'; }
  }
  async function removeUpload(id, upload) {
    if (uploads.get(id) !== upload) return;
    upload.removing = true;
    await deleteUploadFile(upload.path, upload.identity);
    if (uploads.get(id) === upload) uploads.delete(id);
  }
  async function cleanPendingUploads({ all = false } = {}) {
    for (const [id, upload] of uploads) {
      if (!upload.active && (all || now() >= upload.expiresAt)) await removeUpload(id, upload);
    }
  }
  // Only generated UUID video files in this private directory are restart leftovers.
  // Keep arbitrary names, symlinks, directories, source data and pipeline caches intact.
  const previousUploadDirectory = await uploadDirectory({ create: false }).catch(() => null);
  if (previousUploadDirectory) {
    for (const entry of await readdir(previousUploadDirectory, { withFileTypes: true }))
      if (entry.isFile() && uploadFilename.test(entry.name)) await deleteUploadFile(resolve(previousUploadDirectory, entry.name));
  }
  async function importVideo(req, url) {
    if (uploadActive) throw new HTTPError(409, '另一段录像正在导入，请等它完成。');
    if (req.headers['content-type']?.split(';')[0].trim() !== 'application/octet-stream')
      throw new HTTPError(415, '请直接上传 MOV 或 MP4 文件。');
    const filenames = url.searchParams.getAll('filename'), filename = filenames[0];
    if (filenames.length !== 1 || typeof filename !== 'string' || !filename || filename.length > 160
      || /[/\\\x00-\x1f\x7f]/u.test(filename) || !['.mov', '.mp4'].includes(extname(filename).toLowerCase()))
      throw new HTTPError(400, '请选择文件名有效的 MOV 或 MP4 录像。');
    const lengthHeader = req.headers['content-length'];
    if (typeof lengthHeader !== 'string' || !/^\d+$/u.test(lengthHeader))
      throw new HTTPError(411, '上传需要明确的文件大小，请重新选择录像。');
    const expectedSize = Number(lengthHeader);
    if (!Number.isSafeInteger(expectedSize) || expectedSize < 1 || expectedSize > MAX_UPLOAD_BYTES)
      throw new HTTPError(413, '录像必须大于 0 字节且不超过 1 GiB。');
    uploadActive = true;
    let path, identity, file, created = false;
    try {
      const directory = await uploadDirectory(), uploadID = randomUUID();
      path = resolve(directory, `${uploadID}${extname(filename).toLowerCase()}`);
      file = await open(path, 'wx', 0o600); created = true;
      const fileInfo = await file.stat(); identity = { dev: fileInfo.dev, ino: fileInfo.ino };
      let bytes = 0;
      const bounded = new Transform({ transform(chunk, encoding, callback) {
        bytes += chunk.length;
        callback(bytes > MAX_UPLOAD_BYTES || bytes > expectedSize
          ? new HTTPError(413, '录像超过上传大小上限。') : null, chunk);
      } });
      await pipeline(req, bounded, file.createWriteStream());
      if (bytes !== expectedSize) throw new HTTPError(400, '录像未完整上传，请重新选择文件。');
      uploads.set(uploadID, { path, filename, size: bytes, identity,
        expiresAt: now() + UPLOAD_TTL_MS, active: false });
      return { upload_id: uploadID, filename, size_bytes: bytes };
    } catch (error) {
      if (created) await deleteUploadFile(path, identity);
      if (error instanceof HTTPError) throw error;
      throw new HTTPError(400, '录像导入未完成，请重新选择文件。');
    } finally { await file?.close().catch(() => {}); uploadActive = false; }
  }
  const server = http.createServer(async (req, res) => {
    const port = server.address()?.port;
    const host = `127.0.0.1:${port}`;
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('X-Frame-Options', 'DENY');
    res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
    try {
      if (req.headers.host !== host || (req.headers.origin && req.headers.origin !== `http://${host}`))
        return send(res, 403, { message: '请使用本机的 127.0.0.1 地址打开演示。' });
      const url = new URL(req.url, `http://${host}`);
      if (req.method === 'GET' && url.pathname === '/api/catalog') {
        const source = sourceFor(url);
        if (source === 'auto') await refreshAutoSnapshot();
        const snapshot = source === 'auto' ? autoSnapshot.data : data;
        return send(res, 200, catalog(snapshot, { source }));
      }
      if (req.method === 'GET' && url.pathname === '/api/health') return send(res, 200, { app: 'robomemo-object-finder-demo', ready: true });
      if (req.method === 'GET' && url.pathname === '/api/analysis/status') {
        await cleanPendingUploads();
        await refreshAutoSnapshot();
        return send(res, 200, { ...publicAnalysisStatus(analysis?.getStatus() || disabledStatus),
          enabled: Boolean(analysis), available_clips: availableClips,
          auto_index: { available: autoSnapshot.data.clips.length > 0, clip_count: autoSnapshot.data.clips.length,
            observation_count: autoSnapshot.data.observations.length, refresh_error: autoRefreshError } });
      }
      if (req.method === 'POST' && ['/api/analysis/start', '/api/analysis/cancel', '/api/analysis/import'].includes(url.pathname)) {
        if (req.headers.origin !== `http://${host}`) return send(res, 403, { message: '请从本机演示页面发起分析操作。' });
        if (!analysis) return send(res, 503, { message: '此演示未启用自动分析。' });
        await cleanPendingUploads();
        if (url.pathname === '/api/analysis/import') return send(res, 201, await importVideo(req, url));
        const input = await readJSON(req);
        if (url.pathname === '/api/analysis/start') {
          if (record(input) && Object.hasOwn(input, 'upload_id')) {
            if (Object.keys(input).some(key => !['upload_id', 'started_at', 'label'].includes(key))
              || typeof input.upload_id !== 'string' || !uploads.has(input.upload_id)
              || typeof input.started_at !== 'string' || input.started_at.length > 100
              || !/^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/u.test(input.started_at)
              || !Number.isFinite(Date.parse(input.started_at))
              || (Object.hasOwn(input, 'label') && (typeof input.label !== 'string' || !input.label.trim()
                || input.label.length > 160 || /[\x00-\x1f<>]/u.test(input.label))))
              throw new InputError('请选择本次已导入的录像，并填写实际录制开始时间。');
            const upload = uploads.get(input.upload_id);
            if (upload.active || upload.removing) throw new HTTPError(409, '这份上传已交给分析任务或已过期；请再次选择文件。');
            if (typeof analysis.wait !== 'function') throw new HTTPError(503, '分析服务缺少任务完成通知，请更新本机服务。');
            upload.active = true;
            let started;
            try {
              const sourcePath = await checkedPath(upload.path);
              const info = await lstat(upload.path);
              if (info.isSymbolicLink() || !info.isFile() || info.dev !== upload.identity.dev || info.ino !== upload.identity.ino)
                throw new InputError('上传副本已改变，请重新选择录像。');
              started = analysis.start({ sourcePath, startedAt: input.started_at, label: input.label?.trim() || upload.filename });
            } catch (error) { upload.active = false; throw error; }
            // Deletion follows a confirmed terminal job, after the pipeline has stopped using the source.
            Promise.resolve().then(() => analysis.wait(started.job_id)).then(async terminal => {
              if (['completed', 'failed', 'cancelled'].includes(terminal?.status)) await removeUpload(input.upload_id, upload);
            }).catch(() => {});
            return send(res, 202, publicAnalysisStatus(started));
          }
          const clipID = oneField(input, 'clip_id'), clip = data.clips.find(item => item.id === clipID);
          if (!clip) throw new InputError('请选择已有录像。');
          const sourcePath = await checkedPath(clip.video_file);
          return send(res, 202, publicAnalysisStatus(analysis.start({ sourcePath, playbackPath: sourcePath,
            clipId: clip.id, startedAt: clip.started_at, label: clip.label })));
        }
        return send(res, 200, publicAnalysisStatus(analysis.cancel(oneField(input, 'job_id'))));
      }
      if (req.method === 'POST' && url.pathname === '/api/search') {
        const input = await readJSON(req), source = sourceFor(url, input);
        if (source === 'auto') await refreshAutoSnapshot();
        const snapshot = source === 'auto' ? autoSnapshot.data : data;
        return send(res, 200, search(snapshot, record(input) ? { ...input, source } : input));
      }
      if (url.pathname.startsWith('/auto/')) await refreshAutoSnapshot();
      let path = (url.pathname.startsWith('/auto/') ? autoSnapshot.allowed : allowed).get(url.pathname);
      if (!path || !['GET', 'HEAD'].includes(req.method)) return send(res, 404, { message: '没有这个页面或证据文件。' });
      // Recheck the actual target in case a previously allowed file was replaced by a symlink.
      try { path = await checkedPath(path); } catch { return send(res, 404, { message: '这个证据文件目前不可读取。' }); }
      const size = (await stat(path)).size;
      let range;
      try { range = parseRange(req.headers.range, size); }
      catch { res.setHeader('Content-Range', `bytes */${size}`); return send(res, 416, { message: '视频读取范围无效。' }); }
      const start = range?.start ?? 0, end = range?.end ?? size - 1;
      res.setHeader('Content-Type', MIME[extname(path)] || 'application/octet-stream');
      res.setHeader('Accept-Ranges', 'bytes');
      res.setHeader('Content-Length', end - start + 1);
      if (range) res.setHeader('Content-Range', `bytes ${start}-${end}/${size}`);
      res.writeHead(range ? 206 : 200);
      if (req.method === 'HEAD') return res.end();
      await pipeline(createReadStream(path, { start, end }), res);
    } catch (error) {
      if (res.destroyed || res.writableEnded) return;
      if (res.headersSent) return res.destroy();
      if (error instanceof PipelineError) return send(res, error.code === 'busy' ? 409
        : error.code === 'unknown_job' ? 404 : error.code === 'invalid_input' ? 400 : 503, { error: error.code, message: error.message });
      send(res, error instanceof HTTPError ? error.status : error instanceof InputError ? 400 : 500,
        { message: error instanceof InputError || error instanceof HTTPError ? error.message : '读取失败，请刷新重试。' });
    }
  });
  // Importing a local video may take longer than a small JSON query.
  server.requestTimeout = 300000;
  server.headersTimeout = 10000;
  const cleanupTimer = setInterval(() => { cleanPendingUploads().catch(() => {}); }, 60000);
  cleanupTimer.unref();
  server.once('close', () => {
    clearInterval(cleanupTimer);
    cleanPendingUploads({ all: true }).catch(() => {});
  });
  return server;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.ROBOMEMO_DEMO_PORT || 8787);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('演示端口无效。');
  const server = await createDemoServer();
  server.on('error', error => {
    console.error(error.code === 'EADDRINUSE' ? `端口 ${port} 已在使用。若演示已启动，请打开 http://127.0.0.1:${port}` : '演示未能启动，请检查文件是否齐全。');
    process.exitCode = 1;
  });
  server.listen(port, '127.0.0.1', () => console.log(`RoboMemo 找物品演示已就绪：http://127.0.0.1:${port}\n仅本机可访问。查询与回放不会上传录像。`));
}
