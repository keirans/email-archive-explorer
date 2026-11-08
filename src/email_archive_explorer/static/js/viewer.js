/**
 * Message & Attachment Viewer Component
 */

import { UI } from './ui.js';

const RECIPIENTS_SHOWN = 8;     // per To/Cc line before "+N more"
const ATTACHMENTS_SHOWN = 6;    // chips before "+N more"
const THREAD_PAGE = 50;         // conversation rows fetched per request
const THREAD_MAX_RENDER = 200;  // never more rows than this in the DOM

const PREFIX_RE = /^\s*(?:(?:re|fw|fwd|aw|sv|wg|vs|antw|tr|rif|ref|recall)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]|\[[^\]]{1,40}\])\s*/i;
function normSubject(s) {
  let x = (s || '').trim();
  for (;;) { const y = x.replace(PREFIX_RE, ''); if (y === x) break; x = y; }
  return x.replace(/\s+/g, ' ').toLowerCase();
}

export class MessageViewer {
  constructor(containerElement, modalElement, docModalElement) {
    this.container = containerElement;
    this.modal = modalElement;
    this.docModal = docModalElement;
    this.currentDetail = null;
    this._setupModals();
  }

  _setupModals() {
    if (this.modal) {
      const closeBtn = this.modal.querySelector('.modal-close');
      if (closeBtn) closeBtn.onclick = () => this.closeModal();
      this.modal.onclick = (e) => {
        if (e.target === this.modal) this.closeModal();
      };
    }

    if (this.docModal) {
      const closeBtn = this.docModal.querySelector('#doc-modal-close');
      if (closeBtn) closeBtn.onclick = () => this.closeModal();
      this.docModal.onclick = (e) => {
        if (e.target === this.docModal) this.closeModal();
      };
    }
  }

  showEmpty() {
    this.currentDetail = null;
    this.container.innerHTML = `
      <div class="empty-view">
        <div class="empty-view-icon">${UI.icon('mail')}</div>
        <div class="empty-view-text">Select an email from the list to read its contents</div>
      </div>
    `;
  }

