/**
 * Archive Inspector panel (Spec 08 §4.4): technical detail about the open
 * archive. Renders whatever sections the server sends, so new driver facts
 * need no frontend change.
 */
import { UI, Toast } from './ui.js';

export class Inspector {
  constructor(panel, { onReindex, onBrowse }) {
    this.panel = panel;
    this.onReindex = onReindex;
    this.onBrowse = onBrowse;
    this.data = null;
    this.isOpen = UI.store('eae-inspector') === 'open';
    this.panel.classList.toggle('open', this.isOpen);
    this.panel.setAttribute('aria-hidden', String(!this.isOpen));
  }

  toggle(force) {
    this.isOpen = force === undefined ? !this.isOpen : force;
    this.panel.classList.toggle('open', this.isOpen);
    this.panel.setAttribute('aria-hidden', String(!this.isOpen));
    UI.store('eae-inspector', this.isOpen ? 'open' : 'closed');
    document.getElementById('inspector-btn')?.classList.toggle('active', this.isOpen);
    if (this.isOpen) this.refresh();
  }

  async refresh() {
    if (!this.isOpen) return;
    this.panel.innerHTML = this._shell('<div class="insp-muted">Loading…</div>');
    this._wireShell();
    try {
      this.data = await UI.jsonOrThrow(await fetch('/api/archive/details'));
      this.render();
    } catch (err) {
      this.panel.querySelector('.insp-body').innerHTML = `<div class="insp-error">${UI.esc(err.message)}</div>`;
    }
  }

  _shell(body) {
    return `
      <header class="insp-head">
        <h2>${UI.icon('info')} Archive Inspector</h2>
        <button class="btn-icon" data-act="close" aria-label="Close inspector">${UI.icon('x')}</button>
      </header>
      <div class="insp-body">${body}</div>`;
  }

  _wireShell() {
    this.panel.querySelector('[data-act=close]').onclick = () => this.toggle(false);
  }

  render() {
    const d = this.data;
    const path = d.sections[0].items.find(i => i.label === 'Path')?.value || '';
    const sections = d.sections.map(s => `
      <section class="insp-section">
        <h3>${UI.esc(s.title)}</h3>
        <dl>${s.items.map(i => `
          <dt>${UI.esc(i.label)}${i.hint ? ` <span class="insp-hint" title="${UI.esc(i.hint)}">ⓘ</span>` : ''}</dt>
          <dd class="${i.mono ? 'mono' : ''}">${UI.esc(i.value)}</dd>`).join('')}
        </dl>
      </section>`).join('');
    const sum = d.checksum
      ? `<dd class="mono insp-hash">${UI.esc(d.checksum.value)}</dd>
         <dd class="insp-muted">${d.checksum.scope === 'manifest' ? 'Hash of a sorted manifest of every file' : 'Whole file'}</dd>`
      : '<dd class="insp-muted">Not computed yet</dd>';

    this.panel.innerHTML = this._shell(`
      <div class="insp-actions">
        <button class="btn-action" data-act="copy">${UI.icon('copy')} Copy path</button>
        <button class="btn-action" data-act="browse">${UI.icon('folder-open')} Show in browser</button>
        <button class="btn-action" data-act="reindex">${UI.icon('refresh')} Re-index</button>
      </div>
      ${sections}
      <section class="insp-section">
        <h3>Checksum</h3>
        <dl><dt>SHA-256</dt>${sum}</dl>
        <button class="btn-action" data-act="hash">${UI.icon('hash')} ${d.checksum ? 'Recompute' : 'Compute'} SHA-256</button>
      </section>`);
    this._wireShell();
    const q = (a) => this.panel.querySelector(`[data-act=${a}]`);
    q('copy').onclick = async () => {
      try { await navigator.clipboard.writeText(path); Toast.success('Path copied'); }
      catch (e) { Toast.error('Clipboard not available'); }
    };
    q('browse').onclick = () => this.onBrowse(path);
    q('reindex').onclick = () => {
      if (confirm('Rebuild the search index for this archive? Browsing pauses while it runs.')) this.onReindex(path);
    };
    q('hash').onclick = async () => {
      const btn = q('hash');
      btn.disabled = true;
      btn.textContent = 'Hashing…';
      try {
        const r = await UI.jsonOrThrow(await fetch('/api/archive/checksum', { method: 'POST' }));
        this.data.checksum = r;
        this.render();
      } catch (err) {
        Toast.error(`Checksum failed: ${err.message}`);
        btn.disabled = false;
      }
    };
  }
}

window.Inspector = Inspector;
