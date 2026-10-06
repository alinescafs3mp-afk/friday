/* NEW SAME-role native Publisher Root entry, protected reader, one whole
 * master pool, actual prepared producer and finite same-role terminal.
 * INERT AUTHORING ONLY. No compiler, native or Source invocation was made.
 * libc/CPython/OpenSSL/kernel ABI and whole implicit cost are NOT qualified.
 */
#define PY_SSIZE_T_CLEAN
#define _GNU_SOURCE
#include "publisher_root_entry.h"
#include "publisher_root_command.h"
#include "publisher_root_bank.h"
#include <frameobject.h>
#include <openssl/evp.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <time.h>
#include <dirent.h>
#include <sys/wait.h>
#include <sys/syscall.h>
#include <sys/poll.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <limits.h>
#include <stddef.h>
#include <signal.h>
#include <sys/mman.h>
#include <stdatomic.h>
#include <linux/sched.h>

#define READ_CAP 40960000000ULL
#define OUTPUT_CAP 33554432ULL
#define RAM_CAP 8589934592ULL
#define SLOT_CAP 128ULL
#define WORKER_CAP 4ULL
#define INPUT_CAP 2000000ULL
#define BODY_CAP 80000000ULL
#define ENROLLMENT "/etc/friday/root-publisher/enrollment.json"
typedef struct {
    int fd,closed,close_attempted,close_rc,close_errno;
    struct stat birth;
    PyObject *pin,*raw;
    uint64_t generation,read_used,alias_generation;
    int keeper,acquired,close_alias,alias_kind;
    int keeper_open_attempted,keeper_open_rc,keeper_open_errno;
    int birth_stat_attempted,birth_stat_rc,birth_stat_errno;
    int live_body,live_keeper,read_completed,read_error;
    FridayPublisherRootCloseFact body_close,keeper_close;
} RootHeldFile;
struct FridayPublisherPreparedRow {
    PyObject *row,*credit;
    uint64_t token,generation;
    int keeper_open_attempted,keeper_open_rc,keeper_open_errno;
    int birth_stat_attempted,birth_stat_rc,birth_stat_errno;
    int fd,keeper,opened,keeper_attempted,keeper_closed,keeper_rc,keeper_errno;
    struct stat birth;
    FridayPublisherRootCloseFact body_close;
    FridayPublisherRootCloseFact keeper_close;
};

/* SOL101 fixed native history. These cells exist before ANY owned acquisition.
 * Live endpoint capacity remains 128; history capacity is not a new live cap.
 * Wire is the exact same compiled Root ABI, never a portable ABI claim. */
#define ROOT_BOOT_FDS 128
#define ROOT_BOOT_OPS 512
#define ROOT_BOOT_FILES 32768
#define ROOT_BOOT_ERRORS 64
typedef struct {
    int fd,valid,closed,close_attempted,close_validation_attempted,close_rc,close_errno,stat_errno,acquire_errno;
    uint64_t held_index;int reserved_slot;
    struct stat birth,last;
    uint64_t generation;
    int keeper,acquired;
    int keeper_open_attempted,keeper_open_rc,keeper_open_errno;
    int birth_stat_attempted,birth_stat_rc,birth_stat_errno;
    int live_body,live_keeper,reserved_slots;
    FridayPublisherRootCloseFact body_close,keeper_close;
} RootBootstrapFD;
typedef struct { int fd,valid,flags,status_flags,flags_errno,stat_errno;struct stat identity; } RootBootstrapFDView;
typedef struct {uint64_t count;int complete,original_errno;RootBootstrapFDView rows[128];} RootBootstrapCensus;
typedef struct {
    uint64_t sequence;int op,fd,target,argument,attempted,returned,rc,original_errno,identity_valid,after_valid;
    struct stat before,after;
} RootBootstrapOperation;
typedef struct {
    uint64_t schema,bytes,sequence,pid,parent_pid,started_ns,deadline_ns,phase;
    int original_errno,first_op,first_stage,fd_end_complete,full_pre_complete,full_post_complete;
    uint64_t op_count;
    RootBootstrapCensus before,after;
    RootBootstrapFD census_fds[8];unsigned census_fd_count;
    RootBootstrapOperation operations[ROOT_BOOT_OPS];
} RootBootstrapFrame;
typedef struct {
    uint64_t schema,bytes,sequence,pid,parent_pid;int accepted,original_errno;
} RootBootstrapAck;
/* SOL102: the existing outside parent owns this complete bank BEFORE fork.
 * The child records last gate/owner closes here, not into another pipe frame
 * which would itself need a last pipe close. Anonymous shared memory survives
 * child loss in the parent. It does NOT survive a successful fexecve in the
 * child. The same parent accounts public-stock process lifetime separately
 * from a returned native transport end. The tool's internal last return
 * remains unobserved. Selected-image qualification is an external prerequisite,
 * not a boolean that this observation record can issue or invalidate.
 * Atomics must be lock-free in the actual image; no hidden child lock/service.
 * A killed ENTERED operation is UNKNOWN, not a made-up syscall return. */
#define ROOT_BOOT_IO_OPS 2048
#define ROOT_BOOT_IO_BANK (2*sizeof(RootBootstrapFrame)+3*sizeof(RootBootstrapAck))
typedef struct {
    uint64_t sequence,offset,width,used,deadline;
    int fd,writing,entered,returned,rc,original_errno,ready,revents,poll_errno,refusal_code,fault_kind;
} RootBootstrapIO;
typedef struct {
    _Atomic unsigned entered,sequence,sealed;
    uint64_t schema,bytes,pid,parent_pid,started_ns;
    int stage,first_errno_valid,first_errno,first_stage,first_op,first_fault_kind;
    int exec_entered,exec_returned,exec_errno,cleanup_complete;
    uint64_t io_count,io_used,child_fd_count;
    RootBootstrapFrame final;
    RootBootstrapIO io[ROOT_BOOT_IO_OPS];
    unsigned char io_bank[ROOT_BOOT_IO_BANK];
    RootBootstrapFD child_fds[16];
    uint64_t child_fd_operation_at[16];
    int prctl_entered,prctl_returned,prctl_rc,prctl_errno;
    int limit_entered[4],limit_returned[4],limit_rc[4],limit_errno[4];
    struct rlimit limits[4];
} RootBootstrapParentBank;
typedef struct {
    uint64_t schema,bytes,parent_pid,child_pid,started_ns,work_deadline,seal_deadline;
    uint64_t fd_count,active_fds,bank_read_bytes,actual_reads,actual_writes;
    uint64_t stdout_used,stderr_used,owner_used[2],ack_used[2],overflow_used[2];
    int attempted,failed,first_errno_valid,first_errno;
    char first_phase[128];
    int map_attempted,map_errno,map_acquired,protect_attempted,protect_rc,protect_errno;
    int unmap_attempted,unmap_rc,unmap_errno,bank_full_read,bank_stable;
    int native_failure_correspondence,stock_exec_end_unknown;
    struct clone_args spawn_args;
    long spawn_rc;
    int spawn_entered,spawn_returned,spawn_pidfd,spawn_errno,spawn_pidfd_bound;
    int signal_attempted,signal_rc,signal_errno,wait_attempted,waited,status,wait_errno;
    siginfo_t reap_info;int reap_rc,reap_errno,reap_returned;
    struct rusage usage;
    int all_fd_ends,child_IO_known,final_io_attempted,child_transport_end_confirmed;
    int lifetime_phase,public_stock_observables_consumed;
    int public_stock_parent_legal_retirement_accounted;
    int stock_exec_prefix_retained,stock_image_replacement_confirmed;
    int kernel_process_lifetime_ended;
} RootBootstrapParentEnd;
typedef struct {PyObject *type,*value,*tb;} RootBootstrapError;
typedef struct {
    int attempted,settling,published,first_errno,first_errno_valid,failed;
    const char *first_phase;
    RootBootstrapError first,secondary[ROOT_BOOT_ERRORS],return_pending;unsigned secondary_count;
    RootBootstrapFD fd[ROOT_BOOT_FILES];unsigned fd_count;
    int pipe_index[8],pidfd_index,cgroupfd_index;
    uint64_t active_fds;
    unsigned char census_buffer[65536];
    RootBootstrapFrame child,received[2];
    RootBootstrapParentBank *parent_bank,final_parent_copy;
    PyObject *final_parent_raw;
    RootBootstrapParentEnd parent_end;
    PyObject *parent_end_raw;
    int map_attempted,map_errno,map_acquired,protect_attempted,protect_rc,protect_errno;
    int unmap_attempted,unmap_rc,unmap_errno,bank_full_read,bank_stable;
    int native_failure_transport_complete,stock_exec_end_unknown;
    int stock_exec_prefix_retained,lifetime_phase;
    int public_stock_observables_consumed;
    int public_stock_parent_legal_retirement_accounted;
    int kernel_process_lifetime_ended,stock_image_replacement_confirmed;
    uint64_t bank_read_bytes,child_fd_base;
    unsigned char bank_read_sink;
    RootBootstrapAck ack[2],initial_gate;
    int child_start_seen;
    RootBootstrapCensus parent_before,parent_after;
    int parent_census_complete,child_frame_accepted[2],child_ack_written[2];
    int owner_eof,owner_extra,stdout_eof,stderr_eof,identity_qualified;
    int forked,wait_attempted,waited,status,wait_errno,signal_attempted,signal_rc,signal_errno;
    struct clone_args spawn_args;
    int spawn_entered,spawn_returned,spawn_pidfd,spawn_errno,spawn_pidfd_bound;
    long spawn_rc;
    siginfo_t reap_info;int reap_rc,reap_errno,reap_returned;
    int child_terminal,all_fd_ends,endpoint_immutable,acknowledged,signature_ok,child_transport_end_confirmed;
    pid_t pid,parent_pid;struct rusage usage;
    uint64_t token,started,work_deadline,seal_deadline,outmax,errmax,wall;
    uint64_t stdout_used,stderr_used,owner_used[2],ack_used[2],delivered_frame_bytes,helper_alloc;
    PyObject *retained[4096];uint64_t retained_count;
    uint64_t reserved_reads,reserved_output,reserved_alloc,actual_native_reads,actual_native_writes;
    PyObject *profile,*before,*after,*identity,*last_io,*stdout_raw,*stderr_raw;
    PyObject *wire_raw[2],*outcome_raw,*compat_record,*parent_raw[2],*overflow_raw[2];
    PyObject *secondary_immutable,*wait_usage_raw,*ack_raw[3],*reap_info_raw,*spawn_args_raw;int initial_gate_written;
    unsigned char overflow[2][65536];uint64_t overflow_used[2];
    int child_frame_attempted[2],child_ack_received[2],owner_extra_valid;
    unsigned char owner_extra_byte;
    uint64_t child_actual_reads,child_actual_writes;int child_IO_known,final_io_attempted;
    char capture[65536],pidtext[40],keyarg[80],sigarg[80],admarg[80],cgroup_path[4097];
    char *argv[9],*env[3];
} RootBootstrapState;
typedef struct {
    int fd,acquired,close_attempted,close_validation_attempted,closed,close_rc,close_errno,stat_errno;
    struct stat birth,last;
    uint64_t generation;
    int keeper;
    int keeper_open_attempted,keeper_open_rc,keeper_open_errno;
    int birth_stat_attempted,birth_stat_rc,birth_stat_errno;
    int live_body,live_keeper;
    FridayPublisherRootCloseFact body_close,keeper_close;
} RootImageFD;
typedef struct {
    int attempted,complete,first_errno;const char *first_phase;
    uint64_t fd_count,path_count,queue_count,queue_at,raw_directory_bytes,member_bytes,hash_bytes;
    RootImageFD fds[FRIDAY_ROOT_HELD_FILES];
    char queued[513][4097];
    PyObject *mountinfo_raw;
    unsigned char seen[512];
    struct stat identities[512];
    unsigned char directory_raw[65536];
    int root_index;struct stat root_before,root_after;
} RootImageState;

#define ROOT_UTILITY_COUNT FRIDAY_ROOT_UTILITY_COUNT
typedef FridayPublisherRootUtilityEnd RootUtilityEnd;
/* SOL105 selected own producer storage belongs to the SAME original Root.
 * Strong refs, actual full bodies and transitions, never an issuer/pointer ACK.
 * sizeof RootStorage is debited before Root effects; capacities are not fit. */
#define FRIDAY_OWN_FIELDS 12
enum { OWN_VAR=1,OWN_TOKEN=2,OWN_BINDING=3,OWN_MAPPING=4,OWN_HASH=5,OWN_CLASS=6,OWN_SUPPORT=7,OWN_MAP_CUT=8,
    OWN_CLASS_SCOPE=9,OWN_CLASS_RESTORE=10,OWN_ERROR_RECORD=11,OWN_ERROR_CELL=12 };
/* Additional row kinds use the ORIGINAL twelve-ref/fifteen-owner row bank.
 * No capacity, pool, role, grant or cleanup reserve is introduced. */
enum { CLASS_RETURN_LOCAL=1,CLASS_CALL_ERROR=2,CLASS_RETURN_FOREIGN=3,CLASS_RETURN_NONCLASS=4 };
enum { CLASS_COPY=1,CLASS_WRAPPER=2,CLASS_COPY_SET_ATTEMPT=4,CLASS_COPY_SET_OK=8,
    CLASS_MODULE_SET_ATTEMPT=16,CLASS_MODULE_SET_OK=32,CLASS_EVAL_ATTEMPT=64,
    CLASS_EVAL_RETURN=128,CLASS_RESTORE_ATTEMPT=256,CLASS_RESTORE_OK=512,
    CLASS_UNCERTAIN=1024,CLASS_INSTALL_ERROR=2048 };
enum { CLASS_RESTORE_OP_MASK=3,CLASS_RESTORE_LOOKUP_ATTEMPT=4,CLASS_RESTORE_LOOKUP_RETURN=8,
    CLASS_RESTORE_MUTATION_RETURN=16,CLASS_RESTORE_MUTATION_FAILED=32,
    CLASS_RESTORE_AFTER_ATTEMPT=64,CLASS_RESTORE_AFTER_RETURN=128 };
typedef struct {
    uint64_t kind,serial,flags,attempts,confirmed,width;
    pid_t pid;
    PyObject *refs[FRIDAY_OWN_FIELDS];
    PyObject *error_type,*error_value,*error_tb;
    int error_saved;unsigned borrowed_mask;
    /* Original native input scalars for the one error-cell dictionary birth.
     * Included in sizeof RootStorage and the original final read reservation. */
    FridayPublisherFailureScalars cell_input;
    /* Chain of actual successful observation cuts of this SAME original
     * cell. FAILED is terminal, not another fresh row with attempts=1. */
    uint64_t cell_previous,cell_outcome;
    /* Actual one-based owner slots, set only after each successful MOVE.
     * These native identities remain after borrowed Python views are null. */
    uint64_t owner_at[15];
} FridayPublisherOwnValue;
typedef struct {
    uint64_t count,retired,scan_steps;
    PyObject *early_module,*token_missing;
    PyObject *mapping_module,*mapping_factory,*mapping_access;
    PyObject *hash_module,*hash_sha256,*hash_new;
    uint64_t owner_first,owner_count,early_owner_at,missing_owner_at;
    uint64_t stock_owner_at[6]; /* actual six stock dependency MOVE slots */
    uint64_t borrowers_cleared;
    int borrowers_end_confirmed;
    /* Numeric mirror of the actual registered Run's two inline cell guards.
     * Joined through the real group21 capsule node BEFORE borrower loss. */
    uint64_t cell_last[2],cell_state[2],cell_context_node[2];
    FridayPublisherOwnValue rows[FRIDAY_ROOT_COMMAND_NODES];
} FridayPublisherOwnValues;

enum {
    SECONDARY_CUT_FRAME=1,SECONDARY_CUT_CODE=2,SECONDARY_CUT_SOURCE=3,
    SECONDARY_CUT_BUFFER=4,SECONDARY_CUT_TRACEBACK=5,SECONDARY_CUT_ERROR=6,
    SECONDARY_CUT_ERROR_HANDLER=7
};
#define FRIDAY_ROOT_SECONDARY_CUTS 65536
#define FRIDAY_ROOT_SECONDARY_EDGES 16
#define FRIDAY_ROOT_SECONDARY_SCALARS 8
#define SECONDARY_CUT_CAP FRIDAY_ROOT_SECONDARY_CUTS
#define SECONDARY_CUT_EDGES FRIDAY_ROOT_SECONDARY_EDGES
#define SECONDARY_CUT_SCALARS FRIDAY_ROOT_SECONDARY_SCALARS
typedef struct {
    PyObject *original;
    unsigned char kind,nedges,nscalars,pad;
    PyObject *edges[FRIDAY_ROOT_SECONDARY_EDGES];
    int64_t scalars[FRIDAY_ROOT_SECONDARY_SCALARS];
    pid_t pid;
    uint64_t serial;
    unsigned borrowed_mask; /* bit 0 original, bits 1..nedges edges */
    unsigned required_mask; /* same bits, kept after the borrowed view is cleared */
    uint64_t owner_at[1+FRIDAY_ROOT_SECONDARY_EDGES];
} FridayPublisherSecondaryCut;
typedef struct {
    uint64_t count,owner_first,owner_count,borrowers_cleared,scan_steps;
    int borrowers_end_confirmed,retired,end_confirmed,end_attempted;
    FridayPublisherSecondaryCut rows[FRIDAY_ROOT_SECONDARY_CUTS];
} FridayPublisherSecondaryCuts;

/* Numeric scratch is separate from the append-only graph. No Python ref,
 * member hash/equality or frozen record is stored/rewritten here. The live
 * bank belongs to sizeof RootStorage; the final reader has its OWN original
 * constructor-owned bank outside the exposed immutable native body. */
typedef struct {
    uint64_t initialized,generation;
    uint64_t marks[FRIDAY_ROOT_COMMAND_NODES];
} RootSetScratch;
typedef struct {
    uint64_t phase,limit;
    uint64_t order[FRIDAY_ROOT_COMMAND_NODES],next[FRIDAY_ROOT_COMMAND_NODES];
    uint64_t prefix[256];
} RootSetPointerIndex;
typedef struct {
    uint64_t active_phase;
    PyObject *restore_type,*restore_value,*restore_tb;
} RootSetRegion;
/* A280: SAME original row bank, with fixed-size native pending totals.
 * This is an index maintained by the only actual writer, not another pool,
 * issuer, observed-cost claim or replacement for the full original rows.
 * Full rows are rejoined at BOTH real final consumers with prepaid credit. */
typedef struct {
    uint64_t reads,output,hash,allocation,slots;
    uint64_t active_rows,source_rows,rows;
} RootPoolTotals;
typedef struct {
    FridayPublisherOwnValues own_values;
    RootSetScratch set_scratch;
    RootSetPointerIndex set_pointer_index;
    RootSetRegion set_region;
    FridayPublisherSecondaryCuts secondary;
    FridayPublisherMasterPool pool;
    RootPoolTotals pool_totals;
    FridayPublisherBindings bindings;
    FridayPublisherRootTerminal terminal;
    FridayPublisherRootCaseInput case_input;
    PyObject *source_case; /* actual owned decoded value, even if tuple birth fails */
    uint64_t case_owner_at,args_owner_at;
    int case_tuple_alias_verified;
    RootHeldFile held[FRIDAY_ROOT_HELD_FILES];
    uint64_t held_count,prepared_count;
    FridayPublisherPreparedRow prepared[FRIDAY_NATIVE_FD_HISTORY];
    RootHeldFile *enrollment_file,*admission_file,*signature_file,*key_file,*tool_file;
    PyObject *enrollment,*admission,*role_schema,*root_fact,*qualification;
    PyObject *json_loads,*json_pairs,*json_constant,*re_fullmatch;
    /* Actual producer temporaries are Root-owned from their first return.
     * No temporary DECREF or later allocation may erase a failed prefix. */
    PyObject *json_module,*re_module,*source_entry_module;
    PyObject *loader_sys,*loader_meta,*loader_names,*loader_pending_name;
    PyObject *final_identity,*final_close_attempt,*final_close_history;
    PyObject *final_slot,*final_rows;
    int loader_install_attempted,loader_installed;
    int output_root_open_attempted,output_root_open_rc,output_root_open_errno;
    int final_open_attempted,final_open_rc,final_open_errno;
    int output_root_stat_attempted,output_root_stat_rc,output_root_stat_errno;
    int final_stat_attempted,final_stat_rc,final_stat_errno;
    struct stat final_identity_native;
    FridayPublisherRootCloseFact final_owner_close,output_owner_close;
    FridayPublisherRootCloseFact final_keeper_close,output_keeper_close;
    int final_keeper,output_keeper;
    int final_keeper_valid,output_keeper_valid;
    int final_keeper_attempted,final_keeper_closed,final_keeper_rc,final_keeper_errno;
    int output_keeper_attempted,output_keeper_closed,output_keeper_rc,output_keeper_errno;
    PyObject *source_manifest,*consumer_manifest,*source_paths,*consumer_paths;
    PyObject *source_entry,*source_args,*bootstrap_record;
    PyObject *source_modules,*source_codes,*source_loader;
    PyObject *bootstrap_stdout_buffer,*bootstrap_stderr_buffer;
    int bootstrap_pipes[8];uint64_t bootstrap_stdout_used,bootstrap_stderr_used;
    struct stat output_root_identity;
    int output_root_fd,final_fd,initialized,attempted,signed_admission,snapshot_verified;
    int cold_pool_started;
    int cold_runtime_bound;
    pid_t cold_runtime_pid;
    unsigned long cold_runtime_thread;
    PyInterpreterState *cold_runtime_interpreter;
    int bootstrap_pid,bootstrap_pidfd,bootstrap_waited,bootstrap_status;
    struct rusage bootstrap_usage;
    const char *phase;
    RootBootstrapState bootstrap;
    RootImageState image_inventory;
    FridayPublisherRootCommandReceipt command_receipt;
    int command_read_reserved;
    uint64_t generation_clock;
    int utility_preowned,utility_accounted;
    int utility_tail_reserved,utility_tail_active;
    uint64_t utility_tail_remaining,utility_tail_total;
    RootUtilityEnd utility[ROOT_UTILITY_COUNT];
    FridayPublisherRootUtilityReceipt utility_result;
} FridayPublisherRootStorage;
/* Actual Root working memory is PRIVATE, preserving existing fork COW semantics.
 * A separately preowned DONTFORK terminal destination receives the complete
 * current bank only after ColdPerform. Source never inherits a writable view. */
static _Thread_local FridayPublisherRootStorage *root_storage_address;
#define root_storage (*root_storage_address)
/* Outside the transferred Root data: this is the preowned cold caller's
 * binding/receipt support,not a new observer process or another pool. */
typedef struct {
    FridayPublisherRootColdResult *caller;
    pid_t pid;
    uint64_t read_credit,read_used;
    int credit_reserved,transferred;
    RootSetScratch set_scratch; /* mutable receiver work, NEVER exposed input */
} RootFinalCallerBinding;
static _Thread_local RootFinalCallerBinding *final_caller_address;
#define final_caller_binding (*final_caller_address)
static _Thread_local FridayPublisherRootColdResult *bank_cold_address;
static _Thread_local FridayPublisherRootBankHeader *bank_header;
static _Thread_local uint64_t bank_bytes;
_Static_assert(_Alignof(FridayPublisherRootStorage)<=64,"Root bank alignment");
_Static_assert(_Alignof(RootFinalCallerBinding)<=64,"Caller bank alignment");
_Static_assert(_Alignof(FridayPublisherRootColdResult)<=64,"Cold bank alignment");
/* SAME original birth: disjoint bank, final, utility and cold allowances.
 * 128-bit intermediates are checked before narrowing; selected ABI/cost remains
 * REQUIRED_NOT_RUN. None of these receipts grants work or endpoint ownership. */
static int bank_narrow(__uint128_t n,uint64_t *out) {
    if(n>UINT64_MAX)return -1;*out=(uint64_t)n;return 0;
}
static int bank_align_checked(uint64_t a,uint64_t b,uint64_t *out) {
    __uint128_t n=(__uint128_t)a+b+63ULL;
    return bank_narrow(n&~(__uint128_t)63ULL,out);
}
int FridayPublisherRootBankBirthLayout(uint64_t out[9]) {
    if(!out)return -1;
    /* Mandatory selected-image sizeof facts, not runtime/pool provisioning.
     * Exact order matches QualifiedBankLayout; qualification remains separate. */
    out[0]=sizeof(FridayPublisherPoolRow);
    out[1]=sizeof(final_caller_binding.set_scratch.marks);
    out[2]=sizeof(root_storage.utility_result);out[3]=sizeof(root_storage.utility);
    out[4]=sizeof(void *);out[5]=sizeof(FridayPublisherColdConfigText);
    out[6]=sizeof(bank_cold_address->builtin_entries);
    out[7]=sizeof(bank_cold_address->config);out[8]=sizeof(bank_cold_address->preconfig);
    return 0;
}
static int bank_birth_reads(uint64_t out[3]) {
    if(!out)return -1;
    __uint128_t final=4*(__uint128_t)sizeof(FridayPublisherRootStorage)+
        4*(__uint128_t)sizeof(FridayPublisherRootColdResult)+
        3*(__uint128_t)(FRIDAY_ROOT_CASE_INPUT_MAX+1ULL)+
        (__uint128_t)FRIDAY_ROOT_COMMAND_NODES*528ULL*64ULL+160ULL*64ULL+
        8*(__uint128_t)FRIDAY_MASTER_HISTORY*sizeof(FridayPublisherPoolRow)+4096ULL;
    final+=64*((__uint128_t)60*FRIDAY_ROOT_COMMAND_EDGES+
        (__uint128_t)264*FRIDAY_ROOT_COMMAND_NODES+
        (__uint128_t)56*((FRIDAY_ROOT_COMMAND_EDGES+1023ULL)/1024ULL)+
        2*((sizeof(final_caller_binding.set_scratch.marks)+63ULL)/64ULL)+16ULL+128ULL);
    __uint128_t utility=4*(__uint128_t)sizeof(root_storage.utility_result)+
        8*(__uint128_t)sizeof(root_storage.utility)+2ULL*8193+
        8*(__uint128_t)FRIDAY_MASTER_HISTORY*sizeof(FridayPublisherPoolRow)+4096ULL;
    __uint128_t cold=4*(3*(__uint128_t)(FRIDAY_ROOT_COMMAND_TEXT+1ULL)+
        (__uint128_t)FRIDAY_ROOT_CONFIG_ROWS*sizeof(void *))+
        (__uint128_t)FRIDAY_ROOT_CONFIG_ROWS*(FRIDAY_ROOT_CONFIG_ROWS+1ULL)/2ULL*
            sizeof(FridayPublisherColdConfigText)+
        4*(__uint128_t)FRIDAY_ROOT_BUILTIN_NAMES+
        2*(__uint128_t)sizeof(bank_cold_address->builtin_entries)+
        2*(__uint128_t)sizeof(bank_cold_address->config)+sizeof(bank_cold_address->preconfig);
    if(bank_narrow(final,&out[0])<0||bank_narrow(utility,&out[1])<0||
       bank_narrow(cold,&out[2])<0)return -1;
    return 0;
}
int FridayPublisherRootBankLayout(uint64_t out[9]) {
    if(!out)return -1;
    out[0]=FRIDAY_ROOT_BANK_HEADER;out[1]=sizeof(FridayPublisherRootStorage);
    if(bank_align_checked(out[0],out[1],&out[2])<0)return -1;
    out[3]=sizeof(FridayPublisherRootColdResult);
    if(bank_align_checked(out[2],out[3],&out[4])<0)return -1;
    out[5]=sizeof(RootFinalCallerBinding);
    uint64_t end,reads[3];
    if(bank_align_checked(out[4],out[5],&end)<0||bank_birth_reads(reads)<0||
       bank_narrow((__uint128_t)end+FRIDAY_ROOT_BANK_PUBLICATION_OPERAND_STORAGE,&out[6])<0||
       bank_narrow(7*(__uint128_t)out[6]+4ULL*FRIDAY_ROOT_BANK_PARENT_SCRATCH+
            reads[0]+reads[1]+reads[2],&out[7])<0||
       bank_narrow(2*(__uint128_t)out[6]+FRIDAY_ROOT_BANK_PARENT_SCRATCH,&out[8])<0)return -1;
    return out[6]<FRIDAY_ROOT_BANK_RAM_CAP&&out[8]<FRIDAY_ROOT_BANK_RAM_CAP&&
        out[7]<FRIDAY_ROOT_BANK_READ_CAP?0:-1;
}
int FridayPublisherRootBankAttach(void *root,void *cold,void *caller,
        FridayPublisherRootBankHeader *header,uint64_t bytes) {
    uint64_t v[9];
    if(root_storage_address||final_caller_address||bank_cold_address||bank_header||
       !root||!cold||!caller||!header||FridayPublisherRootBankLayout(v)<0||
       bytes!=v[6]||root!=(unsigned char *)header+v[0]||
       cold!=(unsigned char *)header+v[2]||caller!=(unsigned char *)header+v[4]||
       (uintptr_t)root%64||(uintptr_t)cold%64||(uintptr_t)caller%64)return -1;
    root_storage_address=root;final_caller_address=caller;
    bank_cold_address=cold;bank_header=header;bank_bytes=bytes;return 0;
}
int FridayPublisherRootBankAttached(void) {
    return root_storage_address&&final_caller_address&&bank_cold_address&&bank_header;
}
void *FridayPublisherRootBankColdStorage(void) { return bank_cold_address; }
uint64_t FridayPublisherRootBankStart(void) { return bank_header?bank_header->word[B_STARTED]:0; }
uint64_t FridayPublisherRootBankDeadline(void) { return bank_header?bank_header->word[B_DEADLINE]:0; }
static int native_clock_unconfirmed(const FridayPublisherNativeClockAttempt *c) {
    return c->phase!=FRIDAY_NATIVE_CLOCK_UNUSED&&
        c->phase!=FRIDAY_NATIVE_CLOCK_RETURNED;
}
static int native_work_clock_unconfirmed(const FridayPublisherMasterPool *p) {
    const FridayPublisherNativeClockAttempt *c=&p->work_clock;
    if(c->phase!=FRIDAY_NATIVE_CLOCK_WORK_STOPPED)
        return native_clock_unconfirmed(c);
    /* A terminal work denial with a complete valid primitive sample can
     * retain only the original completion reserve. Its exact range
     * belongs to this SAME pool; a narrower applicable full bound is never
     * replaced with the original larger deadline. No sample or mutation. */
    if(c->rc||c->error||c->lower_ns!=p->started_ns||
       p->deadline_ns<p->work_deadline_ns||
       p->deadline_ns-p->work_deadline_ns!=600ULL*1000000000ULL||
       c->raw.tv_sec<0||c->raw.tv_nsec<0||c->raw.tv_nsec>=1000000000L||
       (uint64_t)c->raw.tv_sec>
           (UINT64_MAX-(uint64_t)c->raw.tv_nsec)/1000000000ULL)return 1;
    uint64_t actual=(uint64_t)c->raw.tv_sec*1000000000ULL+(uint64_t)c->raw.tv_nsec;
    if(!actual||actual!=c->observed_ns||actual<p->started_ns||
       actual>p->deadline_ns)return 1;
    if(c->fault_kind==6) {
        if(c->limit_ns!=p->work_deadline_ns||
           (c->site!=FRIDAY_CLOCK_CONFIG&&c->site!=FRIDAY_CLOCK_RESERVE&&
            c->site!=FRIDAY_CLOCK_COLD_ENTRY&&c->site!=FRIDAY_CLOCK_ADMISSION))return 1;
        return !(actual>c->limit_ns||
                 (c->site==FRIDAY_CLOCK_ADMISSION&&actual==c->limit_ns));
    }
    /* The actual admission producer sets kind7 ONLY after a valid sample
     * and the ordinary future-issued refusal. Its original admission body
     * stays held separately; this is not proof of admission or its authority.
     * Work stays denied without re-sampling until it happens to be current. */
    if(c->fault_kind==7)
        return actual>c->limit_ns||
            !((c->site==FRIDAY_CLOCK_ADMISSION&&c->limit_ns==p->work_deadline_ns)||
              (c->site==FRIDAY_CLOCK_GENERIC&&c->limit_ns==p->deadline_ns));
    return 1;
}
uint64_t FridayPublisherRootBankFinalDeadline(void) {
    if(!FridayPublisherRootBankAttached())return 0;
    const FridayPublisherMasterPool *p=&root_storage.pool;
    const FridayPublisherRootColdResult *c=bank_cold_address;
    uint64_t start=bank_header->word[B_STARTED],original=bank_header->word[B_DEADLINE];
    if(!start||start>UINT64_MAX-4200ULL*1000000000ULL||
       original!=start+4200ULL*1000000000ULL||
       c->completion_clock_failed||c->final_handoff.clock_failed||
       root_storage.utility_result.clock_failed)return 0;
    if(native_work_clock_unconfirmed(p)||
       native_clock_unconfirmed(&c->completion_clock_original)||
       native_clock_unconfirmed(&c->final_handoff.clock_original)||
       native_clock_unconfirmed(&root_storage.utility_result.clock_original))return 0;
    const FridayPublisherPoolInitialClock *clock=&p->initial_clock;
    /* A zero deadline alone is NOT a never-attempted clock. Original facts
     * are written prospectively by the actual constructor, before its sample.
     * No cause-string interpretation, new sample or repaired expiry here. */
    if(clock->phase==FRIDAY_POOL_START_NONE||
       clock->phase==FRIDAY_POOL_START_PRECLOCK) {
        if(p->initialized||p->started_ns||p->deadline_ns||p->work_deadline_ns||
           p->spent_read||p->spent_output||p->spent_hash||
           p->birth_adopted||p->birth_final_read||p->birth_utility_read||p->birth_cold_read||
           clock->raw.tv_sec||clock->raw.tv_nsec||clock->observed_ns||
           clock->rc||clock->error||
           p->work_clock.phase!=FRIDAY_NATIVE_CLOCK_UNUSED)return 0;
        if(clock->phase==FRIDAY_POOL_START_NONE) {
            if(root_storage.cold_pool_started||p->pid||p->native_allocation||
               p->next_token||p->count||p->preowner_started||p->source_detached||
               p->refused||p->observation_unknown||p->fault)return 0;
        } else if(p->pid!=getpid())return 0;
        /* Only a genuine no-clock ordinary prefix retains the inherited
         * interval. A partial entered/failed constructor cannot reach this. */
        return original;
    }
    if(clock->phase==FRIDAY_POOL_CLOCK_WORK_STOPPED) {
        if(!FridayPublisherRootInitialWorkData(c))return 0;
        return p->deadline_ns; /* restrictive DATA only, no work or end grant */
    }
    if(clock->phase!=FRIDAY_POOL_CLOCK_VALID||clock->rc||clock->error||
       clock->raw.tv_sec<0||clock->raw.tv_nsec<0||clock->raw.tv_nsec>=1000000000L||
       (uint64_t)clock->raw.tv_sec>(UINT64_MAX-(uint64_t)clock->raw.tv_nsec)/1000000000ULL||
       clock->observed_ns!=(uint64_t)clock->raw.tv_sec*1000000000ULL+(uint64_t)clock->raw.tv_nsec||
       clock->observed_ns<start||clock->observed_ns>original-600ULL*1000000000ULL)return 0;
    /* After its valid original sample the SAME pool may only tighten its
     * deadline. Read retained native scalars, never retired Python admission. */
    if(p->pid!=getpid()||p->started_ns!=start||
       p->deadline_ns<start||p->deadline_ns>original)return 0;
    return p->deadline_ns;
}
uint64_t FridayPublisherRootBankExtraAllocation(void) {
    if(!FridayPublisherRootBankAttached())return 0;
    return 2ULL*bank_bytes-sizeof(root_storage)-sizeof(final_caller_binding)-
        sizeof(*bank_cold_address)+FRIDAY_ROOT_BANK_PARENT_SCRATCH;
}
uint64_t FridayPublisherRootBankReadReserve(void) {
    return bank_header?bank_header->word[B_READ_RESERVE]:0;
}
int FridayPublisherRootBankReservationMatches(uint64_t reads,uint64_t allocation,uint64_t output) {
    if(!FridayPublisherRootBankAttached())return 0;
    uint64_t v[9];if(FridayPublisherRootBankLayout(v)<0)return 0;
    const uint64_t *w=bank_header->word;
    /* Facts of the fixed reservation in the original outside caller. This
     * does not authenticate an image, issue a grant or provision a native
     * pool from Python. All three debit amounts are recomputed by THIS image. */
    return reads==v[7]&&allocation==v[8]&&output==FRIDAY_ROOT_BANK_HEADER&&
        w[B_RESERVATION_MAGIC]==FRIDAY_ROOT_BANK_RESERVATION_MAGIC&&
        w[B_RESERVATION_VERSION]==FRIDAY_ROOT_BANK_RESERVATION_VERSION&&w[B_RESERVATION_GENERATION]==1&&
        w[B_RESERVATION_OWNER]==w[B_PARENT_PID]&&w[B_PARENT_PID]==(uint64_t)getppid()&&
        w[B_RESERVATION_STARTED]==w[B_STARTED]&&
        w[B_RESERVATION_DEADLINE]==w[B_DEADLINE]&&
        w[B_RESERVATION_READ]==reads&&w[B_RESERVATION_RAM]==allocation&&
        w[B_RESERVATION_OUTPUT]==output;
}
int FridayPublisherRootBankCommit(int rc,const void *result) {
    if(!FridayPublisherRootBankAttached()||bank_header->word[B_PUBLISHED]) {
        FridayPublisherRootBankFault(RP_OP_COMMIT_STATE,-1,0,
            (uint64_t)(uintptr_t)bank_header,(uint64_t)(uintptr_t)result,bank_bytes,0);
        return -1;
    }
    const FridayPublisherRootColdResult *c=result;
    if(c!=bank_cold_address) {
        FridayPublisherRootBankFault(RP_OP_COMMIT_RESULT,-1,0,
            (uint64_t)(uintptr_t)c,(uint64_t)(uintptr_t)bank_cold_address,sizeof(*c),0);
        return -1;
    }
    FridayPublisherMasterPool *p=&root_storage.pool;
    const FridayPublisherRootFinalHandoff *h=&c->final_handoff;
    uint64_t *w=bank_header->word;
    uint64_t final_deadline=FridayPublisherRootBankFinalDeadline();
    if(!final_deadline||final_deadline!=w[B_FINAL_DEADLINE]) {
        FridayPublisherRootBankFault(RP_OP_COMMIT_CLOCK_DOMAIN,-1,0,
            0,0,w[B_STARTED],0);
        return -1;
    }
    w[B_CHILD_PID]=(uint64_t)getpid();w[B_COLD_RC]=(uint64_t)(int64_t)rc;
    w[B_PREINIT_ATTEMPTED]=c->preinit_attempted;w[B_INIT_ATTEMPTED]=c->init_attempted;
    w[B_INIT_COMPLETE]=c->init_completed;w[B_PERFORM_ATTEMPTED]=c->perform_attempted;
    w[B_COLD_DATA_COMPLETE]=h->cold_data_complete;
    w[B_SOURCE_DATA_COMPLETE]=h->source_data_complete;
    w[B_CONFIG_END]=h->config_owner_end;w[B_LOCAL_COMMAND_END]=h->command_owner_end_confirmed;
    w[B_RUNTIME_LOCAL_END]=h->runtime_owner_end;w[B_UTILITY_LOCAL_END]=h->utility_owner_end;
    w[B_RUNTIME_EXTERNAL]=c->runtime_was_external;
    w[B_CLOCK_FAILED]=c->completion_clock_failed||h->clock_failed;
    w[B_READ_SPENT]=p->spent_read;w[B_OUTPUT_SPENT]=p->spent_output;
    w[B_NATIVE_ALLOCATION]=p->native_allocation;w[B_RETAINED_ALLOCATION]=p->retained_allocation;
    w[B_OBSERVED_RAM]=p->observed_ram;
    w[B_PARENT_READ_RESERVED]=FridayPublisherRootBankReadReserve();
    w[B_COLD_ADDRESS]=(uint64_t)(uintptr_t)c;
    w[B_ROOT_ADDRESS]=(uint64_t)(uintptr_t)&root_storage;
    w[B_CALLER_ADDRESS]=(uint64_t)(uintptr_t)&final_caller_binding;
    w[B_STATUS_SAVED]=c->first_status_saved;
    w[B_STATUS_COMPLETE]=!c->first_status_saved||c->status_bodies_complete;
    w[B_BUILTIN_COMPLETE]=!c->builtin_attempted||c->builtin_copy_complete;
    const char *texts[FRIDAY_ROOT_BANK_TEXT_ROWS]={c->phase,c->operation_phase,
        c->completion_phase,c->builtin_phase,c->config_receipt.fault,
        p->fault,root_storage.utility_result.phase,h->phase};
    FridayPublisherRootBankText *rows=(void *)((unsigned char *)bank_header+FRIDAY_ROOT_BANK_TEXT_AT);
    int text_complete=1;
    for(uint64_t i=0;i<FRIDAY_ROOT_BANK_TEXT_ROWS;i++) {
        FridayPublisherRootBankText *q=&rows[i];
        q->original=(uint64_t)(uintptr_t)texts[i];q->present=texts[i]!=NULL;
        if(!texts[i])continue;
        size_t n=strnlen(texts[i],sizeof(q->body));
        if(n==sizeof(q->body)){text_complete=0;break;}
        q->bytes=(uint64_t)n;q->alias=i+1;memcpy(q->body,texts[i],n+1);
        for(uint64_t j=0;j<i;j++)if(rows[j].original==q->original) {
            if(rows[j].bytes!=q->bytes||memcmp(rows[j].body,q->body,n+1))text_complete=0;
            q->alias=rows[j].alias;break;
        }
    }
    /* No new runtime-END claim. This is the complete selected pre-Source
     * public/native prefix, preserved in the parent's original storage.
     * Public PyConfig/string/list/status/table bodies were actually consumed
     * by FinalReceive before publication; private CPython heap is NOT dumped.
     * Missing/uncertain data stays HELD, including ordinary capacity failure. */
    int prefix=rc==0&&h->sealed&&h->source_data_complete&&h->cold_data_complete&&
        h->native_read_complete&&h->cold_read_complete&&h->clock_complete&&
        h->config_owner_end&&!c->configuration_owned&&!c->runtime_was_external&&
        !c->perform_attempted&&!root_storage.attempted&&!c->init_completed&&
        (c->preinit_attempted||c->init_attempted)&&
        !c->finalization_runtime_present_before&&!c->runtime_present_after_init&&
        !w[B_CLOCK_FAILED]&&w[B_STATUS_COMPLETE]&&w[B_BUILTIN_COMPLETE]&&text_complete&&
        root_storage.utility_result.copy_complete&&
        root_storage.utility_result.resource_bounds_confirmed&&
        root_storage.utility_result.pool_fault_complete&&!p->observation_unknown;
    int initial_data=rc==0&&FridayPublisherRootInitialWorkData(c)&&
        c->completion_returned&&!c->completion_clock_failed&&h->sealed&&
        h->native_read_complete&&h->cold_read_complete&&h->cold_data_complete&&
        h->source_data_complete&&h->clock_complete&&!h->clock_failed&&text_complete&&
        !h->command_owner_end_confirmed&&!h->producer_transfer_confirmed&&h->original_owner_retained;
    if(initial_data) {
        w[B_INITIAL_PHASE]=p->initial_clock.phase;w[B_INITIAL_RC]=(uint64_t)(int64_t)p->initial_clock.rc;
        w[B_INITIAL_ERRNO]=(uint64_t)(int64_t)p->initial_clock.error;
        w[B_INITIAL_SEC]=(uint64_t)p->initial_clock.raw.tv_sec;
        w[B_INITIAL_NSEC]=(uint64_t)p->initial_clock.raw.tv_nsec;
        w[B_INITIAL_OBSERVED]=p->initial_clock.observed_ns;
        uint64_t reads[3];if(bank_birth_reads(reads)<0)return -1;
        if(bank_narrow(7*(__uint128_t)bank_bytes+4ULL*FRIDAY_ROOT_BANK_PARENT_SCRATCH,
                &w[B_BIRTH_BANK_READ])<0)return -1;
        w[B_BIRTH_FINAL_READ]=reads[0];w[B_BIRTH_UTILITY_READ]=reads[1];w[B_BIRTH_COLD_READ]=reads[2];
        w[B_BIRTH_ADOPTED]=p->birth_adopted;w[B_EARLY_NATIVE_DATA]=1;
        w[B_UTILITY_NOT_ACQUIRED]=1;w[B_INITIAL_WORK_DATA]=1;
    }
    w[B_PARTIAL_PREFIX]=prefix;
    w[B_OUTCOME]=(h->command_owner_end_confirmed&&text_complete)?BANK_LOCAL_END:
        (initial_data?BANK_INITIAL_WORK_DATA:(prefix?BANK_EARLY_PREFIX:BANK_HELD));
    /* The original exposed ColdResult/CommandReceipt/final_handoff are NEVER
     * rewritten. Parent cannot consume this new record before actual reap. */
    struct timespec t={0,0};
    int clock_rc=clock_gettime(CLOCK_MONOTONIC,&t),clock_errno=clock_rc<0?errno:0;
    if(clock_rc<0) {
        FridayPublisherRootBankFault(RP_OP_COMMIT_CLOCK,clock_rc,clock_errno,
            0,0,0,CLOCK_MONOTONIC);
        return -1;
    }
    if(t.tv_sec<0||t.tv_nsec<0||t.tv_nsec>=1000000000L||
       (uint64_t)t.tv_sec>(UINT64_MAX-(uint64_t)t.tv_nsec)/1000000000ULL) {
        FridayPublisherRootBankFault(RP_OP_COMMIT_CLOCK_DOMAIN,-1,0,
            (uint64_t)t.tv_sec,(uint64_t)t.tv_nsec,w[B_STARTED],final_deadline);
        return -1;
    }
    w[B_COMPLETION_NS]=(uint64_t)t.tv_sec*1000000000ULL+(uint64_t)t.tv_nsec;
    if(w[B_COMPLETION_NS]<w[B_STARTED]||w[B_COMPLETION_NS]>final_deadline) {
        FridayPublisherRootBankFault(RP_OP_COMMIT_CLOCK_DOMAIN,-1,0,
            w[B_COMPLETION_NS],0,w[B_STARTED],final_deadline);
        return -1;
    }
    w[B_PUBLISHED]=1;return 0;
}
static int utility_preown(FridayPublisherRootStorage *);
static int utility_refresh(RootUtilityEnd *,uint64_t);
static PyObject *field(PyObject *,const char *);
static int bind_admission_fields(void);
static int current_enrollment_matches(PyObject *,PyObject *);
static int readonly_image_native(void);

static int native_clock_sample(FridayPublisherNativeClockAttempt *clock,
        uint64_t lower,uint64_t limit,uint64_t full_limit,uint64_t site,
        int work,uint64_t *out) {
    if(!clock||!out)return -1;
    *out=0;
    if(native_clock_unconfirmed(clock))return -1;
    clock->lower_ns=lower;clock->limit_ns=limit;clock->site=site;
    clock->raw=(struct timespec){0,0};
    clock->observed_ns=0;clock->rc=0;clock->error=0;clock->fault_kind=0;
    if(!lower||full_limit<lower||limit>full_limit) {
        clock->fault_kind=5;clock->phase=FRIDAY_NATIVE_CLOCK_FAILED;return -1;
    }
    clock->phase=FRIDAY_NATIVE_CLOCK_ENTERED;
    clock->rc=clock_gettime(CLOCK_MONOTONIC,&clock->raw);
    clock->error=clock->rc<0?errno:0; /* immediate original errno */
    if(clock->rc!=0)clock->fault_kind=1;
    else if(clock->raw.tv_sec<0||clock->raw.tv_nsec<0||
            clock->raw.tv_nsec>=1000000000L||
            (uint64_t)clock->raw.tv_sec>
                (UINT64_MAX-(uint64_t)clock->raw.tv_nsec)/1000000000ULL)
        clock->fault_kind=2;
    else {
        clock->observed_ns=(uint64_t)clock->raw.tv_sec*1000000000ULL+
            (uint64_t)clock->raw.tv_nsec;
        if(!clock->observed_ns||clock->observed_ns<lower||clock->observed_ns>full_limit)
            clock->fault_kind=3;
    }
    if(clock->fault_kind) {
        clock->phase=FRIDAY_NATIVE_CLOCK_FAILED;return -1;
    }
    /* A confirmed sample may refuse WORK while the already prepaid FULL
     * interval is still valid. Keep this first original record immutable;
     * no subsequent work helper can sample again or renew the interval. */
    if(work&&(clock->observed_ns>limit||
       (site==FRIDAY_CLOCK_ADMISSION&&clock->observed_ns==limit))) {
        clock->fault_kind=6;clock->phase=FRIDAY_NATIVE_CLOCK_WORK_STOPPED;
        return -1;
    }
    clock->phase=FRIDAY_NATIVE_CLOCK_RETURNED;
    *out=clock->observed_ns;return 0;
}
int FridayPublisherNativeClockSample(FridayPublisherNativeClockAttempt *clock,
        uint64_t lower,uint64_t limit,uint64_t site,uint64_t *out) {
    return native_clock_sample(clock,lower,limit,limit,site,0,out);
}
int FridayPublisherMasterClockBlocked(const FridayPublisherMasterPool *p) {
    if(!p||p->initial_clock.phase!=FRIDAY_POOL_CLOCK_VALID||
       native_work_clock_unconfirmed(p))return 1;
    if(FridayPublisherRootBankAttached()&&p==&root_storage.pool) {
        const FridayPublisherRootColdResult *c=bank_cold_address;
        if(c->completion_clock_failed||c->final_handoff.clock_failed||
           root_storage.utility_result.clock_failed||
           native_clock_unconfirmed(&c->completion_clock_original)||
           native_clock_unconfirmed(&c->final_handoff.clock_original)||
           native_clock_unconfirmed(&root_storage.utility_result.clock_original))return 1;
    }
    return 0;
}
int FridayPublisherMasterWorkClock(FridayPublisherMasterPool *p,uint64_t limit,
        uint64_t site,uint64_t *out) {
    if(out)*out=0;
    if(!out||!FridayPublisherMasterOwns(p)||FridayPublisherMasterClockBlocked(p))return -1;
    /* Do not change either bound here. The common primitive checks the
     * applicable FULL interval first, then the caller's original WORK limit.
     * Its ordinary guard also refuses a retained WORK_STOPPED before writes
     * or another sample, while separate prepaid completion clocks remain. */
    return native_clock_sample(&p->work_clock,p->started_ns,limit,
        p->deadline_ns,site,1,out);
}
static uint64_t now_ns(void) {
    /* The owned Root is the scope of this repair. Do not turn an inherited
     * child shadow into its parent's master or silently change its existing
     * clock interface. Those opaque stock/worker paths remain UNQUALIFIED;
     * their original local helper behavior is not claimed as latch/custody. */
    if(!FridayPublisherRootBankAttached()||root_storage.pool.pid!=getpid()) {
        struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t)<0)return 0;
        return (uint64_t)t.tv_sec*1000000000ULL+(uint64_t)t.tv_nsec;
    }
    uint64_t now=0;
    if(FridayPublisherMasterWorkClock(&root_storage.pool,root_storage.pool.deadline_ns,
            FRIDAY_CLOCK_GENERIC,&now)<0)return 0;
    return now;
}
static int plus(uint64_t *at,uint64_t n) {
    if(n>UINT64_MAX-*at)return -1;*at+=n;return 0;
}
int FridayPublisherMasterOwns(FridayPublisherMasterPool *p) {
    return FridayPublisherRootBankAttached()&&p==&root_storage.pool&&p->initialized&&p->pid==getpid()&&
        !final_caller_binding.transferred;
}
int FridayPublisherRootCaseCopy(FridayPublisherMasterPool *p,
    FridayPublisherRootCaseInput *v,const char *input) {
    if(!v||v->attempted||!input||!FridayPublisherMasterOwns(p))return -1;
    v->attempted=1;v->historical_input=input;
    /* Full bounded scan, copy, identity comparison and internal-NUL scan.
     * Static storage is already part of the original Root/cold sizeof debit.
     * A refused debit leaves the input unconsumed; no unbounded strlen or
     * later Python factory may read it instead. Never retry a partial copy. */
    /* memcmp reads BOTH complete operands, not one abstract comparison. */
    v->read_credit=5ULL*(FRIDAY_ROOT_CASE_INPUT_MAX+1ULL);
    if(FridayPublisherMasterBefore(p,v->read_credit,0,0,0)<0)return -1;
    v->credit_reserved=1;
    size_t n=strnlen(input,FRIDAY_ROOT_CASE_INPUT_MAX+1ULL);
    if(n>FRIDAY_ROOT_CASE_INPUT_MAX)
        return FridayPublisherMasterFault(p,"original_case_input_over_wire_domain");
    v->bytes=(uint64_t)n;v->bounded=1;
    memcpy(v->body,input,n+1);
    if(v->body[n]||memchr(v->body,0,n)||memcmp(v->body,input,n+1)) {
        v->drift=1;
        return FridayPublisherMasterFault(p,"original_case_input_copy_drift");
    }
    v->complete=1;return 0;
}
int FridayPublisherMasterFault(FridayPublisherMasterPool *p,const char *why) {
    if(!FridayPublisherRootBankAttached())return -1;
    /* A published transferred Root body is immutable,including on a late
     * invalid API call. No error setter may mutate its old pool or borrow a
     * Python reference after the caller's actual final end. */
    if(p==&root_storage.pool&&final_caller_binding.transferred)return -1;
    if(p&&!p->refused){p->refused=1;p->fault=why;}
    /* Keep pending and handled originals; only their absence selects the
     * exact preowned refusal. No new/normalized provider exception. */
    /* The actual cold command uses this pool before any interpreter exists.
     * Do not ask for a Python error indicator without an initialized runtime. */
    if(Py_IsInitialized()&&p&&p->refusal_type&&p->refusal_value&&!PyErr_Occurred()) {
        /* An empty raised indicator does not mean no original: an active
         * handler still owns its actual exception. Preserve that instance
         * as the raised error; use the preowned refusal only without one.
         * PyErr_SetObject would instead traverse/rewrite causal chains.
         * The public getter returns an owned ref; the setter steals it with
         * the already-checked old indicator NULL. No context/TB rewrite,
         * normalization or new exception here. Later Python propagation
         * may add real traceback frames, which remain required error DATA. */
        PyObject *original=PyErr_GetHandledException();
        if(!original)original=Py_NewRef(p->refusal_value);
        PyErr_SetRaisedException(original);
    }
    return -1;
}
static const char initial_work_fault[]="Root_original_initial_WORK_exhausted";
static int root_pool_start(FridayPublisherRootStorage *s,uint64_t command_storage) {
    FridayPublisherMasterPool *p=&s->pool;
    FridayPublisherPoolInitialClock *clock=&p->initial_clock;
    if(clock->phase!=FRIDAY_POOL_START_NONE)return -1;
    clock->phase=FRIDAY_POOL_START_PRECLOCK;
    if(p->initialized||p->started_ns||p->spent_read||p->spent_output||p->spent_hash||
       p->native_allocation||(p->pid&&p->pid!=getpid())||p->birth_adopted)return -1;
    p->pid=getpid();p->native_allocation=sizeof(*s)+sizeof(final_caller_binding);
    if(plus(&p->native_allocation,command_storage)<0||
       plus(&p->native_allocation,FridayPublisherRootBankExtraAllocation())<0||
       p->native_allocation>RAM_CAP) {
        p->refused=1;p->fault="Root_static_command_storage_bound";return -1;
    }
    p->started_ns=FridayPublisherRootBankStart();
    uint64_t v[9],reads[3];
    if(!p->started_ns||p->started_ns>UINT64_MAX-4200ULL*1000000000ULL||
       FridayPublisherRootBankDeadline()!=p->started_ns+4200ULL*1000000000ULL||
       FridayPublisherRootBankLayout(v)<0||bank_birth_reads(reads)<0||
       !FridayPublisherRootBankReservationMatches(v[7],p->native_allocation,FRIDAY_ROOT_BANK_HEADER)||
       v[7]>READ_CAP||FRIDAY_ROOT_BANK_HEADER>OUTPUT_CAP||
       final_caller_binding.caller!=bank_cold_address||final_caller_binding.pid!=getpid()||
       command_storage!=sizeof(*bank_cold_address)) {
        p->refused=1;p->fault="original_preowned_bank_reservation_mismatch";return -1;
    }
    /* Adopt ONE outside debit, before the first work-clock sample. These
     * receipts are disjoint components of v[7], NOT subsequent pool debits.
     * No runtime/utility is initialized and no work ownership is granted. */
    p->deadline_ns=FridayPublisherRootBankDeadline();
    p->work_deadline_ns=p->deadline_ns-600ULL*1000000000ULL;
    p->spent_read=v[7];p->spent_output=FRIDAY_ROOT_BANK_HEADER;
    p->birth_final_read=reads[0];p->birth_utility_read=reads[1];p->birth_cold_read=reads[2];
    final_caller_binding.read_credit=reads[0];final_caller_binding.credit_reserved=1;
    s->utility_tail_remaining=reads[1];s->utility_tail_total=reads[1];s->utility_tail_reserved=1;
    bank_cold_address->completion_reserved=1;p->birth_adopted=1;
    bank_cold_address->started_ns=p->started_ns;bank_cold_address->deadline_ns=p->deadline_ns;
    clock->raw=(struct timespec){0,0};clock->phase=FRIDAY_POOL_CLOCK_ENTERED;
    clock->rc=clock_gettime(CLOCK_MONOTONIC,&clock->raw);
    clock->error=clock->rc<0?errno:0; /* FIRST immediate primitive errno */
    int valid=clock->rc==0&&clock->raw.tv_sec>=0&&clock->raw.tv_nsec>=0&&
        clock->raw.tv_nsec<1000000000L&&
        (uint64_t)clock->raw.tv_sec<=(UINT64_MAX-(uint64_t)clock->raw.tv_nsec)/1000000000ULL;
    uint64_t current=valid?(uint64_t)clock->raw.tv_sec*1000000000ULL+
        (uint64_t)clock->raw.tv_nsec:0;
    clock->observed_ns=current;
    if(!valid||!current||current<p->started_ns||current>p->deadline_ns) {
        clock->phase=FRIDAY_POOL_CLOCK_FAILED;
        p->refused=1;p->fault="Root_initial_finite_clock_unavailable";return -1;
    }
    if(current>p->work_deadline_ns) {
        clock->phase=FRIDAY_POOL_CLOCK_WORK_STOPPED;
        p->refused=1;p->fault=initial_work_fault;return -1;
    }
    clock->phase=FRIDAY_POOL_CLOCK_VALID;
    p->initialized=1;p->next_token=1;
    /* Acquisitions remain strictly AFTER valid WORK. A prepaid numeric
     * completion allowance is not a receipt for any acquired endpoint. */
    if(utility_preown(s)<0) {
        p->observation_unknown=1;
        return FridayPublisherMasterFault(p,"utility_endpoint_unestablished");
    }
    return 0;
}
int FridayPublisherRootInitialWorkData(const FridayPublisherRootColdResult *c) {
    if(!c||!FridayPublisherRootBankAttached()||c!=bank_cold_address)return 0;
    const FridayPublisherRootStorage *s=&root_storage;
    const FridayPublisherMasterPool *p=&s->pool;
    const FridayPublisherPoolInitialClock *k=&p->initial_clock;
    uint64_t v[9],reads[3];
    if(!p->birth_adopted||p->initialized||p->pid!=getpid()||p->next_token||p->count||
       p->preowner_started||p->preowner_token||p->source_detached||p->observation_unknown||p->native_live_slots||
       p->refusal_type||p->refusal_value||
       p->spent_hash||p->retained_allocation||p->observed_read||p->observed_output||
       p->observed_ram||p->observed_workers||!p->refused||p->fault!=initial_work_fault||
       k->phase!=FRIDAY_POOL_CLOCK_WORK_STOPPED||k->rc||k->error||
       k->raw.tv_sec<0||k->raw.tv_nsec<0||k->raw.tv_nsec>=1000000000L||
       (uint64_t)k->raw.tv_sec>(UINT64_MAX-(uint64_t)k->raw.tv_nsec)/1000000000ULL)return 0;
    uint64_t actual=(uint64_t)k->raw.tv_sec*1000000000ULL+(uint64_t)k->raw.tv_nsec;
    if(!p->started_ns||p->started_ns>UINT64_MAX-4200ULL*1000000000ULL||
       p->started_ns!=FridayPublisherRootBankStart()||
       p->deadline_ns!=p->started_ns+4200ULL*1000000000ULL||
       p->deadline_ns!=FridayPublisherRootBankDeadline()||
       p->work_deadline_ns!=p->deadline_ns-600ULL*1000000000ULL||
       actual!=k->observed_ns||actual<=p->work_deadline_ns||actual>p->deadline_ns||
       FridayPublisherRootBankLayout(v)<0||bank_birth_reads(reads)<0||
       !FridayPublisherRootBankReservationMatches(v[7],p->native_allocation,FRIDAY_ROOT_BANK_HEADER)||
       p->spent_read!=v[7]||p->spent_output!=FRIDAY_ROOT_BANK_HEADER||
       p->birth_final_read!=reads[0]||p->birth_utility_read!=reads[1]||p->birth_cold_read!=reads[2]||
       !final_caller_binding.credit_reserved||final_caller_binding.read_credit!=reads[0]||
       final_caller_binding.caller!=c||final_caller_binding.pid!=getpid()||final_caller_binding.transferred||
       !s->utility_tail_reserved||s->utility_tail_total!=reads[1]||
       s->utility_tail_remaining!=reads[1]||s->utility_tail_active||s->utility_preowned||
       s->utility_result.attempted||s->attempted||s->initialized||s->bootstrap.attempted||
       s->image_inventory.attempted||s->held_count||s->prepared_count||
       !s->cold_pool_started||c->pool!=p||c->owner_pid!=getpid()||!c->attempted||
       !c->completion_reserved||c->configuration_owned||c->config_receipt.attempted||
       c->config_receipt.clear_attempted||c->case_input.attempted||c->first_status_saved||
       c->preinit_attempted||c->preinit_completed||c->init_attempted||c->init_completed||c->runtime_was_external||
       c->runtime_owned||c->partial_runtime_owned||c->builtin_attempted||c->builtin_installed||
       c->builtin_copy_complete||c->runtime_present_after_init||c->config_end_confirmed||
       c->finalization_runtime_present_before||c->finalization_runtime_present_after||
       c->receiver_attempted||c->receiver_returned||c->utility_end_confirmed||
       c->root_runtime_support_end_confirmed||c->command_end_confirmed||
       c->SourceReady||c->Root_admission||c->GO||
       c->perform_attempted||c->receipt||c->finalization_attempted||c->utility_receiver_attempted||
       p->work_clock.phase!=FRIDAY_NATIVE_CLOCK_UNUSED||p->work_clock.raw.tv_sec||p->work_clock.raw.tv_nsec||
       p->work_clock.observed_ns||p->work_clock.lower_ns||p->work_clock.limit_ns||p->work_clock.site||
       p->work_clock.rc||p->work_clock.error||p->work_clock.fault_kind)return 0;
    return 1; /* numeric unattempted prefix; NOT measured support END */
}
int FridayPublisherMasterCompletionClockBlocked(const FridayPublisherMasterPool *p) {
    if(!FridayPublisherRootBankAttached()||p!=&root_storage.pool)return 1;
    const FridayPublisherRootColdResult *c=bank_cold_address;
    if(!FridayPublisherRootInitialWorkData(c))return FridayPublisherMasterClockBlocked(p);
    return c->completion_clock_failed||c->final_handoff.clock_failed||
        root_storage.utility_result.clock_failed||native_clock_unconfirmed(&c->completion_clock_original)||
        native_clock_unconfirmed(&c->final_handoff.clock_original)||
        native_clock_unconfirmed(&root_storage.utility_result.clock_original);
}
int FridayPublisherRootColdPool(FridayPublisherRootColdResult *caller,FridayPublisherMasterPool **out) {
    if(!FridayPublisherRootBankAttached())return -1;
    FridayPublisherRootStorage *s=&root_storage;
    if(!out||!caller||caller->owner_pid!=getpid()||!caller->attempted||
       s->attempted||s->cold_pool_started||Py_IsInitialized())return -1;
    final_caller_binding.caller=caller;final_caller_binding.pid=getpid();
    *out=&s->pool;s->cold_pool_started=1;
    /* Even a failed start is single-use; no retry may replace its first fact. */
    return root_pool_start(s,sizeof(*caller));
}
int FridayPublisherRootColdRuntimeBind(FridayPublisherMasterPool *p) {
    if(!FridayPublisherRootBankAttached())return -1;
    FridayPublisherRootStorage *s=&root_storage;
    if(p!=&s->pool||!s->cold_pool_started||s->attempted||
       s->cold_runtime_bound||!FridayPublisherMasterOwns(p)||!Py_IsInitialized())
        return -1;
    PyThreadState *t=PyThreadState_GetUnchecked();
    if(!t||PyThreadState_GetInterpreter(t)!=PyInterpreterState_Main()||
       PyErr_Occurred())return -1;
    s->cold_runtime_pid=getpid();s->cold_runtime_thread=PyThread_get_thread_ident();
    s->cold_runtime_interpreter=PyThreadState_GetInterpreter(t);
    s->cold_runtime_bound=1;return 0;
}
static int command_has_owned_runtime(FridayPublisherRootStorage *s) {
    if(!s->cold_pool_started||!s->cold_runtime_bound||!Py_IsInitialized()||
       s->cold_runtime_pid!=getpid()||
       s->cold_runtime_thread!=PyThread_get_thread_ident())return 0;
    PyThreadState *t=PyThreadState_GetUnchecked();
    return t&&PyThreadState_GetInterpreter(t)==s->cold_runtime_interpreter&&
        s->cold_runtime_interpreter==PyInterpreterState_Main();
}
static int master_totals(FridayPublisherMasterPool *p,RootPoolTotals *out) {
    RootPoolTotals *v=&root_storage.pool_totals;
    if(p!=&root_storage.pool||!out||p->count>FRIDAY_MASTER_HISTORY||
       p->next_token!=p->count+1||v->rows!=p->count||
       v->source_rows>v->active_rows||v->active_rows>v->rows)return -1;
    *out=*v;return 0;
}
static int master_sum(FridayPublisherMasterPool *p,uint64_t *r,uint64_t *o,
                      uint64_t *h,uint64_t *a,uint64_t *s) {
    RootPoolTotals v;if(master_totals(p,&v)<0)return -1;
    *r=v.reads;*o=v.output;*h=v.hash;*a=v.allocation;*s=v.slots;return 0;
}
static FridayPublisherPoolRow *pool_row(FridayPublisherMasterPool *p,uint64_t token) {
    /* Existing tokens are exactly append-only ordinal+1, already required
     * by the real final reader. Token zero is NOT a bank traversal. */
    if(p!=&root_storage.pool||p->count>FRIDAY_MASTER_HISTORY||
       p->next_token!=p->count+1||!token||token>p->count)return NULL;
    FridayPublisherPoolRow *q=&p->rows[token-1];
    return q->token==token?q:NULL;
}
static int pool_row_shape(const FridayPublisherPoolRow *q,uint64_t token) {
    return q&&q->token==token&&(q->active==0||q->active==1)&&
        (q->transferred==0||q->transferred==1)&&
        (q->source_owned==0||q->source_owned==1)&&
        q->active+q->transferred==1;
}
static int pool_totals_add(RootPoolTotals *v,const FridayPublisherPoolRow *q) {
    if(!q->active)return 0;
    return plus(&v->reads,q->reads)||plus(&v->output,q->output)||
        plus(&v->hash,q->hash)||plus(&v->allocation,q->allocation)||
        plus(&v->slots,q->slots)||plus(&v->active_rows,1)||
        (q->source_owned&&plus(&v->source_rows,1))?-1:0;
}
static int pool_totals_remove(RootPoolTotals *v,const FridayPublisherPoolRow *q) {
    if(!q->active)return 0;
    if(q->reads>v->reads||q->output>v->output||q->hash>v->hash||
       q->allocation>v->allocation||q->slots>v->slots||!v->active_rows||
       (q->source_owned&&!v->source_rows))return -1;
    v->reads-=q->reads;v->output-=q->output;v->hash-=q->hash;
    v->allocation-=q->allocation;v->slots-=q->slots;v->active_rows--;
    if(q->source_owned)v->source_rows--;return 0;
}
static int pool_totals_equal(const RootPoolTotals *a,const RootPoolTotals *b) {
    return a->reads==b->reads&&a->output==b->output&&a->hash==b->hash&&
        a->allocation==b->allocation&&a->slots==b->slots&&
        a->active_rows==b->active_rows&&a->source_rows==b->source_rows&&a->rows==b->rows;
}
/* Validate ALL local arithmetic/identities before any row/totals mutation.
 * The two-row form is the actual same-pool output transfer: it cannot leave
 * only its debit performed on a later destination refusal. No API, allocator,
 * Python callback, observer or fallible operation lies in the commit stores.
 * Callers memcpy original POD rows before editing scalar fields, preserving
 * their original initialized padding in the full native historical body. */
static int pool_apply(FridayPublisherMasterPool *p,FridayPublisherPoolRow *q,
    const FridayPublisherPoolRow *next,FridayPublisherPoolRow *dest,
    const FridayPublisherPoolRow *dest_next,int append) {
    RootPoolTotals v;if(master_totals(p,&v)<0||!q||!next||
       final_caller_binding.transferred)return -1;
    uint64_t token=next->token;
    if(!token||!pool_row_shape(next,token))return -1;
    if(append) {
        if(dest||dest_next||p->count>=FRIDAY_MASTER_HISTORY||
           token!=p->next_token||q!=&p->rows[p->count]||
           q->token||q->reads||q->output||q->hash||q->allocation||q->slots||
           q->active||q->transferred||q->source_owned||!next->active)return -1;
        v.rows++;
    } else {
        if(token>p->count||q!=&p->rows[token-1]||!pool_row_shape(q,token)||
           !q->active||next->source_owned!=q->source_owned||
           pool_totals_remove(&v,q)<0)return -1;
    }
    if((dest==NULL)!=(dest_next==NULL))return -1;
    if(dest) {
        uint64_t d=dest_next->token;
        if(append||dest==q||!d||d>p->count||dest!=&p->rows[d-1]||
           !pool_row_shape(dest,d)||!dest->active||
           !pool_row_shape(dest_next,d)||dest_next->source_owned!=dest->source_owned||
           pool_totals_remove(&v,dest)<0)return -1;
    }
    if(pool_totals_add(&v,next)<0||(dest&&pool_totals_add(&v,dest_next)<0)||
       v.source_rows>v.active_rows||v.active_rows>v.rows)return -1;
    memcpy(q,next,sizeof(*q));
    if(dest)memcpy(dest,dest_next,sizeof(*dest));
    root_storage.pool_totals=v;
    if(append){p->count++;p->next_token++;}
    return 0;
}
/* Native tail observes own real absolute kernel counters without referring to
 * an observer __dict__. Child terminal observations were persisted by the
 * original Source Root before its graph could detach. Unknown is fatal.
 * Each read's MAX is checked against the same pool before the syscall. */
static int utility_io_fault(FridayPublisherMasterPool *p,const char *why) {
    p->observation_unknown=1;
    return FridayPublisherMasterFault(p,why);
}
static int own_io(FridayPublisherMasterPool *p,uint64_t *r,uint64_t *w) {
    char raw[8192];
    /* utility_refresh prospectively charges this exact read in the same
     * pool, or draws original constructor-reserved final-tail credit. */
    /* Reuse the preowned endpoint. Do not open another and do not call
     * MasterBefore from this observation. A retired generation is not replaced. */
    RootUtilityEnd *u=&root_storage.utility[0];
    if(u->kind!=1||!u->body_close.birth_valid||u->body_close.attempted||u->body_close.closed||
       utility_refresh(u,sizeof(raw)-1)<0||!u->read_completed||u->body_len>=sizeof(raw)) {
        return utility_io_fault(p,"native_root_IO_UNKNOWN");
    }
    memcpy(raw,u->body,(size_t)u->body_len);raw[u->body_len]=0;
    uint64_t rr=0,ww=0,rb=0,wb=0;unsigned seen=0;
    char *at=raw;
    while(*at) {
        char *end=strchr(at,'\n');if(!end)return utility_io_fault(p,"native_IO_raw");
        *end=0;unsigned long long value;char extra;
        if(strncmp(at,"rchar: ",7)==0&&sscanf(at+7,"%llu %c",&value,&extra)==1){rr=value;seen|=1;}
        else if(strncmp(at,"wchar: ",7)==0&&sscanf(at+7,"%llu %c",&value,&extra)==1){ww=value;seen|=2;}
        else if(strncmp(at,"read_bytes: ",12)==0&&sscanf(at+12,"%llu %c",&value,&extra)==1){rb=value;seen|=4;}
        else if(strncmp(at,"write_bytes: ",13)==0&&sscanf(at+13,"%llu %c",&value,&extra)==1){wb=value;seen|=8;}
        at=end+1;
    }
    if(seen!=15)return utility_io_fault(p,"native_IO_missing");
    *r=rr>rb?rr:rb;*w=ww>wb?ww:wb;return 0;
}
static int master_usage_floor(FridayPublisherMasterPool *p,uint64_t *reads,uint64_t *output) {
    if(!p||!reads||!output||!FridayPublisherRootBankAttached())return -1;
    uint64_t v[9];if(FridayPublisherRootBankLayout(v)<0)return -1;
    /* Raw observations remain actual Root/Source facts. They must not mask
     * the outside parent's prepaid receive/hash/copy/freeze and setup work.
     * Root's three copy/compare passes remain inside the original seven-pass
     * debit; only the four parent passes plus the fixed envelope are outside.
     * Adding the fixed envelope conservatively is not a measured-zero claim. */
    uint64_t outside_reads=4ULL*v[6]+4ULL*FRIDAY_ROOT_BANK_PARENT_SCRATCH;
    uint64_t observed_reads=p->observed_read,observed_output=p->observed_output;
    if(outside_reads>v[7]||plus(&observed_reads,outside_reads)||
       plus(&observed_output,FRIDAY_ROOT_BANK_HEADER))return -1;
    *reads=p->spent_read>observed_reads?p->spent_read:observed_reads;
    *output=p->spent_output>observed_output?p->spent_output:observed_output;
    return 0;
}
static int master_check(FridayPublisherMasterPool *p,uint64_t r,uint64_t o,
    uint64_t h,uint64_t a,uint64_t s) {
    if(!FridayPublisherMasterOwns(p)||p->refused||p->observation_unknown)
        return FridayPublisherMasterFault(p,"same_original_master_identity_or_unknown");
    uint64_t now;
    if(FridayPublisherMasterWorkClock(p,p->deadline_ns,FRIDAY_CLOCK_MASTER,&now)<0)
        return FridayPublisherMasterFault(p,"same_original_master_deadline");
    uint64_t kr,kw;if(own_io(p,&kr,&kw)<0)return -1;
    if(kr>p->observed_read)p->observed_read=kr;
    if(kw>p->observed_output)p->observed_output=kw;
    struct rusage usage;if(getrusage(RUSAGE_SELF,&usage)<0)
        return FridayPublisherMasterFault(p,"native_root_RSS_UNKNOWN");
    uint64_t own_peak=(uint64_t)usage.ru_maxrss*1024ULL;
    if(own_peak>p->observed_ram)p->observed_ram=own_peak;
    uint64_t pr,po,ph,pa,ps;if(master_sum(p,&pr,&po,&ph,&pa,&ps)<0)
        return FridayPublisherMasterFault(p,"master_pending_overflow");
    uint64_t reads,output;
    if(master_usage_floor(p,&reads,&output)<0)
        return FridayPublisherMasterFault(p,"original_parent_and_actual_native_usage");
    uint64_t ram=p->native_allocation,slots=ps;
    if(plus(&slots,p->native_live_slots))return FridayPublisherMasterFault(p,"native_slot_overflow");
    if(plus(&reads,p->spent_hash)||plus(&reads,pr)||plus(&reads,ph)||
       plus(&reads,r)||plus(&reads,h)||reads>READ_CAP||
       plus(&output,po)||plus(&output,o)||output>OUTPUT_CAP||
       plus(&ram,p->retained_allocation)||plus(&ram,pa)||plus(&ram,a)||
       /* Actual RSS may overlap declared arenas; summing is conservative.
        * Neither RSS nor a declaration is treated as the other component. */
       plus(&ram,p->observed_ram)||ram>RAM_CAP||plus(&slots,s)||slots>SLOT_CAP||
       p->observed_workers>WORKER_CAP)
        return FridayPublisherMasterFault(p,"same_master_aggregate_bound");
    return 0;
}
int FridayPublisherMasterBefore(void *opaque,uint64_t r,uint64_t o,uint64_t h,uint64_t a) {
    FridayPublisherMasterPool *p=opaque;
    if(master_check(p,r,o,h,a,0)<0)return -1;
    if(plus(&p->spent_read,r)||plus(&p->spent_output,o)||plus(&p->spent_hash,h)||
       plus(&p->native_allocation,a))return FridayPublisherMasterFault(p,"master_debit_overflow");
    return 0;
}
static int bootstrap_token_live(uint64_t token) {
    RootBootstrapState *b=&root_storage.bootstrap;
    if(!token||b->token!=token)return 0;
    for(unsigned i=0;i<b->fd_count;i++) {
        RootBootstrapFD *f=&b->fd[i];
        if(f->acquired&&(!f->body_close.closed||(f->keeper>=0&&!f->keeper_close.closed)))return 1;
    }
    return 0;
}
int FridayPublisherMasterChange(FridayPublisherMasterPool *p,const char *op,uint64_t token,
    uint64_t r,uint64_t o,uint64_t h,uint64_t a,uint64_t s,uint64_t *out) {
    if(!out||!op||!FridayPublisherMasterOwns(p))return FridayPublisherMasterFault(p,"master_change_identity");
    *out=0;
    if(!strcmp(op,"reserve")||!strcmp(op,"preowner")||!strcmp(op,"native-reserve")) {
        if(!strcmp(op,"preowner")&&p->preowner_started)return FridayPublisherMasterFault(p,"preowner_once");
        uint64_t now;
        if(p->source_detached||p->count>=FRIDAY_MASTER_HISTORY||
           FridayPublisherMasterWorkClock(p,p->work_deadline_ns,FRIDAY_CLOCK_RESERVE,&now)<0||
           master_check(p,r,o,h,a,s)<0)return FridayPublisherMasterFault(p,"master_reserve_bound");
        FridayPublisherPoolRow *q=&p->rows[p->count],next;
        memcpy(&next,q,sizeof(next));next.token=p->next_token;
        next.reads=r;next.output=o;next.hash=h;next.allocation=a;
        next.slots=s;next.active=1;next.source_owned=strcmp(op,"native-reserve")!=0;
        if(pool_apply(p,q,&next,NULL,NULL,1)<0)
            return FridayPublisherMasterFault(p,"original_pending_append_relation");
        *out=q->token;
        if(!strcmp(op,"preowner")){p->preowner_started=1;p->preowner_token=q->token;}
        return 0;
    }
    if(!strcmp(op,"spent"))return FridayPublisherMasterBefore(p,r,o,h,a);
    if(!strcmp(op,"observe")) {
        if(r>p->observed_read)p->observed_read=r;if(o>p->observed_output)p->observed_output=o;
        if(a>p->observed_ram)p->observed_ram=a;if(s>p->observed_workers)p->observed_workers=s;
        return master_check(p,0,0,0,0,0);
    }
    if(!strcmp(op,"detach")) {
        if(p->source_detached)return FridayPublisherMasterFault(p,"source_detach_once");
        RootPoolTotals pending;
        if(master_totals(p,&pending)<0)
            return FridayPublisherMasterFault(p,"source_detach_pending_original_credit");
        /* Native issuance precedes fallible PyLong/Source mirror/Reservation
         * birth. A missing returned token is STILL an original active row,
         * not malformed state and not permission to erase it. Withhold Source
         * detach without poisoning this same pool's later full graph reader.
         * The actual cold final receiver alone settles these rows, AFTER
         * Source/config/runtime/utility ownership ends and both full checks.
         * out remains zero; no row/index/counter/history or owner is changed. */
        if(pending.source_rows)return 0;
        p->source_detached=1;*out=1;return 0;
    }
    FridayPublisherPoolRow *q=pool_row(p,token);
    if(!q||!q->active)return FridayPublisherMasterFault(p,"original_pool_row_identity");
    if(!strcmp(op,"commit")) {
        if(r>q->reads||o>q->output||h>q->hash)return FridayPublisherMasterFault(p,"original_component_commit");
        uint64_t sr=p->spent_read,so=p->spent_output,sh=p->spent_hash;
        if(plus(&sr,r)||plus(&so,o)||plus(&sh,h))
            return FridayPublisherMasterFault(p,"original_commit_overflow");
        FridayPublisherPoolRow next;memcpy(&next,q,sizeof(next));
        next.reads-=r;next.output-=o;next.hash-=h;
        if(pool_apply(p,q,&next,NULL,NULL,0)<0)
            return FridayPublisherMasterFault(p,"original_pending_commit_relation");
        p->spent_read=sr;p->spent_output=so;p->spent_hash=sh;return 0;
    }
    if(!strcmp(op,"release")) {
        for(uint64_t i=0;i<root_storage.prepared_count;i++) {
            FridayPublisherPreparedRow *fd=&root_storage.prepared[i];
            if(fd->token==token&&fd->opened&&!fd->keeper_closed)return 0;
        }
        if(bootstrap_token_live(token))return 0;
        /* Full historical aliases transferred to native are STILL retained;
         * only UNUSED prospective IO/slots disappear. No RAM/cumulative refund. */
        uint64_t retained=p->retained_allocation;
        if(plus(&retained,q->allocation))
            return FridayPublisherMasterFault(p,"retained_history_allocation_overflow");
        FridayPublisherPoolRow next;memcpy(&next,q,sizeof(next));
        next.active=0;next.transferred=1;
        if(pool_apply(p,q,&next,NULL,NULL,0)<0)
            return FridayPublisherMasterFault(p,"original_pending_release_relation");
        p->retained_allocation=retained;*out=1;return 0;
    }
    if(!strcmp(op,"grow")) {
        FridayPublisherPoolRow next;memcpy(&next,q,sizeof(next));
        if(master_check(p,0,0,0,a,0)<0||plus(&next.allocation,a)||
           pool_apply(p,q,&next,NULL,NULL,0)<0)
            return FridayPublisherMasterFault(p,"original_allocation_growth");return 0;
    }
    if(!strcmp(op,"retain")) {
        if(a>q->allocation)return FridayPublisherMasterFault(p,"recipient_allocation_lifetime");
        /* Narrowing a wait4-ended child maximum does not refund retained data.
         * Keep excess in retained RAM until original outside owner is proven. */
        uint64_t retained=p->retained_allocation;
        if(plus(&retained,q->allocation-a))
            return FridayPublisherMasterFault(p,"recipient_retained_allocation_overflow");
        FridayPublisherPoolRow next;memcpy(&next,q,sizeof(next));next.allocation=a;
        if(pool_apply(p,q,&next,NULL,NULL,0)<0)
            return FridayPublisherMasterFault(p,"original_pending_retain_relation");
        p->retained_allocation=retained;return 0;
    }
    if(!strcmp(op,"slots")){
        for(uint64_t i=0;i<root_storage.prepared_count;i++) {
            FridayPublisherPreparedRow *fd=&root_storage.prepared[i];
            if(fd->token==token&&fd->opened&&!fd->keeper_closed)
                return FridayPublisherMasterFault(p,"native_prepared_slots_still_live");
        }
        if(bootstrap_token_live(token))
            return FridayPublisherMasterFault(p,"native_bootstrap_slots_still_live");
        FridayPublisherPoolRow next;memcpy(&next,q,sizeof(next));next.slots=0;
        if(pool_apply(p,q,&next,NULL,NULL,0)<0)
            return FridayPublisherMasterFault(p,"original_pending_slots_relation");
        return 0;
    }
    if(!strcmp(op,"transfer")) {
        FridayPublisherPoolRow *dest=pool_row(p,r);
        if(!dest||!dest->active||dest==q||o>q->output||o>UINT64_MAX-dest->output)
            return FridayPublisherMasterFault(p,"unused_same_pool_transfer");
        FridayPublisherPoolRow next,other;
        memcpy(&next,q,sizeof(next));memcpy(&other,dest,sizeof(other));
        next.output-=o;other.output+=o;
        if(pool_apply(p,q,&next,dest,&other,0)<0)
            return FridayPublisherMasterFault(p,"original_pending_transfer_relation");
        return 0;
    }
    return FridayPublisherMasterFault(p,"master_operation");
}
static int scalar_u64(PyObject *v,uint64_t *out) {
    if(!v||!PyLong_CheckExact(v))return -1;
    unsigned long long n=PyLong_AsUnsignedLongLong(v);
    if(PyErr_Occurred())return -1;*out=(uint64_t)n;return 0;
}
static int stat_equal(const struct stat *a,const struct stat *b,int full) {
    return a->st_dev==b->st_dev&&a->st_ino==b->st_ino&&a->st_mode==b->st_mode&&
        a->st_uid==b->st_uid&&a->st_gid==b->st_gid&&a->st_nlink==b->st_nlink&&
        (!full||(a->st_size==b->st_size&&a->st_mtim.tv_sec==b->st_mtim.tv_sec&&
        a->st_mtim.tv_nsec==b->st_mtim.tv_nsec&&a->st_ctim.tv_sec==b->st_ctim.tv_sec&&
        a->st_ctim.tv_nsec==b->st_ctim.tv_nsec));
}
static FridayPublisherPreparedRow *prepared_row(PyObject *row) {
    for(uint64_t i=0;i<root_storage.prepared_count;i++)
        if(root_storage.prepared[i].row==row)return &root_storage.prepared[i];
    return NULL;
}
/* Exact registered producer state, not mutable row booleans or an integer.
 * No Python factory or callback is used between validation and close.
 * The selected image must independently qualify exclusive same-thread FD
 * ownership and KCMP_FILE. Unsupported/denied comparison is NOT a match.
 */
static int root_description_close(FridayPublisherRootCloseFact *f,int fd,int keeper) {
    if(f->attempted)return f->closed?0:-1;
    if(f->validation_attempted||!f->birth_valid||f->fd!=fd||f->keeper!=keeper||
       fd<0||keeper<0||fd==keeper)return -1;
    f->validation_attempted=1;
    memset(&f->current,0,sizeof(f->current));
    f->validation_rc=fstat(fd,&f->current);
    f->validation_errno=f->validation_rc<0?errno:0;
    if(f->validation_rc<0||!stat_equal(&f->current,&f->birth,0))return -1;
    f->keeper_validation_attempted=1;
    f->keeper_validation_rc=fstat(keeper,&f->keeper_current);
    f->keeper_validation_errno=f->keeper_validation_rc<0?errno:0;
    if(f->keeper_validation_rc<0||!stat_equal(&f->keeper_current,&f->birth,0))return -1;
#ifdef SYS_kcmp
    f->description_attempted=1;
    f->description_rc=(int)syscall(SYS_kcmp,getpid(),getpid(),0,fd,keeper);
    f->description_errno=f->description_rc<0?errno:0;
#else
    f->description_rc=-1;f->description_errno=ENOTSUP;
#endif
    if(f->description_rc!=0)return -1;
    f->attempted=1;f->rc=close(fd);
    f->original_errno=f->rc<0?errno:0;f->closed=f->rc==0;
    return f->closed?0:-1; /* never retry EINTR/EBADF or an uncertain generation */
}
/* A keeper is private registered Root state, never returned as a Source FD.
 * It can retire only AFTER the original body generation has a known close.
 * Every validation and close return is stored before any later operation.
 * A denied/failed validation leaves the real keeper owned and NEVER retried.
 */
static int root_keeper_close(const FridayPublisherRootCloseFact *body,int fd,
    FridayPublisherRootCloseFact *f) {
    if(f->attempted)return f->closed?0:-1;
    if(f->validation_attempted||!body->closed||!body->birth_valid||
       fd<0||fd!=body->keeper)return -1;
    f->fd=fd;f->keeper=-1;f->generation=body->generation;f->birth=body->birth;
    f->birth_valid=1;f->validation_attempted=1;
    f->validation_rc=fstat(fd,&f->current);f->validation_errno=f->validation_rc<0?errno:0;
    if(f->validation_rc<0||!stat_equal(&f->birth,&f->current,0))return -1;
    f->attempted=1;f->rc=close(fd);f->original_errno=f->rc<0?errno:0;
    f->closed=f->rc==0;return f->closed?0:-1;
}
int FridayPublisherRootOwnedRowCloseFact(FridayPublisherMasterPool *p,PyObject *row,
    FridayPublisherRootCloseFact *out) {
    FridayPublisherRootStorage *s=&root_storage;
    if(!out||!FridayPublisherMasterOwns(p)||!row)return -1;
    if(row==s->bindings.final_fd_row) {*out=s->final_owner_close;return 1;}
    FridayPublisherPreparedRow *o=prepared_row(row);
    if(!o)return -1;*out=o->body_close;return 1;
}
int FridayPublisherRootCloseOwnedRow(FridayPublisherMasterPool *p,PyObject *row,
    PyObject *credit,int fd,FridayPublisherRootCloseFact *out) {
    FridayPublisherRootStorage *s=&root_storage;
    if(!out||!FridayPublisherMasterOwns(p)||PyErr_Occurred())return -1;
    FridayPublisherRootCloseFact *f=NULL;
    int final=row&&row==s->bindings.final_fd_row&&credit==s->bindings.final_fd_credit;
    if(final) {
        if(fd!=s->final_fd||!s->final_keeper_valid)return -1;
        f=&s->final_owner_close;
    } else {
        FridayPublisherPreparedRow *o=prepared_row(row);
        if(!o||!o->opened||o->row!=row||o->credit!=credit||o->fd!=fd)return -1;
        f=&o->body_close;
        if(f->generation!=o->generation)return -1;
    }
    int was_closed=f->closed;
    int rc=root_description_close(f,fd,f->keeper);
    *out=*f; /* actual fact BEFORE any later Python metadata/error publication */
    if(final&&!was_closed&&f->closed&&p->native_live_slots)p->native_live_slots--;
    return rc;
}
int FridayPublisherPreparedHasRow(FridayPublisherMasterPool *p,PyObject *row) {
    FridayPublisherPreparedRow *owned=prepared_row(row);
    return FridayPublisherMasterOwns(p)&&owned&&owned->opened;
}
int FridayPublisherPreparedCreate(FridayPublisherMasterPool *p,int dirfd,const char *name,
    PyObject *row,PyObject *credit) {
    uint64_t token,generation;struct stat directory;
    if(!FridayPublisherMasterOwns(p)||p->source_detached||!name||!name[0]||
       strlen(name)>240||strchr(name,'/')||!strcmp(name,".")||!strcmp(name,"..")||
       !PyDict_CheckExact(row)||prepared_row(row)||root_storage.prepared_count>=FRIDAY_NATIVE_FD_HISTORY)
        return FridayPublisherMasterFault(p,"actual_prepared_birth_identity");
    PyObject *tokenobj=PyObject_GetAttrString(credit,"token");
    int ok=scalar_u64(tokenobj,&token)==0;Py_XDECREF(tokenobj);
    if(!ok||scalar_u64(PyDict_GetItemString(row,"generation"),&generation)<0)
        return FridayPublisherMasterFault(p,"actual_prepared_credit_scalars");
    FridayPublisherPoolRow *q=pool_row(p,token);uint64_t native_occupied=0;
    for(uint64_t i=0;i<root_storage.prepared_count;i++) {
        FridayPublisherPreparedRow *prior=&root_storage.prepared[i];
        if(prior->token==token&&prior->opened&&!prior->keeper_closed)native_occupied+=2;
    }
    if(!q||!q->active||q->slots<2||native_occupied>q->slots-2||fstat(dirfd,&directory)<0||
       !stat_equal(&directory,&root_storage.output_root_identity,0))
        return FridayPublisherMasterFault(p,"actual_prepared_root_and_original_pool");
    if(FridayPublisherMasterBefore(p,0,0,0,sizeof(FridayPublisherPreparedRow)+512)<0)return -1;
    FridayPublisherPreparedRow *owned=&root_storage.prepared[root_storage.prepared_count++];
    owned->row=Py_NewRef(row);owned->credit=Py_NewRef(credit);owned->token=token;
    owned->generation=generation;owned->fd=owned->keeper=-1;
    /* Record and full row/credit custody exist BEFORE the actual openat. */
    int fd=openat(dirfd,name,O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    if(fd<0){owned->keeper_closed=1;PyErr_SetFromErrno(PyExc_OSError);return -1;}
    owned->opened=1;owned->fd=fd;
    /* Independent actual open-file-description keeper; not Source enrollment.
     * Descriptor number reuse alone can NEVER authenticate this endpoint. */
    owned->keeper_open_attempted=1;
    owned->keeper=fcntl(fd,F_DUPFD_CLOEXEC,3);owned->keeper_open_rc=owned->keeper;
    owned->keeper_open_errno=owned->keeper<0?errno:0;
    if(owned->keeper>=0) {
        owned->birth_stat_attempted=1;owned->birth_stat_rc=fstat(fd,&owned->birth);
        owned->birth_stat_errno=owned->birth_stat_rc<0?errno:0;
    }
    if(owned->keeper<0||owned->birth_stat_rc<0) {
        /* Actual fd stays owned by this native record on post-open failure.
         * No blind close/retry and no NO_RETURN representation. */
        PyErr_SetFromErrno(PyExc_OSError);return -1;
    }
    owned->body_close.generation=owned->generation;
    owned->body_close.fd=fd;owned->body_close.keeper=owned->keeper;
    owned->body_close.birth=owned->birth;owned->body_close.birth_valid=1;
    return fd;
}
int FridayPublisherPreparedMatches(FridayPublisherMasterPool *p,PyObject *row,
    PyObject *credit,int fd) {
    FridayPublisherPreparedRow *o=prepared_row(row);struct stat actual;
    uint64_t generation,token;
    if(!FridayPublisherMasterOwns(p)||!o||o->credit!=credit||o->fd!=fd||
       scalar_u64(field(row,"generation"),&generation)<0||generation!=o->generation||
       scalar_u64(field(row,"credit"),&token)<0||token!=o->token||
       !o->opened||o->keeper_attempted||o->keeper<0||fstat(fd,&actual)<0||
       !stat_equal(&actual,&o->birth,0))return FridayPublisherMasterFault(p,"actual_prepared_generation");
#ifdef SYS_kcmp
    /* Public Linux comparison of TWO OWNED same-process descriptions, not
     * ptrace/private heap. Refusal/EPERM is STOP, never a guessed match or a
     * request to add capabilities. Actual image support remains C2/ABI open. */
    if(syscall(SYS_kcmp,getpid(),getpid(),0 /* KCMP_FILE */,fd,o->keeper)!=0)
        return FridayPublisherMasterFault(p,"owned_description_compare_UNCONFIRMED");
#else
    return FridayPublisherMasterFault(p,"owned_description_compare_NOT_PRESENT_CODE");
#endif
    return 1;
}
int FridayPublisherPreparedKeeperClose(FridayPublisherMasterPool *p,PyObject *row) {
    FridayPublisherPreparedRow *o=prepared_row(row);
    if(!FridayPublisherMasterOwns(p)||!o||!o->body_close.closed||o->keeper_attempted||o->keeper<0)
        return FridayPublisherMasterFault(p,"native_keeper_close_once");
    int rc=root_keeper_close(&o->body_close,o->keeper,&o->keeper_close);
    o->keeper_attempted=o->keeper_close.attempted;o->keeper_rc=o->keeper_close.rc;
    o->keeper_errno=o->keeper_close.original_errno;o->keeper_closed=o->keeper_close.closed;
    if(rc<0&&!o->keeper_attempted)
        return FridayPublisherMasterFault(p,"native_keeper_current_identity_UNKNOWN");
    if(!o->keeper_closed){errno=o->keeper_errno;PyErr_SetFromErrno(PyExc_OSError);return -1;}
    return 0;
}

static PyObject *field(PyObject *o,const char *key) {
    return o&&PyDict_CheckExact(o)?PyDict_GetItemString(o,key):NULL;
}
static int ascii(PyObject *v,const char *s) {
    return v&&PyUnicode_CheckExact(v)&&PyUnicode_CompareWithASCIIString(v,s)==0;
}
static int eq(PyObject *a,PyObject *b) {
    return a&&b&&Py_TYPE(a)==Py_TYPE(b)&&PyObject_RichCompareBool(a,b,Py_EQ)==1;
}
static int exact_keys(PyObject *d,const char *const *names,size_t count) {
    if(!PyDict_CheckExact(d)||(size_t)PyDict_Size(d)!=count)return 0;
    for(size_t i=0;i<count;i++)if(!field(d,names[i]))return 0;return 1;
}
static PyObject *stat9(const struct stat *s) {
    char b[9][64];unsigned long long values[7]={
        (unsigned long long)s->st_dev,(unsigned long long)s->st_ino,
        (unsigned long long)s->st_mode,(unsigned long long)s->st_uid,
        (unsigned long long)s->st_gid,(unsigned long long)s->st_nlink,
        (unsigned long long)s->st_size};
    PyObject *a=PyList_New(9);if(!a)return NULL;
    for(int i=0;i<7;i++)snprintf(b[i],sizeof(b[i]),"%llu",values[i]);
    __int128 ns[2]={(__int128)s->st_mtim.tv_sec*1000000000+s->st_mtim.tv_nsec,
                    (__int128)s->st_ctim.tv_sec*1000000000+s->st_ctim.tv_nsec};
    for(int i=0;i<2;i++) {
        char reverse[64];unsigned at=0;int negative=ns[i]<0;
        unsigned __int128 value=negative?(unsigned __int128)(-ns[i]):(unsigned __int128)ns[i];
        do{reverse[at++]=(char)('0'+value%10);value/=10;}while(value);
        unsigned start=0;if(negative)b[i+7][start++]='-';
        while(at)b[i+7][start++]=reverse[--at];b[i+7][start]=0;
    }
    for(int i=0;i<9;i++){PyObject *v=PyUnicode_FromString(b[i]);if(!v){Py_DECREF(a);return NULL;}PyList_SET_ITEM(a,i,v);}
    return a;
}
static int stat_pin(const struct stat *s,PyObject *pin) {
    PyObject *a=stat9(s);if(!a)return 0;
    int same=eq(a,field(pin,"identity9_decimal_strings"));Py_DECREF(a);return same;
}

static int slots_free(uint64_t need) {
    FridayPublisherMasterPool *p=&root_storage.pool;
    uint64_t r,o,h,a,pending;
    if(master_sum(p,&r,&o,&h,&a,&pending)<0||pending>SLOT_CAP||
       need>SLOT_CAP-pending)return 0;
    return p->native_live_slots<=SLOT_CAP-pending-need;
}
static uint64_t take_generation(void) {
    /* Literal final-row generation 1 stays the pre-existing terminal contract.
     * This clock never reissues that value. */
    if(root_storage.generation_clock>=UINT64_MAX-1)return 0;
    uint64_t g=++root_storage.generation_clock;
    if(g==1)g=++root_storage.generation_clock;
    return g;
}
static void uncount_slot(int *flag) {
    if(flag&&*flag) {
        if(root_storage.pool.native_live_slots)root_storage.pool.native_live_slots--;
        *flag=0;
    }
}
/* Keeper is a same-process CLOEXEC duplicate, never a Source FD and never
 * authority by itself. Open success with a failed dup or birth stat keeps
 * both descriptors owned. No integer close and no retry follows. */
static int bind_generation(uint64_t generation,int fd,int *keeper,
    int *k_att,int *k_rc,int *k_err,int *b_att,int *b_rc,int *b_err,
    struct stat *birth,FridayPublisherRootCloseFact *body,int *live_keeper) {
    if(!k_att||!b_att||!keeper||!birth||!body||*k_att||*b_att||fd<0||!generation)return -1;
    *k_att=1;
    if(!slots_free(1)){*keeper=-1;*k_rc=-1;*k_err=EMFILE;return -1;}
    int k=fcntl(fd,F_DUPFD_CLOEXEC,3);
    *keeper=k;*k_rc=k;*k_err=k<0?errno:0;
    if(k<0)return -1;
    root_storage.pool.native_live_slots++;
    if(live_keeper)*live_keeper=1;
    *b_att=1;*b_rc=fstat(fd,birth);*b_err=*b_rc<0?errno:0;
    if(*b_rc<0)return -1;
    memset(body,0,sizeof(*body));
    body->generation=generation;body->fd=fd;body->keeper=k;
    body->birth=*birth;body->birth_valid=1;
    return 0;
}
static int close_body_once(FridayPublisherRootCloseFact *body,int *live_body) {
    if(!body||!body->birth_valid)return -1;
    if(body->closed)return 0;
    if(body->attempted||body->validation_attempted)return -1;
    (void)root_description_close(body,body->fd,body->keeper);
    if(body->closed)uncount_slot(live_body);
    return body->closed?0:-1;
}
static int close_keeper_once(FridayPublisherRootCloseFact *body,
    FridayPublisherRootCloseFact *keeper,int *live_keeper) {
    if(!body||!keeper||!body->closed||body->keeper<0)return -1;
    if(keeper->closed)return 0;
    if(keeper->attempted||keeper->validation_attempted)return -1;
    (void)root_keeper_close(body,body->keeper,keeper);
    if(keeper->closed)uncount_slot(live_keeper);
    return keeper->closed?0:-1;
}
static int close_generation(FridayPublisherRootCloseFact *body,
    FridayPublisherRootCloseFact *keeper,int *live_body,int *live_keeper) {
    if(close_body_once(body,live_body)<0)return -1;
    return close_keeper_once(body,keeper,live_keeper);
}
static int utility_open_one(RootUtilityEnd *u,int kind,const char *path,int flags) {
    if(u->open_attempted)return u->body_close.birth_valid?0:-1;
    memset(u,0,sizeof(*u));
    u->kind=kind;u->fd=-1;u->keeper=-1;u->open_attempted=1;
    if(!slots_free(2)){u->open_rc=-1;u->open_errno=EMFILE;return -1;}
    uint64_t generation=take_generation();
    if(!generation){u->open_rc=-1;u->open_errno=EOVERFLOW;return -1;}
    u->generation=generation;
    int fd=open(path,flags);
    u->open_rc=fd;u->open_errno=fd<0?errno:0;u->fd=fd;
    if(fd<0)return -1;
    /* Prospective room was checked before open. The born descriptor counts
     * immediately. A later dup or birth fstat failure does not close it. */
    u->acquired=1;root_storage.pool.native_live_slots++;u->live_body=1;
    return bind_generation(generation,fd,&u->keeper,
        &u->keeper_open_attempted,&u->keeper_open_rc,&u->keeper_open_errno,
        &u->birth_stat_attempted,&u->birth_stat_rc,&u->birth_stat_errno,
        &u->birth,&u->body_close,&u->live_keeper);
}
static int utility_read_room(FridayPublisherMasterPool *p,uint64_t n) {
    FridayPublisherRootStorage *s=&root_storage;
    if(s->utility_tail_active) {
        if(!s->utility_tail_reserved||n>s->utility_tail_remaining)return -1;
        s->utility_tail_remaining-=n;return 0; /* already charged at pool birth */
    }
    uint64_t pr,po,ph,pa,ps;
    if(master_sum(p,&pr,&po,&ph,&pa,&ps)<0)return -1;
    uint64_t total,output;
    if(master_usage_floor(p,&total,&output)<0)return -1;
    if(plus(&total,p->spent_hash)||plus(&total,pr)||plus(&total,ph)||
       plus(&total,n)||total>READ_CAP)return -1;
    return plus(&p->spent_read,n); /* full upper BEFORE the actual read */
}
static int utility_refresh(RootUtilityEnd *u,uint64_t max_n) {
    if(!u||!u->body_close.birth_valid||u->fd<0||u->body_close.attempted||
       u->body_close.closed||!max_n||max_n>=sizeof(u->body))return -1;
    if(u->refresh_sequence==UINT64_MAX)return -1;
    u->refresh_sequence++;u->refresh_refusal=0;
    u->seek_attempted=0;u->seek_result=0;u->seek_errno=0;
    u->read_attempted=0;u->read_rc=0;u->read_errno=0;u->read_used=0;u->read_completed=0;
    FridayPublisherMasterPool *p=&root_storage.pool;
    if(utility_read_room(p,max_n)<0){u->refresh_refusal=2;return -1;}
    u->seek_attempted=1;
    off_t pos=lseek(u->fd,0,SEEK_SET);
    u->seek_result=(int64_t)pos;u->seek_errno=pos==(off_t)-1?errno:0;
    if(pos!=(off_t)0){u->refresh_refusal=3;return -1;}
    char tmp[8193];
    u->read_completed=0;u->read_attempted=1;
    ssize_t n=pread(u->fd,tmp,(size_t)max_n,0);
    u->read_rc=n;u->read_errno=n<0?errno:0;u->read_used=n>0?(uint64_t)n:0;
    u->read_completed=n>0&&(uint64_t)n<max_n;
    if(n>0) {
        memcpy(u->prefix,tmp,(size_t)n);u->prefix_len=(uint64_t)n;u->prefix_positive=1;
        u->prefix_sequence=u->refresh_sequence;
        if(u->read_completed) {
            memcpy(u->body,tmp,(size_t)n);u->body_len=(uint64_t)n;u->body_complete=1;
            u->body_sequence=u->refresh_sequence;
        }
        /* read_used is factual. Requested upper was charged before pread;
         * no unused/cumulative IO refund and no second post-effect debit. */
    }
    if(!u->read_completed)u->refresh_refusal=4;
    return u->read_completed?0:-1;
}
static int utility_preown(FridayPublisherRootStorage *s) {
    if(s->utility_preowned)return s->utility[0].body_close.birth_valid?0:-1;
    s->utility_preowned=1;
    static const int kinds[ROOT_UTILITY_COUNT]={1,2,3};
    static const char *const paths[ROOT_UTILITY_COUNT]={
        "/proc/self/io","/proc/self/stat","/proc/self/exe"};
    static const int flags[ROOT_UTILITY_COUNT]={
        O_RDONLY|O_NOFOLLOW|O_CLOEXEC,
        O_RDONLY|O_CLOEXEC|O_NOFOLLOW,
        O_RDONLY|O_CLOEXEC};
    static const uint64_t widths[ROOT_UTILITY_COUNT]={8191,8192,0};
    int bad=0;
    for(int i=0;i<ROOT_UTILITY_COUNT;i++) {
        RootUtilityEnd *u=&s->utility[i];
        if(utility_open_one(u,kinds[i],paths[i],flags[i])<0)bad=1;
        else if(widths[i]&&utility_refresh(u,widths[i])<0)bad=1;
    }
    return bad?-1:0;
}
static int utility_end_clock(FridayPublisherRootUtilityReceipt *r,uint64_t *out) {
    if(r->clock_failed)return -1;
    if(FridayPublisherMasterClockBlocked(&root_storage.pool)) {
        r->clock_failed=1;r->clock_fault_kind=4;return -1;
    }
    r->clock_attempted=1;
    int rc=FridayPublisherNativeClockSample(&r->clock_original,root_storage.pool.started_ns,
        root_storage.pool.deadline_ns,FRIDAY_CLOCK_UTILITY,out);
    r->clock_rc=r->clock_original.rc;r->clock_errno=r->clock_original.error;
    if(rc<0) {
        r->clock_failed=1;r->clock_fault_kind=r->clock_original.fault_kind;return -1;
    }
    return 0;
}
static int utility_full_native_read(FridayPublisherRootUtilityReceipt *r,
                                    const void *data,uint64_t bytes) {
    const unsigned char *p=data;
    for(uint64_t i=0;i<bytes;i++) {
        if((i&65535)==0) {
            uint64_t now;if(utility_end_clock(r,&now)<0)return -1;
        }
        r->reader_sink^=p[i];r->full_native_bytes_read++;
    }
    return 0;
}
static int utility_final_bounds(FridayPublisherRootUtilityReceipt *r,
                                FridayPublisherMasterPool *p) {
    uint64_t pr,po,ph,pa,ps;
    r->resource_bounds_checked=1;
    RootPoolTotals indexed,actual={0};
    if(!root_storage.utility_tail_active||master_totals(p,&indexed)<0||
       utility_read_room(p,8ULL*p->count*sizeof(FridayPublisherPoolRow)+4096ULL)<0)return -1;
    actual.rows=p->count;
    /* Full original historical rows, not the maintained index alone.
     * This one finite scan was paid at the original constructor, before
     * any Source/utility effect. No observer is reopened after its close. */
    for(uint64_t i=0;i<p->count;i++) {
        if((i&1023)==0){uint64_t now;if(utility_end_clock(r,&now)<0)return -1;}
        const FridayPublisherPoolRow *q=&p->rows[i];
        if(!pool_row_shape(q,i+1)||pool_totals_add(&actual,q)<0)return -1;
    }
    if(!pool_totals_equal(&actual,&indexed)||
       master_sum(p,&pr,&po,&ph,&pa,&ps)<0)return -1;
    if(master_usage_floor(p,&r->bound_reads,&r->bound_output)<0)return -1;
    r->bound_ram=p->native_allocation;r->bound_slots=ps;
    if(plus(&r->bound_reads,p->spent_hash)||plus(&r->bound_reads,pr)||plus(&r->bound_reads,ph)||
       plus(&r->bound_output,po)||plus(&r->bound_ram,p->retained_allocation)||
       plus(&r->bound_ram,pa)||plus(&r->bound_ram,p->observed_ram)||
       plus(&r->bound_slots,p->native_live_slots))return -1;
    r->resource_bounds_confirmed=r->bound_reads<=READ_CAP&&r->bound_output<=OUTPUT_CAP&&
        r->bound_ram<=RAM_CAP&&r->bound_slots<=SLOT_CAP&&p->observed_workers<=WORKER_CAP;
    return r->resource_bounds_confirmed?0:-1;
}
int FridayPublisherRootUtilityFinish(int last_consumer,const FridayPublisherRootUtilityReceipt **out) {
    if(!FridayPublisherRootBankAttached())return -1;
    FridayPublisherRootStorage *s=&root_storage;
    FridayPublisherMasterPool *p=&s->pool;
    FridayPublisherRootUtilityReceipt *r=&s->utility_result;
    if(!out||!s->cold_pool_started||p->pid!=getpid())return -1;
    if(r->attempted) {if(r->sealed){*out=r;return 0;}return -1;}
    r->schema=249;r->bytes=sizeof(*r);r->pid=getpid();r->attempted=1;
    r->last_consumer_confirmed=last_consumer==1;
    r->phase="actual_last_native_utility_consumer";
    r->tail_credit_reserved=s->utility_tail_reserved;
    r->tail_read_reserved=s->utility_tail_total;
    r->originals_retained=1;
    if(!p->initialized||!s->utility_tail_reserved) {
        r->phase="utility_original_pool_or_tail_UNCONFIRMED";goto sealed;
    }
    if(utility_end_clock(r,&r->started_ns)<0) {
        r->phase="utility_original_end_clock_UNCONFIRMED";goto sealed;
    }
    s->utility_tail_active=1;
    for(int i=0;i<ROOT_UTILITY_COUNT;i++)
        if(s->utility[i].acquired)r->acquired_endpoints++;
    if(!r->last_consumer_confirmed) {
        /* Publish the actual held endpoint state with original prepaid
         * copy credit, but leave every live support descriptor untouched.
         * No new observation replaces an unresolved earlier consumer. */
        r->uncertain_endpoints=r->acquired_endpoints;
        r->phase="utility_original_users_still_owned_support_retained";
        goto retained_snapshot;
    }
    /* The constructor charged this exact finite final read/copy allowance
     * before ANY utility open. No new grant or observation after closing its
     * own observer. A pre-existing unknown observation is never retried. */
    if(!p->observation_unknown) {
        uint64_t read=0,written=0;
        r->final_observation_attempted=1;
        r->final_observation_rc=own_io(p,&read,&written);
        if(r->final_observation_rc==0) {
            if(read>p->observed_read)p->observed_read=read;
            if(written>p->observed_output)p->observed_output=written;
            r->final_observation_complete=1;
        }
    }
    r->phase="actual_utility_generation_retirement_after_last_budget_user";
    for(int i=0;i<ROOT_UTILITY_COUNT;i++) {
        RootUtilityEnd *u=&s->utility[i];
        if(!u->acquired)continue;
        uint64_t now;if(utility_end_clock(r,&now)<0) {
            r->phase="utility_before_close_original_clock_UNCONFIRMED";goto sealed;
        }
        if(u->body_close.birth_valid)
            (void)close_generation(&u->body_close,&u->keeper_close,&u->live_body,&u->live_keeper);
        if(u->body_close.closed&&u->keeper_close.closed)
            r->confirmed_endpoints++;
        else r->uncertain_endpoints++;
    }
    r->utility_FD_end_confirmed=r->confirmed_endpoints==r->acquired_endpoints&&
        !r->uncertain_endpoints;
retained_snapshot:
    /* Native no-FD observations remain finite. Captured return/error data
     * belong to THIS final receipt, not a mutation of the Source receipt. */
    r->final_usage_attempted=1;
    r->final_usage_rc=getrusage(RUSAGE_SELF,&r->final_usage);
    r->final_usage_errno=r->final_usage_rc<0?errno:0;
    if(r->final_usage_rc==0&&r->final_usage.ru_maxrss>=0&&
       (uint64_t)r->final_usage.ru_maxrss<=UINT64_MAX/1024ULL) {
        uint64_t ram=(uint64_t)r->final_usage.ru_maxrss*1024ULL;
        if(ram>p->observed_ram)p->observed_ram=ram;
        r->final_usage_complete=1;
    }
    (void)utility_final_bounds(r,p);
    r->phase="full_actual_native_utility_and_final_pool_body";
    uint64_t copied=sizeof(r->pool_snapshot)+sizeof(r->endpoints);
    if(utility_read_room(p,4ULL*(copied+sizeof(r->pool_fault_body)))<0) {
        r->phase="utility_prepaid_body_credit_UNCONFIRMED";goto sealed;
    }
    /* MasterFault/boot_error store a selected native cause in the enrolled
     * image, not a Python repr. Preserve the complete cause before exposing
     * the snapshot; its historical address is never a value-body substitute. */
    r->pool_fault_present=p->fault!=NULL;
    if(p->fault) {
        size_t n=strnlen(p->fault,sizeof(r->pool_fault_body));
        if(n==sizeof(r->pool_fault_body)) {
            r->phase="utility_full_native_cause_bound_UNCONFIRMED";goto sealed;
        }
        memcpy(r->pool_fault_body,p->fault,n+1);
        if(memcmp(r->pool_fault_body,p->fault,n+1)) {
            r->phase="utility_native_cause_correspondence_UNCONFIRMED";goto sealed;
        }
        r->pool_fault_bytes=(uint64_t)n+1;
    }
    r->pool_fault_complete=1;copied+=r->pool_fault_bytes;
    memcpy(&r->pool_snapshot,p,sizeof(*p));
    memcpy(r->endpoints,s->utility,sizeof(s->utility));
    if(memcmp(&r->pool_snapshot,p,sizeof(*p))||
       memcmp(r->endpoints,s->utility,sizeof(s->utility))) {
        r->phase="utility_actual_native_copy_correspondence_UNCONFIRMED";goto sealed;
    }
    if(utility_full_native_read(r,&r->pool_snapshot,sizeof(r->pool_snapshot))<0||
       utility_full_native_read(r,r->endpoints,sizeof(r->endpoints))<0||
       utility_full_native_read(r,r->pool_fault_body,r->pool_fault_bytes)<0) {
        r->phase="utility_full_native_body_read_UNCONFIRMED";goto sealed;
    }
    r->copy_complete=r->full_native_bytes_read==copied;
    r->clock_complete=utility_end_clock(r,&r->finished_ns)==0;
    r->complete=r->last_consumer_confirmed&&r->utility_FD_end_confirmed&&
        r->copy_complete&&r->clock_complete&&
        r->final_observation_complete&&r->final_usage_complete&&r->resource_bounds_confirmed&&
        r->pool_fault_complete;
    r->phase=r->complete?"actual_native_utility_tail_received":
        "actual_native_utility_tail_partial_originals_retained";
sealed:
    r->tail_read_remaining=s->utility_tail_remaining;
    s->utility_tail_active=0;s->utility_accounted=1;
    r->sealed=1;*out=r;
    /* RootStorage/full originals stay preowned even after this read. This
     * scoped tail receipt is not whole caller completion, release GO, or a
     * claim about unrepresented Source references/private runtime heaps. */
    return 0;
}
static RootHeldFile *held_begin(void) {
    if(root_storage.held_count>=FRIDAY_ROOT_HELD_FILES||!slots_free(2))return NULL;
    uint64_t generation=take_generation();
    if(!generation)return NULL;
    RootHeldFile *h=&root_storage.held[root_storage.held_count++];
    memset(h,0,sizeof(*h));
    h->fd=-1;h->keeper=-1;h->generation=generation;
    return h;
}
static int held_consume_open(RootHeldFile *h,int fd) {
    if(!h)return -1;
    h->fd=fd;
    if(fd<0)return -1;
    h->acquired=1;
    if(!slots_free(1))return -1;
    root_storage.pool.native_live_slots++;
    h->live_body=1;
    return bind_generation(h->generation,fd,&h->keeper,
        &h->keeper_open_attempted,&h->keeper_open_rc,&h->keeper_open_errno,
        &h->birth_stat_attempted,&h->birth_stat_rc,&h->birth_stat_errno,
        &h->birth,&h->body_close,&h->live_keeper);
}
static int number_is_open_witness(int fd) {
    if(fd<0)return 0;
    int n=0;
    for(uint64_t i=0;i<root_storage.held_count;i++) {
        RootHeldFile *h=&root_storage.held[i];
        if(h->acquired&&h->keeper==fd&&!h->keeper_close.closed)n++;
    }
    for(uint64_t i=0;i<root_storage.image_inventory.fd_count;i++) {
        RootImageFD *f=&root_storage.image_inventory.fds[i];
        if(f->acquired&&f->keeper==fd&&!f->keeper_close.closed)n++;
    }
    RootBootstrapState *b=&root_storage.bootstrap;
    for(unsigned i=0;i<b->fd_count;i++) {
        RootBootstrapFD *f=&b->fd[i];
        if(f->acquired&&f->keeper==fd&&!f->keeper_close.closed)n++;
    }
    for(int i=0;i<ROOT_UTILITY_COUNT;i++) {
        RootUtilityEnd *u=&root_storage.utility[i];
        if(!u->acquired)continue;
        if(u->fd==fd&&!u->body_close.closed)n++;
        if(u->keeper==fd&&u->keeper>=0&&!u->keeper_close.closed)n++;
    }
    return n==1;
}
static void boot_refund_row(RootBootstrapState *b,RootBootstrapFD *f,int n) {
    if(!b||!f||n<=0||f->reserved_slots<=0)return;
    if(n>f->reserved_slots)n=f->reserved_slots;
    FridayPublisherPoolRow *q=b->token?pool_row(&root_storage.pool,b->token):NULL;
    if(!q) {
        FridayPublisherMasterFault(&root_storage.pool,"bootstrap_original_slot_row_missing");
        return;
    }
    FridayPublisherPoolRow next;memcpy(&next,q,sizeof(next));
    if(plus(&next.slots,(uint64_t)n)||
       pool_apply(&root_storage.pool,q,&next,NULL,NULL,0)<0) {
        FridayPublisherMasterFault(&root_storage.pool,"bootstrap_original_slot_return_relation");
        return;
    }
    f->reserved_slots-=n;f->reserved_slot=f->reserved_slots>0;
}

static int close_held(RootHeldFile *h) {
    if(!h||h->close_alias)return -1;
    if(!h->acquired)return 0;
    if(h->body_close.closed&&h->keeper_close.closed)return 0;
    if(!h->body_close.closed&&(h->close_attempted||h->body_close.attempted||h->body_close.validation_attempted))
        return FridayPublisherMasterFault(&root_storage.pool,"held_close_UNKNOWN");
    if(!h->body_close.birth_valid) {
        h->body_close.validation_attempted=1;
        return FridayPublisherMasterFault(&root_storage.pool,"held_close_UNKNOWN");
    }
    int rc=close_generation(&h->body_close,&h->keeper_close,&h->live_body,&h->live_keeper);
    h->close_attempted=h->body_close.attempted;h->close_rc=h->body_close.rc;
    h->close_errno=h->body_close.original_errno;h->closed=h->body_close.closed;
    if(rc<0)return FridayPublisherMasterFault(&root_storage.pool,"held_close_UNKNOWN");
    return 0;
}
/* Every intermediate directory is traversed no-follow; pathname authority
 * never comes from an actor. Protected enrollment additionally enforces UID0
 * and absence of group/world writes on every ancestor. */
static RootHeldFile *open_absolute_held(const char *path,int flags,int protected) {
    if(!path||path[0]!='/'||strlen(path)>4096||!slots_free(2))return NULL;
    char copy[4097];strcpy(copy,path+1);
    RootHeldFile *cur=held_begin();if(!cur)return NULL;
    int fd=open("/",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
    if(held_consume_open(cur,fd)<0||!cur->body_close.birth_valid)return NULL;
    char *at=copy;
    while(at&&*at) {
        char *slash=strchr(at,'/');if(slash)*slash=0;
        if(!*at||!strcmp(at,".")||!strcmp(at,".."))return NULL;
        struct stat pre;
        if(fstat(cur->fd,&pre)<0)return NULL;
        if(protected&&(pre.st_uid!=0||(pre.st_mode&0022)))return NULL;
        if(!slots_free(2))return NULL;
        RootHeldFile *next=held_begin();if(!next)return NULL;
        int nfd=openat(cur->fd,at,(slash?O_RDONLY|O_DIRECTORY:flags)|O_NOFOLLOW|O_CLOEXEC);
        int born=held_consume_open(next,nfd);
        struct stat post;
        int stable=fstat(cur->fd,&post)==0&&stat_equal(&pre,&post,1);
        int closed=close_held(cur);
        if(born<0||closed<0||!stable||!next->body_close.birth_valid)return NULL;
        cur=next;at=slash?slash+1:NULL;
    }
    return cur;
}
static PyObject *sha_bytes(PyObject *raw) {
    unsigned char digest[EVP_MAX_MD_SIZE];unsigned n=0;
    if(!raw||!PyBytes_CheckExact(raw)||
       EVP_Digest(PyBytes_AsString(raw),(size_t)PyBytes_Size(raw),digest,&n,EVP_sha256(),NULL)!=1||n!=32)
        {FridayPublisherMasterFault(&root_storage.pool,"root_digest_provider");return NULL;}
    char hex[65];for(unsigned i=0;i<32;i++)snprintf(hex+2*i,3,"%02x",digest[i]);
    hex[64]=0;return PyUnicode_FromStringAndSize(hex,64);
}
static RootHeldFile *held_by_pin(PyObject *pin) {
    if(!pin)return NULL;
    for(uint64_t i=0;i<root_storage.held_count;i++) {
        RootHeldFile *h=&root_storage.held[i];
        if(h->pin==pin)return h;
        if(eq(field(h->pin,"path"),field(pin,"path"))&&eq(field(h->pin,"sha256"),field(pin,"sha256"))&&
           eq(field(h->pin,"identity9_decimal_strings"),field(pin,"identity9_decimal_strings")))return h;
    }
    return NULL;
}
static RootHeldFile *hold_file(PyObject *pin,uint64_t maximum,int protected) {
    RootHeldFile *prior=held_by_pin(pin);if(prior)return prior;
    uint64_t n;
    if(root_storage.held_count>=FRIDAY_ROOT_HELD_FILES||!PyDict_CheckExact(pin)||
       scalar_u64(field(pin,"bytes"),&n)<0||n>maximum||!PyUnicode_CheckExact(field(pin,"path"))||
       !PyUnicode_CheckExact(field(pin,"sha256"))) {
        FridayPublisherMasterFault(&root_storage.pool,"full_pin_shape_or_bounds");return NULL;
    }
    const char *path=PyUnicode_AsUTF8(field(pin,"path"));if(!path)return NULL;
    if(FridayPublisherMasterBefore(&root_storage.pool,n,0,n,n+8192)<0)return NULL;
    RootHeldFile *h=open_absolute_held(path,O_RDONLY,protected);
    if(!h||!h->body_close.birth_valid) {
        FridayPublisherMasterFault(&root_storage.pool,"held_pin_custody");return NULL;
    }
    h->pin=Py_NewRef(pin);
    if(!S_ISREG(h->birth.st_mode)||h->birth.st_nlink!=1||(uint64_t)h->birth.st_size!=n||
       !stat_pin(&h->birth,pin)||(protected&&(h->birth.st_uid!=0||(h->birth.st_mode&0777)!=0600))) {
        FridayPublisherMasterFault(&root_storage.pool,"held_pin_custody");return NULL;
    }
    h->raw=PyBytes_FromStringAndSize(NULL,(Py_ssize_t)n);if(!h->raw)return NULL;
    /* A short/error read leaves a retained partial buffer, never allocator
     * bytes. read_used, not zero padding, is the actual acquired prefix. */
    memset(PyBytes_AS_STRING(h->raw),0,(size_t)n);
    uint64_t at=0;while(at<n) {
        ssize_t got=pread(h->fd,PyBytes_AsString(h->raw)+at,(size_t)(n-at),at);
        if(got<=0) {
            h->read_used=at;h->read_completed=0;h->read_error=got<0?errno:0;
            if(got<0){errno=h->read_error;PyErr_SetFromErrno(PyExc_OSError);}
            else PyErr_SetString(PyExc_EOFError,"held original ended before its declared full width");
            return NULL;
        }
        at+=(uint64_t)got;h->read_used=at;
    }
    h->read_completed=1;h->read_error=0;
    struct stat after,named;PyObject *digest=sha_bytes(h->raw);
    int good=digest&&eq(digest,field(pin,"sha256"))&&fstat(h->fd,&after)==0&&
        lstat(path,&named)==0&&stat_equal(&h->birth,&after,1)&&stat_equal(&h->birth,&named,1);
    Py_XDECREF(digest);
    if(!good){FridayPublisherMasterFault(&root_storage.pool,"held_complete_bytes_drift");return NULL;}
    return h;
}
static PyObject *native_pairs(PyObject *self,PyObject *pairs) {
    if(!PyList_CheckExact(pairs)){FridayPublisherMasterFault(&root_storage.pool,"json_pairs");return NULL;}
    PyObject *out=PyDict_New();if(!out)return NULL;
    for(Py_ssize_t i=0;i<PyList_Size(pairs);i++) {
        PyObject *p=PyList_GetItem(pairs,i);
        if(!PyTuple_CheckExact(p)||PyTuple_Size(p)!=2||!PyUnicode_CheckExact(PyTuple_GetItem(p,0)))goto bad_pairs;
        PyObject *k=PyTuple_GetItem(p,0);
        if(PyDict_Contains(out,k)!=0||PyDict_SetItem(out,k,PyTuple_GetItem(p,1))<0)goto bad_pairs;
    }return out;
bad_pairs:
    Py_DECREF(out);FridayPublisherMasterFault(&root_storage.pool,"duplicate_or_invalid_JSON_key");return NULL;
}
static PyObject *native_constant(PyObject *self,PyObject *value) {
    FridayPublisherMasterFault(&root_storage.pool,"nonfinite_JSON_constant");return NULL;
}
static PyMethodDef pairs_def={"root_owned_json_pairs",native_pairs,METH_O,NULL};
static PyMethodDef constant_def={"root_owned_json_constant",native_constant,METH_O,NULL};
static PyObject *parse_json(RootHeldFile *h) {
    if(!h||!h->raw||(uint64_t)PyBytes_Size(h->raw)>INPUT_CAP)return NULL;
    const unsigned char *p=(unsigned char *)PyBytes_AsString(h->raw);
    Py_ssize_t n=PyBytes_Size(h->raw);unsigned depth=0;int string=0,escape=0;
    for(Py_ssize_t i=0;i<n;i++) {
        unsigned char c=p[i];
        if(string){if(escape)escape=0;else if(c=='\\')escape=1;else if(c=='"')string=0;}
        else if(c=='"')string=1;
        else if(c=='{'||c=='['){if(++depth>24)return NULL;}
        else if(c=='}'||c==']'){if(!depth)return NULL;depth--;}
    }
    if(string||depth){FridayPublisherMasterFault(&root_storage.pool,"native_JSON_preflight");return NULL;}
    /* Declared prospective floor is explicitly NOT universal factory/RSS.
     * All stock decoder/provider allocations remain independently qualified. */
    if(FridayPublisherMasterBefore(&root_storage.pool,n,0,0,(uint64_t)n*300+131072)<0)return NULL;
    PyObject *args=PyTuple_Pack(1,h->raw);
    PyObject *kw=Py_BuildValue("{s:O,s:O}","object_pairs_hook",root_storage.json_pairs,
                                              "parse_constant",root_storage.json_constant);
    PyObject *v=args&&kw?PyObject_Call(root_storage.json_loads,args,kw):NULL;
    Py_XDECREF(args);Py_XDECREF(kw);return v;
}
static int cmp_limit(PyObject *v,PyObject *node,const char *key,int direction,uint64_t fallback) {
    PyObject *bound=field(node,key),*made=NULL;
    if(!bound){made=PyLong_FromUnsignedLongLong(fallback);bound=made;}
    int good=bound&&PyObject_RichCompareBool(v,bound,direction)==1;Py_XDECREF(made);return good;
}
static int length_limit(Py_ssize_t n,PyObject *node,const char *lo,const char *hi,uint64_t max) {
    PyObject *v=PyLong_FromSsize_t(n);if(!v)return -1;
    int good=cmp_limit(v,node,lo,Py_GE,0)&&cmp_limit(v,node,hi,Py_LE,max)&&n<=(Py_ssize_t)max;
    Py_DECREF(v);return PyErr_Occurred()?-1:(good?0:1);
}
/* Concrete canonical roles.py subset in the pre-Source Root, not a matcher
 * callback stub. 0 valid, 1 mismatch, -1 actual stock/provider error. oneOf
 * mismatch never overwrites the original exception or grants authority. */
static int validate_role_native(PyObject *v,PyObject *node,PyObject *defs,unsigned depth) {
    if(depth>24||!PyDict_CheckExact(node)||!PyDict_CheckExact(defs))return 1;
    PyObject *r=field(node,"$ref");
    if(r) {
        if(!PyUnicode_CheckExact(r))return 1;const char *name=PyUnicode_AsUTF8(r);
        if(!name)return -1;if(strncmp(name,"#/$defs/",8))return 1;
        PyObject *child=field(defs,name+8);return child?validate_role_native(v,child,defs,depth+1):1;
    }
    PyObject *one=field(node,"oneOf");
    if(one) {
        if(!PyList_CheckExact(one))return 1;int matches=0;
        for(Py_ssize_t i=0;i<PyList_Size(one);i++) {
            int rc=validate_role_native(v,PyList_GetItem(one,i),defs,depth+1);
            if(rc<0)return -1;if(!rc)matches++;
        }return matches==1?0:1;
    }
    const char *kind=v==Py_None?"null":PyBool_Check(v)?"boolean":PyLong_CheckExact(v)?"integer":
        PyUnicode_CheckExact(v)?"string":PyList_CheckExact(v)?"array":PyDict_CheckExact(v)?"object":"invalid";
    PyObject *type=field(node,"type");
    if(type) {
        int found=ascii(type,kind);
        if(PyList_CheckExact(type))for(Py_ssize_t i=0;i<PyList_Size(type);i++)
            if(ascii(PyList_GetItem(type,i),kind))found=1;
        if(!found)return 1;
    }
    PyObject *constant=field(node,"const");if(constant&&!eq(v,constant))return PyErr_Occurred()?-1:1;
    PyObject *enumeration=field(node,"enum");
    if(enumeration) {
        if(!PyList_CheckExact(enumeration))return 1;int found=0;
        for(Py_ssize_t i=0;i<PyList_Size(enumeration);i++)if(eq(v,PyList_GetItem(enumeration,i)))found=1;
        if(PyErr_Occurred())return -1;if(!found)return 1;
    }
    if(!strcmp(kind,"integer")) {
        PyObject *lower=field(node,"minimum"),*upper=field(node,"maximum");
        if((lower&&PyObject_RichCompareBool(v,lower,Py_GE)!=1)||
           (upper&&PyObject_RichCompareBool(v,upper,Py_LE)!=1))return PyErr_Occurred()?-1:1;
    } else if(!strcmp(kind,"string")) {
        Py_ssize_t n=PyUnicode_GetLength(v);int rc=length_limit(n,node,"minLength","maxLength",INPUT_CAP);
        if(rc)return rc;
        Py_ssize_t bytes;const char *raw=PyUnicode_AsUTF8AndSize(v,&bytes);if(!raw)return -1;
        if(memchr(raw,0,(size_t)bytes))return 1;
        PyObject *pattern=field(node,"pattern");
        if(pattern) {
            PyObject *match=PyObject_CallFunctionObjArgs(root_storage.re_fullmatch,pattern,v,NULL);
            if(!match)return -1;int no=match==Py_None;Py_DECREF(match);if(no)return 1;
        }
    } else if(!strcmp(kind,"array")) {
        Py_ssize_t n=PyList_Size(v);int rc=length_limit(n,node,"minItems","maxItems",512);if(rc)return rc;
        if(field(node,"uniqueItems")==Py_True)for(Py_ssize_t i=0;i<n;i++)for(Py_ssize_t j=0;j<i;j++)
            if(eq(PyList_GetItem(v,i),PyList_GetItem(v,j)))return 1;
        PyObject *prefix=field(node,"prefixItems"),*items=field(node,"items");
        if(prefix&&(!PyList_CheckExact(prefix)||PyList_Size(prefix)!=n))return 1;
        for(Py_ssize_t i=0;i<n;i++)if(prefix||items) {
            rc=validate_role_native(PyList_GetItem(v,i),prefix?PyList_GetItem(prefix,i):items,defs,depth+1);
            if(rc)return rc;
        }
    } else if(!strcmp(kind,"object")) {
        int rc=length_limit(PyDict_Size(v),node,"minProperties","maxProperties",512);if(rc)return rc;
        PyObject *properties=field(node,"properties"),*required=field(node,"required");
        if(required) {
            if(!PyList_CheckExact(required))return 1;
            for(Py_ssize_t i=0;i<PyList_Size(required);i++)
                if(PyDict_Contains(v,PyList_GetItem(required,i))!=1)return PyErr_Occurred()?-1:1;
        }
        Py_ssize_t at=0;PyObject *key,*value;
        while(PyDict_Next(v,&at,&key,&value)) {
            if(!PyUnicode_CheckExact(key)||PyUnicode_GetLength(key)>240)return 1;
            PyObject *child=properties&&PyDict_CheckExact(properties)?PyDict_GetItemWithError(properties,key):NULL;
            if(!child) {
                PyObject *extra=field(node,"additionalProperties");
                if(!extra||extra==Py_False)return 1;
                if(PyDict_CheckExact(extra))child=extra;
            }
            if(child){rc=validate_role_native(value,child,defs,depth+1);if(rc)return rc;}
        }
    }
    return PyErr_Occurred()?-1:0;
}
static const char *const enrollment_fields[]={
    "schema","installed_by_uid","independent_review_sha256","root_tool","admission",
    "admission_signature","admission_key","signature_tool","signature_tool_dependencies",
    "bootstrap_helper_profile","cgroup","output_root","source_manifest","source_files",
    "consumer_manifest","consumer_files","schema_pins","control_tuples","role_caps",
    "root_namespace","selector_namespace"};
static const char *const admission_fields[]={
    "schema","root_namespace","nonce","issued_ns","expires_ns","root_tool_pid","root_tool_start_ticks",
    "root_tool_sha256","source_manifest_sha256","consumer_manifest_sha256","image","tools","inputs",
    "members","ordinary","coverage","role_caps","effects","cgroup","output_root",
    "independent_review_sha256","launch_mode","root_retention_pin","independent_selector","capacity_plan"};
static const char *const all15[]={
    "authenticate-ubuntu-indexes","authenticate-ubuntu-archives","authenticate-node-archive",
    "hold-unrar-publisher-gap","authenticate-wheels","map-cpython-venv","map-lib-dynload",
    "map-native-loader","map-browser-resources","map-data-closure","bind-candidate","bind-golden",
    "assemble-members","write-final-manifest","external-custody"};
static PyObject *caps(void) {
    return Py_BuildValue("{s:K,s:K,s:K,s:K,s:K,s:i,s:i,s:i,s:i,s:i,s:i,s:K,s:K}",
        "read_bytes",READ_CAP,"output_bytes",OUTPUT_CAP,"ram_bytes",RAM_CAP,
        "slots",SLOT_CAP,"workers",WORKER_CAP,"wall_seconds",4200,"seal_reserve_seconds",600,
        "network",0,"retries",0,"artifacts",350,"members",512,"input_bytes",INPUT_CAP,"document_bytes",BODY_CAP);
}
static int protected_enrollment_native(void) {
    FridayPublisherRootStorage *s=&root_storage;
    RootHeldFile *h=open_absolute_held(ENROLLMENT,O_RDONLY,1);
    if(!h||!h->body_close.birth_valid)
        return FridayPublisherMasterFault(&s->pool,"protected_enrollment_custody");
    int fd=h->fd;struct stat st=h->birth;
    if(!S_ISREG(st.st_mode)||st.st_uid!=0||st.st_nlink!=1||
       (st.st_mode&0777)!=0600||st.st_size<=0||(uint64_t)st.st_size>INPUT_CAP)
        return FridayPublisherMasterFault(&s->pool,"protected_enrollment_custody");
    if(FridayPublisherMasterBefore(&s->pool,st.st_size,0,st.st_size,st.st_size+131072)<0)return -1;
    h->raw=PyBytes_FromStringAndSize(NULL,st.st_size);if(!h->raw)return -1;
    memset(PyBytes_AS_STRING(h->raw),0,(size_t)st.st_size);
    uint64_t at=0;while(at<(uint64_t)st.st_size) {
        ssize_t n=pread(fd,PyBytes_AsString(h->raw)+at,st.st_size-at,at);
        if(n<=0) {
            h->read_used=at;h->read_completed=0;h->read_error=n<0?errno:0;
            if(n<0){errno=h->read_error;PyErr_SetFromErrno(PyExc_OSError);}
            else PyErr_SetString(PyExc_EOFError,"enrollment ended before its declared full width");
            return -1;
        }
        at+=n;h->read_used=at;
    }
    h->read_completed=1;h->read_error=0;
    PyObject *nine=stat9(&st),*hash=sha_bytes(h->raw);
    h->pin=nine&&hash?Py_BuildValue("{s:s,s:K,s:O,s:O}","path",ENROLLMENT,"bytes",(uint64_t)st.st_size,
        "sha256",hash,"identity9_decimal_strings",nine):NULL;
    Py_XDECREF(nine);Py_XDECREF(hash);if(!h->pin)return -1;s->enrollment_file=h;
    struct stat after,named;
    if(fstat(fd,&after)<0||lstat(ENROLLMENT,&named)<0||!stat_equal(&st,&after,1)||!stat_equal(&st,&named,1))
        return FridayPublisherMasterFault(&s->pool,"protected_enrollment_drift");
    s->enrollment=parse_json(h);if(!s->enrollment)return -1;
    PyObject *cap=caps();int good=cap&&
        exact_keys(s->enrollment,enrollment_fields,sizeof(enrollment_fields)/sizeof(*enrollment_fields))&&
        ascii(field(s->enrollment,"schema"),"friday.a138.externally-installed-root-enrollment.v1")&&
        eq(field(s->enrollment,"role_caps"),cap);
    Py_XDECREF(cap);uint64_t uid;
    if(!good||scalar_u64(field(s->enrollment,"installed_by_uid"),&uid)<0||uid!=0||
       eq(field(s->enrollment,"root_namespace"),field(s->enrollment,"selector_namespace")))
        return FridayPublisherMasterFault(&s->pool,"protected_enrollment_schema_or_role");
    return 0;
}

static int snapshot_one(const char *manifest_key,const char *files_key,PyObject **saved) {
    FridayPublisherRootStorage *s=&root_storage;
    RootHeldFile *mh=hold_file(field(s->enrollment,manifest_key),INPUT_CAP,0);
    PyObject *manifest=mh?parse_json(mh):NULL,*declared=field(s->enrollment,files_key);
    if(!manifest||!PyList_CheckExact(declared)||PyList_Size(declared)>350)goto refuse;
    PyObject *members=field(manifest,"members");if(!PyList_CheckExact(members))goto refuse;
    PyObject *seen=PySet_New(NULL);if(!seen)goto refuse;
    for(Py_ssize_t i=0;i<PyList_Size(declared);i++) {
        PyObject *pin=PyList_GetItem(declared,i),*relative=field(pin,"relative_path");
        if(!PyUnicode_CheckExact(relative)||PySet_Contains(seen,relative)!=0||
           PySet_Add(seen,relative)<0){Py_DECREF(seen);goto refuse;}
        const char *rel=PyUnicode_AsUTF8(relative),*path=PyUnicode_AsUTF8(field(pin,"path"));
        const char *mp=PyUnicode_AsUTF8(field(mh->pin,"path"));if(!rel||!path||!mp){Py_DECREF(seen);goto refuse;}
        const char *slash=strrchr(mp,'/');if(!slash||rel[0]=='/'||strstr(rel,"../")||strstr(rel,"//"))
            {Py_DECREF(seen);goto refuse;}
        size_t prefix=(size_t)(slash-mp+1);
        if(strncmp(path,mp,prefix)||strcmp(path+prefix,rel)){Py_DECREF(seen);goto refuse;}
        RootHeldFile *h=hold_file(pin,BODY_CAP,0);if(!h){Py_DECREF(seen);goto refuse;}
        /* Full bytes and original identity remain. Live FD is unnecessary for
         * an immutable retained byte loader; close once and keep every outcome. */
        if(!h->close_alias&&close_held(h)<0){Py_DECREF(seen);goto refuse;}
        int found=0;
        for(Py_ssize_t j=0;j<PyList_Size(members);j++) {
            PyObject *member=PyList_GetItem(members,j);
            if(eq(field(member,"path"),relative)) {
                if(found||!eq(field(member,"bytes"),field(pin,"bytes"))||
                   !eq(field(member,"sha256"),field(pin,"sha256"))){Py_DECREF(seen);goto refuse;}
                found=1;
            }
        }
        if(!found){Py_DECREF(seen);goto refuse;}
    }
    if(PySet_Size(seen)!=PyList_Size(members)){Py_DECREF(seen);goto refuse;}
    /* Exact tree walk follows the same 350/512 and 700/600 no-symlink contract.
     * It is performed by source_snapshot_tree below, before any Source load. */
    Py_DECREF(seen);*saved=manifest;return 0;
refuse:
    Py_XDECREF(manifest);return FridayPublisherMasterFault(&s->pool,"full_current_snapshot_join");
}
struct RootNativeDirent {uint64_t ino;int64_t off;unsigned short reclen;unsigned char type;char name[];};
static int walk_snapshot(const char *base,const char *relative,PyObject *allowed,unsigned *count) {
    char path[4097];int n=snprintf(path,sizeof(path),"%s%s%s",base,*relative?"/":"",relative);
    if(n<0||n>4096)return -1;
    RootHeldFile *h=open_absolute_held(path,O_RDONLY|O_DIRECTORY,0);
    if(!h||!h->body_close.birth_valid||(h->birth.st_mode&0777)!=0700)return -1;
    unsigned char buf[8192];
    if(FridayPublisherMasterBefore(&root_storage.pool,0,0,0,sizeof(buf))<0)return -1;
    int rc=0;
#ifndef SYS_getdents64
    rc=-1;
#else
    for(;;) {
        if(FridayPublisherMasterBefore(&root_storage.pool,sizeof(buf),0,0,0)<0){rc=-1;break;}
        ssize_t got=syscall(SYS_getdents64,h->fd,buf,sizeof(buf));
        if(got<0){rc=-1;break;}if(!got)break;
        size_t pos=0;
        while(pos<(size_t)got) {
            size_t header=offsetof(struct RootNativeDirent,name);
            if((size_t)got-pos<header+2){rc=-1;break;}
            unsigned short reclen;
            memcpy(&reclen,buf+pos+offsetof(struct RootNativeDirent,reclen),sizeof(reclen));
            char *name=(char *)buf+pos+header;
            if(reclen<header+2||reclen>(size_t)got-pos||
               !memchr(name,0,reclen-header)){rc=-1;break;}
            pos+=reclen;
            if(!strcmp(name,".")||!strcmp(name,".."))continue;
            if(++*count>512){rc=-1;break;}
            char rel[4097];struct stat st;
            n=snprintf(rel,sizeof(rel),"%s%s%s",relative,*relative?"/":"",name);
            if(n<0||n>4096||fstatat(h->fd,name,&st,AT_SYMLINK_NOFOLLOW)<0){rc=-1;break;}
            if(S_ISDIR(st.st_mode)){if(walk_snapshot(base,rel,allowed,count)<0){rc=-1;break;}}
            else {
                if(!S_ISREG(st.st_mode)||st.st_nlink!=1||(st.st_mode&0777)!=0600){rc=-1;break;}
                PyObject *key=PyUnicode_FromString(rel);
                int has=key?PySet_Contains(allowed,key):-1;Py_XDECREF(key);
                if(has!=1){rc=-1;break;}
            }
        }
        if(rc)break;
    }
#endif
    if(close_held(h)<0)rc=-1;return rc;
}
static int source_snapshot_tree(const char *mk,const char *fk) {
    PyObject *mp=field(field(root_storage.enrollment,mk),"path"),*declared=field(root_storage.enrollment,fk);
    const char *path=mp?PyUnicode_AsUTF8(mp):NULL;if(!path||strlen(path)>4096)return -1;
    char base[4097];strcpy(base,path);char *slash=strrchr(base,'/');if(!slash)return -1;
    PyObject *allowed=PySet_New(NULL),*manifest_name=PyUnicode_FromString(slash+1);
    if(!allowed||!manifest_name){Py_XDECREF(allowed);Py_XDECREF(manifest_name);return -1;}
    *slash=0;int rc=PySet_Add(allowed,manifest_name);Py_DECREF(manifest_name);
    for(Py_ssize_t i=0;rc==0&&i<PyList_Size(declared);i++)
        rc=PySet_Add(allowed,field(PyList_GetItem(declared,i),"relative_path"));
    unsigned count=0;if(!rc)rc=walk_snapshot(base,"",allowed,&count);
    Py_DECREF(allowed);return rc;
}
static PyObject *pin_relative(const char *files,const char *relative) {
    PyObject *a=field(root_storage.enrollment,files),*found=NULL;
    if(!PyList_CheckExact(a))return NULL;
    for(Py_ssize_t i=0;i<PyList_Size(a);i++) {
        PyObject *pin=PyList_GetItem(a,i);
        if(ascii(field(pin,"relative_path"),relative)){if(found)return NULL;found=pin;}
    }return found;
}
static int source_snapshot_native(void) {
    FridayPublisherRootStorage *s=&root_storage;
    if(snapshot_one("source_manifest","source_files",&s->source_manifest)<0||
       snapshot_one("consumer_manifest","consumer_files",&s->consumer_manifest)<0||
       source_snapshot_tree("source_manifest","source_files")<0||
       source_snapshot_tree("consumer_manifest","consumer_files")<0)
        return FridayPublisherMasterFault(&s->pool,"source_exact_pathset");
    RootHeldFile *rh=hold_file(pin_relative("source_files","schemas/friday.a138.roles.v1.json"),INPUT_CAP,0);
    s->role_schema=rh?parse_json(rh):NULL;
    if(!s->role_schema||!ascii(field(s->role_schema,"$id"),"friday.a138.roles.v1"))
        return FridayPublisherMasterFault(&s->pool,"canonical_pinned_roles");
    PyObject *schemas=field(s->enrollment,"schema_pins");
    if(!PyDict_CheckExact(schemas)||PyDict_Size(schemas)!=20)
        return FridayPublisherMasterFault(&s->pool,"all20_schema_pins");
    Py_ssize_t at=0;PyObject *key,*hash;
    while(PyDict_Next(schemas,&at,&key,&hash)) {
        const char *name=PyUnicode_AsUTF8(key);char path[256];
        if(!name||snprintf(path,sizeof(path),"schemas/%s.json",name)>255)
            return FridayPublisherMasterFault(&s->pool,"all20_schema_name");
        PyObject *pin=pin_relative("consumer_files",path);
        if(!pin||!eq(hash,field(pin,"sha256")))return FridayPublisherMasterFault(&s->pool,"all20_schema_sha");
    }
    RootHeldFile *ch=hold_file(pin_relative("consumer_files","controls/catalog.json"),INPUT_CAP,0);
    PyObject *catalog=ch?parse_json(ch):NULL,*controls=field(catalog,"controls");
    PyObject *tuples=field(s->enrollment,"control_tuples");
    if(!PyList_CheckExact(controls)||PyList_Size(controls)!=69||!PyList_CheckExact(tuples)||PyList_Size(tuples)!=69)
        {Py_XDECREF(catalog);return FridayPublisherMasterFault(&s->pool,"all69_exact_tuples");}
    const char *const fields[]={"id","scenario","status","cause","stage","match"};
    PyObject *ids=PySet_New(NULL);if(!ids){Py_DECREF(catalog);return -1;}
    for(Py_ssize_t i=0;i<69;i++) {
        PyObject *control=PyList_GetItem(controls,i),*tuple=PyList_GetItem(tuples,i),*id=field(control,"id");
        if(!id||PySet_Contains(ids,id)!=0||PySet_Add(ids,id)<0||!exact_keys(tuple,fields,6))
            {Py_DECREF(ids);Py_DECREF(catalog);return FridayPublisherMasterFault(&s->pool,"all69_original_control_identity");}
        for(int j=0;j<6;j++)if(!eq(field(control,fields[j]),field(tuple,fields[j])))
            {Py_DECREF(ids);Py_DECREF(catalog);return FridayPublisherMasterFault(&s->pool,"all69_original_control_tuple");}
    }
    Py_DECREF(ids);Py_DECREF(catalog);s->snapshot_verified=1;return 0;
}
static PyObject *proc_stat_fact(pid_t pid) {
    char raw[8193];
    /* Actual callers pass this process. Another pid is not given a bare FD. */
    if(pid!=getpid())return NULL;
    RootUtilityEnd *u=&root_storage.utility[1];
    if(u->kind!=2||utility_refresh(u,sizeof(raw)-1)<0||!u->read_completed||
       u->body_len>=sizeof(raw))return NULL;
    memcpy(raw,u->body,(size_t)u->body_len);raw[u->body_len]=0;
    /* Body is already retained. Object credit does not reopen the descriptor
     * and does not debit the proc read a second time. */
    if(FridayPublisherMasterBefore(&root_storage.pool,0,0,0,sizeof(raw)*2)<0)return NULL;
    char *end=strrchr(raw,')');if(!end||end[1]!=' ')return NULL;
    char copy[8193];strcpy(copy,end+2);char *save=NULL,*word=strtok_r(copy," ",&save);
    for(int i=0;i<19&&word;i++)word=strtok_r(NULL," ",&save);
    if(!word)return NULL;char *stop;unsigned long long start=strtoull(word,&stop,10);
    if(*stop&&*stop!='\n')return NULL;
    return Py_BuildValue("{s:l,s:K,s:s}","pid",(long)pid,"start_ticks",start,"raw_stat",raw);
}
static int actual_root_native(void) {
    FridayPublisherRootStorage *s=&root_storage;
    RootHeldFile *named=hold_file(field(s->enrollment,"root_tool"),BODY_CAP,0);
    if(!named)return -1;
    RootUtilityEnd *exe_owner=&s->utility[2];
    struct stat exe;
    if(exe_owner->kind!=3||!exe_owner->acquired||!exe_owner->body_close.birth_valid||
       exe_owner->fd<0||exe_owner->body_close.closed||exe_owner->body_close.attempted)
        return FridayPublisherMasterFault(&s->pool,"actual_root_tool_not_named_image");
    exe_owner->compare_attempted=1;
    exe_owner->compare_rc=fstat(exe_owner->fd,&exe);
    exe_owner->compare_errno=exe_owner->compare_rc<0?errno:0;
    if(exe_owner->compare_rc==0)exe_owner->compare_stat=exe;
    /* A failed fstat keeps the descriptor. Retirement is the receiver close. */
    if(exe_owner->compare_rc<0||!stat_equal(&exe,&named->birth,1))
        return FridayPublisherMasterFault(&s->pool,"actual_root_tool_not_named_image");
    PyObject *fact=proc_stat_fact(getpid()),*nine=stat9(&exe);
    if(!fact||!nine){Py_XDECREF(fact);Py_XDECREF(nine);return -1;}
    PyObject *uid=PyLong_FromLong(getuid()),*gid=PyLong_FromLong(getgid());
    int good=uid&&gid&&PyDict_SetItemString(fact,"exe_sha256",field(named->pin,"sha256"))==0&&
        PyDict_SetItemString(fact,"exe_identity9",nine)==0&&PyDict_SetItemString(fact,"actual_uid",uid)==0&&
        PyDict_SetItemString(fact,"actual_gid",gid)==0;
    Py_XDECREF(uid);Py_XDECREF(gid);Py_DECREF(nine);
    if(!good){Py_DECREF(fact);return -1;}s->root_fact=fact;return 0;
}
static int bind_admission_fields(void) {
    FridayPublisherRootStorage *s=&root_storage;PyObject *a=s->admission,*e=s->enrollment;
    if(!exact_keys(a,admission_fields,sizeof(admission_fields)/sizeof(*admission_fields))||
       !ascii(field(a,"schema"),"friday.a138.independent-root-admission.v1"))
        return FridayPublisherMasterFault(&s->pool,"admission_schema");
    const char *const joins[]={"root_namespace","role_caps","cgroup","output_root","independent_review_sha256"};
    for(int i=0;i<5;i++)if(!eq(field(a,joins[i]),field(e,joins[i])))
        return FridayPublisherMasterFault(&s->pool,"admission_role_snapshot");
    if(!eq(field(a,"root_tool_pid"),field(s->root_fact,"pid"))||
       !eq(field(a,"root_tool_start_ticks"),field(s->root_fact,"start_ticks"))||
       !eq(field(a,"root_tool_sha256"),field(s->root_fact,"exe_sha256"))||
       !eq(field(a,"source_manifest_sha256"),field(field(e,"source_manifest"),"sha256"))||
       !eq(field(a,"consumer_manifest_sha256"),field(field(e,"consumer_manifest"),"sha256")))
        return FridayPublisherMasterFault(&s->pool,"admission_actual_current_identity");
    uint64_t issued,expires,now;
    if(scalar_u64(field(a,"issued_ns"),&issued)<0||scalar_u64(field(a,"expires_ns"),&expires)<0||
       expires<issued||expires-issued>4200ULL*1000000000ULL)
        return FridayPublisherMasterFault(&s->pool,"admission_current_clock");
    uint64_t effective_deadline=expires<s->pool.deadline_ns?expires:s->pool.deadline_ns;
    uint64_t seal_reserve=600ULL*1000000000ULL;
    /* Retain this numeric restriction from the existing admission path BEFORE
     * its fallible clock/work check. It is restrictive DATA, not an admission
     * witness: all later identity/effects/schema checks and prior independent
     * qualification remain required. Never grant admission or extend time.
     * A refusal must not restore the larger original interval for completion.
     * An impossible full interval remains failed, not a fresh600 tail. */
    s->pool.deadline_ns=effective_deadline;
    s->pool.work_deadline_ns=effective_deadline>seal_reserve?
        effective_deadline-seal_reserve:0;
    if(FridayPublisherMasterWorkClock(&s->pool,
            s->pool.work_deadline_ns,
            FRIDAY_CLOCK_ADMISSION,&now)<0)
        return FridayPublisherMasterFault(&s->pool,"admission_current_clock");
    if(issued>now) {
        /* Valid primitive time, but this admission is not current. Refuse
         * Source work without falsifying the returned clock as unavailable.
         * The retained original admission and pool fault remain the refusal;
         * separate completion has only the already restricted full interval. */
        s->pool.work_clock.fault_kind=7;
        s->pool.work_clock.phase=FRIDAY_NATIVE_CLOCK_WORK_STOPPED;
        return FridayPublisherMasterFault(&s->pool,"admission_no_original_work_interval");
    }
    PyObject *effects=field(a,"effects");
    if(!PyList_CheckExact(effects)||PyList_Size(effects)!=15)
        return FridayPublisherMasterFault(&s->pool,"admission_all15");
    for(int i=0;i<15;i++)if(!ascii(PyList_GetItem(effects,i),all15[i]))
        return FridayPublisherMasterFault(&s->pool,"admission_all15");
    if(!ascii(field(a,"launch_mode"),"perform-and-retain")&&!ascii(field(a,"launch_mode"),"retained-consumer"))
        return FridayPublisherMasterFault(&s->pool,"admission_launch_mode");
    if(ascii(field(a,"launch_mode"),"retained-consumer")&&field(a,"root_retention_pin")==Py_None)
        return FridayPublisherMasterFault(&s->pool,"independent_retention_required");
    PyObject *defs=field(s->role_schema,"$defs");
    int rc=validate_role_native(a,field(defs,"admission"),defs,0);
    if(rc)return FridayPublisherMasterFault(&s->pool,"canonical_admission_role_schema");
    return 0;
}
static int current_enrollment_matches(PyObject *fact,PyObject *qualification) {
    FridayPublisherRootStorage *s=&root_storage;
    if(!FridayPublisherMasterOwns(&s->pool)||!s->signed_admission||!s->snapshot_verified||
       !fact||!qualification||!PyDict_CheckExact(fact)||!PyDict_CheckExact(qualification))return 0;
    /* Validate actual protected inputs, not qualification["verified"] or a
     * Source-created authority object. The real independent signature was
     * consumed by this exact native Root before any Source module loaded. */
    const char *const keys[]={"pid","start_ticks","exe_sha256","exe_identity9","actual_uid","actual_gid"};
    for(int i=0;i<6;i++)if(!eq(field(fact,keys[i]),field(s->root_fact,keys[i])))return 0;
    uint64_t now=now_ns(),issued,expires;
    if(!now||scalar_u64(field(s->admission,"issued_ns"),&issued)<0||
       scalar_u64(field(s->admission,"expires_ns"),&expires)<0)return 0;
    if(now<issued) {
        s->pool.work_clock.fault_kind=7;
        s->pool.work_clock.phase=FRIDAY_NATIVE_CLOCK_WORK_STOPPED;
        FridayPublisherMasterFault(&s->pool,"admission_not_yet_current");
        return 0;
    }
    if(now>expires) {
        s->pool.work_clock.fault_kind=4;
        s->pool.work_clock.phase=FRIDAY_NATIVE_CLOCK_FAILED;return 0;
    }
    if(qualification!=s->qualification) {
        if(field(qualification,"source_issued_grant")!=Py_False||
           !eq(field(field(qualification,"raw_ref"),"sha256"),field(s->admission_file->pin,"sha256"))||
           !eq(field(field(qualification,"signature_ref"),"sha256"),field(s->signature_file->pin,"sha256"))||
           !eq(field(field(qualification,"key_ref"),"sha256"),field(s->key_file->pin,"sha256")))return 0;
    }
    struct stat st,named;
    for(uint64_t i=0;i<s->held_count;i++) {
        RootHeldFile *h=&s->held[i];const char *path=h->pin?PyUnicode_AsUTF8(field(h->pin,"path")):NULL;
        if(!path||lstat(path,&named)<0||!stat_equal(&named,&h->birth,1)||
           (!h->closed&&(fstat(h->fd,&st)<0||!stat_equal(&st,&h->birth,1))))return 0;
    }
    PyObject *actual=proc_stat_fact(getpid());if(!actual)return 0;
    int same=eq(field(actual,"start_ticks"),field(s->root_fact,"start_ticks"));
    Py_DECREF(actual);return same&&!PyErr_Occurred();
}

static PyObject *kernel_text(const char *path,size_t cap) {
    if(!cap||cap>INPUT_CAP)return NULL;
    if(FridayPublisherMasterBefore(&root_storage.pool,cap,0,0,cap*2+128)<0)return NULL;
    RootHeldFile *h=open_absolute_held(path,O_RDONLY,0);
    if(!h||!h->body_close.birth_valid){PyErr_SetFromErrno(PyExc_OSError);return NULL;}
    h->raw=PyBytes_FromStringAndSize(NULL,cap);if(!h->raw){(void)close_held(h);return NULL;}
    PyObject *raw=h->raw; /* actual preowned strong slot before the read */
    memset(PyBytes_AS_STRING(raw),0,cap);
    ssize_t n=read(h->fd,PyBytes_AsString(raw),cap);
    h->read_error=n<0?errno:0;h->read_used=n>0?(uint64_t)n:0;h->read_completed=0;
    /* Install the actual read failure before cleanup. MasterFault does not
     * replace a pending original; native close facts retain the second end. */
    if(n<0){errno=h->read_error;PyErr_SetFromErrno(PyExc_OSError);}
    else if(n==(ssize_t)cap)PyErr_SetString(PyExc_OverflowError,"kernel text cut reached its bound");
    int rc=close_held(h);
    if(n<0||n==(ssize_t)cap||rc<0)return NULL;
    /* Full allocation retained, acquired prefix explicitly separate. */
    h->read_completed=1;
    return PyUnicode_DecodeUTF8(PyBytes_AsString(raw),n,"strict");
}
static PyObject *root_proc_io(pid_t pid) {
    char path[80];snprintf(path,sizeof(path),"/proc/%ld/io",(long)pid);
    PyObject *raw=kernel_text(path,8192);if(!raw)return NULL;
    const char *s=PyUnicode_AsUTF8(raw);if(!s){Py_DECREF(raw);return NULL;}
    char copy[8193];strcpy(copy,s);PyObject *counters=PyDict_New();if(!counters){Py_DECREF(raw);return NULL;}
    char *at=copy;
    while(*at) {
        char *end=strchr(at,'\n'),*colon=strchr(at,':');
        if(!end||!colon||colon>end)goto invalid;
        *end=0;*colon=0;char *stop;unsigned long long value=strtoull(colon+1,&stop,10);
        while(*stop==' ')stop++;if(*stop||PyDict_GetItemString(counters,at))goto invalid;
        PyObject *v=PyLong_FromUnsignedLongLong(value);if(!v||PyDict_SetItemString(counters,at,v)<0)
            {Py_XDECREF(v);goto invalid;}Py_DECREF(v);at=end+1;
    }
    const char *const names[]={"rchar","wchar","syscr","syscw","read_bytes","write_bytes","cancelled_write_bytes"};
    if(!exact_keys(counters,names,7))goto invalid;
    PyObject *out=Py_BuildValue("{s:O,s:O}","raw",raw,"counters",counters);
    Py_DECREF(raw);Py_DECREF(counters);return out;
invalid:
    Py_DECREF(raw);Py_DECREF(counters);FridayPublisherMasterFault(&root_storage.pool,"actual_process_IO_schema");return NULL;
}
static PyObject *cgroup_sample_native(void) {
    const char *cg=PyUnicode_AsUTF8(field(root_storage.enrollment,"cgroup"));if(!cg)return NULL;
    const char *const names[]={"memory.current","memory.peak","memory.max","pids.current","pids.max","cgroup.procs","io.stat","cgroup.events"};
    PyObject *raw=PyDict_New(),*out=PyDict_New();if(!raw||!out){Py_XDECREF(raw);Py_XDECREF(out);return NULL;}
    for(int i=0;i<8;i++) {
        char path[4097];if(snprintf(path,sizeof(path),"%s/%s",cg,names[i])>4096)goto bad;
        PyObject *v=kernel_text(path,65536);if(!v||PyDict_SetItemString(raw,names[i],v)<0)
            {Py_XDECREF(v);goto bad;}Py_DECREF(v);
    }
    if(PyDict_SetItemString(out,"raw",raw)<0)goto bad;
    const char *const target[]={"memory_current","memory_peak","memory_max","pids_current","pids_max"};
    for(int i=0;i<5;i++) {
        PyObject *v=field(raw,names[i]);const char *text=PyUnicode_AsUTF8(v);if(!text)goto bad;
        char *end;unsigned long long n=strtoull(text,&end,10);
        while(*end==' '||*end=='\n')end++;if(*end)goto bad;
        PyObject *number=PyLong_FromUnsignedLongLong(n);if(!number||PyDict_SetItemString(out,target[i],number)<0)
            {Py_XDECREF(number);goto bad;}Py_DECREF(number);
    }
    uint64_t observed_clock_ns=now_ns();if(!observed_clock_ns)goto bad;
    PyObject *at=PyLong_FromUnsignedLongLong(observed_clock_ns);if(!at||PyDict_SetItemString(out,"at_ns",at)<0)
        {Py_XDECREF(at);goto bad;}Py_DECREF(at);Py_DECREF(raw);return out;
bad:
    Py_DECREF(raw);Py_DECREF(out);FridayPublisherMasterFault(&root_storage.pool,"actual_cgroup_sample");return NULL;
}






/* ONE native settling/publication path; never call Source error code. */
static void boot_error(RootBootstrapState *b,const char *phase,int original_errno) {
    if(getpid()!=b->parent_pid) {
        if(!b->failed){b->failed=1;b->first_phase=phase;b->first_errno=original_errno;}return;
    }
    if(!b->failed) {
        b->failed=1;b->first_phase=phase;b->first_errno=original_errno;b->first_errno_valid=1;
        if(PyErr_Occurred())PyErr_Fetch(&b->first.type,&b->first.value,&b->first.tb);
        else {
            b->first.type=Py_XNewRef(root_storage.pool.refusal_type);
            b->first.value=Py_XNewRef(root_storage.pool.refusal_value);
        }
        if(!root_storage.pool.refused){root_storage.pool.refused=1;root_storage.pool.fault=phase;}
    } else if(PyErr_Occurred()) {
        if(b->secondary_count<ROOT_BOOT_ERRORS) {
            RootBootstrapError *e=&b->secondary[b->secondary_count++];
            PyErr_Fetch(&e->type,&e->value,&e->tb);
        }
        /* If the native history is full, leave the actual pending triple
         * untouched and stop publication. Never PyErr_Clear/normalize. */
    }
}
static int boot_charge(RootBootstrapState *b,uint64_t reads,uint64_t writes) {
    if(b->actual_native_reads>b->reserved_reads||b->actual_native_writes>b->reserved_output||
       reads>b->reserved_reads-b->actual_native_reads||
       writes>b->reserved_output-b->actual_native_writes)return -1;
    return 0; /* MAX is prospective only; actual bytes are charged after syscall */
}
static int boot_new_fd(RootBootstrapState *b) {
    FridayPublisherMasterPool *pool=&root_storage.pool;
    FridayPublisherPoolRow *reservation=b->token?pool_row(pool,b->token):NULL;
    uint64_t r,o,h,a,slots;
    if(b->fd_count>=ROOT_BOOT_FILES||master_sum(pool,&r,&o,&h,&a,&slots)<0||
       slots>SLOT_CAP||pool->native_live_slots>SLOT_CAP-slots||
       (reservation?(!reservation->active||reservation->slots<2):!slots_free(2))) {
        boot_error(b,"bootstrap_FD_history_or_original_pending_slots",0);return -1;
    }
    uint64_t generation=take_generation();
    if(!generation){boot_error(b,"bootstrap_generation_clock",0);return -1;}
    int at=(int)b->fd_count++;RootBootstrapFD *f=&b->fd[at];
    memset(f,0,sizeof(*f));f->fd=-1;f->keeper=-1;f->held_index=UINT64_MAX;f->generation=generation;
    if(reservation) {
        FridayPublisherPoolRow next;memcpy(&next,reservation,sizeof(next));next.slots-=2;
        if(pool_apply(pool,reservation,&next,NULL,NULL,0)<0) {
            boot_error(b,"bootstrap_original_slot_move_relation",0);return -1;
        }
        f->reserved_slots=2;f->reserved_slot=1;
    }
    return at; /* prospective credit moved, never fresh cap or IO refund */
}
static int boot_birth(RootBootstrapState *b,int i,int fd) {
    RootBootstrapFD *f=&b->fd[i];f->fd=fd;
    if(fd<0){
        f->acquire_errno=errno;
        boot_refund_row(b,f,f->reserved_slots); /* UNUSED reservation only */
        boot_error(b,"bootstrap_open_acquisition",f->acquire_errno);return -1;
    }
    f->valid=1;f->acquired=1;b->active_fds++;
    if(!slots_free(1)){boot_error(b,"bootstrap_acquired_FD_birth_slots",EMFILE);return -1;}
    root_storage.pool.native_live_slots++;f->live_body=1;
    if(bind_generation(f->generation,fd,&f->keeper,
        &f->keeper_open_attempted,&f->keeper_open_rc,&f->keeper_open_errno,
        &f->birth_stat_attempted,&f->birth_stat_rc,&f->birth_stat_errno,
        &f->birth,&f->body_close,&f->live_keeper)<0) {
        f->stat_errno=f->birth_stat_errno?f->birth_stat_errno:f->keeper_open_errno;
        boot_error(b,"bootstrap_acquired_FD_birth_stat",f->stat_errno);return -1;
    }
    f->last=f->birth;return 0;
}
static int boot_close(RootBootstrapState *b,int i) {
    if(i<0||i>=(int)b->fd_count)return -1;RootBootstrapFD *f=&b->fd[i];
    if(!f->valid)return 0;
    if(f->body_close.closed&&f->keeper_close.closed)return 0;
    if(!f->body_close.birth_valid) {
        f->close_validation_attempted=1;
        boot_error(b,"bootstrap_close_custody_UNKNOWN",0);return -1;
    }
    int body_was=f->body_close.closed,keeper_was=f->keeper_close.closed;
    int rc=close_generation(&f->body_close,&f->keeper_close,&f->live_body,&f->live_keeper);
    f->close_attempted=f->body_close.attempted;f->close_rc=f->body_close.rc;
    f->close_errno=f->body_close.original_errno;f->closed=f->body_close.closed;
    if(!body_was&&f->body_close.closed){if(b->active_fds)b->active_fds--;boot_refund_row(b,f,1);}
    if(!keeper_was&&f->keeper_close.closed)boot_refund_row(b,f,1);
    if(f->held_index!=UINT64_MAX&&f->held_index<root_storage.held_count) {
        RootHeldFile *h=&root_storage.held[f->held_index];
        h->close_attempted=f->close_attempted;h->close_rc=f->close_rc;
        h->close_errno=f->close_errno;h->closed=f->closed;
        h->body_close=f->body_close;h->keeper_close=f->keeper_close;
    }
    /* Uncertain EINTR/EBADF is not retried and never decrements live credit. */
    return rc<0?-1:0;
}
static int boot_open_absolute(RootBootstrapState *b,const char *path,int flags) {
    if(!path||path[0]!='/'||strlen(path)>4096)return -1;
    int i=boot_new_fd(b);if(i<0)return -1;
    int fd=open("/",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
    if(boot_birth(b,i,fd)<0)return -1;
    char copy[4097];strcpy(copy,path+1);char *at=copy;
    while(at&&*at) {
        char *slash=strchr(at,'/');if(slash)*slash=0;
        if(!*at||!strcmp(at,".")||!strcmp(at,".."))return -1;
        struct stat pre,post;
        if(fstat(b->fd[i].fd,&pre)<0||!S_ISDIR(pre.st_mode))return -1;
        int next=boot_new_fd(b);if(next<0)return -1;
        fd=openat(b->fd[i].fd,at,(slash?O_RDONLY|O_DIRECTORY:flags)|O_NOFOLLOW|O_CLOEXEC);
        int born=boot_birth(b,next,fd);
        int stable=fstat(b->fd[i].fd,&post)==0&&stat_equal(&pre,&post,1);
        if(boot_close(b,i)<0||born<0||!stable)return -1;
        i=next;at=slash?slash+1:NULL;
    }
    return i;
}
static int boot_zero_buffer(PyObject **slot,Py_ssize_t width) {
    /* A partial construction may be handed to the actual failure receiver.
     * Never retain an uninitialized allocation until a later factory succeeds:
     * first failure stops this producer, and every existing byte is defined.
     * Zero fill is storage initialization, NOT proof of a successful read. */
    if(!slot||*slot||width<0)return -1;
    *slot=PyBytes_FromStringAndSize(NULL,width);
    if(!*slot)return -1;
    memset(PyBytes_AS_STRING(*slot),0,(size_t)width);
    return 0;
}
static RootHeldFile *boot_hold_file(RootBootstrapState *b,PyObject *pin,uint64_t maximum) {
    RootHeldFile *prior=held_by_pin(pin);if(prior&&!prior->closed&&!prior->close_attempted)return prior;
    uint64_t width;PyObject *pathobj=field(pin,"path");
    if(!PyDict_CheckExact(pin)||!PyUnicode_CheckExact(pathobj)||
       scalar_u64(field(pin,"bytes"),&width)<0||width>maximum||
       root_storage.held_count>=FRIDAY_ROOT_HELD_FILES)return NULL;
    const char *path=PyUnicode_AsUTF8(pathobj);if(!path)return NULL;
    if(FridayPublisherMasterBefore(&root_storage.pool,width,0,width,width+8192)<0)return NULL;
    uint64_t index=root_storage.held_count++;RootHeldFile *h=&root_storage.held[index];
    memset(h,0,sizeof(*h));h->fd=-1;h->keeper=-1;h->pin=Py_NewRef(pin);
    if(boot_zero_buffer(&h->raw,(Py_ssize_t)width)<0)return NULL;
    int at=boot_open_absolute(b,path,O_RDONLY);if(at<0)return NULL;
    RootBootstrapFD *owned=&b->fd[at];owned->held_index=index;h->fd=owned->fd;h->birth=owned->birth;
    h->close_alias=1;h->alias_kind=1;h->alias_generation=owned->generation;
    h->generation=owned->generation;h->keeper=owned->keeper;
    if(!S_ISREG(h->birth.st_mode)||(uint64_t)h->birth.st_size!=width||!stat_pin(&h->birth,pin))return NULL;
    uint64_t used=0;
    while(used<width) {
        size_t amount=width-used>65536?65536:(size_t)(width-used);
        ssize_t got=pread(h->fd,PyBytes_AsString(h->raw)+used,amount,used);
        if(got<=0){h->read_used=used;h->read_completed=0;h->read_error=got<0?errno:0;return NULL;}
        used+=got;h->read_used=used;
    }
    h->read_completed=1;h->read_error=0;
    struct stat after,named;PyObject *hash=sha_bytes(h->raw);
    int good=hash&&eq(hash,field(pin,"sha256"))&&fstat(h->fd,&after)==0&&lstat(path,&named)==0&&
        stat_equal(&h->birth,&after,1)&&stat_equal(&h->birth,&named,1);
    Py_XDECREF(hash);return good?h:NULL;
}

static int boot_pipe(RootBootstrapState *b,int offset) {
    int a=boot_new_fd(b);if(a<0)return -1;
    int c=boot_new_fd(b),pair[2]={-1,-1};
    if(c<0){boot_refund_row(b,&b->fd[a],b->fd[a].reserved_slots);return -1;}
    b->pipe_index[offset]=a;b->pipe_index[offset+1]=c;
    if(!slots_free(4)) {
        boot_refund_row(b,&b->fd[a],b->fd[a].reserved_slots);
        boot_refund_row(b,&b->fd[c],b->fd[c].reserved_slots);
        boot_error(b,"bootstrap_pipe_generation_slots",EMFILE);return -1;
    }
    int rc=pipe2(pair,O_CLOEXEC|O_NONBLOCK),saved=rc<0?errno:0;
    /* BOTH preowned rows precede pipe2; no subsequent call before storing
     * BOTH returned descriptor numbers, including failed birth validation. */
    b->fd[a].fd=pair[0];b->fd[c].fd=pair[1];
    if(rc<0) {
        boot_refund_row(b,&b->fd[a],b->fd[a].reserved_slots);
        boot_refund_row(b,&b->fd[c],b->fd[c].reserved_slots);
        errno=saved;return -1;
    }
    int ar=boot_birth(b,a,pair[0]),ae=ar<0?errno:0;
    int cr=boot_birth(b,c,pair[1]),ce=cr<0?errno:0;
    root_storage.bootstrap_pipes[offset]=pair[0];
    root_storage.bootstrap_pipes[offset+1]=pair[1];
    /* Avoid overwriting a protocol fd when dup2 targets stdio. Actual Root
     * stdio/runtime layout still needs installed qualification, never waiver. */
    if(pair[0]<3||pair[1]<3||ar<0||cr<0){errno=ar<0?ae:ce;return -1;}
    return 0;
}
static int boot_keep(RootBootstrapState *b,PyObject *v) {
    if(!v)return -1;
    /* All constructors below reserve a history cell before entry. This
     * compatibility store is not a Source-readable root or a guessed end. */
    if(b->retained_count>=4096)return -1;b->retained[b->retained_count++]=v;return 0;
}
static PyObject *boot_kernel_text(RootBootstrapState *b,const char *path,size_t cap) {
    if(!cap||cap>INPUT_CAP||b->retained_count>4096-4||boot_charge(b,cap,0)<0)return NULL;
    int i=boot_open_absolute(b,path,O_RDONLY);if(i<0)return NULL;
    PyObject *raw=NULL;
    if(boot_zero_buffer(&raw,(Py_ssize_t)cap)<0)return NULL;
    /* Original retained cell exists BEFORE read/close/decode can fail.
     * The checked four-cell room above covers this same native path. */
    (void)boot_keep(b,raw);
    ssize_t n=read(b->fd[i].fd,PyBytes_AsString(raw),cap);int saved=n<0?errno:0;
    if(n>0)b->actual_native_reads+=(uint64_t)n;
    if(n<0){errno=saved;return NULL;}
    if(n==(ssize_t)cap){errno=0;return NULL;}
    /* Keep the actual close errno; saved read errno is zero on this path. */
    if(boot_close(b,i)<0)return NULL;
    PyObject *text=PyUnicode_DecodeUTF8(PyBytes_AsString(raw),n,"strict");
    if(text)(void)boot_keep(b,text);return text?Py_NewRef(text):NULL;
}
static PyObject *boot_cgroup(RootBootstrapState *b) {
    const char *cg=PyUnicode_AsUTF8(field(root_storage.enrollment,"cgroup"));
    if(!cg||b->retained_count>4096-64)return NULL;
    const char *const names[]={"memory.current","memory.peak","memory.max","pids.current","pids.max","cgroup.procs","io.stat","cgroup.events"};
    PyObject *raw=PyDict_New();if(!raw)return NULL;
    /* Persist partial sampler dictionaries before fallible member filling. */
    (void)boot_keep(b,raw);
    PyObject *out=PyDict_New();if(!out)return NULL;
    (void)boot_keep(b,out);
    for(int i=0;i<8;i++) {
        char path[4097];int n=snprintf(path,sizeof(path),"%s/%s",cg,names[i]);
        if(n<0||n>4096)return NULL;
        PyObject *v=boot_kernel_text(b,path,65536);
        if(!v||PyDict_SetItemString(raw,names[i],v)<0) {
            if(v)(void)boot_keep(b,v);return NULL;
        }
        (void)boot_keep(b,v);
    }
    if(PyDict_SetItemString(out,"raw",raw)<0)return NULL;
    const char *const targets[]={"memory_current","memory_peak","memory_max","pids_current","pids_max"};
    for(int i=0;i<5;i++) {
        const char *text=PyUnicode_AsUTF8(field(raw,names[i]));if(!text)return NULL;
        char *end;errno=0;unsigned long long value=strtoull(text,&end,10);
        while(*end==' '||*end=='\n')end++;
        if(errno||end==text||*end)return NULL;
        PyObject *number=PyLong_FromUnsignedLongLong(value);
        if(!number)return NULL;(void)boot_keep(b,number);
        if(PyDict_SetItemString(out,targets[i],number)<0)return NULL;
    }
    uint64_t observed_clock_ns=now_ns();if(!observed_clock_ns)return NULL;
    PyObject *at=PyLong_FromUnsignedLongLong(observed_clock_ns);
    if(!at)return NULL;(void)boot_keep(b,at);
    if(PyDict_SetItemString(out,"at_ns",at)<0)return NULL;return Py_NewRef(out);
}
static PyObject *boot_process_identity(RootBootstrapState *b) {
    if(b->retained_count>4096-16)return NULL;
    char path[80];snprintf(path,sizeof(path),"/proc/%ld/stat",(long)b->pid);
    PyObject *raw=boot_kernel_text(b,path,8192);if(!raw)return NULL;
    (void)boot_keep(b,raw);
    const char *text=PyUnicode_AsUTF8(raw);if(!text)return NULL;
    char copy[8193];strcpy(copy,text);char *end=strrchr(copy,')');
    if(!end||end[1]!=' ')return NULL;char *save=NULL,*word=strtok_r(end+2," ",&save);
    for(int i=0;i<19&&word;i++)word=strtok_r(NULL," ",&save);
    if(!word)return NULL;char *stop;errno=0;uint64_t start=strtoull(word,&stop,10);
    if(errno||(*stop&&*stop!='\n'))return NULL;
    return Py_BuildValue("{s:l,s:K,s:O}","pid",(long)b->pid,"start_ticks",start,"raw_stat",raw);
}
static PyObject *boot_process_io(RootBootstrapState *b) {
    if(b->retained_count>4096-16)return NULL;
    char path[80];snprintf(path,sizeof(path),"/proc/%ld/io",(long)b->pid);
    PyObject *raw=boot_kernel_text(b,path,8192);if(!raw)return NULL;
    (void)boot_keep(b,raw);
    const char *text=PyUnicode_AsUTF8(raw);if(!text)return NULL;
    char copy[8193];strcpy(copy,text);PyObject *counters=PyDict_New();
    if(!counters)return NULL;(void)boot_keep(b,counters);
    char *at=copy;
    while(*at) {
        char *end=strchr(at,'\n'),*colon=strchr(at,':');
        if(!end||!colon||colon>end)return NULL;*end=0;*colon=0;
        char *stop;errno=0;unsigned long long value=strtoull(colon+1,&stop,10);
        while(*stop==' ')stop++;if(errno||*stop||PyDict_GetItemString(counters,at))return NULL;
        PyObject *v=PyLong_FromUnsignedLongLong(value);
        if(!v)return NULL;(void)boot_keep(b,v);
        if(PyDict_SetItemString(counters,at,v)<0)return NULL;at=end+1;
    }
    const char *const names[]={"rchar","wchar","syscr","syscw","read_bytes","write_bytes","cancelled_write_bytes"};
    if(!exact_keys(counters,names,7))return NULL;
    return Py_BuildValue("{s:O,s:O}","raw",raw,"counters",counters);
}
static int boot_census(RootBootstrapState *b,RootBootstrapCensus *f) {
    memset(f,0,sizeof(*f));int slot=boot_new_fd(b);
    if(slot<0)return -1;int fd=open("/proc/self/fd",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
    if(boot_birth(b,slot,fd)<0)return -1;
#ifdef SYS_getdents64
    for(;;) {
        if(boot_charge(b,sizeof(b->census_buffer),0)<0)return -1;
        ssize_t got=syscall(SYS_getdents64,fd,b->census_buffer,sizeof(b->census_buffer));
        if(got>0)b->actual_native_reads+=(uint64_t)got;
        if(got<0){f->original_errno=errno;return -1;}if(!got)break;
        size_t at=0;
        while(at<(size_t)got) {
            struct RootNativeDirent *de=(void *)(b->census_buffer+at);
            size_t head=offsetof(struct RootNativeDirent,name);
            if(de->reclen<head+2||de->reclen>(size_t)got-at)return -1;
            size_t bound=de->reclen-head;
            if(!memchr(de->name,0,bound))return -1;
            char *end;errno=0;long number=strtol(de->name,&end,10);
            if(!errno&&de->name[0]&&!*end&&number!=fd&&number!=b->fd[slot].keeper) {
                if(number<0||number>INT_MAX||f->count>=ROOT_BOOT_FDS)return -1;
                RootBootstrapFDView *row=&f->rows[f->count++];row->fd=(int)number;
                if(fstat(row->fd,&row->identity)<0){row->stat_errno=errno;return -1;}
                row->valid=1;row->flags=fcntl(row->fd,F_GETFD);row->status_flags=fcntl(row->fd,F_GETFL);
                if(row->flags<0||row->status_flags<0){row->flags_errno=errno;return -1;}
            }
            at+=de->reclen;
        }
    }
#else
    return -1;
#endif
    if(boot_close(b,slot)<0)return -1;f->complete=1;return 0;
}
static RootBootstrapFDView *boot_view(RootBootstrapCensus *f,int fd) {
    for(uint64_t i=0;i<f->count;i++)if(f->rows[i].fd==fd)return &f->rows[i];return NULL;
}
static int boot_same_census(RootBootstrapCensus *a,RootBootstrapCensus *c) {
    if(!a->complete||!c->complete||a->count!=c->count)return 0;
    for(uint64_t i=0;i<a->count;i++) {
        RootBootstrapFDView *x=&a->rows[i],*y=boot_view(c,x->fd);
        if(!x->valid||!y||!y->valid||x->flags!=y->flags||x->status_flags!=y->status_flags||!stat_equal(&x->identity,&y->identity,1))return 0;
        for(uint64_t j=0;j<i;j++)if(a->rows[j].fd==x->fd)return 0;
    }return 1;
}
static void boot_fold_enter(RootBootstrapState *);
static void boot_fold_leave(RootBootstrapState *);
static void boot_child_first(RootBootstrapState *,int);
static int boot_child_census(RootBootstrapState *b,RootBootstrapFrame *f,RootBootstrapCensus *c) {
    boot_fold_enter(b);
    unsigned at=b->fd_count;
    if(at-b->child_fd_base<16)
        b->parent_bank->child_fd_operation_at[at-b->child_fd_base]=f->op_count;
    int rc=boot_census(b,c);
    for(unsigned i=at;i<b->fd_count;i++) {
        if(f->census_fd_count>=8){
            errno=EOVERFLOW;boot_child_first(b,errno);boot_fold_leave(b);return -1;
        }
        f->census_fds[f->census_fd_count++]=b->fd[i];
    }
    if(rc<0)boot_child_first(b,errno);
    boot_fold_leave(b);
    return rc;
}
/* Exact same outside-parent storage; no new observer process or grant. */
static void boot_fold_enter(RootBootstrapState *b) {
    RootBootstrapParentBank *q=b->parent_bank;if(!q)return;
    atomic_store_explicit(&q->entered,1,memory_order_release);
    atomic_fetch_add_explicit(&q->sequence,1,memory_order_relaxed);
    q->stage=b->child.first_stage;q->final=b->child;
}
static void boot_fold_leave(RootBootstrapState *b) {
    RootBootstrapParentBank *q=b->parent_bank;if(!q)return;
    q->final=b->child;
    uint64_t count=b->fd_count-b->child_fd_base;
    if(count>16)count=16;
    q->child_fd_count=count;
    for(uint64_t i=0;i<count;i++)q->child_fds[i]=b->fd[b->child_fd_base+i];
    atomic_store_explicit(&q->entered,0,memory_order_release);
    atomic_fetch_add_explicit(&q->sequence,1,memory_order_release);
}
static void boot_child_first(RootBootstrapState *b,int saved) {
    RootBootstrapParentBank *q=b->parent_bank;
    if(q&&!q->first_errno_valid) {
        q->first_errno_valid=1;q->first_errno=saved;
        q->first_fault_kind=1; /* actual native failure by default */
        q->first_stage=b->child.first_stage;q->first_op=(int)b->child.op_count;
        b->child.original_errno=saved;b->child.first_op=q->first_op;
    }
}
static void boot_child_refusal(RootBootstrapState *b,int code,int kind) {
    int first=b->parent_bank&&!b->parent_bank->first_errno_valid;
    boot_child_first(b,code);
    if(first)b->parent_bank->first_fault_kind=kind;
    /* kind2 is a native validation refusal; kind3 is a clock/deadline refusal.
     * Neither synthetic code is represented as a kernel errno return. */
}
static int boot_bank_acquire(RootBootstrapState *b) {
    /* Prospective pool allocation includes shared native body AND immutable
     * whole raw receiver, not a new pool or assumed sizeof qualification. */
    b->map_attempted=1;
    void *raw=mmap(NULL,sizeof(RootBootstrapParentBank),PROT_READ|PROT_WRITE,
                   MAP_SHARED|MAP_ANONYMOUS,-1,0);
    if(raw==MAP_FAILED){b->map_errno=errno;return -1;}
    b->parent_bank=raw;b->map_acquired=1;
    memset(raw,0,sizeof(RootBootstrapParentBank));
    RootBootstrapParentBank *q=b->parent_bank;
    atomic_init(&q->entered,0);atomic_init(&q->sequence,0);atomic_init(&q->sealed,0);
    q->schema=102;q->bytes=sizeof(*q);q->parent_pid=b->parent_pid;q->started_ns=b->started;
    if(!atomic_is_lock_free(&q->entered)||!atomic_is_lock_free(&q->sequence)||
       !atomic_is_lock_free(&q->sealed)){errno=ENOTSUP;return -1;}
    return 0;
}
static int boot_parent_failure_correspondence(RootBootstrapState *b,RootBootstrapParentBank *q) {
    RootBootstrapFrame *f=&q->final;
    if(!b->parent_before.complete||b->parent_before.count>ROOT_BOOT_FDS||
       f->before.count>ROOT_BOOT_FDS||f->after.count>ROOT_BOOT_FDS||f->op_count>ROOT_BOOT_OPS||
       q->child_fd_count>16||q->io_count>ROOT_BOOT_IO_OPS||q->io_used>sizeof(q->io_bank))return 0;
    /* Partial first child census is evidence, not ownership loss: every known
     * row still must match the complete parent's real inherited census. */
    for(uint64_t i=0;i<f->before.count;i++) {
        RootBootstrapFDView *r=&f->before.rows[i],*a=boot_view(&b->parent_before,r->fd);
        if(!a||!r->valid||!stat_equal(&a->identity,&r->identity,0))return 0;
    }
    RootBootstrapCensus live=b->parent_before;
    for(uint64_t i=0;i<f->op_count;i++) {
        RootBootstrapOperation *o=&f->operations[i];RootBootstrapFDView *from=boot_view(&live,o->fd);
        if(o->sequence!=i+1||o->op<1||o->op>4||!o->identity_valid||!o->attempted||
           !o->returned||o->rc<0)return 0; /* full uncertainty retained/refused */
        if(!from||!from->valid||!stat_equal(&from->identity,&o->before,0)) {
            /* A census helper FD acquired after fork is a distinct actual
             * birth; it must not be mistaken for a reused inherited OFD. */
            RootBootstrapFD *birth=NULL;
            for(uint64_t j=0;j<q->child_fd_count;j++) {
                RootBootstrapFD *r=&q->child_fds[j];
                if(r->valid&&r->fd==o->fd&&stat_equal(&r->birth,&o->before,0))birth=r;
            }
            if(!birth||o->op!=1||o->rc!=0)return 0;
            continue;
        }
        if(o->op==1){if(o->rc!=0)return 0;from->valid=0;}
        else if(o->op==2) {
            if(o->rc!=o->target||!o->after_valid||!stat_equal(&o->before,&o->after,0))return 0;
            RootBootstrapFDView *to=boot_view(&live,o->target);
            if(!to){if(live.count>=ROOT_BOOT_FDS)return 0;to=&live.rows[live.count++];}
            *to=*from;to->fd=o->target;to->identity=o->after;to->valid=1;to->flags=0;
        } else {
            if(o->rc||!o->after_valid||!stat_equal(&o->before,&o->after,0))return 0;
            from->identity=o->after;
        }
    }
    for(uint64_t i=0;i<live.count;i++)if(live.rows[i].valid)return 0;
    for(uint64_t i=0;i<q->child_fd_count;i++) {
        RootBootstrapFD *r=&q->child_fds[i];
        if(r->valid&&(!r->closed||!r->close_attempted||r->close_rc||r->close_errno))return 0;
    }
    for(uint64_t i=0;i<q->io_count;i++) {
        RootBootstrapIO *r=&q->io[i];
        if(r->sequence!=i+1||r->offset>q->io_used||r->width>q->io_used-r->offset||
           r->used>r->width||(r->entered&&!r->returned))return 0;
    }
    return 1; /* raw correspondence only; installed ABI/OFD qualification OPEN */
}
static void boot_parent_fold(RootBootstrapState *b) {
    /* The bank belongs to this parent from mmap return. A pipe partial frame,
     * missing ACK or child endpoint loss cannot erase these existing bytes.
     * No live concurrent read of mutable payload; only the bound child's real
     * bound waitid completion permits freezing. ECHILD/deadline leaves owner
     * UNKNOWN. Bound reap is synchronization, NOT a data receipt. */
    if(!b->map_acquired)return;
    if(b->forked&&!b->waited) {
        boot_error(b,"bootstrap_parent_bank_child_still_owned_UNKNOWN",ETIMEDOUT);return;
    }
    b->protect_attempted=1;
    b->protect_rc=mprotect(b->parent_bank,sizeof(*b->parent_bank),PROT_READ);
    b->protect_errno=b->protect_rc<0?errno:0;
    if(b->protect_rc<0){boot_error(b,"bootstrap_parent_bank_freeze",b->protect_errno);return;}
    RootBootstrapParentBank *q=b->parent_bank;
    unsigned entered=atomic_load_explicit(&q->entered,memory_order_acquire);
    unsigned sealed=atomic_load_explicit(&q->sealed,memory_order_acquire);
    const volatile unsigned char *raw=(const volatile unsigned char *)q;
    unsigned char *copy=(unsigned char *)&b->final_parent_copy;
    b->bank_read_bytes=0;
    for(size_t i=0;i<sizeof(*q);i++) {
        unsigned char value=raw[i];copy[i]=value;
        b->bank_read_sink^=value;b->bank_read_bytes++;
    }
    b->bank_full_read=b->bank_read_bytes==sizeof(*q);
    b->bank_stable=q->schema==102&&q->bytes==sizeof(*q)&&
        q->parent_pid==(uint64_t)b->parent_pid&&q->started_ns==b->started&&
        (!b->forked||q->pid==(uint64_t)b->pid);
    b->stock_exec_end_unknown=q->exec_entered&&!q->exec_returned;
    /* A retained entered-without-return prefix is not confirmed exec.
     * A signal can end this prefix before image replacement. Preserve it
     * verbatim; the later phase reader joins bound disposition and all
     * required parent observations under the externally qualified profile. */
    int exec_prefix=b->stock_exec_end_unknown&&b->bank_full_read&&b->bank_stable;
    b->stock_exec_prefix_retained=exec_prefix;
    if(b->forked&&!exec_prefix&&(!b->bank_stable||entered||!sealed||!q->cleanup_complete)) {
        boot_error(b,"bootstrap_full_existing_parent_child_end_UNKNOWN",q->first_errno);
    }
    /* This establishes only completed native FAILURE transport returns.
     * Successful enrolled stock exec discards the anonymous child mapping.
     * A zero exit, ACK, EOF, signature or this pre-exec bank is not the
     * tool's internal last return and is not profile qualification. */
    if(b->forked&&b->bank_full_read&&b->bank_stable&&!entered&&sealed&&
       q->cleanup_complete&&q->first_errno_valid&&!b->stock_exec_end_unknown&&
       boot_parent_failure_correspondence(b,q))
        b->native_failure_transport_complete=1;
    if(b->final_parent_raw)memcpy(PyBytes_AsString(b->final_parent_raw),
                                 &b->final_parent_copy,sizeof(*q));
    else {boot_error(b,"bootstrap_parent_bank_immutable_receiver_NOT_BORN",0);return;}
    /* Copy/full read precedes unmap; its actual result survives in Root static
     * preowned cells even if later immutable Python publication fails. */
    b->unmap_attempted=1;b->unmap_rc=munmap(b->parent_bank,sizeof(*q));
    b->unmap_errno=b->unmap_rc<0?errno:0;
    if(b->unmap_rc==0){b->parent_bank=NULL;b->map_acquired=0;}
    else boot_error(b,"bootstrap_parent_bank_once_unmap_UNCONFIRMED",b->unmap_errno);
}
static int boot_body_count(int fd,FridayPublisherRootCloseFact **body,
    FridayPublisherRootCloseFact **keeper,int **live_body,int **live_keeper,
    RootBootstrapFD **bootstrap) {
    if(body)*body=NULL;if(keeper)*keeper=NULL;if(live_body)*live_body=NULL;
    if(live_keeper)*live_keeper=NULL;if(bootstrap)*bootstrap=NULL;
    if(fd<0)return 0;int n=0;
    RootBootstrapState *b=&root_storage.bootstrap;
    for(unsigned i=0;i<b->fd_count;i++) {
        RootBootstrapFD *f=&b->fd[i];
        if(f->acquired&&f->fd==fd&&f->body_close.birth_valid&&!f->body_close.closed) {
            n++;if(body)*body=&f->body_close;if(keeper)*keeper=&f->keeper_close;
            if(live_body)*live_body=&f->live_body;if(live_keeper)*live_keeper=&f->live_keeper;
            if(bootstrap)*bootstrap=f;
        }
    }
    for(uint64_t i=0;i<root_storage.held_count;i++) {
        RootHeldFile *h=&root_storage.held[i];
        if(h->acquired&&!h->close_alias&&h->fd==fd&&h->body_close.birth_valid&&!h->body_close.closed) {
            n++;if(body)*body=&h->body_close;if(keeper)*keeper=&h->keeper_close;
            if(live_body)*live_body=&h->live_body;if(live_keeper)*live_keeper=&h->live_keeper;
            if(bootstrap)*bootstrap=NULL;
        }
    }
    for(uint64_t i=0;i<root_storage.image_inventory.fd_count;i++) {
        RootImageFD *f=&root_storage.image_inventory.fds[i];
        if(f->acquired&&f->fd==fd&&f->body_close.birth_valid&&!f->body_close.closed) {
            n++;if(body)*body=&f->body_close;if(keeper)*keeper=&f->keeper_close;
            if(live_body)*live_body=&f->live_body;if(live_keeper)*live_keeper=&f->live_keeper;
            if(bootstrap)*bootstrap=NULL;
        }
    }
    return n;
}
static int boot_child_log_keeper(RootBootstrapState *b,RootBootstrapFrame *f,
    FridayPublisherRootCloseFact *body,FridayPublisherRootCloseFact *keeper,int *live_keeper) {
    if(!body||!body->closed||body->keeper<0)return -1;
    if(keeper&&keeper->closed)return 0;
    if(!keeper||keeper->attempted||keeper->validation_attempted)return -1;
    if(f->op_count>=ROOT_BOOT_OPS)return -1;
    RootBootstrapOperation *row=&f->operations[f->op_count++];
    memset(row,0,sizeof(*row));
    row->sequence=f->op_count;row->op=1;row->fd=body->keeper;row->target=-1;row->argument=0;
    boot_fold_enter(b);
    if(fstat(body->keeper,&row->before)<0||!stat_equal(&row->before,&body->birth,0)) {
        row->attempted=1;row->returned=0;row->rc=-1;row->original_errno=errno?errno:EAGAIN;
        boot_fold_leave(b);return -1;
    }
    row->identity_valid=1;row->attempted=1;
    (void)close_keeper_once(body,keeper,live_keeper);
    row->rc=keeper->rc;row->original_errno=keeper->original_errno;row->returned=keeper->attempted?1:0;
    if(!keeper->closed){boot_child_first(b,row->original_errno);boot_fold_leave(b);return -1;}
    boot_fold_leave(b);return 0;
}
static int boot_child_operation(RootBootstrapState *b,RootBootstrapFrame *f,int op,int fd,int target,int arg) {
    if(op==1) {
        if(f->op_count>=ROOT_BOOT_OPS)return -1;
        FridayPublisherRootCloseFact *body=NULL,*keeper=NULL;int *lb=NULL,*lk=NULL;RootBootstrapFD *br=NULL;
        int matches=boot_body_count(fd,&body,&keeper,&lb,&lk,&br);
        RootBootstrapOperation *row=&f->operations[f->op_count++];
        memset(row,0,sizeof(*row));
        row->sequence=f->op_count;row->op=1;row->fd=fd;row->target=target;row->argument=arg;
        boot_fold_enter(b);
        if(matches!=1||!body||!body->birth_valid||body->keeper<0||body->attempted||body->validation_attempted) {
            row->attempted=1;row->returned=0;row->rc=-1;row->original_errno=EAGAIN;
            boot_fold_leave(b);return -1;
        }
        if(fstat(fd,&row->before)<0||!stat_equal(&row->before,&body->birth,0)) {
            row->attempted=1;row->returned=0;row->rc=-1;row->original_errno=errno?errno:EAGAIN;
            boot_child_first(b,row->original_errno);boot_fold_leave(b);return -1;
        }
        row->identity_valid=1;row->attempted=1;
        (void)close_body_once(body,lb);
        row->rc=body->rc;row->original_errno=body->original_errno;row->returned=body->attempted?1:0;
        if(br&&body->closed) {
            br->close_attempted=body->attempted;br->close_rc=body->rc;
            br->close_errno=body->original_errno;br->closed=1;
        }
        if(!body->closed){boot_child_first(b,row->original_errno);boot_fold_leave(b);return -1;}
        boot_fold_leave(b);
        return boot_child_log_keeper(b,f,body,keeper,lk);
    }
    if(f->op_count>=ROOT_BOOT_OPS)return -1;
    RootBootstrapOperation *row=&f->operations[f->op_count++];
    row->sequence=f->op_count;row->op=op;row->fd=fd;row->target=target;row->argument=arg;
    boot_fold_enter(b);
    if(fstat(fd,&row->before)<0){
        row->original_errno=errno;boot_child_first(b,errno);boot_fold_leave(b);return -1;
    }
    row->identity_valid=1;row->attempted=1;
    if(op==2)row->rc=dup2(fd,target);
    else row->rc=fcntl(fd,op==3?F_SETFD:F_SETFL,arg);
    row->original_errno=row->rc<0?errno:0;row->returned=1;
    if(row->rc>=0&&fstat(op==2?target:fd,&row->after)<0) {
        row->original_errno=errno;boot_child_first(b,errno);boot_fold_leave(b);return -1;
    }
    row->after_valid=row->rc>=0;
    if(row->rc<0)boot_child_first(b,row->original_errno);
    boot_fold_leave(b);
    return row->rc<0?-1:0;
}
static int boot_child_io(RootBootstrapState *b,int fd,void *raw,size_t width,int writing,uint64_t deadline) {
    RootBootstrapParentBank *q=b->parent_bank;
    if(!q||q->io_count>=ROOT_BOOT_IO_OPS||width>sizeof(q->io_bank)-q->io_used) {
        errno=EOVERFLOW;boot_child_refusal(b,errno,2);return -1;
    }
    uint64_t index=q->io_count++,offset=q->io_used;q->io_used+=width;
    RootBootstrapIO *row=&q->io[index];
    row->sequence=index+1;row->offset=offset;row->width=width;
    row->fd=fd;row->writing=writing;row->deadline=deadline;
    if(writing)memcpy(q->io_bank+offset,raw,width); /* full before first write */
    size_t at=0;boot_fold_enter(b);
    while(at<width) {
        uint64_t now=now_ns();
        if(!now||now>=deadline){
            row->refusal_code=ETIMEDOUT;row->fault_kind=3;
            boot_child_refusal(b,ETIMEDOUT,3);errno=ETIMEDOUT;goto failed;
        }
        struct pollfd item={fd,writing?POLLOUT:POLLIN,0};
        uint64_t left=(deadline-now+999999)/1000000;int delay=left>25?25:(int)left;
        row->ready=poll(&item,1,delay);row->revents=item.revents;
        row->poll_errno=row->ready<0?errno:0;
        if(row->ready<0)goto failed;if(!row->ready)continue;
        if(item.revents&(POLLERR|POLLNVAL)){
            row->refusal_code=EIO;row->fault_kind=2;
            boot_child_refusal(b,EIO,2);errno=EIO;goto failed;
        }
        row->entered=1;row->returned=0;
        ssize_t n=writing?write(fd,(char *)raw+at,width-at):read(fd,(char *)raw+at,width-at);
        row->rc=(int)n;row->original_errno=n<0?errno:0;row->returned=1;
        if(n<0&&errno==EAGAIN)continue;
        if(n<=0){
            if(!n){row->refusal_code=EPIPE;row->fault_kind=2;
                boot_child_refusal(b,EPIPE,2);errno=EPIPE;}
            goto failed;
        }
        if(!writing)memcpy(q->io_bank+offset+at,(char *)raw+at,(size_t)n);
        at+=(size_t)n;row->used=at; /* exact returned prefix, not width=delivered */
    }
    boot_fold_leave(b);return 0;
failed:
    if(!row->fault_kind)row->fault_kind=1;
    boot_child_first(b,errno);boot_fold_leave(b);return -1;
}
static int boot_child_frame(RootBootstrapState *b,int owner,int gate,unsigned sequence,int phase) {
    if(sequence<1||sequence>2||b->child_frame_attempted[sequence-1])return -1;
    b->child_frame_attempted[sequence-1]=1;
    RootBootstrapFrame *f=&b->child;f->schema=101;f->bytes=sizeof(*f);f->sequence=sequence;
    f->pid=getpid();f->parent_pid=b->parent_pid;f->started_ns=b->started;f->deadline_ns=b->work_deadline;
    f->phase=phase;f->full_pre_complete=f->before.complete;f->full_post_complete=f->after.complete;
    uint64_t limit=phase==1?b->work_deadline:b->seal_deadline;
    if(boot_child_io(b,owner,f,sizeof(*f),1,limit)<0)return -1;
    RootBootstrapAck ack;memset(&ack,0,sizeof(ack));
    for(unsigned message=0;message<2;message++) {
        if(boot_child_io(b,gate,&ack,sizeof(ack),0,limit)<0||
           ack.schema!=101||ack.bytes!=sizeof(ack)||
           ack.pid!=f->pid||ack.parent_pid!=f->parent_pid) {
            if(!b->parent_bank->first_errno_valid)boot_child_refusal(b,EPROTO,2);
            return -1;
        }
        if(!ack.sequence&&ack.accepted==2&&!b->child_start_seen) {
            /* One typed startup packet may already precede an EARLY failure
             * ACK. Consume it once; no unframed G byte can corrupt the ACK. */
            b->child_start_seen=1;continue;
        }
        if(ack.sequence!=sequence||ack.accepted!=1){
            boot_child_refusal(b,EPROTO,2);return -1;
        }
        b->child_ack_received[sequence-1]=1;return 0;
    }
    return -1;
}
static int boot_child_close_from(RootBootstrapState *b,int fd,int born_live,uint64_t first_op) {
    RootBootstrapFrame *f=&b->child;int live=born_live;
    for(uint64_t i=first_op;i<f->op_count;i++) {
        RootBootstrapOperation *r=&f->operations[i];
        if(r->op==1&&r->fd==fd&&r->attempted) {
            if(!r->returned||r->rc!=0)return -1;
            live=0; /* uncertain close is NEVER retried */
        }
        if(r->op==2&&r->target==fd&&r->attempted) {
            if(!r->returned||r->rc!=fd)return -1;
            live=1; /* confirmed replacement: new number incarnation */
        }
    }
    return live?boot_child_operation(b,f,1,fd,-1,0):0;
}
static int boot_child_close_needed(RootBootstrapState *b,int fd,int born_live) {
    return boot_child_close_from(b,fd,born_live,0);
}
static void boot_child(RootBootstrapState *b) {
    RootBootstrapFrame *f=&b->child;memset(f,0,sizeof(*f));
    RootBootstrapParentBank *q=b->parent_bank;q->pid=(uint64_t)getpid();
    f->schema=101;f->bytes=sizeof(*f);f->pid=q->pid;f->parent_pid=b->parent_pid;
    f->started_ns=b->started;f->deadline_ns=b->work_deadline;
    int op0=root_storage.bootstrap_pipes[0],op1=root_storage.bootstrap_pipes[1],
        ep0=root_storage.bootstrap_pipes[2],ep1=root_storage.bootstrap_pipes[3],
        gate0=root_storage.bootstrap_pipes[4],gate1=root_storage.bootstrap_pipes[5],
        owner0=root_storage.bootstrap_pipes[6],owner1=root_storage.bootstrap_pipes[7];
    int early[]={op0,ep0,gate1,owner0};
    f->first_stage=1;if(boot_child_census(b,f,&f->before)<0)goto failed;
    f->first_stage=2;boot_fold_enter(b);q->prctl_entered=1;
    q->prctl_rc=prctl(PR_SET_PDEATHSIG,SIGKILL,0,0,0);
    q->prctl_errno=q->prctl_rc<0?errno:0;q->prctl_returned=1;boot_fold_leave(b);
    if(q->prctl_rc<0){errno=q->prctl_errno;goto failed;}
    if(getppid()!=b->parent_pid){boot_child_refusal(b,ECHILD,2);errno=ECHILD;goto failed;}
    f->first_stage=3;
    for(unsigned i=0;i<4;i++)if(boot_child_operation(b,f,1,early[i],-1,0)<0)goto failed;
    f->first_stage=4;
    if(boot_child_operation(b,f,2,op1,1,0)<0||boot_child_operation(b,f,2,ep1,2,0)<0||
       boot_child_operation(b,f,4,1,-1,0)<0||boot_child_operation(b,f,4,2,-1,0)<0)goto failed;
    q->limits[0]=(struct rlimit){b->outmax+b->errmax,b->outmax+b->errmax};
    q->limits[1]=(struct rlimit){b->helper_alloc,b->helper_alloc};
    q->limits[2]=(struct rlimit){0,0};q->limits[3]=(struct rlimit){128,128};
    const int kinds[]={RLIMIT_FSIZE,RLIMIT_AS,RLIMIT_CORE,RLIMIT_NOFILE};
    for(unsigned i=0;i<4;i++) {
        f->first_stage=5+(int)i;boot_fold_enter(b);q->limit_entered[i]=1;
        q->limit_rc[i]=setrlimit(kinds[i],&q->limits[i]);
        q->limit_errno[i]=q->limit_rc[i]<0?errno:0;q->limit_returned[i]=1;boot_fold_leave(b);
        if(q->limit_rc[i]<0){errno=q->limit_errno[i];goto failed;}
    }
    RootBootstrapAck go;memset(&go,0,sizeof(go));f->first_stage=9;
    if(boot_child_io(b,gate0,&go,sizeof(go),0,b->work_deadline)<0)goto failed;
    if(go.schema!=101||go.bytes!=sizeof(go)||go.sequence||go.pid!=q->pid||
       go.parent_pid!=(uint64_t)b->parent_pid||go.accepted!=2){
        boot_child_refusal(b,EPROTO,2);errno=EPROTO;goto failed;
    }
    b->child_start_seen=1;f->first_stage=10;
    for(uint64_t i=0;i<f->before.count;i++) {
        int fd=f->before.rows[i].fd;
        int keep=fd==0||fd==1||fd==2||fd==root_storage.tool_file->fd||
            fd==root_storage.key_file->fd||fd==root_storage.signature_file->fd||
            fd==root_storage.admission_file->fd||fd==gate0||fd==owner1||
            number_is_open_witness(fd);
        /* A census number with no single registered body stays open.
         * CLOEXEC at fexecve is not a confirmed generation retirement. */
        if(!keep&&boot_body_count(fd,NULL,NULL,NULL,NULL,NULL)==1&&
           boot_child_close_needed(b,fd,1)<0)goto failed;
    }
    f->first_stage=11;
    if(boot_child_operation(b,f,3,root_storage.key_file->fd,-1,0)<0||
       boot_child_operation(b,f,3,root_storage.signature_file->fd,-1,0)<0||
       boot_child_operation(b,f,3,root_storage.admission_file->fd,-1,0)<0)goto failed;
    f->first_stage=12;if(boot_child_census(b,f,&f->after)<0)goto failed;
    f->first_stage=13;if(boot_child_frame(b,owner1,gate0,1,1)<0)goto failed;
    f->first_stage=14;boot_fold_enter(b);q->exec_entered=1;
    /* Successful fexecve does not return and drops this anonymous CHILD
     * mapping. Pre-exec bytes stay parent-owned. This call must not invent
     * a later stock return, and a later parent reap is not this return. */
    fexecve(root_storage.tool_file->fd,b->argv,b->env);
    q->exec_errno=errno;q->exec_returned=1;boot_child_first(b,errno);boot_fold_leave(b);
failed:
    boot_child_first(b,errno);int complete=1;f->first_stage=15;
    /* Parent's exact pre-fork census covers inherited FDs even when the first
     * child census is only a prefix. Last channels stay until compatibility
     * frame's one attempt; that frame/ACK is never complete-end authority. */
    for(uint64_t i=0;i<b->parent_before.count;i++) {
        int fd=b->parent_before.rows[i].fd;if(fd==gate0||fd==owner1)continue;
        if(boot_child_close_needed(b,fd,1)<0)complete=0;
    }
    for(int fd=1;fd<=2;fd++)
        if(!boot_view(&b->parent_before,fd)&&boot_child_close_needed(b,fd,0)<0)complete=0;
    for(uint64_t i=b->child_fd_base;i<b->fd_count;i++) {
        RootBootstrapFD *r=&b->fd[i];
        if(!r->valid||r->closed)continue;
        if(r->close_attempted||r->close_validation_attempted){complete=0;continue;}
        uint64_t born_at=q->child_fd_operation_at[i-b->child_fd_base];
        if(boot_child_close_from(b,r->fd,1,born_at)<0)complete=0;
        if(r->body_close.closed) {
            r->close_attempted=r->body_close.attempted;r->close_rc=r->body_close.rc;
            r->close_errno=r->body_close.original_errno;r->closed=1;
        }
    }
    unsigned sequence=b->child_ack_received[0]?2:1;
    int sent=b->child_frame_attempted[0]&&!b->child_ack_received[0]?-1:
        boot_child_frame(b,owner1,gate0,sequence,2);
    (void)sent;f->first_stage=16;
    if(boot_child_close_needed(b,gate0,1)<0)complete=0;
    if(boot_child_close_needed(b,owner1,1)<0)complete=0;
    boot_fold_enter(b);q->cleanup_complete=complete;boot_fold_leave(b);
    atomic_store_explicit(&q->sealed,1,memory_order_release);
    /* All last channel returns precede this non-returning process end.
     * Killed ENTERED operations remain UNKNOWN; never invent lost returns. */
    _exit(126);
}

/* Validate every original inherited FD and every recorded transition against
 * the ACTUAL pre-fork census. Count/PID/exit code alone are never acceptance. */
static int boot_accept_frame(RootBootstrapState *b,unsigned index) {
    RootBootstrapFrame *f=&b->received[index];
    if(f->schema!=101||f->bytes!=sizeof(*f)||f->sequence!=index+1||
       f->pid!=(uint64_t)b->pid||f->parent_pid!=(uint64_t)b->parent_pid||
       f->started_ns!=b->started||f->deadline_ns!=b->work_deadline||
       (f->phase!=1&&f->phase!=2)||f->op_count>ROOT_BOOT_OPS||
       f->census_fd_count>8||!boot_same_census(&f->before,&b->parent_before))return -1;
    RootBootstrapCensus expected=f->before;
    for(uint64_t i=0;i<f->op_count;i++) {
        RootBootstrapOperation *o=&f->operations[i];
        RootBootstrapFDView *from=boot_view(&expected,o->fd);
        if(o->sequence!=i+1||!from||!from->valid||!o->identity_valid||
           !stat_equal(&from->identity,&o->before,0)||o->op<1||o->op>4)return -1;
        if(!o->attempted||!o->returned||o->rc<0) {
            /* An uncertain actual operation is retained, not replayed/closed
             * again or accepted as a successful owner end. */
            if(f->phase==1)return -1;continue;
        }
        if(o->op==1){if(o->rc!=0)return -1;from->valid=0;}
        else if(o->op==2) {
            if(o->rc!=o->target||!o->after_valid||!stat_equal(&o->before,&o->after,0))return -1;
            RootBootstrapFDView *to=boot_view(&expected,o->target);
            if(!to) {
                if(expected.count>=128)return -1;to=&expected.rows[expected.count++];
            }
            *to=*from;to->fd=o->target;to->flags=0;to->identity=o->after;to->valid=1;
        } else {
            if(!o->after_valid||!stat_equal(&o->before,&o->after,0)||o->rc!=0)return -1;
            if(o->op==3)from->flags=o->argument;
            else from->status_flags=(from->status_flags&O_ACCMODE)|o->argument;
            from->identity=o->after;
        }
    }
    RootBootstrapCensus live;memset(&live,0,sizeof(live));live.complete=1;
    for(uint64_t i=0;i<expected.count;i++)if(expected.rows[i].valid)
        live.rows[live.count++]=expected.rows[i];
    if(!boot_same_census(&live,&f->after))return -1;
    for(unsigned i=0;i<f->census_fd_count;i++) {
        RootBootstrapFD *r=&f->census_fds[i];
        if(!r->valid||!r->close_attempted||!r->closed||r->close_rc||r->close_errno)return -1;
    }
    if(index&&b->received[0].phase!=1)return -1;
    if(index&&f->phase!=2)return -1;
    if(f->phase==1&&f->original_errno)return -1;
    b->child_frame_accepted[index]=1;
    if(f->phase==2)boot_error(b,"bootstrap_actual_child_native_first_fault",f->original_errno);
    RootBootstrapAck *ack=&b->ack[index];memset(ack,0,sizeof(*ack));
    ack->schema=101;ack->bytes=sizeof(*ack);ack->sequence=index+1;
    ack->pid=b->pid;ack->parent_pid=b->parent_pid;ack->accepted=1;
    return 0;
}
static int boot_parent_ack(RootBootstrapState *b,unsigned index) {
    if(!b->child_frame_accepted[index])return -1;
    int fd=root_storage.bootstrap_pipes[5];size_t width=sizeof(RootBootstrapAck);
    if(boot_charge(b,0,width-b->ack_used[index])<0)return -1;
    ssize_t n=write(fd,(char *)&b->ack[index]+b->ack_used[index],width-b->ack_used[index]);
    if(n<0&&errno==EAGAIN)return 0;
    if(n<=0)return -1;
    b->ack_used[index]+=n;b->actual_native_writes+=n;
    if(b->ack_used[index]==width)b->child_ack_written[index]=1;return 0;
}
static int boot_owner_read(RootBootstrapState *b) {
    unsigned index=b->owner_used[0]==sizeof(RootBootstrapFrame)?1:0;
    if(b->owner_used[index]==sizeof(RootBootstrapFrame)) {
        unsigned char extra;ssize_t n=read(root_storage.bootstrap_pipes[6],&extra,1);
        if(n==0){b->owner_eof=1;return 0;}
        if(n<0&&errno==EAGAIN)return 0;
        b->owner_extra=1;if(n>0){b->owner_extra_valid=1;b->owner_extra_byte=extra;b->actual_native_reads+=n;}return -1;
    }
    if(boot_charge(b,sizeof(RootBootstrapFrame)-b->owner_used[index],0)<0)return -1;
    ssize_t n=read(root_storage.bootstrap_pipes[6],
        (char *)&b->received[index]+b->owner_used[index],
        sizeof(RootBootstrapFrame)-b->owner_used[index]);
    if(n<0&&errno==EAGAIN)return 0;
    if(n==0){b->owner_eof=1;return b->owner_used[index]? -1:0;}
    if(n<0)return -1;
    b->owner_used[index]+=n;b->delivered_frame_bytes+=n;b->actual_native_reads+=n;
    if(b->owner_used[index]==sizeof(RootBootstrapFrame)&&boot_accept_frame(b,index)<0)return -1;
    return 0;
}
static int boot_capture(RootBootstrapState *b,int which) {
    int fd=root_storage.bootstrap_pipes[which?2:0];uint64_t *used=which?&b->stderr_used:&b->stdout_used;
    uint64_t maximum=which?b->errmax:b->outmax;int *ended=which?&b->stderr_eof:&b->stdout_eof;
    if(boot_charge(b,sizeof(b->capture),0)<0)return -1;
    ssize_t n=read(fd,b->capture,sizeof(b->capture));
    if(n<0&&errno==EAGAIN)return 0;if(n<0)return -1;
    if(!n){*ended=1;return 0;}
    if((uint64_t)n>maximum-*used) {
        memcpy(b->overflow[which],b->capture,n);b->overflow_used[which]=n;
        b->actual_native_reads+=n;return -1;
    }
    /* Exact original full buffer and used length survive every later failure.
     * There is no Py_CLEAR/copy-then-loss of the cap-width original body. */
    memcpy(PyBytes_AsString(which?b->stderr_raw:b->stdout_raw)+*used,b->capture,n);
    *used+=n;b->actual_native_reads+=n;return 0;
}
static int boot_pump(RootBootstrapState *b,uint64_t limit,int preparing) {
    while(now_ns()&&now_ns()<limit) {
        struct pollfd fds[5]={
            {b->stdout_eof?-1:root_storage.bootstrap_pipes[0],POLLIN,0},
            {b->stderr_eof?-1:root_storage.bootstrap_pipes[2],POLLIN,0},
            {b->owner_eof?-1:root_storage.bootstrap_pipes[6],POLLIN,0},
            {root_storage.bootstrap_pidfd,POLLIN,0},
            {-1,POLLOUT,0}};
        unsigned ack_index=b->child_ack_written[0]?1:0;
        if(b->child_frame_accepted[ack_index]&&!b->child_ack_written[ack_index])
            fds[4].fd=root_storage.bootstrap_pipes[5];
        int ready=poll(fds,5,20);if(ready<0)return -1;
        for(unsigned i=0;i<5;i++)if(fds[i].revents&(POLLERR|POLLNVAL))return -1;
        for(int i=0;i<2;i++)if(fds[i].revents&(POLLIN|POLLHUP))
            if(boot_capture(b,i)<0)return -1;
        if(fds[2].revents&(POLLIN|POLLHUP))if(boot_owner_read(b)<0)return -1;
        if(fds[4].revents&POLLOUT)if(boot_parent_ack(b,ack_index)<0)return -1;
        if(fds[3].revents&POLLIN){b->child_terminal=1;return 0;}
        if(preparing&&b->child_ack_written[0])return 0;
    }
    errno=ETIMEDOUT;return -1;
}
static int boot_signal_once(RootBootstrapState *b) {
    if(!b->forked||b->waited||b->child_terminal)return 0;
    if(b->signal_attempted)return -1;b->signal_attempted=1;
#ifdef SYS_pidfd_send_signal
    if(b->spawn_pidfd_bound&&b->pidfd_index>=0&&b->fd[b->pidfd_index].valid&&
       !b->fd[b->pidfd_index].close_attempted&&!b->fd[b->pidfd_index].close_validation_attempted) {
        struct stat actual;
        if(fstat(b->fd[b->pidfd_index].fd,&actual)<0||
           !stat_equal(&actual,&b->fd[b->pidfd_index].birth,0)) {
            b->signal_rc=-1;b->signal_errno=errno;return -1;
        }
        b->signal_rc=(int)syscall(SYS_pidfd_send_signal,b->fd[b->pidfd_index].fd,SIGKILL,NULL,0);
    } else
#endif
    {
        /* No destructive numeric-PID fallback under unqualified exclusive
         * reaper/OFD identity. Keep exact child owner UNKNOWN instead. */
        b->signal_rc=-1;errno=ENOTSUP;
    }
    b->signal_errno=b->signal_rc<0?errno:0;return b->signal_rc;
}
static void boot_settle_child(RootBootstrapState *b) {
    if(!b->forked)return;
    if(!b->child_terminal&&!b->failed&&boot_pump(b,b->work_deadline,0)<0)
        boot_error(b,"bootstrap_capture_or_work_deadline",errno);
    if(!b->child_terminal&&b->failed) {
        /* Let an already delivered child failure frame be ACKed, but only
         * while original seal time remains; never blocking pipe writes. */
        uint64_t now=now_ns(),short_end=now+200000000ULL;
        if(short_end>b->seal_deadline)short_end=b->seal_deadline;
        (void)boot_pump(b,short_end,0);
    }
    if(b->child_terminal&&!b->last_io&&!b->final_io_attempted) {
        b->final_io_attempted=1;
        b->last_io=boot_process_io(b);if(!b->last_io)boot_error(b,"bootstrap_final_IO_UNKNOWN",errno);
    }
    if(!b->child_terminal&&boot_signal_once(b)<0)boot_error(b,"bootstrap_once_signal_UNCONFIRMED",b->signal_errno);
    /* Bound P_PIDFD reap, never wait4 on a potentially reused numeric PID.
     * Empty WNOHANG is a new observation. EINTR/ECHILD is NOT retried.
     * Kernel raw waitid+rusage ABI and exclusive OFD owner remain NOT_RUN. */
    while(!b->waited&&now_ns()&&now_ns()<b->seal_deadline) {
        if(root_storage.bootstrap_pidfd>=0) {
            struct pollfd item={root_storage.bootstrap_pidfd,POLLIN,0};
            int r=poll(&item,1,20);
            if(r<0){boot_error(b,"bootstrap_terminal_poll",errno);break;}
            if(item.revents&(POLLERR|POLLNVAL)){boot_error(b,"bootstrap_pidfd_identity_UNKNOWN",EIO);break;}
            if(!(item.revents&POLLIN))continue;
            b->child_terminal=1;
        }
        if(b->child_terminal&&!b->last_io&&!b->final_io_attempted) {
            /* A failed final sampler is not retried on each wait iteration.
             * Preserve its actual failure; dependent IO remains UNKNOWN. */
            b->final_io_attempted=1;
            b->last_io=boot_process_io(b);if(!b->last_io)boot_error(b,"bootstrap_final_IO_UNKNOWN",errno);
        }
        if(!b->spawn_pidfd_bound||b->pidfd_index<0||
           !b->fd[b->pidfd_index].valid||b->fd[b->pidfd_index].close_attempted) {
            b->wait_errno=ENOTSUP;boot_error(b,"bootstrap_bound_reap_owner_UNCONFIRMED",ENOTSUP);break;
        }
        b->wait_attempted=1;memset(&b->reap_info,0,sizeof(b->reap_info));
        b->reap_returned=0;
#ifdef SYS_waitid
        /* Linux idtype P_PIDFD=3; numeric native ABI explicit, not portability. */
        b->reap_rc=(int)syscall(SYS_waitid,3,b->fd[b->pidfd_index].fd,
                               &b->reap_info,WEXITED|WNOHANG,&b->usage);
#else
        b->reap_rc=-1;errno=ENOSYS;
#endif
        b->reap_errno=b->reap_rc<0?errno:0;b->reap_returned=1;
        if(b->reap_rc<0){
            b->wait_errno=b->reap_errno;
            boot_error(b,"bootstrap_bound_waitid_UNCONFIRMED",b->reap_errno);break;
        }
        if(b->reap_info.si_pid) {
            if(b->reap_info.si_pid!=b->pid){
                boot_error(b,"bootstrap_bound_waitid_identity_mismatch",0);break;
            }
            b->waited=1;b->child_terminal=1;
            if(b->reap_info.si_code==CLD_EXITED)b->status=b->reap_info.si_status<<8;
            else if(b->reap_info.si_code==CLD_KILLED)b->status=b->reap_info.si_status;
            else if(b->reap_info.si_code==CLD_DUMPED)b->status=b->reap_info.si_status|128;
            else boot_error(b,"bootstrap_bound_waitid_exit_disposition_UNKNOWN",0);
            break;
        }
        if(root_storage.bootstrap_pidfd<0) {
            struct pollfd item={root_storage.bootstrap_pipes[6],POLLIN,0};
            if(poll(&item,1,20)<0){boot_error(b,"bootstrap_wait_observation",errno);break;}
        }
    }
    if(!b->waited)boot_error(b,"bootstrap_child_end_DEADLINE_UNCONFIRMED",ETIMEDOUT);
    if(b->waited) {
        /* Drain complete stdout/stderr and owner EOF after wait without an
         * unbounded read loop. Partial frames stay exact partial bytes. */
        while((!b->stdout_eof||!b->stderr_eof||!b->owner_eof)&&now_ns()&&now_ns()<b->seal_deadline) {
            int progress=0;
            if(!b->stdout_eof){uint64_t old=b->stdout_used;if(boot_capture(b,0)<0){boot_error(b,"bootstrap_final_stdout",errno);break;}progress|=old!=b->stdout_used||b->stdout_eof;}
            if(!b->stderr_eof){uint64_t old=b->stderr_used;if(boot_capture(b,1)<0){boot_error(b,"bootstrap_final_stderr",errno);break;}progress|=old!=b->stderr_used||b->stderr_eof;}
            if(!b->owner_eof){uint64_t old=b->delivered_frame_bytes;if(boot_owner_read(b)<0){boot_error(b,"bootstrap_final_owner_frame",errno);break;}progress|=old!=b->delivered_frame_bytes||b->owner_eof;}
            if(!progress){boot_error(b,"bootstrap_after_wait_nonEOF_UNCONFIRMED",EAGAIN);break;}
        }
    }
}
static void boot_close_all(RootBootstrapState *b) {
    b->all_fd_ends=1;
    for(unsigned i=0;i<b->fd_count;i++) {
        if(!b->fd[i].valid&&b->fd[i].reserved_slots)
            boot_refund_row(b,&b->fd[i],b->fd[i].reserved_slots); /* UNUSED not actual close */
        if(b->fd[i].valid&&!(b->fd[i].body_close.closed&&b->fd[i].keeper_close.closed)) {
        if(b->fd[i].close_validation_attempted||
           (b->fd[i].body_close.validation_attempted&&!b->fd[i].body_close.closed))
            {b->all_fd_ends=0;continue;}
        if(boot_close(b,(int)i)<0){b->all_fd_ends=0;boot_error(b,"bootstrap_parent_close_UNCONFIRMED",b->fd[i].close_errno);}
    }
    }
    if(b->active_fds)b->all_fd_ends=0;
}
static int boot_record_object(RootBootstrapState *b,const char *key,PyObject *value) {
    return PyDict_SetItemString(b->compat_record,key,value?value:Py_None);
}
static int boot_record_integer(RootBootstrapState *b,const char *key,uint64_t value) {
    if(b->retained_count>=4096)return -1;
    PyObject *number=PyLong_FromUnsignedLongLong(value);
    if(!number)return -1;(void)boot_keep(b,number);return boot_record_object(b,key,number);
}
static int boot_record_signed(RootBootstrapState *b,const char *key,int64_t value) {
    if(b->retained_count>=4096)return -1;
    PyObject *number=PyLong_FromLongLong(value);
    if(!number)return -1;(void)boot_keep(b,number);return boot_record_object(b,key,number);
}
static int boot_record_blob(RootBootstrapState *b,const char *key,const void *raw,size_t width,PyObject **cell) {
    if(b->retained_count>=4096)return -1;
    *cell=PyBytes_FromStringAndSize(raw,(Py_ssize_t)width);
    if(!*cell)return -1;return boot_record_object(b,key,*cell);
}
static const char *boot_lifetime_phase_name(int phase) {
    if(phase==1)return "PRE_EXEC_NATIVE_RETURNED";
    if(phase==2)return "PUBLIC_STOCK_PROCESS_EXITED";
    if(phase==3)return "SIGNALLED_CHILD";
    if(phase==4)return "UNRESOLVED_ASYNCHRONOUS";
    return "UNRESOLVED_PREFIX";
}
/* Parent observations under the independently qualified selected profile.
 * Qualification/admission belong to the existing external launcher/owner;
 * these actual observations cannot issue them. No author NOT_RUN state is
 * a runtime predicate. CLD_EXITED alone is not the retirement conjunction. */
static void boot_account_public_stock(RootBootstrapState *b) {
    const RootBootstrapParentBank *q=b->bank_full_read?&b->final_parent_copy:NULL;
    int exec_prefix=q&&q->exec_entered&&!q->exec_returned&&b->stock_exec_prefix_retained;
    int native_returned=q&&b->native_failure_transport_complete&&!exec_prefix;
    int bound=b->spawn_pidfd_bound&&b->spawn_returned&&b->pid>0&&b->waited&&
        b->reap_returned&&b->reap_rc==0&&b->reap_info.si_pid==b->pid;
    b->child_transport_end_confirmed=0;
    b->kernel_process_lifetime_ended=0;
    b->stock_image_replacement_confirmed=0;
    b->lifetime_phase=0;
    b->public_stock_observables_consumed=0;
    b->public_stock_parent_legal_retirement_accounted=0;
    if(bound&&(b->reap_info.si_code==CLD_EXITED||b->reap_info.si_code==CLD_KILLED||
               b->reap_info.si_code==CLD_DUMPED))
        b->kernel_process_lifetime_ended=1;
    /* The selected native child has only a returned-failure exit after a
     * failed fexecve. An untouched entered prefix plus normal bound exit,
     * exact accepted preexec frame/ACK and no native failure therefore
     * belongs to the stock image in that qualified profile. Killed/unknown
     * prefixes NEVER get this confirmation. The original absent return is
     * still absent; no syscall return or private tool heap is invented. */
    if(q&&b->bank_full_read&&b->bank_stable&&b->forked) {
        if(exec_prefix&&bound&&b->reap_info.si_code==CLD_EXITED&&
           q->stage==14&&q->final.first_stage==14&&!q->first_errno_valid&&
           !q->cleanup_complete&&b->child_frame_accepted[0]&&
           b->child_ack_written[0]&&b->identity_qualified) {
            b->stock_image_replacement_confirmed=1;
            b->lifetime_phase=2;
        } else if(bound&&(b->reap_info.si_code==CLD_KILLED||b->reap_info.si_code==CLD_DUMPED))
            b->lifetime_phase=3;
        else if(native_returned&&bound&&b->reap_info.si_code==CLD_EXITED)
            b->lifetime_phase=1;
        else if(exec_prefix&&!bound)
            b->lifetime_phase=4;
    }
    /* This also covers completed failures BEFORE fexecve was attempted.
     * The original sealed native failure transport, not stock exit, proves
     * that branch's transport end. */
    if(b->lifetime_phase==1&&q&&q->sealed&&q->cleanup_complete&&!exec_prefix)
        b->child_transport_end_confirmed=1;
    int parent_fds=b->all_fd_ends&&b->active_fds==0&&b->unmap_attempted&&b->unmap_rc==0&&!b->map_acquired;
    int captures=b->stdout_eof&&b->stderr_eof&&b->overflow_used[0]==0&&b->overflow_used[1]==0&&
        b->owner_eof&&b->owner_used[1]==0;
    int frame=b->child_frame_accepted[0]&&b->child_ack_written[0];
    int resources=b->final_io_attempted&&b->child_IO_known&&b->last_io!=NULL;
    b->public_stock_observables_consumed=b->lifetime_phase==2&&parent_fds&&captures&&frame&&
        resources&&b->bank_full_read&&b->bank_stable&&b->kernel_process_lifetime_ended&&
        b->stock_image_replacement_confirmed&&!b->child_transport_end_confirmed&&b->spawn_pidfd_bound;
    b->public_stock_parent_legal_retirement_accounted=!b->failed&&b->public_stock_observables_consumed;
}
static void boot_parent_end_before_publication(RootBootstrapState *b) {
    RootBootstrapParentEnd *e=&b->parent_end;
    e->schema=102;e->bytes=sizeof(*e);e->parent_pid=b->parent_pid;
    e->child_pid=b->pid<0?UINT64_MAX:(uint64_t)b->pid;
    e->started_ns=b->started;e->work_deadline=b->work_deadline;e->seal_deadline=b->seal_deadline;
    e->attempted=b->attempted;
    e->failed=b->failed;
    e->first_errno_valid=b->first_errno_valid;
    e->first_errno=b->first_errno;
    e->fd_count=b->fd_count;
    e->active_fds=b->active_fds;
    e->bank_read_bytes=b->bank_read_bytes;
    e->stdout_used=b->stdout_used;
    e->stderr_used=b->stderr_used;
    e->map_attempted=b->map_attempted;
    e->map_errno=b->map_errno;
    e->map_acquired=b->map_acquired;
    e->protect_attempted=b->protect_attempted;
    e->protect_rc=b->protect_rc;
    e->protect_errno=b->protect_errno;
    e->unmap_attempted=b->unmap_attempted;
    e->unmap_rc=b->unmap_rc;
    e->unmap_errno=b->unmap_errno;
    e->bank_full_read=b->bank_full_read;
    e->bank_stable=b->bank_stable;
    e->stock_exec_end_unknown=b->stock_exec_end_unknown;
    e->spawn_args=b->spawn_args;
    e->spawn_rc=b->spawn_rc;
    e->spawn_entered=b->spawn_entered;
    e->spawn_returned=b->spawn_returned;
    e->spawn_pidfd=b->spawn_pidfd;
    e->spawn_errno=b->spawn_errno;
    e->spawn_pidfd_bound=b->spawn_pidfd_bound;
    e->signal_attempted=b->signal_attempted;
    e->signal_rc=b->signal_rc;
    e->signal_errno=b->signal_errno;
    e->wait_attempted=b->wait_attempted;
    e->waited=b->waited;
    e->status=b->status;
    e->wait_errno=b->wait_errno;
    e->reap_info=b->reap_info;
    e->reap_rc=b->reap_rc;
    e->reap_errno=b->reap_errno;
    e->reap_returned=b->reap_returned;
    e->usage=b->usage;
    e->all_fd_ends=b->all_fd_ends;
    e->child_IO_known=b->child_IO_known;
    e->final_io_attempted=b->final_io_attempted;
    e->child_transport_end_confirmed=b->child_transport_end_confirmed;
    e->lifetime_phase=b->lifetime_phase;
    e->public_stock_observables_consumed=b->public_stock_observables_consumed;
    e->public_stock_parent_legal_retirement_accounted=b->public_stock_parent_legal_retirement_accounted;
    e->stock_exec_prefix_retained=b->stock_exec_prefix_retained;
    e->kernel_process_lifetime_ended=b->kernel_process_lifetime_ended;
    e->stock_image_replacement_confirmed=b->stock_image_replacement_confirmed;
    e->actual_reads=b->actual_native_reads;e->actual_writes=b->actual_native_writes;
    e->native_failure_correspondence=b->native_failure_transport_complete;
    memcpy(e->owner_used,b->owner_used,sizeof(e->owner_used));
    memcpy(e->ack_used,b->ack_used,sizeof(e->ack_used));
    memcpy(e->overflow_used,b->overflow_used,sizeof(e->overflow_used));
    const char *phase=b->first_phase?b->first_phase:"NONE";
    size_t n=strlen(phase);if(n>=sizeof(e->first_phase))n=sizeof(e->first_phase)-1;
    memcpy(e->first_phase,phase,n);e->first_phase[n]=0;
    /* Native last-effect outcome cutoff, BEFORE compatibility publication.
     * Later Python first/secondary/return_pending originals remain strongly
     * in actual b; no post-publication mutation of this immutable byte bank. */
    if(b->parent_end_raw)memcpy(PyBytes_AsString(b->parent_end_raw),e,sizeof(*e));
}
static int boot_publish(RootBootstrapState *b) {
    /* All missing/partially born buffers remain explicit. If stock allocation
     * fails, original native storage/first triple survives; not a fake record. */
    if(PyErr_Occurred()||b->retained_count>4096-128)return -1;
    b->compat_record=PyDict_New();root_storage.bootstrap_record=b->compat_record;
    if(!b->compat_record)return -1;
    for(unsigned i=0;i<2;i++) {
        if(b->wire_raw[i])memcpy(PyBytes_AsString(b->wire_raw[i]),&b->received[i],sizeof(RootBootstrapFrame));
        if(b->parent_raw[i])memcpy(PyBytes_AsString(b->parent_raw[i]),
            i?&b->parent_after:&b->parent_before,sizeof(RootBootstrapCensus));
        if(b->overflow_raw[i])memcpy(PyBytes_AsString(b->overflow_raw[i]),b->overflow[i],sizeof(b->overflow[i]));
    }
    b->outcome_raw=PyBytes_FromStringAndSize((const char *)b->fd,(Py_ssize_t)b->fd_count*sizeof(*b->fd));
    if(!b->outcome_raw)return -1;
    PyObject *out=b->stdout_raw?PyBytes_FromStringAndSize(PyBytes_AsString(b->stdout_raw),b->stdout_used):NULL;
    if(b->stdout_raw&&!out)return -1;
    if(out)(void)boot_keep(b,out);
    PyObject *err=b->stderr_raw?PyBytes_FromStringAndSize(PyBytes_AsString(b->stderr_raw),b->stderr_used):NULL;
    if(b->stderr_raw&&!err)return -1;
    if(err)(void)boot_keep(b,err);
    PyObject *triple=PyTuple_Pack(3,b->first.type?b->first.type:Py_None,
        b->first.value?b->first.value:Py_None,b->first.tb?b->first.tb:Py_None);
    if(!triple)return -1;
    (void)boot_keep(b,triple);
    PyObject *history=PyTuple_New(b->retained_count);
    if(!history)return -1;
    for(uint64_t i=0;i<b->retained_count;i++)PyTuple_SET_ITEM(history,i,Py_NewRef(b->retained[i]));
    (void)boot_keep(b,history);
    PyObject *phase_name=PyUnicode_FromString(boot_lifetime_phase_name(b->lifetime_phase));
    if(!phase_name)return -1;
    (void)boot_keep(b,phase_name);
    const char *phase=b->first_phase?b->first_phase:"NONE";
    PyObject *schema=PyUnicode_FromString("friday.sol102.bootstrap-parent-both-outcome.v1");
    if(!schema)return -1;
    (void)boot_keep(b,schema);
    PyObject *firstphase=PyUnicode_FromString(phase);
    if(!firstphase)return -1;
    (void)boot_keep(b,firstphase);
    b->secondary_immutable=PyTuple_New(b->secondary_count);if(!b->secondary_immutable)return -1;
    for(unsigned at=0;at<b->secondary_count;at++) {
        RootBootstrapError *original=&b->secondary[at];
        PyObject *row=PyTuple_Pack(3,original->type?original->type:Py_None,
            original->value?original->value:Py_None,original->tb?original->tb:Py_None);
        if(!row)return -1;PyTuple_SET_ITEM(b->secondary_immutable,at,row);
    }
    if(boot_record_object(b,"all_actual_secondary_triples",b->secondary_immutable)||
       boot_record_object(b,"full_existing_parent_native_bank",b->final_parent_raw)||
       boot_record_object(b,"full_parent_native_effects_before_publication",b->parent_end_raw)||
       boot_record_signed(b,"existing_parent_bank_full_read",b->bank_full_read)||
       boot_record_integer(b,"existing_parent_bank_read_bytes",b->bank_read_bytes)||
       boot_record_signed(b,"native_failure_transport_correspondence",b->native_failure_transport_complete)||
       boot_record_signed(b,"stock_exec_final_end_UNKNOWN",b->stock_exec_end_unknown)||
       boot_record_object(b,"lifetime_phase",phase_name)||
       boot_record_signed(b,"lifetime_phase_code",b->lifetime_phase)||
       boot_record_signed(b,"public_stock_observables_consumed",b->public_stock_observables_consumed)||
       boot_record_signed(b,"public_stock_parent_legal_retirement_accounted",b->public_stock_parent_legal_retirement_accounted)||
       boot_record_signed(b,"stock_exec_prefix_retained",b->stock_exec_prefix_retained)||
       boot_record_signed(b,"kernel_process_lifetime_ended",b->kernel_process_lifetime_ended)||
       boot_record_signed(b,"stock_image_replacement_confirmed",b->stock_image_replacement_confirmed)||
       boot_record_signed(b,"reap_si_code",b->reap_returned?b->reap_info.si_code:-1)||
       boot_record_signed(b,"parent_shared_mapping_retained",b->map_acquired)||
       boot_record_signed(b,"parent_bank_map_errno",b->map_errno)||
       boot_record_signed(b,"parent_bank_protect_rc",b->protect_rc)||
       boot_record_signed(b,"parent_bank_protect_errno",b->protect_errno)||
       boot_record_signed(b,"parent_bank_unmap_attempted",b->unmap_attempted)||
       boot_record_signed(b,"parent_bank_unmap_rc",b->unmap_rc)||
       boot_record_signed(b,"parent_bank_unmap_errno",b->unmap_errno)||
       boot_record_signed(b,"atomic_spawn_entered",b->spawn_entered)||
       boot_record_signed(b,"atomic_spawn_returned",b->spawn_returned)||
       boot_record_signed(b,"atomic_spawn_rc",b->spawn_rc)||
       boot_record_signed(b,"atomic_spawn_errno",b->spawn_errno)||
       boot_record_signed(b,"atomic_spawn_pidfd_bound",b->spawn_pidfd_bound)||
       boot_record_signed(b,"bound_reap_rc",b->reap_rc)||
       boot_record_signed(b,"bound_reap_errno",b->reap_errno)||
       boot_record_blob(b,"full_bound_waitid_siginfo_native",&b->reap_info,sizeof(b->reap_info),&b->reap_info_raw)||
       boot_record_blob(b,"full_atomic_clone_args_native",&b->spawn_args,sizeof(b->spawn_args),&b->spawn_args_raw)||
       boot_record_blob(b,"full_bound_waitid_rusage_native",&b->usage,sizeof(b->usage),&b->wait_usage_raw)||
       boot_record_blob(b,"full_first_ACK_native",&b->ack[0],sizeof(b->ack[0]),&b->ack_raw[0])||
       boot_record_blob(b,"full_late_ACK_native",&b->ack[1],sizeof(b->ack[1]),&b->ack_raw[1])||
       boot_record_blob(b,"full_initial_gate_native",&b->initial_gate,sizeof(b->initial_gate),&b->ack_raw[2]))return -1;
    if(boot_record_object(b,"schema",schema)||boot_record_object(b,"first_phase",firstphase)||
       boot_record_signed(b,"pid",b->pid)||boot_record_signed(b,"exit_code",b->waited&&WIFEXITED(b->status)?WEXITSTATUS(b->status):-1)||
       boot_record_signed(b,"raw_wait_status",b->status)||boot_record_integer(b,"started_ns",b->started)||
       boot_record_object(b,"before_effect_profile",b->profile)||boot_record_object(b,"actual_process",b->identity)||
       boot_record_object(b,"actual_final_io",b->last_io)||boot_record_object(b,"raw_stdout",out)||
       boot_record_object(b,"raw_stderr",err)||boot_record_object(b,"before_cgroup",b->before)||
       boot_record_object(b,"after_cgroup",b->after)||boot_record_signed(b,"first_errno",b->first_errno)||
       boot_record_signed(b,"forked",b->forked)||boot_record_signed(b,"waited",b->waited)||
       boot_record_signed(b,"wait_errno",b->wait_errno)||boot_record_signed(b,"parent_FD_ends",b->all_fd_ends)||
       boot_record_signed(b,"prepared_frame_ACK",b->child_ack_written[0])||
       boot_record_integer(b,"first_ACK_delivered_bytes",b->ack_used[0])||
       boot_record_integer(b,"late_ACK_delivered_bytes",b->ack_used[1])||
       boot_record_signed(b,"initial_gate_written",b->initial_gate_written)||
       boot_record_signed(b,"failure_frame_ACK",b->child_ack_written[1])||
       boot_record_signed(b,"owner_EOF",b->owner_eof)||boot_record_signed(b,"last_child_transport_end_confirmed",b->child_transport_end_confirmed)||
       boot_record_integer(b,"stdout_used",b->stdout_used)||boot_record_integer(b,"stderr_used",b->stderr_used)||
       boot_record_integer(b,"owner_first_used",b->owner_used[0])||boot_record_integer(b,"owner_late_used",b->owner_used[1])||
       boot_record_object(b,"full_stdout_cap_buffer",b->stdout_raw)||boot_record_object(b,"full_stderr_cap_buffer",b->stderr_raw)||
       boot_record_object(b,"full_first_frame_bank",b->wire_raw[0])||boot_record_object(b,"full_late_frame_bank",b->wire_raw[1])||
       boot_record_object(b,"full_parent_FD_history_bank",b->outcome_raw)||
       boot_record_object(b,"full_parent_before_FD_census",b->parent_raw[0])||
       boot_record_object(b,"full_parent_after_FD_census",b->parent_raw[1])||
       boot_record_object(b,"first_actual_triple",triple)||boot_record_object(b,"all_original_stock_sampler_cells",history)||
       boot_record_object(b,"full_stdout_overflow_buffer",b->overflow_raw[0])||
       boot_record_object(b,"full_stderr_overflow_buffer",b->overflow_raw[1])||
       boot_record_integer(b,"stdout_overflow_used",b->overflow_used[0])||
       boot_record_integer(b,"stderr_overflow_used",b->overflow_used[1])||
       boot_record_integer(b,"native_FD_history_count",b->fd_count)||
       boot_record_signed(b,"owner_extra_valid",b->owner_extra_valid)||
       boot_record_integer(b,"owner_extra_byte",b->owner_extra_byte)||
       boot_record_integer(b,"actual_native_reads",b->actual_native_reads)||
       boot_record_integer(b,"actual_native_writes",b->actual_native_writes)||
       (b->child_IO_known?boot_record_integer(b,"child_actual_reads",b->child_actual_reads):boot_record_object(b,"child_actual_reads",Py_None))||
       (b->child_IO_known?boot_record_integer(b,"child_actual_writes",b->child_actual_writes):boot_record_object(b,"child_actual_writes",Py_None))||
       boot_record_signed(b,"child_IO_known",b->child_IO_known)||
       boot_record_signed(b,"final_IO_attempted_once",b->final_io_attempted)||
       boot_record_object(b,"compatibility_view_is_authority",Py_False)||
       boot_record_object(b,"full_native_end",Py_False))return -1;
    b->endpoint_immutable=1;b->published=1;return 0;
}
static int native_bootstrap_signature(void) {
    FridayPublisherRootStorage *s=&root_storage;RootBootstrapState *b=&s->bootstrap;
    if(b->attempted)return FridayPublisherMasterFault(&s->pool,"bootstrap_once");
    b->attempted=1;b->pid=-1;b->parent_pid=getpid();b->pidfd_index=b->cgroupfd_index=-1;
    for(unsigned i=0;i<8;i++){b->pipe_index[i]=-1;s->bootstrap_pipes[i]=-1;}
    b->started=now_ns();b->work_deadline=s->pool.work_deadline_ns;b->seal_deadline=s->pool.deadline_ns;
    PyObject *e=s->enrollment;
    s->admission_file=boot_hold_file(b,field(e,"admission"),INPUT_CAP);
    if(!s->admission_file){boot_error(b,"bootstrap_admission_held_input",errno);goto settled;}
    s->signature_file=boot_hold_file(b,field(e,"admission_signature"),INPUT_CAP);
    if(!s->signature_file){boot_error(b,"bootstrap_signature_held_input",errno);goto settled;}
    s->key_file=boot_hold_file(b,field(e,"admission_key"),INPUT_CAP);
    if(!s->key_file){boot_error(b,"bootstrap_key_held_input",errno);goto settled;}
    s->tool_file=boot_hold_file(b,field(e,"signature_tool"),BODY_CAP);
    if(!s->tool_file){boot_error(b,"bootstrap_tool_held_input",errno);goto settled;}
    PyObject *deps=field(e,"signature_tool_dependencies");b->profile=Py_XNewRef(field(e,"bootstrap_helper_profile"));
    if(!PyList_CheckExact(deps)||PyList_Size(deps)<1||PyList_Size(deps)>128) {
        boot_error(b,"bootstrap_dependencies",0);goto settled;
    }
    for(Py_ssize_t i=0;i<PyList_Size(deps);i++)if(!boot_hold_file(b,PyList_GetItem(deps,i),BODY_CAP)) {
        boot_error(b,"bootstrap_dependency_held_input",errno);goto settled;
    }
    const char *const names[]={"read_max","stdout_max","stderr_max","wall_ns","allocation_max","slots"};
    uint64_t reads=0,slots=0;
    if(!exact_keys(b->profile,names,6)||scalar_u64(field(b->profile,"read_max"),&reads)||
       scalar_u64(field(b->profile,"stdout_max"),&b->outmax)||scalar_u64(field(b->profile,"stderr_max"),&b->errmax)||
       scalar_u64(field(b->profile,"wall_ns"),&b->wall)||scalar_u64(field(b->profile,"allocation_max"),&b->helper_alloc)||
       scalar_u64(field(b->profile,"slots"),&slots)||slots!=1||!b->wall||
       b->outmax>INPUT_CAP||b->errmax>INPUT_CAP-b->outmax||b->helper_alloc>RAM_CAP) {
        boot_error(b,"bootstrap_profile",0);goto settled;
    }
    uint64_t wall_end=b->started;
    if(plus(&wall_end,b->wall)<0){boot_error(b,"bootstrap_wall_overflow",0);goto settled;}
    if(wall_end<b->work_deadline)b->work_deadline=wall_end;
    b->reserved_reads=reads;b->reserved_output=2*(b->outmax+b->errmax);
    b->reserved_alloc=b->helper_alloc;
    /* +8192 is a prospective floor for the phase records, not a measured upper. */
    if(plus(&b->reserved_reads,4*INPUT_CAP+2*(b->outmax+b->errmax)+2*sizeof(RootBootstrapFrame))||
       plus(&b->reserved_output,2*sizeof(RootBootstrapFrame)+3*sizeof(RootBootstrapAck)+sizeof(b->pidtext))||
       plus(&b->reserved_alloc,2*sizeof(RootBootstrapParentBank)+sizeof(RootBootstrapParentEnd)+4*(b->outmax+b->errmax)+3*sizeof(RootBootstrapFrame)+
            sizeof(b->fd)+4096*1024+4*INPUT_CAP+8192)) {
        boot_error(b,"bootstrap_reservation_overflow",0);goto settled;
    }
    if(FridayPublisherMasterChange(&s->pool,"native-reserve",0,b->reserved_reads,
       b->reserved_output,0,b->reserved_alloc,16,&b->token)<0) {
        boot_error(b,"bootstrap_original_reservation",errno);goto settled;
    }
    if(boot_zero_buffer(&b->stdout_raw,(Py_ssize_t)b->outmax)<0) {
        boot_error(b,"bootstrap_stdout_buffer_birth",errno);goto settled;
    }
    s->bootstrap_stdout_buffer=b->stdout_raw;
    if(boot_zero_buffer(&b->stderr_raw,(Py_ssize_t)b->errmax)<0) {
        boot_error(b,"bootstrap_stderr_buffer_birth",errno);goto settled;
    }
    s->bootstrap_stderr_buffer=b->stderr_raw;
    if(boot_zero_buffer(&b->wire_raw[0],sizeof(RootBootstrapFrame))<0||
       boot_zero_buffer(&b->wire_raw[1],sizeof(RootBootstrapFrame))<0||
       boot_zero_buffer(&b->final_parent_raw,sizeof(RootBootstrapParentBank))<0||
       boot_zero_buffer(&b->parent_end_raw,sizeof(RootBootstrapParentEnd))<0) {
        boot_error(b,"bootstrap_preowned_buffer_birth",errno);goto settled;
    }
    /* Native fixed fd[] is the preowned partial bank; exact used-width bytes
     * are copied only for publication, not all unused history capacity. */
    for(unsigned i=0;i<2;i++) {
        if(boot_zero_buffer(&b->parent_raw[i],sizeof(RootBootstrapCensus))<0||
           boot_zero_buffer(&b->overflow_raw[i],sizeof(b->overflow[i]))<0) {
            boot_error(b,"bootstrap_census_overflow_buffer_birth",errno);goto settled;
        }
    }
    b->before=boot_cgroup(b);
    uint64_t current,peak,pids,memorymax,pidsmax;
    if(!b->before||scalar_u64(field(b->before,"memory_current"),&current)||
       scalar_u64(field(b->before,"memory_peak"),&peak)||scalar_u64(field(b->before,"pids_current"),&pids)||
       scalar_u64(field(b->before,"memory_max"),&memorymax)||scalar_u64(field(b->before,"pids_max"),&pidsmax)||
       current||peak||pids||memorymax!=RAM_CAP||pidsmax!=3) {
        boot_error(b,"bootstrap_initial_cgroup",errno);goto settled;
    }
    for(unsigned i=0;i<8;i+=2)if(boot_pipe(b,i)<0) {
        boot_error(b,"bootstrap_pipe_acquisition",errno);goto settled;
    }
    if(boot_census(b,&b->parent_before)<0) {
        boot_error(b,"bootstrap_original_parent_FD_census",errno);goto settled;
    }
    b->parent_census_complete=1;
    snprintf(b->keyarg,sizeof(b->keyarg),"/proc/self/fd/%d",s->key_file->fd);
    snprintf(b->sigarg,sizeof(b->sigarg),"/proc/self/fd/%d",s->signature_file->fd);
    snprintf(b->admarg,sizeof(b->admarg),"/proc/self/fd/%d",s->admission_file->fd);
    const char *toolname=PyUnicode_AsUTF8(field(s->tool_file->pin,"path"));
    if(!toolname){boot_error(b,"bootstrap_tool_argv",errno);goto settled;}
    char *argv[]={(char *)toolname,"dgst","-sha256","-verify",b->keyarg,"-signature",b->sigarg,b->admarg,NULL};
    memcpy(b->argv,argv,sizeof(argv));b->env[0]="LC_ALL=C";b->env[1]="LANG=C";b->env[2]=NULL;
    if(boot_bank_acquire(b)<0){boot_error(b,"bootstrap_parent_bank_birth",errno);goto settled;}
    /* Atomic kernel child+pidfd birth in the SAME existing native topology.
     * No pidfd_open-by-numeric-PID race, no new role, no observer chain.
     * Bare-clone/CPython/libc/TLS/at-fork image qualification is NOT_RUN and
     * must replace, not inherit, any previous fork qualification. */
    b->pidfd_index=boot_new_fd(b);
    if(b->pidfd_index<0){boot_error(b,"bootstrap_atomic_child_pidfd_preowner",0);goto settled;}
    b->child_fd_base=b->fd_count;
    b->spawn_pidfd=-1;memset(&b->spawn_args,0,sizeof(b->spawn_args));
    b->spawn_args.flags=CLONE_PIDFD;
    b->spawn_args.pidfd=(uint64_t)(uintptr_t)&b->spawn_pidfd;
    b->spawn_args.exit_signal=SIGCHLD;b->spawn_entered=1;
#ifdef SYS_clone3
    b->spawn_rc=syscall(SYS_clone3,&b->spawn_args,sizeof(b->spawn_args));
#else
    b->spawn_rc=-1;errno=ENOSYS;
#endif
    b->spawn_errno=b->spawn_rc<0?errno:0;b->spawn_returned=1;
    b->pid=(pid_t)b->spawn_rc;s->bootstrap_pid=b->pid;
    if(b->pid<0){boot_error(b,"bootstrap_atomic_child_pidfd_birth",b->spawn_errno);goto settled;}
    if(!b->pid)boot_child(b);
    b->forked=1;if(s->pool.observed_workers<1)s->pool.observed_workers=1;
    int pfd=b->spawn_pidfd;
    s->bootstrap_pidfd=pfd;
    b->spawn_pidfd_bound=pfd>=0; /* binding from actual SAME clone3 return */
    if(boot_birth(b,b->pidfd_index,pfd)<0){boot_error(b,"bootstrap_pidfd",errno);goto settled;}
    const int parent_close[]={1,3,4,7};
    for(unsigned i=0;i<4;i++)if(boot_close(b,b->pipe_index[parent_close[i]])<0) {
        boot_error(b,"bootstrap_parent_early_pipe_close",errno);goto settled;
    }
    b->identity=boot_process_identity(b);
    if(!b->identity){boot_error(b,"bootstrap_exact_fork_identity",errno);goto settled;}
    b->identity_qualified=b->spawn_pidfd_bound;
    const char *cg=PyUnicode_AsUTF8(field(e,"cgroup"));if(!cg){boot_error(b,"bootstrap_cgroup_path",0);goto settled;}
    int pn=snprintf(b->pidtext,sizeof(b->pidtext),"%ld",(long)b->pid);
    int pathn=snprintf(b->cgroup_path,sizeof(b->cgroup_path),"%s/cgroup.procs",cg);
    if(pn<0||pn>=(int)sizeof(b->pidtext)||pathn<0||pathn>4096) {
        boot_error(b,"bootstrap_cgroup_path_width",0);goto settled;
    }
    b->cgroupfd_index=boot_open_absolute(b,b->cgroup_path,O_WRONLY);
    if(b->cgroupfd_index<0){boot_error(b,"bootstrap_cgroup_fd",errno);goto settled;}
    ssize_t written=write(b->fd[b->cgroupfd_index].fd,b->pidtext,pn);
    int saved=written<0?errno:0;
    if(written>0)b->actual_native_writes+=written;
    if(written!=pn){boot_error(b,"bootstrap_actual_cgroup_write",saved);goto settled;}
    if(boot_close(b,b->cgroupfd_index)<0){boot_error(b,"bootstrap_cgroup_close",errno);goto settled;}
    memset(&b->initial_gate,0,sizeof(b->initial_gate));
    b->initial_gate.schema=101;b->initial_gate.bytes=sizeof(b->initial_gate);
    b->initial_gate.pid=b->pid;b->initial_gate.parent_pid=b->parent_pid;b->initial_gate.accepted=2;
    if(boot_charge(b,0,sizeof(b->initial_gate))<0){boot_error(b,"bootstrap_initial_gate_budget",0);goto settled;}
    ssize_t gate_written=write(s->bootstrap_pipes[5],&b->initial_gate,sizeof(b->initial_gate));
    if(gate_written>0)b->actual_native_writes+=gate_written;
    if(gate_written!=(ssize_t)sizeof(b->initial_gate)){boot_error(b,"bootstrap_initial_gate",errno);goto settled;}
    b->initial_gate_written=1;
    if(boot_pump(b,b->work_deadline,1)<0||!b->child_ack_written[0]||
       b->received[0].phase!=1){boot_error(b,"bootstrap_prepared_frame_full_acceptance",errno);goto settled;}
settled:
    b->settling=1;boot_settle_child(b);
    boot_parent_fold(b); /* real existing receiver BEFORE Source/publication */
    s->bootstrap_waited=b->waited;s->bootstrap_status=b->status;s->bootstrap_usage=b->usage;
    if(b->token) {
        b->after=boot_cgroup(b);
        if(!b->after)boot_error(b,"bootstrap_after_cgroup_UNKNOWN",errno);
    }
    boot_close_all(b);
    if(b->token&&boot_census(b,&b->parent_after)<0)boot_error(b,"bootstrap_final_parent_census",errno);
    /* Final census itself is tracked and closed, and may change all_fd_ends. */
    if(b->active_fds)boot_close_all(b); /* newly born final census only; never retry old uncertain closes */
    if(b->active_fds)b->all_fd_ends=0;
    if(b->last_io) {
        PyObject *counters=field(b->last_io,"counters");uint64_t rr=0,rb=0,ww=0,wb=0;
        if(!scalar_u64(field(counters,"rchar"),&rr)&&!scalar_u64(field(counters,"read_bytes"),&rb)&&
           !scalar_u64(field(counters,"wchar"),&ww)&&!scalar_u64(field(counters,"write_bytes"),&wb)) {
            b->child_actual_reads=rr>rb?rr:rb;b->child_actual_writes=ww>wb?ww:wb;b->child_IO_known=1;
        } else boot_error(b,"bootstrap_actual_child_IO_schema",errno);
    }
    if(b->after) {
        uint64_t peak=0,current=0,pids=0;
        if(!scalar_u64(field(b->after,"memory_peak"),&peak)&&!scalar_u64(field(b->after,"memory_current"),&current)&&
           !scalar_u64(field(b->after,"pids_current"),&pids)) {
            if(peak>s->pool.observed_ram)s->pool.observed_ram=peak;
            if(current||pids)boot_error(b,"bootstrap_actual_cgroup_end_UNCONFIRMED",0);
        } else boot_error(b,"bootstrap_after_cgroup_scalar",errno);
    }
    uint64_t rss=(uint64_t)b->usage.ru_maxrss*1024ULL;
    if(rss>s->pool.observed_ram)s->pool.observed_ram=rss;
    /* Actual spent is separate from the declaration. Unknown child IO keeps
     * the pending component; it is never replaced by zero or a fresh pool. */
    FridayPublisherPoolRow *row=b->token?pool_row(&s->pool,b->token):NULL;
    if(row) {
        uint64_t actual_r=b->actual_native_reads,actual_o=b->actual_native_writes;
        if(plus(&actual_r,b->child_actual_reads)||plus(&actual_o,b->child_actual_writes)||
           actual_r>row->reads||actual_o>row->output)boot_error(b,"bootstrap_actual_aggregate_exceeds_reservation",0);
        else {
            uint64_t spent_r=s->pool.spent_read,spent_o=s->pool.spent_output;
            uint64_t retained=s->pool.retained_allocation;
            FridayPublisherPoolRow next;memcpy(&next,row,sizeof(next));
            next.reads-=actual_r;next.output-=actual_o;
            int valid=!(plus(&spent_r,actual_r)||plus(&spent_o,actual_o));
            if(!valid)boot_error(b,"bootstrap_actual_debit_overflow",0);
            if(valid&&b->waited&&b->all_fd_ends&&b->child_IO_known) {
                if(plus(&retained,row->allocation)) {
                    valid=0;boot_error(b,"bootstrap_retained_allocation_overflow",0);
                } else {
                    next.reads=next.output=next.hash=next.allocation=next.slots=0;
                    next.active=0;next.transferred=1;
                }
            }
            if(valid) {
                if(pool_apply(&s->pool,row,&next,NULL,NULL,0)<0)
                    boot_error(b,"bootstrap_original_pending_settlement_relation",0);
                else {
                    s->pool.spent_read=spent_r;s->pool.spent_output=spent_o;
                    s->pool.retained_allocation=retained;
                }
            }
        }
    }
    /* Admission positive is the parent retirement account plus a zero
     * stock status. Status alone cannot pass, and it cannot set the
     * returned-native transport flag. */
    boot_account_public_stock(b);
    int stock_exit0=b->waited&&b->reap_returned&&b->reap_rc==0&&
        b->reap_info.si_code==CLD_EXITED&&b->reap_info.si_status==0&&
        b->reap_info.si_pid==b->pid;
    int positive=b->public_stock_parent_legal_retirement_accounted&&
        b->lifetime_phase==2&&!b->child_transport_end_confirmed&&
        b->stock_image_replacement_confirmed&&stock_exit0&&b->forked&&b->waited&&
        b->last_io&&b->all_fd_ends&&b->stdout_eof&&b->stderr_eof&&b->owner_eof&&
        !b->owner_used[1]&&b->child_ack_written[0]&&b->child_IO_known&&
        b->spawn_pidfd_bound;
    if(!b->failed&&!positive)
        boot_error(b,"bootstrap_signature_or_complete_parent_end_FAILED",0);
    b->signature_ok=!b->failed;b->acknowledged=b->child_ack_written[0]||b->child_ack_written[1];
    boot_parent_end_before_publication(b);
    if(boot_publish(b)<0)boot_error(b,"bootstrap_full_outcome_publication",errno);
    if(b->failed) {
        if(PyErr_Occurred())PyErr_Fetch(&b->return_pending.type,&b->return_pending.value,&b->return_pending.tb);
        PyErr_Restore(Py_XNewRef(b->first.type),Py_XNewRef(b->first.value),Py_XNewRef(b->first.tb));
        return -1;
    }
    s->signed_admission=1;return 0; /* signature only; NOT full native/Root end */
}

static void boot_read_existing_parent(FridayPublisherRootTerminal *t) {
    RootBootstrapState *b=&root_storage.bootstrap;
    /* Read the whole immutable native body and EVERY acquired original parent
     * FD history row. Failure to allocate compatibility dict does not erase
     * this preowned owner. No Python/Source decoder or new fallible factory. */
    uint64_t used=0;unsigned char sink=0;
    if(b->final_parent_raw&&b->bank_full_read) {
        const volatile unsigned char *raw=(const volatile unsigned char *)PyBytes_AS_STRING(b->final_parent_raw);
        size_t width=(size_t)PyBytes_GET_SIZE(b->final_parent_raw);
        for(size_t i=0;i<width;i++){sink^=raw[i];used++;}
        t->bootstrap_parent_full_native=Py_NewRef(b->final_parent_raw);
        t->bootstrap_parent_native_read=width==sizeof(RootBootstrapParentBank)&&b->bank_stable;
    }
    const volatile unsigned char *history=(const volatile unsigned char *)b->fd;
    size_t width=(size_t)b->fd_count*sizeof(*b->fd);
    for(size_t i=0;i<width;i++){sink^=history[i];used++;}
    const volatile unsigned char *end=(const volatile unsigned char *)&b->parent_end;
    for(size_t i=0;i<sizeof(b->parent_end);i++){sink^=end[i];used++;}
    if(b->parent_end_raw)t->bootstrap_parent_end_native=Py_NewRef(b->parent_end_raw);
    /* Full same original Python exception objects remain in b->first/
     * secondary/return_pending. Retaining them is NOT deep immutable transfer
     * or an error codec; terminal rejoin must include each exact alias. */
    t->bootstrap_parent_native_bytes_read=used;t->raw_reader_sink^=sink;
    t->bootstrap_native_storage=b;
    t->bootstrap_native_failure_transport_read=b->native_failure_transport_complete;
    t->bootstrap_stock_exec_end_unknown=b->stock_exec_end_unknown;
    t->bootstrap_parent_mapping_retained=b->map_acquired;
    t->bootstrap_lifetime_phase=b->lifetime_phase;
    t->bootstrap_public_stock_observables_consumed=b->public_stock_observables_consumed;
    t->bootstrap_public_stock_parent_legal_retirement_accounted=b->public_stock_parent_legal_retirement_accounted;
    t->bootstrap_child_transport_end_confirmed=b->child_transport_end_confirmed;
    t->bootstrap_stock_exec_prefix_retained=b->stock_exec_prefix_retained;
    t->bootstrap_kernel_process_lifetime_ended=b->kernel_process_lifetime_ended;
    t->bootstrap_stock_image_replacement_confirmed=b->stock_image_replacement_confirmed;
}
PyObject *FridayPublisherPreparedState(FridayPublisherMasterPool *p) {
    if(p!=&root_storage.pool)return NULL;
    if(p->pid==getpid()&&FridayPublisherMasterBefore(p,
       root_storage.prepared_count*sizeof(FridayPublisherPreparedRow),0,0,
       root_storage.prepared_count*1024+131072)<0)return NULL;
    PyObject *rows=PyList_New(root_storage.prepared_count);if(!rows)return NULL;
    for(uint64_t i=0;i<root_storage.prepared_count;i++) {
        FridayPublisherPreparedRow *r=&root_storage.prepared[i];
        PyObject *nine=stat9(&r->birth);
        PyObject *row=nine?Py_BuildValue("{s:O,s:O,s:K,s:K,s:i,s:i,s:i,s:i,s:i,s:i,s:i,s:O}",
            "actual_row",r->row,"actual_credit",r->credit,"token",r->token,"generation",r->generation,
            "fd",r->fd,"keeper_fd",r->keeper,"opened",r->opened,"keeper_attempted",r->keeper_attempted,
            "keeper_closed",r->keeper_closed,"keeper_rc",r->keeper_rc,"keeper_errno",r->keeper_errno,
            "actual_birth9",nine):NULL;
        Py_XDECREF(nine);if(!row){Py_DECREF(rows);return NULL;}PyList_SET_ITEM(rows,i,row);
    }return rows;
}
PyObject *FridayPublisherMasterState(FridayPublisherMasterPool *p) {
    /* Fork inherited copy is read-only and accounted by the original child
     * envelope. No parent pool callback/debit is made from that shadow. */
    if(p!=&root_storage.pool)return NULL;
    if(p->pid==getpid()&&FridayPublisherMasterBefore(p,
       p->count*sizeof(FridayPublisherPoolRow)+root_storage.held_count*sizeof(RootHeldFile),
       0,0,p->count*1024+131072)<0)return NULL;
    PyObject *rows=PyList_New(p->count),*files=PyList_New(root_storage.held_count);
    if(!rows||!files){Py_XDECREF(rows);Py_XDECREF(files);return NULL;}
    for(uint64_t i=0;i<p->count;i++) {
        FridayPublisherPoolRow *r=&p->rows[i];
        PyObject *v=Py_BuildValue("{s:K,s:K,s:K,s:K,s:K,s:K,s:i,s:i,s:i}",
            "token",r->token,"reads",r->reads,"output",r->output,"hash_bytes",r->hash,
            "allocation",r->allocation,"slots",r->slots,"active",r->active,
            "retained_transfer",r->transferred,"source_owned",r->source_owned);
        if(!v){Py_DECREF(rows);Py_DECREF(files);return NULL;}PyList_SET_ITEM(rows,i,v);
    }
    for(uint64_t i=0;i<root_storage.held_count;i++) {
        RootHeldFile *h=&root_storage.held[i];
        PyObject *v=Py_BuildValue("{s:O,s:O,s:i,s:i,s:i,s:i,s:i,s:K,s:i,s:i,s:K,s:i,s:i}",
            "actual_pin",h->pin?h->pin:Py_None,"full_raw",h->raw?h->raw:Py_None,
            "fd",h->fd,"closed",h->closed,"close_attempted",h->close_attempted,
            "close_rc",h->close_rc,"close_errno",h->close_errno,
            "read_used",h->read_used,"read_completed",h->read_completed,"read_error",h->read_error,
            "generation",h->generation,"keeper_fd",h->keeper,"birth_valid",h->body_close.birth_valid);
        if(!v){Py_DECREF(rows);Py_DECREF(files);return NULL;}PyList_SET_ITEM(files,i,v);
    }
    PyObject *state=Py_BuildValue("{s:s,s:l,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:i,s:i,s:i,s:s,s:O,s:O,s:O,s:O,s:O,s:O}",
        "schema","friday.sol100.original-persistent-master-full-cut.v1","owner_pid",(long)p->pid,
        "started_ns",p->started_ns,"deadline_ns",p->deadline_ns,"work_deadline_ns",p->work_deadline_ns,
        "spent_read",p->spent_read,"spent_output",p->spent_output,"spent_hash",p->spent_hash,
        "native_allocation",p->native_allocation,"retained_allocation",p->retained_allocation,
        "observed_read",p->observed_read,"observed_output",p->observed_output,
        "observed_ram",p->observed_ram,"observed_workers",p->observed_workers,"native_live_slots",p->native_live_slots,
        "source_detached",p->source_detached,"observation_unknown",p->observation_unknown,"refused",p->refused,
        "first_fault",p->fault?p->fault:"NONE","all_original_rows",rows,"full_held_original_files",files,
        "actual_enrollment",root_storage.enrollment?root_storage.enrollment:Py_None,
        "actual_admission",root_storage.admission?root_storage.admission:Py_None,
        "actual_role_schema",root_storage.role_schema?root_storage.role_schema:Py_None,
        "actual_bootstrap",root_storage.bootstrap_record?root_storage.bootstrap_record:Py_None);
    Py_DECREF(rows);Py_DECREF(files);return state;
}
PyObject *FridayPublisherRootBootstrapOutcome(FridayPublisherMasterPool *p) {
    RootBootstrapState *b=&root_storage.bootstrap;
    if(!FridayPublisherMasterOwns(p)||!b->attempted||!b->published||!b->compat_record) {
        FridayPublisherMasterFault(p,"bootstrap_current_outcome_NOT_PUBLISHED");return NULL;
    }
    /* BOTH outcomes, not signature authority. Full native frame/body banks
     * and actual original error objects are required by the Root integration. */
    return Py_NewRef(b->compat_record);
}
PyObject *FridayPublisherRootBootstrapSignature(FridayPublisherMasterPool *p,
    PyObject *admission,PyObject *signature,PyObject *key) {
    FridayPublisherRootStorage *s=&root_storage;RootBootstrapState *b=&s->bootstrap;
    int stock_exit0=b->waited&&b->reap_returned&&b->reap_rc==0&&
        b->reap_info.si_code==CLD_EXITED&&b->reap_info.si_status==0&&
        b->reap_info.si_pid==b->pid;
    if(!FridayPublisherMasterOwns(p)||!s->signed_admission||!b->signature_ok||
       !b->published||!b->endpoint_immutable||!b->all_fd_ends||!b->child_IO_known||
       !b->public_stock_parent_legal_retirement_accounted||b->lifetime_phase!=2||
       b->child_transport_end_confirmed||!b->stock_image_replacement_confirmed||
       !b->public_stock_observables_consumed||!b->stock_exec_prefix_retained||
       !b->kernel_process_lifetime_ended||!b->spawn_pidfd_bound||!stock_exit0||
       !b->bank_full_read||!b->bank_stable||
       !b->child_frame_accepted[0]||!b->child_ack_written[0]||
       !s->bootstrap_record||!s->bootstrap_waited||!s->admission_file||!s->signature_file||!s->key_file||
       !eq(admission,s->admission_file->pin)||!eq(signature,s->signature_file->pin)||
       !eq(key,s->key_file->pin)||!WIFEXITED(s->bootstrap_status)||WEXITSTATUS(s->bootstrap_status)) {
        FridayPublisherMasterFault(p,"actual_bootstrap_signature_current_full_correspondence");return NULL;
    }
    /* Returned record is the parent public-stock retirement account.
     * It is not a post-exec native return and not profile qualification. */
    return Py_NewRef(s->bootstrap_record);
}

/* Full exact retained byte loader; no import from a pathname after a hash
 * check and no Source before independent signature/current snapshot. The
 * stock importlib ModuleSpec is a dependency, not a Source authority issuer. */
typedef struct {PyObject_HEAD} RootByteLoader;
static PyTypeObject root_loader_type;
static RootHeldFile *source_module_bytes(const char *name) {
    if(!name||strchr(name,'.'))return NULL;
    char relative[256];if(snprintf(relative,sizeof(relative),"source/%s.py",name)>255)return NULL;
    return held_by_pin(pin_relative("source_files",relative));
}
/* Exact paired own-value producers. Foreign private heaps are not selected.
 * Independent Source/compiler/image/implicit-cost qualification NOT_RUN. */
static int own_before(uint64_t n) {
    FridayPublisherRootStorage *s=&root_storage;
    if(!s->signed_admission||!s->snapshot_verified||s->command_receipt.attempted||!FridayPublisherMasterOwns(&s->pool))
        return FridayPublisherMasterFault(&s->pool,"own_value_same_original_Root_required");
    return FridayPublisherMasterBefore(&s->pool,0,0,0,n);
}
static FridayPublisherOwnValue *own_birth(uint64_t kind) {
    FridayPublisherOwnValues *v=&root_storage.own_values;
    if(v->count>=FRIDAY_ROOT_COMMAND_NODES||own_before(131072)<0)return NULL;
    FridayPublisherOwnValue *p=&v->rows[v->count++];
    p->kind=kind;p->serial=v->count;p->pid=getpid();return p;
}
static void own_ref(FridayPublisherOwnValue *p,unsigned i,PyObject *o) {
    if(o&&!p->refs[i])p->refs[i]=Py_NewRef(o); /* one write; no replacement */
}
static int own_error(FridayPublisherOwnValue *p) {
    if(PyErr_Occurred()&&!p->error_saved) {
        p->error_saved=1;PyErr_Fetch(&p->error_type,&p->error_value,&p->error_tb);
        PyErr_Restore(Py_XNewRef(p->error_type),Py_XNewRef(p->error_value),Py_XNewRef(p->error_tb));
    }
    return -1; /* exact originals, no normalization or diagnostic factory */
}
static FridayPublisherOwnValue *own_find(PyObject *o,uint64_t kind) {
    FridayPublisherOwnValues *v=&root_storage.own_values;
    for(uint64_t i=0;i<v->count;i++) {
        if(v->scan_steps==UINT64_MAX)
            return FridayPublisherMasterFault(&root_storage.pool,"own_scan_step_overflow"),NULL;
        v->scan_steps++;
        if((i&1023)==0&&(!now_ns()||now_ns()>root_storage.pool.deadline_ns))return NULL;
        FridayPublisherOwnValue *p=&v->rows[i];
        if(p->pid==getpid()&&p->kind==kind&&p->refs[0]==o)return p;
    }
    return NULL;
}
/* Nonallocating lookup/comparison for an already pending ORIGINAL error.
 * No PyDict_GetItemString temporary key or unicode factory on this path. */
static int own_text_equal(PyObject *a,PyObject *b) {
    if(!a||!b||!PyUnicode_CheckExact(a)||!PyUnicode_CheckExact(b))return 0;
    Py_ssize_t n=PyUnicode_GET_LENGTH(a);
    if(n!=PyUnicode_GET_LENGTH(b))return 0;
    int ak=PyUnicode_KIND(a),bk=PyUnicode_KIND(b);
    void *ad=PyUnicode_DATA(a),*bd=PyUnicode_DATA(b);
    for(Py_ssize_t i=0;i<n;i++)
        if(PyUnicode_READ(ak,ad,i)!=PyUnicode_READ(bk,bd,i))return 0;
    return 1;
}
static PyObject *own_plain_field(PyObject *d,const char *name) {
    if(!d||!PyDict_CheckExact(d))return NULL;
    PyObject *key,*value;Py_ssize_t pos=0;size_t n=strlen(name);
    while(PyDict_Next(d,&pos,&key,&value)) {
        if(!PyUnicode_CheckExact(key)||(size_t)PyUnicode_GET_LENGTH(key)!=n)continue;
        int kind=PyUnicode_KIND(key);void *data=PyUnicode_DATA(key);size_t i=0;
        for(;i<n&&PyUnicode_READ(kind,data,(Py_ssize_t)i)==(unsigned char)name[i];i++);
        if(i==n)return value;
    }
    return NULL;
}
static int secondary_debit_scan(uint64_t,uint64_t);
static PyObject *own_class_factory(PyObject *,PyObject *,PyObject *);
static FridayPublisherOwnValue *own_class_binding(PyObject *module,unsigned long long serial) {
    FridayPublisherOwnValues *v=&root_storage.own_values;
    if(secondary_debit_scan(v->count,0)<0)return NULL;
    for(uint64_t i=0;i<v->count;i++) {
        FridayPublisherOwnValue *p=&v->rows[i];
        if(p->pid==getpid()&&p->kind==OWN_BINDING&&p->serial==serial&&p->refs[0]==module)
            return p;
    }
    return NULL;
}
static int own_class_success(const FridayPublisherOwnValue *p) {
    return p&&p->kind==OWN_CLASS&&p->flags==CLASS_RETURN_LOCAL&&
        p->attempts==1&&p->confirmed==1&&!p->error_saved;
}
static FridayPublisherOwnValue *own_class_find(PyObject *o) {
    FridayPublisherOwnValues *v=&root_storage.own_values;
    if(secondary_debit_scan(v->count,0)<0)return NULL;
    for(uint64_t i=0;i<v->count;i++) {
        if((i&1023)==0&&(!now_ns()||now_ns()>root_storage.pool.deadline_ns))
            return FridayPublisherMasterFault(&root_storage.pool,"class_result_registry_clock"),NULL;
        FridayPublisherOwnValue *p=&v->rows[i];
        if(p->pid==getpid()&&own_class_success(p)&&p->refs[0]==o)return p;
    }
    return NULL;
}
static int own_row_required(const FridayPublisherOwnValue *p,unsigned j) {
    if(j>=12)return j!=12;
    /* Only an actual linked error producer qualifies this exact stock TYPE.
     * Unknown/Source-defined TYPE remains required full DATA. Stored flags
     * are numeric and remain usable AFTER the borrowed views are invalidated. */
    if((p->kind==OWN_ERROR_RECORD||p->kind==OWN_ERROR_CELL)&&j==2&&p->flags==1)return 0;
    if((p->kind==OWN_HASH&&(j==0||j==2||j==6))||
       (p->kind==OWN_MAPPING&&(j==10||j==11))||(p->kind==OWN_SUPPORT&&j==0))return 0;
    /* Actual runtime creators/wrapper/builtins are SUPPORT aliases, never
     * relabelled own DATA. Arguments, code, local result and original errors
     * remain required; their typed edges distinguish real type operands. */
    if(p->kind==OWN_CLASS&&(j==6||j==11||(j==0&&p->flags==CLASS_RETURN_FOREIGN)))return 0;
    if(p->kind==OWN_CLASS_SCOPE&&(j>=2&&j<=7))return 0;
    if(p->kind==OWN_CLASS_RESTORE&&(j==3||j==4||j==5||j==6||
       (j==1&&(p->flags&CLASS_RESTORE_OP_MASK)==1)))return 0;
    return 1;
}
static int own_class_container(PyObject *o) {
    FridayPublisherOwnValues *v=&root_storage.own_values;
    if(secondary_debit_scan(v->count,0)<0)return -1;
    for(uint64_t i=0;i<v->count;i++) {
        if((i&1023)==0&&(!now_ns()||now_ns()>root_storage.pool.deadline_ns))
            return FridayPublisherMasterFault(&root_storage.pool,"class_operands_registry_clock");
        FridayPublisherOwnValue *p=&v->rows[i];
        if(p->kind!=OWN_CLASS||p->pid!=getpid()||!p->attempts)continue;
        if(p->refs[7]==o)return 1;
        if(p->refs[8]==o&&o!=Py_None)return 2;
    }
    return 0;
}
static int command_builtin_exception(PyTypeObject *);
static const char *const error_cell_names[10]={"phase","original_error","original_type",
    "original_traceback","actual_document","saved","syscall_attempted","syscall_rc",
    "syscall_errno","actual_written"};
static const unsigned error_cell_refs[10]={0,1,2,3,6,7,8,9,10,11};
static int own_plain_name(PyObject *o,const char *s) {
    if(!o||!PyUnicode_CheckExact(o))return 0;
    size_t n=strlen(s);if((size_t)PyUnicode_GET_LENGTH(o)!=n)return 0;
    int k=PyUnicode_KIND(o);void *d=PyUnicode_DATA(o);
    for(size_t j=0;j<n;j++)if(PyUnicode_READ(k,d,(Py_ssize_t)j)!=(unsigned char)s[j])return 0;
    return 1;
}
static int own_error_cell_valid(const FridayPublisherOwnValue *p,PyObject *cell) {
    if(!p||p->kind!=OWN_ERROR_CELL||p->pid!=getpid()||p->attempts!=1||
       p->confirmed!=1||p->width<1||p->width>2||p->flags>2||p->refs[4]!=cell||
       !cell||!PyDict_CheckExact(cell)||PyDict_GET_SIZE(cell)!=10)return 0;
    for(unsigned i=0;i<10;i++)if(!p->refs[error_cell_refs[i]]||
        own_plain_field(cell,error_cell_names[i])!=p->refs[error_cell_refs[i]])return 0;
    if(!own_plain_name(p->refs[0],p->cell_input.phase?p->cell_input.phase:"NOT_ATTEMPTED")||
       (p->refs[3]!=Py_None&&Py_TYPE(p->refs[3])!=&PyTraceBack_Type))return 0;
    if(p->flags==0) {
        if(p->refs[1]!=Py_None||p->refs[2]!=Py_None||p->refs[3]!=Py_None)return 0;
    } else if(!PyExceptionInstance_Check(p->refs[1])||
        p->refs[2]!=(PyObject *)Py_TYPE(p->refs[1])||
        p->flags!=(uint64_t)(command_builtin_exception(Py_TYPE(p->refs[1]))?1:2))return 0;
    /* Conversion checks apply only to exact new builtin scalar results.
     * No Source conversion, equality, getter or error normalization. */
    for(unsigned j=7;j<12;j++)if(!PyLong_CheckExact(p->refs[j]))return 0;
    return PyLong_AsLong(p->refs[7])==p->cell_input.saved&&
        PyLong_AsLong(p->refs[8])==p->cell_input.syscall_attempted&&
        PyLong_AsLong(p->refs[9])==p->cell_input.syscall_rc&&
        PyLong_AsLong(p->refs[10])==p->cell_input.syscall_errno&&
        PyLong_AsUnsignedLongLong(p->refs[11])==p->cell_input.actual_written&&!PyErr_Occurred();
}
/* Pure linked identity plus original scalar state; no Source registration.
 * Every attempted read pays its original scan before looking at native rows.
 * A blocked caller leaves a later occupied indicator untouched. Otherwise
 * it restores the already retained first full triple, WITHOUT normalization,
 * replacement, a new error factory or another dictionary birth. */
int FridayPublisherRootErrorCellReady(FridayPublisherOwnedRun *r,FridayPublisherErrorCell *cell) {
    const FridayPublisherOwnedRun *actual=NULL;
    if(PyErr_Occurred())return -1; /* already occupied later indicator is untouched */
    if(secondary_debit_scan(64,0)<0||!r||!cell||!r->bindings||
       r->bindings->original_master_pool!=&root_storage.pool||
       FridayPublisherCallerContextFull(r->capsule,&actual)!=1||actual!=r||
       (cell!=&r->prefix_error&&cell!=&r->after_document_error)||PyErr_Occurred())return -1;
    FridayPublisherOwnValues *v=&root_storage.own_values;
    unsigned at=cell==&r->prefix_error?0:1;
    if(v->retired||cell->cut_serial!=v->cell_last[at]||cell->cut_state!=v->cell_state[at]||
       cell->cut_state>FRIDAY_CELL_CUT_FAILED||cell->cut_serial>v->count)
        return FridayPublisherMasterFault(&root_storage.pool,"actual_original_cell_guard_relation");
    if(!cell->cut_serial) {
        if(cell->cut_state!=FRIDAY_CELL_CUT_EMPTY)
            return FridayPublisherMasterFault(&root_storage.pool,"actual_original_cell_empty_guard");
        return 0;
    }
    FridayPublisherOwnValue *p=&v->rows[cell->cut_serial-1];
    if(p->kind!=OWN_ERROR_CELL||p->serial!=cell->cut_serial||p->pid!=r->owner_pid||
       p->width!=at+1||p->refs[5]!=r->capsule||p->cell_outcome!=cell->cut_state)
        return FridayPublisherMasterFault(&root_storage.pool,"actual_original_cell_guard_row");
    if(cell->cut_state==FRIDAY_CELL_CUT_CONFIRMED&&p->confirmed==1&&!p->error_saved)return 0;
    if(cell->cut_state==FRIDAY_CELL_CUT_FAILED&&p->error_saved) {
        if(!PyErr_Occurred())PyErr_Restore(Py_XNewRef(p->error_type),
            Py_XNewRef(p->error_value),Py_XNewRef(p->error_tb));
        return -1;
    }
    return FridayPublisherMasterFault(&root_storage.pool,"actual_original_cell_unsettled_cut");
}
static int own_error_cell_failed(FridayPublisherOwnValue *p,FridayPublisherErrorCell *cell) {
    /* Sticky at the original cell BEFORE indicator capture or wrapper DECREF.
     * The row owns any actual return; a later error never replaces its first. */
    p->confirmed=0;p->cell_outcome=FRIDAY_CELL_CUT_FAILED;
    cell->cut_state=FRIDAY_CELL_CUT_FAILED;
    root_storage.own_values.cell_state[p->width-1]=FRIDAY_CELL_CUT_FAILED;
    return own_error(p);
}
/* Only the actual linked custody producer can register this dictionary.
 * The guard precedes own_birth and every subsequent factory effect. */
uint64_t FridayPublisherRootErrorCellBegin(FridayPublisherOwnedRun *r,FridayPublisherErrorCell *cell) {
    if(FridayPublisherRootErrorCellReady(r,cell)<0||secondary_debit_scan(1024,0)<0)return 0;
    FridayPublisherOwnValue *p=own_birth(OWN_ERROR_CELL);if(!p)return 0;
    p->attempts=1;p->width=cell==&r->prefix_error?1:2;
    p->cell_previous=cell->cut_serial;p->cell_outcome=FRIDAY_CELL_CUT_ACTIVE;
    cell->cut_serial=p->serial;cell->cut_state=FRIDAY_CELL_CUT_ACTIVE;
    root_storage.own_values.cell_last[p->width-1]=p->serial;
    root_storage.own_values.cell_state[p->width-1]=FRIDAY_CELL_CUT_ACTIVE;
    p->cell_input=(FridayPublisherFailureScalars){cell->phase,cell->saved,
        cell->syscall_attempted,cell->syscall_rc,cell->syscall_errno,cell->actual_written};
    own_ref(p,1,cell->error_value?cell->error_value:Py_None);
    own_ref(p,2,cell->error_type?cell->error_type:Py_None);
    own_ref(p,3,cell->error_tb?cell->error_tb:Py_None);
    own_ref(p,5,r->capsule);own_ref(p,6,cell->actual_document?cell->actual_document:Py_None);
    if(!cell->error_type&&!cell->error_value&&!cell->error_tb)p->flags=0;
    else if(cell->error_value&&PyExceptionInstance_Check(cell->error_value)&&
        cell->error_type==(PyObject *)Py_TYPE(cell->error_value)&&
        (!cell->error_tb||Py_TYPE(cell->error_tb)==&PyTraceBack_Type))
        p->flags=command_builtin_exception(Py_TYPE(cell->error_value))?1:2;
    else {
        FridayPublisherMasterFault(&root_storage.pool,"actual_error_cell_original_triple");
        own_error_cell_failed(p,cell);return 0; /* all originals stay owned, no normalization */
    }
    return p->serial;
}
int FridayPublisherRootErrorCellFinish(FridayPublisherOwnedRun *r,FridayPublisherErrorCell *original,
    uint64_t serial,PyObject *cell) {
    if(!serial||serial>root_storage.own_values.count)return -1;
    FridayPublisherOwnValue *p=&root_storage.own_values.rows[serial-1];
    if(p->kind!=OWN_ERROR_CELL||p->pid!=getpid()||p->refs[4]||p->confirmed||!r||!original||
       (original!=&r->prefix_error&&original!=&r->after_document_error)||
       p->width!=(uint64_t)(original==&r->prefix_error?1:2)||p->refs[5]!=r->capsule||
       original->cut_serial!=serial||original->cut_state!=FRIDAY_CELL_CUT_ACTIVE||
       p->cell_outcome!=FRIDAY_CELL_CUT_ACTIVE||
       root_storage.own_values.cell_last[p->width-1]!=serial||
       root_storage.own_values.cell_state[p->width-1]!=FRIDAY_CELL_CUT_ACTIVE)return -1;
    if(!cell) {
        if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,
            "actual_error_cell_NULL_without_original_indicator");
        return own_error_cell_failed(p,original);
    }
    own_ref(p,4,cell); /* retain actual returned dictionary BEFORE any check */
    if(PyErr_Occurred())return own_error_cell_failed(p,original);
    if(PyDict_CheckExact(cell)&&PyDict_GET_SIZE(cell)==10) {
        own_ref(p,0,own_plain_field(cell,"phase"));
        for(unsigned j=5;j<10;j++)own_ref(p,error_cell_refs[j],own_plain_field(cell,error_cell_names[j]));
    }
    p->confirmed=1;
    if(!own_error_cell_valid(p,cell)) {
        p->confirmed=0;
        if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"actual_error_cell_return_relation");
        return own_error_cell_failed(p,original);
    }
    p->cell_outcome=FRIDAY_CELL_CUT_CONFIRMED;original->cut_state=FRIDAY_CELL_CUT_CONFIRMED;
    root_storage.own_values.cell_state[p->width-1]=FRIDAY_CELL_CUT_CONFIRMED;
    return 0;
}
static int own_error_record_valid(const FridayPublisherOwnValue *p,PyObject *record) {
    if(p&&p->kind==OWN_ERROR_CELL)return own_error_cell_valid(p,record);
    if(!p||p->kind!=OWN_ERROR_RECORD||p->pid!=getpid()||p->attempts!=1||
       p->confirmed!=1||(p->flags!=1&&p->flags!=2)||p->refs[4]!=record||
       !record||!PyTuple_CheckExact(record)||PyTuple_GET_SIZE(record)!=4||
       !p->refs[0]||!PyUnicode_CheckExact(p->refs[0])||!p->refs[1]||
       !PyExceptionInstance_Check(p->refs[1])||p->refs[2]!=(PyObject *)Py_TYPE(p->refs[1])||
       !p->refs[3]||(p->refs[3]!=Py_None&&Py_TYPE(p->refs[3])!=&PyTraceBack_Type))return 0;
    for(unsigned j=0;j<4;j++)if(PyTuple_GET_ITEM(record,j)!=p->refs[j])return 0;
    return p->flags==(uint64_t)(command_builtin_exception((PyTypeObject *)p->refs[2])?1:2);
}
/* This registration has NO Python-callable setter. The exact linked custody
 * producer, not a key/name/tuple shape, owns the original immutable relation. */
uint64_t FridayPublisherRootErrorRecordBegin(FridayPublisherOwnedRun *r) {
    const FridayPublisherOwnedRun *actual=NULL;
    /* One original prepaid block covers BEGIN + fixed RETURN/type checks +
     * first-error cell bookkeeping BEFORE the sole tuple/anchor effects. */
    if(secondary_debit_scan(256,0)<0)return 0;
    if(!r||!r->bindings||r->bindings->original_master_pool!=&root_storage.pool||
       FridayPublisherCallerContextFull(r->capsule,&actual)!=1||actual!=r||
       !r->source_error||!r->source_error_phase||PyErr_Occurred())return 0;
    FridayPublisherOwnValue *p=own_birth(OWN_ERROR_RECORD);if(!p)return 0;
    own_ref(p,0,r->source_error_phase);own_ref(p,1,r->source_error);
    own_ref(p,2,(PyObject *)Py_TYPE(r->source_error));
    own_ref(p,3,r->source_error_tb?r->source_error_tb:Py_None);own_ref(p,5,r->capsule);
    p->attempts=1;p->flags=command_builtin_exception(Py_TYPE(r->source_error))?1:2;
    return p->serial;
}
int FridayPublisherRootErrorRecordFailure(uint64_t serial) {
    if(!serial||serial>root_storage.own_values.count)return -1;
    FridayPublisherOwnValue *p=&root_storage.own_values.rows[serial-1];
    if((p->kind!=OWN_ERROR_RECORD&&p->kind!=OWN_ERROR_CELL)||p->pid!=getpid())return -1;
    return own_error(p); /* same preowned original triple; no normalization */
}
int FridayPublisherRootErrorRecordFinish(uint64_t serial,PyObject *record) {
    if(!serial||serial>root_storage.own_values.count)return -1;
    FridayPublisherOwnValue *p=&root_storage.own_values.rows[serial-1];
    if(p->kind!=OWN_ERROR_RECORD||p->pid!=getpid()||p->refs[4]||p->confirmed)return -1;
    if(!record)return own_error(p);
    own_ref(p,4,record); /* strong actual return BEFORE a fallible check/anchor */
    p->confirmed=1;
    if(PyErr_Occurred()||!own_error_record_valid(p,record)) {
        p->confirmed=0;
        if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"actual_error_record_return_relation");
        return own_error(p); /* full result retained, no replay */
    }
    return 0;
}
static int own_error_record_lookup(PyObject *record,FridayPublisherOwnValue **out) {
    *out=NULL;
    if(!record||!((PyTuple_CheckExact(record)&&PyTuple_GET_SIZE(record)==4)||
        (PyDict_CheckExact(record)&&PyDict_GET_SIZE(record)==10)))return 0;
    FridayPublisherOwnValues *v=&root_storage.own_values;
    if(v->retired||v->count>FRIDAY_ROOT_COMMAND_NODES||PyErr_Occurred()||
       !FridayPublisherMasterOwns(&root_storage.pool)||secondary_debit_scan(v->count+256,0)<0)return -1;
    for(uint64_t i=0;i<v->count;i++) {
        if((i&1023)==0&&(!now_ns()||now_ns()>root_storage.pool.deadline_ns))
            return FridayPublisherMasterFault(&root_storage.pool,"error_record_lookup_clock");
        FridayPublisherOwnValue *p=&v->rows[i];
        if((p->kind!=OWN_ERROR_RECORD&&p->kind!=OWN_ERROR_CELL)||p->refs[4]!=record)continue;
        if(p->kind==OWN_ERROR_CELL&&secondary_debit_scan(512,0)<0)return -1;
        if(!own_error_record_valid(p,record))
            return FridayPublisherMasterFault(&root_storage.pool,"actual_error_record_original_type_relation");
        *out=p;return 1;
    }
    return 0; /* arbitrary tuple ALWAYS keeps ordinary DATA semantics */
}
PyObject *FridayPublisherRootOwnErrorRecordSerial(PyObject *record) {
    FridayPublisherOwnValue *p=NULL;int found=own_error_record_lookup(record,&p);
    if(found<0)return NULL;if(!found)Py_RETURN_NONE;
    if(own_before(512)<0)return NULL; /* BEFORE the bounded serial scalar factory */
    return PyLong_FromUnsignedLongLong(p->serial);
}
static FridayPublisherOwnValue *own_error_record_arguments(PyObject *args) {
    unsigned long long serial,support=0;PyObject *record;
    if(!PyArg_ParseTuple(args,"KO|K",&serial,&record))return NULL;
    if(secondary_debit_scan(64,0)<0||root_storage.own_values.retired||
       !FridayPublisherMasterOwns(&root_storage.pool)||!serial||serial>root_storage.own_values.count)return NULL;
    FridayPublisherOwnValue *p=&root_storage.own_values.rows[serial-1];
    if(p->kind==OWN_ERROR_CELL&&secondary_debit_scan(512,0)<0)return NULL;
    if(!own_error_record_valid(p,record))return NULL;
    if(support) {
        if(support>root_storage.own_values.count)return NULL;
        const FridayPublisherOwnValue *q=&root_storage.own_values.rows[support-1];
        if(q->kind!=OWN_SUPPORT||q->pid!=getpid()||!q->confirmed||q->refs[0]!=p->refs[2])return NULL;
        /* The existing support reader separately revalidates its origin.
         * This check joins that SAME actual support TYPE to this error. */
    }
    return p;
}
PyObject *FridayPublisherRootOwnErrorRecordCheck(PyObject *args) {
    FridayPublisherOwnValue *p=own_error_record_arguments(args);
    if(PyErr_Occurred())return NULL;return PyBool_FromLong(p!=NULL);
}
PyObject *FridayPublisherRootOwnErrorRecordBuiltin(PyObject *args) {
    FridayPublisherOwnValue *p=own_error_record_arguments(args);
    if(PyErr_Occurred())return NULL;return PyBool_FromLong(p&&p->flags==1);
}
static int own_class_error_take(FridayPublisherOwnValue *p) {
    /* MOVE the original indicator into an EMPTY preowned cell. No factory,
     * normalization, replacement or DECREF of a last original is involved. */
    if(!p||p->error_saved||!PyErr_Occurred())return -1;
    p->error_saved=1;PyErr_Fetch(&p->error_type,&p->error_value,&p->error_tb);
    return 0;
}
static void own_class_error_restore(FridayPublisherOwnValue *p) {
    if(p&&p->error_saved&&!PyErr_Occurred())
        PyErr_Restore(Py_XNewRef(p->error_type),Py_XNewRef(p->error_value),Py_XNewRef(p->error_tb));
}
static FridayPublisherOwnValue *own_class_scope(PyObject *ctx) {
    if(!ctx||!PyTuple_CheckExact(ctx)||PyTuple_GET_SIZE(ctx)!=4)return NULL;
    PyObject *number=PyTuple_GET_ITEM(ctx,3);
    if(!PyLong_CheckExact(number))return NULL;
    unsigned long long serial=PyLong_AsUnsignedLongLong(number);
    if(PyErr_Occurred()||!serial||serial>root_storage.own_values.count)return NULL;
    FridayPublisherOwnValue *p=&root_storage.own_values.rows[serial-1];
    if(p->kind!=OWN_CLASS_SCOPE||p->pid!=getpid()||p->refs[7]!=ctx||
       p->refs[0]!=PyTuple_GET_ITEM(ctx,1)||p->refs[5]!=PyTuple_GET_ITEM(ctx,0))return NULL;
    return p;
}
static PyObject *own_class_factory(PyObject *self,PyObject *args,PyObject *kwargs) {
    FridayPublisherOwnValue *scope=own_class_scope(self);
    if(!scope||!(scope->flags&CLASS_MODULE_SET_OK)||
       !(scope->flags&CLASS_EVAL_ATTEMPT)||(scope->flags&CLASS_RESTORE_ATTEMPT)||
       !PyTuple_CheckExact(args)||PyTuple_GET_SIZE(args)<2||
       (kwargs&&!PyDict_CheckExact(kwargs)))
        return FridayPublisherMasterFault(&root_storage.pool,"own_class_factory_preflight_context"),NULL;
    PyObject *original=PyTuple_GET_ITEM(self,0);
    PyObject *module=PyTuple_GET_ITEM(self,1);
    unsigned long long serial=PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(self,2));
    if(PyErr_Occurred())return NULL;
    if(FridayPublisherMasterBefore(&root_storage.pool,4096,0,0,0)<0)return NULL;
    PyObject *installed=PyDict_GetItemWithError(scope->refs[1],scope->refs[9]);
    PyObject *installed_wrapper=installed==scope->refs[4]?
        PyDict_GetItemWithError(scope->refs[4],scope->refs[10]):NULL;
    if(PyErr_Occurred())return NULL;
    if(installed!=scope->refs[4]||installed_wrapper!=scope->refs[6])
        return FridayPublisherMasterFault(&root_storage.pool,"class_factory_actual_installed_aliases"),NULL;
    FridayPublisherOwnValue *binding=own_class_binding(module,serial);
    if(!binding||binding->pid!=getpid()||!binding->refs[2]||!PyBytes_CheckExact(binding->refs[2])||
       !binding->refs[3]||!PyCode_Check(binding->refs[3])||
       scope->refs[11]!=binding->refs[3]||!PyCallable_Check(original))
        return FridayPublisherMasterFault(&root_storage.pool,"own_class_factory_preflight_binding"),NULL;
    PyObject *dict=PyModule_GetDict(module);
    PyObject *fn_object=PyTuple_GET_ITEM(args,0),*name=PyTuple_GET_ITEM(args,1);
    if(!dict||Py_TYPE(fn_object)!=&PyFunction_Type||!PyUnicode_CheckExact(name))
        return FridayPublisherMasterFault(&root_storage.pool,"own_class_factory_preflight_arguments"),NULL;
    PyFunctionObject *fn=(PyFunctionObject *)fn_object;PyObject *code=fn->func_code;
    if(!code||!PyCode_Check(code))
        return FridayPublisherMasterFault(&root_storage.pool,"own_class_factory_preflight_body"),NULL;
    int local=fn->func_globals==dict;
    uint64_t argc=(uint64_t)PyTuple_GET_SIZE(args);
    Py_ssize_t kw=kwargs?PyDict_Size(kwargs):0;
    if(kw<0||argc>FRIDAY_ROOT_COMMAND_EDGES||(uint64_t)kw>FRIDAY_ROOT_COMMAND_EDGES/2||
       argc>UINT64_MAX-(uint64_t)kw||secondary_debit_scan(argc+(uint64_t)kw,0)<0)return NULL;
    /* Authority, native row capacity and all strong INPUT slots exist BEFORE
     * the actual factory. Even a failed/foreign/nonclass result gets this
     * original outcome row, never a post-effect own_birth. */
    FridayPublisherOwnValue *row=own_birth(OWN_CLASS);
    if(!row)return FridayPublisherMasterFault(&root_storage.pool,"own_class_factory_preowned_capacity"),NULL;
    own_ref(row,1,module);own_ref(row,2,name);
    own_ref(row,3,binding->refs[3]);own_ref(row,4,binding->refs[2]);own_ref(row,5,code);
    own_ref(row,6,original);own_ref(row,7,args);own_ref(row,8,kwargs?kwargs:Py_None);
    own_ref(row,9,fn_object);own_ref(row,10,dict);own_ref(row,11,self);
    row->width=scope->serial;row->attempts=1;
    PyObject *result=PyObject_Call(original,args,kwargs);
    if(!result){row->flags=CLASS_CALL_ERROR;own_error(row);return NULL;}
    /* Direct MOVE into reserved field, before any new lookup/allocation/
     * metadata. The second ref is only the outgoing factory return. */
    row->refs[0]=result;row->confirmed=1;
    row->flags=!local?CLASS_RETURN_FOREIGN:PyType_Check(result)?CLASS_RETURN_LOCAL:CLASS_RETURN_NONCLASS;
    Py_INCREF(result);
    return result;
}
static PyMethodDef own_class_factory_def={
    "own_class_factory",(PyCFunction)own_class_factory,METH_VARARGS|METH_KEYWORDS,NULL
};
static int own_class_factory_install(PyObject *module,FridayPublisherOwnValue *binding,
    FridayPublisherOwnValue **owned_scope) {
    *owned_scope=NULL;
    PyObject *dict=module?PyModule_GetDict(module):NULL;
    if(!dict||!binding||binding->refs[0]!=module||!binding->refs[3]||
       !PyCode_Check(binding->refs[3]))return -1;
    FridayPublisherOwnValues *values=&root_storage.own_values;
    if(values->count>FRIDAY_ROOT_COMMAND_NODES-3)
        return FridayPublisherMasterFault(&root_storage.pool,"class_scope_preowned_rows_capacity");
    FridayPublisherOwnValue *scope=own_birth(OWN_CLASS_SCOPE);
    if(!scope)return -1;
    *owned_scope=scope;own_ref(scope,0,module);own_ref(scope,1,dict);own_ref(scope,11,binding->refs[3]);
    FridayPublisherOwnValue *first=own_birth(OWN_CLASS_RESTORE),*second=first?own_birth(OWN_CLASS_RESTORE):NULL;
    if(!first||!second)return own_error(scope);
    first->flags=1;second->flags=2;first->width=second->width=scope->serial;
    own_ref(first,0,module);own_ref(second,0,module);own_ref(second,1,dict);
    /* These exact keys and their hashes are materialized before install/eval.
     * Restore has no GetItemString key factory or post-effect row birth. */
    scope->refs[9]=PyUnicode_FromString("__builtins__");
    scope->refs[10]=scope->refs[9]?PyUnicode_FromString("__build_class__"):NULL;
    if(!scope->refs[9]||!scope->refs[10]||PyObject_Hash(scope->refs[9])==-1||
       PyObject_Hash(scope->refs[10])==-1)return own_error(scope);
    own_ref(first,2,scope->refs[10]);own_ref(second,2,scope->refs[9]);
    PyObject *current=PyDict_GetItemWithError(dict,scope->refs[9]);
    if(PyErr_Occurred())return own_error(scope);
    own_ref(scope,2,current?current:Py_None);scope->width=current!=NULL;
    own_ref(second,4,current?current:Py_None);
    PyObject *base=NULL;
    if(current&&PyDict_CheckExact(current))base=current;
    else if(current&&Py_TYPE(current)==&PyModule_Type)base=PyModule_GetDict(current);
    else {
        PyObject *builtins=PyEval_GetBuiltins();
        if(builtins&&PyDict_CheckExact(builtins))base=builtins;
        else if(builtins&&Py_TYPE(builtins)==&PyModule_Type)base=PyModule_GetDict(builtins);
    }
    if(!base||!PyDict_CheckExact(base))return FridayPublisherMasterFault(&root_storage.pool,"class_builtins_base_refused"),own_error(scope);
    own_ref(scope,3,base);
    Py_ssize_t count=PyDict_Size(base);
    if(count<0||(uint64_t)count>FRIDAY_ROOT_COMMAND_EDGES/2||
       (uint64_t)count>(UINT64_MAX-131072)/128||
       (uint64_t)count>(UINT64_MAX-4096)/64)
        return FridayPublisherMasterFault(&root_storage.pool,"class_builtins_copy_cardinality"),own_error(scope);
    /* SAME cumulative pool: actual entry multiplicity and explicit copy
     * storage plus both fixed restore operations BEFORE PyDict_Copy. This is
     * a source-level debit, NOT a physical ABI/whole-workload fit theorem. */
    if(FridayPublisherMasterBefore(&root_storage.pool,(uint64_t)count*64+4096,0,0,
       131072+(uint64_t)count*128)<0)return own_error(scope);
    scope->refs[4]=PyDict_Copy(base);
    if(!scope->refs[4])return own_error(scope);
    scope->flags|=CLASS_COPY;own_ref(first,1,scope->refs[4]);own_ref(second,3,scope->refs[4]);
    PyObject *original=PyDict_GetItemWithError(scope->refs[4],scope->refs[10]);
    if(!original||!PyCallable_Check(original)) {
        if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"class_original_factory_missing");
        return own_error(scope);
    }
    own_ref(scope,5,original);own_ref(first,4,original);
    PyObject *serial=PyLong_FromUnsignedLongLong(binding->serial);
    PyObject *scope_serial=serial?PyLong_FromUnsignedLongLong(scope->serial):NULL;
    if(serial&&scope_serial)scope->refs[7]=PyTuple_Pack(4,original,module,serial,scope_serial);
    Py_XDECREF(serial);Py_XDECREF(scope_serial);
    if(!scope->refs[7])return own_error(scope);
    scope->refs[6]=PyCFunction_New(&own_class_factory_def,scope->refs[7]);
    if(!scope->refs[6])return own_error(scope);
    scope->flags|=CLASS_WRAPPER;own_ref(first,3,scope->refs[6]);
    scope->flags|=CLASS_COPY_SET_ATTEMPT;
    if(PyDict_SetItem(scope->refs[4],scope->refs[10],scope->refs[6])<0) {
        scope->flags|=CLASS_UNCERTAIN|CLASS_INSTALL_ERROR;return own_error(scope);
    }
    scope->flags|=CLASS_COPY_SET_OK|CLASS_MODULE_SET_ATTEMPT;
    if(PyDict_SetItem(dict,scope->refs[9],scope->refs[4])<0) {
        scope->flags|=CLASS_UNCERTAIN|CLASS_INSTALL_ERROR;return own_error(scope);
    }
    scope->flags|=CLASS_MODULE_SET_OK;
    return 0;
}
static int own_class_factory_restore(FridayPublisherOwnValue *scope) {
    if(!scope||scope->kind!=OWN_CLASS_SCOPE||scope->pid!=getpid()||
       !(scope->flags&CLASS_MODULE_SET_OK)||(scope->flags&CLASS_RESTORE_ATTEMPT)||
       scope->serial+2>root_storage.own_values.count) {
        if(scope&&scope->kind==OWN_CLASS_SCOPE)scope->flags|=CLASS_UNCERTAIN;
        /* Do NOT clear/replace a pending eval error on structural refusal.
         * Both the original scope/binding and the real indicator retain it.
         * With no original error, use only the existing preowned pool refusal. */
        if(scope&&scope->kind==OWN_CLASS_SCOPE)own_error(scope);
        FridayPublisherMasterFault(&root_storage.pool,"class_restore_initial_relation_refused");
        if(scope&&scope->kind==OWN_CLASS_SCOPE)own_error(scope);
        return -1;
    }
    FridayPublisherOwnValue *rows=root_storage.own_values.rows;
    FridayPublisherOwnValue *first=&rows[scope->serial],*second=&rows[scope->serial+1];
    /* Initial eval error is kept separately from BOTH preowned restore cells.
     * No PyErr_Clear, error replacement or success from a void helper. */
    if(PyErr_Occurred()&&own_class_error_take(scope)<0)return -1;
    scope->flags|=CLASS_RESTORE_ATTEMPT;
    FridayPublisherOwnValue *failed=NULL;
    for(unsigned i=0;i<2;i++) {
        FridayPublisherOwnValue *op=i?second:first;
        if(op->kind!=OWN_CLASS_RESTORE||op->pid!=getpid()||op->width!=scope->serial||
           op->flags!=i+1||op->attempts||op->confirmed||!op->refs[1]||
           !PyDict_CheckExact(op->refs[1])||!op->refs[2]||!op->refs[3]||!op->refs[4]) {
            FridayPublisherMasterFault(&root_storage.pool,"class_restore_preowned_relation");failed=op;break;
        }
        /* First also requires the EXACT installed copy still in the actual
         * module. A changed dictionary/key is retained and STOPPED, not fixed
         * by overwriting somebody else's value or by retrying an effect. */
        if(!i) {
            PyObject *installed=PyDict_GetItemWithError(scope->refs[1],scope->refs[9]);
            if(PyErr_Occurred()||installed!=scope->refs[4]) {
                own_ref(second,5,installed?installed:Py_None);
                if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"class_restore_installed_copy_changed");
                failed=first;break;
            }
        }
        op->flags|=CLASS_RESTORE_LOOKUP_ATTEMPT;
        PyObject *before=PyDict_GetItemWithError(op->refs[1],op->refs[2]);
        if(!PyErr_Occurred())op->flags|=CLASS_RESTORE_LOOKUP_RETURN;
        own_ref(op,5,before?before:Py_None);
        if(PyErr_Occurred()||before!=op->refs[3]) {
            if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"class_restore_current_alias_changed");
            failed=op;break;
        }
        op->attempts=1;
        int rc=i&&!scope->width?PyDict_DelItem(op->refs[1],op->refs[2]):
            PyDict_SetItem(op->refs[1],op->refs[2],op->refs[4]);
        op->flags|=CLASS_RESTORE_MUTATION_RETURN;
        if(rc!=0)op->flags|=CLASS_RESTORE_MUTATION_FAILED;
        if(rc!=0||PyErr_Occurred()) {
            if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"class_restore_mutation_return_refused");
            failed=op;break;
        }
        op->flags|=CLASS_RESTORE_AFTER_ATTEMPT;
        PyObject *after=PyDict_GetItemWithError(op->refs[1],op->refs[2]);
        if(!PyErr_Occurred())op->flags|=CLASS_RESTORE_AFTER_RETURN;
        own_ref(op,6,after?after:Py_None);
        if(PyErr_Occurred()||(i&&!scope->width?after!=NULL:after!=op->refs[4])) {
            if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"class_restore_returned_alias_unconfirmed");
            failed=op;break;
        }
        op->confirmed=1;
    }
    if(failed) {
        scope->flags|=CLASS_UNCERTAIN;
        if(PyErr_Occurred())own_class_error_take(failed);
        /* Priority is initial eval error. The actual NEW restore error remains
         * separately strongly held even when initial is the outward error. */
        own_class_error_restore(scope);own_class_error_restore(failed);
        return -1; /* no second mutation, continuation or terminal success */
    }
    scope->flags|=CLASS_RESTORE_OK;scope->confirmed=1;
    own_class_error_restore(scope);
    return 0;
}
static int own_types(FridayPublisherOwnValue *b) {
    FridayPublisherOwnValues *v=&root_storage.own_values;
    PyObject *module=b->refs[0];
    if(!module||Py_TYPE(module)!=&PyModule_Type)return -1;
    if(secondary_debit_scan(v->count,0)<0)return -1;
    for(uint64_t i=0;i<v->count;i++) {
        FridayPublisherOwnValue *p=&v->rows[i];
        if(p->pid!=getpid()||p->kind!=OWN_CLASS||p->refs[1]!=module)continue;
        if(!own_class_success(p))continue; /* failed/foreign outcomes are real row DATA, not class authority */
        if(!p->refs[0]||!PyType_Check(p->refs[0])||
           !p->refs[2]||!PyUnicode_CheckExact(p->refs[2])||
           !p->refs[3]||!PyCode_Check(p->refs[3])||
           !p->refs[4]||!PyBytes_CheckExact(p->refs[4])||
           !p->refs[5]||!PyCode_Check(p->refs[5])||
           !((PyTypeObject *)p->refs[0])->tp_dict)return -1;
    }
    return 0;
}
static FridayPublisherOwnValue *own_binding(PyObject *m,PyObject *pin,PyObject *raw) {
    if(Py_TYPE(m)!=&PyModule_Type||!PyBytes_CheckExact(raw)||!PyDict_CheckExact(pin))return NULL;
    RootHeldFile *h=held_by_pin(pin);
    if(!h||!h->raw||!PyBytes_CheckExact(h->raw)||
       PyBytes_GET_SIZE(raw)!=PyBytes_GET_SIZE(h->raw)||
       memcmp(PyBytes_AS_STRING(raw),PyBytes_AS_STRING(h->raw),(size_t)PyBytes_GET_SIZE(raw)))return NULL;
    FridayPublisherOwnValue *p=own_birth(OWN_BINDING);if(!p)return NULL;
    own_ref(p,0,m);own_ref(p,1,h->pin);own_ref(p,2,h->raw);
    own_ref(p,10,field(h->pin,"relative_path"));
    p->width=(uint64_t)PyBytes_GET_SIZE(h->raw);return p;
}
PyObject *FridayPublisherRootOwnConsumer(PyObject *args) {
    PyObject *m,*pin,*raw;
    if(!PyArg_ParseTuple(args,"OOO",&m,&pin,&raw))return NULL;
    FridayPublisherOwnValue *p=own_binding(m,pin,raw);
    if(!p){if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"consumer_actual_full_held_binding");return NULL;}
    PyObject *d=PyModule_GetDict(m),*path=field(p->refs[1],"path");
    if(!path||!PyUnicode_CheckExact(path)||PyDict_SetItemString(d,"__file__",path)<0)return own_error(p),NULL;
    const char *filename=PyUnicode_AsUTF8(path);if(!filename)return own_error(p),NULL;
    p->attempts=1;
    /* SAME compile/eval previously in exact HeldConsumerLoader call site. */
    p->refs[3]=Py_CompileString(PyBytes_AS_STRING(raw),filename,Py_file_input);
    if(!p->refs[3])return own_error(p),NULL;
    FridayPublisherOwnValue *factory_scope=NULL;
    if(own_class_factory_install(m,p,&factory_scope)<0)
        return own_error(p),NULL;
    factory_scope->flags|=CLASS_EVAL_ATTEMPT;factory_scope->attempts=1;
    PyObject *value=PyEval_EvalCode(p->refs[3],d,d);
    if(value){factory_scope->refs[8]=value;factory_scope->flags|=CLASS_EVAL_RETURN;own_ref(p,4,value);}
    else {
        if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"consumer_eval_NULL_without_original_error");
        own_error(p); /* original eval error BEFORE restore can fail */
    }
    int restored=own_class_factory_restore(factory_scope);
    if(restored!=0||!value||PyErr_Occurred()||factory_scope->error_saved){own_error(p);return NULL;}
    p->confirmed=1;p->flags=1;
    if(own_types(p)<0)return FridayPublisherMasterFault(&root_storage.pool,"consumer_birth_binding_capacity"),NULL;
    Py_RETURN_NONE;
}
static int secondary_debit_scan(uint64_t,uint64_t);
static FridayPublisherOwnValue *own_module_for_namespace(PyObject *o) {
    FridayPublisherOwnValues *v=&root_storage.own_values;
    /* Full registry cardinality before this Source-facing probe. */
    if(secondary_debit_scan(v->count,0)<0) {
        if(Py_IsInitialized()&&!PyErr_Occurred())
            PyErr_SetString(PyExc_RuntimeError,"own_namespace_registry_scan_refused");
        return NULL;
    }
    for(uint64_t i=0;i<v->count;i++) {
        FridayPublisherOwnValue *p=&v->rows[i];
        if(p->pid!=getpid())continue; /* parent relation is not child authority */
        if(p->kind==OWN_BINDING&&p->refs[0]&&
           (p->refs[0]==o||PyModule_GetDict(p->refs[0])==o))return p;
        if(own_class_success(p)&&p->refs[0]&&
           (p->refs[0]==o||((PyTypeObject *)p->refs[0])->tp_dict==o)) {
            /* Class match selects a second full binding walk. Pay that
             * same registry cardinality before own_find. The clock sample
             * inside own_find is not this debit. */
            if(secondary_debit_scan(v->count,0)<0) {
                if(Py_IsInitialized()&&!PyErr_Occurred())
                    PyErr_SetString(PyExc_RuntimeError,"own_class_binding_scan_refused");
                return NULL;
            }
            return own_find(p->refs[1],OWN_BINDING);
        }
    }
    return NULL;
}
PyObject *FridayPublisherRootOwnBinding(PyObject *o) {
    FridayPublisherOwnValue *p=own_module_for_namespace(o);
    if(PyErr_Occurred())return NULL;
    if(!p||!p->refs[3])Py_RETURN_NONE;
    if(own_before(131072)<0)return NULL;
    return Py_BuildValue("(OOOOOO)",p->refs[0],PyModule_GetDict(p->refs[0]),
        p->refs[3],p->refs[1],p->refs[2],p->confirmed?Py_True:Py_False);
}
PyObject *FridayPublisherRootOwnBindingCheck(PyObject *o) {
    if(!FridayPublisherMasterOwns(&root_storage.pool)||!PyTuple_CheckExact(o)||PyTuple_GET_SIZE(o)!=6)
        Py_RETURN_FALSE;
    FridayPublisherOwnValue *p=own_module_for_namespace(PyTuple_GET_ITEM(o,0));
    if(PyErr_Occurred())return NULL;
    if(!p||p->pid!=getpid()||!p->refs[3]||!p->refs[2]||!p->refs[1])Py_RETURN_FALSE;
    if(PyTuple_GET_ITEM(o,0)!=p->refs[0]||
       PyTuple_GET_ITEM(o,1)!=PyModule_GetDict(p->refs[0])||
       PyTuple_GET_ITEM(o,2)!=p->refs[3]||PyTuple_GET_ITEM(o,3)!=p->refs[1]||
       PyTuple_GET_ITEM(o,4)!=p->refs[2]||
       PyTuple_GET_ITEM(o,5)!=(p->confirmed?Py_True:Py_False))Py_RETURN_FALSE;
    /* Actual retained relation; no sys.modules/filename/class-name issuer. */
    Py_RETURN_TRUE;
}
PyObject *FridayPublisherRootOwnHeldBodyCheck(PyObject *args) {
    PyObject *pin,*raw;
    if(!PyArg_ParseTuple(args,"OO",&pin,&raw))return NULL;
    if(!FridayPublisherMasterOwns(&root_storage.pool)||!PyDict_CheckExact(pin)||!PyBytes_CheckExact(raw))
        Py_RETURN_FALSE;
    RootHeldFile *h=held_by_pin(pin);
    if(!h||!h->raw||!PyBytes_CheckExact(h->raw)||
       PyBytes_GET_SIZE(raw)!=PyBytes_GET_SIZE(h->raw))Py_RETURN_FALSE;
    uint64_t width=(uint64_t)PyBytes_GET_SIZE(raw);
    if(own_before(131072)<0||FridayPublisherMasterBefore(&root_storage.pool,2*width,0,0,0)<0)return NULL;
    if(memcmp(PyBytes_AS_STRING(raw),PyBytes_AS_STRING(h->raw),(size_t)width))Py_RETURN_FALSE;
    Py_RETURN_TRUE;
}
PyObject *FridayPublisherRootOwnClassBody(PyObject *o) {
    if(!PyType_Check(o))Py_RETURN_NONE;
    FridayPublisherOwnValue *p=own_class_find(o);
    if(PyErr_Occurred())return NULL;
    if(!own_class_success(p)||!p->refs[2]||!PyUnicode_CheckExact(p->refs[2])||
       !p->refs[3]||!PyCode_Check(p->refs[3])||
       !p->refs[4]||!PyBytes_CheckExact(p->refs[4])||
       !p->refs[5]||!PyCode_Check(p->refs[5])||
       !((PyTypeObject *)o)->tp_dict)Py_RETURN_NONE;
    return Py_NewRef(((PyTypeObject *)o)->tp_dict);
}
PyObject *FridayPublisherRootOwnFunction(PyObject *o) {
    if(Py_TYPE(o)!=&PyFunction_Type)Py_RETURN_NONE;
    PyFunctionObject *q=(PyFunctionObject *)o;
    FridayPublisherOwnValue *b=own_module_for_namespace(q->func_globals);
    if(PyErr_Occurred())return NULL;
    if(!b||!b->refs[3])Py_RETURN_NONE;
    if(own_before(131072)<0)return NULL;
    return Py_BuildValue("{s:O,s:O,s:O,s:O,s:O}",
        "annotations",q->func_annotations?q->func_annotations:Py_None,
        "annotate",q->func_annotate?q->func_annotate:Py_None,
        "annotations_present",q->func_annotations?Py_True:Py_False,
        "annotate_present",q->func_annotate?Py_True:Py_False,
        "builtins",q->func_builtins);
}
static PyObject *stock_frame_field(PyObject *obj,const char *field) {
    /* Same stock descriptor the frame command reader already uses. */
    PyObject *d=PyFrame_Type.tp_dict?PyDict_GetItemString(PyFrame_Type.tp_dict,field):NULL;
    if(PyErr_Occurred()||!d||
       (Py_TYPE(d)!=&PyGetSetDescr_Type&&Py_TYPE(d)!=&PyMemberDescr_Type)||
       !Py_TYPE(d)->tp_descr_get)return NULL;
    return Py_TYPE(d)->tp_descr_get(d,obj,(PyObject *)Py_TYPE(obj));
}
static int own_support_relation(PyObject *o,PyObject *origin,PyObject *key,
    uint64_t mode,FridayPublisherOwnValue **binding) {
    FridayPublisherOwnValue *b=NULL;
    if(!o||!origin||!key||root_storage.own_values.retired||
       !FridayPublisherMasterOwns(&root_storage.pool))return 0;
    if(mode==1&&PyDict_CheckExact(origin)&&PyUnicode_CheckExact(key)) {
        b=own_module_for_namespace(origin);if(!b)return 0;
        PyObject *actual=PyDict_GetItemWithError(origin,key);
        if(PyErr_Occurred()||actual!=o)return 0;
        int special=PyUnicode_CompareWithASCIIString(key,"__builtins__")==0||
            PyUnicode_CompareWithASCIIString(key,"__loader__")==0||
            PyUnicode_CompareWithASCIIString(key,"__spec__")==0;
        int runtime=Py_TYPE(o)==&PyCFunction_Type||Py_TYPE(o)==&PyGetSetDescr_Type||
            Py_TYPE(o)==&PyMemberDescr_Type||Py_TYPE(o)==&PyMethodDescr_Type||
            Py_TYPE(o)==&PyWrapperDescr_Type;
        if(Py_TYPE(o)==&PyModule_Type)
            runtime=!own_find(o,OWN_BINDING)&&!FridayPublisherOwnedModuleOriginal(o);
        if(PyType_Check(o))runtime=!own_class_find(o);
        if(PyErr_Occurred())return 0;
        if(!runtime&&!special)return 0;
    } else if(mode==2&&Py_TYPE(origin)==&PyFunction_Type) {
        PyFunctionObject *q=(PyFunctionObject *)origin;
        b=own_module_for_namespace(q->func_globals);
        if(!b||q->func_builtins!=o)return 0;
    } else if(mode==3&&Py_TYPE(origin)==&PyFrame_Type&&Py_TYPE(key)==&PyCode_Type&&
              PyDict_CheckExact(o)) {
        /* Foreign frame globals only. An owned namespace stays data.
         * No module binding is invented for this support row. */
        PyObject *code=stock_frame_field(origin,"f_code");
        PyObject *globals=stock_frame_field(origin,"f_globals");
        int matched=code&&globals&&code==key&&globals==o&&!own_module_for_namespace(globals);
        int failed=PyErr_Occurred();
        Py_XDECREF(code);Py_XDECREF(globals);
        if(failed||!matched)return 0;
        *binding=NULL;return 1;
    } else return 0;
    if(!b||!b->refs[3])return 0;
    *binding=b;return 1;
}
PyObject *FridayPublisherRootOwnSupport(PyObject *args) {
    PyObject *o,*origin,*key;unsigned long long mode;
    if(!PyArg_ParseTuple(args,"OOOK",&o,&origin,&key,&mode))return NULL;
    FridayPublisherOwnValue *b=NULL;
    if(!own_support_relation(o,origin,key,(uint64_t)mode,&b)) {
        if(PyErr_Occurred())return NULL;
        Py_RETURN_NONE;
    }
    if(own_before(512)<0)return NULL; /* BEFORE actual serial scalar factory */
    /* Repeated actual Source cuts reuse only this exact retained relation;
     * no unbounded fresh support row on every graph/callback. This read is
     * still bounded/clocked and its actual cost remains C2 UNKNOWN. */
    FridayPublisherOwnValues *v=&root_storage.own_values;
    if(secondary_debit_scan(v->count,0)<0)return NULL;
    for(uint64_t i=0;i<v->count;i++) {
        if((i&1023)==0&&(!now_ns()||now_ns()>root_storage.pool.deadline_ns))
            return FridayPublisherMasterFault(&root_storage.pool,"own_support_lookup_clock"),NULL;
        FridayPublisherOwnValue *q=&v->rows[i];
        if(!(q->kind==OWN_SUPPORT&&q->confirmed&&q->refs[0]==o&&q->refs[1]==origin&&
             q->refs[2]==key&&q->flags==(uint64_t)mode))continue;
        if((uint64_t)mode==3) {
            if(!q->refs[3]&&!q->refs[4]&&!q->refs[5]&&!q->refs[6])
                return PyLong_FromUnsignedLongLong(q->serial);
            continue;
        }
        if(b&&q->refs[3]==b->refs[0]&&q->refs[4]==b->refs[3]&&
           q->refs[5]==b->refs[2]&&q->refs[6]==b->refs[1])
            return PyLong_FromUnsignedLongLong(q->serial);
    }
    FridayPublisherOwnValue *p=own_birth(OWN_SUPPORT);if(!p)return NULL;
    own_ref(p,0,o);own_ref(p,1,origin);own_ref(p,2,key);
    p->flags=(uint64_t)mode;p->confirmed=1;
    if((uint64_t)mode==3)return PyLong_FromUnsignedLongLong(p->serial);
    own_ref(p,3,b->refs[0]);own_ref(p,4,b->refs[3]);own_ref(p,5,b->refs[2]);
    own_ref(p,6,b->refs[1]);
    return PyLong_FromUnsignedLongLong(p->serial);
}
PyObject *FridayPublisherRootOwnSupportCheck(PyObject *args) {
    unsigned long long serial;
    if(!PyArg_ParseTuple(args,"K",&serial))return NULL;
    FridayPublisherOwnValues *v=&root_storage.own_values;
    if(!serial||serial>v->count)Py_RETURN_FALSE;
    FridayPublisherOwnValue *p=&v->rows[serial-1],*b=NULL;
    if(p->kind!=OWN_SUPPORT||!p->confirmed||
       !own_support_relation(p->refs[0],p->refs[1],p->refs[2],p->flags,&b)) {
        if(PyErr_Occurred())return NULL;
        Py_RETURN_FALSE;
    }
    if(p->flags==3) {
        if(b||p->refs[3]||p->refs[4]||p->refs[5]||p->refs[6])Py_RETURN_FALSE;
        Py_RETURN_TRUE;
    }
    if(!b||b->refs[0]!=p->refs[3]||b->refs[3]!=p->refs[4]||
       b->refs[2]!=p->refs[5]||b->refs[1]!=p->refs[6])Py_RETURN_FALSE;
    Py_RETURN_TRUE;
}

PyObject *FridayPublisherRootOwnPrepare(PyObject *args) {
    unsigned long long bytes=0,reads=0;
    if(!PyArg_ParseTuple(args,"K|K",&bytes,&reads)||(!bytes&&!reads))return NULL;
    /* Original sole Root only; no shadow/child issuer or returned capability.
     * One combined debit: the first argument stays allocation for existing
     * callers, and the optional second argument is reads. Zero with zero
     * stays a refusal. Nominal explicit reserve is not a proved whole
     * implicit ABI or workload upper. */
    FridayPublisherRootStorage *s=&root_storage;
    if(!s->signed_admission||!s->snapshot_verified||s->command_receipt.attempted||
       !FridayPublisherMasterOwns(&s->pool))
        return FridayPublisherMasterFault(&s->pool,"own_value_same_original_Root_required"),NULL;
    if(FridayPublisherMasterBefore(&s->pool,(uint64_t)reads,0,0,(uint64_t)bytes)<0)return NULL;
    Py_RETURN_NONE;
}
PyObject *FridayPublisherRootOwnVar(PyObject *args) {
    PyObject *name,*default_value;int has_default;
    if(!PyArg_ParseTuple(args,"OpO",&name,&has_default,&default_value)||!PyUnicode_CheckExact(name))return NULL;
    FridayPublisherOwnValue *p=own_birth(OWN_VAR);if(!p)return NULL;
    own_ref(p,1,name);if(has_default)own_ref(p,2,default_value);
    p->flags=has_default?1:0;p->attempts=1;
    const char *s=PyUnicode_AsUTF8(name);if(!s)return own_error(p),NULL;
    p->refs[0]=PyContextVar_New(s,has_default?default_value:NULL);
    if(!p->refs[0])return own_error(p),NULL;
    p->confirmed=1;return Py_NewRef(p->refs[0]);
}
PyObject *FridayPublisherRootOwnSet(PyObject *args) {
    PyObject *var,*value,*transition;
    if(!PyArg_ParseTuple(args,"OOO",&var,&value,&transition)||!PyDict_CheckExact(transition))return NULL;
    if(!own_find(var,OWN_VAR))return FridayPublisherMasterFault(&root_storage.pool,"own_context_actual_constructor"),NULL;
    FridayPublisherOwnValue *p=own_birth(OWN_TOKEN);if(!p)return NULL;
    own_ref(p,1,var);own_ref(p,2,value);own_ref(p,6,transition);
    p->refs[4]=PyContext_CopyCurrent();if(!p->refs[4])return own_error(p),NULL;
    /* Actual current alias, distinct from copied before-context. Nonlimited
     * selected thread-state context layout remains a C2 obligation. */
    own_ref(p,3,PyThreadState_Get()->context);
    if(!p->refs[3])return FridayPublisherMasterFault(&root_storage.pool,"actual_current_context_missing"),NULL;
    /* Publish the actual context into the already strong original transition
     * BEFORE the setter. No fallible body factory is needed after token birth. */
    if(PyDict_SetItemString(transition,"actual_context",p->refs[3])<0)return own_error(p),NULL;
    int present=PySequence_Contains(p->refs[3],var);if(present<0)return own_error(p),NULL;
    if(present){p->refs[5]=PyObject_GetItem(p->refs[3],var);if(!p->refs[5])return own_error(p),NULL;}
    p->flags=present?1:0;p->attempts=1;
    p->refs[0]=PyContextVar_Set(var,value);if(!p->refs[0])return own_error(p),NULL;
    p->confirmed=1;return Py_NewRef(p->refs[0]);
}
PyObject *FridayPublisherRootOwnReset(PyObject *args) {
    PyObject *var,*token;
    if(!PyArg_ParseTuple(args,"OO",&var,&token))return NULL;
    FridayPublisherOwnValue *p=own_find(token,OWN_TOKEN);
    if(!p||p->refs[1]!=var||p->attempts!=1)return FridayPublisherMasterFault(&root_storage.pool,"own_context_actual_set_transition"),NULL;
    if(own_before(131072)<0)return NULL;
    p->attempts=2;p->flags|=2; /* BEFORE the one actual reset */
    int rc=PyContextVar_Reset(var,token);
    if(rc<0){p->flags|=8;return own_error(p),NULL;}
    p->confirmed=2;p->flags|=4;Py_RETURN_NONE;
}
PyObject *FridayPublisherRootOwnContextBody(PyObject *o) {
    FridayPublisherOwnValue *p=own_find(o,OWN_VAR);
    if(p) {
        if(own_before(131072)<0)return NULL;
        PyObject *ctx=PyThreadState_Get()->context,*current=NULL;
        int present=ctx?PySequence_Contains(ctx,o):0;if(present<0)return NULL;
        if(present&&PyContextVar_Get(o,NULL,&current)<0)return NULL;
        PyObject *cut=Py_BuildValue("{s:O,s:O,s:O,s:O,s:O}",
            "name",p->refs[1],"has_default",(p->flags&1)?Py_True:Py_False,
            "default",p->refs[2]?p->refs[2]:Py_None,"present",present?Py_True:Py_False,
            "current",current?current:Py_None);
        Py_XDECREF(current);return cut;
    }
    p=own_find(o,OWN_TOKEN);
    if(!p||!p->confirmed||p->flags&8)Py_RETURN_NONE;
    if(own_before(131072)<0)return NULL;
    PyObject *old=p->refs[5];
    if(!old){old=root_storage.own_values.token_missing;if(!old)return NULL;}
    return Py_BuildValue("{s:O,s:O,s:O,s:O,s:O}","var",p->refs[1],"old_value",old,
        "transition",p->refs[6],"actual_context",p->refs[3],"used",(p->flags&4)?Py_True:Py_False);
}
static PyObject *own_mapping_current_cut(FridayPublisherOwnValue *p,int cohort) {
    if(!p||p->pid!=getpid()||!p->refs[6]||p->attempts>2||p->confirmed!=1||
       !p->refs[10]||!PyType_Check(p->refs[10])||
       Py_TYPE(p->refs[6])!=(PyTypeObject *)p->refs[10])
        return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_current_phase_required"),NULL;
    FridayPublisherPreparedRow *fd=prepared_row(p->refs[4]);
    if(!fd||fd->credit!=p->refs[5]||fd->keeper<0||fd->keeper_closed||fd->keeper_attempted)
        return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_actual_keeper_before_cut"),NULL;
    /* Existing keeper of SAME OFD, no reopen or use of a retired numeric FD.
     * This branch is BEFORE mapping close only, including original FD-close
     * or Source registration/metadata errors while the returned map lives. */
    FridayPublisherOwnValue *cut=own_birth(OWN_MAP_CUT);if(!cut)return NULL;
    own_ref(cut,1,p->refs[1]);own_ref(cut,3,p->refs[4]);own_ref(cut,4,p->refs[5]);
    own_ref(cut,7,p->refs[6]);cut->attempts=1;cut->width=p->width;
    if(p->width>PY_SSIZE_T_MAX||own_before(2*p->width+131072)<0||
       FridayPublisherMasterBefore(&root_storage.pool,2*p->width,0,0,0)<0)return own_error(cut),NULL;
    struct stat before,after;
    if(fstat(fd->keeper,&before)<0) {PyErr_SetFromErrno(PyExc_OSError);return own_error(cut),NULL;}
    if(!stat_equal(&before,&fd->birth,0)||before.st_size<0||(uint64_t)before.st_size!=p->width)
        return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_keeper_original_generation"),NULL;
    PyMethodDef *tell=NULL;
    for(PyMethodDef *m=((PyTypeObject *)p->refs[10])->tp_methods;m&&m->ml_name;m++)
        if(!strcmp(m->ml_name,"tell")&&m->ml_flags==METH_NOARGS){tell=m;break;}
    if(!tell)return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_actual_stock_tell"),NULL;
    cut->refs[6]=tell->ml_meth(p->refs[6],NULL);
    if(!cut->refs[6]||!PyLong_CheckExact(cut->refs[6])) {
        if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"own_mapping_actual_position_type");
        return own_error(cut),NULL;
    }
    cut->refs[2]=PyBytes_FromStringAndSize(NULL,(Py_ssize_t)p->width);
    if(!cut->refs[2])return own_error(cut),NULL; /* actual destination strong BEFORE copy */
    memset(PyBytes_AS_STRING(cut->refs[2]),0,(size_t)p->width);
    Py_buffer view;memset(&view,0,sizeof(view));
    if(PyObject_GetBuffer(p->refs[6],&view,PyBUF_SIMPLE)<0)return own_error(cut),NULL;
    int valid=view.len>=0&&(uint64_t)view.len==p->width&&view.buf&&!view.readonly;
    if(valid) {
        memcpy(PyBytes_AS_STRING(cut->refs[2]),view.buf,(size_t)p->width);
        valid=!memcmp(PyBytes_AS_STRING(cut->refs[2]),view.buf,(size_t)p->width);
    }
    PyBuffer_Release(&view);
    if(PyErr_Occurred())return own_error(cut),NULL;
    if(!valid)return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_full_current_copy"),NULL;
    if(fstat(fd->keeper,&after)<0) {PyErr_SetFromErrno(PyExc_OSError);return own_error(cut),NULL;}
    if(!stat_equal(&before,&after,1))
        return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_keeper_cut_drift"),NULL;
    PyObject *again=tell->ml_meth(p->refs[6],NULL);
    if(!again)return own_error(cut),NULL;
    int stable=PyLong_CheckExact(again)&&PyObject_RichCompareBool(again,cut->refs[6],Py_EQ)==1;
    Py_DECREF(again);
    if(!stable||PyErr_Occurred()) {
        if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"own_mapping_current_position_changed");
        return own_error(cut),NULL;
    }
    PyObject *nine=stat9(&before);if(!nine)return own_error(cut),NULL;
    cut->refs[5]=PyList_AsTuple(nine);Py_DECREF(nine);
    if(!cut->refs[5])return own_error(cut),NULL;
    PyObject *bd=p->refs[7],*birth=own_plain_field(bd,"birth"),*role=own_plain_field(bd,"role");
    if(!birth||!PyList_CheckExact(birth)||PyList_GET_SIZE(birth)!=9||
       !role||!PyUnicode_CheckExact(role)) {
        FridayPublisherMasterFault(&root_storage.pool,"own_mapping_actual_birth9_role");
        return own_error(cut),NULL;
    }
    for(Py_ssize_t i=0;i<9;i++)if(!PyUnicode_CheckExact(PyList_GET_ITEM(birth,i))) {
        FridayPublisherMasterFault(&root_storage.pool,"own_mapping_birth9_scalar_type");
        return own_error(cut),NULL;
    }
    own_ref(cut,9,role);
    cut->refs[8]=PyList_AsTuple(birth);
    if(!cut->refs[8])return own_error(cut),NULL;
    cut->refs[0]=Py_BuildValue("{s:s,s:l,s:l,s:O,s:O,s:O,s:O,s:O,s:K,s:O,s:s,s:O,s:O,s:O}",
        "schema","friday.sol091.selected-owned-mmap-public-cut.v1",
        "creator_pid",(long)p->pid,"actual_pid",(long)getpid(),"role",role,
        "backing_birth9",cut->refs[8],"backing_cut9",cut->refs[5],
        "actual_row",p->refs[4],"actual_credit",p->refs[5],
        "width",(unsigned long long)p->width,"position",cut->refs[6],"access","WRITE",
        "closed",Py_False,"full_bytes",cut->refs[2],"cohort_before_effect",cohort?Py_True:Py_False);
    if(!cut->refs[0])return own_error(cut),NULL;
    /* Append to the SAME native-retained actual cut list before returning to
     * Source. A later metadata error cannot orphan any returned raw/cut. */
    if(PyList_Append(p->refs[9],cut->refs[0])<0)return own_error(cut),NULL;
    cut->confirmed=1;return Py_NewRef(cut->refs[0]);
}
static PyObject *own_hash_status_key(PyObject *d,const char *name);
static int own_hash_status_replace(PyObject *d,PyObject *key,PyObject *before,PyObject *after);
#define OWN_MAPPING_PREBIRTH UINT64_C(64)
static int own_mapping_body_matches(FridayPublisherOwnValue *p,
    const FridayPublisherPreparedRow *prepared) {
    if(!p->refs[3])return 0; /* unchanged legacy birth uses its old paired inputs */
    PyObject *dict=PyObject_GenericGetDict(p->refs[3],NULL);
    if(!dict||!PyDict_CheckExact(dict)){Py_XDECREF(dict);return own_error(p);}
    Py_ssize_t n=PyDict_Size(dict);
    if(n<0||(uint64_t)n>FRIDAY_ROOT_COMMAND_EDGES/2||
       secondary_debit_scan((uint64_t)n*3,0)<0){Py_DECREF(dict);return own_error(p);}
    PyObject *hold=own_plain_field(dict,"hold"),*fd=own_plain_field(dict,"fd"),
        *pid=own_plain_field(dict,"owner_pid");
    long actual_fd=fd&&PyLong_CheckExact(fd)?PyLong_AsLong(fd):-1;
    if(PyErr_Occurred()){Py_DECREF(dict);return own_error(p);}
    long actual_pid=pid&&PyLong_CheckExact(pid)?PyLong_AsLong(pid):-1;
    if(PyErr_Occurred()){Py_DECREF(dict);return own_error(p);}
    int matches=!PyErr_Occurred()&&prepared&&hold==p->refs[5]&&
        actual_fd==prepared->fd&&actual_pid==(long)getpid();
    Py_DECREF(dict); /* body itself still owns its complete original dictionary */
    if(!matches) {
        FridayPublisherMasterFault(&root_storage.pool,"mapping_actual_BODY_credit_fd_owner");
        return own_error(p);
    }
    return 0;
}
static PyObject *own_prepared_mapping_births(PyObject *body,PyObject *row,PyObject *credit) {
    FridayPublisherOwnValues *v=&root_storage.own_values;uint64_t count=v->count,n=0;
    if(count>FRIDAY_ROOT_COMMAND_NODES||count>UINT64_MAX/2||
       secondary_debit_scan(count*2,0)<0)return NULL;
    for(uint64_t i=0;i<count;i++) {
        if((i&1023)==0&&(!now_ns()||now_ns()>root_storage.pool.deadline_ns))
            return FridayPublisherMasterFault(&root_storage.pool,"mapping_inventory_clock"),NULL;
        FridayPublisherOwnValue *p=&v->rows[i];
        if(p->kind!=OWN_MAPPING||p->pid!=getpid()||p->refs[4]!=row)continue;
        if(p->refs[5]!=credit||(p->refs[3]&&p->refs[3]!=body)||!p->refs[0])
            return FridayPublisherMasterFault(&root_storage.pool,"mapping_inventory_original_body_credit"),NULL;
        n++;
    }
    if(n>(UINT64_MAX-131072)/sizeof(PyObject *)||
       own_before(131072+n*sizeof(PyObject *))<0)return NULL;
    PyObject *out=PyTuple_New((Py_ssize_t)n);if(!out)return NULL;
    uint64_t at=0;
    for(uint64_t i=0;i<count;i++) {
        if((i&1023)==0&&(!now_ns()||now_ns()>root_storage.pool.deadline_ns)) {
            Py_DECREF(out);return FridayPublisherMasterFault(&root_storage.pool,"mapping_inventory_clock"),NULL;
        }
        FridayPublisherOwnValue *p=&v->rows[i];
        if(p->kind!=OWN_MAPPING||p->pid!=getpid()||p->refs[4]!=row)continue;
        if(at>=n||p->refs[5]!=credit||(p->refs[3]&&p->refs[3]!=body)||!p->refs[0]) {
            Py_DECREF(out);return FridayPublisherMasterFault(&root_storage.pool,"mapping_inventory_changed"),NULL;
        }
        PyTuple_SET_ITEM(out,(Py_ssize_t)at++,Py_NewRef(p->refs[0]));
    }
    if(at!=n||v->count!=count) {
        Py_DECREF(out);return FridayPublisherMasterFault(&root_storage.pool,"mapping_inventory_changed"),NULL;
    }
    return out; /* originals remain strongly owned by native rows */
}
PyObject *FridayPublisherRootOwnMapping(PyObject *args) {
    const char *action;PyObject *birth,*mapping,*cut,*ledgers,*row,*credit;
    if(!PyArg_ParseTuple(args,"sOOOOOO",&action,&birth,&mapping,&cut,&ledgers,&row,&credit))return NULL;
    /* Every parent-native entry rejects a copied child PID before observing
     * or debiting its inherited Root. No new child/native grant is created. */
    if(!FridayPublisherMasterOwns(&root_storage.pool))
        return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_actual_Root_PID"),NULL;
    if(!strcmp(action,"prepared-births"))
        return own_prepared_mapping_births(cut,row,credit);
    int unmapped_query=!strcmp(action,"unmapped-state");
    int unmapped_end=!strcmp(action,"retire-unmapped");
    int prebirth_query=!strcmp(action,"prebirth-state");
    int prebirth_end=!strcmp(action,"retire-prebirth");
    int prebirth_ended=!strcmp(action,"prebirth-ended");
    int prebirth_error=!strcmp(action,"prebirth-error");
    /* Actual registry and prepared-row scans are prepaid before lookup.
     * Existing BODY and FD reservations remain distinct, never substituted. */
    if(root_storage.prepared_count>FRIDAY_NATIVE_FD_HISTORY||
       root_storage.prepared_count>UINT64_MAX/2||
       secondary_debit_scan(root_storage.own_values.count,0)<0||
       secondary_debit_scan(root_storage.prepared_count*2,0)<0)return NULL;
    if((unmapped_query||unmapped_end)&&secondary_debit_scan(14*6,0)<0)return NULL;
    FridayPublisherOwnValue *p=own_find(birth,OWN_MAPPING);
    if(!strcmp(action,"prebirth")) {
        FridayPublisherPreparedRow *prepared=prepared_row(row);
        if(p||!prepared||!prepared->opened||!prepared->row||!prepared->credit||
           !birth||birth==Py_None||cut==Py_None||credit==Py_None||
           !own_class_find((PyObject *)Py_TYPE(birth)))
            return FridayPublisherMasterFault(&root_storage.pool,"mapping_prebirth_original_inputs"),NULL;
        p=own_birth(OWN_MAPPING);if(!p)return NULL;
        p->flags=OWN_MAPPING_PREBIRTH; /* native create is forbidden in this phase */
        own_ref(p,0,birth);own_ref(p,1,birth);own_ref(p,3,cut);
        own_ref(p,4,row);own_ref(p,5,credit);
        if(own_mapping_body_matches(p,prepared)<0)return NULL;
        p->refs[7]=PyObject_GenericGetDict(birth,NULL);
        if(!p->refs[7]||!PyDict_CheckExact(p->refs[7]))return own_error(p),NULL;
        Py_RETURN_NONE;
    }
    if(prebirth_error&&(!p||!(p->flags&OWN_MAPPING_PREBIRTH)))Py_RETURN_FALSE;
    if(prebirth_query&&(!p||!(p->flags&OWN_MAPPING_PREBIRTH)))Py_RETURN_NONE;
    if(prebirth_error||prebirth_query||prebirth_end||prebirth_ended) {
        if(!p||p->pid!=getpid()||p->refs[4]!=row||p->refs[5]!=credit||
           !(p->flags&OWN_MAPPING_PREBIRTH)||(p->flags&~(OWN_MAPPING_PREBIRTH|UINT64_C(32)))||
           p->attempts||p->confirmed||p->refs[6]||p->width||!p->refs[3])
            return FridayPublisherMasterFault(&root_storage.pool,"mapping_prebirth_actual_phase"),NULL;
        if(prebirth_error) {
            if((p->flags&32)||!PyExceptionInstance_Check(cut)||
               (p->error_saved&&p->error_value!=cut))
                return FridayPublisherMasterFault(&root_storage.pool,"mapping_prebirth_original_error"),NULL;
            if(!p->error_saved) {
                p->error_saved=1;p->error_type=Py_NewRef((PyObject *)Py_TYPE(cut));
                p->error_value=Py_NewRef(cut); /* own BEFORE the traceback getter */
                p->error_tb=PyException_GetTraceback(cut);
                if(PyErr_Occurred())return NULL;
            }
            Py_RETURN_NONE;
        }
        if(p->refs[3]!=cut)return FridayPublisherMasterFault(&root_storage.pool,"mapping_prebirth_original_carrier"),NULL;
        if(prebirth_ended)return PyBool_FromLong((p->flags&32)!=0);
        if(prebirth_query) {
            if(own_before(131072)<0)return NULL;
            return PyTuple_Pack(8,p->refs[0],p->refs[3],p->refs[4],p->refs[5],
                p->error_value?p->error_value:Py_None,p->refs[7]?p->refs[7]:Py_None,
                p->refs[8]?p->refs[8]:Py_None,p->refs[9]?p->refs[9]:Py_None);
        }
        if(p->flags&32)return FridayPublisherMasterFault(&root_storage.pool,"mapping_prebirth_end_once"),NULL;
        p->flags|=32;Py_RETURN_TRUE; /* capability only; no fictional map.close */
    }
    if(!strcmp(action,"birth")) {
        if((p&&(p->flags!=OWN_MAPPING_PREBIRTH||p->attempts||p->confirmed||
                p->error_saved||p->refs[6]||p->refs[4]!=row||p->refs[5]!=credit))||
           !FridayPublisherPreparedHasRow(&root_storage.pool,row))
            return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_original_prepared_row"),NULL;
        if(!PyDict_CheckExact(cut)||!PyList_CheckExact(ledgers)||
           !ascii(own_plain_field(cut,"schema"),"friday.sol106.actual-mmap-constructor.v1")||
           own_plain_field(cut,"row")!=row||own_plain_field(cut,"credit")!=credit||
           own_plain_field(cut,"returned_mapping")!=Py_None||
           own_plain_field(cut,"returned")!=Py_False||
           own_plain_field(cut,"attempted")!=Py_False||
           own_plain_field(cut,"state_complete")!=Py_False||
           own_plain_field(cut,"original_error")!=Py_None||
           own_plain_field(cut,"metadata_error")!=Py_None||
           own_plain_field(cut,"prebirth_error")!=Py_None||
           !own_plain_field(cut,"birth"))
            return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_actual_birth_inputs"),NULL;
        if(!p){p=own_birth(OWN_MAPPING);if(!p)return NULL;}
        own_ref(p,0,birth);own_ref(p,1,birth);own_ref(p,4,row);own_ref(p,5,credit);
        own_ref(p,8,cut);own_ref(p,9,ledgers);
        PyObject *dict=PyObject_GenericGetDict(birth,NULL);
        if(!dict||!PyDict_CheckExact(dict)) {Py_XDECREF(dict);return own_error(p),NULL;}
        if(p->refs[7]) {
            int same=p->refs[7]==dict;Py_DECREF(dict);
            if(!same)return FridayPublisherMasterFault(&root_storage.pool,"mapping_prebirth_dictionary_changed"),own_error(p),NULL;
        } else p->refs[7]=dict;
        p->flags&=~OWN_MAPPING_PREBIRTH; /* upgrade only after actual full inputs */
        p->attempts=0;Py_RETURN_NONE;
    }
    if(!p||p->pid!=getpid()||p->refs[4]!=row||p->refs[5]!=credit)
        return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_original_birth_custody"),NULL;
    if(unmapped_query||unmapped_end) {
        /* Authoritative actual native outcome, not a Source mapping=None
         * assertion. No unregistered birth or uncertain returned value passes. */
        if(p->refs[6]||p->confirmed||(p->flags&(2|4|16))||
           p->refs[8]!=cut||p->refs[9]!=ledgers||
           !PyDict_CheckExact(cut)||PyDict_Size(cut)!=14||
           own_plain_field(cut,"row")!=row||own_plain_field(cut,"credit")!=credit||
           own_plain_field(cut,"returned")!=Py_False||
           own_plain_field(cut,"returned_mapping")!=Py_None||
           own_plain_field(cut,"state_complete")!=Py_False)
            return FridayPublisherMasterFault(&root_storage.pool,"own_unmapped_exact_outcome"),NULL;
        int state=p->attempts==0?1:
            p->attempts==1&&(p->flags&8)&&p->error_saved&&p->error_type?2:0;
        if(!state||(state==1&&own_plain_field(cut,"attempted")!=Py_False)||
           (state==2&&own_plain_field(cut,"attempted")!=Py_True))
            return FridayPublisherMasterFault(&root_storage.pool,"own_unmapped_actual_call_phase"),NULL;
        if(unmapped_query) {
            if(own_before(512)<0)return NULL;
            return PyLong_FromLong(state);
        }
        if(p->flags&32)
            return FridayPublisherMasterFault(&root_storage.pool,"own_unmapped_end_once"),NULL;
        p->flags|=32; /* retire creation capability, NOT a mapping.close fact */
        Py_RETURN_TRUE;
    }
    if(!strcmp(action,"create")) {
        if(p->flags&(32|OWN_MAPPING_PREBIRTH))
            return FridayPublisherMasterFault(&root_storage.pool,"own_unmapped_creation_retired_or_uninitialized"),NULL;
        PyObject *fd=own_plain_field(cut,"fd"),*width=own_plain_field(cut,"width");
        if(p->attempts||p->refs[6]||cut!=p->refs[8]||ledgers!=p->refs[9]||
           !fd||!PyLong_CheckExact(fd)||!width||!PyLong_CheckExact(width))
            return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_original_constructor_once"),NULL;
        long actual_fd=PyLong_AsLong(fd);unsigned long long count=PyLong_AsUnsignedLongLong(width);
        if(PyErr_Occurred())return own_error(p),NULL;
        FridayPublisherPreparedRow *prepared=prepared_row(row);
        /* credit is the existing terminal BODY allocation/read reservation.
         * The already retained native prepared row owns the separate FD/
         * keeper reservation needed by this generation/description check.
         * Do not replace BODY credit with FD credit or mint another grant. */
        if(actual_fd<0||actual_fd>INT_MAX||!count||count>BODY_CAP||!prepared||
           own_mapping_body_matches(p,prepared)<0||
           !FridayPublisherPreparedMatches(&root_storage.pool,row,prepared->credit,(int)actual_fd)||
           own_before(count+131072)<0)return NULL;
        p->width=(uint64_t)count;
        own_ref(p,11,root_storage.own_values.mapping_module);
        own_ref(p,10,root_storage.own_values.mapping_factory);
        PyObject *access=Py_XNewRef(root_storage.own_values.mapping_access);
        PyObject *actual_access=own_plain_field(cut,"access");
        if(!p->refs[10]||!p->refs[11]||!access||!PyLong_CheckExact(access)||
           !actual_access||!PyLong_CheckExact(actual_access)||
           PyObject_RichCompareBool(access,actual_access,Py_EQ)!=1) {
            Py_XDECREF(access);
            if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"own_mapping_actual_stock_constructor");
            return own_error(p),NULL;
        }
        PyObject *args2=PyTuple_Pack(2,fd,width);
        PyObject *kwargs=args2?Py_BuildValue("{s:O}","access",access):NULL;
        Py_DECREF(access);
        if(!args2||!kwargs){Py_XDECREF(args2);Py_XDECREF(kwargs);return own_error(p),NULL;}
        if(PyDict_SetItemString(cut,"attempted",Py_True)<0) {
            Py_DECREF(args2);Py_DECREF(kwargs);return own_error(p),NULL;
        }
        p->attempts=1; /* before the SINGLE actual constructor */
        p->refs[6]=PyObject_Call(p->refs[10],args2,kwargs);
        Py_DECREF(args2);Py_DECREF(kwargs);
        if(!p->refs[6]){p->flags|=8;return own_error(p),NULL;}
        /* Strong actual result BEFORE any post-return Source bookkeeping.
         * The preowned creation record receives the alias before instance
         * metadata. A later metadata miss keeps that alias. A failed
         * publication stays an unresolved return, not a constructor miss. */
        if(own_before(4096)<0) {
            p->confirmed=1;p->flags|=16;return own_error(p),NULL;
        }
        PyObject *returned_key=own_hash_status_key(cut,"returned_mapping");
        PyObject *flag_key=returned_key?own_hash_status_key(cut,"returned"):NULL;
        if(!returned_key||!flag_key||
           own_hash_status_replace(cut,returned_key,Py_None,p->refs[6])<0||
           own_hash_status_replace(cut,flag_key,Py_False,Py_True)<0) {
            p->confirmed=1;p->flags|=16;return own_error(p),NULL;
        }
        if(!p->refs[7]||PyDict_SetItemString(p->refs[7],"mapping",p->refs[6])<0) {
            p->confirmed=1;return own_error(p),NULL;
        }
        p->confirmed=1;return Py_NewRef(p->refs[6]);
    }
    if(!strcmp(action,"created")) {
        if((p->flags&32)||p->refs[6]||mapping==Py_None)return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_birth_once"),NULL;
        own_ref(p,6,mapping);p->confirmed=1;Py_RETURN_NONE;
    }
    if(!strcmp(action,"retained")) {
        if(!p->refs[6])Py_RETURN_NONE;
        return Py_NewRef(p->refs[6]);
    }
    if(p->refs[6]!=mapping)return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_same_original"),NULL;
    if(!strcmp(action,"cut")||!strcmp(action,"cohort-cut"))
        return own_mapping_current_cut(p,!strcmp(action,"cohort-cut"));
    if(!strcmp(action,"require-cut")) {
        if(p->attempts>2||p->confirmed!=1||!PyDict_CheckExact(cut))
            return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_current_check_phase"),NULL;
        FridayPublisherOwnValue *actual=own_find(cut,OWN_MAP_CUT);
        FridayPublisherPreparedRow *fd=prepared_row(row);
        if(!actual||actual->refs[1]!=birth||actual->refs[3]!=row||actual->refs[4]!=credit||
           actual->refs[7]!=mapping||actual->confirmed!=1||!fd||fd->keeper<0||
           fd->keeper_closed||fd->keeper_attempted)
            return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_exact_retained_current_cut"),NULL;
        uint64_t width_now=0,creator_now=0,pid_now=0;
        if(PyDict_Size(cut)!=14||!ascii(own_plain_field(cut,"schema"),
                "friday.sol091.selected-owned-mmap-public-cut.v1")||
           own_plain_field(cut,"role")!=actual->refs[9]||
           own_plain_field(cut,"backing_birth9")!=actual->refs[8]||
           own_plain_field(cut,"backing_cut9")!=actual->refs[5]||
           own_plain_field(cut,"actual_row")!=row||own_plain_field(cut,"actual_credit")!=credit||
           own_plain_field(cut,"full_bytes")!=actual->refs[2]||
           own_plain_field(cut,"position")!=actual->refs[6]||
           own_plain_field(cut,"closed")!=Py_False||
           own_plain_field(cut,"cohort_before_effect")!=Py_False||
           !ascii(own_plain_field(cut,"access"),"WRITE")||
           scalar_u64(own_plain_field(cut,"width"),&width_now)<0||width_now!=p->width||
           scalar_u64(own_plain_field(cut,"creator_pid"),&creator_now)<0||creator_now!=(uint64_t)p->pid||
           scalar_u64(own_plain_field(cut,"actual_pid"),&pid_now)<0||pid_now!=(uint64_t)getpid()) {
            if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"own_mapping_actual_cut_field_alias");
            return own_error(actual),NULL;
        }
        if(!p->refs[10]||!PyType_Check(p->refs[10])||Py_TYPE(mapping)!=(PyTypeObject *)p->refs[10]||
           !actual->refs[2]||!PyBytes_CheckExact(actual->refs[2])||
           (uint64_t)PyBytes_GET_SIZE(actual->refs[2])!=p->width)
            return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_actual_current_cut_stock_type"),NULL;
        if(own_before(131072)<0||FridayPublisherMasterBefore(&root_storage.pool,2*p->width,0,0,0)<0)return NULL;
        struct stat stat_now;
        if(fstat(fd->keeper,&stat_now)<0) {PyErr_SetFromErrno(PyExc_OSError);return own_error(actual),NULL;}
        PyObject *current9=stat9(&stat_now);if(!current9)return own_error(actual),NULL;
        PyObject *tuple9=PyList_AsTuple(current9);Py_DECREF(current9);
        if(!tuple9)return own_error(actual),NULL;
        int same=PyObject_RichCompareBool(tuple9,actual->refs[5],Py_EQ)==1;Py_DECREF(tuple9);
        if(!same||PyErr_Occurred()) {
            if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"own_mapping_current_keeper_drift");
            return own_error(actual),NULL;
        }
        Py_buffer view;memset(&view,0,sizeof(view));
        if(PyObject_GetBuffer(mapping,&view,PyBUF_SIMPLE)<0)return own_error(actual),NULL;
        same=view.len>=0&&(uint64_t)view.len==p->width&&view.buf&&!view.readonly&&
            !memcmp(view.buf,PyBytes_AS_STRING(actual->refs[2]),(size_t)p->width);
        PyBuffer_Release(&view);
        if(!same||PyErr_Occurred()) {
            if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"own_mapping_current_full_byte_drift");
            return own_error(actual),NULL;
        }
        PyMethodDef *tell=NULL;
        for(PyMethodDef *m=((PyTypeObject *)p->refs[10])->tp_methods;m&&m->ml_name;m++)
            if(!strcmp(m->ml_name,"tell")&&m->ml_flags==METH_NOARGS){tell=m;break;}
        PyObject *position=tell?tell->ml_meth(mapping,NULL):NULL;
        if(!position) {
            if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"own_mapping_current_check_tell");
            return own_error(actual),NULL;
        }
        same=PyLong_CheckExact(position)&&PyObject_RichCompareBool(position,actual->refs[6],Py_EQ)==1;
        Py_DECREF(position);
        if(!same||PyErr_Occurred()) {
            if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"own_mapping_current_position_drift");
            return own_error(actual),NULL;
        }
        Py_RETURN_TRUE;
    }
    if(!strcmp(action,"before-close")) {
        PyObject *raw=field(cut,"full_bytes");
        if(p->attempts!=1||!raw||!PyBytes_CheckExact(raw)||!PyTuple_CheckExact(ledgers)||
           field(cut,"actual_row")!=row||field(cut,"actual_credit")!=credit)
            return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_full_before_close_ledger"),NULL;
        own_ref(p,2,cut);own_ref(p,3,ledgers);p->width=(uint64_t)PyBytes_GET_SIZE(raw);
        p->flags=1;p->attempts=2;Py_RETURN_NONE;
    }
    if(!strcmp(action,"close")) {
        if(p->attempts>=3||(p->flags&4))return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_close_outcome_unknown"),NULL;
        if(p->attempts!=2||!(p->flags&1))return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_close_prefix"),NULL;
        p->attempts=3; /* BEFORE exactly one actual selected stock close */
        PyObject *value=PyObject_CallMethod(mapping,"close",NULL);
        if(!value){p->flags|=4;return own_error(p),NULL;}
        Py_DECREF(value);
        PyObject *closed=PyObject_GetAttrString(mapping,"closed");
        if(!closed){p->flags|=4;return own_error(p),NULL;}
        int confirmed=closed==Py_True;Py_DECREF(closed);
        if(!confirmed){p->flags|=4;return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_close_unconfirmed"),NULL;}
        p->flags|=2;p->confirmed=3;Py_RETURN_NONE;
    }
    if(!strcmp(action,"close-error")) {own_ref(p,7,cut);p->flags|=4;Py_RETURN_NONE;}
    return FridayPublisherMasterFault(&root_storage.pool,"own_mapping_unknown_transition"),NULL;
}
/* Reuse exact preowned status keys; never allocate a temporary string or
 * insert a new field after the real stock effect. Callers prepay the bounded
 * key scans/replacements in their SAME pool BEFORE invoking the provider.
 * Actual unknown ABI/provider cost remains separate qualification. */
static PyObject *own_hash_status_key(PyObject *d,const char *name) {
    if(!d||!PyDict_CheckExact(d)||PyDict_Size(d)>16||strlen(name)>32)
        return FridayPublisherMasterFault(&root_storage.pool,"own_hash_preowned_status_domain"),NULL;
    PyObject *key,*value;Py_ssize_t pos=0;size_t n=strlen(name);
    while(PyDict_Next(d,&pos,&key,&value)) {
        if(!PyUnicode_CheckExact(key)||PyUnicode_GET_LENGTH(key)>32)
            return FridayPublisherMasterFault(&root_storage.pool,"own_hash_preowned_status_key"),NULL;
        if((size_t)PyUnicode_GET_LENGTH(key)!=n)continue;
        int kind=PyUnicode_KIND(key);void *data=PyUnicode_DATA(key);size_t i=0;
        for(;i<n&&PyUnicode_READ(kind,data,(Py_ssize_t)i)==(unsigned char)name[i];i++);
        if(i==n)return key; /* borrowed; original dict or own_ref retains it */
    }
    return FridayPublisherMasterFault(&root_storage.pool,"own_hash_preowned_status_missing"),NULL;
}
static int own_hash_status_replace(PyObject *d,PyObject *key,PyObject *before,PyObject *after) {
    if(!d||!key||!after||!PyDict_CheckExact(d)||!PyUnicode_CheckExact(key))
        return FridayPublisherMasterFault(&root_storage.pool,"own_hash_preowned_status_replace");
    PyObject *old=PyDict_GetItemWithError(d,key);
    if(!old||old!=before) {
        if(PyErr_Occurred())return -1;
        return FridayPublisherMasterFault(&root_storage.pool,"own_hash_preowned_status_drift");
    }
    /* Existing exact Unicode key, same dictionary size, old None/bool value.
     * No exposed tuple is changed; originals remain in native/Source owners. */
    return PyDict_SetItem(d,key,after);
}
PyObject *FridayPublisherRootOwnHashNew(PyObject *args) {
    const char *mode;PyObject *name,*data,*ledger,*parent;
    if(!PyArg_ParseTuple(args,"sOOOO",&mode,&name,&data,&ledger,&parent)||!PyBytes_CheckExact(data)||!PyDict_CheckExact(ledger))return NULL;
    uint64_t mode_tag=!strcmp(mode,"sha256")?1:!strcmp(mode,"new")?2:!strcmp(mode,"copy")?3:0;
    if(!mode_tag||own_plain_field(ledger,"constructor_bytes")!=data||
       own_plain_field(ledger,"name")!=name||
       !ascii(own_plain_field(ledger,"mode"),mode))
        return FridayPublisherMasterFault(&root_storage.pool,"own_hash_actual_constructor_operands"),NULL;
    FridayPublisherOwnValue *p=own_birth(OWN_HASH);if(!p)return NULL;
    own_ref(p,1,ledger);own_ref(p,4,name);own_ref(p,5,data);p->attempts=0;
    p->flags=mode_tag<<8; /* original actual factory selection, not later name */
    if(FridayPublisherMasterBefore(&root_storage.pool,24576,0,0,0)<0)return own_error(p),NULL;
    if(PyDict_Size(ledger)!=14||!ascii(own_plain_field(ledger,"schema"),
            "friday.astra256.own-hash-input-ledger.v2")||
       own_plain_field(ledger,"runtime_h")!=Py_None||
       own_plain_field(ledger,"constructed")!=Py_False||
       own_plain_field(ledger,"actual_attempted")!=Py_False||
       own_plain_field(ledger,"original_error")!=Py_None||
       own_plain_field(ledger,"metadata_error")!=Py_None||
       own_plain_field(ledger,"state_complete")!=Py_False)
        return FridayPublisherMasterFault(&root_storage.pool,"own_hash_initial_status_slots"),NULL;
    own_ref(p,9,own_hash_status_key(ledger,"runtime_h"));
    if(!p->refs[9])return own_error(p),NULL;
    own_ref(p,10,own_hash_status_key(ledger,"constructed"));
    if(!p->refs[10])return own_error(p),NULL;
    own_ref(p,11,own_hash_status_key(ledger,"actual_attempted"));
    if(!p->refs[11])return own_error(p),NULL;
    PyObject *factory=NULL;
    if(!strcmp(mode,"copy")) {
        FridayPublisherOwnValue *old=own_find(parent,OWN_HASH);
        if(!old)return FridayPublisherMasterFault(&root_storage.pool,"own_hash_original_copy_parent"),NULL;
        PyObject *prior_ops=own_plain_field(old->refs[1],"updates");
        PyObject *actual_parent=own_plain_field(ledger,"parent");
        if(!prior_ops||!PyList_CheckExact(prior_ops)||!actual_parent||actual_parent==Py_None)
            return FridayPublisherMasterFault(&root_storage.pool,"own_hash_actual_copy_journal"),NULL;
        own_ref(p,3,parent);own_ref(p,7,actual_parent);
        p->refs[8]=PyObject_GenericGetDict(actual_parent,NULL);
        if(!p->refs[8]||own_plain_field(p->refs[8],"h")!=parent||
           own_plain_field(p->refs[8],"own_state")!=old->refs[1]) {
            if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"own_hash_actual_parent_wrapper_alias");
            return own_error(p),NULL;
        }
        p->width=1+(uint64_t)PyList_GET_SIZE(prior_ops);
        if(old->flags&4)p->flags|=4; /* preserve original stock copy, uncertainty sticky */
        PyObject *copy_count=own_plain_field(ledger,"parent_operations_at_copy");
        unsigned long long count=copy_count&&PyLong_CheckExact(copy_count)?PyLong_AsUnsignedLongLong(copy_count):0;
        if(PyErr_Occurred())return own_error(p),NULL;
        if(count!=p->width)return FridayPublisherMasterFault(&root_storage.pool,"own_hash_actual_copy_prefix"),NULL;
        factory=PyObject_GetAttrString(parent,"copy");
    } else {
        own_ref(p,6,root_storage.own_values.hash_module);
        factory=Py_XNewRef(!strcmp(mode,"sha256")?root_storage.own_values.hash_sha256:root_storage.own_values.hash_new);
    }
    if(!factory)return own_error(p),NULL;p->refs[2]=factory;
    if(own_hash_status_replace(ledger,p->refs[11],Py_False,Py_True)<0)return own_error(p),NULL;
    p->attempts=1; /* actual constructor/copy effect, not pre-factory failure */
    p->refs[0]=!strcmp(mode,"copy")?PyObject_CallNoArgs(factory):
        !strcmp(mode,"sha256")?PyObject_CallFunctionObjArgs(factory,data,NULL):
        PyObject_CallFunctionObjArgs(factory,name,data,NULL);
    if(!p->refs[0]){p->flags|=4;return own_error(p),NULL;}
    p->flags|=1;p->confirmed=1;
    /* Actual returned strong object and outcome precede Source self.h and
     * bookkeeping. Failure of later Source assignments cannot undo these. */
    if(own_hash_status_replace(ledger,p->refs[9],Py_None,p->refs[0])<0||
       own_hash_status_replace(ledger,p->refs[10],Py_False,Py_True)<0)
        return own_error(p),NULL;
    return Py_NewRef(p->refs[0]);
}
PyObject *FridayPublisherRootOwnHashUpdate(PyObject *args) {
    PyObject *hash,*data,*operation;
    if(!PyArg_ParseTuple(args,"OOO",&hash,&data,&operation)||!PyBytes_CheckExact(data)||!PyDict_CheckExact(operation))return NULL;
    FridayPublisherOwnValue *p=own_find(hash,OWN_HASH);
    if(!p)
        return FridayPublisherMasterFault(&root_storage.pool,"own_hash_actual_original_before_update"),NULL;
    /* A failed update does not prohibit a later ORIGINAL stock call. Full
     * input/errors stay retained and flags4 remains sticky after later return. */
    if(own_before((uint64_t)PyBytes_GET_SIZE(data)+131072)<0)return own_error(p),NULL;
    /* Operation/full input was appended to SAME retained ledger BEFORE call. */
    PyObject *ops=field(p->refs[1],"updates");
    if(!ops||!PyList_CheckExact(ops)||!PyList_GET_SIZE(ops)||
       PyList_GET_ITEM(ops,PyList_GET_SIZE(ops)-1)!=operation||field(operation,"full_bytes")!=data)
        return FridayPublisherMasterFault(&root_storage.pool,"own_hash_before_effect_same_full_ledger"),NULL;
    if(FridayPublisherMasterBefore(&root_storage.pool,24576,0,0,0)<0)return own_error(p),NULL;
    if(PyDict_Size(operation)!=7||own_plain_field(operation,"attempted")!=Py_True||
       own_plain_field(operation,"actual_attempted")!=Py_False||
       own_plain_field(operation,"confirmed")!=Py_False||
       own_plain_field(operation,"original_error")!=Py_None||
       own_plain_field(operation,"metadata_error")!=Py_None)
        return FridayPublisherMasterFault(&root_storage.pool,"own_hash_initial_update_status"),NULL;
    PyObject *actual_key=own_hash_status_key(operation,"actual_attempted");
    PyObject *confirmed_key=actual_key?own_hash_status_key(operation,"confirmed"):NULL;
    if(!actual_key||!confirmed_key)return own_error(p),NULL;
    if(own_hash_status_replace(operation,actual_key,Py_False,Py_True)<0)return own_error(p),NULL;
    Py_INCREF(confirmed_key); /* exact original key held across stock call */
    p->attempts++;PyObject *value=PyObject_CallMethod(hash,"update","O",data);
    if(!value){p->flags|=4;own_error(p);Py_DECREF(confirmed_key);return NULL;}
    p->confirmed++;
    int published=own_hash_status_replace(operation,confirmed_key,Py_False,Py_True);
    Py_DECREF(confirmed_key);Py_DECREF(value);
    if(published<0)return own_error(p),NULL;
    Py_RETURN_NONE;
}
PyObject *FridayPublisherRootOwnHashBody(PyObject *o) {
    FridayPublisherOwnValue *p=own_find(o,OWN_HASH);
    if(!p||!p->confirmed)Py_RETURN_NONE; /* full error-prefix ledger is still OWN data */
    return Py_NewRef(p->refs[1]);
}

static PyObject *loader_find_spec(PyObject *self,PyObject *args) {
    PyObject *fullname,*path=Py_None,*target=Py_None;
    if(!PyArg_ParseTuple(args,"O|OO",&fullname,&path,&target))return NULL;
    const char *name=PyUnicode_CheckExact(fullname)?PyUnicode_AsUTF8(fullname):NULL;
    RootHeldFile *h=name?source_module_bytes(name):NULL;
    if(!h)Py_RETURN_NONE;
    PyObject *machinery=PyImport_ImportModule("importlib.machinery");if(!machinery)return NULL;
    PyObject *type=PyObject_GetAttrString(machinery,"ModuleSpec");Py_DECREF(machinery);
    PyObject *spec=type?PyObject_CallFunctionObjArgs(type,fullname,self,NULL):NULL;Py_XDECREF(type);
    if(spec&&PyObject_SetAttrString(spec,"origin",field(h->pin,"path"))<0){Py_DECREF(spec);return NULL;}
    return spec;
}
static PyObject *loader_create_module(PyObject *self,PyObject *spec){Py_RETURN_NONE;}
static PyObject *loader_exec_module(PyObject *self,PyObject *module) {
    PyObject *name=PyObject_GetAttrString(module,"__name__");
    const char *text=name?PyUnicode_AsUTF8(name):NULL;RootHeldFile *h=text?source_module_bytes(text):NULL;
    if(!h||!root_storage.signed_admission||!root_storage.snapshot_verified||
       !current_enrollment_matches(root_storage.root_fact,root_storage.qualification))
        {Py_XDECREF(name);FridayPublisherMasterFault(&root_storage.pool,"source_loader_current_admission");return NULL;}
    struct stat named;const char *file=PyUnicode_AsUTF8(field(h->pin,"path"));
    if(!file||lstat(file,&named)<0||!stat_equal(&named,&h->birth,1))
        {Py_DECREF(name);FridayPublisherMasterFault(&root_storage.pool,"source_loader_full_byte_custody");return NULL;}
    if(FridayPublisherMasterBefore(&root_storage.pool,PyBytes_Size(h->raw),0,0,
        (uint64_t)PyBytes_Size(h->raw)*300+131072)<0){Py_DECREF(name);return NULL;}
    /* Own partial module BEFORE any fallible Source birth/compile/evaluation.
     * Actual interpreter factory bounds remain C2 UNKNOWN_NOT_ZERO. */
    if(PyDict_SetItem(root_storage.source_modules,name,module)<0){Py_DECREF(name);return NULL;}
    FridayPublisherOwnValue *binding=own_binding(module,h->pin,h->raw);
    if(!binding){Py_DECREF(name);return NULL;}
    PyObject *dict=PyModule_GetDict(module);
    if(PyDict_SetItemString(dict,"__file__",field(h->pin,"path"))<0){Py_DECREF(name);return NULL;}
    PyObject *code=Py_CompileString(PyBytes_AsString(h->raw),file,Py_file_input);
    if(!code){Py_DECREF(name);return NULL;}
    if(PyDict_SetItem(root_storage.source_codes,name,code)<0){Py_DECREF(code);Py_DECREF(name);return NULL;}
    own_ref(binding,3,code);binding->attempts=1;
    FridayPublisherOwnValue *factory_scope=NULL;
    if(own_class_factory_install(module,binding,&factory_scope)<0){
        Py_DECREF(code);Py_DECREF(name);return own_error(binding),NULL;
    }
    factory_scope->flags|=CLASS_EVAL_ATTEMPT;factory_scope->attempts=1;
    PyObject *value=PyEval_EvalCode(code,dict,dict);
    if(value){factory_scope->refs[8]=value;factory_scope->flags|=CLASS_EVAL_RETURN;own_ref(binding,4,value);}
    else {
        if(!PyErr_Occurred())FridayPublisherMasterFault(&root_storage.pool,"Source_eval_NULL_without_original_error");
        own_error(binding);
    }
    int restored=own_class_factory_restore(factory_scope);
    Py_DECREF(code);Py_DECREF(name);
    if(restored!=0||!value||PyErr_Occurred()||factory_scope->error_saved){own_error(binding);return NULL;}
    binding->flags=1;binding->confirmed=1;
    if(own_types(binding)<0)return FridayPublisherMasterFault(&root_storage.pool,"Source_type_birth_capacity"),NULL;
    Py_RETURN_NONE;
}
static PyMethodDef loader_methods[]={
    {"find_spec",loader_find_spec,METH_VARARGS,NULL},
    {"create_module",loader_create_module,METH_O,NULL},
    {"exec_module",loader_exec_module,METH_O,NULL},{NULL,NULL,0,NULL}};
static PyTypeObject root_loader_type={
    PyVarObject_HEAD_INIT(NULL,0)
    .tp_name="friday.same-root.retained-byte-loader",
    .tp_basicsize=sizeof(RootByteLoader),.tp_flags=Py_TPFLAGS_DEFAULT,.tp_methods=loader_methods
};
static int install_source_loader(void) {
    FridayPublisherRootStorage *s=&root_storage;
    if(!s->signed_admission||!s->snapshot_verified)return -1;
    PyObject *pins=field(s->enrollment,"source_files");
    if(!pins||!PyList_CheckExact(pins))return -1;
    Py_ssize_t pin_count=PyList_Size(pins);
    if(pin_count<0||(uint64_t)pin_count>FRIDAY_ROOT_HELD_FILES)return -1;
    /* Prospective name/list retention in the SAME pool, not a measured
     * whole loader/provider allocation bound or a fresh cleanup reserve. */
    if(FridayPublisherMasterBefore(&s->pool,0,0,0,131072+(uint64_t)pin_count*1024)<0)return -1;
    PyObject *native_modules=PyImport_GetModuleDict();if(!native_modules)return -1;
    /* The cold entry already installed the exact preowned builtin table.
     * Import its real builtin once; do not construct an unrelated module
     * beside that provider or later overwrite a registered instance. */
    if(!command_has_owned_runtime(s)||!final_caller_binding.caller||
       !final_caller_binding.caller->builtin_installed)return -1;
    s->own_values.early_module=PyImport_ImportModule("publisher_owned_custody");
    if(!s->own_values.early_module)return -1;
    if(!FridayPublisherOwnedModuleOriginal(s->own_values.early_module))
        return FridayPublisherMasterFault(&s->pool,"actual_cold_builtin_provider_required");
    s->own_values.token_missing=PyObject_GetAttrString((PyObject *)&PyContextToken_Type,"MISSING");
    if(!s->own_values.token_missing)return -1;
    /* Genuine stock producer dependencies captured BEFORE any Source module,
     * not accepted from a Source-mutated sys.modules/name/function binding.
     * Selected image/API and all implicit import costs remain NOT_QUALIFIED. */
    if(FridayPublisherMasterBefore(&s->pool,0,0,0,6*131072)<0)return -1;
    s->own_values.mapping_module=PyImport_ImportModule("mmap");
    if(!s->own_values.mapping_module||Py_TYPE(s->own_values.mapping_module)!=&PyModule_Type)return -1;
    s->own_values.mapping_factory=PyObject_GetAttrString(s->own_values.mapping_module,"mmap");
    s->own_values.mapping_access=PyObject_GetAttrString(s->own_values.mapping_module,"ACCESS_WRITE");
    if(!s->own_values.mapping_factory||!PyType_Check(s->own_values.mapping_factory)||
       !s->own_values.mapping_access||!PyLong_CheckExact(s->own_values.mapping_access))return -1;
    s->own_values.hash_module=PyImport_ImportModule("hashlib");
    if(!s->own_values.hash_module||Py_TYPE(s->own_values.hash_module)!=&PyModule_Type)return -1;
    s->own_values.hash_sha256=PyObject_GetAttrString(s->own_values.hash_module,"sha256");
    s->own_values.hash_new=PyObject_GetAttrString(s->own_values.hash_module,"new");
    if(!s->own_values.hash_sha256||!s->own_values.hash_new)return -1;
    s->source_modules=PyDict_New();if(!s->source_modules)return -1;
    s->source_codes=PyDict_New();if(!s->source_codes)return -1;
    if(PyType_Ready(&root_loader_type)<0)return -1;
    s->source_loader=(PyObject *)PyObject_New(RootByteLoader,&root_loader_type);if(!s->source_loader)return -1;
    s->loader_sys=PyImport_ImportModule("sys");if(!s->loader_sys)return -1;
    s->loader_meta=PyObject_GetAttrString(s->loader_sys,"meta_path");if(!s->loader_meta)return -1;
    if(!PyList_CheckExact(s->loader_meta))return FridayPublisherMasterFault(&s->pool,"actual_root_meta_path");
    s->loader_names=PyList_New(0);if(!s->loader_names)return -1;
    PyObject *modules=PyImport_GetModuleDict();if(!modules)return -1;
    for(Py_ssize_t i=0;i<pin_count;i++) {
        PyObject *pin=PyList_GetItem(pins,i),*relative=field(pin,"relative_path");
        if(!relative||!PyUnicode_CheckExact(relative))return -1;
        const char *rel=PyUnicode_AsUTF8(relative);
        if(!rel)return -1;
        size_t n=strlen(rel);
        if(n>10&&!strncmp(rel,"source/",7)&&!strcmp(rel+n-3,".py")) {
            char name[256];size_t len=n-10;if(len>=sizeof(name))return -1;
            memcpy(name,rel+7,len);name[len]=0;
            /* The previous exact name remains strongly owned by loader_names.
             * PyDict_GetItemString can suppress allocation/lookup errors, so
             * use the error-preserving API with this already owned name. */
            Py_CLEAR(s->loader_pending_name);
            s->loader_pending_name=PyUnicode_FromString(name);if(!s->loader_pending_name)return -1;
            if(PyList_Append(s->loader_names,s->loader_pending_name)<0)return -1;
            PyObject *old=PyDict_GetItemWithError(modules,s->loader_pending_name);
            if(PyErr_Occurred())return -1;
            if(old)return FridayPublisherMasterFault(&s->pool,"Source_module_collision");
        }
    }
    s->loader_install_attempted=1;
    int rc=PyList_Insert(s->loader_meta,0,s->source_loader);
    if(rc==0)s->loader_installed=1;
    return rc;
}
static int prepare_native_final_endpoint(void) {
    FridayPublisherRootStorage *s=&root_storage;
    PyObject *output_path=field(s->enrollment,"output_root");
    if(!output_path||!PyUnicode_CheckExact(output_path))return -1;
    const char *path=PyUnicode_AsUTF8(output_path);if(!path)return -1;
    if(FridayPublisherMasterBefore(&s->pool,0,0,0,65536)<0)return -1;
    s->output_root_open_attempted=1;
    RootHeldFile *dir=open_absolute_held(path,O_RDONLY|O_DIRECTORY,0);
    if(!dir||!dir->body_close.birth_valid) {
        s->output_root_fd=-1;s->output_root_open_rc=-1;s->output_root_open_errno=errno;return -1;
    }
    /* Adopt the walk generation and keeper. No second dup and no second slot. */
    s->output_root_fd=dir->fd;s->output_root_open_rc=dir->fd;s->output_root_open_errno=0;
    s->output_root_stat_attempted=dir->birth_stat_attempted;
    s->output_root_stat_rc=dir->birth_stat_rc;s->output_root_stat_errno=dir->birth_stat_errno;
    s->output_root_identity=dir->birth;
    s->output_keeper=dir->keeper;s->output_keeper_valid=dir->live_keeper&&dir->keeper>=0;
    s->output_owner_close=dir->body_close;
    dir->close_alias=1;dir->alias_kind=3;dir->alias_generation=dir->generation;
    if((s->output_root_identity.st_mode&0777)!=0700)
        return FridayPublisherMasterFault(&s->pool,"original_native_output_root");
    s->final_open_attempted=1;
    s->final_fd=openat(s->output_root_fd,"original-native-terminal",O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    s->final_open_rc=s->final_fd;s->final_open_errno=s->final_fd<0?errno:0;
    if(s->final_fd<0){PyErr_SetFromErrno(PyExc_OSError);return -1;}s->pool.native_live_slots++;
    s->bindings.final_fd=s->final_fd;
    s->final_stat_attempted=1;
    s->final_stat_rc=fstat(s->final_fd,&s->final_identity_native);
    s->final_stat_errno=s->final_stat_rc<0?errno:0;
    if(s->final_stat_rc<0)return -1;
    if(s->pool.native_live_slots>=SLOT_CAP)return FridayPublisherMasterFault(&s->pool,"final_keeper_original_slots");
    s->final_keeper=fcntl(s->final_fd,F_DUPFD_CLOEXEC,3);
    if(s->final_keeper<0)return -1;
    s->pool.native_live_slots++;s->final_keeper_valid=1;
    s->final_owner_close.fd=s->final_fd;s->final_owner_close.keeper=s->final_keeper;
    s->final_owner_close.generation=1;s->final_owner_close.birth=s->final_identity_native;
    s->final_owner_close.birth_valid=1;
    s->final_identity=stat9(&s->final_identity_native);if(!s->final_identity)return -1;
    s->final_close_attempt=Py_BuildValue("{s:i,s:s,s:O}","attempt",1,"status","NOT_ATTEMPTED","error",Py_None);
    if(!s->final_close_attempt)return -1;
    s->final_close_history=PyList_New(0);if(!s->final_close_history)return -1;
    s->final_slot=PyLong_FromLong(1);if(!s->final_slot)return -1;
    s->final_rows=PyDict_New();if(!s->final_rows)return -1;
    s->bindings.final_fd_row=Py_BuildValue("{s:i,s:s,s:i,s:s,s:O,s:i,s:i,s:O,s:O,s:O}",
        "fd",s->final_fd,"holder","original-native-terminal","credit",0,"status","HELD",
        "identity9_decimal_strings",s->final_identity,"slot",1,"generation",1,"attempted_close",Py_None,
        "close_history",s->final_close_history,"close_cell",s->final_close_attempt);
    if(!s->bindings.final_fd_row)return -1;
    if(PyDict_SetItem(s->final_rows,s->final_slot,s->bindings.final_fd_row)<0)return -1;
    s->bindings.final_fd_credit=Py_BuildValue("{s:O,s:i,s:i,s:O}",
        "fd_rows",s->final_rows,"token",0,"slots",1,"closed",Py_False);
    if(!s->bindings.final_fd_credit)return -1;
    return 0;
}
static int root_terminal_reader(FridayPublisherRootTerminal *t) {
    FridayPublisherCallerResult *c=&t->result;
    /* A failed Handback has a DIFFERENT actual owner than a pre-Run refusal.
     * Keep that original Run/error/FD graph intact. Never certify the seven
     * entry roots as the complete contents of the full current Run inventory. */
    if(!t->received||c->untransferred_run||
       (c->failure_handback_attempted&&!c->failure_handback_confirmed))return -1;
    if(c->full_packet) {
        if(c->state!=FRIDAY_CALLER_PACKET_HELD||!c->full_packet_readback||
           !c->run_references_transferred||c->failure.received||
           c->failure_handback_attempted)return -1;
        if(FridayPublisherCallerPacketBytes(c->full_packet,1)<0)return -1;
        PyObject *banks=PyTuple_GetItem(c->full_packet,5);
        for(Py_ssize_t i=0;i<PyTuple_Size(banks);i++) {
            PyObject *row=PyTuple_GetItem(banks,i);
            t->full_banks_read++;t->full_bank_bytes+=(uint64_t)PyBytes_Size(PyTuple_GetItem(row,1));
        }
        /* Exact full tuple13 and every current Run-root/alias object belong to
         * this SAME Root terminal, not a held Run pointer or an exit code. */
        t->readback_kind=FRIDAY_ROOT_READBACK_FINAL_PACKET;
        return 0;
    }
    if(c->failure.received) {
        if(c->state!=FRIDAY_CALLER_ERROR_HELD_STOP_UNCONFIRMED||
           !c->failure_handback_attempted||!c->failure_handback_confirmed||
           !c->run_references_transferred)return -1;
        FridayPublisherFailureHandback *f=&c->failure;
        Py_ssize_t at=0;FridayPublisherFailureBankView bank;int rc;
        while((rc=FridayPublisherFailureBankNext(f,&at,&bank))==1) {
            uint64_t width;if(scalar_u64(bank.actual_width,&width)<0)return -1;
            uint64_t parts=0;
            for(Py_ssize_t i=0;i<PyList_Size(bank.full_parts);i++) {
                PyObject *part=PyList_GetItem(bank.full_parts,i);
                if(!PyBytes_CheckExact(part)||plus(&parts,(uint64_t)PyBytes_Size(part))<0)return -1;
                t->partial_parts_read++;t->full_bank_bytes+=PyBytes_Size(part);
                /* Access COMPLETE actual bytes, not a width-only acceptance. */
                const char *raw=PyBytes_AsString(part);unsigned char seen=0;
                for(Py_ssize_t j=0;j<PyBytes_Size(part);j++)seen|=(unsigned char)raw[j];
                t->raw_reader_sink^=seen;
            }
            if(parts!=width)return -1;
            if(bank.full_raw_or_none!=Py_None) {
                if((uint64_t)PyBytes_Size(bank.full_raw_or_none)!=width)return -1;
                uint64_t pos=0;
                for(Py_ssize_t i=0;i<PyList_Size(bank.full_parts);i++) {
                    PyObject *part=PyList_GetItem(bank.full_parts,i);size_t n=PyBytes_Size(part);
                    if(memcmp(PyBytes_AsString(bank.full_raw_or_none)+pos,PyBytes_AsString(part),n))return -1;pos+=n;
                }
            }
            /* Aliases, possibly incomplete, stay strong in the exact full bank;
             * failure is NEVER relabeled a sealed/complete successful bank. */
            t->full_banks_read++;
        }
        if(rc<0)return -1;
        for(uint64_t i=0;i<f->close_count;i++) {
            FridayPublisherFailureCloseView close;
            if(FridayPublisherFailureCloseAt(f,i,&close)!=1)return -1;
            if(!close.actual_called||close.syscall_rc||!close.publication_confirmed)t->remaining_original_owners=1;
        }
        for(int i=0;i<FRIDAY_PUBLISHER_RUN_ROOTS;i++)if(f->roots[i])t->owned_failure_roots++;
        t->readback_kind=FRIDAY_ROOT_READBACK_FAILURE_HANDBACK;
        return 0;
    }
    /* Allocation/admission-before-Run refusal: own all actual supplied seven
     * roots and the unnormalized original triple. Missing roots stay NULL. */
    if(c->state!=FRIDAY_CALLER_ERROR_HELD_STOP_UNCONFIRMED||
       c->run_references_transferred||c->failure_handback_attempted||
       (c->owned_invoke_attempted&&c->invoke_refusal==FRIDAY_INVOKE_PREFLIGHT_OK))return -1;
    for(int i=0;i<7;i++)if(c->before_entry_roots[i])t->owned_entry_roots++;
    t->readback_kind=FRIDAY_ROOT_READBACK_BEFORE_RUN;
    return 0;
}
int FridayPublisherRootPerform(const char *case_id,const FridayPublisherRootTerminal **out) {
    if(!FridayPublisherRootBankAttached())return -1;
    FridayPublisherRootStorage *s=&root_storage;FridayPublisherRootTerminal *t=&s->terminal;
    if(!out||s->attempted)return -1;*out=t;s->attempted=1;t->attempted=1;t->pool=&s->pool;
    s->output_root_fd=s->final_fd=s->bootstrap_pid=s->bootstrap_pidfd=-1;
    s->bindings.final_fd=-1;
    s->final_keeper=s->output_keeper=-1;
    /* Static full Root/caller/error storage precedes any fallible birth.
     * Its real compiler/runtime/provider overhead is UNKNOWN_NOT_ZERO. */
    /* For the cold caller this was charged before preinitialization. Never
     * reset its elapsed time, counters, original fault or static accounting. */
    if(!s->cold_pool_started) {
        s->pool.pid=getpid();s->pool.native_allocation=sizeof(*s);
    }
    if(!Py_IsInitialized()) {
        t->entry_refusal=FRIDAY_ROOT_ENTRY_RUNTIME_UNINITIALIZED;
        t->phase="existing_Root_stock_runtime_NOT_INITIALIZED_CODE";
        t->remaining_original_owners=1;return 0;
    }
    /* A pending provider error predates our first fallible Python call. Own
     * that EXACT triple, without constructing another error or importing. */
    if(PyErr_Occurred()) {
        t->entry_refusal=FRIDAY_ROOT_ENTRY_PENDING_ERROR;
        t->phase="Root-entry-original-pending-error";goto initial_failure;
    }
    if(!case_id) {
        t->entry_refusal=FRIDAY_ROOT_ENTRY_MISSING_CASE;
        t->phase="Root-entry-missing-case-before-effects";goto initial_failure;
    }
    if(s->cold_pool_started) {
        uint64_t current_ns;
        if(!FridayPublisherMasterOwns(&s->pool)||s->pool.refused||s->pool.observation_unknown||
           FridayPublisherMasterWorkClock(&s->pool,s->pool.work_deadline_ns,
               FRIDAY_CLOCK_COLD_ENTRY,&current_ns)<0) {
            t->phase="Root-cold-original-pool-refused-or-expired";goto initial_failure;
        }
    } else if(root_pool_start(s,0)<0) {
        t->entry_refusal=FRIDAY_ROOT_ENTRY_CLOCK_UNAVAILABLE;
        t->phase="Root-entry-finite-clock-unavailable";goto initial_failure;
    }
    t->phase="Root-original-case-owned-copy";
    if(FridayPublisherRootCaseCopy(&s->pool,&s->case_input,case_id)<0)goto initial_failure;
    t->phase="Root-preowned-refusal-value";
    /* SAME original pool, before the first post-preflight Python factory.
     * Storage itself is already in sizeof RootStorage. This covers bounded
     * explicit byte/graph scans, NOT qualified implicit whole native costs.
     * A refused early pool can still MOVE custody, never invent read credit. */
    if(FridayPublisherMasterBefore(&s->pool,
        8ULL*FRIDAY_ROOT_COMMAND_BYTES+
        sizeof(s->bootstrap)+sizeof(s->image_inventory)+
        8ULL*sizeof(s->command_receipt.nodes)+4ULL*sizeof(s->command_receipt.edges)+
        4ULL*sizeof(s->command_receipt.segments)+
        8ULL*sizeof(s->command_receipt.owners)+
        4ULL*sizeof(s->own_values)+
        (uint64_t)FRIDAY_ROOT_COMMAND_OWNERS*
            (sizeof(FridayPublisherRootCommandOwner)+sizeof(FridayPublisherRootValueNode)),0,0,0)<0)
        goto initial_failure;
    s->command_read_reserved=1;
    s->pool.refusal_type=Py_NewRef(PyExc_RuntimeError);
    s->pool.refusal_value=PyObject_CallFunction(PyExc_RuntimeError,"s","original Publisher master pool refused");
    if(!s->pool.refusal_value)goto initial_failure;
    t->phase="protected-enrollment";
    s->json_module=PyImport_ImportModule("json");if(!s->json_module)goto initial_failure;
    s->json_loads=PyObject_GetAttrString(s->json_module,"loads");if(!s->json_loads)goto initial_failure;
    s->re_module=PyImport_ImportModule("re");if(!s->re_module)goto initial_failure;
    s->re_fullmatch=PyObject_GetAttrString(s->re_module,"fullmatch");if(!s->re_fullmatch)goto initial_failure;
    s->json_pairs=PyCFunction_New(&pairs_def,NULL);if(!s->json_pairs)goto initial_failure;
    s->json_constant=PyCFunction_New(&constant_def,NULL);if(!s->json_constant)goto initial_failure;
    if(protected_enrollment_native()<0||actual_root_native()<0||source_snapshot_native()<0)goto initial_failure;
    t->phase="one-real-preSource-independent-signature";
    int bootstrap_rc=native_bootstrap_signature();
    boot_read_existing_parent(t); /* BOTH outcomes, full bytes BEFORE Source */
    if(bootstrap_rc<0) {
        if(s->bootstrap_record)t->bootstrap_observation=Py_NewRef(s->bootstrap_record);
        goto initial_failure;
    }
    /* This entry may run only after independent Source/Root and selected
     * image/ABI admission through the existing external launch path.
     * Neither this record nor a hardcoded author NOT_RUN boolean is that
     * authority. At runtime join actual stock lifetime/captures/FD custody;
     * a successful exec cannot supply a returned-native transport end. */
    if(!s->bootstrap.public_stock_parent_legal_retirement_accounted||
       s->bootstrap.lifetime_phase!=2||s->bootstrap.child_transport_end_confirmed||
       !s->bootstrap.stock_image_replacement_confirmed||
       !s->bootstrap.public_stock_observables_consumed||
       !s->bootstrap.kernel_process_lifetime_ended) {
        if(s->bootstrap_record)t->bootstrap_observation=Py_NewRef(s->bootstrap_record);
        FridayPublisherMasterFault(&s->pool,"bootstrap_public_stock_parent_legal_retirement_UNCONFIRMED");
        goto initial_failure;
    }
    s->admission=parse_json(s->admission_file);
    if(!s->admission||bind_admission_fields()<0||readonly_image_native()<0)goto initial_failure;
    uint64_t qualification_timestamp=now_ns();
    if(!qualification_timestamp)goto initial_failure;
    s->qualification=Py_BuildValue("{s:O,s:O,s:O,s:O,s:K,s:O,s:O}",
        "raw_ref",s->admission_file->pin,"signature_ref",s->signature_file->pin,"key_ref",s->key_file->pin,
        "actual_signature_execution",s->bootstrap_record,"qualified_at_ns",qualification_timestamp,
        "root_fact",s->root_fact,"source_issued_grant",Py_False);
    if(!s->qualification||prepare_native_final_endpoint()<0)goto initial_failure;
    s->bindings.root_fact=s->root_fact;s->bindings.qualification=s->qualification;
    RootHeldFile *tool=held_by_pin(field(s->enrollment,"root_tool"));
    s->bindings.held_root_tool_preimage=tool?tool->raw:NULL;
    s->bindings.existing_envelope=&s->pool;s->bindings.before=FridayPublisherMasterBefore;
    s->bindings.original_master_pool=&s->pool;
    s->bindings.preowned_refusal_type=s->pool.refusal_type;s->bindings.preowned_refusal_value=s->pool.refusal_value;
    s->bindings.root_ram_remaining=RAM_CAP;s->bindings.document_limit=INPUT_CAP;
    s->bindings.body_limit=BODY_CAP;s->bindings.event_limit=262144;
    s->bindings.current_enrollment_matches=current_enrollment_matches;
    if(!s->bindings.held_root_tool_preimage||install_source_loader()<0)goto initial_failure;
    t->phase="actual-owned-byte-Source-entry-load";
    s->source_entry_module=PyImport_ImportModule("root_tool_adapter");if(!s->source_entry_module)goto initial_failure;
    s->source_entry=PyObject_GetAttrString(s->source_entry_module,"independently_invoked_root_tool");
    if(!s->source_entry)goto initial_failure;
    t->phase="actual-owned-case-UTF8-and-tuple";
    /* Preserve the actual first returned Unicode owner BEFORE tuple birth.
     * Both complete copies and UTF8 cache/tuple overlap are admitted before
     * their factories. The existing source case selection/oracles are intact. */
    if(!s->case_input.complete||s->case_input.bytes>INPUT_CAP||
       FridayPublisherMasterBefore(&s->pool,10*s->case_input.bytes+4096,0,0,
           8*s->case_input.bytes+4096)<0)goto initial_failure;
    s->source_case=PyUnicode_DecodeUTF8(s->case_input.body,
        (Py_ssize_t)s->case_input.bytes,"strict");
    if(!s->source_case)goto initial_failure;
    Py_ssize_t encoded_bytes=0;
    const char *encoded=PyUnicode_AsUTF8AndSize(s->source_case,&encoded_bytes);
    if(!encoded||encoded_bytes<0||(uint64_t)encoded_bytes!=s->case_input.bytes||
       memcmp(encoded,s->case_input.body,(size_t)s->case_input.bytes)) {
        if(!PyErr_Occurred())FridayPublisherMasterFault(&s->pool,"actual_case_UTF8_full_join");
        goto initial_failure;
    }
    s->source_args=PyTuple_New(1);if(!s->source_args)goto initial_failure;
    /* This fresh tuple is not exposed until it is fully initialized. */
    PyTuple_SET_ITEM(s->source_args,0,Py_NewRef(s->source_case));
    s->case_tuple_alias_verified=PyTuple_GET_ITEM(s->source_args,0)==s->source_case;
    if(!s->case_tuple_alias_verified)goto initial_failure;
    /* All entry/args/provider/final row originals are preowned by THIS Root
     * before actual invocation. This is the real call site, not another
     * forwarding function requiring missing callbacks from Source. */
    t->phase="actual-CallOriginal";const FridayPublisherCallerResult *received=NULL;t->call_attempted=1;
    int rc=FridayPublisherCallerCallOriginal(&s->bindings,s->source_entry,s->source_args,&received);
    if(rc<0||!received||FridayPublisherCallerMoveToRoot(&t->result)<0)goto initial_failure;
    t->received=1;t->phase="both-outcome-full-original-reader";
    /* Refusal to MOVE the actual Run is not a before-Run empty result. Do not
     * inspect/charge a projected seven-root view and then call it complete. */
    if(t->result.untransferred_run||
       (t->result.failure_handback_attempted&&!t->result.failure_handback_confirmed)) {
        t->phase="original_Run_handback_UNCONFIRMED";goto initial_failure;
    }
    t->root_fact=Py_NewRef(s->root_fact);t->qualification=Py_NewRef(s->qualification);
    t->enrollment=Py_NewRef(s->enrollment);t->admission=Py_NewRef(s->admission);
    t->role_schema=Py_NewRef(s->role_schema);t->entry=Py_NewRef(s->source_entry);t->args=Py_NewRef(s->source_args);
    t->bootstrap_observation=Py_NewRef(s->bootstrap_record);
    /* Prospective full native readback BEFORE touching actual banks/parts.
     * Both success and failure retain all original owners if debit refuses. */
    uint64_t readback=t->result.failure.bank_total,success_bytes=0;
    if(t->result.full_packet) {
        if(t->result.state!=FRIDAY_CALLER_PACKET_HELD||!t->result.full_packet_readback||
           !t->result.run_references_transferred||!PyTuple_CheckExact(t->result.full_packet)||
           PyTuple_Size(t->result.full_packet)!=13)goto initial_failure;
        PyObject *cost=PyTuple_GetItem(t->result.full_packet,12);
        if(!cost||!PyTuple_CheckExact(cost)||PyTuple_Size(cost)!=3||
           scalar_u64(PyTuple_GetItem(cost,1),&success_bytes)<0||
           plus(&readback,success_bytes)<0)goto initial_failure;
    }
    if(readback>UINT64_MAX/6||FridayPublisherMasterBefore(&s->pool,6*readback,0,0,131072)<0||
       root_terminal_reader(t)<0)goto initial_failure;
    t->full_readback=1;
    /* Concrete same-role OWNERSHIP MOVE and full-reader phase are completed.
     * Last original Root/public-stock aliases, initial/failure FD graph,
     * full safe native error codec and immutable terminal publication are
     * NOT proved by that move, and this native entry NEVER claims them. */
    t->native_end_confirmed=0;t->remaining_original_owners=1;
    t->SourceReady=0;t->Root_admission=0;t->GO=0;
    t->phase="PUBLIC_STOCK_PARENT_OBSERVATIONS_ROOT_END_OPEN";
    return 0;
initial_failure:
    t->initial_error_phase=t->phase;t->initial_error_capture_attempted=1;
    PyErr_Fetch(&t->initial_error_type,&t->initial_error_value,&t->initial_error_tb);
    t->remaining_original_owners=1;t->native_end_confirmed=0;
    if(!t->phase)t->phase="native-preSource-first-failure";
    /* Full actual storage, partial modules/compiled bytes/raw inputs/pool/FD
     * records survive. No Source error codec is called from a native failure,
     * no guessed close/retry or process exit is declared a completed end. */
    return 0;
}

static int command_source_namespace(FridayPublisherRootCommandReceipt *,PyObject *);
static int secondary_debit_scan(uint64_t,uint64_t);
static int secondary_globals_role(FridayPublisherRootCommandReceipt *,PyObject *);
static int secondary_edge_required(FridayPublisherRootCommandReceipt *,
    const FridayPublisherSecondaryCut *,unsigned,PyObject *);
/* SOL103 complete actual-owner intake. A native value node contains its FULL
 * exact builtin body and numeric edges (including repeated aliases/cycles).
 * Unknown nonbuiltin REQUIRED values are explicitly retained, never zero-byte
 * accepted, repr()ed, hashed, reconstructed, or erased as a support theorem.
 * Only this SAME Root's registered own fields are enumerated. No foreign heap.
 */
enum {
    COMMAND_VALUE_NONE=1,COMMAND_VALUE_BOOL=2,COMMAND_VALUE_LONG=3,
    COMMAND_VALUE_FLOAT=4,COMMAND_VALUE_UNICODE=5,COMMAND_VALUE_BYTES=6,
    COMMAND_VALUE_BYTEARRAY=7,COMMAND_VALUE_TUPLE=8,COMMAND_VALUE_LIST=9,
    COMMAND_VALUE_DICT=10,COMMAND_VALUE_SOURCE=11,COMMAND_VALUE_ERROR=12,
    COMMAND_VALUE_TRACEBACK=13,COMMAND_VALUE_FRAME=14,COMMAND_VALUE_CODE=15,
    COMMAND_VALUE_FUNCTION=16,COMMAND_VALUE_METHOD=17,COMMAND_VALUE_CELL=18,
    COMMAND_VALUE_SET=19,COMMAND_VALUE_FROZENSET=20,COMMAND_VALUE_RANGE=21,
    COMMAND_VALUE_SLICE=22,COMMAND_VALUE_SUPPORT=23,COMMAND_VALUE_NAMESPACE=24,
    COMMAND_VALUE_CLASS=25,COMMAND_VALUE_MEMORYVIEW=26,
    COMMAND_VALUE_ELLIPSIS=27,COMMAND_VALUE_NOT_IMPLEMENTED=28,
    COMMAND_VALUE_NATIVE_CONTEXT=29,COMMAND_VALUE_NATIVE_MODULE=30,
    COMMAND_VALUE_PROPERTY=31,COMMAND_VALUE_STATICMETHOD=32,COMMAND_VALUE_CLASSMETHOD=33,
    COMMAND_VALUE_SOURCE_MODULE=34,COMMAND_VALUE_FRAME_LOCALS=35,
    COMMAND_VALUE_CONTEXT_VAR=36,COMMAND_VALUE_CONTEXT_TOKEN=37,
    COMMAND_VALUE_CONTEXT=38,COMMAND_VALUE_MAPPING=39,COMMAND_VALUE_HASH=40,
    COMMAND_VALUE_TOKEN_MISSING=41,
    COMMAND_VALUE_UNSUPPORTED=255
};
static int command_error(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootCommandError *e,const char *phase) {
    if(!PyErr_Occurred())return 0;
    /* A second exception is NEVER fetched then discarded. This routine's
     * callers immediately STOP on error. No later fallible op is permitted
     * after a full cell; the existing pending indicator remains owned too. */
    if(e->saved){r->pending_error_retained=1;r->residual="pending_error_with_full_preowned_cell";return -1;}
    e->phase=phase;e->saved=1;
    PyErr_Fetch(&e->type,&e->value,&e->tb);
    r->error_payload_complete=0;return -1;
}
static int command_owner(FridayPublisherRootCommandReceipt *r,PyObject **slot,
    uint64_t group,uint64_t at,uint64_t required) {
    if(!*slot)return 0;
    if(r->owner_count>=FRIDAY_ROOT_COMMAND_OWNERS)return -1;
    FridayPublisherRootCommandOwner *o=&r->owners[r->owner_count++];
    o->group=group;o->index=at;o->required=required;o->owned=*slot;
    *slot=NULL; /* OWNERSHIP MOVE, no destructor/factory or object mutation */
    return 0;
}
static int command_take_owners(FridayPublisherRootStorage *s,
    FridayPublisherRootCommandReceipt *r) {
    FridayPublisherRootTerminal *t=&s->terminal;
    FridayPublisherCallerResult *c=&t->result;
    /* Inspect actual received originals BEFORE any MOVE. No generic TYPE
     * downgrade; raw/unknown triples refuse with their originals retained. */
    if(secondary_debit_scan(128,0)<0)return -1;
    if(c->failure.received) {
        if(!c->failure_handback_confirmed||c->failure.owner_pid!=getpid())return -1;
        for(unsigned j=0;j<2;j++) {
            unsigned at=j?22:17;PyObject *type=c->failure.roots[at];
            PyObject *value=c->failure.roots[at+1],*tb=c->failure.roots[at+2];
            if(!type&&!value&&!tb)r->failure_type_kind[j]=0;
            else if(value&&PyExceptionInstance_Check(value)&&type==(PyObject *)Py_TYPE(value)&&
                (!tb||Py_TYPE(tb)==&PyTraceBack_Type))
                r->failure_type_kind[j]=command_builtin_exception(Py_TYPE(value))?1:2;
            else return -1;
        }
        r->failure_type_provenance=1;
    } else {
        for(unsigned j=0;j<FRIDAY_PUBLISHER_RUN_ROOTS;j++)if(c->failure.roots[j])return -1;
    }
    /* These legacy fields are BORROWED aliases, assigned without NewRef.
     * Moving them as additional strong owners would create a double release.
     * Real strong owners below move once; clear borrowers only after intake. */
    if((s->bootstrap_record&&s->bootstrap_record!=s->bootstrap.compat_record)||
       (s->bootstrap_stdout_buffer&&s->bootstrap_stdout_buffer!=s->bootstrap.stdout_raw)||
       (s->bootstrap_stderr_buffer&&s->bootstrap_stderr_buffer!=s->bootstrap.stderr_raw))return -1;
    uint64_t ordinals[22]={0};
#define TAKE(slot,group,required) do { if(command_owner(r,&(slot),group,ordinals[group]++,required)<0)return -1; } while(0)
    /* EACH actual strong field appears ONCE. Bindings copies are BORROWED and
     * are not fictitious extra owners; they stay non-owning until final join. */
    TAKE(s->pool.refusal_type,1,0);TAKE(s->pool.refusal_value,1,1);
    TAKE(s->enrollment,2,1);TAKE(s->admission,2,1);TAKE(s->role_schema,2,1);
    TAKE(s->root_fact,2,1);TAKE(s->qualification,2,1);
    TAKE(s->json_loads,3,0);TAKE(s->json_pairs,3,0);TAKE(s->json_constant,3,0);
    TAKE(s->re_fullmatch,3,0);TAKE(s->json_module,3,0);TAKE(s->re_module,3,0);
    TAKE(s->source_entry_module,3,0);
    /* Loader/module dictionaries are retained as real support owners; their
     * deregistration is NOT inferred from clearing the local loader pointer. */
    TAKE(s->loader_sys,4,0);TAKE(s->loader_meta,4,0);TAKE(s->loader_names,4,1);
    TAKE(s->loader_pending_name,4,1);TAKE(s->source_modules,4,0);
    TAKE(s->source_codes,4,0);TAKE(s->source_loader,4,0);
    TAKE(s->source_manifest,5,1);TAKE(s->consumer_manifest,5,1);
    TAKE(s->source_paths,5,1);TAKE(s->consumer_paths,5,1);
    TAKE(s->source_entry,5,0);
    uint64_t case_before=r->owner_count;
    TAKE(s->source_args,5,1);
    if(r->owner_count!=case_before)s->args_owner_at=case_before+1;
    case_before=r->owner_count;
    TAKE(s->source_case,5,1);
    if(r->owner_count!=case_before)s->case_owner_at=case_before+1;
    TAKE(s->final_identity,6,1);TAKE(s->final_close_attempt,6,1);
    TAKE(s->final_close_history,6,1);TAKE(s->final_slot,6,1);TAKE(s->final_rows,6,1);
    TAKE(s->bindings.final_fd_row,6,1);TAKE(s->bindings.final_fd_credit,6,1);
    for(uint64_t i=0;i<s->held_count;i++) {
        TAKE(s->held[i].pin,8,1);TAKE(s->held[i].raw,8,1);
    }
    for(uint64_t i=0;i<s->prepared_count;i++) {
        TAKE(s->prepared[i].row,9,1);TAKE(s->prepared[i].credit,9,1);
    }
    RootBootstrapState *b=&s->bootstrap;
    TAKE(b->first.type,10,0);TAKE(b->first.value,10,1);TAKE(b->first.tb,10,1);
    for(unsigned i=0;i<b->secondary_count;i++) {
        TAKE(b->secondary[i].type,10,0);TAKE(b->secondary[i].value,10,1);TAKE(b->secondary[i].tb,10,1);
    }
    TAKE(b->return_pending.type,10,0);TAKE(b->return_pending.value,10,1);TAKE(b->return_pending.tb,10,1);
    for(uint64_t i=0;i<b->retained_count;i++)TAKE(b->retained[i],11,1);
    TAKE(b->profile,11,1);TAKE(b->before,11,1);TAKE(b->after,11,1);
    TAKE(b->identity,11,1);TAKE(b->last_io,11,1);
    TAKE(b->stdout_raw,11,1);TAKE(b->stderr_raw,11,1);
    for(int i=0;i<2;i++) {
        TAKE(b->wire_raw[i],11,1);TAKE(b->parent_raw[i],11,1);TAKE(b->overflow_raw[i],11,1);
    }
    TAKE(b->outcome_raw,11,1);TAKE(b->compat_record,11,1);
    TAKE(b->secondary_immutable,11,1);TAKE(b->wait_usage_raw,11,1);
    TAKE(b->final_parent_raw,11,1);TAKE(b->parent_end_raw,11,1);
    TAKE(b->reap_info_raw,11,1);TAKE(b->spawn_args_raw,11,1);
    for(int i=0;i<3;i++)TAKE(b->ack_raw[i],11,1);
    TAKE(s->image_inventory.mountinfo_raw,12,1);
    TAKE(t->root_fact,13,1);TAKE(t->qualification,13,1);
    TAKE(t->enrollment,13,1);TAKE(t->admission,13,1);TAKE(t->role_schema,13,1);
    TAKE(t->entry,13,0);TAKE(t->args,13,1);TAKE(t->bootstrap_observation,13,1);
    TAKE(t->bootstrap_parent_full_native,13,1);TAKE(t->bootstrap_parent_end_native,13,1);
    TAKE(t->initial_error_type,14,0);TAKE(t->initial_error_value,14,1);TAKE(t->initial_error_tb,14,1);
    TAKE(c->full_packet,15,1);
    TAKE(c->original_call_error_type,16,0);TAKE(c->original_call_error_value,16,1);TAKE(c->original_call_error_tb,16,1);
    for(int i=0;i<7;i++)TAKE(c->before_entry_roots[i],17,1);
    for(int i=0;i<FRIDAY_PUBLISHER_RUN_ROOTS;i++) {
        uint64_t before=r->owner_count;
        int data=!((i==17&&r->failure_type_kind[0]==1)||(i==22&&r->failure_type_kind[1]==1));
        TAKE(c->failure.roots[i],18,data);
        if(r->owner_count!=before)r->failure_root_owner_at[i]=before+1;
    }
    TAKE(c->failure.caller_packet,18,1);
    if(c->failure.received) {
        NativeCloseRecord *cursor=NULL;uint64_t seen=0;
        FridayPublisherFailureCloseView v;int rc;
        while((rc=FridayPublisherFailureCloseNext(&c->failure,&cursor,&seen,&v))==1) {
            if(!v.owner_slot)return -1;
            TAKE(*v.owner_slot,19,1); /* actual strong row, not a borrowed copy */
        }
        if(rc<0)return -1;
        if(FridayPublisherFailureRetireMovedCloseRecords(&c->failure)<0)return -1;
    }
    /* SOL105 actual producer strong slots transfer to SAME native recipient.
     * Registry keeps explicit BORROWED views until its dependent typed reader
     * is done. A251 nulls these views BEFORE the first owner DECREF.
     * Partial overflow retains every un-MOVED original, never erases it. */
    s->own_values.owner_first=r->owner_count;
    uint64_t own_before_count=r->owner_count;
    TAKE(s->own_values.early_module,21,0);
    if(r->owner_count!=own_before_count)s->own_values.early_owner_at=own_before_count+1;
    own_before_count=r->owner_count;
    TAKE(s->own_values.token_missing,21,1); /* actual public TOKEN_MISSING value */
    if(r->owner_count!=own_before_count)s->own_values.missing_owner_at=own_before_count+1;
    /* A254: extend the SAME native owner partition, not a second inventory.
     * Actual globals are strong fields, not borrowed per-row views. Their
     * successful MOVE indices survive both partial intake and final end. */
    PyObject **stock_globals[]={&s->own_values.mapping_module,&s->own_values.mapping_factory,
        &s->own_values.mapping_access,&s->own_values.hash_module,
        &s->own_values.hash_sha256,&s->own_values.hash_new};
    for(unsigned j=0;j<6;j++) {
        own_before_count=r->owner_count;
        TAKE(*stock_globals[j],21,j==2);
        if(r->owner_count!=own_before_count)s->own_values.stock_owner_at[j]=own_before_count+1;
    }
    for(uint64_t i=0;i<s->own_values.count;i++) {
        FridayPublisherOwnValue *p=&s->own_values.rows[i];
        for(unsigned j=0;j<FRIDAY_OWN_FIELDS;j++)if(p->refs[j]&&!(p->borrowed_mask&(1U<<j))) {
            PyObject *view=p->refs[j];uint64_t owner_at=r->owner_count+1;
            TAKE(p->refs[j],21,own_row_required(p,j));
            p->refs[j]=view;p->borrowed_mask|=1U<<j;p->owner_at[j]=owner_at;
        }
        PyObject **slots[]={&p->error_type,&p->error_value,&p->error_tb};
        for(unsigned j=0;j<3;j++)if(*slots[j]&&!(p->borrowed_mask&(1U<<(12+j)))) {
            PyObject *view=*slots[j];uint64_t owner_at=r->owner_count+1;
            TAKE(*slots[j],21,j!=0);
            *slots[j]=view;p->borrowed_mask|=1U<<(12+j);p->owner_at[12+j]=owner_at;
        }
    }
    s->own_values.owner_count=r->owner_count-s->own_values.owner_first;
    /* Group 22 is the secondary-row ordinal space. It is not a new OWN_* kind.
     * A failed move leaves the unmoved strong pointer in the row. */
    if(s->secondary.count>FRIDAY_ROOT_SECONDARY_CUTS)return -1;
    s->secondary.owner_first=r->owner_count;
    s->secondary.owner_count=0;
    for(uint64_t i=0;i<s->secondary.count;i++) {
        FridayPublisherSecondaryCut *p=&s->secondary.rows[i];
        if(p->nedges>FRIDAY_ROOT_SECONDARY_EDGES)return -1;
        if(p->original&&!(p->borrowed_mask&1U)) {
            PyObject *view=p->original;int required=secondary_edge_required(r,p,0,view);
            if(required<0)return -1;uint64_t at=r->owner_count+1;
            if(command_owner(r,&p->original,22,s->secondary.owner_count,(uint64_t)required)<0)return -1;
            p->original=view;p->borrowed_mask|=1U;p->owner_at[0]=at;
            if(required)p->required_mask|=1U;
            s->secondary.owner_count=r->owner_count-s->secondary.owner_first;
        }
        for(unsigned j=0;j<p->nedges;j++) if(p->edges[j]&&!(p->borrowed_mask&(1U<<(j+1)))) {
            PyObject *view=p->edges[j];int required=secondary_edge_required(r,p,j+1,view);
            if(required<0)return -1;uint64_t at=r->owner_count+1;
            if(command_owner(r,&p->edges[j],22,s->secondary.owner_count,(uint64_t)required)<0)return -1;
            p->edges[j]=view;p->borrowed_mask|=1U<<(j+1);p->owner_at[j+1]=at;
            if(required)p->required_mask|=1U<<(j+1);
            s->secondary.owner_count=r->owner_count-s->secondary.owner_first;
        }
    }
#undef TAKE
    /* Incoming originals enter this same required graph before retirement. */
    if(command_owner(r,&r->incoming_error.type,20,0,0)<0||
       command_owner(r,&r->incoming_error.value,20,1,1)<0||
       command_owner(r,&r->incoming_error.tb,20,2,1)<0)return -1;
    s->bootstrap_record=NULL;
    s->bootstrap_stdout_buffer=NULL;s->bootstrap_stderr_buffer=NULL;
    r->owner_inventory_complete=1;r->retained_owners=r->owner_count;
    return 0;
}
static PyObject *command_original(FridayPublisherRootCommandReceipt *r,uint64_t group,
    uint64_t ordinal) {
    for(uint64_t i=0;i<r->owner_count;i++)
        if(r->owners[i].group==group&&r->owners[i].index==ordinal)return r->owners[i].owned;
    return NULL;
}
static uint64_t command_clock(FridayPublisherRootCommandReceipt *);
static uint64_t command_node(FridayPublisherRootCommandReceipt *r,PyObject *o) {
    if(!o)return 0;
    uintptr_t p=(uintptr_t)o;
    uint64_t slot=((p>>4)^(p>>25))&(FRIDAY_ROOT_COMMAND_INDEX-1);
    for(uint64_t n=0;n<FRIDAY_ROOT_COMMAND_INDEX;n++) {
        if((n&1023)==0&&!command_clock(r))return 0;
        r->index_probes++;uint64_t id=r->index[slot];
        if(id) {if(r->nodes[id-1].original==o)return id;}
        else {
            if(r->node_count>=FRIDAY_ROOT_COMMAND_NODES)return 0;
            id=++r->node_count;r->index[slot]=id;
            r->nodes[id-1].id=id;r->nodes[id-1].pointer_index_slot=slot;
            r->nodes[id-1].original=Py_NewRef(o);
            r->nodes[id-1].first_parent=r->active_node;
            r->nodes[id-1].first_edge=r->edge_count;
            r->graph_owned_nodes++;return id;
        }
        slot=(slot+1)&(FRIDAY_ROOT_COMMAND_INDEX-1);
    }
    return 0;
}
static uint64_t command_clock(FridayPublisherRootCommandReceipt *r) {
    if(r->clock_fault_kind)return 0; /* exact first native guard failure, no retry */
    struct timespec t;memset(&t,0,sizeof(t));
    r->clock_calls++;r->clock_rc=clock_gettime(CLOCK_MONOTONIC,&t);
    r->clock_errno=r->clock_rc<0?errno:0;r->clock_fault_kind=0;
    if(r->clock_rc<0){r->clock_fault_kind=1;return 0;}
    if(t.tv_sec<0||t.tv_nsec<0||t.tv_nsec>=1000000000L||
       (uint64_t)t.tv_sec>UINT64_MAX/1000000000ULL) {
        r->clock_fault_kind=2;return 0; /* native validation, NEVER kernel errno */
    }
    uint64_t seconds=(uint64_t)t.tv_sec*1000000000ULL;
    if((uint64_t)t.tv_nsec>UINT64_MAX-seconds){r->clock_fault_kind=2;return 0;}
    r->clock_now_ns=seconds+(uint64_t)t.tv_nsec;
    if(root_storage.pool.deadline_ns&&r->clock_now_ns>root_storage.pool.deadline_ns) {
        r->clock_fault_kind=3;return 0; /* actual expired bound, NOT kernel errno */
    }
    return r->clock_now_ns;
}
static int command_append(FridayPublisherRootCommandReceipt *r,const void *raw,uint64_t n) {
    uint64_t now=command_clock(r);
    if(!now||!root_storage.pool.deadline_ns||now>root_storage.pool.deadline_ns)return -1;
    if(n>FRIDAY_ROOT_COMMAND_BYTES-r->body_bytes)return -1;
    if(n)memcpy(r->body+r->body_bytes,raw,(size_t)n);
    r->body_bytes+=n;return 0;
}
static int command_edge(FridayPublisherRootCommandReceipt *r,PyObject *o) {
    uint64_t id=command_node(r,o);
    if(!id||r->edge_count>=FRIDAY_ROOT_COMMAND_EDGES)return -1;
    r->edges[r->edge_count]=id;r->edge_roles[r->edge_count++]=1;return 0;
}

/* SOL104 finite current Source value reader. Type/module binding is checked
 * against the actual admitted held Source body and live sys.modules object;
 * a user-written __module__/type name alone never selects this codec. */
static const char *command_source_names[]={
    "authority","bill","body_scope","canonical","capability","contract","declared_controls","document_vector","document_windows","effects","formats","ingress","material_literals","performing_contracts","pins","producer_consumer","receipt_chain","recipe_planner","resource_meter","runtime_consumer","schema_validate","semantics","whole_join","actor_bootstrap","actor_context","admission","alljob_cohort","capacity","class_semantics","common","consumer_bridge","custody","existing_root_caller","extraction","fact_bridge","independent_selector","launcher","lifetime","master_pool","native","normalization","observer","operations","owned_prefix_bank","retention","roles","root_tool_adapter","selected_owned_values","snapshot_producer"
};
static PyObject *command_source_module(FridayPublisherRootCommandReceipt *r,
    const char *name) {
    size_t selected=sizeof(command_source_names)/sizeof(*command_source_names);
    for(size_t i=0;i<selected;i++)if(strcmp(command_source_names[i],name)==0){selected=i;break;}
    if(selected>=49)return NULL;
    if(r->source_module_checked[selected])return r->source_module_cache[selected];
    r->source_module_checked[selected]=1;
    /* Actual native compiled-body registry outlives sys.modules. Module names,
     * __file__ and user-controlled __module__ alone never create this relation. */
    char relative[256];int n=snprintf(relative,sizeof(relative),"source/%s.py",name);
    if(n<0||n>255)return NULL;
    for(uint64_t i=0;i<root_storage.own_values.count;i++) {
        if((i&1023)==0&&!command_clock(r))return NULL;
        FridayPublisherOwnValue *b=&root_storage.own_values.rows[i];
        if(b->kind!=OWN_BINDING||!b->refs[0]||!b->refs[2]||!b->refs[3]||!b->refs[10])continue;
        if(PyUnicode_CheckExact(b->refs[10])&&PyUnicode_CompareWithASCIIString(b->refs[10],relative)==0) {
            r->source_module_cache[selected]=b->refs[0];return b->refs[0];
        }
        if(PyErr_Occurred())return NULL;
    }
    return NULL;
}
static int command_source_class(FridayPublisherRootCommandReceipt *r,
    PyTypeObject *type,const char **module,const char **name) {
    static const struct {const char *module,*name;} selected[]={
        {"actor_context","ActorContext"},
        {"common","Refused"},
        {"common","PreObserverHash"},
        {"common","OwnedPreimage"},
        {"common","Frame"},
        {"custody","Held"},
        {"custody","OutputStore"},
        {"custody","PreparedFullBody"},
        {"operations","PerformingOperations"},
        {"existing_root_caller","NativeMemoryBody"},
        {"existing_root_caller","NativeRootReceiver"},
        {"extraction","ArchiveOwner"},
        {"extraction","FDView"},
        {"extraction","Decompressed"},
        {"extraction","Installer"},
        {"native","RootNative"},
        {"actor_bootstrap","StockChildJournal"},
        {"actor_bootstrap","StockChannel"},
        {"actor_bootstrap","VerifiedSource"},
        {"launcher","ControlChannel"},
        {"selected_owned_values","OwnedMappingBirth"},
        {"root_tool_adapter","RootToolAdapter"},
        {"lifetime","FDState"},
        {"lifetime","OwnedFDs"},
        {"lifetime","ForkOwner"},
        {"lifetime","_PreownedFailureReady"},
        {"owned_prefix_bank","_Payload"},
        {"owned_prefix_bank","PrefixBodyMailbox"},
        {"consumer_bridge","HeldConsumerLoader"},
        {"consumer_bridge","OrdinaryInput"},
        {"master_pool","MasterPool"},
        {"observer","Reservation"},
        {"observer","RootObserver"},
        {"observer","MeterRPC"},
        {"observer","FinalArena"},
        {"observer","NativeCompletionReceipt"},
        {"normalization","RootNormalizer"},
        {"resource_meter","WholeMeter"},
        {"resource_meter","_Hash"},
        {"resource_meter","HashlibProxy"},
        {"document_vector","_Lease"},
        {"whole_join","alias_window"},
        {"document_windows","FilePageSource"},
        {"contract","ContractError"},
        {"recipe_planner","_ConstructionMeter"},
        {"recipe_planner","_Held"}
    };
    for(size_t i=0;i<sizeof(selected)/sizeof(*selected);i++) {
        if(!r->source_class_checked[i]) {
            r->source_class_checked[i]=1;
            PyObject *m=command_source_module(r,selected[i].module);
            if(PyErr_Occurred()||r->clock_fault_kind)return -1;
            if(m) {
                r->class_binding_checks++;
                for(uint64_t j=0;j<root_storage.own_values.count;j++) {
                    if((j&1023)==0&&!command_clock(r))return -1;
                    FridayPublisherOwnValue *b=&root_storage.own_values.rows[j];
                    if(own_class_success(b)&&b->refs[1]==m&&b->refs[2]&&
                       b->refs[3]&&b->refs[4]&&b->refs[5]&&
                       PyUnicode_CheckExact(b->refs[2])&&PyCode_Check(b->refs[3])&&
                       PyBytes_CheckExact(b->refs[4])&&PyCode_Check(b->refs[5])&&
                       PyUnicode_CompareWithASCIIString(b->refs[2],selected[i].name)==0) {
                        r->source_class_cache[i]=b->refs[0];break;
                    }
                    if(PyErr_Occurred())return -1;
                }
            }
        }
        if(r->source_class_cache[i]==(PyObject *)type) {
            *module=selected[i].module;*name=selected[i].name;return 1;
        }
    }
    /* Remaining actual captured own classes are not relabelled foreign
     * merely because the old fixed46 memoization list did not name them.
     * Real namespace/type pointer + full compiled held body creates binding;
     * this ASCII namespace-key view is backed by the retained exact unicode. */
    for(uint64_t j=0;j<root_storage.own_values.count;j++) {
        if((j&1023)==0&&!command_clock(r))return -1;
        FridayPublisherOwnValue *p=&root_storage.own_values.rows[j];
        if(!own_class_success(p)||p->refs[0]!=(PyObject *)type||
           !p->refs[2]||!p->refs[3]||!p->refs[4]||!p->refs[5]||
           !PyUnicode_CheckExact(p->refs[2])||!PyCode_Check(p->refs[3])||
           !PyBytes_CheckExact(p->refs[4])||!PyCode_Check(p->refs[5])||
           !PyUnicode_IS_COMPACT_ASCII(p->refs[2]))continue;
        for(size_t k=0;k<sizeof(command_source_names)/sizeof(*command_source_names);k++) {
            PyObject *actual=command_source_module(r,command_source_names[k]);
            if(PyErr_Occurred()||r->clock_fault_kind)return -1;
            if(actual==p->refs[1]) {
                *module=command_source_names[k];*name=(const char *)PyUnicode_1BYTE_DATA(p->refs[2]);
                return 1;
            }
        }
    }
    return 0;
}
static int command_source_namespace(FridayPublisherRootCommandReceipt *r,PyObject *d) {
    /* Same ACTUAL module/class namespace relation used by the Source producer,
     * including retained class tp_dict and after sys.modules deregistration. */
    FridayPublisherOwnValue *found=own_module_for_namespace(d);
    if(PyErr_Occurred())return -1;
    return found!=NULL;
}
static int command_named(FridayPublisherRootCommandReceipt *r,const char *s) {
    uint64_t n=(uint64_t)strlen(s);
    if(command_append(r,&n,sizeof(n))<0)return -1;
    return command_append(r,s,n);
}
static int command_role_edge(FridayPublisherRootCommandReceipt *r,PyObject *o,int role) {
    if(command_edge(r,o?o:Py_None)<0)return -1;
    r->edge_roles[r->edge_count-1]=(unsigned char)role;return 0;
}

static int command_nullable_edge(FridayPublisherRootCommandReceipt *r,PyObject *o,int role) {
    unsigned char present=o!=NULL;
    if(command_append(r,&present,1)<0)return -1;
    /* An absent native field is NOT a claimed alias to actual Source None.
     * Body's presence bit makes the canonical None filler non-data/support. */
    return command_role_edge(r,o?o:Py_None,present?role:0);
}

static int command_factory_begin(FridayPublisherRootCommandReceipt *r,const char *field) {
    r->getter_field=field;
    /* No new ordinary factory during the one terminal secondary traversal,
     * even if a structural refusal did not install a Python exception. */
    if(r->secondary_read_attempted) {r->secondary_factory_blocked=1;return -1;}
    if(r->getter_pending||r->cleanup_error.saved||PyErr_Occurred()||!command_clock(r))return -1;
    /* Actual nominal debit BEFORE materialization in the SAME original pool.
     * NOT an established bound on implicit CPython/ABI allocation. No claim
     * that 131072 suffices for a frame/function/public getter or workload.
     * Current Source/image qualification must establish the whole upper. */
    if(FridayPublisherMasterBefore(&root_storage.pool,0,0,0,131072)<0)return -1;
    if(plus(&r->getter_reservation_bytes,131072)<0)return -1;
    r->getters_attempted++;return 0;
}
static int command_generated(FridayPublisherRootCommandReceipt *r,int role) {
    if(!r->getter_pending||PyErr_Occurred())return -1;
    /* Getter result becomes strong NODE custody BEFORE its temporary drops.
     * If index/edge storage refuses, keep exact getter_pending and STOP. */
    if(command_role_edge(r,r->getter_pending,role)<0)return -1;
    PyObject *owned=r->getter_pending;r->getter_pending=NULL;
    r->getter_results++;Py_DECREF(owned);return 0;
}
static int command_attr(FridayPublisherRootCommandReceipt *r,PyObject *o,
    PyTypeObject *trusted,const char *field,int role) {
    if(command_factory_begin(r,field)<0)return -1;
    PyObject *d=trusted->tp_dict?PyDict_GetItemString(trusted->tp_dict,field):NULL;
    if(PyErr_Occurred()||!d||
       (Py_TYPE(d)!=&PyGetSetDescr_Type&&Py_TYPE(d)!=&PyMemberDescr_Type)||
       !Py_TYPE(d)->tp_descr_get)return -1;
    /* Invoke ONLY this exact exported stock descriptor from a fixed builtin
     * owner, never the object's arbitrary __getattribute__/property/MRO. */
    r->getter_pending=Py_TYPE(d)->tp_descr_get(d,o,(PyObject *)Py_TYPE(o));
    if(r->getter_pending&&trusted==&PyFrame_Type&&strcmp(field,"f_locals")==0&&
       !PyDict_CheckExact(r->getter_pending)) {
        /* Exact stock object returned by the actual stock frame descriptor,
         * not acceptance based on a caller supplied type-name string. */
        if(r->frame_locals_type&&r->frame_locals_type!=Py_TYPE(r->getter_pending))return -1;
        r->frame_locals_type=Py_TYPE(r->getter_pending);
    }
    if(command_generated(r,role)<0)return -1;
    if(trusted==&PyFrame_Type&&strcmp(field,"f_builtins")==0)
        r->nodes[r->edges[r->edge_count-1]-1].support_verified=2;
    if(trusted==&PyFrame_Type&&strcmp(field,"f_globals")==0) {
        FridayPublisherRootValueNode *n=&r->nodes[r->edges[r->edge_count-1]-1];
        int own=command_source_namespace(r,n->original);if(own<0)return -1;
        if(!own){n->support_verified=2;r->edge_roles[r->edge_count-1]=0;}
    }
    return 0;
}
static int command_builtin_exception(PyTypeObject *t) {
    PyObject *selected[]={PyExc_BaseException,PyExc_Exception,PyExc_ArithmeticError,
        PyExc_AssertionError,PyExc_AttributeError,PyExc_BufferError,PyExc_EOFError,
        PyExc_ImportError,PyExc_ModuleNotFoundError,PyExc_LookupError,PyExc_IndexError,
        PyExc_KeyError,PyExc_MemoryError,PyExc_NameError,PyExc_UnboundLocalError,
        PyExc_OSError,PyExc_BlockingIOError,PyExc_ChildProcessError,PyExc_ConnectionError,
        PyExc_BrokenPipeError,PyExc_ConnectionAbortedError,PyExc_ConnectionRefusedError,
        PyExc_ConnectionResetError,PyExc_FileExistsError,PyExc_FileNotFoundError,
        PyExc_InterruptedError,PyExc_IsADirectoryError,PyExc_NotADirectoryError,
        PyExc_PermissionError,PyExc_ProcessLookupError,PyExc_TimeoutError,
        PyExc_ReferenceError,PyExc_RuntimeError,PyExc_RecursionError,PyExc_NotImplementedError,
        PyExc_StopIteration,PyExc_StopAsyncIteration,PyExc_SyntaxError,PyExc_IndentationError,
        PyExc_TabError,PyExc_SystemError,PyExc_SystemExit,PyExc_TypeError,PyExc_ValueError,
        PyExc_UnicodeError,PyExc_UnicodeDecodeError,PyExc_UnicodeEncodeError,
        PyExc_UnicodeTranslateError,PyExc_ZeroDivisionError,PyExc_OverflowError,
        PyExc_FloatingPointError,PyExc_KeyboardInterrupt,PyExc_GeneratorExit,
        PyExc_BaseExceptionGroup};
    for(size_t i=0;i<sizeof(selected)/sizeof(*selected);i++)
        if((PyObject *)t==selected[i])return 1;
    return 0;
}
static int command_error_value(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v) {
    PyObject *o=v->original;const char *module=NULL,*name=NULL;
    int builtin=command_builtin_exception(Py_TYPE(o));
    int own=builtin?0:command_source_class(r,Py_TYPE(o),&module,&name);
    if(own<0)return -1;
    if(!own&&!builtin)return 1;
    v->kind=COMMAND_VALUE_ERROR;
    if(command_named(r,own?module:"builtins")<0||
       command_named(r,own?name:Py_TYPE(o)->tp_name)<0)return -1;
    /* Selected exported NON-LIMITED stock PyBaseExceptionObject prefix, not a
     * private heap traversal or arbitrary user getter. Full original args,
     * notes/dict/causes/context/TB and causal cycles are actual graph edges.
     * Compiler/layout ABI is REQUIRED_NOT_RUN, not implicitly admitted. */
    PyBaseExceptionObject *e=(PyBaseExceptionObject *)o;
    if(command_role_edge(r,(PyObject *)Py_TYPE(o),0)<0||
       command_nullable_edge(r,e->args,1)<0||command_nullable_edge(r,e->dict,1)<0||
       command_nullable_edge(r,e->notes,1)<0||command_nullable_edge(r,e->cause,1)<0||
       command_nullable_edge(r,e->context,1)<0||command_nullable_edge(r,e->traceback,1)<0||
       command_append(r,&e->suppress_context,sizeof(e->suppress_context))<0)return -1;
#define VALUE(x) do { if(command_nullable_edge(r,(x),1)<0)return -1; } while(0)
    if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_OSError)) {
        PyOSErrorObject *q=(PyOSErrorObject *)o;
        VALUE(q->myerrno);VALUE(q->strerror);VALUE(q->filename);VALUE(q->filename2);
        if(command_append(r,&q->written,sizeof(q->written))<0)return -1;
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_SyntaxError)) {
        PySyntaxErrorObject *q=(PySyntaxErrorObject *)o;
        VALUE(q->msg);VALUE(q->filename);VALUE(q->lineno);VALUE(q->offset);
        VALUE(q->end_lineno);VALUE(q->end_offset);VALUE(q->text);VALUE(q->print_file_and_line);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_UnicodeError)) {
        /* The base UnicodeError has only BaseException fields; exact selected
         * encode/decode/translate subclasses own the exported extra payload. */
        if(Py_TYPE(o)==(PyTypeObject *)PyExc_UnicodeEncodeError||
           Py_TYPE(o)==(PyTypeObject *)PyExc_UnicodeDecodeError||
           Py_TYPE(o)==(PyTypeObject *)PyExc_UnicodeTranslateError) {
            PyUnicodeErrorObject *q=(PyUnicodeErrorObject *)o;
            VALUE(q->encoding);VALUE(q->object);VALUE(q->reason);
            if(command_append(r,&q->start,sizeof(q->start))<0||
               command_append(r,&q->end,sizeof(q->end))<0)return -1;
        }
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_ImportError)) {
        PyImportErrorObject *q=(PyImportErrorObject *)o;
        VALUE(q->msg);VALUE(q->name);VALUE(q->path);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_AttributeError)) {
        PyAttributeErrorObject *q=(PyAttributeErrorObject *)o;VALUE(q->obj);VALUE(q->name);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_NameError)) {
        PyNameErrorObject *q=(PyNameErrorObject *)o;VALUE(q->name);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_SystemExit)) {
        PySystemExitObject *q=(PySystemExitObject *)o;VALUE(q->code);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_StopIteration)) {
        PyStopIterationObject *q=(PyStopIterationObject *)o;VALUE(q->value);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_BaseExceptionGroup)) {
        PyBaseExceptionGroupObject *q=(PyBaseExceptionGroupObject *)o;VALUE(q->msg);VALUE(q->excs);
    }
#undef VALUE
    return 0;
}
/* Typed consumer of actual own producer records. Every scalar + body edge
 * belongs to native storage already held BEFORE the corresponding effect.
 * A foreign hash's runtime bytes are NOT claimed: required own computation is
 * its complete exact constructor/update input ledger and real runtime support.
 * Unconfirmed mutation/close/reset keeps originals and refuses completeness. */
/* Mapping phase tail v1: actual CURRENT full stock buffer vs RETIRED held
 * before-close body. Unknown close is deliberately not relabelled CURRENT.
 * This shares the original receipt buffer/owners, never adds a new pool. */
static uint64_t command_mapping_phase(FridayPublisherOwnValue *p) {
    if(!p||p->kind!=OWN_MAPPING||!p->refs[6]||!p->width)return 0;
    if(p->attempts==3&&p->confirmed==3&&(p->flags&3)==3&&!(p->flags&4)&&!(p->flags&16))return 2;
    if((p->attempts==1||p->attempts==2)&&p->confirmed==1&&!(p->flags&(14|16)))return 1;
    if(p->attempts>=3&&(p->flags&4)&&p->confirmed!=3)return 3;
    return 0;
}
/* The absent mmap has no Python mapping value to encode. Preserve its actual
 * native producer row in the EXISTING native-owner body, not as a fabricated
 * COMMAND_VALUE_MAPPING. The trailer is written after the complete group21
 * MOVE and before graph production; owner slots then bind the original DATA.
 * No new bank, struct capacity, Python factory, or execution role is added. */
#define COMMAND_UNMAPPED_MAGIC UINT64_C(0x46524e4f4d415031)
#define COMMAND_UNMAPPED_WORDS 24
static uint64_t command_unmapped_phase(const FridayPublisherOwnValue *p) {
    if(p->kind!=OWN_MAPPING||p->owner_at[6]||p->confirmed||
       p->error_saved<0||p->error_saved>1)return 0;
    if(p->flags&OWN_MAPPING_PREBIRTH) {
        if((p->flags&~(OWN_MAPPING_PREBIRTH|UINT64_C(32)))||p->attempts||
           p->width||!p->owner_at[3])return 0;
        return 3; /* actual pre-init row; not a mapping-value phase */
    }
    if(p->flags&~UINT64_C(40))return 0;
    if(!p->attempts&&!(p->flags&8))return 1;
    if(p->attempts==1&&(p->flags&8)&&p->error_saved&&p->owner_at[12])return 2;
    return 0;
}
static void command_unmapped_record(const FridayPublisherOwnValue *p,
    uint64_t fields[COMMAND_UNMAPPED_WORDS]) {
    fields[0]=p->kind;fields[1]=p->serial;fields[2]=p->flags;
    fields[3]=p->attempts;fields[4]=p->confirmed;fields[5]=p->width;
    fields[6]=(uint64_t)p->pid;fields[7]=(uint64_t)p->error_saved;
    fields[8]=command_unmapped_phase(p);
    for(unsigned j=0;j<15;j++)fields[9+j]=p->owner_at[j];
}
static int command_unmapped_rows_append(FridayPublisherRootStorage *s,
    FridayPublisherRootCommandReceipt *r) {
    uint64_t count=s->own_values.count,rows=0;
    if(!r->owner_inventory_complete||r->graph_started||
       count>FRIDAY_ROOT_COMMAND_NODES||count>UINT64_MAX/4||
       r->native_owner_body_at>r->body_bytes||
       r->native_owner_body_bytes!=r->body_bytes-r->native_owner_body_at||
       secondary_debit_scan(count*4,0)<0)return -1;
    for(uint64_t i=0;i<count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        const FridayPublisherOwnValue *p=&s->own_values.rows[i];
        if(p->kind!=OWN_MAPPING||p->owner_at[6])continue;
        /* Fixed actual row/scalar/slot reads and both copy operands BEFORE
         * materialization. Allocation is existing receipt storage, not zero
         * ABI cost or a promise that the whole workload fits its bound. */
        if(secondary_debit_scan(64,0)<0)return -1;
        uint64_t fields[COMMAND_UNMAPPED_WORDS];command_unmapped_record(p,fields);
        if(command_append(r,fields,sizeof(fields))<0)return -1;
        rows++;
    }
    if(secondary_debit_scan(8,0)<0)return -1;
    uint64_t trailer[]={COMMAND_UNMAPPED_MAGIC,COMMAND_UNMAPPED_WORDS,rows};
    if(command_append(r,trailer,sizeof(trailer))<0)return -1;
    /* Updated only on a complete trailer. A partial copy cannot pass the
     * existing native-body/graph partition predicate on the failure path. */
    r->native_owner_body_bytes=r->body_bytes-r->native_owner_body_at;
    return 0;
}
static int final_caller_clock(FridayPublisherRootFinalHandoff *,uint64_t *);
static int command_unmapped_scan(uint64_t steps,int ended) {
    if(!ended)return secondary_debit_scan(steps,0);
    /* Python/runtime is retired: use only the same originally reserved
     * final-caller credit. No MasterBefore, fresh grant, or Python API here. */
    if(steps>UINT64_MAX/64)return -1;
    uint64_t reads=steps*64;RootFinalCallerBinding *b=&final_caller_binding;
    if(!b->credit_reserved||b->read_used>b->read_credit||
       reads>b->read_credit-b->read_used)return -1;
    b->read_used+=reads;return 0;
}
static int command_unmapped_rows_check(const FridayPublisherRootCommandReceipt *r,
    int ended,FridayPublisherRootFinalHandoff *h) {
    if(command_unmapped_scan(8,ended)<0)return -1;
    const FridayPublisherOwnValues *owned=&root_storage.own_values;
    uint64_t count=owned->count,tail[3],record_bytes=COMMAND_UNMAPPED_WORDS*sizeof(uint64_t);
    if(count>FRIDAY_ROOT_COMMAND_NODES||r->owner_count>FRIDAY_ROOT_COMMAND_OWNERS||
       r->node_count>FRIDAY_ROOT_COMMAND_NODES||
       r->native_owner_body_at>r->graph_body_at||r->graph_body_at>r->body_bytes||
       r->body_bytes>FRIDAY_ROOT_COMMAND_BYTES||
       r->native_owner_body_bytes!=r->graph_body_at-r->native_owner_body_at||
       r->native_owner_body_bytes<sizeof(tail))return -1;
    memcpy(tail,r->body+r->graph_body_at-sizeof(tail),sizeof(tail));
    if(tail[0]!=COMMAND_UNMAPPED_MAGIC||tail[1]!=COMMAND_UNMAPPED_WORDS||
       tail[2]>count||tail[2]>UINT64_MAX/record_bytes)return -1;
    uint64_t bytes=tail[2]*record_bytes;
    if(bytes>r->native_owner_body_bytes-sizeof(tail)||
       count>UINT64_MAX/4||tail[2]>(UINT64_MAX-count*4)/128||
       command_unmapped_scan(count*4+tail[2]*128,ended)<0)return -1;
    uint64_t at=r->graph_body_at-sizeof(tail)-bytes,seen=0;
    for(uint64_t i=0;i<count;i++) {
        if((i&1023)==0) {
            uint64_t now;
            if(ended?(!h||final_caller_clock(h,&now)<0):
               !command_clock((FridayPublisherRootCommandReceipt *)r))return -1;
        }
        const FridayPublisherOwnValue *p=&owned->rows[i];
        if(p->kind!=OWN_MAPPING||p->owner_at[6])continue;
        if(seen>=tail[2]||p->serial!=i+1||p->pid!=r->pid)return -1;
        uint64_t actual[COMMAND_UNMAPPED_WORDS],expected[COMMAND_UNMAPPED_WORDS];
        command_unmapped_record(p,expected);
        memcpy(actual,r->body+at,sizeof(actual));at+=sizeof(actual);seen++;
        if(memcmp(actual,expected,sizeof(actual))||!actual[8]||!(p->flags&32)||
           !p->owner_at[0]||!p->owner_at[1]||!p->owner_at[4]||!p->owner_at[5]||
           (actual[8]==3?!p->owner_at[3]:!p->owner_at[8]||!p->owner_at[9])||
           (p->error_saved&&!p->owner_at[12])||
           (!p->error_saved&&(p->owner_at[12]||p->owner_at[13]||p->owner_at[14])))return -1;
        for(unsigned j=0;j<15;j++) {
            uint64_t id=actual[9+j];
            if(((p->borrowed_mask>>j)&1U)!=(id!=0))return -1;
            if(!id)continue;
            if(id>r->owner_count)return -1;
            const FridayPublisherRootCommandOwner *o=&r->owners[id-1];
            if(o->group!=21||!o->node||o->node>r->node_count)return -1;
            const FridayPublisherRootValueNode *n=&r->nodes[o->node-1];
            int required=j!=10&&j!=11&&j!=12;
            if(o->required!=(uint64_t)required||
               (required&&(!n->required||!n->data_read||n->kind==COMMAND_VALUE_UNSUPPORTED)))return -1;
            /* The live generic group21 check joins actual aliases; after
             * retirement only the SAME numeric links/full DATA are read. */
            if(ended&&(o->owned||n->original))return -1;
        }
    }
    return seen==tail[2]&&at==r->graph_body_at-sizeof(tail)?0:-1;
}
static int command_mapping_return_ledger(FridayPublisherRootCommandReceipt *r) {
    for(uint64_t i=0;i<root_storage.own_values.count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        FridayPublisherOwnValue *p=&root_storage.own_values.rows[i];
        if(p->kind!=OWN_MAPPING||p->pid!=getpid())continue;
        int unknown=(p->flags&4)&&p->attempts>=3&&p->confirmed!=3;
        if((p->flags&16)||command_mapping_phase(p)==3||unknown) {
            r->unresolved_required_nodes++;
        }
        if(FridayPublisherMasterBefore(&root_storage.pool,4096,0,0,0)<0)return -1;
        if(p->flags&OWN_MAPPING_PREBIRTH) {
            /* First-field failure has no promised creation dictionary/cuts.
             * The exact preowned row/carrier/error body is read by the same
             * no-map native trailer and generic DATA graph, before/final end. */
            if(command_unmapped_phase(p)!=3||!(p->flags&32)||!p->refs[3]||
               p->refs[6]||p->attempts||p->confirmed)return -1;
            continue;
        }
        PyObject *creation=p->refs[8];
        PyObject *returned=own_plain_field(creation,"returned");
        PyObject *alias=own_plain_field(creation,"returned_mapping");
        PyObject *original=own_plain_field(creation,"original_error");
        PyObject *complete=own_plain_field(creation,"state_complete");
        if(p->refs[6]&&(p->flags&8))return -1;
        if(p->flags&32) {
            int no_map=p->attempts==0||
                (p->attempts==1&&(p->flags&8)&&p->error_saved&&p->error_type);
            if(!no_map||p->refs[6]||p->confirmed||(p->flags&(2|4|16))||
               returned!=Py_False||alias!=Py_None||complete!=Py_False)return -1;
        }
        if((p->flags&8)&&!p->refs[6]&&creation&&(returned!=Py_False||alias!=Py_None))return -1;
        if(p->refs[6]&&!(p->flags&16)&&(returned!=Py_True||alias!=p->refs[6]))return -1;
        if((p->flags&16)&&(complete==Py_True||(original&&original!=Py_None)||(p->flags&8)||command_mapping_phase(p)==1))return -1;
    }
    return 0;
}
/* Phase 1: full live buffer after the phase word, 16 edges.
 * Phase 2: phase word only, 15 edges; historical bytes stay in the row.
 * Phase 3 and every other scalar are unresolved. */
static int command_mapping_completed_body(const unsigned char *body,uint64_t bytes,uint64_t edges) {
    uint64_t phase=0,width=0,prefix=7*sizeof(uint64_t)+15,need=prefix+sizeof(phase);
    if(!body||bytes<need)return 0;
    memcpy(&phase,body+prefix,sizeof(phase));
    memcpy(&width,body+5*sizeof(uint64_t),sizeof(width));
    if(phase==1&&edges==16&&bytes-need==width)return 1;
    if(phase==2&&edges==15&&bytes==need)return 2;
    return 0;
}
static int command_mapping_retirement_blocked(const FridayPublisherRootCommandReceipt *r) {
    for(uint64_t i=0;i<r->node_count;i++) {
        const FridayPublisherRootValueNode *v=&r->nodes[i];
        if(v->kind!=COMMAND_VALUE_MAPPING)continue;
        if(!v->data_read||!command_mapping_completed_body(
           r->body+v->body_at,v->body_bytes,v->edges))return 1;
    }
    for(uint64_t i=0;i<root_storage.own_values.count;i++) {
        FridayPublisherOwnValue *p=&root_storage.own_values.rows[i];
        if(p->kind!=OWN_MAPPING||p->pid!=getpid())continue;
        int unknown=(p->flags&4)&&p->attempts>=3&&p->confirmed!=3;
        if((p->flags&16)||command_mapping_phase(p)==3||unknown||
           (!p->owner_at[6]&&(!command_unmapped_phase(p)||!(p->flags&32))))return 1;
    }
    return 0;
}
static int command_mapping_tail(FridayPublisherRootCommandReceipt *r,
    FridayPublisherOwnValue *p,const unsigned char *expected,uint64_t bytes) {
    uint64_t phase=command_mapping_phase(p);
    if(!phase)return -1;
    if(expected) {
        uint64_t supplied=0;if(bytes<sizeof(supplied))return -1;
        memcpy(&supplied,expected,sizeof(supplied));if(supplied!=phase)return -1;
    } else if(command_append(r,&phase,sizeof(phase))<0)return -1;
    if(phase==2) {
        /* AFTER confirmed close, no buffer/tell/len/fstat/reopen is attempted. */
        PyObject *raw=own_plain_field(p->refs[2],"full_bytes");
        if(!raw||!PyBytes_CheckExact(raw)||(uint64_t)PyBytes_GET_SIZE(raw)!=p->width||
           own_plain_field(p->refs[2],"actual_row")!=p->refs[4]||
           own_plain_field(p->refs[2],"actual_credit")!=p->refs[5])return -1;
        if(expected&&(bytes!=sizeof(phase)||r->nodes[r->active_node-1].edges!=15))return -1;
        return 0;
    }
    if(phase==3) {
        /* Unknown close stays off CURRENT16 and RETIRED15. No buffer, tell, or fstat. */
        if(expected&&(bytes!=sizeof(phase)||r->nodes[r->active_node-1].edges!=15))return -1;
        return 0;
    }
    if(p->width>SIZE_MAX||p->width>UINT64_MAX/3||
       (expected&&bytes!=sizeof(phase)+p->width))return -1;
    if(!p->refs[6]||!p->refs[10]||!PyType_Check(p->refs[10])||
       Py_TYPE(p->refs[6])!=(PyTypeObject *)p->refs[10]||
       !r->active_node||r->active_node>r->node_count||
       r->nodes[r->active_node-1].original!=p->refs[6]||
       r->active_buffer_owned||r->active_buffer.obj||
       (!expected&&(r->body_bytes>FRIDAY_ROOT_COMMAND_BYTES||
                    p->width>FRIDAY_ROOT_COMMAND_BYTES-r->body_bytes))||
       command_factory_begin(r,"actual_current_owned_mmap_full_buffer")<0||
       FridayPublisherMasterBefore(&root_storage.pool,(expected?2:3)*p->width,0,0,0)<0||
       !command_clock(r))return -1;
    /* A267: all fallible debit/capacity/clock checks precede the export.
     * Own and initialize the whole actual destination before acquisition;
     * a failed getter leaves an incomplete immutable segment, not full DATA.
     * The graph already strongly owns this exact mapping across Release. */
    unsigned char *destination=NULL;
    if(!expected) {
        destination=r->body+r->body_bytes;
        memset(destination,0,(size_t)p->width);r->body_bytes+=p->width;
        r->buffer_copy_attempted=1;r->buffer_copy_rc=-1;
    }
    memset(&r->active_buffer,0,sizeof(r->active_buffer));
    if(PyObject_GetBuffer(p->refs[6],&r->active_buffer,PyBUF_SIMPLE)<0) {
        /* Preserve even a nonconforming partial export; no release retry,
         * exception normalization or fabricated confirmation on failure. */
        r->active_buffer_owned=r->active_buffer.obj!=NULL;return -1;
    }
    r->active_buffer_owned=1;
    if(PyErr_Occurred()||r->active_buffer.obj!=p->refs[6])return -1;
    int shape_ok=r->active_buffer.len>=0&&
        (uint64_t)r->active_buffer.len==p->width&&
        (r->active_buffer.buf||!p->width)&&!r->active_buffer.readonly;
    int rc=-1;
    if(shape_ok) {
        if(expected)rc=p->width?memcmp(r->active_buffer.buf,
            expected+sizeof(phase),(size_t)p->width):0;
        else {if(p->width)memcpy(destination,r->active_buffer.buf,(size_t)p->width);rc=0;}
    }
    /* No Python getter/factory, clock, append or resource operation occurs
     * while the successful export is held. Even a shape/byte mismatch
     * reaches this one public Release before returning its real refusal.
     * This ends ONLY our temporary export, never the mmap or Source owner. */
    PyBuffer_Release(&r->active_buffer);
    if(PyErr_Occurred()||r->active_buffer.obj)return -1;
    r->active_buffer_owned=0; /* observed public Release postcondition */
    if(!shape_ok||rc||!command_clock(r))return -1;
    if(!expected)r->buffer_copy_rc=0;
    if(command_factory_begin(r,"actual_owned_mmap_current_position")<0)return -1;
    PyMethodDef *tell=NULL;
    for(PyMethodDef *m=((PyTypeObject *)p->refs[10])->tp_methods;m&&m->ml_name;m++)
        if(!strcmp(m->ml_name,"tell")&&m->ml_flags==METH_NOARGS){tell=m;break;}
    if(!tell)return -1;
    r->getter_pending=tell->ml_meth(p->refs[6],NULL);
    if(!r->getter_pending||!PyLong_CheckExact(r->getter_pending))return -1;
    if(!expected)return command_generated(r,1);
    /* Reader: compare actual current position against the strongly retained
     * producer result. No new graph/edge is appended during consumption. */
    FridayPublisherRootValueNode *v=&r->nodes[r->active_node-1];
    if(v->edges!=16)return -1;
    uint64_t position_id=r->edges[v->edge_at+15];
    if(!position_id||position_id>r->node_count||
       !PyLong_CheckExact(r->nodes[position_id-1].original))return -1;
    int same=PyObject_RichCompareBool(r->getter_pending,r->nodes[position_id-1].original,Py_EQ);
    if(same!=1)return -1;
    PyObject *position=r->getter_pending;r->getter_pending=NULL;Py_DECREF(position);
    return PyErr_Occurred()?-1:0;
}
static int command_own_value(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v,FridayPublisherOwnValue *p,uint64_t kind) {
    if(!p||p->pid!=getpid())return 1;
    if((p->kind==OWN_TOKEN&&(p->flags&8))||
       (p->kind==OWN_MAPPING&&!command_mapping_phase(p)))return 1;
    v->kind=kind;
    uint64_t scalar[]={p->kind,p->serial,p->flags,p->attempts,p->confirmed,p->width,(uint64_t)p->pid};
    if(command_append(r,scalar,sizeof(scalar))<0)return -1;
    for(unsigned j=0;j<FRIDAY_OWN_FIELDS;j++) {
        int support=(j==0)||(p->kind==OWN_HASH&&(j==2||j==6))||
            (p->kind==OWN_MAPPING&&(j==10||j==11));
        if(command_nullable_edge(r,p->refs[j],support?0:1)<0)return -1;
        if(p->refs[j]&&p->kind==OWN_MAPPING&&(j==10||j==11))
            r->nodes[r->edges[r->edge_count-1]-1].support_verified=2;
    }
    if(command_nullable_edge(r,p->error_type,0)<0||
       command_nullable_edge(r,p->error_value,1)<0||
       command_nullable_edge(r,p->error_tb,1)<0)return -1;
    if(p->kind==OWN_VAR) {
        PyObject *ctx=PyThreadState_Get()->context;
        int present=ctx?PySequence_Contains(ctx,p->refs[0]):0;
        if(present<0||command_append(r,&present,sizeof(present))<0||
           command_nullable_edge(r,ctx,1)<0)return -1;
        if(present) {
            if(command_factory_begin(r,"actual_ContextVar_current_value")<0)return -1;
            if(PyContextVar_Get(p->refs[0],NULL,&r->getter_pending)<0)return -1;
            if(command_generated(r,1)<0)return -1;
        } else if(command_nullable_edge(r,NULL,0)<0)return -1;
    }
    if(p->kind==OWN_MAPPING&&command_mapping_tail(r,p,NULL,0)<0)return -1;
    return 0;
}
static int command_own_selected(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v) {
    PyObject *o=v->original;FridayPublisherOwnValue *p;
    p=own_find(o,OWN_VAR);if(p)return command_own_value(r,v,p,COMMAND_VALUE_CONTEXT_VAR);
    p=own_find(o,OWN_TOKEN);if(p)return command_own_value(r,v,p,COMMAND_VALUE_CONTEXT_TOKEN);
    p=own_find(o,OWN_HASH);if(p)return command_own_value(r,v,p,COMMAND_VALUE_HASH);
    for(uint64_t i=0;i<root_storage.own_values.count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        p=&root_storage.own_values.rows[i];
        if(p->kind==OWN_MAPPING&&p->refs[6]==o)return command_own_value(r,v,p,COMMAND_VALUE_MAPPING);
    }
    if(Py_TYPE(o)==&PyContext_Type) {
        v->kind=COMMAND_VALUE_CONTEXT;
        PyMethodDef *items=NULL;
        for(PyMethodDef *m=PyContext_Type.tp_methods;m&&m->ml_name;m++)
            if(!strcmp(m->ml_name,"items")&&m->ml_flags==METH_NOARGS){items=m;break;}
        if(!items||command_factory_begin(r,"actual_owned_Context_items")<0)return -1;
        r->getter_pending=items->ml_meth(o,NULL);
        return command_generated(r,1);
    }
    return 2; /* not selected here */
}

/* SOL119: public atomic tuple materialization keeps the stock iterator
 * INTERNAL to that public factory. The returned actual tuple moves into the
 * SAME original owner bank BEFORE metadata, index or edge publication. No
 * tuple reconstruction, private set table, member hash/equality or replay.
 * Exact mutable-set current identity remains an explicit prerequisite: its
 * retained tuple proves a historical phase only. Frozen membership is an
 * immutable public stock relation, subject to the unchanged ABI admission. */
#define COMMAND_SET_MAGIC 267119ULL
static uint64_t command_set_node(FridayPublisherRootCommandReceipt *r,PyObject *o) {
    if(!o)return 0;
    uintptr_t p=(uintptr_t)o;
    uint64_t slot=((p>>4)^(p>>25))&(FRIDAY_ROOT_COMMAND_INDEX-1);
    for(uint64_t n=0;n<FRIDAY_ROOT_COMMAND_INDEX;n++) {
        /* Same index/address/node relation as command_node, with the added
         * set-capture lookup paid BEFORE EACH actual probe. No worst-case
         * blanket grant and no hash/equality callback on a Source member. */
        if(secondary_debit_scan(4,sizeof(FridayPublisherRootValueNode))<0)return 0;
        if((n&1023)==0&&!command_clock(r))return 0;
        r->index_probes++;uint64_t id=r->index[slot];
        if(id) {
            if(id>r->node_count)return 0;
            if(r->nodes[id-1].original==o)return id;
        } else {
            if(r->node_count>=FRIDAY_ROOT_COMMAND_NODES)return 0;
            id=++r->node_count;r->index[slot]=id;
            r->nodes[id-1].id=id;r->nodes[id-1].pointer_index_slot=slot;
            r->nodes[id-1].original=Py_NewRef(o);
            r->nodes[id-1].first_parent=r->active_node;
            r->nodes[id-1].first_edge=r->edge_count;
            r->graph_owned_nodes++;return id;
        }
        slot=(slot+1)&(FRIDAY_ROOT_COMMAND_INDEX-1);
    }
    return 0;
}
/* One finite radix join of the ACTUAL existing pointer->NODE index, BEFORE
 * any current factory in a phase. Actual node birth slots authenticate every
 * numeric ID; byte radix uses only uintptr identity, no Source hash/equality.
 * For selected pointer width B, this is O(B*N), never n-by-n membership or
 * collision-chain traversal. Same original static Root storage/caps/pool. */
static int command_set_index_begin(FridayPublisherRootCommandReceipt *r,unsigned phase,
                                   uint64_t limit) {
    RootSetPointerIndex *s=&root_storage.set_pointer_index;
    if(phase<1||phase>3||limit>r->node_count||limit>FRIDAY_ROOT_COMMAND_NODES||
       r->set_index_attempted[phase-1]||root_storage.set_region.active_phase!=phase||
       secondary_debit_scan(8*limit+32,0)<0)return -1;
    r->set_index_attempted[phase-1]=1;r->set_index_nodes[phase-1]=limit;
    s->phase=0;s->limit=limit;
    for(uint64_t i=0;i<limit;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        const FridayPublisherRootValueNode *v=&r->nodes[i];
        if(v->id!=i+1||!v->original||v->pointer_index_slot>=FRIDAY_ROOT_COMMAND_INDEX||
           r->index[v->pointer_index_slot]!=v->id)return -1;
        s->order[i]=i+1;
    }
    uint64_t *from=s->order,*to=s->next;
    for(unsigned shift=0;shift<8*sizeof(uintptr_t);shift+=8) {
        if(secondary_debit_scan(8*limit+512,0)<0||!command_clock(r))return -1;
        memset(s->prefix,0,sizeof(s->prefix));
        for(uint64_t i=0;i<limit;i++) {
            if((i&1023)==0&&!command_clock(r))return -1;
            uintptr_t key=(uintptr_t)r->nodes[from[i]-1].original;
            s->prefix[(unsigned)((key>>shift)&255U)]++;
        }
        uint64_t total=0;
        for(unsigned bucket=0;bucket<256;bucket++) {
            uint64_t count=s->prefix[bucket];s->prefix[bucket]=total;
            if(count>limit-total)return -1;total+=count;
        }
        if(total!=limit)return -1;
        for(uint64_t i=0;i<limit;i++) {
            if((i&1023)==0&&!command_clock(r))return -1;
            uintptr_t key=(uintptr_t)r->nodes[from[i]-1].original;
            unsigned bucket=(unsigned)((key>>shift)&255U);
            uint64_t at=s->prefix[bucket]++;
            if(at>=limit)return -1;to[at]=from[i];
        }
        uint64_t *swap=from;from=to;to=swap;
    }
    if(secondary_debit_scan(8*limit+32,0)<0)return -1;
    if(from!=s->order)memcpy(s->order,from,limit*sizeof(uint64_t));
    for(uint64_t i=1;i<limit;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        uintptr_t previous=(uintptr_t)r->nodes[s->order[i-1]-1].original;
        uintptr_t current=(uintptr_t)r->nodes[s->order[i]-1].original;
        if(previous>=current)return -1; /* exact duplicate/index inconsistency */
    }
    if(!command_clock(r)||PyErr_Occurred())return -1;
    s->phase=phase;r->set_index_complete[phase-1]=1;return 0;
}
/* Lookup-only finite binary read of that actual joined index. With unchanged
 * node capacity262144 there are at most19 probes/member, including a miss.
 * A replacement cannot create a node; no collision-dependent quadratic path.
 * Address order is PRIVATE bookkeeping, never an object ordering oracle. */
static uint64_t command_set_find(FridayPublisherRootCommandReceipt *r,PyObject *o) {
    RootSetPointerIndex *s=&root_storage.set_pointer_index;
    uint64_t phase=s->phase;
    if(!o||phase<1||phase>3||phase!=root_storage.set_region.active_phase||
       r->set_index_complete[phase-1]!=1||s->limit!=r->set_index_nodes[phase-1]||
       s->limit>r->node_count||s->limit>FRIDAY_ROOT_COMMAND_NODES)return 0;
    uintptr_t address=(uintptr_t)o;
    uint64_t low=0,high=s->limit;
    for(unsigned probe=0;low<high&&probe<19;probe++) {
        if(secondary_debit_scan(4,0)<0||r->index_probes==UINT64_MAX)return 0;
        if(!probe&&!command_clock(r))return 0;
        uint64_t middle=low+(high-low)/2,id=s->order[middle];
        r->index_probes++;
        if(!id||id>s->limit)return 0;
        const FridayPublisherRootValueNode *v=&r->nodes[id-1];
        if(v->pointer_index_slot>=FRIDAY_ROOT_COMMAND_INDEX||
           r->index[v->pointer_index_slot]!=id||!v->original)return 0;
        uintptr_t current=(uintptr_t)v->original;
        if(address==current)return v->original==o?id:0;
        if(address<current)high=middle;else low=middle+1;
    }
    return 0;
}
static int command_set_current_edge(FridayPublisherRootCommandReceipt *r,PyObject *o) {
    uint64_t id=command_set_find(r,o);
    if(!id||r->edge_count>=FRIDAY_ROOT_COMMAND_EDGES||
       secondary_debit_scan(4,sizeof(uint64_t)+1)<0)return -1;
    r->edges[r->edge_count]=id;r->edge_roles[r->edge_count++]=1;return 0;
}
static int command_set_edge(FridayPublisherRootCommandReceipt *r,PyObject *o,int role) {
    uint64_t node=command_set_node(r,o);
    if(!node||r->edge_count>=FRIDAY_ROOT_COMMAND_EDGES||
       secondary_debit_scan(4,sizeof(uint64_t)+1)<0)return -1;
    r->edges[r->edge_count]=node;r->edge_roles[r->edge_count++]=(unsigned char)role;return 0;
}
/* A282: prospective REQUESTED stock storage/work for BOTH tuple factories.
 * The selected CPython 3.14 conventional-GIL implementation scans mask+1,
 * not used entries. PySequence_Tuple has stack[8], then a list initially16;
 * append growth is >=9/8 and its last capacity <=max(16,n+n/8+6).
 * Thus the SUM of every requested list item buffer is <=9*last_upper.
 * Charging that sum also covers old/new overlap and a failed last realloc.
 * These are conditional selected-stock request terms, not allocator arenas,
 * callbacks, last-loss neutrality, image/ABI admission or whole C2 fit.
 * Existing nominal factory debit and all other original charges remain.
 */
static int command_set_request_before(FridayPublisherRootCommandReceipt *r,
                                      PyObject *value,uint64_t count) {
    if(!r||!value||(!PySet_CheckExact(value)&&!PyFrozenSet_CheckExact(value))||
       secondary_debit_scan(16,0)<0)return -1;
    PySetObject *actual=(PySetObject *)value;
    if(actual->used<0||actual->fill<actual->used||actual->mask<0||
       (uint64_t)actual->used!=count||!actual->table)
        return FridayPublisherMasterFault(&root_storage.pool,"set_stock_extent_relation");
    uint64_t table=(uint64_t)actual->mask+1;
    if(table<PySet_MINSIZE||(table&(table-1))||
       (uint64_t)actual->fill>table||table>UINT64_MAX/sizeof(setentry))
        return FridayPublisherMasterFault(&root_storage.pool,"set_stock_table_width");
    uint64_t arrays=count; /* final tuple pointers, including failure prefix */
    if(count>=8) {
        uint64_t capacity=count;
        if(plus(&capacity,count/8)<0||plus(&capacity,6)<0)
            return FridayPublisherMasterFault(&root_storage.pool,"set_stock_capacity_overflow");
        if(capacity<16)capacity=16;
        if(capacity>UINT64_MAX/9||plus(&arrays,capacity*9)<0)
            return FridayPublisherMasterFault(&root_storage.pool,"set_stock_buffer_sum_overflow");
    }
    if(arrays>UINT64_MAX/(2*sizeof(PyObject *)))
        return FridayPublisherMasterFault(&root_storage.pool,"set_stock_array_bytes_overflow");
    uint64_t array_bytes=arrays*sizeof(PyObject *);
    /* Selected exact iterator: PyObject_HEAD,set*,used,pos,len. Tuple sizeof
     * includes its inline item, so this header term is conservative even at0.
     * Three selected GC preheaders and struct rounding are retained too.
     * No copied struct is used to read or substitute an actual stock object.
     * Free-list reuse does not refund this original cumulative debit. */
    uint64_t headers=sizeof(PyObject)+sizeof(PyObject *)+3*sizeof(Py_ssize_t)+
        sizeof(PyTupleObject)+sizeof(PyListObject)+6*sizeof(uintptr_t)+
        4*_Alignof(max_align_t)+8*sizeof(PyObject *);
    uint64_t allocation=array_bytes,reads=table*sizeof(setentry);
    if(plus(&allocation,headers)<0||plus(&reads,2*array_bytes)<0||
       plus(&reads,headers)<0)
        return FridayPublisherMasterFault(&root_storage.pool,"set_stock_request_sum_overflow");
    uint64_t recorded=r->getter_reservation_bytes;
    if(plus(&recorded,allocation)<0)
        return FridayPublisherMasterFault(&root_storage.pool,"set_stock_receipt_sum_overflow");
    /* Scalar checks precede the SAME pool debit, which precedes materializing
     * any iterator/list/tuple. Both native final readers already consume this
     * original pool and getter counter; there is no new grant or witness flag.
     * The existing finite set interval remains a required admission condition. */
    if(FridayPublisherMasterBefore(&root_storage.pool,reads,0,0,allocation)<0)return -1;
    r->getter_reservation_bytes=recorded;return 0;
}
static int command_set_encode(FridayPublisherRootCommandReceipt *r,
                              FridayPublisherRootValueNode *v) {
    if(secondary_debit_scan(16,0)<0)return -1;
    PyObject *o=v->original;
    int frozen=PyFrozenSet_CheckExact(o);
    if(!frozen&&!PySet_CheckExact(o))return -1;
    v->kind=frozen?COMMAND_VALUE_FROZENSET:COMMAND_VALUE_SET;
    if(!v->set_factory_attempted) {
        /* Reserve original owner/graph/edge capacity and selected requested
         * stock terms BEFORE the only factory. Whole implicit cost is open. */
        Py_ssize_t before=PySet_Size(o);
        if(before<0||(uint64_t)before>=FRIDAY_ROOT_COMMAND_EDGES||
           r->owner_count>=FRIDAY_ROOT_COMMAND_OWNERS||
           r->node_count>=FRIDAY_ROOT_COMMAND_NODES||
           (uint64_t)before>UINT64_MAX/sizeof(PyObject *)||
           secondary_debit_scan((uint64_t)before+16,
               (uint64_t)before*sizeof(PyObject *))<0||
           command_set_request_before(r,o,(uint64_t)before)<0||
           command_factory_begin(r,"exact_set_atomic_public_tuple")<0)return -1;
        v->set_factory_attempted=1; /* before sole effect, even on NULL/error */
        r->getter_pending=PySequence_Tuple(o);
        if(!r->getter_pending)return -1;
        /* No destructor here: MOVE the first returned owner, including a
         * result returned with an actual pending exception. That indicator
         * is handed to the existing first cleanup-error triple by caller. */
        uint64_t owner_at=r->owner_count+1;
        if(command_owner(r,&r->getter_pending,24,v->id,1)<0)return -1;
        v->set_result_owner_at=owner_at;r->getter_results++;
        if(PyErr_Occurred())return -1;
    }
    if(!v->set_result_owner_at||v->set_result_owner_at>r->owner_count) {
        /* A NULL/failed factory has no invented members. The secondary
         * traversal records this missing prerequisite, never calls again. */
        if(r->secondary_read_attempted)r->secondary_factory_blocked=1;
        r->residual="set_actual_tuple_missing_no_secondary_factory_replay";
        return -1;
    }
    FridayPublisherRootCommandOwner *owner=&r->owners[v->set_result_owner_at-1];
    PyObject *tuple=owner->owned;
    if(owner->group!=24||owner->index!=v->id||!owner->required||
       !tuple||!PyTuple_CheckExact(tuple)||PyErr_Occurred())return -1;
    Py_ssize_t count=PyTuple_GET_SIZE(tuple);
    if(count<0||(uint64_t)count>=FRIDAY_ROOT_COMMAND_EDGES||
       r->edge_count>FRIDAY_ROOT_COMMAND_EDGES||
       (uint64_t)count+1>FRIDAY_ROOT_COMMAND_EDGES-r->edge_count)return -1;
    /* Full tuple/member walk and copies precede their original per-probe
     * debit. Conservative explicit floors, NOT implicit ABI/fit evidence. */
    uint64_t edges=(uint64_t)count+1;
    if(secondary_debit_scan(edges*4+16,
           edges*sizeof(uint64_t)+4*sizeof(uint64_t))<0||!command_clock(r))return -1;
    uint64_t node=command_set_node(r,tuple);
    if(!node||(v->set_tuple_node&&v->set_tuple_node!=node)||
       (owner->node&&owner->node!=node))return -1;
    owner->node=node;v->set_tuple_node=node;
    /* phase1=historical mutable membership; phase2=immutable frozenset.
     * Full result and EVERY actual member are DATA edges, even on failure. */
    uint64_t head[]={COMMAND_SET_MAGIC,frozen?2ULL:1ULL,(uint64_t)count,node};
    if(command_append(r,head,sizeof(head))<0||command_set_edge(r,tuple,1)<0)return -1;
    for(Py_ssize_t j=0;j<count;j++) {
        if((j&1023)==0&&!command_clock(r))return -1;
        if(command_set_edge(r,PyTuple_GET_ITEM(tuple,j),1)<0)return -1;
    }
    if(!command_clock(r))return -1;
    /* This bit describes the FULL historical body only. Mutable membership
     * additionally requires an actual current-phase witness in BOTH live
     * consumers and the before-loss guard; the final reader joins its record.
     * No completed segment is later rewritten to upgrade its data_read bit. */
    v->data_read=1;
    return 0;
}

static int command_secondary_buffer(FridayPublisherRootCommandReceipt *,
    FridayPublisherRootValueNode *,FridayPublisherSecondaryCut *);
static int command_extended(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v) {
    PyObject *o=v->original;
    if(v->support_verified==2) {
        v->kind=COMMAND_VALUE_SUPPORT;return command_named(r,"actual_foreign_runtime_namespace_SUPPORT_ONLY");
    }
    int selected=command_own_selected(r,v);if(selected!=2)return selected;
    if(r->frame_locals_type&&Py_TYPE(o)==r->frame_locals_type) {
        v->kind=COMMAND_VALUE_FRAME_LOCALS;
        /* Builtin stock frame-locals proxy copy, NEVER a user iterator or
         * method lookup. Actual cut is a separate new dict identity, not
         * falsely the original proxy or its hidden storage. */
        PyMethodDef *copy=NULL;
        for(PyMethodDef *d=Py_TYPE(o)->tp_methods;d&&d->ml_name;d++)
            if(strcmp(d->ml_name,"copy")==0&&d->ml_flags==METH_NOARGS){copy=d;break;}
        if(!copy||command_factory_begin(r,"actual_stock_frame_locals_cut")<0)return -1;
        r->getter_pending=copy->ml_meth(o,NULL);
        return command_generated(r,1);
    }

    if(PyCapsule_CheckExact(o)) {
        const FridayPublisherOwnedRun *q=NULL;
        int actual=FridayPublisherCallerContextFull(o,&q);if(actual<0)return -1;
        if(!actual)return 1;
        v->kind=COMMAND_VALUE_NATIVE_CONTEXT;
        if(command_named(r,"friday.publisher.existing-root-owned-run.v1")<0)return -1;
#define SCALAR(x) do {if(command_append(r,&q->x,sizeof(q->x))<0)return -1;} while(0)
        SCALAR(owner_pid);SCALAR(started);SCALAR(serial);SCALAR(charged_allocation);SCALAR(bank_total);
        SCALAR(close_count);SCALAR(end_attempted);SCALAR(final_close_attempted);
        SCALAR(final_close_confirmed);SCALAR(pending_close_rc);SCALAR(pending_close_errno);
        SCALAR(pending_close_published);SCALAR(caller_packet_verified);SCALAR(release_attempted);
        SCALAR(references_retired);
        SCALAR(binding_storage.final_fd);SCALAR(binding_storage.root_ram_remaining);
        SCALAR(binding_storage.document_limit);SCALAR(binding_storage.body_limit);SCALAR(binding_storage.event_limit);
        SCALAR(prefix_error.saved);SCALAR(prefix_error.syscall_attempted);SCALAR(prefix_error.syscall_rc);
        SCALAR(prefix_error.syscall_errno);SCALAR(prefix_error.actual_written);
        SCALAR(prefix_error.cut_serial);SCALAR(prefix_error.cut_state);
        SCALAR(after_document_error.saved);SCALAR(after_document_error.syscall_attempted);
        SCALAR(after_document_error.syscall_rc);SCALAR(after_document_error.syscall_errno);
        SCALAR(after_document_error.actual_written);
        SCALAR(after_document_error.cut_serial);SCALAR(after_document_error.cut_state);
#undef SCALAR
        if(command_named(r,q->prefix_error.phase?q->prefix_error.phase:"")<0||
           command_named(r,q->after_document_error.phase?q->after_document_error.phase:"")<0)return -1;
        PyObject *fields[]={q->anchors,q->banks,q->errors,q->result,q->final_raw,q->source_end,
            q->capsule,q->source_qualification,q->registered_module,q->pending_close_row,
            q->source_entry,q->source_args,q->source_return,q->caller_packet,q->final_caller_packet,
            q->binding_storage.root_fact,q->binding_storage.qualification,q->binding_storage.held_root_tool_preimage,
            q->binding_storage.final_fd_row,q->binding_storage.final_fd_credit,
            q->prefix_error.error_type,q->prefix_error.error_value,q->prefix_error.error_tb,
            q->prefix_error.actual_document,q->prefix_error.accepted_cut,
            q->after_document_error.error_type,q->after_document_error.error_value,q->after_document_error.error_tb,
            q->after_document_error.actual_document,q->after_document_error.accepted_cut,
            q->source_error,q->source_error_phase,q->source_error_tb,q->source_error_record};
        for(size_t i=0;i<sizeof(fields)/sizeof(*fields);i++) {
            int support=i==8||i==10;
            if(i==20||i==25) {
                PyObject *value=fields[i+1];
                if(fields[i]||value) {
                    if(!value||!PyExceptionInstance_Check(value)||
                       fields[i]!=(PyObject *)Py_TYPE(value))return -1;
                    support=command_builtin_exception(Py_TYPE(value));
                }
            }
            if(command_nullable_edge(r,fields[i],support?0:1)<0)return -1;
        }
        /* Native Release/Handback moved original references into these exact
         * actual recipient roots. Link the real aliases, don't reconstitute
         * old Run fields from counts or accept a dead pointer as full state. */
        PyObject *packet=command_original(r,15,0);
        if(packet) {if(command_role_edge(r,packet,1)<0)return -1;}
        else for(uint64_t i=0;i<FRIDAY_PUBLISHER_RUN_ROOTS;i++) {
            int data=!((i==17&&r->failure_type_kind[0]==1)||(i==22&&r->failure_type_kind[1]==1));
            if(command_role_edge(r,command_original(r,18,i),data)<0)return -1;
        }
        /* Same pool is already a complete native-owned body in this receipt;
         * no encoding of debit function/cookie pointers as required data. */
        int pool_bound=q->binding_storage.original_master_pool==&root_storage.pool;
        return command_append(r,&pool_bound,sizeof(pool_bound));
    }

    if(PyExceptionInstance_Check(o))return command_error_value(r,v);
    if(Py_TYPE(o)==&PyTraceBack_Type) {
        v->kind=COMMAND_VALUE_TRACEBACK;PyTracebackObject *q=(PyTracebackObject *)o;
        if(command_role_edge(r,(PyObject *)q->tb_next,1)<0||
           command_role_edge(r,(PyObject *)q->tb_frame,1)<0||
           command_append(r,&q->tb_lasti,sizeof(q->tb_lasti))<0||
           command_append(r,&q->tb_lineno,sizeof(q->tb_lineno))<0)return -1;
        return 0;
    }
    if(Py_TYPE(o)==&PyFrame_Type) {
        static const char *fields[]={"f_code","f_globals","f_locals","f_lasti","f_lineno",
            "f_trace","f_trace_lines","f_trace_opcodes","f_builtins"};
        v->kind=COMMAND_VALUE_FRAME;
        for(size_t i=0;i<sizeof(fields)/sizeof(*fields);i++)
            if(command_attr(r,o,&PyFrame_Type,fields[i],i==8?0:1)<0)return -1;
        return 0;
    }
    if(Py_TYPE(o)==&PyCode_Type) {
        static const char *fields[]={"co_argcount","co_posonlyargcount","co_kwonlyargcount",
            "co_nlocals","co_stacksize","co_flags","co_code","co_consts","co_names",
            "co_varnames","co_filename","co_name","co_qualname","co_firstlineno",
            "co_linetable","co_exceptiontable","co_freevars","co_cellvars"};
        v->kind=COMMAND_VALUE_CODE;
        for(size_t i=0;i<sizeof(fields)/sizeof(*fields);i++)
            if(command_attr(r,o,&PyCode_Type,fields[i],1)<0)return -1;
        return 0;
    }

    if(Py_TYPE(o)==&PyModule_Type) {
        if(FridayPublisherOwnedModuleOriginal(o)) {
            v->kind=COMMAND_VALUE_NATIVE_MODULE;
            if(command_named(r,"publisher_owned_custody.actual_linked_same_Root_module")<0)return -1;
            PyObject *packet=command_original(r,15,0),*cap=NULL;
            if(packet&&PyTuple_CheckExact(packet)&&PyTuple_GET_SIZE(packet)==13) {
                PyObject *roots=PyTuple_GET_ITEM(packet,6);
                if(PyTuple_CheckExact(roots)&&PyTuple_GET_SIZE(roots)==FRIDAY_PUBLISHER_RUN_ROOTS)cap=PyTuple_GET_ITEM(roots,6);
            } else cap=command_original(r,18,6);
            if(!cap||cap==Py_None)return 1; /* pre-cap prefix has no invented context */
            return command_role_edge(r,cap,1);
        }
        v->kind=COMMAND_VALUE_SUPPORT;
        for(size_t i=0;i<sizeof(command_source_names)/sizeof(*command_source_names);i++) {
            PyObject *m=command_source_module(r,command_source_names[i]);
            if(PyErr_Occurred()||r->clock_fault_kind)return -1;
            if(m==o) {
                v->kind=COMMAND_VALUE_SOURCE_MODULE;
                if(command_named(r,command_source_names[i])<0)return -1;
                return command_role_edge(r,PyModule_GetDict(o),1);
            }
        }
        v->support_verified=1;
        /* Runtime module is retained as support, NOT accepted as required
         * value data or recursively inspected for a private heap body. */
        return command_named(r,"actual_foreign_module_SUPPORT_ONLY");
    }
    if(PyType_Check(o)) {
        const char *module=NULL,*name=NULL;
        int own=command_source_class(r,(PyTypeObject *)o,&module,&name);
        if(own<0)return -1;
        if(own) {
            v->kind=COMMAND_VALUE_CLASS;
            if(command_named(r,module)<0||command_named(r,name)<0)return -1;
            PyObject *dict=((PyTypeObject *)o)->tp_dict;
            if(command_role_edge(r,dict,1)<0)return -1;
            r->nodes[r->edges[r->edge_count-1]-1].support_verified=3;
            return 0;
        }
        v->kind=COMMAND_VALUE_SUPPORT;v->support_verified=1;
        return command_named(r,"actual_nonSource_type_SUPPORT_ONLY");
    }
    if(Py_TYPE(o)==&PyCFunction_Type||Py_TYPE(o)==&PyGetSetDescr_Type||
       Py_TYPE(o)==&PyMemberDescr_Type||Py_TYPE(o)==&PyMethodDescr_Type||
       Py_TYPE(o)==&PyWrapperDescr_Type) {
        v->kind=COMMAND_VALUE_SUPPORT;v->support_verified=1;
        return command_named(r,"actual_exported_runtime_callable_descriptor_SUPPORT_ONLY");
    }
    if(Py_TYPE(o)==&PyProperty_Type) {
        v->kind=COMMAND_VALUE_PROPERTY;
        const char *fields[]={"fget","fset","fdel","__doc__"};
        for(int i=0;i<4;i++)if(command_attr(r,o,&PyProperty_Type,fields[i],1)<0)return -1;
        return 0;
    }
    if(Py_TYPE(o)==&PyStaticMethod_Type||Py_TYPE(o)==&PyClassMethod_Type) {
        PyTypeObject *t=Py_TYPE(o);
        v->kind=t==&PyStaticMethod_Type?COMMAND_VALUE_STATICMETHOD:COMMAND_VALUE_CLASSMETHOD;
        if(command_attr(r,o,t,"__func__",1)<0)return -1;
        if(command_factory_begin(r,"actual_static_class_method_dictionary")<0)return -1;
        r->getter_pending=PyObject_GenericGetDict(o,NULL);
        return command_generated(r,1);
    }
    if(Py_TYPE(o)==&PyFunction_Type) {
        PyObject *globals=PyFunction_GetGlobals(o);
        int own=command_source_namespace(r,globals);if(own<0)return -1;
        if(!own)return 1; /* foreign function is not an invented Source value */
        v->kind=COMMAND_VALUE_FUNCTION;PyFunctionObject *q=(PyFunctionObject *)o;
        /* DO NOT read the lazy 3.14 __annotations__ descriptor: it may execute
         * Source. Preserve actual unforced dictionary + annotate thunk as
         * distinct exact values instead; no new evaluated annotation body. */
        PyObject *values[]={q->func_name,q->func_qualname,q->func_module,
            q->func_code,q->func_defaults,q->func_kwdefaults,q->func_dict,
            q->func_closure,q->func_annotations,q->func_annotate,q->func_globals,
            q->func_doc,q->func_typeparams};
        for(size_t i=0;i<sizeof(values)/sizeof(*values);i++)
            if(command_nullable_edge(r,values[i],1)<0)return -1;
        if(command_role_edge(r,q->func_builtins,0)<0)return -1;
        r->nodes[r->edges[r->edge_count-1]-1].support_verified=2;
        return 0;
    }
    if(Py_TYPE(o)==&PyMethod_Type) {
        v->kind=COMMAND_VALUE_METHOD;
        return command_role_edge(r,PyMethod_GET_SELF(o),1)<0||
            command_role_edge(r,PyMethod_GET_FUNCTION(o),1)<0?-1:0;
    }
    if(Py_TYPE(o)==&PyCell_Type) {
        v->kind=COMMAND_VALUE_CELL;PyObject *value=PyCell_GET(o);
        unsigned char present=value!=NULL;
        return command_append(r,&present,1)<0||command_role_edge(r,value,1)<0?-1:0;
    }

    if(Py_TYPE(o)==&PyMemoryView_Type) {
        /* Original node already owns the exact view. Borrow its public
         * descriptor; no second export survives an ordinary read failure.
         * The primary path has no historical cut dependency. */
        return command_secondary_buffer(r,v,NULL);
    }

    if(Py_TYPE(o)==&PyRange_Type||Py_TYPE(o)==&PySlice_Type) {
        PyTypeObject *t=Py_TYPE(o);v->kind=t==&PyRange_Type?COMMAND_VALUE_RANGE:COMMAND_VALUE_SLICE;
        const char *fields[]={"start","stop","step"};
        for(int i=0;i<3;i++)if(command_attr(r,o,t,fields[i],1)<0)return -1;
        return 0;
    }
    if(PySet_CheckExact(o)||PyFrozenSet_CheckExact(o)) {
        return command_set_encode(r,v);
    }
    if(o==Py_Ellipsis||o==Py_NotImplemented) {
        v->kind=o==Py_Ellipsis?COMMAND_VALUE_ELLIPSIS:COMMAND_VALUE_NOT_IMPLEMENTED;return 0;
    }
    const char *module=NULL,*name=NULL;
    int own=command_source_class(r,Py_TYPE(o),&module,&name);
    if(own<0)return -1;
    if(own) {
        v->kind=COMMAND_VALUE_SOURCE;
        if(command_named(r,module)<0||command_named(r,name)<0||
           command_role_edge(r,(PyObject *)Py_TYPE(o),0)<0||
           command_factory_begin(r,"actual_Source_instance_dictionary")<0)return -1;
        r->getter_pending=PyObject_GenericGetDict(o,NULL);
        return command_generated(r,1); /* actual complete dictionary, not copy */
    }
    return 1;
}

/* A266: read the already graph-owned exact memoryview's public buffer.
 * This is a BORROW, not another GetBuffer export. No release of this borrowed
 * descriptor and no new factory/owner survives an ordinary read failure.
 * The same C-order body/metadata serves primary, secondary, live and final
 * readers. Strides (including negative/zero) and indirect arrays are not a
 * promise of physically contiguous memory. Original caps/pool stay unchanged. */
static int command_view_piece(FridayPublisherRootCommandReceipt *r,
    const unsigned char *expected,uint64_t limit,uint64_t *at,
    const void *value,uint64_t n) {
    if(expected) {
        if(*at>limit||n>limit-*at||(n&&memcmp(expected+*at,value,(size_t)n)))return -1;
    } else if(limit!=UINT64_MAX&&command_append(r,value,n)<0)return -1;
    *at+=n;return 0;
}
static int command_view_metadata(FridayPublisherRootCommandReceipt *r,
    const Py_buffer *q,const unsigned char *expected,uint64_t limit,uint64_t *size) {
    if(!q||q->len<0||q->itemsize<=0||q->ndim<0||q->ndim>PyBUF_MAX_NDIM||
       (q->readonly!=0&&q->readonly!=1)||(q->len&&!q->buf)||
       (!q->ndim&&(q->shape||q->strides||q->suboffsets))||
       (q->strides&&!q->shape)||(q->suboffsets&&!q->strides)||
       (!q->shape&&q->ndim>1))return -1;
    if(FridayPublisherMasterBefore(&root_storage.pool,8193+(uint64_t)q->ndim*128,0,0,0)<0)return -1;
    uint64_t fmt=q->format?(uint64_t)strnlen(q->format,8193):0;
    if(fmt>8192)return -1;
    uint64_t product=1;int zero=0;
    if(q->shape) {
        for(int i=0;i<q->ndim;i++) {
            if(q->shape[i]<0)return -1;
            if(!q->shape[i])zero=1;
        }
        if(zero)product=0;
        else for(int i=0;i<q->ndim;i++) {
            uint64_t d=(uint64_t)q->shape[i];
            if(product>UINT64_MAX/d)return -1;product*=d;
        }
        if(product>UINT64_MAX/(uint64_t)q->itemsize||
           product*(uint64_t)q->itemsize!=(uint64_t)q->len)return -1;
    } else if(!q->ndim&&q->len!=q->itemsize)return -1;
    int64_t fields[5]={q->len,q->itemsize,q->ndim,q->readonly,
        (q->format?1:0)|(q->shape?2:0)|(q->strides?4:0)|(q->suboffsets?8:0)};
    uint64_t at=0;
    if(command_view_piece(r,expected,limit,&at,fields,sizeof(fields))<0||
       command_view_piece(r,expected,limit,&at,&fmt,sizeof(fmt))<0||
       command_view_piece(r,expected,limit,&at,q->format,fmt)<0)return -1;
    const Py_ssize_t *arrays[3]={q->shape,q->strides,q->suboffsets};
    for(unsigned j=0;j<3;j++)if(arrays[j])
        for(int i=0;i<q->ndim;i++) {
            int64_t value=arrays[j][i];
            if(command_view_piece(r,expected,limit,&at,&value,sizeof(value))<0)return -1;
        }
    if(expected&&at!=limit)return -1;
    *size=at;return 0;
}
static int command_view_raw(FridayPublisherRootCommandReceipt *r,
    const Py_buffer *q,const unsigned char *expected,uint64_t bytes) {
    if(!q||q->len<0||(uint64_t)q->len!=bytes||
       (bytes&&!q->buf)||q->ndim<0||q->ndim>PyBUF_MAX_NDIM)return -1;
    if(!bytes)return 0;
    int contiguous=PyBuffer_IsContiguous(q,'C');
    uint64_t items=0,steps=0;
    if(!contiguous) {
        if(!q->shape||!q->strides||q->itemsize<=0||bytes%(uint64_t)q->itemsize)return -1;
        items=bytes/(uint64_t)q->itemsize;
        uint64_t per=(uint64_t)q->ndim*4+4;
        if(items>UINT64_MAX/per)return -1;steps=items*per;
    }
    if(bytes>UINT64_MAX/2||steps>UINT64_MAX/64||
       steps*64>UINT64_MAX-2*bytes||
       FridayPublisherMasterBefore(&root_storage.pool,steps*64+2*bytes,0,0,0)<0||
       !command_clock(r))return -1;
    unsigned char *destination=NULL;
    if(!expected) {
        if(r->body_bytes>FRIDAY_ROOT_COMMAND_BYTES||
           bytes>FRIDAY_ROOT_COMMAND_BYTES-r->body_bytes)return -1;
        destination=r->body+r->body_bytes;
        /* Entire destination is owned/initialized before the first item read.
         * A failed prefix remains appended, never recycled as complete. */
        memset(destination,0,(size_t)bytes);r->body_bytes+=bytes;
        r->buffer_copy_attempted=1;r->buffer_copy_rc=-1;
    }
    if(contiguous) {
        if(expected) {if(memcmp(expected,q->buf,(size_t)bytes))return -1;}
        else memcpy(destination,q->buf,(size_t)bytes);
    } else {
        Py_ssize_t indices[PyBUF_MAX_NDIM];memset(indices,0,sizeof(indices));
        uint64_t at=0;
        for(uint64_t item=0;item<items;item++) {
            if((item&1023)==0&&!command_clock(r))return -1;
            const void *part=PyBuffer_GetPointer(q,indices);
            if(!part||(uint64_t)q->itemsize>bytes-at)return -1;
            if(expected) {if(memcmp(expected+at,part,(size_t)q->itemsize))return -1;}
            else memcpy(destination+at,part,(size_t)q->itemsize);
            at+=(uint64_t)q->itemsize;
            for(int d=q->ndim-1;d>=0;d--) {
                if(++indices[d]<q->shape[d])break;
                indices[d]=0;
            }
        }
        if(at!=bytes)return -1;
    }
    if(!command_clock(r))return -1;
    if(!expected)r->buffer_copy_rc=0;
    return 0;
}
/* Pure numeric retained-body reader: also used AFTER original Python end.
 * It never follows q/format/shape pointers or calls a Python accessor. */
static int command_view_body_shape(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherRootValueNode *v,uint64_t *meta_at,uint64_t *raw_at) {
    if(v->edges!=5||v->body_at>r->body_bytes||v->body_bytes>r->body_bytes-v->body_at||
       v->body_bytes<5*sizeof(int64_t)+5)return -1;
    if(command_unmapped_scan(256,r->source_owner_end_confirmed)<0)return -1;
    const unsigned char *body=r->body+v->body_at;int64_t head[5];memcpy(head,body,sizeof(head));
    if(head[0]!=266||(head[1]!=0&&head[1]!=1)||head[4]<0)return -1;
    uint64_t at=sizeof(head)+5,metadata=(uint64_t)head[4];
    for(unsigned j=0;j<5;j++)if(body[sizeof(head)+j]>1)return -1;
    if(metadata>v->body_bytes-at)return -1;
    *meta_at=v->body_at+at;*raw_at=*meta_at+metadata;
    if(head[1]) {
        if(head[2]!=-1||head[3]!=-1||metadata)return -1;
        uint64_t n=v->body_bytes-at;
        if(!body[sizeof(head)+1])return (!n&&!v->data_read)?0:-1;
        if(v->edge_at>r->edge_count||v->edges>r->edge_count-v->edge_at)return -1;
        uint64_t id=r->edges[v->edge_at+1];
        if(!id||id>r->node_count)return -1;
        const FridayPublisherRootValueNode *original=&r->nodes[id-1];
        if(original->kind!=COMMAND_VALUE_BYTES||!original->data_read||
           original->body_at>r->body_bytes||original->body_bytes>r->body_bytes-original->body_at||
           original->body_bytes!=n||!v->data_read||
           command_unmapped_scan(1+(2*n)/64,r->source_owner_end_confirmed)<0||
           (n&&memcmp(body+at,r->body+original->body_at,(size_t)n)))return -1;
        return 0;
    }
    if((head[2]!=0&&head[2]!=1)||head[3]<0||
       metadata<5*sizeof(int64_t)+sizeof(uint64_t))return -1;
    int64_t f[5];uint64_t fmt;memcpy(f,body+at,sizeof(f));at+=sizeof(f);
    memcpy(&fmt,body+at,sizeof(fmt));at+=sizeof(fmt);
    if(f[0]!=head[3]||f[1]<=0||f[2]<0||f[2]>PyBUF_MAX_NDIM||
       f[3]!=head[2]||f[4]<0||f[4]>15||fmt>8192||
       (!(f[4]&1)&&fmt)||fmt>v->body_bytes-at||
       (!f[2]&&(f[4]&14))||((f[4]&4)&&!(f[4]&2))||
       ((f[4]&8)&&!(f[4]&4))||(!(f[4]&2)&&f[2]>1))return -1;
    at+=fmt;
    unsigned arrays=!!(f[4]&2)+!!(f[4]&4)+!!(f[4]&8);
    uint64_t array_bytes=(uint64_t)f[2]*sizeof(int64_t);
    if(arrays*array_bytes>v->body_bytes-at||
       at+arrays*array_bytes!=sizeof(head)+5+metadata)return -1;
    if(f[4]&2) {
        uint64_t product=1;int zero=0;
        for(int64_t i=0;i<f[2];i++) {
            int64_t d;memcpy(&d,body+at+(uint64_t)i*sizeof(d),sizeof(d));
            if(d<0)return -1;if(!d)zero=1;
        }
        if(zero)product=0;
        else for(int64_t i=0;i<f[2];i++) {
            int64_t d;memcpy(&d,body+at+(uint64_t)i*sizeof(d),sizeof(d));
            if(product>UINT64_MAX/(uint64_t)d)return -1;product*=(uint64_t)d;
        }
        if(product>UINT64_MAX/(uint64_t)f[1]||product*(uint64_t)f[1]!=(uint64_t)f[0])return -1;
    } else if(!f[2]&&f[0]!=f[1])return -1;
    return v->body_bytes-(sizeof(head)+5+metadata)==(uint64_t)head[3]?0:-1;
}
static int command_secondary_buffer_check(FridayPublisherRootCommandReceipt *,
    FridayPublisherRootValueNode *);
static int command_builtin(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v) {
    PyObject *o=v->original;v->body_at=r->body_bytes;v->edge_at=r->edge_count;
    if(o==command_original(r,21,1))v->kind=COMMAND_VALUE_TOKEN_MISSING;
    else if(o==Py_None)v->kind=COMMAND_VALUE_NONE;
    else if(o==Py_True||o==Py_False) {
        v->kind=COMMAND_VALUE_BOOL;unsigned char b=o==Py_True;
        if(command_append(r,&b,1)<0)return -1;
    } else if(PyLong_CheckExact(o)) {
        v->kind=COMMAND_VALUE_LONG;
        /* Public selected stock ABI; signed little-endian FULL arbitrary-size
         * value, no __index__, repr, type/hash/pointer surrogate or truncation.
         * This new stock API/compiler/image relation remains C2 NOT_RUN. */
        Py_ssize_t n=PyLong_AsNativeBytes(o,NULL,0,Py_ASNATIVEBYTES_LITTLE_ENDIAN);
        if(n<0||PyErr_Occurred())return -1;
        if((uint64_t)n>FRIDAY_ROOT_COMMAND_BYTES-r->body_bytes)return -1;
        Py_ssize_t actual=PyLong_AsNativeBytes(o,r->body+r->body_bytes,n,Py_ASNATIVEBYTES_LITTLE_ENDIAN);
        if(actual!=n||PyErr_Occurred())return -1;r->body_bytes+=(uint64_t)n;
    } else if(PyFloat_CheckExact(o)) {
        v->kind=COMMAND_VALUE_FLOAT;double d=PyFloat_AS_DOUBLE(o);
        if(command_append(r,&d,sizeof(d))<0)return -1; /* exact selected native ABI */
    } else if(PyUnicode_CheckExact(o)) {
        v->kind=COMMAND_VALUE_UNICODE;
        Py_ssize_t n=PyUnicode_GET_LENGTH(o);int kind=PyUnicode_KIND(o);void *data=PyUnicode_DATA(o);
        if(n<0||(uint64_t)n>(FRIDAY_ROOT_COMMAND_BYTES-r->body_bytes)/sizeof(Py_UCS4))return -1;
        for(Py_ssize_t i=0;i<n;i++) {
            if((i&16383)==0) {
                uint64_t now=command_clock(r);if(!now||now>root_storage.pool.deadline_ns)return -1;
            }
            Py_UCS4 c=PyUnicode_READ(kind,data,i);
            memcpy(r->body+r->body_bytes,&c,sizeof(c));r->body_bytes+=sizeof(c);
        } /* all code points including surrogates, not lossy UTF8/repr */
    } else if(PyBytes_CheckExact(o)||PyByteArray_CheckExact(o)) {
        int b=PyBytes_CheckExact(o);v->kind=b?COMMAND_VALUE_BYTES:COMMAND_VALUE_BYTEARRAY;
        Py_ssize_t n=b?PyBytes_GET_SIZE(o):PyByteArray_GET_SIZE(o);
        void *raw=b?(void *)PyBytes_AS_STRING(o):(void *)PyByteArray_AS_STRING(o);
        if(n<0||command_append(r,raw,(uint64_t)n)<0)return -1;
    } else if(PyTuple_CheckExact(o)||PyList_CheckExact(o)) {
        int t=PyTuple_CheckExact(o);v->kind=t?COMMAND_VALUE_TUPLE:COMMAND_VALUE_LIST;
        Py_ssize_t n=t?PyTuple_GET_SIZE(o):PyList_GET_SIZE(o);
        if(n<0||(uint64_t)n>FRIDAY_ROOT_COMMAND_EDGES-r->edge_count)return -1;
        int operands=t?own_class_container(o):0;if(operands<0)return -1;
        FridayPublisherOwnValue *record=NULL;
        int actual_record=t?own_error_record_lookup(o,&record):0;if(actual_record<0)return -1;
        v->error_record_serial=actual_record?record->serial:0;
        for(Py_ssize_t i=0;i<n;i++) {
            PyObject *item=t?PyTuple_GET_ITEM(o,i):PyList_GET_ITEM(o,i);
            int support=(operands==1&&i>=2&&PyType_Check(item))||
                (actual_record&&i==2&&record->flags==1);
            /* Actual set factory returns an ordinary tuple, so its generic
             * tuple member/index consumer must also pay each real probe.
             * Existing class operand roles remain exactly as before. */
            if((t?command_set_edge(r,item,support?0:1):
                  command_role_edge(r,item,support?0:1))<0)return -1;
        }
    } else if(PyDict_CheckExact(o)) {
        int namespace=command_source_namespace(r,o);if(namespace<0)return -1;
        if(v->support_verified==2) {
            v->kind=COMMAND_VALUE_SUPPORT;
            if(command_named(r,"actual_frame_runtime_builtins_SUPPORT_ONLY")<0)return -1;
        } else {
            v->kind=COMMAND_VALUE_DICT;Py_ssize_t at=0;PyObject *key,*value;
            int operands=own_class_container(o);if(operands<0)return -1;
            FridayPublisherOwnValue *cell=NULL;int actual_cell=own_error_record_lookup(o,&cell);
            if(actual_cell<0)return -1;
            v->error_record_serial=actual_cell?cell->serial:0;
            while(PyDict_Next(o,&at,&key,&value)) {
                if((at&1023)==0&&!command_clock(r))return -1;
                int support=operands==2&&PyType_Check(value);
                if(actual_cell&&cell->flags==1&&own_plain_name(key,"original_type"))support=1;
                if(namespace||v->support_verified==3) {
                    FridayPublisherOwnValue *binding=NULL;
                    support|=own_support_relation(value,o,key,1,&binding);
                    if(PyErr_Occurred())return -1;
                }
                if(command_edge(r,key)<0||command_role_edge(r,value,support?0:1)<0)return -1;
                if(support&&PyDict_CheckExact(value))
                    r->nodes[r->edges[r->edge_count-1]-1].support_verified=2;
            }
        }
    } else {
        int rc=command_extended(r,v);if(rc<0)return -1;
        if(rc>0)v->kind=COMMAND_VALUE_UNSUPPORTED;
    }
    /* Prior body_bytes is still zero on a fresh node. Record the bytes and
     * edges just written, then qualify DATA from that completed body. */
    uint64_t written=r->body_bytes-v->body_at;
    uint64_t nedges=r->edge_count-v->edge_at;
    v->body_bytes=written;v->edges=nedges;
    uint64_t selected_data=v->data_read;
    v->data_read=v->kind!=COMMAND_VALUE_UNSUPPORTED&&v->kind!=COMMAND_VALUE_SUPPORT;
    if(v->kind==COMMAND_VALUE_MEMORYVIEW||v->kind==COMMAND_VALUE_SET||
       v->kind==COMMAND_VALUE_FROZENSET)v->data_read=selected_data;
    if(v->kind==COMMAND_VALUE_MAPPING&&
       !command_mapping_completed_body(r->body+v->body_at,written,nedges))
        v->data_read=0;
    return 0;
}

static void command_graph_begin(FridayPublisherRootCommandReceipt *r) {
    if(r->graph_started)return;
    r->graph_started=1;r->graph_body_at=r->body_bytes;r->graph_edge_at=r->edge_count;
}
static int command_segment_begin(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v,uint64_t encoding) {
    command_graph_begin(r);
    if(!v->id||v->id>r->node_count||!v->original||
       (encoding!=1&&encoding!=2)||
       r->segment_count>=FRIDAY_ROOT_COMMAND_SEGMENTS)return -1;
    if(v->segment) {
        if(v->segment>r->segment_count||!r->secondary_read_attempted)return -1;
        const FridayPublisherRootValueSegment *old=&r->segments[v->segment-1];
        if(!old->closed||old->secondary||old->node!=v->id)return -1;
    }
    FridayPublisherRootValueSegment *s=&r->segments[r->segment_count++];
    s->node=v->id;s->previous=v->segment;s->encoding=encoding;
    s->secondary=r->secondary_read_attempted!=0;
    s->body_at=r->body_bytes;s->edge_at=r->edge_count;
    v->segment=r->segment_count;v->encoding=encoding;
    v->body_at=s->body_at;v->edge_at=s->edge_at;
    v->body_bytes=0;v->edges=0;v->data_read=0;v->error_record_serial=0;
    return 0;
}
static int command_segment_end(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v,int complete) {
    if(!v->segment||v->segment!=r->segment_count)return -1;
    FridayPublisherRootValueSegment *s=&r->segments[v->segment-1];
    if(s->closed||s->node!=v->id||r->body_bytes<s->body_at||
       r->edge_count<s->edge_at)return -1;
    /* The actual written partial tail, not the last successful metadata.
     * No rollback/reuse of failed bytes, edges, nodes or original owners. */
    s->body_bytes=r->body_bytes-s->body_at;s->edges=r->edge_count-s->edge_at;
    s->kind=v->kind;s->data_read=v->data_read;s->closed=1;
    s->complete=complete!=0;
    v->body_at=s->body_at;v->edge_at=s->edge_at;
    v->body_bytes=s->body_bytes;v->edges=s->edges;
    return 0;
}
static int command_segment_relation(const FridayPublisherRootCommandReceipt *r,
    uint64_t at,uint64_t *next_body,uint64_t *next_edge) {
    if(at>=r->segment_count)return -1;
    const FridayPublisherRootValueSegment *s=&r->segments[at];
    if(!s->closed||s->closed>1||s->complete>1||s->secondary>1||
       (s->secondary&&!r->secondary_read_attempted)||
       (s->encoding!=1&&s->encoding!=2)||!s->node||s->node>r->node_count||
       s->body_at!=*next_body||s->body_at>r->body_bytes||
       s->body_bytes>r->body_bytes-s->body_at||
       s->edge_at!=*next_edge||s->edge_at>r->edge_count||
       s->edges>r->edge_count-s->edge_at)return -1;
    const FridayPublisherRootValueNode *v=&r->nodes[s->node-1];
    if(!v->segment||v->segment>r->segment_count)return -1;
    const FridayPublisherRootValueSegment *last=&r->segments[v->segment-1];
    if(s->previous) {
        if(s->previous>at||!s->secondary||v->segment!=at+1)return -1;
        const FridayPublisherRootValueSegment *first=&r->segments[s->previous-1];
        if(first->node!=s->node||first->previous||first->secondary||!first->closed)return -1;
    }
    if(v->segment!=at+1) {
        if(v->segment<=at+1||s->secondary||s->previous||
           last->node!=s->node||last->previous!=at+1||!last->secondary)return -1;
    }
    *next_body+=s->body_bytes;*next_edge+=s->edges;return 0;
}
static int command_node_segment(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherRootValueNode *v) {
    if(!v->segment||v->segment>r->segment_count)return -1;
    const FridayPublisherRootValueSegment *s=&r->segments[v->segment-1];
    if(!s->closed||!s->complete||s->node!=v->id||s->encoding!=v->encoding||
       s->kind!=v->kind||s->data_read!=v->data_read||
       s->body_at!=v->body_at||s->body_bytes!=v->body_bytes||
       s->edge_at!=v->edge_at||s->edges!=v->edges)return -1;
    return 0;
}
/* SOL119 complete historical tuple/member layout, retained unchanged beside
 * A273's distinct boundary observations. The final reader uses numeric
 * records only: never a Python pointer follow after retirement. */
static int command_set_read_clock(const FridayPublisherRootCommandReceipt *r,
                                 FridayPublisherRootFinalHandoff *h) {
    int ended=r->source_owner_end_confirmed!=0;
    if(command_unmapped_scan(8,ended)<0)return -1;
    if(ended) {uint64_t now;return h?final_caller_clock(h,&now):-1;}
    return command_clock((FridayPublisherRootCommandReceipt *)r)?0:-1;
}
static int command_set_scratch_begin(const FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootFinalHandoff *h,RootSetScratch **out,
    uint64_t *expected,uint64_t *visited) {
    int ended=r->source_owner_end_confirmed!=0;
    if(r!=&root_storage.command_receipt||!out||!expected||!visited||
       (ended&&!h)||command_unmapped_scan(8,ended)<0)return -1;
    RootSetScratch *s=ended?&final_caller_binding.set_scratch:&root_storage.set_scratch;
    if(!s->initialized) {
        /* ONE actual full zeroing, prospectively charged including writes.
         * After transfer ONLY receiver scratch changes, never native_body. */
        if(s->generation||command_unmapped_scan(2*((sizeof(s->marks)+63)/64)+8,ended)<0||
           command_set_read_clock(r,h)<0)return -1;
        memset(s->marks,0,sizeof(s->marks));s->initialized=1;
    }
    if(s->initialized!=1||s->generation>UINT64_MAX-2||
       command_set_read_clock(r,h)<0)return -1;
    /* No wrap/reset/refund/retry: even a refused validation consumes its tag. */
    s->generation+=2;*expected=s->generation-1;*visited=s->generation;*out=s;
    return 0;
}
/* Public GC state save/pause is an actual finite mechanism, not a proved
 * mutation exclusion flag. Selected image must independently qualify these
 * APIs, tuple internals, same-thread GIL retention, absence of reentrant
 * mutators and synchronous finalizers at the actual DECREF/native call sites.
 * The existing external Source/image admission is still mandatory. */
static int command_set_region_begin(FridayPublisherRootCommandReceipt *r,unsigned phase) {
    RootSetRegion *s=&root_storage.set_region;
    if(phase<1||phase>3||s->active_phase||s->restore_type||s->restore_value||
       s->restore_tb||!command_has_owned_runtime(&root_storage)||PyErr_Occurred())return -1;
    unsigned p=phase-1;
    for(unsigned i=0;i<3;i++)if(r->set_region_attempted[i]&&
        (!r->set_region_restored[i]||!r->set_region_consumed[i]||i>=p))return -1;
    /* Pay save/pause, both actual state probes, ONE restore + probes/full
     * original error MOVE and the finite native clocks BEFORE changing GC.
     * Restoration is a preowned obligation, not a new debit after refusal. */
    if(secondary_debit_scan(128,0)<0||!command_clock(r))return -1;
    r->set_region_attempted[p]=1;
    int prior=PyGC_IsEnabled();
    if((prior!=0&&prior!=1)||PyErr_Occurred())return -1;
    r->set_region_prior_gc[p]=(uint64_t)prior;s->active_phase=phase;
    int actual_prior=PyGC_Disable();
    if(actual_prior!=prior||PyGC_IsEnabled()!=0||PyErr_Occurred())return -1;
    r->set_region_paused[p]=1;return 0;
}
static int command_set_region_end(FridayPublisherRootCommandReceipt *r,int consumed) {
    RootSetRegion *s=&root_storage.set_region;
    if(!s->active_phase)return 0;
    unsigned p=(unsigned)s->active_phase-1;
    if(p>=3||r->set_region_restore_attempted[p]||s->restore_type||
       s->restore_value||s->restore_tb||!command_has_owned_runtime(&root_storage))return -1;
    int clock_ok=command_clock(r)!=0;
    /* Full pending originals MOVE into preowned cells BEFORE restoration.
     * A restoration-created second error cannot overwrite them. No DECREF,
     * normalization, error codec, or fresh diagnostic object is used here. */
    int pending=PyErr_Occurred()!=NULL;
    if(pending)PyErr_Fetch(&s->restore_type,&s->restore_value,&s->restore_tb);
    r->set_region_restore_attempted[p]=1;
    int before=PyGC_IsEnabled(),rc=0;
    uint64_t prior=r->set_region_prior_gc[p];
    if(prior==1)rc=PyGC_Enable();
    int after=PyGC_IsEnabled();
    int restored=prior<=1&&before==0&&rc==0&&after==(int)prior&&!PyErr_Occurred();
    if(!PyErr_Occurred()&&pending) {
        PyObject *type=s->restore_type,*value=s->restore_value,*tb=s->restore_tb;
        s->restore_type=NULL;s->restore_value=NULL;s->restore_tb=NULL;
        PyErr_Restore(type,value,tb); /* MOVE back, no original loss */
    }
    if(!restored) {
        r->pending_error_retained|=pending||PyErr_Occurred()!=NULL;
        r->residual="set_GC_restore_uncertain_originals_and_second_error_retained";
        return -1; /* active remains uncertain; restore attempt cannot repeat */
    }
    r->set_region_restored[p]=1;s->active_phase=0;
    r->set_region_consumed[p]=consumed&&clock_ok&&!pending&&!PyErr_Occurred();
    return consumed&&!r->set_region_consumed[p]?-1:0;
}
static int command_set_region_live(const FridayPublisherRootCommandReceipt *r) {
    uint64_t phase=root_storage.set_region.active_phase;
    return phase>=1&&phase<=3&&phase==r->set_current_phase&&
        r->set_region_attempted[phase-1]==1&&r->set_region_paused[phase-1]==1&&
        !r->set_region_restore_attempted[phase-1]&&
        command_has_owned_runtime(&root_storage)&&PyGC_IsEnabled()==0&&!PyErr_Occurred();
}
static int command_set_region_abort(FridayPublisherRootCommandReceipt *r) {
    uint64_t phase=root_storage.set_region.active_phase;
    if(!phase)return 0;
    /* An already uncertain restore is a terminal fact, not another call. */
    if(phase>3||r->set_region_restore_attempted[phase-1])return -1;
    return command_set_region_end(r,0);
}
static int command_set_history_shape(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherRootValueNode *v,FridayPublisherRootFinalHandoff *h) {
    int ended=r->source_owner_end_confirmed!=0;
    if(command_unmapped_scan(64,ended)<0||
       r->owner_count>FRIDAY_ROOT_COMMAND_OWNERS||r->node_count>FRIDAY_ROOT_COMMAND_NODES||
       r->edge_count>FRIDAY_ROOT_COMMAND_EDGES||r->body_bytes>FRIDAY_ROOT_COMMAND_BYTES||
       v->body_at>r->body_bytes||v->body_bytes>r->body_bytes-v->body_at||
       v->body_bytes!=4*sizeof(uint64_t)||v->edge_at>r->edge_count||
       v->edges>r->edge_count-v->edge_at||!v->set_factory_attempted||
       v->set_factory_attempted!=1||!v->set_result_owner_at||
       v->set_result_owner_at>r->owner_count)return -1;
    uint64_t head[4];memcpy(head,r->body+v->body_at,sizeof(head));
    uint64_t count=head[2],tuple_id=head[3];
    int frozen=v->kind==COMMAND_VALUE_FROZENSET;
    if((!frozen&&v->kind!=COMMAND_VALUE_SET)||head[0]!=COMMAND_SET_MAGIC||
       head[1]!=(frozen?2ULL:1ULL)||count>=FRIDAY_ROOT_COMMAND_EDGES||
       v->edges!=count+1||v->data_read!=1||
       !tuple_id||tuple_id>r->node_count||v->set_tuple_node!=tuple_id||
       r->edges[v->edge_at]!=tuple_id||r->edge_roles[v->edge_at]!=1)return -1;
    const FridayPublisherRootCommandOwner *owner=&r->owners[v->set_result_owner_at-1];
    const FridayPublisherRootValueNode *tuple=&r->nodes[tuple_id-1];
    if(owner->group!=24||owner->index!=v->id||!owner->required||
       owner->node!=tuple_id||tuple->kind!=COMMAND_VALUE_TUPLE||
       tuple->encoding!=1||!tuple->data_read||tuple->body_bytes||
       tuple->edges!=count||tuple->edge_at>r->edge_count||
       tuple->edges>r->edge_count-tuple->edge_at||
       command_node_segment(r,tuple)<0||
       (ended?(owner->owned!=NULL||tuple->original!=NULL):
               (!owner->owned||owner->owned!=tuple->original)))return -1;
    RootSetScratch *scratch;uint64_t expected,visited;
    if(command_set_scratch_begin(r,h,&scratch,&expected,&visited)<0)return -1;
    for(uint64_t j=0;j<count;j++) {
        /* One numeric NODE-ID visit including bounds, alias and mark write. */
        if(command_unmapped_scan(12,ended)<0)return -1;
        if((j&1023)==0&&command_set_read_clock(r,h)<0)return -1;
        uint64_t id=r->edges[v->edge_at+j+1];
        if(!id||id>r->node_count||r->edge_roles[v->edge_at+j+1]!=1||
           r->edges[tuple->edge_at+j]!=id||r->edge_roles[tuple->edge_at+j]!=1)return -1;
        if(scratch->marks[id-1]==visited)return -1;
        scratch->marks[id-1]=visited;
    }
    return command_set_read_clock(r,h);
}
/* All actual current results, including their full tuple DATA representation,
 * survive in the same append-only graph. This consumer checks the result of
 * EVERY attempted phase, not only the most recent successful observation. */
static int command_set_witness_shape(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherRootValueNode *v,FridayPublisherRootFinalHandoff *h) {
    int ended=r->source_owner_end_confirmed!=0;
    if(command_unmapped_scan(64,ended)<0)return -1;
    int frozen=v->kind==COMMAND_VALUE_FROZENSET;
    if(!frozen&&(!r->set_current_phase||r->set_current_phase>3||
       (ended&&r->set_current_phase!=3)))return -1;
    for(unsigned p=0;p<3;p++) {
        uint64_t attempted=v->set_current_attempted[p];
        uint64_t at=v->set_current_owner_at[p],id=v->set_current_tuple_node[p];
        if(attempted>1||v->set_current_verified[p]>1||
           r->set_phase_attempted[p]>1||r->set_phase_complete[p]>1||
           r->set_phase_complete[p]>r->set_phase_attempted[p]||
           r->set_phase_nodes[p]>r->node_count||
           (!r->set_phase_attempted[p]&&r->set_phase_nodes[p]))return -1;
        if(frozen) {
            if(attempted||at||id||v->set_current_verified[p])return -1;
            continue;
        }
        if(r->set_phase_attempted[p]&&!r->set_phase_complete[p])return -1;
        if(!attempted) {
            if(at||id||v->set_current_verified[p]||
               (r->set_phase_complete[p]&&v->id<=r->set_phase_nodes[p])||
               r->set_current_phase==p+1)return -1;
            continue;
        }
        if(!r->set_phase_complete[p]||v->id>r->set_phase_nodes[p]||
           !v->set_current_verified[p]||
           !at||at>r->owner_count||!id||id>r->node_count)return -1;
        const FridayPublisherRootCommandOwner *owner=&r->owners[at-1];
        const FridayPublisherRootValueNode *tuple=&r->nodes[id-1];
        if(owner->group!=25+p||owner->index!=v->id||owner->required!=1||
           owner->node!=id||tuple->kind!=COMMAND_VALUE_TUPLE||
           tuple->encoding!=1||tuple->data_read!=1||tuple->body_bytes||
           tuple->edges+1!=v->edges||tuple->edge_at>r->edge_count||
           tuple->edges>r->edge_count-tuple->edge_at||
           command_node_segment(r,tuple)<0||
           (ended?(owner->owned!=NULL||tuple->original!=NULL):
                   (!owner->owned||owner->owned!=tuple->original)))return -1;
        RootSetScratch *scratch;uint64_t expected,visited;
        if(command_set_scratch_begin(r,h,&scratch,&expected,&visited)<0)return -1;
        for(uint64_t k=0;k+1<v->edges;k++) {
            if(command_unmapped_scan(8,ended)<0)return -1;
            if((k&1023)==0&&command_set_read_clock(r,h)<0)return -1;
            uint64_t member=r->edges[v->edge_at+k+1];
            if(!member||member>r->node_count||r->edge_roles[v->edge_at+k+1]!=1||
               scratch->marks[member-1]==expected)return -1;
            scratch->marks[member-1]=expected;
        }
        for(uint64_t j=0;j<tuple->edges;j++) {
            if(command_unmapped_scan(8,ended)<0)return -1;
            if((j&1023)==0&&command_set_read_clock(r,h)<0)return -1;
            uint64_t member=r->edges[tuple->edge_at+j];
            if(!member||member>r->node_count||r->edge_roles[tuple->edge_at+j]!=1)return -1;
            /* Matching counts + one-to-one consumption of original marks
             * proves exact unordered bijection; visited rejects duplicates. */
            if(scratch->marks[member-1]!=expected)return -1;
            scratch->marks[member-1]=visited;
        }
    }
    return command_set_read_clock(r,h);
}
static int command_set_shape(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherRootValueNode *v,FridayPublisherRootFinalHandoff *h) {
    return command_set_history_shape(r,v,h)<0?-1:command_set_witness_shape(r,v,h);
}
/* A273 distinct semantic observations, NOT command_factory_begin retries.
 * Only an actually completed original set capture is eligible. Ordinary
 * factories remain forbidden throughout the terminal secondary traversal.
 * There is one attempt at each named boundary; any failed observation stops
 * subsequent phases. No observed result, partial segment or error is dropped.
 * A275 pauses actual public GC BEFORE the first batch capture and retains the
 * region through both live readers; phase3 also covers actual borrower/owner
 * loss. Synchronous callbacks/GIL/API/implicit costs remain qualified external
 * prerequisites: no state flag is a no-mutation theorem. */
static int command_sets_observe(FridayPublisherRootCommandReceipt *r,unsigned phase) {
    if(phase<1||phase>3||r->owner_retirement_attempted||r->source_owner_end_confirmed||
       r->getter_pending||r->pending_error_retained||PyErr_Occurred()||
       r->node_count>FRIDAY_ROOT_COMMAND_NODES||
       r->owner_count>FRIDAY_ROOT_COMMAND_OWNERS||
       (phase==1&&r->secondary_read_attempted)||
       (phase==2&&!r->secondary_read_attempted)||
       (phase==3&&!r->set_current_phase))return -1;
    unsigned p=phase-1;
    for(unsigned j=0;j<3;j++) {
        if(r->set_phase_attempted[j]>1||r->set_phase_complete[j]>1||
           r->set_phase_complete[j]>r->set_phase_attempted[j]||
           (r->set_phase_attempted[j]&&!r->set_phase_complete[j])||
           (j>=p&&r->set_phase_attempted[j]))return -1;
    }
    uint64_t limit=r->node_count;
    if(secondary_debit_scan(limit+32,0)<0||!command_clock(r))return -1;
    if(command_set_region_begin(r,phase)<0)goto failed;
    r->set_phase_attempted[p]=1;r->set_phase_nodes[p]=limit;
    r->graph_consumed=0;r->required_values_complete=0;r->error_payload_complete=0;
    if(command_set_index_begin(r,phase,limit)<0)goto failed;
    for(uint64_t i=0;i<limit;i++) {
        if((i&1023)==0&&!command_clock(r))goto failed;
        FridayPublisherRootValueNode *v=&r->nodes[i];
        if(v->kind!=COMMAND_VALUE_SET)continue;
        if(command_node_segment(r,v)<0||command_set_history_shape(r,v,NULL)<0||
           !v->original||!PySet_CheckExact(v->original)||
           v->set_current_attempted[p]||v->set_current_owner_at[p]||
           v->set_current_tuple_node[p]||v->set_current_verified[p])goto failed;
        for(unsigned j=0;j<3;j++) {
            if(v->set_current_attempted[j]&&!v->set_current_verified[j])goto failed;
            if(r->set_phase_complete[j]&&v->id<=r->set_phase_nodes[j]&&
               !v->set_current_verified[j])goto failed;
        }
        Py_ssize_t before=PySet_Size(v->original);
        if(before<0||(uint64_t)before>=FRIDAY_ROOT_COMMAND_EDGES||
           r->owner_count>=FRIDAY_ROOT_COMMAND_OWNERS||
           r->node_count>=FRIDAY_ROOT_COMMAND_NODES||
           r->segment_count>=FRIDAY_ROOT_COMMAND_SEGMENTS||
           r->edge_count>FRIDAY_ROOT_COMMAND_EDGES||
           (uint64_t)before>FRIDAY_ROOT_COMMAND_EDGES-r->edge_count||
           (uint64_t)before>UINT64_MAX/sizeof(PyObject *)||
           r->getters_attempted==UINT64_MAX||r->getter_results==UINT64_MAX||
           secondary_debit_scan((uint64_t)before+32,
               (uint64_t)before*sizeof(PyObject *)+
               sizeof(FridayPublisherRootCommandOwner)+
               sizeof(FridayPublisherRootValueNode)+
               sizeof(FridayPublisherRootValueSegment))<0||
           command_set_request_before(r,v->original,(uint64_t)before)<0||
           FridayPublisherMasterBefore(&root_storage.pool,0,0,0,131072)<0||
           plus(&r->getter_reservation_bytes,131072)<0||!command_clock(r))goto failed;
        r->getter_field="exact_set_distinct_boundary_current_tuple";
        v->set_current_attempted[p]=1;r->getters_attempted++;
        r->getter_pending=PySequence_Tuple(v->original);
        if(!r->getter_pending)goto failed;
        /* First operation on a non-NULL result: MOVE, before even inspecting
         * a pending error, type, size, members or the node index. */
        uint64_t owner_at=r->owner_count+1;
        if(command_owner(r,&r->getter_pending,25+p,v->id,1)<0)goto failed;
        v->set_current_owner_at[p]=owner_at;r->getter_results++;
        if(PyErr_Occurred())goto failed;
        PyObject *tuple=r->owners[owner_at-1].owned;
        if(!PyTuple_CheckExact(tuple))goto failed;
        Py_ssize_t count=PyTuple_GET_SIZE(tuple);
        if(count<0||(uint64_t)count+1!=v->edges)goto failed;
        /* Build one original numeric membership map, then consume each real
         * current member's existing pointer-index identity ONCE. No member-
         * by-member pair scan, Source hash/equality or new replacement node. */
        RootSetScratch *scratch;uint64_t expected,visited;
        if(command_set_scratch_begin(r,NULL,&scratch,&expected,&visited)<0)goto failed;
        for(uint64_t k=0;k+1<v->edges;k++) {
            if(secondary_debit_scan(8,0)<0)goto failed;
            if((k&1023)==0&&!command_clock(r))goto failed;
            uint64_t member=r->edges[v->edge_at+k+1];
            if(!member||member>r->node_count||r->edge_roles[v->edge_at+k+1]!=1||
               scratch->marks[member-1]==expected)goto failed;
            scratch->marks[member-1]=expected;
        }
        for(Py_ssize_t j=0;j<count;j++) {
            if(secondary_debit_scan(8,0)<0)goto failed;
            if((j&1023)==0&&!command_clock(r))goto failed;
            PyObject *member=PyTuple_GET_ITEM(tuple,j);
            uint64_t id=command_set_find(r,member);
            if(!id||scratch->marks[id-1]!=expected)goto failed;
            scratch->marks[id-1]=visited;
        }
        uint64_t id=command_set_node(r,tuple);
        if(!id)goto failed;
        r->owners[owner_at-1].node=id;v->set_current_tuple_node[p]=id;
        FridayPublisherRootValueNode *t=&r->nodes[id-1];
        if(!t->segment) {
            r->active_node=id;
            if(command_segment_begin(r,t,1)<0)goto failed;
            t->kind=COMMAND_VALUE_TUPLE;
            int complete=1;
            for(Py_ssize_t j=0;j<count;j++) {
                if((j&1023)==0&&!command_clock(r)){complete=0;break;}
                if(command_set_current_edge(r,PyTuple_GET_ITEM(tuple,j))<0){complete=0;break;}
            }
            t->data_read=complete;
            if(command_segment_end(r,t,complete)<0)goto failed;
            r->active_node=0;
            if(!complete||PyErr_Occurred())goto failed;
        } else {
            /* CPython may share the actual empty tuple. Its new returned
             * strong owner is real; reuse the completed representation,
             * without a third segment or a reconstructed tuple. */
            if(command_node_segment(r,t)<0||t->kind!=COMMAND_VALUE_TUPLE||
               t->encoding!=1||t->data_read!=1||t->body_bytes||
               t->edges!=(uint64_t)count||t->edge_at>r->edge_count||
               t->edges>r->edge_count-t->edge_at)goto failed;
            for(Py_ssize_t j=0;j<count;j++) {
                if(secondary_debit_scan(8,0)<0)goto failed;
                if((j&1023)==0&&!command_clock(r))goto failed;
                uint64_t member=r->edges[t->edge_at+(uint64_t)j];
                if(!member||member>r->node_count||r->edge_roles[t->edge_at+(uint64_t)j]!=1||
                   r->nodes[member-1].original!=PyTuple_GET_ITEM(tuple,j))goto failed;
            }
        }
        v->set_current_verified[p]=1;
    }
    if(!command_clock(r)||PyErr_Occurred())goto failed;
    r->set_phase_complete[p]=1;r->set_current_phase=phase;return 0;
failed:
    if(PyErr_Occurred())r->pending_error_retained=1;
    /* Single preowned restoration on BOTH success/refusal contours. It can
     * restore prior GC even when debit/clock already refused further work. */
    if(command_set_region_abort(r)<0)r->pending_error_retained=1;
    r->active_node=0;r->residual="actual_set_boundary_observation_incomplete_originals_retained";
    return -1;
}
static int command_set_live_check(FridayPublisherRootCommandReceipt *r,
                                 FridayPublisherRootValueNode *v) {
    if(command_set_shape(r,v,NULL)<0||secondary_debit_scan(v->edges*4+16,0)<0||
       PyErr_Occurred()||!v->original)return -1;
    int frozen=v->kind==COMMAND_VALUE_FROZENSET;
    if(frozen?!PyFrozenSet_CheckExact(v->original):!PySet_CheckExact(v->original))return -1;
    FridayPublisherRootCommandOwner *owner=&r->owners[v->set_result_owner_at-1];
    PyObject *tuple=owner->owned;
    if(!PyTuple_CheckExact(tuple))return -1;
    Py_ssize_t n=PyTuple_GET_SIZE(tuple);
    if(n<0||(uint64_t)n+1!=v->edges)return -1;
    if(frozen) {
        /* Exact frozenset membership cannot be replaced after this public
         * factory. This is NOT transferred to mutable set phase1. */
        Py_ssize_t current=PySet_Size(v->original);
        if(current<0||current!=n)return -1;
    } else {
        /* Both consumers must be inside the SAME actual paused batch region.
         * Size is only additional consistency. Qualification must exclude
         * non-GC mutations/reentrancy; a saved tuple is not a fresh getter. */
        if(!command_set_region_live(r))return -1;
        Py_ssize_t current=PySet_Size(v->original);
        if(current<0||current!=n||!r->set_current_phase||r->set_current_phase>3)return -1;
        for(unsigned p=0;p<3;p++)if(v->set_current_verified[p]) {
            uint64_t at=v->set_current_owner_at[p],id=v->set_current_tuple_node[p];
            if(!at||at>r->owner_count||!id||id>r->node_count)return -1;
            PyObject *actual=r->owners[at-1].owned;
            FridayPublisherRootValueNode *record=&r->nodes[id-1];
            if(!actual||!PyTuple_CheckExact(actual)||PyTuple_GET_SIZE(actual)!=n)return -1;
            for(Py_ssize_t j=0;j<n;j++) {
                if(secondary_debit_scan(8,0)<0)return -1;
                if((j&1023)==0&&!command_clock(r))return -1;
                uint64_t member=r->edges[record->edge_at+(uint64_t)j];
                if(!member||member>r->node_count||
                   r->nodes[member-1].original!=PyTuple_GET_ITEM(actual,j))return -1;
            }
        }
    }
    for(Py_ssize_t j=0;j<n;j++) {
        if((j&1023)==0&&!command_clock(r))return -1;
        uint64_t id=r->edges[v->edge_at+(uint64_t)j+1];
        if(!id||id>r->node_count||
           r->nodes[id-1].original!=PyTuple_GET_ITEM(tuple,j))return -1;
    }
    return command_clock(r)?0:-1;
}
static int command_values_finish(FridayPublisherRootCommandReceipt *);
static int command_set_before_loss(FridayPublisherRootCommandReceipt *r) {
    if(command_sets_observe(r,3)<0||command_values_finish(r)<0||
       !r->graph_consumed||!r->required_values_complete||
       r->full_bytes_read!=r->body_bytes||r->node_count>FRIDAY_ROOT_COMMAND_NODES||
       secondary_debit_scan(r->node_count,0)<0)return -1;
    for(uint64_t i=0;i<r->node_count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        FridayPublisherRootValueNode *v=&r->nodes[i];
        if((v->kind==COMMAND_VALUE_SET||v->kind==COMMAND_VALUE_FROZENSET)&&
           command_set_live_check(r,v)<0)return -1;
    }
    r->error_payload_complete=1;
    if(r->secondary_read_attempted)r->secondary_payload_complete=1;
    return 0;
}

/* Actual typed native consumer of the preowned data representation. It reads
 * EACH node/body/edge, validates bounds and exact repeated original aliases,
 * not a pointer ACK, XOR count, blanket zero-body or a regenerated Python value.
 * Runtime ABI/full-width admission and all original gates remain separate. */
static int command_native_node_shape(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherRootValueNode *v,FridayPublisherRootFinalHandoff *h) {
    /* One shared native producer/consumer layout,valid before AND after
     * Python retirement. Never use live original objects for these tests. */
    if(command_node_segment(r,v)<0||v->body_at>r->body_bytes||
       v->body_bytes>r->body_bytes-v->body_at)return -1;
    if(!v->id||v->id>r->node_count||v->pointer_index_slot>=FRIDAY_ROOT_COMMAND_INDEX||
       r->index[v->pointer_index_slot]!=v->id)return -1;
    if(v->encoding!=1&&v->encoding!=2)return -1;
    if(v->error_record_serial) {
        if(v->error_record_serial>root_storage.own_values.count||
           v->encoding!=1)return -1;
        const FridayPublisherOwnValue *p=&root_storage.own_values.rows[v->error_record_serial-1];
        if(p->confirmed!=1||p->serial!=v->error_record_serial)return -1;
        if(p->kind==OWN_ERROR_RECORD) {
            if(v->kind!=COMMAND_VALUE_TUPLE||v->edges!=4)return -1;
        } else if(p->kind==OWN_ERROR_CELL) {
            if(v->kind!=COMMAND_VALUE_DICT||v->edges!=20)return -1;
        } else return -1;
    }
    if(v->encoding==2) {
        if(v->kind!=COMMAND_VALUE_ERROR&&v->kind!=COMMAND_VALUE_TRACEBACK&&
           v->kind!=COMMAND_VALUE_FRAME&&v->kind!=COMMAND_VALUE_CODE&&
           v->kind!=COMMAND_VALUE_MEMORYVIEW&&v->kind!=COMMAND_VALUE_SOURCE)return -1;
        if((v->kind==COMMAND_VALUE_FRAME&&v->body_bytes!=5*sizeof(int64_t)+2)||
           (v->kind==COMMAND_VALUE_CODE&&v->body_bytes!=7*sizeof(int64_t)+13)||
           (v->kind==COMMAND_VALUE_TRACEBACK&&v->body_bytes!=2+2*sizeof(int)))return -1;
    }
    if(v->kind==COMMAND_VALUE_MEMORYVIEW) {
        uint64_t metadata=0,raw=0;
        if(command_view_body_shape(r,v,&metadata,&raw)<0)return -1;
    }
    if(v->kind==COMMAND_VALUE_SET||v->kind==COMMAND_VALUE_FROZENSET) {
        if(command_set_shape(r,v,h)<0)return -1;
    } else {
        if(v->set_factory_attempted||v->set_result_owner_at||v->set_tuple_node)return -1;
        for(unsigned p=0;p<3;p++)
            if(v->set_current_attempted[p]||v->set_current_owner_at[p]||
               v->set_current_tuple_node[p]||v->set_current_verified[p])return -1;
    }
    if((v->kind==COMMAND_VALUE_BOOL&&(v->body_bytes!=1||r->body[v->body_at]>1))||
       (v->kind==COMMAND_VALUE_TOKEN_MISSING&&(v->body_bytes||v->edges))||
       (v->kind==COMMAND_VALUE_FLOAT&&v->body_bytes!=sizeof(double))||
       (v->kind==COMMAND_VALUE_UNICODE&&v->body_bytes%sizeof(Py_UCS4))||
       (v->kind==COMMAND_VALUE_DICT&&v->edges%2)||
       (v->kind==COMMAND_VALUE_SOURCE&&v->edges!=2)||
       (v->kind==COMMAND_VALUE_TRACEBACK&&v->edges!=2)||
       (v->kind==COMMAND_VALUE_FRAME&&v->edges!=(v->encoding==2?5:9))||
       (v->kind==COMMAND_VALUE_CODE&&v->edges!=(v->encoding==2?13:18))||
       (v->kind==COMMAND_VALUE_FUNCTION&&v->edges!=14)||
       (v->kind==COMMAND_VALUE_CONTEXT_VAR&&v->edges!=17)||
       ((v->kind==COMMAND_VALUE_CONTEXT_TOKEN||v->kind==COMMAND_VALUE_HASH)&&v->edges!=15)||
       (v->kind==COMMAND_VALUE_MAPPING&&(v->edges!=15&&v->edges!=16))||
       (v->kind==COMMAND_VALUE_CONTEXT&&v->edges!=1)||
       (v->kind==COMMAND_VALUE_CELL&&v->edges!=1)||
       (v->kind==COMMAND_VALUE_METHOD&&v->edges!=2)||
       (v->kind==COMMAND_VALUE_PROPERTY&&v->edges!=4)||
       ((v->kind==COMMAND_VALUE_STATICMETHOD||v->kind==COMMAND_VALUE_CLASSMETHOD)&&v->edges!=2)||
       ((v->kind==COMMAND_VALUE_SOURCE_MODULE||v->kind==COMMAND_VALUE_FRAME_LOCALS||v->kind==COMMAND_VALUE_NATIVE_MODULE)&&v->edges!=1)||
       ((v->kind==COMMAND_VALUE_RANGE||v->kind==COMMAND_VALUE_SLICE)&&v->edges!=3))return -1;
    return 0;
}
static int command_own_record_consumer(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v) {
    FridayPublisherOwnValue *p=NULL;
    if(v->kind==COMMAND_VALUE_CONTEXT_VAR)p=own_find(v->original,OWN_VAR);
    else if(v->kind==COMMAND_VALUE_CONTEXT_TOKEN)p=own_find(v->original,OWN_TOKEN);
    else if(v->kind==COMMAND_VALUE_HASH)p=own_find(v->original,OWN_HASH);
    else if(v->kind==COMMAND_VALUE_MAPPING)
        for(uint64_t i=0;i<root_storage.own_values.count;i++) {
            if((i&1023)==0&&!command_clock(r))return -1;
            FridayPublisherOwnValue *q=&root_storage.own_values.rows[i];
            if(q->kind==OWN_MAPPING&&q->refs[6]==v->original){p=q;break;}
        }
    else return 0;
    if(!p||p->pid!=getpid()||v->body_bytes<7*sizeof(uint64_t)+15)return -1;
    uint64_t actual[7],expected[]={p->kind,p->serial,p->flags,p->attempts,p->confirmed,p->width,(uint64_t)p->pid};
    memcpy(actual,r->body+v->body_at,sizeof(actual));
    if(memcmp(actual,expected,sizeof(actual)))return -1;
    const unsigned char *present=r->body+v->body_at+sizeof(actual);
    for(unsigned j=0;j<15;j++) {
        PyObject *value=j<12?p->refs[j]:j==12?p->error_type:j==13?p->error_value:p->error_tb;
        int support=j<12?((j==0)||(p->kind==OWN_HASH&&(j==2||j==6))||
            (p->kind==OWN_MAPPING&&(j==10||j==11))):j==12;
        uint64_t id=r->edges[v->edge_at+j];
        if(present[j]!=(value!=NULL)||!id||id>r->node_count||
           r->nodes[id-1].original!=(value?value:Py_None)||
           r->edge_roles[v->edge_at+j]!=(value&&!support))return -1;
    }
    if(p->kind==OWN_HASH) {
        PyObject *ledger=p->refs[1];
        uint64_t mode=(p->flags>>8)&3;
        if(FridayPublisherMasterBefore(&root_storage.pool,24576,0,0,0)<0)return -1;
        /* Shape BEFORE the first dictionary walk. This fixed debit pays the
         * constructor/status fields only, never an arbitrary update history. */
        if(!ledger||!PyDict_CheckExact(ledger)||PyDict_Size(ledger)!=14)return -1;
        PyObject *ops=own_plain_field(ledger,"updates");
        PyObject *metadata=own_plain_field(ledger,"metadata_error");
        if(!ascii(own_plain_field(ledger,"schema"),"friday.astra256.own-hash-input-ledger.v2")||
           !ops||!PyList_CheckExact(ops)||!metadata||
           (metadata!=Py_None&&!PyExceptionInstance_Check(metadata))||
           own_plain_field(ledger,"original_error")!=Py_None||
           own_hash_status_key(ledger,"runtime_h")!=p->refs[9]||
           own_hash_status_key(ledger,"constructed")!=p->refs[10]||
           own_hash_status_key(ledger,"actual_attempted")!=p->refs[11]||
           own_plain_field(ledger,"constructor_bytes")!=p->refs[5]||
           own_plain_field(ledger,"runtime_h")!=p->refs[0]||
           own_plain_field(ledger,"name")!=p->refs[4]||
           own_plain_field(ledger,"constructed")!=Py_True||
           own_plain_field(ledger,"actual_attempted")!=Py_True||
           (own_plain_field(ledger,"state_complete")!=Py_True&&
            own_plain_field(ledger,"state_complete")!=Py_False)||
           !ascii(own_plain_field(ledger,"mode"),mode==1?"sha256":mode==2?"new":mode==3?"copy":""))return -1;
        /* A257: pay every actual row BEFORE entering the loop. Exact seven
         * fields bound each row's scans, not the number of update rows. Keep
         * the original pool/caps; refusal retains the same owners. This is an
         * explicit conservative debit, NOT measured whole implicit ABI fit. */
        uint64_t updates=(uint64_t)PyList_GET_SIZE(ops);
        const uint64_t row_reads=24576;
        if(updates>UINT64_MAX/row_reads)
            return FridayPublisherMasterFault(&root_storage.pool,"own_hash_full_update_scan_overflow");
        if(FridayPublisherMasterBefore(&root_storage.pool,updates*row_reads,0,0,0)<0)return -1;
        uint64_t attempted=1,confirmed=1;int complete=own_plain_field(ledger,"state_complete")==Py_True;
        for(Py_ssize_t j=0;j<(Py_ssize_t)updates;j++) {
            PyObject *op=PyList_GET_ITEM(ops,j);
            if((j&1023)==0&&!command_clock(r))return -1;
            if(!op||!PyDict_CheckExact(op)||PyDict_Size(op)!=7)return -1;
            PyObject *full=own_plain_field(op,"full_bytes");
            PyObject *actual=own_plain_field(op,"actual_attempted"),*returned=own_plain_field(op,"confirmed");
            PyObject *error=own_plain_field(op,"original_error");
            PyObject *metadata_error=own_plain_field(op,"metadata_error");
            if(own_plain_field(op,"attempted")!=Py_True||!full||!PyBytes_CheckExact(full)||
               (actual!=Py_True&&actual!=Py_False)||(returned!=Py_True&&returned!=Py_False)||
               !error||(error!=Py_None&&!PyExceptionInstance_Check(error))||
               !metadata_error||(metadata_error!=Py_None&&!PyExceptionInstance_Check(metadata_error))||
               (metadata_error!=Py_None&&returned!=Py_True))return -1;
            if(actual==Py_True)attempted++;
            if(returned==Py_True) {
                if(actual!=Py_True||error!=Py_None)return -1;
                confirmed++;
            }
            if(complete&&(actual!=Py_True||returned!=Py_True||metadata_error!=Py_None))return -1;
        }
        if(p->attempts!=attempted||p->confirmed!=confirmed||
           (complete&&((p->flags&4)||p->attempts!=p->confirmed||
                       own_plain_field(ledger,"original_error")!=Py_None||metadata!=Py_None)))return -1;
        /* False computational completion is a full preserved ERROR prefix,
         * NOT a foreign-private-heap theorem or terminal support retirement. */
    }
    if(p->kind==OWN_MAPPING) {
        if(FridayPublisherMasterBefore(&root_storage.pool,4096,0,0,0)<0)return -1;
        PyObject *creation=p->refs[8];
        PyObject *returned=own_plain_field(creation,"returned");
        PyObject *alias=own_plain_field(creation,"returned_mapping");
        if((p->flags&16)||(p->flags&8)||!p->refs[6]||returned!=Py_True||alias!=p->refs[6])return -1;
        uint64_t prefix=sizeof(actual)+15;
        if(command_mapping_tail(r,p,r->body+v->body_at+prefix,v->body_bytes-prefix)<0)return -1;
    } else if(p->kind!=OWN_VAR) {
        if(v->body_bytes!=sizeof(actual)+15)return -1;
    } else {
        int has_current;uint64_t base=sizeof(actual)+15;
        if(v->body_bytes<base+sizeof(has_current)+1)return -1;
        memcpy(&has_current,r->body+v->body_at+base,sizeof(has_current));
        PyObject *ctx=PyThreadState_Get()->context;
        int current=ctx?PySequence_Contains(ctx,p->refs[0]):0;
        if(current<0||has_current!=current)return -1;
        uint64_t context_id=r->edges[v->edge_at+15],value_id=r->edges[v->edge_at+16];
        unsigned char ctx_present=r->body[v->body_at+base+sizeof(has_current)];
        if(ctx_present!=(ctx!=NULL)||!context_id||context_id>r->node_count||
           r->nodes[context_id-1].original!=(ctx?ctx:Py_None))return -1;
        if(has_current) {
            PyObject *value=NULL;
            /* Actual stored current value: strong exported C result, no user
             * callback, default inference or empty Context.run invocation. */
            if(PyContextVar_Get(p->refs[0],NULL,&value)<0)return -1;
            int equal=value&&value_id&&value_id<=r->node_count&&r->nodes[value_id-1].original==value;
            Py_XDECREF(value);
            if(!equal||v->body_bytes!=base+sizeof(has_current)+1)return -1;
        } else if(v->body_bytes!=base+sizeof(has_current)+2||
                  r->body[v->body_at+base+sizeof(has_current)+1]!=0||
                  !value_id||value_id>r->node_count||r->nodes[value_id-1].original!=Py_None)return -1;
    }
    return 0;
}

static int command_secondary_consumer(FridayPublisherRootCommandReceipt *);
static int command_secondary_error_check(FridayPublisherRootCommandReceipt *,
    FridayPublisherRootValueNode *);
static int command_class_rows_consume(const FridayPublisherRootStorage *,
    const FridayPublisherRootCommandReceipt *,FridayPublisherRootFinalHandoff *);
static int command_class_retirement_blocked(FridayPublisherRootFinalHandoff *);
static int command_error_record_rows_consume(const FridayPublisherRootCommandReceipt *,
    FridayPublisherRootFinalHandoff *);
static int command_graph_consumer(FridayPublisherRootCommandReceipt *r) {
    if(!r->graph_started||r->graph_body_at>r->body_bytes||r->graph_edge_at||
       r->native_owner_body_at!=r->producer_close_body_bytes||
       r->native_owner_body_at>r->graph_body_at||
       r->native_owner_body_bytes!=r->graph_body_at-r->native_owner_body_at||
       r->node_count>FRIDAY_ROOT_COMMAND_NODES||
       r->segment_count>FRIDAY_ROOT_COMMAND_SEGMENTS||
       r->body_bytes>FRIDAY_ROOT_COMMAND_BYTES||r->edge_count>FRIDAY_ROOT_COMMAND_EDGES)return -1;
    uint64_t next_body=r->graph_body_at,next_edge=r->graph_edge_at;
    for(uint64_t i=0;i<r->segment_count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        if(command_segment_relation(r,i,&next_body,&next_edge)<0)return -1;
    }
    if(next_body!=r->body_bytes||next_edge!=r->edge_count)return -1;
    for(uint64_t i=0;i<r->owner_count;i++) {
        FridayPublisherRootCommandOwner *o=&r->owners[i];
        if(o->group>=24&&o->group<=27&&secondary_debit_scan(8,0)<0)return -1;
        if(!o->node||o->node>r->node_count||r->nodes[o->node-1].original!=o->owned)return -1;
    }
    for(uint64_t i=0;i<r->node_count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        FridayPublisherRootValueNode *v=&r->nodes[i];
        if(v->id!=i+1||!v->original||v->body_at>r->body_bytes||
           v->body_bytes>r->body_bytes-v->body_at||v->edge_at>r->edge_count||
           v->edges>r->edge_count-v->edge_at)return -1;
        if(v->first_parent>r->node_count||v->first_edge>r->edge_count)return -1;
        /* Validate the actual common wire shape BEFORE the own reader uses
         * its fixed edge positions. Same check runs after runtime retirement. */
        if(command_native_node_shape(r,v,NULL)<0)return -1;
        r->active_node=v->id;
        if(v->encoding==1&&command_own_record_consumer(r,v)<0)return -1;
        if(v->encoding==1&&v->kind==COMMAND_VALUE_ERROR&&
           command_secondary_error_check(r,v)<0)return -1;
        if(v->encoding==1&&v->kind==COMMAND_VALUE_MEMORYVIEW&&
           command_secondary_buffer_check(r,v)<0)return -1;
        if((v->kind==COMMAND_VALUE_SET||v->kind==COMMAND_VALUE_FROZENSET)&&
           command_set_live_check(r,v)<0)return -1;
        if(v->encoding==1&&v->kind==COMMAND_VALUE_TRACEBACK) {
            PyTracebackObject *q=(PyTracebackObject *)v->original;
            if(Py_TYPE(v->original)!=&PyTraceBack_Type||
               v->body_bytes!=sizeof(q->tb_lasti)+sizeof(q->tb_lineno)||
               memcmp(r->body+v->body_at,&q->tb_lasti,sizeof(q->tb_lasti))||
               memcmp(r->body+v->body_at+sizeof(q->tb_lasti),&q->tb_lineno,sizeof(q->tb_lineno)))return -1;
            PyObject *actual[]={(PyObject *)q->tb_next,(PyObject *)q->tb_frame};
            for(unsigned j=0;j<2;j++) {
                uint64_t id=r->edges[v->edge_at+j];
                if(!id||id>r->node_count||r->edge_roles[v->edge_at+j]!=1||
                   r->nodes[id-1].original!=(actual[j]?actual[j]:Py_None))return -1;
            }
        }
        uint64_t expected=0;
        if(v->kind==COMMAND_VALUE_TUPLE||v->kind==COMMAND_VALUE_LIST) {
            Py_ssize_t n=v->kind==COMMAND_VALUE_TUPLE?PyTuple_GET_SIZE(v->original):PyList_GET_SIZE(v->original);
            if(n<0||(uint64_t)n!=v->edges)return -1;
            int operands=v->kind==COMMAND_VALUE_TUPLE?own_class_container(v->original):0;
            if(operands<0)return -1;
            FridayPublisherOwnValue *record=NULL;
            int actual_record=v->kind==COMMAND_VALUE_TUPLE?own_error_record_lookup(v->original,&record):0;
            if(actual_record<0||v->error_record_serial!=(actual_record?record->serial:0))return -1;
            for(Py_ssize_t j=0;j<n;j++) {
                uint64_t id=r->edges[v->edge_at+j];if(!id||id>r->node_count)return -1;
                PyObject *actual=v->kind==COMMAND_VALUE_TUPLE?
                    PyTuple_GET_ITEM(v->original,j):PyList_GET_ITEM(v->original,j);
                if(r->nodes[id-1].original!=actual)return -1;
                if(operands==1&&r->edge_roles[v->edge_at+j]!=(unsigned char)(j<2||!PyType_Check(actual)))return -1;
                if(actual_record&&r->edge_roles[v->edge_at+j]!=(unsigned char)(j!=2||record->flags!=1))return -1;
                expected++;
            }
        } else if(v->kind==COMMAND_VALUE_DICT) {
            Py_ssize_t pos=0;PyObject *key,*value;
            int operands=own_class_container(v->original);if(operands<0)return -1;
            FridayPublisherOwnValue *cell=NULL;int actual_cell=own_error_record_lookup(v->original,&cell);
            if(actual_cell<0||v->error_record_serial!=(actual_cell?cell->serial:0))return -1;
            while(PyDict_Next(v->original,&pos,&key,&value)) {
                if((expected&1023)==0&&!command_clock(r))return -1;
                if(expected>v->edges||v->edges-expected<2)return -1;
                uint64_t k=r->edges[v->edge_at+expected++],n=r->edges[v->edge_at+expected++];
                if(!k||!n||k>r->node_count||n>r->node_count||
                   r->nodes[k-1].original!=key||r->nodes[n-1].original!=value)return -1;
                if(operands==2&&(!r->edge_roles[v->edge_at+expected-2]||
                   r->edge_roles[v->edge_at+expected-1]!=(unsigned char)!PyType_Check(value)))return -1;
                if(actual_cell&&(!r->edge_roles[v->edge_at+expected-2]||
                   r->edge_roles[v->edge_at+expected-1]!=(unsigned char)
                    !(cell->flags==1&&own_plain_name(key,"original_type"))))return -1;
            }
            if(expected!=v->edges)return -1;
        }
        /* Full structural bank, not only external positive tuple13. Cycles
         * are numeric joins to already owned nodes, no recursive decoder. */
        for(uint64_t j=0;j<v->edges;j++) {
            uint64_t id=r->edges[v->edge_at+j];
            if(!id||id>r->node_count||r->edge_roles[v->edge_at+j]>1)return -1;
        }
        r->structural_nodes_read++;
    }
    /* Failed historical segments still own real aliases. Read the ENTIRE
     * edge bank, including the replaced prefix, not just latest node ranges. */
    for(uint64_t i=0;i<r->edge_count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        if(!r->edges[i]||r->edges[i]>r->node_count||r->edge_roles[i]>1)return -1;
        r->alias_edges_read++;
    }
    if(command_secondary_consumer(r)<0||command_error_record_rows_consume(r,NULL)<0)return -1;
    r->active_node=0;return 0;
}

static int command_values_finish(FridayPublisherRootCommandReceipt *r) {
    r->required_values_complete=0;r->graph_consumed=0;r->full_bytes_read=0;
    r->unresolved_required_nodes=0;r->first_unresolved_node=0;r->first_unresolved_type=NULL;
    r->alias_edges_read=0;r->structural_nodes_read=0;
    if(r->node_count>FRIDAY_ROOT_COMMAND_NODES||r->owner_count>FRIDAY_ROOT_COMMAND_OWNERS)return -1;
    for(uint64_t i=0;i<r->node_count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        if(command_node_segment(r,&r->nodes[i])<0)return -1;
        r->nodes[i].required=0;
    }
    for(uint64_t i=0;i<r->owner_count;i++) {
        if((i&1023)==0) {
            uint64_t now=command_clock(r);if(!now||now>root_storage.pool.deadline_ns)return -1;
        }
        if(r->owners[i].group>=24&&r->owners[i].group<=27&&secondary_debit_scan(8,0)<0)return -1;
        uint64_t id=r->owners[i].node;
        if(!id||id>r->node_count||r->nodes[id-1].original!=r->owners[i].owned)return -1;
        if(r->owners[i].required)r->nodes[id-1].required=1;
    }
    if(r->cleanup_error.saved) {
        if(!r->secondary_read_attempted)return -1;
        PyObject *error[]={r->cleanup_error.type,r->cleanup_error.value,r->cleanup_error.tb};
        for(unsigned j=0;j<3;j++) {
            uint64_t id=r->secondary_roots[j];
            if(((r->secondary_error_present>>j)&1)!=(error[j]!=NULL))return -1;
            if(!error[j]) {if(id)return -1;continue;}
            if(!id||id>r->node_count||r->nodes[id-1].original!=error[j])return -1;
            if(j)r->nodes[id-1].required=1; /* type is an actual SUPPORT alias */
        }
    }
    for(unsigned j=0;j<2;j++)if(r->secondary_aux_roots[j]) {
        uint64_t id=r->secondary_aux_roots[j];if(id>r->node_count)return -1;
        r->nodes[id-1].required=1;
    }
    /* Bounded actual graph reachability, aliases/cycles preserved. Each node
     * enters the preowned queue once and each required edge is visited once.
     * No repeated whole graph polling, recursion or zero-byte nonbyte oracle. */
    uint64_t pending=0;
    for(uint64_t i=0;i<r->node_count;i++)
        if(r->nodes[i].required)r->required_queue[pending++]=i+1;
    for(uint64_t at=0;at<pending;at++) {
            if((at&1023)==0) {
                uint64_t now=command_clock(r);if(!now||now>root_storage.pool.deadline_ns)return -1;
            }
            FridayPublisherRootValueNode *v=&r->nodes[r->required_queue[at]-1];
            v->required=2;
            uint64_t segment=v->segment;
            for(unsigned version=0;segment&&version<2;version++) {
                if(segment>r->segment_count)return -1;
                const FridayPublisherRootValueSegment *s=&r->segments[segment-1];
                if(s->node!=v->id||!s->closed||s->edge_at>r->edge_count||
                   s->edges>r->edge_count-s->edge_at)return -1;
                for(uint64_t j=0;j<s->edges;j++) {
                    if((j&1023)==0&&!command_clock(r))return -1;
                    uint64_t id=r->edges[s->edge_at+j];
                    if(!id||id>r->node_count||r->edge_roles[s->edge_at+j]>1)return -1;
                    if(r->edge_roles[s->edge_at+j]&&!r->nodes[id-1].required) {
                        if(pending>=FRIDAY_ROOT_COMMAND_NODES)return -1;
                        r->nodes[id-1].required=1;r->required_queue[pending++]=id;
                    }
                }
                segment=s->previous;
            }
            if(segment)return -1; /* no third version or cyclic lineage */
    }
    for(uint64_t i=0;i<r->node_count;i++)
        if(r->nodes[i].required&&(!r->nodes[i].data_read||
           r->nodes[i].kind==COMMAND_VALUE_UNSUPPORTED)) {
            r->unresolved_required_nodes++;
            if(!r->first_unresolved_node) {
                r->first_unresolved_node=i+1;
                r->first_unresolved_type=Py_TYPE(r->nodes[i].original)->tp_name;
            }
        }
    /* Completeness is set ONLY after the actual structural/body consumer. */
    /* Every full byte copied is read by the actual native command reader,
     * not a count/pointer receipt. It does not confer independent acceptance. */
    for(uint64_t i=0;i<r->body_bytes;i++) {
        if((i&65535)==0) {
            uint64_t now=command_clock(r);if(!now||now>root_storage.pool.deadline_ns)return -1;
        }
        r->full_reader_sink^=r->body[i];
    }
    r->full_bytes_read=r->body_bytes;
    if(command_graph_consumer(r)<0)return -1;
    if(command_mapping_return_ledger(r)<0||command_unmapped_rows_check(r,0,NULL)<0)return -1;
    if(command_class_rows_consume(&root_storage,r,NULL)<0)return -1;
    r->graph_consumed=1;
    r->required_values_complete=r->owner_inventory_complete&&r->unresolved_required_nodes==0;
    return 0;
}
static int command_values(FridayPublisherRootCommandReceipt *r) {
    command_graph_begin(r);
    for(uint64_t i=0;i<r->owner_count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        uint64_t id=r->owners[i].group>=24&&r->owners[i].group<=27?
            command_set_node(r,r->owners[i].owned):command_node(r,r->owners[i].owned);
        if(!id)return -1;r->owners[i].node=id;
    }
    for(uint64_t i=0;i<r->node_count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        FridayPublisherRootValueNode *v=&r->nodes[i];r->active_node=i+1;
        if(command_segment_begin(r,v,1)<0)return -1;
        int rc=command_builtin(r,v),pending=PyErr_Occurred()!=NULL;
        if(command_segment_end(r,v,rc==0&&!pending)<0)return -1;
        r->active_node=0;
        if(rc<0||pending)return -1;
    }
    if(command_sets_observe(r,1)<0)return -1;
    int rc=command_values_finish(r);
    /* A separately unresolved DATA node does not invent a failed set factory:
     * a completed consumer may still return an incomplete whole graph, whose
     * existing secondary contour remains available with a distinct phase. */
    if(command_set_region_end(r,rc==0)<0)return -1;
    return rc;
}
/* Selected secondary rows live in root_storage.secondary. Source registers
 * strong aliases before Receive. MOVE uses group 22. Borrowed views are
 * cleared before the first owner release on the success end and on the
 * sealed error end. No second pool and no new grant.
 */
static int secondary_kind_name(PyObject *name) {
    if(!PyUnicode_CheckExact(name))return -1;
    const char *kinds[]={"frame","code","source","buffer","traceback","error","error_handler"};
    int values[]={SECONDARY_CUT_FRAME,SECONDARY_CUT_CODE,SECONDARY_CUT_SOURCE,
        SECONDARY_CUT_BUFFER,SECONDARY_CUT_TRACEBACK,SECONDARY_CUT_ERROR,
        SECONDARY_CUT_ERROR_HANDLER};
    for(int i=0;i<7;i++) {
        int cmp=PyUnicode_CompareWithASCIIString(name,kinds[i]);
        if(cmp<0){if(PyErr_Occurred())return -1;continue;}
        if(cmp==0)return values[i];
    }
    return 0;
}
static int secondary_note_scan(uint64_t *steps) {
    if(*steps==UINT64_MAX)
        return FridayPublisherMasterFault(&root_storage.pool,"secondary_scan_step_overflow");
    (*steps)++;
    return 0;
}
static int secondary_debit_scan(uint64_t steps,uint64_t copy_bytes) {
    /* Reads cover the scan about to run. copy_bytes covers explicit strong
     * reference records and already-materialized edge bytes. Inline sizeof
     * does not pay this iteration. Unknown ABI cost stays unclaimed.
     * A product that does not fit is an overflow, not a lower cap. */
    if(steps>UINT64_MAX/64)
        return FridayPublisherMasterFault(&root_storage.pool,"secondary_scan_width_overflow");
    uint64_t reads=steps*64;
    if(copy_bytes>UINT64_MAX-reads)
        return FridayPublisherMasterFault(&root_storage.pool,"secondary_copy_width_overflow");
    return FridayPublisherMasterBefore(&root_storage.pool,reads,0,0,copy_bytes);
}
static int secondary_add(uint64_t *n,uint64_t amount) {
    if(*n>UINT64_MAX-amount)return -1;
    *n+=amount;return 0;
}
static int secondary_explicit_copy(PyObject *edges,Py_ssize_t nedges,uint64_t *out) {
    uint64_t n=0;
    for(Py_ssize_t i=0;i<nedges;i++) {
        PyObject *item=PyTuple_GET_ITEM(edges,i);
        if(!item||item==Py_None)continue;
        if(PyBytes_CheckExact(item)) {
            if(secondary_add(&n,(uint64_t)PyBytes_GET_SIZE(item))<0)return -1;
        } else if(PyDict_CheckExact(item)) {
            Py_ssize_t k=PyDict_Size(item);
            if(k>0) {
                if((uint64_t)k>UINT64_MAX/64||secondary_add(&n,(uint64_t)k*64)<0)return -1;
            }
        } else if(PyTuple_CheckExact(item)||PyList_CheckExact(item)) {
            Py_ssize_t k=PyTuple_CheckExact(item)?PyTuple_GET_SIZE(item):PyList_GET_SIZE(item);
            if(k>0) {
                if((uint64_t)k>UINT64_MAX/32||secondary_add(&n,(uint64_t)k*32)<0)return -1;
            }
        }
    }
    *out=n;return 0;
}
static int secondary_globals_role(FridayPublisherRootCommandReceipt *r,PyObject *globals) {
    /* Same owned-namespace relation as the primary f_globals reader. */
    if(!globals)return 0;
    if(secondary_debit_scan(root_storage.own_values.count,0)<0)return -1;
    int own=command_source_namespace(r,globals);
    if(own<0)return -1;
    return own?1:0;
}
static int secondary_edge_required(FridayPublisherRootCommandReceipt *r,
    const FridayPublisherSecondaryCut *cut,unsigned slot,PyObject *view) {
    if(!view||view==Py_None)return 0;
    if(cut->kind==SECONDARY_CUT_FRAME) {
        if(slot==3)return secondary_globals_role(r,view);
        if(slot==5)return 0; /* builtins stay support, as f_builtins does */
        return 1;
    }
    if(cut->kind==SECONDARY_CUT_ERROR&&slot==1)return 0;
    if(cut->kind==SECONDARY_CUT_SOURCE&&slot==1)return 0;
    return 1;
}
static int secondary_error_handler_capture(PyObject *original,PyObject *edges,
    Py_ssize_t nedges,Py_ssize_t nscalars) {
    if(secondary_debit_scan(8,0)<0)return -1;
    if(original!=edges||nedges!=5||nscalars)return -1;
    PyObject *raised=PyTuple_GET_ITEM(edges,0),*handler=PyTuple_GET_ITEM(edges,1);
    PyObject *context=PyTuple_GET_ITEM(edges,2),*tb=PyTuple_GET_ITEM(edges,3);
    PyObject *chain=PyTuple_GET_ITEM(edges,4);
    if(!PyExceptionInstance_Check(raised)||!PyExceptionInstance_Check(handler)||
       raised==handler||!PyTuple_CheckExact(chain))return -1;
    PyBaseExceptionObject *e=(PyBaseExceptionObject *)raised;
    if(context!=(e->context?e->context:Py_None)||tb!=(e->traceback?e->traceback:Py_None))return -1;
    /* The public handled-exception getter supplies the ACTUAL thread handler,
     * not a Python caller flag or an inferred default. No raise is performed. */
    PyObject *active=PyErr_GetHandledException();
    int same=active==handler;Py_XDECREF(active);if(!same)return -1;
    Py_ssize_t count=PyTuple_GET_SIZE(chain);
    if(count<1||(uint64_t)count>root_storage.secondary.count)return -1;
    if((uint64_t)count>UINT64_MAX/2||secondary_debit_scan((uint64_t)count*2,0)<0)return -1;
    PyObject *cursor=handler;
    for(Py_ssize_t i=0;i<count;i++) {
        PyObject *pair=PyTuple_GET_ITEM(chain,i);
        if(!PyTuple_CheckExact(pair)||PyTuple_GET_SIZE(pair)!=2||
           !cursor||!PyExceptionInstance_Check(cursor)||PyTuple_GET_ITEM(pair,0)!=cursor)return -1;
        PyObject *next=((PyBaseExceptionObject *)cursor)->context;
        if(PyTuple_GET_ITEM(pair,1)!=(next?next:Py_None))return -1;
        cursor=next;
    }
    if(cursor) {
        /* A full unique prefix can terminate at a retained causal cycle.
         * Pay the terminal membership probe separately, before its walk. */
        if(secondary_debit_scan((uint64_t)count,0)<0)return -1;
        int found=0;
        for(Py_ssize_t i=0;i<count;i++)
            if(PyTuple_GET_ITEM(PyTuple_GET_ITEM(chain,i),0)==cursor){found=1;break;}
        if(!found)return -1;
    }
    return 0;
}
PyObject *FridayPublisherRootOwnSecondaryCut(PyObject *o) {
    if(!PyTuple_CheckExact(o)||PyTuple_GET_SIZE(o)!=5) {
        PyErr_SetString(PyExc_TypeError,"secondary own cut requires five fields");
        return NULL;
    }
    PyObject *schema=PyTuple_GET_ITEM(o,0),*kind_name=PyTuple_GET_ITEM(o,1);
    PyObject *original=PyTuple_GET_ITEM(o,2),*edges=PyTuple_GET_ITEM(o,3);
    PyObject *scalars=PyTuple_GET_ITEM(o,4);
    if(!PyUnicode_CheckExact(schema)||
       PyUnicode_CompareWithASCIIString(schema,"friday.lab942.secondary-own-cut.v1")!=0||
       !PyTuple_CheckExact(edges)||!PyTuple_CheckExact(scalars)||original==Py_None) {
        PyErr_SetString(PyExc_TypeError,"secondary own cut shape");
        return NULL;
    }
    int kind=secondary_kind_name(kind_name);
    if(kind<0)return NULL;
    if(!kind){PyErr_SetString(PyExc_TypeError,"secondary own cut kind");return NULL;}
    Py_ssize_t nedges=PyTuple_GET_SIZE(edges),nscalars=PyTuple_GET_SIZE(scalars);
    if(nedges<1||nedges>SECONDARY_CUT_EDGES||nscalars<0||nscalars>SECONDARY_CUT_SCALARS) {
        PyErr_SetString(PyExc_TypeError,"secondary own cut width");
        return NULL;
    }
    if(root_storage.secondary.retired||root_storage.secondary.end_attempted||
       root_storage.own_values.retired) {
        PyErr_SetString(PyExc_RuntimeError,"secondary own cut after owner end");
        return NULL;
    }
    if(kind==SECONDARY_CUT_ERROR_HANDLER&&
       secondary_error_handler_capture(original,edges,nedges,nscalars)<0) {
        if(!PyErr_Occurred())PyErr_SetString(PyExc_TypeError,"actual retained error handler relation");
        return NULL;
    }
    /* Same Root, current admission, and the pre-command phase. The Python
     * wrapper does not admit this row. Debit the probe, then the copy and
     * the identity scan, before any strong reference is taken. */
    if(secondary_debit_scan((uint64_t)nedges,0)<0)return NULL;
    uint64_t copy_bytes=0;
    if(secondary_explicit_copy(edges,nedges,&copy_bytes)<0)
        return FridayPublisherMasterFault(&root_storage.pool,"secondary_copy_width_overflow"),NULL;
    if(own_before(((uint64_t)nedges+1)*sizeof(PyObject *)+copy_bytes+64)<0)return NULL;
    if(secondary_debit_scan(root_storage.secondary.count,copy_bytes)<0)return NULL;
    int64_t numbers[SECONDARY_CUT_SCALARS];
    for(Py_ssize_t i=0;i<nscalars;i++) {
        PyObject *item=PyTuple_GET_ITEM(scalars,i);
        if(!PyLong_CheckExact(item)) {
            PyErr_SetString(PyExc_TypeError,"secondary own cut scalar");
            return NULL;
        }
        int overflow=0;
        long long value=PyLong_AsLongLongAndOverflow(item,&overflow);
        if(overflow||PyErr_Occurred()) {
            if(!PyErr_Occurred())PyErr_SetString(PyExc_TypeError,"secondary own cut scalar");
            return NULL;
        }
        numbers[i]=(int64_t)value;
    }
    int raised_retained=kind!=SECONDARY_CUT_ERROR_HANDLER;
    int handler_retained=kind!=SECONDARY_CUT_ERROR_HANDLER;
    for(uint64_t i=0;i<root_storage.secondary.count;i++) {
        if(secondary_note_scan(&root_storage.secondary.scan_steps)<0)return NULL;
        if(kind==SECONDARY_CUT_ERROR_HANDLER&&
           root_storage.secondary.rows[i].kind==SECONDARY_CUT_ERROR&&
           root_storage.secondary.rows[i].original==PyTuple_GET_ITEM(edges,0))raised_retained=1;
        if(kind==SECONDARY_CUT_ERROR_HANDLER&&
           root_storage.secondary.rows[i].kind==SECONDARY_CUT_ERROR&&
           root_storage.secondary.rows[i].original==PyTuple_GET_ITEM(edges,1))handler_retained=1;
        if(root_storage.secondary.rows[i].original==original)return Py_NewRef(original);
    }
    if(!raised_retained||!handler_retained) {
        PyErr_SetString(PyExc_TypeError,"error handler requires both original first cuts");return NULL;
    }
    if(root_storage.secondary.count>=SECONDARY_CUT_CAP)Py_RETURN_NONE;
    PyObject *held[SECONDARY_CUT_EDGES];
    for(Py_ssize_t i=0;i<nedges;i++)held[i]=PyTuple_GET_ITEM(edges,i);
    if(kind==SECONDARY_CUT_ERROR&&nedges>3&&held[3]==Py_None&&PyExceptionInstance_Check(original)) {
        PyBaseExceptionObject *saved=(PyBaseExceptionObject *)original;
        if(saved->notes)held[3]=saved->notes;
    }
    FridayPublisherSecondaryCut *cut=&root_storage.secondary.rows[root_storage.secondary.count];
    memset(cut,0,sizeof(*cut));
    cut->original=Py_NewRef(original);
    cut->kind=(unsigned char)kind;
    cut->nedges=(unsigned char)nedges;
    cut->nscalars=(unsigned char)nscalars;
    cut->pid=getpid();
    cut->serial=root_storage.secondary.count+1;
    for(Py_ssize_t i=0;i<nedges;i++)cut->edges[i]=Py_XNewRef(held[i]);
    for(Py_ssize_t i=0;i<nscalars;i++)cut->scalars[i]=numbers[i];
    root_storage.secondary.count++;
    return Py_NewRef(original);
}
static int secondary_cut_lookup(FridayPublisherRootCommandReceipt *r,PyObject *o,
    FridayPublisherSecondaryCut **out) {
    *out=NULL;
    if(!o||o==Py_None)return 0;
    if(secondary_debit_scan(root_storage.secondary.count,0)<0)return -1;
    for(uint64_t i=0;i<root_storage.secondary.count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        if(secondary_note_scan(&root_storage.secondary.scan_steps)<0)return -1;
        if(root_storage.secondary.rows[i].original==o){*out=&root_storage.secondary.rows[i];return 1;}
    }
    return 0;
}
static PyObject *secondary_cut_edge(FridayPublisherSecondaryCut *cut,unsigned index) {
    if(!cut||index>=cut->nedges||!cut->edges[index]||cut->edges[index]==Py_None)return NULL;
    return cut->edges[index];
}
static int secondary_class_owned(FridayPublisherRootCommandReceipt *r,PyTypeObject *type,int *owned) {
    *owned=0;
    if(secondary_debit_scan(root_storage.own_values.count,0)<0)return -1;
    for(uint64_t i=0;i<root_storage.own_values.count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        FridayPublisherOwnValue *p=&root_storage.own_values.rows[i];
        if(own_class_success(p)&&p->refs[0]==(PyObject *)type&&
           p->refs[2]&&p->refs[3]&&p->refs[4]&&p->refs[5]){*owned=1;return 0;}
    }
    return 0;
}
static int secondary_edge_is(FridayPublisherRootCommandReceipt *r,FridayPublisherRootValueNode *v,
    uint64_t index,PyObject *expect,int role) {
    if(index>=v->edges)return -1;
    uint64_t id=r->edges[v->edge_at+index];
    if(!id||id>r->node_count)return -1;
    if(r->nodes[id-1].original!=(expect?expect:Py_None))return -1;
    if(role>=0&&r->edge_roles[v->edge_at+index]!=(unsigned char)role)return -1;
    return 0;
}
static int secondary_take_named(FridayPublisherRootCommandReceipt *r,FridayPublisherRootValueNode *v,
    uint64_t *at,const char *expect) {
    uint64_t end=v->body_at+v->body_bytes,n=0;
    if(*at>end||end-*at<sizeof(n))return -1;
    memcpy(&n,r->body+*at,sizeof(n));*at+=sizeof(n);
    if(n>end-*at||strlen(expect)!=n||memcmp(r->body+*at,expect,(size_t)n))return -1;
    *at+=n;return 0;
}
static int secondary_take_nullable(FridayPublisherRootCommandReceipt *r,FridayPublisherRootValueNode *v,
    uint64_t *at,uint64_t *ei,PyObject *expect,int role) {
    uint64_t end=v->body_at+v->body_bytes;
    if(*at>=end)return -1;
    unsigned char present=r->body[(*at)++];
    if(present!=(expect!=NULL))return -1;
    return secondary_edge_is(r,v,(*ei)++,expect,present?role:0);
}
static int command_secondary_write_error(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v) {
    PyObject *o=v->original;
    int builtin=command_builtin_exception(Py_TYPE(o)),owned=0;
    if(!builtin&&secondary_class_owned(r,Py_TYPE(o),&owned)<0)return -1;
    v->kind=COMMAND_VALUE_ERROR;
    if(command_named(r,builtin?"builtins":owned?"owned":"other")<0||
       command_named(r,Py_TYPE(o)->tp_name)<0)return -1;
    PyBaseExceptionObject *e=(PyBaseExceptionObject *)o;
    if(command_role_edge(r,(PyObject *)Py_TYPE(o),0)<0||
       command_nullable_edge(r,e->args,1)<0||command_nullable_edge(r,e->dict,1)<0||
       command_nullable_edge(r,e->notes,1)<0||command_nullable_edge(r,e->cause,1)<0||
       command_nullable_edge(r,e->context,1)<0||command_nullable_edge(r,e->traceback,1)<0||
       command_append(r,&e->suppress_context,sizeof(e->suppress_context))<0)return -1;
#define VALUE(x) do { if(command_nullable_edge(r,(x),1)<0)return -1; } while(0)
    if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_OSError)) {
        PyOSErrorObject *q=(PyOSErrorObject *)o;
        VALUE(q->myerrno);VALUE(q->strerror);VALUE(q->filename);VALUE(q->filename2);
        if(command_append(r,&q->written,sizeof(q->written))<0)return -1;
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_SyntaxError)) {
        PySyntaxErrorObject *q=(PySyntaxErrorObject *)o;
        VALUE(q->msg);VALUE(q->filename);VALUE(q->lineno);VALUE(q->offset);
        VALUE(q->end_lineno);VALUE(q->end_offset);VALUE(q->text);VALUE(q->print_file_and_line);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_UnicodeError)) {
        if(Py_TYPE(o)==(PyTypeObject *)PyExc_UnicodeEncodeError||
           Py_TYPE(o)==(PyTypeObject *)PyExc_UnicodeDecodeError||
           Py_TYPE(o)==(PyTypeObject *)PyExc_UnicodeTranslateError) {
            PyUnicodeErrorObject *q=(PyUnicodeErrorObject *)o;
            VALUE(q->encoding);VALUE(q->object);VALUE(q->reason);
            if(command_append(r,&q->start,sizeof(q->start))<0||
               command_append(r,&q->end,sizeof(q->end))<0)return -1;
        }
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_ImportError)) {
        PyImportErrorObject *q=(PyImportErrorObject *)o;
        VALUE(q->msg);VALUE(q->name);VALUE(q->path);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_AttributeError)) {
        PyAttributeErrorObject *q=(PyAttributeErrorObject *)o;VALUE(q->obj);VALUE(q->name);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_NameError)) {
        PyNameErrorObject *q=(PyNameErrorObject *)o;VALUE(q->name);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_SystemExit)) {
        PySystemExitObject *q=(PySystemExitObject *)o;VALUE(q->code);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_StopIteration)) {
        PyStopIterationObject *q=(PyStopIterationObject *)o;VALUE(q->value);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_BaseExceptionGroup)) {
        PyBaseExceptionGroupObject *q=(PyBaseExceptionGroupObject *)o;VALUE(q->msg);VALUE(q->excs);
    }
#undef VALUE
    v->data_read=1;return 0;
}
static int command_secondary_frame(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v,FridayPublisherSecondaryCut *cut) {
    if(cut&&cut->kind!=SECONDARY_CUT_FRAME)return -1;
    v->kind=COMMAND_VALUE_FRAME;v->body_at=r->body_bytes;v->edge_at=r->edge_count;
    /* No frame getter. A missing preowned exact body stays unresolved. */
    if(!cut||cut->kind!=SECONDARY_CUT_FRAME||cut->nedges<5||cut->nscalars<5) {
        v->data_read=0;
        if(!r->secondary_first_unresolved)r->secondary_first_unresolved=v->id;
        return 0;
    }
    int64_t head[2]={cut->scalars[0],1};
    if(command_append(r,head,sizeof(head))<0||
       command_append(r,&cut->scalars[2],sizeof(int64_t)*3)<0)return -1;
    PyObject *code=cut->edges[0]&&cut->edges[0]!=Py_None?cut->edges[0]:NULL;
    PyObject *locals=cut->edges[1]&&cut->edges[1]!=Py_None&&PyDict_CheckExact(cut->edges[1])?cut->edges[1]:NULL;
    PyObject *globals=cut->edges[2]&&cut->edges[2]!=Py_None?cut->edges[2]:NULL;
    PyObject *trace=cut->edges[3]&&cut->edges[3]!=Py_None?cut->edges[3]:NULL;
    PyObject *builtins=cut->edges[4]&&cut->edges[4]!=Py_None?cut->edges[4]:NULL;
    int globals_role=secondary_globals_role(r,globals);
    if(globals_role<0)return -1;
    if(command_role_edge(r,code,1)<0||command_nullable_edge(r,locals,1)<0||
       command_role_edge(r,globals,globals?globals_role:0)<0)return -1;
    if(globals&&!globals_role)
        r->nodes[r->edges[r->edge_count-1]-1].support_verified=2;
    if(command_nullable_edge(r,trace,1)<0||command_role_edge(r,builtins,0)<0)return -1;
    if(builtins)
        r->nodes[r->edges[r->edge_count-1]-1].support_verified=2;
    v->data_read=locals!=NULL&&code!=NULL;
    if(!v->data_read&&!r->secondary_first_unresolved)r->secondary_first_unresolved=v->id;
    return 0;
}
static int secondary_choose_bytes(PyObject *cached,PyObject *registered,PyObject **chosen) {
    if(registered&&!PyBytes_CheckExact(registered))return -1;
    if(cached&&!PyBytes_CheckExact(cached))cached=NULL;
    if(cached&&registered&&cached!=registered) {
        if(PyBytes_GET_SIZE(cached)!=PyBytes_GET_SIZE(registered)||
           memcmp(PyBytes_AS_STRING(cached),PyBytes_AS_STRING(registered),(size_t)PyBytes_GET_SIZE(cached)))
            return -1;
    }
    *chosen=registered?registered:cached;return 0;
}
static int secondary_code_items(PyCodeObject *q,FridayPublisherSecondaryCut *cut,PyObject *item[13],int *full) {
    PyObject *cached_code=NULL,*cached_var=NULL,*cached_free=NULL,*cached_cell=NULL;
    if(q->_co_cached) {
        cached_code=q->_co_cached->_co_code;
        cached_var=q->_co_cached->_co_varnames;
        cached_free=q->_co_cached->_co_freevars;
        cached_cell=q->_co_cached->_co_cellvars;
    }
    if(secondary_choose_bytes(cached_code,secondary_cut_edge(cut,0),&item[0])<0)return -1;
    item[1]=q->co_consts;item[2]=q->co_names;
    PyObject *registered_var=secondary_cut_edge(cut,3);
    if(registered_var&&cached_var&&registered_var!=cached_var)return -1;
    item[3]=registered_var?registered_var:cached_var;
    item[4]=q->co_filename;item[5]=q->co_name;item[6]=q->co_qualname;
    item[7]=q->co_linetable;item[8]=q->co_exceptiontable;
    PyObject *registered_free=secondary_cut_edge(cut,9),*registered_cell=secondary_cut_edge(cut,10);
    if(registered_free&&cached_free&&registered_free!=cached_free)return -1;
    if(registered_cell&&cached_cell&&registered_cell!=cached_cell)return -1;
    item[9]=registered_free?registered_free:cached_free;
    item[10]=registered_cell?registered_cell:cached_cell;
    item[11]=q->co_localsplusnames;item[12]=q->co_localspluskinds;
    unsigned direct[]={1,2,4,5,6,7,8,11,12};
    for(unsigned i=0;i<sizeof(direct)/sizeof(*direct);i++) {
        PyObject *registered=secondary_cut_edge(cut,direct[i]);
        if(registered&&item[direct[i]]&&registered!=item[direct[i]])return -1;
    }
    *full=item[0]!=NULL;return 0;
}
static int command_secondary_code(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v,FridayPublisherSecondaryCut *cut) {
    if(cut&&cut->kind!=SECONDARY_CUT_CODE)return -1;
    PyCodeObject *q=(PyCodeObject *)v->original;
    PyObject *item[13];int full=0;
    if(secondary_code_items(q,cut,item,&full)<0)return -1;
    int64_t scalars[7]={q->co_argcount,q->co_posonlyargcount,q->co_kwonlyargcount,
        q->co_nlocals,q->co_stacksize,q->co_flags,q->co_firstlineno};
    if(cut&&(cut->nscalars<7||memcmp(cut->scalars,scalars,sizeof(scalars))))return -1;
    v->kind=COMMAND_VALUE_CODE;v->body_at=r->body_bytes;v->edge_at=r->edge_count;
    if(command_append(r,scalars,sizeof(scalars))<0)return -1;
    for(int i=0;i<13;i++)if(command_nullable_edge(r,item[i],1)<0)return -1;
    v->data_read=full;
    if(!full&&!r->secondary_first_unresolved)r->secondary_first_unresolved=v->id;
    return 0;
}
static int command_view_cut(FridayPublisherRootCommandReceipt *r,
    const Py_buffer *view,FridayPublisherSecondaryCut *cut) {
    if(!cut)return 0;
    if(cut->kind!=SECONDARY_CUT_BUFFER||cut->nedges!=5||cut->nscalars!=2||
       cut->scalars[0]!=(view->readonly?1:0)||cut->scalars[1]!=view->len||
       FridayPublisherMasterBefore(&root_storage.pool,16384+(uint64_t)view->ndim*256,0,0,0)<0)return -1;
    PyObject *format=secondary_cut_edge(cut,2);
    const char *actual=view->format?view->format:"B";
    if(!format||!PyUnicode_CheckExact(format)||
       PyUnicode_CompareWithASCIIString(format,actual)!=0||PyErr_Occurred())return -1;
    PyObject *shape=secondary_cut_edge(cut,3),*strides=secondary_cut_edge(cut,4);
    if(!shape||!strides||!PyTuple_CheckExact(shape)||!PyTuple_CheckExact(strides)||
       PyTuple_GET_SIZE(shape)!=view->ndim||PyTuple_GET_SIZE(strides)!=view->ndim)return -1;
    Py_ssize_t stride=view->shape?view->itemsize:1;
    for(int d=view->ndim-1;d>=0;d--) {
        PyObject *s=PyTuple_GET_ITEM(shape,d),*t=PyTuple_GET_ITEM(strides,d);
        if(!PyLong_CheckExact(s)||!PyLong_CheckExact(t))return -1;
        Py_ssize_t actual_shape=view->shape?view->shape[d]:view->len;
        Py_ssize_t actual_stride=view->strides?view->strides[d]:stride;
        Py_ssize_t n=PyLong_AsSsize_t(s);if(PyErr_Occurred()||n!=actual_shape)return -1;
        n=PyLong_AsSsize_t(t);if(PyErr_Occurred()||n!=actual_stride)return -1;
        if(!view->strides&&d) {
            if(actual_shape&&stride>PY_SSIZE_T_MAX/actual_shape)return -1;
            stride*=actual_shape;
        }
    }
    return 0;
}
static int command_secondary_buffer(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v,FridayPublisherSecondaryCut *cut) {
    if(Py_TYPE(v->original)!=&PyMemoryView_Type||(cut&&cut->kind!=SECONDARY_CUT_BUFFER))return -1;
    PyObject *o=v->original;PyMemoryViewObject *mv=(PyMemoryViewObject *)o;
    int released=(mv->flags&_Py_MEMORYVIEW_RELEASED)!=0;
    const Py_buffer *view=released?NULL:PyMemoryView_GET_BUFFER(o);
    /* Released primary values need the existing single secondary historical
     * cut, not a falsely complete primary segment that skips that reader. */
    if(released&&!r->secondary_read_attempted)return -1;
    uint64_t metadata=0;
    if(!released&&(command_view_metadata(r,view,NULL,UINT64_MAX,&metadata)<0||
       command_view_cut(r,view,cut)<0))return -1;
    PyObject *registered=secondary_cut_edge(cut,1);
    if(registered&&!PyBytes_CheckExact(registered))return -1;
    if(!released&&registered&&
       command_view_raw(r,view,(const unsigned char *)PyBytes_AS_STRING(registered),
                        (uint64_t)PyBytes_GET_SIZE(registered))<0)return -1;
    int64_t head[5]={266,released?1:0,released?-1:(view->readonly?1:0),
        released?-1:(int64_t)view->len,(int64_t)metadata};
    v->kind=COMMAND_VALUE_MEMORYVIEW;v->body_at=r->body_bytes;v->edge_at=r->edge_count;
    if(command_append(r,head,sizeof(head))<0)return -1;
    PyObject *base=!released?PyMemoryView_GET_BASE(o):secondary_cut_edge(cut,0);
    if(cut&&!released&&base!=secondary_cut_edge(cut,0))return -1;
    if(command_nullable_edge(r,base,1)<0||command_nullable_edge(r,registered,1)<0||
       command_nullable_edge(r,secondary_cut_edge(cut,2),1)<0||
       command_nullable_edge(r,secondary_cut_edge(cut,3),1)<0||
       command_nullable_edge(r,secondary_cut_edge(cut,4),1)<0)return -1;
    if(released) {
        /* No fabricated current geometry after release. The exact registered
         * historical bytes and format/shape/strides remain full DATA edges. */
        if(registered) {
            Py_ssize_t n=PyBytes_GET_SIZE(registered);
            if(n<0||FridayPublisherMasterBefore(&root_storage.pool,(uint64_t)n,0,0,0)<0||
               command_append(r,PyBytes_AS_STRING(registered),(uint64_t)n)<0)return -1;
        }
    } else {
        uint64_t written=0;
        if(command_view_metadata(r,view,NULL,0,&written)<0||written!=metadata||
           command_view_raw(r,view,NULL,(uint64_t)view->len)<0)return -1;
    }
    v->data_read=!released||registered!=NULL;
    if(!v->data_read&&!r->secondary_first_unresolved)r->secondary_first_unresolved=v->id;
    return 0;
}
static int command_secondary_source(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v,FridayPublisherSecondaryCut *cut) {
    if(!cut||cut->kind!=SECONDARY_CUT_SOURCE||cut->nedges<2)return -1;
    PyObject *tp=cut->edges[0],*dict=cut->edges[1]==Py_None?NULL:cut->edges[1];
    if(tp!=(PyObject *)Py_TYPE(v->original))return -1;
    if(dict&&!PyDict_CheckExact(dict))return -1;
    v->kind=COMMAND_VALUE_SOURCE;v->body_at=r->body_bytes;v->edge_at=r->edge_count;
    if(command_named(r,Py_TYPE(v->original)->tp_name)<0||
       command_role_edge(r,tp,0)<0||command_nullable_edge(r,dict,1)<0)return -1;
    v->data_read=dict!=NULL;
    if(!v->data_read&&!r->secondary_first_unresolved)r->secondary_first_unresolved=v->id;
    return 0;
}
static int command_secondary_traceback(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootValueNode *v,FridayPublisherSecondaryCut *cut) {
    if(cut&&cut->kind!=SECONDARY_CUT_TRACEBACK)return -1;
    PyTracebackObject *q=(PyTracebackObject *)v->original;
    if(cut&&cut->nscalars>=2&&(cut->scalars[0]!=q->tb_lineno||cut->scalars[1]!=q->tb_lasti))return -1;
    if(cut&&cut->nedges>1) {
        PyObject *next=cut->edges[0]==Py_None?NULL:cut->edges[0];
        PyObject *frame=cut->edges[1]==Py_None?NULL:cut->edges[1];
        if(next!=(PyObject *)q->tb_next||frame!=(PyObject *)q->tb_frame)return -1;
    }
    v->kind=COMMAND_VALUE_TRACEBACK;v->body_at=r->body_bytes;v->edge_at=r->edge_count;
    if(command_nullable_edge(r,(PyObject *)q->tb_next,1)<0||
       command_nullable_edge(r,(PyObject *)q->tb_frame,1)<0||
       command_append(r,&q->tb_lineno,sizeof(q->tb_lineno))<0||
       command_append(r,&q->tb_lasti,sizeof(q->tb_lasti))<0)return -1;
    v->data_read=1;return 0;
}
static uint64_t command_find_named(FridayPublisherRootCommandReceipt *r,PyObject *o) {
    if(!o)return 0;
    /* Every call pays its own complete probe, on the SAME original pool.
     * At most node_count distinct occupied slots precede the first empty
     * slot: command_node inserts one index slot per new node. The extra slot
     * pays that empty read; no full-index cap or clock replaces this debit. */
    if(r->node_count>FRIDAY_ROOT_COMMAND_NODES||
       r->node_count==UINT64_MAX)return 0;
    uint64_t bound=r->node_count+1;
    if(bound>FRIDAY_ROOT_COMMAND_INDEX||secondary_debit_scan(bound,0)<0)return 0;
    uintptr_t p=(uintptr_t)o;
    uint64_t slot=((p>>4)^(p>>25))&(FRIDAY_ROOT_COMMAND_INDEX-1);
    for(uint64_t n=0;n<bound;n++) {
        if((n&1023)==0&&!command_clock(r))return 0;
        uint64_t id=r->index[slot];
        if(!id)return 0;
        if(id>r->node_count)return 0;
        if(r->nodes[id-1].original==o)return id;
        slot=(slot+1)&(FRIDAY_ROOT_COMMAND_INDEX-1);
    }
    return 0;
}
static int command_error_traceback_phase(FridayPublisherRootCommandReceipt *r,
    PyObject *historical,PyObject *current) {
    /* Same object, including a null first cut, keeps the existing relation.
     * A later traceback is lawful only when its tb_next chain reaches the
     * retained first object and both are already named. No cut is rewritten. */
    if(historical==current||!historical)return 0;
    if(!current||Py_TYPE(historical)!=&PyTraceBack_Type||Py_TYPE(current)!=&PyTraceBack_Type)
        return -1;
    uint64_t bound=r->node_count;
    if(!bound||bound>FRIDAY_ROOT_COMMAND_NODES)return -1;
    if(secondary_debit_scan(bound,0)<0)return -1;
    PyObject *cursor=current;
    for(uint64_t step=0;step<bound;step++) {
        if((step&1023)==0&&!command_clock(r))return -1;
        if(cursor==historical) {
            if(!command_find_named(r,historical)||!command_find_named(r,current))return -1;
            return 0;
        }
        if(!cursor||Py_TYPE(cursor)!=&PyTraceBack_Type)return -1;
        cursor=(PyObject *)((PyTracebackObject *)cursor)->tb_next;
    }
    return -1;
}
static int command_error_handler_current(FridayPublisherRootCommandReceipt *r,
    PyObject *error,PyObject *context,uint64_t after) {
    PyBaseExceptionObject *e=(PyBaseExceptionObject *)error;
    if(context==e->context)return 1;
    uint64_t count=root_storage.secondary.count;
    if(count>FRIDAY_ROOT_SECONDARY_CUTS||after>count||
       secondary_debit_scan(count-after,0)<0)return -1;
    for(uint64_t i=after;i<count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        FridayPublisherSecondaryCut *p=&root_storage.secondary.rows[i];
        if(p->kind!=SECONDARY_CUT_ERROR_HANDLER)continue;
        if(p->nedges!=5||p->nscalars||p->pid!=r->pid)return -1;
        PyObject *raised=secondary_cut_edge(p,0),*handler=secondary_cut_edge(p,1);
        PyObject *before_context=secondary_cut_edge(p,2),*before_tb=secondary_cut_edge(p,3);
        if(raised!=error||before_context!=context)continue;
        if(!handler||!PyExceptionInstance_Check(handler))return -1;
        if(!e->traceback||e->traceback==before_tb||Py_TYPE(e->traceback)!=&PyTraceBack_Type)continue;
        if(command_error_traceback_phase(r,before_tb,e->traceback)<0||
           !command_find_named(r,e->traceback)||!command_find_named(r,p->original)||
           !command_find_named(r,raised)||!command_find_named(r,handler))return -1;
        context=handler;if(context==e->context)return 1;
    }
    return 0;
}
static int command_error_context_phase(FridayPublisherRootCommandReceipt *r,
    PyObject *error,PyObject *historical,PyObject *current) {
    if(historical==current)return 0;
    uint64_t count=root_storage.secondary.count;
    if(count>FRIDAY_ROOT_SECONDARY_CUTS||secondary_debit_scan(count,0)<0)return -1;
    PyObject *cursor=historical;
    for(uint64_t i=0;i<count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        FridayPublisherSecondaryCut *p=&root_storage.secondary.rows[i];
        if(p->kind!=SECONDARY_CUT_ERROR_HANDLER)continue;
        if(p->nedges!=5||p->nscalars||p->pid!=r->pid||!p->original)return -1;
        PyObject *raised=secondary_cut_edge(p,0),*handler=secondary_cut_edge(p,1);
        PyObject *before_context=secondary_cut_edge(p,2),*before_tb=secondary_cut_edge(p,3);
        PyObject *chain=secondary_cut_edge(p,4);
        if(!raised||!handler||!PyExceptionInstance_Check(raised)||
           !PyExceptionInstance_Check(handler)||!chain||!PyTuple_CheckExact(chain))return -1;
        if(raised!=error&&cursor!=raised)continue;
        PyBaseExceptionObject *e=(PyBaseExceptionObject *)raised;
        /* A mere setter or a stale same-TB handler witness is not a re-raise.
         * Both new TB endpoints and the complete captured handler DATA must
         * already be actual named nodes before either relation is accepted. */
        if(!e->traceback||e->traceback==before_tb||Py_TYPE(e->traceback)!=&PyTraceBack_Type)continue;
        if(command_error_traceback_phase(r,before_tb,e->traceback)<0)return -1;
        if(!command_find_named(r,e->traceback)||!command_find_named(r,p->original)||
           !command_find_named(r,raised)||!command_find_named(r,handler))return -1;
        if(raised==error&&cursor==before_context)cursor=handler;
        else if(cursor==raised) {
            int joined=command_error_handler_current(r,raised,handler,i+1);
            if(joined<0)return -1;
            if(!joined)continue;
            Py_ssize_t n=PyTuple_GET_SIZE(chain);
            if(n<1||(uint64_t)n>count||(uint64_t)n>UINT64_MAX/2||
               secondary_debit_scan((uint64_t)n*2,0)<0)return -1;
            for(Py_ssize_t j=0;j<n;j++) {
                PyObject *pair=PyTuple_GET_ITEM(chain,j);
                if(!PyTuple_CheckExact(pair)||PyTuple_GET_SIZE(pair)!=2)return -1;
                if(PyTuple_GET_ITEM(pair,0)==error&&PyTuple_GET_ITEM(pair,1)==raised) {
                    cursor=NULL;break;
                }
            }
        }
        if(cursor==current)return 0;
    }
    return -1;
}
static int command_secondary_error_check(FridayPublisherRootCommandReceipt *r,FridayPublisherRootValueNode *v) {
    PyObject *o=v->original;uint64_t at=v->body_at,ei=0;
    if(!PyExceptionInstance_Check(o))return -1;
    int builtin=command_builtin_exception(Py_TYPE(o)),owned=0;
    const char *module=NULL,*name=NULL;
    if(v->encoding==1) {
        owned=builtin?0:command_source_class(r,Py_TYPE(o),&module,&name);
        if(owned<0||(!builtin&&!owned))return -1;
        if(builtin){module="builtins";name=Py_TYPE(o)->tp_name;}
    } else {
        if(!builtin&&secondary_class_owned(r,Py_TYPE(o),&owned)<0)return -1;
        module=builtin?"builtins":owned?"owned":"other";name=Py_TYPE(o)->tp_name;
    }
    if(secondary_take_named(r,v,&at,module)<0||
       secondary_take_named(r,v,&at,name)<0||
       secondary_edge_is(r,v,ei++,(PyObject *)Py_TYPE(o),0)<0)return -1;
    PyBaseExceptionObject *e=(PyBaseExceptionObject *)o;
    if(secondary_take_nullable(r,v,&at,&ei,e->args,1)<0||
       secondary_take_nullable(r,v,&at,&ei,e->dict,1)<0||
       secondary_take_nullable(r,v,&at,&ei,e->notes,1)<0||
       secondary_take_nullable(r,v,&at,&ei,e->cause,1)<0||
       secondary_take_nullable(r,v,&at,&ei,e->context,1)<0||
       secondary_take_nullable(r,v,&at,&ei,e->traceback,1)<0)return -1;
    if(at+sizeof(e->suppress_context)>v->body_at+v->body_bytes||
       memcmp(r->body+at,&e->suppress_context,sizeof(e->suppress_context)))return -1;
    at+=sizeof(e->suppress_context);
#define TAKE(x) do { if(secondary_take_nullable(r,v,&at,&ei,(x),1)<0)return -1; } while(0)
    if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_OSError)) {
        PyOSErrorObject *q=(PyOSErrorObject *)o;
        TAKE(q->myerrno);TAKE(q->strerror);TAKE(q->filename);TAKE(q->filename2);
        if(at+sizeof(q->written)>v->body_at+v->body_bytes||
           memcmp(r->body+at,&q->written,sizeof(q->written)))return -1;
        at+=sizeof(q->written);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_SyntaxError)) {
        PySyntaxErrorObject *q=(PySyntaxErrorObject *)o;
        TAKE(q->msg);TAKE(q->filename);TAKE(q->lineno);TAKE(q->offset);
        TAKE(q->end_lineno);TAKE(q->end_offset);TAKE(q->text);TAKE(q->print_file_and_line);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_UnicodeError)) {
        if(Py_TYPE(o)==(PyTypeObject *)PyExc_UnicodeEncodeError||
           Py_TYPE(o)==(PyTypeObject *)PyExc_UnicodeDecodeError||
           Py_TYPE(o)==(PyTypeObject *)PyExc_UnicodeTranslateError) {
            PyUnicodeErrorObject *q=(PyUnicodeErrorObject *)o;
            TAKE(q->encoding);TAKE(q->object);TAKE(q->reason);
            if(at+sizeof(q->start)+sizeof(q->end)>v->body_at+v->body_bytes||
               memcmp(r->body+at,&q->start,sizeof(q->start))||
               memcmp(r->body+at+sizeof(q->start),&q->end,sizeof(q->end)))return -1;
            at+=sizeof(q->start)+sizeof(q->end);
        }
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_ImportError)) {
        PyImportErrorObject *q=(PyImportErrorObject *)o;TAKE(q->msg);TAKE(q->name);TAKE(q->path);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_AttributeError)) {
        PyAttributeErrorObject *q=(PyAttributeErrorObject *)o;TAKE(q->obj);TAKE(q->name);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_NameError)) {
        PyNameErrorObject *q=(PyNameErrorObject *)o;TAKE(q->name);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_SystemExit)) {
        PySystemExitObject *q=(PySystemExitObject *)o;TAKE(q->code);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_StopIteration)) {
        PyStopIterationObject *q=(PyStopIterationObject *)o;TAKE(q->value);
    } else if(PyObject_TypeCheck(o,(PyTypeObject *)PyExc_BaseExceptionGroup)) {
        PyBaseExceptionGroupObject *q=(PyBaseExceptionGroupObject *)o;TAKE(q->msg);TAKE(q->excs);
    }
#undef TAKE
    if(ei!=v->edges||at!=v->body_at+v->body_bytes)return -1;
    FridayPublisherSecondaryCut *cut=NULL;
    if(secondary_cut_lookup(r,o,&cut)<0)return -1;
    if(cut&&cut->kind==SECONDARY_CUT_ERROR&&cut->nedges>=7) {
        PyObject *args=secondary_cut_edge(cut,1),*dict=secondary_cut_edge(cut,2);
        PyObject *notes=cut->edges[3]==Py_None?NULL:cut->edges[3];
        PyObject *cause=secondary_cut_edge(cut,4),*context=secondary_cut_edge(cut,5);
        PyObject *tb=secondary_cut_edge(cut,6);
        if((args&&args!=e->args)||(dict&&dict!=e->dict)||notes!=e->notes||
           (cause&&cause!=e->cause)||command_error_context_phase(r,o,context,e->context)<0||
           command_error_traceback_phase(r,tb,e->traceback)<0)return -1;
    }
    return 0;
}
static int command_secondary_frame_check(FridayPublisherRootCommandReceipt *r,FridayPublisherRootValueNode *v) {
    FridayPublisherSecondaryCut *cut=NULL;
    if(secondary_cut_lookup(r,v->original,&cut)<0)return -1;
    if(!cut||cut->kind!=SECONDARY_CUT_FRAME||cut->nedges<5||cut->nscalars<5)
        return v->edges==0&&v->body_bytes==0&&!v->data_read?0:-1;
    if(v->edges!=5)return -1;
    uint64_t at=v->body_at;int64_t head[2],extra[3];
    if(at+sizeof(head)+sizeof(extra)+2!=v->body_at+v->body_bytes)return -1;
    memcpy(head,r->body+at,sizeof(head));at+=sizeof(head);
    memcpy(extra,r->body+at,sizeof(extra));at+=sizeof(extra);
    if(head[0]!=cut->scalars[0]||head[1]!=1||memcmp(extra,&cut->scalars[2],sizeof(extra)))return -1;
    PyObject *code=cut->edges[0]&&cut->edges[0]!=Py_None?cut->edges[0]:NULL;
    PyObject *locals=cut->edges[1]&&cut->edges[1]!=Py_None&&PyDict_CheckExact(cut->edges[1])?cut->edges[1]:NULL;
    PyObject *globals=cut->edges[2]&&cut->edges[2]!=Py_None?cut->edges[2]:NULL;
    PyObject *trace=cut->edges[3]&&cut->edges[3]!=Py_None?cut->edges[3]:NULL;
    PyObject *builtins=cut->edges[4]&&cut->edges[4]!=Py_None?cut->edges[4]:NULL;
    unsigned char locals_present=r->body[at++],trace_present=r->body[at++];
    if(locals_present!=(locals!=NULL)||trace_present!=(trace!=NULL))return -1;
    int globals_role=secondary_globals_role(r,globals);
    if(globals_role<0)return -1;
    if(secondary_edge_is(r,v,0,code,1)<0||secondary_edge_is(r,v,1,locals,locals?1:0)<0||
       secondary_edge_is(r,v,2,globals,globals_role)<0||secondary_edge_is(r,v,3,trace,trace?1:0)<0||
       secondary_edge_is(r,v,4,builtins,0)<0)return -1;
    return (v->data_read!=0)==(locals!=NULL&&code!=NULL)?0:-1;
}
static int command_secondary_code_check(FridayPublisherRootCommandReceipt *r,FridayPublisherRootValueNode *v) {
    PyCodeObject *q=(PyCodeObject *)v->original;
    FridayPublisherSecondaryCut *cut=NULL;
    if(secondary_cut_lookup(r,v->original,&cut)<0)return -1;
    PyObject *item[13];int full=0;
    if(secondary_code_items(q,cut,item,&full)<0||v->edges!=13)return -1;
    int64_t scalars[7]={q->co_argcount,q->co_posonlyargcount,q->co_kwonlyargcount,
        q->co_nlocals,q->co_stacksize,q->co_flags,q->co_firstlineno};
    if(v->body_bytes<sizeof(scalars)+13||memcmp(r->body+v->body_at,scalars,sizeof(scalars)))return -1;
    for(int i=0;i<13;i++) {
        unsigned char present=r->body[v->body_at+sizeof(scalars)+i];
        if(present!=(item[i]!=NULL)||secondary_edge_is(r,v,(uint64_t)i,item[i],present?1:0)<0)return -1;
    }
    if((v->data_read!=0)!=(full!=0))return -1;
    return v->body_bytes==sizeof(scalars)+13?0:-1;
}
static int command_secondary_buffer_check(FridayPublisherRootCommandReceipt *r,FridayPublisherRootValueNode *v) {
    uint64_t metadata_at=0,raw_at=0;
    if(Py_TYPE(v->original)!=&PyMemoryView_Type||
       command_view_body_shape(r,v,&metadata_at,&raw_at)<0)return -1;
    PyObject *o=v->original;PyMemoryViewObject *mv=(PyMemoryViewObject *)o;
    int released=(mv->flags&_Py_MEMORYVIEW_RELEASED)!=0;
    const Py_buffer *view=released?NULL:PyMemoryView_GET_BUFFER(o);
    FridayPublisherSecondaryCut *cut=NULL;
    if(v->encoding==2&&(secondary_cut_lookup(r,o,&cut)<0||
       (cut&&cut->kind!=SECONDARY_CUT_BUFFER)))return -1;
    PyObject *registered=secondary_cut_edge(cut,1);
    if(registered&&!PyBytes_CheckExact(registered))return -1;
    int64_t head[5];memcpy(head,r->body+v->body_at,sizeof(head));
    if(head[1]!=(released?1:0)||
       head[2]!=(released?-1:(view->readonly?1:0))||
       head[3]!=(released?-1:(int64_t)view->len))return -1;
    PyObject *base=!released?PyMemoryView_GET_BASE(o):secondary_cut_edge(cut,0);
    if(cut&&!released&&base!=secondary_cut_edge(cut,0))return -1;
    PyObject *edges[5]={base,registered,secondary_cut_edge(cut,2),secondary_cut_edge(cut,3),secondary_cut_edge(cut,4)};
    for(int i=0;i<5;i++) {
        unsigned char present=r->body[v->body_at+sizeof(head)+i];
        if(present!=(edges[i]!=NULL)||secondary_edge_is(r,v,(uint64_t)i,edges[i],present?1:0)<0)return -1;
    }
    uint64_t raw_bytes=v->body_at+v->body_bytes-raw_at;
    if(!released) {
        uint64_t metadata=0;
        if(command_view_metadata(r,view,r->body+metadata_at,(uint64_t)head[4],&metadata)<0||
           command_view_cut(r,view,cut)<0||
           command_view_raw(r,view,r->body+raw_at,raw_bytes)<0)return -1;
        if(registered&&command_view_raw(r,view,(const unsigned char *)PyBytes_AS_STRING(registered),
                                        (uint64_t)PyBytes_GET_SIZE(registered))<0)return -1;
    } else if(registered) {
        Py_ssize_t n=PyBytes_GET_SIZE(registered);
        if(n<0||raw_bytes!=(uint64_t)n||
           FridayPublisherMasterBefore(&root_storage.pool,2*(uint64_t)n,0,0,0)<0||
           (n&&memcmp(r->body+raw_at,PyBytes_AS_STRING(registered),(size_t)n)))return -1;
    } else if(raw_bytes)return -1;
    return (v->data_read!=0)==(!released||registered!=NULL)?0:-1;
}
static int command_secondary_consumer(FridayPublisherRootCommandReceipt *r) {
    if(PyErr_Occurred()){r->pending_error_retained=1;return -1;}
    for(uint64_t i=0;i<r->node_count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        FridayPublisherRootValueNode *v=&r->nodes[i];PyObject *o=v->original;
        /* The secondary consumer also checks actual set-result aliases even
         * though the one retained tuple layout uses ordinary encoding1. */
        if(v->kind==COMMAND_VALUE_SET||v->kind==COMMAND_VALUE_FROZENSET) {
            if(command_set_live_check(r,v)<0)return -1;
            continue;
        }
        if(v->encoding!=2)continue;
        if(command_native_node_shape(r,v,NULL)<0)return -1;
        if(v->kind==COMMAND_VALUE_UNSUPPORTED) {
            if(v->edges||v->data_read)return -1;
            continue;
        }
        if(v->kind==COMMAND_VALUE_SUPPORT) {
            uint64_t at=v->body_at;int owned=0;
            if(!PyType_Check(o))return -1;
            int known=command_builtin_exception((PyTypeObject *)o);
            if(!known&&secondary_class_owned(r,(PyTypeObject *)o,&owned)<0)return -1;
            if(secondary_take_named(r,v,&at,known?
                "actual_selected_original_builtin_error_type_SUPPORT_ONLY":
                "actual_selected_owned_error_type_SUPPORT_ONLY")<0)return -1;
            if(at!=v->body_at+v->body_bytes||v->edges)return -1;
        } else if(v->kind==COMMAND_VALUE_ERROR) {
            if(command_secondary_error_check(r,v)<0)return -1;
        } else if(v->kind==COMMAND_VALUE_TRACEBACK) {
            PyTracebackObject *q=(PyTracebackObject *)o;
            uint64_t at=v->body_at,ei=0;
            if(secondary_take_nullable(r,v,&at,&ei,(PyObject *)q->tb_next,1)<0||
               secondary_take_nullable(r,v,&at,&ei,(PyObject *)q->tb_frame,1)<0)return -1;
            if(at+sizeof(q->tb_lineno)+sizeof(q->tb_lasti)!=v->body_at+v->body_bytes||
               memcmp(r->body+at,&q->tb_lineno,sizeof(q->tb_lineno))||
               memcmp(r->body+at+sizeof(q->tb_lineno),&q->tb_lasti,sizeof(q->tb_lasti))||
               ei!=v->edges)return -1;
        } else if(v->kind==COMMAND_VALUE_FRAME) {
            if(command_secondary_frame_check(r,v)<0)return -1;
        } else if(v->kind==COMMAND_VALUE_CODE) {
            if(command_secondary_code_check(r,v)<0)return -1;
        } else if(v->kind==COMMAND_VALUE_MEMORYVIEW) {
            if(command_secondary_buffer_check(r,v)<0)return -1;
        } else if(v->kind==COMMAND_VALUE_SOURCE) {
            uint64_t at=v->body_at,ei=0;
            if(secondary_take_named(r,v,&at,Py_TYPE(o)->tp_name)<0||
               secondary_edge_is(r,v,ei++,(PyObject *)Py_TYPE(o),0)<0)return -1;
            FridayPublisherSecondaryCut *cut=NULL;
            if(secondary_cut_lookup(r,o,&cut)<0||!cut)return -1;
            PyObject *dict=cut->edges[1]==Py_None?NULL:cut->edges[1];
            if(secondary_take_nullable(r,v,&at,&ei,dict,1)<0||ei!=v->edges||at!=v->body_at+v->body_bytes)return -1;
        } else if(v->kind==COMMAND_VALUE_DICT) {
            Py_ssize_t pos=0;PyObject *key,*value;uint64_t seen=0;
            while(PyDict_Next(o,&pos,&key,&value)) {
                if((seen&1023)==0&&!command_clock(r))return -1;
                if(seen+2>v->edges||secondary_edge_is(r,v,seen,key,1)<0||
                   secondary_edge_is(r,v,seen+1,value,1)<0)return -1;
                seen+=2;
            }
            if(seen!=v->edges)return -1;
        } else if(v->kind==COMMAND_VALUE_TUPLE||v->kind==COMMAND_VALUE_LIST) {
            int tuple=v->kind==COMMAND_VALUE_TUPLE;
            Py_ssize_t n=tuple?PyTuple_GET_SIZE(o):PyList_GET_SIZE(o);
            if(n<0||(uint64_t)n!=v->edges)return -1;
            for(Py_ssize_t j=0;j<n;j++) {
                PyObject *item=tuple?PyTuple_GET_ITEM(o,j):PyList_GET_ITEM(o,j);
                if(secondary_edge_is(r,v,(uint64_t)j,item,1)<0)return -1;
            }
        } else if(v->kind==COMMAND_VALUE_BYTES||v->kind==COMMAND_VALUE_BYTEARRAY) {
            int bytes=v->kind==COMMAND_VALUE_BYTES;
            Py_ssize_t n=bytes?PyBytes_GET_SIZE(o):PyByteArray_GET_SIZE(o);
            const void *raw=bytes?PyBytes_AS_STRING(o):PyByteArray_AS_STRING(o);
            if(n<0||(uint64_t)n!=v->body_bytes||(n&&memcmp(r->body+v->body_at,raw,(size_t)n)))return -1;
        } else if(v->kind==COMMAND_VALUE_BOOL) {
            unsigned char bit=o==Py_True;
            if(v->body_bytes!=1||r->body[v->body_at]!=bit)return -1;
        } else if(v->kind==COMMAND_VALUE_FLOAT) {
            double d=PyFloat_AS_DOUBLE(o);
            if(v->body_bytes!=sizeof(d)||memcmp(r->body+v->body_at,&d,sizeof(d)))return -1;
        }
    }
    return PyErr_Occurred()?-1:0;
}
/* SOL105 C1-R1D. One finite secondary read after the ordinary codec stops.
 * Preowned cuts supply frame locals, code bytes, buffer bytes, and source
 * dictionaries. Public frame/code/memoryview fields that do not allocate are
 * read here. Adaptive code bytes are not a substitute for co_code. A missing
 * locals cut or a cold code cache keeps the original and stays unresolved.
 * Role 0 is a support alias and is not enqueued. No third factory and no retry.
 */
static int command_secondary_payload(FridayPublisherRootCommandReceipt *r) {
    if(r->secondary_read_attempted||r->owner_retirement_attempted)return -1;
    r->secondary_read_attempted=1;r->secondary_payload_complete=0;
    r->required_values_complete=0;r->error_payload_complete=0;r->graph_consumed=0;
    command_graph_begin(r);r->secondary_body_at=r->body_bytes;
    if(PyErr_Occurred()){r->pending_error_retained=1;return -1;}
    /* A getter may have returned its exact new value before its primary
     * consumer refused it. MOVE that real owner into the SAME inventory;
     * do not leave it outside a purported complete graph or clear it. */
    if(r->getter_pending) {
        uint64_t before=r->owner_count;
        if(command_owner(r,&r->getter_pending,23,0,1)<0) {
            r->owner_inventory_complete=0;return -1;
        }
        if(r->owner_count!=before+1)return -1;
        r->secondary_pending_owner_at=before+1;
    }
    /* All actual moved roots and all already born graph nodes are in scope,
     * not only the newest error triple. Unmoved/unknown owners still prohibit
     * retirement through owner_inventory_complete and the existing guards. */
    for(uint64_t i=0;i<r->owner_count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        uint64_t id=r->owners[i].group>=24&&r->owners[i].group<=27?
            command_set_node(r,r->owners[i].owned):command_node(r,r->owners[i].owned);
        if(!id)return -1;r->owners[i].node=id;
    }
    PyObject *roots[]={r->cleanup_error.type,r->cleanup_error.value,r->cleanup_error.tb};
    for(unsigned j=0;j<3;j++)if(roots[j]) {
        uint64_t id=command_node(r,roots[j]);if(!id)return -1;
        r->secondary_roots[j]=id;r->secondary_error_present|=1ULL<<j;
    }
    PyObject *aux[]={r->stock_cursor,r->active_buffer_owned?r->active_buffer.obj:NULL};
    for(unsigned j=0;j<2;j++)if(aux[j]) {
        uint64_t id=command_node(r,aux[j]);if(!id)return -1;r->secondary_aux_roots[j]=id;
    }
    int complete=1;
    /* New aliases append to this same bounded node inventory and are visited
     * once. Complete primary representations are preserved byte-for-byte.
     * A failed/unsupported representation gets ONE separate appended segment. */
    for(uint64_t i=0;i<r->node_count;i++) {
        if(!command_clock(r))goto failed;
        FridayPublisherRootValueNode *v=&r->nodes[i];PyObject *o=v->original;
        if(v->segment) {
            if(v->segment>r->segment_count)goto failed;
            const FridayPublisherRootValueSegment *old=&r->segments[v->segment-1];
            if(old->complete&&v->kind!=COMMAND_VALUE_UNSUPPORTED)continue;
        }
        r->active_node=i+1;
        FridayPublisherSecondaryCut *cut=NULL;
        if(secondary_cut_lookup(r,o,&cut)<0)goto failed;
        int selected=PyExceptionInstance_Check(o)||Py_TYPE(o)==&PyTraceBack_Type||
            Py_TYPE(o)==&PyFrame_Type||Py_TYPE(o)==&PyCode_Type||
            Py_TYPE(o)==&PyMemoryView_Type||(cut&&cut->kind==SECONDARY_CUT_SOURCE);
        if(command_segment_begin(r,v,selected?2:1)<0)goto failed;
        r->secondary_factory_blocked=0;
        int rc;
        if(PyExceptionInstance_Check(o))rc=command_secondary_write_error(r,v);
        else if(Py_TYPE(o)==&PyTraceBack_Type)rc=command_secondary_traceback(r,v,cut);
        else if(Py_TYPE(o)==&PyFrame_Type)rc=command_secondary_frame(r,v,cut);
        else if(Py_TYPE(o)==&PyCode_Type)rc=command_secondary_code(r,v,cut);
        else if(Py_TYPE(o)==&PyMemoryView_Type)rc=command_secondary_buffer(r,v,cut);
        else if(cut&&cut->kind==SECONDARY_CUT_SOURCE)rc=command_secondary_source(r,v,cut);
        else rc=command_builtin(r,v);
        int pending=PyErr_Occurred()!=NULL;
        if(command_segment_end(r,v,rc==0&&!pending)<0)goto failed;
        r->secondary_seen[i]=1;r->secondary_nodes_read++;r->active_node=0;
        if(pending) {r->pending_error_retained=1;goto failed;}
        if(rc<0) {
            if(!r->secondary_first_unresolved)r->secondary_first_unresolved=i+1;
            /* A forbidden new factory has NOT run. Keep its partial segment
             * and originals, then collect other available nodes. No successful
             * full-graph decision can contain this incomplete segment. */
            if(!r->secondary_factory_blocked)goto failed;
            complete=0;
        }
    }
    r->secondary_body_bytes=r->body_bytes-r->secondary_body_at;
    if(!complete)return 1;
    if(command_sets_observe(r,2)<0)goto failed;
    r->secondary_body_bytes=r->body_bytes-r->secondary_body_at;
    if(command_values_finish(r)<0)goto failed;
    r->secondary_payload_complete=r->required_values_complete&&r->graph_consumed;
    r->error_payload_complete=r->secondary_payload_complete;
    if(command_set_region_end(r,1)<0)goto failed;
    return r->secondary_payload_complete?0:1;
failed:
    if(PyErr_Occurred())r->pending_error_retained=1;
    if(command_set_region_abort(r)<0)r->pending_error_retained=1;
    r->active_node=0;r->secondary_body_bytes=r->body_bytes-r->secondary_body_at;
    r->secondary_payload_complete=0;r->error_payload_complete=0;return -1;
}

static int command_tuple13(FridayPublisherRootCommandReceipt *r,PyObject *p) {
    if(!p)return 0;
    if(!PyTuple_CheckExact(p)||PyTuple_GET_SIZE(p)!=13)return -1;
    PyObject *schema=PyTuple_GET_ITEM(p,0);
    if(!PyUnicode_CheckExact(schema)||
       PyUnicode_CompareWithASCIIString(schema,FRIDAY_PUBLISHER_CALLER_PACKET_SCHEMA)!=0||
       !PyDict_CheckExact(PyTuple_GET_ITEM(p,1))||
       !PyDict_CheckExact(PyTuple_GET_ITEM(p,2))||
       !PyBytes_CheckExact(PyTuple_GET_ITEM(p,3))||
       !PyDict_CheckExact(PyTuple_GET_ITEM(p,4))||
       !PyDict_CheckExact(PyTuple_GET_ITEM(p,8))||
       !PyDict_CheckExact(PyTuple_GET_ITEM(p,9))||
       !PyDict_CheckExact(PyTuple_GET_ITEM(p,10)))return -1;
    /* Existing typed reader proves the actual current Run-root aliases, full banks,
     * original parts/cuts/offsets, final status and nested tuple relations.
     * It is NOT replaced by calling a bytes-only helper on all13 slots. */
    if(FridayPublisherCallerPacketBytes(p,1)<0||PyErr_Occurred())return -1;
    PyObject *cost=PyTuple_GET_ITEM(p,12);
    if(!PyTuple_CheckExact(cost)||PyTuple_GET_SIZE(cost)!=3)return -1;
    for(int i=0;i<3;i++)if(!PyLong_CheckExact(PyTuple_GET_ITEM(cost,i)))return -1;
    r->packet_typed_read=1;return 0;
}
FridayPublisherRootCommandReceipt *FridayPublisherRootCommandReceiptStorage(
    const FridayPublisherRootTerminal *t) {
    return t==&root_storage.terminal&&root_storage.attempted?
        &root_storage.command_receipt:NULL;
}

/* No arbitrary error codec or Python factory is used in FD completion.
 * Every uncertain native return has a prospective own field BEFORE the next
 * operation. Known producer close is terminal for that descriptor generation.
 */
static void command_keeper_close(const FridayPublisherRootCloseFact *body,int fd,
    FridayPublisherRootCloseFact *fact,int *attempted,int *closed,int *rc,int *err) {
    root_keeper_close(body,fd,fact);
    *attempted=fact->attempted;*closed=fact->closed;*rc=fact->rc;*err=fact->original_errno;
}
static int command_close_records(FridayPublisherRootStorage *s,
    FridayPublisherRootCommandReceipt *r) {
    FridayPublisherCallerResult *c=&s->terminal.result;
    if(c->full_packet) {
        PyObject *closes=PyTuple_GET_ITEM(c->full_packet,7);
        for(Py_ssize_t i=0;i<PyTuple_GET_SIZE(closes);i++) {
            PyObject *row=PyTuple_GET_ITEM(closes,i);FridayPublisherRootCloseFact fact;
            if(!PyTuple_CheckExact(row)||PyTuple_GET_SIZE(row)!=5||
               FridayPublisherRootOwnedRowCloseFact(&s->pool,PyTuple_GET_ITEM(row,0),&fact)!=1||
               !fact.attempted||!fact.closed||fact.rc||fact.original_errno)return -1;
            long scalars[4];
            for(int j=0;j<4;j++) {
                PyObject *x=PyTuple_GET_ITEM(row,j+1);
                if(!PyLong_CheckExact(x))return -1;
                scalars[j]=PyLong_AsLong(x);if(PyErr_Occurred())return -1;
            }
            if(scalars[0]!=1||scalars[1]!=fact.rc||
               scalars[2]!=fact.original_errno||scalars[3]!=1)return -1;
            /* Full producer fact is retained, not a converted mutable flag.
             * The tuple's exact original body is independently in value graph. */
            if(command_append(r,&fact,sizeof(fact))<0)return -1;
            r->close_records_read++;r->closed_by_actual_producer++;
        }
    } else if(c->failure.received) {
        NativeCloseRecord *cursor=NULL;uint64_t seen=0;
        FridayPublisherFailureCloseView v;int rc;
        while((rc=FridayPublisherFailureCloseNext(&c->failure,&cursor,&seen,&v))==1) {
            FridayPublisherRootCloseFact fact;
            int original[4]={v.actual_called,v.syscall_rc,v.syscall_errno,v.publication_confirmed};
            if(command_append(r,original,sizeof(original))<0)return -1;
            /* An uncalled native record is retained; it cannot certify close.
             * Native authoritative owner state may still be safely completed. */
            if(v.actual_called) {
                if(FridayPublisherRootOwnedRowCloseFact(&s->pool,v.actual_row,&fact)!=1||
                   !fact.attempted||fact.rc!=v.syscall_rc||
                   fact.original_errno!=v.syscall_errno)return -1;
                if(command_append(r,&fact,sizeof(fact))<0)return -1;
                if(fact.closed)r->closed_by_actual_producer++;
            }
            r->close_records_read++;
        }
        if(rc<0)return -1;
    }
    return 0;
}
static void account_generation(FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootCloseFact *body,FridayPublisherRootCloseFact *keeper,
    int *live_body,int *live_keeper) {
    if(body&&body->birth_valid)(void)close_generation(body,keeper,live_body,live_keeper);
    if(body&&body->closed)r->closed_by_actual_producer++;
    if(!body||!body->closed||!keeper||!keeper->closed)r->uncertain_FDs++;
}
static void mirror_held_alias(RootHeldFile *h,FridayPublisherRootCloseFact *body,
    FridayPublisherRootCloseFact *keeper,int keeper_fd) {
    if(!h)return;
    h->body_close=*body;h->keeper_close=*keeper;h->keeper=keeper_fd;
    h->close_attempted=body->attempted;h->close_rc=body->rc;
    h->close_errno=body->original_errno;h->closed=body->closed;
}
static int command_complete_FDs(FridayPublisherRootStorage *s,
    FridayPublisherRootCommandReceipt *r) {
    uint64_t current=command_clock(r);
    if(!current||!s->pool.deadline_ns||current>s->pool.deadline_ns)return -1;
    /* Source data FDs end here. Cold observer support stays live through
     * later graph getters, destructors and the actual runtime finalizer. */
    r->utility_support_pending=s->utility_preowned&&!s->utility_result.sealed;
    /* Lookup/close uses private actual row/credit generation and same OFD.
     * No PyDict CLOSED/UNKNOWN flag is promoted into native acceptance. */
    if(s->final_open_attempted&&s->final_open_rc>=0) {
        FridayPublisherRootCloseFact f;
        if(s->final_owner_close.closed)r->closed_by_actual_producer++;
        else if(!s->bindings.final_fd_row||!s->bindings.final_fd_credit||
           FridayPublisherRootCloseOwnedRow(&s->pool,s->bindings.final_fd_row,
               s->bindings.final_fd_credit,s->final_fd,&f)<0)r->uncertain_FDs++;
        if(s->final_owner_close.closed)command_keeper_close(&s->final_owner_close,s->final_keeper,&s->final_keeper_close,
            &s->final_keeper_attempted,&s->final_keeper_closed,
            &s->final_keeper_rc,&s->final_keeper_errno);
        if(!s->final_owner_close.closed||!s->final_keeper_closed)r->uncertain_FDs++;
    }
    {
        uint64_t adopted=0;RootHeldFile *dir=NULL;
        for(uint64_t i=0;i<s->held_count;i++)
            if(s->held[i].alias_kind==3){adopted++;dir=&s->held[i];}
        if(adopted==1) {
            account_generation(r,&dir->body_close,&dir->keeper_close,&dir->live_body,&dir->live_keeper);
            dir->close_attempted=dir->body_close.attempted;dir->close_rc=dir->body_close.rc;
            dir->close_errno=dir->body_close.original_errno;dir->closed=dir->body_close.closed;
            s->output_owner_close=dir->body_close;s->output_keeper_close=dir->keeper_close;
            s->output_keeper=dir->keeper;s->output_keeper_valid=dir->keeper>=0;
            s->output_keeper_attempted=dir->keeper_close.attempted;
            s->output_keeper_closed=dir->keeper_close.closed;
            s->output_keeper_rc=dir->keeper_close.rc;
            s->output_keeper_errno=dir->keeper_close.original_errno;
        } else if(s->output_root_open_attempted&&s->output_root_open_rc>=0)
            r->uncertain_FDs++;
    }
    for(uint64_t i=0;i<s->prepared_count;i++) {
        FridayPublisherPreparedRow *o=&s->prepared[i];
        if(!o->opened)continue;
        if(o->body_close.closed)r->closed_by_actual_producer++;
        else {
            FridayPublisherRootCloseFact fact;
            if(FridayPublisherRootCloseOwnedRow(&s->pool,o->row,o->credit,o->fd,&fact)<0)
                r->uncertain_FDs++;
        }
        if(o->body_close.closed)command_keeper_close(&o->body_close,o->keeper,&o->keeper_close,
            &o->keeper_attempted,&o->keeper_closed,&o->keeper_rc,&o->keeper_errno);
        if(!o->body_close.closed||!o->keeper_closed)r->uncertain_FDs++;
        if((i&63)==0) {
            current=command_clock(r);if(!current||current>s->pool.deadline_ns)return -1;
        }
    }
    /* Held, bootstrap and image owners use the same generation/OFD close.
     * Aliases are not closed again. An uncertain generation is not retried
     * and is not retired from its integer. No numeric PID is killed here. */
    for(unsigned i=0;i<s->bootstrap.fd_count;i++) {
        RootBootstrapFD *f=&s->bootstrap.fd[i];
        if(!f->acquired)continue;
        int body_was=f->body_close.closed,keeper_was=f->keeper_close.closed;
        account_generation(r,&f->body_close,&f->keeper_close,&f->live_body,&f->live_keeper);
        f->close_attempted=f->body_close.attempted;f->close_rc=f->body_close.rc;
        f->close_errno=f->body_close.original_errno;f->closed=f->body_close.closed;
        if(!body_was&&f->body_close.closed) {
            if(s->bootstrap.active_fds)s->bootstrap.active_fds--;
            boot_refund_row(&s->bootstrap,f,1);
        }
        if(!keeper_was&&f->keeper_close.closed)boot_refund_row(&s->bootstrap,f,1);
        if(f->held_index!=UINT64_MAX&&f->held_index<s->held_count)
            mirror_held_alias(&s->held[f->held_index],&f->body_close,&f->keeper_close,f->keeper);
    }
    for(uint64_t i=0;i<s->image_inventory.fd_count;i++) {
        RootImageFD *f=&s->image_inventory.fds[i];
        if(!f->acquired)continue;
        account_generation(r,&f->body_close,&f->keeper_close,&f->live_body,&f->live_keeper);
        f->close_attempted=f->body_close.attempted;f->close_rc=f->body_close.rc;
        f->close_errno=f->body_close.original_errno;f->closed=f->body_close.closed;
        for(uint64_t j=0;j<s->held_count;j++) {
            RootHeldFile *h=&s->held[j];
            if(h->close_alias&&h->alias_kind==2&&h->alias_generation==f->generation)
                mirror_held_alias(h,&f->body_close,&f->keeper_close,f->keeper);
        }
    }
    for(uint64_t i=0;i<s->held_count;i++) {
        RootHeldFile *h=&s->held[i];
        if(h->close_alias||!h->acquired)continue;
        account_generation(r,&h->body_close,&h->keeper_close,&h->live_body,&h->live_keeper);
        h->close_attempted=h->body_close.attempted;h->close_rc=h->body_close.rc;
        h->close_errno=h->body_close.original_errno;h->closed=h->body_close.closed;
    }
    r->owned_FDs_complete=r->uncertain_FDs==0;
    return 0;
}
static int command_native_owners(FridayPublisherRootStorage *s,
    FridayPublisherRootCommandReceipt *r) {
    r->native_owner_body_at=r->body_bytes;
    /* A historical pre-tail copy, not permission to close the live observer. */
    /* Pointer-free complete known actual FD facts, not number-only custody.
     * Selected compiler's full stat/operation/frame/rusage ABI is NOT_RUN. */
#define RAW(value) do { if(command_append(r,&(value),sizeof(value))<0)return -1; } while(0)
    const char *phases[7]={s->phase,s->terminal.phase,s->terminal.initial_error_phase,
        s->pool.fault,s->bootstrap.first_phase,s->image_inventory.first_phase,
        s->terminal.result.failure.prefix.phase};
    for(int at=0;at<7;at++) {
        int present=phases[at]!=NULL;RAW(present);
        uint64_t width=present?(uint64_t)strnlen(phases[at],INPUT_CAP+1):0;
        if(width>INPUT_CAP)return -1;RAW(width);
        if(width&&command_append(r,phases[at],width)<0)return -1;
    }
    const char *after_phase=s->terminal.result.failure.after_document.phase;
    int after_present=after_phase!=NULL;RAW(after_present);
    uint64_t after_width=after_present?(uint64_t)strnlen(after_phase,INPUT_CAP+1):0;
    if(after_width>INPUT_CAP)return -1;RAW(after_width);
    if(after_width&&command_append(r,after_phase,after_width)<0)return -1;
    RAW(s->terminal.readback_kind);RAW(s->terminal.entry_refusal);
    RAW(s->terminal.received);RAW(s->terminal.full_readback);
    RAW(s->terminal.full_banks_read);RAW(s->terminal.full_bank_bytes);
    RAW(s->terminal.partial_parts_read);RAW(s->terminal.owned_failure_roots);
    RAW(s->terminal.owned_entry_roots);RAW(s->terminal.initial_error_capture_attempted);
    RAW(s->terminal.result.state);RAW(s->terminal.result.entry_fault);
    RAW(s->terminal.result.invoke_refusal);
    RAW(s->terminal.result.failure_handback_attempted);RAW(s->terminal.result.failure_handback_confirmed);
    RAW(s->terminal.result.run_references_transferred);RAW(s->terminal.result.full_packet_readback);
    RAW(s->final_owner_close);RAW(s->output_owner_close);
    RAW(s->final_keeper_close);RAW(s->output_keeper_close);
    RAW(s->final_keeper);RAW(s->final_keeper_valid);RAW(s->final_keeper_attempted);
    RAW(s->final_keeper_closed);RAW(s->final_keeper_rc);RAW(s->final_keeper_errno);
    RAW(s->output_keeper);RAW(s->output_keeper_valid);RAW(s->output_keeper_attempted);
    RAW(s->output_keeper_closed);RAW(s->output_keeper_rc);RAW(s->output_keeper_errno);
    RAW(s->final_open_attempted);RAW(s->final_open_rc);RAW(s->final_open_errno);
    RAW(s->final_stat_attempted);RAW(s->final_stat_rc);RAW(s->final_stat_errno);
    RAW(s->output_root_open_attempted);RAW(s->output_root_open_rc);RAW(s->output_root_open_errno);
    RAW(s->output_root_stat_attempted);RAW(s->output_root_stat_rc);RAW(s->output_root_stat_errno);
    RAW(s->held_count);
    for(uint64_t i=0;i<s->held_count;i++) {
        RootHeldFile *h=&s->held[i];
        RAW(h->fd);RAW(h->closed);RAW(h->close_attempted);RAW(h->close_rc);
        RAW(h->close_errno);RAW(h->birth);
        RAW(h->generation);RAW(h->read_used);RAW(h->alias_generation);
        RAW(h->keeper);RAW(h->acquired);RAW(h->close_alias);RAW(h->alias_kind);
        RAW(h->keeper_open_attempted);RAW(h->keeper_open_rc);RAW(h->keeper_open_errno);
        RAW(h->birth_stat_attempted);RAW(h->birth_stat_rc);RAW(h->birth_stat_errno);
        RAW(h->live_body);RAW(h->live_keeper);RAW(h->read_completed);RAW(h->read_error);
        RAW(h->body_close);RAW(h->keeper_close);
    }
    RAW(s->prepared_count);
    for(uint64_t i=0;i<s->prepared_count;i++) {
        FridayPublisherPreparedRow *o=&s->prepared[i];
        RAW(o->token);RAW(o->generation);RAW(o->fd);RAW(o->keeper);
        RAW(o->keeper_open_attempted);RAW(o->keeper_open_rc);RAW(o->keeper_open_errno);
        RAW(o->birth_stat_attempted);RAW(o->birth_stat_rc);RAW(o->birth_stat_errno);
        RAW(o->opened);RAW(o->keeper_attempted);RAW(o->keeper_closed);
        RAW(o->keeper_rc);RAW(o->keeper_errno);RAW(o->body_close);RAW(o->keeper_close);RAW(o->birth);
    }
    RootBootstrapState *b=&s->bootstrap;
    RAW(b->fd_count);
    for(unsigned i=0;i<b->fd_count;i++)RAW(b->fd[i]);
    RAW(b->child);RAW(b->received);RAW(b->ack);RAW(b->initial_gate);
    RAW(b->parent_before);RAW(b->parent_after);RAW(b->usage);
    RAW(b->first_errno);RAW(b->first_errno_valid);RAW(b->failed);
    RAW(b->forked);RAW(b->wait_attempted);RAW(b->waited);RAW(b->status);RAW(b->wait_errno);
    RAW(b->signal_attempted);RAW(b->signal_rc);RAW(b->signal_errno);
    RAW(b->stdout_used);RAW(b->stderr_used);RAW(b->owner_used);RAW(b->ack_used);
    RAW(b->actual_native_reads);RAW(b->actual_native_writes);RAW(b->child_IO_known);
    RAW(b->child_actual_reads);RAW(b->child_actual_writes);RAW(b->child_transport_end_confirmed);
    /* Full immutable copied parent bank + actual before-publication end.
     * Never read an unresolved live child mapping or treat its pointer as
     * payload. Retention/unknown flags remain alongside these real bytes. */
    RAW(b->final_parent_copy);RAW(b->parent_end);
    RAW(b->map_attempted);RAW(b->map_errno);RAW(b->map_acquired);
    RAW(b->protect_attempted);RAW(b->protect_rc);RAW(b->protect_errno);
    RAW(b->unmap_attempted);RAW(b->unmap_rc);RAW(b->unmap_errno);
    RAW(b->bank_full_read);RAW(b->bank_stable);RAW(b->bank_read_bytes);
    RAW(b->native_failure_transport_complete);RAW(b->stock_exec_end_unknown);
    RAW(b->spawn_args);RAW(b->spawn_rc);RAW(b->spawn_entered);RAW(b->spawn_returned);
    RAW(b->spawn_pidfd);RAW(b->spawn_errno);RAW(b->spawn_pidfd_bound);
    RAW(b->reap_info);RAW(b->reap_rc);RAW(b->reap_errno);RAW(b->reap_returned);
    RAW(b->final_io_attempted);RAW(b->lifetime_phase);
    RAW(b->stock_exec_prefix_retained);RAW(b->stock_image_replacement_confirmed);
    RAW(b->kernel_process_lifetime_ended);RAW(b->public_stock_observables_consumed);
    RAW(b->public_stock_parent_legal_retirement_accounted);
    RAW(s->terminal.bootstrap_parent_native_read);RAW(s->terminal.bootstrap_parent_native_bytes_read);
    RAW(s->terminal.bootstrap_native_failure_transport_read);
    RAW(s->terminal.bootstrap_parent_mapping_retained);
    RootImageState *im=&s->image_inventory;
    RAW(im->attempted);RAW(im->complete);RAW(im->first_errno);RAW(im->fd_count);
    for(uint64_t i=0;i<im->fd_count;i++)RAW(im->fds[i]);
    RAW(im->root_before);RAW(im->root_after);RAW(im->seen);RAW(im->identities);
    RAW(s->utility_preowned);RAW(s->utility_accounted);
    for(int utility_at=0;utility_at<ROOT_UTILITY_COUNT;utility_at++)RAW(s->utility[utility_at]);
    RAW(s->pool.started_ns);RAW(s->pool.deadline_ns);RAW(s->pool.work_deadline_ns);
    RAW(s->pool.spent_read);RAW(s->pool.spent_output);RAW(s->pool.spent_hash);
    RAW(s->pool.native_allocation);RAW(s->pool.retained_allocation);
    RAW(s->pool.observed_read);RAW(s->pool.observed_output);RAW(s->pool.observed_ram);
    RAW(s->pool.observed_workers);RAW(s->pool.native_live_slots);RAW(s->pool.count);
    RAW(s->pool.pid);RAW(s->pool.initialized);RAW(s->pool.preowner_started);
    RAW(s->pool.source_detached);RAW(s->pool.refused);RAW(s->pool.observation_unknown);
    RAW(s->pool_totals); /* fixed index body, never a substitute for original rows */
    if(s->pool.count>FRIDAY_MASTER_HISTORY)return -1;
    for(uint64_t i=0;i<s->pool.count;i++)RAW(s->pool.rows[i]);
#undef RAW
    r->native_owner_body_bytes=r->body_bytes-r->native_owner_body_at;
    return 0;
}
static int command_registrations(FridayPublisherRootStorage *s,
    FridayPublisherRootCommandReceipt *r) {
    if(r->source_registrations_attempted)return -1;
    r->source_registrations_attempted=1;
    /* Called ONLY after full required native value representation exists.
     * Each destructive registration operation can fail once; immediately save
     * that first original error and STOP before ANY next fallible operation.
     * Real support objects remain strong in command owners while deregistered.
     */
    PyObject *meta=command_original(r,4,1),*loader=command_original(r,4,6);
    if(s->loader_install_attempted) {
        if(!meta||!PyList_CheckExact(meta)||!loader)return -1;
        Py_ssize_t found=-1;uint64_t matches=0;
        for(Py_ssize_t i=0;i<PyList_GET_SIZE(meta);i++)
            if(PyList_GET_ITEM(meta,i)==loader){found=i;matches++;}
        if(matches>1)return -1;
        if(matches==1) {
            if(PySequence_DelItem(meta,found)<0||
               command_error(r,&r->cleanup_error,"actual_loader_deregister")<0)return -1;
            r->registrations_removed++;
        }
        s->loader_installed=0;
    }
    PyObject *owned=command_original(r,4,4),*modules=PyImport_GetModuleDict();
    if(owned) {
        if(!modules||!PyDict_CheckExact(owned))return -1;
        Py_ssize_t at=0;PyObject *name,*value;
        while(PyDict_Next(owned,&at,&name,&value)) {
            if(!PyUnicode_CheckExact(name))return -1;
            PyObject *actual=PyDict_GetItemWithError(modules,name);
            if(command_error(r,&r->cleanup_error,"actual_registered_module_lookup")<0)return -1;
            if(actual&&actual!=value)return -1;
            if(actual) {
                if(PyDict_DelItem(modules,name)<0||
                   command_error(r,&r->cleanup_error,"actual_registered_module_delete")<0)return -1;
                r->registrations_removed++;
            }
        }
    }
    r->source_registrations_retired=1;
    /* Actual SOURCE registrations ended above. Standard import caches,
     * extension type support and builtin runtime copies belong to the SAME
     * cold-owned interpreter whose concrete finalizer is in ColdPerform.
     * This is a remaining runtime dependency, NOT "all registrations gone".
     * No caller boolean or a different outside process supplies ownership. */
    r->cold_runtime_bound=command_has_owned_runtime(s);
    r->runtime_support_pending=r->cold_runtime_bound;
    r->registrations_remaining=1;r->registrations_retired=0;
    if(!r->cold_runtime_bound)return -1;
    return 0;
}
static uint64_t command_error_reference_count(const FridayPublisherRootCommandReceipt *r) {
    return (r->incoming_error.type!=NULL)+(r->incoming_error.value!=NULL)+
        (r->incoming_error.tb!=NULL)+(r->cleanup_error.type!=NULL)+
        (r->cleanup_error.value!=NULL)+(r->cleanup_error.tb!=NULL);
}
/* Native-only ownership relation, usable on both sides of runtime retirement.
 * before: actual registry view == actual MOVED owner == actual graph original.
 * after: those three Python references are null, while the same owner->node
 * identity and full previously read native value/alias body remain. */
static int command_own_link(const FridayPublisherRootCommandReceipt *r,
    uint64_t at,PyObject *view,int ended,uint64_t *next) {
    if(!at)return view?-1:0;
    if(at!=*next+1||at>r->owner_count)return -1;
    const FridayPublisherRootCommandOwner *o=&r->owners[at-1];
    if(o->group!=21||!o->node||o->node>r->node_count)return -1;
    const FridayPublisherRootValueNode *n=&r->nodes[o->node-1];
    if(ended) {if(view||o->owned||n->original)return -1;}
    else if(!view||o->owned!=view||n->original!=view)return -1;
    if(o->required&&(!n->required||!n->data_read||n->kind==COMMAND_VALUE_UNSUPPORTED))return -1;
    (*next)++;return 0;
}
static int command_own_globals(const FridayPublisherOwnValues *v,
    const FridayPublisherRootCommandReceipt *r,int ended,uint64_t *next) {
    if(v->count>FRIDAY_ROOT_COMMAND_NODES||v->early_module||v->token_missing||
       v->mapping_module||v->mapping_factory||v->mapping_access||
       v->hash_module||v->hash_sha256||v->hash_new||
       v->owner_first>r->owner_count||v->owner_count>r->owner_count-v->owner_first)return -1;
    *next=v->owner_first;
    const uint64_t ids[]={v->early_owner_at,v->missing_owner_at,
        v->stock_owner_at[0],v->stock_owner_at[1],v->stock_owner_at[2],
        v->stock_owner_at[3],v->stock_owner_at[4],v->stock_owner_at[5]};
    for(unsigned j=0;j<8;j++) {
        uint64_t id=ids[j];if(!id)continue;
        if(id>r->owner_count||r->owners[id-1].index!=j||
           r->owners[id-1].required!=(uint64_t)(j==1||j==4))return -1;
        PyObject *view=ended?NULL:r->owners[id-1].owned;
        if(command_own_link(r,id,view,ended,next)<0)return -1;
        if(j==1&&r->nodes[r->owners[id-1].node-1].kind!=COMMAND_VALUE_TOKEN_MISSING)return -1;
        if(j==4&&r->nodes[r->owners[id-1].node-1].kind!=COMMAND_VALUE_LONG)return -1;
    }
    return 0;
}
static int command_own_row_links(const FridayPublisherOwnValue *p,uint64_t row,
    const FridayPublisherRootCommandReceipt *r,int ended,uint64_t *next) {
    if(p->kind<OWN_VAR||p->kind>OWN_ERROR_CELL||p->serial!=row+1||p->pid!=r->pid||
       p->borrowed_mask&~0x7fffU)return -1;
    for(unsigned j=0;j<15;j++) {
        PyObject *view=j<12?p->refs[j]:j==12?p->error_type:j==13?p->error_value:p->error_tb;
        uint64_t id=p->owner_at[j];
        if(((p->borrowed_mask>>j)&1U)!=(id!=0))return -1;
        if(command_own_link(r,id,view,ended,next)<0)return -1;
        if(id) {
            int required=own_row_required(p,j);
            if(r->owners[id-1].required!=(uint64_t)required)return -1;
        }
    }
    return 0;
}
static int final_caller_clock(FridayPublisherRootFinalHandoff *,uint64_t *);
static int command_class_scan(uint64_t steps,FridayPublisherRootFinalHandoff *end) {
    if(steps>UINT64_MAX/64)return -1;
    if(!end)return secondary_debit_scan(steps,0);
    RootFinalCallerBinding *b=&final_caller_binding;uint64_t bytes=steps*64;
    if(!b->credit_reserved||b->read_used>b->read_credit||bytes>b->read_credit-b->read_used)return -1;
    b->read_used+=bytes;return 0; /* original constructor credit, no late grant */
}
static const FridayPublisherRootValueNode *command_class_node(
    const FridayPublisherRootCommandReceipt *r,const FridayPublisherOwnValue *p,unsigned j) {
    if(j>=15||!p->owner_at[j]||p->owner_at[j]>r->owner_count)return NULL;
    const FridayPublisherRootCommandOwner *o=&r->owners[p->owner_at[j]-1];
    if(o->group!=21||!o->node||o->node>r->node_count||
       o->required!=(uint64_t)own_row_required(p,j))return NULL;
    return &r->nodes[o->node-1];
}
static uint64_t command_class_edge(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherRootValueNode *v,uint64_t j) {
    if(!v||j>=v->edges||v->edge_at>r->edge_count||v->edges>r->edge_count-v->edge_at)return 0;
    uint64_t id=r->edges[v->edge_at+j];return id&&id<=r->node_count?id:0;
}
static int command_class_number(const FridayPublisherRootCommandReceipt *r,
    uint64_t id,uint64_t expected) {
    if(!id||id>r->node_count)return -1;
    const FridayPublisherRootValueNode *v=&r->nodes[id-1];
    if(v->kind!=COMMAND_VALUE_LONG||!v->body_bytes||v->body_bytes>8||
       v->body_at>r->body_bytes||v->body_bytes>r->body_bytes-v->body_at)return -1;
    const unsigned char *b=r->body+v->body_at;uint64_t actual=0;
    if(b[v->body_bytes-1]&128)return -1;
    for(uint64_t i=0;i<v->body_bytes;i++)actual|=(uint64_t)b[i]<<(8*i);
    return actual==expected?0:-1; /* full small positive serial, not pointer hash */
}
static int command_class_retirement_blocked(FridayPublisherRootFinalHandoff *end) {
    const FridayPublisherOwnValues *v=&root_storage.own_values;
    if(v->count>FRIDAY_ROOT_COMMAND_NODES||command_class_scan(v->count,end)<0)return -1;
    /* This native scalar barrier is checked BEFORE registrations/borrower
     * invalidation/owner DECREF on both ordinary and codec-error paths. */
    for(uint64_t i=0;i<v->count;i++) {
        if((i&1023)==0) {
            if(end){uint64_t now;if(final_caller_clock(end,&now)<0)return -1;}
            else if(!command_clock(&root_storage.command_receipt))return -1;
        }
        const FridayPublisherOwnValue *p=&v->rows[i];
        if(p->kind==OWN_CLASS_SCOPE&&((p->flags&CLASS_UNCERTAIN)||
           ((p->flags&CLASS_MODULE_SET_OK)&&!(p->flags&CLASS_RESTORE_OK))))return 1;
    }
    return 0;
}
/* Retained numeric readers below work both with live owners and after all
 * Python pointers have been retired. They never call Py_TYPE at the end. */
static int command_cell_text(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherRootValueNode *v,const char *expected) {
    if(!v||!expected)return 0;
    size_t n=strnlen(expected,129);
    if(n>128||v->kind!=COMMAND_VALUE_UNICODE||v->body_bytes!=n*sizeof(Py_UCS4)||
       v->body_at>r->body_bytes||v->body_bytes>r->body_bytes-v->body_at)return 0;
    for(size_t j=0;j<n;j++) {
        Py_UCS4 c;memcpy(&c,r->body+v->body_at+j*sizeof(c),sizeof(c));
        if(c!=(unsigned char)expected[j])return 0;
    }
    return 1;
}
static int command_cell_integer(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherRootValueNode *v,int negative,uint64_t value) {
    if(!v||v->kind!=COMMAND_VALUE_LONG||!v->body_bytes||v->body_bytes>9||
       v->body_at>r->body_bytes||v->body_bytes>r->body_bytes-v->body_at)return -1;
    const unsigned char *b=r->body+v->body_at;
    if(((b[v->body_bytes-1]&128)!=0)!=(negative!=0))return -1;
    uint64_t actual=0;unsigned char extension=negative?255:0;
    for(unsigned j=0;j<8;j++)actual|=(uint64_t)(j<v->body_bytes?b[j]:extension)<<(j*8);
    if(v->body_bytes==9&&b[8]!=extension)return -1;
    return actual==value?0:-1;
}
static int command_failure_types_consume(const FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootFinalHandoff *end) {
    const FridayPublisherCallerResult *c=&root_storage.terminal.result;
    if(command_class_scan(128,end)<0||r->failure_type_provenance!=!!c->failure.received)return -1;
    if(c->failure.received&&(!c->failure_handback_confirmed||c->failure.owner_pid!=r->pid))return -1;
    const FridayPublisherRootValueNode *n[FRIDAY_PUBLISHER_RUN_ROOTS]={0};
    for(unsigned i=0;i<FRIDAY_PUBLISHER_RUN_ROOTS;i++) {
        uint64_t at=r->failure_root_owner_at[i];
        if(c->failure.roots[i])return -1; /* all actual original slots MOVED */
        if(!at)continue;
        if(!r->failure_type_provenance||at>r->owner_count)return -1;
        const FridayPublisherRootCommandOwner *o=&r->owners[at-1];
        int data=!((i==17&&r->failure_type_kind[0]==1)||(i==22&&r->failure_type_kind[1]==1));
        if(o->group!=18||o->index!=i||o->required!=(uint64_t)data||
           !o->node||o->node>r->node_count||(end?o->owned!=NULL:o->owned==NULL))return -1;
        n[i]=&r->nodes[o->node-1];
        if(end?n[i]->original!=NULL:n[i]->original!=o->owned)return -1;
    }
    for(unsigned j=0;j<2;j++) {
        unsigned at=j?22:17;uint64_t kind=r->failure_type_kind[j];
        if(kind>2)return -1;
        if(!kind) {if(n[at]||n[at+1]||n[at+2])return -1;continue;}
        if(!r->failure_type_provenance||!n[at]||!n[at+1]||
           n[at+1]->kind!=COMMAND_VALUE_ERROR||
           command_class_edge(r,n[at+1],0)!=n[at]->id||
           (n[at+2]&&n[at+2]->kind!=COMMAND_VALUE_TRACEBACK)||
           (kind==1?(n[at]->kind!=COMMAND_VALUE_SUPPORT||!n[at]->support_verified):
             (n[at]->kind!=COMMAND_VALUE_CLASS||!n[at]->required||!n[at]->data_read)))return -1;
        if(!end) {
            if(!PyExceptionInstance_Check(n[at+1]->original)||
               (PyObject *)Py_TYPE(n[at+1]->original)!=n[at]->original||
               kind!=(uint64_t)(command_builtin_exception(Py_TYPE(n[at+1]->original))?1:2))return -1;
        }
    }
    return 0;
}
static int command_error_cell_consume(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherOwnValue *p,uint64_t row,FridayPublisherRootFinalHandoff *end) {
    if(command_class_scan(528,end)<0||p->serial!=row+1||p->pid!=r->pid||
       p->attempts!=1||p->confirmed>1||p->flags>2||p->width<1||p->width>2||
       p->error_saved<0||p->error_saved>1||p->cell_previous>=p->serial||
       p->cell_outcome!=(uint64_t)(p->confirmed?FRIDAY_CELL_CUT_CONFIRMED:FRIDAY_CELL_CUT_FAILED))return -1;
    const FridayPublisherRootValueNode *n[15]={0};
    for(unsigned j=0;j<15;j++) {
        n[j]=command_class_node(r,p,j);if(p->owner_at[j]&&!n[j])return -1;
        if(n[j]&&(end?(n[j]->original||r->owners[p->owner_at[j]-1].owned):!n[j]->original))return -1;
    }
    if(!n[1]||!n[2]||!n[3]||!n[5]||!n[6]||n[5]->kind!=COMMAND_VALUE_NATIVE_CONTEXT||
       (n[3]->kind!=COMMAND_VALUE_NONE&&n[3]->kind!=COMMAND_VALUE_TRACEBACK))return -1;
    if(!p->flags) {
        if(n[1]->kind!=COMMAND_VALUE_NONE||n[2]->kind!=COMMAND_VALUE_NONE||
           n[3]->kind!=COMMAND_VALUE_NONE)return -1;
    } else if(n[1]->kind!=COMMAND_VALUE_ERROR||command_class_edge(r,n[1],0)!=n[2]->id||
        (p->flags==1?(n[2]->kind!=COMMAND_VALUE_SUPPORT||!n[2]->support_verified):
          (n[2]->kind!=COMMAND_VALUE_CLASS||!n[2]->required||!n[2]->data_read)))return -1;
    if(p->error_saved) {
        if(!n[12]||!n[13]||n[13]->kind!=COMMAND_VALUE_ERROR||
           (n[14]&&n[14]->kind!=COMMAND_VALUE_TRACEBACK))return -1;
    } else if(n[12]||n[13]||n[14])return -1;
    if(!p->confirmed) {
        /* Absent sole factory return is a genuine failure prefix. A retained
         * but unverified return cannot be certified as a confirmed cell. */
        if(n[4]||n[0]||!p->error_saved)return -1;
        for(unsigned j=7;j<12;j++)if(n[j])return -1;
        return 0;
    }
    if(!n[4]||n[4]->kind!=COMMAND_VALUE_DICT||n[4]->edges!=20||
       n[4]->error_record_serial!=p->serial||
       !command_cell_text(r,n[0],p->cell_input.phase?p->cell_input.phase:"NOT_ATTEMPTED"))return -1;
    unsigned seen=0;
    for(unsigned j=0;j<10;j++) {
        uint64_t key=command_class_edge(r,n[4],2*j),value=command_class_edge(r,n[4],2*j+1);
        if(!key||!value||!r->edge_roles[n[4]->edge_at+2*j])return -1;
        unsigned k=0;for(;k<10;k++)if(command_cell_text(r,&r->nodes[key-1],error_cell_names[k]))break;
        if(k==10||(seen&(1U<<k))||!n[error_cell_refs[k]]||
           value!=n[error_cell_refs[k]]->id||r->edge_roles[n[4]->edge_at+2*j+1]!=(unsigned char)
             !(k==2&&p->flags==1))return -1;
        seen|=1U<<k;
    }
    int scalars[]={p->cell_input.saved,p->cell_input.syscall_attempted,
        p->cell_input.syscall_rc,p->cell_input.syscall_errno};
    for(unsigned j=0;j<4;j++)if(command_cell_integer(r,n[j+7],scalars[j]<0,(uint64_t)(int64_t)scalars[j])<0)return -1;
    if(seen!=1023||command_cell_integer(r,n[11],0,p->cell_input.actual_written)<0||
       (!end&&!own_error_cell_valid(p,n[4]->original)))return -1;
    return 0;
}
static int command_error_record_rows_consume(const FridayPublisherRootCommandReceipt *r,
    FridayPublisherRootFinalHandoff *end) {
    if(command_failure_types_consume(r,end)<0)return -1;
    FridayPublisherOwnValues *v=&root_storage.own_values;
    uint64_t last[2]={0,0};
    if(v->count>FRIDAY_ROOT_COMMAND_NODES||command_class_scan(v->count,end)<0)return -1;
    for(uint64_t i=0;i<v->count;i++) {
        if((i&1023)==0) {
            if(end){uint64_t now;if(final_caller_clock(end,&now)<0)return -1;}
            else if(!command_clock((FridayPublisherRootCommandReceipt *)r))return -1;
        }
        const FridayPublisherOwnValue *p=&v->rows[i];
        if(p->kind==OWN_ERROR_CELL) {
            if(command_error_cell_consume(r,p,i,end)<0)return -1;
            unsigned at=(unsigned)p->width-1;
            if(p->cell_previous!=last[at])return -1;
            if(last[at]) {
                const FridayPublisherOwnValue *prior=&v->rows[last[at]-1];
                const FridayPublisherRootValueNode *a=command_class_node(r,prior,5);
                const FridayPublisherRootValueNode *b=command_class_node(r,p,5);
                if(prior->kind!=OWN_ERROR_CELL||prior->width!=p->width||
                   prior->cell_outcome!=FRIDAY_CELL_CUT_CONFIRMED||prior->confirmed!=1||
                   prior->error_saved||!a||!b||a->id!=b->id)return -1;
            }
            last[at]=p->serial;
            continue;
        }
        if(p->kind!=OWN_ERROR_RECORD)continue;
        if(command_class_scan(256,end)<0||p->serial!=i+1||p->pid!=r->pid||
           p->attempts!=1||p->confirmed>1||(p->flags!=1&&p->flags!=2)||
           p->error_saved<0||p->error_saved>1)return -1;
        const FridayPublisherRootValueNode *n[15];
        for(unsigned j=0;j<15;j++) {
            n[j]=command_class_node(r,p,j);if(p->owner_at[j]&&!n[j])return -1;
            if(j>=6&&j<12&&n[j])return -1;
            if(n[j]&&(end?(n[j]->original||r->owners[p->owner_at[j]-1].owned):!n[j]->original))return -1;
        }
        if(!n[0]||!n[1]||!n[2]||!n[3]||!n[5]||
           n[0]->kind!=COMMAND_VALUE_UNICODE||n[1]->kind!=COMMAND_VALUE_ERROR||
           (n[3]->kind!=COMMAND_VALUE_NONE&&n[3]->kind!=COMMAND_VALUE_TRACEBACK)||
           n[5]->kind!=COMMAND_VALUE_NATIVE_CONTEXT||
           command_class_edge(r,n[1],0)!=n[2]->id||
           (p->flags==1?(n[2]->kind!=COMMAND_VALUE_SUPPORT||!n[2]->support_verified):
             (n[2]->kind!=COMMAND_VALUE_CLASS||!n[2]->required||!n[2]->data_read)))return -1;
        if(p->error_saved) {
            if(!n[12]||!n[13]||n[13]->kind!=COMMAND_VALUE_ERROR||
               (n[14]&&n[14]->kind!=COMMAND_VALUE_TRACEBACK))return -1;
        } else if(n[12]||n[13]||n[14])return -1;
        if(!p->confirmed) {
            /* Failed sole tuple factory: originals and its full first error
             * survive, but no absent record is fabricated or retried. */
            if(n[4]||!p->error_saved)return -1;
            continue;
        }
        if(!n[4]||n[4]->kind!=COMMAND_VALUE_TUPLE||n[4]->edges!=4||
           n[4]->error_record_serial!=p->serial)return -1;
        for(unsigned j=0;j<4;j++)if(command_class_edge(r,n[4],j)!=n[j]->id||
            r->edge_roles[n[4]->edge_at+j]!=(unsigned char)(j!=2||p->flags!=1))return -1;
        if(!end&&!own_error_record_valid(p,n[4]->original))return -1;
        /* Ended route above reads ONLY retained numeric producer/owner/node
         * links. No Py_TYPE, registry borrower, getter or Python operation. */
    }
    /* Same fixed two guards, independent of row count; after loss only the
     * original retained numeric capsule/owner/node and outcome chain are read.
     * No final Py_TYPE, capsule API, original Run dereference or new grant. */
    if(command_class_scan(32,end)<0)return -1;
    for(unsigned at=0;at<2;at++) {
        if(last[at]!=v->cell_last[at])return -1;
        if(!last[at]) {
            if(v->cell_state[at]||v->cell_context_node[at])return -1;
            continue;
        }
        const FridayPublisherOwnValue *p=&v->rows[last[at]-1];
        const FridayPublisherRootValueNode *n=command_class_node(r,p,5);
        if(!n||n->kind!=COMMAND_VALUE_NATIVE_CONTEXT||p->cell_outcome!=v->cell_state[at])return -1;
        if(end) {
            if(!v->borrowers_end_confirmed||!r->own_registry_borrowers_end_confirmed||
               v->cell_context_node[at]!=n->id||n->original)return -1;
        } else {
            const FridayPublisherOwnedRun *actual=NULL;
            if(FridayPublisherCallerContextFull(n->original,&actual)!=1||!actual)return -1;
            const FridayPublisherErrorCell *cell=at?&actual->after_document_error:&actual->prefix_error;
            if(cell->cut_serial!=last[at]||cell->cut_state!=v->cell_state[at]||
               (v->cell_context_node[at]&&v->cell_context_node[at]!=n->id))return -1;
            v->cell_context_node[at]=n->id; /* exact original live rejoin before loss */
        }
    }
    return 0;
}
static int command_class_rows_consume(const FridayPublisherRootStorage *s,
    const FridayPublisherRootCommandReceipt *r,FridayPublisherRootFinalHandoff *end) {
    const FridayPublisherOwnValues *v=&s->own_values;
    if(v->count>FRIDAY_ROOT_COMMAND_NODES||command_class_scan(v->count, end)<0)return -1;
    for(uint64_t i=0;i<v->count;i++) {
        if(end){uint64_t now;if(final_caller_clock(end,&now)<0)return -1;}
        else if(!command_clock((FridayPublisherRootCommandReceipt *)r))return -1;
        const FridayPublisherOwnValue *p=&v->rows[i];
        if(p->kind!=OWN_CLASS&&p->kind!=OWN_CLASS_SCOPE&&p->kind!=OWN_CLASS_RESTORE)continue;
        /* Pay all fixed row/alias/type joins BEFORE owner and node reads. */
        if(command_class_scan(256,end)<0||p->serial!=i+1||p->pid!=r->pid||
           p->error_saved<0||p->error_saved>1)return -1;
        const FridayPublisherRootValueNode *n[15];
        for(unsigned j=0;j<15;j++) {
            n[j]=command_class_node(r,p,j);
            if(p->owner_at[j]&&!n[j])return -1;
        }
        if(p->error_saved) {
            if(!n[12]||!n[13]||n[13]->kind!=COMMAND_VALUE_ERROR||
               (n[14]&&n[14]->kind!=COMMAND_VALUE_TRACEBACK))return -1;
        } else if(n[12]||n[13]||n[14])return -1;
        if(p->kind==OWN_CLASS) {
            if(!p->attempts) {
                if(p->confirmed||p->flags||n[0])return -1;
                continue; /* pre-effect partial row never claims class DATA */
            }
            if(p->attempts!=1||p->flags<CLASS_RETURN_LOCAL||p->flags>CLASS_RETURN_NONCLASS||
               p->confirmed!=(p->flags!=CLASS_CALL_ERROR)||
               (p->flags==CLASS_CALL_ERROR?!p->error_saved||n[0]:!n[0]||p->error_saved))return -1;
            for(unsigned j=1;j<12;j++)if(!n[j])return -1;
            if(n[2]->kind!=COMMAND_VALUE_UNICODE||n[3]->kind!=COMMAND_VALUE_CODE||
               n[4]->kind!=COMMAND_VALUE_BYTES||n[5]->kind!=COMMAND_VALUE_CODE||
               n[7]->kind!=COMMAND_VALUE_TUPLE||n[7]->edges<2||
               (n[8]->kind!=COMMAND_VALUE_DICT&&n[8]->kind!=COMMAND_VALUE_NONE)||
               n[11]->kind!=COMMAND_VALUE_TUPLE||n[11]->edges!=4||
               !p->width||p->width>v->count)return -1;
            const FridayPublisherOwnValue *scope=&v->rows[p->width-1];
            const FridayPublisherRootValueNode *ctx=command_class_node(r,scope,7);
            if(scope->kind!=OWN_CLASS_SCOPE||!ctx||ctx->id!=n[11]->id||
               command_class_edge(r,ctx,0)!=n[6]->id||command_class_edge(r,ctx,1)!=n[1]->id||
               command_class_number(r,command_class_edge(r,ctx,3),scope->serial)<0||
               command_class_edge(r,n[7],0)!=n[9]->id||command_class_edge(r,n[7],1)!=n[2]->id)return -1;
            /* Actual binding row ordinal is in the preowned wrapper context.
             * Scan is charged, not a late name/filename authority issuer. */
            if(command_class_scan(v->count,end)<0)return -1;
            int binding_found=0;
            for(uint64_t k=0;k<v->count;k++) {
                if(end){uint64_t now;if(final_caller_clock(end,&now)<0)return -1;}
                else if(!command_clock((FridayPublisherRootCommandReceipt *)r))return -1;
                const FridayPublisherOwnValue *b=&v->rows[k];if(b->kind!=OWN_BINDING)continue;
                const FridayPublisherRootValueNode *bm=command_class_node(r,b,0),
                    *raw=command_class_node(r,b,2),*code=command_class_node(r,b,3);
                if(bm&&raw&&code&&bm->id==n[1]->id&&raw->id==n[4]->id&&code->id==n[3]->id&&
                   command_class_number(r,command_class_edge(r,ctx,2),b->serial)==0) {binding_found=1;break;}
            }
            if(!binding_found)return -1;
            if(p->flags==CLASS_RETURN_LOCAL) {
                if(n[0]->kind!=COMMAND_VALUE_CLASS||n[9]->kind!=COMMAND_VALUE_FUNCTION||
                   command_class_edge(r,n[9],3)!=n[5]->id||
                   command_class_edge(r,n[9],10)!=n[10]->id)return -1;
            }
            if(command_class_scan(n[7]->edges+n[8]->edges,end)<0)return -1;
            for(uint64_t j=0;j<n[7]->edges;j++) {
                if((j&1023)==0) {
                    if(end){uint64_t now;if(final_caller_clock(end,&now)<0)return -1;}
                    else if(!command_clock((FridayPublisherRootCommandReceipt *)r))return -1;
                }
                uint64_t id=command_class_edge(r,n[7],j);if(!id)return -1;
                if(!r->edge_roles[n[7]->edge_at+j]&&
                   (j<2||(r->nodes[id-1].kind!=COMMAND_VALUE_SUPPORT&&
                          r->nodes[id-1].kind!=COMMAND_VALUE_CLASS)))return -1;
            }
            for(uint64_t j=0;j<n[8]->edges;j+=2) {
                if((j&1023)==0) {
                    if(end){uint64_t now;if(final_caller_clock(end,&now)<0)return -1;}
                    else if(!command_clock((FridayPublisherRootCommandReceipt *)r))return -1;
                }
                uint64_t key=command_class_edge(r,n[8],j),value=command_class_edge(r,n[8],j+1);
                if(!key||!value||!r->edge_roles[n[8]->edge_at+j]||
                   (!r->edge_roles[n[8]->edge_at+j+1]&&
                    r->nodes[value-1].kind!=COMMAND_VALUE_SUPPORT&&r->nodes[value-1].kind!=COMMAND_VALUE_CLASS))return -1;
            }
        } else if(p->kind==OWN_CLASS_SCOPE) {
            if(p->flags&~4095ULL||p->attempts>1||p->confirmed>1||p->width>1||!n[0]||!n[1]||!n[11])return -1;
            if((p->flags&CLASS_COPY)&&!n[4])return -1;
            if((p->flags&CLASS_WRAPPER)&&(!n[6]||!n[7]))return -1;
            if((p->flags&CLASS_COPY_SET_OK)&&!(p->flags&CLASS_COPY_SET_ATTEMPT))return -1;
            if((p->flags&CLASS_MODULE_SET_ATTEMPT)&&!(p->flags&CLASS_COPY_SET_OK))return -1;
            if((p->flags&CLASS_MODULE_SET_OK)&&!(p->flags&CLASS_MODULE_SET_ATTEMPT))return -1;
            if((p->flags&CLASS_EVAL_ATTEMPT)&&(!(p->flags&CLASS_MODULE_SET_OK)||!p->attempts))return -1;
            if((p->flags&CLASS_EVAL_RETURN)&&(!(p->flags&CLASS_EVAL_ATTEMPT)||!n[8]))return -1;
            if(n[8]&&!(p->flags&CLASS_EVAL_RETURN))return -1;
            if(p->confirmed!=((p->flags&CLASS_RESTORE_OK)!=0))return -1;
            if(p->flags&CLASS_RESTORE_OK) {
                if((p->flags&CLASS_UNCERTAIN)||!(p->flags&CLASS_RESTORE_ATTEMPT)||p->serial+2>v->count||
                   !v->rows[p->serial].confirmed||!v->rows[p->serial+1].confirmed)return -1;
            }
            if(p->flags&(CLASS_COPY_SET_ATTEMPT|CLASS_MODULE_SET_ATTEMPT|CLASS_EVAL_ATTEMPT)) {
                if(p->serial+2>v->count||!n[2]||!n[4]||!n[5]||!n[6]||!n[7]||!n[9]||!n[10]||
                   n[7]->kind!=COMMAND_VALUE_TUPLE||n[7]->edges!=4||
                   command_class_edge(r,n[7],0)!=n[5]->id||command_class_edge(r,n[7],1)!=n[0]->id||
                   command_class_number(r,command_class_edge(r,n[7],3),p->serial)<0)return -1;
            }
        } else {
            if(!p->flags) { /* an uninitialized second row after reservation refusal */
                if(p->attempts||p->confirmed||p->width)return -1;
                continue;
            }
            uint64_t operation=p->flags&CLASS_RESTORE_OP_MASK;
            if(p->flags&~255ULL||!operation||operation>2||p->attempts>1||
               p->confirmed>p->attempts||!p->width||p->width>v->count)return -1;
            const FridayPublisherOwnValue *scope=&v->rows[p->width-1];
            if(scope->kind!=OWN_CLASS_SCOPE||p->serial!=scope->serial+operation)return -1;
            if((p->flags&CLASS_RESTORE_LOOKUP_RETURN)&&!(p->flags&CLASS_RESTORE_LOOKUP_ATTEMPT))return -1;
            if((p->flags&CLASS_RESTORE_MUTATION_RETURN)&&!p->attempts)return -1;
            if((p->flags&CLASS_RESTORE_MUTATION_FAILED)&&!(p->flags&CLASS_RESTORE_MUTATION_RETURN))return -1;
            if((p->flags&CLASS_RESTORE_AFTER_ATTEMPT)&&
               (!(p->flags&CLASS_RESTORE_MUTATION_RETURN)||(p->flags&CLASS_RESTORE_MUTATION_FAILED)))return -1;
            if((p->flags&CLASS_RESTORE_AFTER_RETURN)&&!(p->flags&CLASS_RESTORE_AFTER_ATTEMPT))return -1;
            const FridayPublisherRootValueNode *module=command_class_node(r,scope,0);
            if(!module||!n[0]||module->id!=n[0]->id)return -1;
            if(p->attempts||p->confirmed) {
                unsigned j=operation==1?4:1;
                const FridayPublisherRootValueNode *dict=command_class_node(r,scope,j),
                    *key=command_class_node(r,scope,operation==1?10:9),
                    *expected=command_class_node(r,scope,operation==1?6:4),
                    *desired=command_class_node(r,scope,operation==1?5:2);
                if(!dict||!key||!expected||!desired||!n[1]||!n[2]||!n[3]||!n[4]||!n[5]||
                   dict->id!=n[1]->id||key->id!=n[2]->id||expected->id!=n[3]->id||
                   desired->id!=n[4]->id||n[5]->id!=n[3]->id)return -1;
            }
            if(p->confirmed&&(!n[6]||n[6]->id!=n[4]->id||p->error_saved||
               !(p->flags&CLASS_RESTORE_AFTER_RETURN)||(p->flags&CLASS_RESTORE_MUTATION_FAILED)))return -1;
            /* Failed/never-attempted operations retain their real prefix and
             * error triple. There is no required completed class/restore. */
        }
    }
    return 0;
}
static int command_secondary_slot_link(const FridayPublisherRootCommandReceipt *r,
    uint64_t at,PyObject *view,int ended,int required,uint64_t owner_first,uint64_t *next) {
    if(!at)return view?-1:0;
    if(at!=*next+1||at>r->owner_count||*next<owner_first)return -1;
    const FridayPublisherRootCommandOwner *o=&r->owners[at-1];
    if(o->group!=22||o->index!=*next-owner_first||!o->node||o->node>r->node_count)return -1;
    const FridayPublisherRootValueNode *n=&r->nodes[o->node-1];
    if(ended){if(view||o->owned||n->original)return -1;}
    else if(!view||o->owned!=view||n->original!=view)return -1;
    if(o->required!=(uint64_t)required)return -1;
    if(required&&(!n->required||!n->data_read||n->kind==COMMAND_VALUE_UNSUPPORTED))return -1;
    (*next)++;return 0;
}
static int final_caller_clock(FridayPublisherRootFinalHandoff *,uint64_t *);
static int error_relation_scan(uint64_t steps,int ended) {
    if(!ended)return secondary_debit_scan(steps,0);
    /* After utility/Source retirement use ONLY the same constructor's
     * original final-caller debit. Never call MasterBefore or mint credit. */
    if(steps>UINT64_MAX/64)return -1;
    uint64_t reads=steps*64;RootFinalCallerBinding *b=&final_caller_binding;
    if(!b->credit_reserved||b->read_used>b->read_credit||
       reads>b->read_credit-b->read_used)return -1;
    b->read_used+=reads;return 0;
}
static int command_error_handler_numeric(const FridayPublisherRootCommandReceipt *r,
    const FridayPublisherSecondaryCut *p,int ended,FridayPublisherRootFinalHandoff *h) {
    if(error_relation_scan(32,ended)<0)return -1;
    if(p->nedges!=5||p->nscalars)return -1;
    uint64_t ids[6];
    for(unsigned j=0;j<6;j++) {
        uint64_t at=p->owner_at[j];if(!at||at>r->owner_count)return -1;
        ids[j]=r->owners[at-1].node;if(!ids[j]||ids[j]>r->node_count)return -1;
    }
    const FridayPublisherRootValueNode *w=&r->nodes[ids[0]-1];
    if(w->kind!=COMMAND_VALUE_TUPLE||w->edges!=5||w->edge_at>r->edge_count||
       w->edges>r->edge_count-w->edge_at||!w->required||!w->data_read)return -1;
    for(unsigned j=0;j<5;j++)if(r->edges[w->edge_at+j]!=ids[j+1])return -1;
    if(r->nodes[ids[1]-1].kind!=COMMAND_VALUE_ERROR||
       r->nodes[ids[2]-1].kind!=COMMAND_VALUE_ERROR||ids[1]==ids[2])return -1;
    uint64_t context_kind=r->nodes[ids[3]-1].kind,tb_kind=r->nodes[ids[4]-1].kind;
    if((context_kind!=COMMAND_VALUE_NONE&&context_kind!=COMMAND_VALUE_ERROR)||
       (tb_kind!=COMMAND_VALUE_NONE&&tb_kind!=COMMAND_VALUE_TRACEBACK))return -1;
    const FridayPublisherRootValueNode *chain=&r->nodes[ids[5]-1];
    if(chain->kind!=COMMAND_VALUE_TUPLE||!chain->edges||
       chain->edges>root_storage.secondary.count||!chain->required||!chain->data_read||
       chain->edge_at>r->edge_count||chain->edges>r->edge_count-chain->edge_at||
       chain->edges>UINT64_MAX/4||error_relation_scan(chain->edges*4,ended)<0)return -1;
    uint64_t cursor=ids[2];
    for(uint64_t i=0;i<chain->edges;i++) {
        if((i&1023)==0) {
            uint64_t now;
            if(ended?(!h||final_caller_clock(h,&now)<0):!command_clock((FridayPublisherRootCommandReceipt *)r))return -1;
        }
        uint64_t id=r->edges[chain->edge_at+i];if(!id||id>r->node_count)return -1;
        const FridayPublisherRootValueNode *pair=&r->nodes[id-1];
        if(pair->kind!=COMMAND_VALUE_TUPLE||pair->edges!=2||!pair->required||!pair->data_read||
           pair->edge_at>r->edge_count||pair->edges>r->edge_count-pair->edge_at)return -1;
        uint64_t node=r->edges[pair->edge_at],next=r->edges[pair->edge_at+1];
        if(node!=cursor||!node||node>r->node_count||
           r->nodes[node-1].kind!=COMMAND_VALUE_ERROR||!next||next>r->node_count)return -1;
        uint64_t kind=r->nodes[next-1].kind;
        if(kind!=COMMAND_VALUE_ERROR&&kind!=COMMAND_VALUE_NONE)return -1;
        cursor=next;
    }
    if(r->nodes[cursor-1].kind!=COMMAND_VALUE_NONE) {
        if(error_relation_scan(chain->edges,ended)<0)return -1;
        int found=0;
        for(uint64_t i=0;i<chain->edges;i++) {
            if((i&1023)==0) {
                uint64_t now;
                if(ended?(!h||final_caller_clock(h,&now)<0):!command_clock((FridayPublisherRootCommandReceipt *)r))return -1;
            }
            const FridayPublisherRootValueNode *pair=&r->nodes[r->edges[chain->edge_at+i]-1];
            if(r->edges[pair->edge_at]==cursor){found=1;break;}
        }
        if(!found)return -1;
    }
    return 0;
}
static int command_secondary_rows_linked(const FridayPublisherRootStorage *s,
    const FridayPublisherRootCommandReceipt *r,int ended,uint64_t *next,
    FridayPublisherRootFinalHandoff *h) {
    const FridayPublisherSecondaryCuts *v=&s->secondary;
    if(v->count>FRIDAY_ROOT_SECONDARY_CUTS||v->owner_first>r->owner_count||
       v->owner_count>r->owner_count-v->owner_first)return -1;
    *next=v->owner_first;
    for(uint64_t i=0;i<v->count;i++) {
        const FridayPublisherSecondaryCut *p=&v->rows[i];
        if(p->serial!=i+1||p->pid!=r->pid||p->nedges<1||p->nedges>FRIDAY_ROOT_SECONDARY_EDGES)return -1;
        unsigned slots=1u+p->nedges;
        for(unsigned j=0;j<slots;j++) {
            PyObject *live=j?p->edges[j-1]:p->original;
            int required=(int)((p->required_mask>>j)&1U);
            if(!ended&&required&&(!live||live==Py_None))return -1;
            if(command_secondary_slot_link(r,p->owner_at[j],ended?NULL:live,ended,required,
                v->owner_first,next)<0)return -1;
        }
        if(p->kind==SECONDARY_CUT_ERROR_HANDLER&&command_error_handler_numeric(r,p,ended,h)<0)return -1;
    }
    return *next==v->owner_first+v->owner_count?0:-1;
}
static int command_own_views_end(FridayPublisherRootStorage *s,
    FridayPublisherRootCommandReceipt *r) {
    FridayPublisherOwnValues *v=&s->own_values;uint64_t next;
    if(v->retired||v->borrowers_end_confirmed||v->borrowers_cleared||
       r->own_registry_borrowers_end_confirmed||
       command_own_globals(v,r,0,&next)<0||
       command_unmapped_rows_check(r,0,NULL)<0||command_class_rows_consume(s,r,NULL)<0||
       command_error_record_rows_consume(r,NULL)<0)return -1;
    /* Validate the complete actual MOVE relation BEFORE invalidating any
     * view. A partial MOVE never reaches this end or loses an original. */
    for(uint64_t i=0;i<v->count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        if(command_own_row_links(&v->rows[i],i,r,0,&next)<0)return -1;
        r->own_registry_rows_checked++;
    }
    if(next!=v->owner_first+v->owner_count)return -1;
    uint64_t secondary_next=0;
    if(command_secondary_rows_linked(s,r,0,&secondary_next,NULL)<0)return -1;
    v->retired=1; /* prohibit producer/getter reuse BEFORE a borrower is null */
    for(uint64_t i=0;i<v->count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        FridayPublisherOwnValue *p=&v->rows[i];
        for(unsigned j=0;j<12;j++)if(p->refs[j]) {
            p->refs[j]=NULL;v->borrowers_cleared++;
        }
        if(p->error_type){p->error_type=NULL;v->borrowers_cleared++;}
        if(p->error_value){p->error_value=NULL;v->borrowers_cleared++;}
        if(p->error_tb){p->error_tb=NULL;v->borrowers_cleared++;}
    }
    uint64_t globals=(v->early_owner_at!=0)+(v->missing_owner_at!=0);
    for(unsigned j=0;j<6;j++)globals+=v->stock_owner_at[j]!=0;
    if(v->owner_count<globals||v->borrowers_cleared!=v->owner_count-globals)return -1;
    r->own_registry_borrowers_cleared=v->borrowers_cleared;
    v->borrowers_end_confirmed=1;r->own_registry_borrowers_end_confirmed=1;
    FridayPublisherSecondaryCuts *sec=&s->secondary;
    if(sec->retired||sec->borrowers_end_confirmed)return -1;
    for(uint64_t i=0;i<sec->count;i++) {
        if((i&1023)==0&&!command_clock(r))return -1;
        FridayPublisherSecondaryCut *p=&sec->rows[i];
        if(p->nedges>FRIDAY_ROOT_SECONDARY_EDGES)return -1;
        if((p->borrowed_mask&1U)&&p->original){p->original=NULL;sec->borrowers_cleared++;}
        for(unsigned j=0;j<p->nedges;j++)
            if((p->borrowed_mask&(1U<<(j+1)))&&p->edges[j]){p->edges[j]=NULL;sec->borrowers_cleared++;}
    }
    if(sec->borrowers_cleared!=sec->owner_count)return -1;
    sec->borrowers_end_confirmed=1;sec->retired=1;
    return 0; /* NO DECREF: every invalidated slot was a verified borrower */
}
static int command_retire_one(FridayPublisherRootCommandReceipt *r,PyObject **slot,
                             const char *phase) {
    if(!*slot)return 0;
    if(PyErr_Occurred()) {
        command_error(r,&r->cleanup_error,phase);return -1;
    }
    PyObject *owned=*slot;*slot=NULL;
    Py_DECREF(owned);
    /* The required original body/alias graph already lives in preowned
     * native receipt storage before this last-reference operation. A new
     * cleanup error is captured once, or remains actually pending when that
     * independent cell is occupied. STOP before a next fallible operation. */
    return command_error(r,&r->cleanup_error,phase);
}
static int command_retire_owners(FridayPublisherRootStorage *,
    FridayPublisherRootCommandReceipt *);
static int command_secondary_error_end(FridayPublisherRootStorage *s,
    FridayPublisherRootCommandReceipt *r) {
    FridayPublisherSecondaryCuts *v=&s->secondary;
    if(r->owner_retirement_attempted||v->end_confirmed||v->end_attempted)return 0;
    v->end_attempted=1;
    /* The whole actual owner graph, not a three-root error helper, must have
     * passed both live consumers. Reuse the SAME guarded retirement path.
     * Missing cuts, a partial MOVE/segment, unresolved cursor/buffer/FD or an
     * occupied pending error retain all remaining owners and borrowers. */
    if(!r->secondary_payload_complete||!r->graph_consumed||
       !r->owner_inventory_complete||!r->required_values_complete||
       !r->error_payload_complete||r->full_bytes_read!=r->body_bytes||
       !r->owned_FDs_complete||r->uncertain_FDs||r->pending_error_retained||
       r->untransferred_Run||r->bootstrap_end_unconfirmed||r->getter_pending||
       r->stock_cursor||r->active_buffer_owned||!command_has_owned_runtime(s)||
       PyErr_Occurred()||command_class_retirement_blocked(NULL))return 0;
    /* Never repeat a failed destructive registration operation. A fresh
     * cleanup phase may start only after complete evidence for the original
     * codec error. A new error stops before the next fallible operation. */
    if(r->source_registrations_attempted&&!r->source_registrations_retired)return 0;
    if(!r->source_registrations_attempted&&command_registrations(s,r)<0) {
        command_error(r,&r->cleanup_error,"secondary_full_graph_registration_end");
        r->residual="secondary_full_graph_registration_end_UNCONFIRMED";return -1;
    }
    if(command_retire_owners(s,r)<0) {
        command_error(r,&r->cleanup_error,"secondary_full_graph_original_owner_end");
        r->residual="secondary_full_graph_owner_end_UNCONFIRMED";return -1;
    }
    r->residual="actual_error_graph_Source_refs_ended_owned_runtime_end_pending";
    return 0; /* runtime finalization still belongs to the actual cold caller */
}
static int command_retire_owners_in_region(FridayPublisherRootStorage *s,
                                FridayPublisherRootCommandReceipt *r) {
    if(r->owner_retirement_attempted||!r->owner_inventory_complete||
       !r->graph_consumed||!r->required_values_complete||!r->error_payload_complete||
       r->full_bytes_read!=r->body_bytes||!r->source_registrations_retired||
       !r->runtime_support_pending||!command_has_owned_runtime(s)||
       !r->owned_FDs_complete||r->uncertain_FDs||r->pending_error_retained||
       r->untransferred_Run||r->bootstrap_end_unconfirmed||r->getter_pending||
       r->stock_cursor||r->active_buffer_owned||
       r->graph_owned_nodes!=r->node_count||r->graph_retired_nodes||
       PyErr_Occurred()||command_mapping_retirement_blocked(r)||command_class_retirement_blocked(NULL)||
       command_set_before_loss(r)<0||!command_set_region_live(r))return -1;
    r->owner_retirement_attempted=1;
    s->secondary.end_attempted=1;
    if(command_own_views_end(s,r)<0)return -1;
    if(!command_set_region_live(r))return -1;
    /* Invalidated BORROWERS are not strong references to DECREF. Their real
     * strong owners have moved into the exact receiver inventory once. */
    s->bindings.root_fact=NULL;s->bindings.qualification=NULL;
    s->bindings.held_root_tool_preimage=NULL;
    s->bindings.preowned_refusal_type=NULL;s->bindings.preowned_refusal_value=NULL;
    /* These caches borrow objects now strongly held by owners/nodes. Invalidate
     * them before the last real reference can end; never DECREF a borrower. */
    memset(r->source_module_cache,0,sizeof(r->source_module_cache));
    memset(r->source_class_cache,0,sizeof(r->source_class_cache));
    r->frame_locals_type=NULL;
    for(uint64_t i=0;i<r->owner_count;i++) {
        if((i&1023)==0) {
            uint64_t now=command_clock(r);
            if(!now||now>s->pool.deadline_ns)return -1;
        }
        FridayPublisherRootCommandOwner *o=&r->owners[i];
        if(o->group>=24&&o->group<=27&&secondary_debit_scan(8,0)<0)return -1;
        if(secondary_debit_scan(8,0)<0||!command_set_region_live(r))return -1;
        if(!o->owned)return -1; /* duplicate/missing ownership, not success */
        int rc=command_retire_one(r,&o->owned,"actual_command_owner_DECREF");
        if(!o->owned)r->retired_owners++;
        if(rc<0)return -1;
    }
    /* SOL104 graph nodes own actual Py_NewRef references, independently of
     * owner slots. The already-read native body/edge identities survive this
     * finite release; a cleanup failure retains every unvisited original. */
    for(uint64_t i=0;i<r->node_count;i++) {
        if((i&1023)==0) {
            uint64_t now=command_clock(r);
            if(!now||now>s->pool.deadline_ns)return -1;
        }
        FridayPublisherRootValueNode *n=&r->nodes[i];
        if(secondary_debit_scan(8,0)<0||!command_set_region_live(r))return -1;
        if(!n->original)return -1;
        int rc=command_retire_one(r,&n->original,"actual_command_graph_node_DECREF");
        if(!n->original)r->graph_retired_nodes++;
        if(rc<0)return -1;
    }
    /* saved remains historical truth. It is NOT itself a live reference.
     * Exact error payloads/aliases must already be in the full native graph;
     * the Sol104 reader supplies that required coverage on error paths. */
    PyObject **cells[]={&r->incoming_error.type,&r->incoming_error.value,&r->incoming_error.tb,
        &r->cleanup_error.type,&r->cleanup_error.value,&r->cleanup_error.tb};
    for(unsigned i=0;i<sizeof(cells)/sizeof(cells[0]);i++) {
        if(!*cells[i])continue;
        if(secondary_debit_scan(8,0)<0||!command_set_region_live(r))return -1;
        int rc=command_retire_one(r,cells[i],"actual_command_error_reference_DECREF");
        if(!*cells[i])r->error_references_retired++;
        if(rc<0)return -1;
    }
    r->remaining_error_references=command_error_reference_count(r);
    r->owner_retirement_returned=1;
    int secondary_settled=r->retired_owners==r->owner_count&&
        r->graph_retired_nodes==r->graph_owned_nodes&&
        !r->remaining_error_references;
    /* Phase3 stayed paused BEFORE all captures, full consumers, borrower
     * invalidation and the owner/node/error loops. GC restore does not excuse
     * unqualified synchronous callbacks during those actual releases. */
    if(secondary_settled)r->set_region_loss_complete=1;
    if(command_set_region_end(r,secondary_settled)<0)return -1;
    if(secondary_settled)s->secondary.end_confirmed=1;
    return secondary_settled?0:-1;
}
static int command_retire_owners(FridayPublisherRootStorage *s,
                                FridayPublisherRootCommandReceipt *r) {
    int rc=command_retire_owners_in_region(s,r);
    /* BOTH callers (ordinary receiver and secondary-error endpoint) unwind
     * EVERY reached early return, including failure after borrower clearing
     * or a partial real DECREF prefix. No second retirement/restore attempt. */
    if(rc<0&&command_set_region_abort(r)<0)r->pending_error_retained=1;
    return rc;
}
int FridayPublisherRootCommandReceive(const FridayPublisherRootTerminal *t,
    FridayPublisherRootCommandReceipt *r) {
    FridayPublisherRootStorage *s=&root_storage;
    if(!r||t!=&s->terminal||r!=&s->command_receipt||!s->attempted||r->attempted)return -1;
    r->schema=FRIDAY_ROOT_COMMAND_RECEIPT_SCHEMA;r->bytes=sizeof(*r);
    r->pid=getpid();r->attempted=1;r->receiving=1;
    r->phase="actual_same_Root_complete_owner_intake";
    if(!Py_IsInitialized()) {r->residual="runtime_not_initialized";goto sealed;}
    r->thread=PyThread_get_thread_ident();
    /* Two preowned error cells suffice for THIS finite path because any
     * newly failing operation stops it immediately. Not fixed8 + keep going.
     * Incoming full originals are held before ANY later fallible operation. */
    command_error(r,&r->incoming_error,"command_incoming_original_indicator");
    r->pending_error_retained=PyErr_Occurred()!=NULL;
    r->untransferred_Run=t->result.untransferred_run!=NULL||
        (t->result.failure_handback_attempted&&!t->result.failure_handback_confirmed);
    if(r->pending_error_retained||r->untransferred_Run) {
        /* Saved incoming error stays in its cell. FD completion is syscall-only
         * and does not replace that cell. Python intake still stops here. */
        if(!r->untransferred_Run&&s->command_read_reserved&&s->held_count<=FRIDAY_ROOT_HELD_FILES&&
           s->prepared_count<=FRIDAY_NATIVE_FD_HISTORY&&
           s->bootstrap.fd_count<=ROOT_BOOT_FILES&&
           s->image_inventory.fd_count<=FRIDAY_ROOT_HELD_FILES) {
            if(command_complete_FDs(s,r)<0)
                r->residual="finite_native_FD_completion_retained";
            else r->residual="actual_original_Run_or_pending_indicator_retained";
        } else r->residual="native_original_inventory_bounds_UNCONFIRMED";
        goto sealed;
    }
    RootBootstrapState *bootstrap=&s->bootstrap;
    r->bootstrap_parent_mapping_retained=bootstrap->map_acquired;
    r->bootstrap_parent_bank_read=t->bootstrap_parent_native_read&&
        bootstrap->bank_full_read&&bootstrap->bank_stable&&
        t->bootstrap_native_storage==bootstrap;
    int returned_failure=bootstrap->lifetime_phase==1&&
        bootstrap->native_failure_transport_complete&&bootstrap->child_transport_end_confirmed;
    int stock_retirement=bootstrap->lifetime_phase==2&&
        bootstrap->stock_image_replacement_confirmed&&
        bootstrap->public_stock_parent_legal_retirement_accounted&&
        bootstrap->public_stock_observables_consumed&&
        bootstrap->kernel_process_lifetime_ended&&!bootstrap->child_transport_end_confirmed;
    /* Required current native data and either actual phase-correct endpoint,
     * not the impossible returned-native flag on public-stock success. */
    r->bootstrap_end_unconfirmed=bootstrap->attempted&&
        !(r->bootstrap_parent_bank_read&&!r->bootstrap_parent_mapping_retained&&
          bootstrap->all_fd_ends&&(returned_failure||stock_retirement));
    if(s->held_count>FRIDAY_ROOT_HELD_FILES||s->prepared_count>FRIDAY_NATIVE_FD_HISTORY||
       s->bootstrap.fd_count>ROOT_BOOT_FILES||s->bootstrap.retained_count>4096||
       s->bootstrap.secondary_count>ROOT_BOOT_ERRORS||
       s->image_inventory.fd_count>FRIDAY_ROOT_HELD_FILES||
       s->own_values.count>FRIDAY_ROOT_COMMAND_NODES||
       s->secondary.count>FRIDAY_ROOT_SECONDARY_CUTS) {
        r->residual="native_original_inventory_bounds_UNCONFIRMED";goto sealed;
    }
    if(s->command_read_reserved) {
        if(command_tuple13(r,t->result.full_packet)<0) {
            command_error(r,&r->cleanup_error,"typed_original_tuple13_reader");
            r->residual="actual_tuple13_correspondence_UNCONFIRMED";goto sealed;
        }
        /* These record reads precede owner MOVE because lookup authenticates
         * exact actual native row addresses, not reconstructed/public flags. */
        if(command_close_records(s,r)<0) {
            command_error(r,&r->cleanup_error,"actual_producer_close_correspondence");
            r->residual="actual_native_close_record_correspondence_UNCONFIRMED";goto sealed;
        }
        r->producer_close_body_bytes=r->body_bytes;
        if(command_complete_FDs(s,r)<0)r->residual="finite_native_FD_completion_retained";
        if(command_native_owners(s,r)<0) {
            r->residual="full_native_owner_bank_capacity_UNCONFIRMED";goto sealed;
        }
    }
    if(command_take_owners(s,r)<0) {r->residual="owner_MOVE_capacity_incomplete_originals_retained";goto sealed;}
    if(!s->command_read_reserved) {
        r->residual="early_refused_pool_no_full_read_credit_original_custody_MOVED";goto sealed;
    }
    if(command_unmapped_rows_append(s,r)<0) {
        r->residual="actual_unmapped_native_row_body_UNCONFIRMED";goto sealed;
    }
    if(command_values(r)<0) {
        command_error(r,&r->cleanup_error,"full_typed_value_native_reader");
        r->residual="full_typed_value_graph_capacity_or_native_conversion_UNCONFIRMED";goto sealed;
    }
    if(!r->required_values_complete) {
        r->residual="required_nonbuiltin_values_errors_and_aliases_retained_not_erased";goto sealed;
    }
    if(r->cleanup_error.saved) {
        r->residual="first_codec_cleanup_error_exact_originals_owned_NOT_full_read";goto sealed;
    }
    /* Group20 moved incoming originals into the full required value graph.
     * saved is historical; actual reference retirement below decides the end. */
    r->error_payload_complete=r->required_values_complete;
    if(command_class_retirement_blocked(NULL)) {
        r->residual="class_factory_restore_UNCONFIRMED_all_originals_retained";goto sealed;
    }
    if(command_registrations(s,r)<0) {
        command_error(r,&r->cleanup_error,"actual_support_registration_cleanup");
        r->residual="actual_support_registration_cleanup_UNCONFIRMED";goto sealed;
    }
    if(command_retire_owners(s,r)<0) {
        command_error(r,&r->cleanup_error,"actual_command_owner_retirement");
        r->residual="actual_owner_retirement_UNCONFIRMED_originals_retained";goto sealed;
    }
    r->residual="actual_Source_refs_ended_owned_runtime_end_pending";
sealed:
    /* Unwind a reached pre-loss region before any secondary/finalized path.
     * Never retry an uncertain restore, overwrite pending originals or claim
     * a refused region consumed. An unreconciled state forbids later loss. */
    if(s->set_region.active_phase&&command_set_region_abort(r)<0)
        r->pending_error_retained=1;
    /* Exactly one no-factory whole graph traversal before owner retirement.
     * Existing incomplete representations and full failed byte/edge prefixes
     * remain separately named, not overwritten or accepted as full data. */
    if(s->command_read_reserved&&
       (r->cleanup_error.saved||(r->owner_inventory_complete&&!r->required_values_complete))&&
       !r->secondary_read_attempted&&!r->owner_retirement_attempted&&
       !s->set_region.active_phase&&!s->set_region.restore_type&&
       !s->set_region.restore_value&&!s->set_region.restore_tb&&Py_IsInitialized())
        command_secondary_payload(r);
    if(!r->owner_retirement_attempted&&!s->set_region.active_phase&&
       !s->set_region.restore_type&&!s->set_region.restore_value&&
       !s->set_region.restore_tb&&Py_IsInitialized())
        command_secondary_error_end(s,r);
    r->retained_owners=r->owner_count-r->retired_owners+
        r->graph_owned_nodes-r->graph_retired_nodes+
        (r->getter_pending!=NULL)+(r->stock_cursor!=NULL)+(r->active_buffer_owned!=0)+
        (r->incoming_error.type!=NULL)+(r->incoming_error.value!=NULL)+(r->incoming_error.tb!=NULL)+
        (r->cleanup_error.type!=NULL)+(r->cleanup_error.value!=NULL)+(r->cleanup_error.tb!=NULL)+
        (s->own_values.early_module!=NULL)+(s->own_values.token_missing!=NULL)+
        (s->own_values.mapping_module!=NULL)+(s->own_values.mapping_factory!=NULL)+
        (s->own_values.mapping_access!=NULL)+(s->own_values.hash_module!=NULL)+
        (s->own_values.hash_sha256!=NULL)+(s->own_values.hash_new!=NULL)+
        (s->source_case!=NULL)+(s->set_region.restore_type!=NULL)+
        (s->set_region.restore_value!=NULL)+(s->set_region.restore_tb!=NULL);
    /* Include actual unmoved producer strong fields after partial capacity
     * refusal. Explicit borrowed views are NOT counted again as owners. */
    for(uint64_t i=0;i<s->own_values.count&&i<FRIDAY_ROOT_COMMAND_NODES;i++) {
        FridayPublisherOwnValue *p=&s->own_values.rows[i];
        for(unsigned j=0;j<FRIDAY_OWN_FIELDS;j++)
            r->retained_owners+=p->refs[j]!=NULL&&!(p->borrowed_mask&(1U<<j));
        r->retained_owners+=(p->error_type!=NULL&&!(p->borrowed_mask&(1U<<12)))+
            (p->error_value!=NULL&&!(p->borrowed_mask&(1U<<13)))+
            (p->error_tb!=NULL&&!(p->borrowed_mask&(1U<<14)));
    }
    for(uint64_t i=0;i<s->secondary.count&&i<FRIDAY_ROOT_SECONDARY_CUTS;i++) {
        FridayPublisherSecondaryCut *q=&s->secondary.rows[i];
        unsigned nedges=q->nedges>FRIDAY_ROOT_SECONDARY_EDGES?0:q->nedges;
        r->retained_owners+=q->original!=NULL&&!(q->borrowed_mask&1U);
        for(unsigned j=0;j<nedges;j++)
            r->retained_owners+=q->edges[j]!=NULL&&!(q->borrowed_mask&(1U<<(j+1)));
    }
    r->remaining_error_references=command_error_reference_count(r);
    /* Distinct actual phases: full native data + Source references ended
     * permits the OWNED runtime finalizer. It does not claim that finalizer
     * has already run. Keeping those phases separate removes the previous
     * circular "runtime ended before runtime may end" predicate. */
    r->source_owner_end_confirmed=r->owner_retirement_attempted&&
        r->owner_retirement_returned&&r->owner_inventory_complete&&
        r->graph_consumed&&r->required_values_complete&&r->error_payload_complete&&
        r->full_bytes_read==r->body_bytes&&r->source_registrations_retired&&
        s->own_values.retired&&s->own_values.borrowers_end_confirmed&&
        r->own_registry_borrowers_end_confirmed&&
        s->secondary.end_confirmed&&s->secondary.borrowers_end_confirmed&&
        r->own_registry_rows_checked==s->own_values.count&&
        r->owned_FDs_complete&&!r->uncertain_FDs&&!r->retained_owners&&
        !r->remaining_error_references&&r->set_region_loss_complete==1&&
        !s->set_region.active_phase&&!s->set_region.restore_type&&
        !s->set_region.restore_value&&!s->set_region.restore_tb&&
        r->set_region_restored[2]==1&&r->set_region_consumed[2]==1&&
        !r->pending_error_retained&&!r->untransferred_Run&&!r->bootstrap_end_unconfirmed&&
        !s->source_case&&!command_mapping_retirement_blocked(r);
    r->runtime_finalization_eligible=r->source_owner_end_confirmed&&
        r->runtime_support_pending&&r->cold_runtime_bound&&command_has_owned_runtime(s)&&
        !command_mapping_retirement_blocked(r);
    r->native_end_confirmed=r->source_owner_end_confirmed&&
        r->registrations_retired&&!r->registrations_remaining&&!r->runtime_support_pending;
    r->SourceReady=0;r->Root_admission=0;r->GO=0;
    r->receiving=0;r->sealed=1;
    /* This native receipt is private to SAME sole cold command. External
     * tuple13, its False/True statuses, document and bank alias lists were
     * NEVER mutated after exposure. No exit/ACK or return code is acceptance.
     * No repeated Receive, retry, Py_FinalizeEx or external runtime takeover. */
    return 0;
}

static int final_caller_clock(FridayPublisherRootFinalHandoff *h,uint64_t *out) {
    if(h->clock_failed)return -1;
    if(FridayPublisherMasterCompletionClockBlocked(&root_storage.pool)) {
        h->clock_failed=1;h->clock_fault_kind=6;return -1;
    }
    h->clock_attempted=1;
    int rc=FridayPublisherNativeClockSample(&h->clock_original,root_storage.pool.started_ns,
        root_storage.pool.deadline_ns,FRIDAY_CLOCK_FINAL_CALLER,out);
    h->clock_rc=h->clock_original.rc;h->clock_errno=h->clock_original.error;
    if(rc<0) {
        h->clock_failed=1;h->clock_fault_kind=h->clock_original.fault_kind;return -1;
    }
    return 0;
}
static int final_caller_read(FridayPublisherRootFinalHandoff *h,
                            const void *data,uint64_t bytes,uint64_t *actual) {
    RootFinalCallerBinding *b=&final_caller_binding;
    if(h->clock_failed||!b->credit_reserved||b->read_used>b->read_credit||
       bytes>b->read_credit-b->read_used)return -1;
    /* Original constructor debit,not a new MasterBefore after utility close. */
    b->read_used+=bytes;
    const unsigned char *p=data;
    for(uint64_t i=0;i<bytes;i++) {
        if((i&65535)==0) {
            uint64_t now;if(final_caller_clock(h,&now)<0)return -1;
        }
        h->reader_sink^=p[i];(*actual)++;
    }
    return 0;
}
static int final_caller_graph(FridayPublisherRootFinalHandoff *h,
                              const FridayPublisherRootCommandReceipt *r) {
    /* After source retirement only retained historical bytes/native numeric
     * edges are read. NEVER call a Python getter or dereference original. */
    if(r->schema!=FRIDAY_ROOT_COMMAND_RECEIPT_SCHEMA||r->bytes!=sizeof(*r)||
       !r->sealed||r->receiving||!r->source_owner_end_confirmed||
       !r->graph_started||!r->graph_consumed||
       !r->required_values_complete||!r->error_payload_complete||
       r->owner_count>FRIDAY_ROOT_COMMAND_OWNERS||
       r->node_count>FRIDAY_ROOT_COMMAND_NODES||r->edge_count>FRIDAY_ROOT_COMMAND_EDGES||
       r->body_bytes>FRIDAY_ROOT_COMMAND_BYTES||r->full_bytes_read!=r->body_bytes||
       r->segment_count>FRIDAY_ROOT_COMMAND_SEGMENTS||r->graph_body_at>r->body_bytes||
       r->graph_edge_at||r->native_owner_body_at!=r->producer_close_body_bytes||
       r->native_owner_body_at>r->graph_body_at||
       r->native_owner_body_bytes!=r->graph_body_at-r->native_owner_body_at||
       r->retired_owners!=r->owner_count||r->graph_retired_nodes!=r->node_count||
       r->graph_owned_nodes!=r->node_count||r->retained_owners||r->remaining_error_references||
       r->getter_pending||r->stock_cursor||r->active_buffer_owned||
       r->incoming_error.type||r->incoming_error.value||r->incoming_error.tb||
       r->cleanup_error.type||r->cleanup_error.value||r->cleanup_error.tb)return -1;
    if(command_unmapped_scan(128,1)<0||
       root_storage.set_region.active_phase||root_storage.set_region.restore_type||
       root_storage.set_region.restore_value||root_storage.set_region.restore_tb||
       r->set_region_loss_complete!=1)return -1;
    for(unsigned p=0;p<3;p++) {
        if(r->set_region_attempted[p]>1||r->set_region_prior_gc[p]>1||
           r->set_region_paused[p]>1||r->set_region_restore_attempted[p]>1||
           r->set_region_restored[p]>1||r->set_region_consumed[p]>1||
           r->set_index_attempted[p]>1||r->set_index_complete[p]>1||
           r->set_index_nodes[p]>r->node_count)return -1;
        if(r->set_phase_attempted[p]) {
            if(!r->set_phase_complete[p]||r->set_region_attempted[p]!=1||
               r->set_region_paused[p]!=1||r->set_region_restore_attempted[p]!=1||
               r->set_region_restored[p]!=1||r->set_region_consumed[p]!=1||
               r->set_index_attempted[p]!=1||r->set_index_complete[p]!=1||
               r->set_index_nodes[p]!=r->set_phase_nodes[p])return -1;
        } else if(r->set_region_attempted[p]||r->set_region_prior_gc[p]||
                  r->set_region_paused[p]||r->set_region_restore_attempted[p]||
                  r->set_region_restored[p]||r->set_region_consumed[p]||
                  r->set_index_attempted[p]||r->set_index_complete[p]||
                  r->set_index_nodes[p])return -1;
    }
    if((r->secondary_read_attempted&&!r->secondary_payload_complete)||
       (r->cleanup_error.saved&&!r->secondary_read_attempted)||
       r->secondary_error_present>7)return -1;
    if(!r->cleanup_error.saved&&r->secondary_error_present)return -1;
    for(unsigned j=0;j<3;j++) {
        uint64_t id=r->secondary_roots[j];
        if(((r->secondary_error_present>>j)&1)!=(id!=0)||id>r->node_count)return -1;
        if(id&&j&&!r->nodes[id-1].required)return -1;
    }
    if(r->secondary_pending_owner_at) {
        if(r->secondary_pending_owner_at>r->owner_count)return -1;
        const FridayPublisherRootCommandOwner *o=&r->owners[r->secondary_pending_owner_at-1];
        if(o->group!=23||o->index||!o->required||o->owned)return -1;
    }
    /* Cursor/buffer collection is evidence, not retirement. No branch in
     * this implementation claims their unresolved last-owner end. */
    if(r->secondary_aux_roots[0]||r->secondary_aux_roots[1])return -1;
    uint64_t next_body=r->graph_body_at,next_edge=r->graph_edge_at;
    for(uint64_t i=0;i<r->segment_count;i++) {
        if((i&1023)==0){uint64_t now;if(final_caller_clock(h,&now)<0)return -1;}
        if(command_segment_relation(r,i,&next_body,&next_edge)<0)return -1;
        const FridayPublisherRootValueSegment *segment=&r->segments[i];
        if(r->nodes[segment->node-1].required)
            for(uint64_t j=0;j<segment->edges;j++) {
                if((j&1023)==0){uint64_t now;if(final_caller_clock(h,&now)<0)return -1;}
                uint64_t at=segment->edge_at+j,id=r->edges[at];
                if(!id||id>r->node_count||r->edge_roles[at]>1||
                   (r->edge_roles[at]&&!r->nodes[id-1].required))return -1;
            }
    }
    if(next_body!=r->body_bytes||next_edge!=r->edge_count)return -1;
    for(uint64_t i=0;i<r->owner_count;i++) {
        if((i&1023)==0){uint64_t now;if(final_caller_clock(h,&now)<0)return -1;}
        const FridayPublisherRootCommandOwner *o=&r->owners[i];
        if(o->group>=24&&o->group<=27&&command_unmapped_scan(8,1)<0)return -1;
        if(o->owned||!o->node||o->node>r->node_count||o->required>1)return -1;
        if(o->required&&!r->nodes[o->node-1].required)return -1;
        h->owners_checked++;
    }
    /* Exact retained argument owner/tuple aliases, AFTER Python retirement.
     * The ordinary codec already read this immutable Unicode in full. No
     * historical input pointer or Python getter is dereferenced here. */
    const FridayPublisherRootStorage *s=&root_storage;
    if(s->case_owner_at) {
        if(s->case_owner_at>r->owner_count)return -1;
        const FridayPublisherRootCommandOwner *o=&r->owners[s->case_owner_at-1];
        if(o->group!=5||o->index!=6||!o->required||o->owned||
           !o->node||o->node>r->node_count||
           r->nodes[o->node-1].kind!=COMMAND_VALUE_UNICODE)return -1;
    }
    if(s->case_tuple_alias_verified) {
        if(!s->case_owner_at||!s->args_owner_at||s->args_owner_at>r->owner_count)return -1;
        const FridayPublisherRootCommandOwner *o=&r->owners[s->args_owner_at-1];
        if(o->group!=5||o->index!=5||!o->required||o->owned||
           !o->node||o->node>r->node_count)return -1;
        const FridayPublisherRootValueNode *v=&r->nodes[o->node-1];
        if(v->kind!=COMMAND_VALUE_TUPLE||v->edges!=1||v->edge_at>=r->edge_count||
           r->edges[v->edge_at]!=r->owners[s->case_owner_at-1].node)return -1;
    }
    if(s->terminal.call_attempted&&!s->case_tuple_alias_verified)return -1;
    for(uint64_t i=0;i<r->node_count;i++) {
        if((i&1023)==0){uint64_t now;if(final_caller_clock(h,&now)<0)return -1;}
        const FridayPublisherRootValueNode *v=&r->nodes[i];
        if(v->id!=i+1||v->original||v->required>2||
           v->body_at>r->body_bytes||v->body_bytes>r->body_bytes-v->body_at||
           v->edge_at>r->edge_count||v->edges>r->edge_count-v->edge_at||
           v->first_parent>r->node_count||v->first_edge>r->edge_count||
           (v->required&&(!v->data_read||v->kind==COMMAND_VALUE_UNSUPPORTED)))return -1;
        if(command_native_node_shape(r,v,h)<0)return -1;
        if(v->kind==COMMAND_VALUE_MAPPING&&!command_mapping_completed_body(
           r->body+v->body_at,v->body_bytes,v->edges))return -1;
        for(uint64_t j=0;j<v->edges;j++) {
            if((j&1023)==0){uint64_t now;if(final_caller_clock(h,&now)<0)return -1;}
            uint64_t at=v->edge_at+j,id=r->edges[at];
            if(!id||id>r->node_count||r->edge_roles[at]>1||
               (v->required&&r->edge_roles[at]&&!r->nodes[id-1].required))return -1;
        }
        h->nodes_checked++;
    }
    const FridayPublisherOwnValues *owned=&root_storage.own_values;uint64_t next_owner;
    if(!owned->retired||!owned->borrowers_end_confirmed||
       !r->own_registry_borrowers_end_confirmed||
       r->own_registry_rows_checked!=owned->count||
       r->own_registry_borrowers_cleared!=owned->borrowers_cleared||
       command_own_globals(owned,r,1,&next_owner)<0||
       command_unmapped_rows_check(r,1,h)<0)return -1;
    for(uint64_t i=0;i<owned->count;i++) {
        if((i&1023)==0){uint64_t now;if(final_caller_clock(h,&now)<0)return -1;}
        if(command_own_row_links(&owned->rows[i],i,r,1,&next_owner)<0)return -1;
    }
    if(next_owner!=owned->owner_first+owned->owner_count)return -1;
    if(command_class_retirement_blocked(h)||command_class_rows_consume(&root_storage,r,h)<0||
       command_error_record_rows_consume(r,h)<0)return -1;
    if(!root_storage.secondary.end_confirmed||!root_storage.secondary.borrowers_end_confirmed)return -1;
    uint64_t secondary_next=0;
    if(command_secondary_rows_linked(&root_storage,r,1,&secondary_next,h)<0)return -1;
    /* Independently walk the actual complete edge bank too. Node ranges do
     * not authorize reading outside it or silently dropping its tail. */
    for(uint64_t i=0;i<r->edge_count;i++) {
        if((i&1023)==0){uint64_t now;if(final_caller_clock(h,&now)<0)return -1;}
        if(!r->edges[i]||r->edges[i]>r->node_count||r->edge_roles[i]>1)return -1;
        h->edges_checked++;
    }
    return 0;
}
static int final_caller_cold(FridayPublisherRootFinalHandoff *h,
                             const FridayPublisherRootColdResult *c) {
    if(!c->completion_attempted||!c->completion_returned)return -1;
    const FridayPublisherRootCaseInput *input=&c->case_input;
    if(input->attempted&&(!input->credit_reserved||!input->bounded||!input->complete||
       input->drift||input->bytes>FRIDAY_ROOT_CASE_INPUT_MAX||input->body[input->bytes]||
       memchr(input->body,0,(size_t)input->bytes)))return -1;
    if(c->perform_attempted) {
        const FridayPublisherRootCaseInput *copy=&root_storage.case_input;
        if(!input->complete)return -1;
        if(copy->attempted&&(!copy->credit_reserved||!copy->bounded||!copy->complete||
           copy->drift||copy->historical_input!=input->body||copy->bytes!=input->bytes||
           memcmp(copy->body,input->body,(size_t)input->bytes+1)))return -1;
        if(root_storage.terminal.call_attempted&&!copy->complete)return -1;
    }
    if(c->first_status_saved&&(!c->status_bodies_complete||
       c->status_func_bytes>FRIDAY_ROOT_COMMAND_TEXT||
       c->status_message_bytes>FRIDAY_ROOT_COMMAND_TEXT||
       c->status_func[c->status_func_bytes]||c->status_message[c->status_message_bytes]||
       c->first_status.func!=(c->status_func_present?c->status_func:NULL)||
       c->first_status.err_msg!=(c->status_message_present?c->status_message:NULL)))return -1;
    const FridayPublisherColdConfigReceipt *v=&c->config_receipt;
    if(v->attempted) {
        if(!v->complete||v->text_count>FRIDAY_ROOT_CONFIG_ROWS||
           v->list_count>FRIDAY_ROOT_CONFIG_LISTS||v->body_bytes>FRIDAY_ROOT_COMMAND_TEXT||
           v->full_bytes_read!=v->body_bytes)return -1;
        for(uint64_t i=0;i<v->text_count;i++) {
            if((i&1023)==0){uint64_t now;if(final_caller_clock(h,&now)<0)return -1;}
            const FridayPublisherColdConfigText *q=&v->text[i];
            if(!q->original) {
                if(q->alias||q->body_bytes)return -1;
                continue;
            }
            if(!q->alias||q->alias>i+1||q->body_at>v->body_bytes||
               q->body_bytes>v->body_bytes-q->body_at||!q->body_bytes||
               q->body_bytes%sizeof(wchar_t))return -1;
            const FridayPublisherColdConfigText *birth=&v->text[q->alias-1];
            if(birth->original!=q->original||birth->body_at!=q->body_at||
               birth->body_bytes!=q->body_bytes||birth->alias!=q->alias)return -1;
            wchar_t end;memcpy(&end,v->body+q->body_at+q->body_bytes-sizeof(end),sizeof(end));
            if(end)return -1;
        }
        for(uint64_t i=0;i<v->list_count;i++) {
            const FridayPublisherColdConfigList *q=&v->lists[i];
            if(q->original_length<0||q->count!=(uint64_t)q->original_length||
               q->first>v->text_count||q->count>v->text_count-q->first||
               (q->original_items&&(!q->array_alias||q->array_alias>i+1))||
               (!q->original_items&&(q->array_alias||q->count)))return -1;
            if(q->original_items) {
                const FridayPublisherColdConfigList *birth=&v->lists[q->array_alias-1];
                if(birth->original_items!=q->original_items||
                   birth->original_length!=q->original_length||birth->array_alias!=q->array_alias)return -1;
            }
            for(uint64_t j=0;j<q->count;j++)
                if(v->text[q->first+j].field!=q->field||v->text[q->first+j].ordinal!=j)return -1;
        }
    } else if(c->configuration_owned||v->clear_attempted)return -1;
    if(c->builtin_attempted) {
        if(!c->builtin_copy_complete||!c->builtin_count||
           c->builtin_count>FRIDAY_ROOT_BUILTIN_ROWS||
           c->builtin_name_bytes>FRIDAY_ROOT_BUILTIN_NAMES)return -1;
        uint64_t at=0;
        for(uint64_t i=0;i<c->builtin_count;i++) {
            uint64_t now;if(final_caller_clock(h,&now)<0)return -1;
            if(at>=c->builtin_name_bytes||c->builtin_entries[i].name!=c->builtin_names+at||
               !c->builtin_entries[i].initfunc)return -1;
            size_t room=(size_t)(c->builtin_name_bytes-at);
            size_t n=strnlen(c->builtin_names+at,room);
            if(n==room)return -1;
            at+=(uint64_t)n+1;
        }
        if(at!=c->builtin_name_bytes||c->builtin_entries[c->builtin_count].name||
           c->builtin_entries[c->builtin_count].initfunc)return -1;
    }
    /* No reading freed PyConfig strings,old arrays,runtime table or mmap.
     * Complete retained bodies and actual phase-specific close observations
     * remain the evidence. The full cold prefix is consumed below. */
    return 0;
}
static int final_caller_credits(FridayPublisherRootFinalHandoff *h) {
    FridayPublisherRootStorage *s=&root_storage;
    FridayPublisherMasterPool *p=&s->pool;
    /* The actual full pre-settlement pool was received by the utility phase
     * and read again in the full native body. Preserve that immutable history
     * while changing the real remaining rows. No counts-only ACK/replay. */
    if(!s->utility_result.sealed||!s->utility_result.copy_complete||
       !s->utility_result.resource_bounds_confirmed||p->native_live_slots||
       p->count>FRIDAY_MASTER_HISTORY||
       memcmp(p,&s->utility_result.pool_snapshot,sizeof(*p)))return -1;
    RootPoolTotals indexed,actual={0};
    RootFinalCallerBinding *binding=&final_caller_binding;
    uint64_t scan=8ULL*p->count*sizeof(FridayPublisherPoolRow)+4096ULL;
    if(master_totals(p,&indexed)<0||!binding->credit_reserved||
       binding->read_used>binding->read_credit||
       scan>binding->read_credit-binding->read_used)return -1;
    /* BOTH the scalar join and subsequent actual row settlement are covered
     * by this constructor-owned allowance. This is prospective work debit,
     * not measured IO and not a fresh MasterBefore after runtime/FD end. */
    binding->read_used+=scan;actual.rows=p->count;
    h->original_pool_history_received=1;
    uint64_t retained=p->retained_allocation;
    for(uint64_t i=0;i<p->count;i++) {
        if((i&1023)==0){uint64_t now;if(final_caller_clock(h,&now)<0)return -1;}
        FridayPublisherPoolRow *q=&p->rows[i];
        if(q->token!=i+1||q->token>=p->next_token||
           (q->active!=0&&q->active!=1)||(q->transferred!=0&&q->transferred!=1)||
           (q->source_owned!=0&&q->source_owned!=1)||
           (q->active&&q->transferred)||(!q->active&&!q->transferred))return -1;
        if(pool_totals_add(&actual,q)<0)return -1;
        if(q->active&&plus(&retained,q->allocation)<0)return -1;
        h->credit_rows_checked++;
    }
    if(!pool_totals_equal(&actual,&indexed))return -1;
    uint64_t ram=p->native_allocation;
    if(plus(&ram,retained)<0||plus(&ram,p->observed_ram)<0||ram>RAM_CAP)return -1;
    /* All original resource endpoints/Source references already ended and
     * the actual recipient has the full rows. Settle each remaining original
     * credit once. Historical widths/tokens/spent IO are never zeroed/refunded.
     * Pending RAM becomes retained RAM even though no new bank is allocated. */
    h->credit_settlement_attempted=1;
    for(uint64_t i=0;i<p->count;i++) {
        if((i&1023)==0){uint64_t now;if(final_caller_clock(h,&now)<0)return -1;}
        FridayPublisherPoolRow *q=&p->rows[i];
        if(!q->active)continue;
        uint64_t allocation=q->allocation;
        FridayPublisherPoolRow next;memcpy(&next,q,sizeof(next));
        next.active=0;next.transferred=1;
        if(pool_apply(p,q,&next,NULL,NULL,0)<0)return -1;
        p->retained_allocation+=allocation; /* prechecked above */
        h->credit_rows_transferred++;
    }
    if(p->retained_allocation!=retained||master_totals(p,&indexed)<0||
       indexed.active_rows||indexed.source_rows||indexed.reads||indexed.output||
       indexed.hash||indexed.allocation||indexed.slots)return -1;
    p->source_detached=1;h->credit_settlement_complete=1;return 0;
}
int FridayPublisherRootFinalReceive(FridayPublisherRootColdResult *caller) {
    if(!caller||!FridayPublisherRootBankAttached())return -1;
    FridayPublisherRootFinalHandoff *h=&caller->final_handoff;
    if(h->attempted)return h->sealed?0:-1;
    h->schema=250;h->bytes=sizeof(*h);h->pid=getpid();h->attempted=1;
    h->phase="actual_prebound_cold_native_receiver";h->original_owner_retained=1;
    RootFinalCallerBinding *b=&final_caller_binding;
    FridayPublisherRootStorage *s=&root_storage;
    if(b->caller!=caller||b->pid!=getpid()||caller->owner_pid!=getpid()||
       !s->cold_pool_started||caller->pool!=&s->pool||b->transferred) {
        h->phase="no_actual_prebound_Root_owner_no_transfer";goto sealed;
    }
    h->original_read_credit=b->read_credit;
    if(caller->completion_clock_failed) {
        /* The existing cold failure is retained in the caller's full record.
         * Do not invent a new clock result or recover an uncertain contour. */
        h->clock_failed=1;h->clock_fault_kind=4;
        h->phase="actual_cold_clock_failure_original_owner_retained";goto sealed;
    }
    if(s->utility_result.clock_failed) {
        h->clock_failed=1;h->clock_fault_kind=5;
        h->phase="actual_utility_clock_failure_original_owner_retained";goto sealed;
    }
    if(!b->credit_reserved||final_caller_clock(h,&h->started_ns)<0) {
        h->phase="original_caller_credit_or_finite_clock_UNCONFIRMED";goto sealed;
    }
    if(s->pool.initial_clock.phase==FRIDAY_POOL_CLOCK_WORK_STOPPED) {
        /* FULL numeric DATA only. No utility acquisition/observation, runtime
         * finalizer, success-only credit settlement or fabricated END receipt. */
        if(!FridayPublisherRootInitialWorkData(caller)||!caller->completion_returned) {
            h->phase="initial_WORK_original_prefix_or_completion_UNCONFIRMED";goto sealed;
        }
        h->native_body=s;h->native_body_bytes=sizeof(*s);
        h->cold_body=caller;h->cold_body_bytes=offsetof(FridayPublisherRootColdResult,final_handoff);
        if(final_caller_read(h,s,sizeof(*s),&h->full_native_bytes)<0||
           final_caller_read(h,caller,h->cold_body_bytes,&h->full_cold_bytes)<0) {
            h->phase="initial_WORK_full_original_data_or_clock_UNCONFIRMED";goto sealed;
        }
        h->native_read_complete=h->full_native_bytes==sizeof(*s);
        h->cold_read_complete=h->full_cold_bytes==h->cold_body_bytes;
        h->clock_complete=final_caller_clock(h,&h->finished_ns)==0;
        h->cold_data_complete=h->native_read_complete&&h->cold_read_complete&&h->clock_complete;
        h->source_data_complete=h->cold_data_complete&&!caller->perform_attempted&&!s->attempted;
        h->phase="initial_WORK_full_data_only_support_NOT_ACQUIRED";
        goto sealed; /* original owner remains until the actual outer end */
    }
    /* Reserve the complete bounded structural validation scan BEFORE its
     * first read. Edge ranges are a disjoint partition; each numeric edge
     * is visited at most twice,never nodes times the whole edge capacity. */
    uint64_t validation=2ULL*sizeof(*s)+2ULL*offsetof(FridayPublisherRootColdResult,final_handoff)+
        3ULL*(FRIDAY_ROOT_CASE_INPUT_MAX+1ULL); /* retained NUL scan and both comparison operands */
    if(b->read_used>b->read_credit||validation>b->read_credit-b->read_used) {
        h->phase="original_caller_validation_credit_UNCONFIRMED";goto sealed;
    }
    b->read_used+=validation;
    h->native_body=s;h->native_body_bytes=sizeof(*s);
    h->cold_body=caller;h->cold_body_bytes=offsetof(FridayPublisherRootColdResult,final_handoff);
    h->source_receipt=s->attempted?&s->command_receipt:NULL;
    h->utility_receipt=&s->utility_result;
    if(s->attempted) {
        if(caller->receipt!=h->source_receipt||final_caller_graph(h,h->source_receipt)<0) {
            h->phase="actual_required_Source_graph_or_retirement_UNCONFIRMED";goto read_held;
        }
        h->graph_checked=1;h->source_data_complete=1;
        h->source_owner_end=h->source_receipt->source_owner_end_confirmed;
    } else {
        /* Before RootPerform no Source/native factories ran. This is an
         * actual reached prefix,not an invented empty terminal receipt. */
        h->source_data_complete=!caller->perform_attempted&&
            !s->held_count&&!s->prepared_count&&!s->image_inventory.attempted&&
            !s->bootstrap.attempted&&!s->initialized;
        h->source_owner_end=h->source_data_complete;
    }
    h->cold_data_complete=final_caller_cold(h,caller)==0;
    h->config_owner_end=caller->config_end_confirmed&&!caller->configuration_owned;
    h->runtime_owner_end=FridayPublisherRootColdRuntimeEndConfirmed(caller);
    if(caller->builtin_installed||
       (caller->builtin_copy_complete&&!caller->builtin_borrow_end_confirmed))
        h->runtime_owner_end=0;
    h->utility_owner_end=caller->utility_receipt==h->utility_receipt&&
        h->utility_receipt->sealed&&h->utility_receipt->complete&&
        h->utility_receipt->last_consumer_confirmed&&h->utility_receipt->utility_FD_end_confirmed;
read_held:
    /* No pointer/digest/count-only handoff: read every actual private native
     * byte and cold body. The native value bank is inline in this storage.
     * Unrepresented Python values remain explicitly original-owner-held.
     * Receiver metadata itself is outside its immutable input prefix. */
    if(final_caller_read(h,s,sizeof(*s),&h->full_native_bytes)<0||
       final_caller_read(h,caller,h->cold_body_bytes,&h->full_cold_bytes)<0) {
        h->phase="full_actual_caller_body_or_clock_UNCONFIRMED";goto sealed;
    }
    h->native_read_complete=h->full_native_bytes==sizeof(*s);
    h->cold_read_complete=h->full_cold_bytes==h->cold_body_bytes;
    h->clock_complete=final_caller_clock(h,&h->finished_ns)==0;
    int eligible=h->source_data_complete&&h->cold_data_complete&&
        h->native_read_complete&&h->cold_read_complete&&h->clock_complete&&
        h->source_owner_end&&h->config_owner_end&&h->runtime_owner_end&&h->utility_owner_end;
    if(eligible) {
        if(final_caller_credits(h)<0) {
            h->phase="actual_original_credit_settlement_UNCONFIRMED";goto sealed;
        }
        /* Reconsume the changed actual final pool,not its earlier snapshot.
         * The original constructor reserved this second native scan too. */
        if(final_caller_read(h,s,sizeof(*s),&h->post_native_bytes)<0) {
            h->phase="actual_post_settlement_full_body_UNCONFIRMED";goto sealed;
        }
        h->post_native_read_complete=h->post_native_bytes==sizeof(*s);
        h->clock_complete=final_caller_clock(h,&h->finished_ns)==0;
        h->command_owner_end_confirmed=h->post_native_read_complete&&h->clock_complete&&
            h->original_pool_history_received&&h->credit_settlement_complete;
    }
    if(h->command_owner_end_confirmed) {
        /* MOVE ownership of the unchanged full native bank to the prebound
         * SAME cold caller. Do not overwrite a sealed receipt or drop its
         * historical bytes. Late old-master mutations are rejected. No new
         * process,role,pool,wire format or lifetime reset is introduced. */
        b->transferred=1;h->producer_transfer_confirmed=1;h->original_owner_retained=0;
        h->phase="actual_complete_native_bank_received_by_prebound_cold_caller";
    } else h->phase="actual_full_available_native_data_original_owner_retained";
sealed:
    h->read_credit_used=b->read_used;h->sealed=1;
    /* This local source-level transfer is NOT independent acceptance,actual
     * image/entry admission,required-case fit,SourceReady,Root admission or GO.
     * The outer invocation/final evidence consumer must bind this exact new
     * final record,not promote old false phase fields or an exit status. */
    return 0;
}

/* Native image traversal has the SAME positive semantics as bounded_tree_paths:
 * directories + regular files + symlinks counted, symlinks NEVER followed.
 * No 700/600/nlink restriction is invented for image directories/symlinks.
 * Every ancestor/descendant acquisition and actual close is preowned here. */
static int image_fault(RootImageState *i,const char *phase,int error) {
    if(!i->first_phase){i->first_phase=phase;i->first_errno=error;}
    return FridayPublisherMasterFault(&root_storage.pool,phase);
}
static int image_fd_cell(RootImageState *i) {
    if(i->fd_count>=FRIDAY_ROOT_HELD_FILES||!slots_free(2))
        return image_fault(i,"image_native_history_or_live_slots",0);
    uint64_t generation=take_generation();
    if(!generation)return image_fault(i,"image_generation_clock",0);
    int at=(int)i->fd_count++;RootImageFD *f=&i->fds[at];
    memset(f,0,sizeof(*f));f->fd=-1;f->keeper=-1;f->generation=generation;return at;
}
static int image_fd_birth(RootImageState *i,int at,int fd) {
    RootImageFD *f=&i->fds[at];f->fd=fd;
    if(fd<0)return image_fault(i,"image_native_open",errno);
    f->acquired=1;
    if(!slots_free(1))return image_fault(i,"image_native_birth_slots",EMFILE);
    root_storage.pool.native_live_slots++;f->live_body=1;
    if(bind_generation(f->generation,fd,&f->keeper,
        &f->keeper_open_attempted,&f->keeper_open_rc,&f->keeper_open_errno,
        &f->birth_stat_attempted,&f->birth_stat_rc,&f->birth_stat_errno,
        &f->birth,&f->body_close,&f->live_keeper)<0) {
        f->stat_errno=f->birth_stat_errno?f->birth_stat_errno:f->keeper_open_errno;
        return image_fault(i,"image_native_birth_stat",f->stat_errno);
    }
    f->last=f->birth;return 0;
}
static int image_close(RootImageState *i,int at) {
    if(at<0||at>=(int)i->fd_count)return -1;RootImageFD *f=&i->fds[at];
    if(!f->acquired)return 0;
    if(f->body_close.closed&&f->keeper_close.closed)return 0;
    if(!f->body_close.birth_valid) {
        f->close_validation_attempted=1;
        return image_fault(i,"image_FD_custody_UNKNOWN",0);
    }
    int rc=close_generation(&f->body_close,&f->keeper_close,&f->live_body,&f->live_keeper);
    f->close_attempted=f->body_close.attempted;f->close_rc=f->body_close.rc;
    f->close_errno=f->body_close.original_errno;f->closed=f->body_close.closed;
    return rc<0?image_fault(i,"image_close_UNCONFIRMED",f->close_errno):0;
}
static PyObject *image_member_named(const char *path,int *index) {
    PyObject *members=field(field(root_storage.admission,"image"),"members"),*found=NULL;
    for(Py_ssize_t at=0;at<PyList_Size(members);at++) {
        PyObject *pin=PyList_GetItem(members,at);const char *name=PyUnicode_AsUTF8(field(pin,"path"));
        if(!name)return NULL;
        if(!strcmp(name,path)){if(found)return NULL;found=pin;if(index)*index=(int)at;}
    }return found;
}
static int image_absolute_root(RootImageState *i,const char *path) {
    int at=image_fd_cell(i);if(at<0)return -1;
    int fd=open("/",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
    if(image_fd_birth(i,at,fd)<0)return -1;
    char copy[4097];strcpy(copy,path+1);char *component=copy;
    while(component&&*component) {
        char *slash=strchr(component,'/');if(slash)*slash=0;
        if(!*component||!strcmp(component,".")||!strcmp(component,".."))return image_fault(i,"image_root_path_shape",0);
        struct stat before,after;
        if(fstat(i->fds[at].fd,&before)<0||!S_ISDIR(before.st_mode))return image_fault(i,"image_root_parent",errno);
        int next=image_fd_cell(i);if(next<0)return -1;
        fd=openat(i->fds[at].fd,component,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        int born=image_fd_birth(i,next,fd);
        int stable=fstat(i->fds[at].fd,&after)==0&&stat_equal(&before,&after,1);
        int closed=image_close(i,at);
        if(born<0||closed<0||!stable)return image_fault(i,"image_root_parent_drift",errno);
        at=next;component=slash?slash+1:NULL;
    }return at;
}
static int image_relative_open(RootImageState *i,const char *relative,int flags) {
    const char *root=PyUnicode_AsUTF8(field(field(root_storage.admission,"image"),"path"));
    if(!root||!relative||!*relative||strlen(relative)>4096)return -1;
    int parent=i->root_index,owned_parent=-1;char copy[4097],full[4097];
    strcpy(copy,relative);size_t prefix=strlen(root);if(prefix>=4096)return -1;
    strcpy(full,root);char *component=copy;
    while(component&&*component) {
        char *slash=strchr(component,'/');if(slash)*slash=0;
        if(!*component||!strcmp(component,".")||!strcmp(component,".."))return image_fault(i,"image_member_relative_path",0);
        size_t length=strlen(component);if(prefix+1+length>4096)return image_fault(i,"image_member_path_width",0);
        full[prefix++]='/';memcpy(full+prefix,component,length+1);prefix+=length;
        struct stat before,after;
        if(fstat(i->fds[parent].fd,&before)<0||!S_ISDIR(before.st_mode))return image_fault(i,"image_member_parent_custody",errno);
        int child=image_fd_cell(i);if(child<0)return -1;
        int fd=openat(i->fds[parent].fd,component,(slash?O_RDONLY|O_DIRECTORY:flags)|O_NOFOLLOW|O_CLOEXEC);
        int born=image_fd_birth(i,child,fd);
        int stable=fstat(i->fds[parent].fd,&after)==0&&stat_equal(&before,&after,1);
        if(owned_parent>=0&&image_close(i,owned_parent)<0)return -1;
        if(born<0||!stable)return image_fault(i,"image_member_parent_drift",errno);
        PyObject *pin=image_member_named(full,NULL);
        if(!pin||!stat_pin(&i->fds[child].birth,pin)||
           (slash&&(ascii(field(pin,"kind"),"regular")||!S_ISDIR(i->fds[child].birth.st_mode))))
            return image_fault(i,"image_signed_ancestor_identity",0);
        parent=child;owned_parent=child;component=slash?slash+1:NULL;
    }return parent;
}
static int image_lstat_member(RootImageState *i,const char *relative,struct stat *named) {
    if(!relative||!*relative||strlen(relative)>4096)return -1;
    char copy[4097];strcpy(copy,relative);char *slash=strrchr(copy,'/');
    int parent=i->root_index,temporary=-1;const char *leaf=copy;
    if(slash) {
        *slash=0;leaf=slash+1;temporary=image_relative_open(i,copy,O_RDONLY|O_DIRECTORY);
        if(temporary<0)return -1;parent=temporary;
    }
    if(!*leaf||!strcmp(leaf,".")||!strcmp(leaf,".."))return image_fault(i,"image_leaf_shape",0);
    struct stat before,after;
    int valid=fstat(i->fds[parent].fd,&before)==0&&
        fstatat(i->fds[parent].fd,leaf,named,AT_SYMLINK_NOFOLLOW)==0&&
        fstat(i->fds[parent].fd,&after)==0&&stat_equal(&before,&after,1);
    if(temporary>=0&&image_close(i,temporary)<0)return -1;
    return valid?0:image_fault(i,"image_leaf_parent_drift",errno);
}

static int image_hash_regular(RootImageState *i,PyObject *pin,const char *relative) {
    uint64_t width;if(scalar_u64(field(pin,"bytes"),&width)<0||width>BODY_CAP||
        root_storage.held_count>=FRIDAY_ROOT_HELD_FILES)return image_fault(i,"image_regular_full_width",0);
    if(FridayPublisherMasterBefore(&root_storage.pool,width,0,width,width+8192)<0)return -1;
    RootHeldFile *h=&root_storage.held[root_storage.held_count++];
    memset(h,0,sizeof(*h));h->fd=-1;h->keeper=-1;h->pin=Py_NewRef(pin);
    h->raw=PyBytes_FromStringAndSize(NULL,(Py_ssize_t)width);if(!h->raw)return -1;
    memset(PyBytes_AS_STRING(h->raw),0,(size_t)width);
    int at=image_relative_open(i,relative,O_RDONLY);if(at<0)return -1;
    RootImageFD *f=&i->fds[at];h->fd=f->fd;h->birth=f->birth;
    h->close_alias=1;h->alias_kind=2;h->alias_generation=f->generation;
    h->generation=f->generation;h->keeper=f->keeper;
    if(!S_ISREG(f->birth.st_mode)||(uint64_t)f->birth.st_size!=width)return image_fault(i,"image_regular_shape",0);
    uint64_t used=0;
    while(used<width) {
        size_t amount=width-used>65536?65536:(size_t)(width-used);
        ssize_t got=pread(f->fd,PyBytes_AsString(h->raw)+used,amount,used);
        if(got<=0) {
            h->read_used=used;h->read_completed=0;h->read_error=got<0?errno:0;
            return image_fault(i,"image_full_regular_read",got<0?errno:0);
        }
        used+=got;h->read_used=used;i->member_bytes+=got;
    }
    h->read_completed=1;h->read_error=0;
    PyObject *digest=sha_bytes(h->raw);struct stat after,named;
    const char *path=PyUnicode_AsUTF8(field(pin,"path"));
    int valid=digest&&eq(digest,field(pin,"sha256"))&&fstat(f->fd,&after)==0&&
        path&&image_lstat_member(i,relative,&named)==0&&
        stat_equal(&f->birth,&after,1)&&stat_equal(&f->birth,&named,1);
    Py_XDECREF(digest);i->hash_bytes+=width;
    if(!valid)return image_fault(i,"image_regular_full_SHA9_drift",0);
    int rc=image_close(i,at);
    h->close_attempted=f->close_attempted;h->close_rc=f->close_rc;
    h->close_errno=f->close_errno;h->closed=f->closed;
    h->body_close=f->body_close;h->keeper_close=f->keeper_close;
    if(rc<0)return -1;return 0;
}
static int image_walk_directory(RootImageState *i,const char *relative) {
    int at=*relative?image_relative_open(i,relative,O_RDONLY|O_DIRECTORY):i->root_index;
    if(at<0)return -1;RootImageFD *f=&i->fds[at];
    struct stat before,after;if(fstat(f->fd,&before)<0)return image_fault(i,"image_directory_pre",errno);
    const char *root=PyUnicode_AsUTF8(field(field(root_storage.admission,"image"),"path"));
    if(!root)return -1;
#ifdef SYS_getdents64
    for(;;) {
        if(FridayPublisherMasterBefore(&root_storage.pool,sizeof(i->directory_raw),0,0,0)<0)return -1;
        ssize_t got=syscall(SYS_getdents64,f->fd,i->directory_raw,sizeof(i->directory_raw));
        if(got<0)return image_fault(i,"image_directory_read",errno);if(!got)break;
        i->raw_directory_bytes+=got;size_t pos=0;
        while(pos<(size_t)got) {
            struct RootNativeDirent *de=(void *)(i->directory_raw+pos);
            size_t header=offsetof(struct RootNativeDirent,name);
            if(de->reclen<header+2||de->reclen>(size_t)got-pos||
               !memchr(de->name,0,de->reclen-header))return image_fault(i,"image_native_dirent_shape",0);
            pos+=de->reclen;
            if(!strcmp(de->name,".")||!strcmp(de->name,".."))continue;
            if(!de->name[0]||strchr(de->name,'/'))return image_fault(i,"image_directory_name",0);
            if(i->path_count>=512)return image_fault(i,"complete_image_inventory",0);
            char path[4097],rel[4097];
            int rn=snprintf(rel,sizeof(rel),"%s%s%s",relative,*relative?"/":"",de->name);
            int pn=snprintf(path,sizeof(path),"%s/%s",root,rel);
            if(rn<0||rn>4096||pn<0||pn>4096)return image_fault(i,"image_descendant_path_width",0);
            int index=-1;PyObject *pin=image_member_named(path,&index);struct stat st;
            if(!pin||index<0||i->seen[index]||fstatat(f->fd,de->name,&st,AT_SYMLINK_NOFOLLOW)<0||
               !stat_pin(&st,pin))return image_fault(i,"image_unlisted_duplicate_or_identity",errno);
            int regular=S_ISREG(st.st_mode),directory=S_ISDIR(st.st_mode),symlink=S_ISLNK(st.st_mode);
            if((regular&&!ascii(field(pin,"kind"),"regular"))||
               ((directory||symlink)&&ascii(field(pin,"kind"),"regular"))||(!regular&&!directory&&!symlink))
                return image_fault(i,"image_unsupported_or_wrong_kind",0);
            i->seen[index]=1;i->identities[index]=st;i->path_count++;
            if(directory) {
                if(i->queue_count>=513)return image_fault(i,"image_directory_queue_bound",0);
                strcpy(i->queued[i->queue_count++],rel);
            }
            /* Symlink is recorded by lstat and NEVER opened/followed. */
        }
    }
#else
    return image_fault(i,"image_native_getdents_ABI_NOT_PRESENT",0);
#endif
    if(fstat(f->fd,&after)<0||!stat_equal(&before,&after,1))return image_fault(i,"image_directory_post_drift",errno);
    if(at!=i->root_index&&image_close(i,at)<0)return -1;return 0;
}
static int readonly_image_native(void) {
    FridayPublisherRootStorage *s=&root_storage;RootImageState *i=&s->image_inventory;
    PyObject *image=field(s->admission,"image"),*members=field(image,"members");
    if(i->attempted)return image_fault(i,"image_once",0);i->attempted=1;i->root_index=-1;
    const char *root=PyUnicode_AsUTF8(field(image,"path"));
    if(!root||root[0]!='/'||strlen(root)>4096||strstr(root,"//")||strlen(root)<2||
       root[strlen(root)-1]=='/'||!PyList_CheckExact(members)||PyList_Size(members)>512)
        return image_fault(i,"image_shape",0);
    /* Static arrays already belong to sizeof(RootStorage); dependency/import/
     * kernel/provider hidden allocations still require actual qualification. */
    if(FridayPublisherMasterBefore(&s->pool,INPUT_CAP,0,0,INPUT_CAP*2+131072)<0)goto finished;
    i->root_index=image_absolute_root(i,root);if(i->root_index<0)goto finished;
    i->root_before=i->fds[i->root_index].birth;
    PyObject *rootnine=stat9(&i->root_before);
    int same=rootnine&&eq(rootnine,field(image,"root_identity9"));Py_XDECREF(rootnine);
    if(!same){image_fault(i,"image_root_identity",0);goto finished;}
    /* Mount lookup retains actual full mount bytes. Opening it through this
     * SAME tracked no-follow path also prevents an unowned sampler descriptor. */
    char own_proc[80];snprintf(own_proc,sizeof(own_proc),"/proc/%ld",(long)getpid());
    int mount=image_absolute_root(i,own_proc); /* actual own numeric proc DIRECTORY */
    if(mount<0)goto finished;
    int mi=image_fd_cell(i);if(mi<0)goto finished;
    struct stat mount_parent_before,mount_parent_after;
    if(fstat(i->fds[mount].fd,&mount_parent_before)<0)goto finished;
    int mfd=openat(i->fds[mount].fd,"mountinfo",O_RDONLY|O_NOFOLLOW|O_CLOEXEC);
    /* Own numeric PID selects the SAME /proc mountinfo bytes, without the
     * /proc/self symlink rejected by the canonical generic no-follow helper.
     * Actual Source self-route remains a precise Astra integration obligation. */
    if(image_fd_birth(i,mi,mfd)<0||fstat(i->fds[mount].fd,&mount_parent_after)<0||
       !stat_equal(&mount_parent_before,&mount_parent_after,1))goto finished;
    i->mountinfo_raw=PyBytes_FromStringAndSize(NULL,INPUT_CAP+1);
    PyObject *raw=i->mountinfo_raw;if(!raw)goto finished;
    memset(PyBytes_AsString(raw),0,INPUT_CAP+1);
    ssize_t n=read(mfd,PyBytes_AsString(raw),INPUT_CAP+1);
    if(n<0||n>INPUT_CAP){image_fault(i,"image_mountinfo_bound",errno);goto finished;}
    int found=0;const char *begin=PyBytes_AsString(raw),*end=begin+n,*line=begin;
    while(line<end) {
        const char *finish=memchr(line,'\n',end-line);if(!finish)finish=end;
        const char *at=line;
        for(unsigned k=0;k<4;k++){const char *space=memchr(at,' ',finish-at);if(!space){at=NULL;break;}at=space+1;}
        if(at) {
            const char *stop=memchr(at,' ',finish-at);
            if(stop&&(size_t)(stop-at)==strlen(root)&&!memcmp(at,root,strlen(root))) {
                const char *options=stop+1,*last=memchr(options,' ',finish-options);int ro=0;
                if(!last){image_fault(i,"image_mountinfo_shape",0);goto finished;}
                while(options<last) {
                    const char *comma=memchr(options,',',last-options);if(!comma)comma=last;
                    if(comma-options==2&&!memcmp(options,"ro",2))ro=1;options=comma<last?comma+1:last;
                }
                if(!ro){image_fault(i,"image_not_independently_read_only",0);goto finished;}found++;
            }
        }
        line=finish<end?finish+1:end;
    }
    if(image_close(i,mi)<0||image_close(i,mount)<0)goto finished;
    if(found!=1){image_fault(i,"image_unique_readonly_mount",0);goto finished;}
    for(Py_ssize_t index=0;index<PyList_Size(members);index++) {
        PyObject *pin=PyList_GetItem(members,index);const char *path=PyUnicode_AsUTF8(field(pin,"path"));
        if(!path||strncmp(path,root,strlen(root))||path[strlen(root)]!='/'||strlen(path)>4096||
           strstr(path,"//")||strstr(path,"/../")||strstr(path,"/./")||path[strlen(path)-1]=='/')
            {image_fault(i,"image_signed_member_path",0);goto finished;}
        for(Py_ssize_t prior=0;prior<index;prior++)
            if(eq(field(pin,"path"),field(PyList_GetItem(members,prior),"path")))
                {image_fault(i,"image_duplicate_signed_member",0);goto finished;}
    }
    strcpy(i->queued[0],"");i->queue_count=1;
    while(i->queue_at<i->queue_count) {
        const char *relative=i->queued[i->queue_at++];
        if(image_walk_directory(i,relative)<0)goto finished;
    }
    if(i->path_count!=(uint64_t)PyList_Size(members)){image_fault(i,"image_missing_descendant",0);goto finished;}
    for(Py_ssize_t at=0;at<PyList_Size(members);at++) {
        PyObject *pin=PyList_GetItem(members,at);const char *path=PyUnicode_AsUTF8(field(pin,"path"));
        if(!i->seen[at]){image_fault(i,"image_missing_member",0);goto finished;}
        const char *relative=path+strlen(root)+1;struct stat final;
        if(ascii(field(pin,"kind"),"regular")&&image_hash_regular(i,pin,relative)<0)goto finished;
        if(image_lstat_member(i,relative,&final)<0||
           !stat_equal(&i->identities[at],&final,1)||!stat_pin(&final,pin))
            {image_fault(i,"image_final_member_drift",errno);goto finished;}
    }
    if(fstat(i->fds[i->root_index].fd,&i->root_after)<0||
       !stat_equal(&i->root_before,&i->root_after,1))
        {image_fault(i,"image_root_post_drift",errno);goto finished;}
    i->complete=1;
finished:
    for(uint64_t at=0;at<i->fd_count;at++)if(i->fds[at].acquired&&!i->fds[at].closed) {
        if(image_close(i,(int)at)<0)i->complete=0;
    }
    return i->complete?0:-1;
}
