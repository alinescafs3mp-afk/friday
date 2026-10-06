/* Same-process selected Publisher native ownership; never executed by author. */
#define PY_SSIZE_T_CLEAN
#include "publisher_owned_custody.h"
#include "publisher_original_caller.h"
#include "publisher_root_entry.h"
#include <unistd.h>
#include <errno.h>
#include <string.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <limits.h>
#include <stdio.h>

#define CAPSULE_NAME "friday.publisher.existing-root-owned-run.v1"
struct NativeCloseRecord {
    PyObject *row;
    int actual_called, syscall_rc, syscall_errno, publication_confirmed;
    struct NativeCloseRecord *next;
};
static _Thread_local FridayPublisherOwnedRun *active_run;

static int bad(const char *why) {
    /* A provider/conversion error is the original failure, not disposable
     * context for a newly manufactured guard exception. */
    if(!PyErr_Occurred())PyErr_SetString(PyExc_RuntimeError,why);
    return -1;
}
static FridayPublisherOwnedRun *context(PyObject *capsule) {
    FridayPublisherOwnedRun *r=PyCapsule_GetPointer(capsule,CAPSULE_NAME);
    if (!r) return NULL;
    if (r != active_run || r->owner_pid != getpid() || !r->bindings ||
        r->bindings->current_enrollment_matches(r->bindings->root_fact,r->bindings->qualification)!=1||
        PyErr_Occurred()) {
        bad("actual_same_process_enrolled_native_owner"); return NULL;
    }
    return r;
}
static int debit(FridayPublisherOwnedRun *r,uint64_t reads,uint64_t output,
                 uint64_t hash,uint64_t allocation) {
    if (r->charged_allocation>r->bindings->root_ram_remaining ||
        allocation > r->bindings->root_ram_remaining-r->charged_allocation)
        return bad("original_native_parent_prepaid_RAM");
    if (r->bindings->before(r->bindings->existing_envelope,reads,output,hash,allocation) < 0) {
        /* Python-callable methods MUST return NULL with the original error or
         * the native preowned provider refusal, never a fresh SystemError. */
        if(!PyErr_Occurred())FridayPublisherMasterFault(r->bindings->original_master_pool,
            "actual_original_native_provider_refused");
        return -1;
    }
    r->charged_allocation += allocation;
    return 0;
}
static int hold(FridayPublisherOwnedRun *r,const char *key,PyObject *value) {
    if(PyErr_Occurred())return -1;
    if(!r->anchors||!PyDict_CheckExact(r->anchors))
        return bad("actual_native_anchor_dictionary");
    if ((uint64_t)PyDict_GET_SIZE(r->anchors)>=r->bindings->event_limit)
        return bad("original_native_parent_prepaid_anchor_count");
    /* The original debit precedes even temporary key creation for lookup.
     * GetItemString silently suppresses key-allocation/hash/equality errors;
     * the strong-result API preserves the actual failure instead. */
    if (debit(r,0,0,0,512+strlen(key))<0) return -1;
    PyObject *prior=NULL;int found=PyDict_GetItemStringRef(r->anchors,key,&prior);
    if(found<0)return -1;
    if(found) {
        Py_DECREF(prior); /* actual dictionary still owns the original */
        return bad("native_owned_anchor_once");
    }
    /* INCREF performed by PyDict_SetItemString BEFORE Source can detach. */
    return PyDict_SetItemString(r->anchors,key,value);
}
static int save_error_cell(FridayPublisherErrorCell *cell,const char *phase) {
    if(cell->saved)return -1; /* one failure ends this phase, no overwrite */
    cell->saved=1;cell->phase=phase;
    /* Exact original triple, WITHOUT normalizing or diagnostic allocation. */
    PyErr_Fetch(&cell->error_type,&cell->error_value,&cell->error_tb);
    PyErr_Restore(Py_XNewRef(cell->error_type),Py_XNewRef(cell->error_value),Py_XNewRef(cell->error_tb));
    return 0;
}
static PyObject *error_cell_cut(FridayPublisherOwnedRun *r,FridayPublisherErrorCell *cell) {
    /* Preserve the existing read-only fork-shadow export. It neither calls
     * the parent pool nor obtains the SAME-Root provenance introduced here.
     * Its existing cross-process qualification remains separately OPEN. */
    int owning=r->owner_pid==getpid();
    uint64_t serial=owning?FridayPublisherRootErrorCellBegin(r,cell):0;
    if(owning&&!serial)return NULL;
    PyObject *cut=Py_BuildValue("{s:s,s:O,s:O,s:O,s:O,s:i,s:i,s:i,s:i,s:K}",
        "phase",cell->phase?cell->phase:"NOT_ATTEMPTED",
        "original_type",cell->error_type?cell->error_type:Py_None,
        "original_error",cell->error_value?cell->error_value:Py_None,
        "original_traceback",cell->error_tb?cell->error_tb:Py_None,
        "actual_document",cell->actual_document?cell->actual_document:Py_None,
        "saved",cell->saved,"syscall_attempted",cell->syscall_attempted,
        "syscall_rc",cell->syscall_rc,"syscall_errno",cell->syscall_errno,
        "actual_written",(unsigned long long)cell->actual_written);
    if(owning&&FridayPublisherRootErrorCellFinish(r,cell,serial,cut)<0) {
        /* Registry already owns every non-NULL actual return and first error. */
        Py_XDECREF(cut);return NULL;
    }
    return cut;
}
static int retain_pending_error(FridayPublisherOwnedRun *r) {
    if(!PyErr_Occurred())return -1;
    FridayPublisherErrorCell *cell=r->end_attempted?&r->after_document_error:&r->prefix_error;
    if(!cell->saved)return save_error_cell(cell,r->end_attempted?"actual-native-tail-error":"actual-native-error");
    /* First exact triple already belongs to a preowned phase cell. Pending
     * later error is not normalized, reconstructed or late-appended. It stays
     * pending for the actual caller's own exact full triple capture. */
    return 0;
}
static PyObject *py_current(PyObject *self,PyObject *ignored) {
    if (!active_run) Py_RETURN_NONE;
    return Py_NewRef(active_run->capsule);
}
static PyObject *py_prefix_envelope(PyObject *self,PyObject *cap) {
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(FridayPublisherRootErrorCellReady(r,&r->prefix_error)<0)return NULL;
    if(debit(r,0,0,0,8192)<0)return NULL;
    PyObject *error=error_cell_cut(r,&r->prefix_error);
    PyObject *cut=error?Py_BuildValue("{s:s,s:l,s:O,s:O,s:O,s:O,s:O}",
        "schema","friday.sol091.actual-before-Source-native-prefix.v1",
        "owner_pid",(long)r->owner_pid,"actual_entry",r->source_entry,
        "actual_args",r->source_args,"held_original_root_image",r->bindings->held_root_tool_preimage,
        "actual_error_cell",error,"original_final_row",r->bindings->final_fd_row):NULL;
    Py_XDECREF(error);return cut;
}
static PyObject *py_after_document_cell(PyObject *self,PyObject *cap) {
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(FridayPublisherRootErrorCellReady(r,&r->after_document_error)<0)return NULL;
    if(!r->after_document_error.saved)Py_RETURN_NONE;
    if(debit(r,0,0,0,8192)<0)return NULL;
    if(r->after_document_error.accepted_cut)return Py_NewRef(r->after_document_error.accepted_cut);
    PyObject *cut=error_cell_cut(r,&r->after_document_error);
    if(!cut)return NULL;
    if(!r->after_document_error.accepted_cut) {
        r->after_document_error.accepted_cut=Py_NewRef(cut);
    } else {
        Py_DECREF(cut);cut=Py_NewRef(r->after_document_error.accepted_cut);
    }
    return cut;
}
static PyObject *py_accept_after_document(PyObject *self,PyObject *args) {
    PyObject *cap,*cut,*snapshot;
    if(!PyArg_ParseTuple(args,"OOO",&cap,&cut,&snapshot))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(!r->after_document_error.saved||cut!=r->after_document_error.accepted_cut) {
        bad("actual_preowned_after_document_cut_identity");return NULL;
    }
    PyObject *receiver=PyDict_GetItemString(r->anchors,"performing-receiver");
    if(!receiver){bad("actual_original_after_document_receiver");return NULL;}
    PyObject *checked=PyObject_CallMethod(receiver,"verify_native_error_snapshot","OO",cut,snapshot);
    if(!checked)return NULL;
    int same=checked==snapshot;Py_DECREF(checked);
    if(!same){bad("actual_after_document_full_reader");return NULL;}
    if(hold(r,"actual-after-document-full-accepted-body",snapshot)<0)return NULL;
    /* Body custody does NOT certify last close, Source retirement or C2. */
    Py_RETURN_NONE;
}
static PyObject *py_get_fact(PyObject *self,PyObject *capsule) {
    FridayPublisherOwnedRun *r=context(capsule);if(!r)return NULL;
    return Py_NewRef(r->bindings->root_fact);
}
static PyObject *py_get_qualification(PyObject *self,PyObject *capsule) {
    FridayPublisherOwnedRun *r=context(capsule);if(!r)return NULL;
    return Py_NewRef(r->bindings->qualification);
}
static PyObject *py_hold(PyObject *self,PyObject *args) {
    PyObject *cap,*value;const char *key;
    if(!PyArg_ParseTuple(args,"OsO",&cap,&key,&value))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r||hold(r,key,value)<0)return NULL;
    Py_RETURN_NONE;
}
static PyObject *py_hold_source_error(PyObject *self,PyObject *const *args,Py_ssize_t nargs) {
    if(nargs!=3||!PyExceptionInstance_Check(args[1])||!PyUnicode_CheckExact(args[2])) {
        bad("actual_Source_error_and_phase_required");return NULL;
    }
    /* Preserve full enrollment/PID checks; this is not a new authority path.
     * Earlier lookup/context-entry failures remain outside this handoff. */
    FridayPublisherOwnedRun *r=context(args[0]);if(!r)return NULL;
    if(r->source_error||r->source_error_phase||r->source_error_tb||r->source_error_record) {
        bad("actual_Source_error_handoff_already_pending");return NULL;
    }
    /* NewRef itself needs no materialization. Actual originals are strongly
     * owned BEFORE serial/key/tuple/debit/dictionary work can fail. */
    r->source_error=Py_NewRef(args[1]);r->source_error_phase=Py_NewRef(args[2]);
    r->source_error_tb=PyException_GetTraceback(args[1]);
    if(PyErr_Occurred())return NULL;
    if(r->serial==UINT64_MAX){bad("actual_Source_error_serial_overflow");return NULL;}
    if(!r->anchors||!PyDict_CheckExact(r->anchors)) {
        bad("actual_Source_error_anchor_dictionary");return NULL;
    }
    Py_ssize_t count=PyDict_GET_SIZE(r->anchors);
    if(count<0||(uint64_t)count>=r->bindings->event_limit||
       (uint64_t)count>(UINT64_MAX-131072)/256) {
        bad("actual_Source_error_anchor_capacity");return NULL;
    }
    /* Prepay actual dictionary walks and potential resize before key/tuple/
     * anchor work. Floors only: selected ABI/implicit whole fit is separate. */
    uint64_t entries=(uint64_t)count+1;
    if(debit(r,entries*256,0,0,131072+entries*128)<0)return NULL;
    char key[96];int n=snprintf(key,sizeof(key),"actual-source-error:%llu",
        (unsigned long long)++r->serial);
    if(n<0||(size_t)n>=sizeof(key)){bad("actual_Source_error_key_bound");return NULL;}
    uint64_t record_serial=FridayPublisherRootErrorRecordBegin(r);
    if(!record_serial) {
        bad("actual_Source_error_record_registration");return NULL;
    } /* existing staged originals survive refusal; no fresh SystemError */
    r->source_error_record=PyTuple_Pack(4,r->source_error_phase,r->source_error,
        (PyObject *)Py_TYPE(r->source_error),r->source_error_tb?r->source_error_tb:Py_None);
    if(FridayPublisherRootErrorRecordFinish(record_serial,r->source_error_record)<0)return NULL;
    if(hold(r,key,r->source_error_record)<0||PyErr_Occurred()) {
        FridayPublisherRootErrorRecordFailure(record_serial);return NULL;
    }
    /* Confirmed dictionary insertion owns the immutable actual record and
     * all its originals BEFORE these staging aliases drop. No tuple mutation,
     * exception normalization, failed-handoff retry or last-error destruction. */
    Py_CLEAR(r->source_error);Py_CLEAR(r->source_error_phase);
    Py_CLEAR(r->source_error_tb);Py_CLEAR(r->source_error_record);
    Py_RETURN_NONE;
}
static PyObject *py_get_anchor(PyObject *self,PyObject *args) {
    PyObject *cap;const char *key;
    if(!PyArg_ParseTuple(args,"Os",&cap,&key))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    PyObject *value=PyDict_GetItemString(r->anchors,key);
    return Py_NewRef(value ? value : Py_None);
}
static PyObject *py_matches(PyObject *self,PyObject *args) {
    PyObject *cap,*value;const char *key;
    if(!PyArg_ParseTuple(args,"OsO",&cap,&key,&value))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    return PyBool_FromLong(PyDict_GetItemString(r->anchors,key)==value);
}
static PyObject *py_before_graph(PyObject *self,PyObject *args) {
    PyObject *cap;unsigned long long roots;
    if(!PyArg_ParseTuple(args,"OK",&cap,&roots))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(roots>262144){bad("native_root_count");return NULL;}
    if(debit(r,0,0,0,roots*128+131072)<0)return NULL;
    /* This is a debit floor, NOT a proof of whole graph cost/maximum. */
    Py_RETURN_NONE;
}
static PyObject *py_begin_bank(PyObject *self,PyObject *args) {
    PyObject *cap;const char *kind;
    if(!PyArg_ParseTuple(args,"Os",&cap,&kind))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if((uint64_t)PyDict_Size(r->banks)>=r->bindings->event_limit){bad("original_native_parent_bank_count");return NULL;}
    if(debit(r,0,0,0,4096+strlen(kind))<0)return NULL;
    PyObject *key=PyUnicode_FromFormat("%llu:%s",(unsigned long long)++r->serial,kind);
    PyObject *parts=PyList_New(0),*bank=NULL;
    if(key&&parts)bank=Py_BuildValue("{s:O,s:O,s:K,s:O}",
        "parts",parts,"sealed",Py_False,"bytes",(unsigned long long)0,"raw",Py_None);
    Py_XDECREF(parts);
    if(!key||!bank){Py_XDECREF(key);Py_XDECREF(bank);return NULL;}
    if(PyDict_SetItem(r->banks,key,bank)<0){Py_DECREF(key);Py_DECREF(bank);return NULL;}
    Py_DECREF(bank);return key;
}
static PyObject *bank_of(FridayPublisherOwnedRun *r,PyObject *key) {
    PyObject *b=PyDict_GetItemWithError(r->banks,key);
    if(!b||PyDict_GetItemString(b,"sealed")!=Py_False) {bad("native_bank_not_open");return NULL;}
    return b;
}
static PyObject *py_before_bytes(PyObject *self,PyObject *args) {
    PyObject *cap,*key;unsigned long long count;
    if(!PyArg_ParseTuple(args,"OOK",&cap,&key,&count))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    PyObject *b=bank_of(r,key);if(!b)return NULL;
    uint64_t n=PyLong_AsUnsignedLongLong(PyDict_GetItemString(b,"bytes"));
    if(PyErr_Occurred())return NULL;
    if(n>r->bindings->body_limit || count>r->bindings->body_limit-n || count>UINT64_MAX-r->bank_total) {
        bad("original_native_full_body_bound");return NULL;
    }
    if(count>(UINT64_MAX-128)/6){bad("native_byte_debit_overflow");return NULL;}
    if(debit(r,count*3,0,count*2,count*6+128)<0)return NULL;
    Py_RETURN_NONE;
}
static PyObject *py_before_scan(PyObject *self,PyObject *args) {
    PyObject *cap;unsigned long long reads,allocation;
    if(!PyArg_ParseTuple(args,"OKK",&cap,&reads,&allocation))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(allocation>UINT64_MAX-r->charged_allocation) {
        bad("secondary_scan_allocation_overflow");return NULL;
    }
    /* Same debit the native parent already uses. Not a new pool or a fit witness. */
    if(debit(r,(uint64_t)reads,0,0,(uint64_t)allocation)<0)return NULL;
    Py_RETURN_NONE;
}
static PyObject *py_append_bytes(PyObject *self,PyObject *args) {
    PyObject *cap,*key,*raw;
    if(!PyArg_ParseTuple(args,"OOO",&cap,&key,&raw))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(!PyBytes_CheckExact(raw)){bad("native_actual_full_bytes_type");return NULL;}
    PyObject *b=bank_of(r,key);if(!b)return NULL;
    uint64_t n=PyLong_AsUnsignedLongLong(PyDict_GetItemString(b,"bytes"));
    Py_ssize_t width=PyBytes_Size(raw);
    if(PyErr_Occurred()||width<0)return NULL;
    if(n>r->bindings->body_limit || (uint64_t)width>r->bindings->body_limit-n ||
       (uint64_t)width>UINT64_MAX-r->bank_total){bad("original_native_full_body_bound");return NULL;}
    /* Before copy: debit here as well as the caller's prospective preflight.
     * Double accounting is conservative, not a refund or cap raise. */
    if(debit(r,(uint64_t)width,0,0,(uint64_t)width+128)<0)return NULL;
    PyObject *copy=PyBytes_FromStringAndSize(PyBytes_AsString(raw),width);
    if(!copy)return NULL;
    if(PyList_Append(PyDict_GetItemString(b,"parts"),copy)<0){Py_DECREF(copy);return NULL;}
    Py_DECREF(copy);
    PyObject *total=PyLong_FromUnsignedLongLong(n+(uint64_t)width);
    if(!total)return NULL;
    int rc=PyDict_SetItemString(b,"bytes",total);Py_DECREF(total);
    if(rc<0)return NULL;r->bank_total+=(uint64_t)width;
    return PyLong_FromUnsignedLongLong(n);
}
static PyObject *py_finish_bank(PyObject *self,PyObject *args) {
    PyObject *cap,*key,*aliases;
    if(!PyArg_ParseTuple(args,"OOO",&cap,&key,&aliases))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    PyObject *b=bank_of(r,key);if(!b)return NULL;
    uint64_t n=PyLong_AsUnsignedLongLong(PyDict_GetItemString(b,"bytes"));
    if(PyErr_Occurred())return NULL;
    if(n>PY_SSIZE_T_MAX){bad("native_full_bank_ssize");return NULL;}
    if(debit(r,n,0,n,n+128)<0)return NULL;
    PyObject *raw=PyBytes_FromStringAndSize(NULL,(Py_ssize_t)n);if(!raw)return NULL;
    char *out=PyBytes_AsString(raw);PyObject *parts=PyDict_GetItemString(b,"parts");
    Py_ssize_t at=0;
    for(Py_ssize_t i=0;i<PyList_Size(parts);i++) {
        PyObject *part=PyList_GetItem(parts,i);Py_ssize_t width=PyBytes_Size(part);
        if(width<0||width>(Py_ssize_t)n-at){Py_DECREF(raw);bad("native_bank_parts");return NULL;}
        memcpy(out+at,PyBytes_AsString(part),(size_t)width);at+=width;
    }
    if(at!=(Py_ssize_t)n||PyDict_SetItemString(b,"raw",raw)<0||
       PyDict_SetItemString(b,"aliases",aliases)<0||PyDict_SetItemString(b,"sealed",Py_True)<0) {
        Py_DECREF(raw);return NULL;
    }
    Py_DECREF(raw);Py_RETURN_NONE;
}
static PyObject *py_read_bank(PyObject *self,PyObject *args) {
    PyObject *cap,*key;
    if(!PyArg_ParseTuple(args,"OO",&cap,&key))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    PyObject *b=PyDict_GetItemWithError(r->banks,key);
    if(!b||PyDict_GetItemString(b,"sealed")!=Py_True){bad("native_bank_not_complete");return NULL;}
    PyObject *raw=PyDict_GetItemString(b,"raw");
    if(!PyBytes_CheckExact(raw)||debit(r,(uint64_t)PyBytes_Size(raw),0,0,0)<0)return NULL;
    return Py_NewRef(raw);
}
static int row_status(PyObject *row,const char *expected) {
    if(!PyDict_CheckExact(row))return 0;
    PyObject *v=PyDict_GetItemString(row,"status");if(!v)return 0;
    return PyUnicode_Check(v)&&PyUnicode_CompareWithASCIIString(v,expected)==0;
}
static NativeCloseRecord *closed_record(FridayPublisherOwnedRun *r,PyObject *row) {
    for(NativeCloseRecord *p=r->close_records;p;p=p->next)if(p->row==row)return p;
    return NULL;
}
static PyObject *py_close_owned_row(PyObject *self,PyObject *args) {
    PyObject *cap,*row,*credit;
    if(!PyArg_ParseTuple(args,"OOO",&cap,&row,&credit))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    /* The actual openat+keeper birth in SAME Root authenticated this row,
     * credit and descriptor generation BEFORE Source saw the return value.
     * Exact dictionaries are public metadata, never independent authority. */
    int final=row==r->bindings->final_fd_row&&credit==r->bindings->final_fd_credit;
    if(!final&&!FridayPublisherPreparedHasRow(r->bindings->original_master_pool,row)) {
        bad("actual_native_prepared_birth_required");return NULL;
    }
    NativeCloseRecord *prior=closed_record(r,row);
    if(row_status(row,"CLOSED")) {
        if(prior&&prior->actual_called&&prior->syscall_rc==0&&prior->publication_confirmed)Py_RETURN_NONE;
        bad("Source_CLOSED_flag_not_actual_native_close");return NULL;
    }
    if(prior){bad("actual_native_same_row_close_never_repeated");return NULL;}
    if(row_status(row,"UNKNOWN")){bad("native_unknown_close_never_retried");return NULL;}
    if(!(row_status(row,"HELD")||row_status(row,"ACQUIRED"))){bad("native_owned_row_status");return NULL;}
    PyObject *fdobj=PyDict_GetItemString(row,"fd"),*slot=PyDict_GetItemString(row,"slot");
    long fd=fdobj?PyLong_AsLong(fdobj):-1;
    PyObject *fdrows=final&&PyDict_CheckExact(credit)?
        Py_XNewRef(PyDict_GetItemString(credit,"fd_rows")):PyObject_GetAttrString(credit,"fd_rows");
    if(PyErr_Occurred()||fd<0||fd>INT_MAX||!fdrows||!slot||
       !PyDict_CheckExact(fdrows)||PyDict_GetItemWithError(fdrows,slot)!=row||
       (final?fd!=r->bindings->final_fd:
        FridayPublisherPreparedMatches(r->bindings->original_master_pool,row,credit,(int)fd)!=1)) {
        Py_XDECREF(fdrows);bad("native_exact_original_row_credit");return NULL;
    }
    if(r->close_count>=65536){Py_DECREF(fdrows);bad("original_native_FD_history_envelope");return NULL;}
    if(debit(r,0,0,0,sizeof(NativeCloseRecord)+128)<0){Py_DECREF(fdrows);return NULL;}
    NativeCloseRecord *record=calloc(1,sizeof(*record));
    if(!record){Py_DECREF(fdrows);PyErr_NoMemory();return NULL;}
    record->row=Py_NewRef(row);record->next=r->close_records;r->close_records=record;r->close_count++;
    PyObject *attempt=PyDict_GetItemString(row,"close_cell"),*history=PyDict_GetItemString(row,"close_history");
    PyObject *oldstatus=attempt?PyDict_GetItemString(attempt,"status"):NULL;
    if((r->pending_close_row&&!r->pending_close_published)||!oldstatus||
       !PyUnicode_CheckExact(oldstatus)||PyUnicode_CompareWithASCIIString(oldstatus,"NOT_ATTEMPTED")!=0){
        Py_DECREF(fdrows);bad("native_close_attempt_not_repeated_after_publication_failure");return NULL;
    }
    if(!attempt||!history||PyList_Append(history,attempt)<0){Py_DECREF(fdrows);return NULL;}
    PyObject *s=PyUnicode_FromString("ATTEMPTED");
    if(!s||PyDict_SetItemString(attempt,"status",s)<0||PyDict_SetItemString(row,"attempted_close",s)<0) {
        Py_XDECREF(s);Py_DECREF(fdrows);return NULL;
    }
    Py_DECREF(s);
    Py_XSETREF(r->pending_close_row,Py_NewRef(row));r->pending_close_published=0;
    FridayPublisherRootCloseFact native;
    memset(&native,0,sizeof(native));
    int owner_rc=FridayPublisherRootCloseOwnedRow(r->bindings->original_master_pool,
        row,credit,(int)fd,&native);
    /* Root publishes its exact once-only native fact BEFORE this fallible
     * Source-compatible metadata layer. Failure before close is not a syscall
     * receipt; successful close survives failed status/history factories.
     * A prior generation's CLOSED row cannot cause a second integer close. */
    if(!native.attempted) {
        Py_DECREF(fdrows);
        bad("native_close_current_OFD_generation_UNCONFIRMED");return NULL;
    }
    int rc=native.rc,saved=native.original_errno;
    if(owner_rc<0&&rc==0) {
        Py_DECREF(fdrows);bad("native_close_owner_fact_inconsistent");return NULL;
    }
    /* Exact scalar outcome is retained BEFORE any post-close Python allocation. */
    record->actual_called=1;record->syscall_rc=rc;record->syscall_errno=rc==0?0:saved;
    r->pending_close_rc=rc;r->pending_close_errno=rc==0?0:saved;
    if(r->end_attempted&&row==r->bindings->final_fd_row) {
        /* Last native syscall scalars in prospectively owned cell BEFORE any
         * Python post-close allocation, including publication failure. */
        r->after_document_error.phase="final-original-row-close";
        r->after_document_error.syscall_attempted=1;
        r->after_document_error.syscall_rc=rc;
        r->after_document_error.syscall_errno=rc==0?0:saved;
    }
    if(rc==0&&!final&&FridayPublisherPreparedKeeperClose(r->bindings->original_master_pool,row)<0) {
        /* Record retains actual successful body FD close; keeper uncertainty
         * prevents published confirmation/UNUSED release. Never close again. */
        Py_DECREF(fdrows);retain_pending_error(r);return NULL;
    }
    s=PyUnicode_FromString(rc==0?"CLOSED":"UNKNOWN");
    if(!s||PyDict_SetItemString(row,"status",s)<0||
       PyDict_SetItemString(row,"attempted_close",s)<0||PyDict_SetItemString(attempt,"status",s)<0) {
        Py_XDECREF(s);Py_DECREF(fdrows);return NULL;
    }
    Py_DECREF(s);r->pending_close_published=1;record->publication_confirmed=1;
    if(rc==0){
        int del=PyDict_DelItem(fdrows,slot);Py_DECREF(fdrows);if(del<0)return NULL;Py_RETURN_NONE;
    }
    Py_DECREF(fdrows);errno=saved;PyErr_SetFromErrno(PyExc_OSError);
    retain_pending_error(r);return NULL;
}
static PyObject *py_before_document(PyObject *self,PyObject *args) {
    PyObject *cap;unsigned long long n;
    if(!PyArg_ParseTuple(args,"OK",&cap,&n))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(n>r->bindings->document_limit||n>UINT64_MAX/6){bad("native_original_document_bound");return NULL;}
    if(debit(r,n*3,n,n*2,n*6+131072)<0)return NULL;
    Py_RETURN_NONE;
}
static PyObject *final_failure(FridayPublisherOwnedRun *r,const char *phase) {
    save_error_cell(&r->after_document_error,phase);
    retain_pending_error(r);return NULL;
}
static PyObject *py_finish_outside(PyObject *self,PyObject *args) {
    PyObject *cap,*raw,*source_end,*prepared;
    if(!PyArg_ParseTuple(args,"OOOO",&cap,&raw,&source_end,&prepared))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(r->end_attempted||!PyBytes_CheckExact(raw)||!PyDict_CheckExact(source_end)||
       PyDict_GetItemString(source_end,"ownership_retired")!=Py_True) {
        bad("native_actual_source_end_required");return NULL;
    }
    if(PyDict_GetItemString(r->anchors,"actual-source-end")!=source_end ||
       !PyList_CheckExact(prepared)){bad("native_source_end_custody");return NULL;}
    for(Py_ssize_t i=0;i<PyList_Size(prepared);i++) {
        PyObject *p=PyList_GetItem(prepared,i);
        if(!PyTuple_CheckExact(p)||PyTuple_Size(p)!=7) {
            bad("native_prepared_body_outside_close_unconfirmed");return NULL;
        }
        NativeCloseRecord *actual=closed_record(r,PyTuple_GetItem(p,6));
        if(!actual||!actual->actual_called||actual->syscall_rc!=0||!actual->publication_confirmed||
           !row_status(PyTuple_GetItem(p,6),"CLOSED")) {
            bad("native_prepared_body_actual_syscall_unconfirmed");return NULL;
        }
    }
    PyObject *receiver=PyDict_GetItemString(r->anchors,"performing-receiver");
    if(!receiver){bad("actual_native_owned_receiver");return NULL;}
    PyObject *checked=PyObject_CallMethod(receiver,"verify_v2_document","O",raw);
    if(!checked)return NULL;
    int same=checked==raw;Py_DECREF(checked);
    if(!same){bad("native_v2_full_document_actual_reader");return NULL;}
    if(debit(r,(uint64_t)PyBytes_Size(raw),(uint64_t)PyBytes_Size(raw),0,0)<0)return NULL;
    r->end_attempted=1;r->final_raw=Py_NewRef(raw);r->source_end=Py_NewRef(source_end);
    r->after_document_error.actual_document=Py_NewRef(raw);
    if(r->bindings->final_fd<0){bad("existing_native_preowned_final_endpoint");return final_failure(r,"final-endpoint-binding");}
    Py_ssize_t n=PyBytes_Size(raw),at=0;
    while(at<n) {
        r->after_document_error.phase="final-document-write";
        r->after_document_error.syscall_attempted=1;
        ssize_t wrote=write(r->bindings->final_fd,PyBytes_AsString(raw)+at,(size_t)(n-at));
        int saved=wrote<0?errno:(wrote==0?EIO:0);
        r->after_document_error.syscall_rc=(int)wrote; /* exact, <= original 2M document */
        r->after_document_error.syscall_errno=saved;
        if(wrote<=0){errno=saved;PyErr_SetFromErrno(PyExc_OSError);return final_failure(r,"final-document-write");}
        at+=wrote;r->after_document_error.actual_written=(uint64_t)at;
    }
    r->after_document_error.phase="final-document-fsync";
    int sync_rc=fsync(r->bindings->final_fd),sync_errno=sync_rc<0?errno:0;
    r->after_document_error.syscall_rc=sync_rc;r->after_document_error.syscall_errno=sync_errno;
    if(sync_rc<0){errno=sync_errno;PyErr_SetFromErrno(PyExc_OSError);return final_failure(r,"final-document-fsync");}
    /* Root retains the exact raw document. Its last close's original error is
     * separate strong actual custody if it fails; never called part of old pin. */
    r->final_close_attempted=1;
    PyObject *closeargs=PyTuple_Pack(3,cap,r->bindings->final_fd_row,r->bindings->final_fd_credit);
    if(!closeargs)return final_failure(r,"final-close-arguments-before-syscall");
    PyObject *closed=py_close_owned_row(NULL,closeargs);Py_DECREF(closeargs);
    if(!closed)return final_failure(r,"final-close-original-error-or-publication");Py_DECREF(closed);
    NativeCloseRecord *last=closed_record(r,r->bindings->final_fd_row);
    r->final_close_confirmed=last&&last->actual_called&&last->syscall_rc==0&&
        last->publication_confirmed&&row_status(r->bindings->final_fd_row,"CLOSED");
    if(!r->final_close_confirmed){bad("native_final_endpoint_close_unconfirmed");return NULL;}
    r->result=Py_BuildValue("{s:s,s:O,s:O,s:O,s:O,s:O}",
        "schema","friday.sol090.actual-native-outside-completion.v1",
        "ownership_retired",Py_False,"actual_full_document",raw,
        "actual_source_end",source_end,"actual_final_FD_row",r->bindings->final_fd_row,
        "GO",Py_False);
    if(!r->result)return final_failure(r,"final-result-after-actual-close");
    return Py_NewRef(r->result);
}
static PyObject *py_outside_end(PyObject *self,PyObject *args) {
    PyObject *cap,*raw,*end;
    if(!PyArg_ParseTuple(args,"OOO",&cap,&raw,&end))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    /* Source is still active: physical endpoint close is not Run retirement. */
    return PyBool_FromLong(r->references_retired&&r->end_attempted&&r->final_close_confirmed&&r->final_raw==raw&&r->source_end==end&&r->result);
}

