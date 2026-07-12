"""Reset the PHYSICAL archive records for a clean startover (PIV-001 / handoff §7d).

Truncates what describes bytes-on-drives — `archived`, `replicas`, `drives`, `plan_drives`,
`fetch_events` — and KEEPS the catalog knowledge (`models`, `files`, `selection`, `verifications`)
plus the `plans` rows (the `ark` plan survives; its drive set is repopulated as drives re-register,
#34/DEC-030). Does NOT touch git-annex or the iSCSI LUN — that is the operator's DSM + host step
(§7a–c). The models are re-fetchable and NOTHING is deleted from any drive; this only clears the DB's
record of where bytes were, for a fresh fill. Dry-run by default; pass --yes to truncate.

Run with the portal STOPPED (it opens the catalog read-write).
"""
from __future__ import annotations

import argparse

from modelark.core import db

_WIPE = ["archived", "replicas", "drives", "plan_drives", "fetch_events"]
_KEEP = ["models", "files", "selection", "verifications", "plans"]


def _counts(con, tables):
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def main(argv=None):
    p = argparse.ArgumentParser(description="Reset physical archive records for a clean startover (§7d).")
    p.add_argument("--yes", action="store_true", help="actually truncate the WIPE tables (else dry-run)")
    p.add_argument("--last-fill", action="store_true", help="also clear the persisted last-fill oopsie")
    args = p.parse_args(argv)

    con = db.connect()
    try:
        print("BEFORE:")
        for t in _WIPE + _KEEP:
            n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            print(f"  {t:14} {n:>8}  {'(WIPE)' if t in _WIPE else '(keep)'}")
        if not args.yes:
            print("\nDRY RUN — pass --yes to truncate the (WIPE) tables. "
                  "models/files/selection/verifications/plans are kept.")
            return
        con.execute("BEGIN")
        for t in _WIPE:
            con.execute(f"DELETE FROM {t}")
        con.execute("COMMIT")
        print("\nAFTER:")
        for t, n in _counts(con, _WIPE).items():
            print(f"  {t:14} {n:>8}")
        print("\n✅ physical records reset. The `ark` plan survives (its drive set is empty until you "
              "re-register). Next: recreate the LUN with headroom (§7a), re-register drives (§7b → "
              "repopulates drives + plan_drives), fresh annex (§7c), then a fresh fill.")
    finally:
        con.close()

    if args.last_fill:
        from modelark.web import fill_api
        fill_api.ack_terminal()
        print("cleared catalog/last_fill.json")


if __name__ == "__main__":
    main()
