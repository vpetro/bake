import contextlib
import errno
import importlib.machinery
import importlib.util
import io
import os
import shlex
import shutil
import stat
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
NASTY = "fix it's \"quoted\" $HOME `date` ; rm -rf / && echo\n\nsecond line\twith tab"


class FakeInvoke:
    def __init__(self, main, window_id="@7", windows=""):
        self.main = str(main)
        self.top = None
        self.window_id = window_id
        self.windows = windows
        self.tree_status = ""
        self.input_status = ""
        self.failing = []
        self.calls = []
        self.cwds = []

    def __call__(self, command, cwd=None, check=True):
        command = [str(value) for value in command]
        self.calls.append(command)
        self.cwds.append(None if cwd is None else str(cwd))
        if any(command[: len(prefix)] == prefix for prefix in self.failing):
            returncode, stdout = 1, ""
        else:
            returncode, stdout = self.respond(command)
        if check and returncode != 0:
            raise RuntimeError(f"fake command failed: {' '.join(command)}")
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    def respond(self, command):
        if command[:2] == ["git", "rev-parse"] and "--show-toplevel" in command:
            return 0, f"{self.top or self.main}\n"
        if command[:3] == ["git", "status", "--porcelain"]:
            return 0, self.input_status if "--" in command else self.tree_status
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


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


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
    def test_default_selector_without_files(self):
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
                    bake.pi_command(selector, ["/tmp/brief.md"])
                with self.assertRaisesRegex(RuntimeError, "model selector is empty"):
                    bake.check_model(selector)

    def test_files_become_quoted_at_arguments_in_order(self):
        files = [Path("/my briefs/it's \"the\" brief.md"), Path("/repo/.local/bake/x/reporting.md")]
        command = bake.pi_command(OVERRIDE, files)
        self.assertEqual(shlex.split(command), ["pi", "--model", OVERRIDE, f"@{files[0]}", f"@{files[1]}"])
        self.assertTrue(command.startswith(f"pi --model {OVERRIDE} "))


