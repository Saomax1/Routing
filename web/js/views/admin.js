// Admin screens: technicians, settings, sync & parsing status, users.
import { h, render, icon, fmtDateTime, timeAgo, toast, fmtDay } from '../dom.js';
import { api } from '../api.js';

const field = (label, input, help) => h('label', { class: 'field' }, h('span', { class: 'f-label' }, label), input, help ? h('span', { class: 'f-help' }, help) : null);
const note = (text, cls = '') => h('p', { class: `note ${cls}` }, text);
const adminOnly = (root) => render(root, h('div', { class: 'page' }, h('div', { class: 'alert warn' }, icon('alert', 14), ' This page is for admins only.')));
const deepClone = (o) => JSON.parse(JSON.stringify(o));
function getPath(o, p) { return p.split('.').reduce((a, k) => (a == null ? a : a[k]), o); }
function setPath(o, p, v) { const ks = p.split('.'); const last = ks.pop(); const t = ks.reduce((a, k) => (a[k] = a[k] ?? {}), o); t[last] = v; }

// ================================================================== technicians
export function mountTechnicians(root, ctx) {
  if (ctx.user.role !== 'admin') return adminOnly(root);
  const page = h('div', { class: 'page' });
  render(root, page);
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  function card(t) {
    const st = h('span', { class: 'f-status' });
    const active = h('input', { type: 'checkbox', checked: t.active });
    const skills = h('input', { type: 'text', value: t.trade_skills.join(', '), placeholder: 'PLB, HVAC', maxlength: 60 });
    const home = h('input', { type: 'text', value: t.home_address, placeholder: '123 Main St, Chandler, AZ 85225', maxlength: 200 });
    const start = h('input', { type: 'time', value: t.shift_start });
    const end = h('input', { type: 'time', value: t.shift_end });
    const max = h('input', { type: 'number', min: 1, max: 20, value: t.max_jobs_per_day });
    const color = h('input', { type: 'color', value: t.color });
    const days = DAYS.map((d, i) => h('label', { class: 'daybox' }, h('input', { type: 'checkbox', checked: t.work_days.includes(i), dataset: { d: i } }), h('span', {}, d)));
    const homeState = h('span', { class: `chip ${t.home_lat == null ? 'warn' : 'ok'}` }, t.home_lat == null ? 'not located' : 'located');
    const needs = h('span', { class: 'chip warn', hidden: !(t.active && (!t.trade_skills.length || t.home_lat == null)) }, icon('alert', 12), ' needs setup');
    const btn = h('button', { class: 'btn primary', type: 'submit' }, 'Save');
    const form = h('form', { class: 'tech-card', style: { '--c': t.color },
      onsubmit: async (e) => {
        e.preventDefault(); btn.disabled = true; st.textContent = 'Saving…'; st.className = 'f-status';
        try {
          const r = await api.updateTech(t.id, {
            active: active.checked, trade_skills: skills.value.split(/[,\s]+/).filter(Boolean), home_address: home.value,
            shift_start: start.value, shift_end: end.value, max_jobs_per_day: Number(max.value), color: color.value,
            work_days: days.filter((d) => d.querySelector('input').checked).map((d) => Number(d.querySelector('input').dataset.d)),
          });
          const nt = r.technician; form.style.setProperty('--c', nt.color);
          homeState.textContent = nt.home_lat == null ? 'not located' : 'located'; homeState.className = `chip ${nt.home_lat == null ? 'warn' : 'ok'}`;
          needs.hidden = !(nt.active && (!nt.trade_skills.length || nt.home_lat == null));
          skills.value = nt.trade_skills.join(', ');
          st.textContent = r.home_geocode === 'failed' ? 'Saved, but the home address could not be located.' : r.home_geocode === 'error' ? 'Saved; the geocoder was unreachable, home not located yet.' : 'Saved ✓';
          st.className = `f-status ${r.home_geocode === 'failed' || r.home_geocode === 'error' ? 'bad' : 'good'}`;
        } catch (err) { st.textContent = err.message; st.className = 'f-status bad'; }
        btn.disabled = false;
      } },
    h('div', { class: 'tc-head' }, h('span', { class: 'dot' }), h('h3', {}, t.name), h('code', { class: 'dim' }, t.id), needs, h('label', { class: 'switch' }, active, h('span', {}, ' Include in routing'))),
    h('div', { class: 'grid' },
      field('Trade skills', skills, 'Trade codes this tech can do. A job is only suggested to techs with its trade.'),
      field('Home base (route start)', home, null),
      field('Shift start', start), field('Shift end', end), field('Max jobs / day', max), field('Map color', color)),
    h('div', { class: 'tc-row' }, h('div', { class: 'f-label' }, 'Works'), h('div', { class: 'days' }, days), homeState),
    h('div', { class: 'tc-foot' }, btn, st));
    return form;
  }

  (async () => {
    try {
      const { technicians } = await api.technicians();
      render(page, h('h1', {}, 'Technicians'),
        note('These settings are owned by this app (Housecall Pro does not store them). Turn off "Include in routing" for office staff. New employees from Housecall Pro start switched off until you set their skills and home base.'),
        technicians.length ? technicians.map(card) : h('div', { class: 'empty' }, 'No employees yet. Run a sync first (Sync page).'));
    } catch (e) { render(page, h('div', { class: 'alert warn' }, e.message)); }
  })();
  return { destroy() {} };
}

