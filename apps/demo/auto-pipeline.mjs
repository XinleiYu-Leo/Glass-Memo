import { createReadStream } from 'node:fs';
import { chmod, copyFile, mkdir, readFile, realpath, rename, rm, stat, writeFile } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { dirname, extname, isAbsolute, join, resolve, sep } from 'node:path';
import { validateDataset } from './core.mjs';
import { contextDeduplicatedWindows } from './refinement-windows.mjs';

// This module never reads a reference index, credentials, or network at import time.
export const AUTO_PROMPT_VERSION = 'object-observations-v1';
export const AUTO_POLICY_VERSION = 'bounded-refinement-v2';
const DENSE_WINDOW_SECONDS = 8, MAX_INDEX_BYTES = 16 * 1024 * 1024;
const MAX_VIDEO_BYTES = 1024 ** 3, MAX_IMAGE_BYTES = 256 * 1024;
const OBJECTS = [
  { id: 'keys', label_zh: '钥匙', aliases: ['钥匙', '鑰匙', '钥匙串', '钥匙扣', 'key', 'keys', 'keyring', 'keychain'] },
  { id: 'wallet', label_zh: '钱包', aliases: ['钱包', '錢包', '皮夹', '皮夾', 'wallet', 'purse'] },
  { id: 'earbud_case', label_zh: '白色小盒', aliases: ['白色小盒', '耳机盒', '白色耳机盒', '耳机充电盒', 'earbud case', 'earbuds case'] },
];
const objectIDs = new Set(OBJECTS.map(o => o.id));
const plain = x => typeof x === 'string' && x.trim().length > 0;
const cleanText = (x, max) => plain(x) && x.length <= max && !/[\x00-\x1f<>]/u.test(x);
const modelText = (x, max) => plain(x) && x.length <= max && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/u.test(x);
const fileID = x => typeof x === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,79}$/u.test(x);
const digest = x => createHash('sha256').update(x).digest('hex');
const locationKey = x => x.normalize('NFKC').toLowerCase().replace(/[\s，。,.、：:;；()（）\-]/gu, '');
const checkAbort = signal => { if (signal.aborted) throw new PipelineError('cancelled', '任务已取消。'); };
export class PipelineError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const failure = (code, message) => { throw new PipelineError(code, message); };
async function atomicJSON(path, value) {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${randomUUID()}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  await rename(temporary, path);
}
async function hashFile(path) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
}
async function optionalJSON(path) {
  try { return JSON.parse(await readFile(path, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}
async function safeChild(root, filename) {
  if (!plain(filename) || isAbsolute(filename) || filename.includes('\\')
    || filename.split('/').some(p => !p || p === '.' || p === '..')) failure('invalid_file', '抽帧文件路径无效。');
  const actualRoot = await realpath(root), actual = await realpath(join(root, filename));
  if (!actual.startsWith(`${actualRoot}${sep}`)) failure('invalid_file', '抽帧文件越出任务目录。');
  return actual;
}
async function runProcess(binary, args, { signal, timeoutMs, kind }) {
  checkAbort(signal);
  const deadline = AbortSignal.timeout(timeoutMs);
  const combined = AbortSignal.any([signal, deadline]);
  await new Promise((resolvePromise, reject) => {
    let child, killTimer;
    const fail = () => new PipelineError(signal.aborted ? 'cancelled' : deadline.aborted ? `${kind}_timeout` : `${kind}_failed`,
      signal.aborted ? '任务已取消。' : deadline.aborted ? (kind === 'extract' ? '录像抽帧超时；未发布自动索引。' : '回放封装超时；未发布自动索引。')
        : kind === 'extract' ? '录像抽帧失败；未发布自动索引。' : 'MOV 回放封装验证失败；未发布自动索引。');
    try { child = spawn(binary, args, { stdio: ['ignore', 'ignore', 'ignore'], signal: combined }); }
    catch { reject(fail()); return; }
    const forceStop = () => { killTimer = setTimeout(() => child.kill('SIGKILL'), 2000); killTimer.unref(); };
    combined.addEventListener('abort', forceStop, { once: true });
    child.once('error', error => { if (error.name !== 'AbortError') reject(fail()); });
    child.once('close', code => {
      combined.removeEventListener('abort', forceStop); clearTimeout(killTimer);
      if (combined.aborted || code !== 0) reject(fail()); else resolvePromise();
    });
  });
}
async function runExtract(binary, { video, output, interval, ranges, signal, timeoutMs }) {
  await mkdir(output, { recursive: true });
  const args = ['--video', video, '--output', output, '--interval', String(interval)];
  if (ranges) {
    const path = join(dirname(output), `requested-ranges-${randomUUID()}.json`);
    await atomicJSON(path, ranges); args.push('--ranges-json', path);
  }
  await runProcess(binary, args, { signal, timeoutMs, kind: 'extract' });
  return JSON.parse(await readFile(join(output, 'manifest.json'), 'utf8'));
}
async function runRemux(binary, { video, output, reportPath, signal, timeoutMs }) {
  await runProcess(binary, [video, output, reportPath], { signal, timeoutMs, kind: 'remux' });
  return JSON.parse(await readFile(reportPath, 'utf8'));
}
async function validateManifest(manifest, root, prior = null) {
  if (manifest?.version !== 1 || !Number.isFinite(manifest.duration_seconds) || manifest.duration_seconds <= 0
    || manifest.duration_seconds > 301 || !Number.isSafeInteger(manifest.decoded_frames) || manifest.decoded_frames < 1
    || !plain(manifest.sampling_version) || !Array.isArray(manifest.frames) || !manifest.frames.length
    || !Array.isArray(manifest.all_frame_pts) || manifest.all_frame_pts.length !== manifest.decoded_frames)
    failure('invalid_manifest', '抽帧清单缺少完整解码或时间信息。');
  if (prior && (Math.abs(prior.duration_seconds - manifest.duration_seconds) > 0.000001
    || prior.decoded_frames !== manifest.decoded_frames)) failure('invalid_manifest', '加密抽帧与原片时间不一致。');
  const all = new Map(), frameOrdinals = new Map(); let prev = -Infinity;
  for (const frame of manifest.all_frame_pts) {
    if (!fileID(frame.frame_id) || all.has(frame.frame_id) || !Number.isFinite(frame.output_pts_seconds)
      || frame.output_pts_seconds < 0 || frame.output_pts_seconds > manifest.duration_seconds || frame.output_pts_seconds <= prev)
      failure('invalid_manifest', '完整解码帧的编号或时间无效。');
    all.set(frame.frame_id, frame.output_pts_seconds); frameOrdinals.set(frame.frame_id, all.size); prev = frame.output_pts_seconds;
  }
  const frames = new Map(); prev = -Infinity;
  for (const frame of manifest.frames) {
    if (!fileID(frame.frame_id) || frames.has(frame.frame_id) || !all.has(frame.frame_id)
      || all.get(frame.frame_id) !== frame.output_pts_seconds || frame.output_pts_seconds <= prev
      || !Number.isSafeInteger(frame.source_frame_index) || frame.source_frame_index !== frameOrdinals.get(frame.frame_id)
      || !Number.isInteger(frame.width) || frame.width < 1 || !Number.isInteger(frame.height) || frame.height < 1)
      failure('invalid_manifest', '采样帧未匹配实际解码时间。');
    const path = await safeChild(root, frame.image_file), bytes = await readFile(path);
    if (bytes.length < 4 || bytes.length > MAX_IMAGE_BYTES || bytes[0] !== 0xff || bytes[1] !== 0xd8
      || bytes.at(-2) !== 0xff || bytes.at(-1) !== 0xd9) failure('invalid_image', '采样图片不是合规大小的 JPEG。');
    const sha256 = digest(bytes);
    if (frame.sha256 && frame.sha256 !== sha256) failure('image_changed', '采样图片校验未通过。');
    if (prior) {
      const old = prior.all_frame_pts.find(f => f.frame_id === frame.frame_id);
      if (!old || old.output_pts_seconds !== frame.output_pts_seconds) failure('invalid_manifest', '加密帧的真实时间与粗采样不一致。');
    }
    frames.set(frame.frame_id, { ...frame, path, sha256 }); prev = frame.output_pts_seconds;
  }
  return frames;
}
function windowsFor(frames, width = 26, overlap = 2) {
  const result = []; let start = 0;
  while (start < frames.length) {
    const begin = frames[start].output_pts_seconds;
    let end = start + 1;
    while (end < frames.length && end - start < 40 && frames[end].output_pts_seconds < begin + width) end++;
    result.push(frames.slice(start, end));
    if (end === frames.length) break;
    let next = end;
    while (next > start + 1 && frames[next - 1].output_pts_seconds >= frames[end - 1].output_pts_seconds - overlap) next--;
    start = next;
  }
  return result;
}
function validateResponse(data, frames, model, promptVersion) {
  const allowed = new Set(frames.map(f => f.frame_id)), seen = new Set();
  if (!data || data.model !== model || data.prompt_version !== promptVersion || !plain(data.request_id)
    || !Array.isArray(data.observations) || data.observations.length > frames.length * 3
    || !Array.isArray(data.refine_frame_ids) || data.refine_frame_ids.length > frames.length)
    failure('invalid_response', '模型回复的版本或格式无效；尚未发布。');
  for (const o of data.observations) {
    const key = `${o.object_id}|${o.frame_id}`;
    if (!objectIDs.has(o.object_id) || !allowed.has(o.frame_id) || seen.has(key)
      || !['held', 'visible', 'uncertain'].includes(o.state) || !['clear', 'partial', 'unclear'].includes(o.visibility)
      || !['match', 'uncertain'].includes(o.identity) || !['held', 'touching', 'free', 'unclear'].includes(o.hand_relation)
      || typeof o.needs_review !== 'boolean' || !modelText(o.location_zh, 120) || !modelText(o.observation_zh, 300)
      || (o.state === 'held' && o.hand_relation !== 'held') || (o.state === 'visible' && o.hand_relation === 'held')
      || ((o.visibility !== 'clear' || o.identity !== 'match' || o.state === 'uncertain'
        || ['touching', 'unclear'].includes(o.hand_relation)) && !o.needs_review))
      failure('invalid_response', '模型引用了无效帧或不一致状态；尚未发布。');
    seen.add(key);
  }
  if (new Set(data.refine_frame_ids).size !== data.refine_frame_ids.length
    || data.refine_frame_ids.some(id => !allowed.has(id))) failure('invalid_response', '模型加密抽帧引用无效。');
  return data;
}
function mergeRanges(ranges) {
  const merged = [];
  for (const range of [...ranges].sort((a, b) => a.start - b.start || a.end - b.end)) {
    const last = merged.at(-1);
    if (last && range.start <= last.end) last.end = Math.max(last.end, range.end);
    else merged.push({ start: range.start, end: range.end, interval: 0.25 });
  }
  return merged;
}
function nearestSamplingFrames(allFrames, ranges) {
  const selected = new Map();
  for (const range of ranges) {
    const targets = Array.from({ length: Math.floor((range.end - range.start) / 0.25) + 1 }, (_, i) => range.start + i * 0.25);
    targets.push(range.end);
    for (const target of targets) {
      let lo = 0, hi = allFrames.length;
      while (lo < hi) { const mid = (lo + hi) >>> 1; if (allFrames[mid].output_pts_seconds < target) lo = mid + 1; else hi = mid; }
      const after = allFrames[Math.min(lo, allFrames.length - 1)], before = allFrames[Math.max(0, lo - 1)];
      const frame = Math.abs(before.output_pts_seconds - target) <= Math.abs(after.output_pts_seconds - target) ? before : after;
      selected.set(frame.frame_id, frame);
    }
  }
  return [...selected.values()].sort((a, b) => a.output_pts_seconds - b.output_pts_seconds);
}
function subtractRanges(requested, selected) {
  const remainder = [];
  for (const source of requested) {
    let cursor = source.start;
    for (const covered of selected) {
      if (covered.end <= cursor || covered.start >= source.end) continue;
      if (covered.start > cursor) remainder.push({ start: cursor, end: Math.min(covered.start, source.end), reason: 'refinement_budget' });
      cursor = Math.max(cursor, covered.end); if (cursor >= source.end) break;
    }
    if (cursor < source.end) remainder.push({ start: cursor, end: source.end, reason: 'refinement_budget' });
  }
  return remainder;
}
function planRefinement(responses, frames, manifest, { remainingWindows, maxRanges }) {
  const candidates = new Map();
  const add = (id, priority, reason) => {
    const old = candidates.get(id);
    if (!old) candidates.set(id, { frame_id: id, priority, reasons: [reason],
      start: Math.max(0, frames.get(id).output_pts_seconds - 2),
      end: Math.min(manifest.duration_seconds, frames.get(id).output_pts_seconds + 2), interval: 0.25 });
    else { old.priority = Math.min(old.priority, priority); if (!old.reasons.includes(reason)) old.reasons.push(reason); }
  };
  for (const id of responses.flatMap(r => r.refine_frame_ids)) add(id, 0, 'model_requested');
  const observations = responses.flatMap(r => r.observations);
  for (const o of observations) if (o.needs_review) add(o.frame_id, 2, 'unclear_observation');
  for (const objectID of objectIDs) {
    const sequence = observations.filter(o => o.object_id === objectID).sort((a, b) =>
      frames.get(a.frame_id).output_pts_seconds - frames.get(b.frame_id).output_pts_seconds);
    for (let i = 1; i < sequence.length; i++) {
      const a = sequence[i - 1], b = sequence[i];
      const delta = frames.get(b.frame_id).output_pts_seconds - frames.get(a.frame_id).output_pts_seconds;
      if (delta < 0 || delta > 5) continue;
      if (a.state !== b.state || a.hand_relation !== b.hand_relation) {
        add(a.frame_id, 1, 'state_or_hand_change'); add(b.frame_id, 1, 'state_or_hand_change');
      } else if (locationKey(a.location_zh) !== locationKey(b.location_zh)) {
        add(a.frame_id, 3, 'location_wording_change'); add(b.frame_id, 3, 'location_wording_change');
      }
    }
  }
  // Complete short candidate ranges are prioritized deterministically, without reference answers.
  // Later moments win ties because the product asks for the last supported sighting.
  const ordered = [...candidates.values()].sort((a, b) => a.priority - b.priority || b.start - a.start
    || a.frame_id.localeCompare(b.frame_id));
  let selected = [];
  const decisions = [];
  for (const candidate of ordered) {
    const proposed = mergeRanges([...selected, candidate]);
    const proposedFrames = nearestSamplingFrames(manifest.all_frame_pts, proposed);
    const windowCount = proposedFrames.length ? windowsFor(proposedFrames, DENSE_WINDOW_SECONDS, 0.5).length : 0;
    const accepted = proposed.length <= maxRanges && windowCount <= remainingWindows;
    decisions.push({ ...candidate, selected: accepted });
    if (accepted) selected = proposed;
  }
  const requested = mergeRanges(ordered), unrefined = subtractRanges(requested, selected);
  const sampledFrames = nearestSamplingFrames(manifest.all_frame_pts, selected);
  return { policy_version: AUTO_POLICY_VERSION, ranges: selected, requested_ranges: requested,
    unrefined_ranges: unrefined, decisions, expected_frame_ids: sampledFrames.map(f => f.frame_id),
    dense_window_count: sampledFrames.length ? windowsFor(sampledFrames, DENSE_WINDOW_SECONDS, 0.5).length : 0,
    coverage_status: unrefined.length ? 'bounded_with_unrefined_ranges' : 'sampled_plan_completed' };
}
function cacheMatches(cache, key, windowKey, frames) {
  return cache?.key === key && cache.window_key === windowKey && Array.isArray(cache.input_frames)
    && cache.input_frames.length === frames.length && cache.input_frames.every((f, i) => f.frame_id === frames[i].frame_id
      && f.output_pts_seconds === frames[i].output_pts_seconds && f.sha256 === frames[i].sha256);
}
function conservativeMerge(responses, frames, { clipID, key, model, promptVersion, samplingVersion, unrefinedRanges = [], policyVersion = AUTO_POLICY_VERSION }) {
  const grouped = new Map();
  for (const o of responses.flatMap(r => r.observations)) {
    const id = `${o.object_id}|${o.frame_id}`;
    if (!grouped.has(id)) grouped.set(id, []);
    grouped.get(id).push(o);
  }
  const merged = [...grouped.values()].map(group => {
    const first = group[0];
    const conflict = group.some(o => o.state !== first.state || o.hand_relation !== first.hand_relation
      || o.identity !== first.identity || locationKey(o.location_zh) !== locationKey(first.location_zh));
    const needsReview = conflict || group.some(o => o.needs_review || o.visibility !== 'clear' || o.identity !== 'match');
    return { ...first, state: conflict ? 'uncertain' : first.state, needs_review: needsReview,
      conflict, visibility: group.some(o => o.visibility !== 'clear') ? 'partial' : first.visibility,
      alternatives: conflict ? group.map(o => ({ state: o.state, location_zh: o.location_zh, hand_relation: o.hand_relation })) : [] };
  }).sort((a, b) => frames.get(a.frame_id).output_pts_seconds - frames.get(b.frame_id).output_pts_seconds
    || a.object_id.localeCompare(b.object_id));
  return merged.map(o => {
    const frame = frames.get(o.frame_id);
    const inUnrefinedRange = x => unrefinedRanges.some(r => frames.get(x.frame_id).output_pts_seconds >= r.start
      && frames.get(x.frame_id).output_pts_seconds <= r.end);
    const eligible = x => !inUnrefinedRange(x) && !x.needs_review && x.visibility === 'clear' && x.identity === 'match'
      && x.state !== 'uncertain' && ((x.state === 'held' && x.hand_relation === 'held') || (x.state === 'visible' && x.hand_relation === 'free'));
    const supports = eligible(o) ? merged.filter(other => other.object_id === o.object_id && other.frame_id !== o.frame_id
      && eligible(other) && other.state === o.state && other.hand_relation === o.hand_relation
      && locationKey(other.location_zh) === locationKey(o.location_zh)
      && Math.abs(frames.get(other.frame_id).output_pts_seconds - frame.output_pts_seconds) <= 2.1) : [];
    const clear = eligible(o) && supports.length > 0;
    const id = `auto-${key.slice(0, 16)}-${o.object_id}-${o.frame_id}`;
    return { id, object_id: o.object_id, clip_id: clipID, frame_id: o.frame_id,
      output_pts_seconds: frame.output_pts_seconds, source_frame_index: frame.source_frame_index,
      state: o.state, location_zh: o.location_zh, observation_zh: o.observation_zh,
      evidence_file: `data/auto/evidence/${id}-${frame.sha256}.jpg`, evidence_sha256: frame.sha256,
      provenance: 'vision_pipeline', review_status: 'auto_checked', human_ground_truth: null, run_key: key,
      confidence_qualitative: clear ? 'high' : 'medium', is_object_visible: o.visibility !== 'unclear',
      use_for_last_seen: clear, usable_as_clear_evidence: clear,
      limitations: ['自动提取且仅经程序检查，尚未经人工核验。',
        ...(clear ? ['相邻采样帧的一致判断不等于物品身份或位置真值。'] : ['单帧、不清楚或冲突的线索仅保留在时间线，不能作为最后清楚位置。']),
        ...(o.conflict ? ['同一帧的重复识别存在冲突，已保守降为未知。'] : []),
        ...(inUnrefinedRange(o) ? ['该时段因调用预算尚未加密抽帧，不能作为主答案。'] : []),
        ...(o.state === 'held' ? ['未据此推断之后的落点。'] : [])],
      model, prompt_version: promptVersion, sampling_version: samplingVersion, policy_version: policyVersion,
      support_frame_ids: clear ? [o.frame_id, ...supports.map(s => s.frame_id)].sort() : [],
      hand_relation: o.hand_relation, identity: o.identity, needs_review: !clear,
      ...(o.conflict ? { conflicting_observations: o.alternatives } : {}),
    };
  });
}

export function createAutoPipeline({ directory, bridgeConnectionFile, extractBinary, remuxBinary,
  bridgePairingCode, getPairing, bridgeURL = 'http://127.0.0.1:8766', fetchImpl = globalThis.fetch,
  extractImpl, remuxImpl, promptVersion = AUTO_PROMPT_VERSION, maxCalls = 30, maxRefineRanges = 12,
  refinementMode = 'standard',
  requestTimeoutMs = 300000, extractTimeoutMs = 120000, remuxTimeoutMs = 120000, onPublished = async () => {}, now = () => new Date().toISOString(),
} = {}) {
  if (!isAbsolute(directory || '')) throw new PipelineError('configuration', '自动索引目录必须是绝对路径。');
  const endpoint = new URL(bridgeURL);
  if (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1' || endpoint.username || endpoint.password
    || endpoint.pathname !== '/' || endpoint.search || endpoint.hash)
    throw new PipelineError('configuration', '识别连接只允许本机 127.0.0.1 服务。');
  if (!Number.isInteger(maxCalls) || maxCalls < 1 || maxCalls > 30) throw new PipelineError('configuration', '调用预算无效。');
  if (!['standard', 'context-dedup-v1'].includes(refinementMode)) throw new PipelineError('configuration', '补帧实验配置无效。');
  const policyVersion = refinementMode === 'standard' ? AUTO_POLICY_VERSION : 'bounded-refinement-context-dedup-v1';
  const workRoot = join(directory, '.auto-work'), indexPath = join(directory, 'data/auto-index.json');
  const jobs = new Map(); let active = null, current = null;
  function publicStatus(job = current) {
    if (!job) return { status: 'idle', phase: 'idle', message: '选择录像后可自动分析。' };
    return { job_id: job.id, status: job.status, phase: job.phase, message: job.message,
      progress: job.progress, calls: job.calls, cached_windows: job.cachedWindows,
      completed_windows: job.completedWindows, total_windows: job.totalWindows,
      ...(job.model ? { model: job.model } : {}), ...(job.key ? { run_key: job.key } : {}),
      ...(job.result ? { result: job.result } : {}), ...(job.errorCode ? { error_code: job.errorCode } : {}), ...(job.cleanupPending ? { cleanup_pending: true } : {}), ...(job.auditWarning ? { audit_warning: true } : {}),
      started_at: job.startedAt, updated_at: job.updatedAt };
  }
  function update(job, patch) { Object.assign(job, patch, { updatedAt: now() }); }
  async function pairing() {
    let code;
    if (getPairing) code = await getPairing();
    else if (bridgePairingCode) code = bridgePairingCode;
    else {
      if (!bridgeConnectionFile) failure('bridge_unavailable', '请显式配置你自己的本机视觉服务。');
      // Only the pairing field is retained. OAuth/access tokens are never inspected or forwarded.
      const statePath = resolve(bridgeConnectionFile);
      try { ({ pairingCode: code } = JSON.parse(await readFile(statePath, 'utf8'))); }
      catch { failure('bridge_unavailable', '无法读取本机连接码，请先打开视觉连接。'); }
    }
    if (!plain(code) || code.length > 512) failure('bridge_unavailable', '本机视觉连接码无效。');
    return code;
  }
  async function request(job, path, code, body) {
    checkAbort(job.controller.signal);
    const signal = AbortSignal.any([job.controller.signal, AbortSignal.timeout(requestTimeoutMs)]);
    try {
      const response = await fetchImpl(new URL(path, endpoint), { method: body ? 'POST' : 'GET',
        headers: { Authorization: `Bearer ${code}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}), signal, redirect: 'error' });
      if (!response.ok) failure('bridge_request_failed', `视觉服务未完成请求（${response.status}）；可重试并复用已完成窗口。`);
      const chunks = []; let byteLength = 0;
      if (!response.body) failure('invalid_response', '视觉服务没有返回内容。');
      const reader = response.body.getReader();
      try {
        while (true) {
          checkAbort(job.controller.signal);
          const { done, value } = await reader.read();
          if (done) break;
          byteLength += value.length;
          if (byteLength > 2 * 1024 * 1024) { await reader.cancel(); failure('invalid_response', '视觉服务回复超过上限。'); }
          chunks.push(Buffer.from(value));
        }
      } finally { reader.releaseLock(); }
      const text = Buffer.concat(chunks).toString('utf8');
      try { return JSON.parse(text); } catch { failure('invalid_response', '视觉服务回复不是有效 JSON。'); }
    } catch (error) {
      if (job.controller.signal.aborted) failure('cancelled', '任务已取消。');
      if (error instanceof PipelineError) throw error;
      failure(signal.aborted ? 'request_timeout' : 'bridge_unavailable', signal.aborted
        ? '视觉请求超时；可重试并复用已完成窗口。' : '视觉服务暂时无法连接；未发布自动索引。');
    }
  }
  async function execute(job, input) {
    const executionStarted = performance.now();
    const metrics = { version: 1, refinement_mode: refinementMode, started_at: job.startedAt,
      stage_wall_ms: {}, windows: [], token_usage: null, dollar_cost: null };
    let metricStage = 'preparing', metricStageStarted = executionStarted, auditRunRoot;
    function recordStage(next) {
      const currentTime = performance.now();
      metrics.stage_wall_ms[metricStage] = (metrics.stage_wall_ms[metricStage] || 0) + currentTime - metricStageStarted;
      metricStage = next; metricStageStarted = currentTime;
    }
    await mkdir(workRoot, { recursive: true, mode: 0o700 }); await chmod(workRoot, 0o700);
    const importDirectory = join(workRoot, 'imports', job.id);
    await mkdir(importDirectory, { recursive: true, mode: 0o700 });
    try {
    checkAbort(job.controller.signal);
    update(job, { phase: 'preparing', message: '正在固定原片副本并核对文件。', progress: 0.01 });
    const info = await stat(input.sourcePath);
    if (!info.isFile() || info.size < 1 || info.size > MAX_VIDEO_BYTES) failure('invalid_source', '录像应为 1 GiB 以内的本机文件。');
    const snapshot = join(importDirectory, `source${extname(input.sourcePath).toLowerCase()}`);
    await copyFile(input.sourcePath, snapshot);
    const sourceHash = await hashFile(snapshot), sourceAfterHash = await hashFile(input.sourcePath);
    if (sourceHash !== sourceAfterHash) failure('source_changed', '导入时录像发生变化，请录制结束后重试。');
    checkAbort(job.controller.signal);
    const code = await pairing(), connection = await request(job, '/v1/status', code);
    if (!connection.ready || !plain(connection.model)) failure('bridge_not_ready', '请先在本机视觉连接中完成登录并选择模型。');
    if (connection.object_prompt_version && connection.object_prompt_version !== promptVersion)
      failure('version_mismatch', '视觉服务的物品提示版本已变化，请更新分析程序。');
    job.model = connection.model;
    recordStage('coarse_extraction');
    update(job, { phase: 'extracting', message: '正在完整解码并按实际视频时间抽帧。', progress: 0.05 });
    const coarseRoot = join(importDirectory, 'coarse');
    const extractor = extractImpl || (args => runExtract(extractBinary, { ...args, timeoutMs: extractTimeoutMs }));
    const coarse = await extractor({ video: snapshot, output: coarseRoot, interval: 1, signal: job.controller.signal });
    const frames = await validateManifest(coarse, coarseRoot);
    recordStage('playback_preparation');
    let playbackSource = input.playbackPath || snapshot;
    if (!input.playbackPath && extname(snapshot).toLowerCase() === '.mov') {
      checkAbort(job.controller.signal);
      update(job, { phase: 'extracting', message: '正在无损封装并核对浏览器回放副本。' });
      playbackSource = join(importDirectory, 'playback.mp4');
      const reportPath = join(importDirectory, 'playback-verification.json');
      const remuxer = remuxImpl || (args => runRemux(remuxBinary || join(directory, 'tools/auto-remux'),
        { ...args, timeoutMs: remuxTimeoutMs }));
      const report = await remuxer({ video: snapshot, output: playbackSource, reportPath, signal: job.controller.signal });
      if (report?.version !== 1 || report.passed !== true || report.reencoded !== false
        || report.payloads_and_timestamps_identical !== true || report.frames !== coarse.decoded_frames
        || !Number.isFinite(report.duration_seconds) || Math.abs(report.duration_seconds - coarse.duration_seconds) > 0.001)
        failure('invalid_remux', '回放副本未通过逐帧内容和时间一致性校验，未发布。');
    }
    const semanticRun = { sourceHash, backend: endpoint.origin, model: job.model,
      promptVersion, samplingVersion: coarse.sampling_version, mergeVersion: 'two-frame-conservative-v1' };
    const legacyKey = digest(JSON.stringify(semanticRun));
    const key = digest(JSON.stringify({ ...semanticRun, policyVersion, maxCalls, maxRefineRanges }));
    job.key = key;
    const runRoot = join(workRoot, 'runs', key); await mkdir(runRoot, { recursive: true, mode: 0o700 });
    auditRunRoot = runRoot;
    const coarseWindows = windowsFor([...frames.values()]);
    if (coarseWindows.length > maxCalls) failure('budget_exceeded', '全片采样超过调用预算，尚未完成，未发布。');
    update(job, { phase: 'analyzing', message: '正在识别全片采样中的物品。', progress: 0.15, totalWindows: coarseWindows.length });
    const responses = [];
    async function analyzeWindow(window, stage) {
      checkAbort(job.controller.signal);
      const windowKey = digest(JSON.stringify({ stage, frames: window.map(f => [f.frame_id, f.output_pts_seconds, f.sha256]) }));
      const cachePath = join(runRoot, `${windowKey}.json`);
      let cache = await optionalJSON(cachePath), sourceRun = key;
      if (!cache && stage === 'coarse') {
        sourceRun = legacyKey;
        cache = await optionalJSON(join(workRoot, 'runs', legacyKey, `${windowKey}.json`));
      }
      if (cache) {
        if (!cacheMatches(cache, sourceRun, windowKey, window)) failure('invalid_cache', '缓存的输入帧或版本不一致，未复用。');
        validateResponse(cache.response, window, job.model, promptVersion);
        if (sourceRun !== key) await atomicJSON(cachePath, { ...cache, key, cache_source_run_key: sourceRun,
          policy_version: policyVersion, reused_at: now() });
        metrics.windows.push({ stage, cache_hit: true, frame_ids: window.map(f => f.frame_id),
          input_frame_count: window.length, request_wall_ms: null });
        job.cachedWindows++; job.completedWindows++; return cache.response;
      }
      if (job.calls >= maxCalls) failure('budget_exceeded', '已到本轮调用上限，仍有窗口待确认；未发布不完整结果。');
      const requestFrames = [];
      for (const frame of window) {
        const bytes = await readFile(frame.path);
        if (digest(bytes) !== frame.sha256) failure('image_changed', '识别前采样图片发生变化。');
        requestFrames.push({ frame_id: frame.frame_id, timestamp_seconds: frame.output_pts_seconds, jpeg_base64: bytes.toString('base64') });
      }
      const body = { duration_seconds: coarse.duration_seconds, expected_model: job.model, frames: requestFrames };
      if (Buffer.byteLength(JSON.stringify(body)) > 18 * 1024 * 1024) failure('budget_exceeded', '单次识别图片超过大小上限。');
      job.calls++;
      const requestStarted = performance.now();
      const requestMetrics = { stage, cache_hit: false, frame_ids: window.map(f => f.frame_id),
        input_frame_count: window.length, request_wall_ms: null, response_received: false };
      metrics.windows.push(requestMetrics);
      let rawResponse;
      try { rawResponse = await request(job, '/v1/object-observations', code, body); requestMetrics.response_received = true; }
      finally { requestMetrics.request_wall_ms = performance.now() - requestStarted; }
      const audit = { key, window_key: windowKey, stage, created_at: now(), model: job.model,
        input_frames: window.map(f => ({ frame_id: f.frame_id, output_pts_seconds: f.output_pts_seconds, sha256: f.sha256 })),
        response: rawResponse, request_metrics: requestMetrics };
      let response;
      try { response = validateResponse(rawResponse, window, job.model, promptVersion); }
      catch (error) {
        await atomicJSON(join(runRoot, `failed-${windowKey}-${randomUUID()}.json`), { ...audit,
          validation: 'failed', error_code: error.code || 'invalid_response' });
        throw error;
      }
      checkAbort(job.controller.signal);
      await atomicJSON(cachePath, { ...audit, validation: 'passed' });
      job.completedWindows++;
      update(job, { progress: Math.min(0.85, 0.15 + 0.7 * job.completedWindows / job.totalWindows),
        message: `已完成 ${job.completedWindows} / ${job.totalWindows} 个采样窗口。` });
      return response;
    }
    recordStage('coarse_analysis');
    for (const window of coarseWindows) responses.push(await analyzeWindow(window, 'coarse'));
    recordStage('refinement_planning');
    const plan = planRefinement(responses, frames, coarse, { remainingWindows: maxCalls - coarseWindows.length, maxRanges: maxRefineRanges });
    const coarseFrameIDs = new Set(frames.keys());
    const makeDenseWindows = sampled => {
      const original = windowsFor(sampled, DENSE_WINDOW_SECONDS, 0.5);
      return refinementMode === 'standard' ? original : contextDeduplicatedWindows(original, coarseFrameIDs);
    };
    if (refinementMode !== 'standard') {
      const expected = new Set(plan.expected_frame_ids);
      const plannedWindows = makeDenseWindows(coarse.all_frame_pts.filter(f => expected.has(f.frame_id)));
      plan.original_dense_window_count = plan.dense_window_count;
      plan.dense_window_count = plannedWindows.length;
      plan.policy_version = policyVersion;
      plan.planned_window_frame_ids = plannedWindows.map(w => w.map(f => f.frame_id));
      plan.reused_sampled_frame_ids = plan.expected_frame_ids.filter(id => !plannedWindows.some(w => w.some(f => f.frame_id === id)));
      plan.reuse_note = 'Reused frames were inspected in coarse or prior windows; not independently rechecked.';
    }
    const ranges = plan.ranges;
    await atomicJSON(join(runRoot, 'refinement-plan.json'), { ...plan, coarse_window_count: coarseWindows.length });
    if (ranges.length) {
      recordStage('refinement_extraction');
      update(job, { phase: 'refining', message: '正在给预算内的优先时段补充画面；未覆盖区间会明确保留为待确认。' });
      const refineRoot = join(importDirectory, 'refine');
      const dense = await extractor({ video: snapshot, output: refineRoot, interval: 0.25, ranges, signal: job.controller.signal });
      const denseFrames = await validateManifest(dense, refineRoot, coarse);
      if (JSON.stringify([...denseFrames.keys()]) !== JSON.stringify(plan.expected_frame_ids))
        failure('sampling_plan_mismatch', '补帧实际帧与预算计划不一致，未发布。');
      for (const [id, frame] of denseFrames) {
        if (frames.has(id) && frames.get(id).sha256 !== frame.sha256) failure('image_changed', '同一帧在两轮抽帧中图像不同。');
        frames.set(id, frame);
      }
      const denseWindows = makeDenseWindows([...denseFrames.values()]);
      if (plan.planned_window_frame_ids && JSON.stringify(denseWindows.map(w => w.map(f => f.frame_id))) !== JSON.stringify(plan.planned_window_frame_ids))
        failure('sampling_plan_mismatch', '实验窗口与预先确认的帧计划不一致，未发布。');
      job.totalWindows += denseWindows.length;
      if (denseWindows.length !== plan.dense_window_count || job.totalWindows > maxCalls)
        failure('sampling_plan_mismatch', '实际窗口与预先确认的调用预算不一致，未发布。');
      recordStage('refinement_analysis');
      for (const window of denseWindows) responses.push(await analyzeWindow(window, 'refine'));
    }
    checkAbort(job.controller.signal);
    recordStage('publishing');
    update(job, { phase: 'publishing', message: '正在核对证据并发布独立自动索引。', progress: 0.9 });
    const clipID = `${input.clipId || `auto-${sourceHash.slice(0, 16)}`}-${key.slice(0, 8)}`;
    const observations = conservativeMerge(responses, frames, { clipID, key, model: job.model,
      promptVersion, samplingVersion: coarse.sampling_version, unrefinedRanges: plan.unrefined_ranges, policyVersion });
    const videoRoot = join(directory, 'data/auto/videos'), evidenceRoot = join(directory, 'data/auto/evidence');
    await mkdir(videoRoot, { recursive: true }); await mkdir(evidenceRoot, { recursive: true });
    const playbackStats = await stat(playbackSource);
    if (!playbackStats.isFile() || playbackStats.size < 1 || playbackStats.size > MAX_VIDEO_BYTES)
      failure('invalid_playback', '回放副本无效。');
    const playbackHash = await hashFile(playbackSource);
    const videoFile = `data/auto/videos/${key}-${playbackHash}${extname(playbackSource).toLowerCase()}`;
    await copyFile(playbackSource, join(directory, videoFile));
    if (await hashFile(join(directory, videoFile)) !== playbackHash) failure('copy_failed', '回放文件校验失败。');
    for (const o of observations) {
      const frame = frames.get(o.frame_id);
      await copyFile(frame.path, join(directory, o.evidence_file));
      if (await hashFile(join(directory, o.evidence_file)) !== o.evidence_sha256) failure('copy_failed', '证据图片校验失败。');
    }
    const firstFrame = [...frames.values()].sort((a, b) => a.output_pts_seconds - b.output_pts_seconds)[0];
    const poster = observations.find(o => o.usable_as_clear_evidence)?.evidence_file || `data/auto/evidence/${key}-${firstFrame.sha256}-poster.jpg`;
    if (!observations.some(o => o.evidence_file === poster)) await copyFile(firstFrame.path, join(directory, poster));
    const clip = { id: clipID, label: input.label || '自动分析录像', started_at: input.startedAt,
      duration_seconds: coarse.duration_seconds, video_file: videoFile, poster_file: poster,
      source_video_sha256: sourceHash, playback_sha256: playbackHash, run_key: key,
      quality_note: '自动抽帧与程序检查，物品判断尚需核对。', decoded_frames: coarse.decoded_frames,
      sampled_frames: frames.size, model: job.model, prompt_version: promptVersion,
      sampling_version: coarse.sampling_version, policy_version: policyVersion,
      unrefined_ranges: plan.unrefined_ranges, coverage_status: plan.coverage_status,
      analysis_status: plan.unrefined_ranges.length ? 'completed_with_unrefined_ranges' : 'completed',
      review_coverage: plan.unrefined_ranges.length
        ? `全片约每秒一帧，优先时段补帧；另有 ${plan.unrefined_ranges.length} 段受预算限制尚未进一步细查，后续去向待确认。`
        : '全片约每秒一帧，选定的疑似动作时段已补帧；不保证捕获全部短暂动作。' };
    const old = await optionalJSON(indexPath);
    if (old && (old.schema_version !== 1 || old.provenance !== 'vision_pipeline'
      || !Array.isArray(old.clips) || !Array.isArray(old.observations))) failure('invalid_index', '已有自动索引格式不匹配，未覆盖。');
    const previousClips = (old?.clips || []).filter(c => c.source_video_sha256 !== sourceHash && c.run_key !== key && c.id !== clipID);
    const previousIDs = new Set(previousClips.map(c => c.id));
    const data = { schema_version: 1, title: 'RoboMemo · 自动识别', indexed_at: now(), objects: structuredClone(OBJECTS),
      provenance: 'vision_pipeline', ground_truth_status: 'user_ground_truth_not_provided',
      limitations: ['自动识别结果仅通过格式、帧引用和跨帧一致性检查，尚未经人工核验。',
        '采样可能漏掉短暂动作；缺席、空抽屉或镜头移开均不能确定物品去向。'],
      clips: [...previousClips, clip], observations: [...(old?.observations || []).filter(o => previousIDs.has(o.clip_id)), ...observations] };
    const audit = { key, source_video_sha256: sourceHash, completed_at: now(), calls: job.calls,
      cached_windows: job.cachedWindows, total_windows: job.totalWindows, ranges,
      policy_version: policyVersion, unrefined_ranges: plan.unrefined_ranges, coverage_status: plan.coverage_status,
      frame_count: frames.size, observations, clip };
    checkAbort(job.controller.signal);
    validateDataset(data);
    if (Buffer.byteLength(JSON.stringify(data, null, 2) + '\n') > MAX_INDEX_BYTES)
      failure('index_too_large', '自动索引超过 16 MiB 发布上限，未覆盖已有索引。');
    await atomicJSON(join(runRoot, 'prepared.json'), { ...audit, publication_state: 'prepared' });
    checkAbort(job.controller.signal);
    await atomicJSON(indexPath, data);
    try {
      await atomicJSON(join(runRoot, 'completed.json'), { ...audit, publication_state: 'published' });
      await rm(join(runRoot, 'prepared.json'), { force: true });
    } catch { update(job, { auditWarning: true }); }
    // Publication is committed before notification. A UI refresh failure cannot relabel it as uncommitted.
    let refreshFailed = false;
    try { await onPublished({ indexPath, clip_id: clipID, key }); } catch { refreshFailed = true; }
    update(job, { status: 'completed', phase: 'completed', progress: 1,
      message: refreshFailed ? '自动索引已保存；请刷新页面查看。' : plan.unrefined_ranges.length
        ? `自动分析已完成；另有 ${plan.unrefined_ranges.length} 段未进一步细查，相关去向仍待确认。`
        : '自动识别完成，结果已加入独立自动索引。',
      result: { clip_id: clipID, observation_count: observations.length,
        clear_candidate_count: observations.filter(o => o.usable_as_clear_evidence).length,
        needs_review_count: observations.filter(o => o.needs_review).length, duration_seconds: coarse.duration_seconds,
        sampled_frames: frames.size, decoded_frames: coarse.decoded_frames,
        unrefined_range_count: plan.unrefined_ranges.length, coverage_status: plan.coverage_status } });
    } finally {
      recordStage('cleanup');
      // Remove only this job's transient original and samples; retain run caches and published evidence.
      try { await rm(importDirectory, { recursive: true, force: true }); }
      catch { update(job, { cleanupPending: true }); }
      recordStage('finished');
      metrics.elapsed_wall_ms = performance.now() - executionStarted;
      metrics.outcome = job.status === 'completed' ? 'completed' : job.controller.signal.aborted ? 'cancelled' : 'interrupted_before_completion';
      metrics.planned_window_input_images = metrics.windows.reduce((n, w) => n + w.input_frame_count, 0);
      const sent = metrics.windows.filter(w => !w.cache_hit);
      metrics.attempted_submission_images = sent.reduce((n, w) => n + w.input_frame_count, 0);
      metrics.attempted_submission_unique_images = new Set(sent.flatMap(w => w.frame_ids)).size;
      metrics.successful_response_images = sent.filter(w => w.response_received).reduce((n, w) => n + w.input_frame_count, 0);
      metrics.cached_window_count = metrics.windows.filter(w => w.cache_hit).length;
      metrics.accounting_note = 'Attempted submissions refer to local bridge requests, not confirmed upstream billing. Request time includes transport and response parsing, not pure model latency. Cached inputs are not new submissions. No estimated token or dollar cost.';
      if (auditRunRoot) {
        try { await atomicJSON(join(auditRunRoot, `execution-metrics-${job.id}.json`), metrics); }
        catch { update(job, { auditWarning: true }); }
      }
    }
  }
  return {
    getStatus: () => publicStatus(),
    start(input) {
      if (active) throw new PipelineError('busy', '已有一段录像正在分析，请等它完成或取消。');
      if (!input || !isAbsolute(input.sourcePath || '') || !['.mov', '.mp4'].includes(extname(input.sourcePath).toLowerCase())
        || !Number.isFinite(Date.parse(input.startedAt)) || (input.clipId !== undefined && !fileID(input.clipId))
        || (input.label !== undefined && !cleanText(input.label, 160))
        || (input.playbackPath !== undefined && (!isAbsolute(input.playbackPath)
          || !['.mov', '.mp4'].includes(extname(input.playbackPath).toLowerCase()))))
        throw new PipelineError('invalid_input', '请选择本机 MOV/MP4 录像，并提供原始录制时间。');
      const job = { id: randomUUID(), status: 'running', phase: 'queued', message: '分析任务已排队。',
        progress: 0, calls: 0, cachedWindows: 0, completedWindows: 0, totalWindows: 0,
        startedAt: now(), updatedAt: now(), controller: new AbortController() };
      jobs.set(job.id, job); active = job; current = job;
      job.promise = Promise.resolve().then(() => execute(job, { ...input })).catch(error => {
        const cancelled = job.controller.signal.aborted || error.code === 'cancelled';
        update(job, { status: cancelled ? 'cancelled' : 'failed', phase: cancelled ? 'cancelled' : 'failed',
          errorCode: cancelled ? 'cancelled' : (error instanceof PipelineError ? error.code : 'local_error'),
          message: cancelled ? '任务已取消，已完成窗口可在重试时复用。' : (error instanceof PipelineError
            ? error.message : '本机处理未完成，未发布新的自动索引；可重试。') });
      }).finally(() => { if (active === job) active = null; });
      return { job_id: job.id, status: job.status };
    },
    cancel(jobID) {
      const job = jobs.get(jobID);
      if (!job) throw new PipelineError('unknown_job', '找不到这次分析任务。');
      if (job.status === 'running') job.controller.abort();
      return publicStatus(job);
    },
    async wait(jobID) {
      const job = jobs.get(jobID);
      if (!job) throw new PipelineError('unknown_job', '找不到这次分析任务。');
      await job.promise; return publicStatus(job);
    },
  };
}
