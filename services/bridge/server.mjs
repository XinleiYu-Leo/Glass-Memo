import http from 'node:http';
import { chmod, mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { AppError, ISSUER, RESOURCE, randomValue, challenge,
  equalSecret, verifyIDToken, readBody, upstreamError } from './core.mjs';
import { createMediaAPI, sendJSON as send, failJSON as fail } from './media-api.mjs';

const dashboardPort = Number(process.env.ROBOMEMO_DASHBOARD_PORT || 8765);
const captionPort = Number(process.env.ROBOMEMO_CAPTION_PORT || 8766);
const allowLAN = process.env.ROBOMEMO_ALLOW_LAN === '1';
const captionHost = allowLAN ? '0.0.0.0' : '127.0.0.1';
const origin = `http://127.0.0.1:${dashboardPort}`;
const redirectURI = `${origin}/auth/callback`;
const stateDirectory = process.env.ROBOMEMO_STATE_DIR || fileURLToPath(new URL('.private-state', import.meta.url));
await mkdir(stateDirectory, { recursive: true, mode: 0o700 });
await chmod(stateDirectory, 0o700);
await writeFile(join(stateDirectory, '.gitignore'), '*\n', { mode: 0o600 });
const stateFile = join(stateDirectory, 'connection.json');
let state;
try { state = JSON.parse(await readFile(stateFile, 'utf8')); }
catch (error) { if (error.code !== 'ENOENT') throw new Error('Could not read RoboMemo connection settings.'); }
state ||= { hostID: `urn:uuid:${randomUUID()}`, pairingCode: randomValue(), profiles: [], selectedClient: null, model: null };
let writes = Promise.resolve();
function persist() {
  const snapshot = JSON.stringify(state);
  const next = writes.then(async () => {
    const temporary = `${stateFile}.tmp`;
    await writeFile(temporary, snapshot, { mode: 0o600 });
    await chmod(temporary, 0o600);
    await rename(temporary, stateFile);
  });
  writes = next.catch(() => {});
  return next;
}
await persist();
let hostname = 'localhost';
try { hostname = execFileSync('/usr/sbin/scutil', ['--get', 'LocalHostName'], { encoding: 'utf8' }).trim(); } catch {}
const serverURL = allowLAN ? `http://${hostname}.local:${captionPort}` : `http://127.0.0.1:${captionPort}`;
const sessions = new Map();
let discovery, models = [], pendingRefresh, activeAuth = false;
let notice = 'Connect your own account on this computer. LAN access is disabled unless explicitly enabled.';
const profile = () => state.profiles.find(p => p.clientID === state.selectedClient);
const planEnabled = p => p?.scopes?.includes('chatgpt.tokens.use.direct');
async function remoteJSON(url, options = {}) {
  const response = await fetch(url, { ...options, signal: options.signal || AbortSignal.timeout(20000), redirect: 'error' });
  let data; try { data = await response.json(); } catch { throw new AppError(502, 'invalid_response', 'The ChatGPT service returned an unexpected response. Please retry.'); }
  if (!response.ok) {
    if (data.error === 'invalid_grant') throw new AppError(401, 'sign_in_required', 'Please sign in to ChatGPT again.');
    throw upstreamError(response.status, data.error?.code || '');
  }
  return data;
}
async function configuration() {
  if (!discovery) {
    const candidate = await remoteJSON(`${ISSUER}/.well-known/openid-configuration`);
    if (candidate.issuer !== ISSUER) throw new AppError(502, 'invalid_issuer', 'Could not verify the ChatGPT sign-in service.');
    for (const field of ['authorization_endpoint', 'token_endpoint', 'jwks_uri', 'revocation_endpoint']) {
      if (!candidate[field] || new URL(candidate[field]).origin !== ISSUER)
        throw new AppError(502, 'invalid_endpoint', 'Could not verify the ChatGPT sign-in service.');
    }
    discovery = candidate;
  }
  return discovery;
}
async function verifyToken(token, options) {
  const config = await configuration();
  const jwks = await remoteJSON(config.jwks_uri);
  return verifyIDToken(token, jwks.keys || [], options);
}
async function exchange(parameters) {
  const config = await configuration();
  return remoteJSON(config.token_endpoint, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ ...parameters, resource: RESOURCE }) });
}
function checkedTokens(tokens, previous) {
  if (typeof tokens.access_token !== 'string' || !tokens.access_token
    || !Number.isFinite(tokens.expires_in) || tokens.expires_in <= 0)
    throw new AppError(502, 'invalid_token_response', 'ChatGPT did not return a usable connection. Please sign in again.');
  return { accessToken: tokens.access_token, refreshToken: tokens.refresh_token || previous?.refreshToken,
    idToken: tokens.id_token || previous?.idToken,
    scopes: typeof tokens.scope === 'string' ? tokens.scope.split(/\s+/) : previous?.scopes || [],
    expiresAt: Date.now() + tokens.expires_in * 1000 };
}
async function accessToken() {
  const selected = profile();
  if (!selected || !planEnabled(selected)) throw new AppError(503, 'sign_in_required', 'Connect ChatGPT and allow plan usage on this computer first.');
  if (selected.expiresAt > Date.now() + 60000) return selected.accessToken;
  if (!selected.refreshToken) throw new AppError(503, 'sign_in_required', 'Please sign in again on this computer.');
  if (!pendingRefresh) pendingRefresh = (async () => {
    const tokens = await exchange({ grant_type: 'refresh_token', client_id: selected.clientID, refresh_token: selected.refreshToken });
    if (tokens.id_token) await verifyToken(tokens.id_token, { clientID: selected.clientID, subject: selected.subject });
    Object.assign(selected, checkedTokens(tokens, selected));
    await persist();
    if (!planEnabled(selected)) throw new AppError(503, 'plan_permission_required', 'Allow ChatGPT plan usage by signing in again on this computer.');
    return selected.accessToken;
  })().finally(() => { pendingRefresh = null; });
  return pendingRefresh;
}
async function refreshModels() {
  const token = await accessToken();
  const data = await remoteJSON(`${RESOURCE}/models`, { headers: { Authorization: `Bearer ${token}` } });
  if (!Array.isArray(data.models)) throw new AppError(502, 'invalid_catalog', 'Could not load the ChatGPT model list.');
  models = data.models.filter(m => m.visibility === 'list' && typeof m.slug === 'string')
    .map(m => ({ id: m.slug, name: m.display_name || m.slug }));
  if (!models.some(m => m.id === state.model)) {
    state.model = models.find(m => m.id === 'gpt-6-luna')?.id
      || models.find(m => m.id === 'gpt-6.1-sol')?.id || models[0]?.id || null;
    await persist();
  }
}
function status() {
  const p = profile();
  const ready = Boolean(planEnabled(p) && p.accessToken && state.model && models.some(m => m.id === state.model));
  return { ready, model: state.model, message: ready ? 'Ready to analyze selected photos and sampled video frames with your connected account.' : 'Connect ChatGPT and select a model on this computer.' };
}
function getSession(req) {
  const id = (req.headers.cookie || '').split(';').map(c => c.trim()).find(c => c.startsWith('robomemo='))?.slice(9);
  const session = sessions.get(id);
  if (!session || session.expires < Date.now()) return null;
  return session;
}
function newSession(res) {
  for (const [id, s] of sessions) if (s.expires < Date.now()) sessions.delete(id);
  const id = randomValue(), session = { csrf: randomValue(), expires: Date.now() + 12 * 60 * 60 * 1000 };
  sessions.set(id, session);
  res.setHeader('Set-Cookie', `robomemo=${id}; HttpOnly; SameSite=Lax; Path=/; Max-Age=43200`);
  return session;
}
async function dashboard(req, res) {
  try {
    if (req.headers.host !== `127.0.0.1:${dashboardPort}`) throw new AppError(403, 'invalid_host', 'Open the dashboard using its 127.0.0.1 address.');
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Frame-Options', 'DENY');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'");
    const url = new URL(req.url, origin);
    let session = getSession(req);
    if (req.method === 'GET' && ['/', '/dashboard.js', '/style.css'].includes(url.pathname)) {
      session ||= newSession(res);
      const filename = { '/': 'index.html', '/dashboard.js': 'dashboard.js', '/style.css': 'style.css' }[url.pathname];
      res.setHeader('Content-Type', filename.endsWith('.html') ? 'text/html; charset=utf-8' : filename.endsWith('.js') ? 'text/javascript' : 'text/css');
      res.end(await readFile(new URL(filename, import.meta.url))); return;
    }
    if (!session) throw new AppError(401, 'session_expired', 'Reload this page to continue.');
    if (req.method === 'GET' && url.pathname === '/api/state') {
      send(res, 200, { ...status(), csrf: session.csrf, serverURL, pairingCode: state.pairingCode,
        account: profile()?.email || null, connected: Boolean(profile()), planEnabled: Boolean(planEnabled(profile())),
        welcome: Boolean(planEnabled(profile()) && !profile().welcomed), models, notice }); return;
    }
    if (req.method === 'GET' && url.pathname === '/auth/callback') {
      if (activeAuth || mediaAPI.isBusy() || pendingRefresh) throw new AppError(409, 'sign_in_busy', 'Wait for the analysis to finish, then reload this page to complete sign-in.');
      const pending = session.pending;
      if (!pending || pending.expires <= Date.now() || !equalSecret(pending.state, url.searchParams.get('state')))
        throw new AppError(401, 'invalid_sign_in', 'Sign-in expired or could not be verified. Return to RoboMemo and try again.');
      delete session.pending;
      if (url.searchParams.has('error')) {
        notice = 'ChatGPT access was not granted. You can try again when ready.';
        res.writeHead(303, { Location: '/' }); res.end(); return;
      }
      const clientID = url.searchParams.get('client_id') || pending.clientID;
      if (!clientID || clientID === 'dynamic_agent_client' || (pending.clientID && clientID !== pending.clientID))
        throw new AppError(401, 'invalid_registration', 'ChatGPT registration was incomplete. Please try again.');
      const code = url.searchParams.get('code');
      if (!code) throw new AppError(401, 'missing_code', 'ChatGPT did not return an authorization code.');
      session.retryClientID = clientID;
      activeAuth = true;
      try {
        const tokens = await exchange({ grant_type: 'authorization_code', client_id: clientID, code,
          code_verifier: pending.verifier, redirect_uri: redirectURI });
        const claims = await verifyToken(tokens.id_token, { clientID, nonce: pending.nonce, subject: pending.subject });
        const old = state.profiles.find(p => p.clientID === clientID);
        const connection = { clientID, subject: claims.sub, email: claims.email || 'ChatGPT account', welcomed: old?.welcomed || false, ...checkedTokens(tokens) };
        state.profiles = state.profiles.filter(p => p.clientID !== clientID);
        state.profiles.push(connection); state.selectedClient = clientID; models = []; state.model = null;
        await persist();
        delete session.retryClientID;
        notice = planEnabled(connection) ? 'ChatGPT is connected. Your selected photos and video frames can now be analyzed.' : 'Signed in, but ChatGPT plan usage was not granted. Please reconnect and allow plan usage.';
        if (planEnabled(connection)) {
          try { await refreshModels(); } catch { notice = 'ChatGPT connected, but models could not be loaded. Click Refresh models.'; }
        }
      } finally { activeAuth = false; }
      res.writeHead(303, { Location: '/' }); res.end(); return;
    }
    if (req.method !== 'POST' || req.headers.origin !== origin || !equalSecret(req.headers['x-csrf-token'], session.csrf))
      throw new AppError(403, 'request_rejected', 'Reload the RoboMemo page and try again.');
    const body = JSON.parse((await readBody(req)).toString() || '{}');
    if (url.pathname === '/api/sign-in') {
      if (mediaAPI.isBusy() || activeAuth || pendingRefresh) throw new AppError(409, 'busy', 'Wait for the current request to finish.');
      const config = await configuration();
      const selected = profile();
      const pending = { state: randomValue(), nonce: randomValue(), verifier: randomValue(), expires: Date.now() + 10 * 60 * 1000,
        clientID: selected?.clientID || session.retryClientID, subject: selected?.subject };
      session.pending = pending;
      const authURL = new URL(config.authorization_endpoint);
      authURL.search = new URLSearchParams({ client_id: pending.clientID || 'dynamic_agent_client',
        ext_agent_host_id: state.hostID, response_type: 'code', redirect_uri: redirectURI,
        scope: 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct', resource: RESOURCE,
        state: pending.state, nonce: pending.nonce, code_challenge_method: 'S256', code_challenge: challenge(pending.verifier),
        ...(pending.clientID ? {} : { agent_name_hint: 'RoboMemo' }),
      }).toString();
      send(res, 200, { url: authURL.toString() }); return;
    }
    if (url.pathname === '/api/models') { await refreshModels(); send(res, 200, { ok: true }); return; }
    if (url.pathname === '/api/model') {
      if (!models.some(m => m.id === body.model)) throw new AppError(400, 'invalid_model', 'Choose a model from the current list.');
      state.model = body.model; await persist(); send(res, 200, { ok: true }); return;
    }
    if (url.pathname === '/api/welcome') {
      if (profile()) profile().welcomed = true;
      await persist(); send(res, 200, { ok: true }); return;
    }
    if (url.pathname === '/api/sign-out') {
      if (mediaAPI.isBusy() || activeAuth || pendingRefresh) throw new AppError(409, 'busy', 'Wait for the current request to finish.');
      const selected = profile(); let revoked = !selected?.refreshToken;
      if (selected?.refreshToken) {
        try {
          const config = await configuration();
          for (let attempt = 0; attempt < 2 && !revoked; attempt++) {
            if (attempt) await new Promise(resolve => setTimeout(resolve, 1000));
            const response = await fetch(config.revocation_endpoint, { method: 'POST', signal: AbortSignal.timeout(10000), redirect: 'error',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
              body: new URLSearchParams({ token: selected.refreshToken, token_type_hint: 'refresh_token', client_id: selected.clientID }) });
            revoked = response.status === 200;
          }
        } catch {}
      }
      state.profiles = state.profiles.filter(p => p.clientID !== state.selectedClient);
      state.selectedClient = null; state.model = null; models = [];
      for (const s of sessions.values()) delete s.pending;
      await persist();
      notice = revoked ? 'Signed out of RoboMemo.' : 'Signed out locally. Remote revocation was not confirmed; disconnect RoboMemo in ChatGPT settings.';
      send(res, 200, { ok: true }); return;
    }
    throw new AppError(404, 'not_found', 'Not found.');
  } catch (error) { fail(res, error); }
}
const mediaAPI = createMediaAPI({
  getPairingCode: () => state.pairingCode, getStatus: status,
  getModel: () => state.model, getAccessToken: accessToken, isAuthActive: () => activeAuth,
});
const dashboardServer = http.createServer(dashboard);
const captionServer = http.createServer(mediaAPI.handler);
for (const server of [dashboardServer, captionServer]) {
  server.requestTimeout = 80000; server.headersTimeout = 10000; server.keepAliveTimeout = 5000;
  server.on('error', error => { console.error(error.code === 'EADDRINUSE' ? 'RoboMemo is already running, or its port is busy.' : 'RoboMemo could not start.'); process.exitCode = 1; dashboardServer.close(); captionServer.close(); });
}
dashboardServer.listen(dashboardPort, '127.0.0.1', () => console.log(`RoboMemo dashboard: ${origin}`));
captionServer.listen(captionPort, captionHost, () => console.log(`iPhone connection: ${serverURL}`));
if (planEnabled(profile())) refreshModels().catch(() => { notice = 'Reconnect ChatGPT or click Refresh models to resume.'; });
