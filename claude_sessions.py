#!/usr/bin/env python3
"""Browse and resume Claude Code sessions interactively."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def humanize_prompt(text: str) -> str:
    """Render a slash-command prompt without its XML wrapper tags.

    A first prompt run via a slash command looks like
    "<command-name>/g2</command-name>\\n<command-args>how many...</command-args>".
    Turn that into "/g2 how many...". For anything else, just strip stray tags.
    """
    name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if name:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        parts = [name.group(1).strip()]
        if args and args.group(1).strip():
            parts.append(args.group(1).strip())
        return " ".join(parts)
    # ponytail: generic tag strip for any other wrapped content
    return re.sub(r"<[^>]+>", "", text).strip()


PROJECTS_DIR = Path.home() / ".claude" / "projects"
OMP_SESSIONS_DIR = Path.home() / ".omp" / "agent" / "sessions"
LABELS_FILE = Path.home() / ".claude" / "session-labels.json"
PROMPT_MAX_LEN = 120


def load_labels() -> dict[str, str]:
    """Map of session_id -> user label. Missing/corrupt file = no labels."""
    try:
        return json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_labels(labels: dict[str, str]) -> None:
    LABELS_FILE.write_text(json.dumps(labels, indent=2), encoding="utf-8")


def content_text(content) -> str:
    """Flatten a message's content (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts).strip()
    return ""


def human_project(cwd: str) -> str:
    """~/foo/bar from the real cwd (dir-name decoding mangles dashes)."""
    home = str(Path.home())
    if cwd == home or cwd.startswith(home + "/"):
        return "~" + cwd[len(home):]
    return cwd


def extract_session_info(jsonl_path: Path) -> dict | None:
    """Extract key info from a session JSONL file."""
    session_id = jsonl_path.stem
    first_user_prompt = None
    last_timestamp = None
    cwd = None
    custom_title = None
    ai_title = None
    project_dir = jsonl_path.parent.name  # e.g. -home-kjozsa-workspace-foo

    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = entry.get("type")
                # /rename writes custom-title; Claude auto-generates ai-title.
                # Last occurrence wins (latest rename).
                if etype == "custom-title":
                    if entry.get("customTitle"):
                        custom_title = entry["customTitle"]
                    continue
                if etype == "ai-title":
                    if entry.get("aiTitle"):
                        ai_title = entry["aiTitle"]
                    continue

                if etype != "user":
                    continue

                ts_str = entry.get("timestamp")
                if ts_str:
                    last_timestamp = ts_str

                if cwd is None:
                    cwd = entry.get("cwd", "")

                if first_user_prompt is None:
                    content = entry.get("message", {}).get("content", "")
                    text = humanize_prompt(content_text(content))
                    if text:
                        first_user_prompt = text

    except OSError:
        return None

    if first_user_prompt is None or last_timestamp is None:
        return None

    # Parse timestamp
    try:
        dt = datetime.fromisoformat(last_timestamp.replace("Z", "+00:00"))
        dt_local = dt.astimezone()
    except ValueError:
        return None

    return {
        "tool": "claude",
        "session_id": session_id,
        "timestamp": dt_local,
        "first_prompt": first_user_prompt,
        "title": custom_title or ai_title,
        "cwd": cwd or "",
        "project": human_project(cwd) if cwd else project_dir,
    }


def extract_omp_session_info(jsonl_path: Path) -> dict | None:
    """Extract key info from an oh-my-pi (omp) session JSONL file.

    omp writes a "title" line and a "session" header (id, cwd) up front, so
    we stop at the first user message instead of reading the whole file.
    """
    session_id = cwd = title = prompt = None
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = entry.get("type")
                if etype == "session":
                    session_id, cwd = entry.get("id"), entry.get("cwd", "")
                elif etype in ("title", "title_change") and entry.get("title"):
                    title = entry["title"]
                elif etype == "message" and entry.get("message", {}).get("role") == "user":
                    prompt = humanize_prompt(content_text(entry["message"].get("content")))
                    if prompt:
                        break
        # ponytail: mtime as "last activity", avoids reading 100MB+ of history
        mtime = jsonl_path.stat().st_mtime
    except OSError:
        return None

    if not session_id or not prompt:
        return None
    return {
        "tool": "omp",
        "session_id": session_id,
        "timestamp": datetime.fromtimestamp(mtime).astimezone(),
        "first_prompt": prompt,
        "title": title,
        "cwd": cwd or "",
        "project": human_project(cwd or ""),
    }


def load_all_sessions() -> list[dict]:
    """Load all sessions from all project directories, sorted by timestamp desc."""
    sessions = []

    sources = [
        (PROJECTS_DIR, extract_session_info),
        (OMP_SESSIONS_DIR, extract_omp_session_info),
    ]
    for root, extract in sources:
        for jsonl_file in root.glob("*/*.jsonl"):  # missing root = no matches
            info = extract(jsonl_file)
            if info:
                sessions.append(info)

    sessions.sort(key=lambda s: s["timestamp"], reverse=True)
    return sessions


DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
CLAUDE_ORANGE = "\033[1;38;2;215;119;87m"  # bold mascot color, needs truecolor terminal


