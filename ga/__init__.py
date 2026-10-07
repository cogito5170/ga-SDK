"""ga-SDK: machinery for a hub session that drives worker sessions by directives and reports (METHOD.md)."""

__version__ = "0.18.1"

import os as _os
import sys as _sys

if _os.environ.get("GA_ACT_ISOLATE"):
    # ga act runs a worktree's commands with GA_ACT_ISOLATE set. An editable install of the deployed ga-sdk adds a
    # meta-path finder that resolves any ga.* module missing from the worktree to the deployed tree, so a test of a new
    # module could pass on deployed code. Drop those finders so ga.* comes only from this package's own directory.
    _sys.meta_path[:] = [f for f in _sys.meta_path
                         if not (getattr(f, "__module__", "") or "").startswith("__editable__")]
    _sys.path_hooks[:] = [h for h in _sys.path_hooks
                          if not (getattr(h, "__module__", "") or "").startswith("__editable__")]
    _sys.path_importer_cache.clear()
