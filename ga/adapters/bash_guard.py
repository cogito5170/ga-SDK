"""PreToolUse guard for headless session turns (METHOD R3 · R6, CMD-GA4).

Claude Code runs this before every Bash / file-writing tool call of a turn (installed by HeadlessRunner
through ``--settings``). It closes the ways around the git hooks and around the push identity:

- ``--no-verify`` (skips pre-push),
- ``--receive-pack`` / ``--exec`` / ``--upload-pack`` and anything naming ``receivepack`` (would forge GA_SESSION
  or run an arbitrary command on the receiving side),
- ``git -c`` / ``--config-env`` / ``git config`` / ``core.hooksPath`` (rewires hooks or remotes),
- ``GA_SESSION`` and ``GIT_*=`` assignments, ``env``, ``--git-dir`` / ``--work-tree`` (other identity or repository),
- file tools writing into ``.git`` or a ``hooks`` directory.

Protocol (Claude Code command hook): one JSON on stdin, one JSON on stdout, exit 0; a block is
``{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", ...}}``.
Anything it cannot read is denied (closing side). The optional log gets one line per decision with the
rule name only, never the command text.

Standalone on purpose (standard library only, no ga imports): it runs in the session's clean environment.
"""
from __future__ import annotations

import json
import re
import sys
import time

GIT = r"\bgit\b(?:\s+-C\s+(?:\"[^\"]*\"|'[^']*'|\S+))*"

BASH_RULES = [
    # git accepts any unambiguous prefix of a long option (--no-verif, --receive-pac=): match the prefixes
    ("no_verify", re.compile(r"--no-veri?f?y?\b|--no-ve\b")),
    ("receive_pack", re.compile(r"--rec(?:e(?:i(?:v(?:e(?:-(?:p(?:a(?:ck?)?)?)?)?)?)?)?)?(?:\b|=)|--exec\b|--upload-p\w*|receivepack|uploadpack", re.IGNORECASE)),
    ("git_c", re.compile(GIT + r"\s+-c\b|--config-env\b")),
    ("git_config", re.compile(GIT + r"\s+config\b")),
    ("hooks_path", re.compile(r"hookspath|core\.hooks", re.IGNORECASE)),
    ("identity", re.compile(r"\bGA_SESSION\b")),
    # in command position only (start, or after ; & | ( ` $( ), so a commit message that says "env" is fine
    ("git_env", re.compile(r"(?:^|[;&|(`]\s*|\$\(\s*)(?:export\s+)?(?:\w+=\S*\s+)*(?:GIT_[A-Z0-9_]+=|env\b)")),
    ("other_repo", re.compile(r"--git-dir\b|--work-tree\b")),
    ("hook_files", re.compile(r"\.git/(?:hooks|config|worktrees)|/hooks/")),
    # string rules cannot see through expansion or indirection, so a turn does not get them at all (closing side)
    ("script_exec", re.compile(r"(?:^|[;&|(]\s*)(?:(?:ba|z|da|k)?sh|python3?|perl|ruby|node)\s+[^-\s]|(?:^|[;&|(]\s*)\.{1,2}/[\w./-]+"
                                r"|\bmake\b|\bnpm\s+run\b|\bgit\s+-C\s+\S+\s+-c\b")),
    ("shell_indirection", re.compile(r"\$\(|`|\$\{|\$[A-Za-z_]|\beval\b|\bbase64\b|\bxxd\b|\bxargs\b|\b(?:ba|z|da)?sh\s+-c\b"
                                     r"|\bsource\b|(?:^|[;&|]\s*)\.\s|\bpython3?\s+-c\b|\bperl\b|\bruby\b|\bnode\s+-e\b")),
]
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
FILE_RULE = re.compile(r"(?:^|/)\.git(?:/|$)|(?:^|/)hooks/|config\.worktree$")


def decide(event: dict) -> tuple[str, str]:
    """('allow' | 'deny', rule name)."""
    tool = event.get("tool_name")
    inp = event.get("tool_input") or {}
    if tool == "Bash":
        cmd = inp.get("command")
        if not isinstance(cmd, str):
            return "deny", "unreadable"
        # quotes and backslashes split words for the shell but not for us: look at the command without them
        flat = re.sub(r"[\"'\\]", "", cmd)
        for name, rx in BASH_RULES:
            if rx.search(cmd) or rx.search(flat):
                return "deny", name
        return "allow", ""
    if tool in FILE_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path")
        if not isinstance(path, str):
            return "deny", "unreadable"
        if FILE_RULE.search(path.replace("\\", "/")):
            return "deny", "git_internals"
        return "allow", ""
    return "allow", ""


def main(argv: list[str]) -> int:
    log = argv[argv.index("--log") + 1] if "--log" in argv else None
    try:
        event = json.loads(sys.stdin.read())
        if not isinstance(event, dict):
            raise ValueError("not an object")
        decision, rule = decide(event)
        tool = event.get("tool_name")
    except Exception:  # closing side: what cannot be read is denied
        decision, rule, tool = "deny", "unreadable", None
    if log:
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": tool,
                                    "decision": decision, "rule": rule}) + "\n")
        except OSError:
            pass
    if decision == "deny":
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": f"ga guard ({rule}): this command could bypass the git hooks "
                                                                  "or forge the push identity (METHOD R3). Do the work without it."}}
    else:
        out = {}
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
