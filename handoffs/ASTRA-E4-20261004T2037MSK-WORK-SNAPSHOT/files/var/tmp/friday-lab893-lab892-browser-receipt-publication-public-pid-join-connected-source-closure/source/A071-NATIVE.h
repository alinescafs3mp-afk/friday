#ifndef FR_A061_NATIVE_H
#define FR_A061_NATIVE_H
#define _GNU_SOURCE 1
#include <stdint.h>
#include <stddef.h>
#define FR_VERSION 1u
#define FR_SOURCES 19u
#define FR_CAP_FD 100
#define FR_IMAGE_FD 111
#define FR_ROOT_FD 122
#define FR_REG_FD 123
#define FR_SEALS 15u
#define FR_OUTER (64ull*1024*1024)
#define FR_INNER (192ull*1024*1024)
#define FR_COORD (48ull*1024*1024)
#define FR_WORKER FR_COORD
#define FR_RSS (FR_OUTER+FR_INNER)
#define FR_MAX_MEMBERS 8192u
#define FR_IMAGE_MAX (128ull*1024*1024)
static const int fr_source_fd[FR_SOURCES]={101,102,103,104,105,106,107,108,109,110,112,113,114,115,116,117,118,119,127};
/* All integers little endian, fixed Linux x86_64 ABI; no pointers or padding on wire. */
struct __attribute__((packed)) fr_cap {
 uint8_t magic[8]; uint32_t version, mode, generation, uid, gid, sources;
 uint64_t start_ns,work_ns,hard_ns,root_dev,root_ino,mount_ns,outer_dev,outer_ino,inner_dev,inner_ino;
 uint8_t session[32],image_sha[32],manifest_sha[32],os_sha[32],pins[FR_SOURCES][32];
};
struct __attribute__((packed)) fr_os {
 uint8_t magic[8],boot_id[16]; char release[64];
 uint64_t mount_ns,pid_ns,cgroup_ns,outer_dev,outer_ino,inner_dev,inner_ino;
 uint32_t kernel_min,uid,gid,version;
 uint64_t cap_inh,cap_prm,cap_eff,cap_bnd,cap_amb;
 uint32_t securebits,groups_count;
};
/* Image is a sealed descriptor-image index, not a second mutable named runtime.
 * Header is followed by exactly count records; no trailing bytes permitted.
 * FILE paths are real sealed-memfd bind mounts; DIR and ALIAS are complete view.
 * size/hash/dev/ino identify the actual same inode consumed by loader/imports.
 * aliases are relative and can only resolve to a declared FILE inside the view. */
struct __attribute__((packed)) fr_image {uint8_t magic[8];uint32_t version,count;uint64_t total;uint8_t manifest_sha[32];};
struct __attribute__((packed)) fr_member {
 uint32_t kind,mode; uint64_t size,dev,ino; uint8_t sha[32];char path[512],target[512];
};
enum {FR_FILE=1,FR_DIR=2,FR_ALIAS=3};
enum {FR_CONTROLS=1,FR_PREFLIGHT=2,FR_EXECUTE=3,FR_BENIGN=4};
#define FR_SOL068_INTERFACE "friday.sol068.production-clock-source-interface.v1"
/* Same sealed fd119/capsule, fixed original216 order. These entries all own
 * exactly three direct targets. ABI, roles, budgets and four mode meanings stay. */
static inline int fr_sol068_position(unsigned p){
 switch(p){case 35:case 37:case 38:case 39:case 44:case 57:case 58:case 59:
 case 60:case 61:case 62:case 112:case 113:case 114:case 125:case 126:
 case 127:case 128:return 1;default:return 0;}
}
enum {FR_START=1,FR_INTENT=2,FR_INTENT_ACK=3,FR_REGISTER=4,FR_REGISTER_ACK=5,
      FR_ABORT=6,FR_REAP=7,FR_REAP_ACK=8,FR_FINISH=9,FR_FINISH_ACK=10,FR_STOP=11,FR_DRAIN=12,FR_DRAIN_ACK=13,FR_ABORT_ACK=14};
