"""Original-pool parent-custody quartet and ALL20+ workload costs.

Legacy six-pair functions below only select the unchanged SOL097 candidate
unit and preserve historical arithmetic checks. They do NOT grant cases.
The active quartet has FOUR real files, no triple late-store copy. The late
body includes a separate two-cost-frame prefix plus its full payload capacity.
All20 mandatory pairs and all retained publication escrows are counted before
a required fit can be claimed; extra jobs and full value bounds stay open.
"""
import hashlib
import json

_STAGES = (8, 14, 19, 24, 29, 34)
_JOBS = (
    "admission-signature",
    "original-actor",
    "node-version",
    "python-version",
    "loader-list",
    "browser-version",
)
_SLACK = 131072
_PACKET_CEILING = 2_000_000
_HEADER = 128
_STREAMS = 6
_ROW_OUTPUT = 12_004_096
_ROW_READS = 16_000_000
_ROW_HASH = 8_000_000
_ROW_ALLOCATION = 16_004_096
_KNOWN_GROUPS = (196791, 121139, 189406, 218357, 277589, 118611)
_REQUIRED_JOBS = (
    "admission-signature", "original-actor",
    *("ubuntu-index-signature-%d" % index for index in range(13)),
    "node-shasums-signature",
    "node-version", "python-version", "loader-list", "browser-version",
)

# These dimensions bind the actual F99 writer/reader to the prospective
# quota. PrefixBodyMailbox checks its real Struct sizes before any birth.
# They describe this representation, not admitted native/whole-case costs.
_PUBLICATION_WIRE = (72, 24, 56, 88, 262144, 131072, 128)


def publication_pair_layout(payload_width, meta_width):
    """Disjoint actual primary/late bodies in the existing FOUR-file pair.

    Late owns its own payload after the two original terminal cost frames.
    It does not append to, overwrite or inherit the primary payload cursor.
    Both complete before-images remain retained; this is geometry, NOT a
    required-case width supplier, a cost qualification or a new grant.
    """
    cost_prefix = 2 * _PUBLICATION_WIRE[3]
    if (type(payload_width) is not int or type(meta_width) is not int
            or payload_width < cost_prefix or meta_width < _HEADER + 1):
        return None
    return {
        "primary_body": payload_width, "primary_payload_at": 0,
        "late_body": cost_prefix + payload_width,
        "late_payload_at": cost_prefix, "payload_capacity_each": payload_width,
        "metadata_each": meta_width, "cost_prefix": cost_prefix,
        "full_beforeimages": 2 * payload_width + 2 * meta_width + cost_prefix,
        "physical_output": 2 * payload_width + 2 * meta_width + cost_prefix,
        "real_files": 4, "shared_payload_cursor": False,
        "required_case_width_proven": False,
    }


def endpoint_publication_quota(body_width, meta_width, store_bytes):
    """One endpoint's prospective quota, including BOTH local files.

    body_width is the complete payload capacity of EACH endpoint. The late
    physical body also carries TWO independent 88-byte cost frames outside
    that payload. Its full before-image is therefore body_width + 176.
    Physical materialization is separately charged by the original hold;
    this quota covers future writes, not a second physical birth charge.
    This corrects dimensional accounting only, not the required-case bound.
    """
    head, region, cost_data, cost, nodes, workspace, header = _PUBLICATION_WIRE
    layout = publication_pair_layout(body_width, meta_width)
    if (layout is None or type(store_bytes) is not int
            or store_bytes != layout["full_beforeimages"]):
        return None
    local = body_width + meta_width
    return {
        "allocation": nodes * 512 + 16 * (local + store_bytes) + workspace,
        "reads": nodes * 64 + 32 * (local + store_bytes),
        "hash_bytes": 2 * (local + store_bytes) + 2 * cost_data,
        "output": local + 2 * head + region + 2 * cost,
    }


def require_publication_wire(actual):
    """Refuse a writer/reader dimension mismatch before physical births."""
    if type(actual) is not tuple or actual != _PUBLICATION_WIRE:
        raise RuntimeError("prefix-bank-publication-wire-CODE")


