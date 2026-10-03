"""Whole future admission costs; no capacities are called observations."""
from common import Refused,OUTPUT_MAX,INPUT_MAX,RAM_MAX,READ_MAX,DOCUMENT_MAX,exact,integer,MEMBERS_MAX,SLOTS_MAX,WALL_MAX

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

def connected_export_authority():
    """A190 directions index 4 keeps one INPUT_MAX wire for the whole transfer.

    The selected prefix-closed installer chronology is encoded once. Literal
    JSON width times the cap construction is recorded and is not a witness.
    """
    cached=getattr(connected_export_authority,'_cached',None)
    if cached is not None:
        return cached
    from lifetime import FD_HISTORY_MAX
    from common import lossless_history_upper
    upper=lossless_history_upper()
    encoded=upper['encoded_upper']
    reachable_rows=upper['reachable_history_rows']
    # Keep the existing prospective reservation floor, not a transport copy.
    # Journal projection is constant. This old numeric allowance is neither
    # actual collection bytes nor a whole-body upper; no grant is reduced here.
    collection_index_bytes=reachable_rows*(len(str(reachable_rows))+1)+256
    wire_bytes=encoded+collection_index_bytes
    member_name_max=240
    components=member_name_max//2
    parent_rows=1+(components-1)
    output_rows=1
    rows_per_member=parent_rows+output_rows
    installer_rows=MEMBERS_MAX*rows_per_member
    identity9_minimum=len('["0","0","0","0","0","0","0","0","0"]')
    name_table=len('["fd","holder","credit","status","identity9_decimal_strings"]')
    identity_floor=installer_rows*identity9_minimum
    wire=name_table+identity_floor
    fits=wire_bytes<=INPUT_MAX and upper['fits_single_wire'] is True
    canonical_allocation=wire_bytes*4+1024
    hash_bytes=wire_bytes
    reads=wire_bytes*3
    output=wire_bytes*2
    cached={'authority':'A190.directions[4]','path':'extraction.Installer.consume._parents',
        'pinned_wire':INPUT_MAX,'member_name_max':member_name_max,'components_max':components,
        'rows_per_member':rows_per_member,'members_max':MEMBERS_MAX,
        'installer_history_rows':installer_rows,'history_ceiling':FD_HISTORY_MAX,
        'reachable_history_rows':upper['reachable_history_rows'],'pattern_count':upper['pattern_count'],
        'identity9_minimum_bytes':identity9_minimum,'identity9_json_width':identity9_minimum,
        'shared_field_name_table_bytes':name_table,'identity9_floor_bytes':identity_floor,
        'wire_after_shared_names':wire,'single_wire_limit':INPUT_MAX,
        'cap_construction_rows':installer_rows,
        'cap_construction_times_json_width_is_witness':False,'is_lower_bound':False,
        'representation':upper['schema'],'encoded_upper':encoded,'codec_encoded_upper':encoded,
'collection_index_bytes':collection_index_bytes,'encoded_with_collection_refs':wire_bytes,
        'collection_index_transport':False,
        'collection_budget_term':'LEGACY_RETAINED_RESERVATION_FLOOR_NOT_ACTUAL_BODY_UPPER',
        'selected_chronology_is_universal_upper':False,'fits_single_wire':fits,
        'excess_bytes':encoded-INPUT_MAX if encoded>INPUT_MAX else 0,
        'producer_receiver_failure_reads':reads,'send_and_failure_output':output,
        'canonical_allocation':canonical_allocation,'hash_bytes':hash_bytes,
        'one_wire_read':reads,'one_wire_output':output,
        'fork_census_rows':upper['fork_census_rows'],
        'fork_census_allocation':(SLOTS_MAX+1)*512+131072}
    connected_export_authority._cached=cached
    return cached

_AUTHORITY=connected_export_authority()
OWNER_WIRE_FLOOR = INPUT_MAX
OWNER_RESIDENT_FLOOR = _AUTHORITY['canonical_allocation']+_AUTHORITY['send_and_failure_output']+_AUTHORITY['hash_bytes']+131072*2

