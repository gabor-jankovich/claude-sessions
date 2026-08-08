# claude-sessions

Browse and resume recent Claude Code sessions interactively.

Lists all sessions from `~/.claude/projects/` sorted by date (newest first), launches `fzf` for selection, then resumes the chosen session with `claude --resume <uuid>` in its original working directory.

![screenshot](screenshot.png)

## Keys

| Key | Action |
| --- | --- |
| `enter` | resume the selected session (or start a new one) |
| `ctrl-f` | fork the selected session into a new one |
| `ctrl-l` | label the selected session |
| `ctrl-d` | toggle the this-directory-only filter |
| `ctrl-o` | browse directories for a new session |
| `ctrl-x` | delete the selected session — press once to arm the confirmation, again to delete |

Deleting removes the session's `~/.claude/projects/**/<uuid>.jsonl` transcript and its
leftover `~/.claude/todos/<uuid>*.json` files. Moving the cursor cancels a pending delete.

## Requirements

- [`uv`](https://docs.astral.sh/uv/)
- [`fzf`](https://github.com/junegunn/fzf)
- [`claude`](https://claude.ai/code) CLI

## Install

```fish
uv tool install git+https://github.com/kjozsa/claude-sessions
```

## Usage

```fish
claude-sessions
```

## Local development

```fish
git clone https://github.com/kjozsa/claude-sessions
cd claude-sessions
uv run claude_sessions.py
```
