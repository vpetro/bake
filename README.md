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
  `.bake.json`, open a tmux window there running pi (seeded with `--prompt`
  when given). `--prompt-file` (`-P`) passes the file to pi as an `@file`
  argument, so pi reads it at startup; `-` spools stdin to a temp file first.
  `--model` picks the pi model selector; see [Model selection](#model-selection)
  for the default. With `--no-window`, the printed `next:` command includes
  the model and the prompt. `--with-changes` moves uncommitted changes
  (including untracked files) from the current checkout into the new worktree
  via the stash; on conflict the stash is preserved. `--no-window` skips tmux,
  for agents or scripts.
- `bake ls` - worktrees with kind (`work` / `review`), dirty flag, window
  presence, and branch.
- `bake open <name>` - switch to the worktree's window, recreating it if
  needed (`pi -c` inside resumes the last session for that directory).
- `bake rm <name> [--force]` - kill the window and remove the worktree;
  refuses when dirty unless forced. Keeps the branch.
- `bake done <name> [--force]` - `rm` plus delete the branch. For after the
  PR merges.

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

Standard-library `unittest`; git and tmux are replaced with a recorder, so
the tests create nothing outside a temp directory and need no model:

```
python3 -m unittest discover -s tests -v
```
