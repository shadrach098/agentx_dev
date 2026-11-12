"""Security fixes in the tools and storage: the shell and taskkill are never taken from the project folder, grep
does not read through links that leave the project, web_fetch connects only to the address it checked, saved
conversations are owner-only, and a skill install never writes outside its staging folder. No network."""

import http.client
import http.server
import json
import os
import socket
import stat
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from agentx_dev import WebTools, safe_exe
from agentx_dev.DefaultTools import DefaultTools, Permissions
from agentx_dev.Session import Session
from agentx_dev.Skills.Install import SkillInstallError, Source, _inside, _skill_files_only
from agentx_dev.cli import sessions

DT = sys.modules["agentx_dev.DefaultTools"]          # the module (the package exports the class under that name)
ON_WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not ON_WINDOWS, reason="Windows looks programs up in the current folder")
posix_only = pytest.mark.skipif(ON_WINDOWS, reason="file modes are not kept on Windows")


# -- run_shell's interpreter and taskkill (#4) ---------------------------------------------------------------------

@pytest.fixture
def no_machine_git(monkeypatch):
    """Pretend Git for Windows is not in Program Files (it is on many machines that run these tests)."""
    real = os.path.isfile

    def isfile(path):
        text = str(path)
        if text.startswith((r"C:\Program Files\Git", r"C:\Program Files (x86)\Git")):
            return False
        return real(path)
    monkeypatch.setattr(os.path, "isfile", isfile)
    monkeypatch.delenv("AGENTX_SHELL", raising=False)


def _planted_folder(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    for name in ("bash.cmd", "bash.exe", "bash.bat", "powershell.exe", "taskkill.exe"):
        (project / name).write_text("@echo planted\n", encoding="utf-8")
    monkeypatch.chdir(project)
    return project


@windows_only
def test_bash_is_never_taken_from_the_project_folder(tmp_path, monkeypatch, no_machine_git):
    project = _planted_folder(tmp_path, monkeypatch)
    good = tmp_path / "tools"
    good.mkdir()
    (good / "bash.exe").write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-local-git"))
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(project), str(good)]))
    shell = DT._resolve_shell_interpreter()
    assert os.path.isabs(shell[0]) and Path(shell[0]).parent.resolve() == good.resolve()
    assert shell[1:] == ["-c"]


@windows_only
def test_a_per_user_git_install_is_found_before_path(tmp_path, monkeypatch, no_machine_git):
    _planted_folder(tmp_path, monkeypatch)
    local = tmp_path / "local"
    bash = local / "Programs" / "Git" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("PATH", ".")
    assert DT._resolve_shell_interpreter() == [str(bash), "-c"]


@windows_only
def test_with_no_bash_powershell_is_named_by_its_system32_path(tmp_path, monkeypatch, no_machine_git):
    project = _planted_folder(tmp_path, monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-local-git"))
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(project)]))
    shell = DT._resolve_shell_interpreter()
    assert shell == [safe_exe.powershell(), "-NoProfile", "-Command"]
    assert os.path.isabs(shell[0]) and "system32" in shell[0].lower()
    assert not Path(shell[0]).resolve().is_relative_to(project.resolve())


def test_agentx_shell_is_still_the_users_choice(monkeypatch):
    monkeypatch.setenv("AGENTX_SHELL", "mysh -x -c")
    assert DT._resolve_shell_interpreter() == ["mysh", "-x", "-c"]


@windows_only
def test_stopping_a_command_runs_the_system32_taskkill(tmp_path, monkeypatch):
    _planted_folder(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(DT.subprocess, "run", lambda argv, **kw: started.append(argv))

    class Proc:
        pid = 4242

        def kill(self):
            pass
    DT._kill_tree(Proc())
    argv = started[0]
    assert argv[0] == safe_exe.system32("taskkill.exe") and os.path.isabs(argv[0])
    assert argv[1:] == ["/T", "/F", "/PID", "4242"]


# -- grep and find_files do not read through links that leave the project (#14) ------------------------------------

def _tools(project):
    return {t.name: t for t in DefaultTools.build(Permissions.read_only([str(project)]))}


def _file_link(link, target):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"cannot create a file symlink here: {error}")


