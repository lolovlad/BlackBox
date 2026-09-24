(function () {
  const root = document.getElementById('bb-data');
  const form = document.getElementById('bb-data-form');
  const tableHost = document.getElementById('bb-data-table');
  const fieldHost = document.getElementById('bb-data-fields');
  if (!root || !form || !tableHost || !fieldHost) return;

  let page = 1;
  let catalog = [];
  let refreshTimer = null;

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function selected(name) {
    return Array.from(form.querySelectorAll('input[name="' + name + '"]:checked')).map(function (node) { return node.value; });
  }

  function tab() {
    const node = form.querySelector('input[name="active_tab"]:checked');
    return node ? node.value : 'analog';
  }

  function params(extra) {
    const query = new URLSearchParams();
    selected('vm_id').forEach(function (id) { query.append('vm_id', id); });
    query.set('tab', tab());
    query.set('sort', form.querySelector('[name="sort"]').value);
    query.set('page', String(extra && extra.page ? extra.page : page));
    const from = form.querySelector('[name="date_from"]').value;
    const to = form.querySelector('[name="date_to"]').value;
    if (from) query.set('date_from', from);
    if (to) query.set('date_to', to);
    if (tab() === 'analog' || tab() === 'discrete') {
      selected(tab() === 'discrete' ? 'discrete_col' : 'analog_col').forEach(function (key) { query.append('column', key); });
    }
    return query;
  }

  function liveMode() {
    return !form.querySelector('[name="date_from"]').value && !form.querySelector('[name="date_to"]').value && page === 1 && form.querySelector('[name="sort"]').value === 'desc';
  }

  function showHints() {
    root.querySelectorAll('[data-tab-hint]').forEach(function (node) {
      node.hidden = node.dataset.tabHint !== tab();
    });
    fieldHost.hidden = tab() === 'alarms' || tab() === 'gpio';
  }

  function renderFields() {
    const active = tab();
    if (active !== 'analog' && active !== 'discrete') {
      fieldHost.innerHTML = '';
      showHints();
      return;
    }
    const seen = new Map();
    catalog.forEach(function (source) {
      (source[active] || []).forEach(function (field) {
        if (!seen.has(field.key)) seen.set(field.key, field.label || field.key);
      });
    });
    const inputName = active === 'discrete' ? 'discrete_col' : 'analog_col';
    const previous = new Set(selected(inputName));
    const checkAll = !fieldHost.querySelector('input[name="' + inputName + '"]');
    fieldHost.innerHTML = '<fieldset><legend>' + (active === 'analog' ? 'Поля (аналоги)' : 'Поля (дискреты)') + '</legend><div class="bb-check-grid">' +
      Array.from(seen.entries()).map(function (pair) {
        const checked = checkAll || previous.has(pair[0]) ? ' checked' : '';
        return '<label class="bb-chk"><input type="checkbox" name="' + inputName + '" value="' + esc(pair[0]) + '"' + checked + '> ' + esc(pair[1]) + '</label>';
      }).join('') + '</div></fieldset>';
    showHints();
  }

  function renderTable(payload) {
    const rows = payload.rows || [];
    const journal = payload.tab === 'alarms' || payload.tab === 'gpio';
    const head = journal
      ? '<th>Время</th><th>Источник</th><th>Название</th><th>Состояние</th>'
      : '<th>Время</th><th>Источник</th>' + (payload.columns || []).map(function (column) { return '<th>' + esc(column.label) + '</th>'; }).join('');
    const body = rows.map(function (row) {
      if (journal) {
        return '<tr><td>' + esc(row.time) + '</td><td>' + esc(row.vm_name) + '</td><td>' + esc(row.name) + '</td><td>' + esc(row.state_label) + '</td></tr>';
      }
      return '<tr><td>' + esc(row.time) + '</td><td>' + esc(row.vm_name) + '</td>' + (row.cells || []).map(function (cell) { return '<td>' + esc(cell) + '</td>'; }).join('') + '</tr>';
    }).join('');
    const title = { analog: 'Аналоги', discrete: 'Дискреты', alarms: 'Аварии', gpio: 'GPIO' }[payload.tab] || 'Данные';
    tableHost.innerHTML = '<p class="bb-hint">Всего записей: ' + esc(payload.total_rows) + ' · страница ' + esc(payload.page) + ' из ' + esc(payload.total_pages) + ' (по ' + esc(payload.page_size) + ')' + (payload.truncated ? ' · показана неполная выборка' : '') + '</p>' +
      '<div class="bb-table-wrap"><table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>' +
      (rows.length ? '' : '<p class="bb-hint">Нет записей за выбранные условия.</p>') +
      '<div class="bb-row-actions"><button type="button" class="bb-btn bb-btn-ghost" data-page="prev"' + (payload.page <= 1 ? ' disabled' : '') + '>Назад</button><button type="button" class="bb-btn bb-btn-ghost" data-page="next"' + (payload.page >= payload.total_pages ? ' disabled' : '') + '>Вперёд</button><span class="bb-hint">' + esc(title) + '</span></div>';
  }

  async function loadCatalog() {
    const query = new URLSearchParams();
    selected('vm_id').forEach(function (id) { query.append('vm_id', id); });
    const response = await fetch('/api/v1/telemetry/catalog?' + query.toString());
    if (!response.ok) throw new Error('catalog');
    const payload = await response.json();
    catalog = payload.sources || [];
    renderFields();
  }

  async function loadTable() {
    if (!selected('vm_id').length) {
      tableHost.innerHTML = '<p class="bb-hint">Выберите хотя бы один источник.</p>';
      return;
    }
    tableHost.setAttribute('aria-busy', 'true');
    const response = await fetch('/api/v1/telemetry/rows?' + params().toString());
    if (!response.ok) {
      tableHost.innerHTML = '<p class="bb-hint">Не удалось прочитать измерения.</p>';
      return;
    }
    renderTable(await response.json());
    tableHost.removeAttribute('aria-busy');
  }

  function schedule() {
    if (!liveMode()) return;
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () { loadTable(); }, 400);
  }

  form.addEventListener('change', function (event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.name === 'active_tab') {
      page = 1;
      renderFields();
      loadTable();
      return;
    }
    if (target.name === 'vm_id') {
      page = 1;
      loadCatalog().then(loadTable).catch(function () {
        tableHost.innerHTML = '<p class="bb-hint">Не удалось загрузить список полей.</p>';
      });
      return;
    }
    page = 1;
    loadTable();
  });

  document.getElementById('bb-data-refresh').addEventListener('click', function () { loadTable(); });
  document.getElementById('bb-data-export').addEventListener('click', function () {
    const query = params();
    query.delete('page');
    window.location.href = root.dataset.exportUrl + '?' + query.toString();
  });
  tableHost.addEventListener('click', function (event) {
    const button = event.target.closest('[data-page]');
    if (!button || button.disabled) return;
    page += button.dataset.page === 'next' ? 1 : -1;
    if (page < 1) page = 1;
    loadTable();
  });

  window.addEventListener('bb-hub-event', function (event) {
    const message = event.detail || {};
    if (message.type !== 'delta') return;
    if (message.topic === 'tags' && (tab() === 'analog' || tab() === 'discrete')) schedule();
    if (message.topic === 'alarms') {
      const kind = message.payload && message.payload.payload ? message.payload.payload.kind : '';
      if ((tab() === 'alarms' && kind !== 'gpio') || (tab() === 'gpio' && kind === 'gpio')) schedule();
    }
  });

  loadCatalog().then(loadTable).catch(function () {
    tableHost.innerHTML = '<p class="bb-hint">Не удалось загрузить таблицу.</p>';
  });
})();
