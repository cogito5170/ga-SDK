"""GA Actions (CMD-GA42 S2, S3): a model proposes a named command, code checks and trial-runs it, only a human (or a
human-written pre-approval pattern) approves it; then ``ga act`` and ``ga supervise`` see it by name only."""
from .registry import ActionError, about, approve, from_text, home, propose, revoke, run, verified

__all__ = ["ActionError", "about", "approve", "from_text", "home", "propose", "revoke", "run", "verified"]
