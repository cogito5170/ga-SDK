from textutil import is_blank, title_case, truncate, word_count


def test_word_count():
    assert word_count("a b  c") == 3
    assert word_count("") == 0


def test_title_case():
    assert title_case("hELLO wORLD") == "Hello World"


def test_truncate():
    assert truncate("abcdefghij", 6) == "abc..."
    assert truncate("abc", 6) == "abc"


def test_is_blank():
    assert is_blank("  ") and not is_blank("x")
