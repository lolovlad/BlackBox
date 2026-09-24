(function () {
  const root = document.getElementById('bb-charts');
  const form = document.getElementById('bb-chart-form');
  const fieldHost = document.getElementById('bb-chart-fields');
  const meta = document.getElementById('bb-chart-meta');
  const chartEl = document.getElementById('bb-echarts');
  if (!root || !form || !fieldHost || !meta || !chartEl || !window.echarts) return;

  const state = { chart: null, table: 'analog', columns: [], live: false, lastTs: 0, catalog: [] };

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function selectedVms() {
    return Array.from(form.querySelectorAll('input[name="vm_id"]:checked')).map(function (node) { return node.value; });
  }

  function tableName() {
    return form.querySelector('[name="table"]').value === 'discrete' ? 'discrete' : 'analog';
  }

  function selectedFields() {
    const name = tableName() === 'discrete' ? 'discrete_col' : 'analog_col';
    return Array.from(form.querySelectorAll('input[name="' + name + '"]:checked')).map(function (node) { return node.value; });
  }

  function datesOpen() {
    return !form.querySelector('[name="date_from"]').value && !form.querySelector('[name="date_to"]').value;
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

  function renderEmpty(message) {
    if (state.chart) state.chart.clear();
    chartEl.hidden = true;
    meta.textContent = message;
    state.lastTs = 0;
    state.live = false;
  }

  function setFullData(payload) {
    state.table = payload.table || 'analog';
    state.columns = payload.columns || [];
    if (!state.columns.length) {
      renderEmpty('График пуст. Выберите поля и нажмите «Построить график».');
      return;
    }
    if (!payload.points || !payload.points.length) {
      renderEmpty('Нет данных за выбранный период.');
      state.table = payload.table || state.table;
      state.columns = payload.columns || [];
      state.live = datesOpen() && state.columns.length > 0;
      return;
    }
    chartEl.hidden = false;
    meta.textContent = 'Точек: ' + payload.row_count + (payload.realtime ? '. Online: текущие сутки, дальше по WebSocket.' : '.');
    const labels = payload.column_labels || {};
    const series = state.columns.map(function (column) {
      return {
        id: 'chart-col:' + column,
        name: labels[column] || column,
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
    if (!state.chart) state.chart = echarts.init(chartEl);
    state.chart.setOption({
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
  }

  function appendPoint(sample) {
    if (!state.live || !sample || sample.quality === 'bad') return;
    if (!selectedVms().includes(String(sample.vm_id))) return;
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
    chartEl.hidden = false;
    meta.textContent = 'Online. Последняя точка ' + formatTooltipTime(ts) + '.';
  }

  async function loadFields() {
    const query = new URLSearchParams();
    selectedVms().forEach(function (id) { query.append('vm_id', id); });
    const response = await fetch('/api/v1/telemetry/catalog?' + query.toString());
    if (!response.ok) return;
    const payload = await response.json();
    state.catalog = payload.sources || [];
    const active = tableName();
    const seen = new Map();
    state.catalog.forEach(function (source) {
      (source[active] || []).forEach(function (field) {
        if (!seen.has(field.key)) seen.set(field.key, field.label || field.key);
      });
    });
    const previous = new Set(selectedFields());
    const first = !fieldHost.querySelector('input[name$="_col"]');
    const inputName = active === 'discrete' ? 'discrete_col' : 'analog_col';
    fieldHost.innerHTML = '<fieldset><legend>' + (active === 'analog' ? 'Поля (аналоги)' : 'Поля (дискреты)') + '</legend>' +
      '<div class="bb-field-tools"><input type="search" data-field-search placeholder="Поиск поля..."><button type="button" class="bb-btn bb-btn-ghost" data-fields="all">Все</button><button type="button" class="bb-btn bb-btn-ghost" data-fields="none">Снять</button></div>' +
      '<div class="bb-check-grid">' + Array.from(seen.entries()).map(function (pair) {
        const checked = first || previous.size === 0 || previous.has(pair[0]) ? ' checked' : '';
        return '<label class="bb-chk" data-label="' + esc((pair[1] + ' ' + pair[0]).toLowerCase()) + '"><input type="checkbox" name="' + inputName + '" value="' + esc(pair[0]) + '"' + checked + '> ' + esc(pair[1]) + '</label>';
      }).join('') + '</div></fieldset>';
  }

  async function renderChart() {
    const fields = selectedFields();
    const button = document.getElementById('bb-chart-render');
    if (!fields.length) {
      renderEmpty('График пуст. Выберите поля и нажмите «Построить график».');
      return;
    }
    button.disabled = true;
    meta.textContent = 'Подождите. Идёт построение графиков…';
    const query = new URLSearchParams();
    query.set('table', tableName());
    selectedVms().forEach(function (id) { query.append('vm_id', id); });
    fields.forEach(function (field) { query.append('column', field); });
    const from = form.querySelector('[name="date_from"]').value;
    const to = form.querySelector('[name="date_to"]').value;
    if (from) query.set('date_from', from);
    if (to) query.set('date_to', to);
    try {
      const response = await fetch(root.dataset.seriesUrl + '?' + query.toString());
      if (!response.ok) throw new Error('series');
      setFullData(await response.json());
    } catch (_error) {
      renderEmpty('Не удалось построить график: ошибка загрузки данных.');
    } finally {
      button.disabled = false;
    }
  }

  form.addEventListener('change', function (event) {
    const target = event.target;
    if (target && (target.name === 'table' || target.name === 'vm_id')) loadFields();
  });
  fieldHost.addEventListener('input', function (event) {
    if (!event.target.matches('[data-field-search]')) return;
    const needle = event.target.value.trim().toLowerCase();
    fieldHost.querySelectorAll('[data-label]').forEach(function (label) {
      label.hidden = needle && label.dataset.label.indexOf(needle) === -1;
    });
  });
  fieldHost.addEventListener('click', function (event) {
    const button = event.target.closest('[data-fields]');
    if (!button) return;
    fieldHost.querySelectorAll('input[type="checkbox"]').forEach(function (input) {
      const label = input.closest('[data-label]');
      if (label && label.hidden) return;
      input.checked = button.dataset.fields === 'all';
    });
  });
  document.getElementById('bb-chart-render').addEventListener('click', renderChart);
  document.getElementById('bb-chart-reset').addEventListener('click', function () {
    form.reset();
    form.querySelectorAll('input[name="vm_id"]').forEach(function (input) { input.checked = true; });
    loadFields().then(renderChart);
  });
  window.addEventListener('bb-hub-event', function (event) {
    const message = event.detail || {};
    if (message.type === 'delta' && message.topic === 'tags') appendPoint(message.payload || {});
  });
  window.addEventListener('resize', function () {
    if (state.chart) state.chart.resize();
  });
  loadFields();
})();
