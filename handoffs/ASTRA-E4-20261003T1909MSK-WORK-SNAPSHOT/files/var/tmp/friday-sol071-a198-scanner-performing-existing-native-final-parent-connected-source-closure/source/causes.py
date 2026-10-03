"""Typed scan results. Structural observations never grant publisher proof."""

SCAN_RESULT_SCHEMA = "friday.astra.e4.lab821.scan-result.v1"

_INGRESS = "AUTHENTICATED_INGRESS"
_CUSTODY = "HELD_CUSTODY_AND_ADMISSION"
_STREAM = "SHARED_STREAM_AND_CODEC"
_ARCHIVE = "ARCHIVE_AND_MEMBER_CLOSURE"
_META = "RAW_METADATA_CORRESPONDENCE"
_AGG = "AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER"
_FINAL = "FINAL_SCHEMA_OUTPUT_AND_CUSTODY"


def enter_phase(meter, stage, operation, subject=None):
    """Record an actually entered consumer, not a cause-to-stage prediction."""
    if not isinstance(meter,dict):
        return None
    from .bounds import reserve
    resources = meter.get("active_resources")
    from .bounds import performing_requested
    if not isinstance(stage,str) or not isinstance(operation,str) or not 1<=len(operation)<=128 or subject is not None and (not isinstance(subject,str) or len(subject)>65535):
        return "expected_shape_invalid"
    if len(meter.get("phase_trace",[]))>=250000:
        return "resource_ceiling_exceeded"
    amount=512+12*(len(stage)+len(operation)+(len(subject) if subject is not None else 0))
    extra={"output_bytes":amount,"work_bytes":amount*16,"live_bytes":amount*4} if performing_requested(resources) else {}
    tick = reserve(resources,meter,work_bytes=256+extra.get("work_bytes",0),live_bytes=512+extra.get("live_bytes",0),output_bytes=extra.get("output_bytes",0))
    if tick:
        return tick
    capacity=meter.setdefault("terminal_trace_capacity",{"output_bytes":0,"work_bytes":0,"live_bytes":0})
    for key,value in extra.items(): capacity[key]+=value
    if "archive_live_start" in meter:
        meter["archive_persistent_live"]=meter.get("archive_persistent_live",0)+512+extra.get("live_bytes",0)
    trace = meter.setdefault("phase_trace",[])
    trace.append({"ordinal":len(trace),"stage":stage,"operation":operation,"subject":subject})
    meter["reached_stage"] = stage
    meter["reached_operation"] = operation
    return None

