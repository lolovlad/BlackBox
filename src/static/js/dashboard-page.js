(() => {
    const form = document.getElementById("dashboard-live-form");
    const analogOff = document.getElementById("dashboard-analog-off-b64");
    const discreteOff = document.getElementById("dashboard-discrete-off-b64");
    if (!form || !analogOff || !discreteOff) {
        return;
    }

    const encodeUnchecked = (group) => {
        const unchecked = [];
        form.querySelectorAll(`input[type="checkbox"][data-col-group="${group}"]`).forEach((el) => {
            if (!el.checked) {
                unchecked.push(el.value);
            }
        });
        if (!unchecked.length) {
            return "";
        }
        return btoa(unescape(encodeURIComponent(JSON.stringify(unchecked))))
            .replace(/\+/g, "-")
            .replace(/\//g, "_")
            .replace(/=+$/g, "");
    };

    const syncEncodedFilters = () => {
        analogOff.value = encodeUnchecked("analog");
        discreteOff.value = encodeUnchecked("discrete");
    };

    form.addEventListener("change", syncEncodedFilters);
    document.body.addEventListener("htmx:configRequest", (evt) => {
        if (evt.detail && evt.detail.elt && evt.detail.elt.id === "dashboard-live-root") {
            syncEncodedFilters();
        }
    });
    syncEncodedFilters();
})();
