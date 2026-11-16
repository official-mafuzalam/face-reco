// Live in-app notifications: toasts, desktop pop-ups, sound and a full-screen alarm for critical alerts.
// Works offline: everything comes from this app's own /notify/stream (Server-Sent Events).
(() => {
  "use strict";
  const root = document.getElementById("notify-root");
  if (!root || !window.EventSource) return;

  const text = JSON.parse(root.dataset.text);
  const lang = document.documentElement.lang === "bn" ? "bn" : "en";
  const KEY_LAST = "gf-notify-last";
  const KEY_PENDING = "gf-notify-pending";
  const KEY_SETTINGS = "gf-notify-settings";
  const KEY_PLAYED = "gf-notify-played";
  const MAX_TOASTS = 4;

  const store = {
    get(area, key, fallback) {
      try { const value = area.getItem(key); return value === null ? fallback : JSON.parse(value); } catch (_) { return fallback; }
    },
    set(area, key, value) { try { area.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode */ } },
  };
  const settings = Object.assign(
    { soundWarning: true, soundCritical: true, desktop: false },
    store.get(localStorage, KEY_SETTINGS, {}),
  );
  const channel = window.BroadcastChannel ? new BroadcastChannel("gf-notify") : null;

  // ---------------------------------------------------------------- sound
  let audio = null;
  let siren = null;
  const unlockAudio = () => {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === "suspended") audio.resume();
    } catch (_) { audio = null; }
  };
  // Browsers only allow sound after the user has interacted with the page once.
  ["pointerdown", "keydown"].forEach((name) => document.addEventListener(name, unlockAudio, { once: true, capture: true }));

  const tone = (frequency, start, length) => {
    const oscillator = audio.createOscillator();
    const gain = audio.createGain();
    oscillator.type = "square";
    oscillator.frequency.value = frequency;
    gain.gain.value = 0.08;
    oscillator.connect(gain).connect(audio.destination);
    oscillator.start(audio.currentTime + start);
    oscillator.stop(audio.currentTime + start + length);
  };
  const beep = () => { if (audio && audio.state === "running") { tone(880, 0, 0.12); tone(880, 0.2, 0.12); } };
  const startSiren = () => {
    if (siren || !audio) return;
    const cycle = () => { if (audio.state === "running") { tone(700, 0, 0.35); tone(1050, 0.4, 0.35); } };
    cycle();
    siren = setInterval(cycle, 900);
  };
  const stopSiren = () => { clearInterval(siren); siren = null; };

  // Only one tab plays the sound for a given notification.
  const claimSound = (id) => {
    const played = store.get(localStorage, KEY_PLAYED, 0);
    if (played >= id) return false;
    store.set(localStorage, KEY_PLAYED, id);
    return true;
  };

  // ---------------------------------------------------------------- toasts
  const stack = document.createElement("div");
  stack.className = "toast-stack";
  stack.setAttribute("aria-live", "polite");
  document.body.appendChild(stack);

  const timeLabel = (iso) => new Date(iso).toLocaleTimeString(lang === "bn" ? "bn-BD" : undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  const toast = (item) => {
    const card = document.createElement(item.link ? "a" : "div");
    card.className = `toast toast-${item.severity}`;
    if (item.link) card.href = `${item.link}${item.link.includes("?") ? "&" : "?"}lang=${lang}`;
    if (item.image) {
      const image = document.createElement("img");
      image.src = item.image;
      image.alt = "";
      card.appendChild(image);
    }
    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.title[lang];
    const detail = document.createElement("span");
    detail.textContent = `${item.body[lang]} · ${timeLabel(item.at)}`;
    body.append(title, detail);
    card.appendChild(body);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", text.close);
    close.textContent = "×";
    close.addEventListener("click", (event) => { event.preventDefault(); card.remove(); });
    card.appendChild(close);
    stack.prepend(card);
    while (stack.children.length > MAX_TOASTS) stack.lastChild.remove();
    setTimeout(() => card.remove(), item.severity === "info" ? 8000 : 20000);
  };

  // ---------------------------------------------------------------- desktop pop-ups
  const desktop = (item) => {
    if (!settings.desktop || !("Notification" in window) || Notification.permission !== "granted") return;
    if (!document.hidden) return; // the toast is already visible
    const popup = new Notification(item.title[lang], {
      body: item.body[lang],
      icon: item.image || undefined,
      tag: `gf-${item.id}`, // the same alert from several tabs shows once
      requireInteraction: item.severity === "critical",
    });
    popup.onclick = () => { window.focus(); if (item.link) location.href = item.link; popup.close(); };
  };

  // ---------------------------------------------------------------- critical alarm
  let overlay = null;
  const pending = () => store.get(sessionStorage, KEY_PENDING, []);
  const savePending = (items) => store.set(sessionStorage, KEY_PENDING, items.slice(-10));

  const renderAlarm = () => {
    const items = pending();
    if (!items.length) { overlay?.remove(); overlay = null; stopSiren(); return; }
    const item = items[items.length - 1];
    overlay?.remove();
    overlay = document.createElement("div");
    overlay.className = "alarm-overlay";
    overlay.setAttribute("role", "alertdialog");
    overlay.setAttribute("aria-modal", "true");
    const box = document.createElement("div");
    box.className = "alarm-box";
    const heading = document.createElement("h2");
    heading.textContent = item.title[lang];
    const detail = document.createElement("p");
    detail.textContent = `${item.body[lang]} · ${timeLabel(item.at)}`;
    box.append(heading, detail);
    if (item.image) {
      const image = document.createElement("img");
      image.src = item.image;
      image.alt = "";
      box.appendChild(image);
    }
    if (items.length > 1) {
      const more = document.createElement("p");
      more.textContent = text.more.replace("{n}", items.length - 1);
      box.appendChild(more);
    }
    if (!audio || audio.state !== "running") {
      const hint = document.createElement("p");
      hint.className = "alarm-hint";
      hint.textContent = text.enableSound;
      box.appendChild(hint);
    }
    const actions = document.createElement("div");
    actions.className = "actions";
    const ack = document.createElement("button");
    ack.type = "button";
    ack.className = "btn-danger";
    ack.textContent = text.ack;
    ack.addEventListener("click", () => acknowledge(item));
    actions.appendChild(ack);
    if (item.link) {
      const open = document.createElement("a");
      open.className = "btn btn-secondary";
      open.href = `${item.link}${item.link.includes("?") ? "&" : "?"}lang=${lang}`;
      open.textContent = text.open;
      actions.appendChild(open);
    }
    box.appendChild(actions);
    overlay.appendChild(box);
    overlay.addEventListener("click", () => { unlockAudio(); if (settings.soundCritical) startSiren(); });
    document.body.appendChild(overlay);
    ack.focus();
    if (settings.soundCritical) startSiren();
  };

  const dropPending = (id) => {
    savePending(pending().filter((entry) => entry.id !== id));
    renderAlarm();
  };

  const acknowledge = async (item) => {
    if (item.ack) {
      try {
        await fetch("/notify/ack", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(item.ack),
          credentials: "same-origin",
        });
      } catch (_) { /* still close locally; it stays unreviewed on the alerts page */ }
    }
    dropPending(item.id);
    channel?.postMessage({ ack: item.id });
  };
  channel?.addEventListener("message", (event) => { if (event.data?.ack) dropPending(event.data.ack); });

  // ---------------------------------------------------------------- stream
  const handle = (item) => {
    store.set(sessionStorage, KEY_LAST, Math.max(item.id, store.get(sessionStorage, KEY_LAST, 0)));
    if (item.severity === "critical") {
      savePending([...pending().filter((entry) => entry.id !== item.id), item]);
      renderAlarm();
    } else {
      toast(item);
    }
    desktop(item);
    if (claimSound(item.id) && item.severity === "warning" && settings.soundWarning) beep();
    document.dispatchEvent(new CustomEvent("gf:notify", { detail: item }));
  };

  const connect = () => {
    const since = store.get(sessionStorage, KEY_LAST, null);
    const source = new EventSource(`/notify/stream${since === null ? "" : `?since=${since}`}`);
    source.addEventListener("notify", (event) => {
      try { handle(JSON.parse(event.data)); } catch (_) { /* ignore a malformed message */ }
    });
    // EventSource reconnects by itself (and sends Last-Event-ID); a closed stream (e.g. logged out) stays closed.
    source.addEventListener("error", () => { if (source.readyState === EventSource.CLOSED) setTimeout(connect, 10000); });
  };
  renderAlarm();
  connect();

  // ---------------------------------------------------------------- settings panel
  const button = document.getElementById("notify-settings-button");
  const panel = document.getElementById("notify-settings");
  if (button && panel) {
    const bind = (name, onEnable) => {
      const input = panel.querySelector(`[name="${name}"]`);
      input.checked = settings[name];
      input.addEventListener("change", async () => {
        settings[name] = input.checked;
        if (input.checked && onEnable) input.checked = settings[name] = await onEnable();
        store.set(localStorage, KEY_SETTINGS, settings);
        unlockAudio();
      });
    };
    bind("soundWarning");
    bind("soundCritical");
    bind("desktop", async () => {
      if (!("Notification" in window)) { panel.querySelector(".notify-desktop-note").textContent = text.desktopUnsupported; return false; }
      const result = await Notification.requestPermission();
      if (result !== "granted") panel.querySelector(".notify-desktop-note").textContent = text.desktopBlocked;
      return result === "granted";
    });
    panel.querySelector("[data-test-sound]")?.addEventListener("click", () => {
      unlockAudio();
      setTimeout(beep, 50);
    });
    button.addEventListener("click", () => {
      const open = panel.hidden;
      panel.hidden = !open;
      button.setAttribute("aria-expanded", String(open));
      unlockAudio();
    });
    document.addEventListener("click", (event) => {
      if (!panel.hidden && !panel.contains(event.target) && event.target !== button && !button.contains(event.target)) {
        panel.hidden = true;
        button.setAttribute("aria-expanded", "false");
      }
    });
  }
})();
