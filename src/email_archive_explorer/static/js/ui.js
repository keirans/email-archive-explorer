/**
 * Shared UI helpers (Spec 08): session-token fetch wrapper, icons, toasts.
 * Imported first by app.js (it patches fetch to send the session token).
 */

  // -----------------------------------------------------------------------
  // Session token: the server injects it into index.html; send it on every
  // same-origin request so state-changing and file-system routes accept us.
  // -----------------------------------------------------------------------
  const tokenMeta = document.querySelector('meta[name="eae-token"]');
  const TOKEN = tokenMeta ? tokenMeta.content : '';
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    const sameOrigin = url.startsWith('/') || url.startsWith(window.location.origin);
    if (sameOrigin && TOKEN) {
      const headers = new Headers(init.headers || (typeof input !== 'string' ? input.headers : undefined));
      headers.set('X-EAE-Token', TOKEN);
      init = Object.assign({}, init, { headers });
    }
    return nativeFetch(input, init);
  };

  function icon(name, cls = '') {
    return `<svg class="icon ${cls}" aria-hidden="true"><use href="/static/icons.svg#i-${name}"></use></svg>`;
  }

  function esc(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  async function jsonOrThrow(res) {
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    return body;
  }

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) { /* storage unavailable */ }
    return null;
  }

  // -----------------------------------------------------------------------
  // Toasts (replace alert()); aria-live so screen readers announce them
  // -----------------------------------------------------------------------
  let host = null;
  function ensureHost() {
    if (!host) {
      host = document.createElement('div');
      host.className = 'toast-host';
      host.setAttribute('aria-live', 'polite');
      document.body.appendChild(host);
    }
    return host;
  }

  const Toast = {
    show(message, { type = 'info', timeout = 4000, progress = null } = {}) {
      const el = document.createElement('div');
      el.className = `toast toast-${type}`;
      el.innerHTML = `
        <div class="toast-body"><span class="toast-msg"></span>
          <button class="toast-close" aria-label="Dismiss">${icon('x')}</button></div>
        <div class="toast-bar ${progress == null ? 'hidden' : ''}"><span></span></div>`;
      el.querySelector('.toast-msg').textContent = message;
      ensureHost().appendChild(el);
      let timer = null;
      const handle = {
        el,
        update(msg, pct, newType) {
          if (msg != null) el.querySelector('.toast-msg').textContent = msg;
          if (newType) el.className = `toast toast-${newType}`;
          const bar = el.querySelector('.toast-bar');
          if (pct == null) bar.classList.add('hidden');
          else { bar.classList.remove('hidden'); bar.firstElementChild.style.width = `${Math.max(2, Math.min(100, pct))}%`; }
          return handle;
        },
        close() { clearTimeout(timer); el.classList.add('leaving'); setTimeout(() => el.remove(), 200); },
        autoClose(ms = 4000) { clearTimeout(timer); timer = setTimeout(() => handle.close(), ms); return handle; },
      };
      el.querySelector('.toast-close').onclick = () => handle.close();
      if (progress != null) handle.update(null, progress);
      if (timeout) handle.autoClose(timeout);
      return handle;
    },
    error(message) { return Toast.show(message, { type: 'error', timeout: 8000 }); },
    success(message) { return Toast.show(message, { type: 'success' }); },
  };

  // File-type icons, tinted per kind (replaces the old emoji)
  const EXT_ICON = {
    pdf: 'pdf', doc: 'doc', docx: 'doc', rtf: 'doc', txt: 'doc', wpd: 'doc', odt: 'doc',
    xls: 'sheet', xlsx: 'sheet', csv: 'sheet', ods: 'sheet',
    ppt: 'slides', pps: 'slides', pptx: 'slides', ppsx: 'slides', odp: 'slides',
    zip: 'archive', tar: 'archive', gz: 'archive', tgz: 'archive', rar: 'archive', '7z': 'archive', cab: 'archive',
    exe: 'cog', dll: 'cog', msi: 'cog', com: 'cog', scr: 'cog',
    vcf: 'user', ics: 'clock', vcs: 'clock', eml: 'mail', msg: 'mail', mht: 'mail',
    jpg: 'image', jpeg: 'image', jpe: 'image', gif: 'image', png: 'image', bmp: 'image', tif: 'image', tiff: 'image', pcx: 'image', svg: 'image',
    mpg: 'video', mpeg: 'video', mpe: 'video', avi: 'video', mp4: 'video', mov: 'video', wmv: 'video', asf: 'video',
    mp3: 'video', wav: 'video', wma: 'video', mid: 'video',
    htm: 'code', html: 'code', xml: 'code', js: 'code', vbs: 'code', bat: 'code', sh: 'code', log: 'code', ini: 'code', cfg: 'code', url: 'code',
    vsd: 'ruler', dwg: 'ruler', dxf: 'ruler',
  };
  const CAT_ICON = {
    documents: 'doc', spreadsheets: 'sheet', presentations: 'slides', images: 'image', video_audio: 'video',
    archives: 'archive', email: 'mail', contacts_calendar: 'user', web_code: 'code', programs: 'cog',
    drawings: 'ruler', other: 'file',
  };
  function fileIcon(category, ext, mime) {
    let e = (ext || '').toLowerCase();
    let name = EXT_ICON[e] || CAT_ICON[category] || ((mime || '').startsWith('image/') ? 'image' : 'file');
    if (!category && !EXT_ICON[e] && (mime || '').startsWith('message/')) name = 'mail';
    return icon(name, `ft ft-${name}`);
  }
  function extOf(filename) {
    const m = /\.([a-z0-9]{1,8})$/i.exec(filename || '');
    return m ? m[1].toLowerCase() : '';
  }

  // Skeleton placeholders shown while data loads
  function skeleton(kind, n = 6, tag = 'div') {
    const rows = {
      card: '<span class="skel-line w40"></span><span class="skel-line w80"></span><span class="skel-line w95"></span>',
      tree: '<span class="skel-line w60"></span>',
      row: '<span class="skel-box"></span><span class="skel-stack"><span class="skel-line w70"></span><span class="skel-line w45"></span></span>',
    }[kind] || '<span class="skel-line w80"></span>';
    return Array.from({ length: n }, (_, i) =>
      `<${tag} class="skel skel-${kind}" aria-hidden="true" style="--i:${i}">${rows}</${tag}>`).join('');
  }

export const UI = { icon, esc, jsonOrThrow, store, fileIcon, extOf, skeleton, token: TOKEN };
export { Toast };
window.UI = UI;          // for console debugging / legacy inline handlers
window.Toast = Toast;
