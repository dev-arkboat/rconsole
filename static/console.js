"use strict";

const USER = window.RCONSOLE_USER || "user";
let tabs = [];
let activeId = null;

// ---------------------------------------------------------------------------
// xterm.js is heavy and only needed for live terminal tabs (nano, vim, REPLs).
// Load it on demand (once) so line-mode users don't pay the download/parse or
// memory cost on first paint. Consoles then cache it for subsequent visits.
// ---------------------------------------------------------------------------
let xtermLoad = null;
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error("failed to load " + src));
    document.head.appendChild(s);
  });
}
function ensureXterm() {
  if (window.Terminal && window.FitAddon) return Promise.resolve();
  if (!xtermLoad) {
    xtermLoad = Promise.all([
      loadScript("/static/xterm.js"),
      loadScript("/static/addon-fit.js"),
    ]);
  }
  return xtermLoad;
}

const elTabs = document.getElementById("tabs");
const elNew = document.getElementById("newtab");
const elOutput = document.getElementById("output");
const elPrompt = document.getElementById("prompt");
const elCmd = document.getElementById("cmd");

function promptText(cwd) {
  return `${USER}@rconsole:${cwd}$ `;
}

function usedTabNumbers() {
  const nums = new Set();
  tabs.forEach((t) => {
    const m = /^t(\d+)$/.exec(t.id);
    if (m) nums.add(parseInt(m[1], 10));
  });
  return nums;
}

// Reuse freed numbers: the lowest positive integer not already taken.
function nextTabNumber() {
  const nums = usedTabNumbers();
  let n = 1;
  while (nums.has(n)) n += 1;
  return n;
}

function makeTab(opts) {
  opts = opts || {};
  const id = opts.id || ("t" + nextTabNumber());
  const m = /^t(\d+)$/.exec(id);
  const num = m ? parseInt(m[1], 10) : 1;
  const tab = {
    id,
    name: opts.name || ("tab " + num),
    lines: [],
    cwd: opts.cwd || "/home/user",
    hist: [],
    histIdx: 0,
    interactive: false,
    interactivePrompt: "",
    draft: "",
    ptyActive: false,
    ptyWs: null,
    term: null,
    fitAddon: null,
    termRO: null,
    termHost: null,
    isTerminal: !!opts.isTerminal,
    cmd: opts.cmd || "",
    _exited: false,
    _killed: false,
    _reconnectTries: 0,
  };
  if (opts.first) {
    tab.lines.push({ text: "Rconsole — Made by MD ABU SALEHIN", cls: "term-green" });
    tab.lines.push({ text: "type 'help' to get started. Good Day.", cls: "term-dim" });
    tab.lines.push({ text: "" });
  }
  tabs.push(tab);
  renderTabs();
  return tab;
}

function newTab(focus = true) {
  const tab = makeTab({ first: tabs.length === 0 });
  if (focus) selectTab(tab.id);
  return tab;
}

function activeTab() {
  return tabs.find((t) => t.id === activeId);
}

function renderTabs() {
  elTabs.innerHTML = "";
  tabs.forEach((t) => {
    const d = document.createElement("div");
    d.className = "tab" + (t.id === activeId ? " active" : "");
    const label = document.createElement("span");
    label.textContent = t.name;
    d.appendChild(label);
    if (tabs.length > 1) {
      const close = document.createElement("span");
      close.className = "close";
      close.textContent = "×";
      close.onclick = (e) => {
        e.stopPropagation();
        closeTab(t.id);
      };
      d.appendChild(close);
    }
    d.onclick = () => selectTab(t.id);
    elTabs.appendChild(d);
  });
}

function closeTab(id) {
  const idx = tabs.findIndex((t) => t.id === id);
  if (idx === -1) return;
  const t = tabs[idx];
  t._killed = true;
  // A terminal tab's processes must be stopped when the user closes it. Tell
  // the server to kill the session (it pops the server-side tab too); a silent
  // disconnect would instead preserve the process so the user can return.
  if (t.isTerminal && t.ptyWs && t.ptyWs.readyState === 1) {
    try { t.ptyWs.send(JSON.stringify({ t: "kill" })); } catch (e) { /* ignore */ }
  }
  closePty(id);
  fetch("/api/close_tab", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab: id }),
  }).catch(() => {});
  tabs.splice(idx, 1);
  if (activeId === id) {
    activeId = tabs.length ? tabs[Math.max(0, idx - 1)].id : null;
  }
  if (!tabs.length) {
    newTab();
    return;
  }
  renderTabs();
  selectTab(activeId);
}

function selectTab(id) {
  const prev = activeTab();
  if (prev && elCmd) prev.draft = elCmd.value;
  activeId = id;
  renderTabs();
  renderOutput();
  const t = activeTab();
  // Re-attach a persistent terminal that survived a refresh / tab switch.
  if (t && t.isTerminal && !t.ptyActive && t.cmd) {
    openPty(t.cmd, t.id);
    return;
  }
  if (elCmd && t) elCmd.value = t.draft || "";
  updateView();
  if (!isPtyActive() && elCmd) elCmd.focus();
}

