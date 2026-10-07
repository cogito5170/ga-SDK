"""Post-deploy check of vm/G2-INT (baseline, read-only): VI-07 on the VM's own policy and ledger.

`ga llm report --status` with the VM defaults prints one status/1 object: id LLM-<YYYYMMDDHH>, one item per cap with a
numeric limit, states in ok|near|over, and no blocker unless a cap is over or the policy is not ok.
"""
import json
import re
import subprocess
import sys
import unittest


class LiveStatusTest(unittest.TestCase):
    def test_ga_llm_report_status_on_the_vm(self):
        out = subprocess.run([sys.executable, "-m", "ga", "llm", "report", "--status"],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        st = json.loads(out.stdout)
        print(json.dumps(st, ensure_ascii=False))
        self.assertEqual(st["schema"], "status/1")
        self.assertRegex(st["id"], re.compile(r"^LLM-\d{10}$"))
        self.assertTrue(st["items"], "no cap with a numeric limit: the VM policy was not read")
        for it in st["items"]:
            self.assertIn(it["state"], ("ok", "near", "over"))
        self.assertNotIn("policy_ok is false", [b["what"] for b in st["blockers"]])
        over = [it["id"] for it in st["items"] if it["state"] == "over"]
        self.assertEqual(len(st["blockers"]), len(over))

if __name__ == "__main__":
    unittest.main()
