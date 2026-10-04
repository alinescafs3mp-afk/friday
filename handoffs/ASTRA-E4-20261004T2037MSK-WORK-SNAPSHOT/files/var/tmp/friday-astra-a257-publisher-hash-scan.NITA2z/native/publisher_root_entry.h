/* NEW actual performing entry in the SAME enrolled Publisher Root image.
 * This is not recovered/private historical code, a new role or a service.
 * All compilation/loading/execution/admission is REQUIRED_NOT_RUN.
 */
#ifndef FRIDAY_PUBLISHER_ROOT_ENTRY_H
#define FRIDAY_PUBLISHER_ROOT_ENTRY_H
#include "publisher_original_caller.h"
#include <sys/stat.h>
#include <sys/resource.h>
#define FRIDAY_MASTER_HISTORY 262144
#define FRIDAY_NATIVE_FD_HISTORY 65536
#define FRIDAY_ROOT_HELD_FILES 262144
#define FRIDAY_ROOT_CASE_INPUT_MAX 2000000
typedef struct {
    /* Caller supplies a valid C string for the duration of the copy only.
     * This address is PRIVATE history, never dereferenced by a final reader. */
    const char *historical_input;
    uint64_t bytes,read_credit;
    int attempted,credit_reserved,bounded,complete,drift;
    char body[FRIDAY_ROOT_CASE_INPUT_MAX+1];
} FridayPublisherRootCaseInput;
typedef struct FridayPublisherRootColdResult FridayPublisherRootColdResult;
typedef struct {
    uint64_t token,reads,output,hash,allocation,slots;
    int active,transferred,source_owned;
} FridayPublisherPoolRow;
struct FridayPublisherMasterPool {
    pid_t pid;
    uint64_t started_ns,deadline_ns,work_deadline_ns;
    uint64_t spent_read,spent_output,spent_hash,native_allocation,retained_allocation;
    uint64_t observed_read,observed_output,observed_ram,observed_workers,native_live_slots;
    uint64_t next_token,count,preowner_token;
    int initialized,preowner_started,source_detached,refused,observation_unknown;
    const char *fault;
    FridayPublisherPoolRow rows[FRIDAY_MASTER_HISTORY];
    PyObject *refusal_type,*refusal_value;
};
/* Before means prospective conservative cumulative debit, NEVER a measured
 * whole upper, issuer, refund, copied observer or fresh native-only cap. */
int FridayPublisherMasterBefore(void *,uint64_t,uint64_t,uint64_t,uint64_t);
int FridayPublisherMasterChange(FridayPublisherMasterPool *,const char *,uint64_t,
    uint64_t,uint64_t,uint64_t,uint64_t,uint64_t,uint64_t *);
int FridayPublisherMasterFault(FridayPublisherMasterPool *,const char *);
PyObject *FridayPublisherMasterState(FridayPublisherMasterPool *);
int FridayPublisherMasterOwns(FridayPublisherMasterPool *);
/* SAME original pool, before copying or converting the native call argument.
 * The existing 2M independent ordinary-input wire bounds its case identifier;
 * this is not a selected-case graph/whole-cost witness or a new pool. */
int FridayPublisherRootCaseCopy(FridayPublisherMasterPool *,
    FridayPublisherRootCaseInput *,const char *);
/* Actual cold command creates this SAME pool before CPython preinitialization.
 * No Python API, copied budget, refreshed clock, or Source-issued capability.
 * The returned pointer is the owned RootStorage pool, not caller storage. */
int FridayPublisherRootColdPool(FridayPublisherRootColdResult *,FridayPublisherMasterPool **);
/* Actual cold caller, after its own successful init and before RootPerform.
 * Binds the SAME private cold pool to current main interpreter/thread.
 * This does not enroll/admit Source or adopt an external runtime. */
int FridayPublisherRootColdRuntimeBind(FridayPublisherMasterPool *);
int FridayPublisherPreparedCreate(FridayPublisherMasterPool *,int,const char *,
    PyObject *,PyObject *);
