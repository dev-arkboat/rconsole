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
  t.ptyActive = true;
  t.isTerminal = true;
  t.cmd = cmd;
  t.ptyText = "";

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
    ws.send(JSON.stringify({ t: "resize", cols: t.term.cols, rows: t.term.rows }));
  };
  ws.onmessage = (e) => {
    // A control message from the server: the PTY process has exited, so we
    // return to the line console automatically instead of stranding the user.
    if (typeof e.data === "string") {
      try {
        const msg = JSON.parse(e.data);
        if (msg && msg.t === "exit") {
          onPtyExit(tabId);
          return;
        }
      } catch (err) { /* not JSON: regular terminal output */ }
    }
    if (t.ptyText !== undefined) t.ptyText += e.data;
    t.term.write(e.data);
  };
  ws.onclose = () => closePty(tabId);
  ws.onerror = () => closePty(tabId);

  t.term.onData((d) => {
    // Sticky Ctrl: the next typed letter is sent as a control character.
    if (ctrlActive && d.length === 1 && /[a-zA-Z]/.test(d)) {
      const ctrlChar = String.fromCharCode(d.toUpperCase().charCodeAt(0) - 64);
      setCtrl(false);
      sendPtyTo(t, ctrlChar);
      return;
    }
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
function fitToViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
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
const elKeybar = document.getElementById("keybar");

function setCtrl(active) {
  ctrlActive = active;
  const btn = elKeybar.querySelector('[data-mod="ctrl"]');
  if (btn) btn.classList.toggle("active", active);
}

function sendPty(d) {
  const t = activeTab();
  sendPtyTo(t, d);
}

function handlePtyKey(b) {
  const t = activeTab();
  if (!t || !t.ptyActive) return;
  const key = b.dataset.key;
  if (b.dataset.mod === "ctrl") {
    setCtrl(!ctrlActive);
    return;
  }
  setCtrl(false);
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

function doKeybarAction(b) {
  const t = activeTab();
  if (isPtyActive()) {
    handlePtyKey(b);
    return;
  }
  if (b.dataset.key) {
    const key = b.dataset.key;
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
  } else if (b.dataset.mod === "ctrl") {
    setCtrl(!ctrlActive);
  }
}

// pointerdown + preventDefault keeps the text input focused (and the soft
// keyboard open) when a key-bar button is tapped, and removes the tap delay.
elKeybar.addEventListener("pointerdown", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  e.preventDefault();
  doKeybarAction(b);
});

// When Ctrl is held (sticky), forward modifier state on physical keystrokes so
// the backend can treat them as control sequences once interactivity lands.
elCmd.addEventListener("keydown", (e) => {
  if (ctrlActive && e.key.length === 1) {
    e.ctrlKey = true;
  }
});

// Keep the active terminal fitted on window (non-viewport) resizes.
window.addEventListener("resize", onPtyResize);

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
