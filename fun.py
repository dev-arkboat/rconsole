"""Fun / novelty console commands for rconsole.

These are pure-Python and need no host binary. Most return colored text using
ANSI escape codes, which the browser console renders (console.js converts ANSI
to HTML). A few (sl, matrix) produce a single vivid "frame" rather than an
animated loop, because the line console only advances a generator when the user
sends input.
"""
import calendar
import os
import random
import shutil
import sys
import time

import commands
import state

ESC = "\x1b["
START_TIME = time.time()

# --- ANSI helpers ------------------------------------------------------------
def sgr(*codes):
    return ESC + ";".join(str(c) for c in codes) + "m"

RESET = sgr(0)
BOLD = sgr(1)
DIM = sgr(2)

def fg(n):
    return sgr(30 + n) if 0 <= n <= 7 else sgr(90 + (n - 8))

def rgb(r, g, b):
    return sgr(38, 2, r, g, b)

def bg(n):
    return sgr(40 + n) if 0 <= n <= 7 else sgr(100 + (n - 8))


# --- fortune ----------------------------------------------------------------
_FORTUNES = [
    "Never trust a computer you can't lift. -- Alan Kay",
    "Programs must be written for people to read, and only incidentally for machines to execute. -- SICP",
    "Premature optimization is the root of all evil. -- Donald Knuth",
    "The best way to predict the future is to invent it. -- Alan Kay",
    "Talk is cheap. Show me the code. -- Linus Torvalds",
    "Simplicity is the soul of efficiency. -- Austin Freeman",
    "Make it work, make it right, make it fast. -- Kent Beck",
    "First, solve the problem. Then, write the code. -- John Johnson",
    "Code never lies, comments sometimes do. -- Ron Jeffries",
    "Walking on water and developing software from a specification are easy if both are frozen. -- Edward V. Berard",
    "Before software can be reusable it first has to be usable. -- Ralph Johnson",
    "Any fool can write code that a computer can understand. Good programmers write code that humans can understand. -- Martin Fowler",
    "The function of good software is to make the complex appear to be simple. -- Grady Booch",
    "A language that doesn't affect the way you think about programming is not worth knowing. -- Alan Perlis",
    "Deleted code is debugged code. -- Jeff Sickel",
    "There are two ways to write error-free programs; only the third one works. -- Alan Perlis",
    "If you can't explain it simply, you don't understand it well enough. -- Albert Einstein",
    "It works on my machine. -- every developer, ever",
    "It's not a bug, it's an undocumented feature.",
    "99 little bugs in the code, 99 little bugs. Take one down, patch it around, 117 little bugs in the code.",
]


# --- cowsay -----------------------------------------------------------------
def _cowsay(text):
    if not text:
        text = "Moo?"
    width = max(len(line) for line in text.split("\n"))
    width = min(max(width, 8), 60)
    lines = text.split("\n")
    top = " " + "_" * (width + 2)
    bottom = " " + "-" * (width + 2)
    body = []
    for i, line in enumerate(lines):
        pad = " " * (width - len(line))
        if len(lines) == 1:
            body.append("  %s%s  " % (line, pad))
        elif i == 0:
            body.append(" / %s%s \\" % (line, pad))
        elif i == len(lines) - 1:
            body.append(" \\ %s%s /" % (line, pad))
        else:
            body.append(" | %s%s |" % (line, pad))
    cow = [
        "        \\   ^__^",
        "         \\  (oo)\\_______",
        "            (__)\\       )\\/\\",
        "                ||----w |",
        "                ||     ||",
    ]
    return "\n".join([top] + body + [bottom] + cow)


