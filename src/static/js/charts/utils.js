(function () {
    window.BlackBoxCharts = window.BlackBoxCharts || {};
    const ns = window.BlackBoxCharts;

    ns.formatAxisTime = function formatAxisTime(ms, timezone) {
        try {
            return new Intl.DateTimeFormat("ru-RU", {
                timeZone: timezone,
                day: "2-digit",
                month: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
            }).format(new Date(ms));
        } catch (_e) {
            return new Date(ms).toLocaleString("ru-RU");
        }
    };

    ns.formatTooltipTime = function formatTooltipTime(ms, timezone) {
        try {
            return new Intl.DateTimeFormat("ru-RU", {
                timeZone: timezone,
                day: "2-digit",
                month: "2-digit",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
            }).format(new Date(ms));
        } catch (_e) {
            return new Date(ms).toLocaleString("ru-RU");
        }
    };

    ns.coerceAnalog = function coerceAnalog(v) {
        if (v == null || v === "") return null;
        const n = Number(v);
        return isFinite(n) ? n : null;
    };

    ns.coerceDiscrete = function coerceDiscrete(v) {
        if (v == null || v === "") return null;
        return v ? 1 : 0;
    };

    ns.yAxisConfig = function yAxisConfig(table, points, columns) {
        if (table === "discrete") {
            return { type: "value", min: -0.05, max: 1.05, interval: 1, scale: false };
        }
        let minV = Infinity;
        let maxV = -Infinity;
        points.forEach((p) => {
            columns.forEach((col) => {
                const v = ns.coerceAnalog(p.values[col]);
                if (v != null) {
                    minV = Math.min(minV, v);
                    maxV = Math.max(maxV, v);
                }
            });
        });
        if (!isFinite(minV) || !isFinite(maxV)) {
            return { type: "value", scale: true };
        }
        const span = maxV - minV;
        const pad = span > 0 ? span * 0.08 : (Math.abs(maxV) > 1e-9 ? Math.abs(maxV) * 0.05 : 0.1);
        return { type: "value", scale: true, min: minV - pad, max: maxV + pad };
    };
})();