def confirmed_publication_workload_dimensions(unit, pairs):
    """Necessary selected chronology only, not whole-case or runtime credit.

    Admission helper finishes before actor launch. During actor RPC the SAME
    RootNative._run blocks through wait4/full transfer/close/credit completion
    before replying and before the next helper. Thus at most actor+one helper
    pair can retain future publication escrow on the ordinary confirmed path.
    Unknown/failed ends retain their affected maximum and STOP the contour.
    Full immutable original history remains charged; cumulative IO NEVER falls.
    """
    from common import OUTPUT_MAX, READ_MAX, RAM_MAX
    if type(unit) is not int or unit < max(_HEADER + 1, 176) or type(pairs) is not int or pairs < len(_REQUIRED_JOBS):
        return None
    layout=publication_pair_layout(unit,unit)
    if layout is None:
        return None
    prefix=layout["full_beforeimages"]
    each=endpoint_publication_quota(unit,unit,prefix)
    if each is None:
        return None
    # Admission, actor, then all18 required helper calls, not a six-job cap.
    active_pairs=min(2,pairs)
    completed_pairs=pairs-active_pairs
    # UNUSED report: actual pre-exec frame88 plus conservative first-bind72.
    completed_upper=2*completed_pairs*160
    physical=pairs*layout["physical_output"]
    active={key:2*active_pairs*value for key,value in each.items()}
    final_upper=physical+2*pairs*160
    prefix_peak=physical+completed_upper+active["output"]
    old=parent_custody_workload_dimensions(unit,pairs)
    return {
        "schema":"friday.astra.a252.disjoint-payload-publication-chronology.v3",
        "pairs":pairs,"minimum_required_pairs":len(_REQUIRED_JOBS),
        "unit":unit,"packet_room":unit-_HEADER,"full_beforeimages":prefix,
        "four_actual_file_backed_births_per_pair":True,
        "pair_layout":layout,"primary_and_late_payloads_are_disjoint":True,
        "old_unfinished_reservation_relation":old,
        "maximum_active_pairs_on_confirmed_ordinary_path":active_pairs,
        "active_pairs_are_actor_and_one_blocking_helper":True,
        "completed_pairs_at_last_helper":completed_pairs,
        "ordinary_completed_endpoint_output_upper":160,
        "completed_conservative_output_upper":completed_upper,
        "each_endpoint_reservation":each,"active_publication_pending":active,
        "quota_producer":"endpoint_publication_quota_shared_with_actual_before_fork_reservation",
        "both_local_files_counted":True,
        "physical_output_minimum":physical,
        "confirmed_ordinary_prefix_peak_output":prefix_peak,
        "confirmed_ordinary_prefix_final_output_upper":final_upper,
        "whole_output_cap":OUTPUT_MAX,
        "selected_prefix_ordinary_relation_fits":prefix_peak<=OUTPUT_MAX,
        "remaining_output_room_before_other_terms":OUTPUT_MAX-prefix_peak,
        "other_original_output_and_pending":"UNKNOWN_NOT_ZERO",
        "original_child_owner_and_actor_export_IO_rows":"STILL_RETAINED_NOT_ZERO_NOT_SETTLED_BY_PREFIX_CREDIT",
        "other_terms_include":["native-child-complete-owner-before-fork",
            "actor-child-table-and-accepted-stock-bootstrap-before-fork",
            "complete-actor-owner-export-before-fork","terminal/full bodies",
            "full bundle/RPC/native captures/install/member copies/selectors/final tail"],
        "full_required_packet_payload_span_alias_error_widths":"UNKNOWN_NOT_REPLACED_BY_UNIT",
        "failure_publication_costs":"ACTUAL_FULL_FRAME_OR_AFFECTED_MAX_RETAINED",
        "unconfirmed_end_policy":"STOP_UNCONFIRMED_KEEP_AFFECTED_ESCROW",
        "native_full_handover_implicit_costs":"UNKNOWN_NOT_ZERO_NOT_ROW_CREDIT",
        "required_case_width_upper_proven":False,"whole_fit":False,
        "original_all15_reduced":False,"caps_changed":False,
        "IO_refunds":False,"ACK_is_retirement":False,
        "retained_original_RAM_refunded":False,
        "physical_cost_is_measured":False,"pending_is_observed_IO":False,
        "READ_MAX":READ_MAX,"RAM_MAX":RAM_MAX,"runtime":"REQUIRED_NOT_RUN",
    }


