# dailies

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

## Quickstart

Run the tasks scheduled for today:

```bash
$ uvx dly run
No tasks configured for today.
```

Or target a specific date to backfill a missed run:

```bash
$ uvx dly run --date 2026-06-08
No tasks configured for 2026-06-08.
```

## What problems does this solve?

- **Scattered cron entries.** One schedule, defined in code and version-controlled,
  instead of a tangle of crontab lines across machines.
- **Re-running a missed day.** Runs are keyed by date and idempotent, so backfilling
  a skipped day is a single command — no double-sends, no manual cleanup.
- **Opaque failures.** Every task is plain Python with structured logging, so a
  failed run tells you which task broke and why.
- **Local-first testing.** The same `dly run` you schedule is the one you run by
  hand, so there's no separate path to debug.
