# dailies

![dailies banner](https://github.com/yasyf/dailies/raw/main/docs/assets/readme-banner.png)

[![PyPI](https://img.shields.io/pypi/v/dly.svg)](https://pypi.org/project/dly/)
[![Python](https://img.shields.io/pypi/pyversions/dly.svg)](https://pypi.org/project/dly/)
[![License: PolyForm-NC-1.0.0](https://img.shields.io/badge/License-PolyForm--NC--1.0.0-blue.svg)](https://github.com/yasyf/dailies/blob/main/LICENSE)

Daily automation and scheduled task runner.

`dailies` runs the recurring jobs that make up your day — backups, digests,
syncs, reports — on a single schedule you define once. It's a thin, scriptable
layer over cron: every task is plain Python, runs are idempotent per date, and
re-running a missed day picks up exactly where the schedule left off.

## Install

No install needed — run everything through [uvx](https://docs.astral.sh/uv/):

```bash
uvx dly --help
```

`uvx` fetches dailies into a throwaway environment and runs it. To add it
to a project instead:

```bash
uv add dly
```

## Commands

`dailies` stores its tasks, workflows, and runs in MongoDB; per-workflow and
per-task state lives in local SQLite databases under `DAILIES_STATE_DIR`. Point
it at a database, then drive it:

```bash
uvx dly db init             # connect to MongoDB and create indexes
uvx dly run <workflow-id>   # fire a single manual run of a workflow now
uvx dly tick                # sweep cron-due workflows and fire each due trigger
uvx dly tui                 # browse tasks → workflows → runs and current state
```

`dly tick` is meant to be driven by a single, non-overlapping scheduler entry
(system cron or launchd).

## Configuration

`dly` reads its MongoDB connection and agent credentials from the environment (no
auto-loading — a missing required variable fails loudly):

| Variable | Example | Purpose |
| --- | --- | --- |
| `MONGODB_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGODB_DB` | `dailies` | Database name |
| `DAILIES_STATE_DIR` | `scratch/state` | Directory holding the per-workflow and per-task SQLite state databases |
| `ANTHROPIC_API_KEY` | `sk-ant-…` | Anthropic API key — required to run workflows and the onboarding interview (`dly run`, `dly tick`, `dly interview`, `dly tui`) |

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
Node.js runtime and a valid `ANTHROPIC_API_KEY` must be present at run time. Importing
the package needs neither.

## What problems does this solve?

- **Scattered cron entries.** One schedule, defined in code and version-controlled,
  instead of a tangle of crontab lines across machines.
- **Re-running a missed day.** Runs are keyed by date and idempotent, so backfilling
  a skipped day is a single command — no double-sends, no manual cleanup.
- **Opaque failures.** Every task is plain Python with structured logging, so a
  failed run tells you which task broke and why.
- **Local-first testing.** The same `dly run` you schedule is the one you run by
  hand, so there's no separate path to debug.
