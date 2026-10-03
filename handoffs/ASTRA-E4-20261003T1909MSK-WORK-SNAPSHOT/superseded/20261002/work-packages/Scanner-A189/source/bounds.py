"""Proposed future-scan ceilings are not a grant. Hard caps stop allocation, not admission."""

# Largest held deb in the positive set is linux-modules at 169236672 bytes.
# These caps are defensive code limits. They are not permission and not evidence
# that an expanded member fits.
HARD_MAX_ARCHIVE_BYTES = 536870912
HARD_MAX_EXPANDED_BYTES = 2147483648
HARD_MAX_MEMBERS = 250000
HARD_MAX_MEMBER_NAME_BYTES = 65535
HARD_MAX_DEPTH = 256
HARD_MAX_FDS = 1024
HARD_MAX_OUTPUT_BYTES = 67108864
HARD_MAX_WALL_MS = 3600000
HARD_MAX_METADATA_BYTES = 1048576

CEILING_KEYS = (
    "max_archive_bytes",
    "max_expanded_bytes",
    "max_members",
    "max_member_name_bytes",
    "max_depth",
    "max_fds",
    "max_output_bytes",
    "max_wall_ms",
)

PROPOSED_CEILINGS = {
    "max_archive_bytes": HARD_MAX_ARCHIVE_BYTES,
    "max_expanded_bytes": HARD_MAX_EXPANDED_BYTES,
    "max_members": HARD_MAX_MEMBERS,
    "max_member_name_bytes": 4096,
    "max_depth": 64,
    "max_fds": 16,
    "max_output_bytes": HARD_MAX_OUTPUT_BYTES,
    "max_wall_ms": 120000,
}

PROPOSED_IS_NOT_ADMISSION = True

# A refusal still has to be returned after ordinary work has exhausted its lane.
# This capacity is established once before JSON/schema ingress, never refunded.
TERMINAL_OUTPUT_CAP = 8192
TERMINAL_WORK_CAP = 1048576
TERMINAL_LIVE_CAP = 262144

# Current lab-task limits. They bound this source job, not a future body scan.
CURRENT_TASK_LIMITS = {
    "wall_clock_sec": 6600,
    "seal_reserve_seconds": 600,
    "memory_bytes": 8589934592,
    "read_bytes": 268435456,
    "source_output_bytes": 16777216,
    "network_calls": 0,
    "model_calls": 0,
    "effects": 0,
}