# --- figlet / banner (compact 5-row block font) -----------------------------
_FONT = {
    "A": [" ### ", "#   #", "#####", "#   #", "#   #"],
    "B": ["#### ", "#   #", "#### ", "#   #", "#### "],
    "C": [" ####", "#    ", "#    ", "#    ", " ####"],
    "D": ["#### ", "#   #", "#   #", "#   #", "#### "],
    "E": ["#####", "#    ", "#### ", "#    ", "#####"],
    "F": ["#####", "#    ", "#### ", "#    ", "#    "],
    "G": [" ####", "#    ", "#  ##", "#   #", " ####"],
    "H": ["#   #", "#   #", "#####", "#   #", "#   #"],
    "I": ["#####", "  #  ", "  #  ", "  #  ", "#####"],
    "J": ["  ###", "   # ", "   # ", "#  # ", " ##  "],
    "K": ["#   #", "#  # ", "###  ", "#  # ", "#   #"],
    "L": ["#    ", "#    ", "#    ", "#    ", "#####"],
    "M": ["#   #", "## ##", "# # #", "#   #", "#   #"],
    "N": ["#   #", "##  #", "# # #", "#  ##", "#   #"],
    "O": [" ### ", "#   #", "#   #", "#   #", " ### "],
    "P": ["#### ", "#   #", "#### ", "#    ", "#    "],
    "Q": [" ### ", "#   #", "# # #", "#  # ", " ## #"],
    "R": ["#### ", "#   #", "#### ", "#  # ", "#   #"],
    "S": [" ####", "#    ", " ### ", "    #", "#### "],
    "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
    "U": ["#   #", "#   #", "#   #", "#   #", " ### "],
    "V": ["#   #", "#   #", "#   #", " # # ", "  #  "],
    "W": ["#   #", "#   #", "# # #", "## ##", "#   #"],
    "X": ["#   #", " # # ", "  #  ", " # # ", "#   #"],
    "Y": ["#   #", " # # ", "  #  ", "  #  ", "  #  "],
    "Z": ["#####", "   # ", "  #  ", " #   ", "#####"],
    "0": [" ### ", "#  ##", "# # #", "##  #", " ### "],
    "1": ["  #  ", " ##  ", "  #  ", "  #  ", " ### "],
    "2": [" ### ", "#   #", "  ## ", " #   ", "#####"],
    "3": ["#### ", "    #", " ### ", "    #", "#### "],
    "4": ["#  # ", "#  # ", "#####", "   # ", "   # "],
    "5": ["#####", "#    ", "#### ", "    #", "#### "],
    "6": [" ### ", "#    ", "#### ", "#   #", " ### "],
    "7": ["#####", "   # ", "  #  ", " #   ", " #   "],
    "8": [" ### ", "#   #", " ### ", "#   #", " ### "],
    "9": [" ### ", "#   #", " ####", "    #", " ### "],
    " ": ["     ", "     ", "     ", "     ", "     "],
    "-": ["     ", "     ", " ### ", "     ", "     "],
    ".": ["     ", "     ", "     ", "     ", "  #  "],
    "!": ["  #  ", "  #  ", "  #  ", "     ", "  #  "],
    "?": [" ### ", "#   #", "  ## ", "     ", "  #  "],
    ":": ["     ", "  #  ", "     ", "  #  ", "     "],
    "/": ["    #", "   # ", "  #  ", " #   ", "#    "],
    "@": [" ### ", "#  ##", "# ###", "#    ", " ### "],
    "#": [" # # ", "#####", " # # ", "#####", " # # "],
    "*": ["     ", " # # ", "  #  ", " # # ", "     "],
}
_FALLBACK = ["  #  ", " # # ", "  #  ", " # # ", "  #  "]


def _figlet(text):
    if not text:
        text = "rconsole"
    text = text.upper()[:40]
    glyphs = []
    for ch in text:
        glyphs.append(_FONT.get(ch, _FALLBACK))
    rows = []
    for r in range(5):
        rows.append("  ".join(g[r] for g in glyphs))
    return "\n".join(rows)


# --- lolcat -----------------------------------------------------------------
def _lolcat(text):
    out = []
    for i, ch in enumerate(text):
        if ch == "\n":
            out.append("\n")
            continue
        hue = (i * 12) % 360
        r, g, b = _hsv_to_rgb(hue / 360.0, 0.8, 1.0)
        out.append(rgb(r, g, b) + ch)
    return "".join(out) + RESET


def _hsv_to_rgb(h, s, v):
    i = int(h * 6)
    f = h * 6 - i
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    i %= 6
    if i == 0:
        return int(v * 255), int(t * 255), int(p * 255)
    if i == 1:
        return int(q * 255), int(v * 255), int(p * 255)
    if i == 2:
        return int(p * 255), int(v * 255), int(t * 255)
    if i == 3:
        return int(p * 255), int(q * 255), int(v * 255)
    if i == 4:
        return int(t * 255), int(p * 255), int(v * 255)
    return int(v * 255), int(p * 255), int(q * 255)


# --- sl (steam locomotive, static frame) ------------------------------------
_TRAIN = [
    "        ==>____",
    "   ====[]______]=======",
    "  |  ___   ___   ___  |",
    "  | |###| |###| |###| |",
    "  | |###| |###| |###| |",
    " ((O)==========(O))==",
]


# --- matrix -----------------------------------------------------------------
_KATAKANA = [chr(c) for c in range(0x30A0, 0x30FB)]
def _matrix(rows=24, cols=60):
    out = []
    for _ in range(rows):
        line = ""
        for _ in range(cols):
            ch = random.choice(_KATAKANA)
            shade = random.random()
            if shade > 0.85:
                line += BOLD + sgr(38, 2, 180, 255, 180) + ch
            else:
                line += fg(2) + ch
        out.append(line)
    return "\n".join(out) + RESET


