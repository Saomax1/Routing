// A small, dependency-free "slippy map": Web Mercator raster tiles + DOM markers + SVG route lines.
//
// Why not Leaflet/Mapbox/Google? This build has no npm step and no third-party scripts (strict CSP). The tile source is
// just a URL template in Admin > Settings, so you can point it at Mapbox/Google/Stadia/your own tile server.
// If tiles cannot load (offline, blocked, bad key) pins and routes still work and a notice is shown.
//
// Public API:
//   const map = new SlippyMap(el, { tileUrl, attribution, center: [lat, lng], zoom })
//   map.clearMarkers(); map.addMarker(id, lat, lng, element, z)
//   map.setLines([{ id, points: [[lat,lng],...], color, width, dash, opacity }])
//   map.fit(points, { padding, maxZoom }); map.panTo(lat, lng); map.highlight(id, on)
//   map.tip(el, lines)  // hover tooltip for a marker element
//   map.onBackgroundClick = () => {}

import { svgEl, icon, h } from './dom.js';

const TILE = 256;
const MIN_Z = 3, MAX_Z = 18;
const clampLat = (lat) => Math.max(-85.0511, Math.min(85.0511, lat));

export function project(lat, lng, z) {
  const s = TILE * 2 ** z;
  const sin = Math.sin(clampLat(lat) * Math.PI / 180);
  return { x: (lng + 180) / 360 * s, y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * s };
}
export function unproject(x, y, z) {
  const s = TILE * 2 ** z, n = Math.PI - 2 * Math.PI * y / s;
  return { lat: 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n))), lng: x / s * 360 - 180 };
}

export class SlippyMap {
  constructor(container, opts = {}) {
    this.el = container;
    this.tileUrl = opts.tileUrl || '';
    this.zoom = opts.zoom || 10;
    this.center = { lat: (opts.center || [33.3, -111.8])[0], lng: (opts.center || [33.3, -111.8])[1] };
    this.markers = new Map(); this.lines = new Map(); this.tiles = new Map();
    this.onBackgroundClick = null;
    this._tilesLoaded = 0; this._tilesFailed = 0; this._raf = 0; this._lastWheel = 0;

    this.el.classList.add('sm');
    this.tileLayer = h('div', { class: 'sm-tiles' });
    this.svg = svgEl('svg', { class: 'sm-lines' });
    this.markerLayer = h('div', { class: 'sm-markers' });
    this.tipEl = h('div', { class: 'sm-tip', role: 'tooltip' });
    this.notice = h('div', { class: 'sm-notice', hidden: true },
      'Map tiles are not loading (offline, blocked, or a bad tile URL). Pins and routes still work.');
    const zin = h('button', { class: 'sm-btn', type: 'button', 'aria-label': 'Zoom in', title: 'Zoom in', onclick: () => this.setZoom(this.zoom + 1) }, icon('plus'));
    const zout = h('button', { class: 'sm-btn', type: 'button', 'aria-label': 'Zoom out', title: 'Zoom out', onclick: () => this.setZoom(this.zoom - 1) }, icon('minus'));
    const fit = h('button', { class: 'sm-btn', type: 'button', 'aria-label': 'Fit all pins', title: 'Fit all pins', onclick: () => this.refit() }, icon('fit'));
    this.controls = h('div', { class: 'sm-controls' }, zin, zout, fit);
    this.attrib = h('div', { class: 'sm-attrib' }, opts.attribution || '');
    this.el.append(this.tileLayer, this.svg, this.markerLayer, this.controls, this.attrib, this.notice, this.tipEl);

    this._bind();
    this._ro = new ResizeObserver(() => this.render());
    this._ro.observe(this.el);
    setTimeout(() => this._checkTiles(), 5000);
    this.render();
  }

  destroy() { this._ro.disconnect(); cancelAnimationFrame(this._raf); }

  // ---- geometry
  size() { return { w: this.el.clientWidth || 800, h: this.el.clientHeight || 600 }; }
  topLeft() {
    const c = project(this.center.lat, this.center.lng, this.zoom), s = this.size();
    return { x: c.x - s.w / 2, y: c.y - s.h / 2 };
  }
  toPx(lat, lng, tl = this.topLeft()) { const p = project(lat, lng, this.zoom); return { x: p.x - tl.x, y: p.y - tl.y }; }

