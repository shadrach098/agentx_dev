"""Audit fixes in the approval gate and its preview: a command grant never approves a program launched through
options, an edit grant covers edits only, the diff shows every line break that is not a real one, and off Windows
a backslash in a path is read the way the tools read it."""

import os
import sys

import pytest

from agentx_dev.cli import approval
from agentx_dev.cli.approval import Answer, Gate, _grantable_prefix, path_forms, subject_of
from agentx_dev.cli.preview import preview_for
from agentx_dev.cli.ui_text import plain

ON_WINDOWS = sys.platform == "win32"
LINE_ENDINGS = "(only line endings or the final newline change)"


class Asker:
    def __init__(self, *choices):
        self.choices = list(choices)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return Answer(self.choices.pop(0) if self.choices else "n")


# -- #7: a command grant never approves a program launched through options ------------------------------------

@pytest.mark.parametrize("command", [
    "find . -name x", "sed -n 1p f", "gsed -n 1p f", "tar -tf a.tar", "rsync a b", "scp a b", "vim -es f",
    "zip -r a.zip d", "mawk 1 f", "parallel echo ::: a", "command ls", "crontab -l",
    "git submodule status", "git rebase main", "git difftool", "git mergetool", "git filter-branch x",
    "git bisect start", "git config user.name", "git send-email x", "git hook run x",
    "/usr/bin/git submodule foreach x", "GIT.EXE rebase main", "git Submodule status",
])
def test_programs_and_sub_commands_that_run_other_programs_are_never_remembered(command):
    assert _grantable_prefix(command) is None, command


@pytest.mark.parametrize("first,later", [
    ("git fetch origin", "git fetch --upload-pack='touch x' ."),
    ("git fetch origin", "git fetch --upload-pack 'touch x' ."),
    ("git fetch origin", "git fetch --upl=x ."),                       # git takes an unambiguous abbreviation
    ("git fetch origin", "git fetch -u x ."),
    ("git fetch origin", 'git fetch "--upload-pack=x" .'),
    ("git fetch origin", "git fetch \\-\\-upload-pack=x ."),
    ("git fetch origin", "git fetch {--upload-pack=x,.}"),              # brace expansion makes the option
    ("git fetch origin", "git fetch -* ."),                             # a glob could match a file named --upload-pack=x
    ("git fetch origin", "git fetch $OPT ."),
    ("git fetch origin", "git fetch 'unbalanced ."),
    ("git pull origin", "git pull --upload-pack=x origin"),
    ("git ls-remote origin", "git ls-remote -u x origin"),
    ("git clone a b", "git clone --template=t a b"),
    ("git clone a b", "git clone -c core.fsmonitor=x a b"),
    ("git clone a b", "git clone --config core.fsmonitor=x a b"),
    ("git push origin", "git push --receive-pack=x origin"),
    ("git push origin", "git push --exec=x origin"),
    ("git grep foo", "git grep -Ocat foo"),
    ("git grep foo", "git grep -iOcat foo"),
    ("git grep foo", "git grep --open-files-in-pager=cat foo"),
    ("git log", "git log -c core.pager=x"),
    ("git status", "git status --config-env=core.fsmonitor=X"),
    ("git archive HEAD", "git archive --exec=x --remote=r HEAD"),
    ("make all", "make all -f evil.mk"),
    ("make all", "make all -kfevil.mk"),
    ("make all", "make all --eval=x"),
    ("make all", "make all --file=evil.mk"),
    ("make all", "make all SHELL=./x"),
    ("go test ./...", "go test -exec=./x ./..."),
    ("go test ./...", "go test -toolexec x ./..."),
    ("go vet ./...", "go vet -vettool=./x ./..."),
    ("cargo test", "cargo test --config target.x.runner=y"),
    ("rg foo", "rg --pre=./x foo"),
    ("npm test", "npm test --script-shell=./x"),
    ("yarn test", "yarn test --script-shell ./x"),
])
def test_a_grant_does_not_approve_a_call_that_launches_a_program(first, later):
    asker = Asker("a", "n")
    g = Gate(ask=asker)
    assert g.check("run_shell", {"command": first}) is None and g.grants
    assert g.check("run_shell", {"command": later}) is not None
    assert len(asker.requests) == 2


