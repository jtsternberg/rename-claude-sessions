#!/usr/bin/env python3
from __future__ import annotations

"""
rename-claude-sessions - Auto-rename Claude Code sessions to meaningful
titles based on issue/PR context.

Strategies (in order):
  1. GitHub URL in first message → fetch title via gh
  2. Issue number in branch name → fetch title via gh  (skipped for monorepo roots)
  3. PR lookup for the branch → fetch title via gh     (skipped for monorepo roots)
  4. If still no title: LLM fallback (provider selectable via flags) to generate
     a short title from the first few messages
  5. Skip only when there's truly no clue (or provider CLI unavailable)

Monorepo detection: If the session's cwd is a git repo that contains nested
git repos (up to 2 levels deep), it's treated as a monorepo root. The branch
in a monorepo belongs to the monorepo itself, not to the session's actual work,
so branch-based strategies are skipped.

Uses the same custom-title JSONL record as Ctrl+R rename. When appending the
custom-title line, the script restores the file's mtime/atime so "last modified"
does not change and session order in the UI is preserved.

Usage:
    rename-claude-sessions              # run for real
    rename-claude-sessions --dry-run    # preview changes only
    rename-claude-sessions --verbose    # show all decisions
    rename-claude-sessions --force      # include active sessions (skip idle check)
    rename-claude-sessions --cleanup    # delete sessions with no real user messages
    rename-claude-sessions --max-age-days 5  # skip sessions older than 5 days (default)
    rename-claude-sessions --title-provider ollama --ollama-model qwen2.5-coder:1.5b  # default
    rename-claude-sessions --title-provider gemini  # uses Gemini API (needs GEMINI_API_KEY in .env)
    rename-claude-sessions --title-provider claude --claude-model claude-3-5-haiku-latest
    rename-claude-sessions --file PATH [--force-title "Title"]  # single file; --force-title forces that title
    rename-claude-sessions --session-id UUID --force-title "Title"  # find session by ID across all projects
    rename-claude-sessions --current --force-title "Title"  # rename the most recently modified session

Install as cron (every 30 minutes):
    crontab -e
    */30 * * * * PATH="$HOME/.local/bin:$PATH" rename-claude-sessions >> /tmp/rename-claude-sessions.log 2>&1
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
SKIP_BRANCHES = {"master", "main", "develop", "staging"}
ACTIVE_THRESHOLD_SECONDS = 300
DEFAULT_MAX_AGE_DAYS = 5
CLAUDE_EXCERPT_MAX_CHARS = 3000
TITLE_TIMEOUT = 45
DEFAULT_CLAUDE_MODEL = "claude-3-5-haiku-latest"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:1.5b"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# .env file location: next to the script (follows symlinks)
_SCRIPT_DIR = Path(os.path.realpath(__file__)).parent
_monorepo_cache: Dict[str, bool] = {}
# Track consecutive Gemini failures to bail early on rate limits
_gemini_consecutive_failures = 0
GEMINI_MAX_CONSECUTIVE_FAILURES = 3
# Track tool errors separately from "no match"
_tool_errors: Dict[str, int] = {}


def _log_tool_error(tool: str, error: str) -> None:
    """Always log tool errors (not gated on --verbose) and count them."""
    _tool_errors[tool] = _tool_errors.get(tool, 0) + 1
    # Only print the first occurrence per tool to avoid log spam
    if _tool_errors[tool] == 1:
        print(f"  WARNING: {tool}: {error}", file=sys.stderr)


def check_tools_in_path(title_provider: str, verbose: bool) -> None:
    """Verify required CLI tools are available before processing sessions."""
    import shutil
    missing = []
    if not shutil.which("gh"):
        missing.append("gh (GitHub CLI) — needed for issue/PR/branch title strategies")
    if title_provider in ("ollama", "auto") and not shutil.which("ollama"):
        missing.append("ollama — needed for LLM title generation")
    if title_provider in ("claude", "auto") and not shutil.which("claude"):
        missing.append("claude — needed for LLM title generation")
    if missing:
        print("WARNING: tools not found in PATH:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print(f"  PATH={os.environ.get('PATH', '(unset)')}", file=sys.stderr)

# Regex for GitHub issue/PR URLs
GH_URL_RE = re.compile(
    r"github\.com/([^/\s]+/[^/\s]+)/(issues|pull)/(\d+)"
)

# Known project directory → repo mappings (fallback when cwd is gone)
# Built dynamically from working cwds during the run
_repo_from_project_dir: Dict[str, str] = {}


def is_monorepo_root(cwd: str) -> bool:
    """Detect if a cwd is a monorepo root (a git repo containing other git repos).
    Sessions from monorepo roots use branches that don't reflect session content,
    so branch-based naming strategies should be skipped."""
    if cwd in _monorepo_cache:
        return _monorepo_cache[cwd]
    if not os.path.isdir(cwd) or not os.path.exists(os.path.join(cwd, ".git")):
        _monorepo_cache[cwd] = False
        return False
    # Check up to 2 levels deep for nested git repos
    try:
        for child in os.listdir(cwd):
            child_path = os.path.join(cwd, child)
            if not os.path.isdir(child_path) or child.startswith("."):
                continue
            # Level 1: direct child with .git
            if os.path.exists(os.path.join(child_path, ".git")):
                _monorepo_cache[cwd] = True
                return True
            # Level 2: grandchild with .git
            try:
                for grandchild in os.listdir(child_path):
                    gc_path = os.path.join(child_path, grandchild)
                    if os.path.isdir(gc_path) and os.path.exists(os.path.join(gc_path, ".git")):
                        _monorepo_cache[cwd] = True
                        return True
            except OSError:
                continue
    except OSError:
        pass
    _monorepo_cache[cwd] = False
    return False


def extract_issue_number(branch: str) -> Optional[str]:
    """Extract issue/PR number from branch name like fd/feat/554-description."""
    if not branch or branch in SKIP_BRANCHES:
        return None
    match = re.search(r"/(\d+)[-_]", branch)
    if match:
        return match.group(1)
    match = re.search(r"/(\d+)$", branch)
    if match:
        return match.group(1)
    return None


def extract_github_urls(text: str) -> List[Tuple[str, str, str]]:
    """Extract (repo, type, number) tuples from GitHub URLs in text.
    type is 'issues' or 'pull'."""
    if not text:
        return []
    return GH_URL_RE.findall(text)


def run_gh(args: List[str], cwd: Optional[str] = None, timeout: int = 15) -> Optional[str]:
    """Run a gh CLI command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        _log_tool_error("gh", "not found in PATH")
    except subprocess.TimeoutExpired:
        _log_tool_error("gh", f"timed out: gh {' '.join(args[:3])}")
    except OSError as e:
        _log_tool_error("gh", str(e))
    return None


