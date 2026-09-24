(function () {
  const root = document.getElementById('bb-charts');
  const form = document.getElementById('bb-chart-form');
  const fieldHost = document.getElementById('bb-chart-fields');
  const meta = document.getElementById('bb-chart-meta');
  const chartEl = document.getElementById('bb-echarts');
  if (!root || !form || !fieldHost || !meta || !chartEl) return;

  const state = { chart: null, table: 'analog', columns: [], live: false, lastTs: 0, catalog: [] };
  let openPicker = false;
  let activeFetch = null;
  let loadToken = 0;
  const columnPick = JSON.parse(localStorage.getItem('bb-charts-columns') || '{}');

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

  const savedVm = localStorage.getItem('bb-charts-vm') || localStorage.getItem('bb-data-vm');
  if (savedVm) setRadio('vm_id', savedVm);
  const savedTab = localStorage.getItem('bb-charts-tab') || localStorage.getItem('bb-data-tab');
  if (savedTab === 'analog' || savedTab === 'discrete') setRadio('active_tab', savedTab);

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

  function datesOpen() {
    return !form.querySelector('[name="date_from"]').value && !form.querySelector('[name="date_to"]').value;
  }

  function fields() {
    const active = tab();
    const id = vmId();
    const source = state.catalog.find(function (item) { return item.id === id; }) || state.catalog[0];
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
    const pick = currentPick();
    const rows = fields();
    if (!vmId()) {
      fieldHost.innerHTML = '';
      openPicker = false;
      return;
    }
    fieldHost.innerHTML = '<div class="bb-picker"><button type="button" class="bb-picker-btn" data-picker-btn aria-expanded="' + (openPicker ? 'true' : 'false') + '"><span>' + esc(pickerLabel()) + '</span><i class="bi bi-chevron-down"></i></button><div class="bb-picker-menu"' + (openPicker ? '' : ' hidden') + '><p class="bb-picker-hint">По умолчанию все поля этой машины. Отмеченные заменяют набор.</p><button type="button" class="bb-picker-item' + (pick.mode === 'all' ? ' is-on' : '') + '" data-pick="all"><span class="bb-picker-mark">' + (pick.mode === 'all' ? '✓' : '') + '</span><span>Все</span></button>' +
      rows.map(function (pair) {
        const on = pick.mode === 'set' && pick.keys.indexOf(pair[0]) >= 0;
        return '<button type="button" class="bb-picker-item' + (on ? ' is-on' : '') + '" data-pick="' + esc(pair[0]) + '"><span class="bb-picker-mark">' + (on ? '✓' : '') + '</span><span>' + esc(pair[1]) + '</span></button>';
      }).join('') + '</div></div>';
  }

  function formatAxisTime(ms) {
    return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(ms));
  }

  function formatTooltipTime(ms) {
    return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(ms));
  }

  function coerceAnalog(value) {
    if (value == null || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function coerceDiscrete(value) {
    if (value == null || value === '') return null;
    return value ? 1 : 0;
  }

  function seriesName(column, labels) {
    const full = labels[column] || column;
    const sep = ' · ';
    const idx = full.indexOf(sep);
    return idx >= 0 ? full.slice(idx + sep.length) : full;
  }

  function yAxis(table, points, columns) {
    if (table === 'discrete') return { type: 'value', min: -0.05, max: 1.05, interval: 1, scale: false };
    let minV = Infinity;
    let maxV = -Infinity;
    points.forEach(function (point) {
      columns.forEach(function (column) {
        const value = coerceAnalog(point.values[column]);
        if (value != null) {
          minV = Math.min(minV, value);
          maxV = Math.max(maxV, value);
        }
      });
    });
    if (!Number.isFinite(minV) || !Number.isFinite(maxV)) return { type: 'value', scale: true };
    const span = maxV - minV;
    const pad = span > 0 ? span * 0.08 : (Math.abs(maxV) > 1e-9 ? Math.abs(maxV) * 0.05 : 0.1);
    return { type: 'value', scale: true, min: minV - pad, max: maxV + pad };
  }

  function ensureChart() {
    if (!window.echarts) return null;
    if (!state.chart) state.chart = echarts.init(chartEl);
    return state.chart;
  }

  function resizeChart() {
    if (!state.chart) return;
    requestAnimationFrame(function () {
      if (state.chart) state.chart.resize();
    });
  }

  function renderEmpty(message) {
    if (state.chart) state.chart.clear();
    meta.textContent = message;
    state.lastTs = 0;
    state.live = false;
  }

  function setFullData(payload) {
    state.table = payload.table || 'analog';
    state.columns = payload.columns || [];
    if (!state.columns.length) {
      renderEmpty('Нет полей для графика.');
      return;
    }
    if (!payload.points || !payload.points.length) {
      renderEmpty('Нет записей за выбранные условия.');
      state.table = payload.table || state.table;
      state.columns = payload.columns || [];
      state.live = datesOpen() && state.columns.length > 0;
      return;
    }
    const chart = ensureChart();
    if (!chart) {
      meta.textContent = 'Не удалось инициализировать график.';
      return;
    }
    meta.textContent = 'Точек: ' + payload.row_count + (payload.realtime ? '. Online: текущие сутки, дальше по WebSocket.' : '.');
    const labels = payload.column_labels || {};
    const series = state.columns.map(function (column) {
      return {
        id: 'chart-col:' + column,
        name: seriesName(column, labels),
        type: 'line',
        showSymbol: payload.points.length <= 120,
        symbolSize: 4,
        connectNulls: false,
        data: payload.points.map(function (point) {
          const y = state.table === 'discrete' ? coerceDiscrete(point.values[column]) : coerceAnalog(point.values[column]);
          return [point.ts_ms, y];
        }),
      };
    });
    chart.setOption({
      tooltip: {
        trigger: 'axis',
        confine: true,
        axisPointer: { type: 'cross', label: { backgroundColor: '#6a7985' } },
        extraCssText: 'max-height:60vh; overflow:auto; max-width:min(520px, 90vw); white-space:normal; pointer-events:none;',
        formatter: function (params) {
          if (!params || !params.length) return '';
          const lines = [formatTooltipTime(params[0].axisValue)];
          params.forEach(function (item) {
            const y = Array.isArray(item.value) ? item.value[1] : item.value;
            let text = '—';
            if (y != null && y !== '' && Number.isFinite(Number(y))) {
              text = state.table === 'discrete' ? String(Math.round(Number(y))) : String(Number(y));
            }
            lines.push(item.marker + ' ' + item.seriesName + ': ' + text);
          });
          return lines.join('<br/>');
        },
      },
      legend: { type: 'scroll' },
      grid: { left: 92, right: 28, top: 48, bottom: 96 },
      xAxis: {
        type: 'time',
        boundaryGap: false,
        axisLabel: { hideOverlap: true, formatter: function (value) { return formatAxisTime(value); } },
        splitLine: { show: false },
      },
      yAxis: yAxis(state.table, payload.points, state.columns),
      dataZoom: [
        { type: 'inside', xAxisIndex: 0, filterMode: 'none' },
        { type: 'inside', yAxisIndex: 0, filterMode: 'none' },
        { type: 'slider', xAxisIndex: 0, height: 28, bottom: 12 },
        { type: 'slider', yAxisIndex: 0, orient: 'vertical', width: 22, left: 6, top: 52, bottom: 100 },
      ],
      series: series,
    }, true);
    state.lastTs = payload.points[payload.points.length - 1].ts_ms || 0;
    state.live = !!payload.realtime && datesOpen();
    resizeChart();
  }

  function appendPoint(sample) {
    if (!state.live || !sample || sample.quality === 'bad') return;
    if (String(sample.vm_id) !== vmId()) return;
    const ts = Date.parse(sample.captured_at);
    if (!Number.isFinite(ts) || ts <= state.lastTs) return;
    if (!state.chart) {
      const source = state.table === 'discrete' ? (sample.discrete || {}) : (sample.analog || {});
      const values = {};
      state.columns.forEach(function (column) {
        const field = column.split('|').slice(1).join('|');
        const owner = column.split('|')[0];
        values[column] = owner === String(sample.vm_id) ? source[field] : null;
      });
      setFullData({ table: state.table, columns: state.columns, column_labels: {}, points: [{ ts_ms: ts, values: values }], row_count: 1, realtime: true });
      return;
    }
    const option = state.chart.getOption();
    if (!option || !option.series) return;
    const source = state.table === 'discrete' ? (sample.discrete || {}) : (sample.analog || {});
    let changed = false;
    option.series.forEach(function (series) {
      const id = series.id ? String(series.id) : '';
      if (id.indexOf('chart-col:') !== 0) return;
      const key = id.slice('chart-col:'.length);
      const split = key.split('|');
      if (split[0] !== String(sample.vm_id)) return;
      const field = split.slice(1).join('|');
      if (!Object.prototype.hasOwnProperty.call(source, field)) return;
      const y = state.table === 'discrete' ? coerceDiscrete(source[field]) : coerceAnalog(source[field]);
      series.data.push([ts, y]);
      changed = true;
    });
    if (!changed) return;
    state.chart.setOption({ series: option.series }, false);
    state.lastTs = ts;
    meta.textContent = 'Online. Последняя точка ' + formatTooltipTime(ts) + '.';
  }

  function params(stamp) {
    const query = new URLSearchParams();
    const id = vmId();
    if (id) query.append('vm_id', id);
    query.set('table', tab());
    const from = form.querySelector('[name="date_from"]').value;
    const to = form.querySelector('[name="date_to"]').value;
    if (from) query.set('date_from', from);
    if (to) query.set('date_to', to);
    currentPick().keys.forEach(function (key) { query.append('column', key); });
    query.set('_', stamp || String(Date.now()));
    return query;
  }

  async function reloadView() {
    if (activeFetch) activeFetch.abort();
    const ctl = new AbortController();
    activeFetch = ctl;
    const token = ++loadToken;
    const id = vmId();
    if (!id) {
      state.catalog = [];
      renderFields();
      renderEmpty('Выберите машину.');
      return;
    }
    if (!window.echarts) {
      renderFields();
      renderEmpty('Не удалось инициализировать график.');
      return;
    }
    meta.textContent = 'Загрузка графика…';
    chartEl.setAttribute('aria-busy', 'true');
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
      state.catalog = (await catalogRes.json()).sources || [];
      if (token !== loadToken) return;
      renderFields();
      const seriesRes = await fetch(root.dataset.seriesUrl + '?' + params(stamp).toString(), fetchOpts);
      if (token !== loadToken) return;
      if (!seriesRes.ok) throw new Error('series');
      setFullData(await seriesRes.json());
      chartEl.removeAttribute('aria-busy');
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      if (token !== loadToken) return;
      renderEmpty('Не удалось загрузить график.');
    } finally {
      if (activeFetch === ctl) activeFetch = null;
    }
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
    localStorage.setItem('bb-charts-columns', JSON.stringify(columnPick));
    openPicker = true;
    renderFields();
    reloadView();
  }

  let vmClickAt = 0;

  function applyVm(id) {
    if (!id) return;
    setRadio('vm_id', id);
    localStorage.setItem('bb-charts-vm', id);
    openPicker = false;
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
      localStorage.setItem('bb-charts-tab', target.value);
      openPicker = false;
      renderFields();
      reloadView();
      return;
    }
    if (target.name === 'date_from' || target.name === 'date_to') reloadView();
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
    if (!openPicker || (target && target.closest('#bb-chart-fields'))) return;
    openPicker = false;
    renderFields();
  });

  const refresh = document.getElementById('bb-chart-refresh');
  if (refresh) refresh.addEventListener('click', function () { reloadView(); });

  window.addEventListener('bb-hub-event', function (event) {
    const message = event.detail || {};
    if (message.type === 'delta' && message.topic === 'tags') appendPoint(message.payload || {});
  });
  window.addEventListener('resize', function () {
    if (state.chart) state.chart.resize();
  });

  reloadView();
})();
