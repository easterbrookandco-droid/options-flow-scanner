# -*- coding: utf-8 -*-
"""
session_log.py

AI-powered session summarizer. Run at the end of every session
(claude.ai or Claude Code) to update PROJECT_STATE.md and SESSION_HISTORY.md.

Usage:
    python session_log.py                    # Interactive mode

The script:
1. Reads the git diff to see what files changed
2. Accepts your bullet-point notes (optional)
3. Calls Claude API to generate a structured summary
4. Writes FULL entry to SESSION_HISTORY.md (permanent record)
5. Writes CONDENSED 4-5 line summary to PROJECT_STATE.md (last 3 sessions only)
6. Updates NEXT SESSION AGENDA in PROJECT_STATE.md
7. Commits and pushes both files to GitHub
"""

import subprocess
import sys
import os
import re
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
STATE_FILE          = "PROJECT_STATE.md"
HISTORY_FILE        = "SESSION_HISTORY.md"
MARKET_TIMEZONE     = "US/Eastern"
MAX_SESSIONS_IN_STATE = 3


def get_git_diff():
    try:
        r1 = subprocess.run(["git", "diff", "--name-only", "HEAD"], capture_output=True, text=True)
        changed = r1.stdout.strip().split('\n') if r1.stdout.strip() else []
        r2 = subprocess.run(["git", "status", "--short"], capture_output=True, text=True)
        r3 = subprocess.run(["git", "log", "--oneline", "-5"], capture_output=True, text=True)
        return {
            "changed_files": [f for f in changed if f],
            "status": r2.stdout.strip(),
            "recent_commits": r3.stdout.strip()
        }
    except Exception as e:
        return {"changed_files": [], "status": "", "recent_commits": "", "error": str(e)}


def read_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return ""


def write_file(path, content):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def call_claude(prompt, max_tokens=2000):
    import requests
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers=headers, json=body, timeout=30)
        if r.status_code == 200:
            return r.json()["content"][0]["text"]
        print(f"  API error: {r.status_code}")
        return None
    except Exception as e:
        print(f"  API call failed: {e}")
        return None


def generate_full_summary(tool, notes, git_info, duration):
    prompt = f"""Summarize this Options Flow Scanner development session for the permanent history log.

Tool: {tool} | Duration: {duration}
Changed files: {', '.join(git_info['changed_files']) if git_info['changed_files'] else 'none'}
Recent commits: {git_info['recent_commits']}
Developer notes:
{notes if notes else 'No notes — infer from git changes'}

Return ONLY this structure (no preamble):

**What changed:**
- [concrete changes made]

**Key decisions:**
- [decisions and rationale]

**What we learned:**
- [insights or failed approaches]

**Open questions:**
- [unresolved questions to monitor]

One line per bullet. Be specific and technical."""
    return call_claude(prompt, 1500)


def generate_condensed(full_summary, tool, duration):
    prompt = f"""Condense this session summary into exactly 4-5 bullet points.
Each bullet must be under 12 words. Most important changes and decisions only.
Tool: {tool} | Duration: {duration}

{full_summary}

Return ONLY bullet points, no headers:
- [point]
- [point]
- [point]
- [point]
- [point if needed]"""
    return call_claude(prompt, 300)


def update_history(date_str, time_str, tool, duration, full_summary):
    entry = f"""
### {date_str} | {tool} | {time_str} | {duration}
{full_summary}

---"""
    current = read_file(HISTORY_FILE)
    if not current:
        current = """# OPTIONS FLOW SCANNER — SESSION HISTORY
*Full session log. Auto-updated by session_log.py.*
*For recent sessions see PROJECT_STATE.md*

---

## 📝 SESSION LOG
"""
    marker = "## 📝 SESSION LOG"
    if marker in current:
        pos = current.index(marker) + len(marker)
        current = current[:pos] + "\n" + entry + current[pos:]
    else:
        current += f"\n\n{marker}\n{entry}"
    write_file(HISTORY_FILE, current)
    print(f"  ✅ {HISTORY_FILE} updated")


