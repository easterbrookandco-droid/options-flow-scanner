# -*- coding: utf-8 -*-
"""
session_log.py

AI-powered session summarizer. Run at the end of every session
(claude.ai or Claude Code) to update PROJECT_STATE.md.

Usage:
    python session_log.py                    # Interactive mode
    python session_log.py --auto             # Auto mode (reads git diff)
    python session_log.py --tool "claude.ai" # Specify tool used

The script:
1. Reads the git diff to see what files changed
2. Optionally accepts your bullet-point notes
3. Calls Claude API to generate a structured summary
4. Prepends the summary to the SESSION LOG section of PROJECT_STATE.md
5. Updates the NEXT SESSION AGENDA if you provide one
6. Commits and pushes to GitHub
"""

import subprocess
import sys
import os
import re
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
STATE_FILE = "PROJECT_STATE.md"
MARKET_TIMEZONE = "US/Eastern"


def get_git_diff():
    """Get the git diff of staged and unstaged changes."""
    try:
        # Get list of changed files
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True
        )
        changed_files = result.stdout.strip().split('\n') if result.stdout.strip() else []

        # Also get untracked new files
        result2 = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True
        )
        status = result2.stdout.strip()

        # Get recent commit messages
        result3 = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True
        )
        recent_commits = result3.stdout.strip()

        return {
            "changed_files": [f for f in changed_files if f],
            "status": status,
            "recent_commits": recent_commits
        }
    except Exception as e:
        return {"changed_files": [], "status": "", "recent_commits": "", "error": str(e)}


def get_current_state():
    """Read the current PROJECT_STATE.md."""
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return ""


def call_claude_api(prompt):
    """Call Claude API to generate session summary."""
    import requests

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}]
    }

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=body,
            timeout=30
        )
        if response.status_code == 200:
            return response.json()["content"][0]["text"]
        else:
            print(f"  API error: {response.status_code}")
            return None
    except Exception as e:
        print(f"  API call failed: {e}")
        return None


def generate_summary(tool, notes, git_info, duration_hint):
    """Use Claude to generate a structured session summary."""

    prompt = f"""You are summarizing a development session on the Options Flow Scanner project.
Generate a concise, structured session summary for PROJECT_STATE.md.

Tool used: {tool}
Approximate duration: {duration_hint}

Git changes:
- Changed files: {', '.join(git_info['changed_files']) if git_info['changed_files'] else 'none detected'}
- Recent commits: {git_info['recent_commits']}
- Git status: {git_info['status'][:500] if git_info['status'] else 'clean'}

Developer notes (bullet points they provided):
{notes if notes else 'No notes provided — infer from git changes only'}

Generate ONLY the session log entry in this exact format (no preamble):

**What changed:**
- [bullet list of concrete changes made]

**Key decisions:**
- [bullet list of decisions and their rationale]

**What we learned / what didn't work:**
- [bullet list of insights, failed approaches, or key learnings]

**Open questions:**
- [bullet list of unresolved questions or things to monitor]

Keep each bullet concise (1 line). Be specific and technical. Focus on what matters for the next session."""

    return call_claude_api(prompt)


def update_project_state(tool, summary, next_agenda=None):
    """Update PROJECT_STATE.md with the new session entry."""

    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M %Z")

    # Build the new session entry
    entry = f"""
### {date_str} | {tool} | {time_str}
{summary}

---"""

    # Read current state
    current = get_current_state()
    if not current:
        print(f"  ⚠️  {STATE_FILE} not found — creating minimal version")
        current = f"# OPTIONS FLOW SCANNER — PROJECT STATE\n\n## 📝 SESSION LOG\n"

    # Update last updated date
    current = re.sub(
        r'\*Last updated: .*?\*',
        f'*Last updated: {date_str}*',
        current
    )

    # Insert new session entry after SESSION LOG header
    log_marker = "## 📝 SESSION LOG"
    if log_marker in current:
        insert_pos = current.index(log_marker) + len(log_marker)
        current = current[:insert_pos] + "\n" + entry + current[insert_pos:]
    else:
        current += f"\n\n{log_marker}\n{entry}"

    # Update next session agenda if provided
    if next_agenda:
        agenda_section = f"""## 📋 NEXT SESSION AGENDA

{next_agenda}

---"""
        if "## 📋 NEXT SESSION AGENDA" in current:
            # Replace existing agenda
            current = re.sub(
                r'## 📋 NEXT SESSION AGENDA.*?---',
                agenda_section,
                current,
                flags=re.DOTALL
            )
        else:
            # Add before session log
            current = current.replace(log_marker, agenda_section + "\n\n" + log_marker)

    # Write updated state
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        f.write(current)

    print(f"  ✅ {STATE_FILE} updated")


