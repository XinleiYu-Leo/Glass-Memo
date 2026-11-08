// The index is evidence, not ground truth. Querying it never infers off-camera state.
export class InputError extends Error {}

const states = new Set(['held', 'placed', 'visible', 'uncertain']);
const requiredText = (x) => typeof x === 'string' && x.trim().length > 0;
const relativeFile = (x) => requiredText(x) && !x.startsWith('/') && !x.includes('\\')
  && !x.split('/').some(part => !part || part === '.' || part === '..');
const provenanceLabel = (automatic) => automatic
  ? '自动识别 · 程序检查，待本人确认' : '离线画面索引 · AI 核验，待本人确认';
function validProvenance(o, clip) {
  if (o.provenance === 'synthetic_fixture') return o.review_status === 'synthetic';
  if (o.provenance === 'codex_visual_review') return o.review_status === 'ai_reviewed';
  if (o.provenance !== 'vision_pipeline' || o.review_status !== 'auto_checked'
    || o.human_ground_truth !== null || ![o.model, o.prompt_version, o.sampling_version, o.frame_id, o.run_key].every(requiredText)
    || o.run_key !== clip.run_key
    || ['model', 'prompt_version', 'sampling_version'].some(key => o[key] !== clip[key])
    || (clip.policy_version !== undefined && o.policy_version !== clip.policy_version)
    || !Array.isArray(o.support_frame_ids)
    || !o.support_frame_ids.every(requiredText) || typeof o.needs_review !== 'boolean'
    || !['held', 'visible', 'uncertain'].includes(o.state)) return false;
  if (o.use_for_last_seen || o.usable_as_clear_evidence) {
    if (clip.unrefined_ranges?.some(range => o.output_pts_seconds >= range.start && o.output_pts_seconds <= range.end)) return false;
    return o.use_for_last_seen && o.usable_as_clear_evidence && o.is_object_visible && !o.needs_review
      && o.identity === 'match' && new Set(o.support_frame_ids).size >= 2 && o.support_frame_ids.includes(o.frame_id)
      && ((o.state === 'held' && o.hand_relation === 'held') || (o.state === 'visible' && o.hand_relation === 'free'));
  }
  return true;
}

export function validateDataset(data) {
  if (data?.schema_version !== 1 || !Array.isArray(data.objects) || !Array.isArray(data.clips)
    || !Array.isArray(data.observations)) throw new InputError('索引格式不受支持。');
  const objectIDs = new Set(), clipIDs = new Set(), evidenceIDs = new Set();
  for (const object of data.objects) {
    if (!requiredText(object.id) || objectIDs.has(object.id) || !requiredText(object.label_zh)
      || !Array.isArray(object.aliases) || !object.aliases.length || !object.aliases.every(requiredText))
      throw new InputError('物品索引无效。');
    objectIDs.add(object.id);
  }
  for (const clip of data.clips) {
    if (!requiredText(clip.id) || clipIDs.has(clip.id) || !requiredText(clip.label)
      || !Number.isFinite(Date.parse(clip.started_at)) || !Number.isFinite(clip.duration_seconds)
      || clip.duration_seconds <= 0 || !relativeFile(clip.video_file) || !relativeFile(clip.poster_file))
      throw new InputError('录像索引无效。');
    if (clip.unrefined_ranges !== undefined && (!Array.isArray(clip.unrefined_ranges)
      || clip.unrefined_ranges.some(range => !Number.isFinite(range?.start) || !Number.isFinite(range?.end)
        || range.start < 0 || range.end < range.start || range.end > clip.duration_seconds)))
      throw new InputError('录像的补帧覆盖范围无效。');
    clipIDs.add(clip.id);
  }
  const clips = new Map(data.clips.map(c => [c.id, c]));
  for (const observation of data.observations) {
    if (!requiredText(observation.id) || evidenceIDs.has(observation.id)
      || !objectIDs.has(observation.object_id) || !clips.has(observation.clip_id)
      || !Number.isFinite(observation.output_pts_seconds) || observation.output_pts_seconds < 0
      || observation.output_pts_seconds > clips.get(observation.clip_id).duration_seconds
      || !requiredText(observation.observation_zh) || !requiredText(observation.location_zh)
      || !states.has(observation.state) || !relativeFile(observation.evidence_file)
      || typeof observation.is_object_visible !== 'boolean' || typeof observation.use_for_last_seen !== 'boolean'
      || typeof observation.usable_as_clear_evidence !== 'boolean'
      || !validProvenance(observation, clips.get(observation.clip_id))
      || !Array.isArray(observation.limitations) || !observation.limitations.every(requiredText))
      throw new InputError('一条观察缺少有效的画面、时间或来源。');
    evidenceIDs.add(observation.id);
  }
  const automaticFrames = new Map(data.observations.filter(o => o.provenance === 'vision_pipeline')
    .map(o => [`${o.clip_id}|${o.object_id}|${o.frame_id}`, o]));
  for (const o of data.observations.filter(o => o.provenance === 'vision_pipeline' && o.usable_as_clear_evidence)) {
    if (!o.support_frame_ids.every(id => {
      const support = automaticFrames.get(`${o.clip_id}|${o.object_id}|${id}`);
      return support && support.run_key === o.run_key && support.state === o.state
        && support.hand_relation === o.hand_relation && support.usable_as_clear_evidence
        && Math.abs(support.output_pts_seconds - o.output_pts_seconds) <= 2.1;
    })) throw new InputError('自动观察缺少同一轮分析中的相邻画面支持。');
  }
  return data;
}

