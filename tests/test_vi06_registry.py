"""VI-06 (baseline acceptance test, DEV-FORMATS, ga-sdk side): one registry file is the source of ga.forms.

ga/forms/registry.json = {"schema": "forms-registry/1",
                          "enums": {NAME: [values...]},                        # the value sets ga.forms checks
                          "forms": {"<form>/<n>": {"fields": {name: {"required": bool}}}}}
ga.forms.kinds reads its enums from the registry (no second hand-written copy), every form in SCHEMAS has its fields
in the registry, and docs/FORMS.md is generated from the registry by ga.forms.registry.render_doc() (a test fails when
the committed doc drifts). The baseline flow.py side (vendored copy + sha256 test) is a separate item.
"""
import json
import unittest
from pathlib import Path

from ga.forms import hard
from ga.forms import kinds as K
from ga.forms import registry as REG

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "ga" / "forms" / "registry.json"
ENUMS = ("NOTIFY_KINDS", "HANDLED_STATUS", "BLOCKER_KINDS", "CHANGE_SIZES", "NEEDS", "VERDICT_CLASSES", "CAUSES",
         "NEXT_CHOICES")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.reg = json.loads(FILE.read_text(encoding="utf-8"))

    def test_shape(self):
        self.assertEqual(self.reg["schema"], "forms-registry/1")
        self.assertEqual(set(self.reg), {"schema", "enums", "forms"})
        self.assertEqual(REG.load(), self.reg)  # the module reads the same file

    def test_enums_come_from_the_registry(self):
        for name in ENUMS:
            self.assertEqual(tuple(getattr(K, name)), tuple(self.reg["enums"][name]), name)
        self.assertIn("alert", self.reg["enums"]["NOTIFY_KINDS"])  # VI-06a kept

    def test_the_enums_are_read_not_copied(self):
        src = (ROOT / "ga" / "forms" / "kinds.py").read_text(encoding="utf-8")
        for name in ENUMS:
            self.assertNotRegex(src, rf"(?m)^{name}\s*=\s*\(\s*\"", f"{name} is still a hand-written tuple in kinds.py")

    def test_every_form_has_its_fields_in_the_registry(self):
        for form, (fields, _cross) in K.SCHEMAS.items():
            self.assertIn(form, self.reg["forms"], form)
            want = {f.name: {"required": f.required} for f in fields}
            self.assertEqual(self.reg["forms"][form]["fields"], want, form)
        self.assertEqual(set(self.reg["forms"]), set(K.SCHEMAS))

    def test_an_unknown_notify_kind_is_rejected(self):
        head = {"schema": "notify/1", "to": "baseline", "kind": "gossip", "ref": "https://example.org/x"}
        self.assertTrue(hard(K.validate(head)))

    def test_doc_is_generated_from_the_registry(self):
        doc = (ROOT / "docs" / "FORMS.md").read_text(encoding="utf-8")
        self.assertEqual(doc, REG.render_doc(self.reg))
        for form in self.reg["forms"]:
            self.assertIn(form, doc)
        self.assertIn("alert", doc)


if __name__ == "__main__":
    unittest.main()
