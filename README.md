# claude-code-config

Personal Claude Code configuration: a two-line statusline plus the `~/.claude/settings.json`
it is wired into.

## The statusline

```
CV     Opus:medium     main*     1234    +156/-23
 8%    4%   4h50m    12%  7d0h
```

Line 1 is identity, line 2 is pressure. Under load:

```
 ▰▰▰▰▰▰▰▱ 97%     ▰▰▰▰▰▰▰▱ 96%   10m   ▰▰▰▰▰▰▰▰ 100%   1h   31%   45s
```

### Line 1

| Segment | Source | Notes |
|---|---|---|
| `CV` | `~/.claude/.caveman-active` | Caveman mode. Color is the level: grey `CV-` lite, orange `CV` full, red `CV+` ultra, cyan for the wenyan family, `CVc`/`CVr`/`CVz` for commit/review/compress. |
| SVC |  [status.claude.com](https://status.claude.com) | Only appears when the Claude Code component is degraded, in a partial/major outage, or under maintenance — silent the rest of the time. See below. |
| model |  `model.display_name` | `:effort` suffix, plus a bolt when fast mode is on. |
| branch |  `git` | Red `*` when dirty; worktree name appended when in one. |
| PR |  `pr.number` | Review state as a trailing icon. `MR` glyph for GitLab. |
| diff |  `cost.total_lines_*` | Only the non-zero halves render. |
| agent |  `agent.name` | Present only under `--agent`. |
| vim | `vim.mode` | Present only with vim mode enabled. |

### Line 2

| Gauge | Behavior |
|---|---|
|  ctx | Context window. Bare number under 60%, segmented bar above (absolute fill, so it agrees with the number). Yellow  above 85%, where auto-compact gets close. |
|  5h | Session rate limit. Reset clock always shown. Pace arrow  /  once the window is ≥25% elapsed and usage drifts more than 10 points from the elapsed share — or at any point past 50% of the quota, where the reading matters whatever the clock says. |
|  7d | Weekly rate limit. Same, without the arrow — over seven days a double-digit drift is ordinary variation. |
|  cache | Prompt cache hit ratio. Hidden while healthy (≥70%); appears on **inverted** polarity, so low is red. |
|  exp | Cache TTL, shown only within 8 minutes of expiry — the window where pausing actually costs a re-cache. |

Both lines are fitted to the terminal width and shed segments by priority rather
than wrapping. `ctx`, the model, the caveman badge, and an active incident never
drop; vim goes first, then diff/cache, then PR/agent, then the weekly window,
then the 5-hour one.

### Service status (`SVC`)

Polls the public, unauthenticated [status.claude.com](https://status.claude.com)
Statuspage API for the health of the **Claude Code** component specifically —
not claude.ai or the API, which can be down independently. The render path
never makes the network call itself: it only ever reads a small cache file at
`~/.claude/cache/statusline/service-status.json`, and if that file is missing
or older than 5 minutes, it spawns a detached background process to refresh it
for the *next* render. A slow or unreachable status page therefore costs one
render without the segment, never a frozen prompt — and a fetch that fails
outright leaves the previous cached value alone rather than clearing it, so a
real incident stays visible across the whole cache window even through
repeated failed refreshes.

Override the poll interval with `service_status_cache_secs` in
`statusline.local.json` (seconds, default `300`).

## Requirements

- **Python 3** — no third-party packages.
- **A Nerd Font.** Built against MesloLGS NF. Without one every glyph renders as
  a blank box; set `CLAUDE_STATUSLINE_ASCII=1` for text labels instead.
- **Pro or Max subscription** for the `5h` / `7d` gauges. Claude Code only sends
  `rate_limits` to the statusline for subscribers, and only after the first API
  response of a session — on an API-key account those two gauges never appear.

## Install

```sh
./install.sh
```

Symlinks `hooks/statusline.py` into `~/.claude/hooks/` and points
`statusLine` at it, backing up the existing `settings.json` first. Run with
`--copy` to copy instead of symlink.

Or by hand:

```sh
cp hooks/statusline.py ~/.claude/hooks/
```

then in `~/.claude/settings.json`:

```json
"statusLine": {
  "type": "command",
  "command": "python3 /home/YOU/.claude/hooks/statusline.py"
}
```

## settings.json

The real `~/.claude/settings.json` is not tracked — it is full of machine-specific
absolute paths. `settings.example.json` shows the shape instead.

Note that Claude Code does **not** expand `$HOME` in hook commands, so the
placeholders in the example have to become real absolute paths before use. The
`SessionStart` / `UserPromptSubmit` entries there are only meaningful with the
[`caveman`](https://github.com/JuliusBrussee/caveman) plugin installed; drop them
otherwise. Only `statusLine` is needed for this repo.

## Tuning

Most of these are overridable per machine via `~/.claude/statusline.local.json`
(see `statusline.local.example.json` for the key names) without forking the
script; the rest are constants at the top of `hooks/statusline.py`.

| Setting | Local key | Default | Effect |
|---|---|---|---|
| `BAR_WIDTH` | `bar_width` | `8` | Gauge segments. 10 or 12 splits the 86-99% range, which currently flattens. |
| `BAR_THRESHOLD` | `bar_threshold` | `60.0` | Below this a gauge is a bare number. |
| `SEGMENT_SPACING` | `segment_spacing` | `""` | Inserted between bar cells; some fonts render them touching without it. |
| `BURN_MIN_ELAPSED` | `burn_min_elapsed` | `25.0` | Window share that must elapse before a pace arrow. |
| `BURN_ALWAYS_PCT` | `burn_always_pct` | `50.0` | Quota share that shows the arrow regardless of the floor. |
| `BURN_DRIFT_THRESHOLD` | `burn_drift_threshold` | `5.0` | How far usage must drift from elapsed share before the arrow fires. |
| `SERVICE_STATUS_CACHE_SECS` | `service_status_cache_secs` | `300.0` | How often the `SVC` segment re-polls status.claude.com. |
| `COMPACT_WARN` | — | `85.0` | Where ctx grows its warning. |
| `CACHE_HEALTHY` | — | `70.0` | Above this the cache gauge stays hidden. |
| `CACHE_TTL_WARN_SECS` | — | `480` | How close to expiry `exp` appears. |

The color tiers live in `tier()`: green below 60, yellow to 85, red above.

## Notes

- A crashed statusline must never break the prompt, so `main()` is wrapped and
  every exit is 0. Malformed JSON, missing sections, and wrong types all render
  what they can and drop the rest.
- The `.caveman-active` flag is read with a 64-byte cap, a symlink refusal, and a
  mode whitelist. It is a file on disk that something else writes, and its
  contents reach the terminal on every keystroke — an unfiltered read there would
  let planted ANSI escapes execute.
- Only a true 100% fills the bar completely, so "nearly out" and "out" stay
  distinguishable. The cost is that 86-99% all render 7 of 8 segments; the
  number carries that range.
- Bar width is constant across all values, so the line never shifts as a number
  climbs.
- The bar uses whole segments only. Sub-cell partials mixed glyph heights within
  one bar and read as broken rather than precise, and Powerline end caps painted
  in the tier color made the far end look filled at any level.
- `git status` and `rev-parse` run with separate timeouts: the work-tree walk can
  take seconds on a WSL2 mount of an NTFS directory, and a shared budget meant a
  slow repository lost its branch along with its dirty flag. The dirty answer is
  cached for 10s under `~/.claude/cache/statusline`, so it can lag that far
  behind reality.
- The `SVC` segment never calls the network from the render path — it only ever
  reads a cache file and, on a stale or missing one, spawns a detached
  background process to refresh it for the *next* render. A failed refresh
  leaves the previous cached value in place rather than clearing it, so a real
  incident survives repeated failed polls instead of flickering off.
