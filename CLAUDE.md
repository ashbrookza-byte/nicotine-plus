# Nicotine+ — engineering directives

## Fork context

This is a personal fork (`ashbrookza-byte/nicotine-plus`) of the upstream
Nicotine+ project (`nicotine-plus/nicotine-plus`), a Python/GTK graphical
client for the Soulseek peer-to-peer network. Upstream's
[CONTRIBUTING.md](CONTRIBUTING.md) explicitly prohibits AI-generated or
AI-co-authored contributions to the real project. That restriction governs
anything sent upstream, not work on this fork, but it means: **never open a
PR or push a branch against the `nicotine-plus/nicotine-plus` upstream
remote** without the user explicitly asking for it and being aware the
change was AI-assisted. Work against `origin` (the fork) freely.

## Read this first

Read [README.md](README.md) and [doc/DEVELOPING.md](doc/DEVELOPING.md) before
making non-trivial changes — they set out the project's design philosophy
(single language, minimal dependencies, GTK, cross-platform portability) and
what the maintainers will and won't accept. Judge new code against those
constraints, not general best practice: a "convenient" new dependency that
duplicates stdlib functionality is a rejection here even if it's fine
elsewhere.

## Scale effort to the size of the change

What scales is the *ceremony* around a change, not the underlying rules.

- A one-line or single-value fix doesn't need a manual GUI walkthrough — read
  the diff and the surrounding function to confirm correctness.
- A change to `pynicotine/gtkgui/` that alters layout, a widget's behavior, or
  a dialog needs an actual run of the app to verify (see Local testing below).
- A change confined to protocol handling, core logic, or `pynicotine/headless/`
  can usually be verified by reading the diff plus the relevant unit test,
  without launching the GUI.
- Match the end-of-turn summary to the size of the change: one or two
  sentences for a small fix, a fuller explanation only when the change is
  large enough that the user needs it to trust the result.

## Tech stack quick reference

- **Language:** Python only (see [doc/DEVELOPING.md](doc/DEVELOPING.md) for
  the supported version range). No other languages in the codebase by design.
- **GUI:** GTK (via PyGObject), code under `pynicotine/gtkgui/`.
- **Headless/CLI:** `pynicotine/headless/` and `pynicotine/cli.py` for non-GUI
  use.
- **Core:** protocol, networking, and business logic live directly under
  `pynicotine/` (`core.py`, `slskproto.py`, `slskmessages.py`, `search.py`,
  `shares.py`, `downloads.py`, `chatrooms.py`, `privatechat.py`, etc.) —
  these must stay usable by both the GTK and headless front ends. Don't bury
  logic inside `gtkgui/` that other front ends would also need.
- **Plugins:** `pynicotine/plugins/` and `pluginsystem.py`.
- **Tests:** `pynicotine/tests/unit/` and `pynicotine/tests/integration/`,
  run with `pytest`.
- **Linting:** pylint, configured in `pyproject.toml` under `[tool.pylint.*]`.
- **Packaging:** `setup.py`/`setup.cfg`/`pyproject.toml` for the Python
  package; `debian/` for Debian packaging; `build-aux/` for other platform
  build scripts. Touch these only when the task is actually about packaging.

## Local verification

- This is a native GTK desktop app, not a web or mobile app — the browser and
  iOS simulator tools don't apply. For a GUI check, run
  `python3 -m pynicotine` from the repo root (or the `nicotine` launcher
  script) via Bash and describe what you actually observed (exceptions,
  console output). There's no screenshot tooling for a native GTK window in
  this environment, so state what you checked and how, not what it "looks
  like".
- Run relevant tests before considering a change done:
  ```bash
  python3 -m pytest pynicotine/tests/unit
  ```
  Scope to the affected test module when the full suite is slow or unrelated
  to the change (`pytest pynicotine/tests/unit/test_x.py`).
- Run pylint on touched files when the change is more than trivial:
  ```bash
  python3 -m pylint pynicotine/<changed_file>.py
  ```

## Git workflow

- When the user says "push to main" (or similar), they mean push to the
  current working branch on `origin` — not literally `master`. Work happens
  on per-topic branches (e.g. `claude/<topic>`), pushed to `origin` directly;
  don't ask for a separate PR step unless the user wants one.
- Never push or open a PR against the `upstream` remote
  (`nicotine-plus/nicotine-plus`) unless the user explicitly asks for it.
- Commit or push only when the user asks.

## Style

- No comments explaining what code does — well-named identifiers and
  docstrings-where-warranted handle that. Match the project's existing
  comment density; don't strip comments that are already there for a
  non-obvious reason (protocol quirks, platform workarounds).
- No backwards-compatibility shims unless asked.
- No tests or docs files beyond what's explicitly requested, except where
  the project's own conventions require them (e.g. a new protocol message
  handler typically wants a corresponding unit test — check
  `pynicotine/tests/unit/` for the existing pattern before skipping this).
- Match existing patterns in adjacent code before inventing new ones. This
  codebase predates most AI tooling and has its own established idioms —
  defer to them over generic Python style preferences.
- Follow the pylint configuration in `pyproject.toml` rather than introducing
  a different lint standard.
- **Never use em-dashes.** Not in code comments, not in commit messages, not
  in replies to the user. This includes the long dash `—` and the en-dash `–`
  used the same way. Recast the sentence instead: a comma, colon, brackets,
  or two sentences will always do the job. A hyphen in a compound word is
  fine, and a genuine numeric range (`4-12 weeks`) is fine with either a
  hyphen or en-dash.
- Respect SPDX license headers already present at the top of files
  (`SPDX-FileCopyrightText`, `SPDX-License-Identifier`) — keep them intact
  when editing, and use the same header format when adding new files.

## Communication

- Short answers: one or two sentences to confirm what changed, unless the
  change is large enough to need more.
- No options, no alternatives, no "here's what I also noticed" unless asked
  or the finding is a genuine correctness/design concern worth flagging.
- When something in scope conflicts with upstream's design philosophy (see
  Read this first), say so before proceeding rather than silently building
  around it.
