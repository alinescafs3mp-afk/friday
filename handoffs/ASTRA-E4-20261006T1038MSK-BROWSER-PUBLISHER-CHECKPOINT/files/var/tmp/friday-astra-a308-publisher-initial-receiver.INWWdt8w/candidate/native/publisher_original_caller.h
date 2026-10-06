/* NEW inert implementation of SAME original Publisher caller role.
 * No issuer, new main, service, private heap, or reconstructed external code.
 * Protected native enrollment/provider/whole-pool admission remain REQUIRED.
 */
#ifndef FRIDAY_PUBLISHER_ORIGINAL_CALLER_H
#define FRIDAY_PUBLISHER_ORIGINAL_CALLER_H
#include "publisher_owned_custody.h"
#define FRIDAY_PUBLISHER_RUN_ROOTS 32
#define FRIDAY_PUBLISHER_CALLER_PACKET_SCHEMA "friday.a268.actual-native-caller-full-bank-transfer.v2"
typedef struct {
    const char *phase;
    int saved,syscall_attempted,syscall_rc,syscall_errno;
    uint64_t actual_written;
} FridayPublisherFailureScalars;
typedef struct {
    /* Full actual OWNED references, including partial banks/parts/aliases,
     * exact error triples, Source return, original document and FD rows.
     * This is ownership MOVE, not hashes, reconstructed objects or a Run ptr. */
    PyObject *roots[FRIDAY_PUBLISHER_RUN_ROOTS];
    PyObject *caller_packet; /* only the False skeleton, never private True */
    NativeCloseRecord *close_records;
    FridayPublisherFailureScalars prefix,after_document;
    uint64_t close_count,charged_allocation,bank_total;
    pid_t owner_pid;
    int end_attempted,final_close_attempted,final_close_confirmed;
    int pending_close_rc,pending_close_errno,pending_close_published;
    int received,ownership_retired;
} FridayPublisherFailureHandback;
typedef enum {
    FRIDAY_CALLER_UNSTARTED=0,
    FRIDAY_CALLER_PACKET_HELD=1,
    FRIDAY_CALLER_ERROR_HELD_STOP_UNCONFIRMED=2,
    FRIDAY_CALLER_PACKET_READBACK_STOP_UNCONFIRMED=3
} FridayPublisherCallerState;
typedef enum {
    FRIDAY_CALLER_ENTRY_OK=0,
    FRIDAY_CALLER_ENTRY_PENDING_ERROR=1,
    FRIDAY_CALLER_ENTRY_NO_BINDINGS=2,
    FRIDAY_CALLER_ENTRY_NO_ENTRY_ARGS=3,
    FRIDAY_CALLER_ENTRY_NO_SUPPLIER=4,
    FRIDAY_CALLER_ENTRY_NO_OWNED_ROOTS=5
} FridayPublisherCallerEntryFault;
typedef struct {
    FridayPublisherCallerState state;
    /* Preowned native scalars, NOT a fabricated Python exception or evidence
     * of final retirement. The full supplied roots and any original triple
     * below remain owned even when initial admission/debit was unavailable. */
    FridayPublisherCallerEntryFault entry_fault;
    FridayPublisherInvokeRefusal invoke_refusal;
    int initial_debit_attempted,owned_invoke_attempted;
    PyObject *full_packet;  /* actual tuple13, final native phase only */
    FridayPublisherFailureHandback failure;
    PyObject *original_call_error_type,*original_call_error_value,*original_call_error_tb;
    PyObject *before_entry_roots[7]; /* actual original bindings/entry/args */
    /* If handback refuses, the actual preowned Run is STILL live. Expose that
     * exact retained obligation to Root, never confuse it with an allocation-
     * before-Run refusal. This pointer is NOT a transfer/readback receipt. */
    FridayPublisherOwnedRun *untransferred_run;
    int failure_handback_attempted,failure_handback_confirmed;
    int run_references_transferred,full_packet_readback,caller_aliases_retired;
} FridayPublisherCallerResult;
/* Exact original Root owns prospective storage and its finite resource debit.
 * Implemented CallOriginal uses its own static thread-local zeroed storage,
 * not late heap allocation. The supplied original bindings are mandatory
 * protected inputs, NEVER Source-created authority or default callbacks.
 * Their actual provider implementation is absent from supplied49/134. This
 * API does not claim to close that admission/supplier conflict.
 * Return 0 means a full current result is HELD, not successful execution.
 * A first-call structural refusal has ERROR_HELD state and entry_fault, with
 * every supplied original root and any pre-existing Python error retained.
 * It does not allocate an exception before the original prospective debit.
 * Return -1 is only a missing C output slot or a repeated call. A repeat
 * returns the previous held result without changing it; it is NOT a new
 * success, retry, debit, Source invocation or cleanup. Pending Python error
 * state is untouched on those invalid C-API calls.
 * Borrowed views/result may not be cleared or reused as final-end evidence. */
int FridayPublisherCallerCallOriginal(const FridayPublisherBindings *,
    PyObject *entry,PyObject *args,const FridayPublisherCallerResult **);
/* Move every Run reference/close record to the prospectively owned same
 * original caller error cell. Allocation/callback/syscall-free, once only. */
int FridayPublisherOwnedHandback(FridayPublisherOwnedRun **,
    FridayPublisherFailureHandback *);
/* Exact immutable raw and parts/aliases byte reader. No allocator, user
 * callback, parser, digest projection or context() after native Release.
 * Returns -1 without creating a secondary Python exception. */
int FridayPublisherCallerPacketBytes(PyObject *,int final_phase);
typedef struct {
    PyObject *key,*full_raw_or_none,*full_parts,*actual_aliases_or_null;
    PyObject *actual_width,*sealed;
} FridayPublisherFailureBankView;
typedef struct {
    PyObject *actual_row;
    PyObject **owner_slot; /* exact internal strong slot for one native MOVE */
    int actual_called,syscall_rc,syscall_errno,publication_confirmed;
} FridayPublisherFailureCloseView;
/* Full borrowed views remain owned by SAME static caller handback. No context,
 * encoding/allocation or uncertain syscall; no final end inferred from views.
 * BankNext: 1 actual bank, 0 end, -1 inconsistent cell (keep originals).
 * CloseAt: 1 actual record, 0 end, -1 inconsistent cell. */
int FridayPublisherFailureBankNext(const FridayPublisherFailureHandback *,
    Py_ssize_t *,FridayPublisherFailureBankView *);
int FridayPublisherFailureCloseAt(const FridayPublisherFailureHandback *,
    uint64_t,FridayPublisherFailureCloseView *);
/* Only after every actual row strong slot moved to the SAME command receipt.
 * Frees native cells once; refuses any still-owned row or count mismatch.
 * No Python destructor, factory, callback, syscall or claimed IO/RAM refund. */
int FridayPublisherFailureRetireMovedCloseRecords(FridayPublisherFailureHandback *);
/* Native cursor over the same actual cells, O(count), exact end/count check.
 * View remains borrowed; owner_slot names the one existing strong row owner. */
int FridayPublisherFailureCloseNext(const FridayPublisherFailureHandback *,
    NativeCloseRecord **,uint64_t *,FridayPublisherFailureCloseView *);
/* Exact original native context FULL read-only view; not a public grant. */
int FridayPublisherCallerContextFull(PyObject *,const FridayPublisherOwnedRun **);
#endif
