"""The whole chat app, end to end, with a scripted model and scripted input. No terminal needed."""

import io
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from agentx_dev.cli import app as app_module
from agentx_dev.cli.app import DEFAULT_DENY, ChatApp, build_parser, main
from agentx_dev.cli.approval import Answer, Gate
from agentx_dev.cli.models import ModelError
from agentx_dev.cli.sessions import list_sessions, sessions_dir
from tests.conftest import MockModel, make_final, make_react_response


@pytest.fixture(autouse=True)
def folder_is_trusted(monkeypatch):
    """``main`` asks whether to trust the folder; these tests are about what comes after (see test_cli_trust)."""
    monkeypatch.setattr(app_module, "ensure_trusted", lambda *args, **kwargs: True)


@pytest.fixture(autouse=True)
def keep_logging_as_it_was():
    """``main`` installs logging handlers (``setup_logging``); no test may leave them behind."""
    import logging
    loggers = (logging.getLogger(), logging.getLogger("agentx_dev"))
    before = [list(logger.handlers) for logger in loggers]
    yield
    for logger, handlers in zip(loggers, before):
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
                if getattr(handler, "_agentx_cli", False):
                    handler.close()


class Lines:
    """read_line answering from a list; an exception in the list is raised; EOFError when it runs out."""

    def __init__(self, *items):
        self.items = list(items)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.items:
            raise EOFError
        item = self.items.pop(0)
        if callable(item):                       # a side effect between two inputs (a file written, say)
            item()
            return self(prompt)
        if isinstance(item, BaseException):
            raise item
        return item


def make_app(tmp_path, lines, model, **kw):
    out = io.StringIO()
    console = Console(file=out, width=kw.pop("width", 100), force_terminal=False, color_system=None)
    cwd, home = tmp_path / "proj", tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    kw.setdefault("env", {})
    kw.setdefault("model_factory", lambda name, env: model)
    app = ChatApp(cwd=cwd, home=home, console=console, read_line=lines, **kw)
    return app, out, cwd, home


def write_call(path="hello.txt", content="hi\n"):
    return make_react_response("write_file", {"path": path, "content": content, "if_exists": "refuse"})


def seen_by(model):
    return " ".join(str(call[0]) for call in model.calls)


def all_seen_by(model):
    return " ".join(str(message) for call in model.calls for message in call)


# -- a whole turn ---------------------------------------------------------------------------------------------

def test_an_approved_edit_is_made_shown_and_saved(tmp_path):
    model = MockModel(script=[write_call(), make_final("Created hello.txt")])
    app, out, cwd, home = make_app(tmp_path, Lines("create hello.txt", "y", "/exit"), model)
    assert app.start() == 0
    assert (cwd / "hello.txt").read_text() == "hi\n"
    text = out.getvalue()
    assert "Create hello.txt" in text and "+hi" in text and "Created hello.txt" in text
    assert len(list_sessions(home, cwd)) == 1


def test_a_declined_edit_is_not_made_and_the_model_hears_why(tmp_path):
    model = MockModel(script=[write_call(), make_final("Understood, I did not write it")])
    app, out, cwd, _ = make_app(tmp_path, Lines("create hello.txt", "n", "too risky", "/exit"), model)
    app.start()
    assert not (cwd / "hello.txt").exists()
    assert "The user declined: too risky" in all_seen_by(model)


def test_plan_mode_refuses_without_asking(tmp_path):
    lines = Lines("create hello.txt", "/exit")
    model = MockModel(script=[write_call(), make_final("Here is my plan")])
    app, out, cwd, _ = make_app(tmp_path, lines, model, mode="plan")
    app.start()
    assert not (cwd / "hello.txt").exists()
    assert "Allow?" not in lines.prompts and "Here is my plan" in out.getvalue()


def test_mode_command_switches_to_accept_edits(tmp_path):
    lines = Lines("/mode accept-edits", "create hello.txt", "/exit")
    model = MockModel(script=[write_call(), make_final("done")])
    app, out, cwd, _ = make_app(tmp_path, lines, model)
    app.start()
    assert (cwd / "hello.txt").exists() and "Allow?" not in lines.prompts and "Mode: accept-edits" in out.getvalue()


# -- one folder, no cache, no timeouts ------------------------------------------------------------------------

def test_every_part_is_built_for_the_same_folder(tmp_path, monkeypatch):
    seen = {}
    real_asker, real_tools, real_addendum = app_module.make_asker, app_module.build_tools, app_module.build_addendum

    def spy_asker(console, read_line, cwd, select=None, helper=None, helper_models=None):
        seen["asker"] = cwd
        return real_asker(console, read_line, cwd, select=select, helper=helper, helper_models=helper_models)

    def spy_tools(cwd, add_dirs, gate, observer=None):
        seen["tools"], seen["tools_gate"] = cwd, gate
        return real_tools(cwd, add_dirs, gate, observer=observer)

    def spy_addendum(cwd, home, **kw):
        seen["addendum"] = cwd
        return real_addendum(cwd, home, **kw)

    monkeypatch.setattr(app_module, "make_asker", spy_asker)
    monkeypatch.setattr(app_module, "build_tools", spy_tools)
    monkeypatch.setattr(app_module, "build_addendum", spy_addendum)
    app, _, cwd, _ = make_app(tmp_path, Lines(), MockModel(script=[]))
    assert {app.cwd, app.gate.cwd, seen["asker"], seen["tools"], seen["addendum"]} == {cwd.resolve()}
    assert seen["tools_gate"] is app.gate


def test_the_tool_cache_is_off_and_nothing_has_a_timeout(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines("hi", "/exit"), MockModel(script=[make_final("ok")]))
    app.start()
    runner = app.session.runner
    assert runner.auto_cache is False
    assert runner.registry._default_timeout_sec is None
    assert all(getattr(tool, "timeout_sec", None) is None for tool in app.tools)
    assert runner.parallel_tool_workers == 1          # one approval prompt at a time, on the main thread


# -- the built-in deny rules ----------------------------------------------------------------------------------

PROTECTED = [".git", ".git/config", ".git/commondir", ".git/HEAD", ".git/hooks", ".git/hooks/pre-commit",
             ".git/hooks/sub/x", ".git/modules/s/hooks/x", ".git/modules/s/config", "sub/.git", "sub/.git/config",
             "sub/.git/hooks/pre-push", ".agentx", ".agentx/settings.json", ".agentx/new.json",
             "./.agentx/../.agentx/settings.json", "./.git/config", ".git\\config", "sub/../.git/config",
             "sub/../.git"]
if sys.platform == "win32":                    # a case-insensitive file system: other spellings of the same file
    PROTECTED += [".GIT/config", ".Git/hooks/pre-commit", ".AGENTX/settings.json"]


def check(gate, tool, **args):
    return gate.check(tool, args)


@pytest.mark.parametrize("path", PROTECTED)
def test_hooks_and_settings_cannot_be_written_edited_or_deleted_even_in_accept_edits(tmp_path, path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="accept-edits")
    gate = app.gate
    gate.ask = lambda request: pytest.fail("a protected path must be refused, not asked about")
    for tool, args in (("write_file", {"path": path, "content": "x"}),
                       ("edit_file", {"path": path, "old_string": "a", "new_string": "b"}),
                       ("delete_path", {"path": path})):
        refusal = gate.check(tool, args)
        assert refusal is not None and "deny rule" in refusal, (tool, path)


@pytest.mark.parametrize("path", PROTECTED)
def test_hooks_and_settings_cannot_be_moved_from_or_to(tmp_path, path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="accept-edits")
    app.gate.ask = lambda request: pytest.fail("a protected path must be refused, not asked about")
    for args in ({"source": path, "destination": "elsewhere.txt"}, {"source": "evil.txt", "destination": path}):
        refusal = app.gate.check("move_file", args)
        assert refusal is not None and "deny rule" in refusal, args


def test_the_absolute_path_of_a_protected_file_is_refused_too(tmp_path):
    app, _, cwd, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="accept-edits")
    app.gate.ask = lambda request: pytest.fail("must be refused")
    for relative in (".git/config", ".git/commondir", ".agentx/settings.json", ".git"):
        for form in (str(app.cwd / relative), str(cwd / relative), (app.cwd / relative).as_posix()):
            assert "deny rule" in app.gate.check("write_file", {"path": form, "content": "x"}), form
            assert "deny rule" in app.gate.check("move_file", {"source": "a", "destination": form}), form