  render(detail, thread = null) {
    this.currentDetail = detail;
    const esc = (t) => this._escapeHtml(t);
    const formattedDate = detail.date_iso
      ? new Date(detail.date_iso).toLocaleString(undefined, { dateStyle: 'full', timeStyle: 'medium' })
      : 'Unknown date';

    const chips = (type) => {
      const list = (detail.recipients || []).filter(r => r.recipient_type === type);
      if (!list.length) return '';
      const chip = (r) => `<span class="recipient-chip" title="${esc(r.email)}">${esc(r.name || r.email)}</span>`;
      const shown = list.slice(0, RECIPIENTS_SHOWN).map(chip).join('');
      const rest = list.slice(RECIPIENTS_SHOWN);
      const more = rest.length
        ? `<span class="chips-rest hidden">${rest.map(chip).join('')}</span><button class="chip-more" data-act="more" aria-expanded="false">+${rest.length} more</button>`
        : '';
      return `<div class="meta-label">${type}:</div><div class="meta-value chips">${shown}${more}</div>`;
    };

    // Attachments strip: one row, the rest behind "+N more"
    let attachmentsHtml = '';
    const atts = detail.attachments || [];
    if (atts.length) {
      const chip = (att) => {
        const icon = this._getFileIcon(att.filename, att.mime_type);
        const isImage = att.mime_type.startsWith('image/');
        const isPdf = att.filename.toLowerCase().endsWith('.pdf');
        const isTextDoc = /\.(txt|log|csv|ini|dat|htm|html|cfg)$/i.test(att.filename);
        let viewBtn = '';
        if (isImage) {
          viewBtn = `<button class="btn-att-action" title="Preview image" aria-label="Preview image" data-preview="image" data-url="${att.view_url}" data-name="${esc(att.filename)}">${UI.icon('eye')}</button>`;
        } else if (isPdf) {
          viewBtn = `<a href="${att.view_url}" target="_blank" class="btn-att-action" title="Open PDF in new tab" aria-label="Open PDF in new tab">${UI.icon('eye')}</a>`;
        } else if (isTextDoc) {
          viewBtn = `<button class="btn-att-action" title="View text document" aria-label="View text document" data-preview="doc" data-url="${att.view_url}" data-name="${esc(att.filename)}">${UI.icon('eye')}</button>`;
        }
        return `
          <div class="attachment-chip">
            <span class="attachment-icon">${icon}</span>
            <div class="attachment-info">
              <span class="attachment-name" title="${esc(att.filename)}">${esc(att.filename)}</span>
              <span class="attachment-size">${att.size_formatted}</span>
            </div>
            <div class="attachment-actions">
              ${viewBtn}
              <a href="${att.download_url}" class="btn-att-action" title="Download attachment" aria-label="Download ${esc(att.filename)}">${UI.icon('download')}</a>
            </div>
          </div>`;
      };
      const rest = atts.slice(ATTACHMENTS_SHOWN);
      attachmentsHtml = `
        <div class="attachments-strip">
          <div class="attachments-strip-title"><span>${UI.icon('clip')} Attachments (${atts.length})</span></div>
          <div class="attachments-list">
            ${atts.slice(0, ATTACHMENTS_SHOWN).map(chip).join('')}
            ${rest.length ? `<span class="chips-rest hidden">${rest.map(chip).join('')}</span><button class="chip-more" data-act="more" aria-expanded="false">+${rest.length} more</button>` : ''}
          </div>
        </div>`;
    }

    const hasHtml = Boolean(detail.body_html && detail.body_html.trim());
    const mode = hasHtml ? this._bodyMode() : 'text';
    const bodyToggle = hasHtml ? `
      <div class="body-toggle" role="tablist" aria-label="Message body format">
        <button role="tab" data-mode="html" aria-selected="${mode === 'html'}" class="${mode === 'html' ? 'active' : ''}">HTML</button>
        <button role="tab" data-mode="text" aria-selected="${mode === 'text'}" class="${mode === 'text' ? 'active' : ''}">Text</button>
      </div>` : '';

    this.container.innerHTML = `
      <div class="reading-container ${this._compact() ? 'compact' : ''}">
        <div class="reading-header" data-ref="header">
          <div class="view-header">
            <div class="view-header-top">
              <h1 class="view-subject" title="${esc(detail.subject || '(No Subject)')}">${esc(detail.subject || '(No Subject)')}</h1>
              <div class="view-actions">
                <button class="btn-icon" data-act="compact" title="Compact header (Shift+H)" aria-label="Compact header" aria-pressed="${this._compact()}">${UI.icon('list')}</button>
                <a href="/api/messages/${detail.id}/eml" class="btn-action" title="Export as standard RFC 822 .eml file">
                  ${UI.icon('download')} Export .EML
                </a>
              </div>
            </div>
            <div class="view-meta-grid">
              <div class="meta-label">From:</div>
              <div class="meta-value meta-from"><strong>${esc(detail.sender_name || detail.sender_email || 'Unknown')}</strong></div>
              ${chips('To')}
              ${chips('Cc')}
              <div class="meta-label">Date:</div>
              <div class="meta-value">${formattedDate}</div>
            </div>
          </div>
          <div class="thread-section" data-ref="thread" hidden></div>
          ${attachmentsHtml}
        </div>
        ${bodyToggle}
        <div class="view-body" data-ref="body"></div>
      </div>
    `;

    this.allowRemote = false;
    this._renderBody();
    this.container.querySelectorAll('.body-toggle [data-mode]').forEach(b => {
      b.onclick = () => this.setBodyMode(b.dataset.mode);
    });
    this.container.querySelectorAll('[data-act=more]').forEach(b => {
      b.onclick = () => {
        b.previousElementSibling.classList.remove('hidden');
        b.remove();
      };
    });
    this.container.querySelector('[data-act=compact]').onclick = () => this.toggleCompact();
    this.container.querySelectorAll('[data-preview]').forEach(b => {
      b.onclick = () => (b.dataset.preview === 'image'
        ? this.previewImage(b.dataset.url, b.dataset.name)
        : this.previewDocument(b.dataset.url, b.dataset.name));
    });
    this._setThread(thread, detail.id);
  }

