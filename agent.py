"""Native in-console AI agent for rconsole.

This is a line-mode alternative to the opencode TUI: instead of spawning a
browser-hostile PTY TUI, it calls an OpenAI-compatible (or Anthropic) API
directly and can run tools (bash, read/write files, list dirs) inside
rconsole's own host environment. Everything renders through the normal line
console, so there is no terminal-handshake breakage.

Provider selection (first match wins), read from `sudo env` or the process env:
  1. OpenCode Zen  -> OPENCODE_API_KEY / ZEN_API_KEY
                     (OpenAI-compatible gateway at https://opencode.ai/zen/v1)
  2. Anthropic     -> ANTHROPIC_API_KEY
  3. OpenAI        -> OPENAI_API_KEY  (+ OPENAI_BASE_URL for OpenRouter/local)

Robustness features:
  - connection pooling + automatic retry/backoff on 429/5xx (requests.Session)
  - persistent conversation across `sudo agent` invocations (stored in the tab)
  - automatic context compaction when the transcript grows too large
  - token usage reporting per turn and per session
  - a small safety guard that refuses catastrophic shell commands
  - slash commands: /help /clear /model /provider /keys /models /compact /system
"""
import json
import os
import re
from pathlib import Path

import requests

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry

TIMEOUT = 180
MAX_STEPS = 16
TOOL_CLIP = 4000          # chars shown to the user for a tool result
TOOL_KEEP = 8000          # chars of a tool result kept in the model context
HISTORY_BUDGET = 30000    # chars of serialized transcript before compaction
KEEP_RECENT = 8           # recent messages kept verbatim during compaction

# Reusable HTTP session: connection pooling + retries on transient failures.
_SESSION = requests.Session()
_SESSION.mount(
    "https://",
    requests.adapters.HTTPAdapter(
        max_retries=Retry(
            total=4,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
        )
    ),
)
_SESSION.mount(
    "http://",
    requests.adapters.HTTPAdapter(
        max_retries=Retry(
            total=4,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
        )
    ),
)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command in the current working directory and return "
                "its combined stdout/stderr. Use for git, ls, installing packages, "
                "running tests, moving files, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "the shell command to run"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file (path relative to cwd).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file (creates/overwrites; makes parent dirs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files in a directory (defaults to the current directory).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
]


