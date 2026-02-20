import json
import logging

from google import genai

from config import GEMINI_MODEL

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a community-notes fact-checking system for live audio streams.\n"
    "Your job is to identify ONLY verifiable factual claims and assess their accuracy.\n"
    "\n"
    "You must:\n"
    "- Extract only concrete, verifiable factual statements (statistics, dates, "
    "named events, scientific claims, historical facts)\n"
    "- Ignore opinions, predictions, jokes, rhetorical questions, greetings, "
    "filler, and subjective statements\n"
    "- Handle incomplete or broken sentences gracefully — only check claims you "
    "can fully understand\n"
    "- Be conservative: if a claim is ambiguous or you're unsure, classify it as "
    '"needs_context" with low confidence rather than making a wrong call\n'
    "\n"
    "Labels:\n"
    '- "verified": The claim is factually accurate based on established knowledge\n'
    '- "misleading": The claim is factually incorrect or significantly distorts '
    "the truth\n"
    '- "needs_context": The claim is partially true but missing important context, '
    "or verification is difficult\n"
    "\n"
    "Confidence:\n"
    '- "high": You are very confident in your assessment (well-known facts, clear data)\n'
    '- "medium": Likely correct but some ambiguity exists\n'
    '- "low": Uncertain, limited information available'
)

EXTRACTION_PROMPT = """Analyze this live transcript excerpt. The "context" section is from earlier in the stream — use it for understanding but do NOT re-check claims from it.

[CONTEXT (do not re-check)]
{context}

[NEW TRANSCRIPT (check this)]
{transcript}

For each verifiable factual claim in the NEW TRANSCRIPT section:
1. State the claim concisely
2. Assign a label: "verified", "misleading", or "needs_context"
3. Rate your confidence: "high", "medium", or "low"
4. Write a brief, neutral note (1-2 sentences max, like a community note)
5. Suggest 1-2 authoritative sources as full URLs (e.g. "https://www.bbc.com/news/...", "https://reuters.com/..."). Use real, well-known URLs that are likely to cover the topic. If no specific URL is known, use the homepage of a relevant authoritative outlet (e.g. "https://www.nasa.gov", "https://www.who.int").

Respond ONLY with a JSON array. Each element:
{{
  "claim": "concise claim text",
  "label": "verified" | "misleading" | "needs_context",
  "confidence": "high" | "medium" | "low",
  "note": "brief neutral explanation",
  "sources": ["https://example.com/relevant-article"]
}}

If no verifiable factual claims exist, return: []"""


def _is_safe_url(url):
    """Reject non-HTTP URLs and suspiciously long URLs."""
    if not isinstance(url, str):
        return False
    if len(url) > 2048:
        return False
    return url.startswith("https://") or url.startswith("http://")


class Verifier:
    """Sends transcript text to Gemini for claim extraction, verification, and scoring."""

    def __init__(self, api_key):
        self.client = genai.Client(api_key=api_key)
        self.model_name = GEMINI_MODEL
        logger.info("Verifier initialized with model: %s", self.model_name)

    def verify(self, transcript, context=""):
        """Extract and verify factual claims from transcript text.

        Args:
            transcript: Cleaned transcript string to analyze.
            context: Previous context window (for understanding only).

        Returns:
            List of dicts with keys: claim, label, confidence, note, sources.
        """
        text = transcript.strip()
        if not text:
            return []

        prompt = EXTRACTION_PROMPT.replace(
            "{context}", context if context else "(none)"
        ).replace("{transcript}", text)

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config={
                    "system_instruction": SYSTEM_PROMPT,
                },
            )
            return self._parse_response(response.text)
        except Exception as e:
            logger.error("Gemini API error: %s", e)
            return []

    def _parse_response(self, raw_text):
        """Parse the JSON array from Gemini's response."""
        cleaned = raw_text.strip()

        # Strip markdown code fences if Gemini wraps the JSON
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)

        try:
            claims = json.loads(cleaned)
            if not isinstance(claims, list):
                logger.warning("Gemini returned non-list JSON: %s", type(claims))
                return []

            valid_labels = ("verified", "misleading", "needs_context")
            valid_confidence = ("high", "medium", "low")

            validated = []
            for item in claims:
                if not isinstance(item, dict):
                    continue
                if "claim" not in item or "label" not in item:
                    continue
                label = item["label"].lower()
                if label not in valid_labels:
                    continue
                confidence = item.get("confidence", "low").lower()
                if confidence not in valid_confidence:
                    confidence = "low"
                sources = item.get("sources", [])
                if not isinstance(sources, list):
                    sources = [str(sources)] if sources else []
                sources = [s for s in sources if _is_safe_url(s)]
                validated.append({
                    "claim": str(item["claim"]),
                    "label": label,
                    "confidence": confidence,
                    "note": str(item.get("note", "")),
                    "sources": sources,
                })
            return validated

        except json.JSONDecodeError:
            logger.warning("Failed to parse Gemini response as JSON")
            logger.debug("Raw response: %s", raw_text[:500])
            return []