// ===================================================================== settings
export function mountSettings(root, ctx) {
  if (ctx.user.role !== 'admin') return adminOnly(root);
  const page = h('div', { class: 'page' });
  render(root, page);
  let draft = null, durations = [];

  const num = (path, label, opts = {}) => {
    const inp = h('input', { type: 'number', value: getPath(draft, path), step: opts.step || 1, min: opts.min ?? 0,
      oninput: (e) => { if (e.target.value !== '') setPath(draft, path, Number(e.target.value)); } });
    return field(label, inp, opts.help);
  };
  const txt = (path, label, help) => field(label, h('input', { type: 'text', value: getPath(draft, path) ?? '', oninput: (e) => setPath(draft, path, e.target.value) }), help);

  function deadlineEditor() {
    const body = h('tbody');
    const rows = [];
    for (const [co, rules] of Object.entries(draft.deadline_rules)) for (const [pr, hrs] of Object.entries(rules)) rows.push([co, pr, hrs]);
    const sync = () => {
      const out = {};
      for (const tr of body.querySelectorAll('tr')) {
        const [c, p, hr] = tr.querySelectorAll('input');
        const co = c.value.trim().toUpperCase(), pr = p.value.trim();
        if (!co || !pr || hr.value === '') continue;
        (out[co] = out[co] || {})[pr] = Number(hr.value);
      }
      draft.deadline_rules = out;
    };
    const addRow = (co = '', pr = '', hrs = '') => {
      const tr = h('tr', {}, h('td', {}, h('input', { type: 'text', value: co, placeholder: 'AHS', 'aria-label': 'Company', oninput: sync })),
        h('td', {}, h('input', { type: 'text', value: pr, placeholder: 'Expedited', 'aria-label': 'Priority', oninput: sync })),
        h('td', {}, h('input', { type: 'number', min: 0.5, step: 0.5, value: hrs, 'aria-label': 'Hours', oninput: sync })),
        h('td', {}, h('button', { class: 'btn icon', type: 'button', 'aria-label': 'Remove row', onclick: () => { tr.remove(); sync(); } }, icon('x', 14))));
      body.append(tr);
    };
    rows.forEach((r) => addRow(...r));
    return h('div', {}, h('table', { class: 'tbl edit' }, h('thead', {}, h('tr', {}, h('th', {}, 'Source'), h('th', {}, 'Priority'), h('th', {}, 'Hours to schedule'), h('th'))), body),
      h('button', { class: 'btn', type: 'button', onclick: () => addRow() }, icon('plus', 14), ' Add rule'));
  }
  function durationEditor() {
    const body = h('tbody');
    const sync = () => {
      durations = [...body.querySelectorAll('tr')].map((tr) => { const [t, k, m] = tr.querySelectorAll('input'); return { trade_code: t.value.trim() || '*', keyword: k.value.trim(), minutes: Number(m.value) }; })
        .filter((d) => d.minutes > 0);
    };
    const addRow = (d = { trade_code: '', keyword: '', minutes: 60 }) => {
      const tr = h('tr', {}, h('td', {}, h('input', { type: 'text', value: d.trade_code === '*' ? '*' : d.trade_code, placeholder: 'PLB or *', 'aria-label': 'Trade', oninput: sync })),
        h('td', {}, h('input', { type: 'text', value: d.keyword, placeholder: '(trade default)', 'aria-label': 'Keyword', oninput: sync })),
        h('td', {}, h('input', { type: 'number', min: 5, max: 720, step: 5, value: d.minutes, 'aria-label': 'Minutes', oninput: sync })),
        h('td', {}, h('button', { class: 'btn icon', type: 'button', 'aria-label': 'Remove row', onclick: () => { tr.remove(); sync(); } }, icon('x', 14))));
      body.append(tr);
    };
    durations.forEach(addRow);
    return h('div', {}, h('table', { class: 'tbl edit' }, h('thead', {}, h('tr', {}, h('th', {}, 'Trade'), h('th', {}, 'Problem keyword'), h('th', {}, 'Minutes'), h('th'))), body),
      h('button', { class: 'btn', type: 'button', onclick: () => { addRow(); sync(); } }, icon('plus', 14), ' Add duration'));
  }
  const list = (path, label, help) => field(label, h('textarea', { rows: 5, oninput: (e) => setPath(draft, path, e.target.value.split('\n').map((s) => s.trim().toLowerCase()).filter(Boolean)) }, (getPath(draft, path) || []).join('\n')), help);
  const aliases = () => field('Trade aliases (ALIAS = TRADE, one per line)', h('textarea', { rows: 6, oninput: (e) => {
    const m = {}; for (const line of e.target.value.split('\n')) { const [a, b] = line.split('='); if (a && b) m[a.trim().toUpperCase()] = b.trim().toUpperCase(); } draft.trade_aliases = m; } },
  Object.entries(draft.trade_aliases).map(([a, b]) => `${a} = ${b}`).join('\n')), 'Maps job types / warranty trade codes to the codes used for technician skills.');

  async function save(e) {
    e.preventDefault(); const btn = e.submitter; if (btn) btn.disabled = true;
    try { const r = await api.saveSettings({ settings: draft, durations }); draft = deepClone(r.settings); durations = r.durations; toast('Settings saved', 'ok'); }
    catch (err) { toast(err.message, 'error', 6000); }
    if (btn) btn.disabled = false;
  }

  (async () => {
    try {
      const r = await api.settings(); draft = deepClone(r.settings); durations = r.durations;
      const sec = (title, ...k) => h('section', { class: 'card' }, h('h2', {}, title), ...k);
      render(page, h('h1', {}, 'Settings'),
        h('form', { class: 'settings', onsubmit: save },
          sec('Deadline rules', h('div', { class: 'alert warn' }, icon('alert', 14), ' These are placeholder values. Replace them with the real contact/schedule deadlines from your warranty vendor agreements; the deadline warnings and scoring depend on them.'),
            note('Hours from when a job arrives in Housecall Pro until it should be scheduled. Source keys: AHS, OTHER_WARRANTY, DIRECT. Priorities: Emergency, Expedited, Normal.'), deadlineEditor()),
          sec('Priority score', h('div', { class: 'grid' },
            num('scoring.base_by_priority.Emergency', 'Base: Emergency'), num('scoring.base_by_priority.Expedited', 'Base: Expedited'), num('scoring.base_by_priority.Normal', 'Base: Normal'),
            num('scoring.base_direct_lead', 'Base: direct lead'), num('scoring.base_other_warranty', 'Base: other warranty'),
            num('scoring.deadline_points.warning', 'Deadline <= 50% left'), num('scoring.deadline_points.critical', 'Deadline <= 25% left'), num('scoring.deadline_points.overdue', 'Deadline overdue'),
            num('scoring.urgency_points_each', 'Points per urgency keyword'), num('scoring.urgency_points_cap', 'Urgency cap'),
            num('scoring.age_points_per_day', 'Points per day waiting', { step: 0.5 }), num('scoring.age_points_cap', 'Age cap')),
          list('urgency_keywords', 'Urgency keywords (one per line)', 'Matched as whole words in the problem text.')),
          sec('Find best slot', h('div', { class: 'grid' },
            num('scheduling.search_days', 'Days to search', { min: 1 }), num('scheduling.top_n', 'Options to show', { min: 1 }),
            num('scheduling.default_duration_minutes', 'Default job minutes', { min: 5 }), num('scheduling.round_to_minutes', 'Round start times to (min)', { min: 1 }),
            num('scheduling.travel_speed_mph', 'Average drive speed (mph)', { min: 5 }), num('scheduling.travel_circuity', 'Road/straight-line factor', { step: 0.05, min: 1 }),
            num('scheduling.min_travel_minutes', 'Minimum drive (min)'), num('scheduling.same_day_lead_minutes', 'Same-day lead time (min)'),
            num('scheduling.day_penalty_minutes.Emergency', 'Delay penalty / day: Emergency'), num('scheduling.day_penalty_minutes.Expedited', 'Delay penalty / day: Expedited'),
            num('scheduling.day_penalty_minutes.Normal', 'Delay penalty / day: Normal'), num('scheduling.day_penalty_minutes.Direct', 'Delay penalty / day: Direct'),
            num('scheduling.deadline_miss_penalty', 'Penalty if past deadline')),
          note('The delay penalty is "minutes of extra driving you would accept to get the job done one day sooner". A high value for Emergency makes it prefer today.')),
          sec('Job durations', note('Longest matching keyword wins, then the trade default. Used for new jobs and for scheduled jobs that have no end time.'), durationEditor()),
          sec('Mapping & links', h('div', { class: 'grid' }, txt('timezone', 'Company timezone', 'IANA name, e.g. America/Phoenix'),
            txt('map.tile_url', 'Map tile URL', 'https only; {z}/{x}/{y}. Use a Mapbox/Google/Stadia style URL for production (OpenStreetMap’s public tiles are for light use).'),
            txt('map.attribution', 'Map attribution'), txt('links.hcp_job_url_template', 'Housecall Pro job link template', '{id} is replaced with the job id. Unverified default: check a real job URL.')),
          aliases()),
          h('div', { class: 'savebar' }, h('button', { class: 'btn primary', type: 'submit' }, 'Save settings'), h('span', { class: 'dim' }, 'Reload the Dispatch page after changing the map or timezone.'))));
    } catch (e) { render(page, h('div', { class: 'alert warn' }, e.message)); }
  })();
  return { destroy() {} };
}