function appendLineEl(ln) {
  const div = document.createElement("div");
  div.className = "line" + (ln.cls ? " " + ln.cls : "");
  div.textContent = ln.text;
  elOutput.appendChild(div);
}

function renderOutput() {
  const t = activeTab();
  // Only rebuild the whole DOM when the line list itself changed (tab switch
  // or clear). Otherwise append just the lines added since the last render —
  // this keeps long sessions smooth instead of re-parsing every line each time.
  if (t._renderedLines !== t.lines) {
    elOutput.innerHTML = "";
    t.lines.forEach(appendLineEl);
    t._renderedLines = t.lines;
  } else {
    for (let i = t._renderedCount || 0; i < t.lines.length; i++) {
      appendLineEl(t.lines[i]);
    }
  }
  t._renderedCount = t.lines.length;
  // prompt
  if (t.interactive) {
    elPrompt.textContent = t.interactivePrompt;
    elPrompt.className = "interactive";
  } else {
    elPrompt.textContent = promptText(t.cwd);
    elPrompt.className = "";
  }
  const sc = elOutput.parentElement;
  sc.scrollTop = sc.scrollHeight;
  // Return focus to the input only when the user is already there, so we don't
  // yank focus (and re-open the soft keyboard) on every render on mobile.
  if (!isPtyActive() &&
      (document.activeElement === elCmd || document.activeElement === document.body)) {
    elCmd.focus({ preventScroll: true });
  }
}

function appendLines(text, cls) {
  if (text === undefined || text === null) return;
  const t = activeTab();
  String(text).split("\n").forEach((l) => {
    t.lines.push({ text: l === "" ? " " : l, cls: cls || "" });
  });
  // Bound the scrollback so very long sessions don't grow the DOM/memory
  // without limit (which would eventually make every render janky). Trim
  // occasionally and force a single rebuild rather than per-line.
  const CAP = 4000;
  if (t.lines.length > CAP) {
    t.lines.splice(0, t.lines.length - CAP);
    t._renderedLines = null;
  }
}

async function send(cmd) {
  const t = activeTab();
  const body = JSON.stringify({ cmd, tab: t.id });
  let resp;
  try {
    resp = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  } catch (e) {
    return;
  }
  if (resp.status === 401) {
    window.location = "/login";
    return;
  }
  const data = await resp.json();
  if (data.redirect) {
    window.location = data.redirect;
    return;
  }

  if (data.editor) {
    openCodeEditor(data.editor);
    return;
  }

  if (data.clear) t.lines = [];

  if (data.pty) {
    t.isTerminal = true;
    t.cmd = data.cmd;
    openPty(data.cmd, t.id);
    return;
  }

  if (data.output !== undefined && data.output !== "") {
    appendLines(data.output, "");
  }
  if (data.prompt !== undefined) {
    t.interactive = true;
    t.interactivePrompt = data.prompt;
  } else {
    t.interactive = false;
    t.interactivePrompt = "";
  }
  if (data.cwd) t.cwd = data.cwd;
  renderOutput();
}

// ---------------------------------------------------------------------------
// Interactive PTY terminal (xterm.js + websocket). Each tab owns its own
// terminal instance + websocket, so a session (nano, vim, REPL, ...) keeps
// running in the background and reappears when you return to that tab.
// ---------------------------------------------------------------------------
function isPtyActive() {
  const t = activeTab();
  return !!(t && t.ptyActive);
}

function sendPtyTo(t, d) {
  if (t && t.ptyWs && t.ptyWs.readyState === 1) {
    t.ptyWs.send(JSON.stringify({ t: "data", d }));
  }
}

