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
static struct arena ordinary,terminal;
static struct fd_slot fd_slots[FD_MAX];
static uint64_t fd_generation=0;
static uint64_t actual_read,actual_output,started_ns;
static int initialized,failed,in_terminal;
static long owner_pid;
static int pending_child=-1,pending_pidfd=-1;
static int source_birth_committed=0;
static int source_birth_inflight=0,source_birth_slot=-1;
/* Exact native wait result survives a failed Python return allocation. It is
 * written only after the kernel actually consumes this owner's direct child.
 * No pidfd-open receipt or WNOHANG invocation sets a successful reap flag. */
static int pending_reaped=0,pending_wait_status=0;
static struct rusage pending_usage;
static int observed_root_child=-1,observed_root_pidfd=-1,observed_root_reaped=0,observed_root_status=0;
static int observed_root_relation_verified=0;
static struct rusage observed_root_usage;
/* Native scalar retirement origins belong to this original owner. No new FD,
 * process, retry authority or capacity domain is introduced. */
static int source_cancel_attempted=0,source_cancel_errno=0;
static int source_retirement_errno=0,root_retirement_errno=0;
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
static uint64_t original_preowned_started_ns=0,original_preowned_end_ns=0,original_preowned_generation=0;
static uint64_t observed_root_generation=0;
static PyObject *parent_custody=NULL;
static PyObject *parent_error_type=NULL,*parent_error_value=NULL,*parent_error_traceback=NULL;
static PyObject *parent_result=NULL;
/* Same ORIGINAL role/arena registry. No process, grant or private heap.
 * It owns partial Source raw factory/meter/result graphs before projection. */
static PyObject *source_raw_root=NULL;
static int parent_result_accepted=0,parent_native_consumer_entered=0;
static int parent_native_retired=0;
struct caller_error {PyObject *type,*value,*traceback;const char *operation;int error_number;};
static struct caller_error caller_errors[16];
static unsigned caller_error_count=0;
static int caller_error_lane_exhausted=0;
/* Fixed same-original-caller state, present before interpreter construction.
 * No separately allocated observer, process, service or capacity is introduced.
 * The native consumer below owns these complete graphs through real finalize.
 */
struct canonical_slot {
    int fd,prepared,acquired,unlock_attempted,unlock_uncertain,error_number;
    uint64_t generation;
    struct stat before_effect;
};
static struct canonical_slot canonical_slots[4];
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
static struct received_packet *received_first=NULL,*received_last=NULL;
static uint64_t received_count=0;
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
    int thread_tid,result;
    int (*selected_entry)(void);
    PyThreadState *raw_thread_state;
};
static struct original_interpreter_execution original_execution;
static int native_cold_consumer_entered=0;

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
        ordinary.live+terminal.live,ordinary.peak+terminal.peak,0,0,(uint64_t)failed,
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
        /* A zero/ECHILD/error result remains UNCONFIRMED. This signal handler
         * still cannot export the original202/Python/raw custody graph; the
         * whole hard-expiry survival edge remains an explicit Source residual.
         * No blocking wait or expired-role parking is introduced. */
    }
    /* No fallible diagnostic/publication write after this actual hard end.
     * Missing origin/native216 is a failed prefix at the genuine receiver,
     * never permission to perform an expired prepaid encoder/write. */
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
     * prefix. Do not forge a completed finalization record at an expired end. */
    _exit(125);
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
    SET_U64("physical_live_bytes",ordinary.live+terminal.live);
    SET_U64("physical_peak_upper_bytes",ordinary.peak+terminal.peak);
    SET_U64("allocation_work_bytes",ordinary.work+terminal.work);
    SET_U64("actual_read_bytes",actual_read);SET_U64("actual_output_bytes",actual_output);
    SET_U64("started_ns",started_ns);
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
    if(caps.role!=2 || geteuid()!=0 || !initialized || !existing_parent_entry || existing_observer_entry || !parent_custody || !parent_generation || !deadline()) {
        PyErr_SetString(PyExc_PermissionError,"preowned actual existing outside Source parent required; bounded role2 birth refused");return NULL;
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
        __real_close(0); /* no inherited Root stdin or interactive input */
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
        if(__real_fcntl(6,F_SETFD,FD_CLOEXEC)<0) CHILD_REFUSAL(2,"child selected executable close-on-exec failed");
        if(syscall(SYS_close_range,9,UINT_MAX,0)<0)CHILD_REFUSAL(2,"child original Root descriptor isolation failed");
        struct rlimit limits={16,16};if(setrlimit(RLIMIT_NOFILE,&limits)<0) CHILD_REFUSAL(2,"child original Source16 isolation failed");
        char released;
        if(__real_read(8,&released,1)!=1 || released!='1') CHILD_REFUSAL(2,"child original issuer release gate failed");
        __real_close(8);
        if(setgroups(0,NULL)<0 || setgid((gid_t)gid)<0 || setuid((uid_t)uid)<0) CHILD_REFUSAL(2,"child selected Source privilege isolation failed");
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
       original_consumer_end_ns || end<=now_ns() || end<=started_ns){
        PyErr_SetString(PyExc_RuntimeError,"one original selected minimum consumer end required");return NULL;
    }
    uint64_t own_end=original_absolute_end();
    original_consumer_end_ns=end<own_end?end:own_end;
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
static PyMethodDef methods[]={
    {"source_raw_custody",source_raw_custody,METH_VARARGS,NULL},
    {"retain_source_raw_exception",retain_source_raw_exception,METH_O,NULL},
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
    {"close_fd",close_owned,METH_O,NULL},{NULL,NULL,0,NULL}};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,"_friday_source_owner",NULL,-1,methods};