struct __attribute__((packed)) fr_packet {
 uint8_t magic[8],session[32]; uint32_t version,type,sequence,role;
 int32_t pid,owner,status,detail; uint64_t birth,owner_birth,deadline_ns;
};
/* Published COPY of a durable internal preallocated intention. PID/pidfd are
 * written into native owned storage immediately on actual clone3 parent return,
 * before proc/sendmsg/validation. Public wait/stop compare the exact copy and
 * operate ONLY on that internal same-owner entry; caller fields grant no PID,
 * pidfd, status, generation or signal authority. ABI remains32bytes. */
struct fr_child {int32_t pid,pidfd,role,state;uint64_t birth;int32_t status,creation_errno;};
/* Read-only projection of private native state. Unknown wait values are never
 * returned as a caller-authoritative status. No observation grants ownership. */
struct fr_observation {
 int32_t owner,origin,uid,gid,pid,pidfd,role,state;
 uint64_t owner_birth,origin_birth,birth;
 uint32_t next_sequence,session_ready,creation_poisoned,wait_observed;
 uint32_t status_known,cleanup_reaped,handle_closed,stop_attempted;
 int32_t status,reserved;
};
_Static_assert(sizeof(struct fr_observation)==96,"native observation ABI mismatch");
_Static_assert(sizeof(struct fr_cap)==848,"capsule wire mismatch");
_Static_assert(sizeof(struct fr_os)==208,"OS wire mismatch");
_Static_assert(sizeof(struct fr_image)==56,"image header wire mismatch");
_Static_assert(sizeof(struct fr_member)==1088,"image member wire mismatch");
_Static_assert(sizeof(struct fr_packet)==96,"registration wire mismatch");
_Static_assert(sizeof(struct fr_child)==32,"owned child ABI mismatch");
enum {FR_EMPTY=0,FR_CREATED=1,FR_REGISTERED=2,FR_REAPED=3,FR_UNKNOWN=4};
/* Public read-only receipts describe real syscall results, never authority.
 * The fixed v1 packet/capsule/child/observation ABIs above remain unchanged. */
struct __attribute__((packed)) fr_receive_receipt {
 uint32_t credentials,rights,closed,flags;int32_t close_errno,bytes;
};
enum {FR_E_INPUT=1,FR_E_CAPSULE,FR_E_OWNER_PROC,FR_E_SOCKET,FR_E_RECEIVE,
 FR_E_ORIGIN_PID,FR_E_ORIGIN_UID,FR_E_ORIGIN_GID,FR_E_FRAME,FR_E_TYPE,
 FR_E_SEQUENCE,FR_E_ROLE,FR_E_PID,FR_E_OWNER,FR_E_BIRTH,FR_E_OWNER_BIRTH,
 FR_E_DEADLINE,FR_E_STATUS,FR_E_RIGHTS,FR_E_RIGHT_CLOSE,FR_E_COPY,
 FR_E_HANDLE,FR_E_SIGNAL,FR_E_WAIT4,FR_E_HANDLE_CLOSE,FR_E_TRANSPORT_CLOSE,
 FR_E_CREATE_PIPE,FR_E_CREATE_CLONE,FR_E_BARRIER};