async function openPty(cmd, tabId) {
  const t = tabs.find((x) => x.id === tabId);
  if (!t || t.ptyActive) return;
  // Tear down any prior terminal instance (e.g. when re-attaching after a
  // transient websocket drop) so we don't leak hosts / observers / sockets.
  if (t.term) { try { t.term.dispose(); } catch (e) { /* ignore */ } t.term = null; t.fitAddon = null; }
  if (t.termRO) { try { t.termRO.disconnect(); } catch (e) { /* ignore */ } t.termRO = null; }
  if (t.termHost && t.termHost.parentNode) t.termHost.parentNode.removeChild(t.termHost);
  t.termHost = null;
  t.ptyActive = true;
  t.isTerminal = true;
  t.cmd = cmd;
  t.ptyText = "";
  t._exited = false;
  t._killed = false;

  const host = document.createElement("div");
  host.className = "term-host";
  host.id = "term-" + tabId;
  document.getElementById("terms").appendChild(host);
  t.termHost = host;

  // Defer terminal setup until xterm.js (lazy-loaded) is available.
  try {
    await ensureXterm();
  } catch (e) {
    t.ptyActive = false;
    activeTab().lines.push({ text: "Failed to load terminal components.", cls: "term-red" });
    renderOutput();
    elCmd.focus();
    return;
  }

  const Terminal = window.Terminal;
  const FitAddon = window.FitAddon;
  t.term = new Terminal({
    cursorBlink: true,
    // Canvas renderer is dramatically faster than the DOM renderer for
    // full-screen TUIs like nano/vim — it's what keeps typing responsive.
    rendererType: "canvas",
    fontSize: 14,
    fontFamily: "Consolas, 'Courier New', monospace",
    theme: { background: "#000000", foreground: "#d6e2ef" },
  });
  t.fitAddon = new FitAddon.FitAddon();
  t.term.loadAddon(t.fitAddon);
  t.term.open(host);

  // xterm captures keystrokes through its own hidden textarea. On mobile that
  // textarea inherits the browser's autocorrect/autocapitalize and (on iOS)
  // triggers zoom-on-focus — both add latency and mangle editor input. Harden
  // it the same way we did for the line-mode input.
  const ta = t.term.textarea;
  if (ta) {
    ta.setAttribute("autocomplete", "off");
    ta.setAttribute("autocorrect", "off");
    ta.setAttribute("autocapitalize", "off");
    ta.setAttribute("spellcheck", "false");
    ta.style.fontSize = "16px";
  }

  // Fit whenever the terminal box changes size (initial show, window resize,
  // or the mobile soft-keyboard opening/closing) so it always fills the width.
  let fitScheduled = false;
  let lastFitW = 0, lastFitH = 0, lastFitTime = 0;
  const doFit = () => {
    if (fitScheduled) return;
    fitScheduled = true;
    requestAnimationFrame(() => {
      fitScheduled = false;
      const w = host.clientWidth, h = host.clientHeight;
      // Skip when the host is hidden (display:none) so we never tell the backend
      // to resize a terminal to 0x0, which can break running programs.
      if (w === 0 || h === 0) return;
      // Break any resize feedback loop: fit() can change the rendered host size,
      // which re-fires ResizeObserver, which calls fit() again — pegging the CPU
      // and flooding the backend with resize messages (and freezing the terminal
      // on mobile). Only refit when the box actually changed and not too often.
      const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
      if ((w === lastFitW && h === lastFitH) || now - lastFitTime < 120) return;
      lastFitW = w; lastFitH = h; lastFitTime = now;
      try { t.fitAddon.fit(); } catch (e) { /* ignore */ }
      if (t.ptyWs && t.ptyWs.readyState === 1) {
        t.ptyWs.send(JSON.stringify({ t: "resize", cols: t.term.cols, rows: t.term.rows }));
      }
    });
  };
  const ro = new ResizeObserver(doFit);
  ro.observe(host);
  t.termRO = ro;
  requestAnimationFrame(doFit);
  setTimeout(doFit, 60);
  setTimeout(doFit, 250);

  // On touch devices the terminal must hold focus to receive keystrokes.
  host.addEventListener("pointerdown", () => t.term.focus());
  t.term.focus();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url =
    `${proto}://${location.host}/ws?tab=${encodeURIComponent(tabId)}` +
    `&cmd=${encodeURIComponent(cmd)}`;
  const ws = new WebSocket(url);
  t.ptyWs = ws;

  ws.onopen = () => {
    t._reconnectTries = 0;
    ws.send(JSON.stringify({ t: "resize", cols: t.term.cols, rows: t.term.rows }));
  };
  ws.onmessage = (e) => {
    // Control frames are prefixed with SOH (\x01) so raw terminal output can
    // never be misinterpreted as one (e.g. a program printing '{"t":"exit"}').
    if (typeof e.data === "string" && e.data.charCodeAt(0) === 1) {
      try {
        const msg = JSON.parse(e.data.slice(1));
        if (msg && msg.t === "exit") {
          onPtyExit(tabId);
        }
      } catch (err) { /* ignore malformed control frame */ }
      return;
    }
    if (t.ptyText !== undefined) t.ptyText += e.data;
    t.term.write(e.data);
  };
  ws.onclose = () => {
    // The process really ended (server told us) or the user killed it: drop to
    // the line console normally.
    if (t._exited || t._killed) { closePty(tabId); return; }
    // Transient disconnect (network blip, mobile backgrounding, keyboard
    // animation). Re-attach the still-living server session instead of stranding
    // the user in the line console mid-edit.
    t.ptyActive = false;
    t.ptyWs = null;
    t._reconnectTries = (t._reconnectTries || 0) + 1;
    if (t._reconnectTries <= 5) {
      setTimeout(() => {
        const cur = tabs.find((x) => x.id === tabId);
        if (cur && !cur._killed && activeTab() === cur) openPty(cur.cmd, tabId);
      }, 700 * t._reconnectTries);
    } else {
      closePty(tabId);
    }
  };
  ws.onerror = ws.onclose;

  t.term.onData((d) => {
    sendPtyTo(t, d);
  });

  updateView();
}

