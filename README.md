# award-alert-agent

Watch [Seats.aero](https://seats.aero) for business-class (or any-cabin) award space and get
alerted the moment a seat opens — and **manage your alerts by talking to a self-hosted AI agent**
(OpenClaw, Hermes, or any agent that can run a shell command).

The actual watching is a small, deterministic, dependency-free Python daemon that runs on your own
always-on machine every ~10 minutes. The agent only translates your English ("watch business SFO to
Tokyo under 90k in November") into a structured alert — it never sits in the polling loop.

> **Why this exists:** if you already pay for a Seats.aero Partner API key, this spends that access
> you already have — no second alert subscription, and full control over what you watch and how you're
> pinged. See the design notes for the build-vs-buy reasoning.

## How it works

Two planes that meet only at two JSON files:

- **Control plane** — any agent shells out to `manage.py add/list/rm/enable/disable` to edit your
  alert list (`alerts.json`).
- **Data plane** — a `launchd` (or cron) timer runs `run.sh` → `check.py`: it queries the Seats.aero
  Partner API per alert, filters to award space under your mileage cap, dedupes against local state,
  writes `new_hits.json`, and emails you new hits (or lets your agent notify you its own way).

```
you  ──talk──▶  agent  ──manage.py──▶  alerts.json
                                          │
   launchd (every 10 min) ─▶ run.sh ─▶ check.py ─▶ new_hits.json ─▶ notify.py (email)
```

## Privacy & what's committed

**Only code, docs, and `*.example` configs are in this repo.** Your alert list, real config, secrets,
and dedupe state are gitignored and live only on your machine:

- committed: `check.py`, `manage.py`, `notify.py`, `seats_aero.py`, `run.sh`, `setup.sh`, the plist,
  `config.example.json`, `alerts.example.json`, `.env.example`, docs.
- **local-only (gitignored):** `config.json`, `alerts.json`, `.env`, `state.json`, `new_hits.json`,
  `.poll-host`.

## Quick start

```bash
git clone <this repo> && cd award-alert-agent
cp config.example.json config.json     # set your notify.email_to + defaults
cp alerts.example.json alerts.json      # starts effectively empty; your agent fills it
cp .env.example .env && chmod 600 .env  # add SEATS_AERO_API_KEY (+ AWARD_SMTP_PASSWORD for email)
bash setup.sh                            # records this host as the poller, installs the timer, sends a test
```

Then add an alert (by hand or via your agent):

```bash
python3 manage.py add --name "SFO-Tokyo Nov" --from SFO --to NRT,HND,KIX \
  --cabin business --max-miles 90000 --min-seats 2 --start 2026-11-01 --end 2026-11-30
```

See [`AGENTS.md`](AGENTS.md) for wiring this into OpenClaw / Hermes.

## Requirements

- Python 3.12+ (standard library only — no pip install)
- A Seats.aero Partner API key
- An always-on machine (a Mac mini, a small Linux box, a Raspberry Pi) to run the poll

## License

MIT — see [LICENSE](LICENSE).