int FridayPublisherPreparedMatches(FridayPublisherMasterPool *,PyObject *,PyObject *,int);
int FridayPublisherPreparedHasRow(FridayPublisherMasterPool *,PyObject *);
int FridayPublisherPreparedKeeperClose(FridayPublisherMasterPool *,PyObject *);
PyObject *FridayPublisherPreparedState(FridayPublisherMasterPool *);
PyObject *FridayPublisherRootBootstrapSignature(FridayPublisherMasterPool *,PyObject *,PyObject *,PyObject *);
/* SOL105 paired producers; exact original preowned Root, no Source issuer. */
PyObject *FridayPublisherRootOwnConsumer(PyObject *);
PyObject *FridayPublisherRootOwnBinding(PyObject *);
PyObject *FridayPublisherRootOwnBindingCheck(PyObject *);
PyObject *FridayPublisherRootOwnHeldBodyCheck(PyObject *);
PyObject *FridayPublisherRootOwnFunction(PyObject *);
PyObject *FridayPublisherRootOwnClassBody(PyObject *);
PyObject *FridayPublisherRootOwnSupport(PyObject *);
PyObject *FridayPublisherRootOwnSupportCheck(PyObject *);
PyObject *FridayPublisherRootOwnPrepare(PyObject *);
PyObject *FridayPublisherRootOwnVar(PyObject *);
PyObject *FridayPublisherRootOwnSet(PyObject *);
PyObject *FridayPublisherRootOwnReset(PyObject *);
PyObject *FridayPublisherRootOwnContextBody(PyObject *);
PyObject *FridayPublisherRootOwnMapping(PyObject *);
PyObject *FridayPublisherRootOwnHashNew(PyObject *);
PyObject *FridayPublisherRootOwnHashUpdate(PyObject *);
PyObject *FridayPublisherRootOwnHashBody(PyObject *);
/* Full BOTH-outcome reader for the existing Root terminal, not a grant. */
PyObject *FridayPublisherRootBootstrapOutcome(FridayPublisherMasterPool *);
typedef enum {
    FRIDAY_ROOT_READBACK_UNCONFIRMED=0,
    FRIDAY_ROOT_READBACK_FINAL_PACKET=1,
    FRIDAY_ROOT_READBACK_FAILURE_HANDBACK=2,
    FRIDAY_ROOT_READBACK_BEFORE_RUN=3
} FridayPublisherRootReadback;
typedef enum {
    FRIDAY_ROOT_ENTRY_OK=0,
    FRIDAY_ROOT_ENTRY_RUNTIME_UNINITIALIZED=1,
    FRIDAY_ROOT_ENTRY_PENDING_ERROR=2,
    FRIDAY_ROOT_ENTRY_MISSING_CASE=3,
    FRIDAY_ROOT_ENTRY_CLOCK_UNAVAILABLE=4
} FridayPublisherRootEntryRefusal;
typedef struct {
    FridayPublisherCallerResult result; /* actual full MOVE, not a Run pointer */
    PyObject *root_fact,*qualification,*enrollment,*admission,*role_schema;
    PyObject *entry,*args,*bootstrap_observation;
    /* SOL102: immutable native body actually read by existing Root BEFORE
     * Source, plus explicitly retained original native owner on partial/error
     * publication. Counts/pointer are not acceptance or alias retirement. */
    PyObject *bootstrap_parent_full_native,*bootstrap_parent_end_native;
    const void *bootstrap_native_storage;
    uint64_t bootstrap_parent_native_bytes_read;
    int bootstrap_parent_native_read,bootstrap_native_failure_transport_read;
    int bootstrap_stock_exec_end_unknown,bootstrap_parent_mapping_retained;
    /* Same parent lifetime phase. 0 unresolved prefix, 1 pre-exec native
     * returned, 2 public-stock process exited, 3 signalled child, 4 async
     * prefix. child_transport_end_confirmed remains only the returned-native
     * fact. Exit, EOF, ACK and signature do not set it. Independent selected
     * profile/Source/Root qualification is required BEFORE execution; this
     * runtime observation struct has no authority to issue it. */
    int bootstrap_lifetime_phase;
    int bootstrap_public_stock_observables_consumed;
    int bootstrap_public_stock_parent_legal_retirement_accounted;
    int bootstrap_child_transport_end_confirmed;
    int bootstrap_stock_exec_prefix_retained;
    int bootstrap_kernel_process_lifetime_ended;
    int bootstrap_stock_image_replacement_confirmed;
    PyObject *initial_error_type,*initial_error_value,*initial_error_tb;
    uint64_t full_banks_read,full_bank_bytes,partial_parts_read,owned_failure_roots,owned_entry_roots;
    unsigned char raw_reader_sink;
    FridayPublisherRootReadback readback_kind;
    FridayPublisherRootEntryRefusal entry_refusal;
    /* Exact first error is captured into this preowned cell before later
     * imports/allocations can replace it. Not an error codec or final end. */
    const char *initial_error_phase;
    int initial_error_capture_attempted;
    int attempted,call_attempted,received,full_readback;
    int native_end_confirmed,remaining_original_owners,SourceReady,Root_admission,GO;
    const char *phase;
    FridayPublisherMasterPool *pool; /* actual owned static pool, never copied */
} FridayPublisherRootTerminal;
/* NEW same-role command entry constructs Bindings and directly calls
 * CallOriginal. Link/command selection in the actual independently installed
 * Root image remains REQUIRED_NOT_RUN, not recovered historical invocation.
 * Full MOVE/readback is not last-alias destruction or a confirmed Root end. */
