/* NEW inert actual SAME original caller call-site/storage/consumer attempt.
 * Compilers/imports/native/runtime NEVER run by this author.
 * External original Root enrollment and aggregate debit provider must be
 * independently supplied/pinned. No fake matcher or standalone entrypoint.
 */
#include "publisher_original_caller.h"
#include <unistd.h>
#include <string.h>
#include <stdint.h>

typedef struct {
    FridayPublisherOwnedRun run;
    FridayPublisherBindings original,selected;
    FridayPublisherCallerResult result;
    uint64_t reads,output,hash,allocation;
    pid_t pid;
    int attempted;
} FridayPublisherOriginalCaller;
/* Actual prospective OWNED storage inside the SAME original Root process.
 * No constructor/late calloc after a failure, thread/service or new owner.
 * Never reused automatically while complete originals remain held. */
static _Thread_local FridayPublisherOriginalCaller caller_storage;
/* Actual RootPerform calls this on BOTH outcomes after full original reader.
 * Move every OWNED packet/7-root/28-root/close-list/error reference without a
 * destructor, allocation, Source execution or optimistic final status. */
int FridayPublisherCallerMoveToRoot(FridayPublisherCallerResult *destination) {
    FridayPublisherOriginalCaller *c=&caller_storage;
    if(!destination||!c->attempted||c->pid!=getpid()||destination->state!=FRIDAY_CALLER_UNSTARTED||
       (!c->result.full_packet&&!c->result.failure.received&&c->result.state==FRIDAY_CALLER_UNSTARTED))return -1;
    *destination=c->result;
    memset(&c->result,0,sizeof(c->result));
    /* No repeat of CallOriginal; original/selected structs are non-owning
     * copies of the preowned binding refs. Run has already moved/released. */
    return 0;
}

int FridayPublisherFailureBankNext(const FridayPublisherFailureHandback *f,
    Py_ssize_t *position,FridayPublisherFailureBankView *view) {
    if(!f||!f->received||!position||!view||*position<0)return -1;
    PyObject *banks=f->roots[1],*key=NULL,*bank=NULL;
    if(!banks)return 0; /* exact allocation-before-banks failure */
    if(!PyDict_CheckExact(banks))return -1;
    if(!PyDict_Next(banks,position,&key,&bank))return 0;
    if(!PyUnicode_CheckExact(key)||!PyDict_CheckExact(bank))return -1;
    view->key=key;view->full_raw_or_none=PyDict_GetItemString(bank,"raw");
    view->full_parts=PyDict_GetItemString(bank,"parts");
    view->actual_aliases_or_null=PyDict_GetItemString(bank,"aliases");
    view->actual_width=PyDict_GetItemString(bank,"bytes");
    view->sealed=PyDict_GetItemString(bank,"sealed");
    if(!view->full_raw_or_none||!view->full_parts||!PyList_CheckExact(view->full_parts)||
       !view->actual_width||!PyLong_CheckExact(view->actual_width)||
       (view->sealed!=Py_True&&view->sealed!=Py_False)||
       (view->full_raw_or_none!=Py_None&&!PyBytes_CheckExact(view->full_raw_or_none)))return -1;
    return 1;
}

static int original_debit(void *opaque,uint64_t reads,uint64_t output,
                          uint64_t hash,uint64_t allocation) {
    FridayPublisherOriginalCaller *c=opaque;
    if(c!=&caller_storage||c->pid!=getpid()||!c->original.before||
       reads>UINT64_MAX-c->reads||output>UINT64_MAX-c->output||
       hash>UINT64_MAX-c->hash||allocation>UINT64_MAX-c->allocation) {
        /* Pure structural failure precedes a provider debit: no new Python
         * exception allocated here. Python-callable bridges use the exact
         * original Root preowned refusal if no original error is pending. */
        if(c==&caller_storage&&!PyErr_Occurred()&&c->original.preowned_refusal_type&&c->original.preowned_refusal_value)
            PyErr_SetObject(c->original.preowned_refusal_type,c->original.preowned_refusal_value);
        return -1;
    }
    /* Not a new grant/substitute ledger: original whole-pool provider is called
     * BEFORE effect; no cap increase/refund, independent private counter only.
     * Missing real provider is a G1 conflict, never filled with a True stub. */
    if(c->original.before(c->original.existing_envelope,reads,output,hash,allocation)<0)return -1;
    c->reads+=reads;c->output+=output;c->hash+=hash;c->allocation+=allocation;
    return 0;
}


/* SOL104 READ ONLY actual registered Run, including its retired/handback
 * native prefix. No context() re-entry, new binding, factory or Source call.
 * The private address selects the EXACT original; full data are consumed in
 * Root's native node, not accepted on this pointer or capsule name alone. */
