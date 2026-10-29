import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

BAKE_PATH = Path(__file__).resolve().parents[1] / "bin" / "bake"
DEFAULT = "anthropic/claude-fable-5-1:max"
NASTY = "fix it's \"quoted\" $HOME `date` ; rm -rf / && echo PWNED > marker\n\nsecond line\twith tab"

FAKE_TMUX = """\
#!{python}
import json, os, subprocess, sys
log = os.environ["FAKE_LOG_DIR"]
args = sys.argv[1:]
with open(os.path.join(log, "tmux.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
verb = args[0] if args else ""
if verb == "list-windows":
    sys.exit(0)
if verb == "new-window":
    window = {{"id": "@42", "name": args[args.index("-n") + 1], "cwd": args[args.index("-c") + 1]}}
    with open(os.path.join(log, "window.json"), "w", encoding="utf-8") as handle:
        json.dump(window, handle)
    print(window["id"])
    sys.exit(0)
if verb == "send-keys":
    with open(os.path.join(log, "window.json"), encoding="utf-8") as handle:
        window = json.load(handle)
    if args[args.index("-t") + 1] != window["id"]:
        sys.exit(3)
    keys = args[args.index("-t") + 2:]
    if len(keys) != 2 or keys[1] != "Enter":
        sys.exit(4)
    sys.exit(subprocess.run(["bash", "-c", keys[0]], cwd=window["cwd"]).returncode)
if verb == "kill-window":
    sys.exit(0)
sys.exit(5)
"""

FAKE_PI = """\
#!{python}
import json, os, sys
args = sys.argv[1:]
model = None
files = []
messages = []
i = 0
while i < len(args):
    arg = args[i]
    if arg == "--model" and i + 1 < len(args):
        model = args[i + 1]
        i += 2
        continue
    if arg == "--":
        for rest in args[i + 1:]:
            (files.append(rest[1:]) if rest.startswith("@") else messages.append(rest))
        break
    if arg.startswith("@"):
        files.append(arg[1:])
    elif arg.startswith("-"):
        print("Unknown option: " + arg, file=sys.stderr)
        sys.exit(2)
    else:
        messages.append(arg)
    i += 1
text = ""
for file in files:
    path = os.path.abspath(os.path.join(os.getcwd(), os.path.expanduser(file)))
    if not os.path.exists(path):
        print("Error: File not found: " + path, file=sys.stderr)
        sys.exit(1)
    if os.path.getsize(path) == 0:
        continue
    with open(path, encoding="utf-8") as handle:
        text += '<file name="' + path + '">\\n' + handle.read() + '\\n</file>\\n'
if messages:
    text += messages[0]
record = {{"argv": args, "cwd": os.path.realpath(os.getcwd()), "model": model, "files": files, "messages": messages, "prompt": text or None}}
with open(os.environ["FAKE_PI_RECORD"], "w", encoding="utf-8") as handle:
    json.dump(record, handle)
"""