int FridayPublisherRootPerform(const char *,const FridayPublisherRootTerminal **);
/* Called by RootPerform on BOTH outcomes, not an unused forwarding API. */
int FridayPublisherCallerMoveToRoot(FridayPublisherCallerResult *);
/* SOL103 native producer close fact. Public row metadata is NEVER authority.
 * This is filled from the actual Root-owned birth/generation/OFD before and
 * immediately after the ONE destructive syscall. Publication may fail later.
 */
typedef struct {
    uint64_t generation;
    int fd,keeper,birth_valid,validation_attempted,validation_rc,validation_errno;
    int keeper_validation_attempted,keeper_validation_rc,keeper_validation_errno;
    int description_attempted,description_rc,description_errno,attempted,rc,original_errno,closed;
    struct stat birth,current,keeper_current;
} FridayPublisherRootCloseFact;
/* SAME Root's finite native utility phase. This is preowned with RootStorage
 * before any observation. The full post-runtime receipt is separate from the
 * already sealed Source receipt; private historical pointers are NOT a wire. */
#define FRIDAY_ROOT_UTILITY_COUNT 3
typedef struct {
    int kind;
    int fd,keeper;
    int acquired,live_body,live_keeper;
    int open_attempted,open_rc,open_errno;
    int keeper_open_attempted,keeper_open_rc,keeper_open_errno;
    int birth_stat_attempted,birth_stat_rc,birth_stat_errno;
    int seek_attempted,seek_errno;
    int64_t seek_result;
    int read_attempted,read_errno,read_completed,prefix_positive,body_complete;
    int refresh_refusal; /* native phase,never a fabricated syscall errno */
    ssize_t read_rc;
    uint64_t read_used,prefix_len,body_len,generation;
    uint64_t refresh_sequence,prefix_sequence,body_sequence;
    int compare_attempted,compare_rc,compare_errno;
    struct stat birth,compare_stat;
    FridayPublisherRootCloseFact body_close,keeper_close;
    char prefix[8193],body[8193];
} FridayPublisherRootUtilityEnd;
typedef struct {
    uint64_t schema,bytes,started_ns,finished_ns,full_native_bytes_read;
    uint64_t tail_read_reserved,tail_read_remaining;
    uint64_t pool_fault_bytes;
    pid_t pid;
    int attempted,sealed,last_consumer_confirmed,tail_credit_reserved,final_observation_attempted;
    int final_observation_rc,final_observation_complete;
    int final_usage_attempted,final_usage_rc,final_usage_errno,final_usage_complete;
    int clock_attempted,clock_rc,clock_errno,clock_failed,clock_fault_kind,clock_complete,copy_complete;
    int acquired_endpoints,confirmed_endpoints,uncertain_endpoints;
    int utility_FD_end_confirmed,originals_retained,complete;
    int resource_bounds_checked,resource_bounds_confirmed;
    int pool_fault_present,pool_fault_complete;
    uint64_t bound_reads,bound_output,bound_ram,bound_slots;
    const char *phase;
    struct rusage final_usage;
    char pool_fault_body[4097]; /* actual selected native cause,not its pointer */
    FridayPublisherMasterPool pool_snapshot;
    FridayPublisherRootUtilityEnd endpoints[FRIDAY_ROOT_UTILITY_COUNT];
    volatile unsigned char reader_sink;
} FridayPublisherRootUtilityReceipt;
/* Only after the actual last Source/stock/finalizer budget consumer. Uses
 * original prepaid native tail, never MasterBefore after its observer closes.
 * 0 means a retained sealed receipt, not whole Source/native/release success. */