# ANSI helpers (kept local; console.js renders these in the line console).
R = "\x1b[0m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BOLD = "\x1b[1m"

AGENT_HELP = (
    BOLD + "Agent slash commands" + R + "\n"
    "  /help            show this help\n"
    "  /clear           forget the conversation (keep provider/model)\n"
    "  /model <name>    switch the model for this session\n"
    "  /provider        show the current provider / model / base url\n"
    "  /keys            list configured API keys (masked)\n"
    "  /models          list models available from the current provider\n"
    "  /compact         summarise older context now\n"
    "  /system          print the system prompt\n"
    "  /exit, /quit     leave the agent\n"
    "\n"
    "Tips:\n"
    "  - Run `sudo agent` with no args for an interactive session; the chat\n"
    "    persists across commands until you /clear or /exit.\n"
    "  - `sudo agent \"do something\"` runs a single task then stays open.\n"
    "  - `sudo agent models` / `sudo agent help` print without opening a session.\n"
    "  - Tools: bash, read_file, write_file, list_dir."
)


def _clip(text, n=TOOL_CLIP):
    text = text or ""
    if len(text) > n:
        return text[:n] + f"\n... ({len(text) - n} more chars clipped)"
    return text


def _resolve(ctx, target):
    target = (target or "").strip()
    p = Path(target)
    if not p.is_absolute():
        p = Path(ctx["tab"]["cwd"]) / target
    return p.resolve()


# Language guess from a filename, for syntax-highlighted code boxes in the UI.
_LANG_EXT = {
    "py": "python", "pyw": "python", "js": "javascript", "mjs": "javascript",
    "cjs": "javascript", "ts": "typescript", "tsx": "typescript", "jsx": "javascript",
    "json": "json", "html": "html", "htm": "html", "xml": "xml", "vue": "html",
    "css": "css", "scss": "css", "less": "css", "yaml": "yaml", "yml": "yaml",
    "md": "markdown", "sh": "bash", "bash": "bash", "zsh": "bash", "txt": "text",
    "csv": "text", "ini": "ini", "conf": "ini", "rb": "ruby", "go": "go",
    "rs": "rust", "java": "java", "c": "c", "h": "c", "cpp": "cpp", "cxx": "cpp",
    "hpp": "cpp", "sql": "sql", "r": "r", "php": "php", "lua": "lua",
}


def _lang_from_ext(path):
    ext = (path.split(".")[-1] if "." in path else "").lower()
    return _LANG_EXT.get(ext, "")


def _fence(lang, text, n=2000):
    """Wrap text in a markdown code fence so the frontend renders a code box."""
    text = (text or "").rstrip("\n")
    if len(text) > n:
        text = text[:n] + f"\n... ({len(text) - n} more chars clipped)"
    return "```" + (lang or "") + "\n" + text + "\n```"


def _is_long(text, lines=10, chars=1200):
    t = text or ""
    return (len(t.splitlines()) > lines) or (len(t) > chars)


def _tool_tick(name, args, result):
    """Format a tool result as a live tick: file content in a code box, and long
    shell/dir output in a scrollable 'bash' box."""
    if name in ("read_file", "write_file"):
        path = args.get("path", "")
        lang = _lang_from_ext(path)
        head = CYAN + "→ " + name + "(" + json.dumps(path, ensure_ascii=False)[:160] + ")" + R
        return head + "\n" + _fence(lang, result)
    call_desc = name + "(" + json.dumps(args, ensure_ascii=False)[:160] + ")"
    head = CYAN + "→ " + call_desc + R
    if _is_long(result):
        # Long output -> a scrollable, ANSI-stripped "bash" box.
        return head + "\n" + _fence("log", result, n=TOOL_CLIP)
    return head + "\n" + _clip(result, 2000)


_BASH_GUARD = [
    re.compile(r"rm\s+-rf\s+/\s*$"),
    re.compile(r"rm\s+-rf\s+/\*"),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"dd\s+if=/dev/"),
    re.compile(r">\s*/dev/sd"),
    re.compile(r"chmod\s+-R\s+777\s+/"),
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b"),
]


def _guard_bash(cmd):
    for pat in _BASH_GUARD:
        if pat.search(cmd or ""):
            return (
                "refused: that command looks destructive or system-breaking. "
                "If you really mean to run it, do so directly in the console "
                "(not via the agent tool)."
            )
    return None


def _config(username):
    """Resolve provider + key from the user's `sudo env` store, falling back to
    the process environment (so Render dashboard vars work too)."""
    import state

    env = dict(os.environ)
    for k, v in (state.get_env(username) or {}).items():
        env[k] = v

    # OpenCode Zen — an OpenAI-compatible gateway (https://opencode.ai/zen/v1).
    # Keyed by OPENCODE_API_KEY (alias ZEN_API_KEY); model via OPENCODE_MODEL /
    # ZEN_MODEL. The broad catalog (deepseek, glm, kimi, minimax, nemotron-free,
    # mimo-free, ...) speaks /v1/chat/completions. GPT-5.x on Zen needs the
    # separate /v1/responses API (see chat_once fallback note).
    zen_key = env.get("OPENCODE_API_KEY") or env.get("ZEN_API_KEY")
    if zen_key:
        return "openai", {
            "api_key": zen_key,
            "model": (
                env.get("OPENCODE_MODEL")
                or env.get("ZEN_MODEL")
                or "mimo-v2.5-free"
            ),
            "base": (
                env.get("OPENCODE_BASE_URL")
                or env.get("ZEN_BASE_URL")
                or "https://opencode.ai/zen/v1"
            ),
            "is_zen": True,
        }
    if env.get("ANTHROPIC_API_KEY"):
        return "anthropic", {
            "api_key": env["ANTHROPIC_API_KEY"],
            "model": env.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            "base": env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        }
    if env.get("OPENAI_API_KEY"):
        return "openai", {
            "api_key": env["OPENAI_API_KEY"],
            "model": env.get("OPENAI_MODEL", "gpt-4o-mini"),
            "base": env.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        }
    return None, {}