let resizeRaf = null;
function onPtyResize() {
  if (resizeRaf) return;
  resizeRaf = requestAnimationFrame(() => {
    resizeRaf = null;
    const t = activeTab();
    if (!t || !t.ptyActive || !t.term || !t.fitAddon) return;
    // Never push a degenerate size to the backend (can kill the child).
    if (t.term.cols <= 0 || t.term.rows <= 0) return;
    try {
      t.fitAddon.fit();
      if (t.ptyWs && t.ptyWs.readyState === 1) {
        t.ptyWs.send(
          JSON.stringify({ t: "resize", cols: t.term.cols, rows: t.term.rows })
        );
      }
    } catch (e) { /* ignore */ }
  });
}

function closePty(tabId) {
  const t = tabs.find((x) => x.id === tabId);
  if (!t || !t.ptyActive) return;
  t.ptyActive = false;
  setCtrl(false);
  if (t.ptyWs) {
    try { t.ptyWs.close(); } catch (e) { /* ignore */ }
    t.ptyWs = null;
  }
  if (t.term) {
    try { t.term.dispose(); } catch (e) { /* ignore */ }
    t.term = null;
    t.fitAddon = null;
  }
  if (t.termRO) {
    try { t.termRO.disconnect(); } catch (e) { /* ignore */ }
    t.termRO = null;
  }
  if (t.termHost && t.termHost.parentNode) {
    t.termHost.parentNode.removeChild(t.termHost);
  }
  t.termHost = null;

  if (activeId === tabId) {
    renderOutput();
    updateView();
    elCmd.focus();
  }
}

// Show the active tab's view: its live terminal (if it has one) or the line
// console. Background terminals keep running and are merely hidden.
function stripAnsi(s) {
  return String(s)
    .replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "")
    .replace(/\x1b[()][AB0]/g, "")
    .replace(/\x1b[=>]/g, "")
    .replace(/\x1b[78]/g, "");
}

// The PTY process ended on its own (one-shot command, or the user quit a
// full-screen program). Capture the output into the console's scrollback and
// drop back to the line console so the user isn't stuck in the dead terminal.
function onPtyExit(tabId) {
  const t = tabs.find((x) => x.id === tabId);
  if (!t) return;
  t._exited = true;
  if (t.ptyText) {
    const lines = stripAnsi(t.ptyText).replace(/\r/g, "").split("\n");
    for (const l of lines) {
      if (l.trim() !== "") t.lines.push({ text: l });
    }
  }
  // Tell the server to tear down the finished session so it doesn't linger.
  if (t.ptyWs && t.ptyWs.readyState === 1) {
    try { t.ptyWs.send(JSON.stringify({ t: "kill" })); } catch (e) { /* ignore */ }
  }
  t.isTerminal = false;
  t.cmd = "";
  t.ptyText = "";
  closePty(tabId);
  renderOutput();
  updateView();
  elCmd.focus();
}

function updateView() {
  const t = activeTab();
  const app = document.getElementById("app");
  document.querySelectorAll(".term-host").forEach((h) => { h.style.display = "none"; });
  if (t && t.ptyActive && t.termHost) {
    app.classList.add("pty");
    t.termHost.style.display = "block";
    if (t.termHost.clientWidth > 0 && t.termHost.clientHeight > 0) {
      try { t.fitAddon.fit(); } catch (e) { /* ignore */ }
    }
    t.term.focus();
  } else {
    app.classList.remove("pty");
  }
}

function submitCmd() {
  const t = activeTab();
  const val = elCmd.value;
  elCmd.value = "";
  // echo the command (or the interactive answer) to output
  if (t.interactive) {
    t.lines.push({ text: t.interactivePrompt + val, cls: "term-amber" });
  } else {
    if (val.trim() !== "") {
      t.hist.push(val);
      t.histIdx = t.hist.length;
    }
    t.lines.push({ text: promptText(t.cwd) + val });
  }
  renderOutput();
  send(val);
}

// A <form> submit reliably catches the Enter key from mobile soft keyboards,
// which don't always emit a clean keydown "Enter" event.
document.getElementById("promptform").addEventListener("submit", (e) => {
  e.preventDefault();
  submitCmd();
});

elCmd.addEventListener("keydown", (e) => {
  const t = activeTab();
  if (e.key === "ArrowUp" && !t.interactive) {
    if (t.histIdx > 0) {
      t.histIdx -= 1;
      elCmd.value = t.hist[t.histIdx] || "";
      e.preventDefault();
    }
  } else if (e.key === "ArrowDown" && !t.interactive) {
    if (t.histIdx < t.hist.length - 1) {
      t.histIdx += 1;
      elCmd.value = t.hist[t.histIdx] || "";
    } else {
      t.histIdx = t.hist.length;
      elCmd.value = "";
    }
    e.preventDefault();
  }
});

elNew.onclick = () => newTab();
document.getElementById("screen").addEventListener("click", () => elCmd.focus());