def test_the_user_home_settings_folder_is_protected_when_the_tools_can_reach_it(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="accept-edits", add_dirs=[home])
    app.gate.ask = lambda request: pytest.fail("must be refused")
    for name in (".agentx/settings.json", ".agentx/AGENTX.md", ".agentx"):
        target = str(home.resolve() / name)
        assert "deny rule" in app.gate.check("write_file", {"path": target, "content": "x"}), name
        assert "deny rule" in app.gate.check("move_file", {"source": "a", "destination": target}), name


ORDINARY = ["src/app.py", ".gitignore", ".gitattributes", ".gitmodules", ".github/workflows/ci.yml", "git_notes.txt",
            "agentx.md", ".git_notes", "x.git", "docs/git/notes.md", "sub/legit.git/config", "sub/.gitkeep",
            ".agentx.md", ".agentx-old/x", "sub/.agentx/note.txt"]


def test_ordinary_files_are_still_editable_in_accept_edits(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="accept-edits")
    for path in ORDINARY:
        assert app.gate.check("write_file", {"path": path, "content": "x"}) is None, path
        assert app.gate.check("edit_file", {"path": path, "find": "a", "replace": "b"}) is None, path


def test_ordinary_files_can_be_moved_in_default_mode_with_an_ask(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]))
    asked = []
    app.gate.ask = lambda request: (asked.append(request.subject), Answer("y"))[1]
    for path in ORDINARY:
        assert app.gate.check("move_file", {"source": path, "destination": "elsewhere.txt"}) is None, path
    assert len(asked) == len(ORDINARY)


def tool_named(app, name):
    return next(tool for tool in app.tools if tool.name == name)


def git_project(cwd):
    (cwd / ".git" / "hooks").mkdir(parents=True)
    (cwd / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (cwd / ".git" / "modules" / "s" / "hooks").mkdir(parents=True)
    (cwd / "sub" / ".git").mkdir(parents=True)
    (cwd / "sub" / ".git" / "config").write_text("[core]\n", encoding="utf-8")


def test_the_tools_themselves_refuse_to_touch_git_internals_in_accept_edits(tmp_path):
    """Through the real tool set (the gate in front of the real tools), on real files."""
    app, _, cwd, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="accept-edits")
    app.gate.ask = lambda request: pytest.fail("a protected path must be refused, not asked about")
    git_project(cwd)
    call = lambda name, **args: tool_named(app, name).func(**args)
    cases = [
        ("write_file", {"path": ".git/commondir", "content": "../elsewhere"}),
        ("write_file", {"path": ".git/modules/s/hooks/x", "content": "evil"}),
        ("write_file", {"path": ".git/hooks/pre-commit", "content": "evil"}),
        ("write_file", {"path": "sub/.git/config", "content": "evil"}),
        ("edit_file", {"path": ".git/config", "find": "[core]", "replace": "[core]\nhooksPath=/x"}),
        ("delete_path", {"path": ".git", "recursive": True}),
        ("delete_path", {"path": ".git/config"}),
        ("move_file", {"source": ".git", "destination": "moved"}),
        ("move_file", {"source": "moved", "destination": ".git"}),
        ("move_file", {"source": ".git/hooks", "destination": "h"}),
    ]
    (cwd / "moved").mkdir()
    for name, args in cases:
        result = call(name, **args)
        assert "deny rule" in str(result), (name, args, result)
    assert not (cwd / ".git" / "commondir").exists() and not (cwd / ".git" / "hooks" / "pre-commit").exists()
    assert (cwd / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"
    assert (cwd / ".git").is_dir() and (cwd / "moved").is_dir() and not (cwd / "h").exists()


def test_a_git_file_cannot_be_overwritten(tmp_path):
    """A worktree or submodule has a ``.git`` FILE whose ``gitdir:`` line points git elsewhere."""
    app, _, cwd, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="accept-edits")
    app.gate.ask = lambda request: pytest.fail("must be refused")
    (cwd / ".git").write_text("gitdir: ../real/.git\n", encoding="utf-8")
    result = tool_named(app, "write_file").func(path=".git", content="gitdir: /elsewhere\n", if_exists="overwrite")
    assert "deny rule" in str(result) and (cwd / ".git").read_text(encoding="utf-8") == "gitdir: ../real/.git\n"


def test_ordinary_files_are_written_by_the_real_tools_in_accept_edits(tmp_path):
    app, _, cwd, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="accept-edits")
    for path in (".gitignore", ".github/workflows/ci.yml", "git_notes.txt"):
        result = tool_named(app, "write_file").func(path=path, content="x\n", if_exists="refuse")
        assert "deny rule" not in str(result) and (cwd / path).read_text(encoding="utf-8") == "x\n", path


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names exist on Windows only")
def test_a_short_8_3_name_for_the_git_folder_does_not_dodge_the_rule(tmp_path):
    import ctypes
    app, _, cwd, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="accept-edits")
    git_project(cwd)
    buffer = ctypes.create_unicode_buffer(500)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(cwd / ".git"), buffer, 500) or buffer.value.endswith(".git"):
        pytest.skip("this volume has no short names")
    short = Path(buffer.value).name
    app.gate.ask = lambda request: pytest.fail("must be refused")
    assert "deny rule" in str(tool_named(app, "write_file").func(path=f"{short}/commondir", content="x"))
    assert "deny rule" in str(tool_named(app, "write_file").func(path=f"{short}/config", content="x"))


def test_reading_the_settings_file_is_still_allowed(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]))
    assert app.gate.check("read_path", {"path": ".agentx/settings.json"}) is None


def test_the_defaults_are_added_to_the_users_deny_rules_not_replaced(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), deny=["run_shell:*sudo*"], mode="accept-edits")
    assert "run_shell:*sudo*" in app.gate.deny and set(DEFAULT_DENY) <= set(app.gate.deny)
    assert "deny rule" in app.gate.check("run_shell", {"command": "sudo ls"})


def test_a_model_cannot_install_a_hook_through_the_whole_loop(tmp_path):
    model = MockModel(script=[write_call(".git/hooks/pre-commit", "evil"), make_final("could not")])
    lines = Lines("install a hook", "/exit")
    app, out, cwd, _ = make_app(tmp_path, lines, model, mode="accept-edits")
    app.start()
    assert not (cwd / ".git").exists()
    assert "Allow?" not in lines.prompts and "deny rule" in all_seen_by(model)


def test_a_model_cannot_rewrite_its_own_settings_through_the_whole_loop(tmp_path):
    model = MockModel(script=[write_call(".agentx/settings.json", "{}"), make_final("could not")])
    app, _, cwd, _ = make_app(tmp_path, Lines("loosen permissions", "/exit"), model, mode="accept-edits")
    app.start()
    # Only the app's own work log is there (written by the app with Python, not through a tool).
    assert [p.name for p in (cwd / ".agentx").iterdir()] == ["log.md"] and "deny rule" in all_seen_by(model)


# -- startup warnings -----------------------------------------------------------------------------------------

def test_every_settings_problem_is_printed_before_the_first_prompt(tmp_path):
    lines = Lines("/exit")
    app, out, _, _ = make_app(tmp_path, lines, MockModel(script=[]),
                              problems=["settings file a.json: bad JSON; ignored", "second \x1b[31mproblem\x1b[0m"])
    app.start()
    text = out.getvalue()
    assert "bad JSON; ignored" in text and "second" in text
    assert "\x1b" not in text and "\\x1b" in text                  # control characters defused


def test_project_allow_rules_are_listed_as_coming_from_the_project(tmp_path):
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]),
                              project_allow=["run_shell:curl*", "write_file:*\x07evil"])
    app.start()
    text = out.getvalue()
    assert "project" in text.lower() and "run_shell:curl*" in text and "write_file:*\\x07evil" in text
    assert "\x07" not in text
    assert text.index("run_shell:curl*") < text.index("Type / to see")     # before the prompt


def test_no_warnings_block_when_there_is_nothing_to_warn_about(tmp_path):
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]))
    app.start()
    assert "project's settings" not in out.getvalue()