export function normalizeQuery(query) {
  if (typeof query !== 'string' || query.trim().length > 300 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(query))
    throw new InputError('请输入 300 字以内的物品问题。');
  return query.normalize('NFKC').trim().toLocaleLowerCase('en-US');
}

export function recognizes(query, alias) {
  const a = alias.normalize('NFKC').toLocaleLowerCase('en-US');
  if (/^[a-z0-9\s-]+$/u.test(a)) {
    const escaped = a.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return new RegExp(`(?:^|[^a-z0-9])${escaped}(?=$|[^a-z0-9])`, 'u').test(query);
  }
  return query.includes(a);
}

function negates(query, alias) {
  const a = alias.normalize('NFKC').toLocaleLowerCase('en-US');
  const escaped = a.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const ending = /^[a-z0-9\s-]+$/u.test(a) ? '(?=$|[^a-z0-9])' : '';
  return new RegExp(`(?:不是|不要找|不用找)\\s*${escaped}${ending}`, 'u').test(query)
    || new RegExp(`\\bnot\\s+(?:the\\s+|my\\s+)?${escaped}${ending}`, 'u').test(query);
}

const eventTime = (o, clips) => Date.parse(clips.get(o.clip_id).started_at) + o.output_pts_seconds * 1000;
export function publicObservation(observation, clip, { source = 'reviewed' } = {}) {
  const { evidence_file, ...safe } = observation;
  const prefix = source === 'auto' ? '/auto' : '';
  return { ...safe, provenance_label: observation.provenance === 'synthetic_fixture'
      ? '合成测试数据 · 非真实观察' : provenanceLabel(observation.provenance === 'vision_pipeline'),
    evidence_url: `${prefix}/evidence/${encodeURIComponent(observation.id)}`,
    clip_label: clip.label, clip_started_at: clip.started_at,
    duration_seconds: clip.duration_seconds, video_url: `${prefix}/media/${encodeURIComponent(clip.id)}` };
}

export function catalog(data, { source = 'reviewed' } = {}) {
  const clips = new Map(data.clips.map(c => [c.id, c]));
  const prefix = source === 'auto' ? '/auto' : '';
  const label = data.provenance === 'synthetic_fixture' ? '合成测试数据 · 非真实观察' : provenanceLabel(source === 'auto');
  return {
    title: data.title || 'RoboMemo · 找物品',
    source, provenance_label: label,
    dataset: { clip_count: data.clips.length, observation_count: data.observations.length,
      provenance_label: label, limitations: data.limitations || [],
      indexed_at: data.indexed_at, query_method: '物品别名检索与时间排序' },
    objects: data.objects,
    clips: data.clips.map(({ video_file, poster_file, ...clip }) => ({ ...clip,
      video_url: `${prefix}/media/${encodeURIComponent(clip.id)}`, poster_url: `${prefix}/poster/${encodeURIComponent(clip.id)}` })),
    observations: data.observations.map(o => publicObservation(o, clips.get(o.clip_id), { source })),
  };
}

