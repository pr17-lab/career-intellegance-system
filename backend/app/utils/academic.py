"""
Shared academic calculation utilities.
Used by the auth registration flow and any future grading logic.
"""


def score_to_level(score: float) -> str:
    """Map a 0–100 confidence score to a skill level label."""
    if score >= 80:
        return "strong"
    if score >= 50:
        return "moderate"
    return "weak"