// ---------------------------------------------------------------------------
// Mobile key bar: Tab / Esc / Ctrl / arrow buttons for touch keyboards.
// ---------------------------------------------------------------------------
if (window.matchMedia && (window.matchMedia("(max-width: 768px)").matches ||
    "ontouchstart" in window || navigator.maxTouchPoints > 0)) {
  document.body.classList.add("touch");
}

// Keep the footer (input + key bar) visible above the soft keyboard by sizing
// #app to the visible viewport rather than the full window height.
let vpRaf = null;
let lastVpHeight = 0;
function fitToViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  // The soft keyboard animating or the caret scrolling fires resize/scroll
  // events constantly while you type. Only react when the *height* actually
  // changed (keyboard open/close, rotation) — otherwise we'd flood the backend
  // with resize messages and stall the terminal on mobile.
  if (vv.height === lastVpHeight) return;
  lastVpHeight = vv.height;
  if (vpRaf) cancelAnimationFrame(vpRaf);
  vpRaf = requestAnimationFrame(() => {
    document.getElementById("app").style.height = vv.height + "px";
    onPtyResize();
  });
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", fitToViewport);
  window.visualViewport.addEventListener("scroll", fitToViewport);
  fitToViewport();
}

function insertAtCursor(text) {
  const s = elCmd.selectionStart ?? elCmd.value.length;
  const e = elCmd.selectionEnd ?? elCmd.value.length;
  elCmd.value = elCmd.value.slice(0, s) + text + elCmd.value.slice(e);
  elCmd.selectionStart = elCmd.selectionEnd = s + text.length;
}

let ctrlActive = false;
let ctrlResetTimer = null;
const elKeybar = document.getElementById("keybar");

function setCtrl(active) {
  ctrlActive = active;
  const btn = elKeybar.querySelector('[data-mod="ctrl"]');
  if (btn) btn.classList.toggle("active", active);
  if (ctrlResetTimer) { clearTimeout(ctrlResetTimer); ctrlResetTimer = null; }
  // Auto-release the sticky modifier so it can never get "stuck" and turn the
  // next normal keystroke into a control char (e.g. Ctrl+X quitting nano).
  if (active) ctrlResetTimer = setTimeout(() => setCtrl(false), 3000);
}

function sendPty(d) {
  const t = activeTab();
  sendPtyTo(t, d);
}

function handlePtyKey(b) {
  const t = activeTab();
  if (!t || !t.ptyActive) return;
  setCtrl(false);
  if (b.dataset.ctrl) {
    // Explicit control combos (e.g. ^X to quit nano). Sending the control char
    // directly means normal typing is NEVER turned into a control sequence, so
    // a stray modifier can't accidentally quit the editor mid-edit.
    const ch = String.fromCharCode(b.dataset.ctrl.toUpperCase().charCodeAt(0) - 64);
    sendPtyTo(t, ch);
    if (t.term) t.term.focus();
    return;
  }
  const key = b.dataset.key;
  if (key === "Tab") {
    sendPtyTo(t, "\t");
  } else if (key === "Esc") {
    sendPtyTo(t, "\x1b");
  } else if (key && key.startsWith("Arrow")) {
    const seq = {
      ArrowUp: "\x1b[A", ArrowDown: "\x1b[B",
      ArrowLeft: "\x1b[D", ArrowRight: "\x1b[C",
    }[key];
    sendPtyTo(t, seq);
  }
  if (t.term) t.term.focus();
}

function moveCursor(delta) {
  const p = elCmd.selectionStart ?? elCmd.value.length;
  const np = Math.max(0, Math.min(elCmd.value.length, p + delta));
  elCmd.selectionStart = elCmd.selectionEnd = np;
}

function historyNav(dir) {
  const t = activeTab();
  if (t.interactive) return;
  if (dir < 0) {
    if (t.histIdx > 0) {
      t.histIdx -= 1;
      elCmd.value = t.hist[t.histIdx] || "";
    }
  } else {
    if (t.histIdx < t.hist.length - 1) {
      t.histIdx += 1;
      elCmd.value = t.hist[t.histIdx] || "";
    } else {
      t.histIdx = t.hist.length;
      elCmd.value = "";
    }
  }
  const end = elCmd.value.length;
  elCmd.selectionStart = elCmd.selectionEnd = end;
}

function isEditorPrompt() {
  const t = activeTab();
  if (!t) return false;
  return /^(edit|L\d+|ins)> /.test(t.interactivePrompt || "");
}