// ================================================================ sync & parsing
export function mountSync(root, ctx) {
  const page = h('div', { class: 'page' });
  render(root, page);
  async function load() {
    try {
      const [s, pr] = await Promise.all([api.syncStatus(), api.parseReview()]);
      const c = s.counts;
      const stat = (label, val, cls = '') => h('div', { class: `statcard ${cls}` }, h('div', { class: 'sv' }, val), h('div', { class: 'sl' }, label));
      const runBtn = h('button', { class: 'btn primary', type: 'button', onclick: async (e) => { e.currentTarget.disabled = true; try { const r = await api.syncRun(); toast(`Sync ${r.status}`, r.status === 'error' ? 'error' : 'ok'); } catch (err) { toast(err.message, 'error'); } load(); } }, icon('refresh', 14), ' Sync now');
      render(page, h('div', { class: 'page-head' }, h('h1', {}, 'Sync & parsing'), runBtn),
        s.mode === 'mock' ? h('div', { class: 'alert info' }, h('b', {}, 'Demo mode.'), ' This app is running on built-in sample data (HCP_MODE=mock). Nothing is read from or written to Housecall Pro.') : null,
        h('div', { class: 'stats' }, stat('Housecall Pro mode', s.mode), stat('Geocoder', s.geocoder), stat('Sync every', `${Math.round(s.interval_seconds / 60)} min`),
          stat('Active jobs', c.jobs_active), stat('Unscheduled', c.unscheduled), stat('Warranty jobs', c.warranty_jobs),
          stat('Unmapped jobs', c.geocode_failed + c.geocode_pending, c.geocode_failed ? 'warn' : ''), stat('Need review', c.needs_review, c.needs_review ? 'warn' : ''),
          stat('Techs needing setup', c.techs_needing_setup, c.techs_needing_setup ? 'warn' : '')),
        h('section', { class: 'card' }, h('h2', {}, 'Warranty parsing: jobs to review'),
          pr.items.length ? h('table', { class: 'tbl' }, h('thead', {}, h('tr', {}, h('th', {}, 'Job'), h('th', {}, 'Address'), h('th', {}, 'Notes'), h('th', {}, 'Parsed by'), h('th'))),
            h('tbody', {}, pr.items.map((i) => h('tr', {}, h('td', {}, i.customer_name || i.id, h('div', { class: 'dim' }, `${i.priority || ''} · ${fmtDateTime(i.received_at)}`)),
              h('td', {}, i.address || h('span', { class: 'chip warn' }, 'none')), h('td', {}, h('ul', { class: 'warns' }, i.warnings.map((w) => h('li', {}, w)))), h('td', {}, i.parsed_by),
              h('td', {}, h('button', { class: 'btn', type: 'button', onclick: async () => { try { await api.markReviewed(i.id); load(); } catch (e) { toast(e.message, 'error'); } } }, icon('check', 14), ' Reviewed')))))) : h('p', { class: 'dim' }, 'Nothing needs review. ✓'),
          note(s.llm_enabled ? 'AI fallback is ON: jobs missing key fields are sent (names and phone numbers removed) to the configured LLM.' : 'AI fallback is off. Set LLM_API_KEY and LLM_MODEL in .env to enable it for descriptions the regex parser cannot read.')),
        h('section', { class: 'card' }, h('h2', {}, 'Recent syncs'),
          h('table', { class: 'tbl' }, h('thead', {}, h('tr', {}, h('th', {}, 'Started'), h('th', {}, 'Status'), h('th', {}, 'Jobs'), h('th', {}, 'Changed'), h('th', {}, 'Message'))),
            h('tbody', {}, s.runs.map((r) => h('tr', {}, h('td', {}, `${fmtDateTime(r.started_at)} (${timeAgo(r.started_at)})`), h('td', {}, h('span', { class: `chip ${r.status === 'ok' ? 'ok' : r.status === 'running' ? 'dim' : 'warn'}` }, r.status)),
              h('td', {}, r.jobs_seen), h('td', {}, r.jobs_changed), h('td', { class: 'dim' }, r.error || '')))))));
    } catch (e) { render(page, h('div', { class: 'alert warn' }, e.message)); }
  }
  load();
  return { destroy() {} };
}

