# bake

Start new work in an isolated git worktree with its own tmux window running
pi, so nothing ever clobbers work in progress in the main checkout. Inspired
by [workmux](https://github.com/raine/workmux), trimmed to pi and this
machine's conventions.

Worktrees live in `<repo>/.local/worktrees/<name>` (the same convention the
pr-review system uses for its review worktrees) and branches default to
`petroverkhogliad/<name>`.

## Commands

- `bake new <name> [--prompt "..."] [--base X] [--branch Y] [--with-changes] [--no-window]`
  Fetch the base branch, create the worktree and branch, run setup from
  `.bake.json`, open a tmux window there running pi (seeded with `--prompt`
  when given). `--with-changes` moves uncommitted changes (including
  untracked files) from the current checkout into the new worktree via the
  stash; on conflict the stash is preserved. `--no-window` skips tmux, for
  agents or scripts.
- `bake ls` - worktrees with kind (`work` / `review`), dirty flag, window
  presence, and branch.
- `bake open <name>` - switch to the worktree's window, recreating it if
  needed (`pi -c` inside resumes the last session for that directory).
- `bake rm <name> [--force]` - kill the window and remove the worktree;
  refuses when dirty unless forced. Keeps the branch.
- `bake done <name> [--force]` - `rm` plus delete the branch. For after the
  PR merges.

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