export function search(data, input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new InputError('查询格式无效。');
  const query = normalizeQuery(input.query);
  const source = input.source ?? 'reviewed';
  if (!['auto', 'reviewed'].includes(source)) throw new InputError('请选择有效的证据来源。');
  const clipID = input.clip_id ?? null;
  const clips = new Map(data.clips.map(c => [c.id, c]));
  if (clipID !== null && (typeof clipID !== 'string' || !clips.has(clipID))) throw new InputError('请选择已有录像。');
  const recognized = data.objects.filter(o => o.aliases.some(a => recognizes(query, a)));
  const base = { query: input.query.trim(), recognized_objects: recognized.map(o => o.id), answers: [] };
  const labels = data.objects.map(o => o.label_zh);
  if (!query) return { ...base, status: 'choose_object', message: labels.length
    ? `先选一个物品，或输入“${labels[0]}最后在哪”。` : '当前索引还没有可查询的物品。' };
  if (!recognized.length) return { ...base, status: 'no_evidence',
    message: `已建立的索引里没有这个物品的可确认记录。${labels.length ? `目前可查询：${labels.join('、')}。` : '当前索引还没有可查询的物品。'}这不代表其他物品没有出现。` };
  // Avoid answering a negated object as if it were the requested object.
  if (recognized.some(o => [o.label_zh, ...o.aliases].some(a => negates(query, a))))
    return { ...base, status: 'choose_object', message: '请直接选择或输入这次要找的物品。' };

  const answers = [];
  for (const object of recognized) {
    const all = data.observations.filter(o => o.object_id === object.id && (!clipID || o.clip_id === clipID))
      .sort((a, b) => eventTime(b, clips) - eventTime(a, clips) || b.output_pts_seconds - a.output_pts_seconds);
    const clear = all.filter(o => o.state !== 'uncertain' && o.is_object_visible === true
      && o.use_for_last_seen === true && o.usable_as_clear_evidence === true);
    const latest = clear[0];
    if (!latest) continue;
    const observation = publicObservation(latest, clips.get(latest.clip_id), { source });
    const placement = clear.find(o => o.state === 'placed' && o.id !== latest.id);
    const newerUncertain = all.some(o => (o.state === 'uncertain' || o.usable_as_clear_evidence === false)
      && eventTime(o, clips) > eventTime(latest, clips));
    let headline;
    if (latest.state === 'held') headline = `${object.label_zh}最后清楚出现在手中，之后放在哪里尚未确认。`;
    else if (latest.state === 'placed') headline = newerUncertain
      ? `${object.label_zh}较早清楚出现在${latest.location_zh}，后续去向未确认。`
      : `${object.label_zh}最后清楚出现在${latest.location_zh}。`;
    else headline = newerUncertain
      ? `${object.label_zh}较早清楚出现在${latest.location_zh}，后续去向未确认。`
      : `${object.label_zh}的最新清楚画面：${latest.location_zh}。`;
    if (latest.provenance === 'vision_pipeline' && latest.state !== 'held')
      headline = `自动线索：${object.label_zh}曾清楚出现在${latest.location_zh}，后续去向未确认。`;
    const caveat = [
      latest.provenance === 'vision_pipeline'
        ? '这是自动识别的候选线索，尚未经人工确认；不代表物品现在仍在那里。'
        : latest.provenance === 'synthetic_fixture' ? '这是人为编写的合成示例，只用于测试检索和状态规则。'
        : '这是 AI 复核画面中的最后清楚记录，不代表物品现在仍在那里。',
      ...(newerUncertain ? ['后面还有无法确认的画面，不能据此补全去向。'] : []),
      ...latest.limitations,
    ].filter((v, i, a) => a.indexOf(v) === i).join(' ');
    answers.push({ object_id: object.id, label_zh: object.label_zh, headline, observation,
      latest_placement: placement ? publicObservation(placement, clips.get(placement.clip_id), { source }) : null,
      history: all.map(o => publicObservation(o, clips.get(o.clip_id), { source })), caveat });
  }
  return { ...base, answers, status: answers.length ? 'found' : 'no_evidence',
    message: answers.length ? '已找到可回看的画面证据。' : '所选录像中没有这个物品的清楚记录，请尝试全部录像。' };
}

// Single-range support is required for browser seeking. Reject ambiguous ranges.
export function parseRange(value, size) {
  if (!value) return null;
  const match = /^bytes=(\d*)-(\d*)$/u.exec(value);
  if (!match || (!match[1] && !match[2]) || !Number.isSafeInteger(size) || size <= 0) throw new InputError('无效的视频范围。');
  let start, end;
  if (!match[1]) {
    const count = Number(match[2]);
    if (!Number.isSafeInteger(count) || count <= 0) throw new InputError('无效的视频范围。');
    start = Math.max(0, size - count); end = size - 1;
  } else {
    start = Number(match[1]); end = match[2] ? Number(match[2]) : size - 1;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start >= size || end < start)
      throw new InputError('无效的视频范围。');
    end = Math.min(end, size - 1);
  }
  return { start, end };
}
