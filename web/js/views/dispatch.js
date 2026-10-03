// Dispatch screen: map (routes + unscheduled pins), prioritized queue, job card, "find best slot" and its confirmation.
// Read-only against Housecall Pro in this phase: a confirmed slot is saved in this app and added to the technician's
// route here (a "booking"); dispatchers still enter it in HCP, and it drops off the Booked list once HCP shows it.

import { h, render, icon, svgEl, fmtTime, fmtMinutes, fmtDay, fmtDateTime, fmtDuration, fmtDrive, fmtLeft, fmtPhone,
  money, timeAgo, toast } from '../dom.js';
import { api } from '../api.js';
import { SlippyMap } from '../map.js';

const onEnter = (fn) => (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(e); } };

export function mountDispatch(root, ctx) {
  const { config } = ctx;
  const state = {
    date: config.today, data: null, filters: { q: '', type: '', area: '' },
    view: 'queue', areas: null, areasLoading: false, areasError: null, areaDays: null,
    hidden: new Set(), selectedId: null, detail: null, detailLoading: false,
    slots: null, slotsFor: null, slotsLoading: false, slotDays: 3, hoverOpt: null, pinnedOpt: null,
    slotWindow: (config.scheduling && config.scheduling.window_minutes) || 240,
    booking: false, bookNote: '', bookError: null,
    firstFit: true, lastUpdated: null, error: null,
  };

  const queue = h('aside', { class: 'dp-queue', 'aria-label': 'Unscheduled jobs' });
  const mapEl = h('div', { class: 'dp-mapcanvas' });
  const toolbar = h('div', { class: 'dp-toolbar' });
  const legend = buildLegend();
  const banner = h('div', { class: 'dp-banner', hidden: true });
  const mapPane = h('section', { class: 'dp-map', 'aria-label': 'Dispatch map' }, mapEl, toolbar, legend, banner);
  render(root, h('div', { class: 'dispatch' }, queue, mapPane));

  const map = new SlippyMap(mapEl, { tileUrl: config.map.tile_url, attribution: config.map.attribution, center: config.map.center, zoom: config.map.zoom });
  map.onBackgroundClick = () => { if (state.selectedId && !state.detailLoading) { /* keep selection; background click only dismisses tooltips */ map.hideTip(); } };

  let timer = null;

  // ------------------------------------------------------------------ data
  async function load({ quiet = false, detail = false } = {}) {
    try {
      const data = await api.dispatch(state.date);
      state.data = data; state.error = null; state.lastUpdated = new Date();
      banner.hidden = true;
    } catch (e) {
      state.error = e.message;
      banner.hidden = false;
      render(banner, icon('alert', 16), ` ${e.message}. Showing the last data${state.lastUpdated ? ` (updated ${state.lastUpdated.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })})` : ''}.`);
      if (!state.data) return;
    }
    // an area that no longer has calls (all scheduled, or grouping changed) must not stay as a hidden filter
    if (state.filters.area && !state.data.unscheduled.some((u) => u.area_key === state.filters.area)) state.filters.area = '';
    renderToolbar();
    if (!state.selectedId || detail) renderQueue();   // never re-render an open job card on background refresh
    renderMap(state.firstFit);
    state.firstFit = false;
    if (state.view === 'areas') loadAreas({ quiet: true });
    if (!quiet && state.error) toast(state.error, 'error');
  }

  function filtered() {
    if (!state.data) return [];
    const f = state.filters, q = f.q.trim().toLowerCase();
    return state.data.unscheduled.filter((u) => {
      if (f.type && u.type_label !== f.type) return false;
      if (f.area && u.area_key !== f.area) return false;
      if (q) {
        const hay = `${u.customer_name} ${u.address} ${u.summary} ${u.zip} ${u.city} ${(u.warranty && u.warranty.dispatch_number) || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  // --------------------------------------------------------------- toolbar
  function renderToolbar() {
    const d = state.data;
    const prev = h('button', { class: 'btn icon', type: 'button', 'aria-label': 'Previous day', onclick: () => shiftDay(-1) }, icon('left'));
    const next = h('button', { class: 'btn icon', type: 'button', 'aria-label': 'Next day', onclick: () => shiftDay(1) }, icon('right'));
    const input = h('input', { type: 'date', value: state.date, 'aria-label': 'Schedule date', onchange: (e) => { if (e.target.value) { state.date = e.target.value; load({ detail: true }); } } });
    const today = h('button', { class: 'btn', type: 'button', disabled: state.date === d.today, onclick: () => { state.date = d.today; load({ detail: true }); } }, 'Today');
    const stats = h('div', { class: 'dp-stats' },
      h('span', { class: 'stat' }, h('b', {}, d.stats.unscheduled), ' unscheduled'),
      d.stats.overdue ? h('span', { class: 'stat bad' }, h('b', {}, d.stats.overdue), ' overdue') : null,
      d.stats.unmapped ? h('span', { class: 'stat dim', title: 'Jobs without a map location (see the job card)' }, h('b', {}, d.stats.unmapped), ' unmapped') : null,
      h('span', { class: 'stat' }, h('b', {}, d.stats.scheduled_today), ' scheduled this day'),
      d.stats.completed_today ? h('span', { class: 'stat done', title: 'Marked complete in Housecall Pro' }, h('b', {}, d.stats.completed_today), ' completed') : null,
      d.bookings.length ? h('span', { class: 'stat', title: 'Confirmed in this app, not in Housecall Pro yet (Booked tab)' }, h('b', {}, d.bookings.length), ' booked here') : null);
    const chips = h('div', { class: 'dp-techs', role: 'group', 'aria-label': 'Technicians' },
      d.technicians.map((t) => h('button', {
        class: `tech-chip${state.hidden.has(t.id) ? ' off' : ''}${t.active ? '' : ' inactive'}`, type: 'button', 'aria-pressed': String(!state.hidden.has(t.id)),
        style: { '--c': t.color },
        title: t.needs_setup ? 'Needs trade skills and a home base: Admin > Technicians' : `${t.name}: click to show/hide`,
        onclick: () => { state.hidden.has(t.id) ? state.hidden.delete(t.id) : state.hidden.add(t.id); renderToolbar(); renderMap(false); },
      }, h('span', { class: 'dot' }), h('span', { class: 'tc-name' }, t.name),
      h('span', { class: 'tc-meta', title: driveTotal(t) != null ? 'Total drive time by road' : 'Straight-line estimate' }, `${t.job_count} job${t.job_count === 1 ? '' : 's'}${t.done_count ? ` · ${t.done_count} done` : ''}${(driveTotal(t) ?? t.drive_min) ? ` · ${Math.round(driveTotal(t) ?? t.drive_min)} min drive` : ''}`),
      t.needs_setup ? icon('alert', 13) : null)));
    const sync = h('div', { class: 'dp-sync' },
      h('span', { class: 'dim' }, d.last_sync ? `Synced ${timeAgo(d.last_sync.finished_at || d.last_sync.started_at)}${d.last_sync.status === 'error' ? ' (failed)' : ''}` : 'Never synced'),
      h('button', { class: 'btn icon', type: 'button', title: 'Sync with Housecall Pro now', 'aria-label': 'Sync now', onclick: syncNow }, icon('refresh')));
    render(toolbar, h('div', { class: 'dp-row' }, h('div', { class: 'dp-date' }, prev, input, next, today), stats, sync), chips);
  }
  function shiftDay(n) {
    const dt = new Date(state.date + 'T12:00:00Z'); dt.setUTCDate(dt.getUTCDate() + n);
    state.date = dt.toISOString().slice(0, 10); load({ detail: true });
  }
  async function syncNow(e) {
    const btn = e.currentTarget; btn.disabled = true; btn.classList.add('spin');
    try { const r = await api.syncRun(); toast(`Synced: ${r.jobs_seen ?? 0} jobs, ${r.jobs_changed ?? 0} changed${r.status === 'partial' ? ' (some errors)' : ''}`, r.status === 'error' ? 'error' : 'ok'); }
    catch (err) { toast(err.message, 'error'); }
    btn.disabled = false; btn.classList.remove('spin');
    load({ quiet: true });
  }

  // ------------------------------------------------------------------ queue
  function renderQueue() {
    const keep = queue.querySelector('.q-list'); const scroll = keep ? keep.scrollTop : 0;
    if (state.selectedId) { renderDetail(); return; }
    if (state.view === 'areas') { renderAreas(); return; }
    if (state.view === 'booked') { renderBooked(); return; }
    const list = filtered();
    const sel = (key, label, opts) => h('select', { 'aria-label': label, onchange: (e) => { state.filters[key] = e.target.value; renderQueue(); renderMap(false); } },
      h('option', { value: '' }, label), opts.map(([v, t]) => h('option', { value: v, selected: state.filters[key] === v }, t)));
    const search = h('input', { type: 'search', placeholder: 'Search name, address, problem, zip…', value: state.filters.q, 'aria-label': 'Search unscheduled jobs',
      oninput: (e) => { state.filters.q = e.target.value; clearTimeout(search._t); search._t = setTimeout(() => { renderQueue(); renderMap(false); search2Focus(); }, 180); } });
    const search2Focus = () => { const s = queue.querySelector('input[type=search]'); if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); } };
    const head = h('header', { class: 'q-head' },
      viewTabs(),
      h('div', { class: 'q-title' }, h('h2', {}, 'Unscheduled'), h('span', { class: 'count' }, list.length === state.data.unscheduled.length ? `${list.length}` : `${list.length} of ${state.data.unscheduled.length}`)),
      search,
      h('div', { class: 'q-filters' },
        sel('type', 'All types', typeOptions()),
        sel('area', 'All areas', areaOptions())));
    const body = h('div', { class: 'q-list', role: 'list' },
      list.length ? list.map(queueRow) : h('div', { class: 'empty' }, state.data.unscheduled.length ? 'No jobs match these filters.' : 'No unscheduled jobs. Nice work.'));
    render(queue, head, body);
    body.scrollTop = scroll;
  }

  // Running total per area, straight from the queue the page already has (no extra request).
  function areaOptions() {
    const m = new Map();
    for (const u of state.data.unscheduled) { const a = m.get(u.area_key) || { label: u.area, n: 0 }; a.n += 1; m.set(u.area_key, a); }
    return [...m.entries()].sort((a, b) => b[1].n - a[1].n || a[1].label.localeCompare(b[1].label)).map(([k, a]) => [k, `${a.label} (${a.n})`]);
  }
  // Expedited / Normal / Recall / Retail with how many of each are waiting (types with none are left out)
  const TYPES = ['Expedited', 'Normal', 'Recall', 'Retail'];
  function typeOptions() {
    const n = Object.fromEntries(TYPES.map((t) => [t, 0]));
    for (const u of state.data.unscheduled) n[u.type_label] = (n[u.type_label] || 0) + 1;
    return TYPES.filter((t) => n[t]).map((t) => [t, `${t} (${n[t]})`]);
  }
  function viewTabs() {
    const tab = (id, label, count) => h('button', {
      class: `q-tab${state.view === id ? ' on' : ''}`, type: 'button', role: 'tab', 'aria-selected': String(state.view === id),
      onclick: () => { if (state.view === id) return; state.view = id; if (id === 'areas') loadAreas(); renderQueue(); },
    }, label, h('span', { class: 'count' }, count));
    return h('div', { class: 'q-tabs', role: 'tablist', 'aria-label': 'Queue view' },
      tab('queue', 'Queue', state.data.unscheduled.length), tab('areas', 'Areas', areaOptions().length),
      tab('booked', 'Booked', state.data.bookings.length));
  }

  // ----------------------------------------------------------------- booked
  // Slots confirmed in this app that Housecall Pro does not show yet: the dispatcher's "enter these in HCP" list.
  function renderBooked() {
    const list = state.data.bookings, today = state.data.today;
    const head = h('header', { class: 'q-head' }, viewTabs(),
      h('div', { class: 'q-title' }, h('h2', {}, 'Booked here'), h('span', { class: 'count' }, String(list.length))));
    const card = (b) => {
      const open = () => openBooked(b);
      return h('div', { class: 'bk-card', role: 'listitem', tabindex: '0', style: { '--c': b.tech_color || '#2563eb' }, 'data-id': b.job_id, onclick: open, onkeydown: onEnter(open) },
        h('div', { class: 'bk-top' }, h('span', { class: 'dot' }), h('b', {}, b.tech_name),
          h('span', { class: 'bk-when' }, `${dayWord(b.date, today)} · ${fmtMinutes(b.window_start_min)} – ${fmtMinutes(b.window_end_min)}`)),
        h('div', { class: 'q-name' }, b.customer_name || 'Unknown customer'),
        h('div', { class: 'q-addr' }, b.address || 'No address on this job'),
        h('div', { class: 'bk-meta dim' }, `Arrive about ${fmtMinutes(b.arrive_min)} · ${fmtDuration(b.duration_min)} · booked${b.booked_by ? ` by ${b.booked_by}` : ''} ${timeAgo(b.booked_at)}`),
        b.note ? h('div', { class: 'bk-note' }, b.note) : null,
        h('div', { class: 'bk-actions' },
          b.hcp_url ? h('a', { class: 'btn', href: b.hcp_url, target: '_blank', rel: 'noopener noreferrer', onclick: (e) => e.stopPropagation() }, icon('link', 13), ' Open in Housecall Pro') : null,
          h('button', { class: 'btn', type: 'button', onclick: (e) => { e.stopPropagation(); removeBooking(b.job_id); } }, icon('x', 12), ' Remove')));
    };
    const body = h('div', { class: 'q-list bk-list', role: 'list' },
      h('p', { class: 'dim hint bk-hint' }, 'Confirmed here but still unscheduled in Housecall Pro. Enter each one in Housecall Pro so it reaches the technician; it leaves this list once Housecall Pro shows it scheduled.'),
      list.length ? list.map(card) : h('div', { class: 'empty' }, 'Nothing booked yet. Open a job, choose Find best slot, then confirm a slot.'));
    const keep = queue.querySelector('.q-list'); const scroll = keep ? keep.scrollTop : 0;
    render(queue, head, body);
    body.scrollTop = scroll;
  }
  function openBooked(b) {
    if (b.date !== state.date) { state.date = b.date; load({ quiet: true }); }
    select(b.job_id);
  }
  async function removeBooking(id) {
    if (!window.confirm('Remove this booking? The call goes back to the unscheduled queue. Housecall Pro is not changed.')) return;
    try { await api.unbook(id); toast('Booking removed: the call is back in the queue', 'ok'); }
    catch (e) { toast(e.message, 'error'); }
    if (state.selectedId === id) await refreshJob(id);
    else { await load({ quiet: true, detail: true }); if (state.areas) loadAreas({ quiet: true }); }
  }

  // ------------------------------------------------------------------ areas
  let areasSeq = 0;
  async function loadAreas({ quiet = false } = {}) {
    const seq = ++areasSeq;
    state.areasLoading = true; state.areasError = null;
    if (!quiet && !state.selectedId && state.view === 'areas') renderQueue();
    try {
      const r = await api.areas(state.areaDays);
      if (seq !== areasSeq) return;
      state.areas = r; if (state.areaDays == null) state.areaDays = r.days;
    } catch (e) { if (seq !== areasSeq) return; state.areasError = e.message; }
    state.areasLoading = false;
    if (!state.selectedId && state.view === 'areas') renderQueue();
  }
  const isoPlusDays = (iso, n) => { const d = new Date(iso + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const dayWord = (date, today) => (date === today ? 'Today' : date === isoPlusDays(today, 1) ? 'Tomorrow' : fmtDay(date));

  function renderAreas() {
    const a = state.areas;
    const scroll = (queue.querySelector('.q-list') || { scrollTop: 0 }).scrollTop;
    const daysSel = h('select', { 'aria-label': 'Days to look ahead', onchange: (e) => { state.areaDays = Number(e.target.value); loadAreas(); } },
      [1, 2, 3, 5, 7, 14].map((n) => h('option', { value: n, selected: n === (state.areaDays ?? (a ? a.days : 3)) }, `${n} day${n === 1 ? '' : 's'}`)));
    const active = state.filters.area && (a ? a.areas.find((x) => x.key === state.filters.area) : null);
    const head = h('header', { class: 'q-head' }, viewTabs(),
      h('div', { class: 'q-title' }, h('h2', {}, 'Calls by area'),
        h('span', { class: 'count' }, a ? `${a.totals.unscheduled} calls · ${a.totals.areas} area${a.totals.areas === 1 ? '' : 's'}` : '')),
      h('div', { class: 'q-filters ar-ctl' },
        h('label', { class: 'dim' }, 'Openings in the next ', daysSel),
        h('button', { class: 'btn icon', type: 'button', title: 'Refresh', 'aria-label': 'Refresh areas', onclick: () => loadAreas() }, icon('refresh', 14)),
        active ? h('button', { class: 'btn', type: 'button', onclick: () => { state.filters.area = ''; renderQueue(); renderMap(false); } }, icon('x', 12), ` Showing ${active.label}`) : null));
    let body;
    if (!a) body = h('div', { class: 'empty' }, state.areasError ? h('span', {}, icon('alert', 14), ' ', state.areasError) : 'Loading…');
    else if (!a.areas.length) body = h('div', { class: 'empty' }, 'No unscheduled calls. Nice work.');
    else body = h('div', { class: 'q-list ar-list', role: 'list' },
      state.areasError ? h('div', { class: 'alert warn' }, icon('alert', 14), ` ${state.areasError}. Showing the last numbers.`) : null,
      a.notes.map((n) => h('div', { class: 'alert warn' }, icon('alert', 14), ' ', n)),
      h('p', { class: 'dim hint ar-hint' }, 'Each call is checked on its own against technician skills, shifts and today’s routes, so openings show where someone can go soonest, not a booking plan. Tap an area to see just those calls.'),
      a.areas.map((x) => areaCard(x, a)));
    render(queue, head, body);
    const list = queue.querySelector('.q-list'); if (list) list.scrollTop = scroll;
  }
  function areaCard(x, a) {
    const open = () => { state.filters.area = x.key; state.view = 'queue'; renderQueue(); renderMap(false); };
    const when = (t) => `${dayWord(t.date, a.today)} · ${fmtMinutes(t.window_start_min)}–${fmtMinutes(t.window_end_min)}`;
    const chips = TYPES.filter((t) => x.by_type[t]).map((t) => badge(`${x.by_type[t]} ${t}`, 'type'));
    if (x.unlocated) chips.push(h('span', { class: 'chip dim', title: 'No map location, so openings cannot be checked for these' }, icon('mapoff', 12), ` ${x.unlocated} no location`));
    const checkable = x.count - x.unlocated;
    const e = x.earliest;
    const soon = e && e.date <= isoPlusDays(a.today, 1);
    const avail = e
      ? h('div', { class: `ar-open${soon ? ' soon' : ''}` }, icon('clock', 13), ' Earliest opening ', h('b', {}, when(e)))
      : h('div', { class: 'ar-open none' }, checkable ? `No technician has an opening in the next ${a.days} day${a.days === 1 ? '' : 's'}` : 'No map location, so openings cannot be checked');
    const techs = x.techs.length ? h('ul', { class: 'ar-techs' }, x.techs.map((t) => h('li', { style: { '--c': t.color } },
      h('span', { class: 'dot' }), h('b', {}, t.name), ` ${when(t)}`,
      h('span', { class: 'dim' }, ` · can take ${t.eligible_jobs} of ${checkable}${t.added_drive_min ? `, +${Math.round(t.added_drive_min)} min driving` : ''}`)))) : null;
    return h('div', { class: `ar-card${state.filters.area === x.key ? ' sel' : ''}`, role: 'listitem', tabindex: '0', title: 'Show only these calls in the queue and on the map', onclick: open, onkeydown: onEnter(open) },
      h('div', { class: 'ar-top' }, h('span', { class: 'ar-name' }, x.label), h('span', { class: 'ar-count' }, h('b', {}, x.count), x.count === 1 ? ' call' : ' calls')),
      h('div', { class: 'q-chips' }, chips), avail, techs);
  }

  function badge(text, cls = '', title = '') { return h('span', { class: `badge ${cls}`, title }, text); }
  // One row per waiting call: the same red "!" as its pin on the map, its type, the customer and the address.
  // Everything else (trade, deadline, score, warranty details) is on the job card once it is opened.
  function queueRow(u) {
    const open = () => select(u.id);
    return h('div', {
      class: `q-row${u.id === state.selectedId ? ' sel' : ''}`, role: 'listitem', tabindex: '0', 'data-id': u.id,
      onclick: open, onkeydown: onEnter(open),
      onmouseenter: () => { map.highlight(u.id, true); }, onmouseleave: () => map.highlight(u.id, false),
      onfocus: () => map.highlight(u.id, true), onblur: () => map.highlight(u.id, false),
    },
    h('span', { class: 'q-mark', 'aria-hidden': 'true' }, '!'),
    h('div', { class: 'q-main' },
      h('div', { class: 'q-top' }, h('span', { class: 'q-type' }, u.type_label), h('span', { class: 'q-name' }, u.customer_name || 'Unknown customer')),
      h('div', { class: 'q-addr' }, u.address || 'No address on this job'),
      u.lat == null ? h('div', { class: 'q-noloc' }, icon('mapoff', 12), ' No map location yet') : null));
  }

  // ------------------------------------------------------------------- detail
  async function select(id) {
    state.selectedId = id; state.detail = null; state.detailLoading = true;
    state.slots = null; state.slotsFor = null; state.hoverOpt = state.pinnedOpt = null; state.bookNote = ''; state.bookError = null;
    renderQueue(); renderMap(false);
    const u = state.data && (state.data.unscheduled.find((x) => x.id === id) || state.data.bookings.find((x) => x.job_id === id));
    if (u && u.lat != null) map.panTo(u.lat, u.lng);
    try { state.detail = await api.job(id); } catch (e) { toast(e.message, 'error'); state.selectedId = null; }
    state.detailLoading = false;
    renderQueue(); renderMap(false);
  }
  function back() {
    state.selectedId = null; state.detail = null; state.slots = null; state.slotsFor = null; state.hoverOpt = state.pinnedOpt = null;
    state.bookNote = ''; state.bookError = null;
    renderQueue(); renderMap(false);
  }

  const kv = (k, v) => (v == null || v === '' ? null : h('div', { class: 'kv' }, h('dt', {}, k), h('dd', {}, v)));
  const section = (title, ...kids) => h('section', { class: 'd-sec' }, h('h3', {}, title), ...kids);

  function renderDetail() {
    const u = state.data.unscheduled.find((x) => x.id === state.selectedId);
    const d = state.detail;
    const backBtn = h('button', { class: 'btn ghost', type: 'button', onclick: back }, icon('left', 14), state.view === 'booked' ? ' Booked' : ' Unscheduled');
    if (!d) { render(queue, h('header', { class: 'q-head' }, backBtn), h('div', { class: 'd-body' }, h('div', { class: 'empty' }, state.detailLoading ? 'Loading…' : 'Job not found.'))); return; }
    const isWarranty = d.type.kind === 'warranty';
    const w = isWarranty ? d.warranty : null, sc = d.score;       // a retail job's old warranty text must not raise warranty alerts
    const hdr = h('header', { class: 'q-head d-head' },
      h('div', { class: 'd-toprow' }, backBtn, d.hcp_url ? h('a', { class: 'btn ghost', href: d.hcp_url, target: '_blank', rel: 'noopener noreferrer' }, icon('link', 14), ' Open in Housecall Pro') : null),
      h('h2', { class: 'd-name' }, d.customer_name || 'Unknown customer'),
      h('div', { class: 'd-badges' }, badge(d.type.label, 'type', isWarranty ? 'Warranty call' : 'Not a warranty-company call'),
        d.type.ad_lead ? badge('Ad lead', 'type', `Tagged “${d.type.ad_tag}”`) : null,
        d.trade_code ? badge(d.trade_code, 'trade') : null, d.work_status !== 'unscheduled' ? badge(d.work_status.replace('_', ' '), 'dim') : null, d.booking ? badge('booked here', 'type') : null));

    const alerts = [];
    if (w && w.do_not_collect_service_fee) alerts.push(h('div', { class: 'alert fee' }, h('b', {}, 'Do not collect the trade service fee.'), w.payment_type ? ` Payment type: ${w.payment_type}.` : ''));
    if (w && w.authorization_required) alerts.push(h('div', { class: 'alert warn' }, icon('alert', 14), ' Authorization is required before work starts.'));
    if (d.type.warranty_text_without_tag) alerts.push(h('div', { class: 'alert info' }, icon('alert', 14), ' This job carries warranty dispatch text but no warranty tag, so it is treated as Retail (a warranty call turned into a retail job). If it should be warranty work, tag it in Housecall Pro.'));
    if (d.lat == null) alerts.push(h('div', { class: 'alert warn' }, icon('mapoff', 14), ` No map location (${d.geocode_status}). Check the address in Housecall Pro; slots cannot be computed without one.`));

    const dl = sc.deadline_status === 'excused'
      ? h('div', { class: 'd-deadline dl-excused' }, icon('clock', 14), ' Deadline waived · ', h('b', {}, sc.exception.reason_label))
      : sc.deadline_status !== 'none'
        ? h('div', { class: `d-deadline dl-${sc.deadline_status}` }, icon('clock', 14), ` Target ${fmtDateTime(sc.deadline_at)} · `, h('b', {}, fmtLeft(sc.deadline_hours_left)))
        : h('div', { class: 'd-deadline dim' }, 'No deadline clock for this job type (Admin > Settings).');
    const scoreCard = h('div', { class: 'd-score' }, h('div', { class: 'big' }, Math.round(sc.total), h('small', {}, 'priority score')), dl);

    // --- slot finder
    const daysSel = h('select', { 'aria-label': 'Days to search', onchange: (e) => { state.slotDays = Number(e.target.value); if (state.slots) findSlots(); } },
      [1, 2, 3, 5, 7, 14].map((n) => h('option', { value: n, selected: n === state.slotDays }, `${n} day${n === 1 ? '' : 's'}`)));
    // arrival window offered to the customer: the company standard (Admin > Settings), changeable for this job
    const windowSel = h('select', { 'aria-label': 'Arrival window length', onchange: (e) => { state.slotWindow = Number(e.target.value); if (state.slots) findSlots(); } },
      [...new Set([60, 120, 180, 240, 300, 360, 480, state.slotWindow])].sort((x, y) => x - y)
        .map((m) => h('option', { value: m, selected: m === state.slotWindow }, fmtDuration(m))));
    const slotPanel = d.booking ? null : h('section', { class: 'd-sec slots' },
      h('div', { class: 'slots-head' }, h('h3', {}, 'Best slots'),
        h('div', { class: 'slots-ctl' }, h('label', { class: 'dim', title: 'How long a time window the customer is given to expect the technician in' }, 'Window ', windowSel),
          h('label', { class: 'dim' }, 'Search ', daysSel),
          h('button', { class: 'btn primary', type: 'button', disabled: state.slotsLoading || d.lat == null || d.work_status !== 'unscheduled', onclick: findSlots },
            icon('route', 14), state.slotsLoading ? ' Finding…' : ' Find best slot'))),
      renderSlots(d));

    const items = w && w.items.length
      ? h('ul', { class: 'items' }, w.items.map((i) => h('li', {}, h('b', {}, i.name), i.problem ? ` – ${i.problem}` : '', i.area_of_home ? h('span', { class: 'dim' }, ` (${i.area_of_home})`) : null, i.status ? h('span', { class: 'chip dim' }, i.status) : null)))
      : h('p', { class: 'plain' }, d.summary || 'No description.');

    const phones = d.contact_phones.map((p) => h('a', { class: 'phone', href: `tel:${String(p).replace(/\D/g, '')}` }, icon('phone', 13), ' ', fmtPhone(p)));
    const contact = section('Customer', h('dl', {}, kv('Address', d.address || '(none)'), kv('Phone', phones.length ? h('span', { class: 'phones' }, phones) : null),
      kv('Received', fmtDateTime(d.received_at)), kv('Lead source', d.lead_source), kv('Job type', d.job_type), kv('Estimated time', fmtDuration(d.estimated_minutes))));

    const warranty = w ? section('Warranty details', h('dl', {},
      kv('Company', w.company), kv('Dispatch #', w.dispatch_number), kv('Plan', w.plan_name), kv('Priority (from body)', w.dispatch_priority),
      kv('Payment', w.total != null ? `${money(w.total)} total · ${money(w.paid)} paid · ${money(w.remaining)} remaining` : null),
      kv('Payment type', w.payment_type), kv('Completion date', w.completion_date_required ? 'Must be reported to the warranty company' : null),
      kv('Recall', w.recall_applies ? 'Recall period applies' : null), kv('Service request', w.service_request_id), kv('Contract', w.contract_id)),
      h('div', { class: 'links' },
        w.authorization_link ? h('a', { class: 'btn', href: w.authorization_link, target: '_blank', rel: 'noopener noreferrer' }, icon('link', 14), ' Authorization link') : null,
        w.dispatch_me_links.map((u2, i) => h('a', { class: 'btn', href: u2, target: '_blank', rel: 'noopener noreferrer' }, icon('link', 14), ` Dispatch.me ${i + 1}`)))) : null;

    const breakdown = section('Why this score', h('table', { class: 'tbl' }, h('tbody', {},
      sc.breakdown.map((b) => h('tr', {}, h('td', {}, b.label), h('td', { class: 'num' }, `+${b.points}`))),
      h('tr', { class: 'total' }, h('td', {}, 'Total'), h('td', { class: 'num' }, sc.total)))));

    const warnings = w && w.parse_warnings.length
      ? section('Parsing notes', h('ul', { class: 'warns' }, w.parse_warnings.map((x) => h('li', {}, icon('alert', 13), ' ', x))),
        w.parsed_by === 'ai' ? h('p', { class: 'dim' }, 'Some fields were filled in by the AI fallback; please verify.') : null) : null;

    const raw = h('details', { class: 'd-sec raw' }, h('summary', {}, 'Original text from Housecall Pro (description and warranty notes)'), h('pre', {}, d.description_raw || '(empty)'));

    render(queue, hdr, h('div', { class: 'd-body' }, alerts, d.booking ? [bookingPanel(d), scoreCard] : [scoreCard, exceptionPanel(d), slotPanel],
      section('Problem', items), contact, warranty, breakdown, warnings, raw));
    const box = queue.querySelector('.confirm');          // a re-render starts the panel at the top: keep the confirm button in view
    if (box) box.scrollIntoView({ block: 'nearest' });
  }

  // --------------------------------------------------------------- booked job
  function bookingPanel(d) {
    const b = d.booking;
    return h('section', { class: 'd-sec booked', style: { '--c': b.tech_color || '#2563eb' } },
      h('h3', {}, icon('check', 13), ' Booked'),
      h('div', { class: 'bk-what' }, h('span', { class: 'dot' }), h('b', {}, b.tech_name), ` · ${fmtDay(b.date, { weekday: 'long', month: 'short', day: 'numeric' })}`),
      h('dl', {},
        kv('Customer is told', `between ${fmtMinutes(b.window_start_min)} and ${fmtMinutes(b.window_end_min)}`),
        kv('Planned arrival', `about ${fmtMinutes(b.arrive_min)}, done around ${fmtMinutes(b.end_min)}`),
        kv('Note', b.note), kv('Booked', `${b.booked_by ? `by ${b.booked_by} · ` : ''}${fmtDateTime(b.booked_at)}`)),
      h('div', { class: 'alert warn' }, icon('alert', 14), ' Saved in this app only. Housecall Pro does not know about it yet: schedule it there too so it reaches the technician. It leaves the Booked list once Housecall Pro shows it scheduled.'),
      h('div', { class: 'exc-actions' }, h('button', { class: 'btn', type: 'button', onclick: () => removeBooking(d.id) }, icon('x', 12), ' Remove booking')));
  }

  // ------------------------------------------------------------ deadline waived
  // The deadline is a target. A dispatcher records why a job is being booked later (customer not available, ...):
  // it stops counting as overdue and slots are no longer ranked against the deadline. Saved in this app only.
  function exceptionPanel(d) {
    const sc = d.score, ex = sc.exception;
    if (d.work_status !== 'unscheduled' || (sc.deadline_status === 'none' && !ex)) return null;
    const reason = h('select', { 'aria-label': 'Reason' }, h('option', { value: '' }, 'Why is the deadline being waived?'),
      (config.exception_reasons || []).map((r) => h('option', { value: r.code, selected: !!ex && ex.reason === r.code }, r.label)));
    const note = h('input', { type: 'text', maxlength: 300, placeholder: 'Note (required for “Other”)', 'aria-label': 'Note', value: ex ? ex.note : '' });
    const st = h('span', { class: 'f-status', role: 'status' });
    const fail = (msg) => { st.textContent = msg; st.className = 'f-status bad'; };
    const save = h('button', { class: 'btn primary', type: 'submit' }, ex ? 'Update' : 'Save');
    const form = h('form', { class: 'exc-form', onsubmit: async (e) => {
      e.preventDefault();
      if (!reason.value) return fail('Choose a reason.');
      save.disabled = true;
      try { await api.setException(d.id, { reason: reason.value, note: note.value }); toast('Deadline waived', 'ok'); await refreshJob(d.id); }
      catch (err) { fail(err.message); save.disabled = false; }
    } }, reason, note, h('div', { class: 'exc-actions' }, save,
      ex ? h('button', { class: 'btn', type: 'button', onclick: async () => {
        try { await api.clearException(d.id); toast('Back on the normal deadline', 'ok'); await refreshJob(d.id); } catch (err) { fail(err.message); }
      } }, 'Remove') : null, st));
    const intro = ex
      ? h('p', { class: 'plain' }, h('b', {}, ex.reason_label), ex.note ? ` – ${ex.note}` : '', h('span', { class: 'dim' }, ` · marked ${ex.set_by ? `by ${ex.set_by} ` : ''}${fmtDateTime(ex.set_at)}`))
      : h('p', { class: 'dim hint' }, 'The deadline is a target, not a hard limit. If the customer isn’t available, or there’s another reason to book later, record it here: the job stops counting as overdue and slots are no longer ranked against the deadline. Saved in this app only; nothing is written to Housecall Pro.');
    return h('details', { class: 'd-sec exc', open: !!ex }, h('summary', {}, ex ? 'Deadline waived' : 'Booking this past its deadline?'), intro, form);
  }
  async function refreshJob(id) {
    try { state.detail = await api.job(id); } catch (e) { toast(e.message, 'error'); }
    state.slots = null; state.slotsFor = null; state.hoverOpt = state.pinnedOpt = null;   // ranking depends on the exception
    await load({ quiet: true, detail: true });                                              // queue chips, pins, stats, card
    if (state.areas) loadAreas({ quiet: true });
  }

  // ----------------------------------------------------------------- slots UI
  async function findSlots() {
    const id = state.selectedId; state.slotsLoading = true; state.hoverOpt = state.pinnedOpt = null; state.bookError = null;
    renderQueue();
    try { state.slots = await api.slots(id, state.slotDays, state.slotWindow); state.slotsFor = id; }
    catch (e) { toast(e.message, 'error'); state.slots = null; }
    state.slotsLoading = false;
    if (state.selectedId === id) { renderQueue(); renderMap(false); }
  }
  function renderSlots(d) {
    const s = state.slots;
    if (!s || state.slotsFor !== d.id) return h('p', { class: 'dim hint' }, 'Finds the cheapest place in each technician’s day (least added driving) and offers an arrival window to book. Windows may overlap, and a nearby job is offered the same window. Skills, shifts, existing windows and priority are respected. Pick a slot to preview it on the map, then confirm it to add the job to that technician’s route. Saved in this app only: nothing is written to Housecall Pro.');
    const out = [state.bookError ? h('div', { class: 'alert warn', role: 'alert' }, icon('alert', 14), ' ', state.bookError) : null,
      h('p', { class: 'dim hint' }, `${fmtDuration(s.duration_min)} job · ${fmtDuration(s.window_minutes)} arrival windows (they may overlap) · searched ${s.search_days} day${s.search_days === 1 ? '' : 's'} · ranking uses straight-line drive estimates`)];
    if (!s.options.length) out.push(h('div', { class: 'alert warn' }, icon('alert', 14), ' ', s.notes[0] || 'No feasible slot found.'));
    s.options.forEach((o, i) => { out.push(slotCard(o, i)); if (isPinned(o)) out.push(confirmBox(o, s)); });
    if (s.notes.length && s.options.length) s.notes.forEach((n) => out.push(h('p', { class: 'dim hint' }, n)));
    if (s.ineligible.length) out.push(h('details', { class: 'inel' }, h('summary', {}, `${s.ineligible.length} technician${s.ineligible.length === 1 ? '' : 's'} not suggested`),
      h('ul', {}, s.ineligible.map((x) => h('li', {}, h('b', {}, x.name), ` – ${x.reason}`)))));
    return h('div', { class: 'slot-list' }, out);
  }
  // "Same window as Jane (5 min away)" when the new job gets exactly the neighbour's window, else the overlap is spelled out
  function stackText(list) {
    const one = (x) => `${x.same_window ? 'same window as ' : 'overlaps '}${x.label || 'another job'}${x.same_window ? '' : `’s ${fmtMinutes(x.window_start_min)}–${fmtMinutes(x.window_end_min)} window`} (${Math.round(x.drive_min)} min away)`;
    const text = list.slice(0, 2).map(one).join('; ') + (list.length > 2 ? `; and ${list.length - 2} more nearby` : '');
    return text.charAt(0).toUpperCase() + text.slice(1);
  }
  const isPinned = (o) => !!state.pinnedOpt && state.pinnedOpt.tech_id === o.tech_id && state.pinnedOpt.date === o.date;
  function slotCard(o, i) {
    const pinned = isPinned(o);
    const pick = () => { if (state.booking) return; state.bookError = null; state.pinnedOpt = pinned ? null : o; if (!pinned && o.date !== state.date) { state.date = o.date; load({ detail: true }); } else { renderQueue(); renderMap(false); } };
    return h('div', {
      class: `slot${pinned ? ' pinned' : ''}${o.misses_deadline ? ' miss' : ''}`, role: 'button', tabindex: '0', style: { '--c': o.tech_color || '#2563eb' },
      onclick: pick, onkeydown: onEnter(pick),
      onmouseenter: () => { state.hoverOpt = o; renderMap(false); }, onmouseleave: () => { state.hoverOpt = null; renderMap(false); },
    },
    h('div', { class: 'slot-top' }, h('span', { class: 'dot' }), h('b', {}, o.tech_name), h('span', { class: 'slot-when', title: 'Arrival window to give the customer' }, `${fmtDay(o.date)} · ${fmtMinutes(o.window_start_min)} – ${fmtMinutes(o.window_end_min)}`), i === 0 ? badge('best', 'best') : null),
    h('div', { class: 'slot-meta' }, h('b', {}, `Arrive about ${fmtMinutes(o.start_min)}`), ` · stop ${o.position} of ${o.stops_in_day + 1} · `, h('b', {}, `+${Math.round(o.added_drive_min)} min driving`),
      ` · ${Math.round(o.drive_in_min)} min from ${!o.after_stop_id ? (o.origin && o.origin.kind === 'complete' ? 'the last finished job' : o.origin && o.origin.kind === 'in_progress' ? 'the job under way' : 'home') : 'the stop before'}${o.before_stop_id ? `, ${Math.round(o.drive_out_min)} min to the next stop` : ''}`),
    o.stacked_with && o.stacked_with.length ? h('div', { class: 'slot-stack' }, icon('check', 12), ' ', stackText(o.stacked_with)) : null,
    o.misses_deadline ? h('div', { class: 'slot-warn' }, icon('alert', 12), ' Finishes after the deadline target') : null,
    o.date !== state.date ? h('div', { class: 'slot-hint dim' }, 'Click to view this day on the map') : null);
  }

  // Nothing is booked by picking a slot: it is previewed on the map, and only this box's button books it.
  function confirmBox(o, s) {
    const row = (k, v) => h('div', { class: 'kv' }, h('dt', {}, k), h('dd', {}, v));
    const note = h('input', { type: 'text', maxlength: 300, placeholder: 'Note (optional), e.g. customer prefers mornings', 'aria-label': 'Booking note', value: state.bookNote,
      oninput: (e) => { state.bookNote = e.target.value; }, onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); confirmBooking(o, s); } } });
    return h('div', { class: 'confirm', role: 'group', 'aria-label': 'Confirm booking', style: { '--c': o.tech_color || '#2563eb' } },
      h('h4', {}, 'Confirm this booking?'),
      h('dl', {},
        row('Technician', o.tech_name), row('Day', fmtDay(o.date, { weekday: 'long', month: 'short', day: 'numeric' })),
        row('Customer is told', `between ${fmtMinutes(o.window_start_min)} and ${fmtMinutes(o.window_end_min)}`),
        row('Planned arrival', `about ${fmtMinutes(o.start_min)} · ${fmtDuration(s.duration_min)} on site`),
        row('On the route', `stop ${o.position} of ${o.stops_in_day + 1} · +${Math.round(o.added_drive_min)} min driving`)),
      o.stacked_with && o.stacked_with.length ? h('div', { class: 'slot-stack' }, icon('check', 12), ' ', stackText(o.stacked_with)) : null,
      o.misses_deadline ? h('div', { class: 'slot-warn' }, icon('alert', 12), ' Finishes after the deadline target') : null,
      note,
      h('div', { class: 'exc-actions' },
        h('button', { class: 'btn primary', type: 'button', disabled: state.booking, onclick: () => confirmBooking(o, s) }, icon('check', 14), state.booking ? ' Booking…' : ' Confirm booking'),
        h('button', { class: 'btn', type: 'button', disabled: state.booking, onclick: () => { state.pinnedOpt = null; renderQueue(); renderMap(false); } }, 'Cancel')),
      h('p', { class: 'dim hint' }, 'Saved in this app and added to this technician’s route here. Housecall Pro is not updated: schedule it there too so it reaches the technician.'));
  }
  // The server checks the slot again before booking, so a route that changed meanwhile is refused (409), never booked wrongly.
  async function confirmBooking(o, s) {
    const id = state.selectedId;
    if (!id || state.booking) return;
    state.booking = true; state.bookError = null; renderQueue();
    try {
      const r = await api.book(id, { tech_id: o.tech_id, date: o.date, window_start_min: o.window_start_min, window_end_min: o.window_end_min,
        window_minutes: s.window_minutes, after_stop_id: o.after_stop_id, before_stop_id: o.before_stop_id, note: state.bookNote });
      const b = r.booking;
      state.booking = false; state.bookNote = ''; state.slots = null; state.slotsFor = null; state.hoverOpt = state.pinnedOpt = null;
      toast(`Booked with ${b.tech_name}: ${fmtDay(b.date)}, ${fmtMinutes(b.window_start_min)} – ${fmtMinutes(b.window_end_min)}`, 'ok', 6000);
      state.date = b.date;
      if (state.selectedId === id) {
        await refreshJob(id);
        const at = state.data && state.data.bookings.find((x) => x.job_id === id);
        if (at && at.lat != null && state.selectedId === id) map.panTo(at.lat, at.lng);
      } else await load({ quiet: true, detail: true });
    } catch (e) {
      state.booking = false;
      if (state.selectedId !== id) return;
      if (e.status === 409) {
        toast(e.message, 'error', 7000);
        await refreshJob(id);                                                    // booked by someone else, or already in HCP: show the truth
        if (state.selectedId === id && state.detail && !state.detail.booking && state.detail.work_status === 'unscheduled') await findSlots();
        state.bookError = e.message;
      } else state.bookError = e.message;
      if (state.selectedId === id) renderQueue();
    }
  }

  // --------------------------------------------------------------------- map
  // Every unscheduled call is the same red "!". Colour only appears once a job is on a technician's route.
  function makePin(u) {
    const sel = u.id === state.selectedId;
    const el = h('div', { class: `pin${sel ? ' sel' : ''}`, role: 'button', tabindex: '0', 'aria-label': `Unscheduled: ${u.type_label} ${u.customer_name}, ${u.address}` });
    const svg = svgEl('svg', { viewBox: '0 0 44 44', width: 40, height: 40 });
    svg.append(svgEl('circle', { cx: 21, cy: 21, r: 16, class: 'shape' }), svgEl('text', { x: 21, y: 27, 'text-anchor': 'middle', class: 'pin-num', text: '!' }));
    el.append(svg);
    const open = () => select(u.id);
    el.addEventListener('click', open); el.addEventListener('keydown', onEnter(open));
    el.addEventListener('mouseenter', () => { const row = queue.querySelector(`.q-row[data-id="${CSS.escape(u.id)}"]`); if (row) { row.classList.add('hl'); row.scrollIntoView({ block: 'nearest' }); } });
    el.addEventListener('mouseleave', () => { const row = queue.querySelector(`.q-row[data-id="${CSS.escape(u.id)}"]`); if (row) row.classList.remove('hl'); });
    map.tip(el, [`${u.type_label} · ${u.customer_name || 'Unknown'}`, u.address].filter(Boolean));
    return el;
  }
  function makeStop(s, t) {
    const done = s.status === 'complete', prog = s.status === 'in_progress', booked = !!s.booked;
    const el = h('div', { class: `stop${done ? ' done' : ''}${prog ? ' prog' : ''}${booked ? ' booked' : ''}${booked && s.id === state.selectedId ? ' sel' : ''}`, style: { '--c': t ? t.color : '#64748b' } },
      done ? icon('check', 14) : s.seq ? String(s.seq) : '·');
    const head = done ? `Completed${s.completed_iso ? ' ' + fmtTime(s.completed_iso) : ''} · ${s.customer_name || 'Customer'}`
      : prog ? `In progress · ${s.customer_name || 'Customer'}` : booked ? `Booked here · ${s.customer_name || 'Customer'}` : `${s.customer_name || 'Customer'}`;
    const when = done ? '' : booked ? `Window ${fmtMinutes(s.window_start_min)}–${fmtMinutes(s.window_end_min)} · arrive about ${fmtTime(s.start_iso)}`
      : `Window ${fmtMinutes(s.window_start_min)}–${fmtMinutes(s.window_end_min)} · job ${fmtTime(s.start_iso)}–${fmtTime(s.end_iso)}`;
    map.tip(el, [head, when, s.address, s.summary, t ? `${t.name} · stop ${s.seq}` : 'No technician assigned',
      booked ? 'Not in Housecall Pro yet · click to open the booking' : null].filter(Boolean));
    if (booked) {
      el.setAttribute('role', 'button'); el.tabIndex = 0;
      const open = () => select(s.id);
      el.addEventListener('click', open); el.addEventListener('keydown', onEnter(open));
    }
    return el;
  }

  // ---------------------------------------------------------------- road routes
  // Lines start out straight and snap to the roads when /api/routes answers (the page never waits on the routing
  // service). Each leg is asked for once; the server caches roads for weeks and falls back to an estimate if the
  // routing service is off or down, in which case the leg is asked again after a minute.
  const roadsOn = !!(config.routing && config.routing.provider !== 'none');
  const routeCache = new Map(), routeAsked = new Set(), routeWanted = new Map();
  let routeTimer = null, destroyed = false;
  const rkey = (a, b) => `${a[0].toFixed(5)},${a[1].toFixed(5)}>${b[0].toFixed(5)},${b[1].toFixed(5)}`;
  function routeFor(a, b) {
    const k = rkey(a, b), r = routeCache.get(k);
    const stale = !r || (Date.now() - r.at > 60000 && (r.source === 'none' || (r.source === 'estimate' && roadsOn)));
    if (stale && !routeAsked.has(k)) routeWanted.set(k, [a, b]);
    return r || null;
  }
  function fetchRoutes() {
    const legs = [...routeWanted.values()]; routeWanted.clear();
    for (let i = 0; i < legs.length; i += 60) {
      const chunk = legs.slice(i, i + 60);
      chunk.forEach(([a, b]) => routeAsked.add(rkey(a, b)));
      api.routes(chunk.map(([a, b]) => ({ a, b })))
        .then((res) => chunk.forEach(([a, b], j) => routeCache.set(rkey(a, b), { ...res.routes[j], at: Date.now() })))
        .catch(() => chunk.forEach(([a, b]) => routeCache.set(rkey(a, b), { minutes: null, miles: null, path: null, source: 'none', at: Date.now() })))
        .finally(() => { chunk.forEach(([a, b]) => routeAsked.delete(rkey(a, b))); scheduleRouteRedraw(); });
    }
  }
  function scheduleRouteRedraw() {
    clearTimeout(routeTimer);
    routeTimer = setTimeout(() => { if (destroyed || !state.data) return; renderToolbar(); renderMap(false); }, 40);
  }
  // a technician's day as legs between consecutive located points: home -> stop 1 -> stop 2 ...
  function techLegs(t) {
    const nodes = (t.home ? [{ p: [t.home.lat, t.home.lng], label: 'Home' }] : [])
      .concat(t.stops.filter((s) => s.lat != null).map((s) => ({ p: [s.lat, s.lng], label: `Stop ${s.seq}` })));
    return nodes.slice(1).map((n, i) => ({ a: nodes[i].p, b: n.p, from: nodes[i].label, to: n.label }));
  }
  // total drive time by road, only when every leg of the day is known by road (never a mix of road and estimate)
  function driveTotal(t) {
    const legs = techLegs(t), rs = legs.map(({ a, b }) => routeCache.get(rkey(a, b)));
    return legs.length && rs.every((r) => r && r.source === 'road') ? rs.reduce((sum, r) => sum + r.minutes, 0) : null;
  }
  function legTip(r, who) {
    if (!r) return ['Finding the road route…', who];
    if (r.minutes == null) return ['Drive time unavailable', who];
    return [`${fmtDrive(r.minutes)} drive${r.miles ? ` · ${r.miles} mi` : ''}`, who,
      r.source === 'road' ? 'By road' : `Straight-line estimate (road routing is ${roadsOn ? 'unavailable right now' : 'off'})`];
  }

  function renderMap(fit) {
    const d = state.data; if (!d) return;
    map.clearMarkers();
    const lines = [], pts = [];
    for (const t of d.technicians) {
      if (state.hidden.has(t.id)) continue;
      if (t.home) { const hm = h('div', { class: 'home', style: { '--c': t.color }, title: `${t.name}: home base` }, icon('home', 14)); map.addMarker(`home:${t.id}`, t.home.lat, t.home.lng, hm, 2); pts.push([t.home.lat, t.home.lng]); }
      for (const s of t.stops) if (s.lat != null) { map.addMarker(`stop:${s.id}:${t.id}`, s.lat, s.lng, makeStop(s, t), 5); pts.push([s.lat, s.lng]); }
      techLegs(t).forEach(({ a, b, from, to }, i) => {
        const r = routeFor(a, b);
        lines.push({ id: `route:${t.id}:${i}`, points: r && r.path ? r.path : [a, b], color: t.color, width: 3, opacity: 0.75, tip: legTip(r, `${t.name}: ${from} → ${to}`) });
      });
    }
    for (const s of d.unassigned_scheduled) if (s.lat != null) { map.addMarker(`stop:${s.id}`, s.lat, s.lng, makeStop(s, null), 4); pts.push([s.lat, s.lng]); }

    const list = filtered();
    list.forEach((u) => { if (u.lat != null) { map.addMarker(u.id, u.lat, u.lng, makePin(u), u.id === state.selectedId ? 40 : 20); pts.push([u.lat, u.lng]); } });
    if (state.selectedId && !list.some((u) => u.id === state.selectedId)) {   // selected job hidden by filters: still show it
      const u = d.unscheduled.find((x) => x.id === state.selectedId);
      if (u && u.lat != null) map.addMarker(u.id, u.lat, u.lng, makePin(u), 40);
    }

    // slot preview (hover or pinned), drawn on top
    const opt = state.hoverOpt || state.pinnedOpt;
    if (opt && opt.date === d.date) {
      const from = opt.origin || opt.home, fromLabel = opt.origin && opt.origin.kind !== 'home' ? 'Last job' : 'Home';
      const nodes = (from ? [{ p: [from.lat, from.lng], label: fromLabel }] : [])
        .concat(opt.route_preview.filter((p) => p.lat != null).map((p, i) => ({ p: [p.lat, p.lng], label: p.is_new ? 'New job' : `Stop ${i + 1}` })));
      for (let i = 1; i < nodes.length; i++) {
        const a = nodes[i - 1].p, b = nodes[i].p, r = routeFor(a, b), points = r && r.path ? r.path : [a, b];
        lines.push({ id: `preview-halo:${i}`, points, color: '#ffffff', width: 8, opacity: 0.9 },
          { id: `preview:${i}`, points, color: opt.tech_color || '#111827', width: 4, dash: '8 7', opacity: 1,
            tip: legTip(r, `${opt.tech_name}: ${nodes[i - 1].label} → ${nodes[i].label}`) });
      }
      const np = opt.route_preview.find((p) => p.is_new);
      if (np && np.lat != null) {
        const nm = h('div', { class: 'newpin', style: { '--c': opt.tech_color || '#111827' } }, h('span', {}, 'NEW'));
        map.tip(nm, [`Window ${fmtMinutes(opt.window_start_min)} – ${fmtMinutes(opt.window_end_min)}`, `Arrive about ${fmtMinutes(opt.start_min)} · done ${fmtMinutes(opt.end_min)}`, `${opt.tech_name} · +${Math.round(opt.added_drive_min)} min driving`]);
        map.addMarker('preview-new', np.lat, np.lng, nm, 60);
      }
    }
    map.setLines(lines);
    fetchRoutes();
    const fitOpts = { padding: 60, maxZoom: 13, top: 120 };   // 120px: the toolbar + technician chips overlay
    if (fit) map.fit(pts, fitOpts);
    else map._lastFit = { points: pts, ...fitOpts };
  }

  // --------------------------------------------------------------- lifecycle
  timer = setInterval(() => { if (!document.hidden) load({ quiet: true }); }, 60000);
  load();
  return { destroy() { destroyed = true; clearInterval(timer); clearTimeout(routeTimer); map.destroy(); } };
}

function buildLegend() {
  const pin = h('span', { class: 'lg-bang', 'aria-hidden': 'true' }, '!');
  const dot = (cls, label) => h('span', { class: 'lg-item' }, h('span', { class: `lg-dot ${cls}` }), label);
  return h('details', { class: 'dp-legend' }, h('summary', {}, 'Legend'),
    h('div', { class: 'lg-row' }, h('span', { class: 'lg-item' }, pin, 'Unscheduled call (open it to see its type and details)')),
    h('div', { class: 'lg-row' }, h('span', { class: 'lg-item' }, h('span', { class: 'lg-dot tech' }), 'Scheduled stop, in its technician’s color (number = order in the route)'),
      h('span', { class: 'lg-item' }, h('span', { class: 'lg-dot tech done' }), 'finished'), h('span', { class: 'lg-item' }, h('span', { class: 'lg-dot tech booked' }), 'booked here, not in Housecall Pro yet')),
    h('div', { class: 'lg-row dim' }, 'Hover a route line for its drive time.'));
}