  // ---- view control
  setView(lat, lng, zoom) {
    this.center = { lat: clampLat(lat), lng };
    if (zoom != null) this.zoom = Math.max(MIN_Z, Math.min(MAX_Z, Math.round(zoom)));
    this.render();
  }
  setZoom(z, anchor) {
    z = Math.max(MIN_Z, Math.min(MAX_Z, z));
    if (z === this.zoom) return;
    if (anchor) { // keep the point under the cursor fixed
      const s = this.size(), tl = this.topLeft();
      const before = unproject(tl.x + anchor.x, tl.y + anchor.y, this.zoom);
      this.zoom = z;
      const p = project(before.lat, before.lng, z);
      const c = unproject(p.x - anchor.x + s.w / 2, p.y - anchor.y + s.h / 2, z);
      this.center = c;
    } else this.zoom = z;
    this.render();
  }
  panTo(lat, lng) {
    // move only if the point is outside the comfortable middle of the view
    const s = this.size(), p = this.toPx(lat, lng);
    if (p.x < s.w * 0.15 || p.x > s.w * 0.85 || p.y < s.h * 0.15 || p.y > s.h * 0.85) this.setView(lat, lng);
  }
  fit(points, { padding = 60, maxZoom = 15, top = 0 } = {}) {
    // `top` reserves space at the top of the map (e.g. for an overlay toolbar) so pins do not start underneath it
    this._lastFit = { points, padding, maxZoom, top };
    const pts = points.filter((p) => p && p[0] != null && p[1] != null);
    if (!pts.length) return;
    const s = { w: this.size().w, h: this.size().h - top };
    const lats = pts.map((p) => p[0]), lngs = pts.map((p) => p[1]);
    const minLat = Math.min(...lats), maxLat = Math.max(...lats), minLng = Math.min(...lngs), maxLng = Math.max(...lngs);
    let z = maxZoom;
    for (; z > MIN_Z; z--) {
      const a = project(maxLat, minLng, z), b = project(minLat, maxLng, z);
      if (Math.abs(b.x - a.x) <= s.w - padding * 2 && Math.abs(b.y - a.y) <= s.h - padding * 2) break;
    }
    const c = project((minLat + maxLat) / 2, (minLng + maxLng) / 2, z);
    const ctr = unproject(c.x, c.y - top / 2, z);   // shift the view up so the content sits in the area below the overlay
    this.setView(ctr.lat, ctr.lng, z);
  }
  refit() { if (this._lastFit) this.fit(this._lastFit.points, this._lastFit); }

