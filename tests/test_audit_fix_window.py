"""The app window and its server, after the security audit: helper programs are never taken from the project folder,
Open another folder imports nothing from the chosen folder, saving Settings keeps declined project rules off, model
links cannot add attributes, an approval card is never cut, the browser is started with a one-time code instead of
the key, a picture never goes through a planted .agentx link, cards do not take the focus while you type, and
remote off really ends what came through Tailscale."""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
from rich.console import Console

from agentx_dev.cli import webui
from agentx_dev.cli.app import ChatApp
from agentx_dev.cli.approval import Answer, Request, normalize_path
from agentx_dev.cli.preview import preview_for
from agentx_dev.cli.settings import load_settings
from agentx_dev.cli.webui import WebFront, _tailscale_exe as real_tailscale_exe   # the conftest stub comes later
from tests.conftest import MockModel

APP_JS = webui.RESOURCES / "app.js"


def _inside(path: str, folder: Path) -> bool:
    try:
        Path(os.path.abspath(path)).resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


# -- 1. helper programs are never taken from the project folder ----------------------------------------------------

def _plant(folder: Path, name: str) -> None:
    for ext in ("", ".cmd", ".bat", ".exe", ".com"):
        path = folder / (name + ext)
        path.write_text("@echo off\n" if ext in (".cmd", ".bat") else "#!/bin/sh\n", encoding="utf-8")
        if not ext:
            path.chmod(0o755)


def test_tailscale_is_never_found_in_the_project_folder(tmp_path, monkeypatch):
    _plant(tmp_path, "tailscale")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(tmp_path), os.environ.get("PATH", "")]))
    found = real_tailscale_exe()
    assert found is None or (os.path.isabs(found) and not _inside(found, tmp_path))


def test_the_browser_is_never_found_in_the_project_folder(tmp_path, monkeypatch):
    for name in ("msedge", "google-chrome", "chrome", "chromium"):
        _plant(tmp_path, name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(tmp_path)]))
    for key in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / "nothing-here"))
    monkeypatch.setattr(webui.sys, "platform", "linux")              # only the PATH lookup is left
    assert webui._app_window_command("http://127.0.0.1:1/?c=x") is None


