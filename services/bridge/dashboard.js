let csrf = '';
const element = id => document.getElementById(id);
function showError(error) {
  element('notice').textContent = error.message || 'Could not connect. Please reload the page.';
  element('notice').classList.add('error');
}
async function post(path, body = {}) {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || 'Please try again.');
  return result;
}
async function refresh() {
  const response = await fetch('/api/state');
  const state = await response.json();
  if (!response.ok) throw new Error(state.message);
  csrf = state.csrf;
  element('notice').textContent = state.notice;
  element('notice').classList.remove('error');
  element('account').textContent = state.account ? `${state.account} · ${state.planEnabled ? 'ChatGPT plan connected' : 'Plan permission needed'}` : 'Not connected';
  element('sign-in').textContent = 'Continue with ChatGPT';
  element('sign-out').hidden = !state.connected;
  element('model-section').hidden = !state.planEnabled;
  element('server-url').value = state.serverURL;
  element('pairing-code').value = state.pairingCode;
  element('model').replaceChildren(...state.models.map(model => {
    const option = document.createElement('option'); option.value = model.id; option.textContent = model.name; option.selected = model.id === state.model; return option;
  }));
  if (state.welcome && !element('welcome').open) element('welcome').showModal();
}
function action(id, work) {
  element(id).addEventListener('click', async () => {
    const button = element(id); button.disabled = true;
    try { await work(); } catch (error) { showError(error); } finally { button.disabled = false; }
  });
}
action('sign-in', async () => {
  const result = await post('/api/sign-in');
  const target = new URL(result.url);
  if (target.origin !== 'https://auth.openai.com') throw new Error('Invalid sign-in destination.');
  window.location.assign(target.href);
});
action('sign-out', async () => { await post('/api/sign-out'); await refresh(); });
action('refresh-models', async () => { await post('/api/models'); await refresh(); });
action('welcome-done', async () => { await post('/api/welcome'); element('welcome').close(); });
element('welcome').addEventListener('cancel', event => { event.preventDefault(); element('welcome-done').click(); });
element('model').addEventListener('change', async event => {
  try { await post('/api/model', { model: event.target.value }); } catch (error) { showError(error); }
});
action('show-code', () => {
  const field = element('pairing-code'); field.type = field.type === 'password' ? 'text' : 'password';
  element('show-code').textContent = field.type === 'password' ? 'Show' : 'Hide';
});
for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(element(button.dataset.copy).value); button.textContent = 'Copied'; setTimeout(() => { button.textContent = 'Copy'; }, 1500); }
    catch { showError(new Error('Select the field and copy it manually.')); }
  });
}
refresh().catch(showError);