int FridayPublisherRootUtilityFinish(int,const FridayPublisherRootUtilityReceipt **);
int FridayPublisherRootCloseOwnedRow(FridayPublisherMasterPool *,PyObject *,
    PyObject *,int,FridayPublisherRootCloseFact *);
int FridayPublisherRootOwnedRowCloseFact(FridayPublisherMasterPool *,PyObject *,
    FridayPublisherRootCloseFact *);

/* Actual SAME-Root command-owned native storage, charged in sizeof RootStorage
 * before any Root effect. These capacities are NOT new caps or proof that the
 * unchanged required workload fits. Overflow retains originals/incomplete.
 * Pointer identity is private traversal bookkeeping only: the complete native
 * value bank below contains value bytes + numeric graph edges, never pointers.
 */
#define FRIDAY_ROOT_COMMAND_OWNERS (2*FRIDAY_ROOT_HELD_FILES+3*FRIDAY_NATIVE_FD_HISTORY+8192)
#define FRIDAY_ROOT_COMMAND_NODES 262144
#define FRIDAY_ROOT_COMMAND_EDGES 1048576
#define FRIDAY_ROOT_COMMAND_BYTES 80000000
#define FRIDAY_ROOT_COMMAND_INDEX 524288

/* SOL104 data/scope are distinct. Every discovered original/new public
 * accessor value has a real native strong owner, including partial failures.
 * Runtime-support edges preserve exact aliases but do NOT assert byte-body
 * representation of arbitrary foreign runtime heaps. */
typedef struct {
    uint64_t id,kind,body_at,body_bytes,edge_at,edges,required;
    uint64_t first_parent,first_edge,data_read,support_verified;
    PyObject *original; /* OWNED strong node ref, PRIVATE, never a wire */
} FridayPublisherRootValueNode;
typedef struct {
    uint64_t group,index,node,required;
    PyObject *owned;
} FridayPublisherRootCommandOwner;
typedef struct {
    const char *phase;
    PyObject *type,*value,*tb; /* actual error; no normalization or replacement */
    int saved;
} FridayPublisherRootCommandError;
typedef struct {
    uint64_t schema,bytes;
    pid_t pid;unsigned long thread;
    int attempted,receiving,sealed,packet_typed_read,required_values_complete;
    int owner_inventory_complete,registrations_retired,owned_FDs_complete;
    int error_payload_complete,pending_error_retained,untransferred_Run;
    int bootstrap_end_unconfirmed,native_end_confirmed;
    int bootstrap_parent_mapping_retained,bootstrap_parent_bank_read;
    int runtime_finalization_eligible,SourceReady,Root_admission,GO;
    int source_registrations_retired,runtime_support_pending,cold_runtime_bound;
    int utility_support_pending; /* not a Source data FD; ends after runtime */
    int owner_retirement_attempted,owner_retirement_returned,source_owner_end_confirmed;
    uint64_t error_references_retired,remaining_error_references;
    uint64_t own_registry_rows_checked,own_registry_borrowers_cleared;
    int own_registry_borrowers_end_confirmed;
    const char *phase,*residual;
    uint64_t owner_count,node_count,edge_count,body_bytes,full_bytes_read;
    uint64_t unresolved_required_nodes,retired_owners,retained_owners;
    uint64_t close_records_read,closed_by_actual_producer,uncertain_FDs;
    uint64_t native_owner_body_at,native_owner_body_bytes;
    uint64_t producer_close_body_bytes;
    uint64_t registrations_removed,registrations_remaining;
    uint64_t active_node,graph_owned_nodes,graph_retired_nodes;
    uint64_t getters_attempted,getter_results,getter_reservation_bytes;
    uint64_t alias_edges_read,structural_nodes_read,first_unresolved_node;
    const char *getter_field,*first_unresolved_type;
    PyObject *source_module_cache[49];unsigned char source_module_checked[49];
    PyObject *source_class_cache[46];unsigned char source_class_checked[46];
    uint64_t index_probes,class_binding_checks;
    PyTypeObject *frame_locals_type; /* borrowed immutable stock type from actual frame getter */
    PyObject *getter_pending,*stock_cursor; /* exact first born values */
    Py_buffer active_buffer;int active_buffer_owned,buffer_copy_attempted,buffer_copy_rc;
    uint64_t clock_calls,clock_now_ns;
    int clock_rc,clock_errno,clock_fault_kind;
    unsigned char full_reader_sink;
    int secondary_read_attempted,secondary_payload_complete;
    uint64_t secondary_roots[3],secondary_nodes_read,secondary_first_unresolved;
    uint64_t secondary_body_at,secondary_body_bytes;
    unsigned char secondary_seen[FRIDAY_ROOT_COMMAND_NODES];
    FridayPublisherRootCommandError incoming_error,cleanup_error;
    FridayPublisherRootCommandOwner owners[FRIDAY_ROOT_COMMAND_OWNERS];
    FridayPublisherRootValueNode nodes[FRIDAY_ROOT_COMMAND_NODES];
    uint64_t edges[FRIDAY_ROOT_COMMAND_EDGES],index[FRIDAY_ROOT_COMMAND_INDEX];
    unsigned char edge_roles[FRIDAY_ROOT_COMMAND_EDGES];
    uint64_t required_queue[FRIDAY_ROOT_COMMAND_NODES];
    unsigned char body[FRIDAY_ROOT_COMMAND_BYTES];
} FridayPublisherRootCommandReceipt;
/* Accessor names the REAL preowned RootStorage receipt, not an external grant.
 * The cold command must use this exact storage, call Receive on BOTH outcomes,
 * and only then consider its OWN interpreter finalization. No Py_Finalize here.
 */