  // ---- markers / lines / tooltips
  clearMarkers() { this.markerLayer.replaceChildren(); this.markers.clear(); this.hideTip(); }
  addMarker(id, lat, lng, el, z = 0) {
    if (lat == null || lng == null) return;
    el.classList.add('sm-marker');
    el.style.zIndex = String(10 + z);
    el.dataset.z = String(z);
    this.markerLayer.append(el);
    this.markers.set(id, { lat, lng, el });
    this._placeMarker(this.markers.get(id), this.topLeft());
  }
  _placeMarker(m, tl) {
    const p = this.toPx(m.lat, m.lng, tl);
    m.el.style.transform = `translate(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px)`;
  }
  highlight(id, on) {
    const m = this.markers.get(id);
    if (!m) return;
    m.el.classList.toggle('hl', !!on);
    m.el.style.zIndex = on ? '999' : String(10 + Number(m.el.dataset.z || 0));
  }
  setLines(lines) {
    this.lines = new Map(lines.map((l) => [l.id, l]));
    this.svg.replaceChildren();
    for (const l of lines) {
      const pl = svgEl('polyline', {
        fill: 'none', stroke: l.color || '#2563eb', 'stroke-width': l.width || 3, 'stroke-linejoin': 'round',
        'stroke-linecap': 'round', 'stroke-opacity': l.opacity ?? 0.85, ...(l.dash ? { 'stroke-dasharray': l.dash } : {}),
      });
      l._el = pl; this.svg.append(pl);
    }
    // Lines with a `tip` get a wide invisible twin to hover (a 3px line is hard to hit). The twins go last so they sit
    // above every visible line; hovering one thickens its line and shows the tooltip.
    for (const l of lines) {
      if (!l.tip) continue;
      const hit = svgEl('polyline', { class: 'sm-hit', fill: 'none', stroke: 'transparent', 'stroke-width': 16, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
      const width = l.width || 3;
      hit.addEventListener('mouseenter', (e) => { l._el.setAttribute('stroke-width', width + 2); l._el.setAttribute('stroke-opacity', 1); this.showTip(l.tip, e); });
      hit.addEventListener('mousemove', (e) => this.showTip(l.tip, e));
      hit.addEventListener('mouseleave', () => { l._el.setAttribute('stroke-width', width); l._el.setAttribute('stroke-opacity', l.opacity ?? 0.85); this.hideTip(); });
      l._hit = hit; this.svg.append(hit);
    }
    this._placeLines(this.topLeft());
  }
  _placeLines(tl) {
    for (const l of this.lines.values()) {
      if (!l._el) continue;
      const pts = l.points.filter((p) => p[0] != null && p[1] != null)
        .map(([la, ln]) => { const p = this.toPx(la, ln, tl); return `${p.x.toFixed(1)},${p.y.toFixed(1)}`; }).join(' ');
      l._el.setAttribute('points', pts);
      if (l._hit) l._hit.setAttribute('points', pts);
    }
  }
  tip(el, lines) {
    el.addEventListener('mouseenter', (e) => this.showTip(lines, e));
    el.addEventListener('mousemove', (e) => this.showTip(lines, e));
    el.addEventListener('mouseleave', () => this.hideTip());
  }
  showTip(lines, e) {
    const r = this.el.getBoundingClientRect();
    this.tipEl.replaceChildren(...lines.map((t, i) => h('div', { class: i === 0 ? 'tip-title' : 'tip-line' }, t)));
    this.tipEl.classList.add('show');
    const x = Math.min(e.clientX - r.left + 14, r.width - 250), y = Math.min(e.clientY - r.top + 14, r.height - 90);
    this.tipEl.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
  }
  hideTip() { this.tipEl.classList.remove('show'); }

  // ---- rendering
  render() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this._draw(); });
  }
  _draw() {
    const tl = this.topLeft(), s = this.size();
    // tiles
    const z = this.zoom, n = 2 ** z;
    const x0 = Math.floor(tl.x / TILE), x1 = Math.floor((tl.x + s.w) / TILE);
    const y0 = Math.max(0, Math.floor(tl.y / TILE)), y1 = Math.min(n - 1, Math.floor((tl.y + s.h) / TILE));
    const wanted = new Set();
    if (this.tileUrl) {
      for (let ty = y0; ty <= y1; ty++) for (let tx = x0; tx <= x1; tx++) {
        const key = `${z}/${tx}/${ty}`; wanted.add(key);
        let t = this.tiles.get(key);
        if (!t) {
          const img = new Image();
          img.className = 'sm-tile'; img.alt = ''; img.draggable = false;
          img.referrerPolicy = 'origin';   // tile servers (OpenStreetMap) block requests with no Referer; send only the site address, never a path
          img.onload = () => { img.classList.add('loaded'); this._tilesLoaded++; this.notice.hidden = true; };
          img.onerror = () => { this._tilesFailed++; img.remove(); this.tiles.delete(key); this._checkTiles(); };
          const wrapped = ((tx % n) + n) % n;
          img.src = this.tileUrl.replace('{z}', z).replace('{x}', wrapped).replace('{y}', ty);
          this.tileLayer.append(img); t = { img }; this.tiles.set(key, t);
        }
        t.img.style.transform = `translate(${tx * TILE - tl.x}px, ${ty * TILE - tl.y}px)`;
      }
    }
    for (const [key, t] of this.tiles) if (!wanted.has(key)) { t.img.remove(); this.tiles.delete(key); }
    // overlays
    for (const m of this.markers.values()) this._placeMarker(m, tl);
    this._placeLines(tl);
    this.el.dataset.zoom = String(z);
  }
  _checkTiles() {
    if (this.tileUrl && this._tilesLoaded === 0 && this._tilesFailed > 0) this.notice.hidden = false;
  }
  setTileSource(url, attribution) {
    this.tileUrl = url || '';
    this.attrib.textContent = attribution || '';
    for (const t of this.tiles.values()) t.img.remove();
    this.tiles.clear(); this._tilesLoaded = this._tilesFailed = 0; this.notice.hidden = true;
    this.render();
  }

  // ---- interaction
  _bind() {
    let drag = null, moved = false;
    this.el.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || e.target.closest('.sm-controls')) return;
      drag = { x: e.clientX, y: e.clientY, c: project(this.center.lat, this.center.lng, this.zoom), id: e.pointerId };
      moved = false;
    });
    this.el.addEventListener('pointermove', (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!moved && Math.hypot(dx, dy) < 4) return;
      if (!moved) { moved = true; this.el.setPointerCapture?.(drag.id); this.el.classList.add('dragging'); this.hideTip(); }
      this.center = unproject(drag.c.x - dx, drag.c.y - dy, this.zoom);
      this.render();
    });
    const end = () => { drag = null; this.el.classList.remove('dragging'); };
    this.el.addEventListener('pointerup', end);
    this.el.addEventListener('pointercancel', end);
    // a drag must not count as a click on a marker / the background
    this.el.addEventListener('click', (e) => {
      if (moved) { e.stopPropagation(); e.preventDefault(); moved = false; return; }
      if (!e.target.closest('.sm-marker') && !e.target.closest('.sm-controls') && this.onBackgroundClick) this.onBackgroundClick();
    }, true);
    this.el.addEventListener('wheel', (e) => {
      e.preventDefault();
      const now = Date.now();
      if (now - this._lastWheel < 180) return;
      this._lastWheel = now;
      const r = this.el.getBoundingClientRect();
      this.setZoom(this.zoom + (e.deltaY < 0 ? 1 : -1), { x: e.clientX - r.left, y: e.clientY - r.top });
    }, { passive: false });
    this.el.addEventListener('dblclick', (e) => {
      if (e.target.closest('.sm-marker') || e.target.closest('.sm-controls')) return;
      const r = this.el.getBoundingClientRect();
      this.setZoom(this.zoom + 1, { x: e.clientX - r.left, y: e.clientY - r.top });
    });
  }
}
