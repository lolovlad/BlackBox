(function () {
    window.BlackBoxCharts = window.BlackBoxCharts || {};
    const ns = window.BlackBoxCharts;

    const pageRoot = document.getElementById("charts-page-root");
    if (!pageRoot) {
        return;
    }

    const initUrl = pageRoot.dataset.initUrl || "";
    const updateUrl = pageRoot.dataset.updateUrl || "";
    const appTimezone = pageRoot.dataset.appTimezone || "UTC";
    if (!initUrl || !updateUrl) {
        return;
    }

    const state = {
        initUrl,
        updateUrl,
        appTimezone,
        panel: document.getElementById("chart-filters-panel"),
        overlay: document.getElementById("chart-filters-overlay"),
        form: document.getElementById("chart-form"),
        chartMeta: document.getElementById("chart-meta"),
        chartEl: document.getElementById("echarts-main"),
        chart: null,
        lastTs: null,
        pollTimer: null,
        lastTable: "analog",
        lastColumns: [],
    };

    document.getElementById("btn-open-filters").addEventListener("click", () => ns.toggleFilters(state, true));
    document.getElementById("btn-close-filters").addEventListener("click", () => ns.toggleFilters(state, false));
    state.overlay.addEventListener("click", () => ns.toggleFilters(state, false));
    document.getElementById("btn-render-chart").addEventListener("click", () => {
        ns.toggleFilters(state, false);
        ns.fetchInit(state);
    });

    document.getElementById("btn-reset-filters").addEventListener("click", () => {
        ns.resetFilters(state);
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
        state.lastTs = null;
        ns.renderEmpty(state, 'График пуст. Выберите поля и нажмите "Построить график".');
    });

    state.form.addEventListener("click", (ev) => {
        const btn = ev.target.closest(".btn-fields");
        if (!btn) return;
        const target = btn.getAttribute("data-target");
        const action = btn.getAttribute("data-action");
        state.form.querySelectorAll(`input[name="${target}"]`).forEach((el) => {
            if (el.closest(".chk") && el.closest(".chk").style.display === "none") return;
            el.checked = action === "all";
        });
    });

    state.form.querySelectorAll(".field-search").forEach((input) => {
        input.addEventListener("input", () => {
            ns.filterFieldList(input.getAttribute("data-target"), input.value, state.form);
        });
    });

    document.getElementById("chart-table").addEventListener("change", ns.syncColPanels);
    window.addEventListener("resize", () => {
        if (state.chart) state.chart.resize();
    });
    ns.syncColPanels();
})();
