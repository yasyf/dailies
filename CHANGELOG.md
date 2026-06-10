# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial scaffolding.

### Changed
- Per-workflow and per-task state moved from MongoDB documents to real SQLite
  databases under `DAILIES_STATE_DIR`: the interview's DDL is executed at persist
  time and agents read/write state through SQL tools (`query_state`,
  `execute_state`, `describe_state`).

[Unreleased]: https://github.com/yasyf/dailies/commits/main
