#!/usr/bin/env python3
"""Claude Code statusline: two lines, gauge bars, 3-tier warn colors.

Line 1: [CAVEMAN] model  branch*  PR  +added/-removed  agent  vim
Line 2: ctx NN%  5h NN%  7d NN%  cache NN%

Gauges render as a bare number while quiet and grow a segmented bar once
past BAR_THRESHOLD, so line 2 only widens when something is actually filling up.
Both lines are fitted to the terminal width, dropping the lowest-priority
segments first rather than wrapping.

Wired via ~/.claude/settings.json:
  "statusLine": {"type": "command", "command": "python3 /home/.../statusline.py"}

Every field is optional in the input JSON (rate_limits only exists for Pro/Max,
and only after the first API response), so every read is defensive: a missing
section drops its segment rather than raising.
"""
import json
import os
import re
import subprocess
import sys
import time

# --------------------------------------------------------------- local config

# Per-machine tuning that should never round-trip through git: font rendering
# quirks, a preferred bar width, threshold taste. Lives outside the repo, one
# JSON file, absent by default. Any bad or missing file is silently ignored so
# a typo here can never take the statusline down with it.
LOCAL_CONFIG_PATH = os.path.join(
    os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
    "statusline.local.json",
)


def load_local_config(path=None):
    path = path or LOCAL_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


LOCAL = load_local_config()

# ---------------------------------------------------------------- ansi helpers

RESET = "\033[0m"
DIM = "\033[2m"
ORANGE = "\033[38;5;172m"  # caveman badge
GREY = "\033[38;5;245m"
GREEN = "\033[38;5;71m"
YELLOW = "\033[38;5;179m"
RED = "\033[38;5;167m"
CYAN = "\033[38;5;73m"

# ------------------------------------------------- Nerd Font glyphs (MesloLGS NF)
# Every one of these needs a patched font. Set CLAUDE_STATUSLINE_ASCII=1 to fall
# back to plain text (useful over a bare SSH session or in a log).
NF = os.environ.get("CLAUDE_STATUSLINE_ASCII", "") != "1"

G_MODEL = "" if NF else ""        # bolt
G_BRANCH = "" if NF else ""       # git branch
G_PR = "" if NF else "PR"          # git pull-request
G_MR = "" if NF else "MR"          # gitlab
G_DIFF = "" if NF else "diff"         # git commit/diff
G_CTX = "" if NF else "ctx"          # database (context)
G_CLOCK = "" if NF else "5h"        # clock (5h window)
G_CAL = "" if NF else "7d"          # calendar (7d window)
G_WALLET = "" if NF else "spend"       # wallet (spend limit)
G_CACHE = "" if NF else "cache"        # gauge (cache hit)
G_RESET = "" if NF else "~"       # refresh arrow
G_AGENT = "" if NF else "agent"        # robot/user-secret
G_FAST = "" if NF else "fast"        # bolt
G_WARN = "" if NF else "!"        # warning triangle
G_TREE = "" if NF else "wt"         # tree (worktree)
G_OK = "" if NF else "ok"
G_NO = "" if NF else "x"
G_DOT = "" if NF else "*"
G_DRAFT = "" if NF else "-"
G_UP = "" if NF else "^"       # arrow-up, burning ahead of pace
G_DOWN = "" if NF else "v"     # arrow-down, comfortably behind pace
G_TTL = "" if NF else "exp"    # clock-o, cache expiry
G_PULSE = "" if NF else "SVC"  # heartbeat, status.claude.com component health


def tier(pct):
    """3-tier warn color: green <60, yellow 60-85, red >85."""
    if pct is None:
        return GREY
    if pct > 85:
        return RED
    if pct >= 60:
        return YELLOW
    return GREEN


# A gauge only earns its bar once it is worth looking at. Below this it renders
# as a bare number, which keeps line 2 short during the long quiet stretch of a
# session and lets it widen exactly when something is filling up.
BAR_THRESHOLD = LOCAL.get("bar_threshold", 60.0)
BAR_WIDTH = LOCAL.get("bar_width", 8)

# Cache hit ratio stays hidden above this — a warm cache is the normal state and
# needs no column. Below it, a miss actually cost something worth seeing.
CACHE_HEALTHY = 70.0