def _build_system(ctx):
    cwd = ctx["tab"]["cwd"]
    plat = os.name
    return (
        "You are an autonomous coding agent running inside 'rconsole', a "
        "simulated Linux console in the browser. You help the user by using tools.\n"
        "Environment:\n"
        f"  cwd: {cwd}\n"
        f"  platform: {plat}\n"
        "Tools available:\n"
        "  - bash(command): run shell commands in cwd (git, ls, npm, python, pip, ...)\n"
        "  - read_file(path): read a file\n"
        "  - write_file(path, content): write/create a file\n"
        "  - list_dir(path?): list a directory\n"
        "Safety & style:\n"
        "  - Prefer bash for anything a shell does well; prefer read/write for files.\n"
        "  - Do NOT run destructive/system-breaking commands (rm -rf /, mkfs, dd on "
        "disks, shutdown, ...); the bash tool will refuse them.\n"
        "  - Keep tool outputs concise; you will see full results.\n"
        "  - When you create or modify a file, include its final content in a fenced "
        "code block (e.g. ```python\\n...\\n```) so the user can review it in a styled "
        "viewer.\n"
        "  - When you have a final answer, reply with clear prose (no tool call).\n"
    )


# ---------------------------------------------------------------------- providers
def _post(url, headers, body):
    r = _SESSION.post(url, headers=headers, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _openai_once(cfg, messages):
    url = cfg["base"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + cfg["api_key"],
        "Content-Type": "application/json",
    }
    body = {
        "model": cfg["model"],
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
    }
    data = _post(url, headers, body)
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    tool_calls = [
        {
            "id": tc["id"],
            "type": "function",
            "function": {
                "name": tc["function"]["name"],
                "arguments": tc["function"]["arguments"],
            },
        }
        for tc in (msg.get("tool_calls") or [])
    ]
    usage = data.get("usage", {}) or {}
    return {
        "content": content,
        "tool_calls": tool_calls,
        "usage": usage,
    }


def _to_anthropic(messages, tools):
    system = None
    conv = []
    for m in messages:
        role = m["role"]
        if role == "system":
            system = m["content"]
            continue
        if role == "tool":
            conv.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"],
                }],
            })
            continue
        if role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in (m.get("tool_calls") or []):
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"]["arguments"] or "{}"),
                })
            conv.append({"role": "assistant", "content": blocks})
        else:
            content = m["content"]
            conv.append({
                "role": "user",
                "content": [{"type": "text", "text": content}]
                if isinstance(content, str) else content,
            })
    anth_tools = [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        }
        for t in tools
    ]
    return system, conv, anth_tools


def _anthropic_once(cfg, messages):
    system, conv, anth_tools = _to_anthropic(messages, TOOLS)
    url = cfg["base"].rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": cfg["model"],
        "max_tokens": 4096,
        "messages": conv,
        "tools": anth_tools,
    }
    if system:
        body["system"] = system
    data = _post(url, headers, body)
    blocks = data.get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    tool_calls = []
    for b in blocks:
        if b.get("type") == "tool_use":
            tool_calls.append({
                "id": b["id"],
                "type": "function",
                "function": {
                    "name": b["name"],
                    "arguments": json.dumps(b.get("input", {})),
                },
            })
    usage = data.get("usage", {}) or {}
    return {
        "content": text,
        "tool_calls": tool_calls,
        "usage": usage,
    }


