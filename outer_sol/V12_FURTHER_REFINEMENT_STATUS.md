# V12 further refinement status

Status: **STARTED / GAP AUDIT COMPLETE / IMPLEMENTATION QUEUED AFTER ICP MILESTONE**
Date: 2026-08-22

The current authority is:

- `docs/V12_MODEL_FIRST_ARCHITECTURE_DECISION.md` for the frozen `TurnPlan v1`
  and attested file/archive canary boundary;
- Proposal 86 in `sol/PROPOSALS.md` for the continuous retrieval roadmap;
- `outer_sol/INTERACTION_CONTROL_PLANE_AND_OPERATIONAL_MEMORY.md` for the
  required ordering: typed outcomes before Work Items and WorkGraphs.

The historical `docs/NEW_ROUTER_MODEL_MIGRATION_PLAN.md` and the stale
`v12-model-first` branch are not implementation sources.

First bounded implementation slice after the current ICP milestone:

1. Add a closed immutable `CapabilityOutcome v1`.
2. Add a deterministic pre-publication completion gate for the existing
   `FILE_READ` and `ARCHIVE_READ` routes only.
3. Revalidate route, evidence, citations, authority and the typed outcome before
   one atomic publication.
4. Prove complete/partial/empty/unavailable/denied and retryability boundaries
   with handler and real-router regressions.

This slice adds no route, schema, storage family, model profile, web access,
effect capability or companion-plugin dependency. `TurnPlan v1` remains frozen.

Next after that slice: implement the missing read-only `search_explain` path and
its preregistered difficult-query gold set before widening V12 to any new route.
