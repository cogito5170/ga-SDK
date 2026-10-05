"""Hidden check for T2 (never shown to a model): slugify behaviour + the answer's own tests pass + a new test exists."""
import importlib.util
import inspect
import sys

CASES = [("Hello, World!", "hello-world"), ("  A  b__C ", "a-b-c"), ("", ""), ("---", ""), ("x", "x"),
         ("Already-slugged-2", "already-slugged-2"), ("a&b", "a-b"), ("  lead and trail  ", "lead-and-trail")]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def main(root):
    sys.path.insert(0, root)
    tu = load("textutil", f"{root}/textutil.py")
    for a, b in CASES:
        got = tu.slugify(a)
        assert got == b, (a, b, got)
    assert tu.word_count("a b") == 2 and tu.truncate("abcdefghij", 6) == "abc..."
    t = load("test_textutil", f"{root}/test_textutil.py")
    tests = [(n, f) for n, f in vars(t).items() if n.startswith("test_") and callable(f)]
    assert any("slugify" in inspect.getsource(f) for _, f in tests), "no test mentions slugify"
    for n, f in tests:
        f()
    print("HIDDEN_OK")


if __name__ == "__main__":
    main(sys.argv[1])
