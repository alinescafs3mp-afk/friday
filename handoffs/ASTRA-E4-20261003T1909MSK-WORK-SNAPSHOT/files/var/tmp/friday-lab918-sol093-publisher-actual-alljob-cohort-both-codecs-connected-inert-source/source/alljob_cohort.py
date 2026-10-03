"""All-job external cohort on the existing terminal row.

Equal file unit, one immutable store per job, no second copy of enrolled raw.
The ceiling conjunction is recorded as a historical reservation design.
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


def _prospect_ok(row_r, row_h, row_o, row_a, selected, live, banks, addition):
    need_r = addition + 2 * addition + 2 * live
    need_h = 2 * addition + 2 * live
    need_a = selected + 6 * (addition + live) + _SLACK * banks
    return row_r >= need_r and row_h >= need_h and row_o >= addition and row_a >= need_a


def _birth_ok(row_a, selected, live, banks, width):
    backing = 6 * live + _SLACK * banks
    return selected + (width + _SLACK) + backing <= row_a


def _cut_ok(row_a, row_r, selected, live, banks, width):
    backing = 6 * live + _SLACK * banks
    return selected + (2 * width + _SLACK) + backing <= row_a and row_r >= 2 * width


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
    lo, hi = _HEADER + 1, 80_000
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


def enrolled_groups(pins):
    ordered = sorted(pins, key=lambda row: row["path"])
    groups = [0, 0, 0, 0, 0, 0]
    for index, row in enumerate(ordered):
        groups[index % 6] += row["bytes"]
    return groups


def enrolled_raw_duplication(groups, row_o, row_r, row_h, row_a, meta_width):
    """Body width = group bytes + metadata width. Three other files stay at that width.

    This is the measured non-fit of copying enrolled raw into the banks.
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
                }
            row_o -= width
            live += width
            if not _birth_ok(row_a, selected, live, banks, width):
                return {"fits": False, "job_index": job, "width": width, "at": "birth"}
            selected += width + _SLACK
            if not _cut_ok(row_a, row_r, selected, live, banks, width):
                return {"fits": False, "job_index": job, "width": width, "at": "cut"}
            selected += 2 * width + _SLACK
            row_r -= 2 * width
    return {"fits": True, "enrolled_raw_copied": False}


def _canonical_pin_sha(pins):
    rows = []
    for pin in pins:
        rows.append({"bytes": pin["bytes"], "path": pin["path"], "sha256": pin["sha256"]})
    rows.sort(key=lambda row: row["path"])
    blob = json.dumps(rows, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    return hashlib.sha256(blob).hexdigest(), sum(row["bytes"] for row in rows), len(rows)


def bind_alljob_external_cohort(observer, pins):
    from common import Refused
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
    if type(row_o) is not int or type(row_r) is not int or type(row_h) is not int or type(row_a) is not int:
        raise Refused("alljob_external_cohort_row", "before-effect")
    best = maximum_equal_width(row_o, row_r, row_h, row_a)
    digest, pin_sum, pin_count = _canonical_pin_sha(clean)
    groups = enrolled_groups(clean)
    # 128 + 680 is the smallest header-plus-marker width used for the duplication probe.
    duplication = enrolled_raw_duplication(groups, row_o, row_r, row_h, row_a, _HEADER + 680)
    meta_ceiling = _HEADER + _PACKET_CEILING
    ceiling = 12 * meta_ceiling + _STREAMS * _PACKET_CEILING + 4096 + 12
    fits = best is not None and duplication.get("fits") is False
    ledger = {
        "schema": "friday.lab918.alljob-external-cohort.v1",
        "fits": fits,
        "unit": None if best is None else best["unit"],
        "packet_room": None if best is None else best["unit"] - _HEADER,
        "store_width": None if best is None else 3 * best["unit"],
        "banks": 12,
        "next_slot": 0,
        "slots": [],
        "jobs": list(_JOBS),
        "stages": list(_STAGES),
        "live": None if best is None else best["live"],
        "selected": None if best is None else best["selected"],
        "row_output": row_o,
        "row_reads": row_r,
        "row_hash": row_h,
        "row_allocation": row_a,
        "row_output_remaining": None if best is None else best["row_output_remaining"],
        "row_reads_remaining": None if best is None else best["row_reads_remaining"],
        "row_hash_remaining": None if best is None else best["row_hash_remaining"],
        "caps_changed": False,
        "streams_counted_once": True,
        "enrolled_raw_copied": False,
        "pin_count": pin_count,
        "pin_byte_sum": pin_sum,
        "pin_canonical_sha256": digest,
        "enrolled_group_bytes": groups,
        "enrolled_raw_duplication": duplication,
        "ceiling_floor_historical_design": ceiling,
        "ceiling_floor_used": False,
        "whole_output_cap": 33_554_432,
        "historical_shortfall": ceiling - 33_554_432,
        "refusal_uncommitted": 4096,
        "file_pattern": ["unit", "unit", "unit", "store"],
        "store_regions": ["primary-body", "primary-meta", "late-meta"],
    }
    if fits is not True:
        raise Refused("alljob_external_cohort_layout", "before-effect", ledger)
    return ledger
