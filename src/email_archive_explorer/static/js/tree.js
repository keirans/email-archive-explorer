/**
 * Folder Tree component: an ARIA tree with roving tabindex.
 *
 * Keyboard (when the tree has focus): ↑/↓ move, → expand / go to first child,
 * ← collapse / go to parent, Home/End, Enter or Space opens the folder.
 */
import { UI } from './ui.js';

export class FolderTree {
  constructor(containerElement, onSelectFolder) {
    this.container = containerElement;
    this.onSelectFolder = onSelectFolder;
    this.selectedFolderId = null;
    this.collapsedMap = new Set(); // folder IDs that are collapsed
    this.container.addEventListener('keydown', (e) => this._onKey(e));
  }

  render(treeData) {
    this.container.innerHTML = '';
    const rootUl = document.createElement('ul');
    rootUl.className = 'tree-list';
    rootUl.setAttribute('role', 'tree');
    rootUl.setAttribute('aria-label', 'Folders');
    treeData.forEach(node => rootUl.appendChild(this._createNodeElement(node, 1)));
    this.container.appendChild(rootUl);
    this._ensureTabStop();
  }

  _createNodeElement(node, level) {
    const li = document.createElement('li');
    li.className = 'tree-item';
    li.dataset.folderId = node.id;
    li.dataset.name = node.name || 'Personal Folders';
    li.setAttribute('role', 'treeitem');
    li.setAttribute('aria-level', String(level));
    li.setAttribute('aria-selected', String(this.selectedFolderId === node.id));
    li.tabIndex = -1;

    const hasChildren = node.children && node.children.length > 0;
    const isCollapsed = this.collapsedMap.has(node.id);
    if (hasChildren) li.setAttribute('aria-expanded', String(!isCollapsed));

    const row = document.createElement('div');
    row.className = `tree-node ${node.message_count === 0 ? 'empty-folder' : ''}`;
    if (this.selectedFolderId === node.id) row.classList.add('selected');

    const toggle = document.createElement('span');
    toggle.className = `tree-toggle ${hasChildren ? '' : 'empty'} ${isCollapsed ? 'collapsed' : ''}`;
    toggle.setAttribute('aria-hidden', 'true');
    toggle.innerHTML = hasChildren ? '▼' : '';
    toggle.onclick = (e) => {
      e.stopPropagation();
      if (hasChildren) this._setExpanded(li, this.collapsedMap.has(node.id));
    };
    row.appendChild(toggle);

    const icon = document.createElement('span');
    icon.className = 'tree-icon';
    icon.innerHTML = UI.icon('folder');
    row.appendChild(icon);

    const label = document.createElement('span');
    label.className = 'tree-label';
    label.textContent = li.dataset.name;
    label.title = `${node.full_path} (${node.message_count} messages)`;
    row.appendChild(label);

    if (node.message_count > 0) {
      const badge = document.createElement('span');
      badge.className = 'tree-badge';
      badge.textContent = node.message_count;
      badge.setAttribute('aria-label', `${node.message_count} messages`);
      row.appendChild(badge);
    }

    row.onclick = () => {
      this._focus(li);
      this.selectFolder(node.id, node.name);
    };
    li.appendChild(row);

    if (hasChildren) {
      const subUl = document.createElement('ul');
      subUl.className = `tree-sublist ${isCollapsed ? 'hidden' : ''}`;
      subUl.setAttribute('role', 'group');
      node.children.forEach(child => subUl.appendChild(this._createNodeElement(child, level + 1)));
      li.appendChild(subUl);
    }
    return li;
  }

  _setExpanded(li, expand) {
    const id = +li.dataset.folderId;
    const sub = li.querySelector(':scope > .tree-sublist');
    if (!sub) return;
    if (expand) this.collapsedMap.delete(id); else this.collapsedMap.add(id);
    sub.classList.toggle('hidden', !expand);
    li.querySelector(':scope > .tree-node .tree-toggle').classList.toggle('collapsed', !expand);
    li.setAttribute('aria-expanded', String(expand));
  }

  _visibleItems() {
    return [...this.container.querySelectorAll('[role=treeitem]')].filter(li => li.offsetParent !== null);
  }

  _focus(li) {
    this.container.querySelectorAll('[role=treeitem][tabindex="0"]').forEach(x => { x.tabIndex = -1; });
    li.tabIndex = 0;
    li.focus({ preventScroll: true });
    li.querySelector(':scope > .tree-node').scrollIntoView({ block: 'nearest' });
  }

  _ensureTabStop() {
    const target = this.container.querySelector('[role=treeitem][aria-selected=true]')
      || this.container.querySelector('[role=treeitem]');
    if (target) target.tabIndex = 0;
  }

  _onKey(e) {
    const li = e.target.closest && e.target.closest('[role=treeitem]');
    if (!li || e.ctrlKey || e.metaKey || e.altKey) return;
    const items = this._visibleItems();
    const i = items.indexOf(li);
    const expanded = li.getAttribute('aria-expanded');
    let handled = true;
    switch (e.key) {
      case 'ArrowDown': if (items[i + 1]) this._focus(items[i + 1]); break;
      case 'ArrowUp': if (items[i - 1]) this._focus(items[i - 1]); break;
      case 'Home': this._focus(items[0]); break;
      case 'End': this._focus(items[items.length - 1]); break;
      case 'ArrowRight':
        if (expanded === 'false') this._setExpanded(li, true);
        else if (expanded === 'true') { const c = li.querySelector(':scope > .tree-sublist > [role=treeitem]'); if (c) this._focus(c); }
        break;
      case 'ArrowLeft':
        if (expanded === 'true') this._setExpanded(li, false);
        else { const p = li.parentElement.closest('[role=treeitem]'); if (p) this._focus(p); }
        break;
      case 'Enter': case ' ':
        this.selectFolder(+li.dataset.folderId, li.dataset.name);
        break;
      default: handled = false;
    }
    if (handled) { e.preventDefault(); e.stopPropagation(); }
  }

  selectFolder(folderId, folderName) {
    this.selectedFolderId = folderId;
    this.container.querySelectorAll('.tree-node').forEach(el => el.classList.remove('selected'));
    this.container.querySelectorAll('[role=treeitem]').forEach(el => el.setAttribute('aria-selected', 'false'));

    const activeLi = this.container.querySelector(`li[data-folder-id="${folderId}"]`);
    if (activeLi) {
      activeLi.querySelector(':scope > .tree-node')?.classList.add('selected');
      activeLi.setAttribute('aria-selected', 'true');
      // Expand ancestors so the selection is visible
      let parent = activeLi.parentElement.closest('[role=treeitem]');
      while (parent) {
        this._setExpanded(parent, true);
        parent = parent.parentElement.closest('[role=treeitem]');
      }
      this.container.querySelectorAll('[role=treeitem][tabindex="0"]').forEach(x => { x.tabIndex = -1; });
      activeLi.tabIndex = 0;
    }

    if (this.onSelectFolder) this.onSelectFolder(folderId, folderName);
  }
}

window.FolderTree = FolderTree;