def test_a_one_word_grant_for_a_git_path_does_not_cover_risky_sub_commands_or_options():
    asker = Asker("a", "n", "n")
    g = Gate(ask=asker)
    g.check("run_shell", {"command": "/usr/bin/git status"})
    assert g.grants == {"/usr/bin/git"}
    g.check("run_shell", {"command": "/usr/bin/git submodule foreach x"})
    g.check("run_shell", {"command": "/usr/bin/git fetch --upload-pack=x ."})
    assert len(asker.requests) == 3


@pytest.mark.parametrize("grant,command", [
    ("find", "find . -exec touch x {} +"), ("sed", "sed 1ex f"), ("tar", "tar -xf a.tar --to-command=x"),
    ("git submodule", "git submodule foreach x"),
])
def test_a_grant_left_in_the_set_cannot_approve_a_program_that_is_never_granted(grant, command):
    asker = Asker("n")
    g = Gate(ask=asker)
    g.grants.add(grant)
    assert g.check("run_shell", {"command": command}) is not None and len(asker.requests) == 1


@pytest.mark.parametrize("first,later", [
    ("git status", "git status -s"), ("git log", "git log --oneline -n 5"), ("git push origin", "git push -u origin main"),
    ("git add .", "git add -u"), ("git commit -m x", "git commit -am 'fix the config'"), ("git diff", "git diff --cached"),
    ("git fetch origin", "git fetch --prune origin"), ("git grep foo", "git grep -n bar"), ("make all", "make all"),
    ("make all", "make all -j4"), ("pytest -q", "pytest tests -x"), ("go test ./...", "go test -v ./..."),
    ("cargo test", "cargo test --release"), ("npm test", "npm test"), ("rg foo", "rg -n bar src"),
    ("ls -la", "ls *.py"),                       # globs only matter for programs with launching options
])
def test_ordinary_grants_keep_working(first, later):
    asker = Asker("a")
    g = Gate(ask=asker)
    g.check("run_shell", {"command": first})
    assert g.check("run_shell", {"command": later}) is None and len(asker.requests) == 1


def test_a_command_that_launches_a_program_is_approved_once_and_remembers_nothing():
    asker = Asker("a", "n")
    g = Gate(ask=asker)
    assert g.check("run_shell", {"command": "git fetch --upload-pack=x ."}) is None
    assert asker.requests[0].remember is False and asker.requests[0].grant == "" and g.grants == set()
    g.check("run_shell", {"command": "git fetch origin"})
    assert len(asker.requests) == 2


# -- #10: "a" at an edit prompt covers edits, not web lookups ------------------------------------------------------

WRITE = {"path": "a.txt", "content": "x", "if_exists": "refuse"}


def test_an_edit_grant_does_not_let_web_lookups_run_without_asking(tmp_path):
    asker = Asker("a", "n", "n")
    g = Gate(ask=asker, cwd=tmp_path)
    assert g.check("write_file", WRITE) is None
    assert asker.requests[0].grant == "all edits this session"
    assert g.mode == "accept-edits" and g.edits_granted
    assert g.check("edit_file", {"path": "a.txt", "find": "x", "replace": "y"}) is None    # edits: no question
    assert len(asker.requests) == 1
    assert g.check("web_fetch", {"url": "https://example.invalid/?q=data"}) is not None
    assert g.check("web_search", {"query": "data"}) is not None
    assert [r.tool for r in asker.requests[1:]] == ["web_fetch", "web_search"]


def test_after_an_edit_grant_a_web_a_still_grants_the_web(tmp_path):
    asker = Asker("a", "a")
    g = Gate(ask=asker, cwd=tmp_path)
    g.check("write_file", WRITE)
    assert g.check("web_fetch", {"url": "https://a.example"}) is None
    assert g.check("web_search", {"query": "x"}) is None and len(asker.requests) == 2


