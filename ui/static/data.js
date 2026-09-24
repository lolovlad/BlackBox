(function () {
  const root = document.getElementById('bb-data');
  const form = document.getElementById('bb-data-form');
  const tableHost = document.getElementById('bb-data-table');
  const fieldHost = document.getElementById('bb-data-fields');
  if (!root || !form || !tableHost || !fieldHost) return;

  let page = 1;
  let catalog = [];
  let refreshTimer = null;
  let openPicker = false;
  let activeFetch = null;
  let loadToken = 0;
  const columnPick = JSON.parse(localStorage.getItem('bb-data-columns') || '{}');

  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(String(value));
    return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  }

  function setRadio(name, value) {
    const nodes = form.querySelectorAll('input[name="' + name + '"]');
    let matched = false;
    nodes.forEach(function (node) {
      const on = node.value === value;
      node.checked = on;
      if (on) matched = true;
    });
    if (!matched && nodes[0]) nodes[0].checked = true;
  }

  const savedVm = localStorage.getItem('bb-data-vm');
  if (savedVm) setRadio('vm_id', savedVm);
  const savedTab = localStorage.getItem('bb-data-tab');
  if (savedTab === 'analog' || savedTab === 'discrete' || savedTab === 'alarms') {
    setRadio('active_tab', savedTab);
  }

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function vmId() {
    const node = form.querySelector('input[name="vm_id"]:checked');
    return node ? node.value : '';
  }

  function tab() {
    const node = form.querySelector('input[name="active_tab"]:checked');
    return node ? node.value : 'analog';
  }

  function pickKey() {
    return vmId() + ':' + tab();
  }

  function currentPick() {
    const pick = columnPick[pickKey()];
    if (!pick || pick.mode !== 'set' || !Array.isArray(pick.keys) || !pick.keys.length) return { mode: 'all', keys: [] };
    return pick;
  }

  function countLabel(count) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return count + ' столбец';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return count + ' столбца';
    return count + ' столбцов';
  }

  function params(stamp) {
    const query = new URLSearchParams();
    const id = vmId();
    if (id) query.append('vm_id', id);
    query.set('tab', tab());
    query.set('sort', form.querySelector('[name="sort"]').value);
    query.set('page', String(page));
    const from = form.querySelector('[name="date_from"]').value;
    const to = form.querySelector('[name="date_to"]').value;
    if (from) query.set('date_from', from);
    if (to) query.set('date_to', to);
    if (tab() === 'analog' || tab() === 'discrete') {
      currentPick().keys.forEach(function (key) { query.append('column', key); });
    }
    query.set('_', stamp || String(Date.now()));
    return query;
  }

  function liveMode() {
    return !form.querySelector('[name="date_from"]').value && !form.querySelector('[name="date_to"]').value && page === 1 && form.querySelector('[name="sort"]').value === 'desc';
  }

  function place() {
    return { x: window.scrollX, y: window.scrollY };
  }

  function restore(saved) {
    if (!saved) return;
    const apply = function () { window.scrollTo(saved.x, saved.y); };
    apply();
    requestAnimationFrame(apply);
  }

  function showHints() {
    root.querySelectorAll('[data-tab-hint]').forEach(function (node) {
      node.hidden = node.dataset.tabHint !== tab();
    });
  }

  function fields() {
    const active = tab();
    const id = vmId();
    const source = catalog.find(function (item) { return item.id === id; }) || catalog[0];
    const seen = new Map();
    ((source && source[active]) || []).forEach(function (field) {
      if (field && field.key && !seen.has(field.key)) seen.set(field.key, field.label || field.key);
    });
    return Array.from(seen.entries());
  }

  function pickerLabel() {
    const pick = currentPick();
    if (pick.mode === 'all') return 'Все';
    const labels = fields().filter(function (pair) { return pick.keys.indexOf(pair[0]) >= 0; }).map(function (pair) { return pair[1]; });
    if (labels.length === 1) return labels[0];
    if (labels.length) return countLabel(labels.length);
    return 'Все';
  }

  function renderFields() {
    showHints();
    const active = tab();
    if (active !== 'analog' && active !== 'discrete') {
      fieldHost.innerHTML = '';
      openPicker = false;
      return;
    }
    const pick = currentPick();
    const rows = fields();
    fieldHost.innerHTML = '<div class="bb-picker"><button type="button" class="bb-picker-btn" data-picker-btn aria-expanded="' + (openPicker ? 'true' : 'false') + '"><span>' + esc(pickerLabel()) + '</span><i class="bi bi-chevron-down"></i></button><div class="bb-picker-menu"' + (openPicker ? '' : ' hidden') + '><p class="bb-picker-hint">По умолчанию все столбцы этой машины. Отмеченные заменяют набор.</p><button type="button" class="bb-picker-item' + (pick.mode === 'all' ? ' is-on' : '') + '" data-pick="all"><span class="bb-picker-mark">' + (pick.mode === 'all' ? '✓' : '') + '</span><span>Все</span></button>' +
      rows.map(function (pair) {
        const on = pick.mode === 'set' && pick.keys.indexOf(pair[0]) >= 0;
        return '<button type="button" class="bb-picker-item' + (on ? ' is-on' : '') + '" data-pick="' + esc(pair[0]) + '"><span class="bb-picker-mark">' + (on ? '✓' : '') + '</span><span>' + esc(pair[1]) + '</span></button>';
      }).join('') + '</div></div>';
  }

  function renderTable(payload) {
    const rows = payload.rows || [];
    const journal = payload.tab === 'alarms';
    const head = journal
      ? '<th>Время</th><th>Название</th><th>Состояние</th>'
      : '<th>Время</th>' + (payload.columns || []).map(function (column) { return '<th>' + esc(column.label) + '</th>'; }).join('');
    const body = rows.map(function (row) {
      if (journal) return '<tr><td>' + esc(row.time) + '</td><td>' + esc(row.name) + '</td><td>' + esc(row.state_label) + '</td></tr>';
      return '<tr><td>' + esc(row.time) + '</td>' + (row.cells || []).map(function (cell) { return '<td>' + esc(cell) + '</td>'; }).join('') + '</tr>';
    }).join('');
    const title = { analog: 'Аналоги', discrete: 'Дискреты', alarms: 'Аварии' }[payload.tab] || 'Данные';
    if (payload.page) page = payload.page;
    tableHost.innerHTML = '<p class="bb-hint">Всего записей: ' + esc(payload.total_rows) + ' · страница ' + esc(payload.page) + ' из ' + esc(payload.total_pages) + ' (по ' + esc(payload.page_size) + ')' + (payload.truncated ? ' · показана неполная выборка' : '') + '</p>' +
      '<div class="bb-table-wrap"><table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>' +
      (rows.length ? '' : '<p class="bb-hint">Нет записей за выбранные условия.</p>') +
      '<div class="bb-row-actions"><button type="button" class="bb-btn bb-btn-ghost" data-page="prev"' + (payload.page <= 1 ? ' disabled' : '') + '>Назад</button><button type="button" class="bb-btn bb-btn-ghost" data-page="next"' + (payload.page >= payload.total_pages ? ' disabled' : '') + '>Вперёд</button><span class="bb-hint">' + esc(title) + '</span></div>';
  }

  async function reloadView(options) {
    const keepPlace = options && options.keepPlace;
    const saved = keepPlace ? place() : null;
    if (refreshTimer) {
      clearTimeout(refreshTimer);
      refreshTimer = null;
    }
    if (activeFetch) activeFetch.abort();
    const ctl = new AbortController();
    activeFetch = ctl;
    const token = ++loadToken;
    const id = vmId();
    if (!id) {
      catalog = [];
      renderFields();
      tableHost.innerHTML = '<p class="bb-hint">Выберите машину.</p>';
      return;
    }
    if (!keepPlace) tableHost.innerHTML = '<p class="bb-hint">Загрузка таблицы…</p>';
    tableHost.setAttribute('aria-busy', 'true');
    const stamp = String(Date.now());
    const headers = { 'Cache-Control': 'no-store', Pragma: 'no-cache' };
    const fetchOpts = { cache: 'no-store', headers: headers, signal: ctl.signal };
    try {
      const catalogQuery = new URLSearchParams();
      catalogQuery.append('vm_id', id);
      catalogQuery.set('_', stamp);
      const catalogRes = await fetch('/api/v1/telemetry/catalog?' + catalogQuery.toString(), fetchOpts);
      if (token !== loadToken) return;
      if (!catalogRes.ok) throw new Error('catalog');
      catalog = (await catalogRes.json()).sources || [];
      if (token !== loadToken) return;
      renderFields();
      const tableRes = await fetch('/api/v1/telemetry/rows?' + params(stamp).toString(), fetchOpts);
      if (token !== loadToken) return;
      if (!tableRes.ok) throw new Error('rows');
      renderTable(await tableRes.json());
      tableHost.removeAttribute('aria-busy');
      restore(saved);
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      if (token !== loadToken) return;
      tableHost.innerHTML = '<p class="bb-hint">Не удалось загрузить таблицу.</p>';
    } finally {
      if (activeFetch === ctl) activeFetch = null;
    }
  }

  function schedule() {
    if (!liveMode()) return;
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () { reloadView({ keepPlace: true }); }, 400);
  }

  function chooseColumn(key) {
    const id = pickKey();
    const current = currentPick();
    if (key === 'all') delete columnPick[id];
    else if (current.mode === 'all') columnPick[id] = { mode: 'set', keys: [key] };
    else if (current.keys.indexOf(key) >= 0) {
      const keys = current.keys.filter(function (item) { return item !== key; });
      if (keys.length) columnPick[id] = { mode: 'set', keys: keys };
      else delete columnPick[id];
    } else columnPick[id] = { mode: 'set', keys: current.keys.concat([key]) };
    localStorage.setItem('bb-data-columns', JSON.stringify(columnPick));
    openPicker = true;
    page = 1;
    renderFields();
    reloadView();
  }

  let vmClickAt = 0;

  function applyVm(id) {
    if (!id) return;
    setRadio('vm_id', id);
    localStorage.setItem('bb-data-vm', id);
    openPicker = false;
    page = 1;
    reloadView();
  }

  form.addEventListener('click', function (event) {
    const label = event.target instanceof Element ? event.target.closest('.bb-vm-tabs .bb-tab') : null;
    if (!label || !form.contains(label)) return;
    const input = label.querySelector('input[name="vm_id"]');
    if (!input) return;
    vmClickAt = Date.now();
    applyVm(input.value);
  });

  form.addEventListener('change', function (event) {
    const target = event.target;
    if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement)) return;
    if (target.name === 'vm_id') {
      if (Date.now() - vmClickAt < 400) return;
      applyVm(target.value);
      return;
    }
    if (target.name === 'active_tab') {
      localStorage.setItem('bb-data-tab', target.value);
      openPicker = false;
      page = 1;
      renderFields();
      reloadView();
      return;
    }
    if (target.name === 'date_from' || target.name === 'date_to' || target.name === 'sort') {
      page = 1;
      reloadView();
    }
  });

  root.addEventListener('click', function (event) {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const pick = target.closest('[data-pick]');
    if (pick) {
      chooseColumn(pick.getAttribute('data-pick'));
      return;
    }
    const pickerBtn = target.closest('[data-picker-btn]');
    if (pickerBtn) {
      openPicker = !openPicker;
      renderFields();
    }
  });

  document.addEventListener('click', function (event) {
    const target = event.target instanceof Element ? event.target : null;
    if (!openPicker || (target && target.closest('#bb-data-fields'))) return;
    openPicker = false;
    renderFields();
  });

  document.getElementById('bb-data-refresh').addEventListener('click', function () { reloadView(); });
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
    reloadView();
  });

  window.addEventListener('bb-hub-event', function (event) {
    const message = event.detail || {};
    if (message.type !== 'delta') return;
    const payload = message.payload || {};
    if (payload.vm_id && payload.vm_id !== vmId()) return;
    if (message.topic === 'tags' && (tab() === 'analog' || tab() === 'discrete')) schedule();
    if (message.topic === 'alarms') {
      const kind = payload.payload && payload.payload.kind;
      if (tab() === 'alarms' && kind !== 'gpio') schedule();
    }
  });

  reloadView();
})();
