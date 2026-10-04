/* Source text only. Not compiled or loaded by A141.
 * Built into the independently selected static CPython image. The issuer owns
 * the sealed caps description on fd 5; Source JSON has no activation API.
 * All Python allocator domains and selected native malloc/calloc/realloc/free
 * relocation edges use this arena BEFORE Py_PreInitialize. No estimate, id()
 * observation or post-allocation sizeof() establishes the physical owner.
 */
#define _GNU_SOURCE
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <linux/sched.h>
#include <linux/futex.h>
#include <stdatomic.h>
#include <limits.h>
#include <stdarg.h>
#include <sys/epoll.h>
#include <sys/file.h>
#include <poll.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <signal.h>
#include <pthread.h>
#include <time.h>
#include <unistd.h>
#include "scanner_selected_parent.h"

#define ALIGNMENT ((size_t)_Alignof(max_align_t))
#define FD_MAX 4096 /* fixed audit table, NOT a role grant; caps.max_fds is enforced */
#define CONTROL_BYTES 56
#define OWNER_MAGIC UINT64_C(0x4652494441594131)
#define METRIC_MAGIC UINT64_C(0x4652494441594d31)
_Static_assert(ATOMIC_INT_LOCK_FREE==2,"original native signal/task word must be lock free");

/* Fixed ABI. Independent original role admission must bind these exact bytes,
 * all native allocation edges, kernel/loader/API contracts and the held entry
 * descriptor. F_SEAL_* proves custody only, never permission or issuer status.
 */
struct owner_caps {
    uint64_t magic,version,role,canonical_workers,max_fds;
    uint64_t max_live_bytes,max_work_bytes,max_read_bytes,max_output_bytes;
    uint64_t max_wall_ms,max_rss_bytes,terminal_live_bytes,terminal_work_bytes;
    uint64_t terminal_output_bytes;
    unsigned char binding_sha256[32];
    uint64_t module_path_count;
    char runtime_home[4096],module_paths[4][4096];
};
struct block {
    size_t capacity,requested;
    unsigned active;
    struct block *next;
    max_align_t alignment;
};
struct arena {
    unsigned char *base;
    size_t size,used,live,peak;
    uint64_t work;
    struct block *first,*last;
};
struct fd_slot {int fd,active,uncertain,close_errno;uint64_t generation;};
static struct owner_caps caps;
static struct arena ordinary_storage,terminal_storage;
static struct arena *ordinary_owner=&ordinary_storage,*terminal_owner=&terminal_storage;
#define ordinary (*ordinary_owner)
#define terminal (*terminal_owner)
static struct fd_slot fd_slot_storage[FD_MAX];
static struct fd_slot *fd_slots=fd_slot_storage;
static uint64_t fd_generation=0;
static uint64_t actual_read,actual_output,started_ns;
static int initialized,failed,in_terminal;
static long owner_pid;
struct scanner_child_registry {
    int source_child,source_pidfd,source_born,source_inflight,source_slot;
    uint64_t source_started,source_wall;
    int source_reaped,source_status;
    struct rusage source_usage;
    int root_child,root_pidfd,root_reaped,root_status,root_relation_verified;
    uint64_t root_generation;
    struct rusage root_usage;
    int cancel_attempted,cancel_errno,source_retirement_error,root_retirement_error;
};
static struct scanner_child_registry scanner_child_storage={
    .source_child=-1,.source_pidfd=-1,.source_slot=-1,.root_child=-1,.root_pidfd=-1};
static struct scanner_child_registry *scanner_child_owner=&scanner_child_storage;
#define pending_child (scanner_child_owner->source_child)
#define pending_pidfd (scanner_child_owner->source_pidfd)
#define source_birth_committed (scanner_child_owner->source_born)
#define source_birth_inflight (scanner_child_owner->source_inflight)
#define source_birth_slot (scanner_child_owner->source_slot)
#define source_birth_started_ns (scanner_child_owner->source_started)
#define expected_source_wall_ms (scanner_child_owner->source_wall)
/* Exact native wait result survives a failed Python return allocation. It is
 * written only after the kernel actually consumes this owner's direct child.
 * No pidfd-open receipt or WNOHANG invocation sets a successful reap flag. */
#define pending_reaped (scanner_child_owner->source_reaped)
#define pending_wait_status (scanner_child_owner->source_status)
#define pending_usage (scanner_child_owner->source_usage)
#define observed_root_child (scanner_child_owner->root_child)
#define observed_root_pidfd (scanner_child_owner->root_pidfd)
#define observed_root_reaped (scanner_child_owner->root_reaped)
#define observed_root_status (scanner_child_owner->root_status)
#define observed_root_relation_verified (scanner_child_owner->root_relation_verified)
#define observed_root_usage (scanner_child_owner->root_usage)
/* Native scalar retirement origins belong to this original owner. No new FD,
 * process, retry authority or capacity domain is introduced. */
#define source_cancel_attempted (scanner_child_owner->cancel_attempted)
#define source_cancel_errno (scanner_child_owner->cancel_errno)
#define source_retirement_errno (scanner_child_owner->source_retirement_error)
#define root_retirement_errno (scanner_child_owner->root_retirement_error)
static uint64_t root_ledger[8];
static int root_completion_peer=-1;
/* This flag is set ONLY by the C entry in the already-existing independently
 * selected parent image, before ANY interpreter initialization. The bounded
 * role2 main cannot set it through JSON or a Python activation call. */
static int existing_parent_entry=0,parent_delivery_accepted=0;
static int existing_observer_entry=0;
static volatile sig_atomic_t parent_end_reached=0;
static uint64_t parent_generation=0;
static _Atomic uint64_t original_consumer_end_ns=0;
static int original_consumer_bound_by_caller=0;
static uint64_t original_preowned_started_ns=0,original_preowned_end_ns=0,original_preowned_generation=0;
#define observed_root_generation (scanner_child_owner->root_generation)
static PyObject *parent_custody_storage=NULL;
static PyObject **parent_custody_owner=&parent_custody_storage;
#define parent_custody (*parent_custody_owner)
static PyObject *parent_error_type_storage=NULL,*parent_error_value_storage=NULL,*parent_error_traceback_storage=NULL;
static PyObject **parent_type_owner=&parent_error_type_storage,**parent_value_owner=&parent_error_value_storage,**parent_traceback_owner=&parent_error_traceback_storage;
#define parent_error_type (*parent_type_owner)
#define parent_error_value (*parent_value_owner)
#define parent_error_traceback (*parent_traceback_owner)
static PyObject *parent_result_storage=NULL;
static PyObject **parent_result_owner=&parent_result_storage;
#define parent_result (*parent_result_owner)
/* Same ORIGINAL role/arena registry. No process, grant or private heap.
 * It owns partial Source raw factory/meter/result graphs before projection. */
static PyObject *source_raw_storage=NULL;
static PyObject **source_raw_owner=&source_raw_storage;
#define source_raw_root (*source_raw_owner)
static int parent_result_accepted=0,parent_native_consumer_entered=0;
static int parent_native_retired=0;
struct caller_error {PyObject *type,*value,*traceback;const char *operation;int error_number;};
static struct caller_error caller_error_storage[16];
static struct caller_error *caller_errors=caller_error_storage;
static unsigned caller_error_count_storage=0;
static unsigned *caller_count_owner=&caller_error_count_storage;
#define caller_error_count (*caller_count_owner)
static int caller_error_lane_exhausted_storage=0;
static int *caller_lane_owner=&caller_error_lane_exhausted_storage;
#define caller_error_lane_exhausted (*caller_lane_owner)
/* Fixed same-original-caller state, present before interpreter construction.
 * No separately allocated observer, process, service or capacity is introduced.
 * The native consumer below owns these complete graphs through real finalize.
 */
struct canonical_slot {
    int fd,prepared,acquired,unlock_attempted,unlock_uncertain,error_number;
    uint64_t generation;
    struct stat before_effect;
};
static struct canonical_slot canonical_slot_storage[4];
static struct canonical_slot *canonical_slots=canonical_slot_storage;
struct received_packet {
    struct received_packet *next;
    unsigned char *data;
    size_t capacity,control_used;
    ssize_t received;
    int fd,flags,error_number,contract_bad,projected,accepted_native;
    int delivered[10],right_slots[10],count;
    uint64_t right_generation[10];
    int right_close_attempted[10],right_close_errno[10],right_uncertain[10];
    struct ucred peer;
    union {struct cmsghdr align;unsigned char bytes[CONTROL_BYTES];} control;
    PyObject *error_type,*error_value,*error_traceback;
};
static struct received_packet *received_first_storage=NULL,*received_last_storage=NULL;
static struct received_packet **received_first_owner=&received_first_storage,**received_last_owner=&received_last_storage;
#define received_first (*received_first_owner)
#define received_last (*received_last_owner)
static uint64_t received_count_storage=0;
static uint64_t *received_count_owner=&received_count_storage;
#define received_count (*received_count_owner)
static int consume_native_packets(void);
static int retire_native_slots(void);
static pthread_mutex_t arena_lock=PTHREAD_MUTEX_INITIALIZER;
/* The ALREADY EXISTING native caller remains the physical Root process and
 * kernel-parent TGID. Only its in-process interpreter execution has a separate
 * stack. This is no new Root process/role or allowance. The caller prospectively
 * owns the full arenas, fixed FD/slot/child tables and this stack before birth;
 * actual Python/raw/partial objects never leave that original ownership. */
struct original_interpreter_execution {
    pthread_t thread;
    void *stack_storage;
    size_t stack_storage_bytes;
    _Atomic int clear_tid,started,finished,cutoff_accepted;
    _Atomic int thread_tid;
    int result,startup_errno;
    int (*selected_entry)(void);
    PyThreadState *raw_thread_state;
};
static struct original_interpreter_execution original_execution_storage;
static struct original_interpreter_execution *original_execution_owner=&original_execution_storage;
#define original_execution (*original_execution_owner)
static int native_cold_consumer_entered=0;
static int source_registry_retired=0,source_registry_still_owned=0,source_endpoint_entered=0;
static int selected_caller_returns=0,root_custody_returned_to_selected_caller=0;
/* Original canonical packet body already limited to this many bytes by the
 * sealed source contracts. The preheld window is that body plus the native
 * header, and only when both fit the sealed live and output ceilings.
 */
#define ORIGINAL_PACKET_BODY_LIMIT 2000000u
#define PLANE_NOTE_MAGIC UINT64_C(0x4652435553544f32)
#define SOURCE_BACKING_ABI UINT64_C(0x46524241434b3032)
enum { PLANE_ABSENT=0, PLANE_FULL_COMMIT=1, PLANE_SIGNAL_NOTE=2, PLANE_NOT_FIT=3, PLANE_CONFIRMED_RETIRED=4, PLANE_REVOKED=5 };
struct plane_note {
    uint64_t magic;
    uint32_t commit_kind;
    uint32_t python_alias_count;
    uint32_t python_bodies_byte_exported;
    uint32_t source_registry_still_owned;
    uint32_t outside_peer_adoption;
    uint32_t plane_not_fit;
    uint32_t reserved_note;
    uint32_t pad_note;
    uint64_t packet_bytes_retained;
    uint64_t omitted_packet_bytes;
    uint64_t fd_table_bytes;
    uint64_t canonical_bytes;
    int note_source_child, note_source_pidfd, note_source_reaped;
    int note_root_child, note_root_pidfd;
    int note_error_count, note_error_lane_exhausted;
    uint32_t pad_tail;
    uint64_t body_offset, body_capacity, body_used, table_offset, note_generation;
    uint32_t range_count, confirmed_revoked;
};
struct plane_body_range {
    uint64_t offset, length, generation;
    int32_t contract_bad, present;
};
#define FD_TABLE_BYTES (sizeof(fd_slot_storage))
#define CANONICAL_TABLE_BYTES (sizeof(canonical_slot_storage))
/* Physical Source data only: not a Root privileged heap, late byte export,
 * universal interpreter graph walker, or an additional allowance. These are
 * the same pre-initialization allocator and prescribed native root records.
 * No Source pointer is ever dereferenced by the parent. */
struct source_backing_custody {
    uint64_t abi, extent, generation, source_address;
    struct owner_caps source_caps;
    struct arena arena_ordinary,arena_terminal;
    PyObject *raw_source,*error_type,*error_value,*error_traceback;
    struct caller_error error_records[16];
    unsigned error_count;
    int error_lane_exhausted;
    struct received_packet *packet_first,*packet_last;
    uint64_t packet_count;
};
#define PLANE_TABLE_BYTES (sizeof(struct plane_note)+FD_TABLE_BYTES+CANONICAL_TABLE_BYTES)
#define PLANE_CUSTODY_OFFSET ((PLANE_TABLE_BYTES+ALIGNMENT-1)/ALIGNMENT*ALIGNMENT)
#define PLANE_HEADER_BYTES ((PLANE_CUSTODY_OFFSET+sizeof(struct source_backing_custody)+ALIGNMENT-1)/ALIGNMENT*ALIGNMENT)
static struct source_backing_custody *source_backing=NULL;
static size_t expected_source_extent=0,expected_source_terminal=0;
static uint64_t expected_source_generation=0,expected_source_output=0;
static unsigned char expected_source_binding[32];
static int source_backing_received=0;
static uint64_t source_backing_inspection_bytes=0;
_Static_assert(sizeof(struct plane_note)==152, "plane note layout");
_Static_assert(sizeof(struct fd_slot)==24, "fd slot layout");
_Static_assert(sizeof(struct plane_body_range)==32, "plane range layout");
_Static_assert(PLANE_HEADER_BYTES%8==0, "plane header alignment");
static struct plane_note source_plane_note;
static int source_plane_fd=-1;
static int registered_carrier_preowned=0;
static int registered_carrier_accepted_before_birth=0;
static int source_plane_readonly_accepted=0;
static volatile sig_atomic_t plane_full_commit=0;
static volatile sig_atomic_t carrier_signal_scalars_retained=0;
static int cold_python_aliases_not_accepted=0;
static struct fd_slot carrier_fd_storage[FD_MAX];
static struct canonical_slot carrier_canonical_storage[4];
static struct fd_slot *carrier_fd_alias=carrier_fd_storage;
static struct canonical_slot *carrier_canonical_alias=carrier_canonical_storage;
static PyObject *carrier_parent_storage=NULL,*carrier_raw_storage=NULL,*carrier_result_storage=NULL;
static PyObject **carrier_parent_owner=&carrier_parent_storage,**carrier_raw_owner=&carrier_raw_storage;
static PyObject **carrier_result_owner=&carrier_result_storage;
#define carrier_parent_alias (*carrier_parent_owner)
#define carrier_raw_alias (*carrier_raw_owner)
#define carrier_result_alias (*carrier_result_owner)
static PyObject *carrier_error_storage[16];
static PyObject **carrier_error_alias=carrier_error_storage;
static unsigned carrier_error_count_storage=0;
static unsigned *carrier_error_count_owner=&carrier_error_count_storage;
#define carrier_error_alias_count (*carrier_error_count_owner)
static unsigned char *plane_base=NULL,*plane_body=NULL;
static size_t plane_mapped_bytes=0,plane_body_capacity=0,plane_body_used=0;
static uint32_t plane_range_count=0;
static uint64_t plane_retained_bytes=0,plane_omitted_bytes=0,plane_publish_generation=0;
static int plane_placement_not_fit=0;
/* Root-only registered backing. The ORIGINAL privileged caller supplies and
 * retains its complete description before this generation enters preinit.
 * Source never receives either Root backing FD. Root body rows are allocator
 * bodies/actual aliases, not a scalar digest or a late serialized heap walk.
 * Metadata consumes the same sealed Root max_live extent as its two arenas.
 */
struct scanner_root_backing {
    uint64_t abi,generation,start_ns,end_ns,minimum_end_ns,root_address;
    size_t extent;
    struct owner_caps original_caps;
    struct arena root_ordinary,root_terminal;
    struct fd_slot root_fds[FD_MAX];
    struct canonical_slot root_slots[4];
    struct scanner_child_registry root_children;
    struct original_interpreter_execution root_execution;
    PyObject *root_parent,*root_result,*root_raw,*root_error_type,*root_error_value,*root_error_traceback;
    struct caller_error root_errors[16];
    unsigned root_error_count;
    int root_error_lane_exhausted;
    struct received_packet *root_packet_first,*root_packet_last;
    uint64_t root_packet_count;
    struct fd_slot carrier_fds[FD_MAX];
    struct canonical_slot carrier_slots[4];
    PyObject *carrier_parent,*carrier_raw,*carrier_result,*carrier_errors[16];
    unsigned carrier_error_count;
    PyObject *root_materials,*root_module,*root_function,*root_call_result;
    struct friday_scanner_parent_result parent_terminal_result;
};
#define SCANNER_ROOT_HEADER_BYTES ((sizeof(struct scanner_root_backing)+ALIGNMENT-1)/ALIGNMENT*ALIGNMENT)
static struct scanner_root_backing *scanner_root_backing=NULL;
static const struct friday_scanner_selected_enrollment *scanner_selected_enrollment=NULL;
static const unsigned char *scanner_root_readonly_view=NULL;
static const unsigned char *scanner_source_readonly_view=NULL;
static size_t scanner_preowned_source_extent=0;
static int scanner_preowned_source_fds[2]={-1,-1};
static unsigned char scanner_preowned_source_binding[32];
static struct friday_scanner_parent_result *scanner_final_result=NULL;
static int scanner_root_backing_fds[2]={-1,-1};
static int scanner_root_backing_preowned=0,scanner_final_receiver_entered=0;
static int scanner_original_guard_installed=0;
static int scanner_selected_root_callsite(void);
static int scanner_final_root_receiver(int status);
/* A fixed privileged EXEC trampoline is not Source execution. execveat first
 * removes EVERY inherited Root mapping, then this selected image's main drops
 * privilege before native Source initialization/Python/Source text. All eight
 * inherited descriptions at that point are already the original Source set.
 */
static int scanner_source_exec_trampoline=0;
static uint64_t source_trampoline_read_bytes=0;
static int source_caps_prechecked=0,original_execution_created=0;
static uint64_t scanner_preinit_read_bytes=0,scanner_preinit_work_bytes=0;
static size_t registered_header_bytes(void) {
    return scanner_root_backing_preowned?SCANNER_ROOT_HEADER_BYTES:
        (source_backing?PLANE_HEADER_BYTES:0);
}
static int receive_preowned_source_plane(void);
static int publish_source_plane(uint32_t kind);
static void place_received_packet_body(struct received_packet *record);

static void refresh_plane_note(uint32_t kind) {
    unsigned aliases=0;
    source_plane_note.magic=PLANE_NOTE_MAGIC;
    source_plane_note.commit_kind=kind;
    source_plane_note.python_bodies_byte_exported=0;
    source_plane_note.outside_peer_adoption=0;
    source_plane_note.source_registry_still_owned=(uint32_t)source_registry_still_owned;
    source_plane_note.note_source_child=pending_child;
    source_plane_note.note_source_pidfd=pending_pidfd;
    source_plane_note.note_source_reaped=pending_reaped;
    source_plane_note.note_root_child=observed_root_child;
    source_plane_note.note_root_pidfd=observed_root_pidfd;
    source_plane_note.note_error_count=(int)caller_error_count;
    source_plane_note.note_error_lane_exhausted=caller_error_lane_exhausted;
    source_plane_note.pad_note=0;
    source_plane_note.pad_tail=0;
    source_plane_note.reserved_note=0;
    source_plane_note.body_offset=PLANE_HEADER_BYTES;
    source_plane_note.body_capacity=plane_body_capacity;
    source_plane_note.body_used=source_backing?ordinary.used+terminal.used:plane_body_used;
    source_plane_note.table_offset=sizeof(struct plane_note);
    source_plane_note.fd_table_bytes=FD_TABLE_BYTES;
    source_plane_note.canonical_bytes=CANONICAL_TABLE_BYTES;
    source_plane_note.range_count=plane_range_count;
    source_plane_note.packet_bytes_retained=plane_retained_bytes;
    source_plane_note.omitted_packet_bytes=plane_omitted_bytes;
    source_plane_note.plane_not_fit=(uint32_t)plane_placement_not_fit;
    source_plane_note.note_generation=plane_publish_generation?plane_publish_generation:1;
    source_plane_note.confirmed_revoked=(kind==PLANE_REVOKED)?1u:0u;
    if(source_raw_root)aliases++;
    if(parent_custody)aliases++;
    if(parent_result)aliases++;
    if(parent_error_type||parent_error_value||parent_error_traceback)aliases++;
    aliases+=caller_error_count;
    source_plane_note.python_alias_count=aliases;
}

static void commit_source_plane_from_signal(void) {
    ssize_t wrote;
    if(source_plane_fd<0 || plane_full_commit) return;
    source_plane_note.magic=PLANE_NOTE_MAGIC;
    source_plane_note.commit_kind=PLANE_SIGNAL_NOTE;
    source_plane_note.python_bodies_byte_exported=0;
    source_plane_note.outside_peer_adoption=0;
    source_plane_note.source_registry_still_owned=1;
    source_plane_note.confirmed_revoked=0;
    source_plane_note.note_source_child=pending_child;
    source_plane_note.note_source_pidfd=pending_pidfd;
    source_plane_note.note_source_reaped=pending_reaped;
    source_plane_note.body_offset=PLANE_HEADER_BYTES;
    source_plane_note.body_capacity=plane_body_capacity;
    source_plane_note.body_used=plane_body_used;
    source_plane_note.table_offset=sizeof(struct plane_note);
    source_plane_note.fd_table_bytes=FD_TABLE_BYTES;
    source_plane_note.canonical_bytes=CANONICAL_TABLE_BYTES;
    source_plane_note.range_count=plane_range_count;
    source_plane_note.packet_bytes_retained=plane_retained_bytes;
    source_plane_note.omitted_packet_bytes=plane_omitted_bytes;
    source_plane_note.plane_not_fit=(uint32_t)plane_placement_not_fit;
    source_plane_note.note_generation=plane_publish_generation?plane_publish_generation:1;
    wrote=pwrite(source_plane_fd,&source_plane_note,sizeof(source_plane_note),0);
    if(wrote!=(ssize_t)sizeof(source_plane_note)) failed=1;
    source_registry_still_owned=1;
}

static uint64_t original_absolute_end(void) {
    uint64_t end=original_preowned_end_ns;
    if(caps.max_wall_ms && caps.max_wall_ms<=UINT64_C(3600000)){
        uint64_t admitted=started_ns+caps.max_wall_ms*UINT64_C(1000000);
        if(!end || admitted<end)end=admitted;
    }
    if(original_consumer_end_ns && original_consumer_end_ns<end)end=original_consumer_end_ns;
    return end;
}

static void own_current_caller_error(const char *operation) {
    if(!PyErr_Occurred())return;
    if(caller_error_count>=16){caller_error_lane_exhausted=1;failed=1;return;}
    struct caller_error *error=&caller_errors[caller_error_count++];
    error->operation=operation;error->error_number=errno;
    PyErr_Fetch(&error->type,&error->value,&error->traceback);
}

static uint64_t terminal_wall_ms(void) {
    /* A partition INSIDE the same original total end. Source deadlines and
     * native hard expiry are unchanged; no endpoint refresh is possible. */
    if(caps.role!=2)return 0;
    uint64_t reserve=caps.max_wall_ms/4;
    return reserve>UINT64_C(600000)?UINT64_C(600000):reserve;
}

static int emit_final_native_metrics(int finalized) {
    /* Fixed binary metadata trailer on the already-owned stderr pipe. It is
     * emitted AFTER interpreter finalization, not a pre-encoder estimate or
     * Python-authored observed label. Actual selected image custody is still
     * required by Root before these native facts receive evidence credit. */
    uint64_t record[23]={METRIC_MAGIC,2,caps.role,(uint64_t)owner_pid,actual_read,
        actual_output,ordinary.work+terminal.work,
        ordinary.live+terminal.live+registered_header_bytes(),
        ordinary.peak+terminal.peak+registered_header_bytes(),0,0,(uint64_t)failed,
        started_ns,0,(uint64_t)(int64_t)finalized};
    record[13]=0;struct timespec clock;if(clock_gettime(CLOCK_MONOTONIC,&clock)==0)
        record[13]=(uint64_t)clock.tv_sec*UINT64_C(1000000000)+(uint64_t)clock.tv_nsec-started_ns;
    for(int i=0;i<FD_MAX;i++){record[9]+=fd_slots[i].active;record[10]+=fd_slots[i].uncertain;}
    if(caps.role==2)memcpy(record+15,root_ledger,sizeof(root_ledger));
    size_t body=caps.role==2?sizeof(record):15*sizeof(uint64_t);
    if(actual_output>caps.max_output_bytes||body+32>caps.max_output_bytes-actual_output)return -1;
    /* Counter5 is actually performed BEFORE trailer delivery, explicitly NOT
     * intended bytes. The outside EOF/direct-wait consumer owns actual delivery
     * and derives the final actual total from its consumed exact trailer. */
    const unsigned char *parts[2]={(const unsigned char *)record,caps.binding_sha256};size_t sizes[2]={body,32};
    /* Byte escrow grants no time after the original minimum end. The native
     * body and binding remain two writes; an expired prefix emits neither and
     * cannot be presented as a completed finalization trailer. */
    {
        struct timespec current;uint64_t now_emit=UINT64_MAX;
        if(clock_gettime(CLOCK_MONOTONIC,&current)==0)
            now_emit=(uint64_t)current.tv_sec*UINT64_C(1000000000)+(uint64_t)current.tv_nsec;
        uint64_t absolute_end=started_ns+caps.max_wall_ms*UINT64_C(1000000);
        int at_hard_end=parent_end_reached || now_emit>=absolute_end ||
            (original_consumer_end_ns && now_emit>=original_consumer_end_ns);
        if(at_hard_end){failed=1;errno=ETIMEDOUT;return -1;}
    }
    for(int i=0;i<2;i++){size_t offset=0;while(offset<sizes[i]){
        /* No terminal write can renew the original end. A missing/partial
         * trailer is STOP_UNCONFIRMED at the actual EOF/direct-wait consumer.
         */
        struct timespec current;
        if(clock_gettime(CLOCK_MONOTONIC,&current)<0 || parent_end_reached ||
           (uint64_t)current.tv_sec*UINT64_C(1000000000)+(uint64_t)current.tv_nsec>=
           started_ns+caps.max_wall_ms*UINT64_C(1000000))return -1;
        if(original_consumer_end_ns &&
           (uint64_t)current.tv_sec*UINT64_C(1000000000)+(uint64_t)current.tv_nsec>=original_consumer_end_ns)return -1;
        ssize_t written=syscall(SYS_write,2,parts[i]+offset,sizes[i]-offset);
        if(written<=0)return -1;offset+=(size_t)written;actual_output+=(uint64_t)written;
    }}return 0;
}