int FridayPublisherCallerContextFull(PyObject *value,
    const FridayPublisherOwnedRun **out) {
    FridayPublisherOriginalCaller *c=&caller_storage;
    if(!out||!c->attempted||c->pid!=getpid()||!c->run.started||
       c->run.owner_pid!=getpid()||PyErr_Occurred()||
       !PyCapsule_IsValid(value,"friday.publisher.existing-root-owned-run.v1"))return 0;
    void *p=PyCapsule_GetPointer(value,"friday.publisher.existing-root-owned-run.v1");
    if(PyErr_Occurred())return -1;
    if(p!=&c->run)return 0;
    if(PyCapsule_GetDestructor(value)!=NULL||PyErr_Occurred())return -1;
    if(PyCapsule_GetContext(value)!=NULL||PyErr_Occurred())return -1;
    *out=&c->run;return 1;
}

int FridayPublisherCallerPacketBytes(PyObject *packet,int final_phase) {
    if(!PyTuple_CheckExact(packet)||PyTuple_Size(packet)!=13)return -1;
    PyObject *status=PyTuple_GetItem(packet,11),*banks=PyTuple_GetItem(packet,5);
    PyObject *roots=PyTuple_GetItem(packet,6),*closes=PyTuple_GetItem(packet,7);
    if(!PyTuple_CheckExact(status)||PyTuple_Size(status)!=2||
       !PyLong_CheckExact(PyTuple_GetItem(status,0))||
       PyTuple_GetItem(status,1)!=(final_phase?Py_True:Py_False)||
       !PyTuple_CheckExact(roots)||PyTuple_Size(roots)!=FRIDAY_PUBLISHER_RUN_ROOTS||
       !PyTuple_CheckExact(banks)||!PyTuple_CheckExact(closes)||
       PyTuple_GetItem(roots,27)!=PyTuple_GetItem(packet,1)||
       PyTuple_GetItem(roots,3)!=PyTuple_GetItem(packet,2)||
       PyTuple_GetItem(roots,4)!=PyTuple_GetItem(packet,3)||
       PyTuple_GetItem(roots,5)!=PyTuple_GetItem(packet,4))return -1;
    for(Py_ssize_t i=0;i<PyTuple_Size(banks);i++) {
        PyObject *row=PyTuple_GetItem(banks,i);
        if(!PyTuple_CheckExact(row)||PyTuple_Size(row)!=4||
           !PyUnicode_CheckExact(PyTuple_GetItem(row,0)))return -1;
        PyObject *raw=PyTuple_GetItem(row,1),*aliases=PyTuple_GetItem(row,2),*parts=PyTuple_GetItem(row,3);
        if(!PyBytes_CheckExact(raw)||!PyTuple_CheckExact(aliases)||!PyTuple_CheckExact(parts))return -1;
        Py_ssize_t n=PyBytes_Size(raw),at=0;
        const char *physical=PyBytes_AsString(raw);
        if(n<0||!physical)return -1;
        for(Py_ssize_t j=0;j<PyTuple_Size(parts);j++) {
            PyObject *part=PyTuple_GetItem(parts,j);
            if(!PyBytes_CheckExact(part))return -1;
            Py_ssize_t width=PyBytes_Size(part);
            if(width<0||width>n-at||memcmp(physical+at,PyBytes_AsString(part),(size_t)width))return -1;
            at+=width;
        }
        if(at!=n)return -1;
        at=0;
        for(Py_ssize_t j=0;j<PyTuple_Size(aliases);j++) {
            PyObject *alias=PyTuple_GetItem(aliases,j);
            if(!PyTuple_CheckExact(alias)||PyTuple_Size(alias)!=3)return -1;
            PyObject *value=PyTuple_GetItem(alias,0),*cut=PyTuple_GetItem(alias,1),*offset=PyTuple_GetItem(alias,2);
            if(!PyBytes_CheckExact(cut)||!PyLong_CheckExact(offset)||
               !(PyBytes_CheckExact(value)||PyByteArray_CheckExact(value)))return -1;
            /* Offset was made by native append from bounded bank size. These
             * exact immutable ints are prevalidated before Release. No Source
             * __index__/conversion/callback or error allocation after it. */
            /* final_phase requires the two actual guards on this same tuple;
             * immutable offset objects cannot change during callback-free
             * Release/readback. Avoid even a potentially failing integer
             * conversion after retirement. It is NOT a standalone grant. */
            Py_ssize_t start=final_phase?at:PyLong_AsSsize_t(offset),width=PyBytes_Size(cut);
            if(!final_phase&&start==-1&&PyErr_Occurred())return -1;
            Py_ssize_t actual=PyBytes_CheckExact(value)?PyBytes_Size(value):PyByteArray_Size(value);
            const char *body=PyBytes_CheckExact(value)?PyBytes_AsString(value):PyByteArray_AsString(value);
            if(start!=at||width<0||width>n-at||actual!=width||!body||
               memcmp(body,PyBytes_AsString(cut),(size_t)width)||
               memcmp(physical+at,PyBytes_AsString(cut),(size_t)width))return -1;
            at+=width;
        }
        if(at!=n)return -1;
    }
    return 0;
}

