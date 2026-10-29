"""Installing skills from a link or a folder the user gives. No network: the downloader is passed in."""

import io
import json
import urllib.request
import zipfile
from pathlib import Path

import pytest

from agentx_dev.Skills.Defaults import bundled_skills
from agentx_dev.Skills.Install import (
    MAX_ARCHIVE_FILES, MAX_FILE_BYTES, Fetched, SkillInstallError, _SameHosts, discover, extract_zip, install,
    open_source, parse_source, preview_text, remove_skill,
)
from agentx_dev.Skills.Store import SIDECAR, SkillStore

SHA = "a" * 40


def skill_text(name, description="Does a thing", body="Body"):
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}\n"


def make_repo(root: Path) -> Path:
    for name in ("alpha", "beta"):
        (root / "skills" / name).mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text(skill_text(name), encoding="utf-8")
    (root / "skills" / "alpha" / "scripts").mkdir()
    (root / "skills" / "alpha" / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    (root / "README.md").write_text("not a skill", encoding="utf-8")
    return root


def make_zip(entries) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


class TestParseSource:
    def test_github_links(self):
        s = parse_source("https://github.com/obra/superpowers")
        assert (s.kind, s.owner, s.repo, s.ref, s.subpath) == ("github", "obra", "superpowers", "", "")
        assert parse_source("https://github.com/obra/superpowers.git/").repo == "superpowers"
        s = parse_source("https://github.com/o/r/tree/main/skills/pdf")
        assert (s.ref, s.subpath) == ("main", "skills/pdf")

    def test_a_folder(self, tmp_path):
        s = parse_source(str(tmp_path))
        assert s.kind == "local" and s.path == tmp_path.resolve()

    @pytest.mark.parametrize("text", [
        "", "   ", "https://evil.example/o/r", "http://github.com/o/r", "git@github.com:o/r.git",
        "https://github.com/o", "https://github.com/o/r/tree/main/../x", "no/such/folder/here",
    ])
    def test_anything_else_is_refused(self, text):
        with pytest.raises(SkillInstallError):
            parse_source(text)


class TestUnpacking:
    def test_unpacks_and_returns_the_repository_root(self, tmp_path):
        root = extract_zip(make_zip([("repo-abc/skills/a/SKILL.md", skill_text("a"))]), tmp_path)
        assert root.name == "repo-abc" and (root / "skills" / "a" / "SKILL.md").is_file()

    @pytest.mark.parametrize("name", ["../evil.txt", "/abs.txt", "C:/x.txt", "a/../../b.txt", "a\\..\\..\\b.txt"])
    def test_unsafe_paths_are_refused(self, tmp_path, name):
        with pytest.raises(SkillInstallError):
            extract_zip(make_zip([("repo/ok.txt", "1"), (name, "x")]), tmp_path / "out")
        assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "b.txt").exists()

    def test_not_an_archive(self, tmp_path):
        with pytest.raises(SkillInstallError):
            extract_zip(b"this is not a zip", tmp_path)

    def test_too_many_files(self, tmp_path):
        data = make_zip([(f"r/f{i}.txt", "x") for i in range(MAX_ARCHIVE_FILES + 1)])
        with pytest.raises(SkillInstallError):
            extract_zip(data, tmp_path)

    def test_one_huge_file_is_skipped_not_unpacked(self, tmp_path):
        root = extract_zip(make_zip([("r/big.bin", "x" * (MAX_FILE_BYTES + 1)), ("r/small.txt", "ok")]), tmp_path)
        assert not (root / "big.bin").exists() and (root / "small.txt").read_text() == "ok"

    def test_links_in_an_archive_are_not_unpacked(self, tmp_path):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("r/link.txt")
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "/etc/passwd")
            archive.writestr("r/real.txt", "ok")
        root = extract_zip(buffer.getvalue(), tmp_path)
        assert not (root / "link.txt").exists() and (root / "real.txt").exists()


class TestHostileArchives:
    @pytest.mark.parametrize("name", ["CON", "nul.txt", "sub/AUX", "com1", "LPT9.md", "prn.tar.gz"])
    def test_windows_device_names_are_not_written(self, tmp_path, name):
        # on Windows, opening ".../CON" writes to the console instead of making a file
        root = extract_zip(make_zip([("repo/ok.txt", "1"), (f"repo/{name}", "x" * 50)]), tmp_path)
        assert (root / "ok.txt").exists()
        assert not [p for p in root.rglob("*") if p.is_file() and p.name != "ok.txt"]

    def test_names_that_merely_start_like_a_device_are_kept(self, tmp_path):
        root = extract_zip(make_zip([("repo/console.md", "1"), ("repo/auxiliary.py", "2")]), tmp_path)
        assert (root / "console.md").exists() and (root / "auxiliary.py").exists()

    def test_a_file_where_a_later_entry_needs_a_folder_is_a_clear_error(self, tmp_path):
        with pytest.raises(SkillInstallError):
            extract_zip(make_zip([("repo/a", "file"), ("repo/a/b.txt", "x")]), tmp_path)

    def test_an_unreadable_skill_is_reported_not_raised(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path / "repo")
        import agentx_dev.Skills.Install as install_module
        real = install_module.load_skill

        def flaky(folder, *args):
            if folder.name == "beta":
                raise PermissionError("denied")
            return real(folder, *args)
        monkeypatch.setattr(install_module, "load_skill", flaky)
        found, problems = discover(repo)
        assert [c.name for c in found] == ["alpha"] and "PermissionError" in problems[0]