def _whole_bound():
    """Partial codec/constant-descriptor dimensions, not actual arena fit.

    The selected installer chronology is not a universal sample. Semantic rows
    stay in the codec once. Journal projections do not carry a second history.
    Same-PID history is one arena. The native receiver shares that resident.
    The child owner is a distinct arena and a distinct resident. Unknown RSS
    and implicit IO stay unknown.
    """
    authority=connected_export_authority()
    single=authority['encoded_with_collection_refs']
    canon=authority['canonical_allocation']
    history_once=FD_HISTORY_FLOOR
    arenas=3
    history_bytes=history_once*arenas
    resident=canon*2+history_bytes
    reads=authority['producer_receiver_failure_reads']
    output=authority['send_and_failure_output']
    bound={'schema':'friday.sol070.partial-prospective-dimensions.v2','status':'PARTIAL_FACTORY_DIMENSIONS_NOT_UNIVERSAL_PROOF',
        'complete_original_domain_upper_proven':False,
        'codec_bytes':authority['codec_encoded_upper'],'collection_index_bytes':authority['collection_index_bytes'],
        'single_wire_bytes':single,'single_wire_limit':INPUT_MAX,'fits_single_wire':single<=INPUT_MAX,
        'same_pid_history_arena_once':history_once,'distinct_owner_history_arenas':arenas,
        'history_arena_bytes':history_bytes,'same_pid_resident_bytes':canon,
        'distinct_child_resident_bytes':canon,'native_shares_same_pid_resident':True,
        'resident_bytes':resident,'resident_limit':RAM_MAX,'fits_resident':resident<=RAM_MAX,
        'hash_bytes_once':authority['hash_bytes'],'read_bytes':reads,'read_limit':READ_MAX,
        'fits_read':reads+VALIDATION_READ<=READ_MAX,'output_bytes':output,'output_limit':OUTPUT_MAX,
        'fits_output':output<=OUTPUT_MAX,'wall_sec':WALL_MAX,'caps_changed':False,
        'selected_chronology_is_universal_upper':False,
        'sharing':['same-pid history arena once','native receiver shares parent resident','lossless codec stores each semantic row once','journal projection is constant; actual body/key root chunks remain full'],
        'unknown_not_zero':['peak_rss','implicit_helper_io','final_seal_transport',
            'sol070_all_selected_code_consts_context_defaults_and_token_transitions',
            'sol070_identity_owners_pending_and_both_receiver_retained_error_roots',
            'standalone_repeated_call_roots_after_original_call_refund',
            'post_retirement_native_final_end_and_ordinary_close_faults',
            'sol085_actual_full_row_dictionary_body_key_and_alias_arena',
            'sol085_all_live_history_domain_bank_codec_credit_fault_tables',
            'sol085_decode_deepcopy_primary_lookup_and_full_value_validation_overlap',
            'sol086_all_actual_binaries_existing_prepaid_terminal_pool_shared_overlap',
            'sol086_full_prepared_bank_raw_fork_or_actor_hex_request_reply_frames',
            'sol086_all_prefix_metadata_canonical_fit_and_mutable_snapshot_lifetime',
            'sol086_all_new_prepared_FDs_histories_and_true_outside_end_retirement']}
    if not (bound['fits_single_wire'] and bound['fits_resident'] and bound['fits_read'] and bound['fits_output']):
        raise Refused('prospective_whole_bound_before_effect')
    return bound

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
        'original_workload_reachable_history_upper':_AUTHORITY['reachable_history_rows'],
        'workload_history_upper_fits_single_wire':_AUTHORITY['fits_single_wire'],
        'identity9_floor_bytes':_AUTHORITY['identity9_floor_bytes'],
        'wire_after_shared_names':_AUTHORITY['wire_after_shared_names'],
        'encoded_upper':_AUTHORITY['encoded_upper'],
        'cap_construction_is_reachable_witness':False,
        'a190_direction_index':4,
        'selected_chronology_is_universal_upper':False,
        'complete_value_function_module_frame_native_alias_upper':'SELECTED_MUTABLE_BOUND_FOREIGN_HEAP_NOT_WALKED',
        'complete_result_error_raw_transport_encode_parse_hash_copy_report_terminal_tail_fallback_upper':_whole_bound(),
        'full_8GiB_read_output_time_upper_proof':_whole_bound(),
        'workload_encode_parse_upper':'OPEN_NOT_UNIVERSAL_UPPER_NO_EXECUTION_CREDIT',
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
    if _AUTHORITY['fits_single_wire'] is not True or _AUTHORITY['encoded_upper']>INPUT_MAX:
        raise Refused('owner_export_single_wire_before_effect','before-effect')
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
        "numeric_worst_case_graph_proof":"DERIVED_BEFORE_EFFECT",
        "reservations_are_observations":False,"GO":False}

def prepared_full_body_dimensions(body_bytes, fork_headers=0, rpc_wire_bytes=0):
    """Actual physical-carrier dimensions, never a whole-prefix proof.

    Body bytes are complete actual node bodies, counted once per real identity.
    Fork: raw pipe + Root pwrite + complete hash/readback + Held full reread.
    Actor RPC additionally transports its actual complete hex chunk requests
    and replies through the existing prepaid owner channel. Native private
    layouts, full graph/traceback arenas and finite outside end stay CODE.
    """
    integer(body_bytes,DOCUMENT_MAX)
    integer(fork_headers,READ_MAX);integer(rpc_wire_bytes,READ_MAX)
    return {'complete_body_bytes':body_bytes,
        'Root_prepared_endpoint_FDs_per_carrier':1,
        'Root_full_physical_reads_and_transport':4*body_bytes+fork_headers+rpc_wire_bytes,
        'complete_memory_hash_bytes':2*body_bytes,
        'Root_and_existing_child_output':2*body_bytes+fork_headers+rpc_wire_bytes,
        'original_and_snapshot_full_bank_copy_overlap_floor':6*body_bytes+131072,
        'existing_prepaid_credit_only':True,'original_caps_changed':False,
        'child_calls_inherited_Root_meter':False,'native_body_grant':False,
        'new_path_open_in_failure':False,'new_ordinary_reserve_in_failure':False,
        'whole_prefix_upper_proven':False,'C2':'UNKNOWN_NOT_ZERO_NOT_PROVEN'}