/* Exact owned struct/public API export, not an interpreter/private-heap walk.
 * Mutable anchors/bank lists are captured as a finite before-effect cut; later
 * changes remain actual native events/banks and are captured at the next cut. */
static PyObject *py_owned_state(PyObject *self,PyObject *cap) {
    FridayPublisherOwnedRun *r=PyCapsule_GetPointer(cap,CAPSULE_NAME);
    if(!r||r!=active_run){bad("actual_selected_owned_context");return NULL;}
    /* BOTH actual observer/secondary paths meet the original-cell barrier
     * BEFORE copies, scalar factories or either error-cell factory. Shadow
     * remains the old read-only export; no parent registration/debit added. */
    if(r->owner_pid==getpid()&&
       (FridayPublisherRootErrorCellReady(r,&r->prefix_error)<0||
        FridayPublisherRootErrorCellReady(r,&r->after_document_error)<0))return NULL;
    if(r->owner_pid==getpid() && debit(r,0,0,0,131328+(uint64_t)PyDict_Size(r->anchors)*1024)<0)return NULL;
    /* Fork shadow export is READ ONLY of this process's exact selected copied
     * struct. No parent observer/budget/authority callback is ever invoked.
     * Its metadata/temporary RAM is part of the original child stock envelope;
     * complete worst-case multiplicity remains C2 REQUIRED_NOT_PROVEN. */
    PyObject *anchors=PyDict_Copy(r->anchors),*banks=PyDict_New();
    if(!anchors||!banks){Py_XDECREF(anchors);Py_XDECREF(banks);return NULL;}
    Py_ssize_t pos=0;PyObject *key,*b;
    while(PyDict_Next(r->banks,&pos,&key,&b)) {
        PyObject *cut=PyDict_Copy(b),*parts=PyList_AsTuple(PyDict_GetItemString(b,"parts"));
        PyObject *aliases=PyDict_GetItemString(b,"aliases"),*frozen=NULL;
        if(aliases&&PyList_CheckExact(aliases))frozen=PyList_AsTuple(aliases);
        if(!cut||!parts||PyDict_SetItemString(cut,"parts",parts)<0||
           (frozen&&PyDict_SetItemString(cut,"aliases",frozen)<0)||PyDict_SetItem(banks,key,cut)<0) {
            Py_XDECREF(cut);Py_XDECREF(parts);Py_XDECREF(frozen);Py_DECREF(anchors);Py_DECREF(banks);return NULL;
        }
        Py_DECREF(cut);Py_DECREF(parts);Py_XDECREF(frozen);
    }
    PyObject *errors=PyList_AsTuple(r->errors);
    PyObject *state=errors?Py_BuildValue("{s:s,s:l,s:O,s:O,s:O,s:O,s:O,s:O,s:O,s:O,s:O,s:K,s:K,s:K,s:i,s:i,s:i}",
        "schema",r->owner_pid==getpid()?"friday.astra.a275.selected-owned-native-public-state.v7":
            "friday.sol121.selected-owned-native-public-state.v6","owner_pid",(long)r->owner_pid,
        "root_fact",r->bindings->root_fact,"qualification",r->bindings->qualification,
        "held_root_tool_preimage",r->bindings->held_root_tool_preimage,
        "source_qualification",r->source_qualification?r->source_qualification:Py_None,
        "anchors",anchors,"banks",banks,"errors",errors,"result",r->result?r->result:Py_None,
        "source_end",r->source_end?r->source_end:Py_None,
        "serial",(unsigned long long)r->serial,"charged_allocation",(unsigned long long)r->charged_allocation,
        "bank_total",(unsigned long long)r->bank_total,"end_attempted",r->end_attempted,
        "final_close_attempted",r->final_close_attempted,"final_close_confirmed",r->final_close_confirmed):NULL;
    Py_XDECREF(errors);Py_DECREF(anchors);Py_DECREF(banks);
    if(state) {
        PyObject *records=PyList_New(0);
        if(!records){Py_DECREF(state);return NULL;}
        for(NativeCloseRecord *p=r->close_records;p;p=p->next) {
            PyObject *copy=Py_BuildValue("{s:O,s:i,s:i,s:i,s:i}","exact_row",p->row,
                "actual_called",p->actual_called,"syscall_rc",p->syscall_rc,
                "syscall_errno",p->syscall_errno,"publication_confirmed",p->publication_confirmed);
            if(!copy||PyList_Append(records,copy)<0){Py_XDECREF(copy);Py_DECREF(records);Py_DECREF(state);return NULL;}
            Py_DECREF(copy);
        }
        if(PyDict_SetItemString(state,"actual_native_close_records",records)<0){Py_DECREF(records);Py_DECREF(state);return NULL;}
        Py_DECREF(records);
        PyObject *pending=Py_BuildValue("{s:O,s:i,s:i,s:i}","exact_row",r->pending_close_row?r->pending_close_row:Py_None,
            "syscall_rc",r->pending_close_rc,"syscall_errno",r->pending_close_errno,
            "publication_confirmed",r->pending_close_published);
        if(!pending||PyDict_SetItemString(state,"actual_last_close",pending)<0){Py_XDECREF(pending);Py_DECREF(state);return NULL;}
        Py_DECREF(pending);
        PyObject *role=PyUnicode_FromString(r->owner_pid==getpid()?"OWNING_ROOT":"INHERITED_READONLY_SHADOW");
        PyObject *actual=PyLong_FromLong((long)getpid());
        if(!role||!actual||PyDict_SetItemString(state,"role",role)<0||PyDict_SetItemString(state,"actual_pid",actual)<0) {
            Py_XDECREF(role);Py_XDECREF(actual);Py_DECREF(state);return NULL;
        }
        Py_DECREF(role);Py_DECREF(actual);
        PyObject *prefix=error_cell_cut(r,&r->prefix_error);
        PyObject *late=prefix?error_cell_cut(r,&r->after_document_error):NULL;
        if(!prefix||!late||PyDict_SetItemString(state,"prefix_error_cell",prefix)<0||
           PyDict_SetItemString(state,"after_document_error_cell",late)<0) {
            Py_XDECREF(prefix);Py_XDECREF(late);Py_DECREF(state);return NULL;
        }
        Py_DECREF(prefix);Py_DECREF(late);
    }
    if(state) {
        PyObject *pool=FridayPublisherMasterState(r->bindings->original_master_pool);
        PyObject *rows=FridayPublisherPreparedState(r->bindings->original_master_pool);
        if(!pool||!rows||PyDict_SetItemString(state,"original_master_pool",pool)<0||
           PyDict_SetItemString(state,"actual_native_prepared_rows",rows)<0) {
            Py_XDECREF(pool);Py_XDECREF(rows);Py_DECREF(state);return NULL;
        }
        Py_DECREF(pool);Py_DECREF(rows);
    }
    if(state) {
        PyObject *handoff=PyTuple_Pack(4,r->source_error?r->source_error:Py_None,
            r->source_error_phase?r->source_error_phase:Py_None,
            r->source_error_tb?r->source_error_tb:Py_None,
            r->source_error_record?r->source_error_record:Py_None);
        if(!handoff||PyDict_SetItemString(state,"source_error_handoff",handoff)<0) {
            Py_XDECREF(handoff);Py_DECREF(state);return NULL;
        }
        Py_DECREF(handoff); /* dictionary plus original Run own every member */
    }
    if(state&&r->final_raw&&PyDict_SetItemString(state,"final_raw",r->final_raw)<0){Py_DECREF(state);return NULL;}
    if(state&&!r->final_raw&&PyDict_SetItemString(state,"final_raw",Py_None)<0){Py_DECREF(state);return NULL;}
    return state;
}
static PyObject *py_is_context(PyObject *self,PyObject *obj) {
    if(!active_run||!PyCapsule_IsValid(obj,CAPSULE_NAME))Py_RETURN_FALSE;
    return PyBool_FromLong(PyCapsule_GetPointer(obj,CAPSULE_NAME)==active_run);
}
static PyObject *py_is_module(PyObject *self,PyObject *obj) {
    FridayPublisherOwnedRun *r=active_run;
    if(!r)Py_RETURN_FALSE;
    if(!r->registered_module)r->registered_module=Py_NewRef(self);
    return PyBool_FromLong(obj==r->registered_module);
}
static PyObject *py_bind_source(PyObject *self,PyObject *args) {
    PyObject *cap,*fact,*qualification;
    if(!PyArg_ParseTuple(args,"OOO",&cap,&fact,&qualification))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(r->bindings->current_enrollment_matches(fact,qualification)!=1||PyErr_Occurred()){
        bad("current_native_existing_Root_Source_qualification");return NULL;
    }
    if(r->source_qualification){bad("source_qualification_once");return NULL;}
    r->source_qualification=Py_NewRef(qualification);Py_RETURN_NONE;
}