static void hard_deadline(int signal_number) {
    (void)signal_number;
    /* Async-signal-safe native origin terminal. Root observes the exact pipe
     * bytes and direct wait; expiry never manufactures canonical success or
     * successful publication. The held pidfd remains the only signal target. */
    if(pending_pidfd>=0 && !pending_reaped && !source_cancel_attempted){
        source_cancel_attempted=1;
        if(syscall(SYS_pidfd_send_signal,pending_pidfd,SIGKILL,NULL,0)<0)source_cancel_errno=errno;
    }
    if(pending_child>=0 && !pending_reaped){
        int status;struct rusage usage;
        long waited=syscall(SYS_wait4,pending_child,&status,WNOHANG,&usage);
        if(waited==pending_child){pending_wait_status=status;pending_usage=usage;pending_reaped=1;}
        /* A zero/ECHILD/error result remains UNCONFIRMED. No blocking wait
         * or expired-role parking is introduced. */
    }
    /* One note into the plane the existing parent preowned before birth.
     * Not a native216 trailer and not accepted custody. Python object
     * identity is not walked or byte-exported from this handler. */
    commit_source_plane_from_signal();
    source_registry_still_owned=1;
    _exit(125);
}

static void existing_parent_deadline(int signal_number) {
    (void)signal_number;
    /* Physical original hard/minimum end, distinct from the earlier native
     * work-tail cutoff. No Python/destructor/publication tail runs after this
     * edge. A missing confirmed child retirement is still A180-C2, not success
     * or legal adoption by the receiver of bytes. */
    parent_end_reached=1;failed=1;in_terminal=1;
    if(pending_pidfd>=0 && !pending_reaped && !source_cancel_attempted){
        source_cancel_attempted=1;
        if(syscall(SYS_pidfd_send_signal,pending_pidfd,SIGKILL,NULL,0)<0)source_cancel_errno=errno;
    }
    if(pending_child>=0 && !pending_reaped){
        int status;struct rusage usage;
        long waited=syscall(SYS_wait4,pending_child,&status,WNOHANG,&usage);
        if(waited==pending_child){pending_wait_status=status;pending_usage=usage;pending_reaped=1;}
    }
    /* The genuine final observer must retain missing native216 as a failed
     * prefix. Do not forge a completed finalization record at an expired end.
     * This handler does not destroy the already-existing selected caller.
     * The interpreter task alone is signalled. Unconfirmed roots stay charged.
     * A peer ACK, pidfd, or same-TGID flag is not adoption. */
    carrier_signal_scalars_retained=1;
    source_registry_still_owned=1;
    root_custody_returned_to_selected_caller=1;
    int actual_tid=atomic_load_explicit(&original_execution.thread_tid,memory_order_acquire);
    if(actual_tid>0 &&
       atomic_load_explicit(&original_execution.started,memory_order_acquire)>0 &&
       atomic_load_explicit(&original_execution.clear_tid,memory_order_acquire)==actual_tid &&
       !atomic_load_explicit(&original_execution.finished,memory_order_acquire))
        syscall(SYS_tgkill,getpid(),actual_tid,SIGUSR2);
}

static void original_interpreter_work_cutoff(int signal_number) {
    (void)signal_number;
    /* Linux SYS_exit retires only this in-process execution task. It invokes
     * no unsafe Python decref/finalizer or libc cancellation/unwind callback.
     * The original outside native caller owns the complete stack/arenas/FD
     * table and kernel-parent TGID before this local thread loss. Its cold C
     * consumer below must confirm kernel task exit before inspecting/retiring
     * that state. A new process, renewed timer or expired park is not used. */
    atomic_store_explicit(&original_execution.cutoff_accepted,1,memory_order_release);
    atomic_store_explicit(&original_execution.thread_tid,0,memory_order_release);
    syscall(SYS_exit,125);
    __builtin_unreachable();
}

/* These symbols are link-time edges of the separately selected static image.
 * The consumed native relation must prove all required call sites bind them;
 * exporting them alone is deliberately NOT such proof. */
void *__wrap_malloc(size_t n);
void *__wrap_calloc(size_t n,size_t size);
void *__wrap_realloc(void *p,size_t n);
void __wrap_free(void *p);
int __real_close(int fd);
int __real_open(const char *,int,...);
int __real_openat(int,const char *,int,...);
int __real_socket(int,int,int);
int __real_socketpair(int,int,int,int[2]);
int __real_pipe2(int[2],int);
int __real_dup(int);
int __real_dup2(int,int);
int __real_dup3(int,int,int);
int __real_fcntl(int,int,...);
int __real_accept4(int,struct sockaddr *,socklen_t *,int);
int __real_epoll_create1(int);
ssize_t __real_read(int,void *,size_t);
ssize_t __real_pread(int,void *,size_t,off_t);
ssize_t __real_write(int,const void *,size_t);
ssize_t __real_recvmsg(int,struct msghdr *,int);
ssize_t __real_sendmsg(int,const struct msghdr *,int);
ssize_t __real_send(int,const void *,size_t,int);
ssize_t __real_recv(int,void *,size_t,int);

static int origin_refusal_to(int output_fd,const char *file,int line,const char *operation,const char *message,int saved_errno) {
    /* No heap, Python object, sprintf, traceback producer or error-message
     * truncation is required to preserve a preinitialization failure. Root's
     * exact stderr pipe retains this full native origin and direct wait. */
    const char *parts[]={"native-owner: RuntimeError; file=",file,"; operation=",operation,
                         "; full-origin=",message?message:"native entry contract refused","; errno="};
    for(size_t i=0;i<sizeof(parts)/sizeof(parts[0]);i++) {
        const char *text=parts[i];size_t size=strlen(text),offset=0;
        while(offset<size){ssize_t n=syscall(SYS_write,output_fd,text+offset,size-offset);if(n<=0)return 125;offset+=(size_t)n;}
    }
    char digits[32];size_t used=0;unsigned value=saved_errno<0?(unsigned)(-(saved_errno+1))+1:(unsigned)saved_errno;
    do{digits[used++]=(char)('0'+value%10);value/=10;}while(value);
    if(saved_errno<0)digits[used++]='-';
    while(used){used--;if(syscall(SYS_write,output_fd,&digits[used],1)!=1)return 125;}
    static const char label[]="; line=";syscall(SYS_write,output_fd,label,sizeof(label)-1);
    used=0;value=(unsigned)line;do{digits[used++]=(char)('0'+value%10);value/=10;}while(value);
    while(used){used--;if(syscall(SYS_write,output_fd,&digits[used],1)!=1)return 125;}
    syscall(SYS_write,output_fd,"\n",1);return 125;
}
#define ENTRY_REFUSAL(message) origin_refusal_to(2,__FILE__,__LINE__,__func__,message,errno)
#define CHILD_REFUSAL(fd,message) do {origin_refusal_to(fd,__FILE__,__LINE__,__func__,message,errno);_exit(125);}while(0)

static uint64_t now_ns(void) {
    struct timespec ts;
    if(clock_gettime(CLOCK_MONOTONIC,&ts)<0) return UINT64_MAX;
    return (uint64_t)ts.tv_sec*UINT64_C(1000000000)+(uint64_t)ts.tv_nsec;
}
static int deadline(void) {
    if(parent_end_reached)return 0;
    uint64_t now=now_ns();
    uint64_t end=started_ns+caps.max_wall_ms*UINT64_C(1000000);
    if(original_consumer_end_ns && original_consumer_end_ns<end)end=original_consumer_end_ns;
    uint64_t reserve=(in_terminal?0:terminal_wall_ms())*UINT64_C(1000000);
    return end>=reserve && now>=started_ns && now<end-reserve;
}
static size_t aligned(size_t n) {
    if(n>SIZE_MAX-(ALIGNMENT-1)) return 0;
    return (n+ALIGNMENT-1)&~(ALIGNMENT-1);
}
static void *arena_alloc(struct arena *a,size_t n) {
    size_t wanted=aligned(n?n:1),head=aligned(sizeof(struct block));
    uint64_t work_cap=a==&terminal?caps.terminal_work_bytes:
        caps.max_work_bytes-caps.terminal_work_bytes;
    if(!wanted || !head || n>work_cap || a->work>work_cap-n) return NULL;
    /* Lifetime is at actual free, not at the end of a Python lexical scope. */
    struct block *b;
    for(b=a->first;b;b=b->next) {
        if(!b->active && b->capacity>=wanted) {
            if(a->live>a->size-head-b->capacity) return NULL;
            b->active=1; b->requested=n;
            a->live+=head+b->capacity; a->work+=n;
            if(a->live>a->peak) a->peak=a->live;
            return (unsigned char *)b+head;
        }
    }
    if(wanted>a->size || head>a->size-wanted || a->used>a->size-head-wanted) return NULL;
    b=(struct block *)(a->base+a->used);
    b->capacity=wanted; b->requested=n; b->active=1; b->next=NULL;
    if(a->last) a->last->next=b; else a->first=b;
    a->last=b; a->used+=head+wanted; a->live+=head+wanted; a->work+=n;
    if(a->live>a->peak) a->peak=a->live;
    return (unsigned char *)b+head;
}
static struct arena *belongs(void *p) {
    uintptr_t address=(uintptr_t)p;
    if(address>=(uintptr_t)ordinary.base && address<(uintptr_t)ordinary.base+ordinary.size) return &ordinary;
    if(address>=(uintptr_t)terminal.base && address<(uintptr_t)terminal.base+terminal.size) return &terminal;
    return NULL;
}
void *friday_malloc(size_t n) {
    if(!initialized || getpid()!=owner_pid) {errno=EPERM;return NULL;}
    if(parent_end_reached){failed=1;errno=ETIMEDOUT;return NULL;}
    pthread_mutex_lock(&arena_lock);
    if(!in_terminal && !deadline()) {failed=1;in_terminal=1;}
    void *p=arena_alloc(in_terminal?&terminal:&ordinary,n);
    if(!p && !in_terminal) {
        /* The selected terminal reserve is INSIDE the same original caps. */
        failed=1;in_terminal=1;p=arena_alloc(&terminal,n);
    }
    if(!p) {failed=1;errno=ENOMEM;}
    pthread_mutex_unlock(&arena_lock);
    return p;
}
void friday_free(void *p) {
    if(!p) return;
    pthread_mutex_lock(&arena_lock);
    struct arena *a=belongs(p);
    if(!a) {failed=1;pthread_mutex_unlock(&arena_lock);return;}
    size_t head=aligned(sizeof(struct block));
    struct block *b=(struct block *)((unsigned char *)p-head);
    /* Foreign/interior/double frees are refused; no fictitious live refund. */
    struct block *found=a->first;
    while(found && found!=b) found=found->next;
    if(!found || !b->active) {failed=1;pthread_mutex_unlock(&arena_lock);return;}
    b->active=0;a->live-=head+b->capacity;
    pthread_mutex_unlock(&arena_lock);
}
void *friday_calloc(size_t n,size_t size) {
    if(n && size>SIZE_MAX/n) {failed=1;errno=ENOMEM;return NULL;}
    size_t bytes=n*size;void *p=friday_malloc(bytes);
    if(p) memset(p,0,bytes);
    return p;
}
void *friday_realloc(void *p,size_t n) {
    if(!p) return friday_malloc(n);
    if(!n) {friday_free(p);return NULL;}
    struct arena *a=belongs(p);
    if(!a) {failed=1;errno=EINVAL;return NULL;}
    struct block *b=(struct block *)((unsigned char *)p-aligned(sizeof(struct block)));
    struct block *found=a->first;
    while(found && found!=b) found=found->next;
    if(!found || !b->active) {failed=1;errno=EINVAL;return NULL;}
    /* Pre-admit both old and new storage; failure preserves the original. */
    void *q=friday_malloc(n);
    if(!q) return NULL;
    memcpy(q,p,n<b->requested?n:b->requested);friday_free(p);
    return q;
}
void *__wrap_malloc(size_t n) {return friday_malloc(n);}
void *__wrap_calloc(size_t n,size_t s) {return friday_calloc(n,s);}
void *__wrap_realloc(void *p,size_t n) {return friday_realloc(p,n);}
void __wrap_free(void *p) {friday_free(p);}
static void *py_malloc(void *ctx,size_t n) {(void)ctx;return friday_malloc(n);}
static void *py_calloc(void *ctx,size_t n,size_t s) {(void)ctx;return friday_calloc(n,s);}
static void *py_realloc(void *ctx,void *p,size_t n) {(void)ctx;return friday_realloc(p,n);}
static void py_free(void *ctx,void *p) {(void)ctx;friday_free(p);}
static void *py_arena(void *ctx,size_t n) {(void)ctx;return friday_malloc(n);}
static void py_arena_free(void *ctx,void *p,size_t n) {(void)ctx;(void)n;friday_free(p);}

static int register_fd(int fd) {
    int used=0;
    for(int i=0;i<FD_MAX;i++) used+=fd_slots[i].active||fd_slots[i].uncertain;
    for(int i=0;i<FD_MAX;i++) if(fd_slots[i].active && fd_slots[i].fd==fd) return 0;
    if((uint64_t)used>=caps.max_fds) {failed=1;errno=EMFILE;return -1;}
    for(int i=0;i<FD_MAX;i++) if(!fd_slots[i].active && !fd_slots[i].uncertain) {
        fd_slots[i].fd=fd;fd_slots[i].active=1;fd_slots[i].generation=++fd_generation;return 0;
    }
    failed=1;errno=EMFILE;return -1;
}
struct linux_dirent64_owner {uint64_t inode;int64_t offset;unsigned short record_length;unsigned char type;char name[];};
static int inventory_inherited(void) {
    /* Enumerate ALL inherited numbers, including descriptors above a newly
     * lowered RLIMIT_NOFILE. A range(ceiling) is not an inherited-FD proof.
     * The fixed stack buffer and temporary proc directory have their actual
     * preinit native owner; no Python/list/ancillary object exists yet. */
    int directory=(int)syscall(SYS_openat,AT_FDCWD,"/proc/self/fd",O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW,0);
    if(directory<0)return -1;
    if(register_fd(directory)<0){syscall(SYS_close,directory);return -1;}
    unsigned char buffer[8192];int result=0;
    for(;;){
        if(actual_read>caps.max_read_bytes||sizeof(buffer)>caps.max_read_bytes-actual_read){errno=EFBIG;result=-1;break;}
        long got=syscall(SYS_getdents64,directory,buffer,sizeof(buffer));
        if(got<0){result=-1;break;}if(got==0)break;
        actual_read+=(uint64_t)got;
        for(size_t offset=0;offset<(size_t)got;){
            struct linux_dirent64_owner *item=(struct linux_dirent64_owner *)(buffer+offset);
            if(item->record_length<offsetof(struct linux_dirent64_owner,name)+2||item->record_length>(size_t)got-offset){result=-1;break;}
            int number=0,valid=1;size_t n=0;
            while(n<item->record_length- offsetof(struct linux_dirent64_owner,name)&&item->name[n]){
                unsigned char c=(unsigned char)item->name[n++];
                if(c<'0'||c>'9'||number>(INT_MAX-(c-'0'))/10){valid=0;break;}number=number*10+c-'0';
            }
            if(valid&&n&&number!=directory&&register_fd(number)<0){result=-1;break;}
            offset+=item->record_length;
        }
        if(result<0)break;
    }
    /* A failed close keeps the initial native descriptor quota charged. */
    for(int i=0;i<FD_MAX;i++)if(fd_slots[i].active&&fd_slots[i].fd==directory){
        fd_slots[i].active=0;
        if(syscall(SYS_close,directory)<0){fd_slots[i].uncertain=1;fd_slots[i].close_errno=errno;failed=1;result=-1;}
        break;
    }
    return result;
}
static int retire_fd(int fd) {
    struct fd_slot *slot=NULL;
    for(int i=0;i<FD_MAX;i++) if(fd_slots[i].active && fd_slots[i].fd==fd) {slot=&fd_slots[i];break;}
    if(!slot) {failed=1;errno=EBADF;return -1;}
    slot->active=0; /* detach BEFORE the only close attempt */
    if(fd==pending_pidfd){pending_pidfd=-1;if(pending_reaped)pending_child=-1;}
    int result=__real_close(fd);
    if(result<0) {slot->uncertain=1;slot->close_errno=errno;failed=1;}
    for(struct received_packet *r=received_first;r;r=r->next)for(int j=0;j<r->count;j++)
        if(r->delivered[j]==fd && r->right_generation[j]==slot->generation){
            r->right_close_attempted[j]=1;r->right_close_errno[j]=result<0?slot->close_errno:0;
            r->right_uncertain[j]=result<0;
        }
    return result;
}
/* A placeholder is charged BEFORE the kernel producer. Unused placeholders
 * are released only after a failed producer or after the delivered set is
 * completely registered. There is no after-creation admission window. */