# --- neofetch ---------------------------------------------------------------
def _neofetch(ctx):
    # Logo as PLAIN visible text; color is wrapped AFTER padding so the two
    # columns line up (counting ANSI escapes as width would misalign them).
    logo = [
        "  ██████",
        "  ██  ██",
        "  ██  ██",
        "  ██  ██",
        "  ██████",
        "  ██  ██",
        "  ██ ██",
        "  ████",
        "  ██ ██",
        "  ██  ██",
    ]
    try:
        import socket
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    try:
        plat = sys.platform
    except Exception:
        plat = "unknown"
    username = ctx.get("username", "user")
    is_admin = ctx.get("is_admin")
    role = "admin" if is_admin else "user"
    theme = "default"
    try:
        s = state.get_session(username)
        if s:
            theme = s.get("theme", "default")
    except Exception:
        pass
    up = int(time.time() - START_TIME)
    cpus = os.cpu_count() or "?"
    mem = _mem_info()
    disk = "n/a"
    try:
        du = shutil.disk_usage(os.getcwd())
        disk = "%d/%d GB" % (du.free // 1024 ** 3, du.total // 1024 ** 3)
    except Exception:
        pass
    b = fg(4)
    info = [
        BOLD + str(username) + RESET + "@rconsole",
        b + "-----------" + RESET,
        "OS: " + b + "rconsole (%s)" % plat + RESET,
        "Host: " + b + str(host) + RESET,
        "Shell: " + b + "rconsole-sh" + RESET,
        "Term: " + b + "xterm.js" + RESET,
        "Python: " + b + sys.version.split()[0] + RESET,
        "CPU: " + b + "%s cores" % cpus + RESET,
        "Memory: " + b + mem + RESET,
        "Disk(/): " + b + disk + RESET,
        "Theme: " + b + str(theme) + RESET,
        "Role: " + b + role + RESET,
        "Uptime: " + b + _fmt_uptime(up) + RESET,
        "Threads: " + b + str(_count_threads()) + RESET,
        "PID: " + b + str(os.getpid()) + RESET,
    ]
    W = 24
    n = max(len(logo), len(info))
    out = []
    for i in range(n):
        l = logo[i] if i < len(logo) else ""
        r = info[i] if i < len(info) else ""
        out.append(fg(2) + l.ljust(W) + RESET + "  " + r)
    return "\n".join(out) + RESET


def _fmt_uptime(s):
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return "%dh %dm" % (h, m)
    if m:
        return "%dm %ds" % (m, sec)
    return "%ds" % sec


def _count_threads():
    try:
        import threading
        return threading.active_count()
    except Exception:
        return 0


def _mem_info():
    """Best-effort RAM usage using only the standard library."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return "%d%% (%d/%d MB)" % (vm.percent, vm.used // 1024 // 1024,
                                    vm.total // 1024 // 1024)
    except Exception:
        pass
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                d = {}
                for line in f:
                    k, _, v = line.partition(":")
                    d[k.strip()] = v.strip()
            total = int(d["MemTotal"].split()[0])
            avail = int(d.get("MemAvailable", d.get("MemFree", "0")).split()[0])
            used = total - avail
            return "%d%% (%d/%d MB)" % (used * 100 // total, used // 1024, total // 1024)
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            import ctypes
            class _MSX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            s = _MSX()
            s.dwLength = ctypes.sizeof(_MSX)
            k32 = ctypes.windll.kernel32
            k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MSX)]
            k32.GlobalMemoryStatusEx.restype = ctypes.c_int
            if k32.GlobalMemoryStatusEx(ctypes.byref(s)):
                total = s.ullTotalPhys // 1024 // 1024
                used = total - s.ullAvailPhys // 1024 // 1024
                return "%d%% (%d/%d MB)" % (s.dwMemoryLoad, used, total)
    except Exception:
        pass
    return "n/a"


# --- colors palette ---------------------------------------------------------
def _colors():
    out = [BOLD + "ANSI 8/16 colors:" + RESET]
    row = ""
    for n in range(8):
        row += bg(n) + fg((n + 7) % 8) + " %d " % n + RESET
    out.append(row)
    row = ""
    for n in range(8, 16):
        row += bg(n) + fg((n + 7) % 16) + " %d " % n + RESET
    out.append(row)
    out.append("")
    out.append(BOLD + "24-bit rainbow sample:" + RESET)
    out.append(_lolcat("the quick brown fox jumps over the lazy dog 0123456789"))
    return "\n".join(out) + RESET


# ---------------------------------------------------------------------------
# Command registrations
# ---------------------------------------------------------------------------
@commands.command("echo")
def cmd_echo(ctx, args):
    text = " ".join(args)
    no_newline = False
    interpret = False
    if args and args[0] == "-n":
        no_newline = True
        text = " ".join(args[1:])
    elif args and args[0] == "-e":
        interpret = True
        text = " ".join(args[1:])
    if interpret:
        import re as _re
        text = (text.replace("\\n", "\n").replace("\\t", "\t")
                    .replace("\\r", "\r").replace("\\a", "\a"))
        text = text.replace("\\e", ESC).replace("\\x1b", ESC)
        text = _re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)
    return text if no_newline else text + "\n"


@commands.command("cowsay")
def cmd_cowsay(ctx, args):
    return _cowsay(" ".join(args))


@commands.command("fortune")
def cmd_fortune(ctx, args):
    return random.choice(_FORTUNES)


@commands.command("lolcat")
def cmd_lolcat(ctx, args):
    text = " ".join(args) or "rconsole is awesome"
    return _lolcat(text)


@commands.command("figlet")
@commands.command("banner")
def cmd_figlet(ctx, args):
    return _figlet(" ".join(args))


@commands.command("sl")
def cmd_sl(ctx, args):
    return "\n".join(_TRAIN)


@commands.command("matrix")
def cmd_matrix(ctx, args):
    return _matrix()


@commands.command("neofetch")
@commands.command("fetch")
def cmd_neofetch(ctx, args):
    return _neofetch(ctx)


@commands.command("colors")
def cmd_colors(ctx, args):
    return _colors()


@commands.command("date")
def cmd_date(ctx, args):
    return time.strftime("%a %b %d %H:%M:%S %Z %Y")


@commands.command("uptime")
def cmd_uptime(ctx, args):
    up = int(time.time() - START_TIME)
    return "up " + _fmt_uptime(up)


@commands.command("cal")
def cmd_cal(ctx, args):
    try:
        if not args:
            return calendar.TextCalendar().formatyear(time.localtime().tm_year)
        if len(args) == 1:
            return calendar.month(time.localtime().tm_year, int(args[0]))
        y = int(args[1]) if len(args) >= 2 else int(args[0])
        m = int(args[0]) if len(args) >= 2 else 1
        return calendar.month(y, m)
    except Exception as e:
        return "cal: %s" % e


@commands.command("rev")
def cmd_rev(ctx, args):
    """Reverse each line. With no args shows usage; with text it reverses the
    text; with file paths it reverses each line of those files (resolved against
    the current directory)."""
    if not args:
        return "rev: usage: rev <text>   or   rev <file> [file ...]"
    out = []
    for a in args:
        p = commands._resolve_path(ctx, a)
        if p.is_file():
            try:
                data = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                out.append("rev: %s: %s" % (p, e))
                continue
            out.append("\n".join(line[::-1] for line in data.split("\n")))
        else:
            out.append(a[::-1])
    return "\n".join(out)


@commands.command("wc")
def cmd_wc(ctx, args):
    """Count lines/words/chars (like unix wc).

    Usage:
      wc <text>              count the given text
      wc <file> [file ...]   count each file (resolved against cwd) + a total
    """
    if not args:
        return "wc: usage: wc <text>   or   wc <file> [file ...]"
    files, text_parts = [], []
    for a in args:
        p = commands._resolve_path(ctx, a)
        if p.is_file():
            files.append(p)
        else:
            text_parts.append(a)

    def _count(data):
        lines = data.count("\n") + (1 if data and not data.endswith("\n") else 0)
        words = len(data.split())
        chars = len(data)
        return lines, words, chars

    out = []
    tot_l = tot_w = tot_c = 0
    for f in files:
        try:
            data = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            out.append("wc: %s: %s" % (f, e))
            continue
        l, w, c = _count(data)
        tot_l += l; tot_w += w; tot_c += c
        out.append("%5d %5d %5d %s" % (l, w, c, f.name))
    if text_parts:
        l, w, c = _count(" ".join(text_parts))
        tot_l += l; tot_w += w; tot_c += c
        if files:
            out.append("%5d %5d %5d (text)" % (l, w, c))
        else:
            return "%d %d %d" % (l, w, c)
    if files and text_parts:
        out.append("%5d %5d %5d total" % (tot_l, tot_w, tot_c))
    elif len(files) > 1:
        out.append("%5d %5d %5d total" % (tot_l, tot_w, tot_c))
    return "\n".join(out)


@commands.command("hi")
@commands.command("hello")
def cmd_hi(ctx, args):
    return BOLD + fg(2) + "Hello, %s! Type 'help' to explore. (Made by MD ABU SALEHIN)" % ctx.get("username", "friend") + RESET
