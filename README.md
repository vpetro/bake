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
  `.bake.json`, and start Pi in a separate tmux window. Supply the task with
  `--prompt` (`-p`), `--prompt-file` (`-P`), or stdin through `-P -`.
  Pi reads the brief and bake's reporting instructions from files at startup.
  See [Worker input and reporting](#worker-input-and-reporting). `--model` picks
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
- `bake rm <name> [--force]` - stop the window and remove the worktree and
  its generated input directory, keeping the branch. Dirty worktrees require
  `--force`. A saved `result.md` must be preserved elsewhere and removed from
  the input directory first; `--force` does not bypass this check.
- `bake done <name> [--force]` - `rm` plus delete the branch. For after the
  PR merges.

## Worker input and reporting

Bake starts Pi with the task brief and a separate reporting file as `@file`
arguments. Both become part of the first prompt. The worker gets its task
and reporting instructions together; the control does not need to send a
second message through tmux.

The control chat still registers the monitor with `bake_monitor` immediately
after launch. Bake does not create monitor records or choose an owning
session. The worker calls `bake_report` from its worktree without needing a
task id sent to its pane. State the completion criteria and required evidence
in the task brief; the generic reporting instructions do not decide what
counts as finished.

Generated files are under `<repo>/.local/bake/<name>/`, shown as `input:` in
the launch output:

- `reporting.md` tells the worker to report completed, blocked, or failed,
  with evidence and any unverified behavior.
- `brief.md` holds inline or stdin input. For `--prompt-file`, bake instead
  uses the resolved original path without changing or copying the file.
  Keep that file available until Pi has read it.
- `result.md` is a fallback the worker can write if reporting is unavailable
  or the tool says the originating chat will not be notified. The worker
  can use the task's ledger instead. Bake does not create this result or
  automatically deliver it later.

Bake writes private input files and creates `.local/bake/.gitignore` if
missing so generated input stays out of Git. It leaves existing ignore files
unchanged. With `--with-changes`, bake checks that transferring changes will
not remove or revert the input Pi needs. It refuses before moving changes
if a generated file or referenced brief would be affected. Keep a referenced
brief outside the changes being transferred; a clean tracked brief can also
remain in place.

A monitor registered after launch is found by worktree when the worker
reports. A report made before registration is rejected and must be saved
locally instead. This remains a race: the fallback is not an automatic
notification. Later updates to a running worker are not supported by this
file-input flow; changing the original brief does not send a new message.

Without a brief, bake starts Pi without an initial prompt or generated input.
Invalid brief input fails before creating a branch or worktree. Input-file
write failures also prevent the launch. A supplied brief inside this bake's
own input directory is rejected so cleanup cannot delete it.

`bake rm` and `bake done` remove the generated input directory, but refuse
if it contains `result.md`, even with `--force`. Starting another bake with
the same name has the same guard. Preserve the saved outcome elsewhere and
remove that result file before retrying.

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

Unit tests replace Git and tmux calls with recorded operations. End-to-end
tests run the CLI against temporary Git repositories with fake tmux and Pi
executables. They check the files Pi receives, quoting, input validation,
saved-result protection, and transfer of uncommitted changes. They do not
launch real workers or make provider calls.