def test_warnings_are_shown_even_when_the_model_cannot_be_built(tmp_path):
    def refuse(name, env):
        raise ModelError("No API key found: set ANTHROPIC_API_KEY.")
    app, out, _, _ = make_app(tmp_path, Lines(), None, model_factory=refuse, project_allow=["run_shell:make*"])
    assert app.start() == 1 and "run_shell:make*" in out.getvalue()


# -- Ctrl-C, exit, errors -------------------------------------------------------------------------------------

def test_ctrl_c_at_the_prompt_keeps_going_and_ctrl_d_leaves(tmp_path):
    model = MockModel(script=[make_final("hi")])
    app, out, _, _ = make_app(tmp_path, Lines(KeyboardInterrupt(), "hello"), model)
    assert app.start() == 0 and "hi" in out.getvalue() and "/exit" in out.getvalue()


def test_ctrl_c_during_a_turn_cancels_it_and_the_next_one_works(tmp_path):
    calls = []

    def script(messages):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return make_final("second answer")

    app, out, _, _ = make_app(tmp_path, Lines("first", "second", "/exit"), MockModel(script=script))
    assert app.start() == 0
    assert "Turn interrupted" in out.getvalue() and "second answer" in out.getvalue()


def test_ctrl_c_at_an_approval_prompt_only_declines_that_call(tmp_path):
    model = MockModel(script=[write_call(), make_final("ok, not written")])
    lines = Lines("create hello.txt", KeyboardInterrupt(), "/exit")
    app, out, cwd, _ = make_app(tmp_path, lines, model)
    assert app.start() == 0
    assert not (cwd / "hello.txt").exists()
    assert "ok, not written" in out.getvalue() and "Turn interrupted" not in out.getvalue()


def test_ctrl_c_inside_a_command_is_handled_and_the_loop_goes_on(tmp_path, monkeypatch):
    real = app_module.run_command
    state = {"n": 0}

    def flaky(ctx, line):
        if line == "/mode" and state["n"] == 0:
            state["n"] += 1
            raise KeyboardInterrupt
        return real(ctx, line)

    monkeypatch.setattr(app_module, "run_command", flaky)
    model = MockModel(script=[make_final("still here")])
    app, out, _, _ = make_app(tmp_path, Lines("/mode", "hello", "/exit"), model)
    assert app.start() == 0 and "still here" in out.getvalue()


def test_system_exit_inside_a_command_leaves_cleanly_and_saves(tmp_path, monkeypatch):
    def leave(ctx, line):
        raise SystemExit(0)

    monkeypatch.setattr(app_module, "run_command", leave)
    model = MockModel(script=[])
    app, _, cwd, home = make_app(tmp_path, Lines("/anything", "never read"), model)
    assert app.start() == 0 and model.calls == []


def test_exit_leaves_and_later_lines_are_never_read(tmp_path):
    lines = Lines("/exit", "never read")
    app, _, _, _ = make_app(tmp_path, lines, MockModel(script=[]))
    assert app.start() == 0 and lines.items == ["never read"]


def test_an_unknown_slash_command_is_never_sent_to_the_model(tmp_path):
    model = MockModel(script=[make_final("should not happen")])
    app, out, _, _ = make_app(tmp_path, Lines("/frobnicate now", "/exit"), model)
    app.start()
    assert model.calls == [] and "Unknown command" in out.getvalue()


def test_a_model_error_is_shown_in_one_line_and_the_session_continues(tmp_path):
    calls = []

    def script(messages):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("rate limited")
        return make_final("recovered")

    app, out, _, _ = make_app(tmp_path, Lines("first", "second", "/exit"), MockModel(script=script))
    app.start()
    text = out.getvalue()
    assert "Something went wrong (RuntimeError): rate limited" in text and "recovered" in text
    assert "Traceback" not in text


def test_an_error_message_is_defused_and_never_shows_the_api_key(tmp_path):
    key = "sk-ant-api03-SECRETSECRETSECRET"

    def script(messages):
        raise RuntimeError(f"401 invalid key {key}\nline two \x1b[31mred")

    app, out, _, _ = make_app(tmp_path, Lines("go", "/exit"), MockModel(script=script),
                              env={"ANTHROPIC_API_KEY": key})
    app.start()
    text = out.getvalue()
    assert key not in text and "SECRETSECRET" not in text and "\x1b" not in text
    assert "Something went wrong (RuntimeError)" in text
    assert len([line for line in text.splitlines() if "Something went wrong" in line]) == 1


def test_a_failing_save_does_not_end_the_session(tmp_path, monkeypatch):
    model = MockModel(script=[make_final("one"), make_final("two")])
    app, out, _, _ = make_app(tmp_path, Lines("a", "b", "/exit"), model)
    monkeypatch.setattr(app_module.Session, "save", lambda self, path: (_ for _ in ()).throw(OSError("disk full")))
    assert app.start() == 0 and "two" in out.getvalue() and "Could not save" in out.getvalue()


# -- sessions -------------------------------------------------------------------------------------------------

def test_continue_resumes_the_latest_conversation(tmp_path):
    first = MockModel(script=[make_final("noted: blue")])
    app, _, cwd, home = make_app(tmp_path, Lines("my favourite colour is blue", "/exit"), first)
    app.start()
    second = MockModel(script=[make_final("it was blue")])
    app2, out2, _, _ = make_app(tmp_path, Lines("what colour did I say?", "/exit"), second)
    assert app2.start(resume="latest") == 0
    assert "my favourite colour is blue" in all_seen_by(second)
    assert len(list_sessions(home, cwd)) == 1            # the same file was continued


def test_clear_starts_a_new_session_file(tmp_path):
    model = MockModel(script=[make_final("one"), make_final("two")])
    app, _, cwd, home = make_app(tmp_path, Lines("first", "/clear", "second", "/exit"), model)
    app.start()
    assert len(list_sessions(home, cwd)) == 2


def test_clear_forgets_the_always_allow_grants(tmp_path):
    lines = Lines("/clear", "/exit")
    app, _, _, _ = make_app(tmp_path, lines, MockModel(script=[]))
    app.gate.grants.add("pytest")
    app.start()
    assert app.gate.grants == set()


def test_a_command_granted_for_the_session_asks_again_after_clear(tmp_path):
    shell = make_react_response("run_shell", {"command": "echo hi"})
    model = MockModel(script=[shell, make_final("ran"), shell, make_final("ran again")])
    lines = Lines("run tests", "a", "/clear", "run tests again", "n", "", "/exit")
    app, out, _, _ = make_app(tmp_path, lines, model)
    answers = []
    real_ask = app.gate.ask
    app.gate.ask = lambda request: (answers.append(request.subject), real_ask(request))[1]
    app.start()
    assert answers == ["echo hi", "echo hi"]                       # asked again after /clear


def test_resume_also_forgets_the_grants(tmp_path):
    model = MockModel(script=[make_final("saved")])
    app, _, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), model)
    app.start()
    app2, _, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), read_short=Lines("1"))
    app2.start()
    app2.gate.grants.add("git status")
    app2.resume(None)
    assert app2.gate.grants == set()
    app2.gate.grants.add("git status")
    app2.resume(list_sessions(home, cwd)[0].session_id[:8])
    assert app2.gate.grants == set()


def test_resume_with_an_id_that_cannot_be_used_exits_one(tmp_path):
    model = MockModel(script=[make_final("ok")])
    app, out, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), model)
    folder = sessions_dir(home, cwd)
    folder.mkdir(parents=True)
    (folder / "20261010T000000Z-deadbeef.json").write_text("{broken", encoding="utf-8")   # skipped when listing
    assert app.start(resume="deadbeef") == 1
    assert "No saved session" in out.getvalue()


def test_a_session_that_loads_but_is_invalid_starts_a_fresh_one(tmp_path):
    model = MockModel(script=[make_final("ok")])
    app, out, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), model)
    folder = sessions_dir(home, cwd)
    folder.mkdir(parents=True)
    # valid JSON that lists as a session but cannot be loaded as one (history is not a list)
    (folder / "20261010T000000Z-cafebabe.json").write_text(
        '{"session_id": "cafebabe-x", "updated_at": "9", "version": 1, "history": "not a list"}', encoding="utf-8")
    assert app.start(resume="latest") == 0
    text = out.getvalue()
    assert "Could not read" in text and "ok" in text and "Traceback" not in text
    assert "not a list" not in text                                     # file content is not echoed back


