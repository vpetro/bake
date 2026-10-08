import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

BAKE_PATH = Path(__file__).resolve().parents[1] / "bin" / "bake"
LOADER = importlib.machinery.SourceFileLoader("bake", str(BAKE_PATH))
SPEC = importlib.util.spec_from_loader("bake", LOADER)
bake = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(bake)

DEFAULT = "anthropic/claude-fable-5-1:max"
OVERRIDE = "openai/gpt-6-astra:medium"


class FakeInvoke:
    def __init__(self, main, window_id="@7", windows=""):
        self.main = str(main)
        self.window_id = window_id
        self.windows = windows
        self.calls = []

    def __call__(self, command, cwd=None, check=True):
        command = [str(value) for value in command]
        self.calls.append(command)
        returncode, stdout = self.respond(command)
        if check and returncode != 0:
            raise RuntimeError(f"fake command failed: {' '.join(command)}")
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    def respond(self, command):
        if command[:2] == ["git", "rev-parse"] and "--show-toplevel" in command:
            return 0, f"{self.main}\n"
        if command[:3] == ["git", "worktree", "list"]:
            return 0, f"worktree {self.main}\nHEAD 0000\nbranch refs/heads/main\n\n"
        if command[:3] == ["git", "rev-parse", "--verify"]:
            return (0 if command[-1] == "refs/remotes/origin/main" else 1), ""
        if command[:2] == ["git", "symbolic-ref"]:
            return 0, "origin/main\n"
        if command[:2] == ["tmux", "list-windows"]:
            return 0, self.windows
        if command[:2] == ["tmux", "new-window"]:
            return 0, f"{self.window_id}\n"
        return 0, ""

    def named(self, *prefix):
        return [call for call in self.calls if call[: len(prefix)] == list(prefix)]


def parse(*argv):
    return bake.parser().parse_args(list(argv))


