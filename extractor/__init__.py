"""Field extraction package (rules + parser)."""

from .rules import Rules, normalize  # noqa: F401
from .parser import (  # noqa: F401
    extract_fields,
    find_order_id,
    find_name,
    find_address,
    find_truncated_order_id,
    find_ellipsis_truncated,
    find_label_truncated,
)

__all__ = [
    "Rules",
    "normalize",
    "extract_fields",
    "find_order_id",
    "find_name",
    "find_address",
    "find_truncated_order_id",
    "find_ellipsis_truncated",
    "find_label_truncated",
]