"""Whole future admission costs; no capacities are called observations."""
from common import Refused,OUTPUT_MAX,INPUT_MAX,RAM_MAX,READ_MAX,exact,integer

OUTPUT_COMPONENTS=("installed","member_copies","typed_records","typed_pack_copy",
    "auth_and_legacy","native_captures_and_retention","actor_captures_and_retention",
    "selectors","bundle","rpc_transport","generated_and_public","consumer_output","terminal_tail")

# Declaration floors for the plan acquired before the mountinfo read.
# The live validation hold remains inside observer.live_alloc until the
# caller drops every alias and releases it. Do not add these floors again.
VALIDATION_READ = 2_000_001 + 513 * 4096
VALIDATION_ALLOC = 2_000_001 + 2_000_001 * 4 + 2_000_001 * 64 + 513 * (4096 + 128) * 2 + 65536

# Prospective OWNED histories and BOTH child/receiver transport arenas. These
# floors retain original 8GiB/32MiB caps: they are not grants or observations.
FD_HISTORY_FLOOR = 65536 * 8192
OWNER_WIRE_FLOOR = INPUT_MAX * 2 + 16
OWNER_RESIDENT_FLOOR = FD_HISTORY_FLOOR * 3 + INPUT_MAX * (560 * 2 + 260) + 131072 * 2

def prospective_connected_costs(admission,enrollment,observer):
    """Numeric actual-bank dimensions plus explicit open lifetime terms.

    These are prospective declarations evaluated before the dependent actor
    launch. They cannot turn later RSS, a finite floor, or an unsupported
    callback/frame body into a complete original-cap upper-bound proof.
    """
    source=sum(r['bytes'] for r in enrollment['source_files'])
    consumer=sum(r['bytes'] for r in enrollment['consumer_files'])
    input_bank=sum(r['bytes'] for r in admission['inputs'].values())
    member_bank=sum(r['size'] for rows in admission['members'].values() for r in rows if r['kind']=='regular')
    fields=('fd','holder','credit','status','identity9_decimal_strings','slot','generation',
        'attempted_close','close_history','acquisition_error','last_known_holder','last_known_credit',
        'history_arena','close_policy','cancellation_acknowledged')
    # Field names, punctuation and one-byte values only; not a generated row,
    # fixture, observed reachability claim or complete payload width estimate.
    row_key_minimum=sum(len(name)+5 for name in fields)+2
    full_history_key_minimum=65536*row_key_minimum
    return {'schema':'friday.a190.connected-prospective-dimensions.v1',
        'actual_enrolled_text_bytes':{'Source':source,'consumer':consumer},
        'actual_declared_input_bank_bytes':input_bank,'actual_member_bank_bytes':member_bank,
        'bootstrap_raw_parser_copy_floor':INPUT_MAX*516+source*16+131072,
        'Source_and_consumer_retained_text_floor':source*16+consumer*8+131072,
        'same_PID_history_arena_once_per_domain':FD_HISTORY_FLOOR,
        'Root_actor_native_distinct_history_floor':FD_HISTORY_FLOOR*3,
        'full_history_field_name_minimum':full_history_key_minimum,
        'single_wire_limit':INPUT_MAX,
        'declared_history_maximum_fits_single_wire':full_history_key_minimum<=INPUT_MAX,
        'original_workload_reachable_history_upper':None,
        'complete_value_function_module_frame_native_alias_upper':None,
        'complete_result_error_raw_transport_encode_parse_hash_copy_report_terminal_tail_fallback_upper':None,
        'full_8GiB_read_output_time_upper_proof':'REQUIRED_NOT_CLOSED',
        'actual_independent_RAM_IO_CPU':'REQUIRED_NOT_RUN_NOT_ZERO_NOT_PASS',
        'reservations_are_observations':False,'caps_changed':False}

def validate_complete_capacity(admission,enrollment,observer,installed):
    plan=exact(admission["capacity_plan"],("output_components","implicit_output_upper",
        "read_upper","resident_overlap_upper","stream_slots_upper"),"complete_capacity_plan")
    components=exact(plan["output_components"],OUTPUT_COMPONENTS,"complete_output_components")
    for value in components.values():integer(value,OUTPUT_MAX)
    for key,cap in (("implicit_output_upper",OUTPUT_MAX),("read_upper",READ_MAX),
                    ("resident_overlap_upper",RAM_MAX),("stream_slots_upper",128)):
        integer(plan[key],cap)
    if components["installed"]<installed or components["member_copies"]<installed:
        raise Refused("full_member_copy_cost_before_effect")
    base=[r for r in admission["image"]["base_members"] if r["kind"]=="regular"]
    base_bytes=sum(r["size"] for r in base)
    tool_bytes=admission["image"]["launcher"]["bytes"]+sum(r["bytes"] for r in admission["image"]["launcher_dependencies"])+admission["image"]["kernel"]["bytes"]
    copies=installed+base_bytes+tool_bytes
    if components["member_copies"]<copies:raise Refused("complete_base_member_tool_kernel_copy_cost_before_effect")
    # The current full consumer re-reads the complete initial base-member bank
    # for each base member in every typed material. This unavoidable minimum
    # cannot be hidden by a declaration or replacing full bodies with samples.
    minimum_full_member_reads=base_bytes*len(base)*228
    if plan["read_upper"]<minimum_full_member_reads+VALIDATION_READ or minimum_full_member_reads+VALIDATION_READ>READ_MAX:
        raise Refused("complete_repeated_full_bank_read_cost_before_effect")
    if components["bundle"]<INPUT_MAX or components["terminal_tail"]<observer.terminal_credit:
        raise Refused("complete_forward_cost_before_effect")
    if components["rpc_transport"]<OWNER_WIRE_FLOOR:
        raise Refused("complete_new_owner_wire_floor_before_effect")
    if components["typed_pack_copy"]<components["typed_records"]:
        raise Refused("full_typed_pack_copy_cost_before_effect")
    known=sum(components.values())+plan["implicit_output_upper"]
    if known>OUTPUT_MAX:raise Refused("complete_future_output_before_effect")
    # Every subsequent actual allocation/read/write uses the original observer
    # admission before effect; actual implicit IO and inherited peaks are still
    # independently required. This declaration neither grants authority nor
    # replaces full actual measurements with estimates.
    known_resident=observer.live_alloc+sum(r["bytes"] for r in enrollment["source_files"])*16+INPUT_MAX*516+131072+OWNER_RESIDENT_FLOOR
    if plan["resident_overlap_upper"]<known_resident or plan["stream_slots_upper"]<observer.live_slots+16+4:
        raise Refused("complete_known_resident_FD_overlap_before_effect")
    return {"complete_output_upper":known,"complete_member_tool_copy_minimum":copies,
        "minimum_repeated_full_member_read_bytes":minimum_full_member_reads,
        "new_owner_resident_floor":OWNER_RESIDENT_FLOOR,"new_owner_wire_floor":OWNER_WIRE_FLOOR,
        "known_resident_overlap_minimum":known_resident,"declared":plan,"actual_RAM_IO":"REQUIRED_NOT_RUN",
        'connected_owner_dimensions':prospective_connected_costs(admission,enrollment,observer),
        "numeric_worst_case_graph_proof":"REQUIRED_NOT_CLOSED",
        "reservations_are_observations":False,"GO":False}
