'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    catalog: null, source: 'reviewed', clipId: null, query: '', answers: [], answerIndex: 0,
    observation: null, evidence: null, searchController: null,
    searchVersion: 0, mediaVersion: 0, mediaTimer: null, retryAction: null,
    catalogController: null, catalogVersion: 0,
    analysis: null, analysisConnected: false, analysisController: null,
    analysisVersion: 0, analysisTimer: null, analysisAction: '',
    analysisActionError: '', analysisConnectionError: '', analysisClipsSignature: '',
    watchedJobId: null, completedJobId: null, stopped: false,
  };
  const video = $('evidence-video');
  const text = (value, fallback = '') => typeof value === 'string' ? value : fallback;
  const list = (value) => Array.isArray(value) ? value : [];
  const setText = (id, value) => { $(id).textContent = text(value); };
  const show = (id, visible) => { $(id).hidden = !visible; };
  const element = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = String(content);
    return node;
  };

  function localMediaURL(value) {
    if (typeof value !== 'string' || !value.trim()) return null;
    try {
      const url = new URL(value, window.location.href);
      return ['http:', 'https:'].includes(url.protocol) && url.origin === window.location.origin ? url.href : null;
    } catch { return null; }
  }

  function formatTime(value, precise = false) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    const minutes = Math.floor(seconds / 60);
    const remainder = precise ? (Math.floor((seconds % 60) * 10) / 10).toFixed(1).padStart(4, '0') : String(Math.floor(seconds % 60)).padStart(2, '0');
    return `${String(minutes).padStart(2, '0')}:${remainder}`;
  }

  function formatDate(value) {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime())) return '';
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'UTC', month: 'numeric', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(date);
  }

  function limitations(value) {
    return Array.isArray(value) ? value.filter((v) => typeof v === 'string').join('；') : text(value);
  }

  function clipFor(observation) {
    return list(state.catalog?.clips).find((clip) => clip.id === observation?.clip_id) || {};
  }

  function completeObservation(observation) {
    if (!observation || typeof observation !== 'object' || Array.isArray(observation)) return null;
    const clip = clipFor(observation);
    return {
      ...observation,
      clip_label: text(observation.clip_label, text(clip.label, '录像')),
      clip_started_at: observation.clip_started_at || clip.started_at,
      video_url: observation.video_url || clip.video_url,
      duration_seconds: observation.duration_seconds ?? clip.duration_seconds,
    };
  }

  function observationKey(observation) {
    return observation?.id || `${observation?.clip_id}:${observation?.object_id}:${observation?.output_pts_seconds}`;
  }

  function observationCaption(observation) {
    return [observation?.clip_label, formatTime(observation?.output_pts_seconds, true)].filter(Boolean).join(' · ');
  }

  function evidenceQualifier(observation) {
    if (observation?.is_object_visible === false) return '场景辅助 · 此刻未见物品';
    if (observation?.use_for_last_seen === false || observation?.usable_as_clear_evidence === false) return '辅助线索 · 需结合前后画面确认';
    return '';
  }

  function announce(message) { setText('live-status', message); }

  function showError(title, message, retry) {
    setText('global-error-title', title);
    setText('global-error-text', message);
    state.retryAction = retry;
    show('global-error', true);
    show('global-retry', typeof retry === 'function');
    announce(`${title}。${message}`);
  }

  async function readJSON(url, options = {}, controller = new AbortController()) {
    const { timeoutMs = 15000, ...fetchOptions } = options;
    let timedOut = false;
    const timer = window.setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    try {
      const response = await fetch(url, { ...fetchOptions, signal: controller.signal, cache: 'no-store' });
      const body = await response.text();
      let payload;
      try { payload = JSON.parse(body); } catch {
        throw new Error(response.ok ? '本地服务返回了无法读取的数据，请重新尝试。' : `请求未完成（HTTP ${response.status}），请确认演示服务仍在运行。`);
      }
      if (!response.ok) {
        const detail = text(payload.message) || text(payload.detail) || text(payload.error);
        throw new Error(detail || `请求未完成（HTTP ${response.status}），请稍后重试。`);
      }
      return payload;
    } catch (error) {
      if (timedOut) throw new Error(`读取超过了 ${Math.round(timeoutMs / 1000)} 秒，请确认本地演示服务仍在运行后重试。`);
      if (error.name === 'AbortError') throw error;
      if (error instanceof TypeError) throw new Error('暂时连接不上本地演示服务。请确认服务已启动，然后重新尝试。');
      throw error;
    } finally { window.clearTimeout(timer); }
  }

  function setLoading(isLoading) {
    show('loading-state', isLoading);
    $('answer-content').setAttribute('aria-busy', String(isLoading));
    $('search-submit').disabled = isLoading;
    $('search-submit').querySelector('span').textContent = isLoading ? '查找中…' : '查找线索';
    if (isLoading) {
      show('answer-content', false);
      show('empty-state', false);
    }
  }

  function showEmpty(title, message, actions = []) {
    show('answer-content', false);
    show('loading-state', false);
    show('empty-state', true);
    setText('empty-title', title);
    setText('empty-message', message);
    const container = $('empty-actions');
    container.replaceChildren();
    actions.forEach(({ label, action }) => {
      const button = element('button', 'button button-secondary', label);
      button.type = 'button';
      button.addEventListener('click', action);
      container.append(button);
    });
  }

  function renderFilters() {
    const clips = list(state.catalog?.clips);
    const observations = list(state.catalog?.observations);
    const container = $('clip-filters');
    container.replaceChildren();
    setText('clip-count', String(clips.length));

    function filterButton(clip, index) {
      const all = !clip;
      const button = element('button', `clip-filter${all ? ' all-clips' : ''}`);
      button.type = 'button';
      button.setAttribute('aria-pressed', String(all ? state.clipId === null : state.clipId === clip.id));
      const label = all ? '全部录像' : text(clip.label, `录像 ${index + 1}`);
      button.setAttribute('aria-label', `检索${label}`);
      if (all) button.append(element('span', 'all-clips-icon', '▤'));
      else {
        const posterURL = localMediaURL(clip.poster_url);
        if (posterURL) {
          const img = element('img', 'clip-poster');
          img.alt = ''; img.loading = 'lazy'; img.src = posterURL;
          img.addEventListener('error', () => img.replaceWith(element('span', 'clip-number', String(index + 1).padStart(2, '0'))), { once: true });
          button.append(img);
        } else button.append(element('span', 'clip-number', String(index + 1).padStart(2, '0')));
      }
      const copy = element('span', 'clip-content');
      copy.append(element('span', 'clip-title', label));
      const count = all ? observations.length : observations.filter((item) => item.clip_id === clip.id).length;
      copy.append(element('span', 'clip-subtitle', all ? `${clips.length} 段 · ${count} 条画面线索` : `${formatTime(clip.duration_seconds)} · ${count} 条线索`));
      if (!all && clip.quality_note) copy.append(element('span', 'clip-quality', text(clip.quality_note)));
      button.append(copy, element('span', 'clip-check', '✓'));
      button.addEventListener('click', () => {
        state.clipId = all ? null : clip.id;
        renderFilters();
        const query = $('query').value.trim() || state.query;
        if (query) search(query);
        else {
          clearPlayer();
          showEmpty('想查这段里的哪个物品？', '输入物品名称，或点一下上方的快捷问题。');
        }
      });
      return button;
    }

    container.append(filterButton(null, -1));
    clips.forEach((clip, index) => container.append(filterButton(clip, index)));
    const selected = clips.find((clip) => clip.id === state.clipId);
    setText('search-scope', selected ? `范围：${text(selected.label, '当前录像')}` : `范围：全部 ${clips.length} 段录像`);
  }

  function renderQuickQuestions() {
    const container = document.querySelector('.quick-questions');
    container.replaceChildren(element('span', 'quick-label', '试着问'));
    list(state.catalog?.objects).forEach((object) => {
      const label = text(object.label_zh).trim();
      if (!label) return;
      const query = `${label}最后在哪儿看到？`;
      const button = element('button', 'quick-question', `${label}在哪儿？`);
      button.type = 'button';
      button.dataset.query = query;
      button.addEventListener('click', () => search(query));
      container.append(button);
    });
  }

  function provenanceLabel(observation) {
    return text(observation?.provenance_label) || text(state.catalog?.provenance_label)
      || (state.source === 'auto' ? '自动识别 · 待确认' : '复核参考 · 待本人确认');
  }

  function clearResults() {
    state.searchController?.abort();
    state.searchController = null;
    state.searchVersion += 1;
    state.answers = [];
    state.observation = null;
    const image = $('evidence-image');
    image.onload = null; image.onerror = null;
    image.removeAttribute('src');
    $('dialog-image').removeAttribute('src');
    if ($('image-dialog').open) $('image-dialog').close();
    $('timeline').replaceChildren();
    show('answer-content', false);
    clearPlayer();
  }

  async function selectSource(source, force = false) {
    if (!['reviewed', 'auto'].includes(source) || (!force && source === state.source)) return;
    state.source = source;
    state.clipId = null;
    $('source-reviewed').setAttribute('aria-pressed', String(source === 'reviewed'));
    $('source-auto').setAttribute('aria-pressed', String(source === 'auto'));
    await loadCatalog();
  }

  async function loadCatalog() {
    state.catalogController?.abort();
    const controller = new AbortController();
    state.catalogController = controller;
    const version = ++state.catalogVersion;
    const source = state.source;
    clearResults();
    state.catalog = null;
    $('clip-filters').replaceChildren(element('div', 'library-placeholder', '正在读取录像…'));
    document.querySelector('.quick-questions').replaceChildren();
    setText('clip-count', '—');
    setText('search-scope', '正在读取所选来源');
    setText('source-description', provenanceLabel());
    show('global-error', false);
    setLoading(true);
    announce('正在读取录像与画面索引。');
    try {
      const catalog = await readJSON(`/api/catalog?source=${source}`, {}, controller);
      if (version !== state.catalogVersion || source !== state.source) return;
      if (!catalog || !Array.isArray(catalog.clips) || !Array.isArray(catalog.objects)) throw new Error('录像目录格式不完整，请重新尝试。');
      state.catalog = catalog;
      if (!catalog.clips.some((clip) => clip.id === state.clipId)) state.clipId = null;
      const dataset = catalog.dataset || {};
      setText('dataset-summary', `${source === 'auto' ? '自动识别' : '复核参考'} · ${catalog.clips.length} 段录像 · ${list(catalog.observations).length} 条画面线索`);
      const scopeNote = limitations(dataset.limitations);
      setText('dataset-limitations', scopeNote || '只查询已建立画面索引的录像，结果需要结合原视频确认。');
      setText('provenance-label', provenanceLabel());
      setText('source-description', provenanceLabel());
      renderFilters();
      renderQuickQuestions();
      if (source === 'auto' && !catalog.clips.length) {
        setLoading(false);
        announce('还没有已完成的自动识别结果。');
        showEmpty('还没有自动识别结果', '选择一段已有录像，完成自动分析后就能在这里查找。', [
          { label: '选择录像并分析', action: () => { $('analysis-panel').open = true; $('analysis-clip').focus(); } },
          { label: '查看复核参考', action: () => selectSource('reviewed') },
        ]);
        return;
      }
      const firstLabel = text(catalog.objects[0]?.label_zh);
      $('query').placeholder = firstLabel ? `比如：${firstLabel}最后在哪儿看到？` : '输入已建立索引的物品';
      const initialQuery = $('query').value.trim() || state.query || (firstLabel ? `${firstLabel}最后在哪儿看到？` : '');
      if (!initialQuery) {
        setLoading(false);
        announce('这份索引暂时没有物品线索。');
        showEmpty('还没有可查询的物品', '这份离线索引暂时没有物品线索。');
        return;
      }
      $('query').value = initialQuery;
      await search(initialQuery);
    } catch (error) {
      if (version !== state.catalogVersion || error.name === 'AbortError') return;
      setLoading(false);
      clearPlayer();
      showEmpty('录像还没有载入', '连接恢复后，就可以从已有画面里查找线索。');
      showError('暂时无法读取录像', error.message, loadCatalog);
      setText('search-scope', '录像尚未载入');
    } finally {
      if (version === state.catalogVersion) state.catalogController = null;
    }
  }

  async function search(query) {
    query = query.trim();
    if (!query) { $('query').focus(); return; }
    if (!state.catalog) { $('query').value = query; await loadCatalog(); return; }
    clearResults();
    const controller = new AbortController();
    state.searchController = controller;
    const version = ++state.searchVersion;
    state.query = query;
    $('query').value = query;
    show('global-error', false);
    setLoading(true);
    announce('正在查找画面线索。');
    try {
      const result = await readJSON('/api/search', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, clip_id: state.clipId, source: state.source }),
      }, controller);
      if (version !== state.searchVersion) return;
      setLoading(false);
      if (result.status === 'found' && list(result.answers).length) {
        state.answers = result.answers;
        state.answerIndex = 0;
        renderAnswer();
        announce(`找到 ${result.answers.length} 个物品的画面线索。结果需要结合原视频确认。`);
      } else if (result.status === 'choose_object') {
        const choices = list(result.recognized_objects).length ? result.recognized_objects : list(state.catalog.objects);
        const actions = choices.map((choice) => {
          const known = typeof choice === 'string' ? list(state.catalog.objects).find((item) => item.id === choice || item.label_zh === choice) : choice;
          const label = text(known?.label_zh, text(choice));
          return { label, action: () => search(`${label}最后在哪儿看到？`) };
        }).filter((choice) => choice.label);
        showEmpty('你想找哪一个物品？', text(result.message, '选一个物品，我会把对应的画面线索找出来。'), actions);
        announce('请先选择要找的物品。');
      } else if (result.status === 'no_evidence' || (result.status === 'found' && !list(result.answers).length)) {
        const actions = [];
        if (state.clipId) actions.push({ label: '到全部录像中查找', action: () => { state.clipId = null; renderFilters(); search(state.query); } });
        showEmpty('还没有足够清楚的证据', text(result.message, '当前范围里没有能确认这个物品的画面。这不代表它没有出现过，也不能据此判断它在哪里。'), actions);
        announce('当前范围内没有找到足够清楚的证据。');
      } else throw new Error('查找结果格式不完整，请重新尝试。');
    } catch (error) {
      if (version !== state.searchVersion || error.name === 'AbortError') return;
      setLoading(false);
      showEmpty('这次没有查找成功', '已有录像和画面索引仍在，重新尝试即可。');
      showError('查找暂时遇到了问题', error.message, () => search(state.query));
    } finally {
      if (version === state.searchVersion) state.searchController = null;
    }
  }

  function renderTabs() {
    const tabs = $('answer-tabs');
    tabs.replaceChildren();
    show('answer-tabs', state.answers.length > 1);
    state.answers.forEach((answer, index) => {
      const button = element('button', 'answer-tab', text(answer.label_zh, `物品 ${index + 1}`));
      button.type = 'button'; button.setAttribute('role', 'tab');
      button.setAttribute('aria-selected', String(index === state.answerIndex));
      button.tabIndex = index === state.answerIndex ? 0 : -1;
      button.addEventListener('click', () => { state.answerIndex = index; renderAnswer(); $('answer-tabs').children[index]?.focus(); });
      button.addEventListener('keydown', (event) => {
        let next = index;
        if (event.key === 'ArrowRight') next = (index + 1) % state.answers.length;
        else if (event.key === 'ArrowLeft') next = (index - 1 + state.answers.length) % state.answers.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = state.answers.length - 1;
        else return;
        event.preventDefault(); state.answerIndex = next; renderAnswer(); $('answer-tabs').children[next]?.focus();
      });
      tabs.append(button);
    });
  }

  function renderAnswer() {
    const answer = state.answers[state.answerIndex];
    const observation = completeObservation(answer?.observation);
    if (!observation || observation.usable_as_clear_evidence !== true || observation.is_object_visible !== true || observation.use_for_last_seen !== true) {
      clearPlayer();
      showEmpty('这条线索缺少证据画面', '请重新查找，或换一个物品试试。');
      showError('缺少清楚的物品证据', '返回的主结果尚不足以确认物品最后清楚出现的位置。', () => search(state.query));
      return;
    }
    state.observation = observation;
    show('empty-state', false);
    show('answer-content', true);
    renderTabs();
    const label = text(answer.label_zh, text(observation.label_zh, '物品'));
    setText('provenance-label', provenanceLabel(observation));
    const held = observation.state === 'held';
    const states = { held: '手中拿着', placed: '明确放置', visible: '画面中可见', uncertain: '需要再确认', removed: '已被拿起' };
    setText('object-state', states[observation.state] || '画面线索');
    $('object-state').classList.toggle('held', held || observation.state === 'removed');
    setText('answer-headline', text(answer.headline, `最近清楚看到${label}的这一刻`));
    setText('answer-description', text(observation.observation_zh, text(observation.location_zh)));
    const meta = $('answer-meta');
    meta.replaceChildren();
    meta.append(element('span', 'meta-tag meta-time', `视频 ${formatTime(observation.output_pts_seconds, true)}`));
    meta.append(element('span', 'meta-tag', observation.clip_label));
    const date = formatDate(observation.clip_started_at);
    if (date) meta.append(element('span', 'meta-tag', date));

    const evidenceURL = localMediaURL(observation.evidence_url);
    const image = $('evidence-image');
    image.onload = null; image.onerror = null;
    image.alt = `${label}的证据画面：${text(observation.observation_zh, text(observation.location_zh, '请结合原视频确认'))}`;
    show('image-unavailable', !evidenceURL);
    show('expand-evidence', Boolean(evidenceURL));
    image.hidden = !evidenceURL;
    if (evidenceURL) {
      image.onload = () => { image.hidden = false; show('image-unavailable', false); };
      image.onerror = () => { image.hidden = true; show('image-unavailable', true); show('expand-evidence', false); };
      image.src = evidenceURL;
    } else image.removeAttribute('src');
    setText('evidence-caption', `${observationCaption(observation)} · 证据画面`);

    const caveat = text(answer.caveat).trim();
    if (caveat) setText('answer-caveat', caveat);
    else {
      const fallback = [];
      if (held) fallback.push('这时物品在手中，之后放到哪里还没有确认。');
      const observationLimit = limitations(observation.limitations);
      if (observationLimit) fallback.push(observationLimit);
      if (!fallback.length) fallback.push('这是录像中当时看到的情况，不代表物品现在仍在那里。请查看前后画面确认。');
      setText('answer-caveat', fallback.join('\n'));
    }

    const placement = completeObservation(answer.latest_placement);
    const showPlacement = placement && observationKey(placement) !== observationKey(observation);
    show('earlier-placement', Boolean(showPlacement));
    $('view-placement').onclick = null;
    if (showPlacement) {
      setText('placement-description', `${text(placement.observation_zh, text(placement.location_zh))}（${observationCaption(placement)}）`);
      $('view-placement').onclick = () => selectEvidence(placement, { play: true, scroll: true });
    }
    renderTimeline(answer, observation);
    selectEvidence(observation, { play: false, scroll: false });
  }

  function renderTimeline(answer, observation) {
    const history = list(answer.history).map(completeObservation).filter(Boolean);
    if (!history.some((item) => observationKey(item) === observationKey(observation))) history.unshift(observation);
    const unique = history.filter((item, index) => history.findIndex((other) => observationKey(other) === observationKey(item)) === index);
    setText('timeline-count', `${unique.length} 条线索 · 最近在前`);
    const timeline = $('timeline');
    timeline.replaceChildren();
    unique.forEach((item) => {
      const li = element('li', 'timeline-item');
      li.dataset.observationKey = observationKey(item);
      const button = element('button', 'timeline-button');
      button.type = 'button';
      button.setAttribute('aria-label', `播放${observationCaption(item)}，${text(item.observation_zh, text(item.location_zh))}`);
      const imageURL = localMediaURL(item.evidence_url);
      if (imageURL) {
        const img = element('img', 'timeline-thumb');
        img.alt = ''; img.loading = 'lazy'; img.src = imageURL;
        img.addEventListener('error', () => { img.hidden = true; }, { once: true });
        button.append(img);
      }
      const copy = element('span', 'timeline-copy');
      const qualifier = evidenceQualifier(item);
      if (qualifier) copy.append(element('span', 'timeline-qualifier', qualifier));
      copy.append(element('span', 'timeline-line', text(item.observation_zh, text(item.location_zh, '查看这一刻的画面'))));
      copy.append(element('span', 'timeline-meta', [item.clip_label, formatDate(item.clip_started_at)].filter(Boolean).join(' · ')));
      button.append(copy, element('span', 'timeline-time', formatTime(item.output_pts_seconds)));
      button.addEventListener('click', () => selectEvidence(item, { play: true, scroll: true }));
      li.append(button); timeline.append(li);
    });
  }

  function resetMediaHandlers() {
    window.clearTimeout(state.mediaTimer);
    state.mediaTimer = null;
    video.onloadedmetadata = null;
    video.onerror = null;
    video.oncanplay = null;
  }

  function clearPlayer() {
    state.mediaVersion += 1;
    resetMediaHandlers();
    video.pause();
    video.removeAttribute('src');
    video.load();
    state.evidence = null;
    show('evidence-video', false);
    show('player-placeholder', true);
    show('player-info', false);
    show('video-loading', false);
    show('player-error', false);
    setText('player-time', '—');
  }

  function mediaError(message) {
    show('video-loading', false);
    show('player-error', true);
    setText('player-error-text', message);
  }

  function selectEvidence(input, { play = false, scroll = false, forceReload = false } = {}) {
    const observation = completeObservation(input);
    if (!observation) return;
    state.evidence = observation;
    const version = ++state.mediaVersion;
    resetMediaHandlers();
    video.pause();
    show('player-error', false);
    setText('player-time', formatTime(observation.output_pts_seconds, true));
    setText('player-clip-label', observation.clip_label);
    setText('player-observation', [evidenceQualifier(observation), text(observation.observation_zh, text(observation.location_zh))].filter(Boolean).join('\n'));
    show('player-info', true);
    document.querySelectorAll('.timeline-item').forEach((node) => {
      const active = node.dataset.observationKey === observationKey(observation);
      node.classList.toggle('active', active);
      const button = node.querySelector('button');
      if (active) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    });
    const url = localMediaURL(observation.video_url);
    const requestedTime = Number(observation.output_pts_seconds);
    if (!url || !Number.isFinite(requestedTime) || requestedTime < 0) {
      video.removeAttribute('src'); video.load();
      show('evidence-video', false); show('player-placeholder', true);
      mediaError('这条线索缺少有效的原视频或跳转时间，请换一条线索或重新查找。');
      return;
    }
    show('player-placeholder', false);
    show('evidence-video', true);
    show('video-loading', true);

    function finishLoading() {
      if (version !== state.mediaVersion) return;
      window.clearTimeout(state.mediaTimer); state.mediaTimer = null;
      show('video-loading', false);
    }

    function onMediaError() {
      if (version !== state.mediaVersion) return;
      finishLoading();
      const messages = {
        1: '视频加载已中止，可以重新加载。',
        2: '读取原视频时连接中断，请重新加载。',
        3: '浏览器暂时无法解码这段视频，请重新加载或换一条线索。',
        4: '这段原视频暂时不可用，或浏览器不支持它的格式。',
      };
      mediaError(messages[video.error?.code] || '原视频加载失败，请重新尝试。');
    }

    function seekToEvidence() {
      if (version !== state.mediaVersion) return;
      if (video.readyState < 1) return;
      try {
        const duration = Number.isFinite(video.duration) ? video.duration : Number(observation.duration_seconds);
        if (Number.isFinite(duration) && requestedTime > duration + 0.2) {
          finishLoading(); mediaError('证据时间超出了这段视频的长度，请重新查找或选择另一条线索。'); return;
        }
        // Only the MOV/MP4 output timeline is used. Never use SDK sourcePTS.
        video.currentTime = Number.isFinite(duration) && duration > 0 ? Math.min(requestedTime, Math.max(0, duration - 0.02)) : requestedTime;
        if (video.readyState >= 3 && !video.seeking) finishLoading();
        if (play) {
          const attempt = video.play();
          if (attempt && typeof attempt.catch === 'function') attempt.catch((error) => {
            if (version !== state.mediaVersion || error.name === 'AbortError') return;
            finishLoading();
            mediaError('已跳到证据时刻。浏览器没有开始播放，请点视频上的播放按钮。');
          });
        }
      } catch {
        finishLoading(); mediaError('这次没有跳转成功，请点击“重新加载视频”再试一次。');
      }
    }

    video.onerror = onMediaError;
    video.oncanplay = finishLoading;
    state.mediaTimer = window.setTimeout(() => {
      if (version === state.mediaVersion) mediaError('原视频载入时间较长，请点击下方重新加载。');
    }, 15000);

    const sameSource = video.getAttribute('src') === url && video.readyState >= 1;
    if (sameSource && !forceReload) seekToEvidence();
    else {
      video.onloadedmetadata = () => { video.onloadedmetadata = null; seekToEvidence(); };
      video.src = url;
      video.load();
    }
    if (scroll && window.matchMedia('(max-width: 1050px)').matches) {
      $('player-panel').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
    }
  }

  function renderAnalysis() {
    const status = state.analysis || {};
    const clips = list(status.available_clips);
    const signature = JSON.stringify(clips.map((clip) => [clip.id, clip.label]));
    const select = $('analysis-clip');
    if (signature !== state.analysisClipsSignature) {
      state.analysisClipsSignature = signature;
      const selected = select.value;
      select.replaceChildren();
      clips.forEach((clip) => {
        const option = element('option', '', text(clip.label, '录像'));
        option.value = text(clip.id);
        select.append(option);
      });
      if (!clips.length) {
        const option = element('option', '', '暂无可分析的已有录像');
        option.value = '';
        select.append(option);
      }
      else select.value = clips.some((clip) => clip.id === selected) ? selected : clips[clips.length - 1].id;
    }
    const running = status.status === 'running';
    const busy = running || Boolean(state.analysisAction);
    select.disabled = busy || !state.analysisConnected || !clips.length;
    $('analysis-start').disabled = busy || !state.analysisConnected || !clips.length || !select.value;
    $('analysis-start').textContent = ['failed', 'cancelled'].includes(status.status) ? '重试所选录像' : '开始自动分析';
    show('analysis-cancel', running);
    $('analysis-cancel').disabled = Boolean(state.analysisAction) || !state.analysisConnected;
    $('analysis-cancel').textContent = state.analysisAction === 'cancel' ? '正在取消…' : '取消分析';
    $('analysis-file').disabled = busy;
    $('analysis-recorded-at').disabled = busy;
    $('analysis-import-start').disabled = busy || !state.analysisConnected
      || !$('analysis-file').files?.length || !$('analysis-recorded-at').value;
    $('analysis-import-start').textContent = state.analysisAction === 'import' ? '正在导入…' : '导入并自动分析';

    const phases = {
      idle: '准备就绪', queued: '已排队', preparing: '准备录像', extracting: '抽取画面',
      analyzing: '识别物品', refining: '补看关键时段', publishing: '保存结果',
      completed: '分析完成', failed: '分析未完成', cancelled: '已取消',
    };
    const phase = phases[status.phase] || '正在连接';
    const actionMessage = { start: '正在启动自动分析…', cancel: '正在取消分析…', import: '正在导入录像并启动分析…' }[state.analysisAction];
    setText('analysis-summary', state.analysisConnectionError ? '连接中断' : (actionMessage || phase));
    show('analysis-progress-area', Boolean(status.job_id) && state.analysisAction !== 'import');
    setText('analysis-phase', phase);
    const progress = Number.isFinite(status.progress) ? Math.max(0, Math.min(1, status.progress)) : 0;
    $('analysis-progress').value = Math.round(progress * 100);
    setText('analysis-percent', `${Math.round(progress * 100)}%`);
    const counts = [];
    if (Number.isInteger(status.calls)) counts.push(`模型调用 ${status.calls} 次`);
    if (Number.isInteger(status.total_windows) && status.total_windows > 0) counts.push(`完成 ${Number(status.completed_windows) || 0} / ${status.total_windows} 批画面`);
    if (Number.isInteger(status.cached_windows) && status.cached_windows > 0) counts.push(`复用 ${status.cached_windows} 批`);
    setText('analysis-counts', counts.join(' · '));
    const message = actionMessage || text(status.message, '选择录像后可自动分析。');
    setText('analysis-message', status.status === 'completed' && !actionMessage ? `${message} 自动结果仍需结合原视频确认。` : message);
    const error = state.analysisActionError || state.analysisConnectionError
      || (status.status === 'failed' ? `${text(status.message, '分析未完成。')} 已有结果仍可查询，可以重试。` : '')
      || text(status.auto_index?.refresh_error);
    setText('analysis-error', error);
    show('analysis-error', Boolean(error));
    show('analysis-refresh', Boolean(state.analysisConnectionError));
  }

  async function refreshAnalysisStatus() {
    if (state.stopped) return;
    window.clearTimeout(state.analysisTimer);
    state.analysisController?.abort();
    const controller = new AbortController();
    state.analysisController = controller;
    const version = ++state.analysisVersion;
    try {
      const status = await readJSON('/api/analysis/status', {}, controller);
      if (version !== state.analysisVersion || state.stopped) return;
      if (!['idle', 'running', 'completed', 'failed', 'cancelled'].includes(status.status)
          || !Array.isArray(status.available_clips)) throw new Error('自动分析状态格式不完整，请重新连接。');
      state.analysis = status;
      state.analysisConnected = true;
      state.analysisConnectionError = '';
      if (status.status === 'running') state.watchedJobId = status.job_id;
      renderAnalysis();
      if (status.status === 'completed' && status.job_id && status.job_id === state.watchedJobId
          && status.job_id !== state.completedJobId) {
        state.completedJobId = status.job_id;
        announce('自动分析完成，正在显示自动识别结果。请结合原视频确认。');
        await selectSource('auto', true);
      }
    } catch (error) {
      if (version !== state.analysisVersion || error.name === 'AbortError' || state.stopped) return;
      state.analysisConnected = false;
      state.analysisConnectionError = error.message;
      renderAnalysis();
    } finally {
      if (version === state.analysisVersion && !state.stopped) {
        state.analysisController = null;
        state.analysisTimer = window.setTimeout(refreshAnalysisStatus, 2000);
      }
    }
  }

  async function analysisAction(kind) {
    if (state.analysisAction || !state.analysisConnected) return;
    if (kind !== 'cancel' && state.analysis?.status === 'running') return;
    state.analysisAction = kind;
    state.analysisActionError = '';
    $('analysis-panel').open = true;
    renderAnalysis();
    try {
      let url = '/api/analysis/start';
      let payload;
      if (kind === 'cancel') {
        if (!state.analysis?.job_id) throw new Error('没有正在分析的任务。');
        url = '/api/analysis/cancel';
        payload = { job_id: state.analysis.job_id };
      } else if (kind === 'import') {
        const file = $('analysis-file').files?.[0];
        if (!file || !/\.(mov|mp4)$/i.test(file.name)) throw new Error('请选择 MOV 或 MP4 录像。');
        if (!file.size || file.size > 1024 * 1024 * 1024) throw new Error('录像必须非空，且不能超过 1 GiB。');
        const recordedAt = $('analysis-recorded-at');
        const date = new Date(recordedAt.value);
        if (!recordedAt.value || !recordedAt.validity.valid || !Number.isFinite(date.getTime())) throw new Error('请填写录像实际开始的日期和时间。');
        // Completed, failed, and cancelled jobs may clean their temporary upload.
        // Keep the selected File in the browser and send it again on every retry.
        const uploaded = await readJSON(`/api/analysis/import?filename=${encodeURIComponent(file.name)}`, {
          method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file, timeoutMs: 180000,
        });
        if (typeof uploaded.upload_id !== 'string' || !uploaded.upload_id) throw new Error('录像导入结果不完整，请重试。');
        payload = { upload_id: uploaded.upload_id, started_at: date.toISOString(), label: file.name };
      } else {
        const clipId = $('analysis-clip').value;
        if (!clipId) throw new Error('请先选择一段录像。');
        payload = { clip_id: clipId };
      }
      const result = await readJSON(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      if (kind !== 'cancel' && typeof result.job_id === 'string') state.watchedJobId = result.job_id;
    } catch (error) {
      state.analysisActionError = error.message;
    } finally {
      // Re-read after mutations, including timeouts: the server may have accepted the task.
      await refreshAnalysisStatus();
      state.analysisAction = '';
      if (!state.stopped) renderAnalysis();
    }
  }

  $('source-reviewed').addEventListener('click', () => selectSource('reviewed'));
  $('source-auto').addEventListener('click', () => selectSource('auto'));
  $('analysis-start').addEventListener('click', () => analysisAction('start'));
  $('analysis-cancel').addEventListener('click', () => analysisAction('cancel'));
  $('analysis-import-start').addEventListener('click', () => analysisAction('import'));
  $('analysis-file').addEventListener('change', () => { state.analysisActionError = ''; renderAnalysis(); });
  $('analysis-recorded-at').addEventListener('input', renderAnalysis);
  $('analysis-refresh').addEventListener('click', refreshAnalysisStatus);
  $('search-form').addEventListener('submit', (event) => { event.preventDefault(); search($('query').value); });
  $('global-retry').addEventListener('click', () => state.retryAction?.());
  $('primary-evidence').addEventListener('click', () => selectEvidence(state.observation, { play: true, scroll: true }));
  $('view-evidence').addEventListener('click', () => selectEvidence(state.observation, { play: true, scroll: true }));
  $('replay-evidence').addEventListener('click', () => selectEvidence(state.evidence, { play: true }));
  $('retry-video').addEventListener('click', () => selectEvidence(state.evidence, { play: true, forceReload: true }));
  $('expand-evidence').addEventListener('click', () => {
    const observation = state.observation;
    const url = localMediaURL(observation?.evidence_url);
    if (!url) return;
    setText('dialog-title', `${text(observation.label_zh, '物品')} · 证据画面`);
    $('dialog-image').src = url;
    $('dialog-image').alt = $('evidence-image').alt;
    setText('dialog-caption', observationCaption(observation));
    $('image-dialog').showModal();
  });
  $('close-image').addEventListener('click', () => $('image-dialog').close());
  $('image-dialog').addEventListener('click', (event) => {
    if (event.target !== $('image-dialog')) return;
    const bounds = $('image-dialog').getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) $('image-dialog').close();
  });
  window.addEventListener('pagehide', () => {
    state.stopped = true;
    state.searchController?.abort(); state.catalogController?.abort(); state.analysisController?.abort();
    window.clearTimeout(state.analysisTimer); resetMediaHandlers(); video.pause();
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) { state.stopped = false; loadCatalog(); refreshAnalysisStatus(); }
  });
  loadCatalog();
  refreshAnalysisStatus();
})();
