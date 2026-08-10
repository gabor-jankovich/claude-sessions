#!/usr/bin/env python3
"""Browse and resume Claude Code sessions interactively."""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
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
LABELS_FILE = Path.home() / ".claude" / "session-labels.json"
TODOS_DIR = Path.home() / ".claude" / "todos"
PROMPT_MAX_LEN = 120
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def load_labels() -> dict[str, str]:
    """Map of session_id -> user label. Missing/corrupt file = no labels."""
    try:
        return json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_labels(labels: dict[str, str]) -> None:
    LABELS_FILE.write_text(json.dumps(labels, indent=2), encoding="utf-8")


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

                    text = humanize_prompt(text)
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
CYAN = "\033[36m"


NEW_SESSION_PREFIX = "__NEW__:"


def format_for_fzf(sessions: list[dict], labels: dict[str, str]) -> list[str]:
    """Format sessions as lines for fzf input.

    Each line is "<session_id>\t<display>"; the session_id field is hidden
    from view/search (fzf --with-nth=2..) and used to map the chosen line
    back to its session, since --ansi strips color codes from output.

    A user label (set via ctrl-l, stored in LABELS_FILE) is shown up front
    and lives in the searchable field so you can find a session by it.

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
        label = labels.get(s["session_id"])
        tag = f"{CYAN}#{label}{RESET}  " if label else ""
        line = f"{s['session_id']}\t{tag}{dt_str}  [{project}]  {display}"
        lines.append(line)
    return lines


FORK_KEY = "ctrl-f"
LABEL_KEY = "ctrl-l"
DIR_KEY = "ctrl-d"
BROWSE_KEY = "ctrl-o"
DELETE_KEY = "ctrl-x"
BROWSE_SENTINEL = "\x00BROWSE\x00"
DIR_SENTINEL = "\x00DIR\x00"

HEADER = (
    "enter: resume/new  ·  ctrl-f: fork  ·  ctrl-l: label  "
    "·  ctrl-d: dir filter  ·  ctrl-o: browse dirs  ·  ctrl-x: delete"
)
CONFIRM_HEADER = (
    "⚠  DELETE this session permanently?  press ctrl-x again to confirm  "
    "·  move the cursor or press esc to cancel"
)


def session_file(session_id: str) -> Path | None:
    """Return the JSONL file of `session_id`, or None if not found."""
    if not SESSION_ID_RE.match(session_id):
        return None
    for path in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        return path
    return None


def delete_session(session_id: str) -> None:
    """Delete a session's JSONL transcript and its leftover todo files."""
    path = session_file(session_id)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        return
    if TODOS_DIR.is_dir():
        for todo in TODOS_DIR.glob(f"{session_id}*.json"):
            try:
                todo.unlink()
            except OSError:
                pass


def self_cmd() -> str:
    """Shell command that re-invokes this script (for fzf child processes)."""
    return shlex.join([sys.executable, str(Path(__file__).resolve())])


FILTER_CWD_ENV = "CLAUDE_SESSIONS_FILTER_CWD"


def delete_binds(state_path: str) -> list[str]:
    """fzf --bind args implementing two-step ctrl-x deletion.

    First ctrl-x on a row records its session id in `state_path` and swaps the
    header for a confirmation prompt. A second ctrl-x on the *same* row deletes
    the session and reloads the list. Moving the cursor disarms it.

    The reload repeats the active directory filter so ctrl-d's scope survives
    a delete. The filter travels in FILTER_CWD_ENV rather than on the reload
    command line, because fzf parses `reload(...)` by matching parentheses — a
    project path containing one would truncate the action.

    fzf shell-quotes {1} before the snippet runs, so the id cannot break out of
    the shell. The `case` guards are about fzf's own action parsing: an id with
    a paren or a space in it would corrupt the `execute-silent(...)` string this
    builds, so anything that is not a plain session id is ignored outright.
    """
    state = shlex.quote(state_path)
    me = self_cmd()

    arm = (
        # not a session row, or not a well-formed session id -> do nothing
        f"case {{1}} in {NEW_SESSION_PREFIX}*) exit 0;; esac; "
        f"case {{1}} in *[!A-Za-z0-9_-]*|'') exit 0;; esac; "
        f'if [ "$(cat {state} 2>/dev/null)" = {{1}} ]; then '
        f": > {state}; "
        f'echo "execute-silent({me} --delete {{1}})'
        f"+reload({me} --print-list)"
        f'+change-header({HEADER})"; '
        f"else "
        f"printf %s {{1}} > {state}; "
        f'echo "change-header({CONFIRM_HEADER})"; '
        f"fi"
    )
    disarm = (
        f"if [ -s {state} ]; then "
        f": > {state}; "
        f'echo "change-header({HEADER})"; '
        f"fi"
    )

    # Colon form (`action:command`) — the command runs to the end of the
    # argument, so no paren-balancing rules apply to the shell snippets.
    return [
        "--bind", f"{DELETE_KEY}:transform:{arm}",
        "--bind", f"focus:transform:{disarm}",
    ]


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