static PyObject *py_master_owner(PyObject *self,PyObject *cap) {
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(!FridayPublisherMasterOwns(r->bindings->original_master_pool)){bad("actual_original_master_owner");return NULL;}
    return PyLong_FromLong((long)r->owner_pid);
}
static PyObject *py_master_change(PyObject *self,PyObject *args) {
    PyObject *cap;const char *op;unsigned long long token,reads,output,hash,allocation,slots;
    if(!PyArg_ParseTuple(args,"OsKKKKKK",&cap,&op,&token,&reads,&output,&hash,&allocation,&slots))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(!strcmp(op,"native-reserve")){bad("native_only_bootstrap_credit");return NULL;}
    uint64_t out=0;
    if(FridayPublisherMasterChange(r->bindings->original_master_pool,op,token,reads,output,
                                  hash,allocation,slots,&out)<0) {
        if(!PyErr_Occurred())FridayPublisherMasterFault(r->bindings->original_master_pool,"original_pool_refusal");
        return NULL;
    }
    if(!strcmp(op,"release")||!strcmp(op,"detach"))return PyBool_FromLong(out==1);
    return PyLong_FromUnsignedLongLong(out);
}
static PyObject *py_master_state(PyObject *self,PyObject *cap) {
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    return FridayPublisherMasterState(r->bindings->original_master_pool);
}
static PyObject *py_prepare_owned_row(PyObject *self,PyObject *args) {
    PyObject *cap,*row,*credit;const char *name;int dirfd;
    if(!PyArg_ParseTuple(args,"OisOO",&cap,&dirfd,&name,&row,&credit))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    int fd=FridayPublisherPreparedCreate(r->bindings->original_master_pool,dirfd,name,row,credit);
    if(fd<0){if(!PyErr_Occurred())bad("actual_prepared_native_birth");return NULL;}
    return PyLong_FromLong(fd);
}
static PyObject *py_prepared_has_row(PyObject *self,PyObject *args) {
    PyObject *cap,*row;
    if(!PyArg_ParseTuple(args,"OO",&cap,&row))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    return PyBool_FromLong(FridayPublisherPreparedHasRow(r->bindings->original_master_pool,row));
}
static PyObject *py_bootstrap_signature(PyObject *self,PyObject *args) {
    PyObject *cap,*a,*sig,*key;
    if(!PyArg_ParseTuple(args,"OOOO",&cap,&a,&sig,&key))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    return FridayPublisherRootBootstrapSignature(r->bindings->original_master_pool,a,sig,key);
}

