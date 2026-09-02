# Changelog

Notable user-facing changes are recorded here. This project follows [Semantic
Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Source timelines now mark changes in date, channel, or conversation.
- The web brief highlights rows created by the latest dream pass in green and rows
  edited by it in yellow.

### Changed

- Temporal benchmarks no longer mutate a tracked score-history ledger during normal runs.
- Event detail describes state in plain English.
- Relevant open questions are reviewed beside their conversations and can be kept,
  amended with a wait condition, answered, or closed through cited, version-checked
  actions; deferred questions retain their wording history.
- Brief, detail, and web views share concise state and change labels, with diagnostics
  kept off the main overview.
- Dream's cross-conversation stage is now named Merge; old recorded `resolve` stages
  still display with the same label.
- Standing is no longer offered to dream or Hermes as a general memory store.
- Legacy standing rows remain readable, but new writes are rejected and normal prompts,
  briefs, and command listings use typed identity, wiki, event, to-do, and question state.
  Retired rows keep their old `S` handles, evidence, and explicit typed destination so
  migration can be safely repeated.
- Dream and live-write instructions, comments, and docstrings are shorter and focused
  on current behavior.
- The GitHub bug-report form now uses a conventional open-source layout.

### Fixed

- Removed phone numbers and private quoted prose from comments and docstrings, with a
  regression check to keep them out.
- Signing out of WhatsApp and into another account no longer lets reused local message
  IDs collide with or hide the earlier account's archive.

## [0.6.0] - 2026-08-14

- Dates stop being guessed and reminders stop needing to be requested: stated dates win,
  and obligations involving another person can schedule themselves.

## [0.5.0] - 2026-08-13

- Collection health is measured from stored data, and reminders reach connected agent
  and device surfaces.

## [0.4.0] - 2026-08-13

- Fixed deleted-source alarms, invalid link locations, series storage, and withheld
  fields being treated as values.

## [0.3.0] - 2026-08-06

- Deterministic matching merges multiple wordings of the same occasion.

## [0.2.0] - 2026-08-05

- Added persistent benchmark scoring and separated developer documentation.

## [0.1.0] - 2026-07-30

- Initial release of the calendar, to-do, source-ingestion, and agent-context core.
