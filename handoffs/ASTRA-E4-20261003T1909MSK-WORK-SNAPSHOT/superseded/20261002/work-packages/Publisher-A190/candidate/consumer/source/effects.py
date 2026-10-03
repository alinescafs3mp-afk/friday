"""Effect capabilities stay unavailable in this source phase."""

from contract import ContractError
from schema_validate import validate_document


def admit_effect(request, schema):
    validate_document(request, schema)
    if request["available"] is not False:
        raise ContractError("effect_available")
    decision = {
        "schema": "friday.lab815.effect-capability.v1",
        "effect": request["effect"],
        "available": False,
        "reason": "SOURCE_PHASE_DENIAL",
        "max_bytes": request["max_bytes"],
        "network_calls": 0,
        "subprocesses": 0,
        "publisher_proof": False,
    }
    validate_document(decision, schema)
    return decision