/* Native-only storage; these wrappers do not create a Source authority. */
#define OWN_WRAPPER(name,fn) static PyObject *name(PyObject *self,PyObject *arg) {return fn(arg);}
OWN_WRAPPER(py_own_consumer,FridayPublisherRootOwnConsumer)
OWN_WRAPPER(py_own_binding,FridayPublisherRootOwnBinding)
OWN_WRAPPER(py_own_binding_check,FridayPublisherRootOwnBindingCheck)
OWN_WRAPPER(py_own_held_body_check,FridayPublisherRootOwnHeldBodyCheck)
OWN_WRAPPER(py_own_function,FridayPublisherRootOwnFunction)
OWN_WRAPPER(py_own_class_body,FridayPublisherRootOwnClassBody)
OWN_WRAPPER(py_own_support,FridayPublisherRootOwnSupport)
OWN_WRAPPER(py_own_support_check,FridayPublisherRootOwnSupportCheck)
OWN_WRAPPER(py_own_error_record_serial,FridayPublisherRootOwnErrorRecordSerial)
OWN_WRAPPER(py_own_error_record_check,FridayPublisherRootOwnErrorRecordCheck)
OWN_WRAPPER(py_own_error_record_builtin,FridayPublisherRootOwnErrorRecordBuiltin)
OWN_WRAPPER(py_own_prepare,FridayPublisherRootOwnPrepare)
OWN_WRAPPER(py_own_var,FridayPublisherRootOwnVar)
OWN_WRAPPER(py_own_set,FridayPublisherRootOwnSet)
OWN_WRAPPER(py_own_reset,FridayPublisherRootOwnReset)
OWN_WRAPPER(py_own_context_body,FridayPublisherRootOwnContextBody)
OWN_WRAPPER(py_own_mapping,FridayPublisherRootOwnMapping)
OWN_WRAPPER(py_own_hash_new,FridayPublisherRootOwnHashNew)
OWN_WRAPPER(py_own_hash_update,FridayPublisherRootOwnHashUpdate)
OWN_WRAPPER(py_own_hash_body,FridayPublisherRootOwnHashBody)
OWN_WRAPPER(py_own_secondary_cut,FridayPublisherRootOwnSecondaryCut)
#undef OWN_WRAPPER
static PyMethodDef methods[]={
    {"execute_held_consumer",py_own_consumer,METH_VARARGS,NULL},
    {"own_binding",py_own_binding,METH_O,NULL},
    {"own_binding_check",py_own_binding_check,METH_O,NULL},
    {"own_held_body_check",py_own_held_body_check,METH_VARARGS,NULL},
    {"own_function",py_own_function,METH_O,NULL},{"own_class_body",py_own_class_body,METH_O,NULL},
    {"own_support",py_own_support,METH_VARARGS,NULL},
    {"own_support_check",py_own_support_check,METH_VARARGS,NULL},
    {"own_error_record_serial",py_own_error_record_serial,METH_O,NULL},
    {"own_error_record_check",py_own_error_record_check,METH_VARARGS,NULL},
    {"own_error_record_builtin",py_own_error_record_builtin,METH_VARARGS,NULL},
    {"own_prepare",py_own_prepare,METH_VARARGS,NULL},
    {"own_context_var",py_own_var,METH_VARARGS,NULL},
    {"own_context_set",py_own_set,METH_VARARGS,NULL},
    {"own_context_reset",py_own_reset,METH_VARARGS,NULL},
    {"own_context_body",py_own_context_body,METH_O,NULL},
    {"own_mapping",py_own_mapping,METH_VARARGS,NULL},
    {"own_hash_new",py_own_hash_new,METH_VARARGS,NULL},
    {"own_hash_update",py_own_hash_update,METH_VARARGS,NULL},
    {"own_hash_body",py_own_hash_body,METH_O,NULL},
    {"own_secondary_cut",py_own_secondary_cut,METH_O,NULL},
    {"master_owner",py_master_owner,METH_O,NULL},{"master_change",py_master_change,METH_VARARGS,NULL},
    {"master_state",py_master_state,METH_O,NULL},{"prepare_owned_row",py_prepare_owned_row,METH_VARARGS,NULL},
    {"prepared_has_row",py_prepared_has_row,METH_VARARGS,NULL},
    {"bootstrap_signature",py_bootstrap_signature,METH_VARARGS,NULL},
    {"prefix_envelope",py_prefix_envelope,METH_O,NULL},
    {"after_document_cell",py_after_document_cell,METH_O,NULL},
    {"accept_after_document",py_accept_after_document,METH_VARARGS,NULL},
    {"owned_state",py_owned_state,METH_O,NULL},{"is_context",py_is_context,METH_O,NULL},
    {"is_module",py_is_module,METH_O,NULL},{"bind_source",py_bind_source,METH_VARARGS,NULL},
    {"current",py_current,METH_NOARGS,NULL},{"root_fact",py_get_fact,METH_O,NULL},
    {"qualification",py_get_qualification,METH_O,NULL},{"hold",py_hold,METH_VARARGS,NULL},
    {"hold_source_error",(PyCFunction)(void (*)(void))py_hold_source_error,METH_FASTCALL,NULL},
    {"get_anchor",py_get_anchor,METH_VARARGS,NULL},{"matches",py_matches,METH_VARARGS,NULL},
    {"before_graph",py_before_graph,METH_VARARGS,NULL},{"begin_bank",py_begin_bank,METH_VARARGS,NULL},
    {"before_bytes",py_before_bytes,METH_VARARGS,NULL},{"before_scan",py_before_scan,METH_VARARGS,NULL},
    {"append_bytes",py_append_bytes,METH_VARARGS,NULL},
    {"finish_bank",py_finish_bank,METH_VARARGS,NULL},{"read_bank",py_read_bank,METH_VARARGS,NULL},
    {"close_owned_row",py_close_owned_row,METH_VARARGS,NULL},
    {"before_document",py_before_document,METH_VARARGS,NULL},{"finish_outside",py_finish_outside,METH_VARARGS,NULL},
    {"outside_end",py_outside_end,METH_VARARGS,NULL},{NULL,NULL,0,NULL}};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,"publisher_owned_custody",NULL,-1,methods};