def chat_once(provider, cfg, messages):
    if provider == "openai":
        try:
            return _openai_once(cfg, messages)
        except requests.HTTPError as e:
            if cfg.get("is_zen"):
                raise RuntimeError(
                    "%s\n"
                    "If you selected a GPT-5.x Zen model it requires the separate "
                    "/v1/responses API (not yet supported here). Use a Zen model that "
                    "speaks /v1/chat/completions instead, e.g. mimo-v2.5-free, "
                    "deepseek-v4-flash, glm-5.2, kimi-k3, nemotron-3.5-lightning-free "
                    "(set OPENCODE_MODEL)." % e
                )
            raise
    return _anthropic_once(cfg, messages)


# --------------------------------------------------------------- context mgmt
def _maybe_compact(messages, cfg, provider, force=False):
    """Keep the transcript within HISTORY_BUDGET by summarising older turns.

    The transcript is trimmed at a clean 'user' boundary so the model context
    stays valid for both OpenAI and Anthropic schemas.
    """
    if len(messages) <= 1:
        return messages
    ser = json.dumps(messages[1:], ensure_ascii=False)
    if not force and len(ser) <= HISTORY_BUDGET:
        return messages

    system = messages[0]
    rest = messages[1:]
    if len(rest) <= KEEP_RECENT + 1:
        return messages

    split = max(0, len(rest) - KEEP_RECENT)
    # Snap to a 'user' message so we never split an assistant/tool pair.
    while split < len(rest) and rest[split].get("role") != "user":
        split += 1
    head = rest[:split]
    tail = rest[split:]
    if not head:
        return messages

    summary_prompt = [{
        "role": "user",
        "content": (
            "Summarise the prior conversation below into a concise briefing: key "
            "facts, decisions, file paths, commands run, and any open tasks. Keep it "
            "under 250 words. Reply with only the summary."
        ),
    }]
    try:
        res = chat_once(provider, cfg, [system] + head + summary_prompt)
        summary = res.get("content") or ""
    except Exception:
        summary = "(earlier conversation was compacted; detailed history unavailable)"
    if not summary.strip():
        summary = "(earlier conversation was compacted)"
    return [system, {"role": "user", "content": "[Earlier conversation summary]\n" + summary}] + tail


