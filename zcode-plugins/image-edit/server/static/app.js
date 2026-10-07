(() => {
  const $ = (id) => document.getElementById(id);
  const view = $("view");
  const paint = $("paint");
  const vctx = view.getContext("2d");
  const pctx = paint.getContext("2d");
  const wrap = $("wrap");
  const stack = $("stack");

  const LS = "image-edit.v2";
  const state = {
    images: [],
    refs: [],
    active: 0,
    tool: "rect",
    brush: 36,
    drawing: false,
    last: null,
    engines: {},
    displayScale: 1,
    results: [],
    resultActive: 0,
    videoTimer: null,
    rectStart: null,
    selectedRect: null,
    dragRect: null,
    arrowStart: null,
    selectedAnn: null,
    dragAnn: null,
    resizeRect: null,
    serverHasKey: false,
    serverBase: "",
    viewScale: 1,
    panX: 0,
    panY: 0,
    spaceDown: false,
    panning: null,
    fitScale: 1,
  };

  function normalizedBase(base) {
    const clean = (base || "").trim().replace(/\/+$/, "");
    return clean && !clean.endsWith("/v1") ? clean + "/v1" : clean;
  }
  function connectionReady() {
    return Boolean($("baseUrl").value.trim() && ($("apiKey").value.trim()
      || (state.serverHasKey && normalizedBase($("baseUrl").value) === state.serverBase)));
  }

  // 中转站计费（元/张）：gpt 1k=0.03 2k=0.08 4k=0.1；grok 1k=0.05 2k=0.08 4k=0.1
  const PRICE = {
    gpt: { "1k": 0.03, "2k": 0.08, "4k": 0.1 },
    grok: { "1k": 0.05, "2k": 0.08, "4k": 0.1 },
  };
  const TIER_SHORT = { "1k": 1024, "2k": 2048, "4k": 4096 };
  const RATIOS = {
    "1:1": [1, 1], "4:3": [4, 3], "3:4": [3, 4],
    "16:9": [16, 9], "9:16": [9, 16], "3:2": [3, 2], "2:3": [2, 3],
  };
  const EXT = { png: "png", jpeg: "jpg", webp: "webp" };

  function computeSize(tier, ratio) {
    if (ratio === "match") {
      const item = current();
      if (item && item.img) {
        const snap = (v) => Math.max(256, Math.round(v / 64) * 64);
        const maxSide = TIER_SHORT[tier] || 1024;
        const iw = item.img.naturalWidth, ih = item.img.naturalHeight;
        const scale = maxSide / Math.max(iw, ih);
        return snap(iw * Math.min(scale, 1)) + "x" + snap(ih * Math.min(scale, 1));
      }
    }
    const base = TIER_SHORT[tier] || 1024;
    const [rw, rh] = RATIOS[ratio] || [1, 1];
    let w, h;
    if (rw >= rh) { h = base; w = base * rw / rh; }
    else { w = base; h = base * rh / rw; }
    const snap = (v) => Math.max(256, Math.round(v / 64) * 64);
    return snap(w) + "x" + snap(h);
  }

  function saveSettings() {
    const data = {
      baseUrl: $("baseUrl").value,
      apiKey: $("apiKey").value,
      model: $("model").value,
      engine: $("engine").value,
      scope: $("scope").value,
      maskMode: $("maskMode").value,
      quality: $("quality").value,
      tier: $("tier").value,
      ratio: $("ratio").value,
      count: $("count").value,
      outputFormat: $("outputFormat").value,
      padMode: $("padMode").value,
      prompt: $("prompt").value,
      refRoles: $("refRoles").value,
      vModel: $("vModel").value,
      vPrompt: $("vPrompt").value,
      vSize: $("vSize").value,
      vSeconds: $("vSeconds").value,
      tab: document.body.dataset.tab,
    };
    try { localStorage.setItem(LS, JSON.stringify(data)); } catch {}
  }
  function loadSettings() {
    try {
      let data = JSON.parse(localStorage.getItem(LS) || "null");
      if (!data) {
        const old = JSON.parse(localStorage.getItem("image-edit.v1") || "null");
        if (old) data = Object.assign({}, old);
      }
      if (!data) return;
      for (const [k, v] of Object.entries(data)) {
        const el = $(k);
        if (el && v != null) el.value = v;
      }
      if (data.tab && ["edit", "t2i", "video"].includes(data.tab)) setTab(data.tab);
    } catch {}
  }

  async function loadDefaults() {
    try {
      const r = await fetch("/api/defaults");
      const j = await r.json();
      if (!j.ok) return;
      state.serverHasKey = Boolean(j.has_key);
      state.serverBase = normalizedBase(j.base_url);
      if (!$("baseUrl").value && j.base_url) $("baseUrl").value = j.base_url;
      // 安全：服务端只给 has_key 布尔值，绝不下发 api_key 本体。
      // 页面 key 留空时，服务端提交会自动回退到本机配置的 key（/api/edit 已实现）。
      if (!$("model").value && j.model) $("model").value = j.model;
      if (j.version && $("appVersion")) $("appVersion").textContent = "v" + j.version;
      if (j.has_key) {
        setStatus($("connStatus"), "服务端已配置 API Key（页面留空即可）。点「测试」验证连通。");
      } else if (j.base_url) {
        setStatus($("connStatus"), "已从本机配置填入 Base URL。请在页面填写 API Key 后点「测试」。");
      }
      checkForUpdate();
    } catch {}
  }

  async function checkForUpdate() {
    const el = $("updateStatus");
    if (!el) return;
    try {
      const r = await fetch("/api/update");
      const j = await r.json();
      if (!j.ok) return;
      if (j.newer) {
        el.innerHTML = `有新版本 v${j.latest}（当前 v${j.current}）。<a href="${j.url}" target="_blank" rel="noopener">查看 Release</a>`;
        el.className = "status ok";
      } else if (j.latest) {
        el.textContent = `已是最新 v${j.current}`;
        el.className = "status";
      } else if (j.error) {
        el.textContent = j.error;
        el.className = "status";
      }
    } catch {}
  }

  function setStatus(el, msg, kind) {
    el.textContent = msg || "";
    el.className = "status" + (kind ? " " + kind : "");
  }

  function setTab(name) {
    document.body.dataset.tab = name;
    for (const t of document.querySelectorAll(".tab")) {
      t.classList.toggle("active", t.dataset.tab === name);
    }
    const titles = {
      edit: ["改图", "涂抹区 = 要改的区域。红色只是发给上游的标记，结果里不应出现红块。"],
      t2i: ["生图", "直接输入提示词生成新图。想涂改已有图片请切回「改图」。"],
      video: ["视频", "输入提示词生成视频。提交后自动轮询进度，结果在下方播放。"],
    };
    const [t, h] = titles[name] || titles.edit;
    $("sideTitle").textContent = t;
    $("sideHint").textContent = h;
    $("btnRun").textContent = name === "t2i" ? "生成图片" : "提交改图";
    updatePrice();
    layout();
  }
  for (const t of document.querySelectorAll(".tab")) {
    t.onclick = () => { setTab(t.dataset.tab); saveSettings(); };
  }

  function current() {
    return state.images[state.active] || null;
  }

  function loadImageFile(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        const mask = document.createElement("canvas");
        mask.width = img.naturalWidth;
        mask.height = img.naturalHeight;
        resolve({ file, img, mask, url, name: file.name, rects: [], anns: [], history: [], future: [] });
      };
      img.onerror = () => reject(new Error("无法读取 " + file.name));
      img.src = url;
    });
  }

  function fitScale(img) {
    const pad = 24;
    const aw = Math.max(120, wrap.clientWidth - pad);
    const ah = Math.max(120, wrap.clientHeight - pad);
    return Math.min(aw / img.naturalWidth, ah / img.naturalHeight, 1);
  }

  function layout() {
    if (document.body.dataset.tab !== "edit") return;
    const item = current();
    if (!item) {
      view.width = paint.width = 1;
      view.height = paint.height = 1;
      stack.style.width = "0";
      stack.style.height = "0";
      $("imgMeta").textContent = "还没有图";
      return;
    }
    const fit = fitScale(item.img);
    state.fitScale = fit;
    if (!state.viewScale || state.viewScale === 1) state.viewScale = fit;
    const s = state.viewScale;
    state.displayScale = s;
    const w = Math.max(1, Math.round(item.img.naturalWidth * s));
    const h = Math.max(1, Math.round(item.img.naturalHeight * s));
    view.width = paint.width = w;
    view.height = paint.height = h;
    stack.style.width = w + "px";
    stack.style.height = h + "px";
    stack.style.transform = `translate(${state.panX}px, ${state.panY}px)`;
    redraw();
    updateHistoryButtons();
    $("imgMeta").textContent = `${item.name} · ${item.img.naturalWidth}×${item.img.naturalHeight} · ${Math.round(s / fit * 100)}%`;
  }

  function redraw() {
    const item = current();
    if (!item) return;
    vctx.clearRect(0, 0, view.width, view.height);
    vctx.drawImage(item.img, 0, 0, view.width, view.height);
    pctx.clearRect(0, 0, paint.width, paint.height);
    pctx.save();
    pctx.globalAlpha = 0.45;
    pctx.drawImage(item.mask, 0, 0, paint.width, paint.height);
    pctx.restore();
    // 矩形对象层：独立于位图遮罩，可选中拖动
    if (item.rects && item.rects.length) {
      const s = state.displayScale;
      item.rects.forEach((r, i) => {
        const sel = i === state.selectedRect;
        pctx.save();
        pctx.globalAlpha = 0.45;
        pctx.fillStyle = "rgb(255,0,0)";
        pctx.fillRect(r.x * s, r.y * s, r.w * s, r.h * s);
        pctx.globalAlpha = 1;
        pctx.lineWidth = sel ? 2 : 1;
        pctx.strokeStyle = sel ? "#38bdf8" : "rgb(255,90,90)";
        pctx.strokeRect(r.x * s, r.y * s, r.w * s, r.h * s);
        if (sel) {
          const hs = 6;
          [[r.x, r.y], [r.x + r.w, r.y], [r.x, r.y + r.h], [r.x + r.w, r.y + r.h]].forEach(([hx, hy]) => {
            pctx.fillStyle = "#38bdf8";
            pctx.fillRect(hx * s - hs / 2, hy * s - hs / 2, hs, hs);
          });
        }
        if ((r.text || "").trim()) {
          pctx.globalAlpha = 1;
          pctx.font = "12px ui-sans-serif, system-ui, sans-serif";
          pctx.fillStyle = "#fff";
          pctx.strokeStyle = "#000";
          pctx.lineWidth = 3;
          const tx = r.x * s + 6, ty = r.y * s + 16;
          pctx.strokeText(r.text, tx, ty);
          pctx.fillText(r.text, tx, ty);
        }
        pctx.restore();
      });
    }
    if (item.anns && item.anns.length) {
      const s = state.displayScale;
      item.anns.forEach((a, i) => drawAnnotation(a, i === state.selectedAnn, s));
    }
  }

  function drawAnnotation(a, selected, s) {
    const x1 = a.x1 * s, y1 = a.y1 * s, x2 = a.x2 * s, y2 = a.y2 * s;
    pctx.save();
    pctx.strokeStyle = selected ? "#38bdf8" : "rgb(228,30,60)";
    pctx.fillStyle = selected ? "#38bdf8" : "rgb(228,30,60)";
    pctx.lineWidth = selected ? 3 : 2.5;
    pctx.beginPath();
    pctx.moveTo(x1, y1);
    pctx.lineTo(x2, y2);
    pctx.stroke();
    const ang = Math.atan2(y2 - y1, x2 - x1);
    const ah = 12;
    pctx.beginPath();
    pctx.moveTo(x2, y2);
    pctx.lineTo(x2 - ah * Math.cos(ang - 0.4), y2 - ah * Math.sin(ang - 0.4));
    pctx.lineTo(x2 - ah * Math.cos(ang + 0.4), y2 - ah * Math.sin(ang + 0.4));
    pctx.closePath();
    pctx.fill();
    if (a.text) {
      pctx.font = "13px ui-sans-serif, system-ui, sans-serif";
      pctx.strokeStyle = "#000";
      pctx.lineWidth = 3;
      pctx.strokeText(a.text, x1 + 6, y1 - 8);
      pctx.fillStyle = selected ? "#38bdf8" : "rgb(255,80,90)";
      pctx.fillText(a.text, x1 + 6, y1 - 8);
    }
    pctx.restore();
  }

  const HISTORY_LIMIT = 50;

  function cloneRects(rects) {
    return (rects || []).map((r) => ({ x: r.x, y: r.y, w: r.w, h: r.h, text: r.text || "" }));
  }
  function cloneAnns(anns) {
    return (anns || []).map((a) => ({ x1: a.x1, y1: a.y1, x2: a.x2, y2: a.y2, text: a.text || "" }));
  }

  function snapshotItem(item) {
    return {
      mask: item.mask.getContext("2d").getImageData(0, 0, item.mask.width, item.mask.height),
      rects: cloneRects(item.rects),
      anns: cloneAnns(item.anns),
    };
  }

  function restoreItem(item, snap) {
    item.mask.getContext("2d").putImageData(snap.mask, 0, 0);
    item.rects = cloneRects(snap.rects);
    item.anns = cloneAnns(snap.anns);
    state.selectedRect = null;
    state.selectedAnn = null;
  }

  function pushHistory(item) {
    if (!item) return;
    item.history = item.history || [];
    item.future = item.future || [];
    item.history.push(snapshotItem(item));
    if (item.history.length > HISTORY_LIMIT) item.history.shift();
    item.future.length = 0;
    updateHistoryButtons();
  }

  function undo() {
    const item = current();
    if (!item || !(item.history && item.history.length)) return;
    item.future = item.future || [];
    item.future.push(snapshotItem(item));
    restoreItem(item, item.history.pop());
    redraw();
    updateHistoryButtons();
  }

  function redo() {
    const item = current();
    if (!item || !(item.future && item.future.length)) return;
    item.history = item.history || [];
    item.history.push(snapshotItem(item));
    restoreItem(item, item.future.pop());
    redraw();
    updateHistoryButtons();
  }

  function updateHistoryButtons() {
    const item = current();
    const undoBtn = $("btnUndo");
    const redoBtn = $("btnRedo");
    if (!undoBtn || !redoBtn) return;
    undoBtn.disabled = !(item && item.history && item.history.length);
    redoBtn.disabled = !(item && item.future && item.future.length);
  }

  // 把矩形对象固化进位图遮罩（提交、或需要像素级操作时调用）
  function bakeRects(item) {
    if (!item.rects || !item.rects.length) return;
    const c = maskCtx(item);
    c.save();
    c.globalCompositeOperation = "source-over";
    c.fillStyle = "rgb(255,0,0)";
    for (const r of item.rects) c.fillRect(r.x, r.y, r.w, r.h);
    c.restore();
    item.rects = [];
    if (state.selectedRect != null) state.selectedRect = null;
  }

  function annotationPrompt(item) {
    const fromRects = (item.rects || []).map((r) => (r.text || "").trim()).filter(Boolean);
    const fromAnns = (item.anns || []).map((a) => (a.text || "").trim()).filter(Boolean);
    const texts = fromRects.concat(fromAnns);
    if (!texts.length) return "";
    return "图上的红色框是要改的区域，框上的文字是修改要求：" + texts.map((t, i) => `（${i + 1}）${t}`).join("；") + "。结果里不要出现红框、箭头或文字痕迹。";
  }

  function compositeWithAnnotations(item) {
    const c = document.createElement("canvas");
    c.width = item.img.naturalWidth;
    c.height = item.img.naturalHeight;
    const ctx = c.getContext("2d");
    ctx.drawImage(item.img, 0, 0);
    const prev = { pctx, displayScale: state.displayScale };
    // draw onto this canvas in image pixels
    const old = pctx;
    // reuse drawAnnotation but it uses pctx/displayScale — draw locally
    (item.anns || []).forEach((a) => {
      ctx.save();
      ctx.strokeStyle = "rgb(228,30,60)";
      ctx.fillStyle = "rgb(228,30,60)";
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.moveTo(a.x1, a.y1);
      ctx.lineTo(a.x2, a.y2);
      ctx.stroke();
      const ang = Math.atan2(a.y2 - a.y1, a.x2 - a.x1);
      const ah = 18;
      ctx.beginPath();
      ctx.moveTo(a.x2, a.y2);
      ctx.lineTo(a.x2 - ah * Math.cos(ang - 0.4), a.y2 - ah * Math.sin(ang - 0.4));
      ctx.lineTo(a.x2 - ah * Math.cos(ang + 0.4), a.y2 - ah * Math.sin(ang + 0.4));
      ctx.closePath();
      ctx.fill();
      if (a.text) {
        ctx.font = "28px ui-sans-serif, system-ui, sans-serif";
        ctx.strokeStyle = "#000";
        ctx.lineWidth = 6;
        ctx.strokeText(a.text, a.x1 + 8, a.y1 - 10);
        ctx.fillStyle = "rgb(255,80,90)";
        ctx.fillText(a.text, a.x1 + 8, a.y1 - 10);
      }
      ctx.restore();
    });
    return c;
  }

  function maskHasContent(item) {
    if (item.rects && item.rects.length) return true;
    if (item.anns && item.anns.length) return true;
    const data = maskCtx(item).getImageData(0, 0, item.mask.width, item.mask.height).data;
    for (let i = 3; i < data.length; i += 4) if (data[i] >= 128) return true;
    return false;
  }

  function renderThumbs() {
    const box = $("contentThumbs");
    box.innerHTML = "";
    state.images.forEach((it, i) => {
      const d = document.createElement("div");
      d.className = "thumb" + (i === state.active ? " active" : "");
      const img = document.createElement("img");
      img.src = it.url;
      img.alt = it.name;
      const x = document.createElement("button");
      x.className = "x";
      x.textContent = "×";
      x.onclick = (e) => {
        e.stopPropagation();
        URL.revokeObjectURL(it.url);
        state.images.splice(i, 1);
        if (state.active >= state.images.length) state.active = Math.max(0, state.images.length - 1);
        renderThumbs();
        layout();
      };
      d.onclick = () => { state.active = i; renderThumbs(); layout(); };
      d.append(img, x);
      box.appendChild(d);
    });
    const rbox = $("refThumbs");
    rbox.innerHTML = "";
    state.refs.forEach((it, i) => {
      const d = document.createElement("div");
      d.className = "thumb";
      const img = document.createElement("img");
      img.src = it.url;
      img.alt = it.name;
      const x = document.createElement("button");
      x.className = "x";
      x.textContent = "×";
      x.onclick = (e) => {
        e.stopPropagation();
        URL.revokeObjectURL(it.url);
        state.refs.splice(i, 1);
        renderThumbs();
      };
      d.append(img, x);
      rbox.appendChild(d);
    });
  }

  async function addFiles(fileList, append) {
    const files = [...fileList].filter(
      (f) => f.type.startsWith("image/") || /\.(png|jpe?g|webp|gif|bmp)$/i.test(f.name || "")
    );
    if (!files.length) return;
    if (!append) {
      state.images.forEach((it) => URL.revokeObjectURL(it.url));
      state.images = [];
      state.active = 0;
    }
    for (const f of files) state.images.push(await loadImageFile(f));
    if (!append) state.active = 0;
    else state.active = state.images.length - 1;
    renderThumbs();
    layout();
  }

  function maskCtx(item) {
    return item.mask.getContext("2d");
  }

  function pos(ev) {
    const item = current();
    const r = paint.getBoundingClientRect();
    const x = (ev.clientX - r.left) / r.width * item.img.naturalWidth;
    const y = (ev.clientY - r.top) / r.height * item.img.naturalHeight;
    return { x, y };
  }

  function stroke(from, to) {
    const item = current();
    const c = maskCtx(item);
    c.save();
    c.lineCap = "round";
    c.lineJoin = "round";
    c.lineWidth = state.brush;
    if (state.tool === "eraser") {
      c.globalCompositeOperation = "destination-out";
      c.strokeStyle = "rgba(0,0,0,1)";
    } else {
      c.globalCompositeOperation = "source-over";
      c.strokeStyle = "rgb(255,0,0)";
    }
    c.beginPath();
    c.moveTo(from.x, from.y);
    c.lineTo(to.x, to.y);
    c.stroke();
    c.restore();
    redraw();
  }

  // --- 矩形工具：拖拽出实心矩形改区，带预览 ---

  function hitRectHandle(r, p, s) {
    const tol = 10 / s;
    const corners = [
      ["nw", r.x, r.y],
      ["ne", r.x + r.w, r.y],
      ["sw", r.x, r.y + r.h],
      ["se", r.x + r.w, r.y + r.h],
    ];
    for (const [name, hx, hy] of corners) {
      if (Math.abs(p.x - hx) <= tol && Math.abs(p.y - hy) <= tol) return name;
    }
    return null;
  }

  function applyResize(r, handle, p, iw, ih) {
    let x1 = r.ox, y1 = r.oy, x2 = r.ox + r.ow, y2 = r.oy + r.oh;
    if (handle.includes("n")) y1 = p.y;
    if (handle.includes("s")) y2 = p.y;
    if (handle.includes("w")) x1 = p.x;
    if (handle.includes("e")) x2 = p.x;
    r.x = Math.max(0, Math.min(x1, x2));
    r.y = Math.max(0, Math.min(y1, y2));
    r.w = Math.max(4, Math.min(iw - r.x, Math.abs(x2 - x1)));
    r.h = Math.max(4, Math.min(ih - r.y, Math.abs(y2 - y1)));
  }

  function nearestRectIndex(item, p) {
    let best = -1, dist = Infinity;
    (item.rects || []).forEach((r, i) => {
      const cx = r.x + r.w / 2, cy = r.y + r.h / 2;
      const d = Math.hypot(p.x - cx, p.y - cy);
      if (d < dist) { dist = d; best = i; }
    });
    return best;
  }

  function hideAnnEditor() {
    const el = $("annEditor");
    if (!el) return;
    el.classList.remove("show");
    el.blur();
  }

  function placeAnnEditor(item, idx) {
    const el = $("annEditor");
    const r = item.rects[idx];
    if (!el || !r) return;
    const s = state.displayScale;
    el.value = r.text || "";
    el.dataset.idx = String(idx);
    el.style.left = (r.x * s) + "px";
    el.style.top = (r.y * s + r.h * s + 6) + "px";
    el.classList.add("show");
    el.focus();
    el.select();
  }

  function commitAnnEditor() {
    const el = $("annEditor");
    if (!el || !el.classList.contains("show")) return;
    const item = current();
    const idx = Number(el.dataset.idx);
    if (!item || !item.rects || !item.rects[idx]) { hideAnnEditor(); return; }
    const next = el.value.trim();
    if ((item.rects[idx].text || "") !== next) {
      pushHistory(item);
      item.rects[idx].text = next;
      redraw();
    }
    hideAnnEditor();
  }

  function previewRect(from, to) {
    const item = current();
    if (!item) return;
    // 先照常绘制图+遮罩，再叠加预览框
    redraw();
    pctx.save();
    const x = Math.min(from.x, to.x) * state.displayScale;
    const y = Math.min(from.y, to.y) * state.displayScale;
    const w = Math.abs(to.x - from.x) * state.displayScale;
    const h = Math.abs(to.y - from.y) * state.displayScale;
    pctx.globalAlpha = 0.45;
    pctx.fillStyle = "rgb(255,0,0)";
    pctx.fillRect(x, y, w, h);
    pctx.globalAlpha = 1;
    pctx.lineWidth = 1.5;
    pctx.strokeStyle = "rgb(255,60,60)";
    pctx.strokeRect(x, y, w, h);
    pctx.restore();
  }

  function hitExistingRect(item, p) {
    let hit = -1, handle = null;
    if (state.selectedRect != null && item.rects[state.selectedRect]) {
      handle = hitRectHandle(item.rects[state.selectedRect], p, state.displayScale);
      if (handle) return { hit: state.selectedRect, handle };
    }
    for (let i = (item.rects || []).length - 1; i >= 0; i--) {
      const r = item.rects[i];
      const h = hitRectHandle(r, p, state.displayScale);
      if (h) return { hit: i, handle: h };
      if (p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h) return { hit: i, handle: null };
    }
    return { hit, handle };
  }

  paint.addEventListener("pointerdown", (ev) => {
    if (!current()) return;
    paint.setPointerCapture(ev.pointerId);
    const item = current();
    const p = pos(ev);
    if (state.spaceDown || ev.button === 1) {
      state.panning = { x: ev.clientX, y: ev.clientY, panX: state.panX, panY: state.panY };
      paint.style.cursor = "grabbing";
      ev.preventDefault();
      return;
    }
    const { hit, handle } = hitExistingRect(item, p);
    if (hit >= 0) {
      hideAnnEditor();
      state.selectedRect = hit;
      const r = item.rects[hit];
      if (ev.detail === 2) {
        redraw();
        placeAnnEditor(item, hit);
        ev.preventDefault();
        return;
      }
      if (handle) state.resizeRect = { idx: hit, handle, ox: r.x, oy: r.y, ow: r.w, oh: r.h };
      else state.dragRect = { idx: hit, dx: p.x - r.x, dy: p.y - r.y, ox: r.x, oy: r.y };
      redraw();
      ev.preventDefault();
      return;
    }
    if (state.tool === "rect") {
      hideAnnEditor();
      state.selectedRect = null;
      state.drawing = true;
      state.rectStart = p;
      previewRect(p, p);
      ev.preventDefault();
      return;
    }
    hideAnnEditor();
    state.selectedRect = null;
    state.drawing = true;
    state.last = p;
    pushHistory(item);
    stroke(state.last, state.last);
    ev.preventDefault();
  });
  paint.addEventListener("pointermove", (ev) => {
    if (state.panning) {
      state.panX = state.panning.panX + (ev.clientX - state.panning.x);
      state.panY = state.panning.panY + (ev.clientY - state.panning.y);
      stack.style.transform = `translate(${state.panX}px, ${state.panY}px)`;
      return;
    }
    if (!state.drawing && !state.dragRect && !state.resizeRect) return;
    const now = pos(ev);
    if (state.resizeRect) {
      const item = current();
      const r = item.rects[state.resizeRect.idx];
      if (!r) { state.resizeRect = null; return; }
      applyResize(r, state.resizeRect.handle, now, item.img.naturalWidth, item.img.naturalHeight);
      redraw();
      return;
    }
    if (state.dragRect) {
      const item = current();
      const r = item.rects[state.dragRect.idx];
      if (!r) { state.dragRect = null; return; }
      r.x = Math.max(0, Math.min(item.img.naturalWidth - r.w, now.x - state.dragRect.dx));
      r.y = Math.max(0, Math.min(item.img.naturalHeight - r.h, now.y - state.dragRect.dy));
      redraw();
      return;
    }
    if (state.tool === "rect") {
      if (state.rectStart) previewRect(state.rectStart, now);
      return;
    }
    if (!state.drawing) return;
    stroke(state.last, now);
    state.last = now;
  });
  const endDraw = (ev) => {
    const item = current();
    if (state.panning) {
      state.panning = null;
      paint.style.cursor = state.spaceDown ? "grab" : "crosshair";
      return;
    }
    if (state.resizeRect) {
      const r = item && item.rects[state.resizeRect.idx];
      if (r && (r.w !== state.resizeRect.ow || r.h !== state.resizeRect.oh || r.x !== state.resizeRect.ox || r.y !== state.resizeRect.oy)) {
        const nx = r.x, ny = r.y, nw = r.w, nh = r.h;
        r.x = state.resizeRect.ox; r.y = state.resizeRect.oy; r.w = state.resizeRect.ow; r.h = state.resizeRect.oh;
        pushHistory(item);
        r.x = nx; r.y = ny; r.w = nw; r.h = nh;
      }
      state.resizeRect = null;
    } else if (state.dragRect) {
      const r = item && item.rects[state.dragRect.idx];
      if (r && (r.x !== state.dragRect.ox || r.y !== state.dragRect.oy)) {
        const nx = r.x, ny = r.y;
        r.x = state.dragRect.ox;
        r.y = state.dragRect.oy;
        pushHistory(item);
        r.x = nx;
        r.y = ny;
      }
      state.dragRect = null;
    } else if (state.drawing && state.tool === "rect" && state.rectStart && item) {
      const now = ev ? pos(ev) : state.last;
      const x = Math.min(now.x, state.rectStart.x);
      const y = Math.min(now.y, state.rectStart.y);
      const w = Math.abs(now.x - state.rectStart.x);
      const h = Math.abs(now.y - state.rectStart.y);
      if (w > 2 && h > 2) {
        pushHistory(item);
        item.rects.push({ x, y, w, h, text: "" });
        state.selectedRect = item.rects.length - 1;
        redraw();
        placeAnnEditor(item, state.selectedRect);
      } else {
        state.rectStart = null;
        redraw();
      }
      state.rectStart = null;
    }
    state.drawing = false;
    state.last = null;
  };
  paint.addEventListener("pointerup", endDraw);
  paint.addEventListener("pointercancel", () => endDraw(null));
  paint.style.touchAction = "none";

  // Delete/Backspace 删除选中矩形；Ctrl+Z / Ctrl+Y 撤销重做（输入框内不拦截）
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toUpperCase();
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(tag);
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "z") {
      if (typing) return;
      e.preventDefault();
      if (e.shiftKey) redo();
      else undo();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "y") {
      if (typing) return;
      e.preventDefault();
      redo();
      return;
    }
    if (!["Delete", "Backspace"].includes(e.key)) return;
    if (typing) return;
    if (document.body.dataset.tab !== "edit") return;
    const item = current();
    if (state.selectedAnn != null && item && item.anns && item.anns[state.selectedAnn]) {
      pushHistory(item);
      item.anns.splice(state.selectedAnn, 1);
      state.selectedAnn = null;
      redraw();
      e.preventDefault();
      return;
    }
    if (state.selectedRect == null) return;
    if (!item || !item.rects || !item.rects[state.selectedRect]) return;
    pushHistory(item);
    item.rects.splice(state.selectedRect, 1);
    state.selectedRect = null;
    redraw();
    e.preventDefault();
  });

  function setTool(name) {
    state.tool = name;
    $("toolBrush").classList.toggle("active", name === "brush");
    $("toolRect").classList.toggle("active", name === "rect");
    $("toolEraser").classList.toggle("active", name === "eraser");
  }

  const annEl = $("annEditor");
  if (annEl) {
    annEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commitAnnEditor(); }
      if (e.key === "Escape") { hideAnnEditor(); }
    });
    annEl.addEventListener("blur", () => commitAnnEditor());
  }

  $("toolBrush").onclick = () => setTool("brush");
  $("toolRect").onclick = () => setTool("rect");
  $("toolEraser").onclick = () => setTool("eraser");
  $("btnUndo").onclick = () => undo();
  $("btnRedo").onclick = () => redo();
  $("btnFillAll").onclick = () => {
    const item = current();
    if (!item) return;
    pushHistory(item);
    const c = maskCtx(item);
    c.save();
    c.globalCompositeOperation = "source-over";
    c.fillStyle = "rgb(255,0,0)";
    c.fillRect(0, 0, item.mask.width, item.mask.height);
    c.restore();
    item.rects = [];
    item.anns = [];
    state.selectedRect = null;
    state.selectedAnn = null;
    redraw();
  };
  $("brush").oninput = (e) => {
    state.brush = Number(e.target.value);
    $("brushVal").textContent = String(state.brush);
  };
  $("btnClearMask").onclick = () => {
    const item = current();
    if (!item) return;
    pushHistory(item);
    maskCtx(item).clearRect(0, 0, item.mask.width, item.mask.height);
    item.rects = [];
    item.anns = [];
    state.selectedRect = null;
    state.selectedAnn = null;
    redraw();
  };
  $("btnInvert").onclick = () => {
    const item = current();
    if (!item) return;
    pushHistory(item);
    const c = maskCtx(item);
    const w = item.mask.width, h = item.mask.height;
    const data = c.getImageData(0, 0, w, h);
    const px = data.data;
    for (let i = 0; i < px.length; i += 4) {
      const on = px[i + 3] >= 128;
      if (on) {
        px[i] = px[i + 1] = px[i + 2] = px[i + 3] = 0;
      } else {
        px[i] = 255; px[i + 1] = 0; px[i + 2] = 0; px[i + 3] = 255;
      }
    }
    c.putImageData(data, 0, 0);
    redraw();
  };
  $("btnFit").onclick = () => {
    const item = current();
    if (!item) return;
    state.viewScale = fitScale(item.img);
    state.panX = 0;
    state.panY = 0;
    layout();
  };

  wrap.addEventListener("wheel", (e) => {
    if (document.body.dataset.tab !== "edit" || !current()) return;
    e.preventDefault();
    const item = current();
    const old = state.viewScale || fitScale(item.img);
    const next = Math.min(8, Math.max(0.1, old * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
    const rect = wrap.getBoundingClientRect();
    const cx = e.clientX - rect.left - wrap.clientWidth / 2;
    const cy = e.clientY - rect.top - wrap.clientHeight / 2;
    const k = next / old;
    state.panX = cx - (cx - state.panX) * k;
    state.panY = cy - (cy - state.panY) * k;
    state.viewScale = next;
    layout();
  }, { passive: false });

  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space") return;
    const tag = (e.target.tagName || "").toUpperCase();
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
    if (e.repeat) { e.preventDefault(); return; }
    state.spaceDown = true;
    paint.style.cursor = "grab";
    e.preventDefault();
  });
  document.addEventListener("keyup", (e) => {
    if (e.code !== "Space") return;
    state.spaceDown = false;
    if (!state.panning) paint.style.cursor = "crosshair";
  });
  $("btnCopyMask").onclick = () => {
    const src = current();
    if (!src) return;
    for (const it of state.images) {
      if (it === src) continue;
      pushHistory(it);
      const c = maskCtx(it);
      c.clearRect(0, 0, it.mask.width, it.mask.height);
      c.drawImage(src.mask, 0, 0, it.mask.width, it.mask.height);
      it.rects = [];
    }
    state.selectedRect = null;
    redraw();
  };
  $("btnClearList").onclick = () => {
    state.images.forEach((it) => URL.revokeObjectURL(it.url));
    state.images = [];
    state.active = 0;
    renderThumbs();
    layout();
  };

  $("btnAdd").onclick = () => {
    $("fileInput").dataset.mode = "replace";
    $("fileInput").click();
  };
  $("btnAppend").onclick = () => {
    $("fileInput").dataset.mode = "append";
    $("fileInput").click();
  };
  $("fileInput").onchange = (e) => {
    const append = $("fileInput").dataset.mode === "append";
    addFiles(e.target.files, append);
    e.target.value = "";
  };

  $("btnAddRef").onclick = () => $("refInput").click();
  $("refInput").onchange = async (e) => {
    for (const f of e.target.files) {
      if (!f.type.startsWith("image/")) continue;
      state.refs.push(await loadImageFile(f));
    }
    e.target.value = "";
    renderThumbs();
  };
  $("btnClearRef").onclick = () => {
    state.refs.forEach((it) => URL.revokeObjectURL(it.url));
    state.refs = [];
    renderThumbs();
  };

  async function loadEngines() {
    const r = await fetch("/api/engines");
    const j = await r.json();
    state.engines = j.engines || {};
    const sel = $("engine");
    sel.innerHTML = "";
    for (const [id, eng] of Object.entries(state.engines)) {
      const o = document.createElement("option");
      o.value = id;
      o.textContent = eng.label || id;
      sel.appendChild(o);
    }
    if (!sel.value) sel.value = "gpt";
    const cur = $("engine").value;
    if (state.engines[cur]?.i2i_model && !$("model").value) {
      $("model").value = state.engines[cur].i2i_model;
    }
    updatePrice();
  }
  $("engine").onchange = () => {
    const eng = state.engines[$("engine").value];
    if (eng?.t2i_model) $("model").value = eng.t2i_model;
    updatePrice();
  };

  $("toggleKey").onclick = () => {
    const el = $("apiKey");
    const show = el.type === "password";
    el.type = show ? "text" : "password";
    $("toggleKey").textContent = show ? "隐藏" : "显示";
  };

  function updatePrice() {
    const tab = document.body.dataset.tab;
    const el = $("priceEst");
    if (!el) return;
    if (tab === "video") { el.textContent = ""; return; }
    const eng = $("engine").value || "gpt";
    const tier = $("tier").value || "1k";
    const cnt = Number($("count").value || "1");
    const size = computeSize(tier, $("ratio").value);
    const p = (PRICE[eng] || {})[tier];
    const yuan = p == null ? "价格未知" : `预计 ¥${(p * cnt).toFixed(2)}`;
    el.textContent = `${yuan}（${eng} · ${tier.toUpperCase()} · ${$("ratio").value} → ${size} · ${cnt} 张）`;
  }
  ["tier", "ratio", "count", "engine"].forEach((id) => $(id).addEventListener("change", updatePrice));

  async function postJSON(url, obj) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(obj),
    });
    return r.json();
  }

  $("btnTest").onclick = async () => {
    saveSettings();
    setStatus($("connStatus"), "测试中…");
    try {
      const j = await postJSON("/api/test", { base_url: $("baseUrl").value, api_key: $("apiKey").value });
      if (j.ok) setStatus($("connStatus"), `连通。可用模型 ${j.model_count} 个（只显示一条也正常）。`, "ok");
      else setStatus($("connStatus"), `失败 HTTP ${j.status}: ${JSON.stringify(j.error)}`, "err");
    } catch (e) {
      setStatus($("connStatus"), String(e), "err");
    }
  };
  $("btnModels").onclick = async () => {
    saveSettings();
    setStatus($("connStatus"), "拉取模型列表…");
    try {
      const j = await postJSON("/api/models", { base_url: $("baseUrl").value, api_key: $("apiKey").value });
      const list = $("modelList");
      list.innerHTML = "";
      (j.models || []).forEach((id) => {
        const o = document.createElement("option");
        o.value = id;
        list.appendChild(o);
      });
      if (j.ok) {
        const all = j.models || [];
        const img = all.filter((m) => /image/i.test(m));
        const vid = all.filter((m) => /video|sora/i.test(m));
        setStatus(
          $("connStatus"),
          `模型 ${all.length} 个。生图：${img.join(", ") || "（无 image 关键词）"}。视频：${vid.join(", ") || "（无 video/sora 关键词）"}`,
          "ok"
        );
      } else setStatus($("connStatus"), `失败 HTTP ${j.status}`, "err");
    } catch (e) {
      setStatus($("connStatus"), String(e), "err");
    }
  };

  $("btnSaveDefaults").onclick = async () => {
    saveSettings();
    setStatus($("connStatus"), "保存到本机配置…");
    try {
      const r = await fetch("/api/save-defaults", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Image-Edit-Local": "1" },
        body: JSON.stringify({
          base_url: $("baseUrl").value.trim(),
          api_key: $("apiKey").value.trim(),
          model: $("model").value.trim(),
        }),
      });
      const j = await r.json();
      if (j.ok) setStatus($("connStatus"), `已写入本机配置 ${j.path}（${j.saved.join(", ")}）。代跑生成将使用这套接口。`, "ok");
      else setStatus($("connStatus"), "保存失败：" + (j.error || r.status), "err");
    } catch (e) {
      setStatus($("connStatus"), String(e), "err");
    }
  };

  function canvasToBlob(canvas, type) {
    return new Promise((resolve) => canvas.toBlob(resolve, type || "image/png"));
  }

  let viewerMime = "image/png";

  function syncGalleryActive() {
    document.querySelectorAll("#gallery .gitem").forEach((g, i) => g.classList.toggle("active", i === state.resultActive));
  }

  function b64ToFile(b64, name, mime) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], name, { type: mime || "image/png" });
  }

  function renderVersions() {
    const box = $("versions");
    if (!box) return;
    box.innerHTML = "";
    const item = current();
    const chain = (item && item.versions) || [];
    chain.forEach((v, i) => {
      const b = document.createElement("button");
      b.className = "vitem" + (item && item.versionIndex === i ? " active" : "");
      const img = document.createElement("img");
      img.src = v.url;
      img.alt = v.label;
      const lab = document.createElement("span");
      lab.className = "vlbl";
      lab.textContent = v.label;
      b.onclick = async () => {
        if (!item) return;
        item.versionIndex = i;
        item.img = v.img;
        item.url = v.url;
        item.file = v.file;
        item.name = v.name;
        const mask = document.createElement("canvas");
        mask.width = v.img.naturalWidth;
        mask.height = v.img.naturalHeight;
        item.mask = mask;
        item.rects = [];
        item.anns = [];
        item.history = [];
        item.future = [];
        state.selectedRect = null;
        renderVersions();
        renderThumbs();
        layout();
        $("outImg").src = v.url;
        $("btnDownload").href = v.url;
        $("outMeta").textContent = v.label + (v.meta ? " · " + v.meta : "");
      };
      b.append(img, lab);
      box.appendChild(b);
    });
  }

  async function adoptGenerated(images, mime, meta, sourceItem) {
    const list = images || [];
    if (!list.length) return;
    viewerMime = mime || "image/png";
    const ext = (mime || "").includes("jpeg") ? "jpg" : (mime || "").includes("webp") ? "webp" : "png";
    const parent = sourceItem || current();
    const files = list.map((it, i) => b64ToFile(it.b64, `v${Date.now()}-${i + 1}.${ext}`, mime));
    const loaded = [];
    for (const f of files) loaded.push(await loadImageFile(f));
    if (parent && document.body.dataset.tab === "edit") {
      if (!parent.versions || !parent.versions.length) {
        parent.versions = [{
          label: "原图",
          img: parent.img,
          url: parent.url,
          file: parent.file,
          name: parent.name,
          meta: "",
        }];
        parent.versionIndex = 0;
      }
      const start = parent.versions.filter((v) => v.label.startsWith("v")).length;
      loaded.forEach((it, i) => {
        parent.versions.push({
          label: "v" + (start + i + 1),
          img: it.img,
          url: it.url,
          file: it.file,
          name: it.name,
          meta,
        });
      });
      const last = parent.versions[parent.versions.length - 1];
      parent.versionIndex = parent.versions.length - 1;
      parent.img = last.img;
      parent.url = last.url;
      parent.file = last.file;
      parent.name = last.name;
      const mask = document.createElement("canvas");
      mask.width = last.img.naturalWidth;
      mask.height = last.img.naturalHeight;
      parent.mask = mask;
      parent.rects = [];
      parent.anns = [];
      parent.history = [];
      parent.future = [];
      state.selectedRect = null;
      renderVersions();
      renderThumbs();
      layout();
      $("outImg").src = last.url;
      $("btnDownload").href = last.url;
      $("outMeta").textContent = [last.label, meta].filter(Boolean).join(" · ");
    }
    showResults(list, mime, meta);
  }

  function showResults(images, mime, meta) {
    state.results = images || [];
    state.resultActive = 0;
    viewerMime = mime || "image/png";
    const gal = $("gallery");
    gal.innerHTML = "";
    state.results.forEach((it, i) => {
      const b = document.createElement("button");
      b.className = "gitem" + (i === 0 ? " active" : "");
      const img = document.createElement("img");
      img.src = `data:${viewerMime};base64,${it.b64}`;
      b.onclick = () => {
        state.resultActive = i;
        syncGalleryActive();
        $("outImg").src = img.src;
        $("btnDownload").href = img.src;
        $("outMeta").textContent = [it.size ? `第 ${i + 1} 张 · ${it.size}` : `第 ${i + 1} 张`, meta].filter(Boolean).join(" · ");
      };
      b.append(img);
      gal.appendChild(b);
    });
    if (state.results.length) {
      $("outImg").src = `data:${viewerMime};base64,${state.results[0].b64}`;
      $("btnDownload").href = $("outImg").src;
      $("outMeta").textContent = meta;
    }
  }

  $("btnRun").onclick = async () => {
    saveSettings();
    const tab = document.body.dataset.tab;
    const isT2I = tab === "t2i";
    const item = current();
    if (!isT2I && !item) { setStatus($("runStatus"), "先选一张内容图", "err"); return; }
    if (!connectionReady()) {
      setStatus($("runStatus"), "先填 Base URL 和 API Key", "err"); return;
    }
    commitAnnEditor();
    const hasAnns = !isT2I && state.images.some((it) =>
      (it.anns && it.anns.length) || (it.rects || []).some((r) => (r.text || "").trim())
    );
    if (!$("prompt").value.trim() && !hasAnns) {
      setStatus($("runStatus"), "先填提示词，或给红框写上标注", "err"); return;
    }
    if (!isT2I && $("scope").value === "mask" && $("maskMode").value !== "invert") {
      const painted = state.images.some((it) => maskHasContent(it));
      if (!painted) {
        setStatus($("runStatus"), "先涂红、框选或加标注（或把作用域改成整图重画）", "err");
        return;
      }
    }
    $("btnRun").disabled = true;
    setStatus($("runStatus"), "提交中…上游可能要等一会儿。超时或 5xx 后先核对任务/账单，避免重复扣费。");
    try {
      const fd = new FormData();
      fd.set("base_url", $("baseUrl").value.trim());
      fd.set("api_key", $("apiKey").value.trim());
      fd.set("model", $("model").value.trim());
      const extraAnn = !isT2I ? state.images.map(annotationPrompt).filter(Boolean).join(" ") : "";
      fd.set("prompt", [$("prompt").value.trim(), extraAnn].filter(Boolean).join("。"));
      fd.set("size", computeSize($("tier").value, $("ratio").value));
      fd.set("count", $("count").value);
      fd.set("output_format", $("outputFormat").value);
      fd.set("quality", $("quality").value);
      fd.set("pad_mode", $("padMode").value);
      fd.set("mask_mode", $("maskMode").value);
      fd.set("scope", isT2I ? "full" : $("scope").value);
      fd.set("engine", $("engine").value);
      fd.set("operation", isT2I ? "generate" : "edit");
      fd.set("ref_roles", $("refRoles").value);

      if (!isT2I) {
        let usedMask = false;
        for (const it of state.images) {
          bakeRects(it); // 提交前把矩形对象固化进遮罩位图
          const hasMask = maskHasContent({ ...it, anns: [] });
          const hasAnn = it.anns && it.anns.length;
          if (hasMask) {
            usedMask = true;
            const blob = await canvasToBlob(imageToCanvas(it.img));
            fd.append("image", blob, it.name || "content.png");
            const mb = await canvasToBlob(it.mask);
            fd.append("mask", mb, "mask.png");
          } else if (hasAnn) {
            const blob = await canvasToBlob(compositeWithAnnotations(it));
            fd.append("image", blob, it.name || "annotated.png");
          } else {
            const blob = await canvasToBlob(imageToCanvas(it.img));
            fd.append("image", blob, it.name || "content.png");
          }
        }
        if (!usedMask && hasAnns) {
          fd.set("scope", "full");
        }
        for (const it of state.refs) {
          const blob = await canvasToBlob(imageToCanvas(it.img));
          fd.append("ref", blob, it.name || "ref.png");
        }
      }
      const r = await fetch("/api/edit", { method: "POST", body: fd });
      const j = await r.json();
      if (!j.ok) {
        const errText = typeof j.error === "string" ? j.error : JSON.stringify(j.error);
        setStatus($("runStatus"), `HTTP ${j.status || ""} ${errText || "失败"}`, "err");
        return;
      }
      const ext = EXT[$("outputFormat").value] || "png";
      $("btnDownload").download = `image.${ext}`;
      const meta = [
          j.actual_size ? `实际 ${j.actual_size}` : "",
          j.requested_size ? `请求 ${j.requested_size}` : "",
          j.feed_bytes ? `投喂 ${(j.feed_bytes / 1024 / 1024).toFixed(2)} MiB` : "",
          (j.images && j.images.length) > 1 ? `${j.images.length} 张` : "",
          j.note || "",
        ].filter(Boolean).join(" · ");
      const imgs = j.images && j.images.length ? j.images : [{ b64: j.b64, size: j.actual_size }];
      await adoptGenerated(imgs, j.mime || "image/png", meta, isT2I ? null : item);
      setStatus($("runStatus"), isT2I ? "完成" : "完成，已切到新版本，可继续涂改", "ok");
    } catch (e) {
      setStatus($("runStatus"), String(e), "err");
    } finally {
      $("btnRun").disabled = false;
    }
  };

  // --- video tab -------------------------------------------------------------

  $("btnRunVideo").onclick = async () => {
    saveSettings();
    if (!connectionReady()) {
      setStatus($("vStatus"), "先填 Base URL 和 API Key", "err"); return;
    }
    if (!$("vModel").value.trim()) { setStatus($("vStatus"), "先填视频模型名（可点「获取模型列表」看有哪些）", "err"); return; }
    if (!$("vPrompt").value.trim()) { setStatus($("vStatus"), "先填提示词", "err"); return; }
    $("btnRunVideo").disabled = true;
    if (state.videoTimer) { clearInterval(state.videoTimer); state.videoTimer = null; }
    setStatus($("vStatus"), "提交任务中…");
    try {
      const j = await postJSON("/api/video", {
        base_url: $("baseUrl").value.trim(),
        api_key: $("apiKey").value.trim(),
        model: $("vModel").value.trim(),
        prompt: $("vPrompt").value.trim(),
        size: $("vSize").value,
        seconds: $("vSeconds").value,
      });
      if (!j.ok) {
        setStatus($("vStatus"), j.error || "提交失败", "err");
        return;
      }
      setStatus($("vStatus"), "任务已提交，轮询进度中…（一般几分钟）");
      const jobId = j.job_id;
      let dots = 0;
      state.videoTimer = setInterval(async () => {
        try {
          const s = await fetch("/api/video/status?job=" + jobId).then((r) => r.json());
          if (!s.ok) { clearInterval(state.videoTimer); state.videoTimer = null; setStatus($("vStatus"), s.error || "任务丢失", "err"); $("btnRunVideo").disabled = false; return; }
          dots = (dots + 1) % 4;
          const prog = s.progress != null ? ` ${Math.round(s.progress)}%` : "";
          setStatus($("vStatus"), `进行中${prog}（${s.phase || ""}）${".".repeat(dots)}`);
          if (s.status === "completed" && s.file_url) {
            clearInterval(state.videoTimer); state.videoTimer = null;
            $("outVideo").src = s.file_url;
            $("btnVideoDownload").href = s.file_url;
            $("btnVideoDownload").download = `${jobId}.mp4`;
            setStatus($("vStatus"), "视频完成，可播放/下载。", "ok");
            $("btnRunVideo").disabled = false;
          } else if (s.status === "failed") {
            clearInterval(state.videoTimer); state.videoTimer = null;
            setStatus($("vStatus"), "失败：" + (s.error || "未知错误"), "err");
            $("btnRunVideo").disabled = false;
          }
        } catch (e) {
          setStatus($("vStatus"), "轮询出错：" + e, "err");
        }
      }, 3000);
    } catch (e) {
      setStatus($("vStatus"), String(e), "err");
      $("btnRunVideo").disabled = false;
    }
  };

  function imageToCanvas(img) {
    const c = document.createElement("canvas");
    c.width = img.naturalWidth;
    c.height = img.naturalHeight;
    c.getContext("2d").drawImage(img, 0, 0);
    return c;
  }

  // --- 拖拽 / 粘贴放图（QQ、微信聊天图等） ---

  function extractDroppedImageUrl(e) {
    const dt = e.dataTransfer;
    if (!dt) return null;
    if (dt.files && dt.files.length) return null; // 有文件走 files 通道
    try {
      const html = dt.getData("text/html");
      if (html) {
        const src = new DOMParser().parseFromString(html, "text/html").querySelector("img")?.getAttribute("src");
        if (src && (src.startsWith("http://") || src.startsWith("https://") || src.startsWith("data:"))) return src;
      }
    } catch {}
    try {
      const uri = dt.getData("text/uri-list");
      const line = uri && uri.split(/\r?\n/).find((l) => l && !l.startsWith("#"));
      if (line && (line.startsWith("http://") || line.startsWith("https://"))) return line;
    } catch {}
    return null;
  }

  async function urlToFile(url) {
    if (url.startsWith("data:")) {
      const blob = await (await fetch(url)).blob();
      return new File([blob], "dropped.png", { type: blob.type || "image/png" });
    }
    // 先直连拉取（浏览器同源策略拦了就走本机服务代理）
    let blob = null;
    try {
      const r = await fetch(url, { mode: "cors" });
      if (r.ok) blob = await r.blob();
    } catch {}
    if (!blob) {
      const r = await fetch("/api/fetch-url?url=" + encodeURIComponent(url));
      if (r.headers.get("content-type")?.includes("application/json")) {
        const j = await r.json();
        throw new Error(j.error || "HTTP " + r.status);
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      blob = await r.blob();
    }
    if (!blob || blob.size < 64) throw new Error("拉到的内容不是有效图片");
    const ext = blob.type?.includes("jpeg") ? "jpg" : blob.type?.includes("webp") ? "webp" : "png";
    return new File([blob], "dropped-" + Date.now() + "." + ext, { type: blob.type || "image/png" });
  }

  wrap.addEventListener("dragover", (e) => {
    e.preventDefault();
    wrap.classList.add("dragging");
  });
  wrap.addEventListener("dragleave", (e) => {
    if (e.target === wrap) wrap.classList.remove("dragging");
  });
  wrap.addEventListener("drop", async (e) => {
    e.preventDefault();
    wrap.classList.remove("dragging");
    if (document.body.dataset.tab !== "edit") return;
    if (e.dataTransfer?.files?.length) {
      await addFiles(e.dataTransfer.files, true);
      return;
    }
    const url = extractDroppedImageUrl(e);
    if (!url) return;
    setStatus($("runStatus"), "正在拉取拖入的图片…");
    try {
      const file = await urlToFile(url);
      await addFiles([file], true);
      setStatus($("runStatus"), "已加入拖入的图片", "ok");
    } catch (err) {
      setStatus($("runStatus"), "拖入图片失败：" + (err.message || err), "err");
    }
  });

  document.addEventListener("paste", async (e) => {
    if (document.body.dataset.tab !== "edit") return;
    const items = e.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const it of items) {
      if (it.kind === "file" && it.type.startsWith("image/")) {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (!files.length) return;
    e.preventDefault();
    await addFiles(files, true);
    setStatus($("runStatus"), "已粘贴图片", "ok");
  });

  // --- fullscreen viewer: zoom / pan / multi / before-after -------------------

  const viewer = $("viewer");
  const viewerBody = $("viewerBody");
  const imgA = $("imgA");
  const imgB = $("imgB");
  const vState = { mode: "single", idx: 0, zoom: 1, px: 0, py: 0, split: 50 };
  let vDrag = null;
  let vSplitDrag = false;

  const MODE_LABEL = { single: "单张", hold: "按住对比", side: "并排对比", swipe: "滑动对比" };

  function setHold(on) {
    viewerBody.classList.toggle("reveal", !!on && vState.mode === "hold");
  }

  function applyZoom() {
    imgA.style.transform = `translate(${vState.px}px, ${vState.py}px) scale(${vState.zoom})`;
    $("vZoom").textContent = Math.round(vState.zoom * 100) + "%";
  }
  function resetPan() {
    vState.zoom = 1; vState.px = 0; vState.py = 0;
    applyZoom();
  }
  function viewerApplyMode() {
    viewerBody.className = "viewer-body " + vState.mode;
    viewerBody.style.setProperty("--split", vState.split + "%");
    $("viewerMode").textContent = MODE_LABEL[vState.mode];
    const multi = state.results.length > 1;
    $("viewerPrev").classList.toggle("show", multi);
    $("viewerNext").classList.toggle("show", multi);
  }

  function viewerUpdate() {
    const n = state.results.length;
    if (!n) return;
    vState.idx = ((vState.idx % n) + n) % n;
    const it = state.results[vState.idx];
    const src = `data:${viewerMime};base64,${it.b64}`;
    imgB.src = src;
    const item = current();
    const isEdit = document.body.dataset.tab === "edit" && item;
    if (vState.mode === "single") {
      imgA.src = src;
      imgA.style.transform = `translate(${vState.px}px, ${vState.py}px) scale(${vState.zoom})`;
      $("viewerInfo").textContent =
        `第 ${vState.idx + 1}/${n} 张` + (it.size ? ` · ${it.size}` : "") + " · 滚轮缩放 / 拖动平移 / 双击复位";
    } else {
      if (isEdit) {
        imgA.src = imageToCanvas(item.img).toDataURL("image/png");
        $("vLabelA").textContent = "原图";
        $("vLabelB").textContent = "改后";
        $("viewerInfo").textContent = `原图 vs 第 ${vState.idx + 1}/${n} 张` + (it.size ? ` · ${it.size}` : "");
      } else {
        const pi = (vState.idx + n - 1) % n;
        const prev = state.results[pi];
        imgA.src = `data:${viewerMime};base64,${prev.b64}`;
        $("vLabelA").textContent = `第 ${pi + 1} 张`;
        $("vLabelB").textContent = `第 ${vState.idx + 1} 张`;
        $("viewerInfo").textContent = `结果 ${pi + 1} vs ${vState.idx + 1}`;
      }
      resetPan();
    }
    state.resultActive = vState.idx;
    syncGalleryActive();
    $("outImg").src = src;
    $("btnDownload").href = src;
  }

  function openViewer(mode) {
    if (!state.results.length) return;
    vState.mode = mode;
    vState.idx = state.resultActive;
    resetPan();
    viewer.classList.add("open");
    viewerApplyMode();
    viewerUpdate();
  }
  function closeViewer() { viewer.classList.remove("open"); }

  $("outImg").onclick = () => {
    if (suppressOutClick) { suppressOutClick = false; return; }
    openViewer("single");
  };
  // 侧栏大图长按闪看原图（长按 280ms 生效；快速单击仍进全屏）
  const outImgEl = $("outImg");
  let outHoldTimer = null, holdActiveOut = false, outHoldOrig = "", suppressOutClick = false;
  outImgEl.addEventListener("pointerdown", () => {
    if (document.body.dataset.tab !== "edit" || !current() || !state.results.length) return;
    outHoldTimer = setTimeout(() => {
      holdActiveOut = true;
      outHoldOrig = outImgEl.src;
      outImgEl.src = imageToCanvas(current().img).toDataURL("image/png");
    }, 280);
  });
  const outHoldEnd = () => {
    if (outHoldTimer) { clearTimeout(outHoldTimer); outHoldTimer = null; }
    if (holdActiveOut) {
      outImgEl.src = outHoldOrig;
      holdActiveOut = false;
      suppressOutClick = true;
    }
  };
  outImgEl.addEventListener("pointerup", outHoldEnd);
  outImgEl.addEventListener("pointerleave", outHoldEnd);

  $("btnCompare").onclick = () => {
    if (!state.results.length) { setStatus($("runStatus"), "还没有结果", "err"); return; }
    const hasOrig = document.body.dataset.tab === "edit" && current();
    if (!hasOrig && state.results.length < 2) {
      setStatus($("runStatus"), "需要一张原图或两张结果才能对比", "err");
      return;
    }
    openViewer("hold");
  };
  $("btnSideBySide").onclick = () => {
    if (!state.results.length) { setStatus($("runStatus"), "还没有结果", "err"); return; }
    const hasOrig = document.body.dataset.tab === "edit" && current();
    if (!hasOrig && state.results.length < 2) {
      setStatus($("runStatus"), "需要一张原图或两张结果才能对比", "err");
      return;
    }
    openViewer("side");
  };
  $("viewerClose").onclick = closeViewer;
  $("viewerMode").onclick = () => {
    vState.mode = vState.mode === "single" ? "hold" : vState.mode === "hold" ? "side" : vState.mode === "side" ? "swipe" : "single";
    viewerApplyMode();
    viewerUpdate();
  };
  $("viewerPrev").onclick = () => { vState.idx--; viewerUpdate(); };
  $("viewerNext").onclick = () => { vState.idx++; viewerUpdate(); };
  viewer.addEventListener("click", (e) => { if (e.target === viewer) closeViewer(); });

  viewerBody.addEventListener("wheel", (e) => {
    if (vState.mode !== "single") return;
    e.preventDefault();
    vState.zoom = Math.min(10, Math.max(0.15, vState.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    applyZoom();
  }, { passive: false });
  viewerBody.addEventListener("pointerdown", (e) => {
    if (e.target === $("vDivider")) { vSplitDrag = true; viewerBody.setPointerCapture(e.pointerId); return; }
    if (vState.mode === "hold") {
      if (e.target === imgA || e.target === imgB || e.target === viewerBody) {
        setHold(true);
        viewerBody.setPointerCapture(e.pointerId);
      }
      return;
    }
    if (vState.mode !== "single" || e.target !== imgA) return;
    vDrag = { x: e.clientX - vState.px, y: e.clientY - vState.py };
    viewerBody.setPointerCapture(e.pointerId);
  });
  viewerBody.addEventListener("pointermove", (e) => {
    if (vSplitDrag) {
      const r = viewerBody.getBoundingClientRect();
      vState.split = Math.min(96, Math.max(4, ((e.clientX - r.left) / r.width) * 100));
      viewerBody.style.setProperty("--split", vState.split + "%");
      return;
    }
    if (!vDrag) return;
    vState.px = e.clientX - vDrag.x;
    vState.py = e.clientY - vDrag.y;
    applyZoom();
  });
  const endVDrag = () => { vDrag = null; vSplitDrag = false; setHold(false); };
  viewerBody.addEventListener("pointerup", endVDrag);
  viewerBody.addEventListener("pointercancel", endVDrag);
  viewerBody.addEventListener("dblclick", resetPan);
  document.addEventListener("keydown", (e) => {
    if (!viewer.classList.contains("open")) return;
    if (e.key === "Escape") closeViewer();
    else if (e.key === "ArrowLeft") { vState.idx--; viewerUpdate(); }
    else if (e.key === "ArrowRight") { vState.idx++; viewerUpdate(); }
    else if (e.code === "Space") { e.preventDefault(); setHold(true); }
  });
  document.addEventListener("keyup", (e) => {
    if (e.code === "Space") setHold(false);
  });

  $("outVideo").addEventListener("dblclick", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else $("outVideo").requestFullscreen?.();
  });

  ["baseUrl", "apiKey", "model", "prompt", "engine", "scope", "maskMode", "quality", "tier", "ratio", "count",
   "outputFormat", "padMode", "refRoles", "vModel", "vPrompt", "vSize", "vSeconds"]
    .forEach((id) => $(id).addEventListener("change", saveSettings));

  window.addEventListener("resize", layout);
  loadSettings();
  setTab(document.body.dataset.tab || "edit");
  loadDefaults().then(() => loadEngines()).catch((e) => setStatus($("connStatus"), String(e), "err"));
  layout();
})();
