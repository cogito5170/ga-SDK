"""G1: each form has valid and invalid examples; invalid heads are rejected, empty bodies are not."""
import unittest

from ga.forms import FormError, dump_text, hard, load, parse_post, parse_text, soft, validate

from examples import VALID, valid


def mutate(kind, path, value, delete=False):
    doc = valid(kind)
    cur = doc
    for key in path[:-1]:
        cur = cur[key]
    if delete:
        del cur[path[-1]]
    else:
        cur[path[-1]] = value
    return doc


DEL = object()

# (kind, path, value) — every one must be rejected (hard)
INVALID = [
    ("directive/1", ("id",), "GA1"),
    ("directive/1", ("id",), "CMD-ga1"),
    ("directive/1", ("rev",), 0),
    ("directive/1", ("rev",), "2"),
    ("directive/1", ("rev",), True),
    ("directive/1", ("to",), DEL),
    ("directive/1", ("goal",), "   "),
    ("directive/1", ("why",), DEL),
    ("directive/1", ("scope",), DEL),
    ("directive/1", ("done_when",), 3),
    ("directive/1", ("supersedes",), {"id": "CMD-GA1", "rev": 2}),
    ("directive/1", ("supersedes",), {"id": "CMD-GA1"}),
    ("directive/1", ("after",), ["BD-1"]),
    ("directive/1", ("budget",), {"llm_runs": -1}),
    ("directive/1", ("budget",), {}),
    ("directive/1", ("method",), "use X"),
    ("report/1", ("from",), DEL),
    ("report/1", ("handled",), DEL),
    ("report/1", ("handled", 0, "rev_seen"), [2, 1]),
    ("report/1", ("handled", 0, "rev_seen"), 0),
    ("report/1", ("handled", 0, "rev_seen"), [1, 2, 3]),
    ("report/1", ("handled", 0, "status"), "finished"),
    ("report/1", ("handled", 0, "id"), "GA1"),
    ("report/1", ("commits", 0, "sha"), "HEAD"),
    ("report/1", ("commits", 0, "repo"), DEL),
    ("report/1", ("tests",), {"passed": 1}),
    ("report/1", ("tests",), {"passed": 1, "failed": 0, "skipped": -1}),
    ("report/1", ("change_size",), "huge"),
    ("report/1", ("needs",), ["coffee"]),
    ("verdict/1", ("class",), "ok"),
    ("verdict/1", ("cause",), DEL),
    ("verdict/1", ("cause",), "bad luck"),
    ("verdict/1", ("evidence",), DEL),
    ("verdict/1", ("evidence", "heads"), DEL),
    ("verdict/1", ("claims_vs_evidence",), DEL),
    ("verdict/1", ("next",), {"choice": "sleep", "reason": "x"}),
    ("verdict/1", ("next",), {"choice": "wait"}),
    ("round/1", ("n",), 0),
    ("round/1", ("verdict",), "fine"),
    ("round/1", ("next",), DEL),
    ("round/1", ("repos", 0, "sha"), "xyz"),
    ("round/1", ("summary",), ""),
    ("decision/1", ("id",), "BD-x"),
    ("decision/1", ("by",), "llm"),
    ("decision/1", ("basis",), DEL),
    ("decision/1", ("supersedes",), ["CMD-A1"]),
    ("stage/1", ("repos",), []),
    ("stage/1", ("repos", 0, "sha"), "d313414"),
    ("stage/1", ("repos", 0, "tests"), DEL),
    ("stage/1", ("established",), DEL),
    ("exchange/1", ("why",), DEL),
    ("exchange/1", ("got",), ""),
    ("exchange/1", ("to",), "Sensor"),
    ("exchange/1", ("action",), "did it"),
    ("question/1", ("gate",), 8),
    ("question/1", ("gate",), 0),
    ("question/1", ("options",), [{"label": "only one", "effect": "x"}]),
    ("question/1", ("recommendation",), DEL),
    ("question/1", ("C",), DEL),
]


class FormsTest(unittest.TestCase):
    def test_valid_examples_have_no_hard_problems(self):
        for kind in VALID:
            with self.subTest(kind=kind):
                self.assertEqual(hard(validate(valid(kind))), [])
                self.assertIs(load(valid(kind), kind)["schema"], kind)

    def test_every_invalid_example_is_rejected(self):
        for kind, path, value in INVALID:
            with self.subTest(kind=kind, path=path, value=value):
                doc = mutate(kind, path, None, delete=True) if value is DEL else mutate(kind, path, value)
                self.assertTrue(hard(validate(doc)), f"accepted: {doc}")
                with self.assertRaises(FormError):
                    load(doc)

    def test_every_kind_has_invalid_examples(self):
        self.assertEqual({k for k, _, _ in INVALID}, set(VALID))

    def test_unknown_or_wrong_schema(self):
        self.assertTrue(hard(validate({"schema": "directive/2"})))
        self.assertTrue(hard(validate({"no": "schema"})))
        self.assertTrue(hard(validate([1, 2])))
        self.assertTrue(hard(validate(valid("report/1"), expect="directive/1")))

    def test_directive_without_done_when_is_soft_r7(self):
        doc = mutate("directive/1", ("done_when",), None, delete=True)
        probs = validate(doc)
        self.assertEqual(hard(probs), [])
        self.assertEqual([p.rule for p in soft(probs)], ["R7"])

    def test_verdict_success_needs_no_cause(self):
        doc = mutate("verdict/1", ("cause",), None, delete=True)
        doc["class"] = "success"
        self.assertEqual(hard(validate(doc)), [])

    def test_report_minimal_head_and_empty_body_accepted(self):
        text = dump_text({"schema": "report/1", "from": "GA", "handled": []})
        head, body, notes = parse_post(text, "report/1")
        self.assertEqual(head["from"], "GA")
        self.assertEqual(body, "")
        self.assertEqual(notes, [])

    def test_report_body_notes_are_soft_only(self):
        body = "## Result\nX 를 하겠다\n\n## Evidence\n시험 10 통과\n\n## Proposal\nY 를 고친다\n"
        head, _, notes = parse_post(dump_text(valid("report/1"), body), "report/1")
        self.assertEqual(len(notes), 3)
        self.assertTrue(all(n.strength == "soft" for n in notes))

    def test_round_trip(self):
        for kind in VALID:
            with self.subTest(kind=kind):
                head, body = parse_text(dump_text(valid(kind), "## Result\n본문\n"))
                self.assertEqual(head, valid(kind))
                self.assertEqual(body, "## Result\n본문\n")

    def test_bad_wire_heads_rejected(self):
        for text in ("no fence at all", "```ga\nnot json\n```\n", "```ga\n[1]\n```\n", "text first\n```ga\n{}\n```\n"):
            with self.subTest(text=text):
                with self.assertRaises(FormError):
                    parse_text(text)
        with self.assertRaises(FormError):
            parse_post(dump_text({"schema": "report/1", "from": "GA"}), "report/1")

    def test_directive_id_prefixes_are_distinct(self):
        for ok in ("CMD-G1", "CMD-GA1", "CMD-K8", "CMD-GA12"):
            doc = valid("directive/1")
            doc["id"] = ok
            doc.pop("supersedes")
            self.assertEqual(hard(validate(doc)), [], ok)


if __name__ == "__main__":
    unittest.main()
