/**
 * Attachment Explorer (Spec 09)
 *
 * Browse every attachment in the active archive, filter out signature/stationery
 * noise, collapse forwarded duplicates, preview files, peek inside ZIP/TAR
 * attachments, and jump to the message each one came from.
 */

import { UI } from './ui.js';

export class AttachmentExplorer {
  constructor(root, { onOpenMessage } = {}) {
    this.root = root;
    this.onOpenMessage = onOpenMessage || (() => {});
    this.defaults = {
      filters: { q: '', category: '', ext: '', year: '', sender: '', min_size: '', hide_noise: true, rare_only: false },
      collapse: true,
      sort: 'size',
      order: 'desc',
      mode: 'list',
      lens: 'largest',
      pageSize: 60,
    };
    this._restore();
    this.page = 1;
    this.pageSize = [25, 60, 100, 200].includes(+this.pageSize) ? +this.pageSize : 60;
    this.items = [];
    this.total = 0;
    this.totalPages = 1;
    this.cursor = -1;
    this.selected = new Set();
    this.summary = null;
    this.loaded = false;
    this._detailTimer = null;
    this._detailSeq = 0;
    this._listSeq = 0;
    this._buildShell();
  }

  // ---------------------------------------------------------------------
  // Persistence (per-viewer convenience only)
  // ---------------------------------------------------------------------