PyMODINIT_FUNC PyInit__friday_source_owner(void) {return PyModule_Create(&module);}

/* Shared pre-initialization implementation. Calling this from a Python
 * extension after initialization is expressly refused. Neither entry creates
 * another Root process, role, service, model, grant, or capacity domain. */
static int initialize_native_owner(int parent_entry) {
    if(initialized || Py_IsInitialized())return ENTRY_REFUSAL("native owner must precede interpreter initialization");
    existing_parent_entry=parent_entry;
    owner_pid=getpid();
    if(pread(5,&caps,sizeof(caps),0)!=(ssize_t)sizeof(caps) || caps.magic!=OWNER_MAGIC || caps.version!=1 || (caps.role!=1 && caps.role!=2) ||
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
    started_ns=existing_parent_entry?original_preowned_started_ns:now_ns();
    if(existing_parent_entry && (!started_ns || !original_preowned_end_ns ||
            now_ns()>=original_preowned_end_ns))return ENTRY_REFUSAL("actual preowned original caller interval exhausted");
    if(existing_parent_entry && original_preowned_end_ns!=started_ns+caps.max_wall_ms*UINT64_C(1000000))
        return ENTRY_REFUSAL("preowned original interval differs from original admitted role ceiling");
    if(sizeof(caps)>caps.max_read_bytes)return ENTRY_REFUSAL("native entry contract or system operation refused");
    actual_read=sizeof(caps);
    int diagnostic_flags=__real_fcntl(2,F_GETFL);
    if(diagnostic_flags<0||__real_fcntl(2,F_SETFL,diagnostic_flags|O_NONBLOCK)<0)
        return ENTRY_REFUSAL("native deadline diagnostic channel must be nonblocking");
    if(existing_parent_entry && caps.role!=2)return ENTRY_REFUSAL("existing parent must have original distinct Root role");
    struct sigaction guard={0};guard.sa_handler=existing_parent_entry?existing_parent_deadline:hard_deadline;sigemptyset(&guard.sa_mask);
    if(sigaction(SIGALRM,&guard,NULL)<0)return ENTRY_REFUSAL("native entry contract or system operation refused");
    sigset_t original_end_signal;sigemptyset(&original_end_signal);sigaddset(&original_end_signal,SIGALRM);
    if(sigprocmask(SIG_UNBLOCK,&original_end_signal,NULL)<0)return ENTRY_REFUSAL("original hard-end signal cannot be blocked");
    uint64_t remaining=existing_parent_entry?original_absolute_end()-now_ns():caps.max_wall_ms*UINT64_C(1000000);
    if(existing_parent_entry && now_ns()>=original_absolute_end())return ENTRY_REFUSAL("original caller end exhausted before native initialization");
    struct itimerval expiry={0};expiry.it_value.tv_sec=(time_t)(remaining/UINT64_C(1000000000));
    expiry.it_value.tv_usec=(suseconds_t)((remaining%UINT64_C(1000000000))/1000);
    if(!expiry.it_value.tv_sec && !expiry.it_value.tv_usec)expiry.it_value.tv_usec=1;
    if(setitimer(ITIMER_REAL,&expiry,NULL)<0)return ENTRY_REFUSAL("native entry contract or system operation refused");
    ordinary.size=(size_t)(caps.max_live_bytes-caps.terminal_live_bytes);
    terminal.size=(size_t)caps.terminal_live_bytes;
    ordinary.base=mmap(NULL,ordinary.size,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    terminal.base=mmap(NULL,terminal.size,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(ordinary.base==MAP_FAILED || terminal.base==MAP_FAILED) return ENTRY_REFUSAL("native entry contract or system operation refused");
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
        if(!fd_slots[i].active || fd<0 || fd==2)continue;
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
        (fd_slots[i].active && fd_slots[i].fd>=0 && fd_slots[i].fd!=2))retirement_uncertain=1;
    for(int i=0;i<4;i++)if(canonical_slots[i].acquired || canonical_slots[i].unlock_uncertain)retirement_uncertain=1;
    int can_finalize=!retirement_uncertain;
    if(Py_IsInitialized() && can_finalize){
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
    int finalized=Py_IsInitialized() && can_finalize?Py_FinalizeEx():-1;
    /* Skip stderr until the trailer. Skip an unreaped direct pidfd: process
     * exit closes it once. Every other known active description is retired
     * once. Uncertain slots were already recorded and are not retried.
     */
    if(initialized)for(int i=0;i<FD_MAX;i++){
        int fd=fd_slots[i].fd;
        if(!fd_slots[i].active || fd<0 || fd==2)continue;
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
    sigset_t end_signal,work_signal;
    sigemptyset(&end_signal);sigaddset(&end_signal,SIGALRM);
    sigemptyset(&work_signal);sigaddset(&work_signal,SIGUSR2);
    if(pthread_sigmask(SIG_BLOCK,&end_signal,NULL)!=0 || pthread_sigmask(SIG_UNBLOCK,&work_signal,NULL)!=0){
        atomic_store_explicit(&original_execution.started,-1,memory_order_release);
        syscall(SYS_exit,125);__builtin_unreachable();
    }
    int tid=(int)syscall(SYS_gettid);
    original_execution.thread_tid=tid;
    atomic_store_explicit(&original_execution.clear_tid,tid,memory_order_relaxed);
    /* The kernel itself clears this preowned word and wakes the native caller
     * when this task actually exits. A request/flag is never treated as exit.
     * glibc thread/TLS storage remains charged until this Root process ends;
     * no pthread_join/deallocator can touch a canceled Python arena lock. */
    if(syscall(SYS_set_tid_address,&original_execution.clear_tid)<0){
        original_execution.result=125;
        atomic_store_explicit(&original_execution.finished,1,memory_order_release);
        atomic_store_explicit(&original_execution.started,-1,memory_order_release);
        syscall(SYS_exit,125);__builtin_unreachable();
    }
    atomic_store_explicit(&original_execution.started,1,memory_order_release);
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
        if(!fd_slots[i].active || fd<0 || fd==2)continue;
        if((fd==pending_pidfd && pending_child>=0 && !pending_reaped) ||
           (fd==observed_root_pidfd && observed_root_child>=0 && !observed_root_reaped))continue;
        if(retire_fd(fd)<0)failed=1;
    }
    /* Explicitly -1/failed: no Py_FinalizeEx was attempted on interrupted
     * interpreter state. It cannot earn the normal finalization/ACK/runtime
     * relation. The cold owner preserves every actual raw graph until exit. */
    if(emit_final_native_metrics(-1)<0)failed=1;
    parent_native_retired=1;
    return 125;
}

/* Actual C caller, already in the independently selected existing Root image.
 * selected_root_entry is the ORIGINAL REVIEWED PROVIDER/ISSUER callsite which
 * invokes tools.root_holder.launch_selected_source with its actual opaque
 * permit/held selections. It is NOT a Python callback, dynamic ELF/preload,
 * Source-issued issuer, JSON argument, new process or new resource allowance.
 * That exact full loaded callsite relation remains independently qualified.
 */
static int run_selected_existing_caller(int (*selected_root_entry)(void),int observer_kind,
                                       uint64_t original_start,uint64_t original_end,uint64_t generation,
                                       int original_root_child,int original_root_pidfd) {
    if(initialized || Py_IsInitialized() || parent_native_consumer_entered)
        return ENTRY_REFUSAL("independent native generation cannot reuse an active/retired session");
    existing_observer_entry=observer_kind;
    existing_parent_entry=1;owner_pid=getpid();
    /* This is an actual ORIGINAL selected native producer's immutable bound,
     * not a Source-created budget. Commit it before caps reads, parenthood
     * checks, allocator initialization, constructors, imports or callbacks. */
    original_preowned_started_ns=original_start;original_preowned_end_ns=original_end;
    original_preowned_generation=generation;started_ns=original_start;
    if(observer_kind){observed_root_child=original_root_child;observed_root_pidfd=original_root_pidfd;observed_root_generation=generation;}
    uint64_t first_now=now_ns();
    if(!generation || !original_start || original_start>first_now || original_end<=first_now ||
       original_end-original_start>UINT64_C(3600000000000))_exit(125);
    struct sigaction first_guard={0};first_guard.sa_handler=existing_parent_deadline;sigemptyset(&first_guard.sa_mask);
    if(sigaction(SIGALRM,&first_guard,NULL)<0)_exit(125);
    sigset_t first_end_signal;sigemptyset(&first_end_signal);sigaddset(&first_end_signal,SIGALRM);
    if(sigprocmask(SIG_UNBLOCK,&first_end_signal,NULL)<0)_exit(125);
    uint64_t first_remaining=original_end-now_ns();
    if(now_ns()>=original_end)_exit(125);
    struct itimerval first_expiry={0};first_expiry.it_value.tv_sec=(time_t)(first_remaining/UINT64_C(1000000000));
    first_expiry.it_value.tv_usec=(suseconds_t)((first_remaining%UINT64_C(1000000000))/1000);
    if(!first_expiry.it_value.tv_sec && !first_expiry.it_value.tv_usec)first_expiry.it_value.tv_usec=1;
    if(setitimer(ITIMER_REAL,&first_expiry,NULL)<0)_exit(125);
    if(observer_kind){
        /* ACTUAL native callsite producer -> same original preinit receiver.
         * These are already issuer-held original descriptions, not an import,
         * JSON-derived grant, pidfd_open or Root birth created by Source. */
        if(original_root_child<1 || original_root_pidfd<0 || !generation)return ENTRY_REFUSAL("preowned original direct Root prefix absent");
        observed_root_child=original_root_child;observed_root_pidfd=original_root_pidfd;
        observed_root_generation=generation;
        siginfo_t actual={0};
        if(waitid(P_PIDFD,(id_t)original_root_pidfd,&actual,WEXITED|WNOWAIT|WNOHANG)<0 ||
           (actual.si_pid && actual.si_pid!=original_root_child))
            return ENTRY_REFUSAL("original preinit caller is not the held direct Root parent");
        observed_root_relation_verified=1;
    }
    if(!selected_root_entry){
        int error=ENTRY_REFUSAL("original selected Root native callsite absent");
        _exit(friday_existing_parent_error_end(error));
    }
    int result=friday_existing_parent_preinitialize();
    if(result)_exit(friday_existing_parent_error_end(result));
    struct sigaction cutoff={0};cutoff.sa_handler=original_interpreter_work_cutoff;sigemptyset(&cutoff.sa_mask);
    if(sigaction(SIGUSR2,&cutoff,NULL)<0)_exit(friday_existing_parent_error_end(125));
    sigset_t interpreter_work_signal;sigemptyset(&interpreter_work_signal);sigaddset(&interpreter_work_signal,SIGUSR2);
    if(pthread_sigmask(SIG_BLOCK,&interpreter_work_signal,NULL)!=0)_exit(friday_existing_parent_error_end(125));
    /* Reserve actual task stack memory before pthread_create. This uses the
     * original Root arena/live/work ceilings. It adds no FD/process/role or
     * physical-free refund. libc/NPTL/TLS/CPU/stack costs require the same full
     * selected native audit and whole RAM accounting, not a new allowance. */
    long page=sysconf(_SC_PAGESIZE);
    if(page<=0 || (page&(page-1)))_exit(friday_existing_parent_error_end(125));
    size_t stack_bytes=UINT64_C(1048576);
    if((size_t)page>SIZE_MAX-stack_bytes)_exit(friday_existing_parent_error_end(125));
    original_execution.stack_storage_bytes=stack_bytes+(size_t)page;
    original_execution.stack_storage=friday_malloc(original_execution.stack_storage_bytes);
    if(!original_execution.stack_storage)_exit(friday_existing_parent_error_end(125));
    uintptr_t stack=((uintptr_t)original_execution.stack_storage+(uintptr_t)page-1)&~((uintptr_t)page-1);
    original_execution.selected_entry=selected_root_entry;original_execution.result=125;
    pthread_attr_t attributes;int prepared=pthread_attr_init(&attributes)==0;
    if(!prepared || pthread_attr_setstack(&attributes,(void *)stack,stack_bytes)!=0 ||
       pthread_attr_setguardsize(&attributes,0)!=0)_exit(friday_existing_parent_error_end(125));
    result=pthread_create(&original_execution.thread,&attributes,run_original_interpreter_execution,NULL);
    pthread_attr_destroy(&attributes);
    if(result)_exit(friday_existing_parent_error_end(result));
    for(;;){
        uint64_t end=original_absolute_end();
        uint64_t reserve=terminal_wall_ms()*UINT64_C(1000000);
        if(end<=reserve || started_ns>=end-reserve)_exit(125);
        uint64_t work_end=end-reserve;
        if(wait_original_execution_before(work_end,1)==0){
            if(atomic_load_explicit(&original_execution.finished,memory_order_acquire))
                _exit(original_execution.result);
            _exit(native_cold_original_caller_consume());
        }
        if(errno!=ETIMEDOUT)_exit(125);
        int started=atomic_load_explicit(&original_execution.started,memory_order_acquire);
        if(started<=0)_exit(125);
        /* Scoped same-process task signal, bound to the actual preowned
         * pthread generation/clear_tid registration. Never a numeric Source
         * or Root process kill, broad kill, new deadline or guessed task exit. */
        result=pthread_kill(original_execution.thread,SIGUSR2);
        if(result && result!=ESRCH)_exit(125);
        if(wait_original_execution_before(original_absolute_end(),0)<0)_exit(125);
        _exit(native_cold_original_caller_consume());
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

/* Bounded child entry. Source keeps its original actual hard expiry; a role2
 * worker keeps the old independently reviewed worker-end protocol, but it can
 * no longer birth/pretend to own Source through this dying main. */
int main(int argc,char **argv) {
    (void)argc;(void)argv;
    int initialization=initialize_native_owner(0);
    if(initialization)return initialization;
    initialization=initialize_interpreter();
    if(initialization)return initialization;
    /* fd4 is the exact selected held Source entry text, not caller input. Its
     * selected runtime/import/root contract is consumed by worker_entry.py. */
    FILE *entry=fdopen(4,"r");if(!entry) return ENTRY_REFUSAL("native entry contract or system operation refused");
    /* Low-level trusted entry consumer preserves the actual loader/factory
     * exception; the convenience SimpleFile API would print and clear it.
     * No Source-controlled entry, importer, role or compiler authority is added. */
    PyObject *entry_main=PyImport_AddModule("__main__");
    PyObject *entry_globals=entry_main?PyModule_GetDict(entry_main):NULL;
    PyObject *entry_done=entry_globals?PyRun_FileExFlags(entry,
        "<independently-selected-source-entry>",Py_file_input,entry_globals,entry_globals,0,NULL):NULL;
    int result=entry_done?0:125;
    if(!entry_done){own_current_caller_error("Source.trusted_entry.loader_factory_or_return");failed=1;}
    Py_XDECREF(entry_done);
    in_terminal=1;
    /* Source's own fixed native packet consumer is also actually called.
     * Rejection/allocation errors never free raw/control/right history early.
     * No extra performing role/FD/read pass is added by these in-place records.
     */
    if(consume_native_packets()<0)failed=1;
    for(struct received_packet *r=received_first;r;r=r->next){
        Py_CLEAR(r->error_type);Py_CLEAR(r->error_value);Py_CLEAR(r->error_traceback);
    }
    int finalized=Py_FinalizeEx();
    for(int i=0;i<FD_MAX;i++)if(fd_slots[i].active && fd_slots[i].fd>=0 && fd_slots[i].fd!=2)
        if(retire_fd(fd_slots[i].fd)<0)failed=1;
    int metrics=emit_final_native_metrics(finalized);
    /* The observed native trailer is followed only by native exit. No libc
     * epilogue/stdio/atexit producer follows a record called final-end. Kernel
     * EOF and exact direct wait still belong to the outside actual observer. */
    _exit(result==0 && finalized==0 && !failed && metrics==0?0:125);
}