function doKeybarAction(b) {
  const t = activeTab();
  if (isPtyActive()) {
    handlePtyKey(b);
    return;
  }
  const key = b.dataset.key;
  if (t.interactive) {
    // Inside the in-console editor, arrow keys move the line cursor (up/down)
    // so you can navigate like nano on a phone; left/right still move the caret
    // within the command you're typing, and Tab/Esc behave normally.
    if (key === "ArrowUp" && isEditorPrompt()) { send("up"); return; }
    if (key === "ArrowDown" && isEditorPrompt()) { send("down"); return; }
    if (key === "Tab") { insertAtCursor("\t"); elCmd.focus({ preventScroll: true }); return; }
    if (key === "Esc") { elCmd.value = ""; elCmd.focus({ preventScroll: true }); return; }
    if (key === "ArrowLeft") { moveCursor(-1); elCmd.focus({ preventScroll: true }); return; }
    if (key === "ArrowRight") { moveCursor(1); elCmd.focus({ preventScroll: true }); return; }
    return;
  }
  if (key) {
    if (key === "Tab") {
      insertAtCursor("\t");
    } else if (key === "Esc") {
      elCmd.value = "";
    } else if (key === "ArrowLeft") {
      moveCursor(-1);
    } else if (key === "ArrowRight") {
      moveCursor(1);
    } else if (key === "ArrowUp") {
      historyNav(-1);
    } else if (key === "ArrowDown") {
      historyNav(1);
    }
    elCmd.focus({ preventScroll: true });
  }
}

// pointerdown + preventDefault keeps the text input focused (and the soft
// keyboard open) when a key-bar button is tapped, and removes the tap delay.
// Use click (not pointerdown+preventDefault): on mobile a tap is a valid user
// gesture that lets the soft keyboard open/remain, whereas preventDefault can
// swallow the gesture and leave the keyboard shut so taps appear to do nothing.
elKeybar.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  doKeybarAction(b);
});

// Keep the active terminal fitted on window (non-viewport) resizes.
window.addEventListener("resize", onPtyResize);

// ---------------------------------------------------------------------------
// Built-in code editor (client-side). All editing + syntax highlighting happen
// in the browser, so it stays smooth on phones and laptops alike; only Save
// touches the server.
// ---------------------------------------------------------------------------
const elEditor = document.getElementById("editor");
const elEditorFile = document.getElementById("editor-file");
const elEditorLang = document.getElementById("editor-lang");
const elEditorStatus = document.getElementById("editor-status");
const elEditorInput = document.getElementById("editor-input");
const elEditorPre = document.getElementById("editor-highlight");
const elEditorCode = elEditorPre.querySelector("code");
const elEditorGutter = document.getElementById("editor-gutter");
let editorState = null;
let hlRaf = null;

function escHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function langFromPath(p) {
  const ext = (p.split(".").pop() || "").toLowerCase();
  const m = {
    py: "python", pyw: "python", js: "javascript", mjs: "javascript",
    cjs: "javascript", ts: "typescript", tsx: "typescript", jsx: "javascript",
    json: "json", html: "html", htm: "html", xml: "xml", vue: "html",
    css: "css", scss: "css", less: "css", yaml: "yaml", yml: "yaml",
    md: "markdown", sh: "bash", bash: "bash", zsh: "bash", txt: "text",
    csv: "text", ini: "ini", conf: "ini",
  };
  return m[ext] || "text";
}

