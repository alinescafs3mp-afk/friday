"""Call-local body roots: originals survive every error until actual custody.

No original exception/traceback/frame is cleared for a public failure. Public
failure objects are separate, bounded metadata; private custody is never wire
approval. Whole native/body acceptance and finite outside-owner completion
remain distinct requirements.
"""
import contextvars
import functools
from contract import ContractError
from resource_meter import checkpoint, reserve_allocation, current

_SCOPES = contextvars.ContextVar('publisher_body_scopes', default=None)

def register(lease):
    scope = _SCOPES.get()
    if scope is None:
        raise ContractError('body_scope_absent')
    scope['leases'].append(lease)

def temporary_slot(meter, count):
    scope = _SCOPES.get()
    if scope is None:
        raise ContractError('body_scope_absent')
    reserve_allocation(1024)
    cell = {'meter': meter, 'count': count, 'charged': False,
            'body': None, 'retired': False, 'attempted': False, 'producer': scope['producer']}
    # Establish the actual cell before charge or body-producing effects.
    scope['temporary'].append(cell)
    meter.charge_slots(count)
    cell['charged'] = True
    return cell

def retire_temporary(cell):
    if cell['retired']:
        return True
    if cell['attempted']:
        return False
    cell['attempted'] = True
    if cell['charged']:
        if cell['meter'].release_slots(cell['count']) is not True:
            return False
        cell['charged'] = False
    cell['body'] = None
    cell['retired'] = True
    return True

def begin_producer(lease):
    scope = _SCOPES.get()
    if scope is None:
        raise ContractError('body_scope_absent')
    previous = scope['producer']
    scope['producer'] = lease
    return scope, previous

def end_producer(scope, previous):
    scope['producer'] = previous

def retire_producer(scope, lease):
    # The actual producer frame returned normally, and the caller now owns the
    # returned full bytes under its already-charged lease. Failed producer
    # frames never reach this transition, so partial assemblies stay charged.
    for cell in scope['temporary']:
        if cell['producer'] is lease and retire_temporary(cell) is not True:
            return False
    return True

def _metadata_only(value):
    todo = [value]
    count = 0
    while todo:
        item = todo.pop()
        count += 1
        if count > 1000000:
            raise ContractError('count')
        if count % 512 == 0:
            checkpoint()
        if type(item) is dict:
            if any(type(k) is not str for k in item):
                raise ContractError('body_alias_escape')
            reserve_allocation(len(item) * 8)
            todo.extend(item.values())
        elif type(item) in (list, tuple):
            reserve_allocation(len(item) * 8)
            todo.extend(item)
        elif type(item) not in (str, int, bool, type(None)):
            raise ContractError('body_alias_escape')

def _public_failure(exc):
    safe = 'abcdefghijklmnopqrstuvwxyz0123456789_'
    if type(exc) is ContractError:
        cause = exc.cause if type(exc.cause) is str and 1 <= len(exc.cause) <= 128 and all(c in safe for c in exc.cause) else 'body_consumer_error'
        stage = exc.stage if type(exc.stage) is str and 1 <= len(exc.stage) <= 128 and all(c in safe for c in exc.stage) else None
        return ContractError(cause, stage)
    if type(exc) is KeyboardInterrupt:
        return KeyboardInterrupt()
    if type(exc) is SystemExit:
        return SystemExit(1)
    return ContractError('body_consumer_error')

def _retire_scope(scope):
    complete = True
    for lease in scope['leases']:
        try:
            if lease.retire() is not True:
                scope['cleanup'].append(ContractError('body_cleanup'))
                complete = False
        except BaseException as secondary:
            scope['cleanup'].append(secondary)
            complete = False
    for cell in scope['temporary']:
        try:
            if retire_temporary(cell) is not True:
                scope['cleanup'].append(ContractError('body_cleanup'))
                complete = False
        except BaseException as secondary:
            scope['cleanup'].append(secondary)
            complete = False
    return complete

def document_scope(function):
    @functools.wraps(function)
    def consume(*args, **kwargs):
        reserve_allocation(16384)
        scope = {'primary': None, 'traceback': None, 'leases': [],
                 'temporary': [], 'cleanup': [], 'body_roots': None, 'producer': None,
                 'entered': False, 'returned': False, 'retired': False,
                 'context_transition': None}
        whole = current()
        if whole is not None:
            whole.body_scopes.append(scope)
        token = None
        failure = None
        result = None
        try:
            reserve_allocation(4096)
            transition={'sol070_context_transition':True,'token':None,'var':_SCOPES,
                'before':contextvars.copy_context(),'set_value':scope,
                'reset_attempted':False,'reset_confirmed':False,'reset_error':None}
            scope['context_transition']=transition
            token = _SCOPES.set(scope)
            transition['token']=token
            scope['entered'] = True
            result = function(*args, **kwargs)
            _metadata_only(result)
            scope['returned'] = True
        except BaseException as exc:
            # Exact strong roots keep BOTH cause/context branches, original
            # traceback objects, frames, locals and partial buffers transitively.
            # No graph traversal, clear_frames, chain rewiring or body clearing.
            scope['primary'] = exc
            scope['traceback'] = exc.__traceback__
            result = None
            failure = _public_failure(exc)
        finally:
            if token is not None:
                try:
                    transition['reset_attempted']=True
                    _SCOPES.reset(token)
                    transition['reset_confirmed']=True
                except BaseException as secondary:
                    transition['reset_confirmed']=None
                    transition['reset_error']=secondary
                    scope['cleanup'].append(secondary)
                    if failure is None:
                        failure = _public_failure(secondary)
            # A successful consumer frame has gone and returned only metadata.
            # A failed release retains the original body in the lease/cell.
            # Failure frames/bodies stay charged until the actual whole receiver.
            if scope['primary'] is None and not scope['cleanup']:
                scope['retired'] = _retire_scope(scope)
            if scope['cleanup']:
                if whole is not None:
                    whole.cleanup_fault = True
                if failure is None:
                    failure = ContractError('body_cleanup')
            if scope['retired'] and whole is not None:
                whole.discard_body_scope(scope)
        if failure is not None:
            # An unmetered internal call has no module-global/unbounded bucket.
            # Its exact private graph follows the original caller's exception,
            # so the existing outer error receiver sees it rather than a label.
            # This attribute is NOT serialized into the public refusal document.
            failure._private_body_custody = scope
            raise failure from None
        return result
    return consume
