"""Core orchestration package (batch processing loop)."""

from .orchestrator import (  # noqa: F401
    Orchestrator,
    ProcessResult,
    RunSummary,
    RunControl,
    STATUS_OK,
    STATUS_TRUNC,
    STATUS_FALLBACK_NAME,
    STATUS_FALLBACK_ADDRESS,
    STATUS_FAILED,
    STATUS_MISSING,
    STATUS_NO_IMAGE,
    STATUS_SKIPPED,
    STATUS_REVIEW,
)

__all__ = [
    "Orchestrator",
    "ProcessResult",
    "RunSummary",
    "RunControl",
    "STATUS_OK",
    "STATUS_TRUNC",
    "STATUS_FALLBACK_NAME",
    "STATUS_FALLBACK_ADDRESS",
    "STATUS_FAILED",
    "STATUS_MISSING",
    "STATUS_NO_IMAGE",
    "STATUS_SKIPPED",
    "STATUS_REVIEW",
]
