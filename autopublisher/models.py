"""YouTube metadata rules shared by review and publishing. Pure Python."""

from __future__ import annotations

import re

# YouTube hard limits
MAX_TITLE_LEN = 100
MAX_DESCRIPTION_LEN = 5000
MAX_TAGS_TOTAL_LEN = 470  # actual limit is ~500, keep a margin
MAX_SHORT_SECONDS = 179  # Shorts must stay under 3 minutes

def sanitize_title(title: str) -> str:
    """YouTube titles: max 100 chars, no angle brackets."""
    clean = re.sub(r"[<>]", "", title).strip()
    return clean[:MAX_TITLE_LEN].strip()


def sanitize_description(description: str) -> str:
    clean = re.sub(r"[<>]", "", description).strip()
    return clean[:MAX_DESCRIPTION_LEN]


def sanitize_tags(tags: list[str]) -> list[str]:
    """Deduplicate and trim so the total stays within YouTube's tag budget."""
    result: list[str] = []
    total = 0
    seen = set()
    for tag in tags:
        clean = re.sub(r"[<>]", "", tag).strip()
        if not clean or clean.lower() in seen:
            continue
        # tags containing spaces count with surrounding quotes
        cost = len(clean) + (2 if " " in clean else 0) + 1
        if total + cost > MAX_TAGS_TOTAL_LEN:
            break
        seen.add(clean.lower())
        result.append(clean)
        total += cost
    return result