FridayPublisherRootCommandReceipt *FridayPublisherRootCommandReceiptStorage(
    const FridayPublisherRootTerminal *);
int FridayPublisherRootCommandReceive(const FridayPublisherRootTerminal *,
    FridayPublisherRootCommandReceipt *);
/* Private SAME-Root final native handoff, not the Source wire or a new grant.
 * The actual cold caller preowns this cell before the first Root acquisition.
 * Complete native storage and its required value bank stay alive at their
 * original addresses. Address/count alone is NOT receipt: the actual receiver
 * checks every graph edge/owner and reads the full native and cold data here.
 * Existing exposed Source receipts are immutable historical phases. */
typedef struct {
    uint64_t schema,bytes,started_ns,finished_ns;
    uint64_t original_read_credit,read_credit_used,full_native_bytes,full_cold_bytes;
    uint64_t post_native_bytes,credit_rows_checked,credit_rows_transferred;
    uint64_t owners_checked,nodes_checked,edges_checked;
    const void *native_body,*cold_body;
    uint64_t native_body_bytes,cold_body_bytes;
    const FridayPublisherRootCommandReceipt *source_receipt;
    const FridayPublisherRootUtilityReceipt *utility_receipt;
    pid_t pid;
    int attempted,sealed,source_data_complete,graph_checked,cold_data_complete;
    int native_read_complete,cold_read_complete,clock_complete;
    int original_pool_history_received,credit_settlement_attempted,credit_settlement_complete;
    int post_native_read_complete;
    int source_owner_end,config_owner_end,runtime_owner_end,utility_owner_end;
    int producer_transfer_confirmed,command_owner_end_confirmed;
    int original_owner_retained,clock_attempted,clock_rc,clock_errno,clock_failed,clock_fault_kind;
    const char *phase;
    volatile unsigned char reader_sink;
} FridayPublisherRootFinalHandoff;
/* Called exactly once by the ACTUAL cold entry on both outcomes, after its
 * completion phase and before publishing its final result to its own caller.
 * Never touches a Python object after Finalize or a closed historical map. */
int FridayPublisherRootFinalReceive(FridayPublisherRootColdResult *);
#endif
