(function () {
  const root = document.getElementById('bb-dashboard');
  const sourceNode = document.getElementById('bb-dashboard-sources');
  if (!root || !sourceNode) return;

  const sources = JSON.parse(sourceNode.textContent || '[]');
  const latest = {};
  const hiddenVms = new Set(JSON.parse(localStorage.getItem('bb-dash-vm-off') || '[]'));
  const hiddenAnalog = new Set(JSON.parse(localStorage.getItem('bb-dash-analog-off') || '[]'));
  const hiddenDiscrete = new Set(JSON.parse(localStorage.getItem('bb-dash-discrete-off') || '[]'));
  let filterSignature = '';

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function save(key, set) {
    localStorage.setItem(key, JSON.stringify(Array.from(set)));
  }

  function formatTime(iso) {
    if (!iso) return 'нет данных';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    return date.toLocaleString('ru-RU');
  }

  function fields(channel) {
    const seen = new Map();
    sources.forEach(function (source) {
      (source[channel] || []).forEach(function (field) {
        if (!seen.has(field.key)) seen.set(field.key, field.label || field.key);
      });
    });
    return Array.from(seen.entries());
  }

  function rememberLiveFields(sample) {
    const source = sources.find(function (item) { return item.id === sample.vm_id; });
    if (!source || !sample) return;
    function add(channel, values) {
      const known = new Set((source[channel] || []).map(function (field) { return field.key; }));
      Object.keys(values || {}).forEach(function (key) {
        if (!known.has(key)) source[channel].push({ key: key, label: key });
      });
    }
    source.analog = source.analog || [];
    source.discrete = source.discrete || [];
    add('analog', sample.analog);
    add('discrete', sample.discrete);
  }

  function applyTag(sample) {
    if (!sample || !sample.vm_id) return;
    if (sample.quality === 'bad' && latest[sample.vm_id]) return;
    latest[sample.vm_id] = sample;
    rememberLiveFields(sample);
  }

  function renderFilters() {
    const signature = JSON.stringify(sources.map(function (source) {
      return [source.id, (source.analog || []).map(function (field) { return field.key; }), (source.discrete || []).map(function (field) { return field.key; })];
    }));
    if (signature === filterSignature) return;
    filterSignature = signature;
    const sourceHost = document.getElementById('bb-dash-sources');
    const fieldHost = document.getElementById('bb-dash-fields');
    sourceHost.innerHTML = sources.map(function (source) {
      const checked = hiddenVms.has(source.id) ? '' : ' checked';
      return '<label class="bb-chk"><input type="checkbox" data-dash-vm="' + esc(source.id) + '"' + checked + '> ' + esc(source.name) + '</label>';
    }).join('') || '<p class="bb-hint">Источников чтения пока нет.</p>';
    function group(title, channel, hidden) {
      const items = fields(channel);
      if (!items.length) return '';
      return '<fieldset><legend>' + title + '</legend><div class="bb-check-grid">' + items.map(function (pair) {
        const checked = hidden.has(pair[0]) ? '' : ' checked';
        return '<label class="bb-chk"><input type="checkbox" data-dash-field="' + channel + '" value="' + esc(pair[0]) + '"' + checked + '> ' + esc(pair[1]) + '</label>';
      }).join('') + '</div></fieldset>';
    }
    fieldHost.innerHTML = group('Поля аналогов', 'analog', hiddenAnalog) + group('Поля дискретов', 'discrete', hiddenDiscrete);
  }

  function valueOf(map, key) {
    if (!map || !Object.prototype.hasOwnProperty.call(map, key)) return '';
    const value = map[key];
    if (value == null || value === '') return '';
    if (typeof value === 'number') {
      const text = value.toFixed(4).replace(/\.?0+$/, '');
      return text === '-0' ? '0' : text;
    }
    return String(value);
  }

  function renderBoards() {
    const host = document.getElementById('bb-dash-boards');
    const analogFields = fields('analog').filter(function (pair) { return !hiddenAnalog.has(pair[0]); });
    const discreteFields = fields('discrete').filter(function (pair) { return !hiddenDiscrete.has(pair[0]); });
    host.innerHTML = sources.filter(function (source) { return !hiddenVms.has(source.id); }).map(function (source) {
      const sample = latest[source.id];
      const analog = sample && sample.analog ? sample.analog : {};
      const discrete = sample && sample.discrete ? sample.discrete : {};
      const alerts = sample && Array.isArray(sample.alerts) ? sample.alerts : [];
      const when = formatTime(sample && sample.captured_at);
      const analogRows = analogFields.filter(function (pair) {
        return (source.analog || []).some(function (field) { return field.key === pair[0]; }) || Object.prototype.hasOwnProperty.call(analog, pair[0]);
      }).map(function (pair) {
        const text = valueOf(analog, pair[0]);
        return '<div class="bb-analog-row"><span>' + esc(pair[1]) + '</span><progress max="100" value="' + (text === '' ? '0' : '100') + '"></progress><strong>' + esc(text) + '</strong></div>';
      }).join('');
      const discreteRows = discreteFields.filter(function (pair) {
        return (source.discrete || []).some(function (field) { return field.key === pair[0]; }) || Object.prototype.hasOwnProperty.call(discrete, pair[0]);
      }).map(function (pair) {
        const on = !!discrete[pair[0]];
        return '<li class="' + (on ? 'is-on' : 'is-off') + '"><span>' + esc(pair[1]) + '</span><strong>' + (on ? 'ВКЛ' : 'ВЫКЛ') + '</strong></li>';
      }).join('');
      const alarmRows = alerts.map(function (name) {
        return '<li><span class="bb-alarm-time">' + esc(when) + '</span><span>' + esc(name) + '</span></li>';
      }).join('');
      return '<section class="bb-card bb-source-card"><h2>' + esc(source.name) + '</h2><div class="bb-dash-grid">' +
        '<section><h3>Аналоговые данные</h3><p class="bb-hint">Последнее обновление: ' + esc(sample ? when : 'нет данных') + '</p>' +
        (analogRows ? '<div class="bb-analog-list">' + analogRows + '</div>' : '<p class="bb-hint">Аналоговые записи еще не поступали.</p>') +
        '</section><section><h3>Дискретные состояния</h3><p class="bb-hint">Последнее обновление: ' + esc(sample ? when : 'нет данных') + '</p>' +
        (discreteRows ? '<ul class="bb-discrete-list">' + discreteRows + '</ul>' : '<p class="bb-hint">Дискретные записи еще не поступали.</p>') +
        '</section></div><h3>Сообщения аварий <small class="bb-hint">Сейчас: ' + alerts.length + '</small></h3>' +
        (alarmRows ? '<ul class="bb-alarm-list">' + alarmRows + '</ul>' : '<p class="bb-hint">Активных аварийных сообщений нет.</p>') +
        '</section>';
    }).join('') || '<section class="bb-card"><p class="bb-hint">Нет выбранных источников.</p></section>';
  }

  function text(selector, value) {
    const node = root.querySelector(selector);
    if (node) node.textContent = value;
  }

  function renderSystem(payload) {
    const data = payload || {};
    const disk = data.disk || {};
    const cpu = data.cpu || {};
    const memory = data.memory || {};
    const process = data.process || {};
    text('[data-monitor="server_time"]', data.server_time || '—');
    text('[data-monitor="disk"]', disk.percent == null ? 'n/a' : disk.percent + '%');
    text('[data-monitor="disk_hint"]', disk.used_gb == null ? 'Данные недоступны' : disk.used_gb + ' / ' + disk.total_gb + ' ГБ, свободно ' + disk.free_gb + ' ГБ');
    text('[data-monitor="cpu"]', cpu.percent == null ? 'n/a' : cpu.percent + '%');
    text('[data-monitor="cpu_cores"]', 'Ядра: ' + (cpu.cores_physical || '?') + ' физ / ' + (cpu.cores_logical || '?') + ' лог');
    text('[data-monitor="cpu_fan"]', 'Вентилятор CPU: ' + (cpu.fan_rpm == null ? 'n/a' : cpu.fan_rpm + ' RPM'));
    text('[data-monitor="memory"]', memory.percent == null ? 'n/a' : memory.percent + '%');
    text('[data-monitor="memory_hint"]', memory.used_gb == null ? 'Данные недоступны' : memory.used_gb + ' / ' + memory.total_gb + ' ГБ');
    text('[data-monitor="pid"]', 'PID ' + (process.pid == null ? '—' : process.pid));
    text('[data-monitor="uptime"]', 'Uptime: ' + (process.uptime_sec == null ? 'n/a' : process.uptime_sec + ' сек'));
    const disks = root.querySelector('[data-monitor="disks"]');
    const list = data.disks || [];
    disks.innerHTML = list.length ? list.map(function (item) {
      return '<article class="bb-monitor-disk"><div class="bb-monitor-disk-head"><strong>' + esc(item.device || '-') + '</strong><span>' + esc(item.percent) + '%</span></div><div class="bb-hint">' + esc(item.mount) + '</div><div>ФС: ' + esc(item.fstype || '-') + '</div><div>Занято: ' + esc(item.used_gb) + ' / ' + esc(item.total_gb) + ' ГБ</div><div>Свободно: ' + esc(item.free_gb) + ' ГБ</div></article>';
    }).join('') : '<p class="bb-hint">Список дисков недоступен.</p>';
    const gpio = data.gpio_items || [];
    const gpioList = root.querySelector('[data-gpio-list]');
    const empty = root.querySelector('[data-gpio-empty]');
    gpioList.innerHTML = gpio.map(function (item) {
      const label = item.vm_name ? item.vm_name + ' · ' + item.name : item.name;
      return '<li class="is-on"><span>' + esc(label) + '</span><strong>ACTIVE</strong></li>';
    }).join('');
    empty.hidden = gpio.length > 0;
    const stamp = root.querySelector('[data-gpio-time]');
    stamp.textContent = 'Последнее обновление: ' + (data.gpio_time ? formatTime(data.gpio_time) : 'нет данных');
  }

  root.addEventListener('change', function (event) {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.dataset.dashVm) {
      if (target.checked) hiddenVms.delete(target.dataset.dashVm);
      else hiddenVms.add(target.dataset.dashVm);
      save('bb-dash-vm-off', hiddenVms);
      renderBoards();
    }
    if (target.dataset.dashField) {
      const hidden = target.dataset.dashField === 'analog' ? hiddenAnalog : hiddenDiscrete;
      const storageKey = target.dataset.dashField === 'analog' ? 'bb-dash-analog-off' : 'bb-dash-discrete-off';
      if (target.checked) hidden.delete(target.value);
      else hidden.add(target.value);
      save(storageKey, hidden);
      renderBoards();
    }
  });

  window.addEventListener('bb-hub-event', function (event) {
    const message = event.detail || {};
    if (message.type === 'snapshot') {
      const payload = message.payload || {};
      const tags = (payload.tags_good && payload.tags_good.length) ? payload.tags_good : (payload.tags || []);
      tags.forEach(applyTag);
      renderFilters();
      renderBoards();
      renderSystem(payload.system || {});
      return;
    }
    if (message.type !== 'delta') return;
    if (message.topic === 'tags') {
      applyTag(message.payload || {});
      renderFilters();
      renderBoards();
    }
    if (message.topic === 'system') renderSystem(message.payload || {});
  });

  renderFilters();
  renderBoards();
})();