def run(handler, args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        handler(args)
    return output.getvalue()


class ParserTests(unittest.TestCase):
    def test_new_defaults_to_the_coding_model(self):
        args = parse("new", "feature")
        self.assertEqual(args.model, DEFAULT)
        self.assertEqual(bake.DEFAULT_MODEL, DEFAULT)

    def test_new_accepts_a_model_override(self):
        self.assertEqual(parse("new", "feature", "--model", OVERRIDE).model, OVERRIDE)

    def test_prompt_and_prompt_file_stay_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse("new", "feature", "--prompt", "x", "--prompt-file", "y")

    def test_help_shows_the_default_selector(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit):
                parse("new", "--help")
        self.assertIn("--model MODEL", output.getvalue())
        self.assertIn(DEFAULT, output.getvalue())


class CommandTests(unittest.TestCase):
    def test_default_selector_without_prompt(self):
        self.assertEqual(bake.pi_command(DEFAULT), f"pi --model {DEFAULT}")

    def test_override_selector(self):
        self.assertEqual(bake.pi_command(OVERRIDE), f"pi --model {OVERRIDE}")

    def test_selector_with_shell_characters_is_quoted(self):
        selector = "my provider/model id:high"
        command = bake.pi_command(selector)
        self.assertEqual(shlex.split(command), ["pi", "--model", selector])

    def test_empty_and_blank_selectors_are_rejected(self):
        for selector in ("", "   ", "\t\n", None):
            with self.subTest(selector=selector):
                with self.assertRaisesRegex(RuntimeError, "model selector is empty"):
                    bake.pi_command(selector, prompt="do it")

    def test_prompt_is_passed_literally(self):
        prompt = "fix it's \"quoted\" $HOME `date` ; rm -rf / && echo\nsecond line"
        command = bake.pi_command(DEFAULT, prompt=prompt)
        self.assertEqual(shlex.split(command), ["pi", "--model", DEFAULT, prompt])
        self.assertTrue(command.startswith(f"pi --model {DEFAULT} "))

    def test_prompt_file_path_with_spaces_and_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "my briefs"
            folder.mkdir()
            brief = folder / "it's \"the\" brief.md"
            brief.write_text("Do the thing.\n", encoding="utf-8")
            command = bake.pi_command(DEFAULT, prompt_file=str(brief))
            self.assertEqual(shlex.split(command), ["pi", "--model", DEFAULT, f"@{brief.resolve()}"])

    def test_prompt_file_override_keeps_model_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "brief.md"
            brief.write_text("Do the thing.\n", encoding="utf-8")
            command = bake.pi_command(OVERRIDE, prompt_file=str(brief))
            self.assertEqual(command, f"pi --model {OVERRIDE} @{brief.resolve()}")

    def test_prompt_file_must_exist_and_have_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.md"
            with self.assertRaisesRegex(RuntimeError, "prompt file not found"):
                bake.pi_command(DEFAULT, prompt_file=str(missing))
            empty = Path(tmp) / "empty.md"
            empty.write_text(" \n\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "prompt file is empty"):
                bake.pi_command(DEFAULT, prompt_file=str(empty))

    def test_stdin_prompt_is_spooled_to_a_temp_file(self):
        with mock.patch.object(sys, "stdin", io.StringIO("\n\nRead the ledger first.\nThen work.\n\n")):
            command = bake.pi_command(OVERRIDE, prompt_file="-")
        parts = shlex.split(command)
        self.assertEqual(parts[:3], ["pi", "--model", OVERRIDE])
        self.assertTrue(parts[3].startswith("@"))
        spooled = Path(parts[3][1:])
        try:
            self.assertTrue(spooled.is_file())
            self.assertTrue(spooled.name.startswith("bake-prompt-"))
            self.assertEqual(spooled.read_text(encoding="utf-8"), "Read the ledger first.\nThen work.\n")
        finally:
            spooled.unlink()

    def test_empty_stdin_prompt_is_rejected(self):
        for text in ("", "  \n\t\n"):
            with self.subTest(text=repr(text)):
                with mock.patch.object(sys, "stdin", io.StringIO(text)):
                    with self.assertRaisesRegex(RuntimeError, "prompt file is empty"):
                        bake.pi_command(DEFAULT, prompt_file="-")


class NewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.main = Path(self.tmp.name).resolve()
        self.fake = FakeInvoke(self.main)
        patcher = mock.patch.object(bake, "invoke", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def worktree(self, name):
        return self.main / ".local" / "worktrees" / name

    def test_no_window_prints_the_next_command_with_the_default_model(self):
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            output = run(bake.new, parse("new", "feature", "--prompt", "fix the thing", "--no-window"))
        expected = f"pi --model {DEFAULT} 'fix the thing'"
        self.assertIn(f"next:     cd {self.worktree('feature')} && {expected}\n", output)
        self.assertEqual(self.fake.named("tmux"), [])
        self.assertIn(
            ["git", "worktree", "add", "-b", "petro/feature", str(self.worktree("feature")), "refs/remotes/origin/main"],
            self.fake.calls,
        )

    def test_outside_tmux_prints_the_next_command_without_a_prompt(self):
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            output = run(bake.new, parse("new", "feature"))
        self.assertIn(f"next:     cd {self.worktree('feature')} && pi --model {DEFAULT}\n", output)

    def test_no_window_prints_the_override(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.new, parse("new", "feature", "--model", OVERRIDE, "--no-window"))
        self.assertIn(f"&& pi --model {OVERRIDE}\n", output)
        self.assertEqual(self.fake.named("tmux"), [])

    def test_window_receives_the_default_command(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.new, parse("new", "feature", "-p", "it's urgent"))
        self.assertIn("window:   feature (@7) running pi\n", output)
        self.assertEqual(
            self.fake.named("tmux", "new-window"),
            [["tmux", "new-window", "-P", "-F", "#{window_id}", "-n", "feature", "-c", str(self.worktree("feature"))]],
        )
        self.assertEqual(
            self.fake.named("tmux", "send-keys"),
            [["tmux", "send-keys", "-t", "@7", f"pi --model {DEFAULT} 'it'\"'\"'s urgent'", "Enter"]],
        )

    def test_window_receives_the_override_with_a_prompt_file(self):
        brief = self.main / "plans" / "briefs" / "feature brief.md"
        brief.parent.mkdir(parents=True)
        brief.write_text("Read the ledger.\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            run(bake.new, parse("new", "feature", "--model", OVERRIDE, "-P", str(brief)))
        sent = self.fake.named("tmux", "send-keys")[0][4]
        self.assertEqual(shlex.split(sent), ["pi", "--model", OVERRIDE, f"@{brief}"])

    def test_blank_override_is_rejected_before_any_side_effect(self):
        for selector in ("", "  "):
            with self.subTest(selector=repr(selector)):
                with self.assertRaisesRegex(RuntimeError, "model selector is empty"):
                    bake.new(parse("new", "feature", "--model", selector))
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.main / ".local").exists())


class OpenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.main = Path(self.tmp.name).resolve()
        self.path = self.main / ".local" / "worktrees" / "feature"
        self.path.mkdir(parents=True)
        self.fake = FakeInvoke(self.main, window_id="@9")
        patcher = mock.patch.object(bake, "invoke", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_recreated_window_only_hints_at_resuming(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.open_worktree, parse("open", "feature"))
        self.assertIn("window:   feature (@9)\n", output)
        self.assertIn("hint:     pi -c resumes the last session for this directory\n", output)
        self.assertEqual(len(self.fake.named("tmux", "new-window")), 1)
        self.assertEqual(self.fake.named("tmux", "send-keys"), [])

    def test_existing_window_is_selected(self):
        self.fake.windows = "feature\nother\n"
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.open_worktree, parse("open", "feature"))
        self.assertEqual(output, "switched to window feature\n")
        self.assertEqual(self.fake.named("tmux", "select-window"), [["tmux", "select-window", "-t", "feature"]])

    def test_outside_tmux_prints_cd(self):
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            output = run(bake.open_worktree, parse("open", "feature"))
        self.assertEqual(output, f"cd {self.path}\n")


if __name__ == "__main__":
    unittest.main()