def get_repo_for_cwd(cwd: str, cache: dict) -> Optional[str]:
    """Get GitHub repo (owner/name) for a working directory."""
    if cwd in cache:
        return cache[cwd]
    if not os.path.isdir(cwd):
        cache[cwd] = None
        return None
    repo = run_gh(
        ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=cwd,
    )
    cache[cwd] = repo
    return repo


def infer_repo_for_cwd(cwd: str, repo_cache: dict) -> Optional[str]:
    """Try to infer the repo when cwd doesn't exist anymore.
    Walks up parent directories looking for an existing git repo."""
    if cwd in repo_cache:
        return repo_cache[cwd]

    # Try parent directories (e.g., worktree sibling → actual repo)
    parent = os.path.dirname(cwd)
    attempts = 0
    while parent and parent != "/" and attempts < 5:
        if os.path.isdir(parent):
            # Check if this dir itself is a git repo
            if os.path.isdir(os.path.join(parent, ".git")):
                repo = get_repo_for_cwd(parent, repo_cache)
                if repo:
                    repo_cache[cwd] = repo
                    return repo
            # Check sibling directories that are git repos
            try:
                for sibling in os.listdir(parent):
                    sibling_path = os.path.join(parent, sibling)
                    if os.path.isdir(os.path.join(sibling_path, ".git")):
                        repo = get_repo_for_cwd(sibling_path, repo_cache)
                        if repo:
                            repo_cache[cwd] = repo
                            return repo
            except OSError:
                pass
            break  # Found an existing parent, stop walking
        parent = os.path.dirname(parent)
        attempts += 1

    repo_cache[cwd] = None
    return None