PyMODINIT_FUNC PyInit_publisher_owned_custody(void){return PyModule_Create(&module);}
int FridayPublisherOwnedModuleOriginal(PyObject *o) {
    return o&&Py_TYPE(o)==&PyModule_Type&&PyModule_GetDef(o)==&module;
}

#define CALLER_PACKET_SCHEMA FRIDAY_PUBLISHER_CALLER_PACKET_SCHEMA
#define CALLER_ROOT_COUNT FRIDAY_PUBLISHER_RUN_ROOTS
/* Only this exact own Run's registered PyObject fields are traversed.
 * No pointer fabrication, arbitrary/private object layout, or foreign heap. */
static void caller_roots(FridayPublisherOwnedRun *r,PyObject **a) {
    PyObject *v[CALLER_ROOT_COUNT]={
        r->anchors,r->banks,r->errors,r->result,r->final_raw,r->source_end,
        r->capsule,r->source_qualification,r->registered_module,r->pending_close_row,
        r->source_entry,r->source_args,r->binding_storage.root_fact,
        r->binding_storage.qualification,r->binding_storage.held_root_tool_preimage,
        r->binding_storage.final_fd_row,r->binding_storage.final_fd_credit,
        r->prefix_error.error_type,r->prefix_error.error_value,r->prefix_error.error_tb,
        r->prefix_error.actual_document,r->prefix_error.accepted_cut,
        r->after_document_error.error_type,r->after_document_error.error_value,
        r->after_document_error.error_tb,r->after_document_error.actual_document,
        r->after_document_error.accepted_cut,r->source_return,
        r->source_error,r->source_error_phase,r->source_error_tb,r->source_error_record};
    for(int i=0;i<CALLER_ROOT_COUNT;i++)a[i]=v[i]?v[i]:Py_None;
}
static int caller_packet_current(FridayPublisherOwnedRun *r) {
    PyObject *p=r->caller_packet;
    if(!p||!PyTuple_CheckExact(p)||PyTuple_Size(p)!=13||
       !PyDict_CheckExact(r->banks)||!r->final_close_confirmed||
       r->prefix_error.saved||r->after_document_error.saved||
       r->source_error||r->source_error_phase||r->source_error_tb||r->source_error_record)
        return bad("actual_native_caller_transfer_not_complete");
    PyObject *roots=PyTuple_GetItem(p,6),*banks=PyTuple_GetItem(p,5);
    if(!PyTuple_CheckExact(roots)||PyTuple_Size(roots)!=CALLER_ROOT_COUNT||
       !PyTuple_CheckExact(banks)||PyTuple_Size(banks)!=PyDict_Size(r->banks))
        return bad("actual_native_caller_transfer_current_inventory");
    PyObject *actual[CALLER_ROOT_COUNT];caller_roots(r,actual);
    for(int i=0;i<CALLER_ROOT_COUNT;i++)
        if(PyTuple_GetItem(roots,i)!=actual[i])
            return bad("actual_native_caller_transfer_root_alias_changed");
    for(Py_ssize_t i=0;i<PyTuple_Size(banks);i++) {
        PyObject *row=PyTuple_GetItem(banks,i);
        if(!PyTuple_CheckExact(row)||PyTuple_Size(row)!=4)
            return bad("actual_native_caller_transfer_bank_row");
        PyObject *b=PyDict_GetItemWithError(r->banks,PyTuple_GetItem(row,0));
        if(!b||!PyDict_CheckExact(b)||PyDict_GetItemString(b,"sealed")!=Py_True||
           PyDict_GetItemString(b,"raw")!=PyTuple_GetItem(row,1)||
           !PyBytes_CheckExact(PyTuple_GetItem(row,1)))
            return bad("actual_native_caller_transfer_bank_changed");
        PyObject *parts=PyDict_GetItemString(b,"parts"),*aliases=PyDict_GetItemString(b,"aliases");
        PyObject *fp=PyTuple_GetItem(row,3),*fa=PyTuple_GetItem(row,2);
        if(!parts||!aliases||!PyList_CheckExact(parts)||!PyList_CheckExact(aliases)||
           !PyTuple_CheckExact(fp)||!PyTuple_CheckExact(fa)||
           PyList_Size(parts)!=PyTuple_Size(fp)||PyList_Size(aliases)!=PyTuple_Size(fa))
            return bad("actual_native_caller_part_alias_inventory_changed");
        for(Py_ssize_t j=0;j<PyTuple_Size(fp);j++)if(PyTuple_GetItem(fp,j)!=PyList_GetItem(parts,j))
            return bad("actual_native_caller_original_part_changed");
        for(Py_ssize_t j=0;j<PyTuple_Size(fa);j++)if(PyTuple_GetItem(fa,j)!=PyList_GetItem(aliases,j))
            return bad("actual_native_caller_original_alias_changed");
    }
    PyObject *closes=PyTuple_GetItem(p,7);
    if(!PyTuple_CheckExact(closes)||(uint64_t)PyTuple_Size(closes)!=r->close_count)
        return bad("actual_native_caller_close_count_changed");
    Py_ssize_t at=0;
    for(NativeCloseRecord *q=r->close_records;q;q=q->next) {
        if(at>=PyTuple_Size(closes)||!q->actual_called||q->syscall_rc!=0||
           !q->publication_confirmed||!row_status(q->row,"CLOSED"))
            return bad("actual_native_caller_actual_close_changed");
        PyObject *row=PyTuple_GetItem(closes,at++);
        if(!PyTuple_CheckExact(row)||PyTuple_Size(row)!=5||PyTuple_GetItem(row,0)!=q->row)
            return bad("actual_native_caller_close_row_alias_changed");
    }
    if(at!=PyTuple_Size(closes)||FridayPublisherCallerPacketBytes(p,0)<0)
        return bad("actual_native_caller_full_byte_readback");
    return 0;
}
static PyObject *prepare_caller_packet(FridayPublisherOwnedRun *r) {
    if(!r->result||!r->final_raw||!r->source_end||!r->source_qualification||
       !r->final_close_confirmed||r->prefix_error.saved||r->after_document_error.saved||
       r->source_error||r->source_error_phase||r->source_error_tb||r->source_error_record||
       !PyDict_CheckExact(r->banks)||r->caller_packet||r->release_attempted)
        {bad("actual_native_caller_complete_before_transfer");return NULL;}
    Py_ssize_t count=PyDict_Size(r->banks);
    if(count<0||(uint64_t)count>r->bindings->event_limit||
       r->close_count>r->bindings->event_limit)
        {bad("actual_native_caller_transfer_inventory_bound");return NULL;}
    /* Prospective reference-container debit BEFORE allocation. It is a declared
     * floor, not a fabricated whole factory/RSS/CPU/implicit-IO upper bound. */
    uint64_t references=0;
    Py_ssize_t scan=0;PyObject *scan_key,*scan_bank;
    while(PyDict_Next(r->banks,&scan,&scan_key,&scan_bank)) {
        if(!PyDict_CheckExact(scan_bank)) {
            bad("actual_native_caller_bank_before_reference_debit");return NULL;
        }
        PyObject *parts=PyDict_GetItemString(scan_bank,"parts");
        PyObject *aliases=PyDict_GetItemString(scan_bank,"aliases");
        if(!parts||!aliases||!PyList_CheckExact(parts)||!PyList_CheckExact(aliases)) {
            bad("actual_native_caller_reference_inventory");return NULL;
        }
        uint64_t n=(uint64_t)PyList_Size(parts)+(uint64_t)PyList_Size(aliases);
        if(n>UINT64_MAX-references) {
            bad("actual_native_caller_reference_inventory_overflow");return NULL;
        }
        references+=n;
    }
    uint64_t fixed=(uint64_t)count*8192+r->close_count*2048+
                   CALLER_ROOT_COUNT*512+36864;
    if(references>(UINT64_MAX-fixed)/128) {
        bad("actual_native_caller_reference_charge_overflow");return NULL;
    }
    uint64_t charge=fixed+references*128;
    if(debit(r,0,0,0,charge)<0)return NULL;
    /* Two complete native pre-retirement guards and actual outer C readback.
     * Parts compare reads 2B; two alias/value/physical memcmps read 4B
     * each pass. No hidden post-retirement debit or Source/user callback. */
    if(r->bank_total>UINT64_MAX/18||debit(r,18*r->bank_total,0,0,0)<0)return NULL;
    PyObject *banks=PyTuple_New(count),*roots=NULL,*closes=NULL,*pid=NULL,*status=NULL,*cost=NULL;
    PyObject *final_status=NULL,*final_packet=NULL;
    PyObject *packet=NULL,*schema=NULL;
    if(!banks)goto fail;
    Py_ssize_t pos=0,at=0;PyObject *key,*b;
    while(PyDict_Next(r->banks,&pos,&key,&b)) {
        if(!PyUnicode_CheckExact(key)||!PyDict_CheckExact(b)||
           PyDict_GetItemString(b,"sealed")!=Py_True)
            {bad("actual_native_caller_open_bank_not_transferred");goto fail;}
        PyObject *raw=PyDict_GetItemString(b,"raw");
        PyObject *parts=PyDict_GetItemString(b,"parts"),*aliases=PyDict_GetItemString(b,"aliases");
        if(!raw||!PyBytes_CheckExact(raw)||!parts||!PyList_CheckExact(parts)||
           !aliases||!PyList_CheckExact(aliases))
            {bad("actual_native_caller_full_bank_representation");goto fail;}
        PyObject *fp=PyList_AsTuple(parts),*fa=PyList_AsTuple(aliases);
        if(!fp||!fa){Py_XDECREF(fp);Py_XDECREF(fa);goto fail;}
        Py_ssize_t width=0,n=PyBytes_Size(raw);
        for(Py_ssize_t j=0;j<PyTuple_Size(fp);j++) {
            PyObject *part=PyTuple_GetItem(fp,j);
            if(!PyBytes_CheckExact(part)||PyBytes_Size(part)>n-width) {
                Py_DECREF(fp);Py_DECREF(fa);bad("actual_native_caller_full_part_bound");goto fail;
            }
            width+=PyBytes_Size(part);
        }
        if(width!=n){Py_DECREF(fp);Py_DECREF(fa);bad("actual_native_caller_full_parts");goto fail;}
        PyObject *row=PyTuple_Pack(4,key,raw,fa,fp);Py_DECREF(fp);Py_DECREF(fa);
        if(!row)goto fail;
        PyTuple_SET_ITEM(banks,at++,row);
    }
    if(at!=count){bad("actual_native_caller_bank_inventory_changed");goto fail;}
    roots=PyTuple_New(CALLER_ROOT_COUNT);if(!roots)goto fail;
    PyObject *a[CALLER_ROOT_COUNT];caller_roots(r,a);
    for(int i=0;i<CALLER_ROOT_COUNT;i++)PyTuple_SET_ITEM(roots,i,Py_NewRef(a[i]));
    closes=PyTuple_New((Py_ssize_t)r->close_count);if(!closes)goto fail;
    at=0;
    for(NativeCloseRecord *q=r->close_records;q;q=q->next) {
        if(at>=(Py_ssize_t)r->close_count||!q->actual_called||q->syscall_rc!=0||
           !q->publication_confirmed||!row_status(q->row,"CLOSED"))
            {bad("actual_native_caller_close_inventory_unconfirmed");goto fail;}
        PyObject *row=Py_BuildValue("(Oiiii)",q->row,q->actual_called,q->syscall_rc,
                                   q->syscall_errno,q->publication_confirmed);
        if(!row)goto fail;PyTuple_SET_ITEM(closes,at++,row);
    }
    if(at!=(Py_ssize_t)r->close_count){bad("actual_native_caller_close_inventory_changed");goto fail;}
    pid=PyLong_FromLong((long)r->owner_pid);schema=PyUnicode_FromString(CALLER_PACKET_SCHEMA);
    status=pid?PyTuple_Pack(2,pid,Py_False):NULL;
    cost=Py_BuildValue("(KKK)",(unsigned long long)r->charged_allocation,
                       (unsigned long long)r->bank_total,(unsigned long long)r->close_count);
    if(!schema||!status||!cost)goto fail;
    packet=PyTuple_Pack(13,schema,r->source_return,r->result,r->final_raw,r->source_end,
        banks,roots,closes,r->bindings->root_fact,r->bindings->qualification,
        r->source_qualification,status,cost);
    if(!packet)goto fail;
    /* Never published to verify_native_caller_packet. A final True here is a
     * private preconstructed value, not an effect claim exposed to Source. */
    final_status=PyTuple_Pack(2,pid,Py_True);
    final_packet=final_status?PyTuple_Pack(13,schema,r->source_return,r->result,
        r->final_raw,r->source_end,banks,roots,closes,r->bindings->root_fact,
        r->bindings->qualification,r->source_qualification,final_status,cost):NULL;
    if(!final_packet){Py_CLEAR(packet);goto fail;}
    r->final_caller_packet=final_packet;final_packet=NULL;
fail:
    Py_XDECREF(banks);Py_XDECREF(roots);Py_XDECREF(closes);Py_XDECREF(pid);
    Py_XDECREF(schema);Py_XDECREF(status);Py_XDECREF(cost);
    Py_XDECREF(final_status);Py_XDECREF(final_packet);
    return packet;
}