def commit_and_push():
    """Commit and push the updated PROJECT_STATE.md."""
    try:
        subprocess.run(["git", "add", STATE_FILE], check=True, capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m",
             f"Session log update [skip ci] — {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            push = subprocess.run(
                ["git", "push"],
                capture_output=True, text=True
            )
            if push.returncode == 0:
                print("  ✅ Committed and pushed to GitHub")
            else:
                print(f"  ⚠️  Push failed: {push.stderr[:200]}")
                print("  Run 'git push' manually")
        else:
            print(f"  ⚠️  Commit failed: {result.stderr[:200]}")
    except Exception as e:
        print(f"  ⚠️  Git error: {e}")
        print(f"  Run 'git add {STATE_FILE} && git push' manually")


def main():
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(eastern)

    print(f"\n{'='*60}")
    print(f"  📝 SESSION LOG — {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*60}\n")

    # Get tool used
    if "--tool" in sys.argv:
        tool_idx = sys.argv.index("--tool") + 1
        tool = sys.argv[tool_idx] if tool_idx < len(sys.argv) else "claude.ai"
    else:
        print("  Which tool did you use this session?")
        print("  1. claude.ai (default)")
        print("  2. Claude Code")
        print("  3. Both")
        choice = input("  Choice [1]: ").strip() or "1"
        tool = {"1": "claude.ai", "2": "Claude Code", "3": "claude.ai + Claude Code"}.get(choice, "claude.ai")

    # Get duration
    duration = input("\n  Approximate session duration (e.g. '2 hours', '45 min') [unknown]: ").strip() or "unknown"

    # Get notes
    print("\n  Quick notes (what did you work on / decide)?")
    print("  Enter bullet points, one per line. Empty line when done.")
    print("  (Or just press Enter to auto-summarize from git diff only)\n")

    notes_lines = []
    while True:
        line = input("  > ").strip()
        if not line:
            break
        notes_lines.append(f"- {line}")
    notes = "\n".join(notes_lines)

    # Get next agenda
    print("\n  What's the agenda for next session?")
    print("  Enter items, one per line. Empty line when done.")
    print("  (Or press Enter to keep existing agenda)\n")

    agenda_lines = []
    while True:
        line = input("  > ").strip()
        if not line:
            break
        agenda_lines.append(f"{len(agenda_lines)+1}. {line}")
    next_agenda = "\n".join(agenda_lines) if agenda_lines else None

    # Get git info
    print("\n  Reading git changes...")
    git_info = get_git_diff()
    if git_info["changed_files"]:
        print(f"  Changed files: {', '.join(git_info['changed_files'][:5])}")

    # Generate summary with Claude
    print("\n  Generating session summary...")
    if not ANTHROPIC_API_KEY:
        print("  ⚠️  ANTHROPIC_API_KEY not found — writing manual summary")
        summary = notes if notes else "No summary available — API key missing"
    else:
        summary = generate_summary(tool, notes, git_info, duration)
        if not summary:
            summary = notes if notes else "Summary generation failed"

    print("\n  Generated summary:")
    print("  " + "-"*50)
    for line in summary.split('\n'):
        print(f"  {line}")
    print("  " + "-"*50)

    # Confirm before writing
    confirm = input("\n  Write this to PROJECT_STATE.md and push? (y/n) [y]: ").strip().lower() or "y"
    if confirm != "y":
        print("  Aborted.")
        return

    # Update and push
    update_project_state(tool, summary, next_agenda)
    commit_and_push()

    print(f"\n{'='*60}")
    print(f"  ✅ Session logged successfully")
    print(f"  PROJECT_STATE.md updated and pushed to GitHub")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
