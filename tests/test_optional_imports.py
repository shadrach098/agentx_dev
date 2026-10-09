"""An optional package that is missing, or installed but failing to import, is reported as such.

A real report: ChromaVectorStore said "requires the chromadb package" while chromadb was installed.
Importing it failed inside protobuf because the user's own google.py hid the real ``google``
package, and the catch-all hid that. The message must tell the two cases apart."""

import asyncio
import builtins
import sys

import pytest

from agentx_dev._optional import optional_import_error

GOOGLE_SHADOW = ModuleNotFoundError("No module named 'flask'", name="flask")


def fail_imports(monkeypatch, failing, error):
    """Make ``import <name>`` raise ``error`` for the names in ``failing`` (and their submodules)."""
    real = builtins.__import__

    def fake(name, *args, **kwargs):
        if name.split(".")[0] in failing:
            raise error
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)


def missing(name):
    return ModuleNotFoundError(f"No module named '{name}'", name=name)


class TestHelper:
    def test_not_installed_names_the_extra_and_the_interpreter(self):
        err = optional_import_error("ChromaVectorStore", missing("chromadb"),
                                    modules=("chromadb",), pip="chromadb", extra="chroma")
        text = str(err)
        assert isinstance(err, ImportError)
        assert 'pip install "agentx-dev[chroma]"' in text and "pip install chromadb" in text
        assert sys.executable in text

    def test_installed_but_failing_shows_the_real_error_and_not_an_install_command(self):
        err = optional_import_error("ChromaVectorStore", GOOGLE_SHADOW,
                                    modules=("chromadb",), pip="chromadb", extra="chroma")
        text = str(err)
        assert "flask" in text and "ModuleNotFoundError" in text
        assert "installed" in text and "google.py" in text
        assert "pip install" not in text

    def test_a_missing_dependency_of_the_package_is_not_called_a_missing_package(self):
        # chromadb itself is present; one of ITS dependencies is not
        err = optional_import_error("ChromaVectorStore", missing("onnxruntime"),
                                    modules=("chromadb",), pip="chromadb", extra="chroma")
        assert "onnxruntime" in str(err) and "pip install chromadb" not in str(err)

    def test_a_plain_importerror_is_shown_as_is(self):
        err = optional_import_error("QdrantVectorStore", ImportError("DLL load failed while importing x"),
                                    modules=("qdrant_client",), pip="qdrant-client", extra="qdrant")
        assert "DLL load failed" in str(err) and "pip install" not in str(err)

    def test_without_an_extra_only_the_package_is_named(self):
        err = optional_import_error("PgVectorStore", missing("psycopg2"),
                                    modules=("psycopg2",), pip="psycopg2-binary")
        assert "pip install psycopg2-binary" in str(err) and "agentx-dev[" not in str(err)


class FakeEmbeddings:
    pass


def make_chroma():
    from agentx_dev.VectorStores.chroma_store import ChromaVectorStore
    ChromaVectorStore(embeddings=FakeEmbeddings())


def make_qdrant():
    from agentx_dev.VectorStores.qdrant_store import QdrantVectorStore
    QdrantVectorStore(embeddings=FakeEmbeddings())


def make_pg():
    from agentx_dev.VectorStores.pg_store import PgVectorStore
    PgVectorStore(FakeEmbeddings(), dsn="postgresql://x")


def make_claude():
    from agentx_dev import Claude
    Claude(model="claude-sonnet-5-5", api_key="test-key")


def make_otel():
    from agentx_dev.Observability import OTelHook
    OTelHook()


def make_mcp_stdio():
    from agentx_dev.MCP import MCPClient
    asyncio.run(MCPClient.connect_stdio("python"))


def make_mcp_http():
    from agentx_dev.MCP import MCPClient
    asyncio.run(MCPClient.connect_http("http://localhost:1"))


CASES = [
    ("chroma", make_chroma, ("chromadb",), 'agentx-dev[chroma]'),
    ("qdrant", make_qdrant, ("qdrant_client",), 'agentx-dev[qdrant]'),
    ("pgvector", make_pg, ("psycopg", "psycopg2"), 'agentx-dev[pgvector]'),
    ("claude", make_claude, ("anthropic",), 'agentx-dev[anthropic]'),
    ("otel", make_otel, ("opentelemetry",), 'agentx-dev[otel]'),
    ("mcp-stdio", make_mcp_stdio, ("mcp",), 'agentx-dev[mcp]'),
    ("mcp-http", make_mcp_http, ("mcp",), 'agentx-dev[mcp]'),
]


@pytest.mark.parametrize("label,build,names,extra", CASES, ids=[c[0] for c in CASES])
class TestEveryOptionalAdapter:
    def test_not_installed_points_at_the_extra(self, monkeypatch, label, build, names, extra):
        fail_imports(monkeypatch, names, missing(names[0]))
        with pytest.raises(ImportError) as info:
            build()
        assert extra in str(info.value)

    def test_installed_but_broken_shows_the_cause(self, monkeypatch, label, build, names, extra):
        fail_imports(monkeypatch, names, GOOGLE_SHADOW)
        with pytest.raises(ImportError) as info:
            build()
        text = str(info.value)
        assert "flask" in text and "pip install" not in text
        assert info.value.__cause__ is GOOGLE_SHADOW
