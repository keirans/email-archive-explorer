/**
 * File Browser dialog (Spec 08 §4.3): pick an archive by browsing instead of
 * typing a path. The server lists folders (confined to Home, data/ and mounted
 * drives) and flags recognised archives; the OS-native dialog is offered when
 * the server can show one.
 */
import { UI, Toast } from './ui.js';

export class FileBrowser {
  constructor(dialog, { onOpen }) {
    this.dialog = dialog;
    this.onOpen = onOpen;
    this.entries = [];
    this.cursor = -1;
    this.filter = '';
    this.current = null;
    this.rootsLoaded = false;
    this._build();
  }

  _build() {
    const { icon } = UI;
    this.dialog.innerHTML = `
      <form method="dialog" class="fb">
        <header class="fb-head">
          <h2>Open archive</h2>
          <button class="btn-icon fb-x" value="cancel" aria-label="Close">${icon('x')}</button>
        </header>
        <div class="fb-main">
          <nav class="fb-places" aria-label="Places">
            <div class="fb-places-title">Places</div>
            <ul data-ref="roots"></ul>
            <div class="fb-places-title">Recent archives</div>
            <ul data-ref="recent"></ul>
          </nav>
          <section class="fb-pane">
            <div class="fb-bar">
              <button type="button" class="btn-icon" data-ref="up" title="Parent folder (Alt+↑)">${icon('up')}</button>
              <div class="fb-crumbs" data-ref="crumbs"></div>
              <input class="fb-filter" data-ref="filter" placeholder="Filter…" aria-label="Filter this folder">
              <button type="button" class="btn-icon" data-ref="hidden" title="Show hidden files">${icon('eye')}</button>
              <button type="button" class="btn-icon" data-ref="refresh" title="Refresh">${icon('refresh')}</button>
            </div>
            <ul class="fb-list" data-ref="list" role="listbox" tabindex="0" aria-label="Folder contents"></ul>
            <div class="fb-note" data-ref="note"></div>
          </section>
        </div>
        <footer class="fb-foot">
          <input class="fb-path" data-ref="path" placeholder="/path/to/archive.pst — or paste a path" aria-label="Archive path" spellcheck="false">
          <button type="button" class="btn-filter-reset fb-native hidden" data-ref="native">${icon('monitor')} System dialog…</button>
          <button type="button" class="btn-filter-reset" data-ref="cancel">Cancel</button>
          <button type="button" class="btn-filter-apply" data-ref="open" disabled>Open</button>
        </footer>
        <div class="fb-error" data-ref="error" role="alert"></div>
      </form>`;
    this.ref = {};
    this.dialog.querySelectorAll('[data-ref]').forEach(n => { this.ref[n.dataset.ref] = n; });
    const r = this.ref;
    this.showHidden = false;

    r.up.onclick = () => this.current?.parent && this.navigate(this.current.parent);
    r.refresh.onclick = () => this.current && this.navigate(this.current.path);
    r.hidden.onclick = () => {
      this.showHidden = !this.showHidden;
      r.hidden.classList.toggle('active', this.showHidden);
      if (this.current) this.navigate(this.current.path);
    };
    r.cancel.onclick = () => this.dialog.close();
    r.open.onclick = () => this._openPath(r.path.value.trim());
    r.native.onclick = () => this._native();
    r.filter.addEventListener('input', () => { this.filter = r.filter.value.toLowerCase(); this._renderList(); });
    r.path.addEventListener('input', () => { r.open.disabled = !r.path.value.trim(); });
    r.path.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); r.open.click(); } });
    r.list.addEventListener('keydown', (e) => this._key(e));
    r.filter.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); r.list.focus(); this._move(1); }
      if (e.key === 'Enter') { e.preventDefault(); this._activate(); }
    });
  }

  async open(startPath) {
    this.ref.error.textContent = '';
    if (!this.dialog.open) this.dialog.showModal();
    try {
      if (!this.rootsLoaded) await this._loadRoots();
      const start = startPath || UI.store('eae-fb-last') || this.roots.find(x => x.kind === 'data')?.path || this.roots[0]?.path;
      await this.navigate(start);
    } catch (err) {
      this._error(err.message);
    }
  }

  async _loadRoots() {
    const data = await UI.jsonOrThrow(await fetch('/api/fs/roots'));
    this.roots = data.roots;
    this.rootsLoaded = true;
    const iconFor = { home: 'home', data: 'archive', mount: 'drive', custom: 'folder' };
    this.ref.roots.innerHTML = data.roots.map(r => `
      <li><button type="button" data-path="${UI.esc(r.path)}" title="${UI.esc(r.path)}">${UI.icon(iconFor[r.kind] || 'folder')} ${UI.esc(r.label)}</button></li>`).join('');
    this.ref.recent.innerHTML = data.recent.length ? data.recent.map(a => `
      <li><button type="button" data-open="${UI.esc(a.path)}" title="${UI.esc(a.path)}">${UI.icon('clock')} ${UI.esc(a.name)}</button></li>`).join('')
      : '<li class="fb-muted">None yet</li>';
    this.ref.roots.querySelectorAll('[data-path]').forEach(b => { b.onclick = () => this.navigate(b.dataset.path); });
    this.ref.recent.querySelectorAll('[data-open]').forEach(b => { b.onclick = () => this._openPath(b.dataset.open); });
    this.ref.native.classList.toggle('hidden', !data.native_picker_available);
  }

  async navigate(path) {
    this.ref.error.textContent = '';
    this.ref.list.innerHTML = '<li class="fb-muted">Loading…</li>';
    try {
      const q = new URLSearchParams({ path, show_hidden: this.showHidden });
      const data = await UI.jsonOrThrow(await fetch(`/api/fs/list?${q}`));
      this.current = data;
      this.entries = data.entries;
      this.filter = '';
      this.ref.filter.value = '';
      this.cursor = this.entries.findIndex(e => e.archive) >= 0 ? this.entries.findIndex(e => e.archive) : 0;
      UI.store('eae-fb-last', data.path);
      this.ref.up.disabled = !data.parent;
      this.ref.crumbs.innerHTML = data.breadcrumbs.map((c, i) =>
        `${i ? '<span class="fb-sep">›</span>' : ''}<button type="button" data-path="${UI.esc(c.path)}">${UI.esc(c.name)}</button>`).join('');
      this.ref.crumbs.querySelectorAll('[data-path]').forEach(b => { b.onclick = () => this.navigate(b.dataset.path); });
      const notes = [];
      if (data.truncated) notes.push(`Showing the first ${data.entries.length.toLocaleString()} items.`);
      if (data.skipped) notes.push(`${data.skipped} item(s) couldn't be read and are hidden.`);
      this.ref.note.textContent = notes.join(' ');
      this._renderList();
      this.ref.list.focus();
    } catch (err) {
      this.ref.list.innerHTML = '';
      this._error(err.message);
    }
  }

  _visible() {
    return this.entries.filter(e => !this.filter || e.name.toLowerCase().includes(this.filter));
  }

  _renderList() {
    const items = this._visible();
    if (this.cursor >= items.length) this.cursor = items.length - 1;
    if (this.cursor < 0 && items.length) this.cursor = 0;
    if (!items.length) {
      this.ref.list.innerHTML = `<li class="fb-muted">${this.filter ? 'Nothing matches the filter.' : 'This folder is empty.'}</li>`;
      return;
    }
    this.ref.list.innerHTML = items.map((e, i) => {
      const kind = e.archive ? 'archive' : e.type;
      const ic = e.archive ? (e.type === 'dir' ? 'mail' : 'archive') : (e.type === 'dir' ? 'folder' : 'file');
      const badge = e.archive ? `<span class="fb-badge">${UI.esc(e.archive.format_name)}</span>` : '';
      const action = e.archive
        ? `<button type="button" class="fb-open" data-open="${i}">Open</button>${e.type === 'dir' ? `<button type="button" class="fb-into" data-into="${i}">Browse</button>` : ''}`
        : '';
      return `<li role="option" class="fb-item fb-${kind} ${i === this.cursor ? 'cursor' : ''}" data-i="${i}" aria-selected="${i === this.cursor}">
        ${UI.icon(ic)}<span class="fb-name" title="${UI.esc(e.name)}">${UI.esc(e.name)}</span>${badge}
        <span class="fb-size">${e.size_formatted || ''}</span>${action}</li>`;
    }).join('');
    this.ref.list.querySelectorAll('[data-i]').forEach(li => {
      const i = +li.dataset.i;
      li.onclick = (ev) => {
        if (ev.target.closest('[data-open]')) return this._openPath(items[i].path);
        if (ev.target.closest('[data-into]')) return this.navigate(items[i].path);
        this.cursor = i;
        this._select();
        if (items[i].type === 'dir' && !items[i].archive) this.navigate(items[i].path);
      };
      li.ondblclick = () => { this.cursor = i; this._activate(); };
    });
    this._select(false);
  }

  _select(scroll = true) {
    const items = this._visible();
    this.ref.list.querySelectorAll('[data-i]').forEach(li => {
      const on = +li.dataset.i === this.cursor;
      li.classList.toggle('cursor', on);
      li.setAttribute('aria-selected', on);
      if (on && scroll) li.scrollIntoView({ block: 'nearest' });
    });
    const e = items[this.cursor];
    if (e && e.archive) { this.ref.path.value = e.path; this.ref.open.disabled = false; }
  }

  _move(d) {
    const n = this._visible().length;
    if (!n) return;
    this.cursor = Math.max(0, Math.min(n - 1, this.cursor + d));
    this._select();
  }

  _activate() {
    const e = this._visible()[this.cursor];
    if (!e) return;
    if (e.archive) this._openPath(e.path);
    else if (e.type === 'dir') this.navigate(e.path);
  }

  _key(e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); this._move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); this._move(-1); }
    else if (e.key === 'Home') { e.preventDefault(); this.cursor = 0; this._select(); }
    else if (e.key === 'End') { e.preventDefault(); this.cursor = this._visible().length - 1; this._select(); }
    else if (e.key === 'Enter') { e.preventDefault(); this._activate(); }
    else if (e.key === 'ArrowRight') {
      const it = this._visible()[this.cursor];
      if (it && it.type === 'dir') { e.preventDefault(); this.navigate(it.path); }
    }
    else if (e.key === 'Backspace' || (e.altKey && e.key === 'ArrowUp') || e.key === 'ArrowLeft') {
      if (this.current?.parent) { e.preventDefault(); this.navigate(this.current.parent); }
    }
    else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // type-to-filter
      this.ref.filter.focus();
    }
  }

  async _native() {
    const btn = this.ref.native;
    btn.disabled = true;
    const label = btn.innerHTML;
    btn.textContent = 'Waiting for the system dialog…';
    try {
      const mode = (this._visible()[this.cursor]?.type === 'dir') ? 'directory' : 'file';
      const data = await UI.jsonOrThrow(await fetch('/api/fs/native-pick', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }),
      }));
      if (data.path) this._openPath(data.path);
    } catch (err) {
      this._error(err.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = label;
    }
  }

  _openPath(path) {
    if (!path) return;
    this.dialog.close();
    this.onOpen(path);
  }

  _error(msg) {
    this.ref.error.textContent = msg;
  }
}

window.FileBrowser = FileBrowser;