CAUSE_STAGE = {
    "canonical_ingress_refused": _INGRESS,
    "schema_rejected": _INGRESS,
    "schema_unknown": _INGRESS,
    "oracle_not_pinned": _INGRESS,
    "ingress_context_unbound": _INGRESS,
    "plan_unbound": _INGRESS,
    "expected_shape_invalid": _INGRESS,
    "unknown_archive_class": _INGRESS,
    "self_approved_caller": _INGRESS,
    "transferred_execution_rejected": _INGRESS,
    "publisher_evidence_shape_invalid": _INGRESS,
    "held_bytes_absent": _CUSTODY,
    "held_bytes_rejected": _CUSTODY,
    "custody_record_invalid": _CUSTODY,
    "custody_follow_refused": _CUSTODY,
    "custody_membership_changed": _CUSTODY,
    "custody_bytes_changed": _CUSTODY,
    "custody_identity_changed": _CUSTODY,
    "held_root_unpinned": _CUSTODY,
    "held_fd_unretained": _CUSTODY,
    "nofollow_open_failed": _CUSTODY,
    "expected_sha256_mismatch": _CUSTODY,
    "expected_size_mismatch": _CUSTODY,
    "expected_filename_tag_inconsistent": _INGRESS,
    "resource_admission_absent": _STREAM,
    "resource_grant_absent": _STREAM,
    "resource_ceilings_absent": _STREAM,
    "resource_ceiling_above_hard_cap": _STREAM,
    "resource_ceiling_exceeded": _STREAM,
    "resource_clock_invalid": _STREAM,
    "resource_time_exceeded": _STREAM,
    "resource_depth_exceeded": _STREAM,
    "compression_capability_absent": _STREAM,
    "compression_method_unsupported": _STREAM,
    "compression_external_tool_refused": _STREAM,
    "codec_capability_unpinned": _STREAM,
    "codec_memory_policy_absent": _STREAM,
    "compressed_stream_malformed": _STREAM,
    "runtime_zlib_module_absent": _STREAM,
    "runtime_lzma_module_absent": _STREAM,
    "runtime_zstd_module_absent": _STREAM,
    "zip_method_unsupported": _ARCHIVE,
    "truncated_zip_header": _ARCHIVE,
    "zip64_extra_absent": _ARCHIVE,
    "zip_disk_count": _ARCHIVE,
    "zip_central_range": _ARCHIVE,
    "zip_central_local_mismatch": _ARCHIVE,
    "zip_member_overlaps_central": _ARCHIVE,
    "zip_descriptor_missing": _ARCHIVE,
    "zip_descriptor_mismatch": _ARCHIVE,
    "zip_stored_size_mismatch": _ARCHIVE,
    "zip_inflated_size_mismatch": _ARCHIVE,
    "zip_crc_mismatch": _ARCHIVE,
    "zip_encryption_unsupported": _ARCHIVE,
    "zip_flag_unsupported": _ARCHIVE,
    "zip_extra_unsupported": _ARCHIVE,
    "zip_creator_unsupported": _ARCHIVE,
    "zip_member_type_unsupported": _ARCHIVE,
    "zip_member_range_overlap": _ARCHIVE,
    "truncated_ar_header": _ARCHIVE,
    "truncated_ar_member": _ARCHIVE,
    "ar_padding_invalid": _ARCHIVE,
    "ar_padding_missing": _ARCHIVE,
    "debian_member_missing": _ARCHIVE,
    "debian_member_order": _ARCHIVE,
    "debian_member_unexpected": _ARCHIVE,
    "debian_binary_version_unsupported": _ARCHIVE,
    "truncated_tar_header": _ARCHIVE,
    "truncated_tar_member": _ARCHIVE,
    "tar_checksum_mismatch": _ARCHIVE,
    "tar_magic_unsupported": _ARCHIVE,
    "tar_type_unsupported": _ARCHIVE,
    "tar_header_fields_unsupported": _ARCHIVE,
    "tar_terminator_absent": _ARCHIVE,
    "tar_trailing_bytes": _ARCHIVE,
    "tar_padding_invalid": _ARCHIVE,
    "tar_numeric_unsupported": _ARCHIVE,
    "tar_pending_extension": _ARCHIVE,
    "tar_pax_malformed": _ARCHIVE,
    "pax_key_unsupported": _ARCHIVE,
    "path_escape": _ARCHIVE,
    "absolute_member_path": _ARCHIVE,
    "empty_member_name": _ARCHIVE,
    "nul_member_name": _ARCHIVE,
    "member_name_undecodable": _ARCHIVE,
    "duplicate_member": _ARCHIVE,
    "casefold_collision": _ARCHIVE,
    "symlink_escape": _ARCHIVE,
    "symlink_target_undecodable": _ARCHIVE,
    "hardlink_escape": _ARCHIVE,
    "metadata_raw_pin_mismatch": _META,
    "metadata_header_malformed": _META,
    "metadata_version_absent": _META,
    "metadata_version_unsupported": _META,
    "metadata_not_utf8": _META,
    "metadata_member_above_cap": _META,
    "metadata_member_not_regular": _META,
    "metadata_name_version_missing": _META,
    "metadata_filename_name_mismatch": _META,
    "metadata_filename_version_mismatch": _META,
    "wheel_metadata_member_missing": _META,
    "wheel_version_unsupported": _META,
    "wheel_purelib_invalid": _META,
    "wheel_tag_filename_mismatch": _META,
    "wheel_build_tag": _META,
    "dist_info_name_mismatch": _META,
    "record_row_shape": _META,
    "record_hash_missing": _META,
    "record_path_missing_member": _META,
    "record_non_regular_member": _META,
    "record_digest_mismatch": _META,
    "record_size_mismatch": _META,
    "record_coverage_gap": _META,
    "unsupported_record_digest": _META,
    "debian_control_malformed": _META,
    "debian_control_field_missing": _META,
    "debian_control_bill_mismatch": _META,
    "duplicate_member_provenance": _AGG,
    "duplicate_member_not_reached": _AGG,
    "member_provenance_incomplete": _AGG,
    "snapshot_member_key_drift": _AGG,
    "snapshot_key_drift": _AGG,
    "rootfs_link_unproven": _AGG,
    "codec_capability_unsupported": _STREAM,
    "symlink_recipe_unbound": _ARCHIVE,
}


