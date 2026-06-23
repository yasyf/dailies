# dailies

![dailies banner](https://github.com/yasyf/dailies/raw/main/docs/assets/readme-banner.webp)

[![PyPI](https://img.shields.io/pypi/v/dly.svg)](https://pypi.org/project/dly/)
[![Python](https://img.shields.io/pypi/pyversions/dly.svg)](https://pypi.org/project/dly/)
[![License: PolyForm-NC-1.0.0](https://img.shields.io/badge/License-PolyForm--NC--1.0.0-blue.svg)](https://github.com/yasyf/dailies/blob/main/LICENSE)

Daily automation and scheduled task runner — your recurring jobs on one schedule.

`dailies` runs the recurring jobs that make up your day — backups, digests,
syncs, reports — on a single schedule you define once. It's a thin, scriptable
layer over cron: every task is plain Python, runs are idempotent per date, and
re-running a missed day picks up exactly where the schedule left off. One
version-controlled schedule replaces a tangle of crontab lines, and the `dly run`
you schedule is the one you debug by hand.

## Install

```bash
uvx dly --help
```

## Quickstart

`dailies` stores its tasks, workflows, and runs in MongoDB; per-workflow and
per-task state lives in local SQLite databases under `DAILIES_STATE_DIR`. Point
it at a database, then drive it:

```bash
$ uvx dly db init
connected to mongodb://localhost:27017/dailies — indexes created
```

Then schedule `dly tick` from system cron or launchd on a ~1-minute cadence; it
sweeps cron-due workflows and polls subscriptions. Overlapping ticks are safe —
even across machines sharing one MongoDB — so you never need to guard the entry.

## Commands

| Command | Purpose |
| --- | --- |
| `dly db init` | Connect to MongoDB and create indexes |
| `dly run <workflow-id>` | Fire a single manual run of a workflow now |
| `dly tick` | Sweep cron-due workflows and poll subscriptions; safe to overlap |
| `dly tui` | Browse tasks → workflows → runs and current state |
| `dly auth <name>` | Connect a per-integration credential (e.g. `gmail`, `onepassword`, `bluebubbles`) |
| `dly browser import-cookies <id>` | Seed a workflow's browser profile with existing logins |
| `dly interview` | Run the onboarding interview to define workflows |

Run `dly --help` or any subcommand with `--help` for the full flag list.

## Configuration

`dly` reads its MongoDB connection and agent credentials from the environment (no
auto-loading — a missing required variable fails loudly):

| Variable | Example | Purpose |
| --- | --- | --- |
| `MONGODB_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGODB_DB` | `dailies` | Database name |
| `DAILIES_STATE_DIR` | `scratch/state` | Directory holding the per-workflow and per-task SQLite state databases |
| `ANTHROPIC_API_KEY` | `sk-ant-…` | Anthropic API key — required to run workflows and the onboarding interview (`dly run`, `dly tick`, `dly interview`, `dly tui`) |
| `EXA_API_KEY` | `exa-…` | [Exa](https://exa.ai) API key — backs the agent's `search_web` tool; the tool fails per call without it |

Per-integration credentials are not environment variables. Connect each one with
`dly auth <name>` — `dly auth gmail` opens a Nango connect link, while `dly auth
onepassword` and `dly auth bluebubbles` prompt for the 1Password service-account
token and the BlueBubbles URL and password. Each credential is stored in MongoDB,
so `dly auth status` (which reports every integration's readiness) needs
`MONGODB_URI` and `MONGODB_DB` set.

A gitignored `.env` ships with localhost defaults. Load it into your shell before
running:

```bash
# bash / zsh
set -a; source .env; set +a
```

```fish
# fish
for line in (grep -v '^#' .env | grep '=')
    set -gx (string split -m1 '=' -- $line)
end
```

Running a workflow (`dly run`, `dly tick`) or the onboarding interview (`dly
interview`, or the interview launched from inside `dly tui`) drives an agent through
the Claude Agent SDK. The Claude Code CLI it relies on ships bundled with
`claude-agent-sdk` — no separate install — but it runs as a Node.js subprocess, so a
Node.js runtime and a valid `ANTHROPIC_API_KEY` must be present at run time.

### Browser tools

Workflow agents get web access in three tiers. With Claude-in-Chrome enabled (the
one-time `/chrome` setup in `claude`, see the
[Claude in Chrome](https://code.claude.com/docs/en/chrome) docs), agents drive your
real, logged-in Chrome. Without it, agents get a `browse(task)` tool backed by an
autonomous [browser-use](https://browser-use.com) agent — provision its Chromium
once with `uvx browser-use install`, and seed per-workflow logins with `dly browser
import-cookies <workflow-id> --domain example.com` (run `dly browser --help` for the
flags). Always on are `search_web` (Exa), `fetch_url` (plain HTTP), and `scrape` (a
single-page [Stagehand](https://stagehand.dev) extraction; override its Chrome with
`CHROME_PATH`).

## License

[PolyForm Noncommercial 1.0.0](LICENSE).