static int fd_reserve(int count,int *slots) {
    if(parent_end_reached || !deadline()){failed=1;errno=ETIMEDOUT;return -1;}
    pthread_mutex_lock(&arena_lock);
    int used=0;
    for(int i=0;i<FD_MAX;i++) used+=fd_slots[i].active||fd_slots[i].uncertain;
    if(count<0 || (uint64_t)count>caps.max_fds-(uint64_t)used) {
        pthread_mutex_unlock(&arena_lock);failed=1;errno=EMFILE;return -1;
    }
    int found=0;
    for(int i=0;i<FD_MAX && found<count;i++) if(!fd_slots[i].active && !fd_slots[i].uncertain) {
        fd_slots[i].active=1;fd_slots[i].fd=-1;fd_slots[i].generation=++fd_generation;slots[found++]=i;
    }
    pthread_mutex_unlock(&arena_lock);
    return found==count?0:-1;
}
static void fd_finish(int slot,int fd) {
    pthread_mutex_lock(&arena_lock);
    if(fd<0) fd_slots[slot].active=0;else fd_slots[slot].fd=fd;
    pthread_mutex_unlock(&arena_lock);
}
int __wrap_close(int fd) {return initialized?retire_fd(fd):__real_close(fd);}
int __wrap_open(const char *path,int flags,...) {
    mode_t mode=0; if((flags&O_CREAT)||(flags&O_TMPFILE)==O_TMPFILE) {va_list ap;va_start(ap,flags);mode=(mode_t)va_arg(ap,int);va_end(ap);}
    if(!initialized) return __real_open(path,flags,mode);
    int slot;if(fd_reserve(1,&slot)<0)return -1;
    int fd=__real_open(path,flags,mode),saved=errno;fd_finish(slot,fd);errno=saved;return fd;
}
int __wrap_open64(const char *path,int flags,...) {
    mode_t mode=0;if((flags&O_CREAT)||(flags&O_TMPFILE)==O_TMPFILE){va_list ap;va_start(ap,flags);mode=(mode_t)va_arg(ap,int);va_end(ap);}
    return __wrap_open(path,flags,mode);
}
int __wrap_openat(int parent,const char *path,int flags,...) {
    mode_t mode=0;if((flags&O_CREAT)||(flags&O_TMPFILE)==O_TMPFILE){va_list ap;va_start(ap,flags);mode=(mode_t)va_arg(ap,int);va_end(ap);}
    if(!initialized)return __real_openat(parent,path,flags,mode);
    int slot;if(fd_reserve(1,&slot)<0)return -1;
    int fd=__real_openat(parent,path,flags,mode),saved=errno;fd_finish(slot,fd);errno=saved;return fd;
}
int __wrap_openat64(int parent,const char *path,int flags,...) {
    mode_t mode=0;if((flags&O_CREAT)||(flags&O_TMPFILE)==O_TMPFILE){va_list ap;va_start(ap,flags);mode=(mode_t)va_arg(ap,int);va_end(ap);}
    return __wrap_openat(parent,path,flags,mode);
}
int __wrap_socket(int domain,int type,int protocol) {
    if(!initialized)return __real_socket(domain,type,protocol);
    int slot;if(fd_reserve(1,&slot)<0)return -1;
    int fd=__real_socket(domain,type,protocol),saved=errno;fd_finish(slot,fd);errno=saved;return fd;
}
int __wrap_socketpair(int domain,int type,int protocol,int pair[2]) {
    if(!initialized)return __real_socketpair(domain,type,protocol,pair);
    int slots[2];if(fd_reserve(2,slots)<0)return -1;
    int rc=__real_socketpair(domain,type,protocol,pair),saved=errno;
    for(int i=0;i<2;i++)fd_finish(slots[i],rc<0?-1:pair[i]);errno=saved;return rc;
}
int __wrap_pipe2(int pair[2],int flags) {
    if(!initialized)return __real_pipe2(pair,flags);
    int slots[2];if(fd_reserve(2,slots)<0)return -1;
    int rc=__real_pipe2(pair,flags),saved=errno;
    for(int i=0;i<2;i++)fd_finish(slots[i],rc<0?-1:pair[i]);errno=saved;return rc;
}
int __wrap_pipe(int pair[2]) {return __wrap_pipe2(pair,0);}
int __wrap_dup(int oldfd) {
    if(!initialized)return __real_dup(oldfd);
    int slot;if(fd_reserve(1,&slot)<0)return -1;
    int fd=__real_dup(oldfd),saved=errno;fd_finish(slot,fd);errno=saved;return fd;
}
int __wrap_dup2(int oldfd,int newfd) {
    if(!initialized)return __real_dup2(oldfd,newfd);
    if(oldfd==newfd)return __real_dup2(oldfd,newfd);
    for(int i=0;i<FD_MAX;i++)if((fd_slots[i].active||fd_slots[i].uncertain)&&fd_slots[i].fd==newfd){errno=EPERM;return -1;}
    int slot;if(fd_reserve(1,&slot)<0)return -1;
    int fd=__real_dup2(oldfd,newfd),saved=errno;fd_finish(slot,fd);errno=saved;return fd;
}
int __wrap_dup3(int oldfd,int newfd,int flags) {
    if(!initialized)return __real_dup3(oldfd,newfd,flags);
    for(int i=0;i<FD_MAX;i++)if((fd_slots[i].active||fd_slots[i].uncertain)&&fd_slots[i].fd==newfd){errno=EPERM;return -1;}
    int slot;if(fd_reserve(1,&slot)<0)return -1;
    int fd=__real_dup3(oldfd,newfd,flags),saved=errno;fd_finish(slot,fd);errno=saved;return fd;
}
int __wrap_fcntl(int fd,int command,...) {
    if(command==F_GETFD||command==F_GETFL||command==F_GETOWN||command==F_GETSIG||command==F_GETLEASE||command==F_GETPIPE_SZ||command==F_GET_SEALS)
        return __real_fcntl(fd,command);
    va_list ap;va_start(ap,command);
    if(command==F_GETLK||command==F_SETLK||command==F_SETLKW) {
        void *arg=va_arg(ap,void *);va_end(ap);return __real_fcntl(fd,command,arg);
    }
    int arg=va_arg(ap,int);va_end(ap);
    if(command!=F_DUPFD&&command!=F_DUPFD_CLOEXEC)return __real_fcntl(fd,command,arg);
    if(!initialized)return __real_fcntl(fd,command,arg);
    int slot;if(fd_reserve(1,&slot)<0)return -1;
    int created=__real_fcntl(fd,command,arg),saved=errno;fd_finish(slot,created);errno=saved;return created;
}
int __wrap_accept4(int fd,struct sockaddr *address,socklen_t *length,int flags) {
    if(!initialized)return __real_accept4(fd,address,length,flags);
    int slot;if(fd_reserve(1,&slot)<0)return -1;
    int created=__real_accept4(fd,address,length,flags),saved=errno;fd_finish(slot,created);errno=saved;return created;
}
int __wrap_accept(int fd,struct sockaddr *address,socklen_t *length) {return __wrap_accept4(fd,address,length,0);}
int __wrap_epoll_create1(int flags) {
    if(!initialized)return __real_epoll_create1(flags);
    int slot;if(fd_reserve(1,&slot)<0)return -1;
    int fd=__real_epoll_create1(flags),saved=errno;fd_finish(slot,fd);errno=saved;return fd;
}
int __wrap_epoll_create(int size) {if(size<=0){errno=EINVAL;return -1;}return __wrap_epoll_create1(0);}
static int poll_io_until(int fd,short events,uint64_t selected_end) {
    uint64_t wall=caps.max_wall_ms-(in_terminal?0:terminal_wall_ms());
    uint64_t now=now_ns(),end=started_ns+wall*UINT64_C(1000000);
    if(original_consumer_end_ns){
        uint64_t original_end=original_consumer_end_ns-(in_terminal?0:terminal_wall_ms()*UINT64_C(1000000));
        if(original_end<end)end=original_end;
    }
    if(selected_end){
        if(selected_end>started_ns+caps.max_wall_ms*UINT64_C(1000000)){errno=EINVAL;return -1;}
        if(selected_end<end)end=selected_end;
    }
    if(parent_end_reached || now>=end) {errno=ETIMEDOUT;return -1;}
    uint64_t ms=(end-now)/1000000;
    int timeout=ms>INT_MAX?INT_MAX:(int)ms;
    struct pollfd p={fd,events,0};
    int ready=poll(&p,1,timeout);
    if(ready<=0) {if(ready==0) errno=ETIMEDOUT;return -1;}
    if(parent_end_reached || now_ns()>=end){errno=ETIMEDOUT;return -1;}
    if(p.revents&(POLLNVAL|POLLERR)) {errno=EIO;return -1;}
    return 0;
}
static int packet_end_live(uint64_t selected_end) {
    if(!deadline() || (selected_end && now_ns()>=selected_end)){failed=1;errno=ETIMEDOUT;return 0;}
    return 1;
}
static int poll_io(int fd,short events) {return poll_io_until(fd,events,0);}
static int io_before(uint64_t amount,int output) {
    uint64_t used=output?actual_output:actual_read,cap=output?caps.max_output_bytes:caps.max_read_bytes;
    if(!deadline() || used>cap || amount>cap-used){failed=1;errno=EFBIG;return -1;}return 0;
}
ssize_t __wrap_read(int fd,void *buf,size_t n) {
    if(!initialized)return __real_read(fd,buf,n);
    if(io_before(n,0)<0||poll_io(fd,POLLIN)<0)return -1;
    ssize_t got=__real_read(fd,buf,n);if(got>0)actual_read+=(uint64_t)got;return got;
}
ssize_t __wrap_pread(int fd,void *buf,size_t n,off_t offset) {
    if(!initialized)return __real_pread(fd,buf,n,offset);
    if(io_before(n,0)<0)return -1;
    ssize_t got=__real_pread(fd,buf,n,offset);if(got>0)actual_read+=(uint64_t)got;return got;
}
ssize_t __wrap_pread64(int fd,void *buf,size_t n,off64_t offset) {return __wrap_pread(fd,buf,n,(off_t)offset);}
ssize_t __wrap_write(int fd,const void *buf,size_t n) {
    if(!initialized)return __real_write(fd,buf,n);
    if(io_before(n,1)<0||poll_io(fd,POLLOUT)<0)return -1;
    ssize_t got=__real_write(fd,buf,n);if(got>0)actual_output+=(uint64_t)got;return got;
}
ssize_t __wrap_send(int fd,const void *buf,size_t n,int flags) {
    if(!initialized)return __real_send(fd,buf,n,flags);
    if(io_before(n,1)<0||poll_io(fd,POLLOUT)<0)return -1;
    ssize_t got=__real_send(fd,buf,n,flags|MSG_DONTWAIT|MSG_NOSIGNAL);if(got>0)actual_output+=(uint64_t)got;return got;
}
ssize_t __wrap_recv(int fd,void *buf,size_t n,int flags) {
    if(!initialized)return __real_recv(fd,buf,n,flags);
    if(io_before(n,0)<0||poll_io(fd,POLLIN)<0)return -1;
    ssize_t got=__real_recv(fd,buf,n,flags|MSG_DONTWAIT);if(got>0)actual_read+=(uint64_t)got;return got;
}
ssize_t __wrap_sendmsg(int fd,const struct msghdr *message,int flags) {
    if(!initialized)return __real_sendmsg(fd,message,flags);
    uint64_t bytes=0;for(size_t i=0;i<message->msg_iovlen;i++){if(message->msg_iov[i].iov_len>UINT64_MAX-bytes){errno=EOVERFLOW;return -1;}bytes+=message->msg_iov[i].iov_len;}
    if(io_before(bytes,1)<0||poll_io(fd,POLLOUT)<0)return -1;
    ssize_t got=__real_sendmsg(fd,message,flags|MSG_DONTWAIT|MSG_NOSIGNAL);if(got>0)actual_output+=(uint64_t)got;return got;
}
ssize_t __wrap_recvmsg(int fd,struct msghdr *message,int flags) {
    if(!initialized)return __real_recvmsg(fd,message,flags);
    uint64_t bytes=0;for(size_t i=0;i<message->msg_iovlen;i++){if(message->msg_iov[i].iov_len>UINT64_MAX-bytes){errno=EOVERFLOW;return -1;}bytes+=message->msg_iov[i].iov_len;}
    size_t worst=message->msg_controllen/sizeof(int);
    if(worst>caps.max_fds||worst>FD_MAX){errno=EMFILE;return -1;}
    int slots[FD_MAX];if(fd_reserve((int)worst,slots)<0)return -1;
    ssize_t got=-1;int count=0,saved;
    if(io_before(bytes,0)>=0&&poll_io(fd,POLLIN)>=0)got=__real_recvmsg(fd,message,flags|MSG_CMSG_CLOEXEC|MSG_DONTWAIT);
    saved=errno;
    if(got>=0) {
        actual_read+=(uint64_t)got;
        for(struct cmsghdr *c=CMSG_FIRSTHDR(message);c;c=CMSG_NXTHDR(message,c))if(c->cmsg_level==SOL_SOCKET&&c->cmsg_type==SCM_RIGHTS){
            size_t n=(c->cmsg_len-CMSG_LEN(0))/sizeof(int);int *delivered=(int *)CMSG_DATA(c);
            for(size_t j=0;j<n;j++)fd_finish(slots[count++],delivered[j]);
        }
    }
    for(size_t i=(size_t)count;i<worst;i++)fd_finish(slots[i],-1);
    errno=saved;return got;
}
static PyObject *receive_packet(PyObject *self,PyObject *args) {
    (void)self;
    int fd,expect_fd;Py_ssize_t limit;unsigned long long selected_end=0;
    if(!PyArg_ParseTuple(args,"ini|K",&fd,&limit,&expect_fd,&selected_end)) return NULL;
    if(limit<1 || (uint64_t)limit>caps.max_output_bytes) {PyErr_SetString(PyExc_ValueError,"packet limit");return NULL;}
    if(actual_read>caps.max_read_bytes || (uint64_t)limit>caps.max_read_bytes-actual_read) {
        PyErr_SetString(PyExc_MemoryError,"native read reservation");return NULL;
    }
    if((size_t)limit>SIZE_MAX-sizeof(struct received_packet))return PyErr_NoMemory();
    struct received_packet *record=friday_calloc(1,sizeof(*record)+(size_t)limit);
    if(!record)return PyErr_NoMemory();
    record->data=(unsigned char *)(record+1);record->capacity=(size_t)limit;
    record->fd=fd;record->received=-1;
    /* Commit the COMPLETE prospective native owner before poll/recvmsg. Data,
     * control bytes and every delivered-right slot never live only on stack.
     * Successful and rejected packets stay charged until called native end.
     */
    if(received_last)received_last->next=record;else received_first=record;
    received_last=record;received_count++;
    struct iovec io={record->data,(size_t)limit};
    struct msghdr message={0};message.msg_iov=&io;message.msg_iovlen=1;
    message.msg_control=record->control.bytes;message.msg_controllen=sizeof(record->control.bytes);
    if(poll_io_until(fd,POLLIN,(uint64_t)selected_end)<0) {
        record->error_number=errno;return PyErr_SetFromErrno(PyExc_OSError);
    }
    if(fd_reserve(10,record->right_slots)<0){record->error_number=errno;return PyErr_SetFromErrno(PyExc_OSError);}
    if(!packet_end_live((uint64_t)selected_end)){
        record->error_number=errno;for(int i=0;i<10;i++)fd_finish(record->right_slots[i],-1);
        return PyErr_SetFromErrno(PyExc_OSError);
    }
    ssize_t got=__real_recvmsg(fd,&message,MSG_CMSG_CLOEXEC|MSG_DONTWAIT);
    record->received=got;record->error_number=got<0?errno:0;
    record->flags=message.msg_flags;record->control_used=message.msg_controllen;
    if(got<0) {for(int i=0;i<10;i++)fd_finish(record->right_slots[i],-1);return PyErr_SetFromErrno(PyExc_OSError);}
    actual_read+=(uint64_t)got;
    int count=0,bad=0,peers=0;
    /* No Python allocation or rejection precedes registration of ALL kernel
     * delivered descriptors in the fixed native cleanup table. */
    for(struct cmsghdr *c=CMSG_FIRSTHDR(&message);c;c=CMSG_NXTHDR(&message,c)) {
        if(c->cmsg_level==SOL_SOCKET && c->cmsg_type==SCM_RIGHTS) {
            size_t bytes=c->cmsg_len-CMSG_LEN(0),n=bytes/sizeof(int);
            if(bytes%sizeof(int)) bad=1;
            int *fds=(int *)CMSG_DATA(c);
            for(size_t j=0;j<n;j++) {
                if(count>=10) {bad=1;break;}
                fd_finish(record->right_slots[count],fds[j]);record->delivered[count]=fds[j];
                record->right_generation[count]=fd_slots[record->right_slots[count]].generation;count++;
            }
        } else if(c->cmsg_level==SOL_SOCKET && c->cmsg_type==SCM_CREDENTIALS && c->cmsg_len==CMSG_LEN(sizeof(record->peer))) {
            memcpy(&record->peer,CMSG_DATA(c),sizeof(record->peer));peers++;
        } else bad=1;
    }
    record->count=count;
    for(int i=count;i<10;i++)fd_finish(record->right_slots[i],-1);
    if((message.msg_flags&(MSG_TRUNC|MSG_CTRUNC)) || peers!=1 || !got || count!=(expect_fd?1:0)) bad=1;
    if(!packet_end_live((uint64_t)selected_end)){record->error_number=errno;bad=1;}
    record->contract_bad=bad;
    place_received_packet_body(record);
    if(bad) {
        PyErr_SetString(PyExc_ValueError,"native packet contract; full received native history retained");
        goto projection_error;
    }
    PyObject *raw=PyBytes_FromStringAndSize((char *)record->data,got);
    PyObject *result=raw?Py_BuildValue("Niiii",raw,expect_fd?record->delivered[0]:-1,
        record->peer.pid,record->peer.uid,record->peer.gid):NULL;
    if(!result)goto projection_error;
    record->projected=1;
    return result;
projection_error:
    /* Fetch raw type/value/traceback without normalization/allocation, then
     * restore another owned reference for the Python causal consumer. The
     * native packet/right history survives even if that consumer cannot run.
     */
    PyErr_Fetch(&record->error_type,&record->error_value,&record->error_traceback);
    Py_XINCREF(record->error_type);Py_XINCREF(record->error_value);Py_XINCREF(record->error_traceback);
    PyErr_Restore(record->error_type,record->error_value,record->error_traceback);
    return NULL;
}

static int consume_native_packets(void) {
    /* The fixed original native caller accepts complete received storage, not
     * a status-only projection. It performs no guessed FD close: the fixed FD
     * table owns each right's later detach-once state, including uncertainty.
     * Frames remain physically live until actual caller/interpreter destruction.
     */
    for(struct received_packet *r=received_first;r;r=r->next){
        if(r->control_used>CONTROL_BYTES || r->count<0 || r->count>10)return -1;
        r->accepted_native=1;
    }
    return 0;
}

static PyObject *received_history(PyObject *self,PyObject *ignored) {
    (void)self;(void)ignored;
    PyObject *items=PyList_New(0);if(!items)return NULL;
    for(struct received_packet *r=received_first;r;r=r->next){
        PyObject *rights=PyList_New(0);if(!rights){Py_DECREF(items);return NULL;}
        for(int j=0;j<r->count;j++){
            PyObject *right=Py_BuildValue("{s:i,s:K,s:i,s:i,s:i}","fd",r->delivered[j],
                "generation",(unsigned long long)r->right_generation[j],
                "close_attempted",r->right_close_attempted[j],"close_errno",r->right_close_errno[j],
                "uncertain",r->right_uncertain[j]);
            if(!right||PyList_Append(rights,right)<0){Py_XDECREF(right);Py_DECREF(rights);Py_DECREF(items);return NULL;}
            Py_DECREF(right);
        }
        PyObject *item=Py_BuildValue("{s:y#,s:y#,s:L,s:i,s:i,s:i,s:i,s:O,s:O,s:O}",
            "raw",r->data,(Py_ssize_t)(r->received>0?r->received:0),
            "control",r->control.bytes,(Py_ssize_t)(r->control_used<CONTROL_BYTES?r->control_used:CONTROL_BYTES),
            "received",(long long)r->received,"flags",r->flags,"errno",r->error_number,
            "contract_bad",r->contract_bad,"projected",r->projected,
            "error_type",r->error_type?r->error_type:Py_None,
            "error_value",r->error_value?r->error_value:Py_None,
            "error_traceback",r->error_traceback?r->error_traceback:Py_None);
        if(!item||PyDict_SetItemString(item,"rights",rights)<0||PyList_Append(items,item)<0){
            Py_XDECREF(item);Py_DECREF(rights);Py_DECREF(items);return NULL;}
        Py_DECREF(rights);
        Py_DECREF(item);
    }
    return items;
}

static PyObject *lock_canonical_slot(PyObject *self,PyObject *args) {
    (void)self;int fd,ordinal;unsigned long long generation;
    if(!PyArg_ParseTuple(args,"iiK",&fd,&ordinal,&generation))return NULL;
    if(!existing_parent_entry || !parent_custody || getpid()!=owner_pid ||
       generation!=parent_generation || ordinal<0 || ordinal>1 || !deadline()){
        PyErr_SetString(PyExc_PermissionError,"original canonical slot owner/generation absent");return NULL;
    }
    struct canonical_slot *slot=&canonical_slots[ordinal];
    if(slot->prepared){PyErr_SetString(PyExc_RuntimeError,"canonical slot effect cannot repeat");return NULL;}
    slot->fd=fd;slot->generation=generation;slot->prepared=1;
    if(fstat(fd,&slot->before_effect)<0){slot->error_number=errno;return PyErr_SetFromErrno(PyExc_OSError);}
    /* Fixed owner and actual borrowed identity precede flock. The success
     * commit is a native scalar write, before any Python return allocation.
     */
    if(syscall(SYS_flock,fd,LOCK_EX|LOCK_NB)<0){slot->error_number=errno;return PyErr_SetFromErrno(PyExc_OSError);}
    slot->acquired=1;Py_RETURN_NONE;
}

static int retire_native_slots(void) {
    int result=0;
    for(int i=0;i<4;i++){
        struct canonical_slot *s=&canonical_slots[i];
        if(s->unlock_uncertain){result=-1;continue;}
        if(!s->acquired || s->unlock_attempted){if(s->unlock_uncertain)result=-1;continue;}
        if(!deadline()){result=-1;errno=ETIMEDOUT;continue;}
        /* The borrowed number is valid only under the original held lifetime.
         * It was never closed/reopened by Python; check real generation and
         * held identity again. An uncertain unlock is never retried.
         */
        struct stat current;
        if(s->generation!=parent_generation || fstat(s->fd,&current)<0 ||
           current.st_dev!=s->before_effect.st_dev || current.st_ino!=s->before_effect.st_ino ||
           current.st_mode!=s->before_effect.st_mode || current.st_uid!=s->before_effect.st_uid ||
           current.st_gid!=s->before_effect.st_gid || current.st_nlink!=s->before_effect.st_nlink ||
           current.st_size!=s->before_effect.st_size ||
           current.st_mtim.tv_sec!=s->before_effect.st_mtim.tv_sec ||
           current.st_mtim.tv_nsec!=s->before_effect.st_mtim.tv_nsec ||
           current.st_ctim.tv_sec!=s->before_effect.st_ctim.tv_sec ||
           current.st_ctim.tv_nsec!=s->before_effect.st_ctim.tv_nsec){
            s->unlock_uncertain=1;s->error_number=errno?errno:ESTALE;result=-1;continue;
        }
        s->unlock_attempted=1;
        if(syscall(SYS_flock,s->fd,LOCK_UN)<0){s->unlock_uncertain=1;s->error_number=errno;failed=1;result=-1;}
        else s->acquired=0;
    }
    return result;
}

static PyObject *unlock_canonical_slots(PyObject *self,PyObject *ignored) {
    (void)self;(void)ignored;
    if(!existing_parent_entry || !parent_custody || getpid()!=owner_pid){
        PyErr_SetString(PyExc_PermissionError,"canonical slot native caller absent");return NULL;
    }
    if(retire_native_slots()<0){
        for(int i=0;i<4;i++)if(canonical_slots[i].unlock_uncertain){errno=canonical_slots[i].error_number;break;}
        return PyErr_SetFromErrno(PyExc_OSError);
    }
    Py_RETURN_NONE;
}

