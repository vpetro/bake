# bake

Start new work in an isolated git worktree with its own tmux window running
pi, so nothing ever clobbers work in progress in the main checkout. Inspired
by [workmux](https://github.com/raine/workmux), trimmed to pi and this
machine's conventions.

Worktrees live in `<repo>/.local/worktrees/<name>` (the same convention the
pr-review system uses for its review worktrees) and branches default to
`petro/<name>`.

## Commands

- `bake new <name> [--prompt "..." | --prompt-file F] [--model SEL] [--base X] [--branch Y] [--with-changes] [--no-window]`
  Fetch the base branch, create the worktree and branch, run setup from
  `.bake.json`, open a tmux window there running pi. The brief comes from
  `--prompt` (`-p`), from `--prompt-file` (`-P`), or from stdin with `-P -`;
  bake passes it to pi as an `@file` argument together with its reporting
  instructions, so pi reads both at startup. See
  [Worker input and reporting](#worker-input-and-reporting). `--model` picks
  the pi model selector; see [Model selection](#model-selection) for the
  default. With `--no-window`, the printed `next:` command includes the
  model and the files. `--with-changes` moves uncommitted changes (including
  untracked files) from the current checkout into the new worktree via the
  stash; on conflict the stash is preserved. `--no-window` skips tmux, for
  agents or scripts.
- `bake ls` - worktrees with kind (`work` / `review`), dirty flag, window
  presence, and branch.
- `bake open <name>` - switch to the worktree's window, recreating it if
  needed (`pi -c` inside resumes the last session for that directory).
- `bake rm <name> [--force]` - kill the window, remove the worktree and its
  `.local/bake/<name>` input directory; refuses when dirty unless forced.
  Keeps the branch.
- `bake done <name> [--force]` - `rm` plus delete the branch. For after the
  PR merges.

## Worker input and reporting

Bake prepares the worker's input files from your task brief and its standard
reporting instructions, then starts Pi with those files. You do not need to
send a separate reporting prompt through tmux. The control chat registers the
monitor with `bake_monitor`; bake does not create a monitor on its behalf.
Define completion and the required evidence in your task brief. The worker
reports completed, blocked, or failed through `bake_report`.

The launch command is

```
pi --model anthropic/claude-fable-5-1:max @/path/to/brief.md @<repo>/.local/bake/<name>/reporting.md
```

Pi includes each `@file` in the worker's first prompt, in that order, so the
worker reads the brief and then the reporting instructions. Bake-written
files live in `<repo>/.local/bake/<name>/`, beside the worktrees and under
the same ignore rule; `bake new` prints the directory as `input:`.

- `reporting.md` - the reporting instructions, naming this worktree and the
  result file below. Written for every launch that has a brief.
- `brief.md` - the brief text, written only for `--prompt` and `-P -`. A
  `--prompt-file` path is passed to Pi as given and is never copied or
  changed.
- `result.md` - not created by bake. The reporting instructions tell the
  worker to write its outcome here, or to the task's ledger, when no monitor
  is registered, the reporting tool is missing, or the report is rejected.

Register the monitor right after `bake new` returns. The worker binds to a
monitor registered after launch on its next heartbeat, and `bake_report`
looks the monitor up by worktree when it is called, so no task id has to
reach the worker. A report made before any monitor exists is rejected, and
the worker then writes `result.md` instead of claiming delivery.

Without `--prompt` or `--prompt-file` the worker starts bare
(`pi --model <selector>`) and no files are written: there is no task to
report on, and a reporting-only first prompt would start a model turn for
nothing. A missing, unreadable, empty, or non-UTF-8 brief fails before the
worktree or branch is created, and nothing is launched. `bake rm` and
`bake done` remove the input directory with the worktree.

## Model selection

Worker sessions are for programming, so `bake new` always starts pi with an
explicit selector instead of the global default:

```
pi --model anthropic/claude-fable-5-1:max
```

The same command goes to the tmux window and into the `next:` line printed
with `--no-window` or outside tmux. The global pi settings (`defaultModel`,
`defaultThinkingLevel`, `modelThinkingLevels`) stay for the sessions you open
by hand, which is how the conversation stays on Astra at medium while workers
run Fable 5.1 at max.

`--model` overrides the selector for one worker. It takes pi's selector
syntax, `provider/id[:thinking]`:

```
bake new astra-doc-pass --model openai/gpt-6-astra:medium -P brief.md
```

Give the full `provider/id` and the `:thinking` suffix. Without the suffix pi
reads the thinking level from its own settings (`modelThinkingLevels`, then
`defaultThinkingLevel`). An empty or blank selector is rejected before the
worktree is created. The selector is the launch selection only; the worker
can still switch with `/model` or `/thinking` inside the session.

`bake open` recreates a window without starting pi. `pi -c` resumes the
directory's last session with the model and thinking level that session
recorded, so a worker made by `bake new` comes back as Fable 5.1 at max, and
a session from before this default keeps whatever it saved. To move an older
worker to the current policy, pass the selector while resuming; the
command-line selector takes precedence over the saved one:

```
pi -c --model anthropic/claude-fable-5-1:max
```

## Setup hooks

Optional `.bake.json` at the repo root (read from the new worktree's
checkout):

```json
{
  "copy": [".env", "config/local.json"],
  "run": ["npm ci"]
}
```

`copy` globs are copied from the main checkout when the worktree does not
already have them (gitignored files do not exist in fresh worktrees). `run`
commands execute in the worktree; failures warn and continue.

## Installation

```
ln -s ~/code/petro/bake/bin/bake ~/bin/bake
```

## Tests

Standard-library `unittest`, no model and no network:

```
python3 -m unittest discover -s tests -v
```

`tests/test_bake.py` replaces git and tmux with a recorder and creates
nothing outside a temp directory. `tests/test_bake_e2e.py` runs `bin/bake`
as a process against a temporary git repository with fake `tmux` and `pi`
executables on `PATH`; the fake tmux runs the launch command in the new
window's directory and the fake pi records what it received, following Pi's
`@file` handling.
