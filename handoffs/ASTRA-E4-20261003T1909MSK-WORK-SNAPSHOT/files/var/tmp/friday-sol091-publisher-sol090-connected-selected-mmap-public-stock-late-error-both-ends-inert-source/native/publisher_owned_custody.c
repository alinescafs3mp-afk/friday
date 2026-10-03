/* Same-process selected Publisher native ownership; never executed by author. */
#define PY_SSIZE_T_CLEAN
#include "publisher_owned_custody.h"
#include <unistd.h>
#include <errno.h>
#include <string.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <limits.h>

#define CAPSULE_NAME "friday.publisher.existing-root-owned-run.v1"
struct NativeCloseRecord {
    PyObject *row;
    int actual_called, syscall_rc, syscall_errno, publication_confirmed;
    struct NativeCloseRecord *next;
};
static _Thread_local FridayPublisherOwnedRun *active_run;

static int bad(const char *why) { PyErr_SetString(PyExc_RuntimeError,why); return -1; }
static FridayPublisherOwnedRun *context(PyObject *capsule) {
    FridayPublisherOwnedRun *r=PyCapsule_GetPointer(capsule,CAPSULE_NAME);
    if (!r) return NULL;
    if (r != active_run || r->owner_pid != getpid() || !r->bindings ||
        !r->bindings->current_enrollment_matches(r->bindings->root_fact,r->bindings->qualification)) {
        bad("actual_same_process_enrolled_native_owner"); return NULL;
    }
    return r;
}
static int debit(FridayPublisherOwnedRun *r,uint64_t reads,uint64_t output,
                 uint64_t hash,uint64_t allocation) {
    if (r->charged_allocation>r->bindings->root_ram_remaining ||
        allocation > r->bindings->root_ram_remaining-r->charged_allocation)
        return bad("original_native_parent_prepaid_RAM");
    if (r->bindings->before(r->bindings->existing_envelope,reads,output,hash,allocation) < 0)
        return -1;
    r->charged_allocation += allocation;
    return 0;
}
static int hold(FridayPublisherOwnedRun *r,const char *key,PyObject *value) {
    if (PyDict_GetItemString(r->anchors,key)) return bad("native_owned_anchor_once");
    if ((uint64_t)PyDict_Size(r->anchors)>=r->bindings->event_limit)
        return bad("original_native_parent_prepaid_anchor_count");
    if (debit(r,0,0,0,512+strlen(key))<0) return -1;
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
static PyObject *error_cell_cut(FridayPublisherErrorCell *cell) {
    return Py_BuildValue("{s:s,s:O,s:O,s:O,s:O,s:i,s:i,s:i,s:i,s:K}",
        "phase",cell->phase?cell->phase:"NOT_ATTEMPTED",
        "original_type",cell->error_type?cell->error_type:Py_None,
        "original_error",cell->error_value?cell->error_value:Py_None,
        "original_traceback",cell->error_tb?cell->error_tb:Py_None,
        "actual_document",cell->actual_document?cell->actual_document:Py_None,
        "saved",cell->saved,"syscall_attempted",cell->syscall_attempted,
        "syscall_rc",cell->syscall_rc,"syscall_errno",cell->syscall_errno,
        "actual_written",(unsigned long long)cell->actual_written);
}
static int retain_pending_error(FridayPublisherOwnedRun *r) {
    PyObject *t=NULL,*v=NULL,*tb=NULL;
    PyErr_Fetch(&t,&v,&tb);
    PyErr_NormalizeException(&t,&v,&tb);
    if (!v) { Py_XDECREF(t);Py_XDECREF(tb);return -1; }
    if (tb && PyException_SetTraceback(v,tb)<0) PyErr_Clear();
    /* Full original exception/traceback, NOT str(), error_fact(), hash or tag. */
    int rc=r->errors?PyList_Append(r->errors,v):-1;
    PyErr_Restore(t,v,tb);
    return rc;
}
static PyObject *py_current(PyObject *self,PyObject *ignored) {
    if (!active_run) Py_RETURN_NONE;
    return Py_NewRef(active_run->capsule);
}
static PyObject *py_prefix_envelope(PyObject *self,PyObject *cap) {
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(debit(r,0,0,0,8192)<0)return NULL;
    PyObject *error=error_cell_cut(&r->prefix_error);
    PyObject *cut=error?Py_BuildValue("{s:s,s:l,s:O,s:O,s:O,s:O,s:O}",
        "schema","friday.sol091.actual-before-Source-native-prefix.v1",
        "owner_pid",(long)r->owner_pid,"actual_entry",r->source_entry,
        "actual_args",r->source_args,"held_original_root_image",r->bindings->held_root_tool_preimage,
        "actual_error_cell",error,"original_final_row",r->bindings->final_fd_row):NULL;
    Py_XDECREF(error);return cut;
}
static PyObject *py_after_document_cell(PyObject *self,PyObject *cap) {
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    if(!r->after_document_error.saved)Py_RETURN_NONE;
    if(debit(r,0,0,0,8192)<0)return NULL;
    PyObject *cut=error_cell_cut(&r->after_document_error);
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
    if(count>UINT64_MAX/6 || debit(r,count*3,0,count*2,count*6+128)<0)return NULL;
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
    if(PyErr_Occurred()||n>PY_SSIZE_T_MAX||debit(r,n,0,n,n+128)<0)return NULL;
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
    /* Only this prospectively original native row is authenticated by the
     * supplied existing caller. A Source-created credit.fd_rows dictionary is
     * NOT an independent native table/authority proof for any other numeric FD.
     * Remaining Source prepared rows require the exact original native table
     * supplier, not a Source-issued enrollment API or guessed row shape. */
    if(row!=r->bindings->final_fd_row||credit!=r->bindings->final_fd_credit) {
        bad("original_native_prepared_row_supplier_NOT_PRESENT_CODE");return NULL;
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
    PyObject *fdrows=PyObject_GetAttrString(credit,"fd_rows");
    if(PyErr_Occurred()||fd<0||fd>INT_MAX||fd!=r->bindings->final_fd||!fdrows||!slot||
       !PyDict_CheckExact(fdrows)||PyDict_GetItemWithError(fdrows,slot)!=row) {
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
    int rc=close((int)fd),saved=errno; /* one syscall; no EINTR/EBADF retry */
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
    s=PyUnicode_FromString(rc==0?"CLOSED":"UNKNOWN");
    if(!s||PyDict_SetItemString(row,"status",s)<0||
       PyDict_SetItemString(row,"attempted_close",s)<0||PyDict_SetItemString(attempt,"status",s)<0) {
        Py_XDECREF(s);Py_DECREF(fdrows);return NULL;
    }
    Py_DECREF(s);r->pending_close_published=1;record->publication_confirmed=1;
    if(rc==0){int del=PyDict_DelItem(fdrows,slot);Py_DECREF(fdrows);if(del<0)return NULL;Py_RETURN_NONE;}
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
        "ownership_retired",Py_True,"actual_full_document",raw,
        "actual_source_end",source_end,"actual_final_FD_row",r->bindings->final_fd_row,
        "GO",Py_False);
    if(!r->result)return final_failure(r,"final-result-after-actual-close");
    return Py_NewRef(r->result);
}
static PyObject *py_outside_end(PyObject *self,PyObject *args) {
    PyObject *cap,*raw,*end;
    if(!PyArg_ParseTuple(args,"OOO",&cap,&raw,&end))return NULL;
    FridayPublisherOwnedRun *r=context(cap);if(!r)return NULL;
    return PyBool_FromLong(r->end_attempted&&r->final_close_confirmed&&r->final_raw==raw&&r->source_end==end&&r->result);
}

/* Exact owned struct/public API export, not an interpreter/private-heap walk.
 * Mutable anchors/bank lists are captured as a finite before-effect cut; later
 * changes remain actual native events/banks and are captured at the next cut. */
static PyObject *py_owned_state(PyObject *self,PyObject *cap) {
    FridayPublisherOwnedRun *r=PyCapsule_GetPointer(cap,CAPSULE_NAME);
    if(!r||r!=active_run){bad("actual_selected_owned_context");return NULL;}
    if(r->owner_pid==getpid() && debit(r,0,0,0,131072+(uint64_t)PyDict_Size(r->anchors)*1024)<0)return NULL;
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
        "schema","friday.sol091.selected-owned-native-public-state.v3","owner_pid",(long)r->owner_pid,
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
        PyObject *prefix=error_cell_cut(&r->prefix_error),*late=error_cell_cut(&r->after_document_error);
        if(!prefix||!late||PyDict_SetItemString(state,"prefix_error_cell",prefix)<0||
           PyDict_SetItemString(state,"after_document_error_cell",late)<0) {
            Py_XDECREF(prefix);Py_XDECREF(late);Py_DECREF(state);return NULL;
        }
        Py_DECREF(prefix);Py_DECREF(late);
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
    if(!r->bindings->current_enrollment_matches(fact,qualification)){
        bad("current_native_existing_Root_Source_qualification");return NULL;
    }
    if(r->source_qualification){bad("source_qualification_once");return NULL;}
    r->source_qualification=Py_NewRef(qualification);Py_RETURN_NONE;
}

static PyMethodDef methods[]={
    {"prefix_envelope",py_prefix_envelope,METH_O,NULL},
    {"after_document_cell",py_after_document_cell,METH_O,NULL},
    {"accept_after_document",py_accept_after_document,METH_VARARGS,NULL},
    {"owned_state",py_owned_state,METH_O,NULL},{"is_context",py_is_context,METH_O,NULL},
    {"is_module",py_is_module,METH_O,NULL},{"bind_source",py_bind_source,METH_VARARGS,NULL},
    {"current",py_current,METH_NOARGS,NULL},{"root_fact",py_get_fact,METH_O,NULL},
    {"qualification",py_get_qualification,METH_O,NULL},{"hold",py_hold,METH_VARARGS,NULL},
    {"get_anchor",py_get_anchor,METH_VARARGS,NULL},{"matches",py_matches,METH_VARARGS,NULL},
    {"before_graph",py_before_graph,METH_VARARGS,NULL},{"begin_bank",py_begin_bank,METH_VARARGS,NULL},
    {"before_bytes",py_before_bytes,METH_VARARGS,NULL},{"append_bytes",py_append_bytes,METH_VARARGS,NULL},
    {"finish_bank",py_finish_bank,METH_VARARGS,NULL},{"read_bank",py_read_bank,METH_VARARGS,NULL},
    {"close_owned_row",py_close_owned_row,METH_VARARGS,NULL},
    {"before_document",py_before_document,METH_VARARGS,NULL},{"finish_outside",py_finish_outside,METH_VARARGS,NULL},
    {"outside_end",py_outside_end,METH_VARARGS,NULL},{NULL,NULL,0,NULL}};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,"publisher_owned_custody",NULL,-1,methods};
PyMODINIT_FUNC PyInit_publisher_owned_custody(void){return PyModule_Create(&module);}
PyObject *FridayPublisherOwnedInvoke(const FridayPublisherBindings *b,PyObject *entry,
                                     PyObject *args,FridayPublisherOwnedRun **retained) {
    if(!retained||*retained||active_run||!b||!b->already_owned_run||b->already_owned_run->started||!b->before||!b->current_enrollment_matches||
       !b->current_enrollment_matches(b->root_fact,b->qualification)||b->final_fd<0||
       b->document_limit!=2000000||b->body_limit!=80000000||b->root_ram_remaining>8589934592ULL||
       !b->root_fact||!b->qualification||!b->final_fd_row||!b->final_fd_credit||
       b->root_ram_remaining<sizeof(FridayPublisherOwnedRun)+16384||
       !b->held_root_tool_preimage||!PyBytes_CheckExact(b->held_root_tool_preimage)||
       !b->event_limit||b->event_limit>262144||!PyCallable_Check(entry)||!PyTuple_CheckExact(args)) {
        bad("actual_existing_Root_invocation_and_original_envelope");return NULL;
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
    active_run=NULL;
    return result;
}
int FridayPublisherOwnedRelease(FridayPublisherOwnedRun **retained) {
    FridayPublisherOwnedRun *r=retained?*retained:NULL;
    if(!r||active_run||r->owner_pid!=getpid()||!r->final_close_confirmed||!r->result)
        return bad("native_outside_end_not_confirmed");
    /* Explicit same existing caller endpoint; never destructor-on-error. */
    Py_XDECREF(r->anchors);Py_XDECREF(r->banks);Py_XDECREF(r->errors);
    Py_XDECREF(r->result);Py_XDECREF(r->final_raw);Py_XDECREF(r->source_end);Py_XDECREF(r->capsule);
    Py_XDECREF(r->source_qualification);Py_XDECREF(r->registered_module);Py_XDECREF(r->pending_close_row);
    Py_XDECREF(r->bindings->root_fact);Py_XDECREF(r->bindings->qualification);
    Py_XDECREF(r->bindings->held_root_tool_preimage);
    Py_XDECREF(r->bindings->final_fd_row);Py_XDECREF(r->bindings->final_fd_credit);
    NativeCloseRecord *p=r->close_records;
    while(p){NativeCloseRecord *next=p->next;Py_DECREF(p->row);free(p);p=next;}
    /* Original caller retains this explicit storage. No free/reset/reuse. */
    Py_XDECREF(r->source_entry);Py_XDECREF(r->source_args);
    Py_XDECREF(r->prefix_error.error_type);Py_XDECREF(r->prefix_error.error_value);Py_XDECREF(r->prefix_error.error_tb);
    Py_XDECREF(r->after_document_error.error_type);Py_XDECREF(r->after_document_error.error_value);Py_XDECREF(r->after_document_error.error_tb);
    Py_XDECREF(r->after_document_error.actual_document);Py_XDECREF(r->after_document_error.accepted_cut);
    *retained=NULL;return 0;
}
