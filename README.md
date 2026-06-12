# dailies

![dailies banner](https://github.com/yasyf/dailies/raw/main/docs/assets/readme-banner.webp)

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
uvx dly tick                # sweep cron-due workflows and poll subscriptions; safe to overlap
uvx dly tui                 # browse tasks → workflows → runs and current state
```

`dly tick` is meant to be driven by a scheduler entry (system cron or launchd) on
a ~1-minute cadence. Overlapping ticks are safe — even across machines sharing one
MongoDB — because each workflow is processed under a short database lease: a
concurrent tick skips in-flight workflows and processes the rest.

Delivery is at-least-once. A duplicate run happens only when a lease holder dies
or stalls past the lease TTL (~3 minutes — a crash or a long laptop sleep
mid-run), and the re-fired batch may carry a different set of occurrences than
the original if more events arrived in between. Workflow state (SQLite databases,
connections, browser profiles) is host-local, so while tick *dispatch* is
multi-host-safe, a workflow's memory does not follow it across machines — keep
all scheduler entries on one host until remote state storage lands.

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

### Browser tools

Workflow agents get web access in three tiers:

- **Claude-in-Chrome.** When Claude-in-Chrome is enabled — the one-time
  interactive `/chrome` setup in `claude` (see the
  [Claude in Chrome](https://code.claude.com/docs/en/chrome) docs) — workflow
  agents launch with `--chrome` and drive your real, logged-in Chrome through
  the native browser tools. Chrome must be running with the extension connected
  when workflows fire, and chrome runs authenticate via your Claude
  subscription browser login: API-key auth silently disables the chrome bridge,
  so `ANTHROPIC_API_KEY` is blanked for those agent subprocesses.
- **`browse` (browser-use).** Without Chrome, agents instead get a `browse(task)`
  tool that runs an autonomous [browser-use](https://browser-use.com) agent in a
  headless browser. Provision its Chromium once with `uvx browser-use install`.
  Each workflow keeps its own profile — cookies and localStorage persist across
  runs as a Playwright `storage_state` blob saved through the same `StateStorage`
  layer as the SQLite state (`browser/<workflow_id>.json`), so it carries over to
  a remote (Modal) backend unchanged. The backend is chosen by
  `DAILIES_BROWSER_BACKEND` (default `local`; Browserbase and Anchor are the
  planned remote drop-ins, reached over CDP).

  Seed a workflow's profile with existing logins using
  `dly browser import-cookies <workflow-id> --domain example.com` — `--domain` is
  required and repeatable, and matches subdomains. Cookies come from a local
  browser (`--from-browser chrome|firefox|safari|edge|brave|…`, default `chrome`;
  reading Chrome triggers a macOS keychain prompt) or from a previously exported
  file (`--from-file`, either a Playwright `storage_state` JSON or a Netscape
  `cookies.txt`). Imported cookies feed only the `browse` tool — under
  Claude-in-Chrome the agent uses your real Chrome profile instead.
- **`search_web` / `fetch_url` / `scrape` (always on).** Quick Exa search, a plain
  HTTP fetch, and a single-page [Stagehand](https://stagehand.dev) extraction in a
  fresh anonymous headless browser. `scrape` uses your installed Chrome
  (override the binary with `CHROME_PATH`).

## What problems does this solve?

- **Scattered cron entries.** One schedule, defined in code and version-controlled,
  instead of a tangle of crontab lines across machines.
- **Re-running a missed day.** Runs are keyed by date and idempotent, so backfilling
  a skipped day is a single command — no double-sends, no manual cleanup.
- **Opaque failures.** Every task is plain Python with structured logging, so a
  failed run tells you which task broke and why.
- **Local-first testing.** The same `dly run` you schedule is the one you run by
  hand, so there's no separate path to debug.