PyObject *FridayPublisherOwnedInvoke(const FridayPublisherBindings *b,PyObject *entry,
                                     PyObject *args,FridayPublisherOwnedRun **retained,
                                     FridayPublisherInvokeRefusal *refusal) {
    /* The same original C caller owns the output cell before its first debit.
     * Pure preconditions create no Python exception. The external matcher is
     * still the REAL enrolled provider, not a default true/Source-created
     * substitute; its actual cost/authority remains a separate prerequisite. */
    if(!refusal)return NULL; /* invalid C API use: leave pending error alone */
    *refusal=FRIDAY_INVOKE_PREFLIGHT_OK;
    if(PyErr_Occurred()){
        *refusal=FRIDAY_INVOKE_PENDING_ERROR;return NULL;
    }
    if(!retained||*retained||active_run){
        *refusal=FRIDAY_INVOKE_INVALID_OWNER_STORAGE;return NULL;
    }
    if(!b||!entry||!args||!b->root_fact||!b->qualification||!b->before||
       !b->current_enrollment_matches||!b->existing_envelope||!b->final_fd_row||
       !b->final_fd_credit||!b->held_root_tool_preimage||
       !b->original_master_pool||!FridayPublisherMasterOwns(b->original_master_pool)){
        *refusal=FRIDAY_INVOKE_MISSING_BINDINGS;return NULL;
    }
    if(!b->already_owned_run||b->already_owned_run->started){
        *refusal=FRIDAY_INVOKE_INVALID_OWNER_STORAGE;return NULL;
    }
    if(!PyBytes_CheckExact(b->held_root_tool_preimage)||!PyCallable_Check(entry)||
       !PyTuple_CheckExact(args)){
        *refusal=FRIDAY_INVOKE_INVALID_ARGUMENT_TYPES;return NULL;
    }
    if(b->final_fd<0||b->document_limit!=2000000||b->body_limit!=80000000||
       b->root_ram_remaining>8589934592ULL||
       b->root_ram_remaining<sizeof(FridayPublisherOwnedRun)+16384||
       !b->event_limit||b->event_limit>262144){
        *refusal=FRIDAY_INVOKE_INVALID_LIMITS;return NULL;
    }
    if(b->current_enrollment_matches(b->root_fact,b->qualification)!=1){
        *refusal=FRIDAY_INVOKE_ENROLLMENT_REFUSED;return NULL;
    }
    if(PyErr_Occurred()){
        *refusal=FRIDAY_INVOKE_PENDING_ERROR;return NULL;
    }
    /* SAME original caller owns this storage before debit, module allocation
     * and Source invocation. Not a Source-born owner or late native calloc. */
    FridayPublisherOwnedRun *r=b->already_owned_run;
    *retained=r;r->started=1;r->binding_storage=*b;r->bindings=&r->binding_storage;r->owner_pid=getpid();
    r->source_entry=Py_NewRef(entry);r->source_args=Py_NewRef(args);
    Py_INCREF(b->root_fact);Py_INCREF(b->qualification);Py_INCREF(b->held_root_tool_preimage);
    Py_INCREF(b->final_fd_row);Py_INCREF(b->final_fd_credit);
    if(b->before(b->existing_envelope,0,0,0,sizeof(*r)+16384)<0) {
        save_error_cell(&r->prefix_error,"before-Source-debit");return NULL;
    }
    r->charged_allocation=sizeof(*r)+16384;
    r->anchors=PyDict_New();r->banks=PyDict_New();r->errors=PyList_New(0);
    r->capsule=PyCapsule_New(r,CAPSULE_NAME,NULL);
    if(!r->anchors||!r->banks||!r->errors||!r->capsule) {
        save_error_cell(&r->prefix_error,"before-Source-object-allocation");return NULL;
    }
    /* Register this actual builtin, not an importable Source module substitute. */
    PyObject *mods=PyImport_GetModuleDict();
    PyObject *old=PyDict_GetItemString(mods,"publisher_owned_custody");
    if(old && PyModule_GetDef(old)!=&module){bad("actual_native_module_binding_collision");save_error_cell(&r->prefix_error,"before-Source-module-collision");return NULL;}
    r->registered_module=old?Py_NewRef(old):PyInit_publisher_owned_custody();
    if(!r->registered_module||PyDict_SetItemString(mods,"publisher_owned_custody",r->registered_module)<0) {
        save_error_cell(&r->prefix_error,"before-Source-module-registration");return NULL;
    }
    /* Same native parent retains context before first selected Python call. */
    active_run=r;
    PyObject *result=PyObject_CallObject(entry,args);
    if(!result&&PyErr_Occurred()) {
        save_error_cell(&r->prefix_error,"actual-Source-entry-failure");retain_pending_error(r);
    }
    if(!result){active_run=NULL;return NULL;}
    /* Actual same original native caller frame retains the Source return before
     * any packet allocation or reference retirement. Failures keep this Run. */
    r->source_return=Py_NewRef(result);
    r->caller_packet=prepare_caller_packet(r);
    PyObject *receiver=r->anchors?PyDict_GetItemString(r->anchors,"performing-receiver"):NULL;
    PyObject *verified=(r->caller_packet&&receiver)?
        PyObject_CallMethod(receiver,"verify_native_caller_packet","(O)",r->caller_packet):NULL;
    if(!verified||verified!=r->caller_packet||caller_packet_current(r)<0||
       r->bindings->current_enrollment_matches(r->bindings->root_fact,r->source_qualification)!=1||PyErr_Occurred()) {
        if(!PyErr_Occurred())bad("actual_native_caller_transfer_consumer_not_confirmed");
        if(!r->prefix_error.saved)save_error_cell(&r->prefix_error,"native-caller-transfer-before-retirement");
        Py_XDECREF(verified);Py_DECREF(result);active_run=NULL;return NULL;
    }
    Py_DECREF(verified);r->caller_packet_verified=1;
    /* Strong caller-frame custody precedes clearing active Source context.
     * No Source/user callback, allocation, or fallible publication follows the
     * confirmed reference retirement. Native packet raw bytes stay full/owned. */
    PyObject *caller_result=Py_NewRef(r->final_caller_packet);
    Py_DECREF(result);active_run=NULL;
    if(FridayPublisherOwnedRelease(retained)<0) {
        if(!r->prefix_error.saved)save_error_cell(&r->prefix_error,"native-caller-release-before-retirement");
        Py_DECREF(caller_result);return NULL;
    }
    return caller_result;
}
int FridayPublisherOwnedRelease(FridayPublisherOwnedRun **retained) {
    FridayPublisherOwnedRun *r=retained?*retained:NULL;
    if(!r||active_run||r->owner_pid!=getpid()||!r->final_close_confirmed||
       !r->caller_packet_verified||!r->caller_packet||!r->final_caller_packet||r->release_attempted||
       r->references_retired||caller_packet_current(r)<0)
        return bad("native_caller_reference_retirement_not_ready");
    PyObject *packet=Py_NewRef(r->final_caller_packet);
    PyObject *status=PyTuple_GetItem(packet,11),*early=PyTuple_GetItem(r->caller_packet,11);
    if(!PyTuple_CheckExact(status)||PyTuple_Size(status)!=2||
       !PyTuple_CheckExact(early)||PyTuple_Size(early)!=2||
       PyTuple_GetItem(status,0)!=PyTuple_GetItem(early,0)||
       PyTuple_GetItem(status,1)!=Py_True||PyTuple_GetItem(early,1)!=Py_False) {
        Py_DECREF(packet);return bad("native_caller_retirement_status_before_effect");
    }
    for(int i=0;i<13;i++)if(i!=11&&PyTuple_GetItem(packet,i)!=PyTuple_GetItem(r->caller_packet,i)) {
        Py_DECREF(packet);return bad("private_final_packet_not_same_full_skeleton");
    }
    r->release_attempted=1;
    /* Every cleared value has an actual independent strong reference in packet
     * roots/banks/closes BEFORE this phase. No arbitrary destructor can be its
     * last-reference cleanup here. Fields are nulled, not left dangling. */
    Py_CLEAR(r->anchors);Py_CLEAR(r->banks);Py_CLEAR(r->errors);Py_CLEAR(r->result);
    Py_CLEAR(r->final_raw);Py_CLEAR(r->source_end);Py_CLEAR(r->capsule);
    Py_CLEAR(r->source_qualification);Py_CLEAR(r->registered_module);Py_CLEAR(r->pending_close_row);
    Py_CLEAR(r->binding_storage.root_fact);Py_CLEAR(r->binding_storage.qualification);
    Py_CLEAR(r->binding_storage.held_root_tool_preimage);
    Py_CLEAR(r->binding_storage.final_fd_row);Py_CLEAR(r->binding_storage.final_fd_credit);
    NativeCloseRecord *q=r->close_records;r->close_records=NULL;
    while(q){NativeCloseRecord *next=q->next;Py_DECREF(q->row);free(q);q=next;}
    Py_CLEAR(r->source_entry);Py_CLEAR(r->source_args);Py_CLEAR(r->source_return);
    Py_CLEAR(r->prefix_error.error_type);Py_CLEAR(r->prefix_error.error_value);
    Py_CLEAR(r->prefix_error.error_tb);Py_CLEAR(r->prefix_error.actual_document);
    Py_CLEAR(r->prefix_error.accepted_cut);
    Py_CLEAR(r->after_document_error.error_type);Py_CLEAR(r->after_document_error.error_value);
    Py_CLEAR(r->after_document_error.error_tb);Py_CLEAR(r->after_document_error.actual_document);
    Py_CLEAR(r->after_document_error.accepted_cut);Py_CLEAR(r->caller_packet);
    Py_CLEAR(r->source_error);Py_CLEAR(r->source_error_phase);
    Py_CLEAR(r->source_error_tb);Py_CLEAR(r->source_error_record);
    Py_CLEAR(r->final_caller_packet);
    r->bindings=NULL;r->references_retired=1;
    /* Allocation-free final publication: only return the genuinely private
     * already-built packet. Old Source/document/skeleton/status stay unchanged. */
    *retained=NULL;Py_DECREF(packet);return 0;
}

