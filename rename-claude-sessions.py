#!/usr/bin/env python3
"""
rename-claude-sessions - Auto-rename Claude Code sessions to meaningful
titles based on issue/PR context.

Strategies (in order):
  1. GitHub URL in first message → fetch title via gh
  2. Issue number in branch name → fetch title via gh  (skipped for monorepo roots)
  3. PR lookup for the branch → fetch title via gh     (skipped for monorepo roots)
  4. Skip only when there's truly no clue

Monorepo detection: If the session's cwd is a git repo that contains nested
git repos (up to 2 levels deep), it's treated as a monorepo root. The branch
in a monorepo belongs to the monorepo itself, not to the session's actual work,
so branch-based strategies are skipped.

Uses the same custom-title JSONL record as Ctrl+R rename.

Usage:
    rename-claude-sessions              # run for real
    rename-claude-sessions --dry-run    # preview changes only
    rename-claude-sessions --verbose    # show all decisions

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
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
SKIP_BRANCHES = {"master", "main", "develop", "staging"}
ACTIVE_THRESHOLD_SECONDS = 300
_monorepo_cache: dict[str, bool] = {}

# Regex for GitHub issue/PR URLs
GH_URL_RE = re.compile(
    r"github\.com/([^/\s]+/[^/\s]+)/(issues|pull)/(\d+)"
)

# Known project directory → repo mappings (fallback when cwd is gone)
# Built dynamically from working cwds during the run
_repo_from_project_dir: dict[str, str] = {}


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


def extract_issue_number(branch: str) -> str | None:
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


def extract_github_urls(text: str) -> list[tuple[str, str, str]]:
    """Extract (repo, type, number) tuples from GitHub URLs in text.
    type is 'issues' or 'pull'."""
    if not text:
        return []
    return GH_URL_RE.findall(text)


def run_gh(args: list[str], cwd: str | None = None, timeout: int = 15) -> str | None:
    """Run a gh CLI command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def get_repo_for_cwd(cwd: str, cache: dict) -> str | None:
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


def infer_repo_for_cwd(cwd: str, repo_cache: dict) -> str | None:
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


def get_issue_or_pr_title(number: str, repo: str, cache: dict) -> tuple[str, str] | None:
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


def find_pr_for_branch(branch: str, repo: str, cache: dict) -> tuple[str, str] | None:
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


def read_session_metadata(filepath: Path) -> dict | None:
    """Read sessionId, branch, cwd, first message text, and check for custom-title.
    Collects all user text from the first 50 lines for URL extraction."""
    session_id = branch = cwd = first_text = None
    all_user_texts: list[str] = []
    has_custom_title = False
    try:
        with open(filepath) as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                try:
                    d = json.loads(line)
                    if d.get("type") == "custom-title":
                        has_custom_title = True
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
    }


def set_custom_title(filepath: Path, session_id: str, title: str) -> bool:
    """Append a custom-title record to the session JSONL."""
    record = {
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    }
    try:
        with open(filepath, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError as e:
        print(f"  ERROR writing {filepath}: {e}", file=sys.stderr)
        return False


def resolve_title(meta: dict, repo_cache: dict, title_cache: dict, pr_cache: dict, verbose: bool) -> str | None:
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


def main():
    dry_run = "--dry-run" in sys.argv
    verbose = "--verbose" in sys.argv or dry_run

    if not CLAUDE_PROJECTS_DIR.is_dir():
        print(f"No Claude projects directory at {CLAUDE_PROJECTS_DIR}")
        return

    repo_cache: dict = {}
    title_cache: dict = {}
    pr_cache: dict = {}
    renamed = 0
    skipped_has_title = 0
    skipped_no_match = 0
    skipped_empty = 0

    for project_dir in sorted(CLAUDE_PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        for session_file in sorted(project_dir.glob("*.jsonl")):
            # Skip recently modified files (likely active sessions)
            try:
                mtime = session_file.stat().st_mtime
            except OSError:
                continue
            if time.time() - mtime < ACTIVE_THRESHOLD_SECONDS:
                if verbose:
                    print(f"  SKIP (active): {session_file.name}")
                continue

            meta = read_session_metadata(session_file)
            if not meta:
                skipped_empty += 1
                continue

            # Skip sessions that already have a custom title
            if meta["hasCustomTitle"]:
                skipped_has_title += 1
                continue

            # Skip sessions with no useful metadata at all
            if not meta.get("branch") and not meta.get("firstText"):
                skipped_empty += 1
                continue

            # Detect monorepo roots — branch doesn't reflect session content
            cwd = meta.get("cwd")
            if cwd and is_monorepo_root(cwd):
                meta["is_monorepo"] = True

            new_title = resolve_title(meta, repo_cache, title_cache, pr_cache, verbose)

            if not new_title:
                skipped_no_match += 1
                if verbose:
                    branch = meta.get("branch", "(none)")
                    first = (meta.get("firstText") or "(empty)")[:80]
                    print(f"  SKIP (no match): branch={branch} | msg={first}")
                continue

            if verbose:
                print(f"  RENAME: {new_title}")
                print(f"          branch: {meta.get('branch', '(none)')} | {session_file.name}")

            if not dry_run:
                if set_custom_title(session_file, meta["sessionId"], new_title):
                    renamed += 1
                else:
                    print(f"  FAILED: {session_file.name}", file=sys.stderr)
            else:
                renamed += 1

    action = "Would rename" if dry_run else "Renamed"
    print(f"\n{action}: {renamed} | Already titled: {skipped_has_title} | No match: {skipped_no_match} | Empty: {skipped_empty}")


if __name__ == "__main__":
    main()
