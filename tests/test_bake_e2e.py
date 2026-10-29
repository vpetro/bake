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
    def __init__(self, root):
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

    def test_rm_removes_the_worktree_and_its_input_dir(self):
        self.assertEqual(self.h.bake("new", "feature", "-p", "Do it.").returncode, 0)
        (self.h.folder / "result.md").write_text("delivery failed; outcome here\n", encoding="utf-8")

        result = self.h.bake("rm", "feature")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"removed:  {self.h.worktree.resolve()}\n", result.stdout)
        self.assertIn(f"removed:  {self.h.folder.resolve()}\n", result.stdout)
        self.assertIn("kept:     branch petro/feature\n", result.stdout)
        self.assertFalse(self.h.worktree.exists())
        self.assertFalse(self.h.folder.exists())
        self.assertEqual(self.h.git("worktree", "list", "--porcelain").count("worktree "), 1)


if __name__ == "__main__":
    unittest.main()