def get_issue_or_pr_title(number: str, repo: str, cache: dict) -> Optional[Tuple[str, str]]:
    """Fetch issue/PR title. Returns (type, title) or None."""
    cache_key = f"{repo}:{number}"
    if cache_key in cache:
        return cache[cache_key]

    title = run_gh(["issue", "view", number, "--repo", repo, "--json", "title", "-q", ".title"])
    if title:
        val = ("issue", title)
        cache[cache_key] = val
        return val

    title = run_gh(["pr", "view", number, "--repo", repo, "--json", "title", "-q", ".title"])
    if title:
        val = ("pr", title)
        cache[cache_key] = val
        return val

    cache[cache_key] = None
    return None


def find_pr_for_branch(branch: str, repo: str, cache: dict) -> Optional[Tuple[str, str]]:
    """Find a PR for a branch. Returns (number, title) or None."""
    cache_key = f"{repo}:branch:{branch}"
    if cache_key in cache:
        return cache[cache_key]
    output = run_gh([
        "pr", "list", "--repo", repo, "--head", branch,
        "--state", "all", "--json", "number,title", "--limit", "1",
    ])
    if output:
        try:
            prs = json.loads(output)
            if prs:
                val = (str(prs[0]["number"]), prs[0]["title"])
                cache[cache_key] = val
                return val
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
    cache[cache_key] = None
    return None


def read_session_metadata(filepath: Path) -> Optional[dict]:
    """Read sessionId, branch, cwd, first message text, and check for custom-title.
    Collects all user text from the first 50 lines for URL extraction."""
    session_id = branch = cwd = first_text = None
    all_user_texts: List[str] = []
    has_custom_title = False
    custom_title_value: Optional[str] = None
    try:
        with open(filepath) as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                try:
                    d = json.loads(line)
                    if d.get("type") == "custom-title":
                        has_custom_title = True
                        custom_title_value = d.get("customTitle")
                        break
                    if not session_id and d.get("sessionId"):
                        session_id = d["sessionId"]
                    if not branch and d.get("gitBranch"):
                        branch = d["gitBranch"]
                    if not cwd and d.get("cwd"):
                        cwd = d["cwd"]
                    if d.get("type") == "user":
                        msg = d.get("message", {})
                        if isinstance(msg, dict):
                            content = msg.get("content", [])
                            # content can be a string (e.g. slash commands) or a list
                            if isinstance(content, str):
                                all_user_texts.append(content)
                                if not first_text and not content.startswith("<ide_opened_file>"):
                                    first_text = content
                            else:
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        text = c["text"]
                                        all_user_texts.append(text)
                                        # First meaningful text (skip IDE tags)
                                        if not first_text and not text.startswith("<ide_opened_file>"):
                                            first_text = text
                        elif isinstance(msg, str):
                            all_user_texts.append(msg)
                            if not first_text:
                                first_text = msg
                except json.JSONDecodeError:
                    continue
        # Also check the end of the file for custom-title (it gets appended)
        if not has_custom_title:
            with open(filepath, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 2048))
                tail = f.read().decode("utf-8", errors="replace")
                if '"custom-title"' in tail:
                    for line in tail.strip().split("\n"):
                        try:
                            d = json.loads(line)
                            if d.get("type") == "custom-title":
                                has_custom_title = True
                                custom_title_value = d.get("customTitle")
                                break
                        except json.JSONDecodeError:
                            continue
    except OSError:
        return None

    if not session_id:
        return None

    return {
        "sessionId": session_id,
        "branch": branch,
        "cwd": cwd,
        "firstText": first_text,
        "allUserTexts": all_user_texts,
        "hasCustomTitle": has_custom_title,
        "customTitleValue": custom_title_value,
    }