def format_for_fzf(sessions: list[dict], labels: dict[str, str]) -> list[str]:
    """Format sessions as lines for fzf input.

    Each line is "<session_id>\t<display>"; the session_id field is hidden
    from view/search (fzf --with-nth=2..) and used to map the chosen line
    back to its session, since --ansi strips color codes from output.

    A user label (set via ctrl-l, stored in LABELS_FILE) is shown up front
    and lives in the searchable field so you can find a session by it.
    """
    lines = []
    for s in sessions:
        dt_str = s["timestamp"].strftime("%Y-%m-%d %H:%M")
        prompt = s["first_prompt"].replace("\n", " ")
        if len(prompt) > PROMPT_MAX_LEN:
            prompt = prompt[:PROMPT_MAX_LEN] + "…"
        project = s["project"]
        title = s.get("title")
        if title:
            title = title.replace("\n", " ")
            display = f"{title}  {DIM}— {prompt}{RESET}"
        else:
            display = prompt
        label = labels.get(s["session_id"])
        tag = f"{CYAN}#{label}{RESET}  " if label else ""
        # padded to equal width so the [project] column lines up
        tool = f"{MAGENTA}omp{RESET}" if s["tool"] == "omp" else f"{CLAUDE_ORANGE}cc {RESET}"
        line = f"{s['session_id']}\t{tag}{dt_str}  {tool} [{project}]  {display}"
        lines.append(line)
    return lines


FORK_KEY = "ctrl-f"
LABEL_KEY = "ctrl-l"
DIR_KEY = "ctrl-d"


def pick_with_fzf(
    sessions: list[dict], labels: dict[str, str], cwd_only: bool
) -> tuple[dict, str] | None:
    """Launch fzf and return (chosen session, pressed_key).

    pressed_key is "" for Enter (resume), FORK_KEY for fork, LABEL_KEY for
    relabel, DIR_KEY to toggle the this-directory-only filter. Returns None
    if the user cancelled.
    """
    lines = format_for_fzf(sessions, labels)
    fzf_input = "\n".join(lines).encode()

    scope = "this dir" if cwd_only else "all dirs"
    result = subprocess.run(
        [
            "fzf",
            "--ansi",
            "--exact",
            "--no-sort",
            "--delimiter=\t",
            "--with-nth=2..",
            f"--prompt=Resume ({scope})> ",
            "--header=enter: resume  ·  ctrl-f: fork  ·  ctrl-l: label  ·  ctrl-d: dir filter",
            f"--expect={FORK_KEY},{LABEL_KEY},{DIR_KEY}",
            "--height=40%",
            "--layout=reverse",
            "--info=inline",
            "--preview-window=down:3:wrap",
            "--preview",
            "echo {2..}",
        ],
        input=fzf_input,
        # fzf draws its picker UI to stderr (esp. in --height mode); capture
        # only stdout (the chosen line) so the UI still reaches the terminal.
        stdout=subprocess.PIPE,
    )

    if result.returncode != 0:
        return None  # user cancelled

    # With --expect, the first output line is the pressed key (empty for
    # Enter); the second is the chosen line.
    out_lines = result.stdout.decode().split("\n")
    if len(out_lines) < 2:
        return None
    pressed_key = out_lines[0].strip()
    chosen_line = out_lines[1].strip()
    if not chosen_line:
        return None

    # First field is the hidden session_id; map back to the session.
    chosen_id = chosen_line.split("\t", 1)[0]
    for s in sessions:
        if s["session_id"] == chosen_id:
            return s, pressed_key

    return None


def resume_session(session: dict, fork: bool = False) -> None:
    """Invoke claude --resume <session_id> in the session's cwd.

    When fork is True, pass --fork-session so claude creates a new session ID
    and leaves the original conversation untouched.
    """
    session_id = session["session_id"]
    cwd = session["cwd"] or str(Path.home())

    action = "Forking" if fork else "Resuming"
    print(f"{action} session {session_id}")
    print(f"  Project : {session['project']}")
    print(f"  Started : {session['timestamp'].strftime('%Y-%m-%d %H:%M')}")
    print(f"  Prompt  : {session['first_prompt'][:80]}")
    print()

    if session["tool"] == "omp":
        # ponytail: omp has no fork flag; ctrl-f just resumes
        if fork:
            print("omp has no fork option, resuming instead\n")
        cmd = ["omp", "--resume", session_id]
    else:
        cmd = ["claude", "--resume", session_id]
        if fork:
            cmd.append("--fork-session")

    os.chdir(cwd if os.path.isdir(cwd) else Path.home())
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


def prompt_label(session: dict, labels: dict[str, str]) -> None:
    """Ask for a label for the session and persist it. Empty input clears it."""
    sid = session["session_id"]
    current = labels.get(sid, "")
    print(f"\nLabel for: {session['first_prompt'][:80]}")
    new = input(f"Label [{current}] (empty clears): ").strip()
    if new:
        labels[sid] = new
    else:
        labels.pop(sid, None)
    save_labels(labels)


def main() -> None:
    sessions = load_all_sessions()

    if not sessions:
        print("No sessions found.", file=sys.stderr)
        sys.exit(1)

    labels = load_labels()
    start_cwd = os.getcwd()
    cwd_only = False

    while True:
        shown = (
            [s for s in sessions if s["cwd"] == start_cwd] if cwd_only else sessions
        )
        chosen = pick_with_fzf(shown, labels, cwd_only)
        if chosen is None:
            sys.exit(0)

        session, key = chosen
        if key == LABEL_KEY:
            prompt_label(session, labels)
            continue  # reopen picker with the updated label
        if key == DIR_KEY:
            cwd_only = not cwd_only
            continue  # reopen picker with the filter flipped
        resume_session(session, fork=(key == FORK_KEY))


if __name__ == "__main__":
    main()
