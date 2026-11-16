// Page-wide keyboard shortcuts. Media-viewer keys are handled first in viewer.js; anything it
// uses is marked defaultPrevented and ignored here. Nothing fires while typing in a field.
(() => {
  "use strict";
  const help = document.getElementById("shortcuts");
  const lang = document.documentElement.lang === "bn" ? "bn" : "en";
  const GO = { d: "/", l: "/live", e: "/events", w: "/alerts", i: "/incidents", t: "/attendance", s: "/faces/search" };
  const PREFIX_MS = 1500;
  let prefixUntil = 0;

  const editing = (target) =>
    target && (target.isContentEditable || (["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName) && target.type !== "range"));
  window.GateKeys = { prefixActive: () => Date.now() < prefixUntil };
  const go = (path) => { location.href = `${path}${path.includes("?") ? "&" : "?"}lang=${lang}`; };

  const toggleHelp = (show) => {
    if (!help) return;
    const open = show ?? !help.open;
    if (open && !help.open) help.showModal();
    if (!open && help.open) help.close();
  };
  help?.addEventListener("click", (event) => { if (event.target === help) help.close(); });

  window.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || editing(event.target) || event.ctrlKey || event.altKey || event.metaKey) return;
    if (document.querySelector(".alarm-overlay")) return; // the alarm has the keyboard
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;

    if (Date.now() < prefixUntil) {          // second key of "G then …"
      prefixUntil = 0;
      if (GO[key]) { event.preventDefault(); go(GO[key]); }
      return;
    }
    if (key === "?") { event.preventDefault(); toggleHelp(); return; }
    if (key === "g") { prefixUntil = Date.now() + PREFIX_MS; return; }
    if (key === "/") {
      const search = document.querySelector('main input[type="search"], main input[name="q"], main input[name="person"]');
      if (search) { event.preventDefault(); search.focus(); search.select?.(); }
      return;
    }

    // Live page: 1–9 opens that camera large, Escape returns to all cameras.
    if (location.pathname === "/live") {
      const cards = [...document.querySelectorAll('.live-card > a[href^="/live?camera="]')];
      if (/^[1-9]$/.test(key) && cards[Number(key) - 1]) { event.preventDefault(); location.href = cards[Number(key) - 1].href; return; }
      if (key === "Escape" && new URLSearchParams(location.search).has("camera")) { event.preventDefault(); go("/live"); }
    }
  });
})();