// Per-language token rules: [className, regexSource]. Comments/strings first.
const HL_RULES = {
  python: [
    ["comment", "#[^\\n]*"],
    ["string", "'''[\\s\\S]*?'''"],
    ["string", '"""[\\s\\S]*?"""'],
    ["string", "'[^'\\\\\\n]*'"],
    ["string", '"[^"\\\\\\n]*"'],
    ["number", "\\b\\d+(\\.\\d+)?\\b"],
    ["keyword", "\\b(def|class|import|from|as|return|if|elif|else|for|while|break|continue|pass|with|try|except|finally|raise|lambda|yield|global|nonlocal|assert|del|in|is|not|and|or|async|await|print)\\b"],
    ["bool", "\\b(True|False|None)\\b"],
    ["function", "([A-Za-z_]\\w*)\\s*(?=\\()"],
    ["builtin", "\\b(self|cls|super|len|range|str|int|float|list|dict|set|tuple|open|enumerate|zip|map|filter|sorted|reversed|sum|min|max|abs|type|isinstance|object|Exception)\\b"],
  ],
  javascript: [
    ["comment", "//[^\\n]*"],
    ["comment", "/\\*[\\s\\S]*?\\*/"],
    ["string", "`[^`]*`"],
    ["string", "'[^'\\\\\\n]*'"],
    ["string", '"[^"\\\\\\n]*"'],
    ["number", "\\b\\d+(\\.\\d+)?\\b"],
    ["keyword", "\\b(function|return|if|else|for|while|var|let|const|class|extends|new|import|export|from|default|try|catch|finally|throw|typeof|instanceof|in|of|await|async|yield|switch|case|break|continue|do|void|delete|this|super)\\b"],
    ["bool", "\\b(true|false|null|undefined|NaN)\\b"],
    ["function", "([A-Za-z_$]\\w*)\\s*(?=\\()"],
    ["builtin", "\\b(console|document|window|require|module|exports|process|Math|JSON|Object|Array|String|Number|Boolean|Promise|Map|Set|Symbol)\\b"],
  ],
  json: [
    ["string", '"[^"\\\\\\n]*"(?=\\s*:)'],
    ["string", '"[^"\\\\\\n]*"'],
    ["number", "-?\\b\\d+(\\.\\d+)?([eE][+-]?\\d+)?\\b"],
    ["bool", "\\b(true|false|null)\\b"],
    ["punct", "[{}\\[\\]:,]"],
  ],
  html: [
    ["comment", "<!--[\\s\\S]*?-->"],
    ["string", '"[^"\\\\\\n]*"|\'[^\']*\''],
    ["tag", "</?[a-zA-Z][a-zA-Z0-9]*"],
    ["attr", "\\b[a-zA-Z-]+(?==)"],
  ],
  css: [
    ["comment", "/\\*[\\s\\S]*?\\*/"],
    ["string", '"[^"\\\\\\n]*"|\'[^\']*\''],
    ["keyword", "@media|@import|@keyframes|@font-face|@supports"],
    ["attr", "[a-zA-Z-]+(?=\\s*:)"],
    ["number", "#[0-9a-fA-F]{3,8}\\b|\\b\\d+(\\.\\d+)?(px|em|rem|%|s|ms|vh|vw|fr|pt|deg)?\\b"],
  ],
  yaml: [
    ["comment", "#[^\\n]*"],
    ["string", '"[^"\\\\\\n]*"|\'[^\']*\''],
    ["attr", "^[\\s-]*[A-Za-z0-9_.-]+(?=:)"],
    ["bool", "\\b(true|false|null|yes|no|~)\\b"],
    ["number", "\\b\\d+(\\.\\d+)?\\b"],
  ],
  bash: [
    ["comment", "#[^\\n]*"],
    ["string", '"[^"\\\\\\n]*"|\'[^\']*\''],
    ["keyword", "\\b(if|then|else|elif|fi|for|in|do|done|while|case|esac|function|return|export|local|echo|cd|exit|source|set)\\b"],
    ["builtin", "\\b(ls|cd|pwd|cat|echo|grep|sed|awk|cp|mv|rm|mkdir|touch|sudo|apt|pip|python|python3|git|chmod|chown)\\b"],
  ],
  markdown: [
    ["comment", "^#{1,6} .*"],
    ["string", "`[^`]*`"],
    ["attr", "\\[[^\\]]*\\]\\([^\\)]*\\)"],
    ["keyword", "^\\s*[-*+] "],
    ["bool", "^\\s*\\d+\\. "],
  ],
};

// Build a compiled highlighter per language (cached).
const _hlCache = {};
function getHighlighter(lang) {
  if (lang === "text" || !HL_RULES[lang]) return null;
  if (_hlCache[lang]) return _hlCache[lang];
  const rules = HL_RULES[lang];
  const parts = rules.map((r, i) => `(?<g${i}>${r[1]})`);
  const re = new RegExp(parts.join("|"), "g");
  const fn = function (code) {
    let out = "", last = 0, m;
    re.lastIndex = 0;
    while ((m = re.exec(code))) {
      if (m.index > last) out += escHtml(code.slice(last, m.index));
      let cls = null;
      for (let i = 0; i < rules.length; i++) {
        if (m.groups[`g${i}`] != null) { cls = rules[i][0]; break; }
      }
      const text = m[0];
      if (cls) out += `<span class="tok-${cls}">${escHtml(text)}</span>`;
      else out += escHtml(text);
      last = m.index + text.length;
      if (text.length === 0) re.lastIndex++;
    }
    out += escHtml(code.slice(last));
    return out;
  };
  _hlCache[lang] = fn;
  return fn;
}

function renderHighlight() {
  if (!editorState) return;
  const code = elEditorInput.value;
  const hl = getHighlighter(editorState.lang);
  if (hl && code.length <= 300000) {
    elEditorCode.innerHTML = hl(code) + "\n";
  } else {
    elEditorCode.textContent = code;
  }
  updateGutter();
}

function updateGutter() {
  const n = elEditorInput.value.split("\n").length;
  let s = "";
  for (let i = 1; i <= n; i++) s += i + "\n";
  elEditorGutter.textContent = s;
}

function scheduleHighlight() {
  if (hlRaf) return;
  hlRaf = requestAnimationFrame(() => {
    hlRaf = null;
    renderHighlight();
  });
}

async function openCodeEditor(path) {
  let content = "", exists = false;
  try {
    const r = await fetch("/api/file?path=" + encodeURIComponent(path));
    if (r.ok) { const j = await r.json(); content = j.content || ""; exists = !!j.exists; }
  } catch (e) { /* offline: open empty */ }
  editorState = { path, lang: langFromPath(path), dirty: false, exists };
  elEditor.removeAttribute("hidden");
  elEditorFile.textContent = (path.split("/").pop() || path) + (exists ? "" : "  (new)");
  elEditorLang.textContent = editorState.lang;
  elEditorStatus.textContent = "";
  elEditorInput.value = content;
  renderHighlight();
  elEditorInput.scrollTop = 0;
  elEditorGutter.scrollTop = 0;
  document.getElementById("app").classList.add("editing");
  setTimeout(() => elEditorInput.focus({ preventScroll: true }), 60);
}