  _restore() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem('eae-attachments') || 'null'); } catch (e) { saved = null; }
    const d = JSON.parse(JSON.stringify(this.defaults));
    if (saved && typeof saved === 'object') {
      Object.assign(d, saved, { filters: Object.assign(d.filters, saved.filters || {}) });
    }
    Object.assign(this, d);
  }

  _persist() {
    const { filters, collapse, sort, order, mode, lens, pageSize } = this;
    try { localStorage.setItem('eae-attachments', JSON.stringify({ filters, collapse, sort, order, mode, lens, pageSize })); } catch (e) { /* ignore */ }
  }

  // ---------------------------------------------------------------------
  // Lifecycle
  // ---------------------------------------------------------------------

  async activate() {
    if (!this.loaded) {
      this.loaded = true;
      await Promise.all([this.loadSummary(), this.loadList()]);
    }
  }

  reset() {
    this.loaded = false;
    this.items = [];
    this.selected.clear();
    this.cursor = -1;
    this.page = 1;
    this.filters.year = '';
    this.filters.ext = '';
    this.filters.sender = '';
    this.summary = null;
    this._renderDetailEmpty();
    this._syncControls();
  }

  // ---------------------------------------------------------------------
  // Shell
  // ---------------------------------------------------------------------

  _buildShell() {
    this.root.innerHTML = `
      <aside class="att-sidebar" aria-label="Attachment filters">
        <div class="att-search">
          <span class="att-search-icon">${UI.icon('search')}</span>
          <input type="text" class="att-input" data-ref="q" placeholder="Filename contains…  ( / )" autocomplete="off" spellcheck="false" aria-label="Filename contains">
        </div>
        <label class="att-toggle"><input type="checkbox" data-ref="hideNoise"> Hide noise <span class="att-muted" data-ref="noiseCount"></span></label>
        <label class="att-toggle"><input type="checkbox" data-ref="collapse"> Collapse duplicates</label>

        <div class="att-facet-title">Category</div>
        <ul class="att-facet-list" data-ref="categories"></ul>

        <div class="att-facet-title">Year</div>
        <div class="att-years" data-ref="years"></div>

        <div class="att-facet-title">Size</div>
        <select class="select-compact att-full" data-ref="minSize" aria-label="Minimum size">
          <option value="">Any size</option>
          <option value="51200">Over 50 KB</option>
          <option value="102400">Over 100 KB</option>
          <option value="1048576">Over 1 MB</option>
          <option value="10485760">Over 10 MB</option>
        </select>

        <div class="att-facet-title">Sender</div>
        <input type="text" class="att-input att-full" data-ref="sender" placeholder="Name or address" autocomplete="off" aria-label="Sender">

        <div class="att-facet-title">Rare file types</div>
        <div class="att-chips" data-ref="rare"></div>
      </aside>

      <section class="att-main">
        <div class="att-discover">
          <div class="att-discover-head">
            <span class="att-discover-title">Discover</span>
            <span class="att-muted" data-ref="totals"></span>
          </div>
          <div class="att-lenses" data-ref="lenses"></div>
          <div class="att-active-filters" data-ref="activeFilters"></div>
        </div>

        <div class="att-toolbar">
          <label class="att-selectall" title="Select all on this page"><input type="checkbox" data-ref="selectAll"></label>
          <div class="att-mode">
            <button class="btn-filter-toggle" data-mode="list" title="List view">${UI.icon('list')} List</button>
            <button class="btn-filter-toggle" data-mode="grid" title="Grid view">${UI.icon('grid')} Grid</button>
          </div>
          <select class="select-compact" data-ref="sort" title="Sort" aria-label="Sort attachments by">
            <option value="size">Size</option>
            <option value="date">Date</option>
            <option value="name">Name</option>
            <option value="sender">Sender</option>
            <option value="copies">Copies</option>
            <option value="type">Type</option>
          </select>
          <button class="btn-filter-toggle" data-ref="order" title="Reverse order">↓</button>
          <span class="att-spacer"></span>
          <div class="att-pager" aria-label="Pages">
            <button class="pager-btn" data-ref="prevTop" title="Previous page ( [ or PgUp )">‹</button>
            <span data-ref="pageTop">1 / 1</span>
            <button class="pager-btn" data-ref="nextTop" title="Next page ( ] or PgDn )">›</button>
            <select class="select-compact att-pager-size" data-ref="pageSize" title="Files per page" aria-label="Files per page">
              <option value="25">25 / page</option>
              <option value="60">60 / page</option>
              <option value="100">100 / page</option>
              <option value="200">200 / page</option>
            </select>
          </div>
          <button class="btn-filter-toggle" data-ref="zip" disabled title="Download selected attachments as one ZIP">${UI.icon('archive')} ZIP selected</button>
        </div>

        <div class="att-results" data-ref="results" tabindex="-1" role="list" aria-label="Attachments"></div>

        <div class="list-pagination">
          <button class="pager-btn" data-ref="prev">‹ Prev</button>
          <span data-ref="pageLabel">Page 1 of 1</span>
          <button class="pager-btn" data-ref="next">Next ›</button>
          <span class="status-right-label att-keys">j/k move · space enlarge · o open email · d download · x select</span>
        </div>
      </section>

      <aside class="att-detail" data-ref="detail" aria-label="Attachment details"></aside>
    `;

    this.ref = {};
    this.root.querySelectorAll('[data-ref]').forEach(n => { this.ref[n.dataset.ref] = n; });
    this._wire();
    this._syncControls();
    this._renderLenses();
    this._renderDetailEmpty();
  }

  _wire() {
    const r = this.ref;
    let qTimer = null;
    r.q.addEventListener('input', () => {
      clearTimeout(qTimer);
      qTimer = setTimeout(() => { this.filters.q = r.q.value.trim(); this._changed(); }, 250);
    });
    let sTimer = null;
    r.sender.addEventListener('input', () => {
      clearTimeout(sTimer);
      sTimer = setTimeout(() => { this.filters.sender = r.sender.value.trim(); this._changed(); }, 300);
    });
    r.hideNoise.addEventListener('change', () => {
      this.filters.hide_noise = r.hideNoise.checked;
      this.loadSummary();
      this._changed(false);
    });
    r.collapse.addEventListener('change', () => { this.collapse = r.collapse.checked; this._changed(false); });
    r.minSize.addEventListener('change', () => { this.filters.min_size = r.minSize.value; this._changed(); });
    r.sort.addEventListener('change', () => {
      this.sort = r.sort.value;
      this.order = (this.sort === 'name' || this.sort === 'sender' || this.sort === 'type') ? 'asc' : 'desc';
      this._changed();
    });
    r.order.addEventListener('click', () => { this.order = this.order === 'asc' ? 'desc' : 'asc'; this._changed(); });
    r.prev.addEventListener('click', () => this.goPage(this.page - 1));
    r.next.addEventListener('click', () => this.goPage(this.page + 1));
    r.prevTop.addEventListener('click', () => this.goPage(this.page - 1));
    r.nextTop.addEventListener('click', () => this.goPage(this.page + 1));
    r.pageSize.addEventListener('change', () => {
      // Keep the first visible item on screen when the page size changes
      const first = (this.page - 1) * this.pageSize;
      this.pageSize = +r.pageSize.value;
      this.page = Math.floor(first / this.pageSize) + 1;
      this._persist();
      this.loadList();
    });
    r.zip.addEventListener('click', () => this.downloadSelected());
    r.selectAll.addEventListener('change', () => {
      this.items.forEach(i => r.selectAll.checked ? this.selected.add(i.id) : this.selected.delete(i.id));
      this._renderResults();
    });
    this.root.querySelectorAll('[data-mode]').forEach(b => b.addEventListener('click', () => {
      this.mode = b.dataset.mode;
      this._persist();
      this._syncControls();
      this._renderResults();
    }));
  }

  _syncControls() {
    const r = this.ref;
    if (!r) return;
    r.q.value = this.filters.q;
    r.sender.value = this.filters.sender;
    r.hideNoise.checked = this.filters.hide_noise;
    r.collapse.checked = this.collapse;
    r.minSize.value = this.filters.min_size;
    r.pageSize.value = String(this.pageSize);
    r.sort.value = this.sort;
    r.order.textContent = this.order === 'asc' ? '↑' : '↓';
    this.root.querySelectorAll('[data-mode]').forEach(b => b.classList.toggle('active', b.dataset.mode === this.mode));
  }

  _changed(clearLens = true) {
    if (clearLens) this.lens = '';
    this.page = 1;
    this._persist();
    this._syncControls();
    this._renderLenses();
    this._renderFacets();
    this.loadList();
  }

  // ---------------------------------------------------------------------
  // Lenses (one-click discovery presets)
  // ---------------------------------------------------------------------

  static LENSES = [
    { id: 'largest', icon: 'size', label: 'Largest', apply: s => { s.sort = 'size'; s.order = 'desc'; } },
    { id: 'rare', icon: 'gem', label: 'Rare types', apply: s => { s.filters.rare_only = true; s.sort = 'type'; s.order = 'asc'; } },
    { id: 'forwarded', icon: 'repeat', label: 'Most forwarded', apply: s => { s.collapse = true; s.sort = 'copies'; s.order = 'desc'; } },
    { id: 'oldest', icon: 'clock', label: 'Oldest', apply: s => { s.sort = 'date'; s.order = 'asc'; } },
    { id: 'newest', icon: 'sparkle', label: 'Newest', apply: s => { s.sort = 'date'; s.order = 'desc'; } },
    { id: 'video', icon: 'video', label: 'Video & audio', apply: s => { s.filters.category = 'video_audio'; s.sort = 'size'; s.order = 'desc'; } },
    { id: 'archives', icon: 'archive', label: 'Archives', apply: s => { s.filters.category = 'archives'; s.sort = 'size'; s.order = 'desc'; } },
    { id: 'slides', icon: 'slides', label: 'Presentations', apply: s => { s.filters.category = 'presentations'; s.sort = 'size'; s.order = 'desc'; } },
    { id: 'programs', icon: 'cog', label: 'Programs', apply: s => { s.filters.category = 'programs'; s.sort = 'size'; s.order = 'desc'; } },
    { id: 'photos', icon: 'image', label: 'Photos', apply: s => { s.filters.category = 'images'; s.filters.min_size = '51200'; s.mode = 'grid'; s.sort = 'size'; s.order = 'desc'; } },
  ];

  applyLens(id) {
    const lens = AttachmentExplorer.LENSES.find(l => l.id === id);
    if (!lens) return;
    const keepNoise = this.filters.hide_noise;
    this.filters = Object.assign({}, this.defaults.filters, { hide_noise: keepNoise });
    this.mode = this.mode === 'grid' && id !== 'photos' ? 'list' : this.mode;
    lens.apply(this);
    this.lens = id;
    this.page = 1;
    this._persist();
    this._syncControls();
    this._renderLenses();
    this._renderFacets();
    this.loadList();
  }

  _renderLenses() {
    const r = this.ref;
    r.lenses.innerHTML = '';
    AttachmentExplorer.LENSES.forEach(l => {
      const b = document.createElement('button');
      b.className = `att-lens ${this.lens === l.id ? 'active' : ''}`;
      b.innerHTML = `${UI.icon(l.icon)} ${UI.esc(l.label)}`;
      b.setAttribute('aria-pressed', String(this.lens === l.id));
      b.onclick = () => this.applyLens(l.id);
      r.lenses.appendChild(b);
    });

    // Active filter chips
    const chips = [];
    const f = this.filters;
    const label = (c) => (this.summary?.categories || []).find(x => x.category === c)?.label || c;
    if (f.category) chips.push(['category', `Category: ${label(f.category)}`]);
    if (f.ext) chips.push(['ext', `Type: .${f.ext}`]);
    if (f.year) chips.push(['year', `Year: ${f.year}`]);
    if (f.q) chips.push(['q', `Name: “${f.q}”`]);
    if (f.sender) chips.push(['sender', `Sender: ${f.sender}`]);
    if (f.min_size) chips.push(['min_size', `Over ${this._fmtBytes(+f.min_size)}`]);
    if (f.rare_only) chips.push(['rare_only', 'Rare types only']);
    r.activeFilters.innerHTML = '';
    chips.forEach(([key, text]) => {
      const c = document.createElement('button');
      c.className = 'att-filter-chip';
      c.innerHTML = `${this._esc(text)} <span aria-hidden="true">✕</span>`;
      c.title = 'Remove filter';
      c.onclick = () => { this.filters[key] = key === 'rare_only' ? false : ''; this._changed(); };
      r.activeFilters.appendChild(c);
    });
    if (chips.length > 1) {
      const clear = document.createElement('button');
      clear.className = 'att-link';
      clear.textContent = 'Clear all';
      clear.onclick = () => {
        this.filters = Object.assign({}, this.defaults.filters, { hide_noise: this.filters.hide_noise });
        this._changed();
      };
      r.activeFilters.appendChild(clear);
    }
  }

  // ---------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------

  _query(extra = {}) {
    const p = new URLSearchParams();
    const f = this.filters;
    Object.entries({
      q: f.q, category: f.category, ext: f.ext, year: f.year, sender: f.sender, min_size: f.min_size,
    }).forEach(([k, v]) => { if (v !== '' && v != null) p.set(k, v); });
    p.set('hide_noise', f.hide_noise ? 'true' : 'false');
    if (f.rare_only) p.set('rare_only', 'true');
    p.set('collapse', this.collapse ? 'true' : 'false');
    p.set('sort', this.sort);
    p.set('order', this.order);
    p.set('page', this.page);
    p.set('page_size', this.pageSize);
    Object.entries(extra).forEach(([k, v]) => p.set(k, v));
    return p.toString();
  }

  async loadSummary() {
    try {
      const res = await fetch(`/api/attachments/summary?hide_noise=${this.filters.hide_noise}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this.summary = await res.json();
      this._renderFacets();
      this._renderLenses();
      this._renderTotals();
    } catch (err) {
      console.warn('Attachment summary failed:', err);
    }
  }

  async loadList() {
    const seq = ++this._listSeq;
    this.ref.results.setAttribute('aria-busy', 'true');
    this.ref.results.innerHTML = UI.skeleton('row', 10);
    try {
      const res = await fetch(`/api/attachments?${this._query()}`);
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
      const data = await res.json();
      if (seq !== this._listSeq) return;  // a newer request superseded this one
      this.ref.results.removeAttribute('aria-busy');
      this.items = data.items;
      this.total = data.total;
      this.totalBytes = data.total_bytes_formatted;
      this.totalPages = data.total_pages;
      this.page = data.page;
      this.cursor = this.items.length ? (this._cursorToEnd ? this.items.length - 1 : 0) : -1;
      this._cursorToEnd = false;
      this._renderResults();
      this.ref.results.scrollTop = 0;
      this._renderTotals();
      this._renderPager();
      if (this.cursor >= 0) this._selectCursor(true);
      else this._renderDetailEmpty();
    } catch (err) {
      if (seq !== this._listSeq) return;
      this.ref.results.innerHTML = `<div class="att-empty att-error">Could not load attachments: ${this._esc(err.message)}</div>`;
    }
  }

  goPage(n, { cursorToEnd = false } = {}) {
    if (n < 1 || n > this.totalPages || n === this.page) return;
    this.page = n;
    this._cursorToEnd = cursorToEnd;
    this.loadList();
  }

  // ---------------------------------------------------------------------
  // Rendering: facets, totals, results
  // ---------------------------------------------------------------------

  _renderTotals() {
    const s = this.summary;
    const r = this.ref;
    if (s) {
      r.noiseCount.textContent = s.noise_count ? `(${s.noise_count.toLocaleString()})` : '';
    }
    const parts = [];
    if (this.total != null) parts.push(`${(this.total || 0).toLocaleString()} ${this.collapse ? 'unique files' : 'files'} · ${this.totalBytes || '0 B'}`);
    if (s && this.filters.hide_noise && s.noise_count) parts.push(`${s.noise_count.toLocaleString()} noise hidden`);
    r.totals.innerHTML = this._esc(parts.join(' · '));
    if (s && this.filters.hide_noise && s.noise_count) {
      const show = document.createElement('button');
      show.className = 'att-link';
      show.textContent = 'show';
      show.onclick = () => { r.hideNoise.checked = false; r.hideNoise.dispatchEvent(new Event('change')); };
      r.totals.appendChild(document.createTextNode(' '));
      r.totals.appendChild(show);
    }
  }

  _renderFacets() {
    const s = this.summary;
    const r = this.ref;
    if (!s) return;

    r.categories.innerHTML = '';
    const all = { category: '', label: 'All attachments', count: s.visible_count };
    [all, ...s.categories].forEach(c => {
      const li = document.createElement('li');
      li.className = `att-facet ${this.filters.category === c.category ? 'active' : ''}`;
      li.innerHTML = `<span>${this._icon(c.category)} ${this._esc(c.label)}</span><span class="att-count">${c.count.toLocaleString()}</span>`;
      li.title = c.size_formatted ? `${c.size_formatted} · ${c.unique} unique` : '';
      li.onclick = () => { this.filters.category = c.category; this.filters.ext = ''; this._changed(); };
      r.categories.appendChild(li);
    });

    const max = Math.max(1, ...s.years.map(y => y.count));
    r.years.innerHTML = '';
    s.years.forEach(y => {
      const b = document.createElement('button');
      b.className = `att-year ${this.filters.year === y.year ? 'active' : ''}`;
      b.title = `${y.year}: ${y.count.toLocaleString()} attachments`;
      b.innerHTML = `<span class="att-year-bar" style="height:${Math.max(6, Math.round(y.count / max * 100))}%"></span><span class="att-year-label">${y.year.slice(2)}</span>`;
      b.onclick = () => { this.filters.year = this.filters.year === y.year ? '' : y.year; this._changed(); };
      r.years.appendChild(b);
    });

    r.rare.innerHTML = '';
    s.rare_types.forEach(t => {
      const c = document.createElement('button');
      c.className = `att-chip ${this.filters.ext === t.ext ? 'active' : ''}`;
      c.textContent = `.${t.ext}`;
      c.title = `${t.count} file${t.count === 1 ? '' : 's'}`;
      c.onclick = () => { this.filters.ext = this.filters.ext === t.ext ? '' : t.ext; this.filters.category = ''; this._changed(); };
      r.rare.appendChild(c);
    });
    if (!s.rare_types.length) r.rare.innerHTML = '<span class="att-muted">None</span>';
  }

  _renderResults() {
    const r = this.ref;
    r.results.className = `att-results ${this.mode === 'grid' ? 'att-grid' : 'att-list'}`;
    if (!this.items.length) {
      r.results.innerHTML = '<div class="att-empty">No attachments match these filters.</div>';
      this._updateSelectionUi();
      return;
    }
    r.results.innerHTML = '';
    this.items.forEach((it, idx) => {
      const node = this.mode === 'grid' ? this._gridCard(it) : this._listRow(it);
      node.dataset.idx = idx;
      node.classList.toggle('cursor', idx === this.cursor);
      node.addEventListener('click', (e) => {
        if (e.target.closest('input[type=checkbox]')) return;
        this.cursor = idx;
        this._selectCursor(false);
      });
      node.addEventListener('dblclick', () => this.openOrigin(it.message));
      const cb = node.querySelector('input[type=checkbox]');
      cb.checked = this.selected.has(it.id);
      cb.addEventListener('change', () => {
        cb.checked ? this.selected.add(it.id) : this.selected.delete(it.id);
        this._updateSelectionUi();
      });
      r.results.appendChild(node);
    });
    this._updateSelectionUi();
  }

  _badges(it) {
    let b = '';
    if (it.copies > 1) b += `<span class="att-badge" title="${it.copies} copies in the archive">×${it.copies}</span>`;
    if (it.is_noise) b += `<span class="att-badge att-badge-noise" title="Hidden by default: ${this._noiseLabel(it.noise_reason)}">noise</span>`;
    return b;
  }

  _listRow(it) {
    const row = document.createElement('div');
    row.className = 'att-row';
    row.setAttribute('role', 'listitem');
    const date = it.message.date_iso ? this._fmtDate(it.message.date_iso) : '—';
    row.innerHTML = `
      <input type="checkbox" aria-label="Select ${this._esc(it.filename)}">
      <span class="att-row-icon">${this._icon(it.category, it.ext)}</span>
      <div class="att-row-main">
        <div class="att-row-name" title="${this._esc(it.filename)}">${this._esc(it.filename)} ${this._badges(it)}</div>
        <div class="att-row-origin" title="${this._esc(it.message.folder_path)}">
          ${this._esc(it.message.sender_name || it.message.sender_email || 'Unknown')} — ${this._esc(it.message.subject)}
        </div>
      </div>
      <span class="att-row-size">${it.size_formatted}</span>
      <span class="att-row-date">${date}</span>
    `;
    return row;
  }

  _gridCard(it) {
    const card = document.createElement('div');
    card.className = 'att-card';
    card.setAttribute('role', 'listitem');
    const thumb = it.preview === 'image'
      ? `<img loading="lazy" src="${it.view_url}" alt="">`
      : `<span class="att-card-icon">${this._icon(it.category, it.ext)}</span><span class="att-card-ext">${this._esc(it.ext || 'file')}</span>`;
    card.innerHTML = `
      <input type="checkbox" aria-label="Select ${this._esc(it.filename)}">
      <div class="att-card-thumb">${thumb}</div>
      <div class="att-card-name" title="${this._esc(it.filename)}">${this._esc(it.filename)}</div>
      <div class="att-card-meta">${it.size_formatted}${it.copies > 1 ? ` · ×${it.copies}` : ''}</div>
    `;
    return card;
  }

  _renderPager() {
    const r = this.ref;
    const from = this.total ? (this.page - 1) * this.pageSize + 1 : 0;
    const to = Math.min(this.total, this.page * this.pageSize);
    r.pageLabel.textContent = `Page ${this.page} of ${this.totalPages} · ${from.toLocaleString()}–${to.toLocaleString()} of ${this.total.toLocaleString()}`;
    r.pageTop.textContent = `${this.page} / ${this.totalPages}`;
    r.prev.disabled = r.prevTop.disabled = this.page <= 1;
    r.next.disabled = r.nextTop.disabled = this.page >= this.totalPages;
  }

  _updateSelectionUi() {
    const r = this.ref;
    const n = this.selected.size;
    r.zip.disabled = n === 0;
    r.zip.innerHTML = `${UI.icon('archive')} ZIP selected${n ? ` (${n})` : ''}`;
    r.selectAll.checked = this.items.length > 0 && this.items.every(i => this.selected.has(i.id));
  }

  // ---------------------------------------------------------------------
  // Selection & detail pane
  // ---------------------------------------------------------------------

  _selectCursor(immediate) {
    const nodes = this.ref.results.querySelectorAll('[data-idx]');
    nodes.forEach(n => n.classList.toggle('cursor', +n.dataset.idx === this.cursor));
    const node = this.ref.results.querySelector(`[data-idx="${this.cursor}"]`);
    if (node) node.scrollIntoView({ block: 'nearest' });
    const it = this.items[this.cursor];
    if (!it) return;
    clearTimeout(this._detailTimer);
    // Debounce while the user holds j/k so we don't fetch every file on the way
    this._detailTimer = setTimeout(() => this.showDetail(it.id), immediate ? 0 : 120);
  }

  move(delta) {
    if (!this.items.length) return;
    const next = Math.max(0, Math.min(this.items.length - 1, this.cursor + delta));
    if (next === this.cursor) {
      if (delta > 0 && this.page < this.totalPages) this.goPage(this.page + 1);
      else if (delta < 0 && this.page > 1) this.goPage(this.page - 1, { cursorToEnd: true });
      return;
    }
    this.cursor = next;
    this._selectCursor(false);
  }

  _renderDetailEmpty() {
    if (!this.ref) return;
    this.ref.detail.classList.remove('expanded');
    this.ref.detail.innerHTML = `
      <div class="empty-view">
        <div class="empty-view-icon">${UI.icon('clip')}</div>
        <div class="empty-view-text">Select an attachment to preview it and see where it came from</div>
      </div>`;
  }

  async showDetail(id) {
    const seq = ++this._detailSeq;
    const pane = this.ref.detail;
    try {
      const res = await fetch(`/api/attachments/${id}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      if (seq !== this._detailSeq) return;
      this.current = d;
      pane.innerHTML = this._detailHtml(d);
      pane.querySelector('[data-act=open]')?.addEventListener('click', () => this.openOrigin(d.origin));
      pane.querySelector('[data-act=copy]')?.addEventListener('click', (e) => this._copy(d.filename, e.target));
      pane.querySelector('[data-act=expand]')?.addEventListener('click', () => this.toggleExpand());
      pane.querySelectorAll('[data-copy-msg]').forEach(n => n.addEventListener('click', () => {
        const c = d.copies_list.find(x => String(x.id) === n.dataset.copyMsg);
        if (c) this.openOrigin(c.message);
      }));
      this._loadPreview(d, pane.querySelector('.att-preview'), seq);
    } catch (err) {
      if (seq !== this._detailSeq) return;
      pane.innerHTML = `<div class="att-empty att-error">Could not load attachment: ${this._esc(err.message)}</div>`;
    }
  }

  _detailHtml(d) {
    const o = d.origin;
    const to = (o.recipients || []).filter(r => r.recipient_type === 'To');
    const toText = to.slice(0, 3).map(r => r.name || r.email).join(', ') + (to.length > 3 ? ` +${to.length - 3}` : '');
    const first = d.first_seen;
    const copies = d.copies_list || [];
    const canOpenTab = ['image', 'pdf', 'text', 'video', 'audio'].includes(d.preview);
    const embedded = d.mime_type === 'application/x-pst-embedded-item';

    const copiesHtml = copies.length > 1 ? `
      <div class="att-section">
        <div class="att-section-title">Copies in this archive (${copies.length})</div>
        <div class="att-firstseen">First seen ${first.message.date_iso ? this._fmtDate(first.message.date_iso) : '(undated)'}
          from <strong>${this._esc(first.message.sender_name || first.message.sender_email || 'Unknown')}</strong></div>
        <ul class="att-copies">
          ${copies.map(c => `
            <li class="${c.id === d.id ? 'current' : ''}" data-copy-msg="${c.id}" title="Open this email">
              <span class="att-copy-date">${c.message.date_iso ? this._fmtDate(c.message.date_iso) : '—'}</span>
              <span class="att-copy-who">${this._esc(c.message.sender_name || c.message.sender_email || 'Unknown')}</span>
              <span class="att-copy-subj">${this._esc(c.message.subject)}</span>
            </li>`).join('')}
        </ul>
      </div>` : '';

    return `
      <div class="att-detail-head">
        <div class="att-detail-icon">${this._icon(d.category, d.ext)}</div>
        <div class="att-detail-titles">
          <div class="att-detail-name" title="${this._esc(d.filename)}">${this._esc(d.filename)}</div>
          <div class="att-muted">${this._esc(d.category_label)} · ${d.size_formatted} · ${this._esc(d.mime_type)}</div>
          ${d.is_noise ? `<div class="att-noise-note">Hidden as noise: ${this._noiseLabel(d.noise_reason)}</div>` : ''}
        </div>
      </div>
      <div class="att-actions">
        ${embedded ? '' : `<a class="btn-action" href="${d.download_url}" title="Download (d)">${UI.icon('download')} Download</a>`}
        ${canOpenTab ? `<a class="btn-action" href="${d.view_url}" target="_blank" rel="noopener" title="Open in a new tab">${UI.icon('external')} Open</a>` : ''}
        <button class="btn-action" data-act="expand" title="Enlarge preview (space)" aria-label="Enlarge preview">${UI.icon('expand')}</button>
        <button class="btn-action" data-act="copy" title="Copy file name" aria-label="Copy file name">${UI.icon('copy')}</button>
      </div>
      <div class="att-preview"></div>
      <div class="att-section att-origin">
        <div class="att-section-title">Origin message</div>
        <div class="att-origin-subject">${this._esc(o.subject)}</div>
        <div class="att-origin-grid">
          <span>From</span><span>${this._esc(o.sender_name || o.sender_email || 'Unknown')}${o.sender_email && o.sender_name ? ` &lt;${this._esc(o.sender_email)}&gt;` : ''}</span>
          ${toText ? `<span>To</span><span>${this._esc(toText)}</span>` : ''}
          <span>Date</span><span>${o.date_iso ? new Date(o.date_iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Unknown'}</span>
          <span>Folder</span><span class="att-folder" title="${this._esc(o.folder_path)}">${this._esc(o.folder_path)}</span>
          ${o.attachment_count > 1 ? `<span>With</span><span>${o.attachment_count - 1} other attachment${o.attachment_count > 2 ? 's' : ''}</span>` : ''}
        </div>
        ${o.body_preview ? `<div class="att-origin-preview">${this._esc(o.body_preview)}</div>` : ''}
        <button class="btn-filter-apply" data-act="open">${UI.icon('mail')} Open email (o)</button>
      </div>
      ${copiesHtml}
    `;
  }

  async _loadPreview(d, box, seq) {
    if (!box) return;
    const big = d.size_bytes > 25 * 1024 * 1024;
    const load = async () => {
      switch (d.preview) {
        case 'image':
          box.innerHTML = `<img class="att-preview-img" src="${d.view_url}" alt="${this._esc(d.filename)}">`;
          break;
        case 'pdf':
          box.innerHTML = `<iframe class="att-preview-frame" src="${d.view_url}" title="${this._esc(d.filename)}"></iframe>`;
          break;
        case 'html':
          // sandbox="" → no scripts, no same-origin access (server also sends CSP: sandbox)
          box.innerHTML = `<iframe class="att-preview-frame" sandbox="" src="${d.view_url}" title="${this._esc(d.filename)}"></iframe>`;
          break;
        case 'video':
          box.innerHTML = `<video class="att-preview-media" controls preload="metadata" src="${d.view_url}"></video>`;
          break;
        case 'audio':
          box.innerHTML = `<audio class="att-preview-audio" controls preload="metadata" src="${d.view_url}"></audio>`;
          break;
        case 'text': {
          box.innerHTML = '<div class="att-muted att-pad">Loading text…</div>';
          const res = await fetch(d.view_url);
          const buf = await res.arrayBuffer();
          if (seq !== this._detailSeq) return;
          const limit = 256 * 1024;
          let text;
          try { text = new TextDecoder('utf-8', { fatal: true }).decode(buf.slice(0, limit)); }
          catch (e) { text = new TextDecoder('windows-1252').decode(buf.slice(0, limit)); }
          box.innerHTML = '<pre class="att-preview-text"></pre>';
          box.firstChild.textContent = text + (buf.byteLength > limit ? '\n\n… (truncated — download for the full file)' : '');
          break;
        }
        case 'archive': {
          box.innerHTML = '<div class="att-muted att-pad">Reading archive…</div>';
          const res = await fetch(`/api/attachments/${d.id}/contents`);
          const data = await res.json();
          if (seq !== this._detailSeq) return;
          if (!res.ok) { box.innerHTML = `<div class="att-muted att-pad">${this._esc(data.detail || 'Cannot read archive')}</div>`; return; }
          const files = data.entries.filter(e => !e.is_dir);
          box.innerHTML = `
            <div class="att-archive-head">${data.kind.toUpperCase()} · ${data.file_count.toLocaleString()} files · ${data.total_size_formatted} uncompressed${data.truncated ? ' · list truncated' : ''}</div>
            <ul class="att-archive-list">
              ${files.map(e => `
                <li>
                  <span class="att-archive-name" title="${this._esc(e.name)}">${e.encrypted ? '🔒 ' : ''}${this._esc(e.name)}</span>
                  <span class="att-archive-size">${e.size_formatted}</span>
                  <span class="att-archive-date">${e.modified_iso ? e.modified_iso.slice(0, 10) : ''}</span>
                  <a class="att-archive-dl" href="/api/attachments/${d.id}/contents/${e.index}" title="Download this file" aria-label="Download ${this._esc(e.name)}">${UI.icon('download')}</a>
                </li>`).join('')}
            </ul>`;
          break;
        }
        default:
          if (d.mime_type === 'application/x-pst-embedded-item') {
            box.innerHTML = `<div class="att-nopreview"><span class="att-card-icon">${UI.icon('mail')}</span>
              <span>An email that was attached to the message below. Outlook stores these in an internal
              format that can't be extracted as a file — open the origin email to read the conversation.</span></div>`;
            break;
          }
          box.innerHTML = `<div class="att-nopreview"><span class="att-card-icon">${this._icon(d.category, d.ext)}</span>
            <span>No in-browser preview for .${this._esc(d.ext || 'this type')} — download to open it.</span></div>`;
      }
    };
    if (big && d.preview !== 'none' && d.preview !== 'video' && d.preview !== 'audio') {
      box.innerHTML = `<div class="att-nopreview"><span>Large file (${d.size_formatted}).</span>
        <button class="btn-filter-toggle">Load preview</button></div>`;
      box.querySelector('button').onclick = () => load().catch(e => { box.textContent = e.message; });
      return;
    }
    try { await load(); } catch (e) { if (seq === this._detailSeq) box.innerHTML = `<div class="att-muted att-pad">Preview failed: ${this._esc(e.message)}</div>`; }
  }

  toggleExpand(force) {
    const pane = this.ref.detail;
    const on = force === undefined ? !pane.classList.contains('expanded') : force;
    pane.classList.toggle('expanded', on);
  }

  openOrigin(message) {
    if (!message) return;
    this.toggleExpand(false);
    this.onOpenMessage(message.id, message.folder_id);
  }

  async downloadSelected() {
    const ids = [...this.selected];
    if (!ids.length) return;
    const btn = this.ref.zip;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Building ZIP…';
    try {
      const res = await fetch('/api/attachments/zip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: ids.slice(0, 500) }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'attachments.zip';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (err) {
      btn.textContent = `ZIP failed: ${err.message}`;
      setTimeout(() => this._updateSelectionUi(), 3000);
      return;
    } finally {
      btn.disabled = false;
    }
    btn.textContent = label;
    this._updateSelectionUi();
  }

  // ---------------------------------------------------------------------
  // Keyboard
  // ---------------------------------------------------------------------

  handleKey(e) {
    const inField = ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName) && e.target.type !== 'checkbox';
    if (inField) {
      if (e.key === 'Escape') { e.target.blur(); return true; }
      return false;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    // Let focused buttons/links handle their own activation keys
    if ((e.key === 'Enter' || e.key === ' ') && ['BUTTON', 'A'].includes(e.target.tagName)) return false;
    const it = this.items[this.cursor];
    switch (e.key) {
      case 'j': case 'ArrowDown': e.preventDefault(); this.move(1); return true;
      case 'k': case 'ArrowUp': e.preventDefault(); this.move(-1); return true;
      case '[': case 'PageUp': e.preventDefault(); this.goPage(this.page - 1); return true;
      case ']': case 'PageDown': e.preventDefault(); this.goPage(this.page + 1); return true;
      case 'Home': e.preventDefault(); this.cursor = 0; this._selectCursor(false); return true;
      case 'End': e.preventDefault(); this.cursor = this.items.length - 1; this._selectCursor(false); return true;
      case '/': e.preventDefault(); this.ref.q.focus(); this.ref.q.select(); return true;
      case ' ': e.preventDefault(); this.toggleExpand(); return true;
      case 'Escape': this.toggleExpand(false); return false;  // let app close other modals too
      case 'o': case 'Enter': if (it) this.openOrigin(it.message); return true;
      case 'd': if (it) window.location.href = it.download_url; return true;
      case 'x':
        if (it) {
          this.selected.has(it.id) ? this.selected.delete(it.id) : this.selected.add(it.id);
          const cb = this.ref.results.querySelector(`[data-idx="${this.cursor}"] input[type=checkbox]`);
          if (cb) cb.checked = this.selected.has(it.id);
          this._updateSelectionUi();
        }
        return true;
      default: return false;
    }
  }

  // ---------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------

  _icon(category, ext) {
    return UI.fileIcon(category, ext);
  }

  _noiseLabel(reason) {
    return {
      'small-image': 'small image (signature, bullet or spacer)',
      'repeated-image': 'image repeated in many emails (logo or stationery)',
      'outlook-artifact': 'Outlook/Exchange artifact',
      'mail-footer': 'mail-server disclaimer footer',
      'embedded-item': 'attached Outlook item that cannot be extracted',
    }[reason] || reason || '';
  }

  _fmtDate(iso) {
    return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  }

  _fmtBytes(n) {
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n % 1 ? n.toFixed(0) : n} ${u[i]}`;
  }

  _copy(text, btn) {
    try {
      navigator.clipboard.writeText(text);
      const old = btn.innerHTML;
      btn.innerHTML = UI.icon('check');
      setTimeout(() => { btn.innerHTML = old; }, 1200);
    } catch (e) { /* clipboard unavailable */ }
  }

  _esc(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }
}

window.AttachmentExplorer = AttachmentExplorer;
