#!/usr/bin/env python3
"""Browse and resume Claude Code sessions interactively."""

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROJECTS_DIR = Path.home() / ".claude" / "projects"
PROMPT_MAX_LEN = 120


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
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        text = content.strip()
                    elif isinstance(content, list):
                        # content is a list of blocks
                        parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block.get("text", ""))
                            elif isinstance(block, str):
                                parts.append(block)
                        text = " ".join(parts).strip()
                    else:
                        text = ""

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

    # Human-readable project path: convert -home-kjozsa-foo-bar -> ~/foo/bar
    human_path = project_dir.lstrip("-").replace("-", "/")
    if human_path.startswith("home/"):
        parts = human_path.split("/", 2)
        human_path = "~/" + parts[2] if len(parts) > 2 else "~"

    return {
        "session_id": session_id,
        "timestamp": dt_local,
        "first_prompt": first_user_prompt,
        "title": custom_title or ai_title,
        "cwd": cwd or "",
        "project": human_path,
    }


def load_all_sessions() -> list[dict]:
    """Load all sessions from all project directories, sorted by timestamp desc."""
    sessions = []

    if not PROJECTS_DIR.exists():
        print(f"No projects directory found at {PROJECTS_DIR}", file=sys.stderr)
        sys.exit(1)

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            info = extract_session_info(jsonl_file)
            if info:
                sessions.append(info)

    sessions.sort(key=lambda s: s["timestamp"], reverse=True)
    return sessions


DIM = "\033[2m"
RESET = "\033[0m"


NEW_SESSION_PREFIX = "__NEW__:"


def format_for_fzf(sessions: list[dict]) -> list[str]:
    """Format sessions as lines for fzf input.

    Each line is "<session_id>\t<display>"; the session_id field is hidden
    from view/search (fzf --with-nth/--nth=2..) and used to map the chosen
    line back to its session, since --ansi strips color codes from output.

    A single "+ New session" entry is prefixed with NEW_SESSION_PREFIX
    followed by the cwd to start in (the directory claude-sessions was
    invoked from).
    """
    lines = []

    cwd = os.getcwd()
    lines.append(f"{NEW_SESSION_PREFIX}{cwd}\t+ New session  [{cwd}]")

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
        line = f"{s['session_id']}\t{dt_str}  [{project}]  {display}"
        lines.append(line)
    return lines


FORK_KEY = "ctrl-f"
BROWSE_KEY = "ctrl-o"
BROWSE_SENTINEL = "\x00BROWSE\x00"


def browse_directory(start: str) -> str | None:
    """Interactive directory walker starting at `start`.

    Each step shows the entries of the current directory (with "." to pick
    the current directory and ".." to go up) and a right-side preview of the
    highlighted entry's contents. Returns the chosen directory, or None if
    cancelled.
    """
    current = Path(start)
    while True:
        try:
            subdirs = sorted(
                p.name for p in current.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        except OSError:
            subdirs = []

        entries = ["."]
        if current != current.parent:
            entries.append("..")
        entries.extend(subdirs)

        preview = f"ls -la {shlex.quote(str(current))}/{{}}"

        result = subprocess.run(
            [
                "fzf",
                "--prompt", f"{current}> ",
                "--header", ". : select this directory  ·  enter: open  ·  esc: cancel",
                "--height=60%",
                "--layout=reverse",
                "--preview", preview,
                "--preview-window=right:50%",
            ],
            input="\n".join(entries).encode(),
            capture_output=True,
        )

        if result.returncode != 0:
            return None  # cancelled

        choice = result.stdout.decode().strip()
        if not choice:
            return None

        if choice == ".":
            return str(current)
        elif choice == "..":
            current = current.parent
        else:
            current = current / choice


def pick_with_fzf(sessions: list[dict]) -> tuple[dict, bool] | str | None:
    """Launch fzf and return (chosen session, fork?), a cwd string for a new
    session, or None if the user cancelled.

    Enter resumes the session in place (or starts a new session for "+ New
    session" entries); FORK_KEY forks the selected session into a new one
    (original left untouched); BROWSE_KEY opens a directory browser to pick
    a location for a new session.
    """
    lines = format_for_fzf(sessions)
    fzf_input = "\n".join(lines).encode()

    result = subprocess.run(
        [
            "fzf",
            "--ansi",
            "--exact",
            "--no-sort",
            "--delimiter=\t",
            "--with-nth=2..",
            "--nth=2..",
            "--prompt=Resume session> ",
            "--header=enter: resume/new  ·  ctrl-f: fork  ·  ctrl-o: browse dirs",
            f"--expect={FORK_KEY},{BROWSE_KEY}",
            "--height=40%",
            "--layout=reverse",
            "--info=inline",
            "--preview-window=down:3:wrap",
            "--preview",
            "echo {2..}",
        ],
        input=fzf_input,
        capture_output=True,
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

    if pressed_key == BROWSE_KEY:
        return BROWSE_SENTINEL

    if not chosen_line:
        return None

    fork = pressed_key == FORK_KEY

    # First field is the hidden session_id; map back to the session.
    chosen_id = chosen_line.split("\t", 1)[0]

    if chosen_id.startswith(NEW_SESSION_PREFIX):
        return chosen_id[len(NEW_SESSION_PREFIX):]

    for s in sessions:
        if s["session_id"] == chosen_id:
            return s, fork

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

    cmd = ["claude", "--resume", session_id]
    if fork:
        cmd.append("--fork-session")

    os.chdir(cwd)
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


def start_new_session(cwd: str) -> None:
    """Invoke claude (no args) in the given directory to start a fresh session."""
    print("Starting new session")
    print(f"  Directory: {cwd}")
    print()

    os.chdir(cwd)
    result = subprocess.run(["claude"])
    sys.exit(result.returncode)


def main() -> None:
    sessions = load_all_sessions()

    if not sessions:
        print("No sessions found.", file=sys.stderr)
        sys.exit(1)

    chosen = pick_with_fzf(sessions)
    if chosen is None:
        sys.exit(0)

    if chosen == BROWSE_SENTINEL:
        cwd = browse_directory(os.getcwd())
        if cwd is None:
            sys.exit(0)
        start_new_session(cwd)
        return

    if isinstance(chosen, str):
        start_new_session(chosen)
        return

    session, fork = chosen
    resume_session(session, fork=fork)


if __name__ == "__main__":
    main()
