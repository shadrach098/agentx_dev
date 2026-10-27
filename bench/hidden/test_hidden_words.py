from notes.text import word_count


def test_word_count():
    assert word_count("") == 0
    assert word_count("hello world") == 2
    assert word_count("  don't   stop ") == 2
    assert word_count("one, two; three! 4") == 4
    assert word_count("---") == 0
