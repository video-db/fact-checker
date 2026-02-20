import re

MAX_NOTE_LENGTH = 280


def generate(raw_claims):
    """Format raw verifier output into community-notes style.

    Args:
        raw_claims: List of dicts from Verifier.verify().

    Returns:
        List of formatted note dicts with standardized fields.
    """
    notes = []
    for claim in raw_claims:
        note_text = _clean_note(claim.get("note", ""))
        if not note_text:
            continue

        notes.append({
            "claim": claim["claim"],
            "label": claim["label"].lower(),
            "confidence": claim.get("confidence", "low").lower(),
            "note": note_text,
            "sources": claim.get("sources", []),
        })
    return notes


def _clean_note(text):
    """Enforce length limits and strip opinionated language."""
    text = text.strip()
    if not text:
        return ""

    # Remove leading opinion markers
    text = re.sub(
        r"^(actually|obviously|clearly|in fact|of course)[,:]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Truncate to max length at a word boundary
    if len(text) > MAX_NOTE_LENGTH:
        truncated = text[:MAX_NOTE_LENGTH - 3]  # reserve space for "..."
        parts = truncated.rsplit(" ", 1)
        text = parts[0].rstrip(",.;:") + "..." if len(parts) > 1 else truncated + "..."

    return text