def parent_custody_workload_dimensions(unit, pairs):
    """HISTORICAL SOL098 arithmetic, NEVER the current reservation budget.

    Preserved verbatim below as an old unfinished comparison. It omits F99
    cost frames and one local file in read/allocation terms. Neither this
    historical relation nor its fit flag is used for a current reservation.

    Every pair retains four full real file-backed births/cuts, each of unit.
    Child escrows are actual before-fork Root reservations, one per endpoint.
    They remain pending until separately confirmed actual final custody, NOT
    refunded on an ACK, and they share the ORIGINAL whole output pool.
    All future outputs and implicit costs omitted below remain UNKNOWN.
    """
    if type(unit) is not int or unit < _HEADER + 1 or type(pairs) is not int or pairs < len(_REQUIRED_JOBS):
        return None
    from common import OUTPUT_MAX, READ_MAX, RAM_MAX
    prefix = 4 * unit
    each = {
        "allocation": 262144 * 512 + 16 * (unit + prefix) + _SLACK,
        "reads": 262144 * 64 + 32 * (unit + prefix),
        "hash_bytes": 2 * (2 * unit + prefix),
        # _HEAD >8sQQQQ32s = 72; _REGION >QQII = 24.
        "output": 2 * unit + 2 * 72 + 24,
    }
    physical = pairs * 4 * unit
    pending = {key: 2 * pairs * value for key, value in each.items()}
    output = physical + pending["output"]
    return {
        "schema": "friday.sol098.selected-quartet-workload-dimensions.v1",
        "unit": unit, "pairs": pairs, "minimum_required_pairs": len(_REQUIRED_JOBS),
        "extra_required_jobs_are_zero": False,
        "real_files_per_pair": 4, "real_file_width": unit,
        "immutable_prefix_width": prefix,
        "physical_output_minimum": physical,
        "publication_pending": pending,
        "each_endpoint_pending": each,
        "physical_plus_publication_pending_output": output,
        "whole_output_cap": OUTPUT_MAX,
        "physical_only_fits": physical <= OUTPUT_MAX,
        "escrow_retaining_layout_fits": output <= OUTPUT_MAX,
        "output_shortfall_before_other_terms": max(0, output - OUTPUT_MAX),
        "read_limit": READ_MAX, "RAM_limit": RAM_MAX,
        "other_output_and_pending": "UNKNOWN_NOT_ZERO",
        "full_required_positive_widths": "UNKNOWN_NOT_REPLACED_BY_SELECTED_UNIT",
        "complete_original_domain_upper_proven": False,
        "physical_cost_is_measured": False,
        "pending_is_observed_IO": False,
        "runtime_attempt": "REQUIRED_NOT_RUN",
        "caps_changed": False,
    }



def _prospect_ok(row_r, row_h, row_o, row_a, selected, live, banks, addition):
    need_r = 3 * addition + 2 * live
    need_h = 2 * addition + 2 * live
    need_a = selected + 6 * (addition + live) + _SLACK * banks
    return row_r >= need_r and row_h >= need_h and row_o >= addition and row_a >= need_a


def _birth_ok(row_a, selected, live, banks, width):
    return selected + (width + _SLACK) + 6 * live + _SLACK * banks <= row_a


def _cut_ok(row_a, row_r, selected, live, banks, width):
    return selected + (2 * width + _SLACK) + 6 * live + _SLACK * banks <= row_a and row_r >= 2 * width


def simulate_equal_unit(unit, row_o, row_r, row_h, row_a):
    """Prospect, birth, then cohort cut for files (unit, unit, unit, 3*unit)."""
    if type(unit) is not int or unit < _HEADER + 1:
        return None
    selected = 0
    live = 0
    for banks in _STAGES:
        widths = (unit, unit, unit, 3 * unit)
        pairs = ((unit, unit), (unit, 3 * unit))
        for pair in pairs:
            for width in pair:
                if not _prospect_ok(row_r, row_h, row_o, row_a, selected, live, banks, width):
                    return None
                row_o -= width
                live += width
                if not _birth_ok(row_a, selected, live, banks, width):
                    return None
                selected += width + _SLACK
        for width in widths:
            if not _cut_ok(row_a, row_r, selected, live, banks, width):
                return None
            selected += 2 * width + _SLACK
            row_r -= 2 * width
    return {
        "unit": unit,
        "live": live,
        "selected": selected,
        "row_output_remaining": row_o,
        "row_reads_remaining": row_r,
        "row_hash_remaining": row_h,
    }


