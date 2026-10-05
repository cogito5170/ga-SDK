"""CMD-GA36 tests: a tool that is NOT in ga's table (it writes a file). A project may declare it in its own
ga-supervise.json; ga ask --solve must never hand it to the loop."""
from pathlib import Path


def write_file(path: str, text: str) -> str:
    Path(path).write_text(text)
    return "written"