# Auto-compact lands somewhere above 90%, so flag the approach a little early
# rather than the moment it fires.
COMPACT_WARN = 85.0

# Fixed rate-limit window lengths, needed to turn "resets_at" into elapsed
# share for the burn-rate comparison. Only the 5-hour window gets a pace arrow:
# over seven days a double-digit drift is ordinary variation, not a warning.
WINDOW_SECS = {"five_hour": 5 * 3600}

# Only warn about cache expiry when a pause would actually cost a re-cache
# soon; a 40-minute TTL is not news.
CACHE_TTL_WARN_SECS = 8 * 60

# Early in a window the elapsed share is tiny, so a single heavy turn trips the
# pace comparison on noise alone. Stay quiet until enough of the window has run
# for the extrapolation to mean anything.
BURN_MIN_ELAPSED = LOCAL.get("burn_min_elapsed", 25.0)

# ...but past this much of the quota, the reading is worth flagging whatever the
# clock says: half a window spent in its first hour is the situation the arrow
# exists for, not the noise the floor guards against.
BURN_ALWAYS_PCT = LOCAL.get("burn_always_pct", 50.0)

# How far pct can drift from elapsed-share before the pace arrow fires.
BURN_DRIFT_THRESHOLD = LOCAL.get("burn_drift_threshold", 5.0)

# Reading HEAD is a single file read, so a short timeout is plenty. The dirty
# check walks the work tree and gets its own, larger budget — on a WSL2 mount of
# an NTFS directory that walk can run into seconds on a few hundred files.
GIT_HEAD_TIMEOUT = 1.0
GIT_STATUS_TIMEOUT = 2.5

# How long a dirty-flag answer stays good. Long enough to spare the walk across
# a burst of prompts, short enough that the marker is not visibly wrong.
DIRTY_CACHE_SECS = 10.0
CACHE_DIR = os.path.join(
    os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
    "cache", "statusline",
)

# status.claude.com is a public, unauthenticated Statuspage instance. How often
# to re-poll it — an incident is not the kind of thing that needs sub-minute
# freshness, and this is a courtesy check against someone else's endpoint, not
# a monitoring system.
SERVICE_STATUS_URL = "https://status.claude.com/api/v2/summary.json"
SERVICE_STATUS_CACHE_SECS = LOCAL.get("service_status_cache_secs", 300.0)
SERVICE_STATUS_TIMEOUT = 3.0
SERVICE_STATUS_CACHE_PATH = os.path.join(CACHE_DIR, "service-status.json")
# The named Claude Code component on that page, distinct from claude.ai and the
# API — an outage in one does not imply the others, and this is the one that
# actually affects a Claude Code session.
SERVICE_STATUS_COMPONENT = "Claude Code"


# Discrete segments, no partial cells. Sub-cell glyphs mixed heights within one
# bar and read as broken rather than precise, so the bar is deliberately coarse
# and the exact figure is left to the number beside it.
FILLED = "▰"
TRACK = "▱"

# Space between bar cells. Some fonts render these block glyphs tight enough
# to touch/overlap; a per-machine override in statusline.local.json
# ({"segment_spacing": " "}) fixes that without forking the script.
SEGMENT_SPACING = LOCAL.get("segment_spacing", "")


def bar(pct, width=BAR_WIDTH):
    """Segmented gauge, whole cells, absolute 0..100.

    The fill is the percentage: a bar that disagreed with the number beside it
    would be worse than no bar, so the scale stays absolute even though the bar
    is only drawn over part of the range.
    """
    if pct is None:
        return f"{DIM}{SEGMENT_SPACING.join(TRACK * width)}{RESET}"

    pct = max(0.0, min(100.0, float(pct)))
    c = tier(pct)

    filled = int(round(pct / 100.0 * width))
    # Reserve the last segment for a true 100%: "nearly out" and "out" are the
    # two states most worth telling apart.
    if pct < 100.0:
        filled = min(filled, width - 1)
    filled = max(0, filled)

    # Width is constant, so the line never shifts as the value climbs.
    filled_part = SEGMENT_SPACING.join(FILLED * filled)
    track_part = SEGMENT_SPACING.join(TRACK * (width - filled))
    sep = SEGMENT_SPACING if filled and filled < width else ""
    return f"{c}{filled_part}{sep}{DIM}{track_part}{RESET}"