def write_executable(path, text):
    path.write_text(text.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o700)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class Harness:
    def __init__(self, root, ignore_local=True):
        self.root = Path(root).resolve()
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.fakebin = self.root / "fakebin"
        self.log = self.root / "log"
        for folder in (self.home, self.repo, self.fakebin, self.log):
            folder.mkdir()
        write_executable(self.fakebin / "tmux", FAKE_TMUX)
        write_executable(self.fakebin / "pi", FAKE_PI)
        (self.home / ".gitconfig").write_text("", encoding="utf-8")
        self.record = self.log / "pi-record.json"
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# demo\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "initial")
        if ignore_local:
            self.ignore_local()

    def ignore_local(self):
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("/.local/\n")

    def env(self, **extra):
        env = {
            "PATH": f"{self.fakebin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "TMUX": "/tmp/tmux-fake/default,1,0",
            "TMUX_PANE": "%1",
            "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "FAKE_LOG_DIR": str(self.log),
            "FAKE_PI_RECORD": str(self.record),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        }
        env.update(extra)
        return env

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd or self.repo, env=self.env(), check=True, text=True, capture_output=True).stdout

    def bake(self, *args, stdin=None, cwd=None, **env):
        return subprocess.run(
            [sys.executable, str(BAKE_PATH), *args],
            cwd=cwd or self.repo,
            env=self.env(**env),
            input=stdin,
            text=True,
            capture_output=True,
            timeout=120,
        )

    def tmux_calls(self):
        path = self.log / "tmux.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def pi_record(self):
        return json.loads(self.record.read_text(encoding="utf-8"))

    def output_field(self, stdout, label):
        for line in stdout.splitlines():
            if line.startswith(f"{label}:"):
                return line.split(":", 1)[1].strip()
        raise AssertionError(f"no {label}: line in\n{stdout}")

    @property
    def worktree(self):
        return self.repo / ".local" / "worktrees" / "feature"

    @property
    def folder(self):
        return self.repo / ".local" / "bake" / "feature"


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = Harness(self.tmp.name)

    def assert_clean_checkouts(self):
        self.assertEqual(self.h.git("status", "--porcelain"), "")
        self.assertEqual(self.h.git("status", "--porcelain", cwd=self.h.worktree), "")

    def test_launch_from_a_brief_file_needs_one_send_keys_and_no_follow_up(self):
        brief = self.h.repo / ".local" / "plans" / "briefs" / "feature brief.md"
        brief.parent.mkdir(parents=True)
        brief_text = "# Feature\n\nRead the ledger first.\nReport readiness when the preview deploys; mailbox behavior stays unverified.\n"
        brief.write_text(brief_text, encoding="utf-8")
        before = os.stat(brief)

        result = self.h.bake("new", "feature", "-P", str(brief))

        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = Path(self.h.output_field(result.stdout, "worktree"))
        folder = Path(self.h.output_field(result.stdout, "input"))
        self.assertEqual(worktree, self.h.worktree.resolve())
        self.assertEqual(folder, self.h.folder.resolve())
        self.assertIn("window:   feature (@42) running pi\n", result.stdout)
        self.assertIn("branch:   petro/feature (from refs/heads/main)\n", result.stdout)

        calls = self.h.tmux_calls()
        self.assertEqual([call[0] for call in calls], ["new-window", "send-keys"])
        self.assertEqual(calls[0][calls[0].index("-c") + 1], str(worktree))
        launch = calls[1][calls[1].index("-t") + 2]
        reporting = folder / "reporting.md"
        self.assertEqual(shlex.split(launch), ["pi", "--model", DEFAULT, f"@{brief.resolve()}", f"@{reporting}"])

        record = self.h.pi_record()
        self.assertEqual(record["model"], DEFAULT)
        self.assertEqual(record["files"], [str(brief.resolve()), str(reporting)])
        self.assertEqual(record["messages"], [])
        self.assertIn(f'<file name="{brief.resolve()}">\n{brief_text}\n</file>\n', record["prompt"])
        self.assertIn(f'<file name="{reporting}">\n# Report to the originating chat\n', record["prompt"])
        self.assertIn("call bake_report with status completed", record["prompt"])
        self.assertIn(f"Use the monitor associated with this worktree (`{worktree}`)", record["prompt"])
        self.assertIn(f"the result file `{folder / 'result.md'}`", record["prompt"])
        self.assertTrue(record["prompt"].index(brief_text) < record["prompt"].index("# Report to the originating chat"))

        self.assertEqual(record["cwd"], os.path.realpath(worktree))

        self.assertEqual(brief.read_text(encoding="utf-8"), brief_text)
        self.assertEqual(os.stat(brief).st_mtime_ns, before.st_mtime_ns)
        self.assertEqual([path.name for path in folder.iterdir()], ["reporting.md"])
        self.assertEqual(mode(folder), 0o700)
        self.assertEqual(mode(reporting), 0o600)
        self.assertIn("petro/feature", self.h.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree))
        self.assert_clean_checkouts()

    def test_inline_prompt_reaches_the_worker_verbatim_and_never_runs_in_the_shell(self):
        result = self.h.bake("new", "feature", "-p", NASTY)

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.h.pi_record()
        folder = self.h.folder.resolve()
        self.assertEqual(record["files"], [str(folder / "brief.md"), str(folder / "reporting.md")])
        self.assertIn(f'<file name="{folder / "brief.md"}">\n{NASTY}\n\n</file>\n', record["prompt"])
        self.assertIn("`date`", record["prompt"])
        self.assertIn("$HOME", record["prompt"])
        self.assertNotIn(str(self.h.home), record["prompt"])
        for root in (self.h.repo, self.h.worktree, self.h.root):
            self.assertFalse((root / "marker").exists())
        self.assertEqual((folder / "brief.md").read_text(encoding="utf-8"), NASTY + "\n")
        self.assertEqual(len([call for call in self.h.tmux_calls() if call[0] == "send-keys"]), 1)
        self.assert_clean_checkouts()

    def test_stdin_brief_is_spooled_and_read_by_the_worker(self):
        text = "Read the ledger first.\n\nThen work; report with bake_report.\n"
        result = self.h.bake("new", "feature", "-P", "-", stdin=text)

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.h.pi_record()
        folder = self.h.folder.resolve()
        self.assertEqual(record["files"], [str(folder / "brief.md"), str(folder / "reporting.md")])
        self.assertEqual((folder / "brief.md").read_text(encoding="utf-8"), text)
        self.assertIn(f'<file name="{folder / "brief.md"}">\n{text}\n</file>\n', record["prompt"])

    def test_no_prompt_launches_a_bare_worker_and_writes_no_input(self):
        result = self.h.bake("new", "feature")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("input:", result.stdout)
        record = self.h.pi_record()
        self.assertEqual(record["argv"], ["--model", DEFAULT])
        self.assertIsNone(record["prompt"])
        self.assertFalse((self.h.repo / ".local" / "bake").exists())

    def test_missing_brief_fails_before_any_worktree_or_launch(self):
        result = self.h.bake("new", "feature", "-P", str(self.h.repo / "missing.md"))

        self.assertEqual(result.returncode, 1)
        self.assertIn("bake: prompt file not found:", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.h.tmux_calls(), [])
        self.assertFalse(self.h.record.exists())
        self.assertFalse((self.h.repo / ".local").exists())
        self.assertEqual(self.h.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(self.h.git("branch", "--list", "petro/feature"), "")

    def test_empty_brief_fails_the_same_way(self):
        empty = self.h.repo / "empty.md"
        empty.write_text("\n\n", encoding="utf-8")
        result = self.h.bake("new", "feature", "-P", str(empty))

        self.assertEqual(result.returncode, 1)
        self.assertIn("bake: prompt file is empty:", result.stderr)
        self.assertEqual(self.h.tmux_calls(), [])
        self.assertFalse((self.h.repo / ".local").exists())

    def test_no_window_prints_a_next_command_that_launches_the_same_worker(self):
        brief = self.h.repo / "brief.md"
        brief.write_text("Do the thing.\n", encoding="utf-8")
        result = self.h.bake("new", "feature", "-P", str(brief), "--no-window")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.h.tmux_calls(), [])
        self.assertFalse(self.h.record.exists())
        next_command = self.h.output_field(result.stdout, "next")
        self.assertTrue(next_command.startswith(f"cd {self.h.worktree.resolve()} && pi --model "))

        shell = subprocess.run(["bash", "-c", next_command], env=self.h.env(), cwd=self.h.repo, text=True, capture_output=True)
        self.assertEqual(shell.returncode, 0, shell.stderr)
        record = self.h.pi_record()
        self.assertEqual(record["cwd"], os.path.realpath(self.h.worktree))
        self.assertEqual(record["files"], [str(brief.resolve()), str(self.h.folder.resolve() / "reporting.md")])
        self.assertIn("Do the thing.", record["prompt"])

    def test_rm_keeps_a_saved_result_until_it_is_moved_away(self):
        self.assertEqual(self.h.bake("new", "feature", "-p", "Do it.").returncode, 0)
        saved = self.h.folder / "result.md"
        saved.write_text("delivery failed; outcome here\n", encoding="utf-8")
        calls_after_launch = len(self.h.tmux_calls())
        message = f"bake: Saved worker result exists: {self.h.folder.resolve() / 'result.md'}. Preserve it before removing or reusing this bake.\n"

        for extra in ((), ("--force",)):
            with self.subTest(extra=extra):
                refused = self.h.bake("rm", "feature", *extra)
                self.assertEqual(refused.returncode, 1)
                self.assertEqual(refused.stderr, message)
                self.assertEqual(refused.stdout, "")
        self.assertEqual(len(self.h.tmux_calls()), calls_after_launch)
        self.assertTrue(self.h.worktree.is_dir())
        self.assertEqual(self.h.git("worktree", "list", "--porcelain").count("worktree "), 2)
        self.assertEqual(sorted(path.name for path in self.h.folder.iterdir()), ["brief.md", "reporting.md", "result.md"])
        self.assertEqual(saved.read_text(encoding="utf-8"), "delivery failed; outcome here\n")

        saved.rename(self.h.root / "feature-result.md")
        result = self.h.bake("rm", "feature")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"removed:  {self.h.worktree.resolve()}\n", result.stdout)
        self.assertIn(f"removed:  {self.h.folder.resolve()}\n", result.stdout)
        self.assertIn("kept:     branch petro/feature\n", result.stdout)
        self.assertFalse(self.h.worktree.exists())
        self.assertFalse(self.h.folder.exists())
        self.assertEqual(self.h.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual((self.h.root / "feature-result.md").read_text(encoding="utf-8"), "delivery failed; outcome here\n")

    def test_saved_result_blocks_reusing_the_name_until_it_is_moved_away(self):
        self.assertEqual(self.h.bake("new", "feature", "-p", "First task.").returncode, 0)
        saved = self.h.folder / "result.md"
        saved.write_text("delivery failed; outcome here\n", encoding="utf-8")
        self.h.git("worktree", "remove", "--force", str(self.h.worktree))
        self.h.git("branch", "-D", "petro/feature")
        calls_after_launch = len(self.h.tmux_calls())
        self.h.record.unlink()

        refused = self.h.bake("new", "feature", "-p", "Second task.")

        self.assertEqual(refused.returncode, 1)
        self.assertEqual(
            refused.stderr,
            f"bake: Saved worker result exists: {self.h.folder.resolve() / 'result.md'}. Preserve it before removing or reusing this bake.\n",
        )
        self.assertEqual(refused.stdout, "")
        self.assertEqual(len(self.h.tmux_calls()), calls_after_launch)
        self.assertFalse(self.h.record.exists())
        self.assertFalse(self.h.worktree.exists())
        self.assertEqual(self.h.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(self.h.git("branch", "--list", "petro/feature"), "")
        self.assertEqual(sorted(path.name for path in self.h.folder.iterdir()), ["brief.md", "reporting.md", "result.md"])
        self.assertEqual((self.h.folder / "brief.md").read_text(encoding="utf-8"), "First task.\n")
        self.assertEqual(saved.read_text(encoding="utf-8"), "delivery failed; outcome here\n")

        saved.rename(self.h.root / "feature-result.md")
        result = self.h.bake("new", "feature", "-p", "Second task.")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(path.name for path in self.h.folder.iterdir()), ["brief.md", "reporting.md"])
        self.assertEqual((self.h.folder / "brief.md").read_text(encoding="utf-8"), "Second task.\n")
        self.assertIn("Second task.", self.h.pi_record()["prompt"])
        self.assertTrue(self.h.worktree.is_dir())

    def test_brief_inside_the_input_dir_is_rejected_and_kept(self):
        inside = self.h.folder / "brief.md"
        inside.parent.mkdir(parents=True)
        inside.write_text("Brief left in the input directory.\n", encoding="utf-8")

        result = self.h.bake("new", "feature", "-P", str(inside))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            f"bake: Prompt file is inside the bake input directory: {inside.resolve()}. Move it outside that directory before starting this bake.\n",
        )
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.h.tmux_calls(), [])
        self.assertFalse(self.h.record.exists())
        self.assertEqual(inside.read_text(encoding="utf-8"), "Brief left in the input directory.\n")
        self.assertEqual([path.name for path in self.h.folder.iterdir()], ["brief.md"])
        self.assertFalse((self.h.repo / ".local" / "worktrees").exists())
        self.assertEqual(self.h.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(self.h.git("branch", "--list", "petro/feature"), "")

    def test_input_failure_leaves_no_worktree_and_the_same_command_works_after_the_fix(self):
        blocker = self.h.repo / ".local" / "bake"
        blocker.parent.mkdir()
        blocker.write_text("not a directory\n", encoding="utf-8")

        failed = self.h.bake("new", "feature", "-p", "Do it.")

        self.assertEqual(failed.returncode, 1)
        self.assertEqual(
            failed.stderr,
            f"bake: Could not prepare worker input: File exists: {blocker.resolve()}. No worker was started.\n",
        )
        self.assertEqual(failed.stdout, "")
        self.assertEqual(self.h.tmux_calls(), [])
        self.assertFalse(self.h.record.exists())
        self.assertFalse((self.h.repo / ".local" / "worktrees").exists())
        self.assertEqual(self.h.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(self.h.git("branch", "--list", "petro/feature"), "")
        self.assertEqual(blocker.read_text(encoding="utf-8"), "not a directory\n")

        blocker.unlink()
        result = self.h.bake("new", "feature", "-p", "Do it.")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("window:   feature (@42) running pi\n", result.stdout)
        self.assertEqual([call[0] for call in self.h.tmux_calls()], ["new-window", "send-keys"])
        self.assertIn("Do it.", self.h.pi_record()["prompt"])
        self.assertEqual(sorted(path.name for path in self.h.folder.iterdir()), ["brief.md", "reporting.md"])
        self.assert_clean_checkouts()


class UnignoredLocalTests(unittest.TestCase):
    REFUSAL = "bake: Bake input files must be ignored before using --with-changes. No changes were moved.\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = Harness(self.tmp.name, ignore_local=False)
        (self.h.repo / "README.md").write_text("# demo\n\nedited\n", encoding="utf-8")
        (self.h.repo / "notes.txt").write_text("scratch\n", encoding="utf-8")
        plan = self.h.repo / ".local" / "plans" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("plan\n", encoding="utf-8")
        self.ignore = self.h.repo / ".local" / "bake" / ".gitignore"

    def status(self, cwd=None):
        return self.h.git("status", "--porcelain", "--untracked-files=all", cwd=cwd)

    def assert_refused_without_side_effects(self, result, status_before):
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, self.REFUSAL)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.h.tmux_calls(), [])
        self.assertFalse(self.h.record.exists())
        self.assertEqual(self.status(), status_before)
        self.assertEqual(self.h.git("stash", "list"), "")
        self.assertFalse((self.h.repo / ".local" / "worktrees").exists())
        self.assertEqual(self.h.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(self.h.git("branch", "--list", "petro/feature"), "")
        self.assertEqual([path.name for path in self.ignore.parent.iterdir()], [".gitignore"])

    def test_with_changes_moves_the_wip_but_not_the_generated_input(self):
        result = self.h.bake("new", "feature", "-p", "Do it.", "--with-changes")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("note:     moved uncommitted changes into the worktree\n", result.stdout)
        folder = self.h.folder.resolve()
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["brief.md", "reporting.md"])
        self.assertEqual(self.ignore.read_text(encoding="utf-8"), "*\n")
        self.assertEqual(mode(self.ignore), 0o600)
        record = self.h.pi_record()
        self.assertEqual(record["files"], [str(folder / "brief.md"), str(folder / "reporting.md")])
        self.assertIn(f'<file name="{folder / "brief.md"}">\nDo it.\n\n</file>\n', record["prompt"])
        self.assertIn("# Report to the originating chat", record["prompt"])
        self.assertEqual(record["cwd"], os.path.realpath(self.h.worktree))
        self.assertEqual(len([call for call in self.h.tmux_calls() if call[0] == "send-keys"]), 1)
        self.assertEqual(self.status(), "?? .local/worktrees/feature/\n")
        self.assertEqual(self.status(cwd=self.h.worktree), " M README.md\n?? .local/plans/plan.md\n?? notes.txt\n")
        self.assertEqual((self.h.worktree / "README.md").read_text(encoding="utf-8"), "# demo\n\nedited\n")
        self.assertEqual((self.h.worktree / "notes.txt").read_text(encoding="utf-8"), "scratch\n")
        self.assertFalse((self.h.worktree / ".local" / "bake").exists())
        self.assertEqual(self.h.git("stash", "list"), "")

    def test_with_changes_refuses_when_the_brief_would_move_and_works_once_it_is_ignored(self):
        brief = self.h.repo / ".local" / "plans" / "briefs" / "feature.md"
        brief.parent.mkdir()
        brief.write_text("Read the ledger.\n", encoding="utf-8")
        before = self.status()
        self.assertIn("?? .local/plans/briefs/feature.md\n", before)

        refused = self.h.bake("new", "feature", "-P", str(brief), "--with-changes")

        self.assert_refused_without_side_effects(refused, before)
        self.assertEqual(self.ignore.read_text(encoding="utf-8"), "*\n")
        self.assertEqual(brief.read_text(encoding="utf-8"), "Read the ledger.\n")

        self.h.ignore_local()
        result = self.h.bake("new", "feature", "-P", str(brief), "--with-changes")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("note:     moved uncommitted changes into the worktree\n", result.stdout)
        record = self.h.pi_record()
        self.assertEqual(record["files"], [str(brief.resolve()), str(self.h.folder.resolve() / "reporting.md")])
        self.assertIn("Read the ledger.", record["prompt"])
        self.assertEqual(brief.read_text(encoding="utf-8"), "Read the ledger.\n")
        self.assertEqual(self.status(), "")
        self.assertEqual(self.status(cwd=self.h.worktree), " M README.md\n?? notes.txt\n")

    def test_with_changes_refuses_when_an_existing_ignore_file_does_not_cover_the_input(self):
        self.ignore.parent.mkdir()
        self.ignore.write_text("", encoding="utf-8")
        before = self.status()
        self.assertIn("?? .local/bake/.gitignore\n", before)

        refused = self.h.bake("new", "feature", "-p", "Do it.", "--with-changes")

        self.assert_refused_without_side_effects(refused, before)
        self.assertEqual(self.ignore.read_text(encoding="utf-8"), "")

        self.ignore.unlink()
        result = self.h.bake("new", "feature", "-p", "Do it.", "--with-changes")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ignore.read_text(encoding="utf-8"), "*\n")
        self.assertIn("Do it.", self.h.pi_record()["prompt"])
        self.assertEqual(self.status(), "?? .local/worktrees/feature/\n")
        self.assertEqual(self.status(cwd=self.h.worktree), " M README.md\n?? .local/plans/plan.md\n?? notes.txt\n")


if __name__ == "__main__":
    unittest.main()