def pick_with_fzf(
    sessions: list[dict], labels: dict[str, str], cwd_only: bool
) -> tuple[dict, str] | str | None:
    """Launch fzf and return one of:

    * (chosen session, pressed_key) — "" for Enter (resume), FORK_KEY for
      fork, LABEL_KEY for relabel
    * DIR_SENTINEL — toggle the this-directory-only filter
    * BROWSE_SENTINEL — open the directory browser for a new session
    * a cwd string — start a new session there ("+ New session" row)
    * None — the user cancelled
    """
    lines = format_for_fzf(sessions, labels)
    fzf_input = "\n".join(lines).encode()

    scope = "this dir" if cwd_only else "all dirs"
    fd, state_path = tempfile.mkstemp(prefix="claude-sessions-delete-")
    os.close(fd)
    try:
        result = subprocess.run(
            [
                "fzf",
                "--ansi",
                "--exact",
                "--no-sort",
                "--delimiter=\t",
                "--with-nth=2..",
                f"--prompt=Resume ({scope})> ",
                f"--header={HEADER}",
                f"--expect={FORK_KEY},{LABEL_KEY},{DIR_KEY},{BROWSE_KEY}",
                "--height=40%",
                "--layout=reverse",
                "--info=inline",
                "--preview-window=down:3:wrap",
                "--preview",
                "echo {2..}",
                *delete_binds(state_path),
            ],
            input=fzf_input,
            # The reload child re-reads this instead of taking the filter on
            # its command line, so no path is spliced into an fzf action.
            env={**os.environ, FILTER_CWD_ENV: os.getcwd() if cwd_only else ""},
            # fzf draws its picker UI to stderr (esp. in --height mode);
            # capture only stdout (the chosen line) so the UI still reaches
            # the terminal.
            stdout=subprocess.PIPE,
        )
    finally:
        try:
            os.unlink(state_path)
        except OSError:
            pass

    if result.returncode != 0:
        return None  # user cancelled

    # With --expect, the first output line is the pressed key (empty for
    # Enter); the second is the chosen line.
    out_lines = result.stdout.decode().split("\n")
    if len(out_lines) < 2:
        return None
    pressed_key = out_lines[0].strip()
    chosen_line = out_lines[1].strip()

    # These act on the picker itself, not on the highlighted row.
    if pressed_key == BROWSE_KEY:
        return BROWSE_SENTINEL
    if pressed_key == DIR_KEY:
        return DIR_SENTINEL

    if not chosen_line:
        return None

    # First field is the hidden session_id; map back to the session.
    chosen_id = chosen_line.split("\t", 1)[0]

    if chosen_id.startswith(NEW_SESSION_PREFIX):
        return chosen_id[len(NEW_SESSION_PREFIX):]

    for s in sessions:
        if s["session_id"] == chosen_id:
            return s, pressed_key

    # The list may have been reloaded (after a delete) and now contain a
    # session that appeared since startup — look it up on disk.
    path = session_file(chosen_id)
    if path is not None:
        info = extract_session_info(path)
        if info:
            return info, pressed_key

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


def start_new_session(cwd: str) -> None:
    """Invoke claude (no args) in the given directory to start a fresh session."""
    print("Starting new session")
    print(f"  Directory: {cwd}")
    print()

    os.chdir(cwd)
    result = subprocess.run(["claude"])
    sys.exit(result.returncode)


def main() -> None:
    # Helper modes used by fzf's own child processes (see delete_binds).
    args = sys.argv[1:]
    if args and args[0] == "--print-list":
        listed = load_all_sessions()
        only_cwd = os.environ.get(FILTER_CWD_ENV, "")
        if only_cwd:
            listed = [s for s in listed if s["cwd"] == only_cwd]
        print("\n".join(format_for_fzf(listed, load_labels())))
        return
    if args and args[0] == "--delete":
        if len(args) > 1:
            delete_session(args[1])
        return

    sessions = load_all_sessions()

    if not sessions:
        print("No sessions found.", file=sys.stderr)
        sys.exit(1)

    labels = load_labels()
    start_cwd = os.getcwd()
    cwd_only = False

    while True:
        # Drop sessions deleted with ctrl-x during the previous picker run;
        # one directory scan, no re-parsing of the transcripts.
        alive = {p.stem for p in PROJECTS_DIR.glob("*/*.jsonl")}
        sessions = [s for s in sessions if s["session_id"] in alive]
        shown = (
            [s for s in sessions if s["cwd"] == start_cwd] if cwd_only else sessions
        )
        chosen = pick_with_fzf(shown, labels, cwd_only)
        if chosen is None:
            sys.exit(0)

        if chosen == DIR_SENTINEL:
            cwd_only = not cwd_only
            continue  # reopen picker with the filter flipped

        if chosen == BROWSE_SENTINEL:
            cwd = browse_directory(os.getcwd())
            if cwd is None:
                sys.exit(0)
            start_new_session(cwd)
            return

        if isinstance(chosen, str):  # "+ New session" row: the cwd to start in
            start_new_session(chosen)
            return

        session, key = chosen
        if key == LABEL_KEY:
            prompt_label(session, labels)
            continue  # reopen picker with the updated label
        resume_session(session, fork=(key == FORK_KEY))


if __name__ == "__main__":
    main()
