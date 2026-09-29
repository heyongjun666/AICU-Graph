"""Session-level scheduler concurrency limits (not agents per node)."""

DEFAULT_MAX_CONCURRENCY = 4
MIN_MAX_CONCURRENCY = 1
MAX_MAX_CONCURRENCY = 16


def validate_max_concurrency(value: object) -> int:
    """Reject coercions so invalid settings cannot silently change scheduling."""
    if type(value) is not int or not MIN_MAX_CONCURRENCY <= value <= MAX_MAX_CONCURRENCY:
        raise ValueError(
            f"max_concurrency must be an integer between "
            f"{MIN_MAX_CONCURRENCY} and {MAX_MAX_CONCURRENCY}"
        )
    return value