def test_powershell_is_started_by_its_full_path(monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows only")
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Private|Any", stderr="")
    monkeypatch.setattr(webui.subprocess, "run", run)
    webui.windows_ssh_blocked()
    assert seen and os.path.isabs(seen[0][0]) and seen[0][0].lower().endswith(r"system32\windowspowershell\v1.0\powershell.exe")


# -- 2. Open another folder imports nothing from that folder -------------------------------------------------------

def _evil_folder(tmp_path: Path) -> "tuple[Path, Path]":
    evil, marker = tmp_path / "evil", tmp_path / "marker.txt"
    plant = f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n"
    for package in ("agentx_dev", "agentx_dev/cli", "rich"):
        (evil / package).mkdir(parents=True, exist_ok=True)
        (evil / package / "__init__.py").write_text(plant, encoding="utf-8")
    (evil / "agentx_dev" / "__main__.py").write_text(plant, encoding="utf-8")
    return evil, marker


def test_the_new_agentx_is_not_started_with_dash_m_in_the_folder(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    command, cwd = webui.open_folder_command(folder)
    assert "-m" not in command and Path(cwd).resolve() != folder.resolve()
    assert command[0] == sys.executable and command[-2] == str(folder)
    assert "main(['--ui'])" in command[command.index("-c") + 1]


@pytest.mark.parametrize("safe_path_flag", [True, False], ids=["python-3.11+", "python-3.10"])
def test_a_planted_package_in_the_chosen_folder_is_never_imported(tmp_path, safe_path_flag):
    evil, marker = _evil_folder(tmp_path)
    probe = webui.SAFE_START + "import agentx_dev.cli, rich\nprint(agentx_dev.__file__)\nprint(os.getcwd())\n"
    command, cwd = webui.open_folder_command(evil, code=probe)
    if not safe_path_flag:                                   # what Python 3.10 runs: the bootstrap alone
        command = [part for part in command if part != "-P"]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"}
    done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr[-2000:]
    package_file, where = done.stdout.strip().splitlines()[-2:]
    assert not marker.exists(), marker.read_text()
    assert not _inside(package_file, evil)
    assert Path(where).resolve() == evil.resolve()            # it still starts in the chosen folder


def test_open_folder_starts_the_safe_command(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append((a, k)))
    here, other = tmp_path / "here", tmp_path / "other"
    here.mkdir()
    other.mkdir()
    front = WebFront(cwd=here, home=tmp_path, open_it=False)
    assert front.open_folder(str(other))["ok"] is True
    (command,), kwargs = started[0]
    assert command == webui.open_folder_command(other.resolve())[0]
    assert kwargs["cwd"] == webui.open_folder_command(other.resolve())[1]


# -- 8. saving Settings keeps the project rules declined at start off ----------------------------------------------

def _project(tmp_path: Path, allow) -> "tuple[Path, Path]":
    proj, home = tmp_path / "proj", tmp_path / "home"
    (proj / ".agentx").mkdir(parents=True)
    home.mkdir()
    (proj / ".agentx" / "settings.json").write_text(json.dumps({"permissions": {"allow": allow}}), encoding="utf-8")
    return proj, home


def _app(proj: Path, home: Path, answer: str) -> ChatApp:
    settings = load_settings(proj, home)
    return ChatApp(cwd=proj, home=home, console=Console(file=open(os.devnull, "w")), read_line=lambda p: "",
                   read_short=lambda prompt: answer, env={}, model_factory=lambda name, env: MockModel(),
                   allow=settings.allow, project_allow=settings.project_allow)


def test_declined_project_rules_stay_off_after_saving_settings(tmp_path):
    proj, home = _project(tmp_path, ["run_shell"])
    app = _app(proj, home, "n")
    app._confirm_project_rules()
    assert "run_shell" not in app.gate.allow
    front = WebFront(cwd=proj, home=home, open_it=False)
    front.app = app
    saved = front.save_settings("user", {"maxSteps": 50})
    assert saved["ok"] is True and "turned down at start stay off" in saved["message"]
    assert "run_shell" not in app.gate.allow and app.max_steps == 50
    asked = []
    app.gate.ask = lambda request: asked.append(request) or Answer("n", "")
    assert app.gate.check("run_shell", {"command": "echo hi"}) is not None and asked


def test_accepted_project_rules_stay_on_and_new_ones_in_the_file_do_not_come_on(tmp_path):
    proj, home = _project(tmp_path, ["run_shell"])
    app = _app(proj, home, "y")
    app._confirm_project_rules()
    front = WebFront(cwd=proj, home=home, open_it=False)
    front.app = app
    # something else adds a rule to the project file after start: nobody was asked about it
    (proj / ".agentx" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["run_shell", "write_file"]}}),
                                                    encoding="utf-8")
    assert front.save_settings("user", {"maxSteps": 40})["ok"] is True
    assert "run_shell" in app.gate.allow and "write_file" not in app.gate.allow


def test_a_rule_typed_in_the_folder_tab_takes_effect_but_a_declined_one_does_not(tmp_path):
    proj, home = _project(tmp_path, ["run_shell"])
    app = _app(proj, home, "n")
    app._confirm_project_rules()
    front = WebFront(cwd=proj, home=home, open_it=False)
    front.app = app
    assert front.save_settings("folder", {"allow": ["run_shell", "read_path"]})["ok"] is True
    assert "read_path" in app.gate.allow and "run_shell" not in app.gate.allow


def test_the_users_own_copy_of_a_declined_rule_stays(tmp_path):
    proj, home = _project(tmp_path, ["run_shell"])
    (home / ".agentx").mkdir()
    (home / ".agentx" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["run_shell"]}}),
                                                    encoding="utf-8")
    app = _app(proj, home, "n")
    app._confirm_project_rules()
    front = WebFront(cwd=proj, home=home, open_it=False)
    front.app = app
    front.save_settings("user", {"maxSteps": 30})
    assert app.gate.allow.count("run_shell") == 1


# -- 12. links in the model's text cannot add attributes -------------------------------------------------------------

class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))


