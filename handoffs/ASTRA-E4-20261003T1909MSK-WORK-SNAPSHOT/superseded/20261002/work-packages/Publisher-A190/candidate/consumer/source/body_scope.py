"""Body slots last through consumer frames, including parse aliases and errors.

The decorator boundary only returns JSON metadata. A returned body, memoryview,
callback or opaque parser object is a contract failure. No body slot is refunded
by a lease's __exit__ while its consumer frame can still hold aliases.
"""
import contextvars
import functools
import traceback
from contract import ContractError
from resource_meter import checkpoint, reserve_allocation

_SCOPES = contextvars.ContextVar('publisher_body_scopes', default=None)

def register(lease):
    scope = _SCOPES.get()
    if scope is None:
        raise ContractError('body_scope_absent')
    scope.append(lease)

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

def document_scope(function):
    @functools.wraps(function)
    def consume(*args, **kwargs):
        leases = []
        token = _SCOPES.set(leases)
        failure = None
        result = None
        try:
            result = function(*args, **kwargs)
            _metadata_only(result)
        except BaseException as exc:
            # The consumer frame has unwound. Erase every retained traceback
            # frame before clearing body owners and refunding their slots.
            chain = exc
            seen = set()
            while chain is not None and id(chain) not in seen:
                seen.add(id(chain))
                traceback.clear_frames(chain.__traceback__)
                next_error = chain.__cause__ or chain.__context__
                chain.__traceback__ = None
                chain.__cause__ = None
                chain.__context__ = None
                chain = next_error
            # Error objects can themselves retain an input body (for example
            # UnicodeError.object or an arbitrary exception argument). Keep
            # only bounded contract metadata, not the original error object.
            if type(exc) is ContractError:
                cause=exc.cause if type(exc.cause) is str and len(exc.cause)<=128 else 'body_consumer_error'
                stage=exc.stage if type(exc.stage) is str and len(exc.stage)<=128 else None
                failure=ContractError(cause,stage)
            elif type(exc) is KeyboardInterrupt:
                failure=KeyboardInterrupt()
            elif type(exc) is SystemExit:
                failure=SystemExit(1)
            else:
                failure=ContractError('body_consumer_error')
            result=None
        finally:
            _SCOPES.reset(token)
            for lease in leases:
                lease.body = None
            for lease in leases:
                try:
                    if lease.retire() is False and failure is None:
                        failure=ContractError('body_cleanup')
                except BaseException:
                    # Attempt every retirement without replacing the original
                    # failure or retaining the body-owning exception frame.
                    from resource_meter import current
                    if current() is not None: current().cleanup_fault=True
                    if failure is None: failure=ContractError('body_cleanup')
            leases.clear()
        if failure is not None:
            raise failure from None
        return result
    return consume
