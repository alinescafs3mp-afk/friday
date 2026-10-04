"""Root-retention -> independent selection -> exact A128 consumer seam.

This module never creates the selected expected body or the golden. The
independently signed admission selects a full preexisting retention capsule.
The capsule must contain reached full Root terminals and every selected body.
Fresh dynamic PID/time/resource bytes are never retrofitted into an oracle.
"""
from common import Refused,INPUT_MAX,OUTPUT_MAX,exact,parse,sha
from custody import Held
from admission import ALL15


def verify_independently_selected_retention(admission,ordinary,meter):
    pin=admission["root_retention_pin"]
    with Held(pin["path"],pin,meter,INPUT_MAX,True) as held:
        capsule=exact(parse(held.read(INPUT_MAX),meter),(
            "schema","producer_id","selector_id","observer_id","source_manifest_sha256",
            "consumer_manifest_sha256","retained_before_ns","case_id","terminals",
            "performing_bodies","original_all15","effects_granted","own_derived_expected"),"retention_capsule")
    if capsule["schema"]!="friday.a138.independently-selected-root-retention.v1" or capsule["effects_granted"] is not False or capsule["own_derived_expected"] is not False:
        raise Refused("retention_capsule")
    if capsule["source_manifest_sha256"]!=admission["source_manifest_sha256"] or capsule["consumer_manifest_sha256"]!=admission["consumer_manifest_sha256"] or capsule["case_id"]!=ordinary.selected["case_id"]:
        raise Refused("retention_source_case")
    if capsule["selector_id"]!=ordinary.selected["selector_id"] or len({capsule[k] for k in ("producer_id","selector_id","observer_id")})!=3:
        raise Refused("retention_independence")
    if capsule["retained_before_ns"]>=ordinary.selected["prepared_ns"] or capsule["original_all15"]!=list(ALL15):raise Refused("retention_order_full_scope")
    terminals={};body_pins={}
    for row in capsule["terminals"]:
        exact(row,("pin","producer_id","cleanup_tail_pin","delivery_completion_pin"),"root_terminal_selection")
        with Held(row["pin"]["path"],row["pin"],meter,INPUT_MAX,True) as held:
            terminal=parse(held.read(INPUT_MAX),meter)
        if terminal["schema"]!="friday.a138.root-owned-whole-terminal.v1" or terminal["launch"]["actor_id"]!=row["producer_id"] or terminal["source_issued_grant"] is not False:
            raise Refused("retention_actual_Root_terminal")
        launch=terminal["launch"]
        if launch["Root_created"] is not True or launch["actual_parent"]!=terminal["root_fact"] or terminal["qualification"]["root_fact"]!=terminal["root_fact"]:
            raise Refused("retention_owned_launch")
        if terminal["actor_wait_status"] is None or terminal["outer"].get("implicit_io_status")!="OBSERVED" or terminal["outer"]["cleanup_faults"]:
            raise Refused("retention_full_actual_outer_terminal")
        if terminal["outer"]["observer_id"]==row["producer_id"]:raise Refused("retention_outer_independence")
        # This is a connected producer/consumer wire revision. The old
        # terminal-only capsule cannot conceal actual AFTER-terminal close
        # errors, or replace completed ownership with an intention to exit.
        with Held(row["cleanup_tail_pin"]["path"],row["cleanup_tail_pin"],meter,INPUT_MAX,True) as held:
            tail=parse(held.read(INPUT_MAX),meter)
        with Held(row["delivery_completion_pin"]["path"],row["delivery_completion_pin"],meter,INPUT_MAX,True) as held:
            completion=parse(held.read(INPUT_MAX),meter)
        boundary=terminal["actual_owner_cleanup_boundary"]
        if tail["schema"]!="friday.sol053.actual-post-terminal-cleanup-tail.v1" or tail["terminal_pin"]!=row["pin"]:
            raise Refused("retention_actual_cleanup_tail")
        if not tail["terminal_forward_close_confirmed"] or tail["actual_cleanup_faults"] or terminal["errors"] or not boundary["ordinary_close_attempt_completed"]:
            raise Refused("retention_full_actual_cleanup")
        after=tail["post_terminal_observation"]
        if after.get("implicit_io_status")!="OBSERVED" or after["cleanup_faults"] or after["errors"]:
            raise Refused("retention_actual_post_terminal_observation")
        handoff=tail["actual_held_handoff"]
        if handoff["status"]!="ORDINARY_OWNER_CLOSED_DELIVERY_LEASE_HELD" or handoff["same_owner_transfer_validated"] is not True:
            raise Refused("retention_existing_Root_owner_handoff")
        if handoff["same_existing_Root_pid"]!=terminal["root_fact"]["pid"] or handoff["root_start_ticks"]!=terminal["root_fact"]["start_ticks"]:
            raise Refused("retention_existing_Root_owner_identity")
        if completion["schema"]!="friday.sol053.actual-existing-root-delivery-completion.v1" or completion["status"]!="ACTUAL_DELIVERY_FD_CLOSE_CONFIRMED" or completion["ownership_retired"] is not True:
            raise Refused("retention_actual_delivery_retirement")
        if completion["owner_key"]!=handoff["owner_key"] or completion["root_fact"]!=terminal["root_fact"] or completion["terminal_pin"]!=row["pin"] or completion["cleanup_tail_pin"]!=row["cleanup_tail_pin"] or completion["actual_final_close_faults"]:
            raise Refused("retention_complete_delivery_completion_join")
        if completion["caller_must_durably_retain_this_actual_completion"] is not True:
            raise Refused("retention_actual_native_completion_required")
        if tail["full_consumer_output_pin"]!=terminal["full_consumer_output_pin"] or tail["full_five_argument_golden_binding"]!=terminal["full_five_argument_golden_binding"]:
            raise Refused("retention_full_output_five_argument_tail_join")
        if terminal["full_consumer_output_pin"] is None or terminal["full_five_argument_golden_binding"] is None:
            raise Refused("retention_complete_public_output_absent")
        output_pin=terminal["full_consumer_output_pin"]
        with Held(output_pin["path"],output_pin,meter,OUTPUT_MAX,True) as held:
            held.check()
        terminals[row["producer_id"]]=terminal
        for observed in terminal["retained_actual_receipts"]:
            key=(observed["section"],observed["target"])
            if key in body_pins:raise Refused("retention_duplicate_body")
            body_pins[key]=observed["record"]["pin"]
    selected={}
    for row in capsule["performing_bodies"]:
        exact(row,("section","target","observed_pin","expected_pin","expected_producer_id","selected_by"),"retention_selected_body")
        key=(row["section"],row["target"])
        if key in selected or key not in body_pins or row["observed_pin"]!=body_pins[key]:raise Refused("retention_complete_preimage")
        if row["expected_producer_id"]!=ordinary.selected["producer_id"] or row["selected_by"]!=ordinary.selected["selector_id"]:
            raise Refused("retention_expected_independence")
        if row["observed_pin"]["path"]==row["expected_pin"]["path"]:raise Refused("expected_bound_to_itself")
        with Held(row["observed_pin"]["path"],row["observed_pin"],meter,INPUT_MAX,True) as observed, \
             Held(row["expected_pin"]["path"],row["expected_pin"],meter,INPUT_MAX,True) as expected:
            a,b=observed.read(INPUT_MAX),expected.read(INPUT_MAX)
            if a!=b:raise Refused("retention_full_expected_bytes")
            body=parse(a,meter)
        if body["producer_id"] not in terminals:raise Refused("retention_producer_terminal")
        if body["runtime"]["environment"]["process"]["pid"]!=terminals[body["producer_id"]]["launch"]["pid"]:
            raise Refused("retention_actual_pid_join")
        selected[key]=row
    descriptors=ordinary.context["performing_contracts"]
    required={(section,row["target"]) for section in ("materials","operations","closures","snapshot") for row in descriptors[section]}
    if required!=set(selected) or {target for section,target in required if section=="operations"}!=set(ALL15):raise Refused("retention_full_descriptor_census")
    for section in ("materials","operations","closures","snapshot"):
        for descriptor in descriptors[section]:
            row=selected[(section,descriptor["target"])]
            if descriptor["sha256"]!=row["observed_pin"]["sha256"] or descriptor["expected_ref"] is None or descriptor["expected_ref"]["sha256"]!=row["expected_pin"]["sha256"]:
                raise Refused("retention_context_join")
    return {"complete":True,"selected_pin":pin,"publisher_proof":False,"current_grant":False}
