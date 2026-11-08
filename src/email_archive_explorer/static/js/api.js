/**
 * API client module for Email Archive Explorer
 */

const API = {
  baseUrl: '',

  async getArchiveInfo() {
    const res = await fetch(`${this.baseUrl}/api/archive/info`);
    if (!res.ok) throw new Error('Failed to load archive information');
    return await res.json();
  },

  async getFolders(includeEmpty = false) {
    const res = await fetch(`${this.baseUrl}/api/folders?include_empty=${includeEmpty}`);
    if (!res.ok) throw new Error('Failed to load folder hierarchy');
    return await res.json();
  },

  async getFolderMessages(folderId, page = 1, pageSize = 50, sortBy = 'date', sortOrder = 'desc', hasAttachments = null) {
    const params = new URLSearchParams({
      page: page.toString(),
      page_size: pageSize.toString(),
      sort_by: sortBy,
      sort_order: sortOrder
    });
    if (hasAttachments !== null) {
      params.append('has_attachments', hasAttachments ? 'true' : 'false');
    }
    const res = await fetch(`${this.baseUrl}/api/folders/${folderId}/messages?${params.toString()}`);
    if (!res.ok) throw new Error('Failed to load messages for folder');
    return await res.json();
  },

  async getMessageDetail(messageId) {
    const res = await fetch(`${this.baseUrl}/api/messages/${messageId}`);
    if (!res.ok) throw new Error('Failed to load message detail');
    return await res.json();
  },

  async getMessageThread(messageId) {
    const res = await fetch(`${this.baseUrl}/api/messages/${messageId}/thread?limit=50`);
    if (!res.ok) return null;
    return await res.json();
  },

  async getArchiveAnalytics() {
    const res = await fetch(`${this.baseUrl}/api/archive/analytics`);
    if (!res.ok) throw new Error('Failed to load archive analytics');
    return await res.json();
  },

  async searchMessages(query, folderId = null, hasAttachments = null, page = 1, pageSize = 50, filters = {}) {
    const params = new URLSearchParams({
      q: query || '',
      page: page.toString(),
      page_size: pageSize.toString()
    });
    if (folderId !== null) params.append('folder_id', folderId.toString());
    if (hasAttachments !== null) params.append('has_attachments', hasAttachments ? 'true' : 'false');
    if (filters.sender) params.append('sender', filters.sender);
    if (filters.attachmentExt) params.append('attachment_ext', filters.attachmentExt);
    if (filters.dateFrom) params.append('date_from', filters.dateFrom);
    if (filters.dateTo) params.append('date_to', filters.dateTo);

    const res = await fetch(`${this.baseUrl}/api/search?${params.toString()}`);
    if (!res.ok) throw new Error('Search failed');
    return await res.json();
  },

  // -----------------------------------------------------------------------
  // Archive management (Spec 06)
  // -----------------------------------------------------------------------

  async getArchives() {
    const res = await fetch(`${this.baseUrl}/api/archives`);
    if (!res.ok) throw new Error('Failed to load archive list');
    return await res.json();
  },

  /** Returns {status, data}: 200 = switched, 202 = indexing job started, 409 = busy. */
  async openArchive(filePath, forceReindex = false) {
    const res = await fetch(`${this.baseUrl}/api/archives/open`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_path: filePath, force_reindex: forceReindex })
    });
    const data = await res.json().catch(() => ({ detail: 'Unknown error' }));
    if (!res.ok && res.status !== 409) throw new Error(data.detail || `HTTP ${res.status}`);
    return { status: res.status, data };
  },

  async scanArchives(directory = 'data/', recursive = true) {
    const res = await fetch(`${this.baseUrl}/api/archives/scan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ directory, recursive })
    });
    if (!res.ok) throw new Error('Archive scan failed');
    return await res.json();
  }
};

window.API = API;
export { API };
