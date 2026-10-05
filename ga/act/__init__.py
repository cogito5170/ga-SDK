"""``ga act`` (CMD-GA38): one work item executed in its worktree with any backend, token-optimized.

The model answers only in the fixed action format (fmt.SPEC: EDIT search/replace, NEW, RUN <name>, NEED, DONE,
BLOCKED); code applies edits inside the item's files (apply), runs named commands (commands), serves NEED (retrieve)
and rebuilds a fixed-size state card every turn instead of carrying chat history (card), until done_when passes, a cap
is hit or progress stops (loop). No tool schemas in the prompt, no history: the card's stable prefix is the same bytes
every turn so a provider's prompt cache can hit it.
"""
from .loop import Act, Item, Result, make_item, run_item  # noqa: F401
