"""Small text helpers used by the report tools."""
import re


def word_count(text):
    """Number of whitespace-separated words."""
    return len(text.split())


def title_case(text):
    """Capitalise the first letter of every word, lower-case the rest."""
    return " ".join(w[:1].upper() + w[1:].lower() for w in text.split())


def truncate(text, limit):
    """Cut text to at most ``limit`` characters, ending in '...' when it was cut."""
    if limit < 3:
        raise ValueError("limit must be at least 3")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def is_blank(text):
    return not text or not text.strip()