@pytest.mark.parametrize("choose", [lambda g: g.set_mode("accept-edits"),
                                    lambda g: [g.cycle_mode() for _ in range(3)]])
def test_accept_edits_the_user_chooses_still_covers_web_lookups(tmp_path, choose):
    asker = Asker("a")
    g = Gate(ask=asker, cwd=tmp_path)
    g.check("write_file", WRITE)
    choose(g)
    assert g.mode == "accept-edits" and not g.edits_granted
    assert g.check("web_fetch", {"url": "https://a.example"}) is None and len(asker.requests) == 1


def test_accept_edits_from_the_start_covers_web_lookups(tmp_path):
    asker = Asker()
    g = Gate(mode="accept-edits", ask=asker, cwd=tmp_path)
    assert not g.edits_granted
    assert g.check("web_fetch", {"url": "https://a.example"}) is None and asker.requests == []


def test_a_mode_wrapper_like_the_apps_still_ends_the_edit_grant(tmp_path):
    g = Gate(ask=Asker("a"), cwd=tmp_path)
    original = g.set_mode
    g.set_mode = lambda mode: original(mode)          # the app wraps set_mode the same way
    g.check("write_file", WRITE)
    assert g.edits_granted
    g.set_mode("default")                             # what /clear does after an edit grant
    assert g.mode == "default" and not g.edits_granted


# -- #11: the diff splits on real line breaks only -----------------------------------------------------------------

BREAKS = ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ", "\r"]


@pytest.mark.parametrize("char", BREAKS)
def test_swapping_a_line_break_for_another_break_character_shows_in_the_diff(tmp_path, char):
    (tmp_path / "f.py").write_bytes(b"# Verify signature\ncheck_signature()\n")
    p = preview_for("edit_file", {"path": "f.py", "find": "# Verify signature\ncheck_signature()",
                                  "replace": f"# Verify signature{char}check_signature()"}, tmp_path)
    assert p.kind == "diff" and not p.truncated and p.body != LINE_ENDINGS
    lines = p.body.split("\n")
    assert "-# Verify signature" in lines and "-check_signature()" in lines
    assert f"+# Verify signature{char}check_signature()" in lines and "+check_signature()" not in lines
    shown = plain(p.body, keep_newlines=True)
    assert char not in shown and approval_escape(char) in shown


def approval_escape(char):
    code = ord(char)
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


@pytest.mark.parametrize("char", BREAKS)
def test_the_reverse_swap_shows_too(tmp_path, char):
    (tmp_path / "f.py").write_bytes(f"# note{char}check_signature()\n".encode("utf-8"))
    p = preview_for("edit_file", {"path": "f.py", "find": f"# note{char}check", "replace": "# note\ncheck"}, tmp_path)
    assert p.body != LINE_ENDINGS
    assert "+# note" in p.body.split("\n") and "+check_signature()" in p.body.split("\n")


@pytest.mark.parametrize("old,new", [
    (b"a\r\nb\r\n", "a\nb\n"), (b"a\nb", "a\nb\n"), (b"a\nb\n", "a\nb"), (b"a\r\nb", "a\nb\n"),
])
def test_real_line_ending_changes_still_get_the_summary(tmp_path, old, new):
    (tmp_path / "f.txt").write_bytes(old)
    p = preview_for("write_file", {"path": "f.txt", "content": new, "if_exists": "overwrite"}, tmp_path)
    assert p.body == LINE_ENDINGS


def test_a_lone_cr_in_place_of_a_crlf_is_not_a_line_ending_change(tmp_path):
    (tmp_path / "s.sh").write_bytes(b"# note\r\necho LIVE\r\n")
    p = preview_for("write_file", {"path": "s.sh", "content": "# note\recho LIVE\r\n", "if_exists": "overwrite"},
                    tmp_path)
    assert p.body != LINE_ENDINGS and "+# note\recho LIVE" in p.body.split("\n")


