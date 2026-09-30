const $ = (id) => document.getElementById(id);

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.min.js";

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};

const state = {
  sessionId: store.get("sessionId"),
  version: null,       // version being viewed
  latest: null,        // newest version number
  pdf: null,           // PDFDocumentProxy
  zoom: "fit",         // "fit" or a number (1 = 100%)
  renderToken: 0,
  busy: false,
  loaded: false,       // editor has this session's chat + PDF loaded
  loginEnabled: false, // server requires a login
};

const icon = (name) => `<svg class="icon"><use href="#i-${name}"/></svg>`;

class AuthError extends Error {}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new AuthError("Please sign in");
  }
  if (!res.ok && !data.status) throw new Error(data.detail || data.message || `Request failed (${res.status})`);
  return data;
}

function toast(text, kind = "err") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = icon(kind === "err" ? "alert" : "check");
  const span = document.createElement("span");
  span.textContent = text;
  el.appendChild(span);
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

/* ---------------- View switching ---------------- */

// Two views: home ("/") and editor ("#editor"). Using the URL hash lets the browser's
// Back/Forward buttons move between them, and a reload stays on the current view.

function showLogin() {
  $("login").hidden = false;
  $("landing").hidden = true;
  $("editor").hidden = true;
  $("newResume").hidden = true;
  $("logout").hidden = true;
  $("loginError").hidden = true;
  $("loginEmail").focus();
}

function showLanding({ push = true } = {}) {
  $("login").hidden = true;
  $("logout").hidden = !state.loginEnabled;
  $("landing").hidden = false;
  $("editor").hidden = true;
  $("newResume").hidden = true;
  resetDropzone();
  updateContinueCard();
  if (push && location.hash) history.pushState(null, "", location.pathname);
}

function showEditor({ push = true } = {}) {
  $("login").hidden = true;
  $("logout").hidden = !state.loginEnabled;
  $("landing").hidden = true;
  $("editor").hidden = false;
  $("newResume").hidden = false;
  setMobilePanel("chat");
  if (push && location.hash !== "#editor") history.pushState(null, "", "#editor");
  // The viewer had no size while hidden, so redraw the pages now that it's visible.
  requestAnimationFrame(() => renderPdf({ keepScroll: true }));
}

async function openSession({ push = true } = {}) {
  showEditor({ push });
  if (state.loaded) return;
  const list = await api(`/api/sessions/${state.sessionId}/versions`);
  if (!list.length) throw new Error("This resume has no versions");
  welcome(true);
  $("suggestions").hidden = list.length > 1;
  await loadVersion(list[list.length - 1].version);
  state.loaded = true;
}

async function updateContinueCard() {
  const btn = $("continueBtn");
  if (!state.sessionId) {
    btn.hidden = true;
    return;
  }
  try {
    const list = await api(`/api/sessions/${state.sessionId}/versions`);
    if (!list.length) throw new Error("empty");
    const last = list[list.length - 1];
    $("continueMeta").textContent = `Version ${last.version} · ${last.summary || "Your resume"}`;
    btn.hidden = false;
  } catch {
    forgetSession();
    btn.hidden = true;
  }
}

function forgetSession() {
  store.del("sessionId");
  state.sessionId = null;
  state.loaded = false;
  state.version = state.latest = null;
  if (state.pdf) state.pdf.destroy();
  state.pdf = null;
  $("pages").replaceChildren();
}

function setMobilePanel(name) {
  $("editor").dataset.active = name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.panel === name));
  if (name === "preview") renderPdf();
}

/* ---------------- Chat ---------------- */

