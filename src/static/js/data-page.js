window.dataPage = function dataPage() {
    return {
        exportModalOpen: false,
        exportLoading: false,
        lastActiveTab: null,
        init() {
            this.syncColPanels();
            this.syncExportPanels();

            document.body.addEventListener("htmx:beforeRequest", (evt) => {
                const t = evt.detail.target;
                if (t && t.id === "tab-panel") {
                    this.syncColPanels();
                    const fd = new FormData(document.getElementById("data-filter-form"));
                    const tab = fd.get("active_tab");
                    if (this.lastActiveTab !== null && this.lastActiveTab !== tab) {
                        document.getElementById("data-page-num").value = "1";
                    }
                    this.lastActiveTab = tab;
                }
            });
            document.body.addEventListener("htmx:afterSwap", (evt) => {
                if (evt.detail.target && evt.detail.target.id === "tab-panel") {
                    const meta = document.getElementById("data-table-result");
                    if (meta && meta.dataset.page) {
                        document.getElementById("data-page-num").value = meta.dataset.page;
                    }
                }
            });
            ["sort", "date_from", "date_to"].forEach((name) => {
                const el = document.querySelector(`#data-filter-form [name="${name}"]`);
                if (el) {
                    el.addEventListener("change", () => {
                        document.getElementById("data-page-num").value = "1";
                    });
                }
            });
            document.body.addEventListener("click", (ev) => {
                const pageInp = document.getElementById("data-page-num");
                const refreshBtn = document.getElementById("btn-refresh-table");
                if (!pageInp || !refreshBtn) {
                    return;
                }
                if (ev.target.closest(".btn-page-prev")) {
                    const p = parseInt(pageInp.value, 10) || 1;
                    if (p > 1) {
                        pageInp.value = String(p - 1);
                        refreshBtn.click();
                    }
                } else if (ev.target.closest(".btn-page-next")) {
                    const meta = document.getElementById("data-table-result");
                    const maxP = parseInt(meta && meta.dataset.totalPages ? meta.dataset.totalPages : "1", 10);
                    const p2 = parseInt(pageInp.value, 10) || 1;
                    if (p2 < maxP) {
                        pageInp.value = String(p2 + 1);
                        refreshBtn.click();
                    }
                }
            });
            document
                .querySelectorAll('#export-form input[name="table_analog"], #export-form input[name="table_discrete"], #export-form input[name="table_alarms"]')
                .forEach((el) => el.addEventListener("change", () => this.syncExportPanels()));
        },
        syncColPanels() {
            const tab = document.querySelector('input[name="active_tab"]:checked');
            const root = document.getElementById("filter-root");
            if (tab && root) {
                root.dataset.cols = tab.value;
            }
            const t = tab ? tab.value : "analog";
            document.querySelectorAll('.col-panel-analog input[name="analog_col"]').forEach((i) => {
                i.disabled = t !== "analog";
            });
            document.querySelectorAll('.col-panel-discrete input[name="discrete_col"]').forEach((i) => {
                i.disabled = t !== "discrete";
            });
        },
        resetFiltersToDefault() {
            const f = document.getElementById("data-filter-form");
            const df = f.querySelector('[name="date_from"]');
            const dt = f.querySelector('[name="date_to"]');
            if (df) {
                df.value = "";
            }
            if (dt) {
                dt.value = "";
            }
            const sort = f.querySelector('[name="sort"]');
            if (sort) {
                sort.value = "desc";
            }
            document.getElementById("data-page-num").value = "1";
            const analogTab = f.querySelector('input[name="active_tab"][value="analog"]');
            if (analogTab) {
                analogTab.checked = true;
            }
            f.querySelectorAll('input[name="analog_col"]').forEach((c) => {
                c.checked = true;
            });
            f.querySelectorAll('input[name="discrete_col"]').forEach((c) => {
                c.checked = true;
            });
            this.syncColPanels();
            document.getElementById("btn-refresh-table").click();
        },
        toggleExportModal(open) {
            this.exportModalOpen = !!open;
        },
        openExportModal() {
            const dataForm = document.getElementById("data-filter-form");
            document.getElementById("export-date-from").value = dataForm.querySelector('[name="date_from"]').value || "";
            document.getElementById("export-date-to").value = dataForm.querySelector('[name="date_to"]').value || "";
            document.getElementById("export-sort").value = dataForm.querySelector('[name="sort"]').value || "desc";
            document.getElementById("export-outcome").innerHTML = "";
            this.syncExportPanels();
            this.toggleExportModal(true);
        },
        syncExportPanels() {
            const analogOn = !!document.querySelector('#export-form input[name="table_analog"]:checked');
            const discreteOn = !!document.querySelector('#export-form input[name="table_discrete"]:checked');
            const alarmsOn = !!document.querySelector('#export-form input[name="table_alarms"]:checked');
            const setPanel = (selector, enabled) => {
                document.querySelectorAll(selector).forEach((el) => {
                    el.style.display = enabled ? "" : "none";
                    el.querySelectorAll("input").forEach((i) => {
                        i.disabled = !enabled;
                    });
                });
            };
            setPanel("#export-col-root .col-panel-analog", analogOn);
            setPanel("#export-col-root .col-panel-discrete", discreteOn);
            setPanel("#export-col-root .col-panel-alarms", alarmsOn);
        },
        submitExport() {
            this.exportLoading = true;
            const exportOutcome = document.getElementById("export-outcome");
            exportOutcome.innerHTML = "";
            const formEl = document.getElementById("export-form");
            const df = (formEl.querySelector('[name="date_from"]').value || "").trim();
            const dt = (formEl.querySelector('[name="date_to"]').value || "").trim();
            if (!df || !dt) {
                exportOutcome.innerHTML = '<div class="flash error">Для экспорта укажите дату/время начала и конца.</div>';
                this.exportLoading = false;
                return;
            }
            const pageRoot = document.getElementById("data-page-root");
            const exportUrl = pageRoot ? pageRoot.dataset.exportUrl : "";
            fetch(exportUrl, {
                method: "POST",
                body: new FormData(formEl),
            })
                .then((resp) => resp.text())
                .then((html) => {
                    exportOutcome.innerHTML = html;
                })
                .catch((err) => {
                    exportOutcome.innerHTML = `<div class="flash error">Ошибка экспорта: ${String(err)}</div>`;
                })
                .finally(() => {
                    this.exportLoading = false;
                });
        },
    };
};
