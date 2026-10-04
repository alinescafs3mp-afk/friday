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
/* Native-only preflight result in already owned caller storage. A refusal
 * is not a Python exception, an authority grant, or a completed caller end. */
typedef enum {
    FRIDAY_INVOKE_PREFLIGHT_OK=0,
    FRIDAY_INVOKE_PENDING_ERROR=1,
    FRIDAY_INVOKE_INVALID_OWNER_STORAGE=2,
    FRIDAY_INVOKE_MISSING_BINDINGS=3,
    FRIDAY_INVOKE_INVALID_ARGUMENT_TYPES=4,
    FRIDAY_INVOKE_INVALID_LIMITS=5,
    FRIDAY_INVOKE_ENROLLMENT_REFUSED=6
} FridayPublisherInvokeRefusal;
typedef int (*FridayPublisherDebit)(void *,uint64_t,uint64_t,uint64_t,uint64_t);
/* NEW concrete same-role Root entry owns this cookie, pool, and native FD
 * table before Source. The cookie is never constructed by a Python object. */
typedef struct FridayPublisherMasterPool FridayPublisherMasterPool;
typedef struct FridayPublisherPreparedRow FridayPublisherPreparedRow;
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
    FridayPublisherMasterPool *original_master_pool;
    PyObject *preowned_refusal_type,*preowned_refusal_value;
    int final_fd;
    PyObject *final_fd_row,*final_fd_credit;
    uint64_t root_ram_remaining,document_limit,body_limit,event_limit;
    /* Exactly 1 is confirmation. Zero, negative and other values refuse.
     * A pending original Python error also prevents confirmation. */
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
    PyObject *source_entry,*source_args,*source_return,*caller_packet;
    /* Private final packet. NEVER sent to a Source callback. Constructed while
     * all Run references are live; returned only after successful Release. */
    PyObject *final_caller_packet;
    int caller_packet_verified,release_attempted,references_retired;
    FridayPublisherErrorCell prefix_error,after_document_error;
};
/* The actual old caller invokes this performing Source bridge; no new main.
 * Provisioning already_owned_run is a change to the SAME original caller's
 * admitted ABI, not permission from this Source text or a receipt boolean. */
/* Invoke returns the full owned caller packet v1, NOT the early Source return.
 * Tuple13: schema, original Source return, pre-retirement native result,
 * full immutable v2 document, actual Source end, tuple of full bank records,
 * tuple28 of every Run-owned PyObject root, tuple of actual close records,
 * original Root fact, original Root qualification, actual Source qualification,
 * native retirement tuple(owner_pid, references_retired), cost scalars.
 * The receiver sees only a separate False skeleton. The True final tuple is
 * private until reference retirement; neither immutable tuple is mutated.
 * A bank record is (actual key, full immutable raw bytes, actual aliases tuple,
 * full original parts tuple). Retention is actual strong custody, not hashes.
 * Current same-role caller/image/ABI/consumer qualification remains NOT_RUN.
 * On failure the same retained Run owns every uncertain/partial value; no retry.
 */
/* The actual C caller supplies its PREOWNED refusal cell. Initial validation
 * returns NULL plus this exact discriminant without allocating a RuntimeError.
 * Any original provider exception is left pending for the caller's full
 * triple capture. After preflight OK, ordinary failures still return NULL
 * with retained Run ownership. This C API is not a Python-callable function:
 * NULL does not imply that a new Python exception was manufactured.
 * No storage debit is refunded and no missing original provider is replaced. */
PyObject *FridayPublisherOwnedInvoke(const FridayPublisherBindings *,
    PyObject *,PyObject *,FridayPublisherOwnedRun **,FridayPublisherInvokeRefusal *);
int FridayPublisherOwnedRelease(FridayPublisherOwnedRun **);
/* Exact linked module definition, not __name__ or a Source bool. */
int FridayPublisherOwnedModuleOriginal(PyObject *);
PyMODINIT_FUNC PyInit_publisher_owned_custody(void);
#endif
