// Client-side callbacks: no server round-trip for theme or the "updated N s ago" pill.
window.dash_clientside = Object.assign({}, window.dash_clientside, {
  aranet: {
    toggleTheme: function (nClicks, current) {
      let theme = current;
      if (!theme) {
        theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark" : "light";
      }
      if (nClicks) {
        theme = theme === "dark" ? "light" : "dark";
      }
      document.documentElement.dataset.theme = theme;
      return theme;
    },
    freshness: function (_tick, latest) {
      if (!latest || !latest.ts) {
        return ["No data yet", "pill pill-stale"];
      }
      const age = Math.max(0, Math.round(Date.now() / 1000 - latest.ts));
      let txt;
      if (age < 90) txt = age + " s ago";
      else if (age < 5400) txt = Math.round(age / 60) + " min ago";
      else if (age < 172800) txt = Math.round(age / 3600) + " h ago";
      else txt = Math.round(age / 86400) + " d ago";
      // Allow for a missed measurement or two at the device's logging interval.
      const staleAfter = Math.max(300, 2.5 * (latest.interval || 60));
      if (age <= staleAfter) return ["● Live · measured " + txt, "pill pill-live"];
      return ["■ Stale · last reading " + txt, "pill pill-stale"];
    },
  },
});
