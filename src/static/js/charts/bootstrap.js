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
        wheelFixInstalled: false,
    };

    document.getElementById("btn-open-filters").addEventListener("click", () => ns.toggleFilters(state, true));
    document.getElementById("btn-close-filters").addEventListener("click", () => ns.toggleFilters(state, false));
    state.overlay.addEventListener("click", () => ns.toggleFilters(state, false));
    const runBuild = () => {
        ns.toggleFilters(state, false);
        ns.fetchInit(state);
    };
    document.getElementById("btn-render-chart").addEventListener("click", runBuild);
    const applyBtn = document.getElementById("btn-apply-filters");
    if (applyBtn) applyBtn.addEventListener("click", runBuild);

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

    // Wheel UX: if tooltip is scrollable, wheel should scroll it (not zoom the chart).
    if (!state.wheelFixInstalled && state.chartEl) {
        state.wheelFixInstalled = true;
        state.chartEl.addEventListener(
            "wheel",
            (ev) => {
                if (!ev.ctrlKey) return;
                const tooltip = document.querySelector(".echarts-tooltip");
                if (!(tooltip instanceof HTMLElement)) return;
                // Tooltip has pointer-events:none, so wheel target is the chart.
                // We detect "mouse over tooltip" by coordinates.
                const rect = tooltip.getBoundingClientRect();
                const x = ev.clientX;
                const y = ev.clientY;
                const isOver =
                    x >= rect.left &&
                    x <= rect.right &&
                    y >= rect.top &&
                    y <= rect.bottom &&
                    rect.width > 0 &&
                    rect.height > 0;
                if (!isOver) return;

                // Ctrl+wheel over tooltip should scroll it (not zoom the chart).
                tooltip.scrollTop += ev.deltaY;
                ev.preventDefault();
                ev.stopPropagation();
            },
            { capture: true, passive: false }
        );
    }
    ns.syncColPanels();
})();
