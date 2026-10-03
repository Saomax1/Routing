// DOM + formatting helpers.
// SECURITY: all data from Housecall Pro is untrusted. We only ever build DOM with textContent / createTextNode
// (never innerHTML), and every href goes through safeUrl().

export function safeUrl(u) {
  try {
    const x = new URL(String(u), location.href);
    return ['http:', 'https:', 'tel:', 'mailto:'].includes(x.protocol) ? x.href : '#';
  } catch { return '#'; }
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'style' && typeof v === 'object') {
      for (const [sk, sv] of Object.entries(v)) {
        if (sk.startsWith('--')) el.style.setProperty(sk, sv); else el.style[sk] = sv;
      }
    }
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'href') el.setAttribute('href', safeUrl(v));
    else if (k === 'value') el.value = v;
    else if (k === 'checked') el.checked = !!v;
    else if (k === 'disabled') el.disabled = !!v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function render(el, ...children) { clear(el); append(el, children); return el; }

// ---- tiny inline SVG icon set (24x24, stroke based) ----
const SVG_NS = 'http://www.w3.org/2000/svg';
const ICONS = {
  droplet: ['M12 3s6 6.2 6 10.6a6 6 0 0 1-12 0C6 9.2 12 3 12 3z'],
  clock: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 7v5l3 2'],
  alert: ['M12 4 3 20h18L12 4z', 'M12 10v4.5', 'M12 17.5v.5'],
  mapoff: ['M12 21s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11z', 'M3 3l18 18'],
  phone: ['M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z'],
  link: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5'],
  refresh: ['M20 11a8 8 0 1 0-2.3 5.7', 'M20 4v7h-7'],
  left: ['M15 6l-6 6 6 6'], right: ['M9 6l6 6-6 6'],
  plus: ['M12 5v14', 'M5 12h14'], minus: ['M5 12h14'],
  fit: ['M4 9V4h5', 'M20 9V4h-5', 'M4 15v5h5', 'M20 15v5h-5'],
  x: ['M6 6l12 12', 'M18 6 6 18'], check: ['M5 12l5 5L20 7'],
  home: ['M3 11 12 3l9 8', 'M5 10v10h14V10'],
  ban: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M5.6 5.6l12.8 12.8'],
  route: ['M6 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z', 'M18 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4z', 'M8 17h6a3 3 0 0 0 0-6h-4a3 3 0 0 1 0-6h6'],
};
export function icon(name, size = 16, cls = '') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('width', size); svg.setAttribute('height', size);
  svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2'); svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  if (cls) svg.setAttribute('class', cls);
  for (const d of ICONS[name] || []) {
    const p = document.createElementNS(SVG_NS, 'path'); p.setAttribute('d', d); svg.append(p);
  }
  return svg;
}
export function svgEl(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'text') el.textContent = v; else el.setAttribute(k, v);
  }
  for (const k of kids) if (k) el.append(k);
  return el;
}

// ---- formatting (all times shown in the company timezone from /api/config) ----
let TZ = 'America/Phoenix';
export function setTimezone(tz) { TZ = tz || TZ; }
export const getTimezone = () => TZ;

export function fmtTime(iso) {
  if (!iso) return '';
  return new Intl.DateTimeFormat('en-US', { timeZone: TZ, hour: 'numeric', minute: '2-digit' }).format(new Date(iso));
}
export function fmtMinutes(min) {
  const h24 = Math.floor(min / 60) % 24, m = Math.round(min % 60);
  const ap = h24 >= 12 ? 'PM' : 'AM', h12 = h24 % 12 === 0 ? 12 : h24 % 12;
  return `${h12}:${String(m).padStart(2, '0')} ${ap}`;
}
export function fmtDay(dateStr, opts = { weekday: 'short', month: 'short', day: 'numeric' }) {
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', ...opts }).format(new Date(dateStr + 'T12:00:00Z'));
}
export function fmtDateTime(iso) {
  if (!iso) return '';
  return new Intl.DateTimeFormat('en-US', { timeZone: TZ, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(iso));
}
export function fmtDuration(min) {
  min = Math.round(min);
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60), m = min % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}
export function fmtDrive(min) {
  if (min == null) return '';
  return min < 1 ? '<1 min' : fmtDuration(min);
}
export function fmtAge(hours) {
  if (hours == null) return '';
  return hours < 1 ? `${Math.max(1, Math.round(hours * 60))} min` : hours < 48 ? `${Math.round(hours)} h` : `${(hours / 24).toFixed(1)} d`;
}
export function fmtLeft(hours) {
  if (hours == null) return '';
  const a = Math.abs(hours), t = a < 1 ? `${Math.round(a * 60)} min` : a < 48 ? `${a.toFixed(a < 10 ? 1 : 0)} h` : `${(a / 24).toFixed(1)} d`;
  return hours < 0 ? `${t} overdue` : `${t} left`;
}
export const money = (n) => (n == null ? '' : `$${Number(n).toLocaleString('en-US', { maximumFractionDigits: 2 })}`);
export function fmtPhone(p) {
  const d = String(p || '').replace(/\D/g, '').slice(-10);
  return d.length === 10 ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : String(p || '');
}
export function timeAgo(iso) {
  if (!iso) return 'never';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
export const toast = (() => {
  let box;
  return (msg, kind = 'info', ms = 4000) => {
    if (!box) { box = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' }); document.body.append(box); }
    const t = h('div', { class: `toast ${kind}` }, msg);
    box.append(t);
    setTimeout(() => t.remove(), ms);
  };
})();
