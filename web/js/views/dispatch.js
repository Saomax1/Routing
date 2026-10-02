// Dispatch screen: map (routes + unscheduled pins), prioritized queue, job card, "find best slot".
// Read-only against Housecall Pro in this phase: slot suggestions are shown, dispatchers schedule in HCP.

import { h, render, icon, svgEl, fmtTime, fmtMinutes, fmtDay, fmtDateTime, fmtDuration, fmtAge, fmtLeft, fmtPhone,
  money, timeAgo, SOURCE_LABEL, toast } from '../dom.js';
import { api } from '../api.js';
import { SlippyMap } from '../map.js';

const PRIO_CLASS = (p) => String(p || 'normal').toLowerCase();
const onEnter = (fn) => (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(e); } };

export function mountDispatch(root, ctx) {
  const { config } = ctx;
  const state = {
    date: config.today, data: null, filters: { q: '', trade: '', source: '', priority: '' },
    hidden: new Set(), selectedId: null, detail: null, detailLoading: false,
    slots: null, slotsFor: null, slotsLoading: false, slotDays: 3, hoverOpt: null, pinnedOpt: null,
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
    renderToolbar();
    if (!state.selectedId || detail) renderQueue();   // never re-render an open job card on background refresh
    renderMap(state.firstFit);
    state.firstFit = false;
    if (!quiet && state.error) toast(state.error, 'error');
  }

  function filtered() {
    if (!state.data) return [];
    const f = state.filters, q = f.q.trim().toLowerCase();
    return state.data.unscheduled.filter((u) => {
      if (f.trade && u.trade_code !== f.trade) return false;
      if (f.source && u.source_category !== f.source) return false;
      if (f.priority && u.priority_label !== f.priority) return false;
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
      h('span', { class: 'stat' }, h('b', {}, d.stats.scheduled_today), ' scheduled this day'));
    const chips = h('div', { class: 'dp-techs', role: 'group', 'aria-label': 'Technicians' },
      d.technicians.map((t) => h('button', {
        class: `tech-chip${state.hidden.has(t.id) ? ' off' : ''}${t.active ? '' : ' inactive'}`, type: 'button', 'aria-pressed': String(!state.hidden.has(t.id)),
        style: { '--c': t.color },
        title: t.needs_setup ? 'Needs trade skills and a home base: Admin > Technicians' : `${t.name}: click to show/hide`,
        onclick: () => { state.hidden.has(t.id) ? state.hidden.delete(t.id) : state.hidden.add(t.id); renderToolbar(); renderMap(false); },
      }, h('span', { class: 'dot' }), h('span', { class: 'tc-name' }, t.name),
      h('span', { class: 'tc-meta' }, `${t.job_count} job${t.job_count === 1 ? '' : 's'}${t.drive_min ? ` · ${t.drive_min} min drive` : ''}`),
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
    const list = filtered();
    const trades = [...new Set(state.data.unscheduled.map((u) => u.trade_code).filter(Boolean))].sort();
    const sel = (key, label, opts) => h('select', { 'aria-label': label, onchange: (e) => { state.filters[key] = e.target.value; renderQueue(); renderMap(false); } },
      h('option', { value: '' }, label), opts.map(([v, t]) => h('option', { value: v, selected: state.filters[key] === v }, t)));
    const search = h('input', { type: 'search', placeholder: 'Search name, address, problem, zip…', value: state.filters.q, 'aria-label': 'Search unscheduled jobs',
      oninput: (e) => { state.filters.q = e.target.value; clearTimeout(search._t); search._t = setTimeout(() => { renderQueue(); renderMap(false); search2Focus(); }, 180); } });
    const search2Focus = () => { const s = queue.querySelector('input[type=search]'); if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); } };
    const head = h('header', { class: 'q-head' },
      h('div', { class: 'q-title' }, h('h2', {}, 'Unscheduled'), h('span', { class: 'count' }, list.length === state.data.unscheduled.length ? `${list.length}` : `${list.length} of ${state.data.unscheduled.length}`)),
      search,
      h('div', { class: 'q-filters' },
        sel('trade', 'All trades', trades.map((t) => [t, t])),
        sel('source', 'All sources', Object.entries(SOURCE_LABEL)),
        sel('priority', 'All priorities', ['Emergency', 'Expedited', 'Normal', 'Direct'].map((p) => [p, p]))));
    const body = h('div', { class: 'q-list', role: 'list' },
      list.length ? list.map((u, i) => queueRow(u, i + 1)) : h('div', { class: 'empty' }, state.data.unscheduled.length ? 'No jobs match these filters.' : 'No unscheduled jobs. Nice work.'));
    render(queue, head, body);
    body.scrollTop = scroll;
  }

  function badge(text, cls = '', title = '') { return h('span', { class: `badge ${cls}`, title }, text); }
  function chips(u) {
    const out = [badge(SOURCE_LABEL[u.source_category] || u.source_category, `src src-${u.source_category}`)];
    if (u.trade_code) out.push(badge(u.trade_code, 'trade'));
    if (u.deadline_status !== 'none') out.push(h('span', { class: `chip dl-${u.deadline_status}`, title: 'Time left until the contact/schedule deadline (Admin > Settings)' }, icon('clock', 12), ' ', fmtLeft(u.deadline_hours_left)));
    if (u.age_hours != null) out.push(h('span', { class: 'chip dim', title: 'Time since the job arrived in Housecall Pro' }, `waiting ${fmtAge(u.age_hours)}`));
    if (u.urgency_flags.length) out.push(h('span', { class: 'chip urgent', title: 'Urgency keywords found in the problem text' }, icon('droplet', 12), ' ', u.urgency_flags.slice(0, 2).join(', ')));
    if (u.warranty && u.warranty.do_not_collect_service_fee) out.push(h('span', { class: 'chip fee', title: 'Warranty notice: do not collect the trade service fee' }, 'no fee'));
    if (u.lat == null) out.push(h('span', { class: 'chip warn', title: 'No map location yet' }, icon('mapoff', 12), ' no location'));
    if (u.warranty && u.warranty.has_warnings) out.push(h('span', { class: 'chip warn', title: 'Some warranty fields could not be read; open the job to check' }, icon('alert', 12), ' check'));
    return out;
  }
  function queueRow(u, rank) {
    const open = () => select(u.id);
    return h('div', {
      class: `q-row prio-${PRIO_CLASS(u.priority_label)}${u.id === state.selectedId ? ' sel' : ''}`, role: 'listitem', tabindex: '0', 'data-id': u.id,
      onclick: open, onkeydown: onEnter(open),
      onmouseenter: () => { map.highlight(u.id, true); }, onmouseleave: () => map.highlight(u.id, false),
      onfocus: () => map.highlight(u.id, true), onblur: () => map.highlight(u.id, false),
    },
    h('div', { class: 'q-rank', title: 'Rank by priority score (matches the number on the map pin)' }, rank),
    h('div', { class: 'q-main' },
      h('div', { class: 'q-top' }, badge(u.priority_label, `prio prio-${PRIO_CLASS(u.priority_label)}`), h('span', { class: 'q-name' }, u.customer_name || 'Unknown customer'),
        h('span', { class: 'q-score', title: 'Priority score' }, Math.round(u.score))),
      h('div', { class: 'q-addr' }, u.address || 'No address on this job'),
      u.summary ? h('div', { class: 'q-sum' }, u.summary) : null,
      h('div', { class: 'q-chips' }, chips(u))));
  }

  // ------------------------------------------------------------------- detail
  async function select(id) {
    state.selectedId = id; state.detail = null; state.detailLoading = true;
    state.slots = null; state.slotsFor = null; state.hoverOpt = state.pinnedOpt = null;
    renderQueue(); renderMap(false);
    const u = state.data && state.data.unscheduled.find((x) => x.id === id);
    if (u && u.lat != null) map.panTo(u.lat, u.lng);
    try { state.detail = await api.job(id); } catch (e) { toast(e.message, 'error'); state.selectedId = null; }
    state.detailLoading = false;
    renderQueue(); renderMap(false);
  }
  function back() {
    state.selectedId = null; state.detail = null; state.slots = null; state.slotsFor = null; state.hoverOpt = state.pinnedOpt = null;
    renderQueue(); renderMap(false);
  }

  const kv = (k, v) => (v == null || v === '' ? null : h('div', { class: 'kv' }, h('dt', {}, k), h('dd', {}, v)));
  const section = (title, ...kids) => h('section', { class: 'd-sec' }, h('h3', {}, title), ...kids);

  function renderDetail() {
    const u = state.data.unscheduled.find((x) => x.id === state.selectedId);
    const d = state.detail;
    const backBtn = h('button', { class: 'btn ghost', type: 'button', onclick: back }, icon('left', 14), ' Unscheduled');
    if (!d) { render(queue, h('header', { class: 'q-head' }, backBtn), h('div', { class: 'd-body' }, h('div', { class: 'empty' }, state.detailLoading ? 'Loading…' : 'Job not found.'))); return; }
    const w = d.warranty, sc = d.score;
    const hdr = h('header', { class: 'q-head d-head' },
      h('div', { class: 'd-toprow' }, backBtn, d.hcp_url ? h('a', { class: 'btn ghost', href: d.hcp_url, target: '_blank', rel: 'noopener noreferrer' }, icon('link', 14), ' Open in Housecall Pro') : null),
      h('h2', { class: 'd-name' }, d.customer_name || 'Unknown customer'),
      h('div', { class: 'd-badges' }, badge(sc.priority_label, `prio prio-${PRIO_CLASS(sc.priority_label)}`), badge(SOURCE_LABEL[d.source_category], `src src-${d.source_category}`),
        d.trade_code ? badge(d.trade_code, 'trade') : null, d.work_status !== 'unscheduled' ? badge(d.work_status.replace('_', ' '), 'dim') : null));

    const alerts = [];
    if (w && w.do_not_collect_service_fee) alerts.push(h('div', { class: 'alert fee' }, h('b', {}, 'Do not collect the trade service fee.'), w.payment_type ? ` Payment type: ${w.payment_type}.` : ''));
    if (w && w.authorization_required) alerts.push(h('div', { class: 'alert warn' }, icon('alert', 14), ' Authorization is required before work starts.'));
    if (d.lat == null) alerts.push(h('div', { class: 'alert warn' }, icon('mapoff', 14), ` No map location (${d.geocode_status}). Check the address in Housecall Pro; slots cannot be computed without one.`));

    const dl = sc.deadline_status !== 'none'
      ? h('div', { class: `d-deadline dl-${sc.deadline_status}` }, icon('clock', 14), ` Deadline ${fmtDateTime(sc.deadline_at)} · `, h('b', {}, fmtLeft(sc.deadline_hours_left)))
      : h('div', { class: 'd-deadline dim' }, 'No deadline rule for this job type (Admin > Settings).');
    const scoreCard = h('div', { class: 'd-score' }, h('div', { class: 'big' }, Math.round(sc.total), h('small', {}, 'priority score')), dl);

    // --- slot finder
    const daysSel = h('select', { 'aria-label': 'Days to search', onchange: (e) => { state.slotDays = Number(e.target.value); if (state.slots) findSlots(); } },
      [1, 2, 3, 5, 7, 14].map((n) => h('option', { value: n, selected: n === state.slotDays }, `${n} day${n === 1 ? '' : 's'}`)));
    const slotPanel = h('section', { class: 'd-sec slots' },
      h('div', { class: 'slots-head' }, h('h3', {}, 'Best slots'),
        h('div', { class: 'slots-ctl' }, h('label', { class: 'dim' }, 'Search ', daysSel),
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

    const raw = h('details', { class: 'd-sec raw' }, h('summary', {}, 'Original description from Housecall Pro'), h('pre', {}, d.description_raw || '(empty)'));

    render(queue, hdr, h('div', { class: 'd-body' }, alerts, scoreCard, slotPanel, section('Problem', items), contact, warranty, breakdown, warnings, raw));
  }

  // ----------------------------------------------------------------- slots UI
  async function findSlots() {
    const id = state.selectedId; state.slotsLoading = true; state.hoverOpt = state.pinnedOpt = null;
    renderQueue();
    try { state.slots = await api.slots(id, state.slotDays); state.slotsFor = id; }
    catch (e) { toast(e.message, 'error'); state.slots = null; }
    state.slotsLoading = false;
    if (state.selectedId === id) { renderQueue(); renderMap(false); }
  }
  function renderSlots(d) {
    const s = state.slots;
    if (!s || state.slotsFor !== d.id) return h('p', { class: 'dim hint' }, 'Finds the cheapest place in each technician’s day (least added driving), respecting skills, shifts, existing appointment times and priority. Suggestions only: nothing is written to Housecall Pro.');
    const out = [h('p', { class: 'dim hint' }, `${fmtDuration(s.duration_min)} job · searched ${s.search_days} day${s.search_days === 1 ? '' : 's'} · drive times are straight-line estimates`)];
    if (!s.options.length) out.push(h('div', { class: 'alert warn' }, icon('alert', 14), ' ', s.notes[0] || 'No feasible slot found.'));
    s.options.forEach((o, i) => out.push(slotCard(o, i)));
    if (s.notes.length && s.options.length) s.notes.forEach((n) => out.push(h('p', { class: 'dim hint' }, n)));
    if (s.ineligible.length) out.push(h('details', { class: 'inel' }, h('summary', {}, `${s.ineligible.length} technician${s.ineligible.length === 1 ? '' : 's'} not suggested`),
      h('ul', {}, s.ineligible.map((x) => h('li', {}, h('b', {}, x.name), ` – ${x.reason}`)))));
    return h('div', { class: 'slot-list' }, out);
  }
  function slotCard(o, i) {
    const pinned = state.pinnedOpt && state.pinnedOpt.tech_id === o.tech_id && state.pinnedOpt.date === o.date;
    const pick = () => { state.pinnedOpt = pinned ? null : o; if (!pinned && o.date !== state.date) { state.date = o.date; load({ detail: true }); } else { renderQueue(); renderMap(false); } };
    return h('div', {
      class: `slot${pinned ? ' pinned' : ''}${o.misses_deadline ? ' miss' : ''}`, role: 'button', tabindex: '0', style: { '--c': o.tech_color || '#2563eb' },
      onclick: pick, onkeydown: onEnter(pick),
      onmouseenter: () => { state.hoverOpt = o; renderMap(false); }, onmouseleave: () => { state.hoverOpt = null; renderMap(false); },
    },
    h('div', { class: 'slot-top' }, h('span', { class: 'dot' }), h('b', {}, o.tech_name), h('span', { class: 'slot-when' }, `${fmtDay(o.date)} · ${fmtMinutes(o.start_min)} – ${fmtMinutes(o.end_min)}`), i === 0 ? badge('best', 'best') : null),
    h('div', { class: 'slot-meta' }, `Stop ${o.position} of ${o.stops_in_day + 1} · `, h('b', {}, `+${Math.round(o.added_drive_min)} min driving`),
      ` · ${Math.round(o.drive_in_min)} min to get there${o.before_stop_id ? `, ${Math.round(o.drive_out_min)} min to next stop` : ''}`),
    o.misses_deadline ? h('div', { class: 'slot-warn' }, icon('alert', 12), ' Finishes after the deadline') : null,
    o.date !== state.date ? h('div', { class: 'slot-hint dim' }, 'Click to view this day on the map') : null);
  }

  // --------------------------------------------------------------------- map
  function makePin(u, rank) {
    const sel = u.id === state.selectedId;
    const el = h('div', { class: `pin src-${u.source_category} prio-${PRIO_CLASS(u.priority_label)} dl-${u.deadline_status}${sel ? ' sel' : ''}`, role: 'button', tabindex: '0',
      'aria-label': `Rank ${rank}: ${u.priority_label} ${u.customer_name}, ${u.address}` });
    const svg = svgEl('svg', { viewBox: '0 0 44 44', width: 40, height: 40 });
    const shape = u.source_category === 'ahs' ? svgEl('rect', { x: 6, y: 6, width: 30, height: 30, rx: 7, class: 'shape' })
      : u.source_category === 'other_warranty' ? svgEl('polygon', { points: '21,2 40,21 21,40 2,21', class: 'shape' })
        : svgEl('circle', { cx: 21, cy: 21, r: 16, class: 'shape' });
    svg.append(shape, svgEl('text', { x: 21, y: 26, 'text-anchor': 'middle', class: 'pin-num', text: String(rank) }));
    if (u.urgency_flags.length) {
      svg.append(svgEl('circle', { cx: 35, cy: 9, r: 8, class: 'badge-bg' }),
        svgEl('path', { d: 'M35 4.2s3.6 3.8 3.6 6.4a3.6 3.6 0 0 1-7.2 0c0-2.6 3.6-6.4 3.6-6.4z', class: 'badge-drop' }));
    }
    el.append(svg);
    const open = () => select(u.id);
    el.addEventListener('click', open); el.addEventListener('keydown', onEnter(open));
    el.addEventListener('mouseenter', () => { const row = queue.querySelector(`.q-row[data-id="${CSS.escape(u.id)}"]`); if (row) { row.classList.add('hl'); row.scrollIntoView({ block: 'nearest' }); } });
    el.addEventListener('mouseleave', () => { const row = queue.querySelector(`.q-row[data-id="${CSS.escape(u.id)}"]`); if (row) row.classList.remove('hl'); });
    map.tip(el, [`${rank}. ${u.priority_label} · ${u.customer_name || 'Unknown'}`, u.address, u.summary, `${SOURCE_LABEL[u.source_category]}${u.trade_code ? ' · ' + u.trade_code : ''} · score ${Math.round(u.score)}${u.deadline_status !== 'none' ? ' · ' + fmtLeft(u.deadline_hours_left) : ''}`].filter(Boolean));
    return el;
  }
  function makeStop(s, t) {
    const el = h('div', { class: 'stop', style: { '--c': t ? t.color : '#64748b' } }, s.seq ? String(s.seq) : '·');
    map.tip(el, [`${fmtTime(s.start_iso)}–${fmtTime(s.end_iso)} · ${s.customer_name || 'Customer'}`, s.address, s.summary, t ? `${t.name} · stop ${s.seq}` : 'No technician assigned'].filter(Boolean));
    return el;
  }

  function renderMap(fit) {
    const d = state.data; if (!d) return;
    map.clearMarkers();
    const lines = [], pts = [];
    for (const t of d.technicians) {
      if (state.hidden.has(t.id)) continue;
      const path = [];
      if (t.home) { path.push([t.home.lat, t.home.lng]); const hm = h('div', { class: 'home', style: { '--c': t.color }, title: `${t.name}: home base` }, icon('home', 14)); map.addMarker(`home:${t.id}`, t.home.lat, t.home.lng, hm, 2); pts.push([t.home.lat, t.home.lng]); }
      for (const s of t.stops) if (s.lat != null) { path.push([s.lat, s.lng]); map.addMarker(`stop:${s.id}:${t.id}`, s.lat, s.lng, makeStop(s, t), 5); pts.push([s.lat, s.lng]); }
      if (path.length > 1) lines.push({ id: `route:${t.id}`, points: path, color: t.color, width: 3, opacity: 0.75 });
    }
    for (const s of d.unassigned_scheduled) if (s.lat != null) { map.addMarker(`stop:${s.id}`, s.lat, s.lng, makeStop(s, null), 4); pts.push([s.lat, s.lng]); }

    const list = filtered();
    list.forEach((u, i) => { if (u.lat != null) { map.addMarker(u.id, u.lat, u.lng, makePin(u, i + 1), u.id === state.selectedId ? 40 : 20); pts.push([u.lat, u.lng]); } });
    if (state.selectedId && !list.some((u) => u.id === state.selectedId)) {   // selected job hidden by filters: still show it
      const u = d.unscheduled.find((x) => x.id === state.selectedId);
      if (u && u.lat != null) map.addMarker(u.id, u.lat, u.lng, makePin(u, d.unscheduled.indexOf(u) + 1), 40);
    }

    // slot preview (hover or pinned), drawn on top
    const opt = state.hoverOpt || state.pinnedOpt;
    if (opt && opt.date === d.date) {
      const path = (opt.home ? [[opt.home.lat, opt.home.lng]] : []).concat(opt.route_preview.filter((p) => p.lat != null).map((p) => [p.lat, p.lng]));
      lines.push({ id: 'preview-halo', points: path, color: '#ffffff', width: 8, opacity: 0.9 },
        { id: 'preview', points: path, color: opt.tech_color || '#111827', width: 4, dash: '8 7', opacity: 1 });
      const np = opt.route_preview.find((p) => p.is_new);
      if (np && np.lat != null) {
        const nm = h('div', { class: 'newpin', style: { '--c': opt.tech_color || '#111827' } }, h('span', {}, 'NEW'));
        map.tip(nm, [`Proposed: ${fmtMinutes(opt.start_min)} – ${fmtMinutes(opt.end_min)}`, `${opt.tech_name} · +${Math.round(opt.added_drive_min)} min driving`]);
        map.addMarker('preview-new', np.lat, np.lng, nm, 60);
      }
    }
    map.setLines(lines);
    const fitOpts = { padding: 60, maxZoom: 13, top: 120 };   // 120px: the toolbar + technician chips overlay
    if (fit) map.fit(pts, fitOpts);
    else map._lastFit = { points: pts, ...fitOpts };
  }

  // --------------------------------------------------------------- lifecycle
  timer = setInterval(() => { if (!document.hidden) load({ quiet: true }); }, 60000);
  load();
  return { destroy() { clearInterval(timer); map.destroy(); } };
}

function buildLegend() {
  const shape = (kind, label) => {
    const svg = svgEl('svg', { viewBox: '0 0 20 20', width: 16, height: 16 });
    svg.append(kind === 'ahs' ? svgEl('rect', { x: 3, y: 3, width: 14, height: 14, rx: 3, class: 'lg-shape' })
      : kind === 'other' ? svgEl('polygon', { points: '10,1 19,10 10,19 1,10', class: 'lg-shape' }) : svgEl('circle', { cx: 10, cy: 10, r: 7.5, class: 'lg-shape' }));
    return h('span', { class: 'lg-item' }, svg, label);
  };
  const dot = (cls, label) => h('span', { class: 'lg-item' }, h('span', { class: `lg-dot ${cls}` }), label);
  const el = h('details', { class: 'dp-legend' }, h('summary', {}, 'Legend'),
    h('div', { class: 'lg-row' }, h('b', {}, 'Shape'), shape('ahs', 'AHS'), shape('other', 'Other warranty'), shape('direct', 'Direct lead')),
    h('div', { class: 'lg-row' }, h('b', {}, 'Color'), dot('prio-emergency', 'Emergency'), dot('prio-expedited', 'Expedited'), dot('prio-normal', 'Normal'), dot('prio-direct', 'Direct')),
    h('div', { class: 'lg-row' }, h('b', {}, 'Ring'), dot('ring-warning', 'deadline closing'), dot('ring-critical', 'critical'), dot('ring-overdue', 'overdue')),
    h('div', { class: 'lg-row dim' }, 'Numbers = queue rank · water drop = urgency keyword · colored circles = scheduled stops by technician'));
  return el;
}