// ======================================================================== users
export function mountUsers(root, ctx) {
  if (ctx.user.role !== 'admin') return adminOnly(root);
  const page = h('div', { class: 'page' });
  render(root, page);
  async function load() {
    try {
      const { users } = await api.users();
      const email = h('input', { type: 'email', required: true, autocomplete: 'off' }), name = h('input', { type: 'text', maxlength: 80 });
      const pw = h('input', { type: 'password', required: true, minlength: 10, autocomplete: 'new-password' });
      const role = h('select', {}, h('option', { value: 'dispatcher' }, 'Dispatcher'), h('option', { value: 'admin' }, 'Admin'));
      const form = h('form', { class: 'card inline-form', onsubmit: async (e) => {
        e.preventDefault();
        try { await api.createUser({ email: email.value, name: name.value, password: pw.value, role: role.value }); toast('User created', 'ok'); load(); } catch (err) { toast(err.message, 'error', 6000); }
      } }, h('h2', {}, 'Add a user'), h('div', { class: 'grid' }, field('Email', email), field('Name', name), field('Password (10+ characters)', pw), field('Role', role)), h('button', { class: 'btn primary', type: 'submit' }, 'Create user'));
      render(page, h('h1', {}, 'Users'), note('Dispatchers can view the map, job cards and slot suggestions and run syncs. Admins can also change technicians, settings and users.'),
        h('section', { class: 'card' }, h('table', { class: 'tbl' }, h('thead', {}, h('tr', {}, h('th', {}, 'Email'), h('th', {}, 'Name'), h('th', {}, 'Role'), h('th'))),
          h('tbody', {}, users.map((u) => h('tr', {}, h('td', {}, u.email), h('td', {}, u.name), h('td', {}, h('span', { class: `chip ${u.role === 'admin' ? 'ok' : 'dim'}` }, u.role)),
            h('td', {}, u.id === ctx.user.id ? h('span', { class: 'dim' }, 'you') : h('button', { class: 'btn', type: 'button', onclick: async () => { if (!confirm(`Delete ${u.email}?`)) return; try { await api.deleteUser(u.id); load(); } catch (err) { toast(err.message, 'error'); } } }, 'Delete'))))))),
        form);
    } catch (e) { render(page, h('div', { class: 'alert warn' }, e.message)); }
  }
  load();
  return { destroy() {} };
}
