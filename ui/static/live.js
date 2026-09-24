(function () {
  const root = document.getElementById('bb-dashboard');
  const sourceNode = document.getElementById('bb-dashboard-sources');
  if (!root || !sourceNode) return;

  const sources = JSON.parse(sourceNode.textContent || '[]');
  const latest = {};
  const picks = JSON.parse(localStorage.getItem('bb-dash-pick') || '{}');
  const openSheets = new Set(JSON.parse(localStorage.getItem('bb-dash-open') || '[]'));
  let boardSignature = '';
  let openPicker = '';

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function saveOpen() {
    localStorage.setItem('bb-dash-open', JSON.stringify(Array.from(openSheets)));
  }

  function savePicks() {
    localStorage.setItem('bb-dash-pick', JSON.stringify(picks));
  }

  function sheetKey(vmId, part) {
    return part ? vmId + '/' + part : vmId;
  }

  function pickOf(vmId) {
    const pick = picks[vmId];
    if (!pick || pick.mode !== 'set' || !Array.isArray(pick.keys)) return { mode: 'all', keys: [] };
    return { mode: 'set', keys: pick.keys };
  }

  function countLabel(count) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return count + ' показатель';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return count + ' показателя';
    return count + ' показателей';
  }

  function formatTime(iso) {
    if (!iso) return 'нет данных';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    return date.toLocaleString('ru-RU');
  }

  function sourceFields(source, channel) {
    const seen = new Map();
    (source[channel] || []).forEach(function (field) {
      if (field && field.key && !seen.has(field.key)) seen.set(field.key, field.label || field.key);
    });
    return Array.from(seen.entries());
  }

  function visibleFields(source, channel) {
    const own = sourceFields(source, channel);
    const pick = pickOf(source.id);
    if (pick.mode === 'all') return own;
    const wanted = new Set(pick.keys);
    return own.filter(function (pair) { return wanted.has(channel + ':' + pair[0]); });
  }

  function pickerLabel(source) {
    const pick = pickOf(source.id);
    if (pick.mode === 'all') return 'Все';
    const labels = [];
    ['analog', 'discrete'].forEach(function (channel) {
      sourceFields(source, channel).forEach(function (pair) {
        if (pick.keys.indexOf(channel + ':' + pair[0]) >= 0) labels.push(pair[1]);
      });
    });
    if (!labels.length) return 'Все';
    if (labels.length === 1) return labels[0];
    return countLabel(labels.length);
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

  function pickerMenu(source) {
    const pick = pickOf(source.id);
    const allOn = pick.mode === 'all';
    function item(key, label, on) {
      return '<button type="button" class="bb-picker-item' + (on ? ' is-on' : '') + '" data-pick="' + esc(key) + '" data-vm="' + esc(source.id) + '"><span class="bb-picker-mark">' + (on ? '✓' : '') + '</span><span>' + esc(label) + '</span></button>';
    }
    function group(title, channel) {
      const rows = sourceFields(source, channel);
      if (!rows.length) return '';
      return '<div class="bb-picker-group">' + title + '</div>' + rows.map(function (pair) {
        const key = channel + ':' + pair[0];
        return item(key, pair[1], !allOn && pick.keys.indexOf(key) >= 0);
      }).join('');
    }
    return '<div class="bb-picker-menu"' + (openPicker === source.id ? '' : ' hidden') + '>' +
      '<p class="bb-picker-hint">По умолчанию показаны все показатели этого источника. Отмеченные заменяют набор.</p>' +
      item('all', 'Все', allOn) +
      group('Аналоги', 'analog') +
      group('Дискреты', 'discrete') +
      '</div>';
  }

  function subsheet(source, part, title, body) {
    const key = sheetKey(source.id, part);
    const open = openSheets.has(key);
    return '<section class="bb-subsheet"><div class="bb-subsheet-head"><button type="button" class="bb-sheet-toggle bb-subsheet-toggle" data-sheet-toggle data-sheet="' + esc(source.id) + '" data-part="' + part + '" aria-expanded="' + (open ? 'true' : 'false') + '"><i class="bi ' + (open ? 'bi-chevron-down' : 'bi-chevron-right') + '"></i><span>' + title + '</span></button></div><div class="bb-subsheet-body"' + (open ? '' : ' hidden') + '>' + body + '</div></section>';
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

  function analogBody(source) {
    const rows = visibleFields(source, 'analog');
    if (!rows.length) {
      const text = pickOf(source.id).mode === 'all' ? 'Аналоговые записи еще не поступали.' : 'В этом наборе нет аналоговых показателей.';
      return '<p class="bb-hint">' + text + '</p>';
    }
    return '<div class="bb-analog-list">' + rows.map(function (pair) {
      return '<div class="bb-analog-row" data-analog-row="' + esc(pair[0]) + '"><span>' + esc(pair[1]) + '</span><progress max="100" value="0"></progress><strong data-analog-value>—</strong></div>';
    }).join('') + '</div>';
  }

  function discreteBody(source) {
    const rows = visibleFields(source, 'discrete');
    if (!rows.length) {
      const text = pickOf(source.id).mode === 'all' ? 'Дискретные записи еще не поступали.' : 'В этом наборе нет дискретных показателей.';
      return '<p class="bb-hint">' + text + '</p>';
    }
    return '<ul class="bb-discrete-list">' + rows.map(function (pair) {
      return '<li class="is-off" data-discrete-row="' + esc(pair[0]) + '"><span>' + esc(pair[1]) + '</span><strong>ВЫКЛ</strong></li>';
    }).join('') + '</ul>';
  }

  function renderBoards() {
    const host = document.getElementById('bb-dash-boards');
    const signature = JSON.stringify(sources.map(function (source) {
      return [source.id, source.name, sourceFields(source, 'analog'), sourceFields(source, 'discrete'), pickOf(source.id)];
    }));
    if (signature !== boardSignature) {
      boardSignature = signature;
      host.innerHTML = sources.map(function (source) {
        const open = openSheets.has(source.id);
        return '<section class="bb-card bb-sheet" data-vm-card="' + esc(source.id) + '"><div class="bb-sheet-head"><button type="button" class="bb-sheet-toggle" data-sheet-toggle data-sheet="' + esc(source.id) + '" data-part="" aria-expanded="' + (open ? 'true' : 'false') + '"><i class="bi ' + (open ? 'bi-chevron-down' : 'bi-chevron-right') + '"></i><span class="bb-sheet-name">' + esc(source.name) + '</span></button><span class="bb-sheet-time" data-vm-time>нет данных</span><div class="bb-picker"><button type="button" class="bb-picker-btn" data-picker-btn data-vm="' + esc(source.id) + '" aria-expanded="' + (openPicker === source.id ? 'true' : 'false') + '"><span>' + esc(pickerLabel(source)) + '</span><i class="bi bi-chevron-down"></i></button>' + pickerMenu(source) + '</div></div><div class="bb-sheet-body"' + (open ? '' : ' hidden') + '>' +
          subsheet(source, 'analog', 'Аналоговые данные <span class="bb-subsheet-count">' + visibleFields(source, 'analog').length + '</span>', analogBody(source)) +
          subsheet(source, 'discrete', 'Дискретные состояния <span class="bb-subsheet-count">' + visibleFields(source, 'discrete').length + '</span>', discreteBody(source)) +
          subsheet(source, 'alarms', 'Сообщения аварий <span class="bb-subsheet-count" data-alarm-count>0</span>', '<ul class="bb-alarm-list" data-alarm-list></ul><p class="bb-hint" data-alarm-empty>Активных аварийных сообщений нет.</p>') +
          '</div></section>';
      }).join('') || '<section class="bb-card"><p class="bb-hint">Источников чтения пока нет.</p></section>';
    }
    sources.forEach(patchBoard);
  }

  function patchBoard(source) {
    const card = document.querySelector('[data-vm-card="' + cssEscape(source.id) + '"]');
    if (!card) return;
    const sample = latest[source.id];
    const analog = sample && sample.analog ? sample.analog : {};
    const discrete = sample && sample.discrete ? sample.discrete : {};
    const alerts = sample && Array.isArray(sample.alerts) ? sample.alerts : [];
    const when = formatTime(sample && sample.captured_at);
    const time = card.querySelector('[data-vm-time]');
    if (time) time.textContent = sample ? when : 'нет данных';
    card.querySelectorAll('[data-analog-row]').forEach(function (row) {
      const text = valueOf(analog, row.getAttribute('data-analog-row'));
      const strong = row.querySelector('[data-analog-value]');
      const bar = row.querySelector('progress');
      if (strong) strong.textContent = text === '' ? '—' : text;
      if (bar) bar.value = text === '' ? 0 : 100;
    });
    card.querySelectorAll('[data-discrete-row]').forEach(function (row) {
      const on = !!discrete[row.getAttribute('data-discrete-row')];
      row.className = on ? 'is-on' : 'is-off';
      const strong = row.querySelector('strong');
      if (strong) strong.textContent = on ? 'ВКЛ' : 'ВЫКЛ';
    });
    const count = card.querySelector('[data-alarm-count]');
    if (count) count.textContent = String(alerts.length);
    const list = card.querySelector('[data-alarm-list]');
    const empty = card.querySelector('[data-alarm-empty]');
    if (list) {
      list.innerHTML = alerts.map(function (name) {
        return '<li><span class="bb-alarm-time">' + esc(when) + '</span><span>' + esc(name) + '</span></li>';
      }).join('');
    }
    if (empty) empty.hidden = alerts.length > 0;
  }

  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(String(value));
    return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  }

  function setOpen(vmId, part, open) {
    const key = sheetKey(vmId, part);
    if (open) openSheets.add(key);
    else openSheets.delete(key);
    saveOpen();
    const card = document.querySelector('[data-vm-card="' + cssEscape(vmId) + '"]');
    if (!card) return;
    const button = card.querySelector('[data-sheet-toggle][data-part="' + part + '"]');
    const body = part ? (button ? button.parentElement.parentElement.querySelector('.bb-subsheet-body') : null) : card.querySelector('.bb-sheet-body');
    if (button) {
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
      const icon = button.querySelector('i');
      if (icon) icon.className = 'bi ' + (open ? 'bi-chevron-down' : 'bi-chevron-right');
    }
    if (body) body.hidden = !open;
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

  root.addEventListener('click', function (event) {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const pick = target.closest('[data-pick]');
    if (pick) {
      const vmId = pick.getAttribute('data-vm');
      const key = pick.getAttribute('data-pick');
      const current = pickOf(vmId);
      if (key === 'all') {
        delete picks[vmId];
      } else if (current.mode === 'all') {
        picks[vmId] = { mode: 'set', keys: [key] };
      } else if (current.keys.indexOf(key) >= 0) {
        const keys = current.keys.filter(function (item) { return item !== key; });
        if (keys.length) picks[vmId] = { mode: 'set', keys: keys };
        else delete picks[vmId];
      } else {
        picks[vmId] = { mode: 'set', keys: current.keys.concat([key]) };
      }
      openPicker = vmId;
      savePicks();
      boardSignature = '';
      renderBoards();
      return;
    }
    const pickerBtn = target.closest('[data-picker-btn]');
    if (pickerBtn) {
      const vmId = pickerBtn.getAttribute('data-vm');
      openPicker = openPicker === vmId ? '' : vmId;
      boardSignature = '';
      renderBoards();
      return;
    }
    const toggle = target.closest('[data-sheet-toggle]');
    if (!toggle) return;
    const vmId = toggle.getAttribute('data-sheet');
    const part = toggle.getAttribute('data-part') || '';
    setOpen(vmId, part, !openSheets.has(sheetKey(vmId, part)));
  });

  document.addEventListener('click', function (event) {
    const target = event.target instanceof Element ? event.target : null;
    if (!openPicker || (target && target.closest('.bb-picker'))) return;
    openPicker = '';
    boardSignature = '';
    renderBoards();
  });

  window.addEventListener('bb-hub-event', function (event) {
    const message = event.detail || {};
    if (message.type === 'snapshot') {
      const payload = message.payload || {};
      const tags = (payload.tags_good && payload.tags_good.length) ? payload.tags_good : (payload.tags || []);
      tags.forEach(applyTag);
      renderBoards();
      renderSystem(payload.system || {});
      return;
    }
    if (message.type !== 'delta') return;
    if (message.topic === 'tags') {
      applyTag(message.payload || {});
      renderBoards();
    }
    if (message.topic === 'system') renderSystem(message.payload || {});
  });

  renderBoards();
})();
