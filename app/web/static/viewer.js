// Media viewer for snapshots, incident clips and live video:
// zoom (wheel, buttons, double-click, pinch), pan, rotate, brightness/contrast, full screen,
// video speed / frame stepping, marking (box, arrow, pen) and saving the marked picture.
// Usage: <div data-viewer> containing one <img> or <video>  -> enhanced in place
//        <a href="photo.jpg" data-viewer-open>              -> opens the photo in a full-screen viewer
(() => {
  "use strict";
  const textHost = document.getElementById("viewer-text");
  const T = textHost ? JSON.parse(textHost.dataset.text) : {};
  const label = (key, fallback) => T[key] || fallback;
  const MIN_ZOOM = 1;
  const MAX_ZOOM = 8;
  const MARK_COLOUR = "#ff2d2d";
  const viewers = [];
  let activeViewer = null;
  let lightbox = null;

  const el = (tag, attrs = {}, children = []) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    children.forEach((child) => node.appendChild(child));
    return node;
  };
  const button = (text, title, onclick, extra = {}) =>
    el("button", { type: "button", class: "viewer-btn", title, "aria-label": title, text, onclick, ...extra });
  const clockText = (seconds) => {
    if (!Number.isFinite(seconds)) return "0:00";
    const whole = Math.floor(seconds);
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}.${Math.floor((seconds - whole) * 10)}`;
  };

  function createViewer(container, media, options = {}) {
    const isVideo = media.tagName === "VIDEO";
    const isLive = options.live === true;
    const state = { scale: 1, x: 0, y: 0, rotate: 0, brightness: 100, contrast: 100, tool: "pan", shapes: [], draft: null };
    media.removeAttribute("controls");
    media.classList.add("viewer-media");
    media.draggable = false;

    const marks = el("canvas", { class: "viewer-marks" });
    const content = el("div", { class: "viewer-content" });
    media.replaceWith(content);
    content.append(media, marks);
    const stage = el("div", { class: "viewer-stage", tabindex: "0" }, [content]);
    const zoomLabel = el("span", { class: "viewer-zoom", text: "100%" });
    const root = el("div", { class: "viewer" }, [stage]);
    container.appendChild(root);

    // ---------------------------------------------------------- geometry
    const fitted = () => {
      const width = media.naturalWidth || media.videoWidth || 16;
      const height = media.naturalHeight || media.videoHeight || 9;
      const box = stage.getBoundingClientRect();
      const ratio = Math.min(box.width / width, (box.height || box.width * height / width) / height);
      return { width: width * ratio, height: height * ratio, naturalWidth: width, naturalHeight: height };
    };
    const layout = () => {
      const size = fitted();
      content.style.width = `${size.width}px`;
      content.style.height = `${size.height}px`;
      const box = stage.getBoundingClientRect();
      content.style.left = `${(box.width - size.width) / 2}px`;
      content.style.top = `${(box.height - size.height) / 2}px`;
      const dpr = window.devicePixelRatio || 1;
      marks.width = Math.round(size.width * dpr);
      marks.height = Math.round(size.height * dpr);
      apply();
    };
    const clampPan = () => {
      const size = fitted();
      const limitX = (size.width * (state.scale - 1)) / 2 + 40;
      const limitY = (size.height * (state.scale - 1)) / 2 + 40;
      state.x = Math.max(-limitX, Math.min(limitX, state.x));
      state.y = Math.max(-limitY, Math.min(limitY, state.y));
    };
    const apply = () => {
      clampPan();
      content.style.transform = `translate(${state.x}px, ${state.y}px) scale(${state.scale}) rotate(${state.rotate}deg)`;
      media.style.filter = `brightness(${state.brightness}%) contrast(${state.contrast}%)`;
      zoomLabel.textContent = `${Math.round(state.scale * 100)}%`;
      stage.classList.toggle("is-zoomed", state.scale > 1);
      drawMarks();
    };
    const zoomAt = (factor, clientX, clientY) => {
      const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, state.scale * factor));
      const box = stage.getBoundingClientRect();
      // Keep the point under the cursor where it is while zooming.
      const px = (clientX ?? box.left + box.width / 2) - (box.left + box.width / 2) - state.x;
      const py = (clientY ?? box.top + box.height / 2) - (box.top + box.height / 2) - state.y;
      const change = next / state.scale;
      state.x -= px * (change - 1);
      state.y -= py * (change - 1);
      state.scale = next;
      if (next === 1) { state.x = 0; state.y = 0; }
      apply();
    };
    const reset = () => { Object.assign(state, { scale: 1, x: 0, y: 0, rotate: 0, brightness: 100, contrast: 100 }); brightness.value = 100; contrast.value = 100; apply(); };

    // Pointer position -> 0..1 coordinates on the (unrotated, unzoomed) picture.
    const toPicture = (clientX, clientY) => {
      const box = stage.getBoundingClientRect();
      const size = fitted();
      const centreX = box.left + box.width / 2 + state.x;
      const centreY = box.top + box.height / 2 + state.y;
      let dx = (clientX - centreX) / state.scale;
      let dy = (clientY - centreY) / state.scale;
      const angle = (-state.rotate * Math.PI) / 180;
      [dx, dy] = [dx * Math.cos(angle) - dy * Math.sin(angle), dx * Math.sin(angle) + dy * Math.cos(angle)];
      return { x: dx / size.width + 0.5, y: dy / size.height + 0.5 };
    };

    // ---------------------------------------------------------- marks
    const paintShapes = (ctx, width, height, shapes) => {
      ctx.lineWidth = Math.max(2, width / 300);
      ctx.strokeStyle = MARK_COLOUR;
      ctx.fillStyle = MARK_COLOUR;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      for (const shape of shapes) {
        const p = shape.points.map((point) => [point.x * width, point.y * height]);
        if (p.length < 2) continue;
        ctx.beginPath();
        if (shape.kind === "box") {
          const [a, b] = [p[0], p[p.length - 1]];
          ctx.strokeRect(a[0], a[1], b[0] - a[0], b[1] - a[1]);
        } else if (shape.kind === "arrow") {
          const [a, b] = [p[0], p[p.length - 1]];
          const angle = Math.atan2(b[1] - a[1], b[0] - a[0]);
          const head = Math.max(12, width / 60);
          ctx.moveTo(a[0], a[1]);
          ctx.lineTo(b[0], b[1]);
          ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(b[0], b[1]);
          ctx.lineTo(b[0] - head * Math.cos(angle - 0.45), b[1] - head * Math.sin(angle - 0.45));
          ctx.lineTo(b[0] - head * Math.cos(angle + 0.45), b[1] - head * Math.sin(angle + 0.45));
          ctx.closePath();
          ctx.fill();
          continue;
        } else {
          ctx.moveTo(p[0][0], p[0][1]);
          p.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
        }
        ctx.stroke();
      }
    };
    function drawMarks() {
      const ctx = marks.getContext("2d");
      ctx.clearRect(0, 0, marks.width, marks.height);
      paintShapes(ctx, marks.width, marks.height, state.draft ? [...state.shapes, state.draft] : state.shapes);
    }

    // ---------------------------------------------------------- pointer: pan, pinch, draw
    const pointers = new Map();
    let pinchStart = null;
    let panStart = null;
    stage.addEventListener("pointerdown", (event) => {
      stage.focus({ preventScroll: true });
      stage.setPointerCapture(event.pointerId);
      pointers.set(event.pointerId, event);
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinchStart = { distance: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), scale: state.scale };
        state.draft = null;
        return;
      }
      if (state.tool === "pan") {
        panStart = { x: event.clientX - state.x, y: event.clientY - state.y };
      } else {
        state.draft = { kind: state.tool, points: [toPicture(event.clientX, event.clientY)] };
      }
    });
    stage.addEventListener("pointermove", (event) => {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, event);
      if (pinchStart && pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const distance = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
        zoomAt((pinchStart.scale * distance) / pinchStart.distance / state.scale, (a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2);
      } else if (panStart && state.scale > 1) {
        state.x = event.clientX - panStart.x;
        state.y = event.clientY - panStart.y;
        apply();
      } else if (state.draft) {
        const point = toPicture(event.clientX, event.clientY);
        if (state.draft.kind === "pen") state.draft.points.push(point);
        else state.draft.points[1] = point;
        drawMarks();
      }
    });
    const release = (event) => {
      pointers.delete(event.pointerId);
      if (pointers.size < 2) pinchStart = null;
      panStart = null;
      if (state.draft && state.draft.points.length > 1) state.shapes.push(state.draft);
      state.draft = null;
      drawMarks();
    };
    stage.addEventListener("pointerup", release);
    stage.addEventListener("pointercancel", release);
    stage.addEventListener("wheel", (event) => { event.preventDefault(); zoomAt(event.deltaY < 0 ? 1.2 : 1 / 1.2, event.clientX, event.clientY); }, { passive: false });
    stage.addEventListener("dblclick", (event) => (state.scale > 1 ? reset() : zoomAt(2.5, event.clientX, event.clientY)));

    // ---------------------------------------------------------- save picture (with marks and adjustments)
    const save = () => {
      const size = fitted();
      const quarter = ((state.rotate % 360) + 360) % 360;
      const sideways = quarter === 90 || quarter === 270;
      const canvas = document.createElement("canvas");
      canvas.width = sideways ? size.naturalHeight : size.naturalWidth;
      canvas.height = sideways ? size.naturalWidth : size.naturalHeight;
      const ctx = canvas.getContext("2d");
      ctx.translate(canvas.width / 2, canvas.height / 2);
      ctx.rotate((quarter * Math.PI) / 180);
      ctx.translate(-size.naturalWidth / 2, -size.naturalHeight / 2);
      ctx.filter = `brightness(${state.brightness}%) contrast(${state.contrast}%)`;
      try {
        ctx.drawImage(media, 0, 0, size.naturalWidth, size.naturalHeight);
      } catch (_) {
        alert(label("saveFailed", "This picture cannot be saved."));
        return;
      }
      ctx.filter = "none";
      paintShapes(ctx, size.naturalWidth, size.naturalHeight, state.shapes);
      if (isVideo) {
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.font = `${Math.max(14, canvas.width / 50)}px sans-serif`;
        ctx.fillStyle = "rgba(0,0,0,0.6)";
        const stamp = `${options.title || ""} ${clockText(media.currentTime)}`.trim();
        ctx.fillRect(0, canvas.height - Math.max(24, canvas.width / 35), ctx.measureText(stamp).width + 16, canvas.height);
        ctx.fillStyle = "#fff";
        ctx.fillText(stamp, 8, canvas.height - 8);
      }
      canvas.toBlob((blob) => {
        if (!blob) return;
        const link = el("a", { href: URL.createObjectURL(blob), download: `${options.name || "gate-face"}-${Date.now()}.png` });
        document.body.appendChild(link);
        link.click();
        setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
      }, "image/png");
    };

    // ---------------------------------------------------------- toolbar
    const brightness = el("input", { type: "range", min: "30", max: "250", value: "100", "aria-label": label("brightness", "Brightness") });
    brightness.addEventListener("input", () => { state.brightness = Number(brightness.value); apply(); });
    const contrast = el("input", { type: "range", min: "30", max: "250", value: "100", "aria-label": label("contrast", "Contrast") });
    contrast.addEventListener("input", () => { state.contrast = Number(contrast.value); apply(); });
    const toolButtons = {};
    const setTool = (tool) => {
      state.tool = tool;
      Object.entries(toolButtons).forEach(([name, node]) => node.setAttribute("aria-pressed", String(name === tool)));
      stage.dataset.tool = tool;
    };
    [["pan", "✋", label("pan", "Move")], ["box", "▭", label("box", "Mark with a box")],
      ["arrow", "➚", label("arrow", "Mark with an arrow")], ["pen", "✎", label("pen", "Draw")]]
      .forEach(([tool, icon, title]) => { toolButtons[tool] = button(icon, title, () => setTool(tool)); });
    setTool("pan");

    const rotate = () => { state.rotate = (state.rotate + 90) % 360; apply(); };
    const undo = () => { state.shapes.pop(); drawMarks(); };
    const clearMarks = () => { state.shapes = []; drawMarks(); };
    const fullscreen = () => (document.fullscreenElement ? document.exitFullscreen() : root.requestFullscreen?.());
    const adjust = (slider, key, delta) => {
      state[key] = Math.max(Number(slider.min), Math.min(Number(slider.max), state[key] + delta));
      slider.value = String(state[key]);
      apply();
    };
    const panBy = (dx, dy) => { if (state.scale > 1) { state.x += dx; state.y += dy; apply(); } };
    const groups = [
      el("div", { class: "viewer-group" }, [
        button("−", label("zoomOut", "Zoom out"), () => zoomAt(1 / 1.5)), zoomLabel,
        button("+", label("zoomIn", "Zoom in"), () => zoomAt(1.5)),
        button("⟲", label("rotate", "Rotate"), rotate),
        button("⤢", label("reset", "Reset view"), reset),
      ]),
      el("div", { class: "viewer-group" }, [
        el("label", { class: "viewer-slider", title: label("brightness", "Brightness") }, [el("span", { text: "☀" }), brightness]),
        el("label", { class: "viewer-slider", title: label("contrast", "Contrast") }, [el("span", { text: "◐" }), contrast]),
      ]),
      el("div", { class: "viewer-group" }, [
        ...Object.values(toolButtons),
        button("↶", label("undo", "Undo mark"), undo),
        button("🗑", label("clear", "Clear marks"), clearMarks),
      ]),
      el("div", { class: "viewer-group" }, [
        button("💾", label("save", "Save picture with marks"), save),
        button("⛶", label("fullscreen", "Full screen"), fullscreen),
      ]),
    ];

    let video = null;
    if (isVideo) {
      const fps = Number(options.fps) || 4;
      const playButton = button("▶", label("play", "Play / pause"), () => (media.paused ? media.play() : media.pause()));
      const seek = el("input", { type: "range", min: "0", max: "1000", value: "0", class: "viewer-seek", "aria-label": label("seek", "Position") });
      const time = el("span", { class: "viewer-time", text: "0:00" });
      const speed = el("select", { "aria-label": label("speed", "Speed") });
      [0.25, 0.5, 1, 2, 4].forEach((rate) => speed.appendChild(el("option", { value: String(rate), text: `${rate}×` })));
      speed.value = "1";
      speed.addEventListener("change", () => { media.playbackRate = Number(speed.value); });
      const step = (frames) => { media.pause(); media.currentTime = Math.max(0, Math.min(media.duration || 0, media.currentTime + frames / fps)); };
      const jump = (seconds) => { media.currentTime = Math.max(0, Math.min(media.duration || 0, media.currentTime + seconds)); };
      seek.addEventListener("input", () => { if (media.duration) media.currentTime = (Number(seek.value) / 1000) * media.duration; });
      const sync = () => {
        playButton.textContent = media.paused ? "▶" : "⏸";
        time.textContent = `${clockText(media.currentTime)} / ${clockText(media.duration)}`;
        if (media.duration) seek.value = String(Math.round((media.currentTime / media.duration) * 1000));
      };
      ["play", "pause", "timeupdate", "loadedmetadata", "seeked"].forEach((name) => media.addEventListener(name, sync));
      groups.unshift(el("div", { class: "viewer-group viewer-playback" }, [
        playButton,
        button("⏪", label("back5", "Back 5 seconds"), () => jump(-5)),
        button("⏮", label("prevFrame", "Previous frame"), () => step(-1)),
        button("⏭", label("nextFrame", "Next frame"), () => step(1)),
        button("⏩", label("forward5", "Forward 5 seconds"), () => jump(5)),
        speed, time,
      ]));
      root.insertBefore(el("div", { class: "viewer-timeline" }, [seek]), stage.nextSibling);
      const rates = [...speed.options].map((option) => option.value);
      const changeSpeed = (direction) => {
        const index = Math.max(0, Math.min(rates.length - 1, rates.indexOf(speed.value) + direction));
        speed.value = rates[index];
        media.playbackRate = Number(speed.value);
      };
      video = {
        toggle: () => playButton.click(), step, jump, changeSpeed,
        start: () => { media.currentTime = 0; }, end: () => { media.currentTime = media.duration || 0; },
      };
    }
    if (isLive) groups[groups.length - 1].prepend(el("span", { class: "viewer-live", text: label("live", "LIVE") }));
    root.appendChild(el("div", { class: "viewer-toolbar" }, groups));

    media.addEventListener("load", layout);
    media.addEventListener("loadedmetadata", layout);
    new ResizeObserver(layout).observe(stage);
    document.addEventListener("fullscreenchange", layout);
    layout();
    const api = {
      root, video,
      zoomIn: () => zoomAt(1.25), zoomOut: () => zoomAt(1 / 1.25), reset, rotate, fullscreen, save, undo, clearMarks,
      setTool, panBy,
      brightness: (delta) => adjust(brightness, "brightness", delta),
      contrast: (delta) => adjust(contrast, "contrast", delta),
    };
    // The viewer the keyboard controls: the one last pointed at or clicked.
    root.addEventListener("pointerenter", () => { activeViewer = api; });
    root.addEventListener("pointerdown", () => { activeViewer = api; });
    viewers.push(api);
    return api;
  }

  // ------------------------------------------------------------ full-screen photo viewer
  function openLightbox(url, name, link = null) {
    lightbox?.close();
    const box = el("div", { class: "viewer-lightbox", role: "dialog", "aria-modal": "true" });
    const close = () => {
      box.remove();
      if (lightbox && lightbox.box === box) lightbox = null;
      activeViewer = viewers.find((item) => document.body.contains(item.root)) || null;
    };
    const image = el("img", { src: url, alt: "" });
    const holder = el("div", { class: "viewer-lightbox-body" }, [image]);
    box.append(
      el("div", { class: "viewer-lightbox-bar" }, [
        el("a", { href: url, target: "_blank", rel: "noopener", class: "viewer-btn", text: label("original", "Original") }),
        button("✕", label("close", "Close"), close),
      ]),
      holder,
    );
    box.addEventListener("click", (event) => { if (event.target === box) close(); });
    document.body.appendChild(box);
    const api = createViewer(holder, image, { name });
    activeViewer = api;
    lightbox = { box, close, link };
    holder.querySelector(".viewer-stage").focus();
  }

  // Previous / next photo of the page while the full-screen viewer is open.
  const browse = (direction) => {
    if (!lightbox || !lightbox.link) return false;
    const links = [...document.querySelectorAll("a[data-viewer-open]")];
    const next = links[links.indexOf(lightbox.link) + direction];
    if (next) openLightbox(next.href, next.dataset.name, next);
    return true;
  };

  const enhance = (scope) => {
    scope.querySelectorAll("[data-viewer]:not([data-viewer-ready])").forEach((container) => {
      const media = container.querySelector("img, video");
      if (!media) return;
      container.dataset.viewerReady = "1";
      createViewer(container, media, {
        fps: container.dataset.fps, name: container.dataset.name, title: container.dataset.title, live: container.dataset.live === "1",
      });
    });
  };
  document.addEventListener("click", (event) => {
    const link = event.target.closest && event.target.closest("a[data-viewer-open]");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey) return; // keep "open in new tab"
    event.preventDefault();
    openLightbox(link.href, link.dataset.name, link);
  });
  const editing = (target) =>
    target && (target.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName) && target.type !== "range");

  // Returns true when the key was used, so the page-wide shortcuts leave it alone.
  function handleKey(event) {
    if (event.defaultPrevented || editing(event.target) || event.altKey || event.metaKey) return false;
    if (document.querySelector(".alarm-overlay")) return false; // the alarm has the keyboard
    if (window.GateKeys?.prefixActive()) return false; // second key of a "G then …" shortcut
    if (lightbox && event.key === "Escape") { lightbox.close(); return true; }
    if (lightbox && (event.key === "PageDown" || event.key === "PageUp")) return browse(event.key === "PageDown" ? 1 : -1);
    const viewer = (activeViewer && document.body.contains(activeViewer.root)) ? activeViewer
      : (viewers.filter((item) => document.body.contains(item.root)).length === 1 ? viewers.find((item) => document.body.contains(item.root)) : null);
    if (!viewer) return false;
    const key = event.key;
    const lower = key.length === 1 ? key.toLowerCase() : key;
    const step = 60;
    if (event.ctrlKey) {
      if (lower === "z") { viewer.undo(); return true; }
      const pans = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
      if (pans[key]) { viewer.panBy(...pans[key]); return true; }
      return false;
    }
    if (event.shiftKey && key.startsWith("Arrow")) {
      if (key === "ArrowUp") viewer.brightness(10);
      if (key === "ArrowDown") viewer.brightness(-10);
      if (key === "ArrowRight") viewer.contrast(10);
      if (key === "ArrowLeft") viewer.contrast(-10);
      return true;
    }
    const actions = {
      "+": viewer.zoomIn, "=": viewer.zoomIn, "-": viewer.zoomOut, "0": viewer.reset,
      r: viewer.rotate, f: viewer.fullscreen, s: viewer.save, Delete: viewer.clearMarks,
      v: () => viewer.setTool("pan"), b: () => viewer.setTool("box"), a: () => viewer.setTool("arrow"), d: () => viewer.setTool("pen"),
    };
    if (viewer.video) {
      Object.assign(actions, {
        " ": viewer.video.toggle, k: viewer.video.toggle,
        ArrowLeft: () => viewer.video.step(-1), ArrowRight: () => viewer.video.step(1),
        j: () => viewer.video.jump(-5), l: () => viewer.video.jump(5),
        "[": () => viewer.video.changeSpeed(-1), "]": () => viewer.video.changeSpeed(1),
        Home: viewer.video.start, End: viewer.video.end,
      });
    }
    const action = actions[lower];
    if (!action) return false;
    action();
    return true;
  }
  document.addEventListener("keydown", (event) => { if (handleKey(event)) event.preventDefault(); });

  enhance(document);
  document.body.addEventListener("htmx:afterSwap", (event) => enhance(event.target));
  window.GateViewer = { enhance, open: openLightbox, isOpen: () => Boolean(lightbox) };
})();