def test_resume_command_with_a_bad_file_reports_and_starts_fresh(tmp_path):
    model = MockModel(script=[make_final("fine")])
    app, out, cwd, home = make_app(tmp_path, Lines("/resume cafebabe", "hello", "/exit"), model)
    folder = sessions_dir(home, cwd)
    folder.mkdir(parents=True)
    (folder / "20261010T000000Z-cafebabe.json").write_text(
        '{"session_id": "cafebabe-x", "updated_at": "9", "version": 1, "history": "no"}', encoding="utf-8")
    assert app.start() == 0 and "Could not read" in out.getvalue() and "fine" in out.getvalue()
    assert "Resumed" not in out.getvalue()                 # it did not resume anything


def test_no_api_key_exits_one_with_the_instruction(tmp_path):
    def refuse(name, env):
        raise ModelError("No API key found: set ANTHROPIC_API_KEY or OPENAI_API_KEY.")
    out = io.StringIO()
    app = ChatApp(cwd=tmp_path, home=tmp_path / "h", console=Console(file=out, color_system=None),
                  read_line=Lines(), model_factory=refuse, env={})
    assert app.start() == 1 and "ANTHROPIC_API_KEY" in out.getvalue()


# -- models ---------------------------------------------------------------------------------------------------

def test_model_command_switches_and_keeps_the_conversation(tmp_path):
    model = MockModel(script=[make_final("one")])
    other = MockModel(script=[make_final("two")])
    models = {"first": model, "other": other}
    app, out, _, _ = make_app(tmp_path, Lines("hello", "/model other", "again", "/exit"), None, model_name="first",
                              model_factory=lambda name, env: models[name])
    app.start()
    assert "hello" in all_seen_by(other) and app.model is other


def test_a_model_switch_that_fails_keeps_the_old_model(tmp_path):
    model = MockModel(script=[make_final("still the old one")])

    def factory(name, env):
        if name == "bad":
            raise ModelError("No API key for gpt: set the OPENAI_API_KEY environment variable.")
        return model

    app, out, _, _ = make_app(tmp_path, Lines("/model bad", "hello", "/exit"), None, model_factory=factory)
    assert app.start() == 0
    assert app.model is model and "OPENAI_API_KEY" in out.getvalue() and "still the old one" in out.getvalue()


def test_init_sends_the_drafting_prompt(tmp_path):
    model = MockModel(script=[make_final("I drafted it")])
    app, out, _, _ = make_app(tmp_path, Lines("/init", "/exit"), model)
    app.start()
    assert "AGENTX.md" in all_seen_by(model)


def test_toolbar_names_the_model_mode_and_folder(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]))
    app.start()
    bar = app.toolbar()
    assert "default" in bar and str(app.cwd) in bar
    app.cycle_mode()
    assert "accept-edits" in app.toolbar()


# -- the command line -----------------------------------------------------------------------------------------

class TestMain:
    def test_version_and_help_work_without_a_terminal(self, capsys):
        with pytest.raises(SystemExit) as info:
            main(["--version"])
        assert info.value.code == 0 and capsys.readouterr().out.startswith("agentx ")
        with pytest.raises(SystemExit) as info:
            main(["--help"])
        assert info.value.code == 0 and "--add-dir" in capsys.readouterr().out

    def test_a_bad_flag_is_a_usage_error(self, capsys):
        with pytest.raises(SystemExit) as info:
            main(["--mode", "yolo"])
        assert info.value.code == 2

    def test_continue_and_resume_exclude_each_other(self):
        with pytest.raises(SystemExit) as info:
            main(["--continue", "--resume"])
        assert info.value.code == 2

    def test_no_terminal_exits_one_with_a_message(self, monkeypatch, capsys):
        monkeypatch.setattr(app_module, "is_terminal", lambda: False)
        assert main([]) == 1
        captured = capsys.readouterr()
        assert "interactive terminal" in captured.err and "Traceback" not in captured.err

    def test_piped_stdin_in_a_real_process_is_one_message_and_exit_one(self, tmp_path):
        done = subprocess.run([sys.executable, "-m", "agentx_dev"], input="hi\n", capture_output=True, text=True,
                              cwd=tmp_path, env={**__import__("os").environ, "PYTHONPATH": str(Path.cwd())},
                              timeout=120)
        assert done.returncode == 1
        assert "interactive terminal" in done.stderr and "Traceback" not in done.stderr

    def test_the_parser_reads_every_flag(self):
        args = build_parser().parse_args(
            ["--model", "gpt-4o", "--mode", "plan", "--add-dir", "a", "--add-dir", "b", "--resume"])
        assert (args.model, args.mode, args.add_dir, args.resume) == ("gpt-4o", "plan", ["a", "b"], "pick")
        assert build_parser().parse_args(["--resume", "abcd1234"]).resume == "abcd1234"
        assert build_parser().parse_args(["--continue"]).continue_ is True

    def test_there_is_no_debug_flag(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--debug"])

    def test_main_wires_settings_flags_and_warnings_into_the_app(self, tmp_path, monkeypatch):
        proj, home = tmp_path / "proj", tmp_path / "home"
        (proj / ".agentx").mkdir(parents=True)
        home.mkdir()
        (proj / ".agentx" / "settings.json").write_text(
            '{"model": "gpt-4o", "mode": "plan", "permissions": {"allow": ["run_shell:make*"], '
            '"deny": ["run_shell:*sudo*"]}, "addDirs": ["../outside"]}', encoding="utf-8")
        (home / ".agentx").mkdir()
        (home / ".agentx" / "settings.json").write_text("{not json", encoding="utf-8")
        captured = {}

        class FakeApp:
            def __init__(self, **kw):
                captured.update(kw)

            def start(self, resume=None):
                captured["resume"] = resume
                return 7

        class FakePrompt:
            def __init__(self, **kw):
                self.read = self.read_short = lambda prompt="": ""
                self.select = lambda *args, **kwargs: None

        monkeypatch.setattr(app_module, "is_terminal", lambda: True)
        monkeypatch.setattr(app_module, "ChatApp", FakeApp)
        monkeypatch.setattr(app_module, "PromptInput", FakePrompt)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.chdir(proj)
        assert main(["--add-dir", "extra", "--continue"]) == 7
        assert captured["cwd"] == proj.resolve() and captured["home"] == home
        assert captured["model_name"] == "gpt-4o" and captured["mode"] == "plan"
        assert captured["model_source"] == proj / ".agentx" / "settings.json"
        assert captured["resume"] == "latest"
        assert captured["allow"] == ["run_shell:make*"] and captured["project_allow"] == ["run_shell:make*"]
        assert captured["deny"] == ["run_shell:*sudo*"]
        assert captured["add_dirs"] == [(proj / "extra").resolve()]
        assert any("settings.json" in problem for problem in captured["problems"])        # the user's broken file
        assert any("outside the project" in problem for problem in captured["problems"])

    def test_a_flag_beats_the_settings(self, tmp_path, monkeypatch):
        proj, home = tmp_path / "proj", tmp_path / "home"
        (proj / ".agentx").mkdir(parents=True)
        home.mkdir()
        (proj / ".agentx" / "settings.json").write_text('{"model": "gpt-4o", "mode": "plan"}', encoding="utf-8")
        captured = {}

        class FakeApp:
            def __init__(self, **kw):
                captured.update(kw)

            def start(self, resume=None):
                return 0

        class FakePrompt:
            def __init__(self, **kw):
                self.read = self.read_short = lambda prompt="": ""
                self.select = lambda *args, **kwargs: None

        monkeypatch.setattr(app_module, "is_terminal", lambda: True)
        monkeypatch.setattr(app_module, "ChatApp", FakeApp)
        monkeypatch.setattr(app_module, "PromptInput", FakePrompt)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.chdir(proj)
        main(["--model", "claude-sonnet-4-6", "--mode", "accept-edits"])
        assert captured["model_name"] == "claude-sonnet-4-6" and captured["mode"] == "accept-edits"
        assert captured["model_source"] is None                    # the flag named it, not a file
        # main writes no settings file of its own: the app does, once the model is built (ChatApp.start)
        assert not (home / ".agentx" / "settings.json").exists()

    def test_a_crash_while_starting_up_is_one_line_not_a_traceback(self, monkeypatch, capsys):
        monkeypatch.setattr(app_module, "is_terminal", lambda: True)
        monkeypatch.setattr(app_module, "load_settings", lambda cwd, home: (_ for _ in ()).throw(OSError("boom")))
        assert main([]) == 1
        err = capsys.readouterr().err
        assert "OSError" in err and "boom" in err and "Traceback" not in err


# -- fix round 1 ----------------------------------------------------------------------------------------------

def test_a_model_cannot_swing_git_to_another_folder_through_the_whole_loop(tmp_path):
    model = MockModel(script=[write_call(".git/commondir", "../evil"), make_final("could not")])
    lines = Lines("redirect git", "/exit")
    app, out, cwd, _ = make_app(tmp_path, lines, model, mode="accept-edits")
    (cwd / ".git").mkdir()
    app.start()
    assert not (cwd / ".git" / "commondir").exists() and "Allow?" not in lines.prompts
    assert "deny rule" in all_seen_by(model)


def test_always_allow_for_edits_does_not_survive_a_new_conversation(tmp_path):
    model = MockModel(script=[write_call("a.txt"), make_final("one"), write_call("b.txt"), make_final("two")])
    lines = Lines("make a", "a", "/clear", "make b", "n", "", "/exit")
    app, out, cwd, _ = make_app(tmp_path, lines, model)
    app.start()
    assert (cwd / "a.txt").exists() and not (cwd / "b.txt").exists()
    assert lines.prompts.count("Allow? [y/a/n] ") == 2           # asked again after /clear
    assert app.gate.mode == "default"
    assert "Started a new conversation" in out.getvalue() and "mode: default" in out.getvalue()


def test_a_mode_chosen_with_the_mode_command_survives_a_new_conversation(tmp_path):
    model = MockModel(script=[write_call("a.txt"), make_final("one"), write_call("b.txt"), make_final("two")])
    lines = Lines("make a", "a", "/mode accept-edits", "/clear", "make b", "/exit")
    app, _, cwd, _ = make_app(tmp_path, lines, model)
    app.start()
    assert (cwd / "b.txt").exists() and app.gate.mode == "accept-edits"
    assert lines.prompts.count("Allow? [y/a/n] ") == 1


def test_a_mode_cycled_with_shift_tab_survives_a_new_conversation(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), read_short=Lines("a"))
    app.start()
    app.gate.check("write_file", {"path": "a.txt", "content": "x", "if_exists": "refuse"})   # answers "a"
    assert app.gate.mode == "accept-edits"
    app.cycle_mode()
    assert app.gate.mode == "plan"
    app.new_conversation()
    assert app.gate.mode == "plan"