class TestRedirects:
    def test_only_github_over_https(self):
        handler = _SameHosts()
        request = urllib.request.Request("https://codeload.github.com/o/r/zip/x")
        for target in ("https://evil.example/x", "http://github.com/x", "https://github.com.evil.example/x"):
            with pytest.raises(SkillInstallError):
                handler.redirect_request(request, None, 302, "Found", {}, target)


class TestOpenSource:
    def fake_get(self, calls):
        def get(url, limit, accept=None):
            calls.append(url)
            if url.startswith("https://api.github.com/"):
                return SHA.encode() + b"\n"
            return make_zip([(f"repo-{SHA}/skills/a/SKILL.md", skill_text("a")),
                             (f"repo-{SHA}/skills/b/SKILL.md", skill_text("b"))])
        return get

    def test_a_github_link_pins_the_commit_then_downloads_that_exact_commit(self):
        calls = []
        with open_source("https://github.com/o/r/tree/main/skills", get=self.fake_get(calls)) as fetched:
            found, problems = discover(fetched.root, fetched.subpath)
            assert [c.name for c in found] == ["a", "b"] and problems == []
            assert fetched.commit == SHA and fetched.label == "https://github.com/o/r"
            root = fetched.root
        assert calls == ["https://api.github.com/repos/o/r/commits/main", f"https://codeload.github.com/o/r/zip/{SHA}"]
        assert not root.exists()                       # the download is removed afterwards

    def test_only_the_skill_folders_are_unpacked_and_a_skills_assets_come_along(self):
        # like latent-spaces/brag: demo videos next to the skills, music files of a few megabytes inside one
        seen = []

        def get(url, limit, accept=None):
            if url.startswith("https://api.github.com/"):
                return SHA.encode()
            return make_zip([(f"repo-{SHA}/examples/demo/brag.mp4", b"v" * (MAX_FILE_BYTES + 5)),
                             (f"repo-{SHA}/docs/readme.md", "docs"),
                             (f"repo-{SHA}/skills/brag/SKILL.md", skill_text("brag")),
                             (f"repo-{SHA}/skills/brag/assets/music.mp3", b"m" * (3 * 1024 * 1024)),
                             (f"repo-{SHA}/skills/brag-slim/SKILL.md", skill_text("brag-slim"))])
        with open_source("https://github.com/o/r", get=get) as fetched:
            seen = sorted(p.relative_to(fetched.root).as_posix() for p in fetched.root.rglob("*") if p.is_file())
            found, problems = discover(fetched.root, fetched.subpath)
        assert seen == ["skills/brag-slim/SKILL.md", "skills/brag/SKILL.md", "skills/brag/assets/music.mp3"]
        assert [c.name for c in found] == ["brag", "brag-slim"] and problems == []
        assert "assets/music.mp3" in found[0].files

    def test_a_folder_link_unpacks_only_that_folder(self):
        def get(url, limit, accept=None):
            if url.startswith("https://api.github.com/"):
                return SHA.encode()
            return make_zip([(f"repo-{SHA}/skills/a/SKILL.md", skill_text("a")),
                             (f"repo-{SHA}/skills/b/SKILL.md", skill_text("b"))])
        with open_source("https://github.com/o/r/tree/main/skills/b", get=get) as fetched:
            assert not (fetched.root / "skills" / "a").exists()
            assert [c.name for c in discover(fetched.root, fetched.subpath)[0]] == ["b"]

    def test_a_download_over_the_limit_says_what_the_limit_is(self):
        from agentx_dev.Skills.Install import http_get

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b"x" * n

        class Opener:
            def open(self, request, timeout):
                return Response()
        import agentx_dev.Skills.Install as module
        original = module.urllib.request.build_opener
        module.urllib.request.build_opener = lambda *a: Opener()
        try:
            with pytest.raises(SkillInstallError, match="over the 2 MB limit"):
                http_get("https://codeload.github.com/o/r/zip/x", 2 * 1024 * 1024)
        finally:
            module.urllib.request.build_opener = original

    def test_a_reply_that_is_not_a_commit_stops_it(self):
        with pytest.raises(SkillInstallError):
            with open_source("https://github.com/o/r", get=lambda url, limit, accept=None: b"<html>no</html>"):
                pass

    def test_a_local_folder_is_used_in_place(self, tmp_path):
        make_repo(tmp_path / "repo")
        with open_source(str(tmp_path / "repo")) as fetched:
            assert fetched.root == (tmp_path / "repo").resolve() and fetched.commit == ""
        assert (tmp_path / "repo" / "README.md").exists()


