from pipeline.claim_detector import preprocess, get_context_prefix
from pipeline.verifier import Verifier
from pipeline.note_generator import generate
from pipeline.alert_manager import AlertManager


def run_pipeline(transcript_chunk, context, verifier, alert_manager):
    """Orchestrate the full fact-checking pipeline.

    Args:
        transcript_chunk: Raw transcript text from the current cycle.
        context: Previous context window string.
        verifier: Verifier instance.
        alert_manager: AlertManager instance.

    Returns:
        (alerts, all_notes, new_context)
    """
    # 1. Preprocess: clean transcript, build sliding context
    cleaned, new_context = preprocess(transcript_chunk, context)
    if not cleaned:
        return [], [], new_context

    # 2. Verify: single Gemini call for extraction + verification + scoring
    context_prefix = get_context_prefix(context)
    raw_claims = verifier.verify(cleaned, context=context_prefix)

    # 3. Generate: format as community notes
    notes = generate(raw_claims)

    # 4. Filter: confidence gate + dedup + throttle
    alerts, log_only = alert_manager.filter(notes)

    all_notes = alerts + log_only
    return alerts, all_notes, new_context