def test_the_startup_mode_is_kept_by_a_new_conversation(tmp_path):
    model = MockModel(script=[write_call("a.txt"), make_final("one")])
    lines = Lines("/clear", "make a", "/exit")
    app, _, cwd, _ = make_app(tmp_path, lines, model, mode="accept-edits")
    app.start()
    assert (cwd / "a.txt").exists() and "Allow?" not in lines.prompts and app.gate.mode == "accept-edits"


def test_resume_also_restores_the_startup_mode(tmp_path):
    first = MockModel(script=[make_final("saved")])
    app1, _, _, _ = make_app(tmp_path, Lines("hello", "/exit"), first)
    app1.start()
    ident = app1.session.session_id[:8]
    model = MockModel(script=[write_call("a.txt"), make_final("one")])
    app2, out, _, _ = make_app(tmp_path, Lines("make a", "a", f"/resume {ident}", "/exit"), model)
    app2.start()
    assert app2.gate.mode == "default" and f"Resumed {ident}" in out.getvalue() and "mode: default" in out.getvalue()


def test_agentx_md_written_during_the_session_is_picked_up_by_clear_and_resume(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    write_rules = lambda: (cwd / "AGENTX.md").write_text("NEWRULE always use tabs", encoding="utf-8")
    model = MockModel(script=[make_final("one"), make_final("two")])
    app, _, _, _ = make_app(tmp_path, Lines("first", write_rules, "/clear", "second", "/exit"), model)
    app.start()
    assert "NEWRULE" not in str(model.calls[0]) and "NEWRULE" in str(model.calls[1])
    assert "<project-instructions>" in str(model.calls[1])
    # and /resume
    ident = app.session.session_id[:8]
    (cwd / "AGENTX.md").write_text("RESUMERULE be brief", encoding="utf-8")
    model2 = MockModel(script=[make_final("again")])
    app2, _, _, _ = make_app(tmp_path, Lines(f"/resume {ident}", "go on", "/exit"), model2)
    app2.start()
    assert "RESUMERULE" in str(model2.calls[0])


def test_the_extra_folders_are_resolved_listed_and_checked(tmp_path):
    (tmp_path / "shared").mkdir()
    app, out, cwd, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), width=400,
                                add_dirs=[tmp_path / "proj" / ".." / "shared", tmp_path / "missing"])
    app.start()
    text = out.getvalue()
    assert app.add_dirs == [(tmp_path / "shared").resolve(), (tmp_path / "missing").resolve()]
    assert str((tmp_path / "shared").resolve()) in text and ".." not in text
    assert "missing" in text and "does not exist" in text
    assert not (tmp_path / "missing").exists()                          # never created


@pytest.mark.parametrize("which, word", [("root", "filesystem root"), ("home", "home folder")])
def test_a_root_or_home_extra_folder_gets_a_loud_warning(tmp_path, which, word):
    home = tmp_path / "home"
    home.mkdir()
    folder = Path(tmp_path.anchor) if which == "root" else home
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), add_dirs=[folder], width=300)
    loud = []
    app.screen.error = lambda text: loud.append(text)                    # the bold-red channel
    app.start()
    assert any(word in text for text in loud), loud


def test_an_ordinary_extra_folder_gets_no_loud_warning(tmp_path):
    (tmp_path / "shared").mkdir()
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), add_dirs=[tmp_path / "shared"])
    loud = []
    app.screen.error = lambda text: loud.append(text)
    app.start()
    assert loud == []


def _exits_with(code):
    def run(ctx, line):
        raise SystemExit(code)
    return run


def test_a_command_that_exits_with_a_message_exits_one_and_shows_it(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "run_command", _exits_with("bad \x1b thing"))
    app, out, _, _ = make_app(tmp_path, Lines("/anything"), MockModel(script=[]))
    assert app.start() == 1 and "bad \\x1b thing" in out.getvalue()


@pytest.mark.parametrize("code, expected", [(None, 0), (0, 0), (3, 3)])
def test_a_command_exit_code_is_used(tmp_path, monkeypatch, code, expected):
    monkeypatch.setattr(app_module, "run_command", _exits_with(code))
    app, _, _, _ = make_app(tmp_path, Lines("/anything"), MockModel(script=[]))
    assert app.start() == expected


def test_an_interrupted_turn_says_that_changes_made_stay_on_disk(tmp_path):
    def script(messages):
        raise KeyboardInterrupt

    app, out, _, _ = make_app(tmp_path, Lines("go", "/exit"), MockModel(script=script), width=300)
    app.start()
    assert "remain on disk" in out.getvalue()


class TestMainRoundOne:
    def test_ctrl_c_during_setup_is_one_quiet_line_and_130(self, monkeypatch, capsys):
        def interrupted(cwd, home):
            raise KeyboardInterrupt

        monkeypatch.setattr(app_module, "is_terminal", lambda: True)
        monkeypatch.setattr(app_module, "load_settings", interrupted)
        assert main([]) == 130
        err = capsys.readouterr().err
        assert "Traceback" not in err and len(err.strip().splitlines()) == 1

    def test_a_startup_failure_never_prints_the_api_key(self, monkeypatch, capsys):
        key = "sk-ant-api03-ZZZZSECRETVALUE"
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)

        def broken(cwd, home):
            raise OSError(f"bad {key} \x1b[31m")

        monkeypatch.setattr(app_module, "is_terminal", lambda: True)
        monkeypatch.setattr(app_module, "load_settings", broken)
        assert main([]) == 1
        err = capsys.readouterr().err
        assert key not in err and "SECRETVALUE" not in err and "\x1b" not in err and "OSError" in err

    def test_extra_folders_from_the_flag_are_resolved(self, tmp_path, monkeypatch):
        captured = {}

        class FakeApp:
            def __init__(self, **kw):
                captured.update(kw)

            def start(self, resume=None):
                return 0

        class FakePrompt:
            def __init__(self, **kw):
                self.read = self.read_short = lambda prompt="": ""
                self.select = lambda *args, **kwargs: None

        proj = tmp_path / "proj"
        (tmp_path / "shared").mkdir()
        proj.mkdir()
        monkeypatch.setattr(app_module, "is_terminal", lambda: True)
        monkeypatch.setattr(app_module, "ChatApp", FakeApp)
        monkeypatch.setattr(app_module, "PromptInput", FakePrompt)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        monkeypatch.chdir(proj)
        main(["--add-dir", "../shared", "--add-dir", str(tmp_path / "proj" / ".." / "shared")])
        assert captured["add_dirs"] == [(tmp_path / "shared").resolve()] * 2