  // ---------------------------------------------------------------------
  // Compact header (Shift+H)
  // ---------------------------------------------------------------------
  _compact() { return UI.store('eae-compact-header') === 'on'; }

  toggleCompact() {
    const on = !this._compact();
    UI.store('eae-compact-header', on ? 'on' : 'off');
    const rc = this.container.querySelector('.reading-container');
    if (rc) rc.classList.toggle('compact', on);
    const b = this.container.querySelector('[data-act=compact]');
    if (b) b.setAttribute('aria-pressed', String(on));
  }

  // ---------------------------------------------------------------------
  // Conversation bar (Spec 10 §3.2)
  // ---------------------------------------------------------------------
  _threadOpen() { return UI.store('eae-thread-open') === 'open'; }

  _setThread(win, messageId) {
    this.thread = null;
    const box = this.container.querySelector('[data-ref=thread]');
    if (!box) return;
    if (!win || win.size <= 1) { box.hidden = true; box.innerHTML = ''; return; }
    this.thread = {
      id: messageId, size: win.size, position: win.position, method: win.method,
      first: win.first_date, last: win.last_date, start: win.offset, items: win.items.slice(),
    };
    box.hidden = false;
    this._renderThread();
  }

  _renderThread() {
    const t = this.thread;
    const box = this.container.querySelector('[data-ref=thread]');
    if (!t || !box) return;
    const open = this._threadOpen();
    const fmt = (d) => d ? new Date(d).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '';
    const span = t.first && t.last ? `${fmt(t.first)}${fmt(t.first) !== fmt(t.last) ? ` – ${fmt(t.last)}` : ''}` : '';
    const label = t.method === 'subject' ? 'Related by subject' : 'Conversation';
    const hint = {
      headers: 'Linked by reply headers (In-Reply-To / References)',
      'conversation-index': "Linked by Outlook's conversation index",
      subject: 'Grouped by matching subject and date; may include unrelated messages',
    }[t.method] || '';
    const end = t.start + t.items.length;
    const rootSubject = normSubject(this.currentDetail ? this.currentDetail.subject : '');
    const rows = !open ? '' : `
      <div class="thread-scroll" data-ref="tscroll">
        ${t.start > 0 ? `<button class="thread-load" data-load="before">Load earlier (${t.start})</button>` : ''}
        <ul class="thread-rows" role="listbox" aria-label="Messages in this conversation">
          ${t.items.map((m, i) => {
            const pos = t.start + i + 1;
            const active = m.id === t.id;
            const sameSubj = rootSubject && normSubject(m.subject) === rootSubject;
            const text = sameSubj && m.body_preview ? `<span class="thread-row-snippet">${this._escapeHtml(m.body_preview)}</span>` : this._escapeHtml(m.subject);
            return `<li role="option" class="thread-row ${active ? 'active' : ''}" data-id="${m.id}" aria-selected="${active}" tabindex="${active ? 0 : -1}">
              <span class="thread-row-pos">${pos}</span>
              <span class="thread-row-sender" title="${this._escapeHtml(m.sender_name)}">${this._escapeHtml(m.sender_name || 'Unknown')}</span>
              <span class="thread-row-date">${fmt(m.date_iso)}</span>
              <span class="thread-row-subject">${text}</span>
              ${m.has_attachments ? `<span class="thread-row-att" title="${m.attachment_count} attachments">${UI.icon('clip')}</span>` : ''}
            </li>`;
          }).join('')}
        </ul>
        ${end < t.size ? `<button class="thread-load" data-load="after">Load more (${t.size - end})</button>` : ''}
      </div>`;
    box.innerHTML = `
      <div class="thread-bar">
        <span class="thread-title" title="${hint}">${UI.icon('chat')} ${label}
          <span class="thread-count">· ${t.position} of ${t.size.toLocaleString()}</span>
          ${span ? `<span class="thread-span">· ${span}</span>` : ''}</span>
        <span class="thread-nav">
          <button class="btn-thread" data-nav="-1" ${t.position <= 1 ? 'disabled' : ''} title="Previous in conversation (p)">‹ Prev</button>
          <button class="btn-thread" data-nav="1" ${t.position >= t.size ? 'disabled' : ''} title="Next in conversation (n)">Next ›</button>
          <button class="btn-icon thread-toggle ${open ? 'open' : ''}" data-act="tthread" aria-expanded="${open}"
            title="${open ? 'Hide' : 'Show'} conversation (t)" aria-label="${open ? 'Hide' : 'Show'} conversation messages">${UI.icon('chevron')}</button>
        </span>
      </div>${rows}`;
    box.querySelectorAll('[data-nav]').forEach(b => { b.onclick = () => this.threadStep(+b.dataset.nav); });
    box.querySelector('[data-act=tthread]').onclick = () => this.toggleThread();
    box.querySelectorAll('[data-load]').forEach(b => { b.onclick = () => this._loadThread(b.dataset.load); });
    const list = box.querySelector('.thread-rows');
    if (list) {
      list.querySelectorAll('.thread-row').forEach(li => {
        li.onclick = () => window.appSelectMessage(+li.dataset.id);
      });
      list.addEventListener('keydown', (e) => this._threadKey(e));
      const active = list.querySelector('.thread-row.active');
      const sc = box.querySelector('[data-ref=tscroll]');
      if (active && sc) sc.scrollTop = active.offsetTop - sc.clientHeight / 2 + active.offsetHeight / 2;
    }
  }