def maximum_equal_width(row_o, row_r, row_h, row_a):
    lo, hi = _HEADER + 1, _HEADER + _PACKET_CEILING
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        got = simulate_equal_unit(mid, row_o, row_r, row_h, row_a)
        if got is None:
            hi = mid - 1
        else:
            best = got
            lo = mid + 1
    if best is None:
        return None
    if simulate_equal_unit(best["unit"] + 1, row_o, row_r, row_h, row_a) is not None:
        return None
    return best


def maximum_single_file_cut(banks, row_o, row_r, row_h, row_a):
    """Widest one cohort-cut file at this bank count, before later files."""
    lo, hi = 1, _HEADER + _PACKET_CEILING
    best = None
    while lo <= hi:
        width = (lo + hi) // 2
        if not _prospect_ok(row_r, row_h, row_o, row_a, 0, 0, banks, width):
            hi = width - 1
            continue
        if not _birth_ok(row_a, 0, width, banks, width):
            hi = width - 1
            continue
        if not _cut_ok(row_a, row_r, width + _SLACK, width, banks, width):
            hi = width - 1
            continue
        best = width
        lo = width + 1
    return best


def ceiling_file_cut_shortfall(banks, row_a):
    width = _HEADER + _PACKET_CEILING
    need = (width + _SLACK) + (2 * width + _SLACK) + 6 * width + _SLACK * banks
    return need - row_a, need


def enrolled_groups(pins):
    ordered = sorted(pins, key=lambda row: row["path"])
    groups = [0, 0, 0, 0, 0, 0]
    for index, row in enumerate(ordered):
        groups[index % 6] += row["bytes"]
    return groups


def enrolled_raw_duplication(groups, row_o, row_r, row_h, row_a, meta_width):
    """Body width is group bytes plus metadata width. The other three files stay there.

    This measures whether a second enrolled copy fits. It is not a runtime sample.
    """
    selected = 0
    live = 0
    for job, banks in enumerate(_STAGES):
        body = groups[job] + meta_width
        for width in (meta_width, body, meta_width, meta_width):
            if not _prospect_ok(row_r, row_h, row_o, row_a, selected, live, banks, width):
                need_a = selected + 6 * (width + live) + _SLACK * banks
                return {
                    "fits": False,
                    "job_index": job,
                    "width": width,
                    "need_allocation": need_a,
                    "row_allocation": row_a,
                    "selected": selected,
                    "live": live,
                    "shortfall": need_a - row_a,
                    "metadata_width": meta_width,
                    "enrolled_raw_copied": False,
                    "simulator_is_runtime_measurement": False,
                }
            row_o -= width
            live += width
            if not _birth_ok(row_a, selected, live, banks, width):
                return {"fits": False, "job_index": job, "width": width, "at": "birth",
                        "enrolled_raw_copied": False, "simulator_is_runtime_measurement": False}
            selected += width + _SLACK
            if not _cut_ok(row_a, row_r, selected, live, banks, width):
                return {"fits": False, "job_index": job, "width": width, "at": "cut",
                        "enrolled_raw_copied": False, "simulator_is_runtime_measurement": False}
            selected += 2 * width + _SLACK
            row_r -= 2 * width
    return {"fits": True, "enrolled_raw_copied": False, "simulator_is_runtime_measurement": False}


