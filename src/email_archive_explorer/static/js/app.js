/**
 * Main Application Controller for Email Archive Explorer (ES module entry point)
 */
import { UI, Toast } from './ui.js';   // first: installs the session-token fetch wrapper
import { API } from './api.js';
import { FolderTree } from './tree.js';
import { MessageViewer } from './viewer.js';
import { AttachmentExplorer } from './attachments.js';
import { FileBrowser } from './filebrowser.js';
import { Inspector } from './inspector.js';

(function () {
  const state = {
    archiveInfo: null,
    folderTreeData: null,
    currentFolderId: null,
    currentFolderName: '',
    currentPage: 1,
    pageSize: 50,
    totalPages: 1,
    totalMessages: 0,
    sortBy: 'date',
    sortOrder: 'desc',
    hasAttachmentsOnly: false,
    searchQuery: '',
    selectedMessageId: null,
    currentMessages: [],
    view: 'mail',
    // Advanced filters
    filters: {
      dateFrom: '',
      dateTo: '',
      sender: '',
      attachmentExt: ''
    }
  };

  // DOM Elements
  const el = {};

  function _cacheElements() {
    el.brandSubtitle = document.getElementById('brand-subtitle');
    el.archiveStatus = document.getElementById('archive-status');
    el.statusRight = document.getElementById('status-right');
    el.searchInput = document.getElementById('search-input');
    el.searchClear = document.getElementById('search-clear');
    el.themeToggle = document.getElementById('theme-toggle');
    el.treeContainer = document.getElementById('tree-container');
    el.listTitle = document.getElementById('list-title');
    el.listCount = document.getElementById('list-count');
    el.messageList = document.getElementById('message-list');
    el.sortSelect = document.getElementById('sort-select');
    el.filterAttBtn = document.getElementById('filter-att-btn');
    el.exportZipBtn = document.getElementById('export-zip-btn');
    el.prevBtn = document.getElementById('prev-btn');
    el.nextBtn = document.getElementById('next-btn');
    el.pageLabel = document.getElementById('page-label');
    el.viewPane = document.getElementById('view-pane');
    el.modalOverlay = document.getElementById('modal-overlay');
    el.docModal = document.getElementById('doc-modal');
    // Filter drawer elements
    el.filterDrawerToggle = document.getElementById('filter-drawer-toggle');
    el.filterDrawer = document.getElementById('filter-drawer');
    el.filterDateFrom = document.getElementById('filter-date-from');
    el.filterDateTo = document.getElementById('filter-date-to');
    el.filterSender = document.getElementById('filter-sender');
    el.filterAttType = document.getElementById('filter-att-type');
    el.btnApplyFilters = document.getElementById('btn-apply-filters');
    el.btnResetFilters = document.getElementById('btn-reset-filters');
    // Analytics modal
    el.analyticsBtn = document.getElementById('analytics-btn');
    el.analyticsModal = document.getElementById('analytics-modal');
    el.analyticsModalClose = document.getElementById('analytics-modal-close');
    el.analyticsModalBody = document.getElementById('analytics-modal-body');
    // Archive switcher (Spec 06)
    el.archiveMenuBtn = document.getElementById('archive-menu-btn');
    el.archiveDropdown = document.getElementById('archive-dropdown');
    el.archiveList = document.getElementById('archive-list');
    el.archiveMenuLabel = document.getElementById('archive-menu-label');
    el.openFileBtn = document.getElementById('open-file-btn');
    el.rescanBtn = document.getElementById('rescan-btn');
    // Spec 08: file browser, inspector, help
    el.fileBrowserDialog = document.getElementById('file-browser');
    el.inspectorPanel = document.getElementById('inspector');
    el.inspectorBtn = document.getElementById('inspector-btn');
    el.helpBtn = document.getElementById('help-btn');
    el.helpDialog = document.getElementById('help-dialog');
    el.switchingOverlay = document.getElementById('switching-overlay');
    // View tabs & attachment explorer (Spec 09)
    el.tabMail = document.getElementById('tab-mail');
    el.tabAttachments = document.getElementById('tab-attachments');
    el.mailView = document.getElementById('mail-view');
    el.attachmentsView = document.getElementById('attachments-view');
    el.switchingLabel = document.getElementById('switching-label');
  }

  let tree = null;
  let viewer = null;
  let explorer = null;
  let fileBrowser = null;
  let inspector = null;
  let searchDebounceTimer = null;
  let lastGKey = 0;

  async function init() {
    _cacheElements();
    viewer = new MessageViewer(el.viewPane, el.modalOverlay, el.docModal);
    window.viewer = viewer;
    window.appSelectMessage = selectMessage;
    viewer.showEmpty();

    tree = new FolderTree(el.treeContainer, (folderId, folderName) => {
      state.searchQuery = '';
      if (el.searchInput) {
        el.searchInput.value = '';
        el.searchClear.style.display = 'none';
      }
      _clearFilters();
      state.currentFolderId = folderId;
      state.currentFolderName = folderName;
      state.currentPage = state.pendingPage || 1;
      state.pendingPage = null;
      loadMessages();
    });

    if (el.treeContainer) el.treeContainer.innerHTML = UI.skeleton('tree', 12);
    explorer = new AttachmentExplorer(el.attachmentsView, { onOpenMessage: openMessageInMail });
    window.attachmentExplorer = explorer;
    fileBrowser = new FileBrowser(el.fileBrowserDialog, {
      onOpen: (path) => _switchArchive(path, path.split('/').filter(Boolean).pop()),
    });
    inspector = new Inspector(el.inspectorPanel, {
      onReindex: (path) => _switchArchive(path, path.split('/').filter(Boolean).pop(), { force: true }),
      onBrowse: (path) => fileBrowser.open(path.replace(/\/[^/]*$/, '') || '/'),
    });
    if (el.inspectorBtn) {
      el.inspectorBtn.classList.toggle('active', inspector.isOpen);
      el.inspectorBtn.addEventListener('click', () => inspector.toggle());
    }
    if (el.helpBtn) el.helpBtn.addEventListener('click', () => el.helpDialog.showModal());
    _setupSplitters();

    _setupEventListeners();
    _setupArchiveListeners();
    _setupViewTabs();
    _initTheme();

    try {
      const catalog = await API.getArchives();
      if (catalog.active_archive_id == null) {
        _showWelcome(catalog.archives.length);
        return;
      }
      _applyArchiveInfo(await API.getArchiveInfo());

      state.folderTreeData = await API.getFolders(true);
      tree.render(state.folderTreeData);

      const firstFolder = _findFirstFolderWithMessages(state.folderTreeData);
      if (firstFolder) {
        tree.selectFolder(firstFolder.id, firstFolder.name);
      }

      // Populate archive menu after everything loads
      _loadArchiveMenu();

      inspector.refresh();

      let savedView = 'mail';
      try { savedView = localStorage.getItem('eae-view') || 'mail'; } catch (e) { /* ignore */ }
      if (savedView === 'attachments') switchView('attachments');
    } catch (err) {
      console.error('Initialization error:', err);
      if (el.listTitle) el.listTitle.textContent = 'Error loading archive';
      if (el.messageList) {
        el.messageList.innerHTML = `<li style="padding: 20px; color: #ef4444;">${err.message}</li>`;
      }
    }
  }

  /** No archive open yet (e.g. `email-archive serve` with no path): invite the user to pick one. */
  function _showWelcome(knownCount) {
    if (el.treeContainer) el.treeContainer.innerHTML = '';
    if (el.listTitle) el.listTitle.textContent = 'No archive open';
    if (el.messageList) el.messageList.innerHTML = '';
    el.viewPane.innerHTML = `
      <div class="empty-view welcome">
        <div class="empty-view-icon">${UI.icon('archive')}</div>
        <h2 class="welcome-title">Open an email archive</h2>
        <p class="empty-view-text">Outlook PST/OST, MBOX files, or folders of .eml files / Maildirs.
          ${knownCount ? `${knownCount} archive${knownCount === 1 ? '' : 's'} found in <code>data/</code> are in the archive menu.` : ''}</p>
        <button class="btn-filter-apply" data-act="welcome-open">${UI.icon('folder-open')} Open archive…</button>
        <p class="empty-view-text welcome-hint">No mail to hand? Run <code>uv run email-archive demo</code> to create a fictional demo archive in <code>data/demo/</code>.</p>
      </div>`;
    el.viewPane.querySelector('[data-act=welcome-open]').onclick = () => fileBrowser.open();
    _loadArchiveMenu();
    fileBrowser.open();
  }

  // =========================================================================
  // Views: Mail | Attachments (Spec 09)
  // =========================================================================

  function switchView(view) {
    state.view = view === 'attachments' ? 'attachments' : 'mail';
    const att = state.view === 'attachments';
    document.body.dataset.view = state.view;
    if (el.mailView) el.mailView.classList.toggle('view-hidden', att);
    if (el.attachmentsView) el.attachmentsView.classList.toggle('view-hidden', !att);
    [[el.tabMail, !att], [el.tabAttachments, att]].forEach(([tab, on]) => {
      if (!tab) return;
      tab.classList.toggle('active', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (el.filterDrawer) el.filterDrawer.classList.add('hidden');
    try { localStorage.setItem('eae-view', state.view); } catch (e) { /* ignore */ }
    if (att && explorer) explorer.activate();
  }

  function _setupViewTabs() {
    if (el.tabMail) el.tabMail.addEventListener('click', () => switchView('mail'));
    if (el.tabAttachments) el.tabAttachments.addEventListener('click', () => switchView('attachments'));
  }

  function _findFolderById(nodes, id) {
    for (const node of nodes || []) {
      if (node.id === id) return node;
      const found = _findFolderById(node.children, id);
      if (found) return found;
    }
    return null;
  }

  /** Jump from an attachment to the email it came from. */
  async function openMessageInMail(messageId, folderId) {
    switchView('mail');
    state.searchQuery = '';
    if (el.searchInput) el.searchInput.value = '';
    if (el.searchClear) el.searchClear.style.display = 'none';
    _clearFilters();
    state.hasAttachmentsOnly = false;
    if (el.filterAttBtn) el.filterAttBtn.classList.remove('active');

    // Open the folder on the page that contains the message, then select it
    try {
      const q = new URLSearchParams({ page_size: state.pageSize, sort_by: state.sortBy, sort_order: state.sortOrder });
      const res = await fetch(`/api/messages/${messageId}/position?${q}`);
      if (res.ok) {
        const pos = await res.json();
        state.pendingPage = pos.page;
        folderId = pos.folder_id;
      }
    } catch (e) { /* fall back to page 1 */ }

    state.selectedMessageId = messageId;
    const folder = _findFolderById(state.folderTreeData, folderId);
    if (folder && tree) {
      tree.selectFolder(folder.id, folder.name);
      const node = el.treeContainer && el.treeContainer.querySelector(`li[data-folder-id="${folder.id}"] .tree-node`);
      if (node) node.scrollIntoView({ block: 'center' });
    }
    await selectMessage(messageId);
  }

  function _findFirstFolderWithMessages(nodes) {
    for (const node of nodes) {
      if (node.message_count > 0) return node;
      if (node.children && node.children.length > 0) {
        const found = _findFirstFolderWithMessages(node.children);
        if (found) return found;
      }
    }
    return null;
  }

  function _hasActiveFilters() {
    return Boolean(
      state.filters.dateFrom ||
      state.filters.dateTo ||
      state.filters.sender ||
      state.filters.attachmentExt
    );
  }

  function _clearFilters() {
    state.filters.dateFrom = '';
    state.filters.dateTo = '';
    state.filters.sender = '';
    state.filters.attachmentExt = '';
    if (el.filterDateFrom) el.filterDateFrom.value = '';
    if (el.filterDateTo) el.filterDateTo.value = '';
    if (el.filterSender) el.filterSender.value = '';
    if (el.filterAttType) el.filterAttType.value = '';
    if (el.filterDrawerToggle) el.filterDrawerToggle.classList.remove('active');
  }

  async function loadMessages() {
    if (el.messageList) {
      el.messageList.setAttribute('aria-busy', 'true');
      el.messageList.innerHTML = UI.skeleton('card', 7, 'li');
    }

    try {
      let data;
      const isFiltered = _hasActiveFilters();

      if (state.searchQuery || isFiltered) {
        const titleText = state.searchQuery ? `Search: "${state.searchQuery}"` : 'Filtered Emails';
        if (el.listTitle) el.listTitle.textContent = titleText;

        data = await API.searchMessages(
          state.searchQuery,
          null,
          state.hasAttachmentsOnly ? true : null,
          state.currentPage,
          state.pageSize,
          state.filters
        );
        state.totalMessages = data.total;
        state.totalPages = data.total_pages;
        state.currentMessages = data.results;
        renderSearchResults(data.results);
      } else {
        if (!state.currentFolderId) return;
        if (el.listTitle) el.listTitle.textContent = state.currentFolderName || 'Folder';
        data = await API.getFolderMessages(
          state.currentFolderId,
          state.currentPage,
          state.pageSize,
          state.sortBy,
          state.sortOrder,
          state.hasAttachmentsOnly ? true : null
        );
        state.totalMessages = data.total;
        state.totalPages = data.total_pages;
        state.currentMessages = data.items;
        renderMessageList(data.items);
      }

      if (el.messageList) el.messageList.removeAttribute('aria-busy');
      if (el.listCount) el.listCount.textContent = `(${state.totalMessages.toLocaleString()})`;
      _updatePagination();
    } catch (err) {
      console.error('Load messages error:', err);
      if (el.messageList) {
        el.messageList.innerHTML = `<li style="padding: 20px; color: #ef4444;">Failed to load messages: ${err.message}</li>`;
      }
    }
  }

  function renderMessageList(items) {
    if (!el.messageList) return;
    if (!items || items.length === 0) {
      el.messageList.innerHTML = '<li style="padding: 30px; text-align: center; color: var(--text-muted);">No messages found in this folder.</li>';
      return;
    }

    el.messageList.innerHTML = '';
    items.forEach(msg => {
      const li = document.createElement('li');
      li.className = `message-card ${state.selectedMessageId === msg.id ? 'selected' : ''}`;
      li.dataset.msgId = msg.id;
      _asOption(li, state.selectedMessageId === msg.id);

      const dateStr = msg.date_iso ? _formatCompactDate(msg.date_iso) : '';

      li.innerHTML = `
        <div class="card-top">
          <span class="card-sender" title="${_escapeHtml(msg.sender_name)}">${_escapeHtml(msg.sender_name || 'Unknown')}</span>
          <span class="card-date">${dateStr}</span>
        </div>
        <div class="card-middle">
          <span class="card-subject" title="${_escapeHtml(msg.subject)}">${_escapeHtml(msg.subject || '(No Subject)')}</span>
          ${msg.has_attachments ? `<span class="card-att-badge" title="${msg.attachment_count} attachments">${UI.icon('clip')} ${msg.attachment_count}</span>` : ''}
          ${_threadBadge(msg)}
        </div>
        <div class="card-preview">${_escapeHtml(msg.body_preview || '')}</div>
      `;

      li.onclick = (ev) => {
        if (ev.target.closest('[data-thread]')) viewer.toggleThread(true, true);
        selectMessage(msg.id);
      };
      el.messageList.appendChild(li);
    });
    const selected = el.messageList.querySelector('.message-card.selected');
    if (selected) selected.scrollIntoView({ block: 'nearest' });
  }

  function renderSearchResults(results) {
    if (!el.messageList) return;
    if (!results || results.length === 0) {
      el.messageList.innerHTML = '<li style="padding: 30px; text-align: center; color: var(--text-muted);">No matching emails found.</li>';
      return;
    }

    el.messageList.innerHTML = '';
    results.forEach(res => {
      const li = document.createElement('li');
      li.className = `message-card ${state.selectedMessageId === res.id ? 'selected' : ''}`;
      li.dataset.msgId = res.id;
      _asOption(li, state.selectedMessageId === res.id);

      const dateStr = res.date_iso ? _formatCompactDate(res.date_iso) : '';

      li.innerHTML = `
        <div class="card-top">
          <span class="card-sender">${_escapeHtml(res.sender_name || 'Unknown')}</span>
          <span class="card-date">${dateStr}</span>
        </div>
        <div class="card-middle">
          <span class="card-subject">${_escapeHtml(res.subject || '(No Subject)')}</span>
          ${res.has_attachments ? `<span class="card-att-badge" title="${res.attachment_count} attachments">${UI.icon('clip')} ${res.attachment_count}</span>` : ''}
          ${_threadBadge(res)}
        </div>
        <div class="card-preview">${res.snippet || ''}</div>
      `;

      li.onclick = (ev) => {
        if (ev.target.closest('[data-thread]')) viewer.toggleThread(true, true);
        selectMessage(res.id);
      };
      el.messageList.appendChild(li);
    });
  }

  function _threadBadge(m) {
    if (!m.thread_size || m.thread_size < 2) return '';
    return `<span class="card-thread-badge" data-thread title="${m.thread_size} messages in this conversation — click to show them">${UI.icon('chat')} ${m.thread_size}</span>`;
  }

  function _asOption(li, selected) {
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', String(selected));
    li.tabIndex = selected ? 0 : -1;
    li.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); li.click(); }
    });
  }

  async function selectMessage(messageId) {
    state.selectedMessageId = messageId;
    if (el.messageList) {
      el.messageList.querySelectorAll('.message-card').forEach(c => {
        c.classList.remove('selected');
        c.setAttribute('aria-selected', 'false');
        c.tabIndex = -1;
      });
      const active = el.messageList.querySelector(`.message-card[data-msg-id="${messageId}"]`);
      if (active) {
        active.classList.add('selected');
        active.setAttribute('aria-selected', 'true');
        active.tabIndex = 0;
        if (el.messageList.contains(document.activeElement)) active.focus({ preventScroll: true });
      }
    }

    try {
      const [detail, thread] = await Promise.all([
        API.getMessageDetail(messageId),
        API.getMessageThread(messageId)
      ]);
      viewer.render(detail, thread);
    } catch (err) {
      console.error('Error loading email detail:', err);
    }
  }

  function _updatePagination() {
    if (el.pageLabel) {
      el.pageLabel.textContent = `Page ${state.currentPage} of ${Math.max(1, state.totalPages)}`;
    }
    if (el.prevBtn) {
      el.prevBtn.disabled = state.currentPage <= 1;
    }
    if (el.nextBtn) {
      el.nextBtn.disabled = state.currentPage >= state.totalPages;
    }
    if (el.statusRight) {
      el.statusRight.textContent = `${state.totalMessages.toLocaleString()} emails`;
    }
  }

  // =========================================================================
  // Archive Switcher (Spec 06)
  // =========================================================================

  function _archiveYearRange() {
    const range = state.archiveInfo && state.archiveInfo.date_range;
    if (!range || !range.start || !range.end) return '';
    const start = range.start.substring(0, 4);
    const end = range.end.substring(0, 4);
    return start === end ? start : `${start}\u2013${end}`;
  }

  function _applyArchiveInfo(info) {
    state.archiveInfo = info;
    if (el.brandSubtitle) {
      el.brandSubtitle.textContent = `${info.file_size_formatted} \u2022 ${info.total_messages.toLocaleString()} emails`;
    }
    if (el.archiveStatus) {
      el.archiveStatus.textContent = `${info.format} \u2022 ${info.total_attachments.toLocaleString()} attachments`;
    }
    // Constrain the date filters to this archive's span
    const range = info.date_range || {};
    [el.filterDateFrom, el.filterDateTo].forEach(input => {
      if (!input) return;
      if (range.start) input.min = range.start.substring(0, 10); else input.removeAttribute('min');
      if (range.end) input.max = range.end.substring(0, 10); else input.removeAttribute('max');
    });
    const years = _archiveYearRange();
    const titleEl = document.getElementById('analytics-modal-title');
    if (titleEl) titleEl.innerHTML = `${UI.icon('chart')} Archive Analytics &amp; Timeline${years ? ` (${years})` : ''}`;
  }

  async function _loadArchiveMenu() {
    if (!el.archiveList) return;
    try {
      const data = await API.getArchives();
      _renderArchiveList(data);
    } catch (err) {
      console.warn('Could not load archive list:', err.message);
    }
  }

  function _renderArchiveList(data) {
    if (!el.archiveList) return;
    el.archiveList.innerHTML = '';

    if (!data.archives || data.archives.length === 0) {
      el.archiveList.innerHTML = '<div style="padding:12px 16px;color:var(--text-muted);font-size:12px;">No archives discovered</div>';
      return;
    }

    data.archives.forEach(archive => {
      const div = document.createElement('div');
      div.className = `archive-entry ${archive.is_active ? 'active' : ''}`;
      div.title = archive.file_path;

      const dot = archive.is_active
        ? '<span class="archive-active-dot"></span>'
        : '<span class="archive-inactive-dot"></span>';
      const size = archive.file_size_formatted || '';
      const count = archive.total_messages ? `${archive.total_messages.toLocaleString()} msgs` : '';
      const meta = [size, count].filter(Boolean).join(' • ');

      div.innerHTML = `
        ${dot}
        <div class="archive-entry-info">
          <div class="archive-entry-name">${_escapeHtml(archive.name)}</div>
          <div class="archive-entry-meta">${_escapeHtml(meta)}</div>
        </div>
      `;

      if (!archive.is_active) {
        div.onclick = () => _switchArchive(archive.file_path, archive.name);
      }

      el.archiveList.appendChild(div);
    });

    // Update the button label
    const active = data.archives.find(a => a.is_active);
    if (active && el.archiveMenuLabel) {
      const shortName = active.name.length > 22
        ? active.name.substring(0, 20) + '\u2026'
        : active.name;
      el.archiveMenuLabel.textContent = shortName;
    }
  }

  let openJobActive = false;

  async function _switchArchive(filePath, archiveName, { force = false } = {}) {
    _closeArchiveDropdown();
    if (openJobActive) {
      Toast.show('Another archive is still being indexed — please wait for it to finish.');
      return;
    }
    let res;
    try {
      res = await API.openArchive(filePath, force);
    } catch (err) {
      Toast.error(`Couldn't open ${archiveName || 'archive'}: ${err.message}`);
      return;
    }
    if (res.status === 409) {
      Toast.show(`Still indexing ${res.data.job ? res.data.job.name : 'another archive'} — try again when it finishes.`);
      return;
    }
    if (res.status === 202) {
      // Indexing runs on the server; the current archive stays usable meanwhile.
      await _followOpenJob(res.data);
      return;
    }
    await _afterSwitch(archiveName);
  }

  async function _followOpenJob(job) {
    openJobActive = true;
    const toast = Toast.show(`Indexing ${job.name}…`, { timeout: 0, progress: 0 });
    try {
      for (;;) {
        await new Promise(r => setTimeout(r, 500));
        const status = await UI.jsonOrThrow(await fetch(job.status_url));
        if (status.state === 'indexing') {
          const pct = status.total ? (status.indexed / status.total) * 100 : 2;
          toast.update(status.total
            ? `Indexing ${status.name}: ${status.indexed.toLocaleString()} of ${status.total.toLocaleString()} messages`
            : `Indexing ${status.name}: reading folders…`, pct);
          continue;
        }
        if (status.state === 'error') throw new Error(status.error || 'indexing failed');
        toast.update(`${status.name} ready: ${status.archive.total_messages.toLocaleString()} messages (${status.elapsed_s}s)`, null, 'success').autoClose(4000);
        break;
      }
      await _afterSwitch(job.name, false);
    } catch (err) {
      toast.update(`Couldn't open ${job.name}: ${err.message}`, null, 'error').autoClose(10000);
      _loadArchiveMenu();
    } finally {
      openJobActive = false;
    }
  }

  async function _afterSwitch(archiveName, showOverlay = true) {
    if (showOverlay && el.switchingOverlay) el.switchingOverlay.classList.remove('hidden');
    if (el.switchingLabel) el.switchingLabel.textContent = `Loading ${archiveName || 'archive'}\u2026`;
    try {

      // Reset all app state
      state.currentFolderId = null;
      state.currentFolderName = '';
      state.currentPage = 1;
      state.searchQuery = '';
      state.selectedMessageId = null;
      state.currentMessages = [];
      _clearFilters();

      if (el.searchInput) el.searchInput.value = '';
      if (el.searchClear) el.searchClear.style.display = 'none';
      if (el.messageList) el.messageList.innerHTML = '';
      viewer.showEmpty();

      // Reload archive metadata
      _applyArchiveInfo(await API.getArchiveInfo());

      // Reload folder tree
      state.folderTreeData = await API.getFolders(true);
      tree.render(state.folderTreeData);

      const firstFolder = _findFirstFolderWithMessages(state.folderTreeData);
      if (firstFolder) {
        tree.selectFolder(firstFolder.id, firstFolder.name);
      }

      await _loadArchiveMenu();

      if (explorer) {
        explorer.reset();
        if (state.view === 'attachments') explorer.activate();
      }
      if (inspector) inspector.refresh();
    } catch (err) {
      console.error('Archive switch failed:', err);
      Toast.error(`Failed to load the archive: ${err.message}`);
    } finally {
      if (el.switchingOverlay) el.switchingOverlay.classList.add('hidden');
    }
  }

  function _closeArchiveDropdown() {
    if (el.archiveDropdown) el.archiveDropdown.classList.add('hidden');
  }

  function _setupArchiveListeners() {
    // Toggle dropdown
    if (el.archiveMenuBtn) {
      el.archiveMenuBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const isHidden = el.archiveDropdown.classList.toggle('hidden');
        if (!isHidden) _loadArchiveMenu();
      });
    }

    // Close dropdown when clicking outside
    document.addEventListener('click', (e) => {
      if (el.archiveDropdown && !el.archiveDropdown.classList.contains('hidden')) {
        if (!el.archiveDropdown.contains(e.target) && e.target !== el.archiveMenuBtn) {
          _closeArchiveDropdown();
        }
      }
    });

    // Open archive… → file browser dialog
    if (el.openFileBtn) {
      el.openFileBtn.addEventListener('click', () => {
        _closeArchiveDropdown();
        fileBrowser.open();
      });
    }

    // Rescan button
    if (el.rescanBtn) {
      el.rescanBtn.addEventListener('click', async () => {
        _closeArchiveDropdown();
        try {
          const result = await API.scanArchives('data/', true);
          await _loadArchiveMenu();
          if (el.archiveDropdown) el.archiveDropdown.classList.remove('hidden');
          const msg = result.newly_added && result.newly_added.length > 0
            ? `Discovered ${result.discovered_count} archive(s). ${result.newly_added.length} new.`
            : `${result.discovered_count} archive(s) found (no new).`;
          if (el.archiveStatus) el.archiveStatus.textContent = msg;
          Toast.show(msg);
        } catch (err) {
          Toast.error(`Rescan failed: ${err.message}`);
        }
      });
    }
  }

  // =========================================================================
  // Event Listeners (existing)
  // =========================================================================

  function _setupEventListeners() {
    // Search input
    if (el.searchInput) {
      el.searchInput.addEventListener('input', (e) => {
        const query = e.target.value.trim();
        if (el.searchClear) el.searchClear.style.display = query ? 'block' : 'none';
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => {
          state.searchQuery = query;
          state.currentPage = 1;
          loadMessages();
        }, 300);
      });
    }

    if (el.searchClear) {
      el.searchClear.addEventListener('click', () => {
        if (el.searchInput) el.searchInput.value = '';
        el.searchClear.style.display = 'none';
        state.searchQuery = '';
        state.currentPage = 1;
        loadMessages();
      });
    }

    // Filter drawer toggle
    if (el.filterDrawerToggle) {
      el.filterDrawerToggle.addEventListener('click', () => {
        if (!el.filterDrawer) return;
        const isHidden = el.filterDrawer.classList.toggle('hidden');
        el.filterDrawerToggle.classList.toggle('active', !isHidden);
      });
    }

    // Apply & Reset filters
    if (el.btnApplyFilters) {
      el.btnApplyFilters.addEventListener('click', () => {
        state.filters.dateFrom = el.filterDateFrom?.value || '';
        state.filters.dateTo = el.filterDateTo?.value || '';
        state.filters.sender = el.filterSender?.value || '';
        state.filters.attachmentExt = el.filterAttType?.value || '';
        state.currentPage = 1;
        loadMessages();
      });
    }

    if (el.btnResetFilters) {
      el.btnResetFilters.addEventListener('click', () => {
        _clearFilters();
        state.currentPage = 1;
        loadMessages();
      });
    }

    // Sort select
    if (el.sortSelect) {
      el.sortSelect.addEventListener('change', (e) => {
        const val = e.target.value;
        if (val === 'date-desc') { state.sortBy = 'date'; state.sortOrder = 'desc'; }
        else if (val === 'date-asc') { state.sortBy = 'date'; state.sortOrder = 'asc'; }
        else if (val === 'sender') { state.sortBy = 'sender'; state.sortOrder = 'asc'; }
        else if (val === 'subject') { state.sortBy = 'subject'; state.sortOrder = 'asc'; }
        state.currentPage = 1;
        loadMessages();
      });
    }

    // Filter attachments toggle
    if (el.filterAttBtn) {
      el.filterAttBtn.addEventListener('click', () => {
        state.hasAttachmentsOnly = !state.hasAttachmentsOnly;
        el.filterAttBtn.classList.toggle('active', state.hasAttachmentsOnly);
        state.currentPage = 1;
        loadMessages();
      });
    }

    // Export ZIP button
    if (el.exportZipBtn) {
      el.exportZipBtn.addEventListener('click', () => {
        if (state.searchQuery || _hasActiveFilters()) {
          const params = new URLSearchParams({ q: state.searchQuery });
          if (state.filters.sender) params.append('sender', state.filters.sender);
          if (state.filters.attachmentExt) params.append('attachment_ext', state.filters.attachmentExt);
          if (state.filters.dateFrom) params.append('date_from', state.filters.dateFrom);
          if (state.filters.dateTo) params.append('date_to', state.filters.dateTo);
          window.location.href = `/api/search/zip?${params.toString()}`;
        } else if (state.currentFolderId) {
          window.location.href = `/api/folders/${state.currentFolderId}/zip`;
        }
      });
    }

    // Analytics modal
    if (el.analyticsBtn) {
      el.analyticsBtn.addEventListener('click', _openAnalytics);
    }
    if (el.analyticsModalClose) {
      el.analyticsModalClose.addEventListener('click', () => {
        if (el.analyticsModal) el.analyticsModal.classList.remove('active');
      });
    }

    // Pagination buttons
    if (el.prevBtn) {
      el.prevBtn.addEventListener('click', () => {
        if (state.currentPage > 1) {
          state.currentPage--;
          loadMessages();
        }
      });
    }

    if (el.nextBtn) {
      el.nextBtn.addEventListener('click', () => {
        if (state.currentPage < state.totalPages) {
          state.currentPage++;
          loadMessages();
        }
      });
    }

    // Theme toggle
    if (el.themeToggle) {
      el.themeToggle.addEventListener('click', _toggleTheme);
    }

    // Keyboard shortcuts
    window.addEventListener('keydown', _handleKeydown);
  }

  async function _openAnalytics() {
    if (!el.analyticsModal || !el.analyticsModalBody) return;
    el.analyticsModal.classList.add('active');
    el.analyticsModalBody.innerHTML = '<div style="padding: 30px; text-align: center; color: var(--text-muted);">Loading archive analytics...</div>';

    try {
      const data = await API.getArchiveAnalytics();
      const maxCount = Math.max(...data.monthly.map(m => m.count), 1);

      const timelineBars = data.monthly.map(m => {
        const heightPct = Math.max(3, Math.round((m.count / maxCount) * 100));
        return `
          <div class="timeline-bar-col" title="${m.month}: ${m.count.toLocaleString()} emails">
            <div class="timeline-bar" style="height: ${heightPct}%;"></div>
            <span class="timeline-label">${m.month}</span>
          </div>
        `;
      }).join('');

      const sendersRows = data.top_senders.map((s, idx) => `
        <tr>
          <td>${idx + 1}. ${_escapeHtml(s.sender)}</td>
          <td>${s.count.toLocaleString()}</td>
        </tr>
      `).join('');

      const attachmentsRows = data.top_attachments.map(a => `
        <tr>
          <td>.${a.ext}</td>
          <td>${a.count.toLocaleString()} (${a.formatted_size})</td>
        </tr>
      `).join('');

      el.analyticsModalBody.innerHTML = `
        <div>
          <div class="analytics-section-title">Email Activity Timeline${_archiveYearRange() ? ` (${_archiveYearRange()})` : ''}</div>
          <div class="timeline-chart">
            ${timelineBars}
          </div>
        </div>
        <div class="analytics-grid">
          <div class="analytics-card">
            <div class="analytics-section-title">Top 10 Senders</div>
            <table class="analytics-table">
              <thead><tr><th>Sender</th><th>Emails</th></tr></thead>
              <tbody>${sendersRows}</tbody>
            </table>
          </div>
          <div class="analytics-card">
            <div class="analytics-section-title">Top 10 Attachment Types</div>
            <table class="analytics-table">
              <thead><tr><th>Type</th><th>Files &amp; Volume</th></tr></thead>
              <tbody>${attachmentsRows}</tbody>
            </table>
          </div>
        </div>
      `;
    } catch (err) {
      el.analyticsModalBody.innerHTML = `<div style="padding: 20px; color: #ef4444;">Failed to load analytics: ${err.message}</div>`;
    }
  }

  function _handleKeydown(e) {
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName) && e.target.type !== 'checkbox';

    // While a modal dialog is open, let it handle the keyboard
    if (document.querySelector('dialog[open]')) return;

    // Global shortcuts: ? help, i inspector, O open archive
    if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === '?') { e.preventDefault(); el.helpDialog.showModal(); return; }
      if (e.key === 'i') { inspector.toggle(); return; }
      if (e.key === 'O') { e.preventDefault(); fileBrowser.open(); return; }
      if (e.key === 'Escape' && inspector.isOpen) { inspector.toggle(false); }
    }

    // "g m" / "g a" switch views
    if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === 'g') { lastGKey = Date.now(); return; }
      if (Date.now() - lastGKey < 1000 && (e.key === 'm' || e.key === 'a')) {
        lastGKey = 0;
        switchView(e.key === 'a' ? 'attachments' : 'mail');
        return;
      }
    }

    if (state.view === 'attachments' && explorer && explorer.handleKey(e)) return;
    if (state.view === 'attachments' && e.key !== 'Escape') return;  // mail shortcuts don't apply here
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') {
      if (e.key === 'Escape') {
        if (el.searchInput) el.searchInput.blur();
        if (state.searchQuery) {
          if (el.searchInput) el.searchInput.value = '';
          if (el.searchClear) el.searchClear.style.display = 'none';
          state.searchQuery = '';
          loadMessages();
        }
      }
      return;
    }

    if (e.key === '/' || (e.ctrlKey && e.key.toLowerCase() === 'k')) {
      e.preventDefault();
      if (el.searchInput) {
        el.searchInput.focus();
        el.searchInput.select();
      }
    } else if (e.key === 'Escape') {
      viewer.closeModal();
      if (el.analyticsModal) el.analyticsModal.classList.remove('active');
      _closeArchiveDropdown();
    } else if (e.key === 'j' || e.key === 'ArrowDown') {
      e.preventDefault();
      _navigateMessage(1);
    } else if (e.key === 'k' || e.key === 'ArrowUp') {
      e.preventDefault();
      _navigateMessage(-1);
    } else if (e.key === 'n' || e.key === 'p') {
      viewer.threadStep(e.key === 'n' ? 1 : -1);
    } else if (e.key === 't') {
      viewer.toggleThread();
    } else if (e.key === 'H') {
      viewer.toggleCompact();
    } else if (e.key === 'h') {
      viewer.toggleBodyMode();
    } else if (e.key === 'e') {
      if (state.selectedMessageId) {
        window.location.href = `/api/messages/${state.selectedMessageId}/eml`;
      }
    } else if (e.key === '[') {
      if (state.currentPage > 1) { state.currentPage--; loadMessages(); }
    } else if (e.key === ']') {
      if (state.currentPage < state.totalPages) { state.currentPage++; loadMessages(); }
    }
  }

  function _navigateMessage(direction) {
    if (!state.currentMessages || state.currentMessages.length === 0) return;
    const currentIdx = state.currentMessages.findIndex(m => m.id === state.selectedMessageId);
    let nextIdx = currentIdx + direction;
    if (nextIdx < 0) nextIdx = 0;
    if (nextIdx >= state.currentMessages.length) nextIdx = state.currentMessages.length - 1;

    const targetMsg = state.currentMessages[nextIdx];
    if (targetMsg && targetMsg.id !== state.selectedMessageId) {
      selectMessage(targetMsg.id);
      if (el.messageList) {
        const card = el.messageList.querySelector(`.message-card[data-msg-id="${targetMsg.id}"]`);
        if (card) card.scrollIntoView({ block: 'nearest' });
      }
    }
  }

  function _setThemeIcon(theme) {
    if (el.themeToggle) el.themeToggle.innerHTML = UI.icon(theme === 'dark' ? 'sun' : 'moon');
  }

  function _initTheme() {
    // Default follows the operating system; an explicit toggle is remembered.
    const saved = UI.store('email-archive-theme');
    const system = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    const theme = saved || system;
    document.documentElement.setAttribute('data-theme', theme);
    _setThemeIcon(theme);
    if (!saved && window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (ev) => {
        if (UI.store('email-archive-theme')) return;
        const t = ev.matches ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', t);
        _setThemeIcon(t);
      });
    }
  }

  function _toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    UI.store('email-archive-theme', next);
    _setThemeIcon(next);
  }

  // Resizable panes (Spec 08 §4.2): drag or arrow-key the splitters; widths persist.
  function _setupSplitters() {
    const panes = {
      sidebar: { el: document.querySelector('.sidebar-pane'), min: 180, max: 520 },
      list: { el: document.querySelector('.list-pane'), min: 260, max: 760 },
    };
    Object.entries(panes).forEach(([key, p]) => {
      const saved = parseInt(UI.store(`eae-pane-${key}`) || '', 10);
      if (p.el && saved) p.el.style.width = `${Math.max(p.min, Math.min(p.max, saved))}px`;
    });
    document.querySelectorAll('.splitter').forEach(sp => {
      const p = panes[sp.dataset.split];
      if (!p || !p.el) return;
      const syncAria = () => {
        sp.setAttribute('aria-valuemin', String(p.min));
        sp.setAttribute('aria-valuemax', String(p.max));
        sp.setAttribute('aria-valuenow', String(Math.round(p.el.getBoundingClientRect().width)));
      };
      syncAria();
      const setWidth = (w) => {
        const width = Math.max(p.min, Math.min(p.max, Math.round(w)));
        p.el.style.width = `${width}px`;
        p.el.style.maxWidth = 'none';
        p.el.style.minWidth = `${p.min}px`;
        UI.store(`eae-pane-${sp.dataset.split}`, String(width));
        syncAria();
      };
      sp.addEventListener('pointerdown', (ev) => {
        ev.preventDefault();
        sp.setPointerCapture(ev.pointerId);
        sp.classList.add('dragging');
        const startX = ev.clientX;
        const startW = p.el.getBoundingClientRect().width;
        const move = (m) => setWidth(startW + (m.clientX - startX));
        const up = () => {
          sp.classList.remove('dragging');
          sp.removeEventListener('pointermove', move);
          sp.removeEventListener('pointerup', up);
        };
        sp.addEventListener('pointermove', move);
        sp.addEventListener('pointerup', up);
      });
      sp.addEventListener('keydown', (ev) => {
        if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
        ev.preventDefault();
        setWidth(p.el.getBoundingClientRect().width + (ev.key === 'ArrowRight' ? 24 : -24));
      });
      sp.addEventListener('dblclick', () => {
        p.el.style.width = '';
        p.el.style.maxWidth = '';
        UI.store(`eae-pane-${sp.dataset.split}`, '');
      });
    });
  }

  function _formatCompactDate(isoStr) {
    const d = new Date(isoStr);
    return d.toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric'
    });
  }

  function _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text || '';
    return div.innerHTML;
  }

  document.addEventListener('DOMContentLoaded', init);
})();
