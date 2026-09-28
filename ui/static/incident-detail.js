(function () {
  const root = document.getElementById('bb-incident-detail');
  const chartEl = document.getElementById('bb-incident-chart');
  const meta = document.getElementById('bb-incident-chart-meta');
  if (!root || !chartEl || !meta || !window.echarts) return;
  const chart = echarts.init(chartEl);
  const vmId = root.dataset.vmId;
  const params = new URLSearchParams({ vm_id: vmId, date_from: root.dataset.telemetryFrom, date_to: root.dataset.telemetryTo });

  fetch('/api/v1/telemetry/series?' + params.toString(), { credentials: 'same-origin' })
    .then(function (response) {
      if (!response.ok) throw new Error('series');
      return response.json();
    })
    .then(function (payload) {
      if (!payload.points || !payload.points.length || !payload.columns || !payload.columns.length) {
        meta.textContent = 'За это окно телеметрия не найдена.';
        chartEl.removeAttribute('aria-busy');
        return;
      }
      const labels = payload.column_labels || {};
      const series = payload.columns.map(function (column) {
        return {
          name: labels[column] || column,
          type: 'line',
          showSymbol: false,
          connectNulls: false,
          data: payload.points.map(function (point) {
            const value = point.values[column];
            return [point.ts_ms, value == null || value === '' ? null : Number(value)];
          }),
        };
      });
      chart.setOption({
        tooltip: { trigger: 'axis', confine: true },
        legend: { type: 'scroll' },
        grid: { left: 64, right: 24, top: 48, bottom: 70 },
        xAxis: { type: 'time' },
        yAxis: { type: 'value', scale: true },
        dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 8 }],
        series: series,
      });
      meta.textContent = 'Точек: ' + payload.row_count + '. Данные графика входят в ZIP экспорт.';
      chartEl.removeAttribute('aria-busy');
    })
    .catch(function () {
      meta.textContent = 'Не удалось загрузить телеметрию инцидента.';
      chartEl.removeAttribute('aria-busy');
    });

  window.addEventListener('resize', function () { chart.resize(); });
})();
