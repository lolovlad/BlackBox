(function () {
    window.BlackBoxCharts = window.BlackBoxCharts || {};
    const ns = window.BlackBoxCharts;

    ns.syncColPanels = function syncColPanels() {
        const t = document.getElementById("chart-table").value;
        const root = document.getElementById("chart-col-root");
        root.dataset.cols = t;
    };

    ns.filterFieldList = function filterFieldList(targetName, query, form) {
        const normalized = (query || "").trim().toLowerCase();
        form.querySelectorAll(`input[name="${targetName}"]`).forEach((input) => {
            const row = input.closest(".chk");
            if (!row) return;
            const text = row.getAttribute("data-label") || row.textContent.toLowerCase();
            row.style.display = !normalized || text.indexOf(normalized) !== -1 ? "" : "none";
        });
    };

    ns.toggleFilters = function toggleFilters(state, open) {
        if (!open && state.panel.contains(document.activeElement)) {
            document.activeElement.blur();
        }
        state.panel.classList.toggle("open", !!open);
        state.overlay.classList.toggle("open", !!open);
        state.panel.setAttribute("aria-hidden", open ? "false" : "true");
        state.overlay.setAttribute("aria-hidden", open ? "false" : "true");
    };

    ns.selectedColumns = function selectedColumns(form, table) {
        const out = [];
        if (table === "analog") {
            form.querySelectorAll('input[name="analog_col"]:checked').forEach((el) => out.push(el.value));
        } else {
            form.querySelectorAll('input[name="discrete_col"]:checked').forEach((el) => out.push(el.value));
        }
        return out;
    };

    ns.resetFilters = function resetFilters(state) {
        state.form.querySelector('[name="table"]').value = "analog";
        state.form.querySelector('[name="date_from"]').value = "";
        state.form.querySelector('[name="date_to"]').value = "";
        state.form.querySelectorAll('input[name="analog_col"]').forEach((el) => {
            el.checked = true;
        });
        state.form.querySelectorAll('input[name="discrete_col"]').forEach((el) => {
            el.checked = true;
        });
        state.form.querySelectorAll(".field-search").forEach((el) => {
            el.value = "";
        });
        ns.filterFieldList("analog_col", "", state.form);
        ns.filterFieldList("discrete_col", "", state.form);
        ns.syncColPanels();
    };
})();
