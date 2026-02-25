# Changelog

All notable changes since the first version (credit: @fernandoduro).

## [Unreleased]

### Added

- **LLM fallback (strategy 4)** — When no GitHub URL, branch, or PR yields a title, the script can generate one via an LLM. Supports:
  - `--title-provider ollama` (default) — use `ollama run <model>` with `--ollama-model` (default: `qwen2.5-coder:1.5b`)
  - `--title-provider claude` — use Claude CLI with `--claude-model` (default: `claude-3-5-haiku-latest`)
  - `--title-provider auto` — try Ollama first, then Claude
- **mtime/atime preservation** — When appending the `custom-title` JSONL record, the script restores the file’s mtime/atime so session order in the UI is unchanged.
- **Single-file mode** — `--file PATH` processes one session file; `--force-title "Title"` forces that title (useful for testing mtime preservation).
- **Age filter** — `--max-age-days N` (default: 5) skips sessions older than N days; use `0` to disable. Summary line reports “Too old” count.
- **README** — Install, requirements, usage, and option reference.
- **sessions-index.json** (credit: @fernandoduro) — When setting a title, the script updates the project’s `sessions-index.json` so Ctrl+R shows the new title without reopening. If a session already has a custom title but the index doesn’t, the script syncs the index.
- **--force** (credit: @fernandoduro) — Include active sessions (skip the 300s idle check).
- **--cleanup** (credit: @fernandoduro) — Delete sessions with no real user messages (only IDE tags, etc.) and remove them from the index. Summary line reports “Deleted” / “Would delete” when used.

### Fixed

- **Python 3.9 compatibility** — Replaced PEP 604/585 type hints (`str | None`, `list[...]`) with `typing` equivalents (`Optional[str]`, `List[...]`) so the script runs on Python < 3.10 (fixes `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'` in cron when system `python3` is older).

### Changed

- **README: cron** — Document using the full path to `python3` in the cron job so the correct Python version is used; optional one-liner records `$(which python3)` into crontab.
- LLM title prompt now suggests “5–15 words” instead of “3–8 words”.
- `--verbose` is no longer implied by `--dry-run`; dry-run still prints each “RENAME” line.
- Internal refactor: shared `_title_prompt_from_meta()` and `_clean_model_title()`; single `process()` used for both batch and `--file` runs.
- `from __future__ import annotations` for forward references.
