import { randomUUID } from 'node:crypto';
import {
  AppError, RESOURCE, MAX_IMAGE_BYTES, MAX_VIDEO_BODY_BYTES, equalSecret, readBody,
  MEMORY_PROMPT_VERSION, MEMORY_HIGHLIGHTS_PROMPT_VERSION, MAX_MEMORY_OUTPUT_CHARS,
  MAX_MEMORY_HIGHLIGHTS_OUTPUT_CHARS, validateJPEG, parseVideoRequest,
  captionPayload, videoSummaryPayload, parseVideoMemoryResponse, readCaptionStream, upstreamError,
} from './core.mjs';
import { OBJECT_PROMPT_VERSION, MAX_OBJECT_OUTPUT_CHARS, parseObjectRequest,
  objectObservationsPayload, parseObjectObservationsResponse } from './object-memory.mjs';
import { TEACHER_PROMPT_VERSION, MAX_TEACHER_BODY_BYTES, MAX_TEACHER_OUTPUT_CHARS,
  parseTeacherRequest, teacherLabelsPayload, parseTeacherLabelsResponse } from './teacher-labels.mjs';
import { TEACHER_PRESENCE_PROMPT_VERSION, teacherPresencePayload } from './teacher-presence.mjs';
import { INPUT_DIAGNOSTIC_PROMPT_VERSION, MAX_INPUT_DIAGNOSTIC_BODY_BYTES,
  MAX_INPUT_DIAGNOSTIC_OUTPUT_CHARS, parseInputDiagnosticRequest, inputDiagnosticPayload,
  parseInputDiagnosticResponse } from './teacher-input-diagnostic.mjs';

export function sendJSON(res, status, value) {
  if (res.destroyed) return;
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff' });
  res.end(JSON.stringify(value));
}
export function failJSON(res, error) {
  const safe = error instanceof AppError ? error : new AppError(500, 'internal_error', 'Something went wrong. Please retry.');
  sendJSON(res, safe.status, { error: safe.code, message: safe.message });
}
async function untilAborted(work, signal) {
  signal.throwIfAborted();
  let onAbort;
  const aborted = new Promise((_, reject) => {
    onAbort = () => reject(signal.reason);
    signal.addEventListener('abort', onAbort, { once: true });
  });
  try { return await Promise.race([work, aborted]); }
  finally { signal.removeEventListener('abort', onAbort); }
}