function addMessage(text, { from = "bot", kind = null, version = null, via = null } = {}) {
  const row = document.createElement("div");
  row.className = `msg ${from}`;
  const bubble = document.createElement("div");
  bubble.className = `bubble ${kind || ""}`;
  if (kind) {
    const iconName = { ok: "check", warn: "info", err: "alert" }[kind];
    bubble.classList.add("status");
    bubble.innerHTML = icon(iconName);
    const span = document.createElement("span");
    span.textContent = text;
    if (version) {
      const tag = document.createElement("span");
      tag.className = "vtag";
      tag.textContent = `v${version}`;
      span.appendChild(tag);
    }
    if (via) {
      const tag = document.createElement("span");
      tag.className = "vtag";
      tag.textContent = `via ${via}`;
      span.appendChild(tag);
    }
    bubble.appendChild(span);
  } else {
    bubble.textContent = text;
  }
  if (from === "bot") {
    const av = document.createElement("div");
    av.className = "avatar";
    av.innerHTML = icon("spark");
    row.appendChild(av);
  }
  row.appendChild(bubble);
  $("messages").appendChild(row);
  $("messages").scrollTop = $("messages").scrollHeight;
  return row;
}

function showTyping() {
  const row = document.createElement("div");
  row.className = "msg bot";
  row.innerHTML = `<div class="avatar">${icon("spark")}</div><div class="bubble typing"><span></span><span></span><span></span></div>`;
  $("messages").appendChild(row);
  $("messages").scrollTop = $("messages").scrollHeight;
  return row;
}

function welcome(returning = false) {
  $("messages").innerHTML = "";
  addMessage(
    returning
      ? "Welcome back! Continuing with your last resume. What would you like to change?"
      : "Your resume is ready. Tell me what to change — reword a bullet, add a skill, update a job title, or add a new section."
  );
}

function setBusy(on) {
  state.busy = on;
  updateSendState();
  $("undo").disabled = on || state.latest <= 1;
}

function updateSendState() {
  $("send").disabled = state.busy || !$("input").value.trim();
}