# -- polish round ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("startup", ["accept-edits", "plan"])
def test_the_mode_in_force_before_an_a_answer_comes_back_on_clear(tmp_path, startup):
    """--mode X, /mode default, a, /clear => default (the mode the user had when they answered), not X."""
    model = MockModel(script=[write_call("a.txt"), make_final("one"), write_call("b.txt"), make_final("two")])
    lines = Lines("/mode default", "make a", "a", "/clear", "make b", "n", "", "/exit")
    app, _, cwd, _ = make_app(tmp_path, lines, model, mode=startup)
    app.start()
    assert (cwd / "a.txt").exists() and not (cwd / "b.txt").exists()
    assert lines.prompts.count("Allow? [y/a/n] ") == 2 and app.gate.mode == "default"


def test_the_saved_mode_is_used_by_resume_too(tmp_path):
    first = MockModel(script=[make_final("saved")])
    app1, _, _, _ = make_app(tmp_path, Lines("hello", "/exit"), first)
    app1.start()
    ident = app1.session.session_id[:8]
    model = MockModel(script=[write_call("a.txt"), make_final("one")])
    app2, _, _, _ = make_app(tmp_path, Lines("/mode default", "make a", "a", f"/resume {ident}", "/exit"), model,
                             mode="plan")
    app2.start()
    assert app2.gate.mode == "default"


def test_a_mode_chosen_after_the_a_answer_is_kept_by_clear(tmp_path):
    model = MockModel(script=[write_call("a.txt"), make_final("one")])
    lines = Lines("/mode default", "make a", "a", "/mode plan", "/clear", "/exit")
    app, _, _, _ = make_app(tmp_path, lines, model, mode="accept-edits")
    app.start()
    assert app.gate.mode == "plan"


HOME_FILES = [".gitconfig", ".config/git", ".config/git/config", ".config/git/ignore", ".bashrc", ".bash_profile",
              ".zshrc", ".profile", ".zprofile", ".bash_login", ".config/fish/config.fish", ".agentx/settings.json"]


def test_git_config_and_shell_profiles_in_a_reachable_home_are_protected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="accept-edits", add_dirs=[home])
    app.gate.ask = lambda request: pytest.fail("must be refused")
    for name in HOME_FILES:
        target = str(home.resolve() / name)
        for tool, args in (("write_file", {"path": target, "content": "x"}),
                           ("edit_file", {"path": target, "find": "a", "replace": "b"}),
                           ("delete_path", {"path": target}),
                           ("move_file", {"source": target, "destination": "x.txt"}),
                           ("move_file", {"source": "x.txt", "destination": target})):
            assert "deny rule" in str(app.gate.check(tool, args)), (tool, name)
    for name in (".bashrc.bak", ".config/other/x", ".gitconfig.txt", ".config/gitx", "notes.txt"):
        assert app.gate.check("write_file", {"path": str(home.resolve() / name), "content": "x"}) is None, name


def test_the_same_files_are_protected_when_the_project_is_the_home_folder(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    out = io.StringIO()
    app = ChatApp(cwd=home, home=home, console=Console(file=out, color_system=None), read_line=Lines(),
                  mode="accept-edits", env={})
    app.gate.ask = lambda request: pytest.fail("must be refused")
    for name in HOME_FILES:
        assert "deny rule" in str(app.gate.check("write_file", {"path": name, "content": "x"})), name
        assert "deny rule" in str(app.gate.check("move_file", {"source": "x.txt", "destination": name})), name


def test_the_home_warning_says_what_is_not_protected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), add_dirs=[home], width=400)
    loud = []
    app.screen.error = lambda text: loud.append(text)
    app.start()
    assert any("not protected" in text and "run later" in text for text in loud), loud


def test_an_a_answer_is_noticed_even_though_the_gate_switches_the_mode_right_after(tmp_path):
    """Gate.check calls ask first and only then switches to accept-edits; the app's note of the grant relies on
    that order (it checks the mode is not yet accept-edits when the answer comes back)."""
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), read_short=Lines("a"))
    app.start()
    seen = []
    inner = app.gate.ask
    app.gate.ask = lambda request: (seen.append(app.gate.mode), inner(request))[1]
    assert app.gate.check("write_file", {"path": "a.txt", "content": "x", "if_exists": "refuse"}) is None
    assert seen == ["default"] and app.gate.mode == "accept-edits"
    app.new_conversation()
    assert app.gate.mode == "default"


# -- the framework logger must not write to the terminal ------------------------------------------------------

@pytest.fixture
def bare_logging(monkeypatch):
    """Logging as it is in a fresh process: no handler anywhere, so only ``logging.lastResort`` could print.
    pytest adds its own capture handler to the root logger around each test, so the test calls ``strip()`` in its
    body (the monkeypatch puts the handlers back afterwards)."""
    import logging
    root, framework = logging.getLogger(), logging.getLogger("agentx_dev")
    created = []

    def strip():
        monkeypatch.setattr(root, "handlers", created_root := [])
        monkeypatch.setattr(framework, "handlers", created_framework := [])
        # an earlier test may have left a console hook on the global observability manager
        from agentx_dev.Observability import observability
        monkeypatch.setattr(observability, "hooks", [])
        created.extend([created_root, created_framework])

    yield strip
    for handlers in created:
        for handler in handlers:
            if getattr(handler, "_agentx_cli", False):
                handler.close()


EVIL_PATH = "../../\x1b]0;PWNED\x07\x1b[2J"


def run_failing_tool_turn(tmp_path):
    model = MockModel(script=[make_react_response("read_path", {"path": EVIL_PATH}), make_final("done")])
    app, out, cwd, home = make_app(tmp_path, Lines("look", "/exit"), model)
    assert app.start() == 0
    return out.getvalue(), home


def test_a_failing_tool_does_not_leak_escapes_or_a_traceback_to_the_terminal(tmp_path, bare_logging, capfd):
    bare_logging()
    app_module.setup_logging(tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    shown, home = run_failing_tool_turn(tmp_path)
    captured = capfd.readouterr()
    for text in (captured.out, captured.err):
        assert "\x1b" not in text and "Traceback" not in text and "PWNED" not in text
    assert "\x1b" not in shown


def test_without_the_setup_the_logger_would_write_to_stderr(tmp_path, bare_logging, capfd):
    bare_logging()
    # the control: proves the test above is able to see the leak
    (tmp_path / "home").mkdir(exist_ok=True)
    run_failing_tool_turn(tmp_path)
    assert "Traceback" in capfd.readouterr().err


def test_the_errors_go_to_a_log_file_in_the_agentx_folder_with_controls_defused(tmp_path, bare_logging):
    bare_logging()
    import logging
    home = tmp_path / "home"
    app_module.setup_logging(home)
    run_failing_tool_turn(tmp_path)
    for handler in logging.getLogger("agentx_dev").handlers:
        handler.flush()
    log = (home / ".agentx" / "agentx.log").read_text(encoding="utf-8")
    assert "read_path" in log and "Traceback" in log
    assert "\x1b" not in log and "\x07" not in log and "\\x1b" in log


def test_key_values_never_reach_the_log_file_and_the_file_is_private(tmp_path, bare_logging, monkeypatch):
    bare_logging()
    import logging
    import os
    import stat
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "sk-test-0123456789abcdef")
    home = tmp_path / "home"
    app_module.setup_logging(home)
    logging.getLogger("agentx_dev.ChatModel").error("auth failed for key sk-test-0123456789abcdef")
    for handler in logging.getLogger("agentx_dev").handlers:
        handler.flush()
    path = home / ".agentx" / "agentx.log"
    log = path.read_text(encoding="utf-8")
    assert "auth failed" in log and "sk-test-0123456789abcdef" not in log and "[hidden]" in log
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_ordinary_progress_messages_are_not_logged(tmp_path, bare_logging):
    bare_logging()
    import logging
    home = tmp_path / "home"
    app_module.setup_logging(home)
    logging.getLogger("agentx_dev.Tools").info("ACTION: secret input")
    logging.getLogger("agentx_dev.Tools").warning("a warning")
    for handler in logging.getLogger("agentx_dev").handlers:
        handler.flush()
    log = (home / ".agentx" / "agentx.log").read_text(encoding="utf-8")
    assert "a warning" in log and "secret input" not in log