def gauge(label, pct, width=BAR_WIDTH):
    """`label NN%` while quiet; `label <bar>NN%` once past BAR_THRESHOLD."""
    if pct is None:
        return None
    c = tier(pct)
    head = f"{GREY}{label}{RESET} "
    if float(pct) < BAR_THRESHOLD:
        return f"{head}{c}{pct:.0f}%{RESET}"
    return f"{head}{bar(pct, width)} {c}{pct:.0f}%{RESET}"


def until(epoch):
    """Compact 'resets in' string: 2h14m / 4d3h / 45m / now."""
    if not epoch:
        return None
    delta = int(epoch) - int(time.time())
    if delta <= 0:
        return "now"
    d, rem = divmod(delta, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        # Always carry the hours at day scale so a 7-day window reads "7d0h"
        # rather than a bare "7d" beside a sibling showing "4h50m".
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m" if m else f"{h}h"
    # Sub-minute needs seconds: "0m" reads as already-expired when the real
    # answer is half a minute away, and this range is exactly when the cache
    # TTL warning fires.
    if m:
        return f"{m}m"
    return f"{delta}s"


def burn_arrow(pct, resets_at, window_secs):
    """Pace indicator: is consumption outrunning the clock?

    Compares share-of-quota-spent against share-of-window-elapsed. Ahead of pace
    means the cap arrives before the reset does, which is the only version of
    this number worth acting on. Returns None when the two are close enough that
    an arrow would just be noise.
    """
    if not resets_at or not window_secs:
        return None
    remaining = int(resets_at) - int(time.time())
    if remaining <= 0 or remaining >= window_secs:
        return None
    elapsed_share = (window_secs - remaining) / window_secs * 100.0
    # The floor exists so one heavy turn in a fresh window does not trip the
    # comparison on noise. But heavy usage this early is the case most worth
    # flagging, so a high enough reading overrides it.
    if elapsed_share < BURN_MIN_ELAPSED and float(pct) < BURN_ALWAYS_PCT:
        return None

    drift = float(pct) - elapsed_share
    if drift > BURN_DRIFT_THRESHOLD:
        return f"{RED}{G_UP}{RESET}"
    if drift < -BURN_DRIFT_THRESHOLD:
        return f"{GREEN}{G_DOWN}{RESET}"
    return None


ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def visible_width(s):
    """Printable columns: strip SGR codes, count Nerd Font glyphs as 1 cell.

    The private-use glyphs are single-width in a patched font even though
    wcwidth-style tables have no opinion on them, so a plain len() over the
    de-ANSI'd string is the right measure here.
    """
    return len(ANSI_RE.sub("", s))


def fit(segments, budget):
    """Drop lowest-priority segments until the joined line fits `budget`.

    `segments` is a list of (priority, text) with priority 0 = never drop.
    Order is preserved; only whole segments are removed, lowest priority first,
    so the line degrades predictably instead of wrapping.
    """
    kept = [s for s in segments if s[1]]
    sep_w = 3  # " · "

    def width(items):
        if not items:
            return 0
        return sum(visible_width(t) for _, t in items) + sep_w * (len(items) - 1)

    while kept and budget > 0 and width(kept) > budget:
        droppable = [p for p, _ in kept if p > 0]
        if not droppable:
            break
        worst = max(droppable)
        for i, (p, _) in enumerate(kept):
            if p == worst:
                del kept[i]
                break

    return f" {GREY}·{RESET} ".join(t for _, t in kept)


def term_width():
    """Columns available. Falls back to 80 when not attached to a tty."""
    try:
        cols = int(os.environ.get("COLUMNS", "") or 0)
        if cols > 0:
            return cols
    except ValueError:
        pass
    try:
        return os.get_terminal_size(sys.stderr.fileno()).columns
    except (OSError, ValueError, AttributeError):
        return 80


def dig(d, *path, default=None):
    """Nested get that tolerates missing keys and non-dict nodes."""
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return default if cur is None else cur


# ------------------------------------------------------------------ caveman

# Same whitelist + hardening as caveman-statusline.sh: the flag file is
# attacker-writable in principle, so cap the read and strip anything that
# could carry a terminal escape.
CAVEMAN_MODES = {
    "off", "lite", "full", "ultra", "wenyan-lite", "wenyan",
    "wenyan-full", "wenyan-ultra", "commit", "review", "compress",
}

# Mode is carried by color plus an optional suffix rather than a spelled-out
# name. Color alone cannot separate 10 modes, so the wenyan family keeps a
# marker and the intensity levels keep theirs; plain "full" needs neither.
CAVEMAN_STYLES = {
    "lite":         (GREY,   "-"),
    "full":         (ORANGE, ""),
    "ultra":        (RED,    "+"),
    "wenyan-lite":  (CYAN,   "-"),
    "wenyan":       (CYAN,   ""),
    "wenyan-full":  (CYAN,   ""),
    "wenyan-ultra": (CYAN,   "+"),
    "commit":       (GREEN,  "c"),
    "review":       (YELLOW, "r"),
    "compress":     (GREY,   "z"),
}


def caveman_badge(cfg_dir):
    flag = os.path.join(cfg_dir, ".caveman-active")
    if os.path.islink(flag) or not os.path.isfile(flag):
        return None
    try:
        with open(flag, "rb") as f:
            raw = f.read(64)
    except OSError:
        return None
    mode = re.sub(r"[^a-z0-9-]", "", raw.decode("utf-8", "replace").strip().lower())
    if mode not in CAVEMAN_MODES or mode == "off":
        return None
    # "CV" rather than a glyph: no Nerd Font icon reads as "caveman", and two
    # letters stay unambiguous with or without a patched font.
    color, suffix = CAVEMAN_STYLES.get(mode, (ORANGE, ""))
    return f"{color}CV{suffix}{RESET}"


# ---------------------------------------------------------------------- git


def _git(cwd, args, timeout):
    """Run a git command, returning stdout or None on any failure."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def dirty_cached(cwd):
    """Whether the work tree has tracked modifications, memoized on disk.

    The walk is far too slow to repeat on every prompt on a WSL2/NTFS mount, and
    the answer rarely changes between two renders seconds apart. Cache it keyed
    by directory and let it go stale briefly rather than pay the cost each time.
    """
    key = re.sub(r"[^A-Za-z0-9]", "_", cwd)[-120:]
    path = os.path.join(CACHE_DIR, f"dirty-{key}")
    try:
        if time.time() - os.path.getmtime(path) < DIRTY_CACHE_SECS:
            with open(path, encoding="utf-8") as f:
                return f.read(1) == "1"
    except OSError:
        pass

    dirty = _git(cwd, ["status", "--porcelain", "-uno"], GIT_STATUS_TIMEOUT)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        # Write via a temp file so a concurrent render never reads a half-written
        # value, and so a crash mid-write cannot leave a corrupt cache entry.
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("1" if dirty else "0")
        os.replace(tmp, path)
    except OSError:
        pass
    return bool(dirty)


def git_segment(cwd):
    """branch + dirty marker. Silent on any failure (not a repo, no git, slow FS).

    The two calls are independent on purpose. `git status` walks the whole work
    tree, which on a WSL2 mount of an NTFS directory can take seconds, while
    `rev-parse` only reads HEAD. Sharing a timeout meant one slow repo lost its
    branch as well as its dirty flag.
    """
    # cwd=None would make subprocess inherit the process directory, which is the
    # very guess the caller declined to make.
    if not cwd or not os.path.isdir(cwd):
        return None

    branch = _git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"], GIT_HEAD_TIMEOUT)
    if not branch:
        return None

    # A dirty check that times out yields no marker rather than no branch. Cheap
    # flags first: -uno skips untracked files, which is the expensive part of the
    # walk, and the marker only reports tracked modifications.
    mark = "*" if dirty_cached(cwd) else ""
    return f"{CYAN}{branch}{RED}{mark}{RESET}"


# -------------------------------------------------------------- service status

# Statuspage's own vocabulary, worst to best. Anything not in this list (a new
# indicator value the page starts using, or a timed-out/malformed response)
# renders nothing rather than guess a severity for it.
_SERVICE_STATUS_TIER = {
    "major_outage": (RED, "outage"),
    "partial_outage": (RED, "partial outage"),
    "degraded_performance": (YELLOW, "degraded"),
    "under_maintenance": (YELLOW, "maintenance"),
}


def _read_service_status_cache():
    """(status_str, fetched_at) from disk, or (None, None) if absent/corrupt."""
    try:
        with open(SERVICE_STATUS_CACHE_PATH, encoding="utf-8") as f:
            rec = json.load(f)
        return rec.get("status"), float(rec.get("fetched_at", 0))
    except (OSError, ValueError, TypeError):
        return None, None


def _spawn_background_refresh():
    """Fire-and-forget a refresh of the cache file; never block the render.

    Re-invokes this same script with an internal flag so there is no second
    file to install or keep in sync. Detached (its own session, stdio to
    devnull) so it outlives this process without the statusline waiting on it
    or a leaked pipe holding the terminal open.
    """
    try:
        kwargs = dict(
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
        if hasattr(os, "setsid"):  # POSIX: detach from our process group
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, __file__, "--refresh-service-status"], **kwargs)
    except OSError:
        pass


def _do_refresh_service_status():
    """Fetch status.claude.com and atomically write the cache. Runs standalone.

    Imports urllib lazily and only here: the main render path must not pay for
    it, and must never touch the network at all. Any failure (network, bad
    JSON, missing component, timeout) leaves the previous cache file alone --
    a stale "operational" is harmless, and a stale incident is at worst a late
    all-clear, never a fabricated one.
    """
    import urllib.request

    try:
        req = urllib.request.Request(
            SERVICE_STATUS_URL, headers={"User-Agent": "claude-code-statusline"},
        )
        with urllib.request.urlopen(req, timeout=SERVICE_STATUS_TIMEOUT) as resp:
            body = json.load(resp)
    except Exception:
        return

    status = None
    for comp in body.get("components", []) if isinstance(body, dict) else []:
        if isinstance(comp, dict) and comp.get("name") == SERVICE_STATUS_COMPONENT:
            status = comp.get("status")
            break
    if not isinstance(status, str):
        return

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{SERVICE_STATUS_CACHE_PATH}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"status": status, "fetched_at": time.time()}, f)
        os.replace(tmp, SERVICE_STATUS_CACHE_PATH)
    except OSError:
        pass


def service_status_segment():
    """Claude Code component health from status.claude.com, cache-only read.

    The render path never makes a network call itself -- it reads whatever is
    already on disk and, if that answer is missing or stale, kicks off a
    background refresh for the *next* render and returns what it has now (which
    may be nothing, on a cold cache). A hung or slow status page therefore can
    never add latency to a prompt; the cost of a slow fetch is one extra render
    without the segment, not a frozen terminal.
    """
    status, fetched_at = _read_service_status_cache()
    stale = fetched_at is None or time.time() - fetched_at > SERVICE_STATUS_CACHE_SECS
    if stale:
        _spawn_background_refresh()

    if status not in _SERVICE_STATUS_TIER:
        return None  # operational, unknown, or no cache yet: say nothing
    color, label = _SERVICE_STATUS_TIER[status]
    return f"{color}{G_PULSE} {label}{RESET}"


# ------------------------------------------------------------------ session

SESSIONS_DIR_NAME = "sessions"


def session_name_lookup(session_id, cfg_dir=None):
    """Short SendMessage/ListAgents name (e.g. "work-aa") for this session_id.

    Undocumented internal state: Claude Code drops one <pid>.json per running
    process under ~/.claude/sessions, each carrying its own sessionId and
    name. There is no index by session_id, so this scans the directory --
    fine at the handful of files a single machine ever has open. Any failure
    (missing dir, bad JSON, no match) returns None so the caller's hash
    fallback takes over instead of showing a wrong or crashed segment.
    """
    cfg_dir = cfg_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    sessions_dir = os.path.join(cfg_dir, SESSIONS_DIR_NAME)
    try:
        entries = os.scandir(sessions_dir)
    except OSError:
        return None
    with entries:
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            try:
                with open(entry.path, encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("sessionId") == session_id:
                # Every nameSource is SendMessage-addressable -- "auto" included.
                # An earlier version of this filtered "auto" out on the theory
                # that it meant a long AI-generated title rather than a short
                # handle, but that was wrong: a session with nameSource "auto"
                # (e.g. "pr-auditor-footer-styling") routes a SendMessage by
                # that exact name just like a "derived" work-xx one does. The
                # field describes how the name was picked, not whether it works.
                name = rec.get("name")
                if isinstance(name, str) and name:
                    return name
    return None


# ---------------------------------------------------------------------- main


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0

    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    # No "." fallback: the process cwd is wherever Claude Code happens to be,
    # not the session's directory, so guessing it reports a branch for a repo
    # this session is not in. Better to show no branch than the wrong one.
    cwd = dig(data, "workspace", "current_dir") or data.get("cwd")

    # ---- line 1: identity + money
    left = []

    badge = caveman_badge(cfg_dir)
    if badge:
        left.append((0, badge))

    # An active incident on status.claude.com outranks everything else on the
    # line -- it explains away odd behavior before you go looking for a local
    # cause -- but it is invisible the overwhelming majority of the time
    # (operational, or no cache yet), so in practice it costs nothing.
    svc = service_status_segment()
    if svc:
        left.append((0, svc))

    model = dig(data, "model", "display_name")
    if model:
        fast = f" {YELLOW}{G_FAST}{RESET}" if data.get("fast_mode") else ""
        effort = dig(data, "effort", "level")
        suffix = f"{GREY}:{effort}{RESET}" if effort else ""
        left.append((0, f"{CYAN}{G_MODEL}{RESET} \033[1m{model}{RESET}{suffix}{fast}"))

    git = git_segment(cwd)
    if git:
        wt = dig(data, "workspace", "git_worktree")
        seg = f"{GREY}{G_BRANCH}{RESET} {git}"
        if wt:
            seg += f" {GREY}{G_TREE} {wt}{RESET}"
        left.append((1, seg))

    pr_num = dig(data, "pr", "number")
    if pr_num:
        state = dig(data, "pr", "review_state")
        icon = {
            "approved": f"{GREEN}{G_OK}",
            "changes_requested": f"{RED}{G_NO}",
            "pending": f"{YELLOW}{G_DOT}",
            "draft": f"{GREY}{G_DRAFT}",
        }.get(state, "")
        glyph = G_MR if dig(data, "pr", "kind") == "mr" else G_PR
        seg = f"{GREY}{glyph} {pr_num}{RESET}"
        if icon:
            seg += f" {icon}{RESET}"
        left.append((2, seg))

    added = dig(data, "cost", "total_lines_added", default=0)
    removed = dig(data, "cost", "total_lines_removed", default=0)
    if added or removed:
        # Show only the non-zero halves so a read-only or pure-delete session
        # does not render a meaningless "+0".
        parts = []
        if added:
            parts.append(f"{GREEN}+{added}{RESET}")
        if removed:
            parts.append(f"{RED}-{removed}{RESET}")
        left.append((4, f"{GREY}{G_DIFF}{RESET} " + f"{GREY}/{RESET}".join(parts)))

    agent = dig(data, "agent", "name")
    if agent:
        left.append((2, f"{GREY}{G_AGENT} {agent}{RESET}"))

    # Session tag: the name SendMessage/ListAgents use to address this session
    # (e.g. "work-aa", or an auto-generated one like "pr-auditor-footer-styling"
    # -- both route). That name isn't in the statusline JSON at all -- it lives
    # in Claude Code's internal ~/.claude/sessions/<pid>.json, keyed by the same
    # session_id we do get.
    #
    # No fallback when the lookup comes up empty: an earlier version showed the
    # first 6 chars of session_id instead, on the assumption that it matched
    # the bracketed suffix ListAgents prints. It does not -- that suffix is
    # derived some other way and does not appear anywhere in the session file,
    # so the fallback was printing a plausible-looking ID that silently fails
    # every SendMessage sent to it. Showing nothing is honest; showing a wrong
    # ID is worse than showing none, since only one of those looks reachable
    # to an agent that doesn't know better.
    sess_id = dig(data, "session_id")
    if sess_id:
        tag = session_name_lookup(sess_id)
        if tag:
            left.append((3, f"{GREY}ID {CYAN}{tag}{RESET}"))

    vim_mode = dig(data, "vim", "mode")
    if vim_mode:
        left.append((5, f"{GREY}{vim_mode}{RESET}"))

    # ---- line 2: the three gauges
    gauges = []

    ctx_pct = dig(data, "context_window", "used_percentage")
    if ctx_pct is None:
        # Before the first API response there is no percentage. Fall back to
        # deriving one from raw counts so the bar is not blank on turn one.
        size = dig(data, "context_window", "context_window_size")
        cur = dig(data, "context_window", "current_usage")
        if isinstance(size, (int, float)) and size and isinstance(cur, dict):
            used = sum(
                cur.get(k, 0) or 0
                for k in ("input_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens")
            )
            ctx_pct = used / size * 100.0
    if ctx_pct is not None:
        # Window size is fixed for the session, so it earns no permanent column;
        # the percentage is the part that moves.
        g = gauge(G_CTX, ctx_pct)
        # Auto-compact is close enough to matter here: the bar already shows the
        # level, but the warning says it is about to act on its own.
        if float(ctx_pct) >= COMPACT_WARN:
            g += f" {YELLOW}{G_WARN}{RESET}"
        gauges.append((0, g))

    # The 5-hour window bites first, so it outranks the weekly one when the
    # terminal is too narrow to hold both.
    for key, glyph, prio in (("five_hour", G_CLOCK, 1),
                             ("seven_day", G_CAL, 2),
                             ("spend_limit", G_WALLET, 2)):
        pct = dig(data, "rate_limits", key, "used_percentage")
        if pct is None:
            continue
        g = gauge(glyph, pct)
        arrow = burn_arrow(pct, dig(data, "rate_limits", key, "resets_at"),
                           WINDOW_SECS.get(key))
        if arrow:
            g += f" {arrow}"
        # Always show the reset clock: knowing when a window rolls over is
        # useful at 8% too, not only once it is nearly spent.
        reset = until(dig(data, "rate_limits", key, "resets_at"))
        if reset:
            # Nerd Font glyphs render full-width; without padding on both
            # sides the reload icon collides with the % and the duration.
            g += f" {GREY}{G_RESET} {reset}{RESET}"
        gauges.append((prio, g))

    # Cache hit ratio is the one gauge where high is good, so it would read
    # backwards sitting next to three where high is bad. Show it only once it
    # has actually degraded, and color it on inverted polarity.
    cache_ratio = dig(data, "prompt_cache", "hit_ratio")
    if isinstance(cache_ratio, (int, float)):
        cache_pct = cache_ratio * 100.0
        if cache_pct < CACHE_HEALTHY:
            c = tier(100.0 - cache_pct)
            gauges.append((4, f"{GREY}{G_CACHE}{RESET} {c}{cache_pct:.0f}%{RESET}"))

    # Cache expiry, but only near the edge: this is the one window where knowing
    # the deadline changes a decision — pause now and the next turn is cheap,
    # pause past it and the whole context re-caches.
    expires_at = dig(data, "prompt_cache", "expires_at")
    if expires_at:
        left_secs = int(expires_at) - int(time.time())
        if 0 < left_secs <= CACHE_TTL_WARN_SECS:
            left_str = until(expires_at)
            gauges.append((3, f"{GREY}{G_TTL}{RESET} {YELLOW}{left_str}{RESET}"))

    budget = term_width()
    lines = [s for s in (fit(left, budget), fit(gauges, budget)) if s]
    sys.stdout.write("\n".join(lines))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--refresh-service-status":
        # Detached child spawned by service_status_segment(); does the one
        # network call this script ever makes, then exits. No stdin to read,
        # nothing to print -- a crash here is invisible and harmless, the next
        # render just sees the same stale (or absent) cache and retries.
        try:
            _do_refresh_service_status()
        except Exception:
            pass
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception:
        # A crashed statusline must never break the prompt.
        sys.exit(0)