def _canonical_pin_sha(pins):
    rows = []
    for pin in pins:
        rows.append({"bytes": pin["bytes"], "path": pin["path"], "sha256": pin["sha256"]})
    rows.sort(key=lambda row: row["path"])
    blob = json.dumps(rows, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    return hashlib.sha256(blob).hexdigest(), sum(row["bytes"] for row in rows), len(rows)


def _regression():
    best = maximum_equal_width(_ROW_OUTPUT, _ROW_READS, _ROW_HASH, _ROW_ALLOCATION)
    if (best is None or best["unit"] != 16222 or best["live"] != 583992
            or best["selected"] != 8043432 or best["row_output_remaining"] != 11420104
            or best["row_reads_remaining"] != 14832016):
        return None
    if sum(_KNOWN_GROUPS) != 1121893:
        return None
    dup = enrolled_raw_duplication(list(_KNOWN_GROUPS), _ROW_OUTPUT, _ROW_READS, _ROW_HASH,
                                   _ROW_ALLOCATION, _HEADER + 680)
    if (dup.get("fits") is not False or dup.get("job_index") != 4 or dup.get("width") != 278397
            or dup.get("need_allocation") != 16582779 or dup.get("selected") != 6674735
            or dup.get("live") != 739429 or dup.get("shortfall") != 578683):
        return None
    one8 = maximum_single_file_cut(8, _ROW_OUTPUT, _ROW_READS, _ROW_HASH, _ROW_ALLOCATION)
    one34 = maximum_single_file_cut(34, _ROW_OUTPUT, _ROW_READS, _ROW_HASH, _ROW_ALLOCATION)
    short, need = ceiling_file_cut_shortfall(8, _ROW_ALLOCATION)
    if one8 != 1632597 or one34 != 1253944 or short != 3307776 or need != 19311872:
        return None
    return {"best": best, "duplication": dup, "single8": one8, "single34": one34, "short": short}


def bind_alljob_external_cohort(observer, pins):
    from common import Refused
    checked = _regression()
    if checked is None:
        raise Refused("alljob_external_cohort_layout", "before-effect")
    if type(pins) not in (list, tuple) or not pins:
        raise Refused("alljob_external_cohort_pins", "before-effect")
    hold = getattr(observer, "terminal_hold", None)
    if hold is None:
        raise Refused("alljob_external_cohort_row", "before-effect")
    row = observer.pending.get(hold.token)
    if type(row) is not dict:
        raise Refused("alljob_external_cohort_row", "before-effect")
    clean = []
    for pin in pins:
        if type(pin) is not dict:
            raise Refused("alljob_external_cohort_pins", "before-effect")
        path, digest, size = pin.get("path"), pin.get("sha256"), pin.get("bytes")
        if type(path) is not str or type(digest) is not str or len(digest) != 64 or type(size) is not int or size < 0:
            raise Refused("alljob_external_cohort_pins", "before-effect")
        clean.append({"path": path, "sha256": digest, "bytes": size})
    row_o = row.get("output")
    row_r = row.get("reads")
    row_h = row.get("hash_bytes")
    row_a = row.get("allocation")
    from common import OUTPUT_MAX, READ_MAX, RAM_MAX
    for value, cap in ((row_o, OUTPUT_MAX), (row_r, READ_MAX),
                       (row_h, READ_MAX), (row_a, RAM_MAX)):
        if type(value) is not int or not 0 <= value <= cap:
            raise Refused("alljob_external_cohort_row", "before-effect")
    # Preserve the current candidate unit; do NOT lower it to force a fit.
    # This legacy six-pair selector is NOT the active physical representation
    # or an original required-case width bound. The ALL20+ quartet relation
    # including publication escrows is computed independently below.
    best = maximum_equal_width(row_o, row_r, row_h, row_a)
    if best is None:
        raise Refused("alljob_external_cohort_layout", "before-effect")
    digest, pin_sum, pin_count = _canonical_pin_sha(clean)
    groups = enrolled_groups(clean)
    duplication = enrolled_raw_duplication(groups, row_o, row_r, row_h, row_a, _HEADER + 680)
    meta_ceiling = _HEADER + _PACKET_CEILING
    ceiling = 12 * meta_ceiling + _STREAMS * _PACKET_CEILING + 4096 + 12
    packet_room = best["unit"] - _HEADER
    workload = parent_custody_workload_dimensions(best["unit"], len(_REQUIRED_JOBS))
    if workload is None:
        raise Refused("alljob_parent_custody_workload_layout", "before-effect")
    chronology=confirmed_publication_workload_dimensions(best["unit"],len(_REQUIRED_JOBS))
    if chronology is None:
        raise Refused("alljob_confirmed_publication_chronology","before-effect")
    granted = packet_room >= _PACKET_CEILING
    # Both duplication outcomes are now legal arithmetic outcomes. Neither
    # the historical failure nor a fitting duplication proves required cases.
    row_grant_complete = best is not None
    if row_grant_complete is not True:
        raise Refused("alljob_external_cohort_layout", "before-effect")
    return {
        "schema": "friday.sol099.confirmed-publication-original-pool-workload.v1",
        "representation": "four-real-file-backed-births-plus-full-parent-owned-before-images",
        "publication_escrow_in_this_map_simulator": False,
        "required_workload_dimensions": chronology,
        "old_unfinished_required_workload_dimensions":workload,
        "ordinary_prefix_chronology_fits":chronology["selected_prefix_ordinary_relation_fits"],
        "actual_completion_requires_full_native_custody_and_real_closes":True,
        "physical_copy_reduction": "NO_TRIPLE_LATE_STORE_COPY",
        "required_workload_fit": "OPEN_FULL_REQUIRED_WIDTHS_NATIVE_HANDOVER_AND_OTHER_ORIGINAL_COSTS",
        "full_publication_cost_qualified": False,
        "candidate_unit_is_original_case_bound": False,
        "caps_or_required_values_changed": False,
        "internal_read_RAM_split_changed": True,
        "whole_limits_changed": False,
        "fits": False,
        "fits_means_required_cases": False,
        "row_grant_complete": True,
        "required_cases_established": False,
        "simulator_is_runtime_measurement": False,
        "larger_positive_is_semantic_negative": False,
        "original_packet_ceiling": _PACKET_CEILING,
        "original_packet_ceiling_granted": False,
        "unit": best["unit"],
        "packet_room": packet_room,
        "store_width": publication_pair_layout(best["unit"],best["unit"])["full_beforeimages"],
        "physical_late_width": publication_pair_layout(best["unit"],best["unit"])["late_body"],
        "publication_pair_layout": publication_pair_layout(best["unit"],best["unit"]),
        "required_pairs_minimum": len(_REQUIRED_JOBS),
        "banks": 2 * len(_REQUIRED_JOBS),
        # Six pairs are a historical minimum, not the original workload cap.
        # Existing process-history128 bounds Root plus all child births.
        "maximum_banks": 2 * (128 - 1),
        "six_jobs_are_complete_workload": False,
        "next_slot": 0,
        "slots": [],
        "jobs": list(_REQUIRED_JOBS),
        "stages": [8] + [14 + 5 * index for index in range(len(_REQUIRED_JOBS) - 1)],
        "legacy_six_pair_selector_only": True,
        "live": best["live"],
        "selected": best["selected"],
        "row_output": row_o,
        "row_reads": row_r,
        "row_hash": row_h,
        "row_allocation": row_a,
        "row_output_remaining": best["row_output_remaining"],
        "row_reads_remaining": best["row_reads_remaining"],
        "row_hash_remaining": best["row_hash_remaining"],
        "caps_changed": False,
        "streams_counted_once": True,
        "enrolled_raw_copied": False,
        "pin_count": pin_count,
        "pin_byte_sum": pin_sum,
        "pin_canonical_sha256": digest,
        "enrolled_group_bytes": groups,
        "enrolled_raw_duplication": duplication,
        "known_enrolled_duplication_shortfall": checked["duplication"]["shortfall"],
        "single_file_cut_max_banks8": checked["single8"],
        "single_file_cut_max_banks34": checked["single34"],
        "metadata_ceiling_file": _HEADER + _PACKET_CEILING,
        "metadata_ceiling_cut_shortfall": checked["short"],
        "ceiling_floor_historical_design": ceiling,
        "ceiling_floor_used": False,
        "whole_output_cap": 33_554_432,
        "historical_shortfall": ceiling - 33_554_432,
        "refusal_uncommitted": 4096,
        "file_pattern": ["unit", "unit", "unit", "two_cost_frames+unit"],
        "store_regions": ["primary-body-before-image", "primary-meta-before-image",
                          "late-meta-before-image", "late-body-before-image"],
        "preimages_are_parent_owned_full_bytes": True,
        "preimages_physically_written_to_late_file": False,
        "journal_occupies_metadata_grant": True,
        "offset_space": "four-actual-before-images-then-physical-payload",
        "required_full_body_and_packet_qualification": "OPEN_NOT_SHRUNK",
    }