def test_an_unwritable_home_still_keeps_the_terminal_quiet(tmp_path, bare_logging, capfd):
    bare_logging()
    import logging
    blocker = tmp_path / "home"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    app_module.setup_logging(blocker)                              # mkdir fails: ignored
    logging.getLogger("agentx_dev.Tools").error("boom", exc_info=True)
    captured = capfd.readouterr()
    assert captured.err == "" and captured.out == ""


def test_a_log_that_cannot_be_written_later_stays_quiet(tmp_path, bare_logging, capfd):
    bare_logging()
    import logging
    app_module.setup_logging(tmp_path / "home")
    handler = [h for h in logging.getLogger("agentx_dev").handlers if getattr(h, "_agentx_cli", False)][0]
    handler.stream = None
    handler.baseFilename = str(tmp_path / "no" / "such" / "dir" / "x.log")
    logging.getLogger("agentx_dev.Tools").warning("will not be written")
    assert capfd.readouterr().err == ""


def test_setting_up_twice_does_not_stack_handlers(tmp_path, bare_logging):
    bare_logging()
    import logging
    app_module.setup_logging(tmp_path / "home")
    app_module.setup_logging(tmp_path / "home")
    ours = [h for lg in (logging.getLogger(), logging.getLogger("agentx_dev")) for h in lg.handlers
            if getattr(h, "_agentx_cli", False)]
    assert len(ours) == 2


def test_main_sets_the_logging_up_before_the_app_is_built(tmp_path, monkeypatch):
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    home.mkdir()
    order = []

    class FakeApp:
        def __init__(self, **kw):
            order.append("app")

        def start(self, resume=None):
            return 0

    class FakePrompt:
        def __init__(self, **kw):
            self.read = self.read_short = lambda prompt="": ""
            self.select = lambda *args, **kwargs: None

    monkeypatch.setattr(app_module, "is_terminal", lambda: True)
    monkeypatch.setattr(app_module, "ChatApp", FakeApp)
    monkeypatch.setattr(app_module, "PromptInput", FakePrompt)
    monkeypatch.setattr(app_module, "setup_logging", lambda h: order.append(("log", h)))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    assert main([]) == 0
    assert order == [("log", home), "app"]


def test_a_protected_folder_cannot_be_moved_into_place_by_naming_its_parent_as_the_destination(tmp_path):
    model = MockModel(script=[write_call("stuff/.agentx/settings.json", "{}"),
                              make_react_response("move_file", {"source": "stuff/.agentx", "destination": "."}),
                              make_final("could not")])
    lines = Lines("sneak settings in", "y", "/exit")
    app, out, cwd, _ = make_app(tmp_path, lines, model)
    app.start()
    assert (cwd / "stuff" / ".agentx" / "settings.json").exists()
    assert {p.name for p in (cwd / ".agentx").iterdir()} <= {"log.md", "notes", "memory"}   # only the app's own files
    assert lines.prompts.count("Allow? [y/n] ") + lines.prompts.count("Allow? [y/a/n] ") == 1     # only the write asked
    assert "deny rule" in all_seen_by(model)


def test_an_ordinary_move_into_an_existing_folder_asks_and_says_where_it_lands(tmp_path):
    model = MockModel(script=[make_react_response("move_file", {"source": "a.txt", "destination": "dest"}),
                              make_final("moved")])
    app, out, cwd, _ = make_app(tmp_path, Lines("move it", "y", "/exit"), model)
    (cwd / "dest").mkdir()
    (cwd / "a.txt").write_text("x", encoding="utf-8")
    app.start()
    assert (cwd / "dest" / "a.txt").exists() and not (cwd / "a.txt").exists()
    assert "Move a.txt to dest/a.txt" in out.getvalue()


# -- project allow rules need the user's yes ------------------------------------------------------------------

RULE = "run_shell:make*"


def project_rules_app(tmp_path, lines, *, allow=(RULE,), project=(RULE,), deny=(), model=None, **kw):
    return make_app(tmp_path, lines, model or MockModel(script=[]), allow=list(allow), project_allow=list(project),
                    deny=list(deny), **kw)


def test_the_user_is_asked_once_whether_to_honour_the_project_rules(tmp_path):
    lines = Lines("y", "/exit")
    app, out, _, _ = project_rules_app(tmp_path, lines)
    app.start()
    assert lines.prompts[0] == "Honour these project rules? [y/N] " and lines.prompts.count(lines.prompts[0]) == 1
    assert RULE in out.getvalue()


def test_yes_keeps_the_project_rules(tmp_path):
    app, out, _, _ = project_rules_app(tmp_path, Lines("y", "/exit"))
    app.start()
    assert app.gate.allow == [RULE] and app.gate.check("run_shell", {"command": "make test"}) is None


def test_yes_is_accepted_in_any_case_and_spelling(tmp_path):
    for answer in ("Y", " yes ", "YES"):
        app, _, _, _ = project_rules_app(tmp_path, Lines(answer, "/exit"))
        app.start()
        assert app.gate.allow == [RULE], answer


@pytest.mark.parametrize("answer", ["", "n", "no", "N"])
def test_enter_or_no_drops_the_project_rules(tmp_path, answer):
    asked = []
    app, out, _, _ = project_rules_app(tmp_path, Lines(answer, "/exit"), allow=[RULE])
    app.gate.ask = lambda request: asked.append(request) or Answer("n")
    app.start()
    assert app.gate.allow == []
    app.gate.check("run_shell", {"command": "make test"})
    assert asked                                                       # it asks, as it would without the rule
    assert "ignor" in out.getvalue().lower()


@pytest.mark.parametrize("fault", [EOFError(), KeyboardInterrupt(), RuntimeError("terminal gone")])
def test_end_of_input_ctrl_c_or_a_failing_reader_drop_them(tmp_path, fault):
    app, _, _, _ = project_rules_app(tmp_path, Lines(fault, "/exit"))
    app.start()
    assert app.gate.allow == []


def test_an_unrecognised_answer_is_asked_once_more_and_then_means_no(tmp_path):
    lines = Lines("maybe", "perhaps", "/exit")
    app, _, _, _ = project_rules_app(tmp_path, lines)
    app.start()
    assert app.gate.allow == [] and lines.prompts.count("Honour these project rules? [y/N] ") == 2
    app, _, _, _ = project_rules_app(tmp_path, Lines("maybe", "y", "/exit"))
    app.start()
    assert app.gate.allow == [RULE]


def test_a_no_keeps_the_project_deny_rules_and_the_users_own_allow_rules(tmp_path):
    app, _, _, _ = project_rules_app(tmp_path, Lines("n", "/exit"), allow=["run_shell:pytest*", RULE],
                                     project=[RULE], deny=["run_shell:*sudo*"])
    app.start()
    assert app.gate.allow == ["run_shell:pytest*"]
    assert "run_shell:*sudo*" in app.gate.deny and set(DEFAULT_DENY) <= set(app.gate.deny)
    assert app.gate.check("run_shell", {"command": "pytest -q"}) is None
    assert "deny rule" in app.gate.check("run_shell", {"command": "sudo ls"})


def test_a_rule_the_user_also_wrote_survives_a_no_once(tmp_path):
    app, _, _, _ = project_rules_app(tmp_path, Lines("n", "/exit"), allow=[RULE, RULE], project=[RULE])
    app.start()
    assert app.gate.allow == [RULE]