def _hex64(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def performing_requested(resources):
    return (
        isinstance(resources, dict)
        and resources.get("performing_whole") is True
        and resources.get("fixture_only") is not True
    )


def admission_cause(resources):
    """Return a cause when the caller did not supply a typed admission.

    A future caller can pass admitted=True with a 64-hex grant digest and
    integer ceilings. PROPOSED_CEILINGS alone has no admitted flag and is
    refused. This function does not mint a grant.
    """
    if not isinstance(resources, dict):
        return "resource_admission_absent"
    if resources.get("performing_whole") is True and resources.get("fixture_only") is True:
        return "resource_admission_absent"
    if resources.get("admitted") is not True:
        return "resource_admission_absent"
    grant = resources.get("grant")
    if not isinstance(grant, dict) or not _hex64(grant.get("sha256")):
        return "resource_grant_absent"
    ceilings = resources.get("ceilings")
    if not isinstance(ceilings, dict):
        return "resource_ceilings_absent"
    for key in CEILING_KEYS:
        value = ceilings.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            return "resource_ceilings_absent"
    if ceilings["max_archive_bytes"] > HARD_MAX_ARCHIVE_BYTES:
        return "resource_ceiling_above_hard_cap"
    if ceilings["max_expanded_bytes"] > HARD_MAX_EXPANDED_BYTES:
        return "resource_ceiling_above_hard_cap"
    if ceilings["max_members"] > HARD_MAX_MEMBERS:
        return "resource_ceiling_above_hard_cap"
    if ceilings["max_member_name_bytes"] > HARD_MAX_MEMBER_NAME_BYTES:
        return "resource_ceiling_above_hard_cap"
    if ceilings["max_depth"] > HARD_MAX_DEPTH:
        return "resource_ceiling_above_hard_cap"
    if ceilings["max_fds"] > HARD_MAX_FDS:
        return "resource_ceiling_above_hard_cap"
    if ceilings["max_output_bytes"] > HARD_MAX_OUTPUT_BYTES:
        return "resource_ceiling_above_hard_cap"
    if ceilings["max_wall_ms"] > HARD_MAX_WALL_MS:
        return "resource_ceiling_above_hard_cap"
    if ceilings["max_output_bytes"] < 8192:
        return "resource_ceilings_absent"
    if performing_requested(resources):
        if grant.get("actual_permission") is not True or grant.get("role") != "readonly-archive-scan":
            return "resource_grant_absent"
        for key in ("max_live_bytes", "max_read_bytes", "max_work_bytes"):
            if not isinstance(ceilings.get(key), int) or isinstance(ceilings[key], bool) or ceilings[key] < 1:
                return "resource_ceilings_absent"
        rss = ceilings.get("max_rss_bytes")
        if not isinstance(rss, int) or isinstance(rss, bool) or rss < 1:
            return "resource_ceilings_absent"
        if grant.get("filesystem_read") is not True:
            return "resource_grant_absent"
        if "now_ns" not in resources or "started_ns" not in resources:
            return "resource_clock_invalid"
        timed = clock_cause(resources)
        if timed is not None:
            return timed
    return None


def reserve(resources, meter, **delta):
    """Record the budget before the caller reads, decodes, or copies."""
    return charge(resources, meter, **delta)


def release_live(meter, amount):
    if isinstance(meter, dict) and isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
        owner=meter.get('source_raw_owner')
        if owner is not None and (owner.head is not None or owner.pending is not None or owner.pending_recorder is not None):
            # Captured tracebacks may alias the producer's complete scratch.
            # Logical release is not proof those native objects were freed.
            return
        meter["live_bytes"] = max(0, meter.get("live_bytes", 0) - amount)


def bootstrap_reserve(meter, size):
    """Fixed bootstrap bounds precede every untrusted JSON allocator."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > 20000000:
        return "canonical_ingress_refused"
    return charge({"ceilings": {"max_work_bytes": 160000000, "max_live_bytes": 1280000000,
                               "max_output_bytes": HARD_MAX_OUTPUT_BYTES}}, meter,
                  work_bytes=size * 8, live_bytes=size * 64)


def charge_output(resources, meter, document):
    """Bound the canonical buffer by the remaining output ceiling, then charge its actual length."""
    from .canonical import canonical_bytes_bounded

    ceilings = resources.get("ceilings") if isinstance(resources, dict) else None
    cap = ceilings.get("max_output_bytes") if isinstance(ceilings, dict) else HARD_MAX_OUTPUT_BYTES
    if not isinstance(cap, int) or isinstance(cap, bool) or not isinstance(meter, dict):
        return "resource_ceiling_exceeded"
    already = meter.get("output_bytes", 0)
    if not isinstance(already, int) or isinstance(already, bool) or already < 0:
        return "resource_ceiling_exceeded"
    room = cap - already
    if room < 1:
        return "resource_ceiling_exceeded"
    blob = canonical_bytes_bounded(document, room, resources, meter)
    if blob is None:
        return "resource_ceiling_exceeded"
    try:
        return charge(resources, meter, output_bytes=len(blob))
    finally:
        amount = len(blob)
        blob = None
        release_live(meter, amount)


def new_meter(raw_owner=None):
    import time
    from tools.native_support import selected_owner,source_raw_owner,_source_raw_scope
    if raw_owner is None:raw_owner=source_raw_owner()
    token=None
    try:
        token=_source_raw_scope.set(raw_owner)
        native=selected_owner()
        physical=native.snapshot() if native is not None else None
        prepaid={'output_bytes':TERMINAL_OUTPUT_CAP,'work_bytes':TERMINAL_WORK_CAP,
                 'live_bytes':TERMINAL_LIVE_CAP}
        if physical is not None:
            prepaid={'output_bytes':physical['terminal_output_bytes'],
                     'work_bytes':physical['terminal_work_bytes'],
                     'live_bytes':physical['terminal_live_bytes']}
        return {
            'source_raw_owner':raw_owner,
            'source_raw_scope_token':token,
            "bootstrap_started_ns": physical['started_ns'] if physical is not None else time.monotonic_ns(),
            "native_actual_entry_read_bytes": physical['actual_read_bytes'] if physical is not None else None,
            "read_bytes": 0,
            "expanded_bytes": 0,
            "output_bytes": 0,
            "work_bytes": 0,
            "members": 0,
            "fds": physical['fds'] if physical is not None else 0,
            "fds_peak": physical['fds'] if physical is not None else 0,
            "live_bytes": 0,
            "live_peak": 0,
            "terminal_prepaid": prepaid,
            "native_owner":native,
            "terminal_spent": False,
            "terminal_trace_capacity": {"output_bytes":0,"work_bytes":0,"live_bytes":0},
            "terminal_causal_capacity": {"output_bytes":0,"work_bytes":0,"live_bytes":0},
            "terminal_capacity_cause": None,
            "actual_read_bytes": 0,
            "phase_trace": [],
            "fd_close_uncertainties": [],
        }

    except BaseException as exc:
        raw_owner.capture(exc)
        if token is not None:_source_raw_scope.reset(token)
        raise


def terminal_meter(meter):
    """Consume the one prepaid completion/refusal lane, preserving ordinary totals."""
    if not isinstance(meter, dict):
        return None
    if meter.get("terminal_spent") is True:
        return meter.get("terminal_lane_meter")
    prepaid = meter.get("terminal_prepaid")
    if not isinstance(prepaid,dict) or set(prepaid)!={'output_bytes','work_bytes','live_bytes'} or any(type(value) is not int or value<1 for value in prepaid.values()):
        return None
    meter["terminal_spent"] = True
    for key,value in prepaid.items():
        meter[key] = meter.get(key, 0) + value
    # The terminal encoder has finite depth/scalars and its own already admitted
    # capacity. A wall/RSS failure is the output cause, not an excuse to lose it.
    # Complete reached traces grow a distinct prepaid subdomain INSIDE the
    # original max_output/work/live ceilings before an entry can be reached.
    # These are conservative reservations, never actual encoded-byte counters.
    extra=meter.get("terminal_trace_capacity",{})
    causal=meter.get("terminal_causal_capacity",{})
    limits={key:value+extra.get(key,0)+causal.get(key,0) for key,value in prepaid.items()}
    lane = {"work_bytes": 0, "live_bytes": 0, "output_bytes": 0,
            "terminal_lane": True, "terminal_limits": limits,
            "bootstrap_started_ns": meter["bootstrap_started_ns"],
            'source_raw_owner':meter.get('source_raw_owner')}
    # The selected14-schema graph is already physically owned by this same
    # call. Terminal validation borrows it; it must not create a new metadata
    # IO/FD producer in a lane that admits only work/live/output.
    if isinstance(meter.get('schema_snapshot'),dict):
        lane['schema_snapshot']=meter['schema_snapshot']
    if meter.get('native_owner') is not None:
        lane['native_owner']=meter['native_owner']
    meter["terminal_lane_meter"] = lane
    return lane


def reserve_terminal_causal(resources, meter, primary):
    """Admit the complete causal graph, not a projected 8192-byte refusal.

    This reservation cannot repair allocations made by a producer before this
    call. If prior work exhausted the unchanged ceiling, the missing producer
    reservation remains explicit; this function does not invent capacity.
    """
    if not isinstance(meter, dict):
        return "resource_ceiling_exceeded"
    from .canonical import canonical_resource_bound
    bound = canonical_resource_bound({"detail":primary.get("detail"),
        "phase_trace":meter.get("phase_trace",[]),
        "terminal_failure":primary.get("terminal_failure")})
    if bound is None:
        meter["terminal_capacity_cause"] = "expected_shape_invalid"
        return "expected_shape_invalid"
    needed = {"output_bytes":bound["output_bytes"]*2,
              "work_bytes":bound["work_bytes"]*2,
              "live_bytes":bound["live_bytes"]*2}
    if meter.get('native_owner') is not None:
        # Native terminal capacity was physically isolated BEFORE Python
        # initialization, input decoding, or any exception object producer.
        # This consumer checks complete successors against that fixed reserve;
        # it cannot grow the physical lane or confer a new role/grant.
        limits=meter['terminal_prepaid']
        if any(needed[key]>limits[key] for key in needed):
            return 'resource_ceiling_exceeded'
        return None
    if meter.get('terminal_spent') is True:
        lane=meter.get('terminal_lane_meter')
        return None if isinstance(lane,dict) and all(needed[key]<=lane['terminal_limits'][key]-lane.get(key,0) for key in needed) else 'resource_ceiling_exceeded'
    previous = meter["terminal_causal_capacity"]
    delta = {key:max(0,value-previous[key]) for key,value in needed.items()}
    tick = reserve(resources,meter,**delta)
    if tick:
        meter["terminal_capacity_cause"] = tick
        return tick
    for key,value in delta.items():
        previous[key] += value
    meter["terminal_capacity_cause"] = None
    return None


def _sample_host(resources, meter):
    """Sample host RSS and a monotonic deadline only on an explicit whole-scan admission.

    Fixture resources set fixture_only and omit performing_whole, so caller clocks
    such as resource_time_exceeded stay reachable.
    """
    if resources.get("performing_whole") is not True or resources.get("fixture_only") is True:
        return None
    import resource
    import time

    now = time.monotonic_ns()
    resources["started_ns"] = meter["bootstrap_started_ns"]
    resources["now_ns"] = now
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss = int(usage.ru_maxrss) * 1024
    previous = meter.get("rss_bytes")
    if isinstance(previous, int) and not isinstance(previous, bool) and rss < previous:
        return "resource_clock_invalid"
    meter["rss_bytes"] = rss
    return None


def charge(resources, meter, **delta):
    """Add monotonic budget counters and refresh the admission clock.

    Host monotonic time and RSS are sampled. A caller-supplied rss_bytes value is refused when
    it moves backwards or exceeds an optional max_rss_bytes ceiling.
    """
    if not isinstance(meter, dict):
        return None
    if meter.get("terminal_lane") is True:
        for key,value in delta.items():
            if not isinstance(value,int) or isinstance(value,bool) or value < 0:
                return "resource_ceiling_exceeded"
            if key not in ("work_bytes","live_bytes","output_bytes"):
                return "resource_ceiling_exceeded"
            if meter.get(key,0) + value > meter["terminal_limits"][key]:
                return "resource_ceiling_exceeded"
        for key,value in delta.items():
            meter[key] = meter.get(key,0) + value
        return None
    import resource
    import time
    started = meter.get("bootstrap_started_ns")
    if isinstance(started, int) and time.monotonic_ns() - started > HARD_MAX_WALL_MS * 1000000:
        return "resource_time_exceeded"
    if int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024 > 8589934592:
        return "resource_ceiling_exceeded"
    # The native preinitialization owner covers kwargs/integers/container
    # production too. Validate projected values without cloning the whole meter.
    for key, value in delta.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return "resource_ceiling_exceeded"
    if not isinstance(resources, dict):
        for key,value in delta.items(): meter[key]=meter.get(key,0)+value
        meter['fds_peak']=max(meter.get('fds_peak',0),meter.get('fds',0))
        meter['live_peak']=max(meter.get('live_peak',0),meter.get('live_bytes',0))
        return None
    if performing_requested(resources):
        from tools.native_support import native_entry_cause
        owner_cause=native_entry_cause(resources,meter)
        if owner_cause: return owner_cause
    sampled = _sample_host(resources, meter)
    if sampled is not None:
        return sampled
    timed = clock_cause(resources)
    if timed is not None:
        return timed
    ceilings = resources.get("ceilings")
    if not isinstance(ceilings, dict):
        for key,value in delta.items(): meter[key]=meter.get(key,0)+value
        return None
    limits = {
        "read_bytes": ceilings.get("max_read_bytes", ceilings.get("max_archive_bytes")),
        "expanded_bytes": ceilings.get("max_expanded_bytes"),
        "output_bytes": ceilings.get("max_output_bytes"),
        "members": ceilings.get("max_members"),
        "fds": ceilings.get("max_fds"),
        "live_bytes": ceilings.get("max_live_bytes", ceilings.get("max_rss_bytes")),
        "work_bytes": ceilings.get("max_work_bytes"),
    }
    if meter.get("terminal_spent") is not True:
        for key,value in meter.get("terminal_prepaid", {}).items():
            if isinstance(limits.get(key),int):
                limits[key] -= value
    archive_cap = ceilings.get("max_archive_bytes")
    expanded_cap = ceilings.get("max_expanded_bytes")
    if isinstance(archive_cap, int) and isinstance(expanded_cap, int):
        if limits["work_bytes"] is None:
            limits["work_bytes"] = archive_cap + expanded_cap
    rss_cap = ceilings.get("max_rss_bytes")
    if isinstance(rss_cap, int) and not isinstance(rss_cap, bool):
        limits["rss_bytes"] = rss_cap
    for key, cap in limits.items():
        if not isinstance(cap, int) or isinstance(cap, bool):
            continue
        if meter.get(key, 0)+delta.get(key,0) > cap:
            return "resource_ceiling_exceeded"
    reported = resources.get("rss_bytes")
    if isinstance(reported, int) and not isinstance(reported, bool):
        previous = meter.get("rss_bytes")
        if isinstance(previous, int) and not isinstance(previous, bool) and reported < previous:
            return "resource_clock_invalid"
        meter["rss_bytes"] = reported
        if isinstance(rss_cap, int) and not isinstance(rss_cap, bool) and reported > rss_cap:
            return "resource_ceiling_exceeded"
    for key,value in delta.items(): meter[key]=meter.get(key,0)+value
    meter['fds_peak']=max(meter.get('fds_peak',0),meter.get('fds',0))
    meter['live_peak']=max(meter.get('live_peak',0),meter.get('live_bytes',0))
    return None


def clock_cause(resources):
    started = resources.get("started_ns")
    now = resources.get("now_ns")
    if started is None and now is None:
        return None
    if not isinstance(started, int) or isinstance(started, bool):
        return "resource_clock_invalid"
    if not isinstance(now, int) or isinstance(now, bool):
        return "resource_clock_invalid"
    if now < started:
        return "resource_clock_invalid"
    elapsed_ms = (now - started) // 1000000
    if elapsed_ms > resources["ceilings"]["max_wall_ms"]:
        return "resource_time_exceeded"
    return None
