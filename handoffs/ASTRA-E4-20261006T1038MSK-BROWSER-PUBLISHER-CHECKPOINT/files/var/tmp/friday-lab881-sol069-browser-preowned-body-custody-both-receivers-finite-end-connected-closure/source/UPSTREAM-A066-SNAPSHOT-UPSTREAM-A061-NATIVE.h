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
enum {FR_START=1,FR_INTENT=2,FR_INTENT_ACK=3,FR_REGISTER=4,FR_REGISTER_ACK=5,
      FR_ABORT=6,FR_REAP=7,FR_REAP_ACK=8,FR_FINISH=9,FR_FINISH_ACK=10,FR_STOP=11,FR_DRAIN=12,FR_DRAIN_ACK=13,FR_ABORT_ACK=14};
struct __attribute__((packed)) fr_packet {
 uint8_t magic[8],session[32]; uint32_t version,type,sequence,role;
 int32_t pid,owner,status,detail; uint64_t birth,owner_birth,deadline_ns;
};
/* Durable preallocated intention. PID/pidfd are written immediately on clone3
 * parent return, before proc/sendmsg/validation; caller must always dispose it. */
struct fr_child {int32_t pid,pidfd,role,state;uint64_t birth;int32_t status,creation_errno;};
_Static_assert(sizeof(struct fr_cap)==848,"capsule wire mismatch");
_Static_assert(sizeof(struct fr_os)==208,"OS wire mismatch");
_Static_assert(sizeof(struct fr_image)==56,"image header wire mismatch");
_Static_assert(sizeof(struct fr_member)==1088,"image member wire mismatch");
_Static_assert(sizeof(struct fr_packet)==96,"registration wire mismatch");
_Static_assert(sizeof(struct fr_child)==32,"owned child ABI mismatch");
enum {FR_EMPTY=0,FR_CREATED=1,FR_REGISTERED=2,FR_REAPED=3,FR_UNKNOWN=4};
struct fr_sha {uint32_t h[8];uint64_t bits;uint8_t b[64];size_t used;};
void fr_sha_init(struct fr_sha*);void fr_sha_update(struct fr_sha*,const void*,size_t);void fr_sha_end(struct fr_sha*,uint8_t[32]);
uint64_t fr_now(void);int fr_hash_fd(int,uint64_t,uint8_t[32]);int fr_sealed(int,int);
int fr_proc(int,int*,uint64_t*);int fr_pidfd_pid(int);int fr_cap_read(int,const char*,struct fr_cap*);
int fr_limits(uint64_t,unsigned);int fr_send(int,const struct fr_packet*,int,uint64_t);
int fr_recv(int,struct fr_packet*,int*,int*,int*,int*,uint64_t);
int fr_own_spawn(int,unsigned,int,int,int,int,int,uint64_t,struct fr_child*);
int fr_own_wait(int,struct fr_child*,int);int fr_own_stop(int,struct fr_child*,uint64_t);
int fr_fixture_fork(int);int fr_fixture_reap(int,int,int);
int fr_public_command(unsigned);
int fr_open_beneath(int,const char*,int);int fr_image_verify(int,int,const uint8_t*,const uint8_t*,uint64_t);
int fr_runtime_verify(int,struct fr_member*,unsigned,uint64_t);
void fr_stage_set(const char*);const char *fr_stage_get(void);
#endif
