(function () {
  const body = document.getElementById('bb-incident-rows');
  const vm = document.getElementById('bb-incident-vm');
  const refresh = document.getElementById('bb-incident-refresh');
  const pager = document.getElementById('bb-incident-pager');
  if (!body || !vm || !refresh) return;

  let page = 1;

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function when(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value || '') : date.toLocaleString('ru-RU');
  }

  async function load() {
    body.innerHTML = '<tr><td colspan="6" class="bb-muted">Загрузка…</td></tr>';
    const query = new URLSearchParams();
    if (vm.value) query.set('vm_id', vm.value);
    query.set('page', String(page));
    query.set('limit', '50');
    try {
      const response = await fetch('/api/v1/video/incidents?' + query.toString(), { credentials: 'same-origin' });
      if (!response.ok) throw new Error('incidents');
      const payload = await response.json();
      page = payload.page || page;
      const items = payload.items || [];
      if (pager && window.bbPagerHtml) pager.innerHTML = window.bbPagerHtml(payload);
      if (!items.length) {
        body.innerHTML = '<tr><td colspan="6" class="bb-muted">Инцидентов за выбранные условия нет.</td></tr>';
        return;
      }
      body.innerHTML = items.map(function (item) {
        const alerts = (item.alerts || []).map(function (alarm) { return esc(alarm.name); }).filter(Boolean);
        const cameras = Array.from(new Set((item.episodes || []).map(function (episode) { return episode.camera_id; })));
        return '<tr><td>' + esc(when(item.started_at)) + '</td><td>' + esc(item.vm_name || item.vm_id) + '</td><td><span class="bb-badge bb-badge-' + esc(item.state) + '">' + esc(item.state) + '</span></td><td>' + esc(Array.from(new Set(alerts)).join(', ') || '—') + '</td><td>' + esc(cameras.join(', ') || '—') + '</td><td><a class="bb-btn bb-btn-sm bb-btn-secondary" href="/incidents/' + encodeURIComponent(item.id) + '"><i class="bi bi-box-arrow-up-right"></i> Открыть</a></td></tr>';
      }).join('');
    } catch (_error) {
      body.innerHTML = '<tr><td colspan="6" class="bb-error-inline">Не удалось загрузить инциденты.</td></tr>';
    }
  }

  refresh.addEventListener('click', function () { page = 1; load(); });
  vm.addEventListener('change', function () { page = 1; load(); });
  if (pager) {
    pager.addEventListener('click', function (event) {
      const button = event.target.closest('[data-page]');
      if (!button || button.disabled) return;
      page += button.dataset.page === 'next' ? 1 : -1;
      if (page < 1) page = 1;
      load();
    });
  }
  load();
})();