class BriefTests(unittest.TestCase):
    def test_no_prompt_means_no_brief(self):
        self.assertIsNone(bake.load_brief())
        self.assertIsNone(bake.load_brief(None, None))

    def test_inline_prompt_is_kept_verbatim_without_a_path(self):
        brief = bake.load_brief(prompt=NASTY)
        self.assertIsNone(brief.path)
        self.assertEqual(brief.text, NASTY)

    def test_blank_inline_prompt_is_rejected(self):
        for prompt in ("", "   ", "\n\t"):
            with self.subTest(prompt=repr(prompt)):
                with self.assertRaisesRegex(RuntimeError, "prompt is empty"):
                    bake.load_brief(prompt=prompt)

    def test_prompt_file_is_resolved_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "my briefs"
            folder.mkdir()
            path = folder / "it's \"the\" brief.md"
            path.write_text(NASTY, encoding="utf-8")
            brief = bake.load_brief(prompt_file=str(path))
            self.assertEqual(brief.path, path.resolve())
            self.assertEqual(brief.text, NASTY)

    def test_prompt_file_expands_the_home_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "brief.md").write_text("Do the thing.\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                brief = bake.load_brief(prompt_file="~/brief.md")
            self.assertEqual(brief.path, (Path(tmp) / "brief.md").resolve())

    def test_missing_directory_empty_binary_and_unreadable_prompt_files_fail_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "prompt file not found"):
                bake.load_brief(prompt_file=str(root / "missing.md"))
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                bake.load_brief(prompt_file=str(root))
            empty = root / "empty.md"
            empty.write_text(" \n\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "prompt file is empty"):
                bake.load_brief(prompt_file=str(empty))
            zero = root / "zero.md"
            zero.touch()
            with self.assertRaisesRegex(RuntimeError, "prompt file is empty"):
                bake.load_brief(prompt_file=str(zero))
            binary = root / "image.png"
            binary.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00binary")
            with self.assertRaisesRegex(RuntimeError, "not UTF-8 text"):
                bake.load_brief(prompt_file=str(binary))
            if os.geteuid() != 0:
                locked = root / "locked.md"
                locked.write_text("secret plan\n", encoding="utf-8")
                locked.chmod(0)
                closed = root / "closed"
                closed.mkdir()
                (closed / "brief.md").write_text("plan\n", encoding="utf-8")
                closed.chmod(0)
                try:
                    with self.assertRaisesRegex(RuntimeError, "not readable"):
                        bake.load_brief(prompt_file=str(locked))
                    with self.assertRaisesRegex(RuntimeError, "not readable"):
                        bake.load_brief(prompt_file=str(closed / "brief.md"))
                finally:
                    locked.chmod(0o600)
                    closed.chmod(0o700)

    def test_stdin_is_kept_verbatim(self):
        text = "\n\nRead the ledger first.\nThen work.\n\n"
        with mock.patch.object(sys, "stdin", io.StringIO(text)):
            brief = bake.load_brief(prompt_file="-")
        self.assertIsNone(brief.path)
        self.assertEqual(brief.text, text)

    def test_empty_stdin_is_rejected(self):
        for text in ("", "  \n\t\n"):
            with self.subTest(text=repr(text)):
                with mock.patch.object(sys, "stdin", io.StringIO(text)):
                    with self.assertRaisesRegex(RuntimeError, "prompt file is empty"):
                        bake.load_brief(prompt_file="-")


class InputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.main = Path(self.tmp.name).resolve()
        self.worktree = self.main / ".local" / "worktrees" / "feature"
        self.folder = self.main / ".local" / "bake" / "feature"

    def test_input_dir_sits_beside_the_worktrees(self):
        self.assertEqual(bake.input_dir(self.main, "feature"), self.folder)

    def test_inline_brief_is_spooled_next_to_the_reporting_file(self):
        folder, files = bake.prepare_input(self.main, "feature", self.worktree, bake.Brief(None, NASTY))
        self.assertEqual(folder, self.folder)
        self.assertEqual(files, [self.folder / "brief.md", self.folder / "reporting.md"])
        self.assertEqual((self.folder / "brief.md").read_text(encoding="utf-8"), NASTY + "\n")
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["brief.md", "reporting.md"])

    def test_brief_with_trailing_newline_is_written_byte_for_byte(self):
        _, files = bake.prepare_input(self.main, "feature", self.worktree, bake.Brief(None, "Do it.\n"))
        self.assertEqual(files[0].read_text(encoding="utf-8"), "Do it.\n")

    def test_user_brief_is_referenced_not_copied_or_changed(self):
        brief = self.main / "plans" / "briefs" / "feature brief.md"
        brief.parent.mkdir(parents=True)
        brief.write_text(NASTY, encoding="utf-8")
        before = os.stat(brief)
        folder, files = bake.prepare_input(self.main, "feature", self.worktree, bake.Brief(brief, NASTY))
        self.assertEqual(files, [brief, self.folder / "reporting.md"])
        self.assertEqual(brief.read_text(encoding="utf-8"), NASTY)
        self.assertEqual(os.stat(brief).st_mtime_ns, before.st_mtime_ns)
        self.assertEqual([path.name for path in folder.iterdir()], ["reporting.md"])

    def test_reporting_file_uses_the_supplied_copy_with_this_bake_s_paths(self):
        _, files = bake.prepare_input(self.main, "feature", self.worktree, bake.Brief(None, "Do it."))
        text = files[1].read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Report to the originating chat\n\n"))
        for phrase in (
            "call bake_report with status completed, a short summary, and the\nevidence paths",
            "use status blocked and state the decision",
            "use status failed",
            "does\nnot authorize merging, publishing, or deploying",
            f"Use the monitor associated with this worktree (`{self.worktree}`)",
            "Do not guess a\ntask id or choose between ambiguous monitors",
            f"the result file `{self.folder / 'result.md'}`",
            "Do not claim that the originating chat was notified.",
        ):
            self.assertIn(phrase, text)
        self.assertIn(
            "If no monitor is registered, the reporting tool is unavailable, the report is rejected, or the tool says the"
            " originating chat will not be notified, save the outcome and evidence to the task's ledger or to the result"
            f" file `{self.folder / 'result.md'}` and state that delivery failed.",
            " ".join(text.split()),
        )
        self.assertNotIn("{", text)
        self.assertFalse((self.folder / "result.md").exists())

    def test_files_are_private_and_complete(self):
        folder, files = bake.prepare_input(self.main, "feature", self.worktree, bake.Brief(None, "Do it."))
        self.assertEqual(mode(folder), 0o700)
        self.assertEqual(mode(folder.parent), 0o700)
        for path in files:
            self.assertEqual(mode(path), 0o600)
        self.assertEqual([path.name for path in folder.iterdir() if path.name.startswith(".")], [])

    def test_leftover_input_dir_without_a_result_is_replaced(self):
        self.folder.mkdir(parents=True)
        (self.folder / "brief.md").write_text("stale brief\n", encoding="utf-8")
        (self.folder / "reporting.md").write_text("stale reporting\n", encoding="utf-8")
        brief = self.main / "brief.md"
        brief.write_text("fresh brief\n", encoding="utf-8")
        bake.prepare_input(self.main, "feature", self.worktree, bake.Brief(brief, "fresh brief\n"))
        self.assertEqual([path.name for path in self.folder.iterdir()], ["reporting.md"])
        self.assertIn("# Report to the originating chat", (self.folder / "reporting.md").read_text(encoding="utf-8"))

    def test_input_root_ignores_itself_and_an_existing_ignore_file_is_kept(self):
        folder, _ = bake.prepare_input(self.main, "feature", self.worktree, bake.Brief(None, "Do it."))
        ignore = folder.parent / ".gitignore"
        self.assertEqual(ignore.read_text(encoding="utf-8"), "*\n")
        self.assertEqual(mode(ignore), 0o600)
        self.assertEqual(sorted(path.name for path in folder.parent.iterdir()), [".gitignore", "feature"])
        ignore.write_text("brief.md\n", encoding="utf-8")
        bake.prepare_input(self.main, "second", self.main / ".local" / "worktrees" / "second", bake.Brief(None, "Do it."))
        self.assertEqual(ignore.read_text(encoding="utf-8"), "brief.md\n")
        self.assertEqual(sorted(path.name for path in folder.parent.iterdir()), [".gitignore", "feature", "second"])

    def test_saved_result_is_never_deleted_on_reuse(self):
        self.folder.mkdir(parents=True)
        result = self.folder / "result.md"
        result.write_text("delivery failed; outcome here\n", encoding="utf-8")
        (self.folder / "brief.md").write_text("earlier brief\n", encoding="utf-8")
        message = f"Saved worker result exists: {result}. Preserve it before removing or reusing this bake."
        with self.assertRaises(RuntimeError) as caught:
            bake.prepare_input(self.main, "feature", self.worktree, bake.Brief(None, "fresh brief\n"))
        self.assertEqual(str(caught.exception), message)
        with self.assertRaises(RuntimeError) as caught:
            bake.check_input(self.main, "feature")
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["brief.md", "result.md"])
        self.assertEqual(result.read_text(encoding="utf-8"), "delivery failed; outcome here\n")
        self.assertEqual((self.folder / "brief.md").read_text(encoding="utf-8"), "earlier brief\n")

    def test_brief_inside_the_input_dir_is_rejected_and_kept(self):
        self.folder.mkdir(parents=True)
        inside = self.folder / "brief.md"
        inside.write_text("reused brief\n", encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            bake.prepare_input(self.main, "feature", self.worktree, bake.Brief(inside, "reused brief\n"))
        self.assertEqual(
            str(caught.exception),
            f"Prompt file is inside the bake input directory: {inside}. Move it outside that directory before starting this bake.",
        )
        self.assertEqual(inside.read_text(encoding="utf-8"), "reused brief\n")
        self.assertEqual([path.name for path in self.folder.iterdir()], ["brief.md"])
        beside = self.folder.parent / "brief.md"
        beside.write_text("shared brief\n", encoding="utf-8")
        _, files = bake.prepare_input(self.main, "feature", self.worktree, bake.Brief(beside, "shared brief\n"))
        self.assertEqual(files, [beside, self.folder / "reporting.md"])
        self.assertEqual(beside.read_text(encoding="utf-8"), "shared brief\n")


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

    def folder(self, name):
        return self.main / ".local" / "bake" / name

    def test_no_window_prints_the_next_command_with_both_files(self):
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            output = run(bake.new, parse("new", "feature", "--prompt", "fix the thing", "--no-window"))
        folder = self.folder("feature")
        expected = f"pi --model {DEFAULT} @{folder}/brief.md @{folder}/reporting.md"
        self.assertIn(f"input:    {folder}\n", output)
        self.assertIn(f"next:     cd {self.worktree('feature')} && {expected}\n", output)
        self.assertEqual((folder / "brief.md").read_text(encoding="utf-8"), "fix the thing\n")
        self.assertEqual(self.fake.named("tmux"), [])
        self.assertIn(
            ["git", "worktree", "add", "-b", "petro/feature", str(self.worktree("feature")), "refs/remotes/origin/main"],
            self.fake.calls,
        )

    def test_outside_tmux_without_a_prompt_launches_bare_and_writes_nothing(self):
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            output = run(bake.new, parse("new", "feature"))
        self.assertIn(f"next:     cd {self.worktree('feature')} && pi --model {DEFAULT}\n", output)
        self.assertNotIn("input:", output)
        self.assertFalse((self.main / ".local" / "bake").exists())

    def test_no_window_prints_the_override(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.new, parse("new", "feature", "--model", OVERRIDE, "--no-window"))
        self.assertIn(f"&& pi --model {OVERRIDE}\n", output)
        self.assertEqual(self.fake.named("tmux"), [])

    def test_window_gets_one_launch_command_and_the_prompt_never_reaches_the_shell(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.new, parse("new", "feature", "-p", NASTY))
        folder = self.folder("feature")
        self.assertIn("window:   feature (@7) running pi\n", output)
        self.assertEqual(
            self.fake.named("tmux", "new-window"),
            [["tmux", "new-window", "-P", "-F", "#{window_id}", "-n", "feature", "-c", str(self.worktree("feature"))]],
        )
        sent = self.fake.named("tmux", "send-keys")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][:4] + sent[0][5:], ["tmux", "send-keys", "-t", "@7", "Enter"])
        self.assertEqual(shlex.split(sent[0][4]), ["pi", "--model", DEFAULT, f"@{folder}/brief.md", f"@{folder}/reporting.md"])
        self.assertNotIn("rm -rf", sent[0][4])
        self.assertEqual((folder / "brief.md").read_text(encoding="utf-8"), NASTY + "\n")

    def test_window_receives_the_override_with_the_user_s_brief_by_reference(self):
        brief = self.main / "plans" / "briefs" / "feature brief.md"
        brief.parent.mkdir(parents=True)
        brief.write_text("Read the ledger.\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.new, parse("new", "feature", "--model", OVERRIDE, "-P", str(brief)))
        sent = self.fake.named("tmux", "send-keys")
        self.assertEqual(len(sent), 1)
        self.assertEqual(shlex.split(sent[0][4]), ["pi", "--model", OVERRIDE, f"@{brief}", f"@{self.folder('feature')}/reporting.md"])
        self.assertIn(f"input:    {self.folder('feature')}\n", output)
        self.assertEqual(brief.read_text(encoding="utf-8"), "Read the ledger.\n")
        self.assertEqual([path.name for path in self.folder("feature").iterdir()], ["reporting.md"])

    def test_stdin_brief_is_spooled_into_the_input_dir(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            with mock.patch.object(sys, "stdin", io.StringIO("Read the ledger first.\nThen work.\n")):
                run(bake.new, parse("new", "feature", "-P", "-"))
        folder = self.folder("feature")
        sent = self.fake.named("tmux", "send-keys")[0][4]
        self.assertEqual(shlex.split(sent)[3:], [f"@{folder}/brief.md", f"@{folder}/reporting.md"])
        self.assertEqual((folder / "brief.md").read_text(encoding="utf-8"), "Read the ledger first.\nThen work.\n")

    def test_reporting_file_names_this_worktree(self):
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            run(bake.new, parse("new", "My Feature!", "-p", "Do it.", "--no-window"))
        folder = self.folder("My-Feature")
        text = (folder / "reporting.md").read_text(encoding="utf-8")
        self.assertIn(f"(`{self.worktree('My-Feature')}`)", text)
        self.assertIn(f"`{folder / 'result.md'}`", text)

    def test_blank_override_is_rejected_before_any_side_effect(self):
        for selector in ("", "  "):
            with self.subTest(selector=repr(selector)):
                with self.assertRaisesRegex(RuntimeError, "model selector is empty"):
                    bake.new(parse("new", "feature", "--model", selector, "-p", "Do it."))
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.main / ".local").exists())

    def test_bad_brief_input_is_rejected_before_any_side_effect(self):
        missing = self.main / "missing.md"
        empty = self.main / "empty.md"
        empty.write_text("\n", encoding="utf-8")
        cases = [
            (("-P", str(missing)), "prompt file not found"),
            (("-P", str(empty)), "prompt file is empty"),
            (("-P", str(self.main)), "not a regular file"),
            (("-p", "  "), "prompt is empty"),
        ]
        for extra, message in cases:
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(RuntimeError, message):
                    bake.new(parse("new", "feature", *extra))
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            with self.assertRaisesRegex(RuntimeError, "prompt file is empty"):
                bake.new(parse("new", "feature", "-P", "-"))
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.main / ".local").exists())

    def assert_nothing_created_or_launched(self):
        self.assertEqual(self.fake.named("git", "worktree", "add"), [])
        self.assertEqual(self.fake.named("tmux"), [])
        self.assertFalse((self.main / ".local" / "worktrees").exists())

    def test_saved_result_blocks_reusing_the_name_before_any_side_effect(self):
        folder = self.folder("feature")
        folder.mkdir(parents=True)
        result = folder / "result.md"
        result.write_text("delivery failed; outcome here\n", encoding="utf-8")
        message = f"Saved worker result exists: {result}. Preserve it before removing or reusing this bake."
        for extra in (("-p", "Do it again."), ()):
            with self.subTest(extra=extra):
                with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
                    with self.assertRaises(RuntimeError) as caught:
                        bake.new(parse("new", "feature", *extra))
                self.assertEqual(str(caught.exception), message)
        self.assert_nothing_created_or_launched()
        self.assertEqual([path.name for path in folder.iterdir()], ["result.md"])
        self.assertEqual(result.read_text(encoding="utf-8"), "delivery failed; outcome here\n")

    def test_brief_inside_the_input_dir_is_rejected_before_any_side_effect(self):
        inside = self.folder("feature") / "brief.md"
        inside.parent.mkdir(parents=True)
        inside.write_text("reused brief\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            with self.assertRaises(RuntimeError) as caught:
                bake.new(parse("new", "feature", "-P", str(inside)))
        self.assertEqual(
            str(caught.exception),
            f"Prompt file is inside the bake input directory: {inside}. Move it outside that directory before starting this bake.",
        )
        self.assert_nothing_created_or_launched()
        self.assertEqual(inside.read_text(encoding="utf-8"), "reused brief\n")

    def test_input_failure_is_reported_without_a_worktree_or_branch(self):
        blocker = self.main / ".local" / "bake"
        blocker.parent.mkdir()
        blocker.write_text("not a directory\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            with self.assertRaises(RuntimeError) as caught:
                bake.new(parse("new", "feature", "-p", "Do it."))
        self.assertEqual(str(caught.exception), f"Could not prepare worker input: File exists: {blocker}. No worker was started.")
        self.assertEqual(blocker.read_text(encoding="utf-8"), "not a directory\n")
        blocker.unlink()
        with mock.patch.object(bake, "write_private", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
                with self.assertRaises(RuntimeError) as caught:
                    bake.new(parse("new", "feature", "-p", "Do it."))
        self.assertEqual(str(caught.exception), "Could not prepare worker input: No space left on device. No worker was started.")
        self.assert_nothing_created_or_launched()
        self.assertEqual(list(self.folder("feature").iterdir()), [])
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            output = run(bake.new, parse("new", "feature", "-p", "Do it.", "--no-window"))
        self.assertIn(f"input:    {self.folder('feature')}\n", output)
        self.assertEqual(sorted(path.name for path in self.folder("feature").iterdir()), ["brief.md", "reporting.md"])
        self.assertEqual(len(self.fake.named("git", "worktree", "add")), 1)

    def status_checks(self):
        return self.fake.named("git", "status", "--porcelain", "--untracked-files=all")

    def test_with_changes_checks_the_input_files_before_creating_the_worktree(self):
        self.fake.tree_status = " M README.md\n"
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.new, parse("new", "feature", "-p", "Do it.", "--with-changes"))
        folder = self.folder("feature")
        self.assertIn("note:     moved uncommitted changes into the worktree\n", output)
        check = [
            "git", "status", "--porcelain", "--untracked-files=all", "--",
            ":(literal).local/bake/feature/brief.md", ":(literal).local/bake/feature/reporting.md",
        ]
        self.assertEqual(self.status_checks(), [check])
        self.assertEqual(self.fake.cwds[self.fake.calls.index(check)], str(self.main))
        add = self.fake.named("git", "worktree", "add")[0]
        push = ["git", "stash", "push", "-u", "-m", "bake/feature"]
        self.assertLess(self.fake.calls.index(check), self.fake.calls.index(add))
        self.assertLess(self.fake.calls.index(add), self.fake.calls.index(push))
        self.assertEqual(self.fake.named("git", "stash"), [push, ["git", "stash", "apply"], ["git", "stash", "drop"]])
        self.assertEqual((folder.parent / ".gitignore").read_text(encoding="utf-8"), "*\n")
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["brief.md", "reporting.md"])
        self.assertEqual(len(self.fake.named("tmux", "send-keys")), 1)

    def test_with_changes_refuses_before_any_side_effect_when_the_input_would_move(self):
        brief = self.main / "plans" / "brief.md"
        brief.parent.mkdir(parents=True)
        brief.write_text("Read the ledger.\n", encoding="utf-8")
        self.fake.tree_status = " M README.md\n?? plans/brief.md\n"
        self.fake.input_status = "?? plans/brief.md\n"
        message = "Bake input files must be ignored before using --with-changes. No changes were moved."
        for extra in (("-p", "Do it."), ("-P", str(brief))):
            with self.subTest(extra=extra):
                with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
                    with self.assertRaises(RuntimeError) as caught:
                        bake.new(parse("new", "feature", *extra, "--with-changes"))
                self.assertEqual(str(caught.exception), message)
                self.assertFalse(self.folder("feature").exists())
        self.assert_nothing_created_or_launched()
        self.assertEqual(self.fake.named("git", "stash"), [])
        self.assertEqual(len(self.status_checks()), 2)
        self.assertEqual(brief.read_text(encoding="utf-8"), "Read the ledger.\n")
        self.assertEqual((self.main / ".local" / "bake" / ".gitignore").read_text(encoding="utf-8"), "*\n")

    def test_with_changes_checks_only_the_input_files_inside_the_source_checkout(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        brief = Path(outside.name).resolve() / "brief.md"
        brief.write_text("Read the ledger.\n", encoding="utf-8")
        self.fake.tree_status = " M README.md\n"
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.new, parse("new", "feature", "-P", str(brief), "--with-changes"))
        self.assertIn("note:     moved uncommitted changes into the worktree\n", output)
        self.assertEqual(
            self.status_checks(),
            [["git", "status", "--porcelain", "--untracked-files=all", "--", ":(literal).local/bake/feature/reporting.md"]],
        )
        self.assertEqual(len(self.fake.named("git", "stash", "push")), 1)

    def test_with_changes_from_a_linked_worktree_skips_files_under_the_main_checkout(self):
        top = self.worktree("other")
        top.mkdir(parents=True)
        self.fake.top = str(top)
        self.fake.tree_status = " M README.md\n"
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.new, parse("new", "feature", "-p", "Do it.", "--with-changes"))
        self.assertIn("note:     moved uncommitted changes into the worktree\n", output)
        self.assertEqual(self.status_checks(), [])
        push = ["git", "stash", "push", "-u", "-m", "bake/feature"]
        self.assertEqual(self.fake.cwds[self.fake.calls.index(push)], str(top))
        self.assertEqual(sorted(path.name for path in self.folder("feature").iterdir()), ["brief.md", "reporting.md"])

    def test_bare_launch_with_changes_has_nothing_to_check(self):
        self.fake.tree_status = " M README.md\n"
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            output = run(bake.new, parse("new", "feature", "--with-changes"))
        self.assertIn("note:     moved uncommitted changes into the worktree\n", output)
        self.assertEqual(self.status_checks(), [])
        self.assertFalse((self.main / ".local" / "bake").exists())

    def test_failed_worktree_add_removes_only_the_input_files_bake_wrote(self):
        brief = self.main / "plans" / "brief.md"
        brief.parent.mkdir(parents=True)
        brief.write_text("Read the ledger.\n", encoding="utf-8")
        self.fake.failing = [["git", "worktree", "add"]]
        for extra in (("-p", "Do it."), ("-P", str(brief))):
            with self.subTest(extra=extra):
                with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
                    with self.assertRaisesRegex(RuntimeError, "fake command failed: git worktree add"):
                        bake.new(parse("new", "feature", *extra))
                self.assertFalse(self.folder("feature").exists())
        self.assertEqual(len(self.fake.named("git", "worktree", "add")), 2)
        self.assertEqual(self.fake.named("tmux"), [])
        self.assertEqual(brief.read_text(encoding="utf-8"), "Read the ledger.\n")


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


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.main = Path(self.tmp.name).resolve()
        self.path = self.main / ".local" / "worktrees" / "feature"
        self.path.mkdir(parents=True)
        self.folder = self.main / ".local" / "bake" / "feature"
        self.folder.mkdir(parents=True)
        (self.folder / "reporting.md").write_text("report\n", encoding="utf-8")
        self.fake = FakeInvoke(self.main)
        patcher = mock.patch.object(bake, "invoke", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rm_removes_the_input_dir_with_the_worktree(self):
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            output = run(bake.remove, parse("rm", "feature"))
        self.assertIn(f"removed:  {self.path}\n", output)
        self.assertIn(f"removed:  {self.folder}\n", output)
        self.assertFalse(self.folder.exists())
        self.assertTrue((self.main / ".local" / "bake").is_dir())
        self.assertIn(["git", "worktree", "remove", str(self.path)], self.fake.calls)

    def test_rm_without_an_input_dir_prints_one_removed_line(self):
        shutil.rmtree(self.folder)
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            output = run(bake.remove, parse("rm", "feature"))
        self.assertEqual(output.count("removed:"), 1)

    def test_done_removes_the_input_dir_too(self):
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            output = run(bake.done, parse("done", "feature"))
        self.assertFalse(self.folder.exists())
        self.assertIn("removed:", output)

    def test_saved_result_blocks_rm_and_done_before_any_side_effect(self):
        result = self.folder / "result.md"
        result.write_text("delivery failed; outcome here\n", encoding="utf-8")
        self.fake.windows = "feature\n"
        message = f"Saved worker result exists: {result}. Preserve it before removing or reusing this bake."
        for handler, argv in (
            (bake.remove, ("rm", "feature")),
            (bake.remove, ("rm", "feature", "--force")),
            (bake.done, ("done", "feature", "--force")),
        ):
            with self.subTest(argv=argv):
                with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
                    with self.assertRaises(RuntimeError) as caught:
                        run(handler, parse(*argv))
                self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.fake.named("tmux"), [])
        self.assertEqual(self.fake.named("git", "worktree", "remove"), [])
        self.assertEqual(self.fake.named("git", "branch"), [])
        self.assertTrue(self.path.is_dir())
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["reporting.md", "result.md"])
        self.assertEqual(result.read_text(encoding="utf-8"), "delivery failed; outcome here\n")
        result.rename(self.main / "saved-result.md")
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            output = run(bake.remove, parse("rm", "feature"))
        self.assertIn(f"removed:  {self.folder}\n", output)
        self.assertFalse(self.folder.exists())


if __name__ == "__main__":
    unittest.main()
