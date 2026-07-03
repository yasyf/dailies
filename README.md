# ![dailies](https://github.com/yasyf/dailies/raw/main/docs/assets/readme-banner.webp)

**Stop being your own cron job.** dailies turns one interview into a real cron job where an agent uses your Gmail, browser, and 1Password to run the chore daily under a spend cap.

[![CI](https://github.com/yasyf/dailies/actions/workflows/ci.yml/badge.svg)](https://github.com/yasyf/dailies/actions/workflows/ci.yml)
[![License: PolyForm-NC-1.0.0](https://img.shields.io/badge/license-PolyForm--NC--1.0.0-blue.svg)](https://github.com/yasyf/dailies/blob/main/LICENSE)

## Get started

```bash
git clone https://github.com/yasyf/dailies && cd dailies
uv run dly --help
```

<img src="https://github.com/yasyf/dailies/raw/main/docs/assets/demo.png" alt="Terminal running 'uv run dly --help' — the full command surface, from db init and interview to activate, tick, and tui" width="700">

Driving with an agent? Paste this:

```text
Set up dailies from github.com/yasyf/dailies: clone it, and with MONGODB_URI and
ANTHROPIC_API_KEY set, run `uv run dly db init`, then `uv run dly interview` to
describe one recurring chore — start with a daily price watch that alerts only on
a new record low. Activate the task it drafts with `uv run dly activate <task-id>`,
schedule `uv run dly tick` from cron on a ~1-minute cadence, and confirm the first
run in `uv run dly tui`.
```

---

## Use cases

### Chase airline and hotel credits until they pay out

An unclaimed flight credit expires because claiming it means digging up the airline login, filing the form, and babysitting the reply thread:

```bash
uv run dly interview
```

Describe the chore once — "14 days after any flight with no credit posted, file the claim with the airline login in my 1Password vault." The interview drafts the task; `dly activate` refuses to arm it until every prerequisite is met, and lists the exact fix for each (`dly auth gmail`, `dly auth onepassword`). Once live, the daily run files the claim and subscribes to the email thread, so an airline reply wakes the workflow on the next tick instead of waiting on you to notice.

### Watch one exact SKU and get pinged only at a record-low delivered price

Reloading three retailer tabs every morning to re-compute the same delivered total is a chore an agent should own:

```bash
uv run dly tick   # from cron or launchd, ~1-minute cadence
```

Each tick sweeps cron-due workflows. The daily run re-crawls retailers for the exact model number, totals the all-in delivered price — item, tax, shipping — and compares it against the record low kept in the workflow's SQLite state. You get an iMessage only when the record breaks. Overlapping ticks are safe, even across machines sharing one MongoDB.

### Grow a vetted research list with daily deep-dives

The list you actually trust only grows when you burn an evening on forum archaeology:

```bash
uv run dly tui
```

The nightly run hunts for new candidates, digs up the buried forum threads and reviews that tell the real story, and adds only the ones that clear your bar to the list in task state — entries already vetted are skipped, not re-litigated. The TUI walks tasks → workflows → runs, so you watch the list grow a couple of vetted entries a week.

## How it works

The interview turns "watch this price" into a task: one or more workflows with cron or event triggers, plus the state schema they share. Every run drives a Claude Agent SDK agent with tools for Gmail, an authenticated browser, 1Password, iMessage, web search, and SQL over the workflow's own SQLite state. Anything that spends money routes through a single authorization codepath — the per-order and weekly caps you set at `dly activate` — and a denied spend becomes an email asking for your approval, not a purchase.

## Commands

| Command | Purpose |
| --- | --- |
| `dly db init` | Connect to MongoDB and create indexes |
| `dly interview` | Run the onboarding interview to draft a task and its workflows |
| `dly tasks` | List every task with its status, workflow count, and open gaps |
| `dly activate <task-id>` | Activate a drafted task once every prerequisite is met |
| `dly run <workflow-id>` | Fire a single manual run of a workflow now |
| `dly tick` | Sweep cron-due workflows and poll event subscriptions |
| `dly tui` | Browse tasks → workflows → runs and current state |
| `dly auth <name>` | Connect a per-integration credential (`gmail`, `onepassword`, `bluebubbles`) |
| `dly browser import-cookies <id>` | Seed a workflow's browser profile with existing logins |
| `dly profile` | Manage the mined user profile that personalizes workflows |

Run any command with `--help` for its full flag list.

## Configuration

dailies keeps tasks, workflows, and runs in MongoDB; per-workflow and per-task state lives in SQLite databases under `DAILIES_STATE_DIR`. Everything below comes from the environment — nothing auto-loads, and a missing required variable fails loudly:

| Variable | Example | Purpose |
| --- | --- | --- |
| `MONGODB_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGODB_DB` | `dailies` | Database name |
| `DAILIES_STATE_DIR` | `scratch/state` | Directory holding the per-workflow and per-task SQLite state databases |
| `ANTHROPIC_API_KEY` | `sk-ant-…` | Anthropic API key — required to run workflows and the interview (`dly run`, `dly tick`, `dly interview`, `dly tui`) |
| `EXA_API_KEY` | `exa-…` | [Exa](https://exa.ai) API key — backs the agent's `search_web` tool; the tool fails per call without it |

Per-integration credentials aren't environment variables. Connect each one with `dly auth <name>` — `dly auth gmail` opens a Nango connect link, while `dly auth onepassword` and `dly auth bluebubbles` prompt for the 1Password service-account token and the BlueBubbles URL and password. Credentials land in MongoDB, so `dly auth status` (which reports every integration's readiness) needs `MONGODB_URI` and `MONGODB_DB` set.

A gitignored `.env` ships with localhost defaults.

<details>
<summary>Load <code>.env</code> into your shell</summary>

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

</details>

Running a workflow (`dly run`, `dly tick`) or the interview drives the agent through the Claude Agent SDK. The Claude Code CLI it relies on ships bundled with `claude-agent-sdk` — no separate install — but it runs as a Node.js subprocess, so a Node.js runtime and a valid `ANTHROPIC_API_KEY` must be present at run time.

### Browser tools

Workflow agents get web access in three tiers. With Claude-in-Chrome enabled (the one-time `/chrome` setup in `claude`, see the [Claude in Chrome](https://code.claude.com/docs/en/chrome) docs), agents drive your real, logged-in Chrome. Without it, agents get a `browse(task)` tool backed by an autonomous [browser-use](https://browser-use.com) agent — provision its Chromium once with `uvx browser-use install`, and seed per-workflow logins with `dly browser import-cookies <workflow-id> --domain example.com`. Always on are `search_web` (Exa), `fetch_url` (plain HTTP), and `scrape` (a single-page [Stagehand](https://stagehand.dev) extraction; override its Chrome with `CHROME_PATH`).

---

Status: pre-release — the `dly` PyPI release is pending, so run from a clone with `uv run dly` for now.

Licensed under [PolyForm Noncommercial 1.0.0](https://github.com/yasyf/dailies/blob/main/LICENSE).