class TestDiscover:
    def test_finds_every_skill_folder(self, tmp_path):
        found, problems = discover(make_repo(tmp_path / "repo"))
        assert [c.name for c in found] == ["alpha", "beta"] and problems == []
        alpha = found[0]
        assert alpha.files == ("SKILL.md", "scripts/run.py") and alpha.scripts == ("scripts/run.py",)

    def test_a_subpath_narrows_it_and_cannot_leave_the_repository(self, tmp_path):
        repo = make_repo(tmp_path / "repo")
        assert [c.name for c in discover(repo, "skills/beta")[0]] == ["beta"]
        with pytest.raises(SkillInstallError):
            discover(repo, "../")
        with pytest.raises(SkillInstallError):
            discover(repo, "nope")

    def test_broken_and_duplicate_skills_are_reported_not_fatal(self, tmp_path):
        repo = make_repo(tmp_path / "repo")
        (repo / "other" / "bad").mkdir(parents=True)
        (repo / "other" / "bad" / "SKILL.md").write_text("no front matter", encoding="utf-8")
        (repo / "other" / "dup").mkdir()
        (repo / "other" / "dup" / "SKILL.md").write_text(skill_text("alpha"), encoding="utf-8")
        found, problems = discover(repo)
        assert [c.name for c in found] == ["alpha", "beta"] and len(problems) == 2

    def test_a_skills_sub_folders_are_files_not_more_skills(self, tmp_path):
        repo = make_repo(tmp_path / "repo")
        (repo / "skills" / "alpha" / "examples" / "inner").mkdir(parents=True)
        (repo / "skills" / "alpha" / "examples" / "inner" / "SKILL.md").write_text(skill_text("inner"), encoding="utf-8")
        assert [c.name for c in discover(repo)[0]] == ["alpha", "beta"]


class TestPreview:
    def test_shows_names_descriptions_commit_and_the_program_files_warning(self, tmp_path):
        found, _ = discover(make_repo(tmp_path / "repo"))
        text = preview_text(Fetched(tmp_path, "https://github.com/o/r", SHA), found)
        assert "Found 2 skills in https://github.com/o/r (commit aaaaaaaaaa)" in text
        assert "alpha - Does a thing (2 files)" in text and "scripts/run.py" in text
        assert "Install only ones you trust" in text

    def test_downloaded_text_is_made_one_printable_line(self, tmp_path):
        folder = tmp_path / "repo" / "s"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(skill_text("s", description="one \x1b[31m red " + "x" * 500), encoding="utf-8")
        found, _ = discover(tmp_path / "repo")
        text = preview_text(Fetched(tmp_path, "label"), found)
        assert "\x1b" not in text and max(len(line) for line in text.splitlines()) < 300


class TestInstall:
    def fetched(self, tmp_path):
        return Fetched(tmp_path / "repo", "https://github.com/o/r", SHA)

    def test_copies_the_files_and_writes_our_own_sidecar(self, tmp_path):
        repo = make_repo(tmp_path / "repo")
        (repo / "skills" / "alpha" / SIDECAR).write_text(json.dumps({"managed": True}), encoding="utf-8")  # a download's claim
        found, _ = discover(repo)
        target = tmp_path / "skills"
        assert install(found, target, self.fetched(tmp_path)) == ["alpha", "beta"]
        assert (target / "alpha" / "scripts" / "run.py").read_text() == "print(1)"
        sidecar = json.loads((target / "alpha" / SIDECAR).read_text(encoding="utf-8"))
        assert sidecar["managed"] is False and sidecar["source"] == "https://github.com/o/r"
        assert sidecar["commit"] == SHA
        store = SkillStore([(target, "user")])
        assert {s.name: s.source for s in store.all()} == {"alpha": "installed", "beta": "installed"}
        assert not [p for p in target.iterdir() if p.name.startswith(".incoming")]

    def test_a_name_that_is_taken_stops_everything_before_anything_is_written(self, tmp_path):
        found, _ = discover(make_repo(tmp_path / "repo"))
        target = tmp_path / "skills"
        (target / "beta").mkdir(parents=True)
        with pytest.raises(SkillInstallError, match="already"):
            install(found, target, self.fetched(tmp_path))
        assert not (target / "alpha").exists()

    def test_a_download_cannot_take_the_name_of_a_built_in_skill(self, tmp_path):
        folder = tmp_path / "repo" / "x"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(skill_text(bundled_skills()[0].name), encoding="utf-8")
        found, _ = discover(tmp_path / "repo")
        with pytest.raises(SkillInstallError, match="built-in"):
            install(found, tmp_path / "skills", self.fetched(tmp_path))

    def test_remove(self, tmp_path):
        found, _ = discover(make_repo(tmp_path / "repo"))
        target = tmp_path / "skills"
        install(found, target, self.fetched(tmp_path))
        remove_skill("alpha", target)
        assert not (target / "alpha").exists() and (target / "beta").exists()

    @pytest.mark.parametrize("name", ["..", "../x", "a/b", "", "UPPER", "nope"])
    def test_remove_refuses_anything_that_is_not_a_skill_folder(self, tmp_path, name):
        (tmp_path / "skills").mkdir()
        (tmp_path / "x").mkdir()
        with pytest.raises(SkillInstallError):
            remove_skill(name, tmp_path / "skills")
        assert (tmp_path / "x").exists()
