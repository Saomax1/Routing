// Thin fetch wrapper for the JSON API. Sends X-Requested-With (CSRF defence) and surfaces readable errors.
// The browser never sees any API keys: the Housecall Pro key lives only on the server.

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

let onUnauthorized = () => {};
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn; };

async function req(method, path, body) {
  const opts = { method, credentials: 'same-origin', headers: { 'X-Requested-With': 'routing-app' } };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opts); } catch { throw new ApiError(0, 'Cannot reach the server'); }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && path !== '/api/auth/login' && path !== '/api/auth/me') onUnauthorized();
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || `Request failed (${res.status})`);
  return data;
}

export const api = {
  me: () => req('GET', '/api/auth/me'),
  login: (email, password) => req('POST', '/api/auth/login', { email, password }),
  logout: () => req('POST', '/api/auth/logout'),
  config: () => req('GET', '/api/config'),
  dispatch: (date) => req('GET', '/api/dispatch' + (date ? `?date=${encodeURIComponent(date)}` : '')),
  job: (id) => req('GET', `/api/jobs/${encodeURIComponent(id)}`),
  slots: (id, days) => req('POST', `/api/jobs/${encodeURIComponent(id)}/slots`, days ? { days } : {}),
  setException: (id, body) => req('PUT', `/api/jobs/${encodeURIComponent(id)}/exception`, body),
  clearException: (id) => req('DELETE', `/api/jobs/${encodeURIComponent(id)}/exception`),
  areas: (days) => req('GET', '/api/areas' + (days ? `?days=${encodeURIComponent(days)}` : '')),
  technicians: () => req('GET', '/api/technicians'),
  updateTech: (id, body) => req('PUT', `/api/technicians/${encodeURIComponent(id)}`, body),
  settings: () => req('GET', '/api/settings'),
  saveSettings: (body) => req('PUT', '/api/settings', body),
  syncStatus: () => req('GET', '/api/sync/status'),
  syncRun: () => req('POST', '/api/sync/run'),
  parseReview: () => req('GET', '/api/parse-review'),
  markReviewed: (id) => req('POST', `/api/parse-review/${encodeURIComponent(id)}/reviewed`),
  users: () => req('GET', '/api/users'),
  createUser: (body) => req('POST', '/api/users', body),
  deleteUser: (id) => req('DELETE', `/api/users/${id}`),
};