def _dir_link(link, target):
    try:
        if ON_WINDOWS:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, ImportError, AttributeError, NotImplementedError) as error:
        pytest.skip(f"cannot create a link here: {error}")


def test_grep_does_not_read_a_file_link_that_points_outside(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "inside.txt").write_text("TOPSECRET is a word here too\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("TOPSECRET outside\n", encoding="utf-8")
    _file_link(project / "notes.txt", outside)
    tools = _tools(project)
    with pytest.raises(PermissionError):
        tools["read_path"].func(path=str(project / "notes.txt"))
    out = tools["grep"].func(pattern="TOPSECRET", path=str(project))
    assert "inside.txt" in out and "outside" not in out
    found = tools["find_files"].func(glob="**/*.txt", path=str(project))
    assert "inside.txt" in found and "notes.txt" not in found


def test_grep_still_reads_a_file_link_that_stays_inside(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "real.txt").write_text("NEEDLE\n", encoding="utf-8")
    _file_link(project / "alias.txt", project / "real.txt")
    out = _tools(project)["grep"].func(pattern="NEEDLE", path=str(project))
    assert "alias.txt" in out and "real.txt" in out


def test_a_linked_folder_is_still_not_entered(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    (outdir / "s.txt").write_text("TOPSECRET2\n", encoding="utf-8")
    _dir_link(project / "dlink", outdir)
    assert "TOPSECRET2" not in _tools(project)["grep"].func(pattern="TOPSECRET2", path=str(project))


def test_a_link_out_of_the_sandbox_is_seen_as_outside(tmp_path):
    # A junction (Windows) or symlink is a reparse point: it is resolved and judged by where it goes.
    project = tmp_path / "proj"
    project.mkdir()
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    _dir_link(project / "j", outdir)
    (project / "plain.txt").write_text("x", encoding="utf-8")
    assert DT._links_outside(project / "j", [str(project)]) is True
    assert DT._links_outside(project / "plain.txt", [str(project)]) is False
    assert DT._links_outside(project / "j", None) is False          # no sandbox: nothing to keep out


# -- web_fetch connects only to the address it checked (#16) ---------------------------------------------------------

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"


class _Server:
    """A tiny server on 127.0.0.1 that records each request's path and Host header."""

    def __init__(self, routes):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Host")))
                code, headers, body = routes.get(self.path, (404, {}, b"no"))
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value.replace("{port}", str(port)))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def web(monkeypatch):
    """No proxy (the real opener reads the machine's proxy settings), a scripted DNS, and a record of every
    address a connection is made to. A connection to a 'public' test address goes to the local server only when
    the test says so; anything else is refused, so nothing leaves this computer."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), WebTools._GuardedRedirectHandler(),
                                         WebTools._PinnedHTTPHandler(), WebTools._PinnedHTTPSHandler())
    monkeypatch.setattr(WebTools, "_GUARDED_OPENER", opener)
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.create_connection
    state = {"answers": {}, "lookups": [], "connects": [], "served": set()}

    def getaddrinfo(host, port, *args, **kwargs):
        answers = state["answers"].get(host)
        if answers is None:
            return real_getaddrinfo(host, port, *args, **kwargs)
        state["lookups"].append(host)
        index = min(sum(1 for h in state["lookups"] if h == host) - 1, len(answers) - 1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[index], port or 0))]

    def create_connection(address, *args, **kwargs):
        state["connects"].append(address[0])
        if address[0] in state["served"]:
            return real_connect(("127.0.0.1", address[1]), *args, **kwargs)
        if address[0] == "127.0.0.1":
            return real_connect(address, *args, **kwargs)
        raise ConnectionRefusedError(f"test: no route to {address[0]}")
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    servers = []

    def serve(routes):
        servers.append(_Server(routes))
        return servers[-1]
    state["serve"] = serve
    yield state
    for server in servers:
        server.close()


def test_a_name_that_answers_public_then_loopback_never_reaches_loopback(web):
    server = web["serve"]({"/": (200, {}, b"LOOPBACK-SECRET")})
    web["answers"]["rebind.test"] = [PUBLIC, "127.0.0.1"]
    out = WebTools._do_fetch(f"http://rebind.test:{server.port}/", None, 1000)
    assert "LOOPBACK-SECRET" not in out and out.startswith("ERROR:")
    assert server.seen == []                                   # the loopback service was never asked
    assert web["connects"] == [PUBLIC]                         # only the checked address was dialled
    assert web["lookups"] == ["rebind.test"]                   # one lookup, the one that was checked


def test_a_name_that_answers_loopback_is_refused(web):
    server = web["serve"]({"/": (200, {}, b"LOOPBACK-SECRET")})
    web["answers"]["rebind.test"] = ["127.0.0.1"]
    out = WebTools._do_fetch(f"http://rebind.test:{server.port}/", None, 1000)
    assert out.startswith("ERROR: refused") and "LOOPBACK-SECRET" not in out
    assert server.seen == [] and web["connects"] == []


def test_the_connection_keeps_the_host_name_for_the_host_header(web):
    server = web["serve"]({"/page": (200, {}, b"hello")})
    web["answers"]["site.test"] = [PUBLIC]
    web["served"].add(PUBLIC)
    out = WebTools._do_fetch(f"http://site.test:{server.port}/page", None, 1000)
    assert out == "hello"
    assert server.seen == [("/page", f"site.test:{server.port}")]
    assert web["connects"] == [PUBLIC]


def test_a_redirect_hop_connects_to_the_address_checked_for_it(web):
    server = web["serve"]({"/start": (302, {"Location": "http://rebind2.test:{port}/secret"}, b""),
                           "/secret": (200, {}, b"LOOPBACK-SECRET")})
    web["answers"]["public.test"] = [PUBLIC]
    web["answers"]["rebind2.test"] = [PUBLIC_2, "127.0.0.1"]
    web["served"].add(PUBLIC)                                  # public.test is "on the internet" (the local server)
    out = WebTools._do_fetch(f"http://public.test:{server.port}/start", None, 1000)
    assert "LOOPBACK-SECRET" not in out
    assert [path for path, _ in server.seen] == ["/start"]
    assert web["connects"] == [PUBLIC, PUBLIC_2]


def test_a_redirect_to_a_loopback_name_is_refused(web):
    server = web["serve"]({"/start": (302, {"Location": "http://inside.test:{port}/secret"}, b""),
                           "/secret": (200, {}, b"LOOPBACK-SECRET")})
    web["answers"]["public.test"] = [PUBLIC]
    web["answers"]["inside.test"] = ["127.0.0.1"]
    web["served"].add(PUBLIC)
    out = WebTools._do_fetch(f"http://public.test:{server.port}/start", None, 1000)
    assert out.startswith("ERROR: refused") and "LOOPBACK-SECRET" not in out
    assert [path for path, _ in server.seen] == ["/start"]


def test_a_request_opened_without_a_stored_check_is_checked_where_it_connects(web):
    server = web["serve"]({"/": (200, {}, b"LOOPBACK-SECRET")})
    web["answers"]["rebind.test"] = ["127.0.0.1"]
    with pytest.raises(WebTools._Blocked):
        WebTools._GUARDED_OPENER.open(urllib.request.Request(f"http://rebind.test:{server.port}/"), timeout=5)
    assert server.seen == []


def test_an_ipv4_address_written_as_ipv6_is_judged_as_ipv4():
    assert WebTools._is_public("::ffff:127.0.0.1") is False
    assert WebTools._is_public("::ffff:" + PUBLIC) is True
    assert WebTools._is_public("not-an-address") is False


def test_https_keeps_the_host_name_for_sni_and_the_certificate():
    req = urllib.request.Request("https://site.test/")
    WebTools._pin(req, [PUBLIC])
    conn = WebTools._pinned_class(http.client.HTTPSConnection, req)("site.test", timeout=5)
    assert isinstance(conn, http.client.HTTPSConnection) and conn.host == "site.test"   # SNI and cert use .host
    assert conn._create_connection is not socket.create_connection


def test_through_a_proxy_nothing_is_pinned(monkeypatch):
    def no_lookup(*args, **kwargs):
        raise AssertionError("no lookup is made when a proxy carries the request")
    monkeypatch.setattr(socket, "getaddrinfo", no_lookup)
    plain = urllib.request.Request("http://site.test/")
    plain.set_proxy("proxy.example:3128", "http")
    assert WebTools._pinned_class(http.client.HTTPConnection, plain) is http.client.HTTPConnection
    tunnel = urllib.request.Request("https://site.test/")
    tunnel.set_proxy("proxy.example:3128", "http")
    assert WebTools._pinned_class(http.client.HTTPSConnection, tunnel) is http.client.HTTPSConnection


def test_the_opener_still_uses_a_proxy_from_the_environment_and_the_pinned_handlers(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
    kinds = [type(h) for h in WebTools._build_opener().handlers]
    assert urllib.request.ProxyHandler in kinds
    assert WebTools._PinnedHTTPHandler in kinds and WebTools._PinnedHTTPSHandler in kinds
    assert urllib.request.HTTPHandler not in kinds and urllib.request.HTTPSHandler not in kinds


# -- saved conversations and titles are owner-only (#22) -------------------------------------------------------------

def _session():
    return Session(history=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}])


def test_the_transcript_is_created_owner_only(tmp_path, monkeypatch):
    modes = {}
    real_open = os.open

    def recording_open(path, flags, mode=0o777, *args, **kwargs):
        modes[str(path)] = mode
        return real_open(path, flags, mode, *args, **kwargs)
    monkeypatch.setattr(os, "open", recording_open)
    target = tmp_path / "home" / ".agentx" / "sessions" / "p-12345678" / "20260101T000000Z-abcd1234.json"
    _session().save(target)
    assert modes[str(target) + ".tmp"] == 0o600
    assert Session.load(target).history[0]["content"] == "hello"
    assert not Path(str(target) + ".tmp").exists()


def test_the_title_file_is_created_owner_only(tmp_path, monkeypatch):
    modes = []
    real_open = os.open

    def recording_open(path, flags, mode=0o777, *args, **kwargs):
        modes.append((str(path), mode))
        return real_open(path, flags, mode, *args, **kwargs)
    monkeypatch.setattr(os, "open", recording_open)
    home, cwd = tmp_path / "home", tmp_path / "proj"
    cwd.mkdir()
    assert sessions.save_title(home, cwd, "20260101T000000Z-abcd1234.json", "A title") is True
    assert [mode for path, mode in modes if path.endswith(".tmp")] == [0o600]
    assert sessions.read_titles(home, cwd) == {"20260101T000000Z-abcd1234.json": "A title"}


@posix_only
def test_on_posix_the_transcript_and_its_new_folders_are_private_even_with_umask_022(tmp_path):
    old = os.umask(0o022)
    try:
        target = tmp_path / "home" / ".agentx" / "sessions" / "p-12345678" / "x.json"
        Path(str(target) + ".tmp").parent.mkdir(parents=True)
        Path(str(target) + ".tmp").write_text("{}")
        os.chmod(str(target) + ".tmp", 0o644)                  # a leftover temp file with a wide mode
        _session().save(target)
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
        fresh = tmp_path / "fresh" / "sessions" / "p-1" / "y.json"
        _session().save(fresh)
        assert stat.S_IMODE(os.stat(fresh).st_mode) == 0o600
        for folder in (fresh.parent, fresh.parent.parent, fresh.parent.parent.parent):
            assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
        home, cwd = tmp_path / "home2", tmp_path / "proj"
        cwd.mkdir()
        assert sessions.save_title(home, cwd, "x.json", "T") is True
        titles = sessions.titles_path(home, cwd)
        assert stat.S_IMODE(os.stat(titles).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(titles.parent).st_mode) == 0o700
    finally:
        os.umask(old)


def test_an_existing_folder_is_left_as_it_is(tmp_path):
    folder = tmp_path / "mine"
    folder.mkdir()
    before = os.stat(folder).st_mode
    _session().save(folder / "s.json")
    assert os.stat(folder).st_mode == before


# -- a skill install never writes outside its staging folder (#5) -----------------------------------------------------

SHA = "a" * 40


def _tree_get(files, fetched):
    def get(url, limit, accept=None):
        if "/git/trees/" in url:
            return json.dumps({"truncated": False, "tree": [
                {"path": p, "type": "blob", "mode": "100644", "size": len(d)} for p, d in files.items()]}).encode()
        if url.startswith("https://raw.githubusercontent.com/o/r/" + SHA + "/"):
            path = urllib.parse.unquote(url.split(SHA + "/", 1)[1])
            fetched.append(path)
            return files[path].encode()
        raise AssertionError(f"unexpected download: {url}")
    return get


SKILL = "---\nname: s\ndescription: d\n---\nBody\n"


@pytest.mark.parametrize("bad", ["s/D:x/marker.txt", "s/D:/marker.txt", "s/Q:x/marker.txt",
                                 "s/notes.txt:hidden", "s/sub/E:evil.txt"])
def test_a_drive_or_stream_in_any_part_refuses_the_install_and_writes_nothing(tmp_path, bad):
    fetched = []
    stage = tmp_path / "stage"
    with pytest.raises(SkillInstallError):
        _skill_files_only(Source("github", "o", "r"), SHA, _tree_get({"s/SKILL.md": SKILL, bad: "MARKER"}, fetched),
                          stage)
    assert fetched == []                                       # refused before anything was downloaded
    assert not stage.exists() or not any(p.is_file() for p in stage.rglob("*"))


@pytest.mark.parametrize("device", ["s/CON", "s/nul.txt", "s/CON .txt", "s/conout$", "s/COM\u00b9.md"])
def test_a_windows_device_name_is_skipped(tmp_path, device):
    fetched = []
    root = _skill_files_only(Source("github", "o", "r"), SHA,
                             _tree_get({"s/SKILL.md": SKILL, device: "x"}, fetched), tmp_path / "stage")
    assert fetched == ["s/SKILL.md"]
    assert (root / "s" / "SKILL.md").read_text(encoding="utf-8") == SKILL


def test_a_good_tree_still_installs(tmp_path):
    fetched = []
    files = {"s/SKILL.md": SKILL, "s/refs/a.md": "notes", "other/readme.md": "not a skill"}
    root = _skill_files_only(Source("github", "o", "r"), SHA, _tree_get(files, fetched), tmp_path / "stage")
    assert sorted(fetched) == ["s/SKILL.md", "s/refs/a.md"]
    assert (root / "s" / "refs" / "a.md").read_text(encoding="utf-8") == "notes"


def test_the_containment_check_follows_the_resolved_path(tmp_path):
    base = (tmp_path / "stage" / "r").resolve()
    assert _inside(tmp_path / "stage" / "r" / "s" / "a.md", base) is True
    assert _inside(tmp_path / "stage" / "r" / ".." / "x.md", base) is False
    assert _inside(tmp_path / "elsewhere.md", base) is False