static FridayPublisherFailureScalars failure_scalars(FridayPublisherErrorCell *e) {
    FridayPublisherFailureScalars s={e->phase,e->saved,e->syscall_attempted,
        e->syscall_rc,e->syscall_errno,e->actual_written};return s;
}
int FridayPublisherOwnedHandback(FridayPublisherOwnedRun **retained,
    FridayPublisherFailureHandback *f) {
    FridayPublisherOwnedRun *r=retained?*retained:NULL;
    /* NO exception creation on refusal: caller already holds exact first
     * triple. Keep every original in Run and report STOP_UNCONFIRMED. */
    if(!r||!f||f->received||active_run||r->owner_pid!=getpid()||
       r->references_retired||r->release_attempted)return -1;
    if(r->final_caller_packet) {
        PyObject *early=r->caller_packet,*final=r->final_caller_packet;
        if(!early||!PyTuple_CheckExact(early)||PyTuple_Size(early)!=13||
           !PyTuple_CheckExact(final)||PyTuple_Size(final)!=13)return -1;
        for(int i=0;i<13;i++)if(i!=11&&PyTuple_GetItem(early,i)!=PyTuple_GetItem(final,i))return -1;
        /* Never hand an optimistic private True packet to an error consumer.
         * Its other 12 refs remain independently strong in the False skeleton;
         * its status owns only exact native pid/True scalar builtins. This
         * builtin-only tuple cleanup cannot be a last Source-object decref. */
        PyObject *status=PyTuple_GetItem(final,11),*old=PyTuple_GetItem(early,11);
        if(!PyTuple_CheckExact(status)||PyTuple_Size(status)!=2||
           !PyTuple_CheckExact(old)||PyTuple_Size(old)!=2||
           !PyLong_CheckExact(PyTuple_GetItem(status,0))||
           PyTuple_GetItem(status,0)!=PyTuple_GetItem(old,0)||
           PyTuple_GetItem(status,1)!=Py_True||PyTuple_GetItem(old,1)!=Py_False)return -1;
        Py_CLEAR(r->final_caller_packet);
    }
    f->owner_pid=r->owner_pid;f->close_count=r->close_count;
    f->charged_allocation=r->charged_allocation;f->bank_total=r->bank_total;
    f->prefix=failure_scalars(&r->prefix_error);
    f->after_document=failure_scalars(&r->after_document_error);
    f->end_attempted=r->end_attempted;f->final_close_attempted=r->final_close_attempted;
    f->final_close_confirmed=r->final_close_confirmed;
    f->pending_close_rc=r->pending_close_rc;f->pending_close_errno=r->pending_close_errno;
    f->pending_close_published=r->pending_close_published;
    /* Ownership MOVE: full bank dictionary includes every unsealed partial
     * part, alias and raw byte object; no late tuple/codec allocation needed.
     * No INCREF/DECREF, destructor, Source callback or syscall in this phase. */
#define MOVE_ROOT(index,field) do { f->roots[index]=r->field;r->field=NULL; } while(0)
    MOVE_ROOT(0,anchors);MOVE_ROOT(1,banks);MOVE_ROOT(2,errors);MOVE_ROOT(3,result);
    MOVE_ROOT(4,final_raw);MOVE_ROOT(5,source_end);MOVE_ROOT(6,capsule);
    MOVE_ROOT(7,source_qualification);MOVE_ROOT(8,registered_module);MOVE_ROOT(9,pending_close_row);
    MOVE_ROOT(10,source_entry);MOVE_ROOT(11,source_args);
    MOVE_ROOT(12,binding_storage.root_fact);MOVE_ROOT(13,binding_storage.qualification);
    MOVE_ROOT(14,binding_storage.held_root_tool_preimage);
    MOVE_ROOT(15,binding_storage.final_fd_row);MOVE_ROOT(16,binding_storage.final_fd_credit);
    MOVE_ROOT(17,prefix_error.error_type);MOVE_ROOT(18,prefix_error.error_value);
    MOVE_ROOT(19,prefix_error.error_tb);MOVE_ROOT(20,prefix_error.actual_document);
    MOVE_ROOT(21,prefix_error.accepted_cut);
    MOVE_ROOT(22,after_document_error.error_type);MOVE_ROOT(23,after_document_error.error_value);
    MOVE_ROOT(24,after_document_error.error_tb);MOVE_ROOT(25,after_document_error.actual_document);
    MOVE_ROOT(26,after_document_error.accepted_cut);MOVE_ROOT(27,source_return);
    MOVE_ROOT(28,source_error);MOVE_ROOT(29,source_error_phase);
    MOVE_ROOT(30,source_error_tb);MOVE_ROOT(31,source_error_record);
#undef MOVE_ROOT
    f->caller_packet=r->caller_packet;r->caller_packet=NULL;
    f->close_records=r->close_records;r->close_records=NULL;
    r->bindings=NULL;r->references_retired=1;*retained=NULL;
    f->received=1;f->ownership_retired=0;
    /* Run ownership moved to ACTUAL same caller, not all aliases retired.
     * Caller remains full owner of uncertain endpoints. Never repeat close,
     * write/fsync, enroll, or retry Source; final cleanup still unconfirmed. */
    return 0;
}

