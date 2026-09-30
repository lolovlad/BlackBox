(function () {
  const root = document.getElementById('bb-alarms-page');
  if (!root) return;
  const eventsBody = document.getElementById('bb-emergency-rows');
  const rulesBody = document.getElementById('bb-rule-rows');
  const form = document.getElementById('bb-rule-form');
  const groupsBox = document.getElementById('bb-rule-groups');
  const preview = document.getElementById('bb-rule-preview');
  const error = document.getElementById('bb-rule-error');
  const isAdmin = root.dataset.isAdmin === 'true';
  const csrf = document.cookie.split('; ').find(function (item) { return item.startsWith('bb_csrf='); });
  const csrfToken = csrf ? decodeURIComponent(csrf.split('=').slice(1).join('=')) : '';
  let rules = [];
  let sources = [];
  let groups = [[{ source: '', field: '', operator: 'lt', value: '' }]];
  let advancedMode = false;

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]; });
  }

  function selected(value, expected) {
    return String(value) === String(expected) ? ' selected' : '';
  }

  function when(value) {
    const time = new Date(value);
    return Number.isNaN(time.getTime()) ? String(value || '') : time.toLocaleString('ru-RU');
  }

  function sourceById(id) {
    return sources.find(function (item) { return item.id === id; }) || null;
  }

  function fieldByKey(condition) {
    const source = sourceById(condition.source);
    return source ? source.fields.find(function (item) { return item.key === condition.field; }) || null : null;
  }

  function operators(field) {
    if (!field) return [];
    if (field.type === 'list') return [['contains', 'содержит'], ['not_contains', 'не содержит']];
    if (field.type === 'boolean') return [['eq', 'равно'], ['ne', 'не равно']];
    return [['lt', 'меньше'], ['lte', 'меньше или равно'], ['gt', 'больше'], ['gte', 'больше или равно'], ['eq', 'равно'], ['ne', 'не равно']];
  }

  function defaultCondition() {
    const source = sources.length ? sources[0] : null;
    const field = source && source.fields.length ? source.fields[0] : null;
    return { source: source ? source.id : '', field: field ? field.key : '', operator: field && field.type === 'list' ? 'contains' : 'lt', value: '' };
  }

  function valueControl(condition, field, groupIndex, conditionIndex) {
    const attrs = ' data-rule-value data-group="' + groupIndex + '" data-condition="' + conditionIndex + '"';
    if (field && field.type === 'boolean') {
      return '<select' + attrs + '><option value="true"' + selected(condition.value, 'true') + '>Да</option><option value="false"' + selected(condition.value, 'false') + '>Нет</option></select>';
    }
    if (field && field.labels && field.labels.length) {
      return '<select' + attrs + '><option value="">Выберите значение</option>' + field.labels.map(function (label) { return '<option value="' + esc(label) + '"' + selected(condition.value, label) + '>' + esc(label) + '</option>'; }).join('') + '</select>';
    }
    return '<input' + attrs + ' value="' + esc(condition.value) + '" placeholder="' + (field && field.type === 'number' ? 'Число' : 'Название алерта') + '">';
  }

  function renderGroups() {
    groupsBox.innerHTML = groups.map(function (group, groupIndex) {
      const conditions = group.map(function (condition, conditionIndex) {
        const source = sourceById(condition.source);
        const field = fieldByKey(condition);
        const sourceOptions = sources.map(function (item) {
          return '<option value="' + esc(item.id) + '"' + selected(condition.source, item.id) + '>' + esc(item.name) + '</option>';
        }).join('');
        const fieldOptions = (source ? source.fields : []).map(function (item) {
          return '<option value="' + esc(item.key) + '"' + selected(condition.field, item.key) + '>' + esc(item.label) + '</option>';
        }).join('');
        const operatorOptions = operators(field).map(function (item) {
          return '<option value="' + item[0] + '"' + selected(condition.operator, item[0]) + '>' + item[1] + '</option>';
        }).join('');
        return '<div class="bb-rule-condition">' +
          '<span class="bb-rule-join">' + (conditionIndex ? 'И' : 'ЕСЛИ') + '</span>' +
          '<select data-rule-source data-group="' + groupIndex + '" data-condition="' + conditionIndex + '"><option value="">Источник</option>' + sourceOptions + '</select>' +
          '<select data-rule-field data-group="' + groupIndex + '" data-condition="' + conditionIndex + '"><option value="">Показатель</option>' + fieldOptions + '</select>' +
          '<select data-rule-operator data-group="' + groupIndex + '" data-condition="' + conditionIndex + '">' + operatorOptions + '</select>' +
          valueControl(condition, field, groupIndex, conditionIndex) +
          '<button type="button" class="bb-icon-btn bb-icon-btn-danger" data-remove-condition="' + groupIndex + ':' + conditionIndex + '" aria-label="Удалить условие"><i class="bi bi-x-lg"></i></button></div>';
      }).join('');
      return '<section class="bb-rule-group"><div class="bb-rule-group-head"><strong>' + (groupIndex ? 'ИЛИ' : 'Блок условий') + '</strong>' +
        (groupIndex ? '<button type="button" class="bb-icon-btn bb-icon-btn-danger" data-remove-group="' + groupIndex + '" aria-label="Удалить блок"><i class="bi bi-trash"></i></button>' : '') +
        '</div>' + conditions + '<button type="button" class="bb-rule-add-condition" data-add-condition="' + groupIndex + '"><i class="bi bi-plus-lg"></i> Условие «И»</button></section>';
    }).join('');
    syncExpression();
  }

  function conditionExpression(condition) {
    const source = sourceById(condition.source);
    const field = fieldByKey(condition);
    if (!source || !field || !condition.value) return '';
    const reference = source.namespace + '.' + field.key;
    if (field.type === 'list') {
      return '(' + JSON.stringify(condition.value) + (condition.operator === 'not_contains' ? ' not in ' : ' in ') + reference + ')';
    }
    const operatorMap = { lt: '<', lte: '<=', gt: '>', gte: '>=', eq: '==', ne: '!=' };
    const value = field.type === 'boolean' ? (condition.value === 'true' ? 'True' : 'False') : Number(condition.value);
    if (field.type === 'number' && !Number.isFinite(value)) return '';
    return '(' + reference + ' ' + (operatorMap[condition.operator] || '==') + ' ' + String(value) + ')';
  }

  function compileExpression() {
    const compiledGroups = groups.map(function (group) {
      const items = group.map(conditionExpression).filter(Boolean);
      return items.length ? '(' + items.join(' and ') + ')' : '';
    }).filter(Boolean);
    return compiledGroups.join(' or ');
  }

  function syncExpression() {
    if (!form) return;
    const expression = advancedMode ? form.elements.expression.value.trim() : compileExpression();
    if (!advancedMode) form.elements.expression.value = expression;
    preview.textContent = expression || 'Добавьте заполненное условие';
  }

  function resetForm() {
    form.reset();
    form.elements.rule_id.value = '';
    if (sources.length) form.elements.vm_id.value = sources[0].id;
    groups = [[defaultCondition()]];
    advancedMode = false;
    form.querySelector('.bb-rule-advanced').open = false;
    document.getElementById('bb-rule-cancel').hidden = true;
    error.textContent = '';
    renderGroups();
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
      return '<tr><td>' + esc(when(item.started_at)) + '</td><td>' + esc(when(item.ended_at)) + '</td><td>' + esc(vmNames.get(item.vm_id) || item.vm_id) + '</td><td>' + esc(item.rule_name) + '</td><td><code>' + esc(item.expression) + '</code></td><td>' + link + '</td></tr>';
    }).join('') || '<tr><td colspan="6" class="bb-muted">Срабатываний пока нет.</td></tr>';
  }

  async function loadRules() {
    if (!isAdmin) return;
    const response = await fetch('/api/v1/emergency-rules', { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Не удалось загрузить правила');
    const data = await response.json();
    rules = data.items || [];
    sources = data.sources || [];
    form.elements.vm_id.innerHTML = '<option value="">Выберите ВМ</option>' + sources.map(function (source) { return '<option value="' + esc(source.id) + '">' + esc(source.name) + '</option>'; }).join('');
    const sourceNames = new Map(sources.map(function (source) { return [source.id, source.name]; }));
    rulesBody.innerHTML = rules.map(function (rule) {
      return '<tr><td>' + esc(rule.name) + '</td><td>' + esc(sourceNames.get(rule.vm_id) || 'Все ВМ (старое правило)') + '</td><td><code>' + esc(rule.expression) + '</code></td><td><div class="bb-actions"><button type="button" class="bb-btn bb-btn-sm bb-btn-secondary" data-edit-rule="' + rule.id + '" aria-label="Редактировать правило"><i class="bi bi-pencil"></i></button><button type="button" class="bb-btn bb-btn-sm bb-btn-danger" data-delete-rule="' + rule.id + '" aria-label="Удалить правило"><i class="bi bi-trash"></i></button></div></td></tr>';
    }).join('') || '<tr><td colspan="4" class="bb-muted">Правила не настроены.</td></tr>';
    if (!form.elements.rule_id.value && !form.elements.vm_id.value && sources.length) {
      form.elements.vm_id.value = sources[0].id;
      groups = [[defaultCondition()]];
      renderGroups();
    }
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
    syncExpression();
    const id = form.elements.rule_id.value;
    const expression = form.elements.expression.value.trim();
    if (!expression) { error.textContent = 'Добавьте хотя бы одно полностью заполненное условие.'; return; }
    const method = id ? 'PUT' : 'POST';
    const url = id ? '/api/v1/emergency-rules/' + encodeURIComponent(id) : '/api/v1/emergency-rules';
    try {
      const response = await fetch(url, { method: method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify({ vm_id: form.elements.vm_id.value, name: form.elements.name.value.trim(), expression: expression }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail?.message || 'Не удалось сохранить правило');
      resetForm();
      await reload();
    } catch (reason) { error.textContent = reason.message; }
  });

  form?.elements.expression.addEventListener('input', function () { advancedMode = true; syncExpression(); });
  groupsBox?.addEventListener('change', function (event) {
    const target = event.target;
    const groupIndex = Number(target.dataset.group);
    const conditionIndex = Number(target.dataset.condition);
    const condition = groups[groupIndex] && groups[groupIndex][conditionIndex];
    if (!condition) return;
    if (target.hasAttribute('data-rule-source')) {
      condition.source = target.value;
      const source = sourceById(condition.source);
      const field = source && source.fields.length ? source.fields[0] : null;
      condition.field = field ? field.key : '';
      condition.operator = field && field.type === 'list' ? 'contains' : 'lt';
      condition.value = field && field.type === 'boolean' ? 'true' : '';
      renderGroups();
    } else if (target.hasAttribute('data-rule-field')) {
      condition.field = target.value;
      const field = fieldByKey(condition);
      condition.operator = field && field.type === 'list' ? 'contains' : 'lt';
      condition.value = field && field.type === 'boolean' ? 'true' : '';
      renderGroups();
    } else if (target.hasAttribute('data-rule-operator')) {
      condition.operator = target.value;
      syncExpression();
    } else if (target.hasAttribute('data-rule-value')) {
      condition.value = target.value;
      syncExpression();
    }
  });
  groupsBox?.addEventListener('input', function (event) {
    const target = event.target;
    if (!target.hasAttribute('data-rule-value')) return;
    const condition = groups[Number(target.dataset.group)]?.[Number(target.dataset.condition)];
    if (condition) { condition.value = target.value; syncExpression(); }
  });
  groupsBox?.addEventListener('click', function (event) {
    const add = event.target.closest('[data-add-condition]');
    const remove = event.target.closest('[data-remove-condition]');
    const removeGroup = event.target.closest('[data-remove-group]');
    if (add) groups[Number(add.dataset.addCondition)].push(defaultCondition());
    if (remove) {
      const parts = remove.dataset.removeCondition.split(':').map(Number);
      groups[parts[0]].splice(parts[1], 1);
      if (!groups[parts[0]].length) groups[parts[0]].push(defaultCondition());
    }
    if (removeGroup) groups.splice(Number(removeGroup.dataset.removeGroup), 1);
    if (add || remove || removeGroup) renderGroups();
  });
  document.getElementById('bb-rule-add-group')?.addEventListener('click', function () { groups.push([defaultCondition()]); renderGroups(); });

  rulesBody?.addEventListener('click', async function (event) {
    const edit = event.target.closest('[data-edit-rule]');
    const remove = event.target.closest('[data-delete-rule]');
    if (edit) {
      const rule = rules.find(function (item) { return String(item.id) === edit.dataset.editRule; });
      if (!rule) return;
      form.elements.rule_id.value = rule.id;
      form.elements.vm_id.value = rule.vm_id || '';
      form.elements.name.value = rule.name;
      form.elements.expression.value = rule.expression;
      advancedMode = true;
      form.querySelector('.bb-rule-advanced').open = true;
      document.getElementById('bb-rule-cancel').hidden = false;
      syncExpression();
      form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else if (remove && window.confirm('Удалить это правило? История срабатываний сохранится.')) {
      const response = await fetch('/api/v1/emergency-rules/' + encodeURIComponent(remove.dataset.deleteRule), { method: 'DELETE', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrfToken } });
      if (response.ok) await reload();
      else error.textContent = 'Не удалось удалить правило.';
    }
  });

  document.getElementById('bb-rule-cancel')?.addEventListener('click', resetForm);
  document.getElementById('bb-emergency-refresh').addEventListener('click', reload);
  reload();
})();
