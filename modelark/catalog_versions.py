"""Closed physical catalog compatibility; separate from private Slice formats.

Version 8 raises the reader floor for corrected identity evidence without changing
the version-7 table layout. Ordinary bootstrap/provenance migration still targets
7; only the explicit serial repair raises an existing catalog to 8 (DEC-133).
"""

CATALOG_LAYOUT_VERSION = 7
SUPPORTED_CATALOG_VERSIONS = frozenset({7, 8})
MAX_SUPPORTED_CATALOG_VERSION = max(SUPPORTED_CATALOG_VERSIONS)
SERIAL_REPAIR_CATALOG_VERSION = 8
