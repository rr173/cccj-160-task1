// Minimal API helper shared by all pages.
const api = {
  async req(method, path, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    const text = await r.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
    if (!r.ok) {
      const msg = data.message || data.error || r.statusText;
      const detail = data.detail && data.detail.errors ? `\n${data.detail.errors.join('\n')}` : '';
      throw new Error(`${data.error || r.status}: ${msg}${detail}`);
    }
    return data;
  },
  get(p) { return this.req('GET', p); },
  post(p, b) { return this.req('POST', p, b || {}); },
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function short(h, n = 12) { return h ? String(h).slice(0, n) + '…' : '—'; }
function statusChip(s) {
  const map = { draft: '', pending: 'warn', published: 'ok', rejected: 'bad' };
  return `<span class="chip ${map[s] || ''}">${esc(s)}</span>`;
}
let toastTimer;
function toast(msg, isErr) {
  let el = document.querySelector('.toast');
  if (!el) { el = document.createElement('div'); el.className = 'toast'; document.body.appendChild(el); }
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = 'toast', 5000);
}
async function guard(fn) { try { await fn(); } catch (e) { toast(e.message, true); } }