def is_empty_session(filepath: Path) -> bool:
    """True if the session has no real user messages (only IDE tags, system messages, etc.)."""
    try:
        with open(filepath) as f:
            for i, line in enumerate(f):
                if i > 30:
                    return False
                try:
                    d = json.loads(line)
                    if d.get("type") == "user":
                        msg = d.get("message", {})
                        if isinstance(msg, str) and msg.strip():
                            return False
                        if isinstance(msg, dict):
                            content = msg.get("content", [])
                            if isinstance(content, str) and content.strip():
                                return False
                            if isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        text = c.get("text", "")
                                        if text and not text.startswith("<ide_opened_file>") and not text.startswith("<ide_selection>"):
                                            return False
                    elif d.get("type") == "assistant":
                        msg = d.get("message", {})
                        if isinstance(msg, dict):
                            content = msg.get("content", [])
                            if isinstance(content, list) and len(content) > 0:
                                return False
                except json.JSONDecodeError:
                    continue
    except OSError:
        return False
    return True


def load_sessions_index(project_dir: Path) -> Optional[dict]:
    """Load sessions-index.json for a project directory."""
    index_path = project_dir / "sessions-index.json"
    if not index_path.exists():
        return None
    try:
        with open(index_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_sessions_index(project_dir: Path, index_data: dict) -> bool:
    """Write sessions-index.json back to disk."""
    index_path = project_dir / "sessions-index.json"
    try:
        with open(index_path, "w") as f:
            json.dump(index_data, f, ensure_ascii=False)
        return True
    except OSError as e:
        print(f"  ERROR writing {index_path}: {e}", file=sys.stderr)
        return False


def set_custom_title(filepath: Path, session_id: str, title: str, index_data: Optional[dict] = None) -> bool:
    """Append a custom-title record to the session JSONL, preserving mtime/atime.
    If index_data is provided, update the matching entry's customTitle so Ctrl+R shows the title."""
    try:
        st = filepath.stat()
        atime, mtime = st.st_atime, st.st_mtime
    except OSError:
        atime = mtime = None
    record = {
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    }
    try:
        with open(filepath, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if atime is not None and mtime is not None:
            os.utime(filepath, (atime, mtime))
        if index_data and "entries" in index_data:
            found = False
            for entry in index_data["entries"]:
                if entry.get("sessionId") == session_id:
                    entry["customTitle"] = title
                    found = True
                    break
            if not found:
                # Create a minimal index entry so claude --resume shows the title
                index_data["entries"].append({
                    "sessionId": session_id,
                    "fullPath": str(filepath),
                    "customTitle": title,
                })
        return True
    except OSError as e:
        print(f"  ERROR writing {filepath}: {e}", file=sys.stderr)
        return False


def resolve_title(meta: dict, repo_cache: dict, title_cache: dict, pr_cache: dict, verbose: bool) -> Optional[str]:
    """Try all strategies to find a meaningful title for a session."""
    branch = meta.get("branch")
    cwd = meta.get("cwd")
    first_text = meta.get("firstText") or ""
    skip_branch = meta.get("is_monorepo", False)

    # Strategy 1: GitHub URL in any early user message (works even without cwd/branch)
    # Search all user texts from the first 50 lines, not just the first message
    combined_text = "\n".join(meta.get("allUserTexts", [first_text]))
    gh_urls = extract_github_urls(combined_text)
    for repo, url_type, number in gh_urls:
        if url_type == "pull":
            title = run_gh(["pr", "view", number, "--repo", repo, "--json", "title", "-q", ".title"])
            if title:
                return f"PR {number}: {title}"
        else:
            title = run_gh(["issue", "view", number, "--repo", repo, "--json", "title", "-q", ".title"])
            if title:
                return f"#{number}: {title}"

    # For monorepo roots, branch belongs to the monorepo, not the session's topic.
    # Skip branch-based strategies — only GitHub URLs in the message can help.
    if skip_branch:
        if verbose:
            print(f"    (monorepo root — skipping branch-based strategies)")
        return None

    # Need a repo for remaining strategies
    repo = None
    if cwd:
        if os.path.isdir(cwd):
            repo = get_repo_for_cwd(cwd, repo_cache)
        else:
            repo = infer_repo_for_cwd(cwd, repo_cache)

    if not repo and not branch:
        return None

    # Strategy 2: Issue number in branch name
    if branch and repo:
        issue_num = extract_issue_number(branch)
        if issue_num:
            result = get_issue_or_pr_title(issue_num, repo, title_cache)
            if result:
                kind, title = result
                prefix = f"PR {issue_num}" if kind == "pr" else f"#{issue_num}"
                return f"{prefix}: {title}"

    # Strategy 3: Find a PR for the branch
    if branch and branch not in SKIP_BRANCHES and repo:
        pr_info = find_pr_for_branch(branch, repo, pr_cache)
        if pr_info:
            pr_num, pr_title = pr_info
            return f"PR {pr_num}: {pr_title}"

    return None


def _clean_model_title(text: str) -> Optional[str]:
    title = text.strip().split("\n")[0].strip()
    # Drop quotes if the model wrapped the title
    if len(title) >= 2 and title[0] == title[-1] and title[0] in "\"'":
        title = title[1:-1].strip()
    if 2 <= len(title) <= 80:
        return title
    return None


def _title_prompt_from_meta(meta: dict) -> Optional[str]:
    texts = meta.get("allUserTexts") or []
    if not texts:
        return None
    excerpt = "\n".join(texts).strip()
    if not excerpt:
        return None
    if len(excerpt) > CLAUDE_EXCERPT_MAX_CHARS:
        excerpt = excerpt[:CLAUDE_EXCERPT_MAX_CHARS] + "…"

    return (
        "Generate a very short title (5–15 words) for this coding conversation. "
        "Reply with only the title, no quotes, no explanation.\n\nConversation excerpt:\n\n"
    ) + excerpt


def generate_title_via_claude(meta: dict, verbose: bool, model: str) -> Optional[str]:
    """Use claude -p to generate a short title from the first few messages."""
    prompt = _title_prompt_from_meta(meta)
    if not prompt:
        return None

    try:
        result = subprocess.run(
            ["claude", "--model", model, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=TITLE_TIMEOUT,
        )
        if result.returncode != 0 or not result.stdout:
            if verbose and result.stderr:
                print(f"    (claude: {result.stderr.strip()[:200]})")
            return None
        return _clean_model_title(result.stdout)
    except FileNotFoundError:
        _log_tool_error("claude", "not found in PATH")
    except subprocess.TimeoutExpired:
        _log_tool_error("claude", f"timed out after {TITLE_TIMEOUT}s")
    except OSError as e:
        _log_tool_error("claude", str(e))
    return None


def generate_title_via_ollama(meta: dict, verbose: bool, model: str) -> Optional[str]:
    """Use ollama run to generate a short title from the first few messages."""
    prompt = _title_prompt_from_meta(meta)
    if not prompt:
        return None

    try:
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True,
            text=True,
            timeout=TITLE_TIMEOUT,
        )
        if result.returncode != 0 or not result.stdout:
            if verbose and result.stderr:
                print(f"    (ollama: {result.stderr.strip()[:200]})")
            return None
        return _clean_model_title(result.stdout)
    except FileNotFoundError:
        _log_tool_error("ollama", "not found in PATH")
    except subprocess.TimeoutExpired:
        _log_tool_error("ollama", f"timed out after {TITLE_TIMEOUT}s")
    except OSError as e:
        _log_tool_error("ollama", str(e))
    return None


def _load_env_var(name: str) -> Optional[str]:
    """Load a variable from the .env file next to the script, or from the environment."""
    val = os.environ.get(name)
    if val:
        return val
    env_path = _SCRIPT_DIR / ".env"
    if env_path.is_file():
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == name:
                        v = v.strip()
                        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                            v = v[1:-1]
                        return v
        except OSError:
            pass
    return None


def generate_title_via_gemini(meta: dict, verbose: bool, model: str) -> Optional[str]:
    """Use the Gemini REST API to generate a short title from the first few messages."""
    global _gemini_consecutive_failures
    if _gemini_consecutive_failures >= GEMINI_MAX_CONSECUTIVE_FAILURES:
        if verbose:
            print("    (gemini: skipping — too many consecutive failures)")
        return None

    api_key = _load_env_var("GEMINI_API_KEY")
    if not api_key:
        if verbose:
            print("    (gemini: no GEMINI_API_KEY found in .env or environment)")
        return None

    prompt = _title_prompt_from_meta(meta)
    if not prompt:
        return None

    url = GEMINI_API_URL.format(model=model) + f"?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.3},
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TITLE_TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
        # For thinking models (e.g. gemini-2.5-flash), skip thought parts
        parts = body["candidates"][0]["content"]["parts"]
        text = None
        for part in reversed(parts):
            if part.get("thought"):
                continue
            if "text" in part:
                text = part["text"]
                break
        if not text:
            text = parts[-1].get("text", "")
        title = _clean_model_title(text)
        if title:
            _gemini_consecutive_failures = 0
        else:
            _gemini_consecutive_failures += 1
        return title
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError, json.JSONDecodeError, OSError) as e:
        _gemini_consecutive_failures += 1
        if verbose:
            print(f"    (gemini: {e})")
    return None


def main():
    dry_run = "--dry-run" in sys.argv
    verbose = "--verbose" in sys.argv
    force = "--force" in sys.argv
    cleanup = "--cleanup" in sys.argv
    title_provider = "ollama"
    claude_model = DEFAULT_CLAUDE_MODEL
    ollama_model = DEFAULT_OLLAMA_MODEL
    gemini_model = DEFAULT_GEMINI_MODEL
    max_age_days = DEFAULT_MAX_AGE_DAYS
    single_file: Optional[Path] = None
    force_title: Optional[str] = None
    if "--title-provider" in sys.argv:
        i = sys.argv.index("--title-provider")
        if i + 1 < len(sys.argv):
            title_provider = sys.argv[i + 1].lower()
        if title_provider not in {"gemini", "ollama", "claude", "auto"}:
            print("Usage: --title-provider must be one of: gemini, ollama, claude, auto", file=sys.stderr)
            sys.exit(1)
    if "--claude-model" in sys.argv:
        i = sys.argv.index("--claude-model")
        if i + 1 < len(sys.argv):
            claude_model = sys.argv[i + 1]
        if not claude_model:
            print("Usage: --claude-model <model-name>", file=sys.stderr)
            sys.exit(1)
    if "--ollama-model" in sys.argv:
        i = sys.argv.index("--ollama-model")
        if i + 1 < len(sys.argv):
            ollama_model = sys.argv[i + 1]
        if not ollama_model:
            print("Usage: --ollama-model <model-name>", file=sys.stderr)
            sys.exit(1)
    if "--gemini-model" in sys.argv:
        i = sys.argv.index("--gemini-model")
        if i + 1 < len(sys.argv):
            gemini_model = sys.argv[i + 1]
        if not gemini_model:
            print("Usage: --gemini-model <model-name>", file=sys.stderr)
            sys.exit(1)
    if "--max-age-days" in sys.argv:
        i = sys.argv.index("--max-age-days")
        if i + 1 < len(sys.argv):
            try:
                max_age_days = int(sys.argv[i + 1])
            except ValueError:
                max_age_days = -1
        if max_age_days < 0:
            print("Usage: --max-age-days must be an integer >= 0 (0 disables age limit)", file=sys.stderr)
            sys.exit(1)
    if "--file" in sys.argv:
        i = sys.argv.index("--file")
        if i + 1 < len(sys.argv):
            single_file = Path(sys.argv[i + 1]).expanduser().resolve()
        if not single_file or not single_file.is_file():
            print("Usage: rename-claude-sessions --file <path-to-session.jsonl> [--force-title \"Title\"]", file=sys.stderr)
            sys.exit(1)
        if "--force-title" in sys.argv:
            j = sys.argv.index("--force-title")
            if j + 1 < len(sys.argv):
                force_title = sys.argv[j + 1]
            if not force_title:
                print("Usage: rename-claude-sessions --file <path> --force-title \"Your title\"", file=sys.stderr)
                sys.exit(1)
    if "--session-id" in sys.argv and "--current" in sys.argv:
        print("Usage: use only one of --session-id or --current", file=sys.stderr)
        sys.exit(1)
    if "--session-id" in sys.argv:
        i = sys.argv.index("--session-id")
        sid = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        if not sid:
            print("Usage: --session-id <UUID>", file=sys.stderr)
            sys.exit(1)
        # Find the JSONL file matching this session ID
        for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
            candidate = project_dir / f"{sid}.jsonl"
            if candidate.is_file():
                single_file = candidate
                break
        if not single_file:
            print(f"Session {sid} not found in {CLAUDE_PROJECTS_DIR}", file=sys.stderr)
            sys.exit(1)
        if "--force-title" in sys.argv:
            j = sys.argv.index("--force-title")
            if j + 1 < len(sys.argv):
                force_title = sys.argv[j + 1]
    if "--current" in sys.argv:
        # Find the most recently modified JSONL (the active session)
        newest: Optional[Path] = None
        newest_mtime: float = 0.0
        for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            for f in project_dir.glob("*.jsonl"):
                try:
                    mt = f.stat().st_mtime
                    if mt > newest_mtime:
                        newest_mtime = mt
                        newest = f
                except OSError:
                    continue
        if not newest:
            print(f"No session files found in {CLAUDE_PROJECTS_DIR}", file=sys.stderr)
            sys.exit(1)
        single_file = newest
        if "--force-title" in sys.argv:
            j = sys.argv.index("--force-title")
            if j + 1 < len(sys.argv):
                force_title = sys.argv[j + 1]

    if not single_file and not CLAUDE_PROJECTS_DIR.is_dir():
        print(f"No Claude projects directory at {CLAUDE_PROJECTS_DIR}")
        return

    check_tools_in_path(title_provider, verbose)

    repo_cache: dict = {}
    title_cache: dict = {}
    pr_cache: dict = {}
    renamed = 0
    cleaned = 0
    skipped_has_title = 0
    skipped_no_match = 0
    skipped_empty = 0
    skipped_old = 0

    def process(session_file: Path, index_data: Optional[dict] = None, index_modified_ref: Optional[list] = None) -> None:
        nonlocal renamed, skipped_has_title, skipped_no_match, skipped_empty
        meta = read_session_metadata(session_file)
        if not meta:
            skipped_empty += 1
            return

        if not force_title and meta["hasCustomTitle"]:
            if index_data and not dry_run and meta.get("customTitleValue"):
                for entry in index_data.get("entries", []):
                    if entry.get("sessionId") == meta["sessionId"] and "customTitle" not in entry:
                        entry["customTitle"] = meta["customTitleValue"]
                        if index_modified_ref is not None:
                            index_modified_ref[0] = True
                        if verbose:
                            print(f"  SYNC INDEX: {meta['customTitleValue']}")
                        break
            skipped_has_title += 1
            return

        if not force_title and not meta.get("branch") and not meta.get("firstText"):
            skipped_empty += 1
            return

        cwd = meta.get("cwd")
        if cwd and is_monorepo_root(cwd):
            meta["is_monorepo"] = True

        new_title = force_title if force_title else resolve_title(meta, repo_cache, title_cache, pr_cache, verbose)
        if not new_title:
            if title_provider == "gemini":
                new_title = generate_title_via_gemini(meta, verbose, gemini_model)
            elif title_provider == "ollama":
                new_title = generate_title_via_ollama(meta, verbose, ollama_model)
            elif title_provider == "claude":
                new_title = generate_title_via_claude(meta, verbose, claude_model)
            else:  # auto: try gemini → ollama → claude
                new_title = generate_title_via_gemini(meta, verbose, gemini_model)
                if not new_title:
                    new_title = generate_title_via_ollama(meta, verbose, ollama_model)
                if not new_title:
                    new_title = generate_title_via_claude(meta, verbose, claude_model)
        if not new_title:
            skipped_no_match += 1
            if verbose:
                branch = meta.get("branch", "(none)")
                first = (meta.get("firstText") or "(empty)")[:80]
                print(f"  SKIP (no match): branch={branch} | msg={first}")
            return

        if dry_run or verbose:
            first = (meta.get("firstText") or "(empty)")[:80]
            print(f"  RENAME: {new_title}")
            print(f"          was: {first}")
        if verbose:
            print(f"          branch: {meta.get('branch', '(none)')} | {session_file.name}")

        if dry_run:
            renamed += 1
            return

        # Single-file test: show mtime/atime before and after
        if single_file:
            st_before = session_file.stat()
            print(f"  Before: mtime={st_before.st_mtime} atime={st_before.st_atime}")

        if set_custom_title(session_file, meta["sessionId"], new_title, index_data):
            renamed += 1
            if index_modified_ref is not None:
                index_modified_ref[0] = True
            if single_file:
                st_after = session_file.stat()
                print(f"  After:  mtime={st_after.st_mtime} atime={st_after.st_atime}")
                if (st_before.st_mtime, st_before.st_atime) == (st_after.st_mtime, st_after.st_atime):
                    print("  OK: timestamps preserved")
                else:
                    print("  MISMATCH: timestamps changed", file=sys.stderr)
        else:
            print(f"  FAILED: {session_file.name}", file=sys.stderr)

    if single_file:
        project_dir = single_file.parent
        index_data = load_sessions_index(project_dir)
        index_modified_ref: List[bool] = [False]
        process(single_file, index_data, index_modified_ref)
        if index_modified_ref[0] and index_data and not dry_run:
            save_sessions_index(project_dir, index_data)
    else:
        now = time.time()
        max_age_seconds = max_age_days * 86400
        for project_dir in sorted(CLAUDE_PROJECTS_DIR.iterdir()):
            if not project_dir.is_dir():
                continue
            index_data = load_sessions_index(project_dir)
            index_modified = False
            index_modified_ref: List[bool] = [False]
            for session_file in sorted(project_dir.glob("*.jsonl")):
                try:
                    mtime = session_file.stat().st_mtime
                except OSError:
                    continue
                age_seconds = now - mtime
                if not force and age_seconds < ACTIVE_THRESHOLD_SECONDS:
                    if verbose:
                        print(f"  SKIP (active): {session_file.name}")
                    continue
                if max_age_days > 0 and age_seconds > max_age_seconds:
                    skipped_old += 1
                    if verbose:
                        print(f"  SKIP (old): {session_file.name}")
                    continue
                if cleanup and is_empty_session(session_file):
                    if verbose:
                        print(f"  CLEANUP: {session_file.name}")
                    if not dry_run:
                        try:
                            session_file.unlink()
                            if index_data and "entries" in index_data:
                                sid = session_file.stem
                                index_data["entries"] = [
                                    e for e in index_data["entries"]
                                    if e.get("sessionId") != sid
                                ]
                                index_modified = True
                            cleaned += 1
                        except OSError as e:
                            print(f"  ERROR deleting {session_file.name}: {e}", file=sys.stderr)
                    else:
                        cleaned += 1
                    continue
                index_modified_ref[0] = False
                process(session_file, index_data, index_modified_ref)
                if index_modified_ref[0]:
                    index_modified = True
            if index_modified and index_data and not dry_run:
                if not save_sessions_index(project_dir, index_data):
                    print(f"  WARNING: Failed to update sessions-index.json in {project_dir.name}", file=sys.stderr)

    action = "Would rename" if dry_run else "Renamed"
    total_tool_errors = sum(_tool_errors.values())
    parts = [
        f"{action}: {renamed}",
        f"Already titled: {skipped_has_title}",
        f"No match: {skipped_no_match}",
        f"Empty: {skipped_empty}",
        f"Too old: {skipped_old}",
    ]
    if total_tool_errors:
        parts.append(f"Tool errors: {total_tool_errors}")
    if cleanup:
        clean_action = "Would delete" if dry_run else "Deleted"
        parts.append(f"{clean_action}: {cleaned}")
    print(f"\n{' | '.join(parts)}")
    if _tool_errors:
        for tool, count in _tool_errors.items():
            print(f"  WARNING: {tool} failed {count} time(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