function closeCodeEditor() {
  if (editorState && editorState.dirty) {
    if (!confirm("Discard unsaved changes to " + editorState.path + "?")) return;
  }
  document.getElementById("app").classList.remove("editing");
  elEditor.setAttribute("hidden", "");
  editorState = null;
  elCmd.focus();
}

async function saveCodeEditor() {
  if (!editorState) return;
  elEditorStatus.textContent = "Saving…";
  try {
    const r = await fetch("/api/file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: editorState.path, content: elEditorInput.value }),
    });
    const j = await r.json();
    if (j.ok) {
      editorState.dirty = false;
      editorState.exists = true;
      elEditorStatus.textContent = "Saved " + new Date().toLocaleTimeString();
    } else {
      elEditorStatus.textContent = "Save failed: " + (j.error || "unknown");
    }
  } catch (e) {
    elEditorStatus.textContent = "Save failed: " + e;
  }
}

function indentSelection(dir) {
  const ta = elEditorInput;
  const s = ta.selectionStart, e = ta.selectionEnd;
  const val = ta.value;
  if (s === e) {
    if (dir > 0) {
      ta.value = val.slice(0, s) + "    " + val.slice(s);
      ta.selectionStart = ta.selectionEnd = s + 4;
    } else {
      const lineStart = val.lastIndexOf("\n", s - 1) + 1;
      const mm = /^( {1,4}|\t)/.exec(val.slice(lineStart));
      if (mm) {
        ta.value = val.slice(0, lineStart) + val.slice(lineStart + mm[0].length);
        ta.selectionStart = ta.selectionEnd = Math.max(lineStart, s - mm[0].length);
      }
    }
    return;
  }
  const lineStart = val.lastIndexOf("\n", s - 1) + 1;
  const block = val.slice(lineStart, e);
  const newBlock = dir > 0 ? block.replace(/^/gm, "    ") : block.replace(/^( {1,4}|\t)/gm, "");
  ta.value = val.slice(0, lineStart) + newBlock + val.slice(e);
  ta.selectionStart = lineStart;
  ta.selectionEnd = lineStart + newBlock.length;
}

elEditorInput.addEventListener("input", () => {
  if (editorState) {
    editorState.dirty = true;
    elEditorStatus.textContent = "● unsaved";
  }
  scheduleHighlight();
});
elEditorInput.addEventListener("scroll", () => {
  elEditorPre.scrollTop = elEditorInput.scrollTop;
  elEditorPre.scrollLeft = elEditorInput.scrollLeft;
  elEditorGutter.scrollTop = elEditorInput.scrollTop;
});
elEditorInput.addEventListener("keydown", (e) => {
  if (e.key === "Tab") {
    e.preventDefault();
    indentSelection(e.shiftKey ? -1 : 1);
    if (editorState) { editorState.dirty = true; elEditorStatus.textContent = "● unsaved"; }
    scheduleHighlight();
  } else if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
    e.preventDefault();
    saveCodeEditor();
  } else if (e.key === "Escape") {
    e.preventDefault();
    closeCodeEditor();
  }
});
elEditorGutter.addEventListener("click", (e) => {
  const rect = elEditorGutter.getBoundingClientRect();
  const lh = parseFloat(getComputedStyle(elEditorInput).lineHeight) || 21;
  const line = Math.floor((e.clientY - rect.top + elEditorGutter.scrollTop) / lh) + 1;
  const lines = elEditorInput.value.split("\n");
  if (line < 1 || line > lines.length) return;
  let pos = 0;
  for (let i = 0; i < line - 1; i++) pos += lines[i].length + 1;
  elEditorInput.focus();
  elEditorInput.selectionStart = elEditorInput.selectionEnd = pos;
});
document.getElementById("editor-save").addEventListener("click", saveCodeEditor);
document.getElementById("editor-close").addEventListener("click", closeCodeEditor);

// On load, rebuild tabs from the server so persistent terminal sessions
// (e.g. a long-running bot) survive a page refresh and can be re-attached.
async function init() {
  let serverTabs = [];
  try {
    const r = await fetch("/api/tabs");
    if (r.ok) serverTabs = (await r.json()).tabs || [];
  } catch (e) { /* offline: start fresh */ }

  if (!serverTabs.length) {
    newTab();
    return;
  }
  serverTabs.forEach((st, i) => {
    makeTab({
      id: st.id,
      name: st.name,
      cwd: st.cwd,
      isTerminal: st.is_terminal,
      cmd: st.cmd,
      first: i === 0,
    });
  });
  selectTab(tabs[0].id);
}

init();
