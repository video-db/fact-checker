import re

from config import CONTEXT_WINDOW_WORDS


def preprocess(transcript_chunk, previous_context=""):
    """Clean transcript and merge with sliding context window.

    Args:
        transcript_chunk: Raw transcript text from the current cycle.
        previous_context: Trailing words from the previous cycle.

    Returns:
        (full_text, new_context) where full_text is the combined text for
        the verifier (context marked separately) and new_context is the
        tail to carry forward.
    """
    cleaned = _clean(transcript_chunk)
    if not cleaned:
        return "", previous_context

    context = previous_context.strip()
    new_context = _tail_words(cleaned, CONTEXT_WINDOW_WORDS)

    return cleaned, new_context


def get_context_prefix(previous_context):
    """Return the context string to prepend, or empty string."""
    return previous_context.strip()


def _clean(text):
    """Collapse whitespace and strip common transcription artifacts."""
    text = text.strip()
    # Collapse multiple spaces/newlines into single space
    text = re.sub(r"\s+", " ", text)
    # Remove common filler artifacts (standalone only, surrounded by spaces or at edges)
    text = re.sub(r"(?<!\w)(um|uh|ah|er|hmm)(?!\w)", "", text, flags=re.IGNORECASE)
    # Clean up any double spaces left after removal
    text = re.sub(r"  +", " ", text).strip()
    return text


def _tail_words(text, n):
    """Return the last *n* words of *text*."""
    words = text.split()
    return " ".join(words[-n:]) if len(words) > n else text
