/* Inert, selected own-object ABI of SAME enrolled Publisher Root.
 * No executable, role, grant, private heap walk, or Source constructor.
 * Independent original caller/image/ABI/cost qualification REQUIRED_NOT_RUN.
 */
#ifndef FRIDAY_PUBLISHER_OWNED_CUSTODY_H
#define FRIDAY_PUBLISHER_OWNED_CUSTODY_H
#include <Python.h>
#include <stdint.h>
#include <sys/types.h>
typedef struct FridayPublisherOwnedRun FridayPublisherOwnedRun;
typedef struct NativeCloseRecord NativeCloseRecord;
typedef int (*FridayPublisherDebit)(void *,uint64_t,uint64_t,uint64_t,uint64_t);
typedef struct {
    /* Original caller reserves this scalar/reference cell BEFORE any selected
     * module/Source allocation. Full originals, not tags or a guessed pointer. */
    const char *phase;
    PyObject *error_type,*error_value,*error_tb,*actual_document,*accepted_cut;
    int saved,syscall_attempted,syscall_rc,syscall_errno;
    uint64_t actual_written;
} FridayPublisherErrorCell;
typedef struct {
    PyObject *root_fact,*qualification,*held_root_tool_preimage;
    void *existing_envelope;
    FridayPublisherDebit before;
    int final_fd;
    PyObject *final_fd_row,*final_fd_credit;
    uint64_t root_ram_remaining,document_limit,body_limit,event_limit;
    int (*current_enrollment_matches)(PyObject *,PyObject *);
    /* Prospectively initialized zero storage INSIDE existing original Root.
     * Not a late calloc after its first ordinary allocation failure.
     * Original caller must retain this exact storage on every uncertain end.
     * Source never passes or initializes this structure. */
    FridayPublisherOwnedRun *already_owned_run;
} FridayPublisherBindings;
struct FridayPublisherOwnedRun {
    FridayPublisherBindings binding_storage;
    const FridayPublisherBindings *bindings;
    pid_t owner_pid;
    PyObject *capsule,*anchors,*banks,*errors,*result,*final_raw,*source_end,*source_qualification;
    PyObject *registered_module,*pending_close_row;
    int pending_close_rc,pending_close_errno,pending_close_published;
    NativeCloseRecord *close_records;
    uint64_t close_count,serial,charged_allocation,bank_total;
    int end_attempted,final_close_attempted,final_close_confirmed,started;
    PyObject *source_entry,*source_args;
    FridayPublisherErrorCell prefix_error,after_document_error;
};
/* The actual old caller invokes this performing Source bridge; no new main.
 * Provisioning already_owned_run is a change to the SAME original caller's
 * admitted ABI, not permission from this Source text or a receipt boolean. */
PyObject *FridayPublisherOwnedInvoke(const FridayPublisherBindings *,
    PyObject *,PyObject *,FridayPublisherOwnedRun **);
int FridayPublisherOwnedRelease(FridayPublisherOwnedRun **);
PyMODINIT_FUNC PyInit_publisher_owned_custody(void);
#endif