def update_state(date_str, time_str, tool, duration, condensed, next_agenda=None):
    current = read_file(STATE_FILE)
    if not current:
        print(f"  ⚠️  {STATE_FILE} not found")
        return

    # Update last updated date
    current = re.sub(r'\*Last updated: .*?\*', f'*Last updated: {date_str}*', current)

    new_entry = f"""
### {date_str} | {tool} | {time_str}
{condensed}
*→ Full details in SESSION_HISTORY.md*

---"""

    # Rename section header if needed
    old_marker = "## 📝 SESSION LOG"
    new_marker = "## 📝 SESSION LOG (Last 3 Sessions)"
    if old_marker in current and new_marker not in current:
        current = current.replace(old_marker, new_marker, 1)
    if new_marker not in current:
        current += f"\n\n{new_marker}\n"

    # Insert new entry
    pos = current.index(new_marker) + len(new_marker)
    current = current[:pos] + "\n" + new_entry + current[pos:]

    # Trim to last MAX_SESSIONS_IN_STATE entries
    section_start = current.index(new_marker)
    section = current[section_start:]
    headers = [m.start() for m in re.finditer(r'\n### \d{4}-\d{2}-\d{2}', section)]
    if len(headers) > MAX_SESSIONS_IN_STATE:
        cutoff = headers[MAX_SESSIONS_IN_STATE]
        after = section[cutoff:]
        sep = after.find('\n---\n')
        if sep != -1:
            trim_at = section_start + cutoff + sep + 5
            current = current[:trim_at] + "\n*Older sessions in SESSION_HISTORY.md*\n"

    # Update next agenda
    if next_agenda:
        agenda_marker = "## 📋 NEXT SESSION AGENDA"
        new_agenda_block = f"{agenda_marker}\n\n{next_agenda}\n\n---"
        if agenda_marker in current:
            start = current.index(agenda_marker)
            after = current[start + len(agenda_marker):]
            next_sec = re.search(r'\n## ', after[1:])
            if next_sec:
                end = start + len(agenda_marker) + 1 + next_sec.start()
                current = current[:start] + new_agenda_block + "\n\n" + current[end:]
            else:
                current = current[:start] + new_agenda_block
        else:
            current += f"\n\n{new_agenda_block}"

    write_file(STATE_FILE, current)
    print(f"  ✅ {STATE_FILE} updated (last {MAX_SESSIONS_IN_STATE} sessions)")


def commit_and_push():
    try:
        subprocess.run(["git", "add", STATE_FILE, HISTORY_FILE],
                       check=True, capture_output=True)
        r = subprocess.run(
            ["git", "commit", "-m",
             f"Session log [skip ci] — {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            p = subprocess.run(["git", "push"], capture_output=True, text=True)
            if p.returncode == 0:
                print("  ✅ Committed and pushed")
            else:
                print(f"  ⚠️  Push failed — run 'git push' manually")
        else:
            print(f"  ⚠️  Commit issue: {r.stderr[:100]}")
    except Exception as e:
        print(f"  ⚠️  Git error: {e}")


def main():
    eastern = pytz.timezone(MARKET_TIMEZONE)
    now      = datetime.now(eastern)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M %Z")

    print(f"\n{'='*60}")
    print(f"  📝 SESSION LOG — {date_str} {time_str}")
    print(f"{'='*60}\n")

    # Tool
    if "--tool" in sys.argv:
        idx  = sys.argv.index("--tool") + 1
        tool = sys.argv[idx] if idx < len(sys.argv) else "claude.ai"
    else:
        print("  Tool used this session?")
        print("  1. claude.ai (default)  2. Claude Code  3. Both")
        c    = input("  Choice [1]: ").strip() or "1"
        tool = {"1": "claude.ai", "2": "Claude Code", "3": "claude.ai + Claude Code"}.get(c, "claude.ai")

    duration = input("\n  Duration (e.g. '2 hours') [unknown]: ").strip() or "unknown"

    print("\n  Notes — what did you work on? (empty line to finish)\n")
    lines = []
    while True:
        line = input("  > ").strip()
        if not line:
            break
        lines.append(f"- {line}")
    notes = "\n".join(lines)

    print("\n  Next session agenda? (empty line to keep existing)\n")
    agenda_lines = []
    while True:
        line = input("  > ").strip()
        if not line:
            break
        agenda_lines.append(f"{len(agenda_lines)+1}. {line}")
    next_agenda = "\n".join(agenda_lines) if agenda_lines else None

    print("\n  Reading git changes...")
    git_info = get_git_diff()
    if git_info["changed_files"]:
        print(f"  Changed: {', '.join(git_info['changed_files'][:5])}")

    print("\n  Generating summaries...")
    if not ANTHROPIC_API_KEY:
        full_summary = notes or "No summary — API key missing"
        condensed    = notes[:200] if notes else "No summary"
    else:
        full_summary = generate_full_summary(tool, notes, git_info, duration) or notes or "Generation failed"
        condensed    = generate_condensed(full_summary, tool, duration)
        if not condensed:
            bullets   = [l for l in full_summary.split('\n') if l.strip().startswith('-')]
            condensed = '\n'.join(bullets[:4])

    print("\n  Condensed (→ PROJECT_STATE.md):")
    print("  " + "-"*50)
    for line in condensed.split('\n'):
        print(f"  {line}")
    print("  " + "-"*50)

    confirm = input("\n  Write to both files and push? (y/n) [y]: ").strip().lower() or "y"
    if confirm != "y":
        print("  Aborted.")
        return

    update_history(date_str, time_str, tool, duration, full_summary)
    update_state(date_str, time_str, tool, duration, condensed, next_agenda)
    commit_and_push()

    print(f"\n{'='*60}")
    print(f"  ✅ Done — full entry in SESSION_HISTORY.md")
    print(f"            condensed in PROJECT_STATE.md")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()