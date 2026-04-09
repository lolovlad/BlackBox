(function () {
    window.BlackBoxCharts = window.BlackBoxCharts || {};
    const ns = window.BlackBoxCharts;

    ns.ensureChart = function ensureChart(state) {
        if (!state.chart) state.chart = echarts.init(state.chartEl);
        return state.chart;
    };

    ns.renderEmpty = function renderEmpty(state, msg) {
        if (state.chart) state.chart.clear();
        state.chartEl.style.display = "none";
        state.chartMeta.textContent = msg;
    };

    ns.setFullData = function setFullData(state, payload) {
        if (!payload.columns || payload.columns.length === 0) {
            ns.renderEmpty(state, 'График пуст. Выберите поля и нажмите "Построить график".');
            state.lastTs = null;
            return;
        }
        if (!payload.points || payload.points.length === 0) {
            ns.renderEmpty(state, "Нет данных за выбранный период.");
            state.lastTs = null;
            return;
        }

        state.chartEl.style.display = "";
        state.chartMeta.textContent = `Точек: ${payload.row_count}${payload.realtime ? ". Online: только текущие сутки (по часовому поясу приложения)." : "."}`;
        state.lastTable = payload.table || "analog";
        state.lastColumns = payload.columns.slice();
        const labels = payload.column_labels || {};
        const n = payload.points.length;
        const series = payload.columns.map((col) => ({
            id: `chart-col:${col}`,
            name: labels[col] || col,
            type: "line",
            showSymbol: n <= 120,
            symbolSize: 4,
            connectNulls: false,
            data: payload.points.map((p) => {
                const y = state.lastTable === "discrete" ? ns.coerceDiscrete(p.values[col]) : ns.coerceAnalog(p.values[col]);
                return [p.ts_ms, y];
            }),
        }));
        const yAxis = ns.yAxisConfig(state.lastTable, payload.points, payload.columns);
        ns.ensureChart(state).setOption(
            {
                tooltip: {
                    trigger: "axis",
                    axisPointer: { type: "cross", label: { backgroundColor: "#6a7985" } },
                    formatter(params) {
                        if (!params || !params.length) return "";
                        const t = params[0].axisValue;
                        const head = ns.formatTooltipTime(t, state.appTimezone);
                        const lines = [head];
                        const tbl = state.lastTable;
                        function fmtY(y) {
                            if (y == null || y === "" || (typeof y === "number" && !isFinite(y))) return "—";
                            if (tbl === "discrete") return String(Math.round(Number(y)));
                            let s = Number(y).toFixed(4).replace(/\.?0+$/, "");
                            if (s.endsWith(".")) s = s.slice(0, -1);
                            return s;
                        }
                        params.forEach((p) => {
                            const y = Array.isArray(p.value) && p.value.length > 1 ? p.value[1] : p.value;
                            lines.push(`${p.marker} ${p.seriesName}: ${fmtY(y)}`);
                        });
                        return lines.join("<br/>");
                    },
                },
                legend: { type: "scroll" },
                grid: { left: 92, right: 28, top: 48, bottom: 96 },
                xAxis: {
                    type: "time",
                    boundaryGap: false,
                    axisLabel: { hideOverlap: true, formatter: (v) => ns.formatAxisTime(v, state.appTimezone) },
                    splitLine: { show: false },
                },
                yAxis,
                dataZoom: [
                    { type: "inside", xAxisIndex: 0, filterMode: "none" },
                    { type: "inside", yAxisIndex: 0, filterMode: "none" },
                    { type: "slider", xAxisIndex: 0, height: 28, bottom: 12, labelFormatter: (v) => ns.formatAxisTime(v, state.appTimezone) },
                    {
                        type: "slider",
                        yAxisIndex: 0,
                        orient: "vertical",
                        filterMode: "none",
                        width: 22,
                        left: 6,
                        top: 52,
                        bottom: 100,
                        labelFormatter(v) {
                            const n = Number(v);
                            if (!isFinite(n)) return "";
                            const a = Math.abs(n);
                            if (a >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
                            if (a >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
                            if (a >= 100) return String(Math.round(n));
                            if (a >= 1) return n.toFixed(1);
                            return n.toFixed(2);
                        },
                    },
                ],
                series,
            },
            true
        );
        state.lastTs = payload.last_ts || null;
    };

    ns.appendUpdates = function appendUpdates(state, payload) {
        if (!payload.points || payload.points.length === 0 || !state.chart) return;
        const option = state.chart.getOption();
        if (!option || !option.series || option.series.length === 0) return;
        const cols = payload.columns || state.lastColumns;
        if (!cols.length) return;
        const tbl = payload.table || state.lastTable;
        const prefix = "chart-col:";
        payload.points.forEach((p) => {
            option.series.forEach((s) => {
                const sid = s.id != null ? String(s.id) : "";
                const col = sid.indexOf(prefix) === 0 ? sid.slice(prefix.length) : null;
                if (!col || !s.data) return;
                if (cols.indexOf(col) === -1) return;
                const y = tbl === "discrete" ? ns.coerceDiscrete(p.values[col]) : ns.coerceAnalog(p.values[col]);
                s.data.push([p.ts_ms, y]);
            });
        });
        state.chart.setOption({ series: option.series }, false);
        state.lastTs = payload.last_ts || state.lastTs;
    };
})();
