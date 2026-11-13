"""The single definition of how an email address is normalized (docs/data-model.md, #269)."""


def normalize_email(value: str) -> str:
    """Strip surrounding whitespace and lowercase. No validation; callers validate when needed."""
    return value.strip().lower()
