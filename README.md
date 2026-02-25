# rename-claude-sessions

Auto-renames Claude Code session files (`~/.claude/projects/**/*.jsonl`) with meaningful titles.

It tries strategies in this order:
1. GitHub URL in early user messages (issue/PR title via `gh`)
2. Issue/PR inferred from branch name
3. PR inferred from branch name
4. LLM fallback (`ollama`, `claude`, or `auto`)

The script appends a `custom-title` JSONL record and restores file `mtime/atime` so session ordering is preserved. It also updates each project’s `sessions-index.json` so Ctrl+R shows the new title without reopening.

## Requirements

- Python 3
- `gh` CLI (for issue/PR lookups); must be authenticated (e.g. `gh auth login`)
- `ollama` (if using Ollama fallback)
- `claude` CLI (if using Claude fallback)

No pip dependencies — stdlib only, plus the tools above.

## Install

Clone the repository:

```bash
git clone git@github.com:jtsternberg/rename-claude-sessions.git
cd rename-claude-sessions
```

Optional: make the script executable:

```bash
chmod +x rename-claude-sessions.py
```

Optional: install as a shell command:

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/rename-claude-sessions.py" ~/.local/bin/rename-claude-sessions
```

Then run it as:

```bash
rename-claude-sessions --dry-run --title-provider ollama --ollama-model qwen2.5-coder:1.5b
```

## Usage

Run against all sessions:

```bash
python3 rename-claude-sessions.py \
  --title-provider ollama \
  --ollama-model qwen2.5-coder:1.5b
```

Dry-run (prints per-file `RENAME:` lines, no writes):

```bash
python3 rename-claude-sessions.py \
  --dry-run \
  --title-provider ollama \
  --ollama-model qwen2.5-coder:1.5b
```

Verbose mode (shows `SKIP` details):

```bash
python3 rename-claude-sessions.py \
  --verbose \
  --title-provider ollama \
  --ollama-model qwen2.5-coder:1.5b
```

Single file:

```bash
python3 rename-claude-sessions.py \
  --file ~/.claude/projects/<project>/<session>.jsonl \
  --title-provider ollama \
  --ollama-model qwen2.5-coder:1.5b
```

Include active sessions (skip idle check):

```bash
python3 rename-claude-sessions.py --force --dry-run
```

Preview then delete empty sessions (no real user messages):

```bash
python3 rename-claude-sessions.py --cleanup --dry-run   # preview
python3 rename-claude-sessions.py --cleanup             # run for real
```

Useful flags:
- `--title-provider ollama|claude|auto`
- `--ollama-model <model>` — must be one shown by `ollama list`
- `--claude-model <model>` — must be one of: `haiku`, `sonnet`, or `opus`
- `--max-age-days <N>` (default: `5`; `0` disables age limit)
- `--force` — include active sessions (skip the 300s idle check)
- `--cleanup` — delete sessions with no real user messages and remove them from the index (use `--dry-run` first to preview)

## Cron setup

Cron uses a minimal environment: `python3` may point to an older system Python (e.g. macOS 3.9) and can cause type-hint or dependency errors. Use the **full path** to the Python you want.

Find it (in a shell where you normally run the script):

```bash
which python3
# e.g. /opt/homebrew/bin/python3 or ~/.pyenv/shims/python3
```

Current recommended cron (every 30 minutes), with explicit Python:

```cron
*/30 * * * * PATH="$HOME/.local/bin:$PATH" /opt/homebrew/bin/python3 "$HOME/Code/rename-claude-sessions/rename-claude-sessions.py" --title-provider ollama --ollama-model qwen2.5-coder:1.5b >> /tmp/rename-claude-sessions.log 2>&1
```

Replace `/opt/homebrew/bin/python3` with the path from `which python3` (e.g. `$HOME/.pyenv/shims/python3` if you use pyenv).

Optional (more portable) setup using variables:

```bash
PYTHON="$(which python3)"
SCRIPT="$HOME/Code/rename-claude-sessions/rename-claude-sessions.py"
(crontab -l 2>/dev/null; echo "*/30 * * * * PATH=\"$HOME/.local/bin:\$PATH\" $PYTHON \"$SCRIPT\" --title-provider ollama --ollama-model qwen2.5-coder:1.5b >> /tmp/rename-claude-sessions.log 2>&1") | crontab -
```

That records the current `python3` path into crontab so the job keeps using that binary.

## Check cron

Show all cron entries:

```bash
crontab -l
```

Tail log output:

```bash
tail -f /tmp/rename-claude-sessions.log
```

## Modify cron

Edit interactively:

```bash
crontab -e
```

Replace the rename line with your new command/schedule, then save.

## Remove cron

Remove only this job:

```bash
crontab -l | grep -v 'rename-claude-sessions.py' | crontab -
```

Remove all user cron jobs (destructive):

```bash
crontab -r
```

---

License: MIT