def test_nothing_is_asked_without_project_rules(tmp_path):
    lines = Lines("/exit")
    app, _, _, _ = project_rules_app(tmp_path, lines, allow=["run_shell:pytest*"], project=[])
    app.start()
    assert "Honour these project rules? [y/N] " not in lines.prompts and app.gate.allow == ["run_shell:pytest*"]


def test_the_question_comes_before_the_model_is_built_and_through_the_short_reader(tmp_path):
    short = Lines("y")
    line = Lines("/exit")
    app, out, _, _ = make_app(tmp_path, line, None, model_factory=lambda n, e: (_ for _ in ()).throw(
        ModelError("No API key found.")), allow=[RULE], project_allow=[RULE], read_short=short)
    assert app.start() == 1
    assert short.prompts == ["Honour these project rules? [y/N] "] and line.prompts == [] and app.gate.allow == [RULE]


def test_a_project_that_allows_all_edits_cannot_skip_the_edit_prompts_unless_the_user_says_yes(tmp_path):
    model = MockModel(script=[write_call(), make_final("done")])
    lines = Lines("n", "create hello.txt", "y", "/exit")
    app, out, cwd, _ = project_rules_app(tmp_path, lines, allow=["write_file"], project=["write_file"], model=model)
    app.start()
    assert "Allow? [y/a/n] " in lines.prompts and (cwd / "hello.txt").exists()
    model = MockModel(script=[write_call(), make_final("done")])
    lines = Lines("y", "create hello.txt", "/exit")
    app, out, cwd, _ = project_rules_app(tmp_path, lines, allow=["write_file"], project=["write_file"], model=model)
    (cwd / "hello.txt").unlink()
    app.start()
    assert "Allow? [y/a/n] " not in lines.prompts and (cwd / "hello.txt").exists()


# -- errors outside a turn ------------------------------------------------------------------------------------

def test_a_failing_input_line_degrades_to_the_plain_prompt_with_one_clean_line(tmp_path, monkeypatch):
    typed = iter(["hello", "/exit"])
    shown = []
    monkeypatch.setattr("builtins.input", lambda prompt="": shown.append(prompt) or next(typed))
    model = MockModel(script=[make_final("hi there")])
    app, out, _, _ = make_app(tmp_path, Lines(PermissionError("cannot write history \x1b[2J")), model)
    assert app.start() == 0
    text = out.getvalue()
    assert "PermissionError" in text and "Traceback" not in text and "\x1b" not in text
    assert text.count("Could not read your input") == 1 and "hi there" in text
    assert shown == ["> ", "> "]


def test_a_plain_prompt_that_fails_too_ends_the_session_cleanly(tmp_path, monkeypatch):
    def broken(prompt=""):
        raise OSError("stdin is gone")
    monkeypatch.setattr("builtins.input", broken)
    app, out, _, _ = make_app(tmp_path, Lines(RuntimeError("first")), MockModel(script=[]))
    assert app.start() == 1
    text = out.getvalue()
    assert text.count("Could not read your input") == 2 and "Traceback" not in text


def test_ctrl_c_and_end_of_input_at_the_prompt_are_not_input_failures(tmp_path):
    app, out, _, _ = make_app(tmp_path, Lines(KeyboardInterrupt(), EOFError()), MockModel(script=[]))
    assert app.start() == 0 and "Could not read your input" not in out.getvalue()


def test_main_turns_an_unexpected_error_in_the_session_into_one_line(tmp_path, monkeypatch, capsys):
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    home.mkdir()

    class FakeApp:
        def __init__(self, **kw):
            pass

        def start(self, resume=None):
            raise RuntimeError("boom \x1b[2J")

    class FakePrompt:
        def __init__(self, **kw):
            self.read = self.read_short = lambda prompt="": ""
            self.select = lambda *args, **kwargs: None

    monkeypatch.setattr(app_module, "is_terminal", lambda: True)
    monkeypatch.setattr(app_module, "ChatApp", FakeApp)
    monkeypatch.setattr(app_module, "PromptInput", FakePrompt)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    assert main([]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "boom" in err and "Traceback" not in err and "\x1b" not in err
    assert len(err.strip().splitlines()) == 1


def test_settings_command_names_the_files_and_what_is_in_force(tmp_path):
    app, out, _, _ = make_app(tmp_path, Lines("/settings", "/exit"), MockModel(script=[]))
    app.start()
    text = out.getvalue()
    assert "Your settings file" in text and "settings.json" in text and "Mode: default" in text
    assert "Rules:" in text and "Model:" in text and "--folder" in text


def test_the_startup_banner_says_when_a_settings_file_was_created(tmp_path):
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]))
    app.settings_created = tmp_path / "home" / ".agentx" / "settings.json"
    app.start()
    text = out.getvalue()
    assert "Created your settings file" in text and "/settings" in text


class SpyProgress:
    def __init__(self):
        self.log = []

    def start(self, label="Thinking"): self.log.append(("start", label))
    def stop(self): self.log.append(("stop",))

    def paused(self):
        from contextlib import contextmanager

        @contextmanager
        def block():
            self.log.append(("pause",))
            yield
            self.log.append(("resume",))
        return block()


def test_the_spinner_runs_between_events_and_is_paused_for_a_prompt(tmp_path):
    model = MockModel(script=[write_call(), make_final("done")])
    app, out, cwd, _ = make_app(tmp_path, Lines("make it", "/exit"), model, read_short=Lines("y"))
    spy = SpyProgress()
    app.progress = spy
    app.start()
    kinds = [entry[0] for entry in spy.log]
    assert kinds[0] == "start" and spy.log[0] == ("start", "Thinking")
    assert ("start", "Running write_file") in spy.log
    assert "pause" in kinds and kinds.index("pause") < kinds.index("resume")
    assert spy.log[-1] == ("stop",)                          # never left running after the turn
    assert (cwd / "hello.txt").exists()


def test_the_spinner_is_stopped_after_a_failed_turn(tmp_path):
    def boom(messages):
        raise RuntimeError("model down")

    app, out, _, _ = make_app(tmp_path, Lines("hi", "/exit"), MockModel(script=boom))
    spy = SpyProgress()
    app.progress = spy
    app.start()
    assert spy.log[-1] == ("stop",) and "Something went wrong" in out.getvalue()


def test_an_untrusted_folder_stops_before_anything_from_it_is_read(tmp_path, monkeypatch, capsys):
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(app_module, "is_terminal", lambda: True)
    monkeypatch.chdir(proj)
    monkeypatch.setattr(app_module, "ensure_trusted", lambda *args, **kwargs: False)
    read = []
    monkeypatch.setattr(app_module, "load_settings", lambda *a, **k: read.append("settings"))
    monkeypatch.setattr(app_module, "ChatApp", lambda **k: read.append("app"))
    assert main([]) == 1
    assert read == []                                   # neither the project's settings nor AGENTX.md were read
    assert "not trusted" in capsys.readouterr().err


def test_resume_with_no_id_offers_an_arrow_key_list_and_loads_the_pick(tmp_path):
    model = MockModel(script=[make_final("first answer"), make_final("second answer"), make_final("ok")])
    app1, _, _, home = make_app(tmp_path, Lines("first topic", "/exit"), model)
    app1.start()
    app2, out, _, _ = make_app(tmp_path, Lines("second topic", "/exit"), model)
    app2.start()
    menus = []

    def select(options, default=0, hotkeys=None):
        menus.append(list(options))
        return 1                                         # the second entry

    app3, out, _, _ = make_app(tmp_path, Lines("/resume", "/exit"), model, select=select)
    app3.start()
    assert len(menus) == 1 and menus[0][-1] == "Cancel" and len(menus[0]) == 3
    assert "Resumed" in out.getvalue()


def test_cancelling_the_resume_list_resumes_nothing(tmp_path):
    model = MockModel(script=[make_final("answer")])
    app1, _, _, _ = make_app(tmp_path, Lines("topic", "/exit"), model)
    app1.start()
    app2, out, _, _ = make_app(tmp_path, Lines("/resume", "/exit"), model, select=lambda options, default=0, hotkeys=None: len(options) - 1)
    app2.start()
    assert "Resumed" not in out.getvalue()