int FridayPublisherCallerCallOriginal(const FridayPublisherBindings *original,
    PyObject *entry,PyObject *args,const FridayPublisherCallerResult **out) {
    FridayPublisherOriginalCaller *c=&caller_storage;
    /* Invalid native API use must not allocate/overwrite an exception, reset
     * the one-shot owner or overwrite a previous accepted/failed run. No
     * Source code or cleanup is called merely to report such a refusal. */
    if(!out)return -1;
    if(c->attempted){*out=&c->result;return -1;}
    *out=&c->result;c->attempted=1;c->pid=getpid();
    /* Establish actual first-call custody before validating the input or
     * invoking the original prospective debit. Static storage already owns
     * this failure slot; NULL denotes a genuinely absent supplied root.
     * Exact reference increments allocate no new Python value or container.
     * This is not admission of the static storage cost or a new allowance:
     * a missing/refusing original provider leaves this contour NOT admitted. */
    if(original){c->original=*original;c->selected=*original;}
    PyObject *before[7]={original?original->root_fact:NULL,
        original?original->qualification:NULL,
        original?original->held_root_tool_preimage:NULL,
        original?original->final_fd_row:NULL,
        original?original->final_fd_credit:NULL,entry,args};
    for(int i=0;i<7;i++)c->result.before_entry_roots[i]=Py_XNewRef(before[i]);
    /* Preserve an already pending exact triple, never normalize it or invoke
     * Source with it set. Structural refusals without a Python error are
     * represented by the preowned native discriminant, not a late exception. */
    if(PyErr_Occurred())c->result.entry_fault=FRIDAY_CALLER_ENTRY_PENDING_ERROR;
    else if(!original)c->result.entry_fault=FRIDAY_CALLER_ENTRY_NO_BINDINGS;
    else if(!entry||!args)c->result.entry_fault=FRIDAY_CALLER_ENTRY_NO_ENTRY_ARGS;
    else if(!original->before||!original->current_enrollment_matches||
            !original->existing_envelope)
        c->result.entry_fault=FRIDAY_CALLER_ENTRY_NO_SUPPLIER;
    else if(!original->root_fact||!original->qualification||
            !original->held_root_tool_preimage||!original->final_fd_row||
            !original->final_fd_credit)
        c->result.entry_fault=FRIDAY_CALLER_ENTRY_NO_OWNED_ROOTS;
    if(c->result.entry_fault!=FRIDAY_CALLER_ENTRY_OK)goto entry_failure;
    c->selected.already_owned_run=&c->run;
    c->selected.before=original_debit;c->selected.existing_envelope=c;
    /* Storage including FAILURE cells is charged prospectively in the SAME
     * whole pool. If debit fails, no Source has run; exact triple is captured
     * into this existing static owner cell without allocation/normalization. */
    c->result.initial_debit_attempted=1;
    if(original_debit(c,0,0,0,sizeof(*c))<0)goto entry_failure;
    FridayPublisherOwnedRun *retained=NULL;
    c->result.owned_invoke_attempted=1;
    PyObject *packet=FridayPublisherOwnedInvoke(&c->selected,entry,args,&retained,
                                               &c->result.invoke_refusal);
    if(!packet) {
        PyErr_Fetch(&c->result.original_call_error_type,&c->result.original_call_error_value,
                    &c->result.original_call_error_tb);
        if(retained) {
            c->result.failure_handback_attempted=1;
            if(FridayPublisherOwnedHandback(&retained,&c->result.failure)==0) {
                c->result.failure_handback_confirmed=1;
                c->result.run_references_transferred=1;
            }
            c->result.untransferred_run=retained;
            /* All uncertain original owners stay in this prospective storage
             * if handback refuses. Actual Root receives that distinction too;
             * no second Invoke/Handback/Release/syscall attempt. */
        }
        c->result.state=FRIDAY_CALLER_ERROR_HELD_STOP_UNCONFIRMED;
        return 0;
    }
    c->result.full_packet=packet; /* Own returned strong ref before readback. */
    c->result.run_references_transferred=1;
    /* All read bytes were charged in prepare_caller_packet before retirement.
     * This actual consumer uses only public owned C data after context clear.
     * A refusal keeps packet/full originals; no late Python exception, Source
     * callback, new endpoint, or optimistic original caller alias end. */
    if(c->result.invoke_refusal!=FRIDAY_INVOKE_PREFLIGHT_OK||
       FridayPublisherCallerPacketBytes(packet,1)<0) {
        c->result.state=FRIDAY_CALLER_PACKET_READBACK_STOP_UNCONFIRMED;return 0;
    }
    c->result.full_packet_readback=1;c->result.state=FRIDAY_CALLER_PACKET_HELD;
    /* Run reference phase ended, NOT the last caller alias. Actual end/storage
     * cleanup is a separate original role obligation; no false green bit. */
    c->result.caller_aliases_retired=0;
    return 0;
entry_failure:
    PyErr_Fetch(&c->result.original_call_error_type,&c->result.original_call_error_value,
                &c->result.original_call_error_tb);
    c->result.state=FRIDAY_CALLER_ERROR_HELD_STOP_UNCONFIRMED;return 0;
}