def _inline(texts):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = APP_JS.read_text(encoding="utf-8")
    esc = next(line for line in script.splitlines() if line.strip().startswith("const esc ="))
    start = script.index("  const URL_PART")                     # the link patterns, then inline() itself
    end = script.index("\n  }\n", script.index("  function inline(text) {")) + len("\n  }\n")
    program = (esc + "\n" + script[start:end] +
               "\nlet data = '';process.stdin.on('data', (c) => data += c);"
               "process.stdin.on('end', () => console.log(JSON.stringify(JSON.parse(data).map(inline))));\n")
    done = subprocess.run([node, "-e", program], input=json.dumps(texts), capture_output=True, text=True,
                          timeout=60, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _links(html: str):
    parser = _Tags()
    parser.feed(html)
    parser.close()
    for tag, attrs in parser.tags:
        assert tag in ("a", "code", "strong", "em"), (tag, html)
        if tag == "a":
            assert [name for name, _ in attrs] == ["href", "target", "rel"], html
            assert dict(attrs)["target"] == "_blank" and dict(attrs)["rel"] == "noopener noreferrer"
            assert dict(attrs)["href"].startswith(("http://", "https://")), html
    return [dict(attrs)["href"] for tag, attrs in parser.tags if tag == "a"]


def test_a_link_inside_a_link_cannot_add_attributes():
    attacks = ["[t](https://a.test/(https://b.test/x/data-probe=1)",
               "[t](https://a.test/(https://b.test/x/style=outline:3px;position:fixed;inset:0;;)",
               '[t](https://a.test/x"onmouseover=1) https://b.test/"style=x',
               "(https://a.test/(https://b.test/style=x;)",
               "[t](https://a.test/\u00010\u0001) https://b.test/y",
               "[**a** https://x.test/](https://a.test/q?x=1&y=2) and `https://c.test/(d` too"]
    for text, html in zip(attacks, _inline(attacks)):
        for href in _links(html):                      # each <a>: exactly href, target and rel
            assert "<" not in href and '"' not in href and "(" not in href, (text, html)


def test_plain_links_still_work():
    texts = ["see [the docs](https://example.com/a?b=1&c=2) now", "at https://example.com/x, ok",
             "(https://example.com/y)", "**bold** and `code` and *em*"]
    out = _inline(texts)
    assert _links(out[0]) == ["https://example.com/a?b=1&c=2"] and ">the docs</a>" in out[0]
    assert _links(out[1]) == ["https://example.com/x,"]           # as before: a trailing comma stays in the link
    assert _links(out[2]) == ["https://example.com/y"]
    assert "<strong>bold</strong>" in out[3] and "<code>code</code>" in out[3] and "<em>em</em>" in out[3]


# -- 13. an approval card is never cut ------------------------------------------------------------------------------

def test_a_title_too_long_to_show_whole_is_declined(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("x", encoding="utf-8")
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
    path = "./" * 1000 + "src"
    request = Request(tool="delete_path", kind="destructive", subject=normalize_path(path, tmp_path),
                      args={"path": path, "recursive": True}, remember=False, grant="")
    assert "everything inside it" in preview_for("delete_path", request.args, tmp_path).title
    fields = front.approval_fields(request)
    assert isinstance(fields, Answer) and fields.choice == "n" and "too long" in fields.reason
    assert any(e["type"] == "notice" and "Declined without asking" in e["text"] for e in front.bus.after(0))
    short = Request(tool="delete_path", kind="destructive", subject="src", args={"path": "src", "recursive": True},
                    remember=False, grant="")
    shown = front.approval_fields(short)
    assert isinstance(shown, dict) and "everything inside it" in shown["title"]


def test_a_body_too_long_to_show_whole_is_declined(tmp_path):
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
    request = Request(tool="write_file", kind="edit", subject="big.txt",
                      args={"path": "big.txt", "content": "\n".join(["x" * 2000] * 300)}, remember=False, grant="")
    assert preview_for("write_file", request.args, tmp_path).body.rstrip().endswith("more lines")
    fields = front.approval_fields(request)
    assert isinstance(fields, Answer) and fields.choice == "n"


def test_dont_ask_again_is_offered_only_for_a_grant_shown_whole(tmp_path):
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
    long_grant = "echo " + "a" * 400
    request = Request(tool="run_shell", kind="command", subject=long_grant, args={"command": long_grant},
                      remember=True, grant=long_grant)
    fields = front.approval_fields(request)
    assert isinstance(fields, dict) and fields["always"] is False
    ok = Request(tool="run_shell", kind="command", subject="echo hi", args={"command": "echo hi"},
                 remember=True, grant="echo")
    assert front.approval_fields(ok)["always"] is True


# -- 20. the browser is started with a one-time code, not the key ----------------------------------------------------

def _post(port, path, body, host=None, content_type="application/json", token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}", "Content-Type": content_type}
    if token:
        headers["X-Agentx-Token"] = token
    conn.request("POST", path, body=json.dumps(body), headers=headers)
    response = conn.getresponse()
    status, data = response.status, json.loads(response.read() or b"{}")
    conn.close()
    return status, data


def test_the_browser_gets_a_code_not_the_key(tmp_path):
    opened = []
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=True, opener=lambda url: opened.append(url) or "")
    assert front.run(lambda: 0) == 0
    assert opened and "?c=" in opened[0] and front.token not in opened[0] and "t=" not in opened[0]


def test_the_code_is_traded_for_the_key_once(tmp_path):
    said = []
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
    front.on_window = said.append
    front.start_server()
    port = front._server.server_address[1]
    try:
        code = front.launch_address().rpartition("?c=")[2]
        first_key = front.token
        assert _post(port, "/api/launch", {"code": code}) == (200, {"token": first_key})
        assert _post(port, "/api/launch", {"code": "not-the-code"})[0] == 403
        status, data = _post(port, "/api/launch", {"code": code})          # the same code again
        assert status == 403 and data == {"error": "used"}
        assert front.token != first_key                                  # the key was changed
        assert said and "used a second time" in said[0] and front.token in said[0]
        assert _post(port, "/api/send", {"text": "hi"}, token=first_key)[0] == 403
        assert _post(port, "/api/launch", {"code": code}, content_type="text/plain")[0] == 415
    finally:
        front.close()


def test_an_old_code_has_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "LAUNCH_CODE_SEC", -1.0)
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
    code = front.launch_address().rpartition("?c=")[2]
    key = front.token
    assert front.claim_launch(code) == {"error": "expired"} and front.token == key