int FridayPublisherFailureCloseAt(const FridayPublisherFailureHandback *f,
    uint64_t index,FridayPublisherFailureCloseView *view) {
    if(!f||!f->received||!view||f->close_count>65536)return -1;
    if(index>=f->close_count)return 0;
    NativeCloseRecord *q=f->close_records;
    for(uint64_t i=0;i<index&&q;i++)q=q->next;
    if(!q)return -1;
    view->actual_row=q->row;view->actual_called=q->actual_called;
    view->owner_slot=&q->row;
    view->syscall_rc=q->syscall_rc;view->syscall_errno=q->syscall_errno;
    view->publication_confirmed=q->publication_confirmed;
    return 1;
}
int FridayPublisherFailureCloseNext(const FridayPublisherFailureHandback *f,
    NativeCloseRecord **cursor,uint64_t *seen,FridayPublisherFailureCloseView *view) {
    if(!f||!f->received||!cursor||!seen||!view||f->close_count>65536||*seen>f->close_count)return -1;
    if(*seen==0)*cursor=f->close_records;
    if(*seen==f->close_count)return *cursor?-1:0;
    NativeCloseRecord *q=*cursor;if(!q)return -1;
    view->actual_row=q->row;view->owner_slot=&q->row;
    view->actual_called=q->actual_called;view->syscall_rc=q->syscall_rc;
    view->syscall_errno=q->syscall_errno;view->publication_confirmed=q->publication_confirmed;
    *cursor=q->next;(*seen)++;return 1;
}
int FridayPublisherFailureRetireMovedCloseRecords(FridayPublisherFailureHandback *f) {
    if(!f||!f->received||f->close_count>65536)return -1;
    NativeCloseRecord *q=f->close_records;uint64_t n=0;
    for(;q;q=q->next) {
        if(q->row||n>=f->close_count)return -1;n++;
    }
    if(n!=f->close_count)return -1;
    q=f->close_records;f->close_records=NULL;
    while(q) {NativeCloseRecord *next=q->next;free(q);q=next;}
    return 0;
}
