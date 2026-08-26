"use strict";

const USER = window.RCONSOLE_USER || "user";
let tabs = [];
let activeId = null;

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

function renderOutput() {
  const t = activeTab();
  elOutput.innerHTML = "";
  t.lines.forEach((ln) => {
    const div = document.createElement("div");
    div.className = "line" + (ln.cls ? " " + ln.cls : "");
    div.textContent = ln.text;
    elOutput.appendChild(div);
  });
  // prompt
  if (t.interactive) {
    elPrompt.textContent = t.interactivePrompt;
    elPrompt.className = "interactive";
  } else {
    elPrompt.textContent = promptText(t.cwd);
    elPrompt.className = "";
  }
  elOutput.parentElement.scrollTop = elOutput.parentElement.scrollHeight;
  elCmd.focus();
}

function appendLines(text, cls) {
  if (text === undefined || text === null) return;
  String(text).split("\n").forEach((l) => {
    activeTab().lines.push({ text: l === "" ? " " : l, cls: cls || "" });
  });
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

function openPty(cmd, tabId) {
  const t = tabs.find((x) => x.id === tabId);
  if (!t || t.ptyActive) return;
  t.ptyActive = true;
  t.isTerminal = true;
  t.cmd = cmd;

  const host = document.createElement("div");
  host.className = "term-host";
  host.id = "term-" + tabId;
  document.getElementById("terms").appendChild(host);
  t.termHost = host;

  t.term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: "Consolas, 'Courier New', monospace",
    theme: { background: "#000000", foreground: "#d6e2ef" },
  });
  t.fitAddon = new FitAddon.FitAddon();
  t.term.loadAddon(t.fitAddon);
  t.term.open(host);

  // Fit whenever the terminal box changes size (initial show, window resize,
  // or the mobile soft-keyboard opening/closing) so it always fills the width.
  const doFit = () => {
    // Skip when the host is hidden (display:none) so we never tell the backend
    // to resize a terminal to 0x0, which can break running programs.
    if (host.clientWidth === 0 || host.clientHeight === 0) return;
    try { t.fitAddon.fit(); } catch (e) { /* ignore */ }
    if (t.ptyWs && t.ptyWs.readyState === 1) {
      t.ptyWs.send(JSON.stringify({ t: "resize", cols: t.term.cols, rows: t.term.rows }));
    }
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
  ws.onmessage = (e) => { t.term.write(e.data); };
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

function onPtyResize() {
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

elCmd.addEventListener("keydown", (e) => {
  const t = activeTab();
  if (e.key === "Enter") {
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
  } else if (e.key === "ArrowUp" && !t.interactive) {
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
function fitToViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  document.getElementById("app").style.height = vv.height + "px";
  onPtyResize();
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
}

elKeybar.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
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
    } else {
      // Arrow keys: forward a real keydown so history/cursor logic runs.
      elCmd.dispatchEvent(new KeyboardEvent("keydown", {
        key, bubbles: true, cancelable: true,
      }));
    }
    elCmd.focus();
  } else if (b.dataset.mod === "ctrl") {
    setCtrl(!ctrlActive);
  }
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