def test_a_new_key_ends_the_streams_opened_with_the_old_one(tmp_path):
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
    front.start_server()
    port = front._server.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/events?t={front.token}", headers={"Host": f"127.0.0.1:{port}"})
        response = conn.getresponse()
        assert response.status == 200
        time.sleep(0.2)
        front.new_token()
        seen = ""
        deadline = time.monotonic() + 8
        while "keyChanged" not in seen and time.monotonic() < deadline:
            seen += response.readline().decode()
        assert '"keyChanged": true' in seen
        conn.close()
    finally:
        front.close()


def test_the_page_trades_the_code():
    script = APP_JS.read_text(encoding="utf-8")
    assert 'fetch("/api/launch"' in script and "keepKey()" in script


# -- 21. a picture never goes through a planted .agentx link --------------------------------------------------------

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n-dummy").decode()


def _link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import _winapi
        try:
            _winapi.CreateJunction(str(target), str(link))
            return
        except OSError:
            pass
    pytest.skip("no way to make a folder link here")


@pytest.mark.parametrize("which", [".agentx", ".agentx/uploads"])
def test_an_upload_does_not_follow_a_planted_link(tmp_path, which):
    proj, outside, home = tmp_path / "proj", tmp_path / "outside", tmp_path / "home"
    for folder in (proj, outside, home):
        folder.mkdir()
    if which == ".agentx/uploads":
        (proj / ".agentx").mkdir()
    _link(proj / which, outside)
    front = WebFront(cwd=proj, home=home, open_it=False)
    result = front.upload("a.png", PNG, "image/png")
    assert "path" in result, result
    assert not list(outside.rglob("*.png"))
    saved = Path(result["path"])
    assert saved.is_absolute() and saved.is_file() and _inside(str(saved), home / ".agentx" / "uploads")


