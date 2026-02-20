import time

from config import CONFIDENCE_THRESHOLD, ALERT_COOLDOWN_SECONDS

# Ranked confidence levels — higher index means higher confidence.
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _meets_confidence(note_confidence, threshold):
    """Return True if note_confidence meets or exceeds the threshold."""
    note_rank = _CONFIDENCE_RANK.get(note_confidence, -1)
    threshold_rank = _CONFIDENCE_RANK.get(threshold, 0)
    return note_rank >= threshold_rank


def _normalize(text):
    """Produce a simple fingerprint for deduplication."""
    return " ".join(text.lower().split())


class AlertManager:
    """Decide which notes reach the terminal vs logs-only.

    Applies confidence gating, deduplication, and throttling.
    """

    def __init__(self):
        self._seen = {}  # fingerprint -> timestamp
        self._last_alert_time = 0.0

    def filter(self, notes):
        """Split notes into alerts (terminal) and log-only.

        Args:
            notes: List of note dicts from note_generator.

        Returns:
            (alerts, log_only) — two lists of note dicts.
            Each dict gets an extra "alerted" boolean key.
        """
        alerts = []
        log_only = []
        now = time.time()

        # Expire stale entries to prevent unbounded memory growth
        expiry = 3 * ALERT_COOLDOWN_SECONDS
        stale = [fp for fp, ts in self._seen.items() if now - ts > expiry]
        for fp in stale:
            del self._seen[fp]

        for note in notes:
            note_copy = dict(note)
            if self._should_alert(note_copy, now):
                note_copy["alerted"] = True
                alerts.append(note_copy)
            else:
                note_copy["alerted"] = False
                log_only.append(note_copy)

        return alerts, log_only

    def reset(self):
        """Clear state at session end."""
        self._seen.clear()
        self._last_alert_time = 0.0

    def _should_alert(self, note, now):
        """Return True if this note should be surfaced as a terminal alert."""
        # Confidence gate — alert only if note confidence meets the threshold
        if not _meets_confidence(note.get("confidence"), CONFIDENCE_THRESHOLD):
            return False

        # Deduplication
        fp = _normalize(note.get("claim", ""))
        if not fp:
            return False
        if fp in self._seen:
            elapsed = now - self._seen[fp]
            if elapsed < ALERT_COOLDOWN_SECONDS:
                return False

        self._seen[fp] = now
        self._last_alert_time = now
        return True
