(function () {
  const root = document.getElementById('bb-alarms-page');
  if (!root) return;
  const eventsBody = document.getElementById('bb-emergency-rows');
  const rulesBody = document.getElementById('bb-rule-rows');
  const form = document.getElementById('bb-rule-form');
  const error = document.getElementById('bb-rule-error');
  const isAdmin = root.dataset.isAdmin === 'true';
  const csrf = document.cookie.split('; ').find(function (item) { return item.startsWith('bb_csrf='); });
  const csrfToken = csrf ? decodeURIComponent(csrf.split('=').slice(1).join('=')) : '';
  let rules = [];

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]; });
  }

  function when(value) {
    const time = new Date(value);
    return Number.isNaN(time.getTime()) ? String(value || '') : time.toLocaleString('ru-RU');
  }

  async function loadEvents() {
    const response = await fetch('/api/v1/emergency-events?limit=1000', { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Не удалось загрузить события');
    const data = await response.json();
    const vmsResponse = await fetch('/api/v1/vms', { credentials: 'same-origin' });
    const vmNames = new Map();
    if (vmsResponse.ok) (await vmsResponse.json()).items.forEach(function (vm) { vmNames.set(vm.id, vm.name); });
    eventsBody.innerHTML = (data.items || []).map(function (item) {
      const link = item.incident_id ? '<a class="bb-btn bb-btn-sm bb-btn-secondary" href="/incidents/' + encodeURIComponent(item.incident_id) + '">Инцидент</a>' : '';
      return '<tr><td>' + esc(when(item.started_at)) + '</td><td>' + esc(when(item.ended_at)) + '</td><td>' + esc(vmNames.get(item.vm_id) || item.vm_id) + '</td><td>' + esc(item.rule_name) + '</td><td>' + esc(item.expression) + '</td><td>' + link + '</td></tr>';
    }).join('') || '<tr><td colspan="6" class="bb-muted">Срабатываний пока нет.</td></tr>';
  }

  async function loadRules() {
    if (!isAdmin) return;
    const response = await fetch('/api/v1/emergency-rules', { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Не удалось загрузить правила');
    rules = (await response.json()).items || [];
    rulesBody.innerHTML = rules.map(function (rule) {
      return '<tr><td>' + esc(rule.name) + '</td><td><code>' + esc(rule.expression) + '</code></td><td><div class="bb-actions"><button type="button" class="bb-btn bb-btn-sm bb-btn-secondary" data-edit-rule="' + rule.id + '" aria-label="Редактировать правило"><i class="bi bi-pencil"></i></button><button type="button" class="bb-btn bb-btn-sm bb-btn-danger" data-delete-rule="' + rule.id + '" aria-label="Удалить правило"><i class="bi bi-trash"></i></button></div></td></tr>';
    }).join('') || '<tr><td colspan="3" class="bb-muted">Правила не настроены.</td></tr>';
  }

  async function reload() {
    try {
      await Promise.all([loadEvents(), loadRules()]);
    } catch (reason) {
      eventsBody.innerHTML = '<tr><td colspan="6" class="bb-error-inline">' + esc(reason.message) + '</td></tr>';
    }
  }

  form?.addEventListener('submit', async function (event) {
    event.preventDefault();
    error.textContent = '';
    const id = form.elements.rule_id.value;
    const method = id ? 'PUT' : 'POST';
    const url = id ? '/api/v1/emergency-rules/' + encodeURIComponent(id) : '/api/v1/emergency-rules';
    try {
      const response = await fetch(url, { method: method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify({ name: form.elements.name.value.trim(), expression: form.elements.expression.value.trim() }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail?.message || 'Не удалось сохранить правило');
      form.reset();
      form.elements.rule_id.value = '';
      document.getElementById('bb-rule-cancel').hidden = true;
      await reload();
    } catch (reason) { error.textContent = reason.message; }
  });

  rulesBody?.addEventListener('click', async function (event) {
    const edit = event.target.closest('[data-edit-rule]');
    const remove = event.target.closest('[data-delete-rule]');
    if (edit) {
      const rule = rules.find(function (item) { return String(item.id) === edit.dataset.editRule; });
      if (!rule) return;
      form.elements.rule_id.value = rule.id;
      form.elements.name.value = rule.name;
      form.elements.expression.value = rule.expression;
      document.getElementById('bb-rule-cancel').hidden = false;
      form.elements.name.focus();
    } else if (remove && window.confirm('Удалить это правило? История срабатываний сохранится.')) {
      const response = await fetch('/api/v1/emergency-rules/' + encodeURIComponent(remove.dataset.deleteRule), { method: 'DELETE', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrfToken } });
      if (response.ok) await reload();
      else error.textContent = 'Не удалось удалить правило.';
    }
  });

  document.getElementById('bb-rule-cancel')?.addEventListener('click', function () {
    form.reset(); form.elements.rule_id.value = ''; error.textContent = ''; this.hidden = true;
  });
  document.getElementById('bb-emergency-refresh').addEventListener('click', reload);
  reload();
})();
