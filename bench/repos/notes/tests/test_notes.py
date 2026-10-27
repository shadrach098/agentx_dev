from notes.export import index
from notes.store import NoteStore
from notes.text import first_line, shorten, slugify


def test_slugify():
    assert slugify("Shopping list: Monday!") == "shopping-list-monday"
    assert slugify("???") == "untitled"


def test_first_line_skips_blank_lines():
    assert first_line("\n\n  Hello  \nworld") == "Hello"


def test_shorten():
    assert shorten("a  b   c") == "a b c"
    assert shorten("x" * 50, limit=10) == "x" * 9 + "…"


def test_ids_are_unique():
    store = NoteStore()
    assert store.add("Plan\nstuff") == "plan"
    assert store.add("Plan\nmore") == "plan-2"


def test_index():
    store = NoteStore()
    store.add("Groceries\nmilk")
    assert index(store) == "# Notes\n\n- [Groceries](groceries.md)\n"
