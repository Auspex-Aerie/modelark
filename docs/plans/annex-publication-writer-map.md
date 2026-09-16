# Stage 1 writer integration map

Implementation working notes, 2026-09-14. The approved contract remains
`annex-payload-migration.md` / DEC-157. This is not a completion claim or a new deployment decision.
No live catalog, archive, registration, service or Fill action is authorized by this document.

## Remaining entry points

| Entry point | Current boundary | Required integration before Stage 1 acceptance |
| --- | --- | --- |
| `fetch.run` / `fetch_model` | Existing enclosing dirty generation; per-file archived-only write; drive sync then named map sync | Enter one publication scope before physical locks, preserve session token/frozen config, prepare file intent before publication, verify bytes/path/tree, atomic pair CAS, batch map receipt, enclosing closure |
| `fetch.run_replica_tasks` | Source/target group fences, native key copy, whereis, archived-only mirror, named pair map sync | Same publisher; bind both selected participants, prove actual target object **and mapped path**, retain full source pair, no closure based on whereis or successful command |
| `fetch.run_replica` | Legacy whole-repo copy and unscoped map sync, then archive mirror | Route through the same guarded implementation or explicitly reject publication-enabled use; do not leave an alternate unguarded writer |
| `drive_lifecycle.register_new_identity` → `register.prepare_new_identity_archive` | v9 uses `registration_setup.hold`: map-only publication scope, durable `registration` operation, qualified map UUID/root, physical prep outside SQLite, catalog CAS then map receipt/closure. v7/v8 keeps controller-spanned preparation and graph CAS | Conversion and returning-clone remain disabled. Resume of a leftover PREPARED registration operation is later work |
| `register.register_drive` / `register.ensure_library` / `register.register_nas` | v9 catalog-facing map/catalog mutation enters the same setup adapter. ensure_library on v9 inspects the sealed map UUID and never annex-inits a second identity. v7/v8 bootstrap/controller path unchanged | Occupied invalid/incomplete namespaces still refuse. Device probes stay outside SQLite |
| Recovery/identity repair/clean publisher | Shared pending-publication guards now present | Finish route audit and explicit owner-bound continuation; a generic clean-anchor call cannot erase unfinished map work |

The six sync calls are the two in `fetch.run`, one in `run_replica_tasks`, one in
`run_replica`, and one in each registration function. They are not six independently
optional checks. Their containing tree/config writes belong inside the same boundary.

## Integration constraints found in the current code

- Do not wrap a new controller/map scope **inside** `drive_mutation` after it already
  acquired physical fences. That reverses the approved order and reacquires non-reentrant
  locks. The enclosing coordinator must select/own one complete scope.
- `RunCtx.write` includes ordinary event/progress updates which also bump planner revision.
  Use the new scope's existing-adapter write route so those own writes advance the operation
  marker exactly once. Unrelated writers cannot refresh or adopt its revision.
- Registration's physical callback was moved outside `graph_write`; preflight and final CAS
  both recheck exact catalog facts and idle status. A concurrent revision/collision refuses
  final insertion while preserving the physical preparation receipt. No filesystem cleanup
  or success receipt is inferred from the failed catalog cutover.
- Registration map creation now serializes all `ensure_library` callers on a canonical-path
  bootstrap lock while no UUID exists, then overlaps it with the real map-UUID lock through
  initial setup. Existing occupied paths require ordinary repository directories, a valid local
  annex UUID, an initialized annex-format value and committed HEAD;
  incomplete initialization remains an explicit refusal, not permission to run `annex init`.
  This preserves explicit legacy map creation; new-identity registration still never creates
  a missing map implicitly. Version-nine setup/publication activation remains disabled.
- Hold the same map-UUID exclusion for every participating writer, including compatible
  old-catalog paths sharing that physical map. A v9-only lock cannot exclude an older path
  which ignores it. Reader admission/version activation waits for this audit.
- The pinned migration profile is SHA256 / annex 8.20210223 / repository format 8. Ordinary
  archives also contain SHA256E keys. Do not silently narrow existing reader support or run
  an auto-upgrading annex client; qualify the appropriate ordinary-ingestion profile before
  routing those keys into the new strict representation verifier.
- The new descriptor verifier proves stored bytes, not decompressed/original bytes. Keep
  existing codec resource and round-trip evidence separate and unchanged.
- Source fences stay read-only: no reverse map/controller lock acquisition and no implicit
  `annex get` in publication-enabled restore. Other proven available copies remain eligible.

## Not yet implemented

Public ArchivePublisher orchestration; independently qualified command/profile capability;
committed-tree and staged native map proof factories; mapped replica-path publication;
journalled profile setup; terminal-owned maintenance adapter; complete returning-clone flow;
global floor-9 admission and explicit clone-migration orchestration. New store/lock/CAS/payload
primitives and synthetic receipts must not be mistaken for these remaining gates.
