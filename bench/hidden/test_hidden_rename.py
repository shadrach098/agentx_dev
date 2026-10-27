from notes.export import index
from notes.store import NoteStore
from notes.text import make_slug


def test_make_slug():
    assert make_slug("Shopping list: Monday!") == "shopping-list-monday"


def test_the_store_and_the_index_still_work():
    store = NoteStore()
    assert store.add("Plan\nstuff") == "plan"
    assert index(store) == "# Notes\n\n- [Plan](plan.md)\n"