// No account state or network requests occur until a handler is explicitly invoked.
// Dependencies let local tests exercise the real auth/timeout/single-flight path.
export function createMediaAPI({ getPairingCode, getStatus, getModel, getAccessToken,
  isAuthActive, fetchImpl = fetch, timeoutMs }) {
  let active = false;
  async function handler(req, res) {
    let ownsRequest = false;
    let controller, timeout, abortOnClose;
    try {
      if (req.headers.origin || !equalSecret(req.headers.authorization, `Bearer ${getPairingCode()}`))
        throw new AppError(401, 'pairing_required', 'Check the pairing code shown on your Mac.');
      if (req.method === 'GET' && req.url === '/v1/status') {
        sendJSON(res, 200, { ...getStatus(), object_prompt_version: OBJECT_PROMPT_VERSION }); return;
      }
      const isVideo = req.url === '/v1/video-summary';
      const isObjects = req.url === '/v1/object-observations';
      const isTeacherPresence = req.url === '/v1/teacher-presence';
      const isTeacher = req.url === '/v1/teacher-labels' || isTeacherPresence;
      const isInputDiagnostic = req.url === '/v1/teacher-input-diagnostic';
      const isJSONMedia = isVideo || isObjects || isTeacher || isInputDiagnostic;
      if (req.method !== 'POST' || (!isJSONMedia && req.url !== '/v1/caption')) throw new AppError(404, 'not_found', 'Not found.');
      if (active || isAuthActive()) throw new AppError(429, 'busy', 'Another analysis is running. Please wait.');
      const contentType = req.headers['content-type']?.split(';')[0].trim();
      if (contentType !== (isJSONMedia ? 'application/json' : 'image/jpeg'))
        throw new AppError(415, isJSONMedia ? 'json_required' : 'jpeg_required',
          isJSONMedia ? 'Send video frames as JSON.' : 'Choose a JPEG-compatible photo.');
      active = ownsRequest = true;
      const body = await readBody(req, isInputDiagnostic ? MAX_INPUT_DIAGNOSTIC_BODY_BYTES
        : isTeacher ? MAX_TEACHER_BODY_BYTES : isJSONMedia ? MAX_VIDEO_BODY_BYTES : MAX_IMAGE_BYTES);
      const media = isInputDiagnostic ? parseInputDiagnosticRequest(body)
        : isTeacher ? parseTeacherRequest(body) : isObjects ? parseObjectRequest(body) : isVideo ? parseVideoRequest(body) : validateJPEG(body);
      const status = getStatus();
      if (!status.ready) throw new AppError(503, 'not_ready', status.message);
      if (res.destroyed) return;
      // Snapshot the selected model before any awaited token refresh.
      const model = getModel();
      if ((isObjects || isTeacher || isInputDiagnostic) && media.expected_model !== undefined && media.expected_model !== model)
        throw new AppError(409, 'model_changed', 'The selected model changed. Start a new analysis with the current model.');
      const payload = isInputDiagnostic ? inputDiagnosticPayload(model, media)
        : isTeacherPresence ? teacherPresencePayload(model, media)
        : isTeacher ? teacherLabelsPayload(model, media) : isObjects ? objectObservationsPayload(model, media)
        : isVideo ? videoSummaryPayload(model, media) : captionPayload(model, media);
      controller = new AbortController();
      abortOnClose = () => { if (!res.writableEnded) controller.abort(); };
      res.on('close', abortOnClose);
      // Tests can still override both routes with timeoutMs; production keeps photos shorter.
      const requestTimeoutMs = timeoutMs ?? (isJSONMedia ? 300000 : 60000);
      timeout = setTimeout(() => controller.abort(new DOMException('Timed out', 'TimeoutError')), requestTimeoutMs);
      try {
        // A shared token refresh can finish in the background, but a cancelled
        // or timed-out phone request must never proceed to inference afterward.
        const token = await untilAborted(getAccessToken(), controller.signal);
        if (res.destroyed) return;
        controller.signal.throwIfAborted();
        const upstream = await fetchImpl(`${RESOURCE}/responses`, { method: 'POST', signal: controller.signal, redirect: 'error',
          headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
          body: JSON.stringify(payload) });
        if (!upstream.ok) {
          const error = await upstream.json().catch(() => ({}));
          throw upstreamError(upstream.status, error.error?.code || '');
        }
        const includeMemory = isVideo && media.include_memory === true;
        const includeHighlights = includeMemory && media.include_highlights === true;
        const output = await readCaptionStream(upstream.body, isInputDiagnostic ? { maxTextLength: MAX_INPUT_DIAGNOSTIC_OUTPUT_CHARS }
          : isTeacher ? { maxTextLength: MAX_TEACHER_OUTPUT_CHARS } : isObjects ? { maxTextLength: MAX_OBJECT_OUTPUT_CHARS } : includeMemory ? {
          maxTextLength: includeHighlights ? MAX_MEMORY_HIGHLIGHTS_OUTPUT_CHARS : MAX_MEMORY_OUTPUT_CHARS,
        } : undefined);
        if (isInputDiagnostic) {
          const decision = parseInputDiagnosticResponse(output, media);
          sendJSON(res, 200, { request_id: randomUUID(), model: payload.model,
            prompt_version: INPUT_DIAGNOSTIC_PROMPT_VERSION, ...decision, raw_output: output });
          return;
        }
        if (isTeacher) {
          const labels = parseTeacherLabelsResponse(output, media);
          sendJSON(res, 200, { request_id: randomUUID(), model: payload.model,
            prompt_version: isTeacherPresence ? TEACHER_PRESENCE_PROMPT_VERSION : TEACHER_PROMPT_VERSION,
            ...labels, raw_output: output });
          return;
        }
        if (isObjects) {
          const objects = parseObjectObservationsResponse(output, media.frames);
          sendJSON(res, 200, { request_id: randomUUID(), model: payload.model,
            prompt_version: OBJECT_PROMPT_VERSION, ...objects, raw_output: output });
          return;
        }
        const memory = includeMemory ? parseVideoMemoryResponse(output, media.frames, { includeHighlights }) : undefined;
        sendJSON(res, 200, { caption: memory ? memory.summary : output, request_id: randomUUID(),
          ...(isVideo ? { frame_count: media.frames.length } : {}),
          ...(memory ? { model: payload.model,
            prompt_version: includeHighlights ? MEMORY_HIGHLIGHTS_PROMPT_VERSION : MEMORY_PROMPT_VERSION,
            observations: memory.observations,
            ...(includeHighlights ? { highlights: memory.highlights } : {}),
          } : {}),
        });
      } catch (error) {
        if (res.destroyed) return;
        if (controller.signal.aborted) throw new AppError(504, 'timeout', 'ChatGPT took too long. Please try again.');
        throw error;
      }
    } catch (error) { failJSON(res, error); }
    finally {
      clearTimeout(timeout);
      if (abortOnClose) res.off('close', abortOnClose);
      if (ownsRequest) active = false;
    }
  }
  return { handler, isBusy: () => active };
}
