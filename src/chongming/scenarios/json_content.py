"""One content identity rule for scenario JSON, independent of file formatting.

Re-exported from :mod:`chongming.contracts.json_codec`.
"""

from chongming.contracts.json_codec import (
    MAX_SAFE_INTEGER,
    JsonValue,
    canonical_json,
    content_sha256,
    parse_json,
)

__all__ = [
    "MAX_SAFE_INTEGER",
    "JsonValue",
    "canonical_json",
    "content_sha256",
    "parse_json",
]