def test_an_upload_in_a_plain_project_stays_there(tmp_path):
    front = WebFront(cwd=tmp_path, home=tmp_path / "home", open_it=False)
    result = front.upload("shot.png", PNG, "image/png")
    assert result["path"].startswith(".agentx/uploads/") and (tmp_path / result["path"]).is_file()


# -- deferred A: a card never takes the focus while you type; it shows the default it is given ----------------------

def test_a_card_does_not_take_the_focus_while_you_type():
    script = APP_JS.read_text(encoding="utf-8")
    on_ask = script[script.index("  function onAsk(ev) {"):script.index("  function onAnswered(ev) {")]
    assert "if (!typingNow())" in on_ask and ".focus(" in on_ask
    assert "!!input.value.trim()" in script                       # a message in the box counts as typing
    keys = script[script.index("// keys for the open card"):]
    assert "if (typing || e.ctrlKey" in keys                      # card keys stay off in a text field


def test_a_question_keeps_the_default_it_is_given(tmp_path):
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
    box = {}
    thread = threading.Thread(target=lambda: box.update(answer=front.select(["Yes, I trust it", "No, exit"],
                                                                            default=1)), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    asked = []
    while not asked and time.monotonic() < deadline:
        asked = [e for e in front.bus.after(0) if e["type"] == "ask"]
        time.sleep(0.01)
    assert asked and asked[0]["default"] == 1
    front.answer(asked[0]["id"], 1)
    thread.join(5)
    assert box["answer"] == 1


# -- deferred B: remote off ------------------------------------------------------------------------------------------

def test_remote_off_during_a_slow_check_is_never_undone(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "SHARE_CHECK_SEC", 0.01)
    entered, release = threading.Event(), threading.Event()

    def slow_ports():
        entered.set()
        release.wait(5)
        return set()                                  # "not shared any more": the loop would share it again
    served, unserved = [], []
    monkeypatch.setattr(webui, "tailscale_agentx_ports", slow_ports)
    monkeypatch.setattr(webui, "tailscale_serve", lambda port: served.append(port) or "")
    monkeypatch.setattr(webui, "tailscale_unserve", lambda port: unserved.append(port))
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False, remote=True)
    front._serving = 14902
    thread = threading.Thread(target=front._keep_shared, args=(14902, front._share_turn), daemon=True)
    thread.start()
    assert entered.wait(5)
    front.disable_remote()                            # while Tailscale is being asked
    release.set()
    thread.join(5)
    assert not thread.is_alive() and served == [] and unserved == [14902]


def test_remote_off_ends_the_windows_that_came_through_tailscale(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "tailscale_serve", lambda port: "")
    monkeypatch.setattr(webui, "tailscale_unserve", lambda port: None)
    monkeypatch.setattr(webui, "tailscale_clear_leftovers", lambda keep=None: [])
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False, remote=True)
    front.tailnet = "pc.tail1.ts.net"
    front.start_server()
    port = front._server.server_address[1]
    try:
        def stream(host, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", f"/events?t={front.token}", headers={"Host": host, **(headers or {})})
            response = conn.getresponse()
            assert response.status == 200
            return conn, response
        far_conn, far = stream(f"pc.tail1.ts.net:{port}", {"Tailscale-User-Login": "me@example.com"})
        near_conn, near = stream(f"127.0.0.1:{port}")
        deadline = time.monotonic() + 5
        while len(front._streams) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        # a device that came through Tailscale has its own conversation (a room): it is put away
        room = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
        room.host, room.device = front, "device-ipad-1"
        front.rooms[room.device] = room
        with front._lock:
            front._streams.append({"tailnet": True, "device": room.device, "stop": ""})
        message = front.disable_remote()
        assert "windows opened through it were closed" in message and "same link" in message
        seen = ""
        deadline = time.monotonic() + 8
        while "remoteOff" not in seen and time.monotonic() < deadline:
            seen += far.readline().decode()
        assert '"remoteOff": true' in seen
        assert any(e["type"] == "closed" and e.get("remoteOff") for e in room.bus.after(0)) and room.idle
        time.sleep(0.3)
        assert any(not s["tailnet"] for s in front._streams)      # the window on this computer stays
        far_conn.close()
        near_conn.close()
    finally:
        front.close()