static PyObject *canonical_slot_history(PyObject *self,PyObject *ignored) {
    (void)self;(void)ignored;
    PyObject *items=PyList_New(0);if(!items)return NULL;
    for(int i=0;i<4;i++){
        struct canonical_slot *s=&canonical_slots[i];
        PyObject *item=Py_BuildValue("{s:i,s:i,s:K,s:i,s:i,s:i,s:i,s:i}",
            "ordinal",i,"fd",s->fd,"generation",(unsigned long long)s->generation,
            "prepared",s->prepared,"acquired",s->acquired,"unlock_attempted",s->unlock_attempted,
            "unlock_uncertain",s->unlock_uncertain,"errno",s->error_number);
        if(!item||PyList_Append(items,item)<0){Py_XDECREF(item);Py_DECREF(items);return NULL;}
        /* Full held native struct stat remains in the fixed original owner;
         * this is an observation, not authority to reconstruct/close a slot.
         */
        PyObject *identity=Py_BuildValue("(KKKKKKLLL)",
            (unsigned long long)s->before_effect.st_dev,(unsigned long long)s->before_effect.st_ino,
            (unsigned long long)s->before_effect.st_mode,(unsigned long long)s->before_effect.st_uid,
            (unsigned long long)s->before_effect.st_gid,(unsigned long long)s->before_effect.st_nlink,
            (long long)s->before_effect.st_size,
            (long long)s->before_effect.st_mtim.tv_sec*1000000000+s->before_effect.st_mtim.tv_nsec,
            (long long)s->before_effect.st_ctim.tv_sec*1000000000+s->before_effect.st_ctim.tv_nsec);
        if(!identity||PyDict_SetItemString(item,"before_identity9",identity)<0){Py_XDECREF(identity);Py_DECREF(item);Py_DECREF(items);return NULL;}
        Py_DECREF(identity);Py_DECREF(item);
    }
    return items;
}
static PyObject *send_packet(PyObject *self,PyObject *args) {
    (void)self;int fd;Py_buffer raw;unsigned long long selected_end=0;
    if(!PyArg_ParseTuple(args,"iy*|K",&fd,&raw,&selected_end)) return NULL;
    if(actual_output>caps.max_output_bytes || (uint64_t)raw.len>caps.max_output_bytes-actual_output) {
        PyBuffer_Release(&raw);PyErr_SetString(PyExc_MemoryError,"native transport output");return NULL;
    }
    if(poll_io_until(fd,POLLOUT,(uint64_t)selected_end)<0) {PyBuffer_Release(&raw);return PyErr_SetFromErrno(PyExc_OSError);}
    ssize_t written=__real_send(fd,raw.buf,(size_t)raw.len,MSG_DONTWAIT|MSG_NOSIGNAL);
    if(written<0) {PyBuffer_Release(&raw);return PyErr_SetFromErrno(PyExc_OSError);}
    actual_output+=(uint64_t)written;
    PyBuffer_Release(&raw);
    if(!packet_end_live((uint64_t)selected_end))return PyErr_SetFromErrno(PyExc_OSError);
    return PyLong_FromSsize_t(written);
}
static PyObject *close_owned(PyObject *self,PyObject *arg) {
    (void)self;long fd=PyLong_AsLong(arg);if(fd==-1 && PyErr_Occurred()) return NULL;
    if(retire_fd((int)fd)<0) return PyErr_SetFromErrno(PyExc_OSError);
    Py_RETURN_NONE;
}
static PyObject *snapshot(PyObject *self,PyObject *ignored) {
    (void)self;(void)ignored;
    char digest[65];const char *hex="0123456789abcdef";
    for(int i=0;i<32;i++) {digest[i*2]=hex[caps.binding_sha256[i]>>4];digest[i*2+1]=hex[caps.binding_sha256[i]&15];}
    digest[64]=0;int fds=0,uncertain=0;
    for(int i=0;i<FD_MAX;i++) {fds+=fd_slots[i].active;uncertain+=fd_slots[i].uncertain;}
    PyObject *result=PyDict_New(),*value=NULL;
    if(!result) return NULL;
#define SET_U64(key,n) do {value=PyLong_FromUnsignedLongLong((unsigned long long)(n));if(!value || PyDict_SetItemString(result,key,value)<0) goto error;Py_CLEAR(value);} while(0)
#define SET_OBJECT(key,obj) do {if(PyDict_SetItemString(result,key,obj)<0) goto error;} while(0)
    value=PyUnicode_FromString(caps.role==1?"readonly-archive-scan":"actual-root-native-tool");
    if(!value || PyDict_SetItemString(result,"role",value)<0) goto error;Py_CLEAR(value);
    SET_OBJECT("preinitialization",initialized?Py_True:Py_False);
    SET_U64("max_fds",caps.max_fds);SET_U64("canonical_workers",caps.canonical_workers);
    SET_U64("max_live_bytes",caps.max_live_bytes);SET_U64("max_work_bytes",caps.max_work_bytes);
    SET_U64("max_read_bytes",caps.max_read_bytes);SET_U64("max_output_bytes",caps.max_output_bytes);
    SET_U64("max_wall_ms",caps.max_wall_ms);SET_U64("max_rss_bytes",caps.max_rss_bytes);
    SET_U64("physical_live_bytes",ordinary.live+terminal.live+registered_header_bytes());
    SET_U64("physical_peak_upper_bytes",ordinary.peak+terminal.peak+registered_header_bytes());
    SET_U64("allocation_work_bytes",ordinary.work+terminal.work);
    SET_U64("actual_read_bytes",actual_read);SET_U64("actual_output_bytes",actual_output);
    SET_U64("started_ns",started_ns);
    SET_U64("source_birth_started_ns",source_birth_started_ns);
    SET_OBJECT("selected_final_parent_preowned",scanner_root_backing_preowned?Py_True:Py_False);
    SET_OBJECT("outside_final_end_confirmed",Py_False);
    SET_U64("owner_pid",owner_pid);
    value=PyUnicode_FromString(existing_parent_entry?"existing-outside-native-parent.v2":"bounded-native-worker.v1");
    if(!value||PyDict_SetItemString(result,"entry_topology",value)<0)goto error;Py_CLEAR(value);
    SET_OBJECT("parent_end_reached",parent_end_reached?Py_True:Py_False);
    SET_OBJECT("parent_custody_claimed",parent_custody?Py_True:Py_False);
    SET_OBJECT("parent_delivery_accepted",parent_delivery_accepted?Py_True:Py_False);
    SET_OBJECT("existing_observer_entry",existing_observer_entry?Py_True:Py_False);
    SET_U64("existing_observer_entry_kind",existing_observer_entry);
    SET_OBJECT("native_result_accepted",parent_result_accepted?Py_True:Py_False);
    SET_U64("received_packet_history_count",received_count);
    SET_U64("native_caller_error_count",caller_error_count);
    value=canonical_slot_history(NULL,NULL);if(!value||PyDict_SetItemString(result,"canonical_slot_history",value)<0)goto error;Py_CLEAR(value);
    SET_U64("parent_custody_generation",parent_generation);
    SET_U64("preowned_original_started_ns",original_preowned_started_ns);
    SET_U64("preowned_original_end_ns",original_preowned_end_ns);
    SET_U64("preowned_original_generation",original_preowned_generation);
    value=PyLong_FromLong(observed_root_child);if(!value||PyDict_SetItemString(result,"preowned_root_child",value)<0)goto error;Py_CLEAR(value);
    value=PyLong_FromLong(observed_root_pidfd);if(!value||PyDict_SetItemString(result,"preowned_root_pidfd",value)<0)goto error;Py_CLEAR(value);
    SET_U64("preowned_root_generation",observed_root_generation);
    SET_U64("original_consumer_end_ns",original_consumer_end_ns);
    SET_U64("terminal_live_bytes",caps.terminal_live_bytes);
    SET_U64("terminal_work_bytes",caps.terminal_work_bytes);
    SET_U64("terminal_output_bytes",caps.terminal_output_bytes);
    SET_U64("terminal_wall_ms",terminal_wall_ms());
    value=PyLong_FromLong(root_completion_peer);if(!value||PyDict_SetItemString(result,"root_completion_peer",value)<0)goto error;Py_CLEAR(value);
    SET_U64("fds",fds);SET_U64("uncertain_fds",uncertain);
    value=PyList_New(0);if(!value)goto error;
    for(int i=0;i<FD_MAX;i++)if(fd_slots[i].uncertain){
        PyObject *item=Py_BuildValue("{s:i,s:i,s:s,s:O,s:O}","descriptor",fd_slots[i].fd,
            "errno",fd_slots[i].close_errno,"operation","native.detach-once.close",
            "attempted_once",Py_True,"charged",Py_True);
        if(!item||PyList_Append(value,item)<0){Py_XDECREF(item);goto error;}Py_DECREF(item);
    }
    if(PyDict_SetItemString(result,"close_uncertainties",value)<0)goto error;Py_CLEAR(value);
    value=PyLong_FromLong(pending_child);if(!value||PyDict_SetItemString(result,"pending_child",value)<0)goto error;Py_CLEAR(value);
    value=PyLong_FromLong(pending_pidfd);if(!value||PyDict_SetItemString(result,"pending_pidfd",value)<0)goto error;Py_CLEAR(value);
    SET_OBJECT("pending_source_reaped",pending_reaped?Py_True:Py_False);
    SET_OBJECT("source_birth_committed",source_birth_committed?Py_True:Py_False);
    value=PyLong_FromLong(pending_wait_status);if(!value||PyDict_SetItemString(result,"pending_source_wait_status",value)<0)goto error;Py_CLEAR(value);
    SET_OBJECT("failed",failed?Py_True:Py_False);SET_OBJECT("terminal",in_terminal?Py_True:Py_False);
    SET_OBJECT("selected_caller_returns",selected_caller_returns?Py_True:Py_False);
    SET_OBJECT("source_registry_retired",source_registry_retired?Py_True:Py_False);
    SET_OBJECT("source_registry_still_owned",source_registry_still_owned?Py_True:Py_False);
    SET_OBJECT("root_custody_returned_to_selected_caller",root_custody_returned_to_selected_caller?Py_True:Py_False);
    SET_OBJECT("outside_peer_adoption",Py_False);
    SET_OBJECT("registered_carrier_preowned",registered_carrier_preowned?Py_True:Py_False);
    SET_OBJECT("registered_carrier_accepted_before_birth",registered_carrier_accepted_before_birth?Py_True:Py_False);
    SET_OBJECT("source_plane_readonly_accepted",source_plane_readonly_accepted?Py_True:Py_False);
    SET_OBJECT("carrier_signal_scalars_retained",carrier_signal_scalars_retained?Py_True:Py_False);
    SET_OBJECT("cold_python_aliases_not_accepted",cold_python_aliases_not_accepted?Py_True:Py_False);
    SET_OBJECT("python_bodies_byte_exported",Py_False);
    SET_U64("plane_commit_kind",source_plane_note.commit_kind);
    SET_U64("plane_python_alias_count",source_plane_note.python_alias_count);
    SET_U64("plane_omitted_packet_bytes",source_plane_note.omitted_packet_bytes);
    SET_U64("plane_not_fit",source_plane_note.plane_not_fit);
    SET_U64("plane_body_used",source_plane_note.body_used);
    SET_U64("plane_body_capacity",source_plane_note.body_capacity);
    SET_U64("plane_range_count",source_plane_note.range_count);
    SET_U64("plane_generation",source_plane_note.note_generation);
    SET_U64("plane_confirmed_revoked",source_plane_note.confirmed_revoked);
    SET_U64("plane_packet_bytes_retained",source_plane_note.packet_bytes_retained);
    SET_OBJECT("source_allocator_storage_retained",source_backing_received?Py_True:Py_False);
    SET_U64("source_allocator_backing_bytes",expected_source_extent);
    SET_U64("source_allocator_backing_abi",SOURCE_BACKING_ABI);
    SET_U64("source_allocator_inspection_bytes",source_backing_inspection_bytes);
    value=PyUnicode_FromString(digest);
    if(!value || PyDict_SetItemString(result,"binding_sha256",value)<0) goto error;Py_CLEAR(value);
    return result;
error:
    Py_XDECREF(value);Py_DECREF(result);return NULL;
#undef SET_U64
#undef SET_OBJECT
}
static PyObject *begin_terminal(PyObject *self,PyObject *ignored) {
    (void)self;(void)ignored;in_terminal=1;Py_RETURN_NONE;
}
static PyObject *send_root_completion(PyObject *self,PyObject *args) {
    (void)self;Py_buffer raw;unsigned long long selected_end=0;
    if(caps.role!=2 || getpid()!=owner_pid || root_completion_peer<0) {
        PyErr_SetString(PyExc_PermissionError,"genuine existing Root completion peer absent");return NULL;
    }
    if(!PyArg_ParseTuple(args,"y*|K",&raw,&selected_end))return NULL;
    if(io_before((uint64_t)raw.len,1)<0 || poll_io_until(3,POLLOUT,(uint64_t)selected_end)<0) {
        PyBuffer_Release(&raw);return PyErr_SetFromErrno(PyExc_OSError);
    }
    ssize_t sent=__real_send(3,raw.buf,(size_t)raw.len,MSG_DONTWAIT|MSG_NOSIGNAL);
    if(sent>0)actual_output+=(uint64_t)sent;
    PyBuffer_Release(&raw);
    if(sent<0)return PyErr_SetFromErrno(PyExc_OSError);
    if(!packet_end_live((uint64_t)selected_end))return PyErr_SetFromErrno(PyExc_OSError);
    return PyLong_FromSsize_t(sent);
}
static PyObject *capture_root_ledger(PyObject *self,PyObject *args) {
    (void)self;PyObject *sequence;
    unsigned long long read_bytes,output_bytes;
    if(caps.role!=2 || getpid()!=owner_pid) {
        PyErr_SetString(PyExc_PermissionError,"actual native Root ledger owner absent");return NULL;
    }
    if(!PyArg_ParseTuple(args,"OKK",&sequence,&read_bytes,&output_bytes))return NULL;
    if(!PyTuple_Check(sequence)||PyTuple_GET_SIZE(sequence)!=5) {
        PyErr_SetString(PyExc_ValueError,"exact performed Root ledger shape");return NULL;
    }
    uint64_t values[8]={0};
    for(int i=0;i<5;i++) {
        values[i]=(uint64_t)PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(sequence,i));
        if(PyErr_Occurred())return NULL;
    }
    const uint64_t ceilings[5]={caps.max_fds,caps.max_read_bytes,caps.max_work_bytes,caps.max_live_bytes,caps.max_output_bytes};
    for(int i=0;i<5;i++)if(values[i]>ceilings[i]) {
        PyErr_SetString(PyExc_ValueError,"performed Root ledger exceeds original cap");return NULL;
    }
    if(read_bytes>values[1] || output_bytes>values[4]) {
        PyErr_SetString(PyExc_ValueError,"performed Root IO exceeds reservation");return NULL;
    }
    values[5]=(uint64_t)read_bytes;values[6]=(uint64_t)output_bytes;values[7]=1;
    memcpy(root_ledger,values,sizeof(root_ledger));Py_RETURN_NONE;
}
static PyObject *direct_source_wait(PyObject *self,PyObject *ignored) {
    (void)self;(void)ignored;
    if(caps.role!=2 || !existing_parent_entry || !parent_custody || getpid()!=owner_pid || pending_child<0 || pending_pidfd<0){
        PyErr_SetString(PyExc_PermissionError,"actual Source birth/direct-wait owner absent");return NULL;
    }
    if(!pending_reaped){
        if(!deadline()){PyErr_SetString(PyExc_TimeoutError,"original parent wait end exhausted; owner retained");return NULL;}
        siginfo_t observation={0};
        if(waitid(P_PIDFD,(id_t)pending_pidfd,&observation,WEXITED|WNOWAIT|WNOHANG)<0)
            return PyErr_SetFromErrno(PyExc_OSError);
        if(observation.si_pid==0)Py_RETURN_NONE;
        if(observation.si_pid!=pending_child){PyErr_SetString(PyExc_RuntimeError,"held pidfd/direct child mismatch");return NULL;}
        int status;struct rusage usage;sigset_t mask,previous;
        sigemptyset(&mask);sigaddset(&mask,SIGALRM);
        if(sigprocmask(SIG_BLOCK,&mask,&previous)<0)return PyErr_SetFromErrno(PyExc_OSError);
        pid_t waited=wait4(pending_child,&status,WNOHANG,&usage);
        int saved=errno;
        if(waited==pending_child){pending_wait_status=status;pending_usage=usage;pending_reaped=1;}
        int restored=sigprocmask(SIG_SETMASK,&previous,NULL);
        if(restored<0)return PyErr_SetFromErrno(PyExc_OSError);
        errno=saved;
        if(waited<0)return PyErr_SetFromErrno(PyExc_OSError);
        if(waited==0)Py_RETURN_NONE;
        if(waited!=pending_child){PyErr_SetString(PyExc_RuntimeError,"exact Source direct wait4 mismatch");return NULL;}
        /* Fixed fields precede ALL return-map/int/tuple allocations. */
    }
    return Py_BuildValue("ii{s:K,s:L,s:L}",pending_child,pending_wait_status,
        "RSS_bytes",(unsigned long long)pending_usage.ru_maxrss*1024,
        "kernel_input_blocks_wait4",(long long)pending_usage.ru_inblock,
        "kernel_output_blocks_wait4",(long long)pending_usage.ru_oublock);
}
static PyObject *direct_root_wait(PyObject *self,PyObject *args) {
    (void)self;int child,pidfd;
    if(caps.role!=2 || getpid()!=owner_pid){PyErr_SetString(PyExc_PermissionError,"actual existing Root caller owner absent");return NULL;}
    if(!PyArg_ParseTuple(args,"ii",&child,&pidfd))return NULL;
    if(child<1 || pidfd<0 || (observed_root_child>=0 && (observed_root_child!=child || observed_root_pidfd!=pidfd))){
        PyErr_SetString(PyExc_ValueError,"one exact original Root direct-wait generation required");return NULL;
    }
    if(!observed_root_reaped){
        if(!deadline()){PyErr_SetString(PyExc_TimeoutError,"original Root caller wait end exhausted; owner retained");return NULL;}
        siginfo_t observation={0};
        if(waitid(P_PIDFD,(id_t)pidfd,&observation,WEXITED|WNOWAIT|WNOHANG)<0)return PyErr_SetFromErrno(PyExc_OSError);
        if(observation.si_pid==0)Py_RETURN_NONE;
        if(observation.si_pid!=child){PyErr_SetString(PyExc_RuntimeError,"actual Root pidfd/direct child differs");return NULL;}
        sigset_t mask,previous;sigemptyset(&mask);sigaddset(&mask,SIGALRM);
        if(sigprocmask(SIG_BLOCK,&mask,&previous)<0)return PyErr_SetFromErrno(PyExc_OSError);
        int status;struct rusage usage;pid_t waited=wait4(child,&status,WNOHANG,&usage);int saved=errno;
        if(waited==child){observed_root_child=child;observed_root_pidfd=pidfd;
            observed_root_status=status;observed_root_usage=usage;observed_root_reaped=1;}
        int restored=sigprocmask(SIG_SETMASK,&previous,NULL);
        if(restored<0)return PyErr_SetFromErrno(PyExc_OSError);
        errno=saved;if(waited<0)return PyErr_SetFromErrno(PyExc_OSError);if(waited==0)Py_RETURN_NONE;
        if(waited!=child){PyErr_SetString(PyExc_RuntimeError,"actual Root direct wait4 mismatch");return NULL;}
    }
    return Py_BuildValue("ii{s:K,s:L,s:L}",observed_root_child,observed_root_status,
        "RSS_bytes",(unsigned long long)observed_root_usage.ru_maxrss*1024,
        "kernel_input_blocks_wait4",(long long)observed_root_usage.ru_inblock,
        "kernel_output_blocks_wait4",(long long)observed_root_usage.ru_oublock);
}

static int wait_owned_child_before_original_end(int child,int pidfd,int *reaped,
                                                int *saved_status,struct rusage *saved_usage) {
    if(*reaped || child<1)return 0;
    if(pidfd<0){errno=EBADF;return -1;}
    for(;;){
        uint64_t end=original_absolute_end();
        uint64_t now=now_ns();
        if(parent_end_reached || now>=end){errno=ETIMEDOUT;return -1;}
        int status;struct rusage usage;
        long waited=syscall(SYS_wait4,child,&status,WNOHANG,&usage);
        if(waited==child){*saved_status=status;*saved_usage=usage;*reaped=1;return 0;}
        if(waited<0)return -1;
        if(waited!=0){errno=ECHILD;return -1;}
        now=now_ns();if(parent_end_reached || now>=end){errno=ETIMEDOUT;return -1;}
        uint64_t remaining_ms=(end-now)/UINT64_C(1000000);
        struct pollfd held={pidfd,POLLIN,0};
        int ready=poll(&held,1,remaining_ms>INT_MAX?INT_MAX:(int)remaining_ms);
        if(ready<0){if(errno==EINTR && !parent_end_reached)continue;return -1;}
        if(held.revents&(POLLERR|POLLNVAL)){errno=EIO;return -1;}
        /* No WNOHANG zero, readable pidfd, ESRCH or timeout is a reap. Only
         * the next exact wait4 commit above retires the direct-parent edge. */
    }
}

static int retire_owned_children_before_original_end(void) {
    if(source_retirement_errno || root_retirement_errno){
        errno=root_retirement_errno?root_retirement_errno:source_retirement_errno;
        return -1;
    }
    int result=0;
    if(source_birth_inflight && pending_pidfd>=0 && source_birth_slot>=0){
        /* clone3 writes its actual created pidfd DIRECTLY into the preowned
         * original caller, before either interpreter return or task cutoff.
         * Recover the actual reserved description, never an inferred pidfd. */
        fd_slots[source_birth_slot].fd=pending_pidfd;
        source_birth_committed=1;
    }
    if(pending_pidfd>=0 && !pending_reaped && !source_cancel_attempted){
        source_cancel_attempted=1;
        if(syscall(SYS_pidfd_send_signal,pending_pidfd,SIGKILL,NULL,0)<0){
            source_cancel_errno=errno;
            if(errno!=ESRCH)result=-1;
        }
    }
    if(pending_pidfd>=0 && pending_child<1 && !pending_reaped){
        /* A cutoff can precede the clone3 return-value projection. The held
         * kernel pidfd, not a numeric guess, identifies that actual birth.
         * Only a genuine direct-parent waitid exit observation supplies PID. */
        while(pending_child<1){
            uint64_t end=original_absolute_end(),now=now_ns();
            if(parent_end_reached || now>=end){source_retirement_errno=ETIMEDOUT;result=-1;break;}
            siginfo_t child={0};
            if(waitid(P_PIDFD,(id_t)pending_pidfd,&child,WEXITED|WNOWAIT|WNOHANG)<0){source_retirement_errno=errno;result=-1;break;}
            if(child.si_pid){pending_child=child.si_pid;break;}
            uint64_t ms=(end-now)/UINT64_C(1000000);
            struct pollfd held={pending_pidfd,POLLIN,0};
            int ready=poll(&held,1,ms>INT_MAX?INT_MAX:(int)ms);
            if(ready<0 && errno!=EINTR){source_retirement_errno=errno;result=-1;break;}
            if(held.revents&(POLLERR|POLLNVAL)){source_retirement_errno=EIO;result=-1;break;}
        }
    }
    if(wait_owned_child_before_original_end(pending_child,pending_pidfd,&pending_reaped,
            &pending_wait_status,&pending_usage)<0){source_retirement_errno=errno;result=-1;}
    /* An observer must not kill a Root whose full original202/inner-Source/raw
     * ownership has never been accepted outside it. Consume its genuine direct
     * wait in the existing interval. Failure remains the explicit A180 C2
     * relation, not a claimed adoption or an authority to destroy that Root. */
    if(observed_root_child>=0 && !observed_root_relation_verified){root_retirement_errno=EPERM;result=-1;}
    else if(wait_owned_child_before_original_end(observed_root_child,observed_root_pidfd,&observed_root_reaped,
            &observed_root_status,&observed_root_usage)<0){root_retirement_errno=errno;result=-1;}
    return result;
}

static PyObject *retire_direct_children(PyObject *self,PyObject *ignored) {
    (void)self;(void)ignored;
    if(!existing_parent_entry || !parent_custody || getpid()!=owner_pid){
        PyErr_SetString(PyExc_PermissionError,"original preowned direct-child caller required");return NULL;
    }
    in_terminal=1;
    if(retire_owned_children_before_original_end()<0){
        failed=1;errno=root_retirement_errno?root_retirement_errno:
            source_retirement_errno?source_retirement_errno:source_cancel_errno;
        return PyErr_SetFromErrno(PyExc_OSError);
    }
    Py_RETURN_NONE;
}
static PyObject *spawn_source(PyObject *self,PyObject *args) {
    (void)self;PyObject *sequence,*argv_object,*environment;unsigned int uid,gid;
    sigset_t deadline_mask,previous_mask;int deadline_blocked=0;
    if(caps.role!=2 || geteuid()!=0 || !initialized || !existing_parent_entry || existing_observer_entry || !parent_custody || !parent_generation || !deadline() || !selected_caller_returns || !registered_carrier_accepted_before_birth || !source_plane_readonly_accepted) {
        PyErr_SetString(PyExc_PermissionError,"registered carrier and read-only source plane must already be accepted; bounded role2 birth refused");return NULL;
    }
    if(source_birth_committed || pending_child>=0 || pending_pidfd>=0){PyErr_SetString(PyExc_RuntimeError,"Source birth generation already owned or retired");return NULL;}
    if(!PyArg_ParseTuple(args,"OIIOO",&sequence,&uid,&gid,&argv_object,&environment)) return NULL;
    PyObject *fds=PySequence_Fast(sequence,"closed source descriptor tuple"),*argvs=NULL;
    if(!fds) return NULL;
    if(PySequence_Fast_GET_SIZE(fds)!=8 || !PyDict_Check(environment)) {
        Py_DECREF(fds);PyErr_SetString(PyExc_ValueError,"closed native launch shape");return NULL;
    }
    int source[8];
    for(int i=0;i<8;i++) {
        long fd=PyLong_AsLong(PySequence_Fast_GET_ITEM(fds,i));
        if(fd<0 || fd>INT_MAX || PyErr_Occurred()) {Py_DECREF(fds);return NULL;}
        source[i]=(int)fd;
        if(source[i]==scanner_root_backing_fds[0] || source[i]==scanner_root_backing_fds[1]){
            Py_DECREF(fds);PyErr_SetString(PyExc_PermissionError,"privileged Root backing can NEVER be an inherited Source description");return NULL;
        }
    }
    argvs=PySequence_Fast(argv_object,"fixed selected argv");
    if(!argvs) {Py_DECREF(fds);return NULL;}
    Py_ssize_t argc=PySequence_Fast_GET_SIZE(argvs),envc=PyDict_Size(environment);
    if(argc<1 || argc>128 || envc<0 || envc>128) {
        Py_DECREF(fds);Py_DECREF(argvs);PyErr_SetString(PyExc_ValueError,"fixed argv/environment bound");return NULL;
    }
    char *argv[129]={0},*env[129]={0};PyObject *env_strings[128]={0};
    PyObject *key,*value;Py_ssize_t position=0;int used=0;
    for(Py_ssize_t i=0;i<argc;i++) {
        argv[i]=(char *)PyUnicode_AsUTF8(PySequence_Fast_GET_ITEM(argvs,i));
        if(!argv[i] || strlen(argv[i])>65535) goto error;
    }
    while(PyDict_Next(environment,&position,&key,&value)) {
        const char *name=PyUnicode_AsUTF8(key),*text=PyUnicode_AsUTF8(value);
        if(!name || !text || strchr(name,'=') || strlen(name)+strlen(text)>65535) goto error;
        env_strings[used]=PyUnicode_FromFormat("%s=%s",name,text);
        if(!env_strings[used]) goto error;
        env[used]=(char *)PyUnicode_AsUTF8(env_strings[used]);if(!env[used]) goto error;used++;
    }
    /* One fixed table slot is reserved before clone3 can create its pidfd. */
    int native_slot=-1,used_fds=0;
    for(int i=0;i<FD_MAX;i++) used_fds+=fd_slots[i].active||fd_slots[i].uncertain;
    if((uint64_t)used_fds>=caps.max_fds) {PyErr_SetString(PyExc_OSError,"native pidfd pre-admission");goto error;}
    for(int i=0;i<FD_MAX;i++) if(!fd_slots[i].active && !fd_slots[i].uncertain) {native_slot=i;break;}
    if(native_slot<0) {PyErr_SetString(PyExc_OSError,"native descriptor table");goto error;}
    fd_slots[native_slot].active=1;fd_slots[native_slot].fd=-1;
    fd_slots[native_slot].generation=++fd_generation;
    pending_pidfd=-1;source_birth_inflight=1;source_birth_slot=native_slot;
    source_birth_started_ns=now_ns();
    if(!expected_source_wall_ms || expected_source_wall_ms>UINT64_C(3600000) ||
       source_birth_started_ns==UINT64_MAX ||
       expected_source_wall_ms>UINT64_MAX/UINT64_C(1000000) ||
       source_birth_started_ns>UINT64_MAX-expected_source_wall_ms*UINT64_C(1000000)){
        fd_slots[native_slot].active=0;source_birth_inflight=0;source_birth_slot=-1;
        PyErr_SetString(PyExc_RuntimeError,"selected original Source birth clock absent");goto error;
    }
    struct clone_args plan={0};plan.flags=CLONE_PIDFD;plan.pidfd=(uint64_t)(uintptr_t)&pending_pidfd;
    plan.exit_signal=SIGCHLD;
    /* Birth and held pidfd are atomic. If clone3 is absent/refused, no fallback
     * fork creates an unowned child and no numeric signal is used. */
    sigemptyset(&deadline_mask);sigaddset(&deadline_mask,SIGALRM);
    if(sigprocmask(SIG_BLOCK,&deadline_mask,&previous_mask)<0){fd_slots[native_slot].active=0;source_birth_inflight=0;source_birth_slot=-1;PyErr_SetFromErrno(PyExc_OSError);goto error;}
    deadline_blocked=1;
    pid_t child=(pid_t)syscall(SYS_clone3,&plan,sizeof(plan));
    if(child<0) {int saved=errno;fd_slots[native_slot].active=0;source_birth_inflight=0;source_birth_slot=-1;sigprocmask(SIG_SETMASK,&previous_mask,NULL);deadline_blocked=0;errno=saved;PyErr_SetFromErrno(PyExc_OSError);goto error;}
    if(child==0) {
        /* Async-signal-safe fixed C trampoline; no Python/arena allocation,
         * arbitrary preexec callback or inherited Root source lease survives. */
        if(sigprocmask(SIG_SETMASK,&previous_mask,NULL)<0)CHILD_REFUSAL(source[1],"child original deadline mask restoration failed");
        /* Enforce the original PRE-CLONE Source interval before descriptor
         * setup, not only after the fixed C trampoline happens to return. The
         * selected clock precedes actual birth; it is not a kernel birth sample.
         */
        uint64_t child_end=source_birth_started_ns+expected_source_wall_ms*UINT64_C(1000000);
        uint64_t child_now=now_ns();
        if(child_now>=child_end)CHILD_REFUSAL(source[1],"original Source birth interval exhausted");
        struct sigaction child_alarm={0};child_alarm.sa_handler=SIG_DFL;sigemptyset(&child_alarm.sa_mask);
        if(sigaction(SIGALRM,&child_alarm,NULL)<0)CHILD_REFUSAL(source[1],"child hard expiry installation failed");
        uint64_t child_remaining=child_end-child_now;
        struct itimerval child_expiry={0};child_expiry.it_value.tv_sec=(time_t)(child_remaining/UINT64_C(1000000000));
        child_expiry.it_value.tv_usec=(suseconds_t)((child_remaining%UINT64_C(1000000000))/1000);
        if(!child_expiry.it_value.tv_sec && !child_expiry.it_value.tv_usec)child_expiry.it_value.tv_usec=1;
        if(setitimer(ITIMER_REAL,&child_expiry,NULL)<0)CHILD_REFUSAL(source[1],"child original Source finite clock installation failed");
        for(int fd=1;fd<(int)caps.max_fds;fd++) {
            int keep=0;for(int j=0;j<8;j++) if(source[j]==fd) keep=1;
            if(!keep) __real_close(fd);
        }
        int copies[8];
        for(int i=0;i<8;i++) {
            copies[i]=__real_fcntl(source[i],F_DUPFD_CLOEXEC,32);
            if(copies[i]<0) CHILD_REFUSAL(source[1],"child descriptor copy failed");
        }
        for(int i=0;i<8;i++) if(__real_dup2(copies[i],i+1)<0) CHILD_REFUSAL(copies[1],"child fixed descriptor transfer failed");
        /* A selected source description may originally be FD0. Preserve it
         * until copies exist, then close old stdin before Source privilege. */
        if(__real_close(0)<0 && errno!=EBADF)CHILD_REFUSAL(2,"child original Root stdin close unconfirmed");
        if(__real_fcntl(6,F_SETFD,FD_CLOEXEC)<0) CHILD_REFUSAL(2,"child selected executable close-on-exec failed");
        if(syscall(SYS_close_range,9,UINT_MAX,0)<0)CHILD_REFUSAL(2,"child original Root descriptor isolation failed");
        struct rlimit limits={16,16};if(setrlimit(RLIMIT_NOFILE,&limits)<0) CHILD_REFUSAL(2,"child original Source16 isolation failed");
        /* Remain in the privileged fixed C trampoline until exec has erased
         * Root's heap/arenas/stack mappings. Dropping UID here would expose the
         * inherited Root backing to an unprivileged Source prefix. FD8 remains
         * the SAME original release gate; new image consumes its fixed record.
         * Timer uses the same pre-clone bound and survives exec, never release.
         */
        if(now_ns()>=child_end)CHILD_REFUSAL(2,"original Source pre-exec interval exhausted");
        syscall(SYS_execveat,6,"",argv,env,AT_EMPTY_PATH);
        CHILD_REFUSAL(2,"child held selected image execveat failed");
    }
    int pidfd=pending_pidfd;
    fd_slots[native_slot].fd=pidfd;
    pending_child=child;pending_reaped=0;pending_wait_status=0;source_birth_committed=1;source_birth_inflight=0;
    memset(&pending_usage,0,sizeof(pending_usage));
    if(sigprocmask(SIG_SETMASK,&previous_mask,NULL)<0){PyErr_SetFromErrno(PyExc_OSError);goto error;}
    deadline_blocked=0;
    for(int i=0;i<used;i++) Py_DECREF(env_strings[i]);
    Py_DECREF(fds);Py_DECREF(argvs);
    /* The fixed native owner owns birth even if Python cannot allocate its
     * return. Root's all-error owner retrieves the held pair from snapshot;
     * no blocking unbounded wait or numeric-PID kill occurs here. */
    PyObject *birth=Py_BuildValue("ii",child,pidfd);
    return birth;
error:
    if(deadline_blocked)sigprocmask(SIG_SETMASK,&previous_mask,NULL);
    for(int i=0;i<128;i++) Py_XDECREF(env_strings[i]);
    Py_DECREF(fds);Py_XDECREF(argvs);return NULL;
}
static PyObject *claim_existing_parent_custody(PyObject *self,PyObject *args) {
    (void)self;PyObject *owner;unsigned long long generation,start,end;
    if(!PyArg_ParseTuple(args,"OKKK",&owner,&generation,&start,&end))return NULL;
    if(!initialized || caps.role!=2 || !existing_parent_entry || getpid()!=owner_pid ||
       !generation || generation!=original_preowned_generation || start!=started_ns ||
       end!=original_preowned_end_ns || !deadline()){
        PyErr_SetString(PyExc_PermissionError,"exact original existing-parent generation/end absent");return NULL;
    }
    if(existing_observer_entry && observed_root_generation!=generation){
        PyErr_SetString(PyExc_RuntimeError,"preinitialization actual Root generation differs");return NULL;
    }
    if(parent_custody && (parent_custody!=owner || parent_generation!=generation)){
        PyErr_SetString(PyExc_RuntimeError,"original existing-parent custody cannot be replaced");return NULL;
    }
    if(!parent_custody){Py_INCREF(owner);parent_custody=owner;parent_generation=generation;}
    /* Fixed native reference is committed before any return allocation and
     * survives constructor, recorder, consumer and caller return failure. */
    Py_RETURN_NONE;
}
static PyObject *accept_existing_parent_delivery(PyObject *self,PyObject *args) {
    (void)self;PyObject *owner;unsigned long long generation;
    if(!PyArg_ParseTuple(args,"OK",&owner,&generation))return NULL;
    if(!initialized || !existing_parent_entry || getpid()!=owner_pid ||
       owner!=parent_custody || generation!=parent_generation || parent_delivery_accepted ||
       (pending_child>=0 && !pending_reaped) || !deadline()){
        PyErr_SetString(PyExc_RuntimeError,"exact existing-parent one-time accepted delivery required");return NULL;
    }
    parent_delivery_accepted=1;Py_RETURN_NONE;
}

