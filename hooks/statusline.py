#!/usr/bin/env python3
"""Claude Code statusline: two lines, gauge bars, 3-tier warn colors.

Line 1: [CAVEMAN] model <badge>  branch*  PR  +added/-removed  agent  vim
Line 2: ctx NN% (200K)  5h NN%  7d NN%  cache NN%  rate  turns

Gauges render as a bare number while quiet and grow a segmented bar once
past BAR_THRESHOLD, so line 2 only widens when something is actually filling up.
Both lines are fitted to the terminal width, dropping the lowest-priority
segments first rather than wrapping.

Wired via ~/.claude/settings.json:
  "statusLine": {"type": "command", "command": "python3 /home/.../statusline.py"}

Every field is optional in the input JSON (rate_limits only exists for Pro/Max,
and only after the first API response), so every read is defensive: a missing
section drops its segment rather than raising.

New features (v2):
  A. Model-aware intelligence: tier badge, context window size, sustainable rate
  B. Window analytics: ETA, pace score, bottleneck detection
  C. Context intelligence: tokens remaining, context burn rate
  D. Session analytics: session duration, total turns
  F. Rate tracking: EMA smoothing, confidence indicator, per-window tracking
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
BOLD = "\033[1m"
ORANGE = "\033[38;5;172m"  # caveman badge
GREY = "\033[38;5;245m"
GREEN = "\033[38;5;71m"
YELLOW = "\033[38;5;179m"
RED = "\033[38;5;167m"
CYAN = "\033[38;5;73m"
PURPLE = "\033[38;5;141m"

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
G_RATE = "" if NF else "r/h"   # tachometer, consumption rate per hour
G_TURNS = "" if NF else "~t"    # hourglass-half, estimated turns remaining
G_BOLT = "⚡"                   # bottleneck indicator (unicode, no NF needed)


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
WINDOW_SECS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}

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

# --------------------------------------------------- rate & turns estimation
# Snapshots of rate-limit percentages, persisted to disk so the rate survives
# across renders and even across short restarts. Keyed by session_id.
RATE_CACHE_MAX_SNAPSHOTS = LOCAL.get("rate_max_snapshots", 200)
RATE_CACHE_MIN_INTERVAL_SECS = LOCAL.get("rate_min_interval", 10.0)
# Need enough elapsed time for the slope to mean something.
RATE_MIN_DATA_SECS = LOCAL.get("rate_min_data_secs", 120.0)
# A "turn" is a distinct pct jump separated by a time gap. Small intra-turn
# renders that nudge the pct by dust don't count.
RATE_JUMP_THRESHOLD = LOCAL.get("rate_jump_threshold", 0.1)
RATE_JUMP_MIN_GAP_SECS = LOCAL.get("rate_jump_min_gap", 15.0)
RATE_MIN_TURNS = LOCAL.get("rate_min_turns", 2)
# EMA alpha for smoothing per-interval rates (Feature 13)
RATE_EMA_ALPHA = LOCAL.get("rate_ema_alpha", 0.3)
# Minimum snapshots to use EMA instead of linear (Feature 13)
RATE_EMA_MIN_SNAPS = LOCAL.get("rate_ema_min_snaps", 4)
# Minimum jumps for high-confidence turn estimate (Feature 14)
RATE_CONFIDENCE_MIN_JUMPS = LOCAL.get("rate_confidence_min_jumps", 3)
# Minimum snaps for high-confidence rate (Feature 14)
RATE_CONFIDENCE_MIN_SNAPS = LOCAL.get("rate_confidence_min_snaps", 4)

# Model-aware sustainable rate thresholds (Feature 3 / A)
# Overrides RATE_SUSTAINABLE based on model tier:
RATE_SUSTAINABLE_BY_TIER = {
    "opus":   LOCAL.get("rate_sustainable_opus",   15.0),
    "ultra":  LOCAL.get("rate_sustainable_ultra",  15.0),
    "sonnet": LOCAL.get("rate_sustainable_sonnet", 20.0),
    "pro":    LOCAL.get("rate_sustainable_pro",    20.0),
    "flash":  LOCAL.get("rate_sustainable_flash",  35.0),
    "haiku":  LOCAL.get("rate_sustainable_haiku",  35.0),
}
RATE_SUSTAINABLE = LOCAL.get("rate_sustainable", 20.0)  # fallback
RATE_WARN = LOCAL.get("rate_warn", 30.0)

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


# ---------------------------------------------------------------- model tier (Feature A)

# Known context window sizes by model tier (K tokens)
MODEL_CONTEXT_K = {
    "opus":   200,
    "sonnet": 200,
    "haiku":  200,
    "flash":  1000,
    "pro":    128,
    "ultra":  200,
}

# Default context window size (K) for unknown models
MODEL_CONTEXT_K_DEFAULT = 200


def detect_model_tier(display_name):
    """Detect model family from display_name string. Returns lowercase tier name or None."""
    if not display_name or not isinstance(display_name, str):
        return None
    dn = display_name.lower()
    for tier_name in ("opus", "ultra", "sonnet", "haiku", "flash", "pro"):
        if tier_name in dn:
            return tier_name
    return None


def model_badge(tier_name, display_name=None):
    """Return colored model tier badge string with version if present."""
    ver = ""
    if display_name:
        m_ver = re.search(r"\b(\d+(?:\.\d+)?)\b", display_name)
        if m_ver:
            ver = m_ver.group(1)

    dn = (display_name or "").lower()
    prefix = ""
    if "gemini" in dn:
        prefix = f"{BOLD}{CYAN}G{ver}{RESET} " if ver else f"{CYAN}G{RESET} "
    elif "claude" in dn:
        prefix = f"{PURPLE}C{ver}{RESET} " if ver else f"{PURPLE}C{RESET} "
    elif "gpt" in dn:
        prefix = f"{YELLOW}GPT{RESET} "

    if tier_name == "opus":
        b = f"{BOLD}{PURPLE}OPS{RESET}"
    elif tier_name == "sonnet":
        b = f"{CYAN}SNT{RESET}"
    elif tier_name == "haiku":
        b = f"{GREEN}HKU{RESET}"
    elif tier_name == "flash":
        b = f"{YELLOW}FLS{RESET}"
    elif tier_name == "pro":
        b = f"{CYAN}PRO{RESET}"
    elif tier_name == "ultra":
        b = f"{BOLD}{RED}ULT{RESET}"
    else:
        b = f"{GREY}???{RESET}"
    return f"{prefix}{b}" if prefix else b


def model_context_k(tier_name):
    """Return known context window size in K tokens for model tier. Feature A.2."""
    if tier_name:
        return MODEL_CONTEXT_K.get(tier_name, MODEL_CONTEXT_K_DEFAULT)
    return MODEL_CONTEXT_K_DEFAULT


def model_sustainable_rate(tier_name):
    """Return sustainable %/h rate for this model tier. Feature A.3."""
    if tier_name:
        return RATE_SUSTAINABLE_BY_TIER.get(tier_name, RATE_SUSTAINABLE)
    return RATE_SUSTAINABLE


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


# ------------------------------------------------------- rate & turns tracking
# Per-window tracking: track five_hour and seven_day separately (Feature 15/F)


def _rate_cache_path(session_id):
    """Cache file path for rate-tracking snapshots, keyed by session."""
    key = re.sub(r"[^A-Za-z0-9]", "_", session_id)[-60:]
    return os.path.join(CACHE_DIR, f"rate-{key}.json")


def _load_rate_cache(session_id):
    """Load rate-tracking snapshots from disk, or return an empty structure."""
    path = _rate_cache_path(session_id)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("sid") != session_id:
            return {"sid": session_id, "snaps": [], "snaps_5h": [], "snaps_7d": []}
        # Ensure per-window arrays exist (upgrade old caches gracefully)
        for key in ("snaps", "snaps_5h", "snaps_7d"):
            if not isinstance(data.get(key), list):
                data[key] = []
        return data
    except (OSError, ValueError):
        return {"sid": session_id, "snaps": [], "snaps_5h": [], "snaps_7d": []}


def _save_rate_cache(cache):
    """Persist rate-tracking snapshots to disk, atomically."""
    path = _rate_cache_path(cache["sid"])
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


def _ema_rate(snaps):
    """Compute EMA-smoothed rate in %/hour from snapshot list.

    Feature 13/F: Uses Exponential Moving Average of per-interval rates
    (alpha=0.3) to smooth out bursty turns. Falls back to linear if <4 samples.

    Returns (rate_pct_per_hour, is_confident).
    """
    if len(snaps) < 2:
        return None, False

    if len(snaps) < RATE_EMA_MIN_SNAPS:
        # Linear fallback: first→last slope
        first, last = snaps[0], snaps[-1]
        dt = last["t"] - first["t"]
        dp = last["p"] - first["p"]
        if dt >= RATE_MIN_DATA_SECS and dp > 0:
            return dp / (dt / 3600.0), False
        return None, False

    # EMA of per-interval rates
    ema = None
    total_dt = snaps[-1]["t"] - snaps[0]["t"]
    if total_dt < RATE_MIN_DATA_SECS:
        return None, False

    for i in range(1, len(snaps)):
        dt_i = snaps[i]["t"] - snaps[i - 1]["t"]
        dp_i = snaps[i]["p"] - snaps[i - 1]["p"]
        if dt_i <= 0:
            continue
        r_i = dp_i / (dt_i / 3600.0)
        if ema is None:
            ema = r_i
        else:
            ema = RATE_EMA_ALPHA * r_i + (1 - RATE_EMA_ALPHA) * ema

    if ema is None or ema <= 0:
        return None, False

    return ema, len(snaps) >= RATE_CONFIDENCE_MIN_SNAPS


def _count_jumps_and_turns(snaps, pct):
    """Count detected turns and estimate remaining turns.

    Returns (jumps, est_turns, is_confident).
    Feature 14/F: confidence flag when <RATE_CONFIDENCE_MIN_JUMPS jumps.
    """
    if len(snaps) < 3:
        return 0, None, False

    jumps = 0
    total_delta = 0.0
    last_jump_t = 0.0
    for i in range(1, len(snaps)):
        delta = snaps[i]["p"] - snaps[i - 1]["p"]
        if (delta > RATE_JUMP_THRESHOLD
                and snaps[i]["t"] - last_jump_t >= RATE_JUMP_MIN_GAP_SECS):
            jumps += 1
            total_delta += delta
            last_jump_t = snaps[i]["t"]

    if jumps < RATE_MIN_TURNS or total_delta <= 0:
        return jumps, None, False

    avg_per_turn = total_delta / jumps
    remaining = 100.0 - pct
    turns = max(0, int(remaining / avg_per_turn)) if remaining > 0 else 0
    confident = jumps >= RATE_CONFIDENCE_MIN_JUMPS
    return jumps, turns, confident


def _append_snap(snaps, now, pct, max_snaps=RATE_CACHE_MAX_SNAPSHOTS):
    """Throttle and append snapshot; reset on window rollover."""
    if snaps and pct < snaps[-1].get("p", 0) - 0.5:
        snaps = []
    if not snaps or now - snaps[-1]["t"] >= RATE_CACHE_MIN_INTERVAL_SECS:
        snaps.append({"t": now, "p": pct})
        if len(snaps) > max_snaps:
            snaps = snaps[-max_snaps:]
    return snaps


def record_and_analyze(session_id, five_h_pct, seven_d_pct):
    """Record rate-limit snapshots per window and return analytics.

    Feature 15/F: track 5h and 7d windows in separate snap arrays.
    Feature 13/F: EMA smoothing.
    Feature 14/F: confidence indicators.

    Returns:
        (rate_per_hour, est_turns, rate_confident, turns_confident,
         session_start_t, total_jumps)
    """
    if not session_id:
        return None, None, False, False, None, 0

    cache = _load_rate_cache(session_id)
    now = time.time()

    # Per-window tracking (Feature 15)
    pct_5h = float(five_h_pct) if five_h_pct is not None else None
    pct_7d = float(seven_d_pct) if seven_d_pct is not None else None

    # Pick primary tracking metric: 5h bites first
    pct = pct_5h if pct_5h is not None else pct_7d
    if pct is None:
        return None, None, False, False, None, 0

    # Update per-window snaps
    if pct_5h is not None:
        cache["snaps_5h"] = _append_snap(cache.get("snaps_5h", []), now, pct_5h)
    if pct_7d is not None:
        cache["snaps_7d"] = _append_snap(cache.get("snaps_7d", []), now, pct_7d)

    # Also update combined snaps (for backward compat / session-start tracking)
    cache["snaps"] = _append_snap(cache.get("snaps", []), now, pct)

    _save_rate_cache(cache)

    # Use 5h snaps preferentially for rate/turns, fall back to combined
    active_snaps = cache["snaps_5h"] if cache.get("snaps_5h") else cache["snaps"]

    # --- Compute rate with EMA ---
    rate, rate_confident = _ema_rate(active_snaps)

    # --- Estimate turns remaining ---
    jumps, est_turns, turns_confident = _count_jumps_and_turns(active_snaps, pct)

    # Session start = first snapshot timestamp
    all_snaps = cache.get("snaps", [])
    session_start_t = all_snaps[0]["t"] if all_snaps else None

    return rate, est_turns, rate_confident, turns_confident, session_start_t, jumps


# ---------------------------------------------------------------- window analytics (Feature B)


def window_eta_secs(pct, rate_pct_per_hour):
    """Estimate seconds until quota hits 100% at current rate. Feature B.4.

    Returns None if rate unknown or infinite.
    """
    if rate_pct_per_hour is None or rate_pct_per_hour <= 0:
        return None
    remaining_pct = 100.0 - float(pct)
    if remaining_pct <= 0:
        return 0
    hours = remaining_pct / rate_pct_per_hour
    return hours * 3600.0


def pace_score(pct, resets_at, window_secs):
    """Pace score: ratio of (pct_used / pct_of_window_elapsed). Feature B.5.

    >1.2 burning fast (red), 0.8-1.2 on-pace (yellow), <0.8 efficient (green).
    Returns (score, color_str) or (None, None).
    """
    if not resets_at or not window_secs or pct is None:
        return None, None
    remaining = int(resets_at) - int(time.time())
    if remaining <= 0 or remaining >= window_secs:
        return None, None
    elapsed_share = (window_secs - remaining) / window_secs * 100.0
    if elapsed_share < 5.0:
        return None, None  # too early for meaningful score
    pct_f = float(pct)
    if pct_f <= 0 or elapsed_share <= 0:
        return None, None
    score = pct_f / elapsed_share
    if score > 1.2:
        color = RED
    elif score >= 0.8:
        color = YELLOW
    else:
        color = GREEN
    return score, color


def find_bottleneck(windows):
    """Find the window that will be exhausted first. Feature B.6.

    `windows`: list of (key, pct, resets_at, window_secs, rate_pct_per_hour)
    Returns index of bottleneck window, or None if not determinable.
    Only returns a value when ETA < reset time for at least one window.
    """
    min_eta = None
    min_idx = None
    for i, (key, pct, resets_at, wsecs, rate) in enumerate(windows):
        if pct is None or rate is None:
            continue
        eta = window_eta_secs(pct, rate)
        if eta is None:
            continue
        reset_secs = (int(resets_at) - int(time.time())) if resets_at else None
        if reset_secs is None or eta >= reset_secs:
            continue  # won't hit cap before reset
        if min_eta is None or eta < min_eta:
            min_eta = eta
            min_idx = i
    return min_idx


# ------------------------------------------------------------------ session (Feature D)


def format_session_duration(session_start_t):
    """Format session duration as 'Xh Ym'. Feature D.9."""
    if session_start_t is None:
        return None
    elapsed = time.time() - session_start_t
    if elapsed < 60:
        return None  # too short to be informative
    h = int(elapsed // 3600)
    m = int((elapsed % 3600) // 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


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
                #
                # Caveat this segment cannot fix: this is a *self-report*, the
                # same string ListAgents prints as "This session is X [ref]"
                # for itself. It is reachable from a peer that already has
                # this session in ITS OWN live ListAgents -- but a peer with
                # no such row (different machine, stale/never-populated
                # listing) cannot dial this name even though it looks like a
                # normal ListAgents row. It also drifts: the name can change
                # between when this renders and when someone acts on it.
                # Read this ID as "what I currently call myself", not as a
                # guarantee any given peer can reach it.
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

    # Detect model tier early — used in multiple places
    model_display = dig(data, "model", "display_name")
    m_tier = detect_model_tier(model_display)
    ctx_k = model_context_k(m_tier)
    sustainable_rate = model_sustainable_rate(m_tier)

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

    if model_display:
        fast = f" {YELLOW}{G_FAST}{RESET}" if data.get("fast_mode") else ""
        effort = dig(data, "effort", "level")
        suffix = f"{GREY}:{effort}{RESET}" if effort else ""
        # Feature A.1: model tier badge
        tier_badge = model_badge(m_tier, model_display)
        left.append((0, (
            f"{CYAN}{G_MODEL}{RESET} \033[1m{model_display}{RESET}{suffix}"
            f" {DIM}·{RESET} {tier_badge}{fast}"
        )))

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
    sess_id = dig(data, "session_id")
    if sess_id:
        tag = session_name_lookup(sess_id)
        if tag:
            shown = f'"{tag}"' if " " in tag else tag
            left.append((3, f"{GREY}as {CYAN}{shown}{RESET}"))

    vim_mode = dig(data, "vim", "mode")
    if vim_mode:
        left.append((5, f"{GREY}{vim_mode}{RESET}"))

    # ---- line 2: the gauges

    # --- Rate & turns tracking (Features D, F) ---
    sess_for_rate = dig(data, "session_id")
    five_h_pct = dig(data, "rate_limits", "five_hour", "used_percentage")
    seven_d_pct = dig(data, "rate_limits", "seven_day", "used_percentage")

    (rate, est_turns, rate_confident, turns_confident,
     session_start_t, total_jumps) = record_and_analyze(
        sess_for_rate, five_h_pct, seven_d_pct
    )

    # --- Context window analytics (Feature C) ---
    ctx_pct = dig(data, "context_window", "used_percentage")
    ctx_prev_pct = None  # will try to get from cache for burn rate
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

    # Context burn rate: track ctx_pct in cache for per-turn delta
    ctx_burn_str = None
    ctx_remaining_str = None
    if ctx_pct is not None and sess_for_rate:
        cache_r = _load_rate_cache(sess_for_rate)
        ctx_snaps = cache_r.get("ctx_snaps", [])
        now_t = time.time()

        # Reset on context compaction (pct drops significantly)
        if ctx_snaps and float(ctx_pct) < ctx_snaps[-1].get("p", 0) - 5.0:
            ctx_snaps = []

        if not ctx_snaps or now_t - ctx_snaps[-1]["t"] >= RATE_CACHE_MIN_INTERVAL_SECS:
            ctx_snaps.append({"t": now_t, "p": float(ctx_pct)})
            if len(ctx_snaps) > RATE_CACHE_MAX_SNAPSHOTS:
                ctx_snaps = ctx_snaps[-RATE_CACHE_MAX_SNAPSHOTS:]
            cache_r["ctx_snaps"] = ctx_snaps
            _save_rate_cache(cache_r)

        # Feature C.8: context burn rate (%/turn)
        if len(ctx_snaps) >= 3:
            ctx_jumps = []
            last_ctx_t = 0.0
            for i in range(1, len(ctx_snaps)):
                d = ctx_snaps[i]["p"] - ctx_snaps[i - 1]["p"]
                if d > RATE_JUMP_THRESHOLD and ctx_snaps[i]["t"] - last_ctx_t >= RATE_JUMP_MIN_GAP_SECS:
                    ctx_jumps.append(d)
                    last_ctx_t = ctx_snaps[i]["t"]
            if ctx_jumps:
                avg_ctx_delta = sum(ctx_jumps) / len(ctx_jumps)
                if avg_ctx_delta >= 0.5:
                    ctx_burn_str = f"ctx +{avg_ctx_delta:.1f}%/t"

        # Feature C.7: tokens remaining when >50% used
        if float(ctx_pct) >= 50.0:
            remaining_pct = 100.0 - float(ctx_pct)
            remaining_k = int(remaining_pct / 100.0 * ctx_k)
            if remaining_k > 0:
                ctx_remaining_str = f"~{remaining_k}Kt left"

    # --- Compute window ETAs for bottleneck detection (Feature B.4, B.6) ---
    five_h_resets = dig(data, "rate_limits", "five_hour", "resets_at")
    seven_d_resets = dig(data, "rate_limits", "seven_day", "resets_at")

    windows_info = [
        ("five_hour", five_h_pct, five_h_resets, WINDOW_SECS.get("five_hour"), rate),
        ("seven_day", seven_d_pct, seven_d_resets, WINDOW_SECS.get("seven_day"), rate),
    ]
    bottleneck_idx = find_bottleneck(windows_info)

    gauges = []

    # --- Context gauge ---
    if ctx_pct is not None:
        # Feature A.2: show context window size in K tokens
        g = gauge(G_CTX, ctx_pct)
        g += f" {DIM}({ctx_k}K){RESET}"
        # Auto-compact is close enough to matter here
        if float(ctx_pct) >= COMPACT_WARN:
            g += f" {YELLOW}{G_WARN}{RESET}"
        gauges.append((0, g))

    # Feature C.7: tokens remaining (droppable, priority 6)
    if ctx_remaining_str:
        gauges.append((6, f"{GREY}{ctx_remaining_str}{RESET}"))

    # Feature C.8: context burn rate (droppable, priority 7)
    if ctx_burn_str:
        gauges.append((7, f"{DIM}{ctx_burn_str}{RESET}"))

    # --- Rate-limit window gauges ---
    # The 5-hour window bites first, so it outranks the weekly one when the
    # terminal is too narrow to hold both.
    for idx, (key, glyph, prio) in enumerate((
        ("five_hour", G_CLOCK, 1),
        ("seven_day", G_CAL, 2),
        ("spend_limit", G_WALLET, 2),
    )):
        pct = dig(data, "rate_limits", key, "used_percentage")
        if pct is None:
            continue
        resets_at = dig(data, "rate_limits", key, "resets_at")
        wsecs = WINDOW_SECS.get(key)

        # Feature B.6: bottleneck indicator (⚡ prefix on bottleneck window)
        is_bottleneck = (idx == bottleneck_idx)
        prefix = f"{YELLOW}{G_BOLT}{RESET} " if is_bottleneck else ""

        g = prefix + gauge(glyph, pct)

        arrow = burn_arrow(pct, resets_at, wsecs)
        if arrow:
            g += f" {arrow}"

        # Feature B.5: pace score
        ps, ps_color = pace_score(pct, resets_at, wsecs)
        if ps is not None:
            g += f" {ps_color}pace {ps:.1f}x{RESET}"

        # Always show the reset clock: knowing when a window rolls over is
        # useful at 8% too, not only once it is nearly spent.
        reset = until(resets_at)
        if reset:
            g += f" {GREY}{G_RESET} {reset}{RESET}"

        # Feature B.4: window ETA (show when on pace to hit cap)
        if rate is not None and resets_at:
            eta_secs = window_eta_secs(pct, rate)
            reset_remaining = int(resets_at) - int(time.time())
            if eta_secs is not None and reset_remaining > 0 and eta_secs < reset_remaining:
                eta_h = int(eta_secs // 3600)
                eta_m = int((eta_secs % 3600) // 60)
                if eta_h > 0:
                    eta_str = f"~{eta_h}h"
                else:
                    eta_str = f"~{eta_m}m"
                g += f" {RED}hit {eta_str}{RESET}"

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

    # ---- consumption rate and estimated turns remaining (Feature F)
    if rate is not None:
        # Feature A.3: model-aware sustainable rate thresholds
        conf_suffix = "" if rate_confident else "?"
        if rate >= RATE_WARN:
            rate_color = RED
        elif rate >= sustainable_rate:
            rate_color = YELLOW
        else:
            rate_color = GREEN
        gauges.append((5, f"{GREY}{G_RATE}{RESET} {rate_color}{rate:.1f}%/h{conf_suffix}{RESET}"))

    if est_turns is not None:
        conf_suffix = "" if turns_confident else "?"
        if est_turns > 20:
            turns_color = GREEN
        elif est_turns > 5:
            turns_color = YELLOW
        else:
            turns_color = RED
        gauges.append((6, f"{GREY}{G_TURNS}{RESET} {turns_color}~{est_turns}t{conf_suffix}{RESET}"))

    # Feature D.9: session duration (priority 7)
    sess_dur = format_session_duration(session_start_t)
    if sess_dur:
        gauges.append((7, f"{GREY}sess {sess_dur}{RESET}"))

    # Feature D.10: total turns this session (priority 8)
    if total_jumps >= RATE_MIN_TURNS:
        gauges.append((8, f"{GREY}{total_jumps}t total{RESET}"))

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