  _threadKey(e) {
    const rows = [...this.container.querySelectorAll('.thread-row')];
    const i = rows.indexOf(document.activeElement);
    if (i < 0) return;
    let target = null;
    if (e.key === 'ArrowDown') target = rows[i + 1];
    else if (e.key === 'ArrowUp') target = rows[i - 1];
    else if (e.key === 'Home') target = rows[0];
    else if (e.key === 'End') target = rows[rows.length - 1];
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); rows[i].click(); return; }
    else return;
    e.preventDefault();
    e.stopPropagation();
    if (target) { rows[i].tabIndex = -1; target.tabIndex = 0; target.focus(); }
  }

  /** Expand/collapse the conversation; `preset` stores the choice before a message loads. */
  toggleThread(force, preset = false) {
    const open = force === undefined ? !this._threadOpen() : force;
    if (!this.thread && !preset) return;
    UI.store('eae-thread-open', open ? 'open' : 'closed');
    if (this.thread) this._renderThread();
  }

  async _loadThread(dir) {
    const t = this.thread;
    if (!t) return;
    const offset = dir === 'before' ? Math.max(0, t.start - THREAD_PAGE) : t.start + t.items.length;
    const limit = dir === 'before' ? t.start - offset : THREAD_PAGE;
    const win = await UI.jsonOrThrow(await fetch(`/api/messages/${t.id}/thread?offset=${offset}&limit=${limit}`));
    if (this.thread !== t) return;
    if (dir === 'before') {
      t.items = win.items.concat(t.items);
      t.start = offset;
      if (t.items.length > THREAD_MAX_RENDER) t.items = t.items.slice(0, THREAD_MAX_RENDER);
    } else {
      t.items = t.items.concat(win.items);
      const drop = t.items.length - THREAD_MAX_RENDER;
      if (drop > 0) { t.items = t.items.slice(drop); t.start += drop; }
    }
    this._renderThread();
  }

  /** Open the previous (-1) or next (+1) message in the conversation. */
  async threadStep(dir) {
    const t = this.thread;
    if (!t) return;
    const target = t.position - 1 + dir;      // 0-based index in the conversation
    if (target < 0 || target >= t.size) return;
    let item = t.items[target - t.start];
    if (!item) {
      const win = await UI.jsonOrThrow(await fetch(`/api/messages/${t.id}/thread?offset=${target}&limit=1`));
      item = win.items[0];
    }
    if (item) window.appSelectMessage(item.id);
  }

  _bodyMode() {
    try { return localStorage.getItem('eae-body-mode') || 'html'; } catch (e) { return 'html'; }
  }

  setBodyMode(mode) {
    try { localStorage.setItem('eae-body-mode', mode); } catch (e) { /* ignore */ }
    if (!this.currentDetail) return;
    this.container.querySelectorAll('.body-toggle [data-mode]').forEach(b => {
      const on = b.dataset.mode === mode;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on);
    });
    this._renderBody();
  }

  toggleBodyMode() {
    if (!this.currentDetail || !this.currentDetail.body_html) return;
    this.setBodyMode(this._bodyMode() === 'html' ? 'text' : 'html');
  }

  _renderBody() {
    const detail = this.currentDetail;
    const box = this.container.querySelector('[data-ref=body]');
    if (!box || !detail) return;
    const useHtml = detail.body_html && detail.body_html.trim() && this._bodyMode() === 'html';
    if (!useHtml) {
      box.innerHTML = `<pre>${this._formatBody(detail.body_plain || '')}</pre>`;
      return;
    }
    const html = detail.body_html;
    const hasRemote = /<img[^>]+src=["']?https?:/i.test(html) || /url\(\s*["']?https?:/i.test(html);
    // No scripts, no same-origin access: sandbox="" plus a CSP that blocks
    // every network request unless the user opts in to remote images.
    const csp = `default-src 'none'; style-src 'unsafe-inline'; img-src data: ${this.allowRemote ? 'http: https:' : ''}; font-src data:`;
    const doc = `<!doctype html><html><head><meta charset="utf-8">
      <meta http-equiv="Content-Security-Policy" content="${csp}">
      <base target="_blank">
      <style>body{font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#111;background:#fff;margin:16px;word-wrap:break-word}img{max-width:100%;height:auto}</style>
      </head><body>${html}</body></html>`;
    box.innerHTML = `
      ${hasRemote && !this.allowRemote ? `<div class="remote-banner">Remote images are blocked to protect your privacy.
        <button class="btn-filter-toggle" data-act="remote">Load remote images</button></div>` : ''}
      <iframe class="html-body" sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer" title="Message body (HTML)"></iframe>`;
    box.querySelector('iframe').srcdoc = doc;
    const btn = box.querySelector('[data-act=remote]');
    if (btn) btn.onclick = () => { this.allowRemote = true; this._renderBody(); };
  }

  previewImage(url, caption) {
    if (!this.modal) return;
    const img = this.modal.querySelector('.modal-img');
    const cap = this.modal.querySelector('.modal-caption');
    if (img) img.src = url;
    if (cap) cap.textContent = caption;
    this.modal.classList.add('active');
  }

  async previewDocument(url, filename) {
    if (!this.docModal) return;
    const title = this.docModal.querySelector('#doc-modal-title');
    const code = this.docModal.querySelector('#doc-modal-code');
    const dl = this.docModal.querySelector('#doc-modal-download');

    if (title) title.textContent = filename;
    if (dl) dl.href = `${url}?download=1`;
    if (code) code.textContent = 'Loading document...';
    this.docModal.classList.add('active');

    try {
      const res = await fetch(url);
      const text = await res.text();
      if (code) code.textContent = text;
    } catch (err) {
      if (code) code.textContent = `Error loading document: ${err.message}`;
    }
  }

  closeModal() {
    if (this.modal) {
      this.modal.classList.remove('active');
      const img = this.modal.querySelector('.modal-img');
      if (img) img.src = '';
    }
    if (this.docModal) {
      this.docModal.classList.remove('active');
      const code = this.docModal.querySelector('#doc-modal-code');
      if (code) code.textContent = '';
    }
  }

  _getFileIcon(filename, mimeType) {
    return UI.fileIcon(null, UI.extOf(filename), mimeType);
  }

  _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text || '';
    return div.innerHTML;
  }

  _formatBody(text) {
    const escaped = this._escapeHtml(text);
    const urlPattern = /https?:\/\/[^\s<>'"]+/g;
    return escaped.replace(urlPattern, (url) => {
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`;
    });
  }
}

window.MessageViewer = MessageViewer;
