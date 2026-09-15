"""Closed physical catalog compatibility; separate from private Slice formats.

Version 8 raises the reader floor for corrected identity evidence without changing
the version-7 table layout. Ordinary bootstrap/provenance migration still targets
7; only the explicit serial repair raises an existing catalog to 8 (DEC-133).
Version 9 adds the publication contract. Supporting its reader floor does not
install that contract or enable attended representation conversion.
"""

CATALOG_LAYOUT_VERSION = 7
SUPPORTED_CATALOG_VERSIONS = frozenset({7, 8, 9})
MAX_SUPPORTED_CATALOG_VERSION = max(SUPPORTED_CATALOG_VERSIONS)
SERIAL_REPAIR_CATALOG_VERSION = 8


def validate_publication_schema(con):
    """Read-only contract validation, never admission or implicit migration.

    Validate older catalogs too: publication tables under an older floor are
    corruption, not permission to bypass obligations. Pending operations remain
    readable; source/write admission uses the separate shared obligation guard.
    """
    from modelark.publication_store import library
    return library(con)
