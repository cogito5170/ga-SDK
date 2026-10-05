"""GA Planner, in shadow (CMD-GA40): ``ga plan "<request>"`` -> a directive/2 draft (+ an item draft for code work)
from one model turn on a fixed card, checked by code against the planning lessons (ga.plan.lessons). Drafts are only
written under ``<ga home>/plan/drafts/``, never sent; ``ga plan compare`` measures how far the final moved.

    ga plan "가격 페이지에 연간 토글" --repo ../app --to GA      cost first, y/N, then one turn
    ga plan compare <draft.md> <final.md>                       field-level changes and a score
"""
from .card import CARD_MAX, Card, build as build_card
from .compare import compare as compare_heads, compare_files
from .draft import Draft, PlanFailed, plan, render, write
from .lessons import CHECKLIST, LESSONS, check as check_lessons

__all__ = ["CARD_MAX", "CHECKLIST", "LESSONS", "Card", "Draft", "PlanFailed", "build_card", "check_lessons", "compare_heads",
           "compare_files", "plan", "render", "write"]
