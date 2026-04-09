(function () {
    window.BlackBoxCharts = window.BlackBoxCharts || {};
    const ns = window.BlackBoxCharts;

    ns.buildQuery = function buildQuery(state, includeSince) {
        const table = state.form.querySelector('[name="table"]').value;
        const q = new URLSearchParams();
        q.set("table", table);
        q.set("date_from", state.form.querySelector('[name="date_from"]').value);
        q.set("date_to", state.form.querySelector('[name="date_to"]').value);
        const selected = ns.selectedColumns(state.form, table);
        const jsonCols = JSON.stringify(selected);
        const encoded = btoa(unescape(encodeURIComponent(jsonCols)))
            .replace(/\+/g, "-")
            .replace(/\//g, "_")
            .replace(/=+$/g, "");
        q.set("selected_col_b64", encoded);
        if (includeSince && state.lastTs) q.set("since", state.lastTs);
        return q;
    };

    ns.fetchInit = function fetchInit(state) {
        const query = ns.buildQuery(state, false);
        fetch(`${state.initUrl}?${query.toString()}`)
            .then((r) => r.json())
            .then((payload) => {
                ns.setFullData(state, payload);
                ns.restartPolling(state);
            });
    };

    ns.fetchUpdate = function fetchUpdate(state) {
        const query = ns.buildQuery(state, true);
        fetch(`${state.updateUrl}?${query.toString()}`)
            .then((r) => r.json())
            .then((payload) => {
                ns.appendUpdates(state, payload);
            });
    };

    ns.restartPolling = function restartPolling(state) {
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
        const df = state.form.querySelector('[name="date_from"]').value;
        const dt = state.form.querySelector('[name="date_to"]').value;
        const table = state.form.querySelector('[name="table"]').value;
        const cols = ns.selectedColumns(state.form, table);
        if (!df && !dt && cols.length > 0) {
            state.pollTimer = setInterval(() => ns.fetchUpdate(state), 1000);
        }
    };
})();
