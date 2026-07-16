# dailies Development Guide

Daily automation and scheduled task runner. Published to PyPI as `dly`; the CLI is `dly`, run as `uvx dly`.

## Repository Structure

```
dailies/
├── dailies/          # The package
│   ├── cli.py        # Click entry point — the `dly` command and subcommands
│   └── __main__.py   # `python -m dailies` shim
├── tests/            # Pytest suite
├── .github/          # CI and PyPI release workflows
├── AGENTS.md         # This file — shared conventions
└── README.md         # Project overview
```