def test_a_new_file_keeps_a_break_character_inside_its_line(tmp_path):
    p = preview_for("write_file", {"path": "n.py", "content": "x = 1\n# Verify signature\x0ccheck_signature()\n",
                                   "if_exists": "refuse"}, tmp_path)
    lines = p.body.split("\n")
    assert "+# Verify signature\x0ccheck_signature()" in lines and "+check_signature()" not in lines
    assert not [line for line in lines if line.startswith("-") and not line.startswith("---")]


def test_an_empty_file_and_a_single_newline_differ(tmp_path):
    (tmp_path / "e.txt").write_bytes(b"")
    p = preview_for("write_file", {"path": "e.txt", "content": "\n", "if_exists": "overwrite"}, tmp_path)
    assert p.body == LINE_ENDINGS          # only the final newline changes
    p = preview_for("write_file", {"path": "e.txt", "content": "\n\n", "if_exists": "overwrite"}, tmp_path)
    assert p.body != LINE_ENDINGS and "+" in p.body.split("\n")


# -- #15: off Windows a backslash is part of a name ------------------------------------------------------------------

def test_windows_keeps_reading_a_backslash_as_a_separator():
    assert approval._BACKSLASH_IN_NAMES is (os.name != "nt")


def test_a_backslash_path_without_links_has_the_same_forms(tmp_path):
    assert path_forms("src\\a.py", tmp_path) == [path_forms("src/a.py", tmp_path)[0]]


def test_the_raw_text_is_resolved_as_well_where_a_backslash_is_a_name_character(tmp_path, monkeypatch):
    """A stand-in for a POSIX link named ``x\\y`` pointing at ``.git`` (so the logic is checked on Windows too)."""
    real = approval._resolve
    monkeypatch.setattr(approval, "_BACKSLASH_IN_NAMES", True)
    monkeypatch.setattr(approval, "_resolve", lambda text, cwd: ".git/config" if text == "x\\y/config"
                        else real(text, cwd))
    assert path_forms("x\\y/config", tmp_path)[-1] == ".git/config"
    asker = Asker("y")
    g = Gate(mode="accept-edits", deny=["write_file:.git/*"], ask=asker, cwd=tmp_path)
    assert "deny rule" in g.check("write_file", {"path": "x\\y/config", "content": "x"})
    assert asker.requests == []
    assert subject_of("write_file", {"path": "x\\y/config"}, tmp_path) == ".git/config"


@pytest.mark.skipif(ON_WINDOWS, reason="a backslash is a separator on Windows")
def test_a_link_named_with_a_backslash_cannot_reach_the_git_folder(tmp_path, monkeypatch):
    from agentx_dev.cli.app import default_deny
    from agentx_dev.cli.tools import build_tools

    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[core]\n")
    os.symlink(".git", project / "x\\y", target_is_directory=True)
    gate = Gate(mode="accept-edits", deny=default_deny(tmp_path / "home"), cwd=project)
    tools = {tool.name: tool for tool in build_tools(project, [], gate)}
    result = tools["write_file"].func(path="x\\y/config", content="[core]\n\tfsmonitor = x\n", if_exists="overwrite")
    assert "deny rule" in result
    assert (project / ".git" / "config").read_text() == "[core]\n"
    (project / "f.txt").write_text("x")
    assert "deny rule" in gate.check("move_file", {"source": "f.txt", "destination": "x\\y"})


@pytest.mark.skipif(ON_WINDOWS, reason="a backslash is a separator on Windows")
def test_the_prompt_names_the_file_a_backslash_link_reaches(tmp_path):
    (tmp_path / "docs").mkdir()
    os.symlink("docs", tmp_path / "d\\x", target_is_directory=True)
    asker = Asker("n")
    g = Gate(ask=asker, cwd=tmp_path)
    g.check("write_file", {"path": "d\\x/f.md", "content": "x"})
    assert asker.requests[0].subject == "docs/f.md"
