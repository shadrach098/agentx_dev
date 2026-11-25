"""The gate and the new tools: background tasks and terminals are judged as run_shell, the browser as web."""

from pathlib import Path

from agentx_dev.cli.app import DEFAULT_DENY
from agentx_dev.cli.approval import PLAN_REFUSAL, WEB_GRANT, Answer, Gate, kind_of, rule_view, subject_of


class Asker:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.answers.pop(0) if self.answers else Answer("y")


def make(mode="default", allow=(), deny=(), answers=()):
    asker = Asker(*answers)
    return Gate(mode=mode, allow=allow, deny=deny, ask=asker, cwd=Path.cwd()), asker


REFUSED = "Refused: the settings deny rule 'run_shell:rm *' blocks this."


def test_kinds():
    for name in ("start_background", "send_to_terminal", "stop_other_task"):
        assert kind_of(name) == "command"
    for name in ("background_output", "list_background", "stop_background", "browser_read",
                 "browser_screenshot", "browser_console"):
        assert kind_of(name) == "read"
    assert kind_of("browser_open") == "web"
    for name in ("browser_click", "browser_type", "browser_press", "browser_back"):
        assert kind_of(name) == "browse"


def test_rule_view():
    assert rule_view("send_to_terminal", {"id": "t1", "text": "ls"}) == ("run_shell", {"command": "ls"})
    assert rule_view("start_background", {"command": "x"}) == ("run_shell", {"command": "x"})
    assert rule_view("browser_open", {"url": "u"}) == ("web_fetch", {"url": "u"})
    assert rule_view("stop_other_task", {"command": "x"}) == ("stop_other_task", {"command": "x"})


def test_shell_deny_rule_blocks_background_and_terminal_input():
    g, asker = make(deny=["run_shell:rm *"])
    assert g.check("start_background", {"command": "rm -rf x"}) == REFUSED
    assert g.check("send_to_terminal", {"id": "term1", "text": "rm x"}) == REFUSED
    assert asker.requests == []


def test_default_skills_rules_cover_background():
    g, asker = make(deny=DEFAULT_DENY)
    assert g.check("start_background", {"command": "cat ~/.agentx/skills/x"}) is not None
    assert g.check("send_to_terminal", {"id": "t", "text": "cat ~/.agentx/skills/x"}) is not None
    assert asker.requests == []


def test_allow_rule_and_grant_for_run_shell_cover_background():
    g, asker = make(allow=["run_shell:npm run *"], answers=[Answer("a")])
    assert g.check("start_background", {"command": "npm run dev"}) is None
    assert g.check("send_to_terminal", {"id": "t", "text": "npm run build"}) is None
    assert asker.requests == []
    assert g.check("start_background", {"command": "npm test"}) is None
    assert len(asker.requests) == 1 and asker.requests[0].tool == "start_background"
    assert g.check("run_shell", {"command": "npm test"}) is None
    assert len(asker.requests) == 1


def test_grant_from_run_shell_covers_terminal_input():
    g, asker = make(answers=[Answer("a")])
    assert g.check("run_shell", {"command": "npm test"}) is None
    assert g.check("send_to_terminal", {"id": "t", "text": "npm test"}) is None
    assert len(asker.requests) == 1


def test_compound_background_always_asks():
    g, asker = make(allow=["run_shell:npm *"])
    assert g.check("start_background", {"command": "npm run dev & rm x"}) is None
    assert len(asker.requests) == 1
    assert g.check("send_to_terminal", {"id": "t", "text": "npm run dev; rm x"}) is None
    assert len(asker.requests) == 2
    assert g.check("send_to_terminal", {"id": "t"}) is None       # no text at all: compound, asks
    assert len(asker.requests) == 3


def test_interpreter_is_never_granted_for_background():
    g, asker = make(answers=[Answer("a")])
    g.check("start_background", {"command": "python -c 'x'"})
    assert asker.requests[0].remember is False
    g.check("start_background", {"command": "python -c 'x'"})
    assert len(asker.requests) == 2


def test_plan_mode():
    g, asker = make(mode="plan")
    for tool, args in (("start_background", {"command": "ls"}), ("send_to_terminal", {"id": "t", "text": "ls"}),
                       ("browser_click", {"target": "b", "page_url": "http://x"})):
        assert g.check(tool, args) == PLAN_REFUSAL
    assert g.check("browser_read", {}) is None
    assert g.check("background_output", {"id": "t"}) is None
    assert g.check("browser_open", {"url": "http://x"}) is None      # a fetch, allowed in plan mode as web_fetch is
    assert asker.requests == [] or [r.tool for r in asker.requests] == ["browser_open"]


def test_auto_mode_runs_commands_and_browse():
    g, asker = make(mode="auto")
    assert g.check("start_background", {"command": "npm run dev"}) is None
    assert g.check("browser_click", {"target": "b", "page_url": "http://x"}) is None
    assert g.check("stop_other_task", {"command": "task3"}) is None
    assert asker.requests == []


def test_browse_asks_like_web_and_web_grant_covers_it():
    g, asker = make(answers=[Answer("y"), Answer("a")])
    assert g.check("browser_click", {"target": "Buy", "page_url": "http://x.test/"}) is None
    assert asker.requests[0].kind == "browse"
    assert asker.requests[0].subject == "http://x.test/ :: Buy"
    assert g.check("web_fetch", {"url": "http://y.test/"}) is None
    assert asker.requests[1].grant == WEB_GRANT
    assert g.check("browser_type", {"target": "q", "text": "hi", "page_url": "http://x.test/"}) is None
    assert g.check("browser_open", {"url": "http://z.test/"}) is None
    assert len(asker.requests) == 2


def test_browse_grant_covers_web():
    g, asker = make(answers=[Answer("a")])
    assert g.check("browser_press", {"key": "Enter", "page_url": "http://x/"}) is None
    assert g.check("web_search", {"query": "q"}) is None
    assert len(asker.requests) == 1


def test_web_deny_rule_applies_to_browser_open_and_page():
    g, asker = make(deny=["web_fetch:*evil*"])
    assert g.check("browser_open", {"url": "https://evil.com"}).startswith("Refused: the settings deny rule")
    assert g.check("browser_click", {"target": "b", "page_url": "https://evil.com/x"}).startswith("Refused")
    assert asker.requests == []


def test_browse_subjects():
    assert subject_of("browser_type", {"page_url": "http://x/", "target": "q", "text": "hi"}) == "http://x/ :: q <- hi"
    assert subject_of("browser_press", {"page_url": "http://x/", "key": "Enter"}) == "http://x/ :: Enter"
    assert subject_of("browser_back", {"page_url": "http://x/"}) == "http://x/ :: back"
    assert subject_of("send_to_terminal", {"id": "t", "text": "ls  -l"}) == "ls -l"
