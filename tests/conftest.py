"""R1: every model turn goes through ga.llm, which refuses on a missing policy. The suite runs with a permissive policy
and a throwaway home, set before ga is imported; the R1 tests build their own gateways with their own policies."""
import json
import os
import tempfile

_d = tempfile.mkdtemp(prefix="ga-llm-test-")
_p = os.path.join(_d, "policy.json")
with open(_p, "w") as _f:
    json.dump({"vm_budget": {"caps": {"vm_coordination_dev_plus_ops_usd_per_h": 1e9, "vm_baselines_usd_per_h": 1e9,
                                      "vm_total_usd_per_h": 1e9, "vm_total_usd_per_day": 1e9}}}, _f)
os.environ["GA_LLM_POLICY"] = _p
os.environ["GA_LLM_HOME"] = os.path.join(_d, "home")