static PyObject *bind_original_consumer_end(PyObject *self,PyObject *args) {
    (void)self;PyObject *owner;unsigned long long generation,end;
    if(!PyArg_ParseTuple(args,"OKK",&owner,&generation,&end))return NULL;
    if(!existing_parent_entry || owner!=parent_custody || generation!=parent_generation ||
       parent_end_reached || end<=now_ns() || end<=started_ns){
        PyErr_SetString(PyExc_RuntimeError,"one original selected minimum consumer end required");return NULL;
    }
    uint64_t own_end=original_preowned_end_ns;
    uint64_t selected_end=end<own_end?end:own_end;
    uint64_t current_end=atomic_load_explicit(&original_consumer_end_ns,memory_order_acquire);
    /* The actual selected parent now installs this immutable minimum before
     * backing validation/Python. The original public caller must REJOIN that
     * exact bound, rather than rejecting every meaningful positive call just
     * because its producer has already installed it. No refreshed interval.
     */
    if(!own_end || (current_end && selected_end>current_end) ||
       (original_consumer_bound_by_caller && selected_end!=current_end) ||
       (scanner_selected_enrollment &&
        selected_end!=scanner_selected_enrollment->original_minimum_consumer_end_ns)){
        PyErr_SetString(PyExc_RuntimeError,"original caller end disagrees with preowned selected minimum");return NULL;
    }
    original_consumer_bound_by_caller=1;
    if(selected_end==current_end)Py_RETURN_NONE;
    atomic_store_explicit(&original_consumer_end_ns,selected_end,memory_order_release);
    /* SHORTEN the timer to the exact original minimum, never refresh/extend.
     * Commit that bound BEFORE the fallible timer syscall/Python projection.
     */
    uint64_t now=now_ns();
    if(now>=original_consumer_end_ns){parent_end_reached=1;errno=ETIMEDOUT;return PyErr_SetFromErrno(PyExc_TimeoutError);}
    uint64_t remaining=original_consumer_end_ns-now;
    struct itimerval expiry={0};expiry.it_value.tv_sec=(time_t)(remaining/UINT64_C(1000000000));
    expiry.it_value.tv_usec=(suseconds_t)((remaining%UINT64_C(1000000000))/1000);
    if(!expiry.it_value.tv_sec && !expiry.it_value.tv_usec)expiry.it_value.tv_usec=1;
    if(setitimer(ITIMER_REAL,&expiry,NULL)<0)return PyErr_SetFromErrno(PyExc_OSError);
    /* The original native caller is already waiting on this word. Shortening
     * the selected end must wake that actual receiver, not leave it asleep on
     * an obsolete later work cutoff. No timer is refreshed or extended. */
    syscall(SYS_futex,&original_execution.clear_tid,FUTEX_WAKE,1,NULL,NULL,0);
    Py_RETURN_NONE;
}

static PyObject *accept_existing_parent_result(PyObject *self,PyObject *args) {
    (void)self;PyObject *owner,*result;unsigned long long generation;
    if(!PyArg_ParseTuple(args,"OKO",&owner,&generation,&result))return NULL;
    if(!initialized || !existing_parent_entry || getpid()!=owner_pid ||
       owner!=parent_custody || generation!=parent_generation || parent_result_accepted){
        PyErr_SetString(PyExc_RuntimeError,"one exact native final-caller result owner required");return NULL;
    }
    /* This accepts the actual raw/result/capsule graph into the fixed existing
     * C caller. It is NOT a peer ACK or a completed native-end observation.
     */
    Py_INCREF(result);parent_result=result;parent_result_accepted=1;
    Py_RETURN_NONE;
}

static PyObject *bind_observed_root(PyObject *self,PyObject *args) {
    (void)self;int child,pidfd;unsigned long long generation;
    if(!PyArg_ParseTuple(args,"iiK",&child,&pidfd,&generation))return NULL;
    if(!existing_observer_entry || !parent_custody || generation!=parent_generation ||
       getpid()!=owner_pid || child<1 || pidfd<0 || !deadline()){
        PyErr_SetString(PyExc_PermissionError,"one preowned actual observer direct-child generation required");return NULL;
    }
    if(observed_root_child>=0){
        if(observed_root_child!=child || observed_root_pidfd!=pidfd || observed_root_generation!=generation){
            PyErr_SetString(PyExc_RuntimeError,"original preinitialization Root child binding differs");return NULL;
        }
        Py_RETURN_NONE;
    }
    siginfo_t actual={0};
    if(waitid(P_PIDFD,(id_t)pidfd,&actual,WEXITED|WNOWAIT|WNOHANG)<0)return PyErr_SetFromErrno(PyExc_OSError);
    if(actual.si_pid && actual.si_pid!=child){PyErr_SetString(PyExc_RuntimeError,"observer held direct child mismatch");return NULL;}
    observed_root_child=child;observed_root_pidfd=pidfd;observed_root_generation=generation;
    observed_root_relation_verified=1;
    /* Borrowed pidfd/actual direct parenthood precede selector/JSON allocations.
     * Binding alone is not successful reap; direct_root_wait commits that.
     */
    Py_RETURN_NONE;
}
static PyObject *source_raw_custody(PyObject *self,PyObject *args) {
    (void)self;PyObject *owner=NULL;
    if(!PyArg_ParseTuple(args,"|O",&owner))return NULL;
    if(!initialized || getpid()!=owner_pid || (caps.role!=1 && !existing_parent_entry)){
        PyErr_SetString(PyExc_PermissionError,"same original initialized raw owner required");return NULL;
    }
    if(owner){
        if(source_raw_root && source_raw_root!=owner){
            PyErr_SetString(PyExc_RuntimeError,"original raw owner cannot be replaced");return NULL;
        }
        if(!source_raw_root){Py_INCREF(owner);source_raw_root=owner;}
    }
    if(source_raw_root){Py_INCREF(source_raw_root);return source_raw_root;}
    Py_RETURN_NONE;
}
static PyObject *retain_source_raw_exception(PyObject *self,PyObject *exc) {
    (void)self;
    if(!initialized || getpid()!=owner_pid || (caps.role!=1 && !existing_parent_entry) || !PyExceptionInstance_Check(exc)){
        PyErr_SetString(PyExc_RuntimeError,"exact preowned Source raw origin absent");return NULL;
    }
    if(caller_error_count>=16){
        caller_error_lane_exhausted=1;failed=1;
        /* Actual current thread-state root, not a fabricated policy exception.
         * A exhausted fixed lane cannot continue or claim accepted retirement. */
        PyErr_SetObject((PyObject *)Py_TYPE(exc),exc);return NULL;
    }
    struct caller_error *record=&caller_errors[caller_error_count++];
    Py_INCREF(exc);record->value=exc;
    Py_INCREF(Py_TYPE(exc));record->type=(PyObject *)Py_TYPE(exc);
    record->operation="SourceRawOwner.pending_recorder";record->error_number=errno;
    Py_RETURN_NONE;
}
static PyObject *accept_registered_carrier_before_birth(PyObject *self,PyObject *args) {
    (void)self;PyObject *owner;unsigned long long generation;unsigned i;
    if(!PyArg_ParseTuple(args,"OK",&owner,&generation)) return NULL;
    if(!initialized || caps.role!=2 || !existing_parent_entry || getpid()!=owner_pid ||
       !parent_custody || owner!=parent_custody || generation!=parent_generation || !deadline() || source_birth_committed){
        PyErr_SetString(PyExc_PermissionError,"existing parent carrier accept requires the claimed generation before birth");
        return NULL;
    }
    if(registered_carrier_accepted_before_birth){
        if(carrier_parent_alias!=owner){
            PyErr_SetString(PyExc_RuntimeError,"registered carrier generation cannot be replaced");
            return NULL;
        }
        Py_RETURN_NONE;
    }
    memcpy(carrier_fd_alias,fd_slots,FD_TABLE_BYTES);
    memcpy(carrier_canonical_alias,canonical_slots,CANONICAL_TABLE_BYTES);
    Py_INCREF(owner);carrier_parent_alias=owner;
    if(source_raw_root){Py_INCREF(source_raw_root);carrier_raw_alias=source_raw_root;}
    carrier_error_alias_count=0;
    for(i=0;i<caller_error_count && i<16;i++){
        if(caller_errors[i].value){
            Py_INCREF(caller_errors[i].value);
            carrier_error_alias[i]=caller_errors[i].value;
            carrier_error_alias_count=i+1;
        }
    }
    registered_carrier_preowned=1;
    registered_carrier_accepted_before_birth=1;
    Py_RETURN_NONE;
}
static PyObject *note_source_plane_readonly(PyObject *self,PyObject *args) {
    (void)self;unsigned long long extent,terminal_live,output,generation,source_wall;
    const char *binding;Py_ssize_t binding_length;
    if(!PyArg_ParseTuple(args,"KKKKy#K",&extent,&terminal_live,&output,&generation,&binding,&binding_length,&source_wall))return NULL;
    if(!initialized || caps.role!=2 || !existing_parent_entry || getpid()!=owner_pid ||
       !registered_carrier_accepted_before_birth || source_birth_committed ||
       binding_length!=32 || !generation || generation!=parent_generation ||
       extent>SIZE_MAX || extent<=PLANE_HEADER_BYTES || terminal_live==0 ||
       terminal_live>=extent-PLANE_HEADER_BYTES || output<ORIGINAL_PACKET_BODY_LIMIT ||
       !source_wall || source_wall>UINT64_C(3600000) || source_plane_readonly_accepted){
        PyErr_SetString(PyExc_PermissionError,"exact original Source allocator backing must be preowned before birth");
        return NULL;
    }
    if(scanner_root_backing_preowned &&
       (extent!=scanner_preowned_source_extent || !scanner_source_readonly_view ||
        memcmp(binding,scanner_preowned_source_binding,32))){
        PyErr_SetString(PyExc_RuntimeError,"Source handover must use the same native preowned full backing before Root initialization");return NULL;
    }
    expected_source_extent=(size_t)extent;expected_source_terminal=(size_t)terminal_live;
    expected_source_output=output;expected_source_generation=generation;
    expected_source_wall_ms=source_wall;
    memcpy(expected_source_binding,binding,32);
    source_plane_readonly_accepted=1;
    Py_RETURN_NONE;
}
static PyObject *borrow_preowned_source_backing(PyObject *self,PyObject *args) {
    (void)self;unsigned long long extent,generation;const char *binding;Py_ssize_t length;
    if(!PyArg_ParseTuple(args,"KKy#",&extent,&generation,&binding,&length))return NULL;
    if(!initialized || caps.role!=2 || !existing_parent_entry || !scanner_root_backing_preowned ||
       !scanner_source_readonly_view || source_birth_committed || !deadline() ||
       generation!=original_preowned_generation || length!=32 ||
       extent!=scanner_preowned_source_extent || memcmp(binding,scanner_preowned_source_binding,32)){
        PyErr_SetString(PyExc_PermissionError,"original native preowned Source backing absent or mismatched");return NULL;
    }
    /* Actual duplication is fixed-native registered before PyLong projection.
     * The original outside caller still owns the full read-only view/file even
     * when this projection/handover/close fails. No new data storage is created.
     */
    int fd=__wrap_dup(scanner_preowned_source_fds[0]);
    if(fd<0)return PyErr_SetFromErrno(PyExc_OSError);
    return PyLong_FromLong(fd);
}
static PyObject *preowned_source_backing_view(PyObject *self,PyObject *ignored) {
    (void)self;(void)ignored;
    if(caps.role!=2 || !scanner_root_backing_preowned || !scanner_source_readonly_view ||
       scanner_preowned_source_extent>PY_SSIZE_T_MAX || !deadline()){
        PyErr_SetString(PyExc_PermissionError,"original native read-only Source owner absent");return NULL;
    }
    /* Same preowned read-only physical mapping, not a second mmap/full copy.
     * Its fixed original native owner outlives BOTH receiving Python objects.
     */
    return PyMemoryView_FromMemory((char *)scanner_source_readonly_view,
                                  (Py_ssize_t)scanner_preowned_source_extent,PyBUF_READ);
}
/* Convert a Source virtual address to a parent-local byte range. Never cast
 * or dereference a Source PyObject/packet/block pointer in the parent process.
 * Parent reception is permitted only after the existing direct kernel wait.
 * The complete backing stays held even when a typed prefix cannot qualify. */
static int source_address_range(uint64_t address,size_t length,
                                const struct source_backing_custody *custody,size_t *offset) {
    if(address<custody->source_address)return -1;
    uint64_t delta=address-custody->source_address;
    if(delta<PLANE_HEADER_BYTES || delta>custody->extent || length>custody->extent-delta)return -1;
    *offset=(size_t)delta;return 0;
}
static int source_backing_geometry(const struct source_backing_custody *custody,size_t extent) {
    const struct owner_caps *selected=&custody->source_caps;
    if(custody->abi!=SOURCE_BACKING_ABI || custody->extent!=extent ||
       custody->generation!=expected_source_generation ||
       selected->magic!=OWNER_MAGIC || selected->version!=1 || selected->role!=1 ||
       selected->canonical_workers!=4 || selected->max_fds!=16 ||
       selected->max_live_bytes!=extent || selected->terminal_live_bytes!=expected_source_terminal ||
       selected->max_output_bytes!=expected_source_output ||
       memcmp(selected->binding_sha256,expected_source_binding,32)!=0 ||
       extent<=PLANE_HEADER_BYTES || expected_source_terminal>=extent-PLANE_HEADER_BYTES)return -1;
    size_t ordinary_size=extent-PLANE_HEADER_BYTES-expected_source_terminal;
    uint64_t ordinary_address=custody->source_address+PLANE_HEADER_BYTES;
    if(ordinary_address<custody->source_address)return -1;
    if((uint64_t)(uintptr_t)custody->arena_ordinary.base!=ordinary_address ||
       (uint64_t)(uintptr_t)custody->arena_terminal.base!=ordinary_address+ordinary_size ||
       custody->arena_ordinary.size!=ordinary_size ||
       custody->arena_terminal.size!=expected_source_terminal ||
       custody->arena_ordinary.used>ordinary_size || custody->arena_terminal.used>expected_source_terminal ||
       custody->arena_ordinary.live>ordinary_size || custody->arena_terminal.live>expected_source_terminal ||
       custody->error_count>16)return -1;
    return 0;
}
static int plane_ranges_match(const unsigned char *view,size_t view_len,const struct plane_note *note,
                              const struct source_backing_custody *custody) {
    if(note->body_offset!=PLANE_HEADER_BYTES || note->body_capacity!=view_len-PLANE_HEADER_BYTES ||
       note->table_offset!=sizeof(struct plane_note) || note->fd_table_bytes!=FD_TABLE_BYTES ||
       note->canonical_bytes!=CANONICAL_TABLE_BYTES || note->omitted_packet_bytes || note->plane_not_fit)return -1;
    uint64_t address=(uint64_t)(uintptr_t)custody->packet_first,retained=0;
    uint64_t visited=0,max_records=(view_len-PLANE_HEADER_BYTES)/sizeof(struct received_packet);
    while(address){
        struct received_packet packet;size_t offset,data_offset;
        if(visited++>=max_records || source_address_range(address,sizeof(packet),custody,&offset)<0)return -1;
        source_backing_inspection_bytes+=sizeof(packet);
        memcpy(&packet,view+offset,sizeof(packet));
        if((uint64_t)(uintptr_t)packet.data!=address+sizeof(packet))return -1;
        if(packet.capacity>custody->source_caps.max_output_bytes || packet.control_used>CONTROL_BYTES ||
           packet.count<0 || packet.count>10 || packet.received>(ssize_t)packet.capacity ||
           source_address_range((uint64_t)(uintptr_t)packet.data,packet.capacity,custody,&data_offset)<0)return -1;
        if(packet.received>0){
            if((uint64_t)packet.received>UINT64_MAX-retained)return -1;
            retained+=(uint64_t)packet.received;
        }
        address=(uint64_t)(uintptr_t)packet.next;
    }
    if(visited!=custody->packet_count || retained!=note->packet_bytes_retained)return -1;
    return 0;
}
static PyObject *accept_source_plane_bytes(PyObject *self,PyObject *args) {
    (void)self;Py_buffer view;struct plane_note note;struct source_backing_custody custody;
    if(!PyArg_ParseTuple(args,"y*",&view))return NULL;
    if(!initialized || caps.role!=2 || !existing_parent_entry || !source_plane_readonly_accepted ||
       !pending_reaped || view.buf==NULL || view.readonly!=1 ||
       view.len!=(Py_ssize_t)expected_source_extent || expected_source_extent<PLANE_HEADER_BYTES){
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_RuntimeError,"exact read-only original Source backing and direct reap required");
        return NULL;
    }
    source_backing_inspection_bytes+=sizeof(custody);
    memcpy(&custody,(const unsigned char *)view.buf+PLANE_CUSTODY_OFFSET,sizeof(custody));
    /* Geometry is native full-state correspondence, not final source admission.
     * Strong holder storage was registered before Source birth. */
    if(source_backing_geometry(&custody,(size_t)view.len)<0){
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_RuntimeError,"original Source backing geometry/binding differs");
        return NULL;
    }
    source_backing_received=1;
    source_backing_inspection_bytes+=sizeof(note);
    memcpy(&note,view.buf,sizeof(note));source_plane_note=note;
    if(note.magic!=PLANE_NOTE_MAGIC || note.outside_peer_adoption || note.python_bodies_byte_exported ||
       plane_ranges_match((const unsigned char *)view.buf,(size_t)view.len,&note,&custody)<0){
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_RuntimeError,"Source retained prefix is not a complete qualified terminal record");
        return NULL;
    }
    /* A late close may consume the backing fd and make pwrite(REVOKED) fail.
     * The actual shared native table, not the stale confirmed marker, decides.
     * No guessed-free fd or Source stderr-only live exemption hides uncertainty. */
    for(int i=0;i<FD_MAX;i++){
        struct fd_slot slot;
        const unsigned char *row=(const unsigned char *)view.buf+sizeof(struct plane_note)+(size_t)i*sizeof(slot);
        memcpy(&slot,row,sizeof(slot));
        source_backing_inspection_bytes+=sizeof(slot);
        if(slot.uncertain || (slot.active && slot.fd!=2)){
            PyBuffer_Release(&view);
            PyErr_SetString(PyExc_RuntimeError,"confirmed Source note conflicts with actual FD retirement");
            return NULL;
        }
    }
    for(int i=0;i<4;i++){
        struct canonical_slot slot;
        const unsigned char *row=(const unsigned char *)view.buf+sizeof(struct plane_note)+FD_TABLE_BYTES+(size_t)i*sizeof(slot);
        memcpy(&slot,row,sizeof(slot));
        source_backing_inspection_bytes+=sizeof(slot);
        if(slot.acquired || slot.unlock_uncertain){
            PyBuffer_Release(&view);
            PyErr_SetString(PyExc_RuntimeError,"confirmed Source note conflicts with actual canonical retirement");
            return NULL;
        }
    }
    PyBuffer_Release(&view);
    /* Uncertain end retains the actual bytes/roots but NEVER mints success. A
     * later ABI/native graph qualification must consume the retained prefix.
     * Both the warm Root end and actual outside native parent remain required. */
    if(note.commit_kind!=PLANE_CONFIRMED_RETIRED || note.source_registry_still_owned ||
       note.confirmed_revoked || custody.raw_source || custody.error_type ||
       custody.error_value || custody.error_traceback){
        PyErr_SetString(PyExc_RuntimeError,"Source allocator backing retained; retirement is not confirmed");
        return NULL;
    }
    for(unsigned i=0;i<custody.error_count;i++)
        if(custody.error_records[i].type || custody.error_records[i].value || custody.error_records[i].traceback){
            PyErr_SetString(PyExc_RuntimeError,"confirmed Source note conflicts with actual error roots");return NULL;
        }
    Py_RETURN_NONE;
}
static PyObject *source_backing_extent(PyObject *self,PyObject *args) {
    unsigned long long max_live,max_output;(void)self;
    if(!PyArg_ParseTuple(args,"KK",&max_live,&max_output))return NULL;
    if(max_live>SIZE_MAX || max_live<=PLANE_HEADER_BYTES || max_output<ORIGINAL_PACKET_BODY_LIMIT){
        PyErr_SetString(PyExc_RuntimeError,"native metadata and original packet cannot fit Source ceilings");
        return NULL;
    }
    /* Header + BOTH actual original allocator partitions use the SAME total
     * original Source ceiling. A preheld alias creates no second packet body
     * and is not output on the 2M wire. Its real mapping/read/work costs remain. */
    return PyLong_FromUnsignedLongLong(max_live);
}
static PyMethodDef methods[]={
    {"borrow_preowned_source_backing",borrow_preowned_source_backing,METH_VARARGS,NULL},
    {"preowned_source_backing_view",preowned_source_backing_view,METH_NOARGS,NULL},
    {"source_raw_custody",source_raw_custody,METH_VARARGS,NULL},
    {"retain_source_raw_exception",retain_source_raw_exception,METH_O,NULL},
    {"accept_registered_carrier_before_birth",accept_registered_carrier_before_birth,METH_VARARGS,NULL},
    {"note_source_plane_readonly",note_source_plane_readonly,METH_VARARGS,NULL},
    {"accept_source_plane_bytes",accept_source_plane_bytes,METH_VARARGS,NULL},
    {"snapshot",snapshot,METH_NOARGS,NULL},{"begin_terminal",begin_terminal,METH_NOARGS,NULL},
    {"recv_packet",receive_packet,METH_VARARGS,NULL},{"send_packet",send_packet,METH_VARARGS,NULL},
    {"spawn_source",spawn_source,METH_VARARGS,NULL},
    {"send_root_completion",send_root_completion,METH_VARARGS,NULL},
    {"capture_root_ledger",capture_root_ledger,METH_VARARGS,NULL},
    {"direct_source_wait",direct_source_wait,METH_NOARGS,NULL},
    {"direct_root_wait",direct_root_wait,METH_VARARGS,NULL},
    {"retire_direct_children",retire_direct_children,METH_NOARGS,NULL},
    {"claim_existing_parent_custody",claim_existing_parent_custody,METH_VARARGS,NULL},
    {"accept_existing_parent_delivery",accept_existing_parent_delivery,METH_VARARGS,NULL},
    {"accept_existing_parent_result",accept_existing_parent_result,METH_VARARGS,NULL},
    {"bind_observed_root",bind_observed_root,METH_VARARGS,NULL},
    {"bind_original_consumer_end",bind_original_consumer_end,METH_VARARGS,NULL},
    {"lock_canonical_slot",lock_canonical_slot,METH_VARARGS,NULL},
    {"unlock_canonical_slots",unlock_canonical_slots,METH_NOARGS,NULL},
    {"received_history",received_history,METH_NOARGS,NULL},
    {"close_fd",close_owned,METH_O,NULL},
    {"source_backing_extent",source_backing_extent,METH_VARARGS,NULL},
    {NULL,NULL,0,NULL}};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,"_friday_source_owner",NULL,-1,methods};
PyMODINIT_FUNC PyInit__friday_source_owner(void) {return PyModule_Create(&module);}

/* Shared pre-initialization implementation. Calling this from a Python
 * extension after initialization is expressly refused. Neither entry creates
 * another Root process, role, service, model, grant, or capacity domain. */