def _model_ids(provider, cfg):
    """Return the list of model ids from the provider, or None on failure."""
    if provider == "anthropic":
        url = cfg["base"].rstrip("/") + "/v1/models"
        headers = {
            "x-api-key": cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    else:
        url = cfg["base"].rstrip("/") + "/models"
        headers = {
            "Authorization": "Bearer " + cfg["api_key"],
            "Content-Type": "application/json",
        }
    try:
        r = _SESSION.get(url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None
    ids = [m.get("id") or m.get("name") for m in data.get("data", [])]
    return [i for i in ids if i] or None


def _list_models(provider, cfg):
    ids = _model_ids(provider, cfg)
    if not ids:
        return YELLOW + "Could not list models (or none returned by the endpoint)." + R
    head = "\n".join("  " + i for i in ids[:80])
    more = "" if len(ids) <= 80 else f"\n  ... and {len(ids) - 80} more"
    return f"Available models ({len(ids)}):\n{head}{more}"


def run_tool(name, args, ctx):
    import interp

    if name == "bash":
        refusal = _guard_bash(args.get("command", ""))
        if refusal:
            return refusal
        out = interp._passthrough(ctx, args.get("command", ""))
        return out or "(no output)"
    if name == "read_file":
        p = _resolve(ctx, args.get("path", ""))
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"error: {e}"
    if name == "write_file":
        p = _resolve(ctx, args.get("path", ""))
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            content = args.get("content", "")
            p.write_text(content, encoding="utf-8")
            # Return the written content so the UI can show it in a code box
            # (clipped for live display; the model context keeps more).
            return content
        except Exception as e:
            return f"error: {e}"
    if name == "list_dir":
        target = args.get("path") or ctx["tab"]["cwd"]
        p = _resolve(ctx, target)
        try:
            entries = sorted(
                os.listdir(p),
                key=lambda s: (not os.path.isdir(os.path.join(p, s)), s.lower()),
            )
        except Exception as e:
            return f"error: {e}"
        return "\n".join(
            ("[D] " + e if os.path.isdir(os.path.join(p, e)) else e) for e in entries
        )
    return f"unknown tool: {name}"


def _mask(val):
    if not val:
        return "—"
    if len(val) > 8:
        return val[:4] + "…" + val[-2:]
    return "***"


def _configured_keys(username):
    import state

    env = dict(os.environ)
    for k, v in (state.get_env(username) or {}).items():
        env[k] = v
    keys = ["OPENCODE_API_KEY", "ZEN_API_KEY", "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY", "GROQ_API_KEY",
            "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY"]
    return [(k, env.get(k)) for k in keys if env.get(k)]


# --------------------------------------------------------------- interactive loop
def agent_routine(ctx, initial=None):
    username = ctx["sid"]
    store = ctx["tab"].setdefault("_agent_session", {})
    provider, cfg = _config(username)
    if not provider:
        yield {
            "prompt": "agent> ",
            "clear": True,
            "output": (
                BOLD + "Agent not configured." + R + "\n"
                "Set an API key with `sudo env`, then run `sudo agent` again:\n"
                "  sudo env set OPENCODE_API_KEY=...      (OpenCode Zen — https://opencode.ai/auth)\n"
                "  sudo env set OPENCODE_MODEL=hy3-free   (any Zen chat/completions model)\n"
                "  sudo env set OPENAI_API_KEY=sk-...     (or OpenAI / OpenRouter)\n"
                "  sudo env set ANTHROPIC_API_KEY=...     (Claude)\n"
                "OpenRouter / local models: also set OPENAI_BASE_URL."
            ),
        }
        return {"output": "agent exited."}

    messages = store.get("messages")
    if messages is None:
        messages = [{"role": "system", "content": _build_system(ctx)}]
    store["provider"] = provider
    store["model"] = cfg["model"]
    store["messages"] = messages

    resumed = len(messages) > 1
    banner = (
        BOLD + "Agent ready" + R + DIM + " (%s / %s)" % (provider, cfg["model"]) + R
        + ("  " + GREEN + "[resumed session]" + R if resumed else "")
        + "\nType a task, or '/help' for commands. 'exit'/'quit' to leave."
    )
    total_tokens = 0
    first_yield = True
    line = initial
    while True:
        if line is None:
            out_block = banner if first_yield else ""
            first_yield = False
            line = yield {"prompt": "agent> ", "output": out_block}
            continue
        cmd = (line or "").strip()
        if cmd in ("exit", "quit", "q"):
            store.clear()
            return {"output": "agent session ended."}
        if cmd.startswith("/"):
            r = _slash(cmd, ctx, provider, cfg, store)
            first_yield = False
            line = yield r
            continue
        if not cmd:
            line = None
            continue

        # Refresh cwd in the system prompt each turn.
        messages[0] = {"role": "system", "content": _build_system(ctx)}
        messages.append({"role": "user", "content": cmd})
        out = []
        steps = 0
        turn_tokens = 0
        # Surface the "ready" banner as a live tick for a one-shot initial prompt
        # (so the user sees something immediately instead of a blank wait).
        if first_yield and cmd:
            first_yield = False
            yield {"_tick": True, "output": banner}
        while steps < MAX_STEPS:
            steps += 1
            messages = _maybe_compact(messages, cfg, provider)
            store["messages"] = messages
            # Live "thinking" indicator so the console isn't silent during a call.
            yield {"_tick": True, "output": DIM + "… thinking (" + cfg["model"] + ")" + R}
            try:
                res = chat_once(provider, cfg, messages)
            except Exception as e:
                out.append(YELLOW + f"agent error: {e}" + R)
                break
            content = res["content"]
            tool_calls = res["tool_calls"]
            u = res.get("usage") or {}
            turn_tokens += u.get("total_tokens") or (
                u.get("input_tokens", 0) + u.get("output_tokens", 0)
            )
            if tool_calls:
                asst = {"role": "assistant", "content": content}
                if provider == "openai":
                    asst["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                        for tc in tool_calls
                    ]
                messages.append(asst)
                store["messages"] = messages
                block = []
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except Exception:
                        args = {}
                    result = run_tool(name, args, ctx)
                    block.append(_tool_tick(name, args, result))
                    if provider == "openai":
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "content": _clip(result, TOOL_KEEP),
                        })
                    else:
                        messages.append({
                            "role": "user",
                            "content": [{
                                "type": "tool_result",
                                "tool_use_id": tc.get("id"),
                                "content": _clip(result, TOOL_KEEP),
                            }],
                        })
                store["messages"] = messages
                yield {"_tick": True, "output": "\n".join(block)}
                continue
            else:
                messages.append({"role": "assistant", "content": content})
                store["messages"] = messages
                if content:
                    out.append(content)
                break
        else:
            out.append(YELLOW + f"agent stopped after {MAX_STEPS} steps "
                       f"(use /clear to reset context)." + R)

        total_tokens += turn_tokens
        foot = DIM + (f"\n— {steps} step(s), ~{turn_tokens} tokens this turn "
                      f"(session ~{total_tokens}) —" + R)
        out_block = "\n".join(out) + foot
        first_yield = False
        line = yield {"prompt": "agent> ", "output": out_block}