def pack(status, cause, observation=None, detail=None, stage=None):
    if stage is None:
        stage = CAUSE_STAGE.get(cause)
    return {
        "schema": SCAN_RESULT_SCHEMA,
        "status": status,
        "cause": cause,
        "stage": stage,
        "detail": detail,
        "publisher_proof": False,
        "compatibility_approved": False,
        "execution_performed": False,
        "members_executed": False,
        "resource_permission_actual": "NOT_PROVEN",
        "observation": observation,
    }


def causal_refusal(primary, cause, meter, stage=None, failure_detail=None,
                   exception_class=None, cleanup_causes=None):
    """One causal successor: no earlier detail or terminal cause is replaced.

    The complete previous marker remains an acyclic borrowed graph. A marker is
    evidence of the actual owner failure, not intended-negative acceptance.
    Producer allocation admission and host-failure capacity remain separate.
    """
    primary=primary if isinstance(primary,dict) else pack('REFUSED',cause)
    result=pack('REFUSED',cause,detail=primary.get('detail'),
        stage=stage or meter.get('reached_stage') or 'AUTHENTICATED_INGRESS')
    result['phase_trace']=meter.get('phase_trace',[])
    marker={'primary_status':primary.get('status'),'primary_cause':primary.get('cause'),
        'primary_stage':primary.get('stage'),'primary_detail':primary.get('detail'),
        'failure_cause':cause,'failure_detail':failure_detail,
        'prior_failure':primary.get('terminal_failure'),
        'close_uncertainties':meter.get('fd_close_uncertainties',[]),
        'causal_detail':'PRESERVED_COMPLETE','scope':'same-public-owner.v1'}
    if exception_class is not None: marker['exception_class']=exception_class
    if cleanup_causes is not None: marker['cleanup_causes']=cleanup_causes
    result['terminal_failure']=marker
    meter['pending_terminal_result']=result
    return result


def exception_detail(exc):
    """Preserve host/native exception facts at their first catching consumer.

    Traceback objects stay physically owned by the selected allocator until
    Python actually retires them. The public evidence keeps all reached frame
    coordinates and exception chaining; it never projects/truncates a message.
    """
    seen=set()
    def filename(value):
        return {'type':'bytes','hex':value.hex()} if isinstance(value,bytes) else value
    def visit(error):
        if error is None: return None
        if id(error) in seen:
            return {'cycle_to_exception_identity':str(id(error))}
        seen.add(id(error))
        frames=[]; frame=error.__traceback__
        while frame is not None:
            frames.append({'filename':frame.tb_frame.f_code.co_filename,
                'function':frame.tb_frame.f_code.co_name,'line':frame.tb_lineno,
                'instruction':frame.tb_lasti})
            frame=frame.tb_next
        return {'exception_class':type(error).__module__+'.'+type(error).__qualname__,
            'exception_message':str(error),'exception_errno':getattr(error,'errno',None),
            'exception_filename':filename(getattr(error,'filename',None)),
            'exception_filename2':filename(getattr(error,'filename2',None)),
            'exception_notes':getattr(error,'__notes__',None),'traceback':frames,
            'suppress_context':error.__suppress_context__,
            'explicit_cause':visit(error.__cause__),
            'implicit_context':visit(error.__context__)}
    return visit(exc)


def source_exception_detail(meter,exc):
    """First accept the exact raw graph, then form its fallible typed view."""
    from tools.native_support import retain_source_origin
    retain_source_origin(meter,exc)
    try:return exception_detail(exc)
    except BaseException as recorder:
        retain_source_origin(meter,recorder)
        raise recorder from exc
