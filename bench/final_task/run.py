"""python bench/final_task/run.py [--max-runs N]   (needs `claude` logged in; counts against the 110-call / $35 cap)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ft.runner import main  # noqa: E402

raise SystemExit(main())