def _slash(cmd, ctx, provider, cfg, store):
    parts = cmd.split(None, 1)
    name = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if name in ("/help", "/?"):
        return {"prompt": "agent> ", "output": AGENT_HELP}
    if name in ("/clear", "/reset"):
        store["messages"] = [{"role": "system", "content": _build_system(ctx)}]
        return {"prompt": "agent> ", "output": GREEN + "Conversation cleared." + R}
    if name == "/model":
        if not arg:
            ids = _model_ids(provider, cfg)
            if ids:
                # The frontend shows a dropdown picker; selecting one sends
                # `/model <name>` back into this session.
                return {
                    "agent_picker": {
                        "action": "model",
                        "models": ids,
                        "current": cfg["model"],
                    },
                    "output": DIM + "Choose a model:" + R,
                }
            return {"prompt": "agent> ",
                    "output": YELLOW + "Could not fetch the model list; use /model <name>." + R}
        cfg["model"] = arg
        store["model"] = arg
        return {"prompt": "agent> ", "output": GREEN + f"Model set to {arg} (this session)." + R}
    if name == "/provider":
        return {"prompt": "agent> ", "output":
                f"provider : {provider}\nmodel    : {cfg['model']}\nbase url : {cfg['base']}"}
    if name == "/keys":
        rows = [f"  {k}={_mask(v)}" for k, v in _configured_keys(ctx['sid'])]
        return {"prompt": "agent> ", "output":
                ("Configured keys (masked):\n" + "\n".join(rows)
                 if rows else "No provider keys configured.")}
    if name == "/models":
        out = _list_models(provider, cfg)
        return {"prompt": "agent> ", "output": out}
    if name == "/compact":
        store["messages"] = _maybe_compact(store["messages"], cfg, provider, force=True)
        return {"prompt": "agent> ", "output": GREEN + "Conversation compacted." + R}
    if name == "/system":
        return {"prompt": "agent> ", "output": _build_system(ctx)}
    if name in ("/exit", "/quit"):
        store.clear()
        return {"prompt": "agent> ", "output": "agent session ended."}
    return {"prompt": "agent> ",
            "output": YELLOW + f"unknown agent command: {name}  (try /help)" + R}


def cmd_agent(ctx, args):
    if args and args[0] in ("models", "model"):
        provider, cfg = _config(ctx["sid"])
        if not provider:
            return {"output": "agent not configured (set a key via `sudo env`)."}
        return {"output": _list_models(provider, cfg)}
    if args and args[0] in ("help", "?", "h"):
        return {"output": AGENT_HELP}
    return agent_routine(ctx, " ".join(args) if args else None)
