# Wiring award-alert-agent into an AI agent

This tool is driven through a **neutral CLI + two JSON files** — not any agent's SDK.
Any agent that can run a shell command (OpenClaw, Hermes, a plain terminal) manages
alerts the same way. Adding a second agent is a wiring step, not a code change.

## The contract

### Commands the agent may run

| Command | What it does | Output |
|---|---|---|
| `python3 manage.py add --name <n> --from <A[,B]> --to <C[,D]> [--cabin <c>] [--max-miles <n>] [--min-seats <n>] [--only-direct] [--start YYYY-MM-DD] [--end YYYY-MM-DD]` | Add (or idempotently replace) an alert | JSON `{"ok": true, "action": "added", "alert": {…, "_resolved": {…}}}` |
| `python3 manage.py list --json` | List alerts with effective (resolved) params | JSON `{"ok": true, "alerts": [...], "count": N}` |
| `python3 manage.py disable --id <id>` / `enable --id <id>` | Pause / resume an alert | JSON `{"ok": true, "action": "...", "id": "..."}` |
| `python3 manage.py rm --id <id>` | Remove an alert | JSON `{"ok": true, "action": "removed", "id": "..."}` |

Every command prints a single JSON object to **stdout** (`{"ok": true|false, ...}`), so the
agent can parse the result and confirm it. On bad input it prints `{"ok": false, "error": "..."}`
and exits non-zero, leaving the alert file **unchanged**. `--cabin` is one of `economy`, `premium`,
`business`, `first`, or `any` — `any` watches every cabin under the one `--max-miles` cap using a
single API call per leg (prefer it over four single-cabin alerts, which cost 4x the API quota). `--name` and all values are stored as
JSON data only — never interpreted as shell or git.

### Files

- `alerts.json` — the alert list. **Agents edit it only through `manage.py`, never by hand.**
- `new_hits.json` — written by `check.py` each poll; the seam an agent reads to notify its own way.
  Each hit (authoritative shape is the dict built in `check.filter_rows`):
  `{alert_id, alert_name, origin, dest, date, cabin, region, airlines, program, miles, seats,
  direct, duration_min, connections, stops, taxes_cents, link}`.

## Invariants (do not violate)

1. **One poll host, many editors.** Exactly one machine runs the poll timer (the one where
   `setup.sh` ran and wrote `.poll-host`). Any other agent host is an **editor only** — it may run
   `manage.py`, but must **never** install the launchd timer. `check.py` self-guards: on a
   non-designated host it does nothing and reports `NEW_HITS: 0`, so a stray poller can't double-alert.
2. **`manage.py` is the only sanctioned mutator.** Don't hand-edit `alerts.json`.
3. **Secrets live in `.env`** (gitignored, `chmod 600`) — never in `alerts.json`, never in git, never
   echoed to logs.

## Per-agent wiring

The recipe is identical for every agent — register these shell commands as tools/skills the agent may
invoke, and teach it the mapping "user says watch X → `manage.py add …`". The exact tool-manifest
syntax differs per agent; confirm it against that agent's own docs at wire-up.

**OpenClaw / Hermes / any shell-capable agent:**
- Expose `python3 <repo>/manage.py add|list|rm|enable|disable` as a callable tool.
- Give the agent this rule: translate a natural-language request ("watch business SFO→Tokyo under 90k
  in November, 2 seats") into a single `manage.py add` call; read the JSON result back to confirm.
- Optional — **agent-owned notification:** instead of (or in addition to) the built-in SMTP email,
  the agent can watch `new_hits.json` after each poll and deliver hits through its own channel
  (push, Slack, chat). This is the one piece that is genuinely per-agent; the built-in SMTP email is
  the default so notifications work even when no agent is running.

## Example exchange

> **User:** watch business SFO to Tokyo under 90k, any time in November, at least 2 seats
> **Agent:** *runs* `python3 manage.py add --name "SFO-Tokyo Nov" --from SFO --to NRT,HND,KIX --cabin business --max-miles 90000 --min-seats 2 --start 2026-11-01 --end 2026-11-30`
> → `{"ok": true, "action": "added", "alert": {"id": "…", "_resolved": {"max_miles": 90000, …}}}`
> **Agent:** "Added 'SFO-Tokyo Nov'. I'll check every 10 minutes and let you know when a seat opens."
