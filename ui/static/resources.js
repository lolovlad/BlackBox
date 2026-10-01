(function () {
  const PAGE_SIZE = 50;

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function statusHtml(item, storage) {
    if (!item.available) return '<span class="bb-badge bb-badge-stopped">нет в системе</span>';
    if (!storage && item.metadata && item.metadata.reachable === false) return '<span class="bb-badge bb-badge-pending">нет ответа</span>';
    const approved = item.approved
      ? '<span class="bb-badge bb-badge-running">подтверждён</span>'
      : '<span class="bb-badge bb-badge-pending">ожидает</span>';
    if (storage) return approved;
    const oper = item.metadata && item.metadata.operstate;
    const extra = oper === 'up'
      ? ' <span class="bb-badge bb-badge-running">up</span>'
      : (oper ? ' <span class="bb-badge bb-badge-stopped">' + esc(oper) + '</span>' : '');
    return approved + extra;
  }

  function approveCell(item) {
    if (item.available && !item.approved) {
      return '<button type="button" class="bb-btn bb-btn-sm" data-resource-approve="' + esc(item.resource_id) + '">Подтвердить</button>';
    }
    return '';
  }

  function readRow(item) {
    const detail = item.metadata && item.metadata.detail
      ? '<div class="bb-resource-detail">' + esc(item.metadata.detail) + '</div>'
      : '';
    return '<tr class="' + (item.available ? '' : 'is-gone') + '"><td><div class="bb-resource-name">' + esc(item.name) + '</div>' + detail +
      '</td><td class="bb-mono">' + esc(item.path || item.address || '') + '</td><td>' + statusHtml(item, false) +
      '</td><td>' + approveCell(item) + '</td></tr>';
  }

  function storageRow(item) {
    const free = (item.metadata && (item.metadata.free_human || item.metadata.detail)) || '—';
    return '<tr class="' + (item.available ? '' : 'is-gone') + '"><td>' + esc(item.name) + '</td><td class="bb-mono">' +
      esc(item.path || '') + '</td><td>' + esc(free) + '</td><td>' + statusHtml(item, true) + '</td><td>' + approveCell(item) + '</td></tr>';
  }

  async function loadKind(kind, page) {
    const body = document.querySelector('[data-resource-body="' + kind + '"]');
    const pager = document.querySelector('[data-resource-pager="' + kind + '"]');
    if (!body) return;
    const query = new URLSearchParams();
    query.set('kind', kind);
    query.set('page', String(page || 1));
    query.set('page_size', String(PAGE_SIZE));
    const response = await fetch('/api/v1/resources?' + query.toString(), { credentials: 'same-origin' });
    if (!response.ok) throw new Error('resources');
    const payload = await response.json();
    const items = payload.items || [];
    if (!items.length) {
      body.innerHTML = kind === 'storage'
        ? '<tr><td colspan="5" class="bb-muted">Нет носителей.</td></tr>'
        : '<tr><td colspan="4" class="bb-muted">Нет устройств.</td></tr>';
    } else {
      body.innerHTML = items.map(kind === 'storage' ? storageRow : readRow).join('');
    }
    if (pager && window.bbPagerHtml) {
      pager.innerHTML = window.bbPagerHtml(payload);
      pager.hidden = (payload.total_pages || 1) <= 1;
      pager.dataset.page = String(payload.page || 1);
      pager.dataset.pages = String(payload.total_pages || 1);
    }
  }

  document.querySelectorAll('[data-resource-pager]').forEach(function (pager) {
    if (window.bbPagerHtml) {
      pager.innerHTML = window.bbPagerHtml({
        page: Number(pager.dataset.page || 1),
        total_pages: Number(pager.dataset.pages || 1),
        total_rows: Number(pager.dataset.total || 0),
        page_size: PAGE_SIZE,
      });
    }
    pager.addEventListener('click', function (event) {
      const button = event.target.closest('[data-page]');
      if (!button || button.disabled) return;
      const current = Number(pager.dataset.page || 1);
      const next = current + (button.dataset.page === 'next' ? 1 : -1);
      loadKind(pager.getAttribute('data-resource-pager'), Math.max(1, next)).catch(function () {});
    });
  });
})();