struct __attribute__((packed)) fr_evidence {
 uint32_t version,phase,stage;int32_t primitive_errno;
 int32_t observed_pid,observed_uid,observed_gid,expected_pid,expected_uid,expected_gid;
 uint32_t rights,rights_closed;int32_t rights_close_errno;
 uint32_t ready_released,signal_attempts,close_attempts,transport_close_attempts,status_redacted;
 uint64_t intent_ack_ns,clone_ns,register_send_ns,register_ack_ns,release_ns,
          wait4_ns,reap_ack_ns,signal_ns,close_ns,transport_close_ns;
 struct fr_packet expected,observed;
};
_Static_assert(sizeof(struct fr_receive_receipt)==24,"receive receipt ABI mismatch");
_Static_assert(sizeof(struct fr_evidence)==344,"private evidence ABI mismatch");
struct fr_sha {uint32_t h[8];uint64_t bits;uint8_t b[64];size_t used;};
void fr_sha_init(struct fr_sha*);void fr_sha_update(struct fr_sha*,const void*,size_t);void fr_sha_end(struct fr_sha*,uint8_t[32]);
uint64_t fr_now(void);int fr_hash_fd(int,uint64_t,uint8_t[32]);int fr_sealed(int,int);
int fr_proc(int,int*,uint64_t*);int fr_pidfd_pid(int);int fr_cap_read(int,const char*,struct fr_cap*);
int fr_limits(uint64_t,unsigned);int fr_send(int,const struct fr_packet*,int,uint64_t);
int fr_recv(int,struct fr_packet*,int*,int*,int*,int*,uint64_t);
int fr_recv_receipt(int,struct fr_packet*,int*,int*,int*,int*,uint64_t,struct fr_receive_receipt*);
int fr_send_rights(int,const struct fr_packet*,const int*,unsigned,uint64_t);
int fr_own_spawn(int,unsigned,int,int,int,int,int,uint64_t,struct fr_child*);
int fr_own_wait(int,struct fr_child*,int);int fr_own_stop(int,struct fr_child*,uint64_t);
int fr_fixture_fork(int);int fr_fixture_reap(int,int,int);
int fr_fixture_wait(int,int,int,int*);
int fr_public_command(unsigned);
int fr_session_start(int,const char*,struct fr_packet*);
int fr_session_observe(struct fr_observation*);
int fr_session_projection(struct fr_observation*);
int fr_own_release(struct fr_child*);
int fr_fixture_observe(int,struct fr_observation*);
int fr_fixture_evidence(int,struct fr_evidence*);
int fr_fixture_stop(int,int,uint64_t);
/* Ordinary owned fork is a real public API, used by the new ordinary caller.
 * It returns a real native-bound child copy even on post-clone refusal. No
 * supplied PID/status/pidfd can be adopted. Children return only after ACK. */
int fr_own_fork(int,unsigned,uint64_t,struct fr_child*);
int fr_own_observe(struct fr_child*,struct fr_observation*);
int fr_own_evidence(struct fr_child*,struct fr_evidence*);
int fr_session_evidence(struct fr_evidence*);
int fr_session_close(void);
/* A158: a distinct, one-shot Root-selected controller binding. This NEVER
 * resets/adopts the inherited ordinary session or its private child records.
 * prepare is only a bounded request; join needs actual Root SCM credentials
 * and the exact actual registered role0 generation. No caller PID is accepted.
 * Existing cap848/packet96/child32/observation96/evidence344 remain unchanged. */
int fr_controller_prepare(unsigned);
int fr_controller_join(unsigned,struct fr_evidence*);
int fr_controller_finish(struct fr_evidence*);
int fr_controller_observe(struct fr_observation*);
/* Restricts this calling process's own syscall capability in the kernel.
 * operation1 denies pidfd_send_signal; operation2 denies close of exact fd.
 * This cannot restore permissions and does not fabricate a syscall result. */
int fr_local_restrict(unsigned,int);
int fr_open_beneath(int,const char*,int);int fr_image_verify(int,int,const uint8_t*,const uint8_t*,uint64_t);
int fr_runtime_verify(int,struct fr_member*,unsigned,uint64_t);
void fr_stage_set(const char*);const char *fr_stage_get(void);
/* Source-only backing capacity is NOT a universal fit or Root grant. */
#define FR_BODY_CAP (16ull*1024*1024)
#define FR_BODY_HEADER 128u
#define FR_BODY_FD 131
struct __attribute__((packed)) fr_body_plane{
 uint8_t magic[8];uint32_t version,slot;uint64_t capacity,generation;
 uint8_t session[32],cap_sha[32];uint64_t sequence,end,poison,retired;
};
_Static_assert(sizeof(struct fr_body_plane)==128,"A201 body header ABI");
int fr_body_source_validate(int,unsigned,const struct fr_cap*,int);
#endif
