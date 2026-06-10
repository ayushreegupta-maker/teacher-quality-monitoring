import logging

from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


def retry_external(max_attempts: int = 3, min_wait: float = 2.0, max_wait: float = 30.0):
    """Decorator for external-service calls. Exponential backoff, reraise on final failure."""
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=min_wait, min=min_wait, max=max_wait),
        before_sleep=lambda rs: log.warning(
            f"retry {rs.attempt_number}/{max_attempts} after {rs.outcome.exception()!r}"
        ),
        reraise=True,
    )
