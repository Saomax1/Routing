// App shell: auth, navigation (hash routes), and view lifecycle.
import { h, render, icon, setTimezone, toast } from './dom.js';
import { api, setUnauthorizedHandler } from './api.js';
import { mountDispatch } from './views/dispatch.js';
import { mountTechnicians, mountSettings, mountSync, mountUsers } from './views/admin.js';

const app = document.getElementById('app');
let current = null;
let ctx = null;

const ROUTES = {
  dispatch: { label: 'Dispatch', mount: mountDispatch, full: true },
  sync: { label: 'Sync & parsing', mount: mountSync },
  technicians: { label: 'Technicians', mount: mountTechnicians, admin: true },
  settings: { label: 'Settings', mount: mountSettings, admin: true },
  users: { label: 'Users', mount: mountUsers, admin: true },
};

function showLogin(message) {
  if (current && current.destroy) current.destroy();
  current = null; ctx = null;
  const email = h('input', { type: 'email', required: true, autocomplete: 'username', autofocus: true });
  const pw = h('input', { type: 'password', required: true, autocomplete: 'current-password' });
  const err = h('div', { class: 'alert warn', hidden: !message, role: 'alert' }, message || '');
  const btn = h('button', { class: 'btn primary wide', type: 'submit' }, 'Log in');
  render(app, h('main', { class: 'login' },
    h('form', { class: 'card login-card', onsubmit: async (e) => {
      e.preventDefault(); btn.disabled = true; err.hidden = true;
      try { await api.login(email.value, pw.value); await boot(); }
      catch (ex) { err.textContent = ex.message; err.hidden = false; btn.disabled = false; pw.value = ''; pw.focus(); }
    } },
    h('div', { class: 'brand big' }, icon('route', 28), h('span', {}, 'Dispatch & Routing')),
    h('p', { class: 'dim' }, 'Internal tool. Sign in to see jobs and routes.'), err,
    h('label', { class: 'field' }, h('span', { class: 'f-label' }, 'Email'), email),
    h('label', { class: 'field' }, h('span', { class: 'f-label' }, 'Password'), pw), btn)));
}

function shell(user, config) {
  const nav = h('nav', { class: 'nav', 'aria-label': 'Main' },
    Object.entries(ROUTES).filter(([, r]) => !r.admin || user.role === 'admin').map(([key, r]) => h('a', { href: `#/${key}`, 'data-route': key }, r.label)));
  const logout = h('button', { class: 'btn ghost', type: 'button', onclick: async () => { try { await api.logout(); } catch { /* ignore */ } showLogin(); } }, 'Log out');
  const mode = config.mode === 'mock' ? h('span', { class: 'pill demo', title: 'Running on built-in sample data. Nothing is read from or written to Housecall Pro.' }, 'DEMO DATA') : h('span', { class: 'pill live', title: 'Connected to Housecall Pro (read-only)' }, 'LIVE · read-only');
  const view = h('main', { class: 'view', id: 'view' });
  render(app, h('header', { class: 'topbar' }, h('div', { class: 'brand' }, icon('route', 20), h('span', {}, 'Dispatch & Routing')), nav, h('div', { class: 'spacer' }), mode,
    h('span', { class: 'who', title: user.email }, user.name || user.email, h('small', {}, user.role)), logout), view);
  return view;
}

function route() {
  if (!ctx) return;
  const key = (location.hash.replace(/^#\//, '').split(/[/?]/)[0]) || 'dispatch';
  const r = ROUTES[key] && (!ROUTES[key].admin || ctx.user.role === 'admin') ? ROUTES[key] : ROUTES.dispatch;
  const rk = Object.keys(ROUTES).find((k) => ROUTES[k] === r);
  if (current && current.destroy) current.destroy();
  document.querySelectorAll('.nav a').forEach((a) => a.classList.toggle('active', a.dataset.route === rk));
  const view = document.getElementById('view');
  view.className = `view${r.full ? ' full' : ''}`;
  render(view);
  document.title = `${r.label} · Dispatch & Routing`;
  current = r.mount(view, ctx) || null;
}

async function boot() {
  try {
    const { user } = await api.me();
    const config = await api.config();
    setTimezone(config.timezone);
    ctx = { user, config };
    shell(user, config);
    window.removeEventListener('hashchange', route); window.addEventListener('hashchange', route);
    route();
  } catch (e) {
    if (e.status === 401) showLogin();
    else render(app, h('main', { class: 'login' }, h('div', { class: 'card login-card' }, h('h1', {}, 'Cannot load the app'), h('p', {}, e.message),
      h('button', { class: 'btn', type: 'button', onclick: () => location.reload() }, 'Retry'))));
  }
}

setUnauthorizedHandler(() => { if (ctx) { toast('Your session expired. Please log in again.', 'error'); showLogin(); } });
boot();