static int initialize_native_owner(int parent_entry) {
    if(initialized || Py_IsInitialized())return ENTRY_REFUSAL("native owner must precede interpreter initialization");
    existing_parent_entry=parent_entry;
    owner_pid=getpid();
    if((!source_caps_prechecked && pread(5,&caps,sizeof(caps),0)!=(ssize_t)sizeof(caps)) || caps.magic!=OWNER_MAGIC || caps.version!=1 || (caps.role!=1 && caps.role!=2) ||
       (caps.role==1 && caps.max_fds!=16) || (caps.role==2 && (caps.max_fds<207 || caps.max_fds>FD_MAX)) ||
       caps.canonical_workers!=4 || !caps.max_live_bytes || !caps.max_work_bytes ||
       !caps.max_read_bytes || !caps.max_output_bytes || !caps.max_wall_ms || !caps.max_rss_bytes || caps.max_wall_ms>3600000 ||
       !caps.terminal_live_bytes || !caps.terminal_work_bytes || !caps.terminal_output_bytes ||
       caps.terminal_live_bytes>=caps.max_live_bytes || caps.terminal_work_bytes>=caps.max_work_bytes ||
       caps.terminal_output_bytes>caps.max_output_bytes || caps.max_live_bytes>SIZE_MAX ||
       caps.max_rss_bytes>RLIM_INFINITY) return ENTRY_REFUSAL("native entry contract or system operation refused");
    int seals=fcntl(5,F_GET_SEALS);
    if(seals<0 || (seals&(F_SEAL_SEAL|F_SEAL_SHRINK|F_SEAL_GROW|F_SEAL_WRITE))!=(F_SEAL_SEAL|F_SEAL_SHRINK|F_SEAL_GROW|F_SEAL_WRITE)) return ENTRY_REFUSAL("native entry contract or system operation refused");
    struct stat issuer;
    if(fstat(5,&issuer)<0 || !S_ISREG(issuer.st_mode) || issuer.st_uid!=0 || issuer.st_nlink!=0 ||
       (caps.role==2 && geteuid()!=0) || (caps.role==1 && geteuid()==0)) return ENTRY_REFUSAL("native entry contract or system operation refused");
    if(caps.module_path_count<1 || caps.module_path_count>4 || caps.runtime_home[0]!='/' ||
       memchr(caps.runtime_home,0,sizeof(caps.runtime_home))==NULL) return ENTRY_REFUSAL("native entry contract or system operation refused");
    for(uint64_t i=0;i<caps.module_path_count;i++) {
        if(caps.module_paths[i][0]!='/' || memchr(caps.module_paths[i],0,sizeof(caps.module_paths[i]))==NULL) return ENTRY_REFUSAL("native entry contract or system operation refused");
    }
    struct rlimit nofile={(rlim_t)caps.max_fds,(rlim_t)caps.max_fds};if(setrlimit(RLIMIT_NOFILE,&nofile)<0) return ENTRY_REFUSAL("native entry contract or system operation refused");
    started_ns=existing_parent_entry?original_preowned_started_ns:
        (scanner_source_exec_trampoline?source_birth_started_ns:now_ns());
    if(existing_parent_entry && (!started_ns || !original_preowned_end_ns ||
            now_ns()>=original_preowned_end_ns))return ENTRY_REFUSAL("actual preowned original caller interval exhausted");
    if(existing_parent_entry && original_preowned_end_ns!=started_ns+caps.max_wall_ms*UINT64_C(1000000))
        return ENTRY_REFUSAL("preowned original interval differs from original admitted role ceiling");
    if(sizeof(caps)>caps.max_read_bytes)return ENTRY_REFUSAL("native entry contract or system operation refused");
    if(source_trampoline_read_bytes+scanner_preinit_read_bytes>caps.max_read_bytes-sizeof(caps))return ENTRY_REFUSAL("original Source/Root preinit read cap exhausted");
    actual_read=sizeof(caps)+source_trampoline_read_bytes+scanner_preinit_read_bytes;
    int diagnostic_flags=__real_fcntl(2,F_GETFL);
    if(diagnostic_flags<0||__real_fcntl(2,F_SETFL,diagnostic_flags|O_NONBLOCK)<0)
        return ENTRY_REFUSAL("native deadline diagnostic channel must be nonblocking");
    if(existing_parent_entry && caps.role!=2)return ENTRY_REFUSAL("existing parent must have original distinct Root role");
    if(existing_parent_entry && scanner_original_guard_installed){
        /* A203 NEW rejoin. The selected entry already armed this absolute minimum.
         * Do not install SIGALRM again and do not call setitimer. A second arm
         * would refresh the same interval. Refuse when that installed end is gone. */
        uint64_t installed_end=atomic_load_explicit(&original_consumer_end_ns,memory_order_acquire);
        uint64_t rejoined_end=original_absolute_end();
        if(parent_end_reached || !installed_end || !rejoined_end || rejoined_end!=installed_end ||
           now_ns()>=rejoined_end)
            return ENTRY_REFUSAL("installed original guard cannot be rejoined");
    }else{
        struct sigaction guard={0};guard.sa_handler=existing_parent_entry?existing_parent_deadline:hard_deadline;sigemptyset(&guard.sa_mask);
        if(sigaction(SIGALRM,&guard,NULL)<0)return ENTRY_REFUSAL("native entry contract or system operation refused");
        sigset_t original_end_signal;sigemptyset(&original_end_signal);sigaddset(&original_end_signal,SIGALRM);
        if(sigprocmask(SIG_UNBLOCK,&original_end_signal,NULL)<0)return ENTRY_REFUSAL("original hard-end signal cannot be blocked");
        uint64_t original_native_end=existing_parent_entry?original_absolute_end():started_ns+caps.max_wall_ms*UINT64_C(1000000);
        if(now_ns()>=original_native_end)return ENTRY_REFUSAL("original preinit Source/Root end exhausted");
        uint64_t remaining=original_native_end-now_ns();
        if(existing_parent_entry && now_ns()>=original_absolute_end())return ENTRY_REFUSAL("original caller end exhausted before native initialization");
        struct itimerval expiry={0};expiry.it_value.tv_sec=(time_t)(remaining/UINT64_C(1000000000));
        expiry.it_value.tv_usec=(suseconds_t)((remaining%UINT64_C(1000000000))/1000);
        if(!expiry.it_value.tv_sec && !expiry.it_value.tv_usec)expiry.it_value.tv_usec=1;
        if(setitimer(ITIMER_REAL,&expiry,NULL)<0)return ENTRY_REFUSAL("native entry contract or system operation refused");
    }
    if(!existing_parent_entry && receive_preowned_source_plane()<0)
        return ENTRY_REFUSAL("preowned source backing absent before interpreter initialization");
    if(caps.role==1){
        /* Already mapped Source-owned data and registry BEFORE Py_PreInitialize.
         * No anonymous Python/native body remains in this Source role. */
        if(!source_backing || !plane_base || plane_mapped_bytes!=caps.max_live_bytes ||
           caps.terminal_live_bytes>=caps.max_live_bytes-PLANE_HEADER_BYTES)
            return ENTRY_REFUSAL("original Source allocator backing cannot fit");
        ordinary.size=plane_mapped_bytes-PLANE_HEADER_BYTES-(size_t)caps.terminal_live_bytes;
        terminal.size=(size_t)caps.terminal_live_bytes;
        ordinary.base=plane_base+PLANE_HEADER_BYTES;
        terminal.base=ordinary.base+ordinary.size;
    }else{
        /* Root's privileged storage is NEVER inherited/mapped into Source. */
        if(scanner_root_backing_preowned){
            if(!scanner_root_backing || scanner_root_backing->extent!=caps.max_live_bytes ||
               caps.terminal_live_bytes>=caps.max_live_bytes-SCANNER_ROOT_HEADER_BYTES ||
               memcmp(&scanner_root_backing->original_caps,&caps,sizeof(caps))!=0)
                return ENTRY_REFUSAL("Root backing header/arenas do not fit SAME original sealed extent");
            ordinary.size=(size_t)caps.max_live_bytes-SCANNER_ROOT_HEADER_BYTES-(size_t)caps.terminal_live_bytes;
            terminal.size=(size_t)caps.terminal_live_bytes;
            ordinary.base=(unsigned char *)scanner_root_backing+SCANNER_ROOT_HEADER_BYTES;
            terminal.base=ordinary.base+ordinary.size;
        }else{
            ordinary.size=(size_t)(caps.max_live_bytes-caps.terminal_live_bytes);
            terminal.size=(size_t)caps.terminal_live_bytes;
            ordinary.base=mmap(NULL,ordinary.size,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
            terminal.base=mmap(NULL,terminal.size,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
            if(ordinary.base==MAP_FAILED || terminal.base==MAP_FAILED)
                return ENTRY_REFUSAL("native entry contract or system operation refused");
        }
    }
    if(inventory_inherited()<0)return ENTRY_REFUSAL("full inherited descriptor inventory failed");
    initialized=1;
    if(caps.role==2) {
        /* fd3 belongs to the independently selected already-existing parent,
         * not a new service/process or a Source-issued authority. Receipt
         * delivery is refused before launch without this real kernel channel. */
        struct ucred peer={0};socklen_t length=sizeof(peer);int kind=0;
        if(getsockopt(3,SOL_SOCKET,SO_PEERCRED,&peer,&length)<0 || length!=sizeof(peer) ||
           peer.pid!=getppid() || peer.uid!=0 || peer.gid!=0)
            return ENTRY_REFUSAL("existing actual Root completion parent custody absent");
        length=sizeof(kind);
        if(getsockopt(3,SOL_SOCKET,SO_TYPE,&kind,&length)<0 || kind!=SOCK_SEQPACKET)
            return ENTRY_REFUSAL("existing Root completion channel type differs");
        int flags=__real_fcntl(3,F_GETFL);
        if(flags<0 || __real_fcntl(3,F_SETFL,flags|O_NONBLOCK)<0)
            return ENTRY_REFUSAL("existing Root completion channel nonblocking policy absent");
        int credentials=1;
        if(setsockopt(3,SOL_SOCKET,SO_PASSCRED,&credentials,sizeof(credentials))<0)
            return ENTRY_REFUSAL("existing Root completion acceptance credentials absent");
        root_completion_peer=peer.pid;
    }
    if(caps.role==1)for(int fd=1;fd<=3;fd++) {
        int flags=__real_fcntl(fd,F_GETFL);
        if(flags<0||__real_fcntl(fd,F_SETFL,flags|O_NONBLOCK)<0)return ENTRY_REFUSAL("native entry contract or system operation refused");
    }
    PyMemAllocatorEx allocator={NULL,py_malloc,py_calloc,py_realloc,py_free};
    PyMem_SetAllocator(PYMEM_DOMAIN_RAW,&allocator);
    PyMem_SetAllocator(PYMEM_DOMAIN_MEM,&allocator);
    PyMem_SetAllocator(PYMEM_DOMAIN_OBJ,&allocator);
    PyObjectArenaAllocator arenas={NULL,py_arena,py_arena_free};PyObject_SetArenaAllocator(&arenas);
    if(PyImport_AppendInittab("_friday_source_owner",PyInit__friday_source_owner)<0) return ENTRY_REFUSAL("native entry contract or system operation refused");
    return 0;
}

/* Concrete C entry for the ALREADY EXISTING independently selected native
 * caller. Its selected image calls this before Py_PreInitialize, then invokes
 * tools.root_holder.existing_parent_source_entry in that SAME interpreter.
 * No Python API can turn a bounded main into this topology. Admission must
 * select the changed host image/callsite; Source does not issue that choice. */
int friday_existing_parent_preinitialize(void) {
    return initialize_native_owner(1);
}

static void hold_live_aliases_in_carrier(void) {
    unsigned i;
    if(!registered_carrier_accepted_before_birth) return;
    if(parent_custody && carrier_parent_alias==NULL){Py_INCREF(parent_custody);carrier_parent_alias=parent_custody;}
    if(source_raw_root && carrier_raw_alias==NULL){Py_INCREF(source_raw_root);carrier_raw_alias=source_raw_root;}
    if(parent_result && carrier_result_alias==NULL){Py_INCREF(parent_result);carrier_result_alias=parent_result;}
    for(i=0;i<caller_error_count && i<16;i++){
        if(caller_errors[i].value && carrier_error_alias[i]==NULL){
            Py_INCREF(caller_errors[i].value);
            carrier_error_alias[i]=caller_errors[i].value;
            if(i+1>carrier_error_alias_count) carrier_error_alias_count=i+1;
        }
    }
}

static int publish_source_plane(uint32_t kind) {
    ssize_t wrote;
    if(source_plane_fd<0 || !plane_base) return -1;
    if(kind==PLANE_CONFIRMED_RETIRED && (plane_placement_not_fit || plane_omitted_bytes)) return -1;
    plane_publish_generation++;
    refresh_plane_note(kind);
    /* Tables and packet bodies already live in the preheld mapping. The note
     * is the published state and is written last. */
    wrote=pwrite(source_plane_fd,&source_plane_note,sizeof(source_plane_note),0);
    if(wrote!=(ssize_t)sizeof(source_plane_note)) return -1;
    if(kind==PLANE_CONFIRMED_RETIRED) plane_full_commit=1;
    else plane_full_commit=0;
    return 0;
}

static int receive_preowned_source_plane(void) {
    unsigned char envelope[48]={0};
    union {struct cmsghdr alignment;unsigned char bytes[CONTROL_BYTES];} control;
    struct iovec iov={envelope,sizeof(envelope)};struct msghdr msg={0};
    struct cmsghdr *hdr;struct stat st;uint64_t generation;int fd=-1;
    msg.msg_iov=&iov;msg.msg_iovlen=1;msg.msg_control=control.bytes;msg.msg_controllen=sizeof(control.bytes);
    ssize_t got=recvmsg(3,&msg,MSG_DONTWAIT|MSG_CMSG_CLOEXEC);
    hdr=CMSG_FIRSTHDR(&msg);
    if(hdr && hdr->cmsg_level==SOL_SOCKET && hdr->cmsg_type==SCM_RIGHTS &&
       hdr->cmsg_len==CMSG_LEN(sizeof(int)))memcpy(&fd,CMSG_DATA(hdr),sizeof(fd));
    if(fd<0)return -1;
    generation=0;
    if(got==(ssize_t)sizeof(envelope))memcpy(&generation,envelope+8,sizeof(generation));
    int seals=__real_fcntl(fd,F_GET_SEALS);
    if(got!=(ssize_t)sizeof(envelope) || (msg.msg_flags&(MSG_TRUNC|MSG_CTRUNC)) ||
       memcmp(envelope,"FRCUSTO2",8) || !generation || memcmp(envelope+16,caps.binding_sha256,32) ||
       !hdr || CMSG_NXTHDR(&msg,hdr)!=NULL || caps.role!=1 ||
       caps.max_live_bytes>SIZE_MAX || caps.max_live_bytes<=PLANE_HEADER_BYTES ||
       caps.terminal_live_bytes>=caps.max_live_bytes-PLANE_HEADER_BYTES ||
       caps.max_output_bytes<ORIGINAL_PACKET_BODY_LIMIT || fstat(fd,&st)<0 ||
       st.st_size!=(off_t)caps.max_live_bytes || st.st_uid!=0 || st.st_nlink!=0 || !S_ISREG(st.st_mode) ||
       seals<0 || (seals&(F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL))!=(F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL)){
        __real_close(fd);return -1;
    }
    void *mapped=mmap(NULL,(size_t)caps.max_live_bytes,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
    if(mapped==MAP_FAILED){__real_close(fd);return -1;}
    plane_base=mapped;plane_mapped_bytes=(size_t)caps.max_live_bytes;
    plane_body=plane_base+PLANE_HEADER_BYTES;plane_body_capacity=plane_mapped_bytes-PLANE_HEADER_BYTES;
    source_backing=(struct source_backing_custody *)(plane_base+PLANE_CUSTODY_OFFSET);
    source_backing->abi=SOURCE_BACKING_ABI;source_backing->extent=plane_mapped_bytes;
    source_backing->generation=generation;source_backing->source_address=(uint64_t)(uintptr_t)plane_base;
    source_backing->source_caps=caps;
    ordinary_owner=&source_backing->arena_ordinary;terminal_owner=&source_backing->arena_terminal;
    ordinary.size=plane_mapped_bytes-PLANE_HEADER_BYTES-(size_t)caps.terminal_live_bytes;
    terminal.size=(size_t)caps.terminal_live_bytes;
    ordinary.base=plane_base+PLANE_HEADER_BYTES;terminal.base=ordinary.base+ordinary.size;
    expected_source_extent=plane_mapped_bytes;
    source_raw_owner=&source_backing->raw_source;
    parent_type_owner=&source_backing->error_type;parent_value_owner=&source_backing->error_value;
    parent_traceback_owner=&source_backing->error_traceback;
    caller_errors=source_backing->error_records;caller_count_owner=&source_backing->error_count;
    caller_lane_owner=&source_backing->error_lane_exhausted;
    received_first_owner=&source_backing->packet_first;received_last_owner=&source_backing->packet_last;
    received_count_owner=&source_backing->packet_count;
    fd_slots=(struct fd_slot *)(plane_base+sizeof(struct plane_note));
    canonical_slots=(struct canonical_slot *)(plane_base+sizeof(struct plane_note)+FD_TABLE_BYTES);
    source_plane_fd=fd;
    if(register_fd(fd)<0)return -1; /* mapped registry remains owned on failure */
    source_plane_note.magic=PLANE_NOTE_MAGIC;
    source_plane_note.body_offset=PLANE_HEADER_BYTES;source_plane_note.body_capacity=plane_body_capacity;
    source_plane_note.table_offset=sizeof(struct plane_note);source_plane_note.fd_table_bytes=FD_TABLE_BYTES;
    source_plane_note.canonical_bytes=CANONICAL_TABLE_BYTES;
    return 0;
}
static void place_received_packet_body(struct received_packet *record) {
    /* receive_packet allocated the record, exact body and ancillary/error
     * roots together through the already preheld actual Source allocator.
     * No late 32-byte range+body copy can shrink the original 2M packet.
     * Root follows only validated Source-address -> local-offset ranges. */
    if(caps.role!=1 || !source_backing || !record || record->received<=0)return;
    size_t offset,data_offset;
    if(source_address_range((uint64_t)(uintptr_t)record,sizeof(*record),source_backing,&offset)<0 ||
       source_address_range((uint64_t)(uintptr_t)record->data,record->capacity,source_backing,&data_offset)<0 ||
       (uint64_t)record->received>UINT64_MAX-plane_retained_bytes){
        plane_placement_not_fit=1;return;
    }
    plane_retained_bytes+=(uint64_t)record->received;plane_range_count++;
    plane_body_used=ordinary.used+terminal.used;
    plane_publish_generation++;
}

static int native_caller_destruct(int error_end) {
    /* One same-original-C consumer for every prefix. Unreaped, uncertain, and
     * hard-end facts force exit 125 AFTER the session is accepted. They do not
     * return a live owner, and they do not add a wait, role, or parking domain.
     */
    if(parent_native_consumer_entered)return 125;
    parent_native_consumer_entered=1;in_terminal=1;
    if(consume_native_packets()<0){failed=1;error_end=1;}
    /* The exact C child state is consumed even when the Python owner failed
     * during construction. This is a finite original-tail attempt, not legal
     * acceptance of an unreaped generation at the end. */
    if((initialized || original_preowned_end_ns) && retire_owned_children_before_original_end()<0){failed=1;error_end=1;}
    if(Py_IsInitialized() && parent_custody){
        PyObject *done=PyObject_CallMethod(parent_custody,"retire_before_native_end",NULL);
        if(!done){own_current_caller_error("fixed_native_caller.retire_before_native_end");failed=1;error_end=1;}
        else Py_DECREF(done);
    }
    if((pending_child>=0 && !pending_reaped) || (observed_root_child>=0 && !observed_root_reaped) ||
       caller_error_lane_exhausted || parent_end_reached || (initialized && !deadline())){
        failed=1;error_end=1;
    }
    if(retire_native_slots()<0){failed=1;error_end=1;}
    for(int i=0;i<FD_MAX;i++)if(fd_slots[i].uncertain){failed=1;error_end=1;}
    if(!error_end && (!parent_custody || !parent_result_accepted ||
       (existing_observer_entry!=2 && !parent_delivery_accepted) || root_ledger[7]!=1)){
        failed=1;error_end=1;
    }
    /* Confirm all currently registered description retirements BEFORE clearing
     * complete raw202/caller state. Close uncertainty must still own that graph.
     * This warm finite relation does not accept the unconfirmed hard-end prefix. */
    if(initialized)for(int i=0;i<FD_MAX;i++){
        int fd=fd_slots[i].fd;
        if(!fd_slots[i].active || fd<0 || fd==2 ||
           fd==scanner_root_backing_fds[0] || fd==scanner_root_backing_fds[1] ||
           fd==scanner_preowned_source_fds[0] || fd==scanner_preowned_source_fds[1])continue;
        if((fd==pending_pidfd && pending_child>=0 && !pending_reaped) ||
           (fd==observed_root_pidfd && observed_root_child>=0 && !observed_root_reaped))continue;
        if(retire_fd(fd)<0){failed=1;error_end=1;}
    }
    /* A failed/uncertain prefix is NOT permission to erase its raw graphs.
     * Leave the complete VM, caller roots and original arenas in the SAME
     * preowned native process. The eventual process/end survival relation
     * remains A180-C2; this retention is not claimed as accepted adoption. */
    /* A typed/recorder refusal alone need not strand an otherwise confirmed
     * original native generation. Attempt finite warm error retirement only
     * while every child/slot/description retirement is actually confirmed.
     * Failed metrics remain failed; this establishes no external transfer. */
    int retirement_uncertain=(pending_child>=0 && !pending_reaped) ||
        (observed_root_child>=0 && !observed_root_reaped) || caller_error_lane_exhausted ||
        parent_end_reached || !initialized || !deadline() || !parent_custody;
    for(int i=0;i<FD_MAX;i++)if(fd_slots[i].uncertain ||
        (fd_slots[i].active && fd_slots[i].fd>=0 && fd_slots[i].fd!=2 &&
         fd_slots[i].fd!=scanner_root_backing_fds[0] && fd_slots[i].fd!=scanner_root_backing_fds[1] &&
         fd_slots[i].fd!=scanner_preowned_source_fds[0] && fd_slots[i].fd!=scanner_preowned_source_fds[1]))retirement_uncertain=1;
    for(int i=0;i<4;i++)if(canonical_slots[i].acquired || canonical_slots[i].unlock_uncertain)retirement_uncertain=1;
    int can_finalize=!retirement_uncertain;
    if(Py_IsInitialized() && registered_carrier_accepted_before_birth) hold_live_aliases_in_carrier();
    if(Py_IsInitialized() && can_finalize){
        if(scanner_root_backing){
            Py_CLEAR(scanner_root_backing->root_call_result);
            Py_CLEAR(scanner_root_backing->root_function);
            Py_CLEAR(scanner_root_backing->root_module);
            Py_CLEAR(scanner_root_backing->root_materials);
            /* Exact actual same-body aliases, not a second static/private
             * carrier. Only confirmed warm retirement may clear these roots.
             * Unconfirmed prefixes leave all aliases physically preowned.
             */
            Py_CLEAR(carrier_parent_alias);Py_CLEAR(carrier_raw_alias);
            Py_CLEAR(carrier_result_alias);
            for(unsigned i=0;i<carrier_error_alias_count && i<16;i++)
                Py_CLEAR(carrier_error_alias[i]);
        }
        Py_CLEAR(source_raw_root);
        Py_CLEAR(parent_result);Py_CLEAR(parent_custody);
        Py_CLEAR(parent_error_type);Py_CLEAR(parent_error_value);Py_CLEAR(parent_error_traceback);
        for(unsigned i=0;i<caller_error_count;i++){
            Py_CLEAR(caller_errors[i].type);Py_CLEAR(caller_errors[i].value);Py_CLEAR(caller_errors[i].traceback);
        }
        for(struct received_packet *r=received_first;r;r=r->next){
            Py_CLEAR(r->error_type);Py_CLEAR(r->error_value);Py_CLEAR(r->error_traceback);
        }
    }
    if(Py_IsInitialized() && can_finalize){source_registry_retired=1;source_registry_still_owned=0;}
    else if(source_raw_root || caller_error_count || parent_error_type || parent_error_value || parent_error_traceback || parent_custody || parent_result)source_registry_still_owned=1;
    int finalized=Py_IsInitialized() && can_finalize?Py_FinalizeEx():-1;
    /* Skip stderr until the trailer. Skip an unreaped direct pidfd: process
     * exit closes it once. Every other known active description is retired
     * once. Uncertain slots were already recorded and are not retried.
     */
    if(initialized)for(int i=0;i<FD_MAX;i++){
        int fd=fd_slots[i].fd;
        if(!fd_slots[i].active || fd<0 || fd==2 ||
           fd==scanner_root_backing_fds[0] || fd==scanner_root_backing_fds[1] ||
           fd==scanner_preowned_source_fds[0] || fd==scanner_preowned_source_fds[1])continue;
        if((fd==pending_pidfd && pending_child>=0 && !pending_reaped) ||
           (fd==observed_root_pidfd && observed_root_child>=0 && !observed_root_reaped))continue;
        if(retire_fd(fd)<0){failed=1;error_end=1;}
    }
    if(error_end)failed=1;
    int metrics=initialized?emit_final_native_metrics(finalized):-1;
    parent_native_retired=1;
    return finalized==0 && metrics==0 && !failed && !error_end?0:125;
}

int friday_existing_parent_finalize(void) {
    if(!existing_parent_entry || !initialized || getpid()!=owner_pid)return 125;
    int error_end=!parent_result_accepted || (existing_observer_entry!=2 && !parent_delivery_accepted) || failed;
    return native_caller_destruct(error_end);
}

int friday_existing_parent_error_end(int original_error) {
    /* Every selected failure, including preinit, init, and callback, reaches
     * the same destructor. A partial interpreter is finalized here. Refused
     * caps stay unforged: native216 is emitted only after initialized=1.
     */
    if(parent_native_consumer_entered)return 125;
    if(Py_IsInitialized())own_current_caller_error("selected_native_run.error_end");
    (void)original_error;return native_caller_destruct(1);
}

static int initialize_interpreter(void) {
    PyConfig config;PyConfig_InitIsolatedConfig(&config);
    config.site_import=0;config.use_environment=0;config.write_bytecode=0;config.install_signal_handlers=0;
    PyStatus status=PyConfig_SetBytesString(&config,&config.home,caps.runtime_home);
    if(PyStatus_Exception(status)) {int refusal=ENTRY_REFUSAL(status.err_msg);PyConfig_Clear(&config);return refusal;}
    config.module_search_paths_set=1;
    for(uint64_t i=0;i<caps.module_path_count;i++) {
        wchar_t *path=Py_DecodeLocale(caps.module_paths[i],NULL);
        if(!path) {PyConfig_Clear(&config);return ENTRY_REFUSAL("native entry contract or system operation refused");}
        status=PyWideStringList_Append(&config.module_search_paths,path);PyMem_RawFree(path);
        if(PyStatus_Exception(status)) {int refusal=ENTRY_REFUSAL(status.err_msg);PyConfig_Clear(&config);return refusal;}
    }
    status=Py_InitializeFromConfig(&config);PyConfig_Clear(&config);
    if(PyStatus_Exception(status)) return ENTRY_REFUSAL(status.err_msg);
    PyObject *native=PyImport_ImportModule("_friday_source_owner");
    if(!native) {own_current_caller_error("initialize_interpreter.import_native_owner");return ENTRY_REFUSAL("builtin native owner initialization failed; raw Python origin remains caller-owned");}Py_DECREF(native);
    return 0;
}

static void *run_original_interpreter_execution(void *ignored) {
    (void)ignored;
    /* Register the actual kernel exit word BEFORE the first fallible task
     * setup. SIGUSR2 remains blocked by the original creator until this word
     * is installed; no local startup flag is substituted for kernel exit.
     */
    int tid=(int)syscall(SYS_gettid);
    if(tid<=0){
        original_execution.startup_errno=errno;original_execution.result=125;
        atomic_store_explicit(&original_execution.finished,1,memory_order_release);
        atomic_store_explicit(&original_execution.started,-1,memory_order_release);
        syscall(SYS_exit,125);__builtin_unreachable();
    }
    atomic_store_explicit(&original_execution.clear_tid,tid,memory_order_relaxed);
    /* The kernel itself clears this preowned word and wakes the native caller
     * when this task actually exits. A request/flag is never treated as exit.
     * glibc thread/TLS storage remains charged until this Root process ends;
     * no pthread_join/deallocator can touch a canceled Python arena lock. */
    if(syscall(SYS_set_tid_address,&original_execution.clear_tid)<0){
        original_execution.startup_errno=errno;original_execution.result=125;
        atomic_store_explicit(&original_execution.finished,1,memory_order_release);
        atomic_store_explicit(&original_execution.started,-1,memory_order_release);
        syscall(SYS_exit,125);__builtin_unreachable();
    }
    atomic_store_explicit(&original_execution.thread_tid,tid,memory_order_release);
    atomic_store_explicit(&original_execution.started,1,memory_order_release);
    sigset_t end_signal,work_signal;
    sigemptyset(&end_signal);sigaddset(&end_signal,SIGALRM);
    sigemptyset(&work_signal);sigaddset(&work_signal,SIGUSR2);
    int setup_error=pthread_sigmask(SIG_BLOCK,&end_signal,NULL);
    if(!setup_error)setup_error=pthread_sigmask(SIG_UNBLOCK,&work_signal,NULL);
    if(setup_error || parent_end_reached || now_ns()>=original_absolute_end()){
        original_execution.startup_errno=setup_error?setup_error:ETIMEDOUT;
        original_execution.result=125;
        atomic_store_explicit(&original_execution.finished,1,memory_order_release);
        atomic_store_explicit(&original_execution.thread_tid,0,memory_order_release);
        syscall(SYS_exit,125);__builtin_unreachable();
    }
    int result=initialize_interpreter();
    if(!result){
        original_execution.raw_thread_state=PyThreadState_Get();
        result=original_execution.selected_entry();
    }
    if(Py_IsInitialized() && PyErr_Occurred()){
        PyErr_Fetch(&parent_error_type,&parent_error_value,&parent_error_traceback);
        result=125;
    }
    original_execution.result=result?friday_existing_parent_error_end(result):friday_existing_parent_finalize();
    atomic_store_explicit(&original_execution.finished,1,memory_order_release);
    /* No NPTL/TLS/atexit/Python epilogue follows the native final trailer.
     * The fixed task stack and native bookkeeping remain caller-owned. */
    atomic_store_explicit(&original_execution.thread_tid,0,memory_order_release);
    syscall(SYS_exit,original_execution.result);__builtin_unreachable();
}

static int wait_original_execution_before(uint64_t end,int work_phase) {
    for(;;){
        int tid=atomic_load_explicit(&original_execution.clear_tid,memory_order_acquire);
        int started=atomic_load_explicit(&original_execution.started,memory_order_acquire);
        if(started>0 && tid==0)return 0;
        if(started<0){errno=EIO;return -1;}
        uint64_t actual_end=original_absolute_end();
        if(work_phase){
            uint64_t reserve=terminal_wall_ms()*UINT64_C(1000000);
            if(actual_end<=reserve){errno=ETIMEDOUT;return -1;}
            actual_end-=reserve;
        }
        if(actual_end<end)end=actual_end;
        uint64_t now=now_ns();
        if(parent_end_reached || now>=end){errno=ETIMEDOUT;return -1;}
        uint64_t remaining=end-now;
        /* Before the child installed its own clear_tid word, wait only on
         * the original finite cutoff. No zero word implies a started task. */
        if(started==0 && remaining>UINT64_C(1000000))remaining=UINT64_C(1000000);
        struct timespec interval={(time_t)(remaining/UINT64_C(1000000000)),
            (long)(remaining%UINT64_C(1000000000))};
        long waited=syscall(SYS_futex,&original_execution.clear_tid,FUTEX_WAIT,tid,&interval,NULL,0);
        if(waited<0 && errno!=EAGAIN && errno!=EINTR && errno!=ETIMEDOUT)return -1;
    }
}

static int native_cold_original_caller_consume(void) {
    /* This is the actual original native receiver of the complete raw VM,
     * stack, all caller/thread-state roots, FD/slot tables and direct children.
     * It never invokes Python or the potentially interrupted arena mutex.
     * Root202 is not duplicated, serialized into a byte-only observer, or
     * discarded with an interpreter task. Existing process-level aliases and
     * all raw origins remain physically charged until Root's actual exit. */
    if(native_cold_consumer_entered)return 125;
    if(atomic_load_explicit(&original_execution.started,memory_order_acquire)<=0 ||
       atomic_load_explicit(&original_execution.clear_tid,memory_order_acquire)!=0){failed=1;return 125;}
    native_cold_consumer_entered=1;parent_native_consumer_entered=1;in_terminal=1;failed=1;
    if(consume_native_packets()<0)failed=1;
    if(retire_owned_children_before_original_end()<0)failed=1;
    if(retire_native_slots()<0)failed=1;
    for(int i=0;i<FD_MAX;i++){
        int fd=fd_slots[i].fd;
        if(!fd_slots[i].active || fd<0 || fd==2 ||
           fd==scanner_root_backing_fds[0] || fd==scanner_root_backing_fds[1] ||
           fd==scanner_preowned_source_fds[0] || fd==scanner_preowned_source_fds[1])continue;
        if((fd==pending_pidfd && pending_child>=0 && !pending_reaped) ||
           (fd==observed_root_pidfd && observed_root_child>=0 && !observed_root_reaped))continue;
        if(retire_fd(fd)<0)failed=1;
    }
    /* Explicitly -1/failed: no Py_FinalizeEx was attempted on interrupted
     * interpreter state. It cannot earn the normal finalization/ACK/runtime
     * relation. The cold owner preserves every actual raw graph until exit. */
    if(emit_final_native_metrics(-1)<0)failed=1;
    parent_native_retired=1;
    if(source_raw_root || caller_error_count || parent_error_type || parent_error_value || parent_error_traceback || parent_custody || parent_result)
        cold_python_aliases_not_accepted=1;
    carrier_signal_scalars_retained=1;
    source_registry_still_owned=1;
    root_custody_returned_to_selected_caller=1;
    return 125;
}

/* One original absolute guard, installed before backing validation/mapping,
 * cap reads, interpreter construction or child/task birth. Rejoining the
 * performing call path may not create or refresh a second interval. This
 * controls this native path, not the missing external issuer's own final end.
 */
static int install_original_caller_guard(uint64_t start,uint64_t end,
                                         uint64_t generation,uint64_t minimum_end) {
    uint64_t now=now_ns();
    if(!generation || !start || start>now || end<=now || end<=start ||
       end-start>UINT64_C(3600000000000) || minimum_end<=now || minimum_end>end ||
       minimum_end<=start){errno=ETIMEDOUT;return -1;}
    if(scanner_original_guard_installed){
        if(original_preowned_started_ns!=start || original_preowned_end_ns!=end ||
           original_preowned_generation!=generation ||
           atomic_load_explicit(&original_consumer_end_ns,memory_order_acquire)!=minimum_end ||
           parent_end_reached){errno=EPERM;return -1;}
        return 0;
    }
    original_preowned_started_ns=start;original_preowned_end_ns=end;
    original_preowned_generation=generation;started_ns=start;
    atomic_store_explicit(&original_consumer_end_ns,minimum_end,memory_order_release);
    struct sigaction guard={0};guard.sa_handler=existing_parent_deadline;sigemptyset(&guard.sa_mask);
    if(sigaction(SIGALRM,&guard,NULL)<0)return -1;
    sigset_t end_signal;sigemptyset(&end_signal);sigaddset(&end_signal,SIGALRM);
    if(sigprocmask(SIG_UNBLOCK,&end_signal,NULL)<0)return -1;
    now=now_ns();
    if(parent_end_reached || now>=minimum_end){errno=ETIMEDOUT;return -1;}
    uint64_t remaining=minimum_end-now;
    struct itimerval expiry={0};expiry.it_value.tv_sec=(time_t)(remaining/UINT64_C(1000000000));
    expiry.it_value.tv_usec=(suseconds_t)((remaining%UINT64_C(1000000000))/1000);
    if(!expiry.it_value.tv_sec && !expiry.it_value.tv_usec)expiry.it_value.tv_usec=1;
    if(setitimer(ITIMER_REAL,&expiry,NULL)<0)return -1;
    scanner_original_guard_installed=1;
    if(parent_end_reached || now_ns()>=minimum_end){errno=ETIMEDOUT;return -1;}
    return 0;
}

/* Actual C caller, already in the independently selected existing Root image.
 * selected_root_entry is the ORIGINAL REVIEWED PROVIDER/ISSUER callsite which
 * invokes tools.root_holder.launch_selected_source with its actual opaque
 * permit/held selections. It is NOT a Python callback, dynamic ELF/preload,
 * Source-issued issuer, JSON argument, new process or new resource allowance.
 * That exact full loaded callsite relation remains independently qualified.
 */
#define RETURN_SELECTED(status) do { \
    if(registered_carrier_accepted_before_birth){ \
        memcpy(carrier_fd_alias,fd_slots,FD_TABLE_BYTES); \
        memcpy(carrier_canonical_alias,canonical_slots,CANONICAL_TABLE_BYTES); \
    } \
    root_custody_returned_to_selected_caller=1; \
    if(!source_registry_retired && (source_raw_root || caller_error_count || parent_error_type || parent_error_value || parent_error_traceback || parent_custody || parent_result)) source_registry_still_owned=1; \
    int selected_status=(status); \
    if(scanner_root_backing_preowned) selected_status=scanner_final_root_receiver(selected_status); \
    if(atomic_load_explicit(&original_execution.started,memory_order_acquire)>0 && \
       atomic_load_explicit(&original_execution.clear_tid,memory_order_acquire)==0) \
        atomic_store_explicit(&original_execution.thread_tid,0,memory_order_release); \
    if(scanner_final_result){ \
        scanner_final_result->original_guard_armed=scanner_original_guard_installed; \
        scanner_final_result->original_end_reached=parent_end_reached; \
        if(parent_end_reached || now_ns()>=original_absolute_end()){ \
            selected_status=125;scanner_final_result->late_error=1; \
            scanner_final_result->receiver_errno=ETIMEDOUT; \
            scanner_final_result->state=FRIDAY_SCANNER_PARENT_UNCONFIRMED; \
        } \
        scanner_final_result->status=selected_status; \
        if(scanner_root_backing_preowned && scanner_final_receiver_entered && \
           scanner_final_result->task_exited) \
            scanner_root_backing->parent_terminal_result=*scanner_final_result; \
    } \
    /* Never disarm the original guard or replace its handler merely because \
     * a local return occurred. The preowned outside caller still owns the \
     * original registered bodies, descriptions and any unconfirmed child. */ \
    return selected_status; \
} while(0)
static int run_selected_existing_caller(int (*selected_root_entry)(void),int observer_kind,
                                       uint64_t original_start,uint64_t original_end,uint64_t generation,
                                       int original_root_child,int original_root_pidfd) {
    if(initialized || Py_IsInitialized() || parent_native_consumer_entered)
        return ENTRY_REFUSAL("independent native generation cannot reuse an active/retired session");
    existing_observer_entry=observer_kind;
    existing_parent_entry=1;owner_pid=getpid();
    selected_caller_returns=1;
    /* This is an actual ORIGINAL selected native producer's immutable bound,
     * not a Source-created budget. Commit it before caps reads, parenthood
     * checks, allocator initialization, constructors, imports or callbacks. */
    uint64_t minimum_end=scanner_selected_enrollment?
        scanner_selected_enrollment->original_minimum_consumer_end_ns:original_end;
    if(install_original_caller_guard(original_start,original_end,generation,minimum_end)<0)
        RETURN_SELECTED(125);
    if(observer_kind){observed_root_child=original_root_child;observed_root_pidfd=original_root_pidfd;observed_root_generation=generation;}
    uint64_t first_now=now_ns();
    if(!generation || !original_start || original_start>first_now || original_end<=first_now ||
       original_end-original_start>UINT64_C(3600000000000))RETURN_SELECTED(125);
    if(observer_kind){
        /* ACTUAL native callsite producer -> same original preinit receiver.
         * These are already issuer-held original descriptions, not an import,
         * JSON-derived grant, pidfd_open or Root birth created by Source. */
        if(original_root_child<1 || original_root_pidfd<0 || !generation)RETURN_SELECTED(ENTRY_REFUSAL("preowned original direct Root prefix absent"));
        observed_root_child=original_root_child;observed_root_pidfd=original_root_pidfd;
        observed_root_generation=generation;
        siginfo_t actual={0};
        if(waitid(P_PIDFD,(id_t)original_root_pidfd,&actual,WEXITED|WNOWAIT|WNOHANG)<0 ||
           (actual.si_pid && actual.si_pid!=original_root_child))
            RETURN_SELECTED(ENTRY_REFUSAL("original preinit caller is not the held direct Root parent"));
        observed_root_relation_verified=1;
    }
    if(!selected_root_entry){
        int error=ENTRY_REFUSAL("original selected Root native callsite absent");
        RETURN_SELECTED(friday_existing_parent_error_end(error));
    }
    int result=friday_existing_parent_preinitialize();
    if(result)RETURN_SELECTED(friday_existing_parent_error_end(result));
    struct sigaction cutoff={0};cutoff.sa_handler=original_interpreter_work_cutoff;sigemptyset(&cutoff.sa_mask);
    if(sigaction(SIGUSR2,&cutoff,NULL)<0)RETURN_SELECTED(friday_existing_parent_error_end(125));
    sigset_t interpreter_work_signal;sigemptyset(&interpreter_work_signal);sigaddset(&interpreter_work_signal,SIGUSR2);
    if(pthread_sigmask(SIG_BLOCK,&interpreter_work_signal,NULL)!=0)RETURN_SELECTED(friday_existing_parent_error_end(125));
    /* Reserve actual task stack memory before pthread_create. This uses the
     * original Root arena/live/work ceilings. It adds no FD/process/role or
     * physical-free refund. libc/NPTL/TLS/CPU/stack costs require the same full
     * selected native audit and whole RAM accounting, not a new allowance. */
    long page=sysconf(_SC_PAGESIZE);
    if(page<=0 || (page&(page-1)))RETURN_SELECTED(friday_existing_parent_error_end(125));
    size_t stack_bytes=UINT64_C(1048576);
    if((size_t)page>SIZE_MAX-stack_bytes)RETURN_SELECTED(friday_existing_parent_error_end(125));
    original_execution.stack_storage_bytes=stack_bytes+(size_t)page;
    original_execution.stack_storage=friday_malloc(original_execution.stack_storage_bytes);
    if(!original_execution.stack_storage)RETURN_SELECTED(friday_existing_parent_error_end(125));
    uintptr_t stack=((uintptr_t)original_execution.stack_storage+(uintptr_t)page-1)&~((uintptr_t)page-1);
    original_execution.selected_entry=selected_root_entry;original_execution.result=125;
    pthread_attr_t attributes;int prepared=pthread_attr_init(&attributes)==0;
    if(!prepared || pthread_attr_setstack(&attributes,(void *)stack,stack_bytes)!=0 ||
       pthread_attr_setguardsize(&attributes,0)!=0)RETURN_SELECTED(friday_existing_parent_error_end(125));
    original_execution_created=1;
    result=pthread_create(&original_execution.thread,&attributes,run_original_interpreter_execution,NULL);
    pthread_attr_destroy(&attributes);
    if(result){original_execution_created=0;RETURN_SELECTED(friday_existing_parent_error_end(result));}
    for(;;){
        uint64_t end=original_absolute_end();
        uint64_t reserve=terminal_wall_ms()*UINT64_C(1000000);
        if(end<=reserve || started_ns>=end-reserve)RETURN_SELECTED(125);
        uint64_t work_end=end-reserve;
        if(wait_original_execution_before(work_end,1)==0){
            if(atomic_load_explicit(&original_execution.finished,memory_order_acquire))
                RETURN_SELECTED(original_execution.result);
            RETURN_SELECTED(native_cold_original_caller_consume());
        }
        if(errno!=ETIMEDOUT)RETURN_SELECTED(125);
        int started=atomic_load_explicit(&original_execution.started,memory_order_acquire);
        if(started<=0)RETURN_SELECTED(125);
        /* Scoped same-process task signal, bound to the actual preowned
         * pthread generation/clear_tid registration. Never a numeric Source
         * or Root process kill, broad kill, new deadline or guessed task exit. */
        result=pthread_kill(original_execution.thread,SIGUSR2);
        if(result && result!=ESRCH)RETURN_SELECTED(125);
        if(wait_original_execution_before(original_absolute_end(),0)<0)RETURN_SELECTED(125);
        RETURN_SELECTED(native_cold_original_caller_consume());
    }
}

int friday_existing_parent_run(int (*selected_root_entry)(void),uint64_t original_start,uint64_t original_end,uint64_t generation) {
    return run_selected_existing_caller(selected_root_entry,0,original_start,original_end,generation,-1,-1);
}
int friday_existing_observer_run(int (*selected_observer_entry)(void),uint64_t original_start,uint64_t original_end,uint64_t generation,int child,int held_pidfd) {
    return run_selected_existing_caller(selected_observer_entry,1,original_start,original_end,generation,child,held_pidfd);
}
int friday_existing_final_caller_run(int (*selected_final_entry)(void),uint64_t original_start,uint64_t original_end,uint64_t generation,int child,int held_pidfd) {
    /* The final already-existing original C consumer accepts its actual result
     * directly; no recursively added observer/role/process is created. */
    return run_selected_existing_caller(selected_final_entry,2,original_start,original_end,generation,child,held_pidfd);
}

static int scanner_prepare_root_backing(const struct friday_scanner_selected_enrollment *selected,
                                       struct friday_scanner_parent_result *result) {
    struct stat writable,readonly,control;
    struct owner_caps original;
    if(!selected || !result || selected->abi!=FRIDAY_SCANNER_PARENT_ABI ||
       scanner_final_result!=result || scanner_selected_enrollment!=selected ||
       !scanner_original_guard_installed || parent_end_reached ||
       !selected->generation || !selected->original_materials_factory || !selected->original_start_ns ||
       selected->root_backing_fd<0 || selected->root_readonly_fd<0 ||
       selected->root_backing_fd==selected->root_readonly_fd ||
       selected->original_start_ns>now_ns() || now_ns()>=selected->original_end_ns ||
       selected->original_minimum_consumer_end_ns<=now_ns() ||
       selected->original_minimum_consumer_end_ns>selected->original_end_ns ||
       selected->original_end_ns<=selected->original_start_ns ||
       selected->original_end_ns-selected->original_start_ns>UINT64_C(3600000000000) ||
       initialized || Py_IsInitialized() || scanner_root_backing_preowned || geteuid()!=0){errno=EPERM;return -1;}
    /* Borrowed actual provider slots first, before mmap/read/constructors. A
     * failure is returned into this same preowned native result, not erased.
     * This is NOT independent admission and never constructs a permit.
     */
    scanner_root_backing_fds[0]=selected->root_backing_fd;
    scanner_root_backing_fds[1]=selected->root_readonly_fd;
    if(fstat(5,&control)<0 || pread(5,&original,sizeof(original),0)!=(ssize_t)sizeof(original))return -1;
    int cap_seals=__real_fcntl(5,F_GET_SEALS);
    if(original.magic!=OWNER_MAGIC || original.version!=1 || original.role!=2 ||
       original.canonical_workers!=4 || original.max_fds<207 || original.max_fds>FD_MAX ||
       !original.max_live_bytes || original.max_live_bytes>SIZE_MAX ||
       original.max_live_bytes<=SCANNER_ROOT_HEADER_BYTES ||
       !original.terminal_live_bytes || original.terminal_live_bytes>=original.max_live_bytes-SCANNER_ROOT_HEADER_BYTES ||
       !original.max_wall_ms || original.max_wall_ms>UINT64_C(3600000) ||
       selected->original_end_ns-selected->original_start_ns!=original.max_wall_ms*UINT64_C(1000000) ||
       memcmp(selected->root_binding_sha256,original.binding_sha256,32) ||
       !S_ISREG(control.st_mode) || control.st_uid!=0 || control.st_nlink!=0 ||
       control.st_size!=(off_t)sizeof(original) || cap_seals<0 ||
       (cap_seals&(F_SEAL_SEAL|F_SEAL_WRITE|F_SEAL_SHRINK|F_SEAL_GROW))!=
           (F_SEAL_SEAL|F_SEAL_WRITE|F_SEAL_SHRINK|F_SEAL_GROW) ||
       selected->root_backing_bytes!=original.max_live_bytes){errno=EINVAL;return -1;}
    int wf=__real_fcntl(selected->root_backing_fd,F_GETFL),rf=__real_fcntl(selected->root_readonly_fd,F_GETFL);
    int seals=__real_fcntl(selected->root_backing_fd,F_GET_SEALS);
    if(fstat(selected->root_backing_fd,&writable)<0 || fstat(selected->root_readonly_fd,&readonly)<0 ||
       wf<0 || rf<0 || (wf&O_ACCMODE)!=O_RDWR || (rf&O_ACCMODE)!=O_RDONLY ||
       !S_ISREG(writable.st_mode) || writable.st_uid!=0 || writable.st_nlink!=0 ||
       writable.st_size!=(off_t)selected->root_backing_bytes ||
       writable.st_dev!=readonly.st_dev || writable.st_ino!=readonly.st_ino ||
       writable.st_mode!=readonly.st_mode || writable.st_uid!=readonly.st_uid ||
       writable.st_gid!=readonly.st_gid || writable.st_nlink!=readonly.st_nlink || writable.st_size!=readonly.st_size ||
       writable.st_mtim.tv_sec!=readonly.st_mtim.tv_sec || writable.st_mtim.tv_nsec!=readonly.st_mtim.tv_nsec ||
       writable.st_ctim.tv_sec!=readonly.st_ctim.tv_sec || writable.st_ctim.tv_nsec!=readonly.st_ctim.tv_nsec ||
       seals<0 || (seals&(F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL))!=
           (F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL) || (seals&(F_SEAL_WRITE|F_SEAL_FUTURE_WRITE))){errno=EPERM;return -1;}
    if(original.max_read_bytes<sizeof(original)+SCANNER_ROOT_HEADER_BYTES ||
       original.max_work_bytes<3*SCANNER_ROOT_HEADER_BYTES+sizeof(original) ||
       original.terminal_work_bytes>=original.max_work_bytes ||
       3*SCANNER_ROOT_HEADER_BYTES+sizeof(original)>original.max_work_bytes-original.terminal_work_bytes){errno=ENOMEM;return -1;}
    if(parent_end_reached || now_ns()>=selected->original_minimum_consumer_end_ns){errno=ETIMEDOUT;return -1;}
    void *write_view=mmap(NULL,selected->root_backing_bytes,PROT_READ|PROT_WRITE,MAP_SHARED,selected->root_backing_fd,0);
    if(write_view==MAP_FAILED)return -1;
    /* Store the actual whole writable view before its second receiver mmap can
     * fail. Both descriptions remain borrowed/charged; never close-on-failure.
     */
    scanner_root_backing=write_view;
    result->complete_root_backing=write_view;result->complete_root_backing_bytes=selected->root_backing_bytes;
    if(parent_end_reached || now_ns()>=selected->original_minimum_consumer_end_ns){errno=ETIMEDOUT;return -1;}
    void *read_view=mmap(NULL,selected->root_backing_bytes,PROT_READ,MAP_SHARED,selected->root_readonly_fd,0);
    if(read_view==MAP_FAILED)return -1;
    scanner_root_readonly_view=read_view;
    result->complete_root_backing=read_view;
    if(parent_end_reached || now_ns()>=selected->original_minimum_consumer_end_ns){errno=ETIMEDOUT;return -1;}
    /* BOTH full actual bodies are physically preowned before Root init/birth.
     * Source has a separate data file. Neither Root private description can be
     * confused with it. This original provider backing is not re-created later
     * by a Python Root holder which could disappear at its native prefix.
     */
    struct stat sw,sr;int sf=__real_fcntl(selected->source_backing_fd,F_GETFL);
    int srf=__real_fcntl(selected->source_readonly_fd,F_GETFL);
    int ss=__real_fcntl(selected->source_backing_fd,F_GET_SEALS);
    if(selected->source_backing_fd<0 || selected->source_readonly_fd<0 ||
       selected->source_backing_fd==selected->source_readonly_fd ||
       selected->source_backing_fd==selected->root_backing_fd || selected->source_backing_fd==selected->root_readonly_fd ||
       selected->source_readonly_fd==selected->root_backing_fd || selected->source_readonly_fd==selected->root_readonly_fd ||
       !selected->source_backing_bytes || selected->source_backing_bytes>SIZE_MAX ||
       selected->source_backing_bytes>PY_SSIZE_T_MAX ||
       selected->source_backing_bytes<=PLANE_HEADER_BYTES || sf<0 || srf<0 ||
       (sf&O_ACCMODE)!=O_RDWR || (srf&O_ACCMODE)!=O_RDONLY ||
       fstat(selected->source_backing_fd,&sw)<0 || fstat(selected->source_readonly_fd,&sr)<0 ||
       !S_ISREG(sw.st_mode) || sw.st_uid!=0 || sw.st_nlink!=0 ||
       sw.st_size!=(off_t)selected->source_backing_bytes ||
       sw.st_dev!=sr.st_dev || sw.st_ino!=sr.st_ino || sw.st_size!=sr.st_size ||
       sw.st_mode!=sr.st_mode || sw.st_uid!=sr.st_uid || sw.st_gid!=sr.st_gid || sw.st_nlink!=sr.st_nlink ||
       (sw.st_dev==writable.st_dev && sw.st_ino==writable.st_ino) ||
       ss<0 || (ss&(F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL))!=(F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL) ||
       (ss&(F_SEAL_WRITE|F_SEAL_FUTURE_WRITE))){errno=EPERM;return -1;}
    scanner_preowned_source_fds[0]=selected->source_backing_fd;
    scanner_preowned_source_fds[1]=selected->source_readonly_fd;
    if(parent_end_reached || now_ns()>=selected->original_minimum_consumer_end_ns){errno=ETIMEDOUT;return -1;}
    void *source_view=mmap(NULL,selected->source_backing_bytes,PROT_READ,MAP_SHARED,selected->source_readonly_fd,0);
    if(source_view==MAP_FAILED)return -1;
    scanner_source_readonly_view=source_view;scanner_preowned_source_extent=selected->source_backing_bytes;
    memcpy(scanner_preowned_source_binding,selected->source_binding_sha256,32);
    result->complete_source_backing=source_view;
    result->complete_source_backing_bytes=selected->source_backing_bytes;
    if(parent_end_reached || now_ns()>=selected->original_minimum_consumer_end_ns){errno=ETIMEDOUT;return -1;}
    /* Header-zero is a one-generation guard, not proof of full fresh storage.
     * Fresh creation/current fence + selected stock/static image correspondence
     * remain original provider enrollment facts. No extra whole archive read.
     */
    for(size_t i=0;i<SCANNER_ROOT_HEADER_BYTES;i++){
        if((i&4095u)==0 && (parent_end_reached || now_ns()>=selected->original_minimum_consumer_end_ns)){
            errno=ETIMEDOUT;return -1;
        }
        if(((unsigned char *)write_view)[i]){errno=EEXIST;return -1;}
    }
    if(parent_end_reached || now_ns()>=selected->original_minimum_consumer_end_ns){errno=ETIMEDOUT;return -1;}
    memset(write_view,0,SCANNER_ROOT_HEADER_BYTES);
    scanner_root_backing->abi=FRIDAY_SCANNER_PARENT_ABI;
    scanner_root_backing->generation=selected->generation;
    scanner_root_backing->start_ns=selected->original_start_ns;
    scanner_root_backing->end_ns=selected->original_end_ns;
    scanner_root_backing->minimum_end_ns=selected->original_minimum_consumer_end_ns;
    scanner_root_backing->root_address=(uint64_t)(uintptr_t)write_view;
    scanner_root_backing->extent=selected->root_backing_bytes;
    scanner_root_backing->original_caps=original;
    /* Same immutable caps read once. Header reads/writes and fixed copies are
     * not free. Native/implicit allocator/VM work remains a qualification cost,
     * not manufactured from this explicit byte counter.
     */
    caps=original;source_caps_prechecked=1;
    scanner_preinit_read_bytes=SCANNER_ROOT_HEADER_BYTES;
    scanner_preinit_work_bytes=3*SCANNER_ROOT_HEADER_BYTES+sizeof(original);
    scanner_root_backing->root_ordinary.work=scanner_preinit_work_bytes;
    ordinary_owner=&scanner_root_backing->root_ordinary;
    terminal_owner=&scanner_root_backing->root_terminal;
    fd_slots=scanner_root_backing->root_fds;canonical_slots=scanner_root_backing->root_slots;
    scanner_child_owner=&scanner_root_backing->root_children;
    *scanner_child_owner=scanner_child_storage;
    original_execution_owner=&scanner_root_backing->root_execution;
    parent_custody_owner=&scanner_root_backing->root_parent;
    parent_result_owner=&scanner_root_backing->root_result;
    source_raw_owner=&scanner_root_backing->root_raw;
    parent_type_owner=&scanner_root_backing->root_error_type;
    parent_value_owner=&scanner_root_backing->root_error_value;
    parent_traceback_owner=&scanner_root_backing->root_error_traceback;
    caller_errors=scanner_root_backing->root_errors;
    caller_count_owner=&scanner_root_backing->root_error_count;
    caller_lane_owner=&scanner_root_backing->root_error_lane_exhausted;
    received_first_owner=&scanner_root_backing->root_packet_first;
    received_last_owner=&scanner_root_backing->root_packet_last;
    received_count_owner=&scanner_root_backing->root_packet_count;
    carrier_fd_alias=scanner_root_backing->carrier_fds;
    carrier_canonical_alias=scanner_root_backing->carrier_slots;
    carrier_parent_owner=&scanner_root_backing->carrier_parent;
    carrier_raw_owner=&scanner_root_backing->carrier_raw;
    carrier_result_owner=&scanner_root_backing->carrier_result;
    carrier_error_alias=scanner_root_backing->carrier_errors;
    carrier_error_count_owner=&scanner_root_backing->carrier_error_count;
    scanner_root_backing_preowned=1;
    result->root_read_bytes=sizeof(original)+SCANNER_ROOT_HEADER_BYTES;
    return 0;
}

static int scanner_selected_root_callsite(void) {
    /* REAL performing callsite: invoked by run_original_interpreter_execution,
     * not an unused exported callback or a Source-created materials provider.
     * Every factory/import/call/default error is fetched BEFORE projection.
     */
    struct scanner_root_backing *body=scanner_root_backing;
    if(!body || !scanner_selected_enrollment || !deadline())return 125;
    body->root_materials=scanner_selected_enrollment->original_materials_factory(
        scanner_selected_enrollment->original_provider_context);
    if(!body->root_materials){own_current_caller_error("original_provider.actual_materials_factory");return 125;}
    if(!PyTuple_CheckExact(body->root_materials) || PyTuple_GET_SIZE(body->root_materials)!=5){
        PyErr_SetString(PyExc_TypeError,"original issuer must supply exact original five-argument tuple");
        own_current_caller_error("selected_callsite.materials_shape");return 125;
    }
    PyObject *permit=PyTuple_GET_ITEM(body->root_materials,0);
    if(permit==Py_None || permit!=PyTuple_GET_ITEM(body->root_materials,1)){
        PyErr_SetString(PyExc_PermissionError,"actual original permit identity absent");
        own_current_caller_error("selected_callsite.permit_identity");return 125;
    }
    body->root_module=PyImport_ImportModule("tools.root_holder");
    if(!body->root_module){own_current_caller_error("selected_callsite.import_original_root_holder");return 125;}
    body->root_function=PyObject_GetAttrString(body->root_module,"launch_selected_source");
    if(!body->root_function){own_current_caller_error("selected_callsite.original_source_entry");return 125;}
    body->root_call_result=PyObject_CallObject(body->root_function,body->root_materials);
    if(!body->root_call_result){own_current_caller_error("selected_callsite.invoke_original_source_entry");return 125;}
    if(!parent_custody || !parent_result_accepted || parent_result!=body->root_call_result){
        PyErr_SetString(PyExc_RuntimeError,"actual same Root result was not installed in original native receiver");
        own_current_caller_error("selected_callsite.actual_result_join");return 125;
    }
    return 0;
}

static int scanner_final_root_receiver(int status) {
    if(scanner_final_receiver_entered)return 125;
    scanner_final_receiver_entered=1;
    struct friday_scanner_parent_result *result=scanner_final_result;
    struct scanner_root_backing *body=scanner_root_backing;
    if(!result || !body || !scanner_root_readonly_view)return 125;
    result->status=status;result->state=FRIDAY_SCANNER_PARENT_UNCONFIRMED;
    result->source_reaped=(!source_birth_inflight && !source_birth_committed &&
        pending_child<1 && pending_pidfd<0)?1:pending_reaped;
    result->root_reaped=observed_root_child<1?1:observed_root_reaped;
    int task_started=atomic_load_explicit(&original_execution.started,memory_order_acquire);
    int task_clear=atomic_load_explicit(&original_execution.clear_tid,memory_order_acquire);
    /* A203 NEW. No birth is not kernel exit. pthread_create failure stores the
     * created flag back to zero. started below zero never finished registering
     * the kernel word. Only a registered task whose kernel cleared that word
     * is an exit. A local return is not transfer. */
    int never_created=!original_execution_created;
    int startup_unregistered=original_execution_created && task_started<0;
    int kernel_cleared=original_execution_created && task_started>0 && task_clear==0;
    result->task_exited=kernel_cleared?1:0;
    result->root_read_bytes=actual_read;
    result->source_read_bytes=source_backing_inspection_bytes;
    result->root_output_bytes=actual_output;
    result->original_guard_armed=scanner_original_guard_installed;
    result->original_end_reached=parent_end_reached;
    result->independently_accepted=0;
    result->outside_final_end_confirmed=0;
    if(never_created || startup_unregistered){
        /* Keep the preowned backing, the ordinary errno and the error aliases.
         * Do not wait a task that was not registered, and do not read the
         * mapped header. */
        result->state=FRIDAY_SCANNER_PARENT_UNCONFIRMED;
        result->task_exited=0;
        if(!result->primary_errno){
            if(startup_unregistered && original_execution.startup_errno)
                result->primary_errno=original_execution.startup_errno;
            else if(errno)result->primary_errno=errno;
        }
        return status?status:125;
    }
    /* A return/EOF/pidfd/cancellation request never establishes task exit,
     * independent custody or original outside end. Do not read a racing body
     * or destroy storage when an interrupted task has not actually stopped.
     */
    if(!result->task_exited){result->receiver_errno=EBUSY;return 125;}
    if(parent_end_reached || now_ns()>=original_absolute_end()){
        result->receiver_errno=ETIMEDOUT;result->late_error=1;return 125;
    }
    /* Reserve BEFORE the first body/header/table read. The full original
     * physical map is already held; it is not itself a measured read pass.
     */
    size_t inspection=SCANNER_ROOT_HEADER_BYTES;
    if(actual_read>caps.max_read_bytes || inspection>caps.max_read_bytes-actual_read){
        result->receiver_errno=ENOMEM;result->late_error=1;return 125;
    }
    actual_read+=inspection;result->root_read_bytes=actual_read;
    const struct scanner_root_backing *received=(const void *)scanner_root_readonly_view;
    if(received->abi!=FRIDAY_SCANNER_PARENT_ABI ||
       received->generation!=scanner_selected_enrollment->generation ||
       received->extent!=scanner_selected_enrollment->root_backing_bytes ||
       received->root_address!=(uint64_t)(uintptr_t)body ||
       received->start_ns!=original_preowned_started_ns ||
       received->end_ns!=original_preowned_end_ns ||
       received->minimum_end_ns!=scanner_selected_enrollment->original_minimum_consumer_end_ns){
        result->receiver_errno=EINVAL;result->late_error=1;return 125;
    }
    /* This consumes the actual same backing; it never casts Root/Source graph
     * addresses into a foreign interpreter or treats counts as full bodies.
     * Full arena bytes remain held in the preowned file. Native/static aliases
     * require original selected-image qualification; not a universal heap walk.
     */
    int uncertain=!result->source_reaped || !result->root_reaped || failed ||
        parent_end_reached || caller_error_lane_exhausted || !parent_native_retired ||
        !source_registry_retired || source_registry_still_owned ||
        now_ns()>=original_absolute_end();
    for(int i=0;i<FD_MAX;i++)if(received->root_fds[i].uncertain ||
       (received->root_fds[i].active && received->root_fds[i].fd>=0 &&
        received->root_fds[i].fd!=2 && received->root_fds[i].fd!=scanner_root_backing_fds[0] &&
        received->root_fds[i].fd!=scanner_root_backing_fds[1] &&
        received->root_fds[i].fd!=scanner_preowned_source_fds[0] &&
        received->root_fds[i].fd!=scanner_preowned_source_fds[1]))uncertain=1;
    for(int i=0;i<4;i++)if(received->root_slots[i].acquired || received->root_slots[i].unlock_uncertain)uncertain=1;
    if(received->root_materials || received->root_module || received->root_function ||
       received->root_call_result || received->root_parent || received->root_result ||
       received->root_raw || received->root_error_type || received->root_error_value ||
       received->root_error_traceback || received->carrier_parent || received->carrier_raw ||
       received->carrier_result)uncertain=1;
    for(unsigned i=0;i<16;i++)if(received->root_errors[i].type || received->root_errors[i].value ||
        received->root_errors[i].traceback || received->carrier_errors[i])uncertain=1;
    result->late_error=uncertain;
    if(original_execution.startup_errno && !result->primary_errno)
        result->primary_errno=original_execution.startup_errno;
    if(!uncertain && status==0)result->state=FRIDAY_SCANNER_PARENT_REGISTERED_RETIRED;
    /* This receiver is called AFTER native end/metrics and never clears the
     * original provider's result/storage slots. Full external provider end,
     * separately accepted Root+Source parenthood and its costs remain OPEN.
     */
    result->independently_accepted=0;result->outside_final_end_confirmed=0;
    result->status=uncertain?125:status;
    body->parent_terminal_result=*result;
    return uncertain?125:status;
}

int friday_scanner_selected_parent_entry(const struct friday_scanner_selected_enrollment *selected,
                                        struct friday_scanner_parent_result *result) {
    /* New authored original-role entry, not reconstructed private host Source.
     * Actual external issuer must select/invoke it and supply its enrolled
     * factory and held backing before runtime qualification. No new process.
     */
    if(!selected || !result || selected->abi!=FRIDAY_SCANNER_PARENT_ABI ||
       initialized || Py_IsInitialized() || scanner_final_result || geteuid()!=0){errno=EPERM;return 125;}
    /* The existing issuer's actual result and enrollment lifetime is held
     * BEFORE the first guard/cap/mapping operation. No new grant is minted. */
    scanner_final_result=result;scanner_selected_enrollment=selected;
    memset(result,0,sizeof(*result));result->abi=FRIDAY_SCANNER_PARENT_ABI;
    result->generation=selected->generation;result->original_start_ns=selected->original_start_ns;
    result->original_end_ns=selected->original_minimum_consumer_end_ns;
    result->state=FRIDAY_SCANNER_PARENT_PREOWNED;result->status=125;
    if(install_original_caller_guard(selected->original_start_ns,selected->original_end_ns,
                                    selected->generation,selected->original_minimum_consumer_end_ns)<0 ||
       scanner_prepare_root_backing(selected,result)<0){
        result->primary_errno=errno;result->state=FRIDAY_SCANNER_PARENT_UNCONFIRMED;
        result->original_guard_armed=scanner_original_guard_installed;
        result->original_end_reached=parent_end_reached;
        result->late_error=parent_end_reached;
        /* Keep every borrowed FD and successful physical view preowned.
         * A failed prepare has no initialized header/task to falsely consume. */
        return 125;
    }
    return run_selected_existing_caller(scanner_selected_root_callsite,0,
        selected->original_start_ns,selected->original_end_ns,selected->generation,-1,-1);
}

/* Bounded child entry. Source keeps its original actual hard expiry; a role2
 * worker keeps the old independently reviewed worker-end protocol, but it can
 * no longer birth/pretend to own Source through this dying main. */
static int retire_source_registered_fds_once(void) {
    int result=0;
    if(!initialized)return 0;
    for(int i=0;i<FD_MAX;i++){
        int fd=fd_slots[i].fd;
        if(!fd_slots[i].active || fd<0 || fd==2 || fd==source_plane_fd)continue;
        if(fd_slots[i].uncertain){result=-1;continue;}
        if((fd==pending_pidfd && pending_child>=0 && !pending_reaped) ||
           (fd==observed_root_pidfd && observed_root_child>=0 && !observed_root_reaped))continue;
        if(retire_fd(fd)<0){failed=1;result=-1;}
    }
    return result;
}
static int source_native_raw_endpoint(int error_end) {
    /* Source's own called consumer. It is not native_caller_destruct.
     * The still-open plane fd is the preheld backing, not uncertainty.
     * Confirmed retirement is published only after raw clears and a zero
     * Py_FinalizeEx. A later metric or close failure revokes that note. */
    uint32_t kind;
    if(source_endpoint_entered)return 125;
    source_endpoint_entered=1;in_terminal=1;
    int uncertain=error_end?1:0;
    if(initialized && consume_native_packets()<0){failed=1;uncertain=1;}
    if(initialized && retire_source_registered_fds_once()<0){failed=1;uncertain=1;}
    if(caller_error_lane_exhausted || parent_end_reached || (initialized && !deadline()))uncertain=1;
    if(pending_child>=0 && !pending_reaped)uncertain=1;
    if(observed_root_child>=0 && !observed_root_reaped)uncertain=1;
    if(caps.role==1 && source_plane_fd<0)uncertain=1;
    if(plane_placement_not_fit || plane_omitted_bytes)uncertain=1;
    for(int i=0;i<FD_MAX;i++){
        if(fd_slots[i].uncertain)uncertain=1;
        if(fd_slots[i].active && fd_slots[i].fd>=0 && fd_slots[i].fd!=2 && fd_slots[i].fd!=source_plane_fd)uncertain=1;
    }
    for(int i=0;i<4;i++)if(canonical_slots[i].acquired || canonical_slots[i].unlock_uncertain)uncertain=1;
    if(uncertain){
        source_registry_still_owned=1;failed=1;
        kind=(plane_placement_not_fit || plane_omitted_bytes)?PLANE_NOT_FIT:PLANE_SIGNAL_NOTE;
        if(publish_source_plane(kind)<0)failed=1;
        if(initialized && emit_final_native_metrics(-1)<0)failed=1;
        if(source_plane_fd>=0 && retire_fd(source_plane_fd)<0)failed=1;
        return 125;
    }
    if(!Py_IsInitialized()){
        if(source_raw_root || caller_error_count || parent_error_type || parent_error_value || parent_error_traceback){
            source_registry_still_owned=1;failed=1;
            kind=(plane_placement_not_fit || plane_omitted_bytes)?PLANE_NOT_FIT:PLANE_SIGNAL_NOTE;
            if(publish_source_plane(kind)<0)failed=1;
            return 125;
        }
        source_registry_retired=1;source_registry_still_owned=0;
        if(publish_source_plane(PLANE_CONFIRMED_RETIRED)<0){failed=1;return 125;}
        if(source_plane_fd>=0 && retire_fd(source_plane_fd)<0){
            failed=1;
            if(publish_source_plane(PLANE_REVOKED)<0)failed=1;
            return 125;
        }
        return 0;
    }
    Py_CLEAR(source_raw_root);
    Py_CLEAR(parent_error_type);Py_CLEAR(parent_error_value);Py_CLEAR(parent_error_traceback);
    for(unsigned i=0;i<caller_error_count;i++){
        Py_CLEAR(caller_errors[i].type);Py_CLEAR(caller_errors[i].value);Py_CLEAR(caller_errors[i].traceback);
    }
    for(struct received_packet *packet=received_first;packet;packet=packet->next){
        Py_CLEAR(packet->error_type);Py_CLEAR(packet->error_value);Py_CLEAR(packet->error_traceback);
    }
    source_registry_retired=1;source_registry_still_owned=0;
    int finalized=Py_FinalizeEx();
    if(finalized!=0){
        source_registry_still_owned=1;source_registry_retired=0;failed=1;
        if(publish_source_plane(PLANE_REVOKED)<0)failed=1;
        return 125;
    }
    if(publish_source_plane(PLANE_CONFIRMED_RETIRED)<0){failed=1;return 125;}
    if(emit_final_native_metrics(finalized)!=0){
        failed=1;
        if(publish_source_plane(PLANE_REVOKED)<0)failed=1;
        return 125;
    }
    if(source_plane_fd>=0 && retire_fd(source_plane_fd)<0){
        failed=1;
        if(publish_source_plane(PLANE_REVOKED)<0)failed=1;
        return 125;
    }
    return 0;
}

int main(int argc,char **argv) {
    (void)argc;(void)argv;
    /* The selected privileged EXEC trampoline has already erased ALL old Root
     * mappings, closed all non-Source descriptions and installed a birth timer.
     * Consume only the same Root-created release pipe and sealed Source caps.
     * No Python, factory, allocation or Source body runs with elevated UID.
     */
    if(geteuid()==0){
        struct stat before;unsigned char release[64];size_t used=0;
        if(fstat(5,&before)<0 || before.st_uid!=0 || before.st_nlink!=0 ||
           !S_ISREG(before.st_mode) || before.st_size!=(off_t)sizeof(caps) ||
           syscall(SYS_pread64,5,&caps,sizeof(caps),0)!=(ssize_t)sizeof(caps))
            _exit(ENTRY_REFUSAL("privileged Source exec sealed-cap prefix refused"));
        int seals=__real_fcntl(5,F_GET_SEALS);
        if(caps.magic!=OWNER_MAGIC || caps.version!=1 || caps.role!=1 ||
           seals<0 || (seals&(F_SEAL_WRITE|F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL))!=
               (F_SEAL_WRITE|F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL))
            _exit(ENTRY_REFUSAL("fixed Source-only privileged exec identity refused"));
        while(used<sizeof(release)){
            ssize_t n=syscall(SYS_read,8,release+used,sizeof(release)-used);
            if(n<=0)_exit(ENTRY_REFUSAL("original Source release record partial/absent"));
            used+=(size_t)n;
        }
        uint64_t generation,birth;uint32_t uid,gid;
        memcpy(&generation,release+8,8);memcpy(&birth,release+16,8);
        memcpy(&uid,release+24,4);memcpy(&gid,release+28,4);
        uint64_t now=now_ns();
        if(memcmp(release,"FRDROP71",8) || !generation || !uid || !gid ||
           !birth || birth>now || !caps.max_wall_ms || caps.max_wall_ms>UINT64_C(3600000) ||
           now-birth>=caps.max_wall_ms*UINT64_C(1000000) ||
           memcmp(release+32,caps.binding_sha256,32))
            _exit(ENTRY_REFUSAL("actual original Source release identity/birth interval differs"));
        if(__real_close(8)<0)_exit(ENTRY_REFUSAL("original release close unconfirmed"));
        if(setgroups(0,NULL)<0 || setgid((gid_t)gid)<0 || setuid((uid_t)uid)<0 ||
           geteuid()==0 || getuid()!=(uid_t)uid || getgid()!=(gid_t)gid)
            _exit(ENTRY_REFUSAL("fixed post-exec Source privilege isolation failed"));
        source_birth_started_ns=birth;scanner_source_exec_trampoline=1;
        source_caps_prechecked=1;source_trampoline_read_bytes=sizeof(release);
    }
    int initialization=initialize_native_owner(0);
    if(initialization)_exit(source_native_raw_endpoint(1));
    initialization=initialize_interpreter();
    if(initialization)_exit(source_native_raw_endpoint(1));
    /* fd4 is the exact selected held Source entry text, not caller input. Its
     * selected runtime/import/root contract is consumed by worker_entry.py. */
    FILE *entry=fdopen(4,"r");
    if(!entry){ENTRY_REFUSAL("native entry contract or system operation refused");_exit(source_native_raw_endpoint(1));}
    /* Low-level trusted entry keeps the loader/factory/return exception.
     * source_native_raw_endpoint retires source_raw_root and caller_errors.
     * Py_FinalizeEx, exit 125, and the Root destructor are not that retirement. */
    PyObject *entry_main=PyImport_AddModule("__main__");
    PyObject *entry_globals=entry_main?PyModule_GetDict(entry_main):NULL;
    PyObject *entry_done=entry_globals?PyRun_FileExFlags(entry,
        "<independently-selected-source-entry>",Py_file_input,entry_globals,entry_globals,0,NULL):NULL;
    int result=entry_done?0:125;
    if(!entry_done){own_current_caller_error("Source.trusted_entry.loader_factory_or_return");failed=1;}
    Py_XDECREF(entry_done);
    _exit(source_native_raw_endpoint(result!=0 || failed));
}