async function sendEdit(message) {
  $("suggestions").hidden = true;
  addMessage(message, { from: "user" });
  setBusy(true);
  const typing = showTyping();
  try {
    const data = await api(`/api/sessions/${state.sessionId}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, provider: $("provider").value || null }),
    });
    typing.remove();
    if (data.status === "ok") {
      addMessage(data.message, { kind: "ok", version: data.version, via: data.provider });
      await loadVersion(data.version);
    } else if (data.status === "refused") {
      addMessage(data.message, { kind: "warn" });
    } else {
      addMessage(data.message || "Something went wrong.", { kind: "err" });
    }
  } catch (e) {
    typing.remove();
    addMessage(e.message, { kind: "err" });
  } finally {
    setBusy(false);
  }
}

/* ---------------- Versions ---------------- */

async function refreshVersions() {
  const list = await api(`/api/sessions/${state.sessionId}/versions`);
  state.latest = list.length ? list[list.length - 1].version : null;
  const sel = $("versions");
  sel.innerHTML = "";
  for (const v of [...list].reverse()) {
    const opt = document.createElement("option");
    opt.value = v.version;
    const label = v.version === state.latest ? "Current" : `v${v.version}`;
    opt.textContent = `${label} · ${v.summary || ""}`.slice(0, 60);
    sel.appendChild(opt);
  }
  sel.value = state.version;
  const viewingLatest = state.version === state.latest;
  $("undo").hidden = !viewingLatest;
  $("undo").disabled = state.busy || state.latest <= 1;
  $("restore").hidden = viewingLatest;
  return list;
}

async function loadVersion(version) {
  state.version = version;
  const base = `/api/sessions/${state.sessionId}/versions/${version}`;
  $("dlPdf").href = `${base}/pdf?download=true`;
  $("dlTex").href = `${base}/tex`;
  await Promise.all([refreshVersions(), loadPdf(`${base}/pdf`)]);
}

async function revertTo(version, note) {
  setBusy(true);
  try {
    const data = await api(`/api/sessions/${state.sessionId}/revert/${version}`, { method: "POST" });
    addMessage(note || data.message, { kind: "ok", version: data.version });
    await loadVersion(data.version);
  } catch (e) {
    toast(e.message);
  } finally {
    setBusy(false);
  }
}

/* ---------------- PDF viewer (PDF.js) ---------------- */

async function loadPdf(url) {
  $("viewerLoading").hidden = false;
  try {
    const buf = await (await fetch(url)).arrayBuffer();
    const doc = await pdfjsLib.getDocument({ data: buf }).promise;
    if (state.pdf) state.pdf.destroy();
    state.pdf = doc;
    await renderPdf({ keepScroll: true });
  } catch (e) {
    toast("Could not load the PDF preview.");
  } finally {
    $("viewerLoading").hidden = true;
  }
}

function fitScale(page) {
  const vp = page.getViewport({ scale: 1 });
  const available = $("viewer").clientWidth - 48;
  // Fit to width, but don't blow the page up past a comfortable reading size on wide screens.
  return Math.max(0.3, Math.min(available / vp.width, 1.35));
}

async function renderPdf({ keepScroll = false } = {}) {
  if (!state.pdf || $("viewer").clientWidth === 0) return;
  const token = ++state.renderToken;
  const viewer = $("viewer");
  const ratio = keepScroll && viewer.scrollHeight > 0 ? viewer.scrollTop / viewer.scrollHeight : 0;
  const dpr = window.devicePixelRatio || 1;

  const first = await state.pdf.getPage(1);
  const scale = state.zoom === "fit" ? fitScale(first) : state.zoom;
  $("zoomLabel").textContent = state.zoom === "fit" ? "Fit" : `${Math.round(scale * 100)}%`;

  const canvases = [];
  for (let n = 1; n <= state.pdf.numPages; n++) {
    const page = n === 1 ? first : await state.pdf.getPage(n);
    const vp = page.getViewport({ scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(vp.width * dpr);
    canvas.height = Math.floor(vp.height * dpr);
    canvas.style.width = `${Math.floor(vp.width)}px`;
    canvas.style.height = `${Math.floor(vp.height)}px`;
    await page.render({
      canvasContext: canvas.getContext("2d"),
      viewport: vp,
      transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null,
    }).promise;
    if (token !== state.renderToken) return; // a newer render started
    canvases.push(canvas);
  }
  $("pages").replaceChildren(...canvases);
  viewer.scrollTop = ratio * viewer.scrollHeight;
}

function setZoom(z) {
  if (!state.pdf) return;
  if (z !== "fit") z = Math.round(Math.max(0.5, Math.min(z, 3)) * 100) / 100;
  state.zoom = z;
  renderPdf({ keepScroll: true });
}

async function currentScale() {
  if (state.zoom !== "fit") return state.zoom;
  return fitScale(await state.pdf.getPage(1));
}

let resizeTimer;
new ResizeObserver(() => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => state.zoom === "fit" && renderPdf({ keepScroll: true }), 150);
}).observe($("viewer"));

/* ---------------- Upload ---------------- */

let stepTimers = [];

function setStep(i) {
  document.querySelectorAll(".steps li").forEach((li) => {
    const n = Number(li.dataset.step);
    li.classList.toggle("done", n < i);
    li.classList.toggle("active", n === i);
  });
}

function resetDropzone() {
  stepTimers.forEach(clearTimeout);
  $("drop").classList.remove("busy", "over");
  $("dropIdle").hidden = false;
  $("dropBusy").hidden = true;
  $("file").value = "";
}

async function upload(file) {
  if (!file || $("drop").classList.contains("busy")) return;
  if (file.size > 5 * 1024 * 1024) return toast("That file is larger than 5 MB.");
  if (!/\.(pdf|docx|txt)$/i.test(file.name)) return toast("Please upload a PDF, DOCX or TXT file.");

  $("drop").classList.add("busy");
  $("dropIdle").hidden = true;
  $("dropBusy").hidden = false;
  $("fileName").textContent = file.name;
  setStep(0);
  stepTimers = [setTimeout(() => setStep(1), 1200), setTimeout(() => setStep(2), 9000)];

  const form = new FormData();
  form.append("file", file);
  try {
    const provider = encodeURIComponent($("provider").value || "");
    const data = await api(`/api/upload?provider=${provider}`, { method: "POST", body: form });
    stepTimers.forEach(clearTimeout);
    setStep(3);
    state.sessionId = data.session_id;
    state.zoom = "fit";
    store.set("sessionId", state.sessionId);
    showEditor();
    welcome();
    $("suggestions").hidden = false;
    await loadVersion(data.version);
    state.loaded = true;
  } catch (e) {
    toast(e.message);
    resetDropzone();
  }
}

/* ---------------- Config ---------------- */

async function loadConfig() {
  const sel = $("provider");
  try {
    const cfg = await api("/api/config");
    state.loginEnabled = cfg.login;
    sel.innerHTML = "";
    if (!cfg.providers.length) {
      sel.innerHTML = "<option>No API key</option>";
      sel.disabled = true;
      document.querySelector(".model-pill .dot").style.background = "var(--danger)";
      return;
    }
    const options = cfg.providers.length > 1
      ? [{ id: "", label: "Auto" }, ...cfg.providers]
      : cfg.providers;
    for (const p of options) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.label;
      sel.appendChild(opt);
    }
    sel.title = cfg.providers.length > 1
      ? "Auto tries each provider in turn if one is busy"
      : "AI model";
  } catch (e) {
    if (e instanceof AuthError) throw e;
    sel.innerHTML = "<option>Offline</option>";
  }
}

/* ---------------- Wiring ---------------- */

const drop = $("drop");
drop.addEventListener("click", () => !drop.classList.contains("busy") && $("file").click());
drop.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && drop.click());
$("file").addEventListener("change", (e) => upload(e.target.files[0]));
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  upload(e.dataTransfer.files[0]);
});

const input = $("input");
function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}
input.addEventListener("input", () => { autosize(); updateSendState(); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});
$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = "";
  autosize();
  updateSendState();
  sendEdit(text);
});
document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    input.value = chip.textContent;
    autosize();
    updateSendState();
    input.focus();
  })
);

$("versions").addEventListener("change", (e) => loadVersion(Number(e.target.value)));
$("undo").addEventListener("click", () => {
  if (state.latest > 1) revertTo(state.latest - 1, `Undid the last change (restored v${state.latest - 1})`);
});
$("restore").addEventListener("click", () => revertTo(state.version));

$("zoomIn").addEventListener("click", async () => setZoom((await currentScale()) + 0.1));
$("zoomOut").addEventListener("click", async () => setZoom((await currentScale()) - 0.1));
$("zoomFit").addEventListener("click", () => setZoom("fit"));

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => setMobilePanel(t.dataset.panel)));

$("newResume").addEventListener("click", () => {
  forgetSession();
  showLanding();
});

// Logo -> home page. The current resume is kept and offered via "Continue editing".
document.querySelector(".brand").addEventListener("click", (e) => {
  e.preventDefault();
  if ($("landing").hidden) showLanding();
});

$("continueBtn").addEventListener("click", () =>
  openSession().catch((e) => {
    toast(e.message);
    forgetSession();
    showLanding();
  })
);

window.addEventListener("popstate", () => {
  if (location.hash === "#editor" && state.sessionId) {
    openSession({ push: false }).catch(() => showLanding({ push: false }));
  } else {
    showLanding({ push: false });
  }
});

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("loginBtn").disabled = true;
  $("loginError").hidden = true;
  try {
    await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("loginEmail").value, password: $("loginPassword").value }),
    });
    $("loginPassword").value = "";
    await start();
  } catch (err) {
    $("loginError").textContent = err.message;
    $("loginError").hidden = false;
  } finally {
    $("loginBtn").disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  state.loaded = false;
  showLogin();
});

async function start() {
  try {
    await loadConfig();
  } catch {
    return; // not signed in: the login screen is showing
  }
  if (state.sessionId && location.hash === "#editor") {
    try {
      await openSession({ push: false });
      return;
    } catch {
      forgetSession();
    }
  }
  showLanding({ push: false });
}

start();
