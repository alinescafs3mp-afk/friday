/* SOURCE ONLY: separately reviewed static x86_64 Linux owner. No setuid bit,
 * capability grant, provisioning, named generated exec, or public command seam. */
#include "A071-NATIVE.h"
#include <linux/sched.h>
#include <linux/magic.h>
#include <linux/capability.h>
#include <linux/securebits.h>
#include <linux/memfd.h>
#include <sys/syscall.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/statvfs.h>
#include <sys/resource.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <sys/utsname.h>
#include <sys/poll.h>
#include <sched.h>
#include <grp.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <elf.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/audit.h>
static volatile sig_atomic_t cancelled;
static void owner_stop(int s){(void)s;cancelled=1;}
static int drop_credentials(unsigned uid,unsigned gid){
 if(prctl(PR_CAP_AMBIENT,PR_CAP_AMBIENT_CLEAR_ALL,0,0,0))return -errno;
 for(unsigned cap=0;cap<64;cap++)if(prctl(PR_CAPBSET_DROP,cap,0,0,0)&&errno!=EINVAL)return -errno;
 if(prctl(PR_SET_SECUREBITS,SECBIT_NOROOT|SECBIT_NOROOT_LOCKED|SECBIT_NO_CAP_AMBIENT_RAISE|SECBIT_NO_CAP_AMBIENT_RAISE_LOCKED,0,0,0)||setgroups(0,0)||setresgid(gid,gid,gid)||setresuid(uid,uid,uid))return -errno;
 struct __user_cap_header_struct h={_LINUX_CAPABILITY_VERSION_3,0};struct __user_cap_data_struct d[2]={{0},{0}};
 if(syscall(SYS_capset,&h,d)||prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0))return -errno;
 if(syscall(SYS_capget,&h,d)||d[0].effective||d[0].permitted||d[0].inheritable||d[1].effective||d[1].permitted||d[1].inheritable)return -EPERM;
 return 0;
}
struct owned {int pid,pidfd,reaped,status,registered,role;uint64_t birth;int kernel_status_known;};
static struct owned records[513];
static unsigned used=1,started_workers;
static uint32_t registry_sequence=2;
static unsigned char stop_seen[513];
static const char*failure;
static int sticky;
static char control_case[64];
static int control_reply_used,control_helper_created,control_helper_reaped,control_helper_closed;
static int control_plain_created,control_plain_closed,control_credential_transitions;
static unsigned control_ack_type,control_reply_rights;
static int control_reply_pid,control_reply_uid,control_reply_gid;
static uint64_t control_reply_ns;
/* A091 receipts belong to the normal Root receiver. There is no second
 * receiver, fabricated credential or caller-supplied wait custody. */
static int receiving_case;
static unsigned whole216_case;
static int whole216_controller_start_sent,whole216_controller_finished;
static const char*const whole216_ids[216]={
 "actual_pinned_registry_browsers_cft_positive",
 "helper_path",
 "helper_mirror",
 "helper_tail",
 "helper_unknown",
 "platform_value",
 "platform_unknown",
 "platform_duplicate",
 "unknown_fallback",
 "route_tail",
 "ffmpeg_route",
 "revision",
 "version",
 "revision_type",
 "duplicate_browser",
 "browsers_type",
 "cft_version",
 "cft_url",
 "cft_platform",
 "cft_duplicate_linux",
 "cft_download_type",
 "owner_pin",
 "helper_digest",
 "browser_route",
 "browser_cap_type",
 "resource_type",
 "fourth_browser",
 "target_change",
 "real_owner_wrong_digest",
 "real_metadata_wrong_digest",
 "header64KiB_cap",
 "mapping_source262KiB_cap",
 "duplicate_json_key",
 "nonfinite_json",
 "json_depth",
 "whole3_actual_G1_worker_positive",
 "whole3_redirect",
 "whole3_http404",
 "whole3_tls_certificate",
 "whole3_tls_protocol",
 "whole3_encoding",
 "whole3_framing",
 "whole3_duplicate_length",
 "whole3_length_cap",
 "whole3_truncated",
 "whole3_hostname",
 "whole3_verify_mode",
 "whole3_verified_type",
 "whole3_cert_digest",
 "whole3_accounting",
 "whole3_digest",
 "whole3_acceptance",
 "whole3_unknown_final",
 "whole3_duplicate_final",
 "late_input",
 "aggregate_RSS",
 "deadline_create_fresh_browser3",
 "deadline_receipt_write",
 "deadline_receipts_fsynced",
 "deadline_inventory_write",
 "deadline_inventory_fsynced",
 "deadline_terminal_seal",
 "deadline_closed_terminal",
 "owner_stop",
 "sticky_cleanup",
 "unknown_owned_child_sticky",
 "existing_target",
 "wrong_mode",
 "hardlink",
 "symlink",
 "outer_mirror",
 "retained_positive",
 "retained_content",
 "retained_path",
 "retained_fd",
 "retained_directory",
 "retained_membership",
 "output_positive",
 "output_mode",
 "output_hardlink",
 "output_symlink",
 "output_membership",
 "output_root",
 "output_directory",
 "output_disk",
 "output_collision",
 "reservation",
 "file_count",
 "dir_count",
 "pinned_body_file_cap",
 "guarded_hash",
 "guarded_write",
 "actual_CA_wrong_digest",
 "actual_CA_parse",
 "actual_resources_positive",
 "actual_resources_leaf",
 "actual_resources_limit",
 "actual_resources_memory",
 "actual_resources_ancestor",
 "actual_resources_disk",
 "actual_resources_host_memory",
 "whole3_bounded_header",
 "late_during_work",
 "actual_output_close",
 "actual_signal_restore",
 "active_owned_stop",
 "public_admission_positive",
 "public_unknown",
 "public_environment",
 "public_self_pin",
 "public_bill_pin",
 "public_controls_pin",
 "deadline_final_retained_hash",
 "deadline_inventory_serialization",
 "deadline_inventory_serialized",
 "terminal_serialization",
 "terminal_serialized",
 "serialization_failure",
 "sticky_serialization",
 "terminal_output_path",
 "connect15_unconnected",
 "request300_connected",
 "work1140_boundary",
 "hard1200_boundary",
 "work1140_active",
 "body_eof_at_cap",
 "body_cap_without_eof",
 "body_short_write",
 "body_partial_write",
 "disk_reservation_positive",
 "disk_reservation_boundary",
 "moving_owned_collect_no_hash",
 "combined_active_unknown_close_serialization",
 "terminal_sink_partial_positive",
 "terminal_sink_zero",
 "terminal_sink_failed",
 "terminal_sink_blocked",
 "terminal_sink_partial_failed",
 "outer_positive",
 "outer_partial",
 "outer_json",
 "outer_multiple",
 "outer_false_success",
 "outer_exit",
 "outer_stderr",
 "outer_stdout_cap",
 "outer_stderr_cap",
 "outer_blocked",
 "outer_rss",
 "outer_unknown_member",
 "outer_stop_unknown",
 "outer_owner_stop",
 "outer_unknown_pin",
 "outer_runtime_path",
 "outer_sink_short",
 "outer_sink_failed",
 "outer_sink_blocked",
 "held_positive",
 "held_path_replaced_exact_bytes",
 "held_wrong_sha",
 "held_cap",
 "held_root_authority_missing",
 "held_read_timeout",
 "held_kernel_write_refused",
 "native_launcher_authority_missing",
 "held_unsealed",
 "owned_registration_pidfd",
 "owned_registration_proc",
 "owned_registration_after_spawn",
 "owned_registration_race",
 "owned_registration_post_custody",
 "runtime_positive",
 "runtime_schema",
 "runtime_file_set",
 "runtime_stdlib_absent",
 "runtime_directory",
 "runtime_symlink",
 "runtime_zip",
 "runtime_preload",
 "runtime_library_path",
 "runtime_membership",
 "runtime_pin_row",
 "runtime_read_cap",
 "runtime_unknown_pin",
 "runtime_pin_sha",
 "runtime_pin_custody",
 "runtime_soname_map",
 "runtime_cache_format",
 "runtime_cache_cap",
 "runtime_cache_string",
 "runtime_cache_resolution",
 "runtime_interpreter",
 "runtime_elf_format",
 "runtime_elf_headers",
 "runtime_dynamic_cap",
 "runtime_dynamic_search",
 "runtime_strtab",
 "runtime_soname",
 "runtime_dependency",
 "runtime_loader_pin",
 "runtime_loader_custody",
 "runtime_timeout",
 "resource_envelope_positive",
 "resource_envelope_membership",
 "resource_envelope_mount",
 "resource_envelope_envelope",
 "resource_envelope_memory",
 "resource_envelope_ancestor",
 "resource_envelope_host",
 "resource_envelope_disk",
 "resource_envelope_cpu",
 "resource_envelope_cap",
 "cgroup_values_positive",
 "cgroup_values_bound",
 "cgroup_values_populated",
 "cgroup_values_events"
};
enum {REG_PROC_PARENT=30,REG_PROC_BIRTH,REG_PENDING,REG_FD_TYPE,
      REG_READY,REG_ACK,REG_GENERATION,REG_DUPLICATE,REG_LIVE,REG_FINISH};
struct reg_evidence {
 unsigned kind,phase,stage,status_argument_invalid;int error;
 int peer_pid,peer_uid,peer_gid,expected_pid,expected_uid,expected_gid;
 int proc_parent,expected_parent,handle_pid,pending_role;
 uint64_t proc_birth,expected_birth,at_ns,pending_end,ack_ns;
 struct fr_receive_receipt received;struct fr_packet packet;
};
static struct reg_evidence reg_events[64];static unsigned reg_event_count;
struct root_live_member {int pid,parent,role,handle_pid;uint64_t birth;struct stat handle;};
static struct root_live_member root_all3[4];static uint64_t root_all3_ns;
static struct stat root_all3_image;static char root_all3_image_sha[65],root_all3_argv_sha[65];static size_t root_all3_argv_size;
static int stock_same_stat(const struct stat*,const struct stat*);
static void stock_hex(const uint8_t*,char[65]);
/* Driver-created output-only pipes. They are not Source roles, registry
 * operands or grants. The actual inner parent closes both before stock work. */
struct raw_custody {int fd,write_errno,close_errno,closed,after_known;size_t emitted;uint64_t close_ns;struct stat identity,after;};
static struct raw_custody raw_custody[2];static int raw_transport_selected;
static int raw_transport_bind(void){
 struct stat a,b;int ra=fstat(128,&a),ea=errno,rb=fstat(129,&b),eb=errno;
 if(ra&&rb&&ea==EBADF&&eb==EBADF)return 0;
 if(ra||rb||!S_ISFIFO(a.st_mode)||!S_ISFIFO(b.st_mode)||a.st_uid||a.st_gid||b.st_uid||b.st_gid||a.st_ino==b.st_ino||a.st_nlink!=1||b.st_nlink!=1||(fcntl(128,F_GETFL)&O_ACCMODE)!=O_WRONLY||(fcntl(129,F_GETFL)&O_ACCMODE)!=O_WRONLY)return -EPERM;
 raw_custody[0]=(struct raw_custody){.fd=128,.identity=a};raw_custody[1]=(struct raw_custody){.fd=129,.identity=b};raw_transport_selected=1;return 0;
}
static unsigned root_signal_attempts[513],root_close_attempts[513];
static int root_signal_errno[513],root_close_errno[513];
static uint64_t root_signal_ns[513],root_wait_ns[513],root_close_ns[513];
static unsigned root_transport_close_attempts;static int root_transport_close_errno;
static uint64_t root_transport_close_ns,root_failure_ns,root_cleanup_grace_end;
static uint64_t root_last_ack_begin_ns;
struct reg_origin_expected {int pid,uid,gid,parent;uint64_t birth;};
static struct reg_origin_expected reg_origin;
static void registry_origin(int pid,int uid,int gid,int parent,uint64_t birth){
 /* Expected arguments are not observed facts or owned-process authority.
  * The durable actual clone/PID/pidfd record is untouched. */
 reg_origin=(struct reg_origin_expected){pid,uid,gid,parent,birth};
}
static int root_transport_retained;
static struct reg_evidence *reg_event(unsigned kind){
 if(!receiving_case)return 0;
 if(reg_event_count>=64){if(!failure)failure="REG_EVIDENCE_CAP";sticky=1;return 0;}
 struct reg_evidence*e=&reg_events[reg_event_count++];memset(e,0,sizeof(*e));e->kind=kind;e->at_ns=fr_now();e->peer_pid=e->peer_uid=e->peer_gid=e->handle_pid=e->proc_parent=-1;return e;
}
static void reg_failure(struct reg_evidence*e,unsigned stage,int error,const char*cause){
 if(e){e->stage=stage;e->error=error;}
 if(!failure){failure=cause;root_failure_ns=fr_now();}sticky=1;
}
static int restrict_own_root(unsigned op,int fd){
 /* Irreversible reduction for this actual owner's own syscall only. No
  * userspace errno seam, permission restoration or privileged-service grant. */
 struct sock_filter f[]={
  BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,arch)),
  BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,AUDIT_ARCH_X86_64,1,0),
  BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
  BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr)),
  BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,op==1?SYS_pidfd_send_signal:SYS_close,0,3),
  BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0])),
  BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,(unsigned)fd,0,1),
  BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM),
  BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW)};
 if(op==1)f[5]=(struct sock_filter)BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
 struct sock_fprog p={sizeof(f)/sizeof(f[0]),f};
 return prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0)||prctl(PR_SET_SECCOMP,SECCOMP_MODE_FILTER,&p)?-errno:0;
}
static int control_input(const struct fr_cap*c){
 if(c->mode!=FR_CONTROLS)return 0;
 struct stat typed_st;char typed_head[512];
 if(fstat(119,&typed_st)||typed_st.st_size<0||typed_st.st_size>1048576)return -EIO;
 size_t typed_size=typed_st.st_size<511?(size_t)typed_st.st_size:511;
 if(pread(119,typed_head,typed_size,0)!=(ssize_t)typed_size)return -EIO;typed_head[typed_size]=0;
 const char typed_prefix[]="{\"schema\":\"friday.a158.whole216-input.v1\",\"position\":";
 if(!strncmp(typed_head,typed_prefix,sizeof(typed_prefix)-1)){
  char*end=0;errno=0;unsigned long position=strtoul(typed_head+sizeof(typed_prefix)-1,&end,10);
  if(errno||position>=216||!end||strncmp(end,",\"id\":\"",7))return -EINVAL;
  char expected[256];int n=snprintf(expected,sizeof(expected),"%s%lu,\"id\":\"%s\",",typed_prefix,position,whole216_ids[position]);
  if(n<=0||(size_t)n>=sizeof(expected)||typed_size<(size_t)n||memcmp(typed_head,expected,n))return -EINVAL;
  whole216_case=(unsigned)position+1;strcpy(control_case,whole216_ids[position]);receiving_case=1;return 0;
 }
 char b[4097];struct stat st;if(fstat(119,&st)||st.st_size<0||st.st_size>4096||pread(119,b,st.st_size,0)!=st.st_size)return -EIO;b[st.st_size]=0;
 const char prefix[]="{\"schema\":\"friday.a087.native-public-input.v1\",\"case\":\"";
 const char receiving_prefix[]="{\"schema\":\"friday.a091.receiving-public-input.v1\",\"case\":\"";
 if(!strncmp(b,receiving_prefix,sizeof(receiving_prefix)-1)){
  static const char*receiving[]={"positive","peer_pid","peer_uid","peer_gid","origin_parent_argument","origin_birth_argument","owner","owner_birth","frame_session","version","type","role","sequence_replay","sequence_future","deadline","intent_right","register_zero_rights","register_many_rights","register_plaintext","register_other_owned_pidfd","register_parent","register_birth","register_stale_pidfd","register_without_intent","register_late","abort_without_intent","abort_detail","abort_right","abort_positive","reap_live","reap_birth","reap_pid","reap_status","reap_right","reap_invalid_ACK","reap_lost_ACK","pending_timeout","drain_pending","drain_live","finish_without_drain","transport_closed","Root_signal_positive","Root_signal_denied","Root_transport_close_denied","inherited_start_owner"};
  for(unsigned i=0;i<sizeof(receiving)/sizeof(receiving[0]);i++){
   char expected[512];int n=snprintf(expected,sizeof(expected),"%s%s\",\"effects\":\"ordinary-own-process-and-plaintext-fd-only\"}\n",receiving_prefix,receiving[i]);
   if(n>0&&(size_t)n<sizeof(expected)&&st.st_size==n&&!memcmp(b,expected,n)){strcpy(control_case,receiving[i]);receiving_case=1;return 0;}
  }return -EINVAL;
 }
 if(strncmp(b,prefix,sizeof(prefix)-1))return 0;
 static const char*cases[]={"positive","start_origin_pid","start_origin_uid","start_origin_gid","start_owner","start_owner_birth","start_deadline","start_role","start_sequence","start_rights", "ack_origin_pid","ack_origin_uid","ack_origin_gid","ack_owner","ack_owner_birth","ack_session","ack_role","ack_sequence_replay","ack_sequence_future","ack_deadline","ack_rights","ack_many_rights", "register_invalid_ACK","register_lost_ACK","register_delayed_ACK", "abort_positive","abort_invalid_ACK","abort_lost_ACK", "reap_invalid_ACK","reap_lost_ACK", "copy_pid","copy_pidfd","copy_birth","copy_role","pidfd_plaintext","pidfd_closed","signal_denied","close_denied","transport_closed","role_invalid","deadline_expired","session_restart","wait_flags_DATA","caller_status_DATA"};
 for(unsigned i=0;i<sizeof(cases)/sizeof(cases[0]);i++){
  char expected[1024];int n=snprintf(expected,sizeof(expected),"%s%s\",\"workers\":[{\"payload\":\"ordinary-owned-a087/0\\n\",\"exit\":23},{\"payload\":\"ordinary-owned-a087/1\\n\",\"exit\":24},{\"payload\":\"ordinary-owned-a087/2\\n\",\"exit\":25}]}\n",prefix,cases[i]);
  if(n>0&&(size_t)n<sizeof(expected)&&st.st_size==n&&!memcmp(b,expected,n)){strcpy(control_case,cases[i]);return 0;}
 }
 return -EINVAL;
}
static int control_at(unsigned type){
 if(whole216_case)return 0;
 if(control_reply_used||!control_case[0])return 0;
 if(receiving_case)return (!strcmp(control_case,"reap_invalid_ACK")||!strcmp(control_case,"reap_lost_ACK"))&&type==FR_REAP_ACK;
 if(!strncmp(control_case,"start_",6))return type==FR_START;
 if(!strncmp(control_case,"ack_",4))return type==FR_INTENT_ACK;
 if(!strncmp(control_case,"register_",9))return type==FR_REGISTER_ACK;
 if(!strncmp(control_case,"abort_",6))return type==FR_ABORT_ACK&&strcmp(control_case,"abort_positive");
 if(!strncmp(control_case,"reap_",5))return type==FR_REAP_ACK;
 return 0;
}
static int controlled_reply(int sock,const struct fr_packet*original,unsigned type,uint64_t end){
 struct fr_packet q=*original;q.type=type;
 if(!control_at(type))return fr_send(sock,&q,-1,end);
 control_reply_used=1;control_ack_type=type;
 if(strstr(control_case,"lost_ACK")){control_reply_ns=fr_now();return receiving_case?-ETIMEDOUT:1;/* no ACK sent and no sequence credit */}
 if(!strcmp(control_case,"register_delayed_ACK")){uint64_t until=fr_now()+100000000ull;while(fr_now()<until&&fr_now()<end)poll(0,0,1);}
 if(strstr(control_case,"invalid_ACK"))q.type=FR_STOP;
 if(strstr(control_case,"owner_birth"))q.owner_birth++;
 else if(strstr(control_case,"owner"))q.owner++;
 if(strstr(control_case,"session"))q.session[0]^=1;
 if(strstr(control_case,"role"))q.role++;
 if(strstr(control_case,"sequence_replay"))q.sequence--;
 else if(strstr(control_case,"sequence_future")||!strcmp(control_case,"start_sequence"))q.sequence++;
 if(strstr(control_case,"deadline"))q.deadline_ns--;
 int plain=-1,passes[2]={-1,-1};unsigned count=0;
 if(strstr(control_case,"rights")){
  plain=syscall(SYS_memfd_create,"ordinary-public-right-a087",MFD_CLOEXEC|MFD_ALLOW_SEALING);if(plain<0)return -errno;control_plain_created++;
  const char text[]="ordinary-owned-plaintext-a087\n";
  if(write(plain,text,sizeof(text)-1)!=sizeof(text)-1||fcntl(plain,F_ADD_SEALS,FR_SEALS)<0){int saved=errno;close(plain);return -saved;}
  count=strstr(control_case,"many_rights")?2:1;passes[0]=passes[1]=plain;
 }
 int r=0;unsigned real_uid=getuid(),real_gid=getgid();
 if(strstr(control_case,"origin_uid")){if(setresuid(1000,0,0)){r=-errno;goto close_plain;}control_credential_transitions++;}
 if(strstr(control_case,"origin_gid")){if(setresgid(1000,0,0)){r=-errno;goto restore_uid;}control_credential_transitions++;}
 control_reply_uid=getuid();control_reply_gid=getgid();control_reply_rights=count;
 control_reply_ns=fr_now();
 if(strstr(control_case,"origin_pid")){
  int handle=-1;struct clone_args a={0};a.flags=CLONE_PIDFD|CLONE_INTO_CGROUP;a.pidfd=(uintptr_t)&handle;a.cgroup=121;a.exit_signal=SIGCHLD;
  pid_t child=syscall(SYS_clone3,&a,sizeof(a));
  if(child==0){if(fr_limits(FR_COORD,180)||prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0))_exit(125);int sent=fr_send_rights(sock,&q,passes,count,end);_exit(sent?125:0);}
  if(child<0){r=-errno;goto restore_gid;}
  control_helper_created++;control_reply_pid=child;int parent=0;uint64_t birth=0;
  if(handle<0||fr_proc(child,&parent,&birth)||parent!=getpid()||fr_pidfd_pid(handle)!=child){r=-ESTALE;}
  int status=0;struct rusage usage;uint64_t stop=fr_now()+1000000000ull;if(stop>end)stop=end;
  while(fr_now()<stop){pid_t waited=wait4(child,&status,WNOHANG,&usage);if(waited==child){control_helper_reaped++;if(!WIFEXITED(status)||WEXITSTATUS(status))r=-EIO;break;}if(waited<0){r=-errno;break;}poll(0,0,1);}
  if(!control_helper_reaped){/* the exact direct intention is retained; no guessed PID */
   int p=0;uint64_t b=0;if(handle>=0&&fr_pidfd_pid(handle)==child&&!fr_proc(child,&p,&b)&&p==getpid()&&b==birth)syscall(SYS_pidfd_send_signal,handle,SIGKILL,0,0);
   while(fr_now()<end){pid_t waited=wait4(child,&status,WNOHANG,&usage);if(waited==child){control_helper_reaped++;break;}if(waited<0)break;poll(0,0,1);}r=-ETIMEDOUT;
  }
  if(handle>=0){if(close(handle))r=-errno;else control_helper_closed++;}
 }else {control_reply_pid=getpid();r=fr_send_rights(sock,&q,passes,count,end);}
restore_gid:if(getgid()!=real_gid&&setresgid(real_gid,0,0))r=-errno;
restore_uid:if(getuid()!=real_uid&&setresuid(real_uid,0,0))r=-errno;
close_plain:if(plain>=0){if(close(plain))r=-errno;else control_plain_closed++;}
 return r;
}
static void fail(const char*s,int unknown){if(!failure){failure=s;root_failure_ns=fr_now();}if(unknown)sticky=1;}
static int text_at(int root,const char*name,char*b,size_t cap){int fd=openat(root,name,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return -errno;struct stat st;if(fstat(fd,&st)||st.st_uid||st.st_gid){close(fd);return -EPERM;}ssize_t n=read(fd,b,cap);close(fd);if(n<0||(size_t)n>=cap)return -EFBIG;while(n>0&&(b[n-1]=='\n'||b[n-1]==' '))n--;b[n]=0;return 0;}
static int value_at(int fd,const char*key,uint64_t*value){char b[128],*e;if(text_at(fd,key,b,sizeof(b)))return -EIO;errno=0;unsigned long long v=strtoull(b,&e,10);if(errno||*e||!b[0])return -EBADMSG;*value=v;return 0;}
static int group_check(int fd,uint64_t dev,uint64_t ino,uint64_t memory,unsigned pids,int empty){struct stat st;struct statfs fs;char b[256],wanted[64];if(fstat(fd,&st)||!S_ISDIR(st.st_mode)||st.st_uid||st.st_gid||(st.st_mode&022)||st.st_dev!=dev||st.st_ino!=ino||fstatfs(fd,&fs)||(unsigned long)fs.f_type!=CGROUP2_SUPER_MAGIC)return -EPERM;
 const char*keys[]={"memory.max","memory.swap.max","memory.oom.group","pids.max","cpu.max"};snprintf(wanted,sizeof(wanted),"%llu",(unsigned long long)memory);if(text_at(fd,keys[0],b,sizeof(b))||strcmp(b,wanted))return -EINVAL;
 snprintf(wanted,sizeof(wanted),"%u",pids);const char*values[]={"0","1",wanted,"max 100000"};for(unsigned i=1;i<5;i++)if(text_at(fd,keys[i],b,sizeof(b))||strcmp(b,values[i-1]))return -EINVAL;
 for(unsigned i=0;i<5;i++){int f=openat(fd,keys[i],O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(f<0)return -errno;if(fstat(f,&st)||(st.st_mode&022)){close(f);return -EPERM;}close(f);}
 uint64_t current;if(value_at(fd,"memory.current",&current)||current>memory)return -ENOMEM;
 if(empty&&(text_at(fd,"cgroup.procs",b,sizeof(b))||b[0]||text_at(fd,"cgroup.events",b,sizeof(b))||strcmp(b,"populated 0\nfrozen 0")))return -EBUSY;return 0;}
static uint64_t nsino(const char*path){struct stat st;return stat(path,&st)?0:st.st_ino;}
static int boot_id(uint8_t out[16]){char b[128];int fd=open("/proc/sys/kernel/random/boot_id",O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return -errno;ssize_t n=read(fd,b,sizeof(b));close(fd);unsigned used=0,v=0,half=0;for(ssize_t i=0;i<n;i++){char c=b[i];if(c=='-'||c=='\n')continue;unsigned x;if(c>='0'&&c<='9')x=c-'0';else if(c>='a'&&c<='f')x=c-'a'+10;else return -EINVAL;v=(v<<4)|x;if(++half==2){if(used>=16)return -EINVAL;out[used++]=v;v=half=0;}}return used==16&&!half?0:-EINVAL;}
static int os_check(const struct fr_cap*c){
 struct fr_os o;struct stat st;struct utsname u;uint8_t boot[16];
 fr_stage_set("os_header");
 if(fr_sealed(116,1)||fstat(116,&st)||st.st_size!=sizeof(o)||pread(116,&o,sizeof(o),0)!=sizeof(o)||memcmp(o.magic,"FRA061O1",8)||o.version!=1||o.uid!=c->uid||o.gid!=c->gid||o.kernel_min!=1)return -EPERM;
 fr_stage_set("os_release");if(!memchr(o.release,0,sizeof(o.release))||uname(&u)||strcmp(o.release,u.release))return -EPERM;
 fr_stage_set("os_boot");if(boot_id(boot)||memcmp(boot,o.boot_id,16))return -EPERM;
 fr_stage_set("os_mount_namespace");if(o.mount_ns!=nsino("/proc/self/ns/mnt")||o.mount_ns!=c->mount_ns)return -EPERM;
 fr_stage_set("os_pid_namespace");if(o.pid_ns!=nsino("/proc/self/ns/pid"))return -EPERM;
 fr_stage_set("os_cgroup_namespace");if(o.cgroup_ns!=nsino("/proc/self/ns/cgroup"))return -EPERM;
 fr_stage_set("os_cgroup_identity");if(o.outer_dev!=c->outer_dev||o.outer_ino!=c->outer_ino||o.inner_dev!=c->inner_dev||o.inner_ino!=c->inner_ino)return -EPERM;
 fr_stage_set("os_capabilities");
 char b[16384];int fd=open("/proc/self/status",O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return -errno;ssize_t n=read(fd,b,sizeof(b)-1);close(fd);if(n<=0||n==(ssize_t)sizeof(b)-1)return -EIO;b[n]=0;
 const char*names[]={"\nCapInh:\t","\nCapPrm:\t","\nCapEff:\t","\nCapBnd:\t","\nCapAmb:\t"};uint64_t wanted[]={o.cap_inh,o.cap_prm,o.cap_eff,o.cap_bnd,o.cap_amb};for(unsigned i=0;i<5;i++){char*p=strstr(b,names[i]),*end;if(!p)return -EPERM;errno=0;uint64_t v=strtoull(p+strlen(names[i]),&end,16);if(errno||(*end!='\n'&&*end!=' ')||v!=wanted[i])return -EPERM;}
 fr_stage_set("os_supplementary_groups");int groups=getgroups(0,0);if(groups<0||groups!=0||o.groups_count)return -EPERM;
 fr_stage_set("os_securebits");int secure=prctl(PR_GET_SECUREBITS,0,0,0,0);if(secure<0||secure!=(int)o.securebits)return -EPERM;return 0;}
static int static_elf(int fd){Elf64_Ehdr h;if(pread(fd,&h,sizeof(h),0)!=sizeof(h)||memcmp(h.e_ident,ELFMAG,4)||h.e_ident[EI_CLASS]!=ELFCLASS64||h.e_ident[EI_DATA]!=ELFDATA2LSB||h.e_machine!=EM_X86_64||(h.e_type!=ET_EXEC&&h.e_type!=ET_DYN)||h.e_phentsize!=sizeof(Elf64_Phdr)||!h.e_phnum||h.e_phnum>128)return -ENOEXEC;for(unsigned i=0;i<h.e_phnum;i++){Elf64_Phdr p;if(pread(fd,&p,sizeof(p),h.e_phoff+(uint64_t)i*sizeof(p))!=sizeof(p)||p.p_type==PT_INTERP)return -ENOEXEC;if(p.p_type==PT_DYNAMIC){if(p.p_filesz>65536||p.p_filesz%sizeof(Elf64_Dyn))return -ENOEXEC;for(uint64_t j=0;j<p.p_filesz;j+=sizeof(Elf64_Dyn)){Elf64_Dyn d;if(pread(fd,&d,sizeof(d),p.p_offset+j)!=sizeof(d)||d.d_tag==DT_NEEDED)return -ENOEXEC;}}}return 0;}
static int sources(const struct fr_cap*c){static char source_stage[64];uint8_t h[32];for(unsigned i=0;i<FR_SOURCES;i++){snprintf(source_stage,sizeof(source_stage),"source_kernel_seals_%d",fr_source_fd[i]);fr_stage_set(source_stage);if(fr_sealed(fr_source_fd[i],1))return -EPERM;snprintf(source_stage,sizeof(source_stage),"source_exact_pin_%d",fr_source_fd[i]);if(fr_hash_fd(fr_source_fd[i],i==9?16*1024*1024:1024*1024,h)||memcmp(h,c->pins[i],32))return -EPERM;}if(memcmp(c->pins[14],c->os_sha,32)||memcmp(c->pins[15],c->manifest_sha,32)||static_elf(110))return -EPERM;
 int self=open("/proc/self/exe",O_RDONLY|O_CLOEXEC);if(self<0)return -errno;int r=fr_hash_fd(self,16*1024*1024,h);close(self);return r||memcmp(h,c->pins[9],32)?-EPERM:0;}
static void packet(struct fr_packet*p,const struct fr_cap*c,unsigned type,unsigned role){memset(p,0,sizeof(*p));memcpy(p->magic,"FRA061P1",8);memcpy(p->session,c->session,32);p->version=1;p->type=type;p->role=role;p->sequence=role+1;p->deadline_ns=c->work_ns;}
static int find_role(unsigned role){for(unsigned i=1;i<used;i++)if((unsigned)records[i].role==role)return i;return -1;}
static int list_members(int group,int out[16]){char b[2048],*save=0;if(text_at(group,"cgroup.procs",b,sizeof(b)))return -1;unsigned n=0;for(char*t=strtok_r(b,"\n",&save);t;t=strtok_r(0,"\n",&save)){char*end;long p=strtol(t,&end,10);if(*end||p<=0||p>2147483647||n>=16)return -1;out[n++]=p;}return n;}
static int membership(int group,int pending){int ps[16],n=list_members(group,ps);if(n<0||n>4)return -1;unsigned unknown=0;for(int i=0;i<n;i++){int known=0;for(unsigned j=0;j<used;j++)if(records[j].pid==ps[i]&&!records[j].reaped)known=1;if(!known)unknown++;}return unknown&&(!pending||unknown>1)?-1:0;}
static int root_observe_all3(const struct fr_cap*c){
 int members[16];if(used!=4||root_all3_ns||list_members(121,members)!=4)return -EUCLEAN;
 char argv[4096],observed[4096],path[64];int argv_fd=open("/proc/self/cmdline",O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(argv_fd<0)return -errno;
 ssize_t argv_size=read(argv_fd,argv,sizeof(argv));if(close(argv_fd))return -errno;if(argv_size<=0||argv_size==(ssize_t)sizeof(argv)||argv[argv_size-1])return -EFBIG;
 struct fr_sha argv_hash;uint8_t sha[32];fr_sha_init(&argv_hash);fr_sha_update(&argv_hash,argv,argv_size);fr_sha_end(&argv_hash,sha);stock_hex(sha,root_all3_argv_sha);root_all3_argv_size=argv_size;
 if(fstat(110,&root_all3_image))return -errno;stock_hex(c->pins[9],root_all3_image_sha);
 for(unsigned i=0;i<4;i++){struct owned*r=&records[i];int parent=0,present=0;uint64_t birth=0;struct stat a,b;
  for(unsigned j=0;j<4;j++)if(members[j]==r->pid)present++;
  if(present!=1||r->reaped||r->pidfd<0||fr_pidfd_pid(r->pidfd)!=r->pid||fstat(r->pidfd,&a)||fr_proc(r->pid,&parent,&birth)||parent!=(i?records[0].pid:getpid())||birth!=r->birth||fstat(r->pidfd,&b)||a.st_dev!=b.st_dev||a.st_ino!=b.st_ino||a.st_mode!=b.st_mode||a.st_uid!=b.st_uid||a.st_gid!=b.st_gid||a.st_nlink!=b.st_nlink||a.st_size!=b.st_size||a.st_mtim.tv_sec!=b.st_mtim.tv_sec||a.st_mtim.tv_nsec!=b.st_mtim.tv_nsec||a.st_ctim.tv_sec!=b.st_ctim.tv_sec||a.st_ctim.tv_nsec!=b.st_ctim.tv_nsec)return -ESTALE;
  snprintf(path,sizeof(path),"/proc/%d/exe",r->pid);int exe=open(path,O_RDONLY|O_CLOEXEC);if(exe<0)return -errno;
  struct stat image,after;int bad=fstat(exe,&image)||!stock_same_stat(&image,&root_all3_image)||fr_hash_fd(exe,16*1024*1024,sha)||memcmp(sha,c->pins[9],32)||fstat(exe,&after)||!stock_same_stat(&image,&after);
  if(close(exe))return -errno;if(bad)return -ESTALE;
  snprintf(path,sizeof(path),"/proc/%d/cmdline",r->pid);int arg=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(arg<0)return -errno;
  ssize_t size=read(arg,observed,sizeof(observed));if(close(arg))return -errno;if(size!=argv_size||memcmp(argv,observed,argv_size))return -ESTALE;
  if(fr_proc(r->pid,&parent,&birth)||parent!=(i?records[0].pid:getpid())||birth!=r->birth||fr_pidfd_pid(r->pidfd)!=r->pid)return -ESTALE;
  root_all3[i]=(struct root_live_member){r->pid,parent,r->role,r->pid,birth,a};
 }
 root_all3_ns=fr_now();return 0;
}
static uint64_t rss_pid(int pid,int*ok){char path[64],b[16384];snprintf(path,sizeof(path),"/proc/%d/status",pid);int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0){*ok=0;return 0;}ssize_t n=read(fd,b,sizeof(b)-1);close(fd);if(n<=0||n==(ssize_t)sizeof(b)-1){*ok=0;return 0;}b[n]=0;char*x=strstr(b,"\nVmRSS:");if(x){unsigned long long k;char unit[8];if(sscanf(x+8,"%llu %7s",&k,unit)==2&&!strcmp(unit,"kB"))return k*1024;}if(strstr(b,"\nState:\tZ"))return 0;*ok=0;return 0;}
static void stop_known(void){for(unsigned i=0;i<used;i++){struct owned*r=&records[i];if(r->reaped||r->pid<=0||stop_seen[i])continue;stop_seen[i]=1;if(r->pidfd>=0){
 int seen=fr_pidfd_pid(r->pidfd);if(seen==-1)continue;int parent;uint64_t birth;
 if(seen!=r->pid||fr_proc(r->pid,&parent,&birth)||birth!=r->birth||(parent!=getpid()&&(i==0||parent!=records[0].pid))){fail("OWNED_SIGNAL_GENERATION",1);continue;}
 root_signal_attempts[i]++;root_signal_ns[i]=fr_now();
 if(syscall(SYS_pidfd_send_signal,r->pidfd,SIGKILL,0,0)&&errno!=ESRCH){root_signal_errno[i]=errno;fail("OWNED_SIGNAL_FAILED",1);}
 struct reg_evidence*e=reg_event(4);if(e){e->phase=(unsigned)i;e->stage=FR_E_SIGNAL;e->error=-root_signal_errno[i];e->peer_pid=r->pid;e->handle_pid=seen;e->proc_parent=parent;e->proc_birth=birth;e->expected_birth=r->birth;}
 }else if(i==0){int status;struct rusage usage;pid_t p=wait4(r->pid,&status,WNOHANG,&usage);if(p==r->pid){r->reaped=1;r->status=status;r->kernel_status_known=1;}else if(p==0){if(kill(r->pid,SIGKILL)&&errno!=ESRCH)fail("OWNED_SIGNAL_FAILED",1);}else fail("OWNED_WAIT_CUSTODY_LOST",1);}else fail("OWNED_HANDLE_MISSING",1);}}
static void reap_owned(void){for(unsigned i=0;i<used;i++){struct owned*r=&records[i];if(r->reaped||r->pid<=0)continue;int status;struct rusage usage;pid_t p=wait4(r->pid,&status,WNOHANG,&usage);if(p==r->pid){r->reaped=1;r->status=status;r->kernel_status_known=1;root_wait_ns[i]=fr_now();struct reg_evidence*e=reg_event(5);if(e){e->phase=i;e->stage=FR_E_WAIT4;e->peer_pid=r->pid;}}else if(p<0&&errno!=ECHILD)fail("OWNED_WAIT_FAILED",1);else if(p<0&&i==0)fail("OWNED_WAIT_CUSTODY_LOST",1);/* ECHILD and caller labels grant no kernel status credit. */}}
static int send_ack(int sock,struct fr_packet*q,unsigned type,uint64_t end){q->type=type;root_last_ack_begin_ns=fr_now();int r=controlled_reply(sock,q,type,end);if(!r){if(registry_sequence==UINT32_MAX)return -EOVERFLOW;registry_sequence++;if(receiving_case&&!strcmp(control_case,"reap_invalid_ACK")&&type==FR_REAP_ACK)return -EBADMSG;}return r<0?r:0;}
static int controller_finish_request(int sock,const struct fr_cap*c,struct fr_packet*q,
 int pass,int pid,int uid,int gid,const struct fr_receive_receipt*receipt,struct reg_evidence*e){
 /* Root selected this exact actual registered generation, not an arbitrary
  * inherited caller. This one FINISH has a separate two-message sequence;
  * the original coordinator sequence, owner and private waits are untouched. */
 int idx=find_role(0),parent=0;uint64_t birth=0;const char*cause=0;
 if(idx<1||!whole216_controller_start_sent||whole216_controller_finished||failure||pass>=0||
    receipt->rights||receipt->credentials!=1||uid!=(int)c->uid||gid!=(int)c->gid||
    pid!=records[idx].pid||records[idx].reaped||fr_pidfd_pid(records[idx].pidfd)!=pid||
    fr_proc(pid,&parent,&birth)||parent!=records[0].pid||birth!=records[idx].birth||
    memcmp(q->magic,"FRA061P1",8)||memcmp(q->session,c->session,32)||q->version!=1||
    q->type!=FR_FINISH||q->sequence!=2||q->role||q->pid!=pid||q->owner!=pid||
    q->birth!=birth||q->owner_birth!=birth||q->status||q->detail!=(int)whole216_case||
    q->deadline_ns!=c->work_ns||fr_now()>=c->work_ns)cause="REG_CONTROLLER_FINISH_BINDING";
 if(e){e->expected_pid=idx>=1?records[idx].pid:-1;e->expected_uid=c->uid;e->expected_gid=c->gid;
  e->expected_parent=records[0].pid;e->expected_birth=idx>=1?records[idx].birth:0;
  e->proc_parent=parent;e->proc_birth=birth;e->handle_pid=idx>=1?fr_pidfd_pid(records[idx].pidfd):-1;}
 if(cause){reg_failure(e,REG_GENERATION,-EPERM,cause);return -EPERM;}
 uint64_t ack_begin=fr_now();int r=controlled_reply(sock,q,FR_FINISH_ACK,c->work_ns);
 if(r){reg_failure(e,REG_ACK,r,"REG_CONTROLLER_FINISH_ACK");return r;}
 whole216_controller_finished=1;if(e)e->ack_ns=ack_begin;return 0;
}
static int registry(int sock,const struct fr_cap*c,int*pending,uint64_t*pending_end,int*finished,int*draining){
 struct fr_packet q;struct fr_receive_receipt receipt;int pass=-1,pid=-1,uid=-1,gid=-1;
 int r=fr_recv_receipt(sock,&q,&pass,&pid,&uid,&gid,fr_now()+10000000ull,&receipt);
 struct reg_evidence*e=reg_event(1);
 if(e){e->phase=q.type;e->packet=q;e->received=receipt;e->peer_pid=pid;e->peer_uid=uid;e->peer_gid=gid;
  e->expected_pid=reg_origin.pid;e->expected_uid=reg_origin.uid;e->expected_gid=reg_origin.gid;e->expected_parent=reg_origin.parent;e->expected_birth=reg_origin.birth;e->pending_role=*pending;e->pending_end=*pending_end;}
 const char*cause=0;unsigned stage=0;int error=-EBADMSG,idx=-1,parent=-1;uint64_t birth=0;unsigned max=c->mode==FR_CONTROLS?512:3;
 if(r){int eof=r==-EBADMSG&&!receipt.bytes&&!receipt.credentials&&!receipt.rights&&!receipt.flags;stage=receipt.close_errno?FR_E_RIGHT_CLOSE:receipt.rights>1?FR_E_RIGHTS:eof?FR_E_TRANSPORT_CLOSE:FR_E_RECEIVE;error=r;cause=receipt.close_errno?"REG_RIGHT_CLOSE_UNCONFIRMED":receipt.rights>1?"REG_RIGHT_COUNT":eof?"REG_TRANSPORT_CLOSED":"REG_RECEIVE";goto out;}
 if(whole216_case&&q.type==FR_FINISH&&pid!=records[0].pid){
  r=controller_finish_request(sock,c,&q,pass,pid,uid,gid,&receipt,e);
  if(pass>=0){if(close(pass)){if(e)e->received.close_errno=errno;reg_failure(e,FR_E_RIGHT_CLOSE,-errno,"REG_CONTROLLER_RIGHT_CLOSE");return -errno;}else if(e)e->received.closed++;}
  return r;
 }
 if(failure){stage=REG_GENERATION;cause="REG_GENERATION_ALREADY_POISONED";goto out;}
 if(pid!=records[0].pid){stage=FR_E_ORIGIN_PID;cause="REG_ORIGIN_PID";goto out;}
 if(uid!=(int)c->uid){stage=FR_E_ORIGIN_UID;cause="REG_ORIGIN_UID";goto out;}
 if(gid!=(int)c->gid){stage=FR_E_ORIGIN_GID;cause="REG_ORIGIN_GID";goto out;}
 if(records[0].reaped||fr_proc(pid,&parent,&birth)){stage=FR_E_OWNER_PROC;cause="REG_ORIGIN_PROC";goto out;}
 if(e){e->proc_parent=parent;e->proc_birth=birth;}
 if(parent!=reg_origin.parent){stage=REG_PROC_PARENT;cause="REG_ORIGIN_PARENT";goto out;}
 if(birth!=reg_origin.birth){stage=REG_PROC_BIRTH;cause="REG_ORIGIN_BIRTH";goto out;}
 if(memcmp(q.magic,"FRA061P1",8)||memcmp(q.session,c->session,32)||q.version!=1){stage=FR_E_FRAME;cause="REG_FRAME";goto out;}
 if(q.owner!=records[0].pid){stage=FR_E_OWNER;cause="REG_OWNER";goto out;}
 if(q.owner_birth!=records[0].birth){stage=FR_E_OWNER_BIRTH;cause="REG_OWNER_BIRTH";goto out;}
 if(q.role>=max){stage=FR_E_ROLE;cause="REG_ROLE";goto out;}
 if(q.sequence!=registry_sequence){stage=FR_E_SEQUENCE;cause="REG_SEQUENCE";goto out;}
 if(q.deadline_ns!=c->work_ns){stage=FR_E_DEADLINE;cause="REG_DEADLINE";goto out;}
 if(q.type!=FR_INTENT&&q.type!=FR_REGISTER&&q.type!=FR_ABORT&&q.type!=FR_REAP&&q.type!=FR_DRAIN&&q.type!=FR_FINISH){stage=FR_E_TYPE;cause="REG_PACKET_TYPE";goto out;}
 if((q.type==FR_REGISTER?receipt.rights!=1:receipt.rights!=0)){stage=FR_E_RIGHTS;cause="REG_RIGHT_COUNT";goto out;}
 idx=find_role(q.role);
 if(q.type==FR_INTENT){
  if(whole216_case&&(q.role!=0||used!=1||whole216_controller_start_sent)){stage=FR_E_ROLE;cause="REG_CONTROLLER_EXACT_NORMAL_ROLE";goto out;}
  if(q.pid||q.birth||q.status||q.detail||idx>=0||*pending>=0||used>=513||*draining||fr_now()>=c->work_ns){stage=FR_E_INPUT;cause="REG_INTENT_STATE";goto out;}
  *pending=q.role;*pending_end=fr_now()+15000000000ull;if(*pending_end>c->work_ns)*pending_end=c->work_ns;
  if(e){e->pending_role=*pending;e->pending_end=*pending_end;}
  r=send_ack(sock,&q,FR_INTENT_ACK,c->work_ns);if(r){stage=REG_ACK;error=r;cause="REG_INTENT_ACK";}
 }
 else if(q.type==FR_REGISTER){
  if(*pending!=(int)q.role||fr_now()>=*pending_end){stage=REG_PENDING;cause="REG_REGISTER_PENDING";goto out;}
  if(idx>=0||q.pid<=0||q.status||q.detail){stage=FR_E_INPUT;cause="REG_REGISTER_STATE";goto out;}
  int seen=fr_pidfd_pid(pass);if(e)e->handle_pid=seen;
  if(seen< -1){stage=REG_FD_TYPE;cause="REG_PIDFD_TYPE";goto out;}
  if(seen!=q.pid){stage=FR_E_HANDLE;cause="REG_PIDFD_IDENTITY";goto out;}
  if(fr_proc(q.pid,&parent,&birth)){stage=FR_E_OWNER_PROC;cause="REG_REGISTER_PROC";goto out;}
  if(e){e->proc_parent=parent;e->proc_birth=birth;e->expected_parent=q.owner;e->expected_birth=q.birth;}
  if(parent!=q.owner){stage=REG_PROC_PARENT;cause="REG_PARENT_START_GENERATION";goto out;}
  if(birth!=q.birth){stage=REG_PROC_BIRTH;cause="REG_PARENT_START_GENERATION";goto out;}
  for(unsigned i=0;i<used;i++)if(records[i].pid==q.pid){stage=REG_DUPLICATE;cause="REG_PIDFD_DUPLICATE";goto out;}
  records[used]=(struct owned){q.pid,pass,0,0,1,(int)q.role,q.birth};pass=-1;used++;started_workers++;*pending=-1;
  if(receiving_case&&!strcmp(control_case,"positive")&&used==4&&root_observe_all3(c)){stage=REG_LIVE;cause="REG_ALL3_LIVE_UNCONFIRMED";goto out;}
  r=send_ack(sock,&q,FR_REGISTER_ACK,c->work_ns);if(r){stage=REG_ACK;error=r;cause="REG_ACK_UNCONFIRMED";}
  if(!r&&whole216_case){
   /* Exact fd119-bound Root selection follows ordinary actual REGISTER_ACK.
    * Only this actual child may join; inherited session START still refuses. */
   struct fr_packet selected;packet(&selected,c,FR_START,0);selected.sequence=1;
   selected.pid=records[used-1].pid;selected.birth=records[used-1].birth;
   selected.owner=records[0].pid;selected.owner_birth=records[0].birth;selected.detail=whole216_case;
   uint64_t selected_send_begin=fr_now();r=controlled_reply(sock,&selected,FR_START,c->work_ns);
   if(r){stage=REG_ACK;error=r;cause="REG_CONTROLLER_START";}
   else {whole216_controller_start_sent=1;struct reg_evidence*start_event=reg_event(8);
    if(start_event){start_event->phase=FR_START;start_event->packet=selected;start_event->peer_pid=getpid();
     start_event->peer_uid=start_event->peer_gid=0;start_event->expected_pid=selected.pid;
     start_event->expected_parent=selected.owner;start_event->expected_birth=selected.birth;start_event->ack_ns=selected_send_begin;}}
  }
 }
 else if(q.type==FR_ABORT){
  if(*pending!=(int)q.role||fr_now()>=*pending_end||q.pid||q.birth||q.status||q.detail<=0||membership(121,0)){stage=FR_E_CREATE_PIPE;cause="REG_ABORT_CREATION_UNKNOWN";goto out;}
  *pending=-1;r=send_ack(sock,&q,FR_ABORT_ACK,c->work_ns);if(r){stage=REG_ACK;error=r;cause="REG_ABORT_ACK_UNCONFIRMED";}
 }
 else if(q.type==FR_REAP){
  if(whole216_case&&!whole216_controller_finished){stage=REG_READY;cause="REG_CONTROLLER_FINISH_NOT_CONFIRMED";goto out;}
  if(idx<0||records[idx].reaped){stage=FR_E_WAIT4;cause="REG_REAP_KERNEL_UNCONFIRMED";goto out;}
  int seen=fr_pidfd_pid(records[idx].pidfd);if(e)e->handle_pid=seen;
  if(q.pid!=records[idx].pid){stage=FR_E_PID;cause="REG_REAP_PID";goto out;}
  if(q.birth!=records[idx].birth){stage=FR_E_BIRTH;cause="REG_REAP_BIRTH";goto out;}
  if(q.detail||q.status<0||q.status>65535||(!WIFEXITED(q.status)&&!WIFSIGNALED(q.status))){if(e)e->status_argument_invalid=1;stage=FR_E_STATUS;cause="REG_REAP_STATUS";goto out;}
  if(seen!=-1){stage=FR_E_WAIT4;cause="REG_REAP_KERNEL_UNCONFIRMED";goto out;}
  r=send_ack(sock,&q,FR_REAP_ACK,c->hard_ns-1000000000ull);if(r){stage=REG_ACK;error=r;cause="REG_REAP_ACK_UNCONFIRMED";}
  else {records[idx].reaped=1;/* Borrowed receipt never grants Root wait4. */records[idx].status=0;records[idx].kernel_status_known=0;}
 }
 else if(q.type==FR_DRAIN){
  if(q.pid||q.birth||q.status||q.detail||*pending>=0||*draining||fr_now()>=c->work_ns){stage=FR_E_INPUT;cause="REG_DRAIN_STATE";goto out;}
  for(unsigned i=1;i<used;i++)if(!records[i].reaped){stage=REG_LIVE;cause="REG_DRAIN_LIVE";goto out;}
  *draining=1;r=send_ack(sock,&q,FR_DRAIN_ACK,c->hard_ns-1000000000ull);if(r){stage=REG_ACK;error=r;cause="REG_DRAIN_ACK";}
 }
 else if(q.type==FR_FINISH){
  if(q.pid||q.birth||q.status||q.detail||*pending>=0||!*draining){stage=REG_FINISH;cause="REG_FINISH_STATE";goto out;}
  for(unsigned i=1;i<used;i++)if(!records[i].reaped){stage=REG_LIVE;cause="REG_FINISH_LIVE";goto out;}
  if((c->mode==FR_EXECUTE||c->mode==FR_BENIGN)&&started_workers!=3){stage=REG_FINISH;cause="REG_EXACT3";goto out;}
  if(c->mode==FR_PREFLIGHT&&started_workers){stage=REG_FINISH;cause="REG_PREFLIGHT_EFFECT";goto out;}
  *finished=1;r=send_ack(sock,&q,FR_FINISH_ACK,c->hard_ns-1000000000ull);if(r){stage=REG_ACK;error=r;cause="REG_FINISH_ACK";}
 }
 if(e&&!cause)e->ack_ns=root_last_ack_begin_ns;
 out:
 if(pass>=0){if(close(pass)){if(e)e->received.close_errno=errno;if(!cause){cause="REG_RIGHT_CLOSE_UNCONFIRMED";stage=FR_E_RIGHT_CLOSE;error=-errno;}}else if(e)e->received.closed++;}
 /* Retained REGISTER rights belong to durable Root storage, never a close
  * count. Private/unacknowledged REAP status is redacted in public evidence. */
 if(e&&e->phase==FR_REAP&&(cause||!e->ack_ns))e->packet.status=0;
 if(cause){reg_failure(e,stage,error,cause);return error;}return 0;
}
#include "A091-STOCK-CALLER.h"
static int write_bounded(int fd,const void*buf,size_t count,uint64_t end){const char*p=buf;fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)|O_NONBLOCK);while(count&&fr_now()<end){ssize_t n=write(fd,p,count);if(n>0){p+=n;count-=n;}else if(n<0&&(errno==EAGAIN||errno==EINTR)){struct pollfd f={fd,POLLOUT,0};poll(&f,1,5);}else return -EIO;}return count?-ETIMEDOUT:0;}
static int raw_transfer(unsigned stream,const uint8_t*body,size_t size,uint64_t end){
 struct raw_custody*r=&raw_custody[stream];struct stat s;
 if(size>1048576||(!body&&size)||fstat(r->fd,&s)||!stock_same_stat(&s,&r->identity)){r->write_errno=ESTALE;return -ESTALE;}
 int flags=fcntl(r->fd,F_GETFL);if(flags<0||fcntl(r->fd,F_SETFL,flags|O_NONBLOCK)){r->write_errno=errno;return -errno;}
 while(r->emitted<size&&fr_now()<end){ssize_t n=write(r->fd,body+r->emitted,size-r->emitted);
  if(n>0)r->emitted+=n;
  else if(n<0&&(errno==EAGAIN||errno==EINTR)){struct pollfd p={r->fd,POLLOUT,0};poll(&p,1,5);}
  else {r->write_errno=n<0?errno:EIO;break;}
 }
 if(r->emitted!=size&&!r->write_errno)r->write_errno=ETIMEDOUT;
 if(fstat(r->fd,&s)){if(!r->write_errno)r->write_errno=errno;}
 else {r->after=s;r->after_known=1;
  /* Writable pipe timestamps can change legitimately. Full before/after
   * identities are emitted, while endpoint ownership uses immutable fields. */
  if(s.st_dev!=r->identity.st_dev||s.st_ino!=r->identity.st_ino||s.st_mode!=r->identity.st_mode||s.st_uid!=r->identity.st_uid||s.st_gid!=r->identity.st_gid||s.st_nlink!=r->identity.st_nlink||s.st_size!=r->identity.st_size){if(!r->write_errno)r->write_errno=ESTALE;}
 }
 if(close(r->fd))r->close_errno=errno;else {r->closed=1;r->close_ns=fr_now();r->fd=-1;}
 return r->write_errno?-r->write_errno:r->close_errno?-r->close_errno:0;
}
static int raw_metadata(const uint8_t*const buffers[2],const size_t counts[2],const uint64_t seen[2],const int eof[2],const int overflow[2],const int read_error[2],const int close_error[2],uint64_t end){
 if(!raw_transport_selected)return 0;
 const char*head=",\"native_inner_streams\":{\"schema\":\"friday.a118.native-raw-transport.v1\",\"streams\":[";
 if(write_bounded(1,head,strlen(head),end))return -EIO;
 for(unsigned i=0;i<2;i++){struct raw_custody*r=&raw_custody[i];struct fr_sha h;uint8_t digest[32];char hex[65],id[256],after_id[256],prefix[129];fr_sha_init(&h);if(buffers[i])fr_sha_update(&h,buffers[i],counts[i]);fr_sha_end(&h,digest);stock_hex(digest,hex);
  size_t prefix_size=counts[i]<64?counts[i]:64;for(size_t j=0;j<prefix_size;j++)snprintf(prefix+2*j,3,"%02x",buffers[i][j]);prefix[2*prefix_size]=0;
  int ni=stock_stat_json(id,sizeof(id),&r->identity);if(ni<=0||(size_t)ni>=sizeof(id))return -EFBIG;
  if(r->after_known){ni=stock_stat_json(after_id,sizeof(after_id),&r->after);if(ni<=0||(size_t)ni>=sizeof(after_id))return -EFBIG;}else strcpy(after_id,"null");
  char b[1536];int n=snprintf(b,sizeof(b),"%s{\"stream\":%u,\"cap\":1048576,\"retained_size\":%zu,\"total_seen\":%llu,\"eof\":%s,\"overflow\":%s,\"read_errno\":%d,\"read_close_errno\":%d,\"sha256\":\"%s\",\"prefix_hex\":\"%s\",\"writer_identity9\":%s,\"writer_identity9_after\":%s,\"emitted_bytes\":%zu,\"write_errno\":%d,\"writer_closed\":%s,\"close_errno\":%d,\"close_ns\":%llu}",i?",":"",i,counts[i],(unsigned long long)seen[i],eof[i]?"true":"false",overflow[i]?"true":"false",read_error[i],close_error[i],hex,prefix,id,after_id,r->emitted,r->write_errno,r->closed?"true":"false",r->close_errno,(unsigned long long)r->close_ns);
  if(n<=0||(size_t)n>=sizeof(b)||write_bounded(1,b,n,end))return -EIO;
 }
 return write_bounded(1,"]}",2,end);
}
static int receiving_terminal(uint64_t end){
 if(!receiving_case)return 0;
 char b[2048];int n=snprintf(b,sizeof(b),",\"root_receiving\":{\"schema\":\"friday.a091.actual-root-receipt.v1\",\"case\":\"%s\",\"failure_ns\":%llu,\"cleanup_grace_end\":%llu,\"transport_close_attempts\":%u,\"transport_close_errno\":%d,\"transport_close_ns\":%llu,\"transport_retained\":%s,\"event_count\":%u,\"events\":[",control_case,(unsigned long long)root_failure_ns,(unsigned long long)root_cleanup_grace_end,root_transport_close_attempts,root_transport_close_errno,(unsigned long long)root_transport_close_ns,root_transport_retained?"true":"false",reg_event_count);
 if(n<=0||(size_t)n>=sizeof(b)||write_bounded(1,b,n,end))return -EIO;
 for(unsigned i=0;i<reg_event_count;i++){
  struct reg_evidence*e=&reg_events[i];char hex[193];const unsigned char*p=(const void*)&e->packet;for(unsigned j=0;j<96;j++)snprintf(hex+2*j,3,"%02x",p[j]);
  n=snprintf(b,sizeof(b),"%s{\"kind\":%u,\"phase\":%u,\"stage\":%u,\"primitive_errno\":%d,\"peer\":[%d,%d,%d],\"expected_peer\":[%d,%d,%d],\"proc_parent\":%d,\"expected_parent\":%d,\"proc_birth\":%llu,\"expected_birth\":%llu,\"handle_pid\":%d,\"pending_role\":%d,\"at_ns\":%llu,\"pending_end\":%llu,\"ack_ns\":%llu,\"credentials\":%u,\"rights\":%u,\"rights_closed\":%u,\"rights_close_errno\":%d,\"receive_bytes\":%d,\"receive_flags\":%u,\"packet_hex\":\"%s\",\"status_redacted\":%s,\"status_argument_invalid\":%s}",i?",":"",e->kind,e->phase,e->stage,e->error,e->peer_pid,e->peer_uid,e->peer_gid,e->expected_pid,e->expected_uid,e->expected_gid,e->proc_parent,e->expected_parent,(unsigned long long)e->proc_birth,(unsigned long long)e->expected_birth,e->handle_pid,e->pending_role,(unsigned long long)e->at_ns,(unsigned long long)e->pending_end,(unsigned long long)e->ack_ns,e->received.credentials,e->received.rights,e->received.closed,e->received.close_errno,e->received.bytes,e->received.flags,hex,e->phase==FR_REAP&&!e->ack_ns?"true":"false",e->status_argument_invalid?"true":"false");
  if(n<=0||(size_t)n>=sizeof(b)||write_bounded(1,b,n,end))return -EIO;
 }
 if(write_bounded(1,"],\"all3_live\":",14,end))return -EIO;
 if(root_all3_ns){
  char image_id[256];int ni=stock_stat_json(image_id,sizeof(image_id),&root_all3_image);if(ni<=0||(size_t)ni>=sizeof(image_id))return -EFBIG;
  n=snprintf(b,sizeof(b),"{\"observed_ns\":%llu,\"inner_processes\":4,\"same_actual_image_and_argv\":true,\"image_identity9\":%s,\"image_sha256\":\"%s\",\"argv_sha256\":\"%s\",\"argv_bytes\":%zu,\"members\":[",(unsigned long long)root_all3_ns,image_id,root_all3_image_sha,root_all3_argv_sha,root_all3_argv_size);
  if(n<=0||(size_t)n>=sizeof(b)||write_bounded(1,b,n,end))return -EIO;
  for(unsigned i=0;i<4;i++){struct root_live_member*m=&root_all3[i];char id[256];int ni=stock_stat_json(id,sizeof(id),&m->handle);if(ni<=0||(size_t)ni>=sizeof(id))return -EFBIG;
   n=snprintf(b,sizeof(b),"%s{\"slot\":%u,\"role\":%d,\"pid\":%d,\"parent\":%d,\"birth\":%llu,\"handle_pid\":%d,\"pidfd_identity9\":%s}",i?",":"",i,m->role,m->pid,m->parent,(unsigned long long)m->birth,m->handle_pid,id);
   if(n<=0||(size_t)n>=sizeof(b)||write_bounded(1,b,n,end))return -EIO;
  }
  if(write_bounded(1,"]}",2,end))return -EIO;
 }else if(write_bounded(1,"null",4,end))return -EIO;
 if(write_bounded(1,",\"owned\":[",10,end))return -EIO;
 for(unsigned i=0;i<used;i++){
  struct owned*r=&records[i];n=snprintf(b,sizeof(b),"%s{\"index\":%u,\"pid\":%d,\"birth\":%llu,\"role\":%d,\"reaped\":%s,\"kernel_status_known\":%s,\"status\":%d,\"signal_attempts\":%u,\"signal_errno\":%d,\"signal_ns\":%llu,\"wait4_ns\":%llu,\"close_attempts\":%u,\"close_errno\":%d,\"close_ns\":%llu,\"handle_retained\":%s}",i?",":"",i,r->pid,(unsigned long long)r->birth,r->role,r->reaped?"true":"false",r->kernel_status_known?"true":"false",r->kernel_status_known?r->status:0,root_signal_attempts[i],root_signal_errno[i],(unsigned long long)root_signal_ns[i],(unsigned long long)root_wait_ns[i],root_close_attempts[i],root_close_errno[i],(unsigned long long)root_close_ns[i],r->pidfd>=0?"true":"false");
  if(n<=0||(size_t)n>=sizeof(b)||write_bounded(1,b,n,end))return -EIO;
 }
 return write_bounded(1,"]}",2,end);
}
static int run(const struct fr_cap*c,const char*pin,int python){int out[2],err[2],reg[2],barrier[2];if(pipe2(out,O_CLOEXEC)||pipe2(err,O_CLOEXEC)||socketpair(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0,reg)||pipe2(barrier,O_CLOEXEC))return -errno;
 int on=1;if(setsockopt(reg[0],SOL_SOCKET,SO_PASSCRED,&on,sizeof(on))||setsockopt(reg[1],SOL_SOCKET,SO_PASSCRED,&on,sizeof(on)))return -errno;
 int pidfd=-1;struct clone_args a={0};a.flags=CLONE_PIDFD|CLONE_INTO_CGROUP;a.pidfd=(uintptr_t)&pidfd;a.cgroup=121;a.exit_signal=SIGCHLD;pid_t pid=syscall(SYS_clone3,&a,sizeof(a));
 if(pid>0){records[0].pid=pid;records[0].pidfd=pidfd;records[0].registered=1;records[0].role=-1;}
 if(pid<0)return -errno;
 if(pid==0){close(out[0]);close(err[0]);close(reg[0]);close(barrier[1]);struct pollfd p={barrier[0],POLLIN,0};int release=0;uint64_t end=fr_now()+15000000000ull;if(end>c->work_ns)end=c->work_ns;while(fr_now()<end){int n=poll(&p,1,10);if(n>0){unsigned char b=0;release=read(barrier[0],&b,1)==1&&b==0xa5;break;}if(n<0&&errno!=EINTR)break;}close(barrier[0]);if(!release)_exit(124);
  unsigned actual_uid=c->uid,actual_gid=c->gid;
  if(receiving_case&&!strcmp(control_case,"peer_uid"))actual_uid=1001;
  if(receiving_case&&!strcmp(control_case,"peer_gid"))actual_gid=1001;
  if(fr_limits(FR_COORD,(c->mode==FR_CONTROLS||c->mode==FR_BENIGN)?180:1200)||drop_credentials(actual_uid,actual_gid))_exit(125);
  int src[]={out[1],err[1],reg[1],python},dst[]={1,2,123,130},tmp[4];for(int i=0;i<4;i++){tmp[i]=fcntl(src[i],F_DUPFD_CLOEXEC,400);if(tmp[i]<0)_exit(125);}for(int i=0;i<4;i++){if(dup3(tmp[i],dst[i],0)<0)_exit(125);close(tmp[i]);}
  for(int fd=0;fd<512;fd++){int keep=fd==1||fd==2||fd==100||fd==111||fd==120||fd==121||fd==123||fd==130;for(unsigned i=0;i<FR_SOURCES;i++)if(fd==fr_source_fd[i])keep=1;if(keep){if(fcntl(fd,F_SETFD,0)<0)_exit(125);}else close(fd);}
  if(receiving_case&&!whole216_case&&strcmp(control_case,"inherited_start_owner"))_exit(stock_caller(123,c)?76:0);
  /* Root-controlled cgroup descriptors are O_PATH, never writable attach fds. */
  char*argv[]={"/usr/bin/python3.14","-I","-S","-B","-c","import os,sys,types;d=os.pread(103,1048577,0);m=types.ModuleType('a061_supervisor');m.__file__='/var/tmp/friday-astra-browser-a099-whole-source-closure-a104-g1/source/A071-SUPERVISOR.py';sys.modules[m.__name__]=m;exec(compile(d,m.__file__,'exec'),m.__dict__);sys.exit(m.main())",(char*)pin,0};char*env[]={"PATH=/usr/bin:/bin","LANG=C","LC_ALL=C",0};fexecve(130,argv,env);_exit(125);
 }
 close(out[1]);close(err[1]);close(reg[1]);close(barrier[0]);int parent;if(fr_proc(pid,&parent,&records[0].birth)||parent!=getpid()||pidfd<0||fr_pidfd_pid(pidfd)!=pid)fail("COORD_CLONE_IDENTITY",1);
 registry_origin(records[0].pid,c->uid,c->gid,getpid(),records[0].birth);
 /* Public negative constructor arguments. The actual direct-owned actor,
  * its parent/birth receipt, FR_START and native storage stay genuine. */
 if(receiving_case&&!strcmp(control_case,"origin_parent_argument"))registry_origin(records[0].pid,c->uid,c->gid,getpid()+1,records[0].birth);
 if(receiving_case&&!strcmp(control_case,"origin_birth_argument"))registry_origin(records[0].pid,c->uid,c->gid,getpid(),records[0].birth+1);
 struct fr_packet start;packet(&start,c,FR_START,0);start.pid=pid;start.birth=records[0].birth;start.owner=getpid();fr_proc(getpid(),&parent,&start.owner_birth);if(!failure&&controlled_reply(reg[0],&start,FR_START,c->work_ns)<0)fail("COORD_START_PACKET",1);unsigned char release=0xa5;if(!failure&&write(barrier[1],&release,1)!=1)fail("COORD_START_BARRIER",1);close(barrier[1]);
 if(receiving_case&&!strcmp(control_case,"Root_signal_denied")&&restrict_own_root(1,-1))fail("ROOT_SIGNAL_RESTRICTION_INPUT",1);
 if(receiving_case&&!strcmp(control_case,"Root_transport_close_denied")&&restrict_own_root(2,reg[0]))fail("ROOT_CLOSE_RESTRICTION_INPUT",1);
 fcntl(out[0],F_SETFL,O_NONBLOCK);fcntl(err[0],F_SETFL,O_NONBLOCK);
 uint8_t*buffers[2]={calloc(1,1048577),calloc(1,1048577)};size_t counts[2]={0,0};uint64_t seen[2]={0,0};int eof[2]={0,0},overflow[2]={0,0},read_error[2]={0,0},close_error[2]={0,0};int pipes[2]={out[0],err[0]},openpipes=2,pending=-1,finished=0,draining=0,stopped=0;uint64_t pending_end=0,peak=0,current_outer=0,current_inner=0;
 if(!buffers[0]||!buffers[1])fail("OUTER_ALLOCATION",1);
 while(fr_now()<c->hard_ns-2000000000ull){
  if(cancelled)fail("OWNER_STOP",0);if(fr_now()>=c->work_ns&&!draining)fail("WORK1140_TIMEOUT",0);if(pending>=0&&fr_now()>=pending_end&&!failure){struct reg_evidence*e=reg_event(2);if(e){e->phase=FR_REGISTER;e->pending_role=pending;e->pending_end=pending_end;}reg_failure(e,REG_PENDING,-ETIMEDOUT,"REG_PENDING_CREATION_UNKNOWN");}
  if(membership(121,pending>=0))fail("UNKNOWN_CGROUP_MEMBER",1);
  if(group_check(120,c->outer_dev,c->outer_ino,FR_OUTER,1,0)||group_check(121,c->inner_dev,c->inner_ino,FR_INNER,4,0)||value_at(120,"memory.current",&current_outer)||value_at(121,"memory.current",&current_inner)||current_outer+current_inner>FR_RSS)fail("KERNEL_MEMORY_ENVELOPE",1);
  int ok=1;uint64_t rss=rss_pid(getpid(),&ok);for(unsigned i=0;i<used;i++)if(!records[i].reaped){int seen=1;uint64_t value=rss_pid(records[i].pid,&seen);if(!seen&&records[i].pidfd>=0&&fr_pidfd_pid(records[i].pidfd)==-1)seen=1;rss+=value;if(!seen)ok=0;}if(!ok)fail("RAW_RSS_UNKNOWN",1);if(rss>peak)peak=rss;if(rss>FR_RSS)fail("AGGREGATE_RAW_RSS_CAP",1);
  if(failure&&!stopped){
   /* Root receiving refusal first releases only its write-side transport.
    * The actual caller may dispose its private unregistered clone. Root
    * never adopts a rejected packet PID as cleanup authority. */
   if(receiving_case&&root_failure_ns){if(!root_cleanup_grace_end){root_cleanup_grace_end=fr_now()+2000000000ull;if(root_cleanup_grace_end>c->hard_ns-3000000000ull)root_cleanup_grace_end=c->hard_ns-3000000000ull;shutdown(reg[0],SHUT_WR);}
    if(fr_now()>=root_cleanup_grace_end){stop_known();stopped=1;}
   }else {stop_known();stopped=1;}
  }
  struct pollfd f[3]={{reg[0],POLLIN,0},{pipes[0],POLLIN,0},{pipes[1],POLLIN,0}};int polled=poll(f,3,5);if(polled<0&&errno!=EINTR)fail("OUTER_POLL",1);
  if(!failure&&!finished&&(f[0].revents&POLLIN)){if(registry(reg[0],c,&pending,&pending_end,&finished,&draining)&&!failure)fail("REG_TRANSPORT",1);}
  else if(receiving_case&&!failure&&!finished&&(f[0].revents&(POLLHUP|POLLERR|POLLNVAL))){struct reg_evidence*e=reg_event(3);if(e)e->received.flags=f[0].revents;reg_failure(e,FR_E_TRANSPORT_CLOSE,-EPIPE,"REG_TRANSPORT_CLOSED");}
  for(int i=0;i<2;i++)if(pipes[i]>=0&&(f[i+1].revents&(POLLIN|POLLHUP))){uint8_t b[65536];ssize_t n=read(pipes[i],b,sizeof(b));if(n>0){seen[i]+=n;size_t keep=1048576-counts[i];if(keep>(size_t)n)keep=n;if(buffers[i]){memcpy(buffers[i]+counts[i],b,keep);counts[i]+=keep;}if(seen[i]>1048576){overflow[i]=1;fail("OUTER_PIPE_CAP",0);}}else if(n==0){eof[i]=1;if(close(pipes[i])){close_error[i]=errno;fail("OWNED_PIPE_CLOSE_UNCONFIRMED",1);}pipes[i]=-1;openpipes--;}else if(errno!=EAGAIN&&errno!=EINTR){read_error[i]=errno;fail("OUTER_DRAIN_UNCONFIRMED",1);}}
  reap_owned();int all=1;for(unsigned i=0;i<used;i++)if(!records[i].reaped)all=0;
  if(records[0].reaped&&(!WIFEXITED(records[0].status)||WEXITSTATUS(records[0].status)))fail("COORD_EXIT",0);
  if(all&&!openpipes)break;
  /* A dead worker handle is not a wait receipt. It only permits ending this
   * rejected own contour without waiting out the work budget; the terminal
   * unreaped check below still makes uncertainty sticky and grants no status. */
  if(records[0].reaped&&!openpipes){int live=0;for(unsigned i=1;i<used;i++)if(!records[i].reaped&&(records[i].pidfd<0||fr_pidfd_pid(records[i].pidfd)!=-1))live=1;if(!live)break;}
 }
 if(!failure){if(!finished)fail("MISSING_FINISH_REGISTRATION",1);if(pending>=0)fail("REG_PENDING_TERMINAL",1);for(unsigned i=0;i<used;i++)if(!records[i].reaped)fail("OWNED_REAP_UNCONFIRMED",1);if(openpipes)fail("OUTER_DRAIN_UNCONFIRMED",1);if(counts[1])fail("INNER_STDERR",0);int ps[16];if(list_members(121,ps)!=0)fail("INNER_CGROUP_NOT_EMPTY",1);if(!counts[0]||buffers[0][counts[0]-1]!='\n'||memchr(buffers[0],'\n',counts[0]-1))fail("PARTIAL_OR_MULTIPLE_TERMINAL",0);}
 if(failure)stop_known();uint64_t reap_end=fr_now()+500000000ull;if(reap_end>c->hard_ns-1500000000ull)reap_end=c->hard_ns-1500000000ull;while(fr_now()<reap_end){reap_owned();int complete=1;for(unsigned i=0;i<used;i++)if(!records[i].reaped)complete=0;if(complete)break;poll(0,0,5);}reap_owned();for(unsigned i=0;i<used;i++)if(!records[i].reaped)fail("OWNED_REAP_UNCONFIRMED",1);for(int i=0;i<2;i++)if(pipes[i]>=0){if(close(pipes[i])){close_error[i]=errno;fail("OWNED_PIPE_CLOSE_UNCONFIRMED",1);}pipes[i]=-1;}
 root_transport_close_attempts++;if(close(reg[0])){root_transport_close_errno=errno;root_transport_retained=1;fail("REG_CLOSE_UNCONFIRMED",1);}else root_transport_close_ns=fr_now();
 struct reg_evidence*close_event=reg_event(7);if(close_event){close_event->stage=FR_E_TRANSPORT_CLOSE;close_event->error=-root_transport_close_errno;}
 for(unsigned i=0;i<used;i++)if(records[i].pidfd>=0){int fd=records[i].pidfd;root_close_attempts[i]++;if(close(fd)){root_close_errno[i]=errno;fail("OWNED_PIDFD_CLOSE_UNCONFIRMED",1);}else {records[i].pidfd=-1;root_close_ns[i]=fr_now();}struct reg_evidence*e=reg_event(6);if(e){e->phase=i;e->stage=FR_E_HANDLE_CLOSE;e->error=-root_close_errno[i];}}
 if(pending>=0)fail("REG_PENDING_TERMINAL",1);
 /* Full bounded bytes are sent through separate actual stock output pipes,
  * including refusal. Hex expansion cannot consume the original terminal cap. */
 if(raw_transport_selected)for(unsigned i=0;i<2;i++)if(raw_transfer(i,buffers[i],counts[i],c->hard_ns-1000000000ull))fail("INNER_RAW_TRANSPORT_UNCONFIRMED",1);
 uint8_t sha[32];struct fr_sha hash;fr_sha_init(&hash);if(buffers[0])fr_sha_update(&hash,buffers[0],counts[0]);fr_sha_end(&hash,sha);char hex[65];for(unsigned i=0;i<32;i++)snprintf(hex+2*i,3,"%02x",sha[i]);
 unsigned reaped_workers=0;for(unsigned i=1;i<used;i++)if(records[i].reaped)reaped_workers++;char head[2048];struct rusage raw;getrusage(RUSAGE_SELF,&raw);int n=snprintf(head,sizeof(head),"{\"state\":\"%s\",\"reason\":%s%s%s,\"body_complete\":false,\"acceptance_complete\":false,\"terminal_completion\":%s,\"uncertainty_sticky\":%s,\"registered_workers\":%u,\"reaped_workers\":%u,\"aggregate_raw_RSS_peak_bytes\":%llu,\"raw_self_peak_KiB\":%ld,\"outer_memory_current\":%llu,\"inner_memory_current\":%llu,\"inner_terminal_sha256\":\"%s\",\"inner_terminal\":",
  sticky?"STOP_UNCONFIRMED":failure?"OUTER_FAILED":"OUTER_BOUNDED_DRAINED_FINISHED",failure?"\"":"",failure?failure:"null",failure?"\"":"",failure?"false":"true",sticky?"true":"false",started_workers,reaped_workers,(unsigned long long)peak,raw.ru_maxrss,(unsigned long long)current_outer,(unsigned long long)current_inner,hex);
 int emitted=n>0&&(size_t)n<sizeof(head)&&!write_bounded(1,head,n,c->hard_ns-1000000000ull);
 if(emitted)emitted=!write_bounded(1,failure||raw_transport_selected?"null":(char*)buffers[0],failure||raw_transport_selected?4:counts[0]-1,c->hard_ns-1000000000ull);
 /* Mode1 bounded raw DATA is evidence only. It grants neither a native wait
  * receipt nor terminal success; failure/sticky remain unchanged. Hex framing
  * keeps the Root terminal valid even when the rejected inner JSON is invalid. */
 char observation[256];int no=snprintf(observation,sizeof(observation),",\"registry_next_sequence\":%u,\"coordinator_kernel_status_known\":%s,\"borrowed_status_kernel_credit\":false,\"native_inner_DATA_hex\":",registry_sequence,records[0].kernel_status_known?"true":"false");
 if(emitted)emitted=no>0&&(size_t)no<sizeof(observation)&&!write_bounded(1,observation,no,c->hard_ns-1000000000ull);
 if(emitted&&!raw_transport_selected&&c->mode==FR_CONTROLS&&buffers[0]&&counts[0]&&counts[0]<=16384){
  static const char digits[]="0123456789abcdef";char encoded[32768];for(size_t i=0;i<counts[0];i++){encoded[2*i]=digits[buffers[0][i]>>4];encoded[2*i+1]=digits[buffers[0][i]&15];}
  emitted=!write_bounded(1,"\"",1,c->hard_ns-1000000000ull)&&!write_bounded(1,encoded,2*counts[0],c->hard_ns-1000000000ull)&&!write_bounded(1,"\"",1,c->hard_ns-1000000000ull);
 }else if(emitted)emitted=!write_bounded(1,"null",4,c->hard_ns-1000000000ull);
 if(emitted)emitted=!raw_metadata((const uint8_t*const*)buffers,counts,seen,eof,overflow,read_error,close_error,c->hard_ns-1000000000ull);
 if(emitted&&control_case[0]){char controls[1536];int own_parent=0;uint64_t own_birth=0;int generation=fr_proc(getpid(),&own_parent,&own_birth);int nc=snprintf(controls,sizeof(controls),",\"public_control\":{\"case\":\"%s\",\"reply_used\":%d,\"reply_type\":%u,\"reply_pid\":%d,\"reply_uid\":%d,\"reply_gid\":%d,\"reply_rights\":%u,\"reply_ns\":%llu,\"helper_created\":%d,\"helper_reaped\":%d,\"helper_closed\":%d,\"plaintext_created\":%d,\"plaintext_closed\":%d,\"credential_transitions\":%d,\"origin_pid\":%d,\"origin_parent\":%d,\"origin_birth\":%llu,\"coordinator_pid\":%d,\"coordinator_birth\":%llu,\"pending_role\":%d}",control_case,control_reply_used,control_ack_type,control_reply_pid,control_reply_uid,control_reply_gid,control_reply_rights,(unsigned long long)control_reply_ns,control_helper_created,control_helper_reaped,control_helper_closed,control_plain_created,control_plain_closed,control_credential_transitions,getpid(),generation?-1:own_parent,(unsigned long long)(generation?0:own_birth),records[0].pid,(unsigned long long)records[0].birth,pending);emitted=nc>0&&(size_t)nc<sizeof(controls)&&!write_bounded(1,controls,nc,c->hard_ns-1000000000ull);}
 if(emitted)emitted=!receiving_terminal(c->hard_ns-1000000000ull);
 if(emitted)emitted=!write_bounded(1,"}\n",2,c->hard_ns-1000000000ull);
 free(buffers[0]);free(buffers[1]);return emitted&&!failure?0:-EIO;
}
static int pre_refused(const char*stage,int detail){
 struct rusage self,children;struct rlimit as,cpu,files,descriptors,core;
 int measured=!getrusage(RUSAGE_SELF,&self)&&!getrusage(RUSAGE_CHILDREN,&children)&&
  !getrlimit(RLIMIT_AS,&as)&&!getrlimit(RLIMIT_CPU,&cpu)&&!getrlimit(RLIMIT_FSIZE,&files)&&
  !getrlimit(RLIMIT_NOFILE,&descriptors)&&!getrlimit(RLIMIT_CORE,&core);
 uint64_t outer=0,inner=0;int memory=!value_at(120,"memory.current",&outer)&&!value_at(121,"memory.current",&inner);
 char resources[512];int nr=0;
 if(measured)nr=snprintf(resources,sizeof(resources),"{\"raw_self_peak_bytes\":%llu,\"raw_children_peak_bytes\":%llu,\"AS\":[%llu,%llu],\"CPU\":[%llu,%llu],\"FSIZE\":[%llu,%llu],\"NOFILE\":[%llu,%llu],\"CORE\":[%llu,%llu]}",
  (unsigned long long)self.ru_maxrss*1024ull,(unsigned long long)children.ru_maxrss*1024ull,
  (unsigned long long)as.rlim_cur,(unsigned long long)as.rlim_max,(unsigned long long)cpu.rlim_cur,(unsigned long long)cpu.rlim_max,
  (unsigned long long)files.rlim_cur,(unsigned long long)files.rlim_max,(unsigned long long)descriptors.rlim_cur,(unsigned long long)descriptors.rlim_max,
  (unsigned long long)core.rlim_cur,(unsigned long long)core.rlim_max);
 char kernel[128];int nk=memory?snprintf(kernel,sizeof(kernel),"{\"outer_current\":%llu,\"inner_current\":%llu}",(unsigned long long)outer,(unsigned long long)inner):0;
 char b[1536];int n=snprintf(b,sizeof(b),"{\"state\":\"REFUSED\",\"reason\":\"%s\",\"primitive_errno\":%d,\"primitive_stage\":\"%s\",\"terminal_completion\":false,\"body_complete\":false,\"acceptance_complete\":false,\"preinterpreter_rejection\":true,\"worker_effects\":0,\"registered_workers\":%u,\"coordinator_created\":%s,\"reap_status\":null,\"resources\":%s,\"kernel_memory\":%s}\n",
  stage,detail,fr_stage_get(),started_workers,records[0].pid>0?"true":"false",
  nr>0&&(size_t)nr<sizeof(resources)?resources:"null",nk>0&&(size_t)nk<sizeof(kernel)?kernel:"null");
 struct stat st;if(n>0&&(size_t)n<sizeof(b)&&!fstat(1,&st)&&S_ISFIFO(st.st_mode))write_bounded(1,b,n,fr_now()+1000000000ull);return 77;
}
int main(int argc,char**argv){
 /* No interpreter exists before this actual kernel envelope and source/view
  * validation. Missing admission descriptors never fall back to host paths. */
 if(geteuid()!=0||getuid()!=0||getgid()!=0)return pre_refused("ROOT_INVOKER",-EPERM);
 int r=fr_limits(FR_OUTER,1200);if(r)return pre_refused("NATIVE_PREINTERPRETER_LIMITS",r);
 struct rusage raw_self,raw_children;if(getrusage(RUSAGE_SELF,&raw_self)||getrusage(RUSAGE_CHILDREN,&raw_children)||raw_self.ru_maxrss*1024ull>FR_OUTER||raw_children.ru_maxrss*1024ull>FR_RSS)return pre_refused("RAW_HISTORICAL_RSS",-ENOMEM);
 if(argc!=3||strcmp(argv[1],"--held-a061"))return pre_refused("PUBLIC_ARGUMENTS",-EINVAL);
 struct fr_cap c;r=fr_cap_read(100,argv[2],&c);if(r)return pre_refused("ROOT_CAPSULE",r);
 r=fr_limits(FR_OUTER,(c.mode==FR_CONTROLS||c.mode==FR_BENIGN)?180:1200);if(r)return pre_refused("NATIVE_OUTER_MODE_LIMITS",r);
 r=sources(&c);if(r)return pre_refused("HELD_SOURCES",r);
 r=os_check(&c);if(r)return pre_refused("AUTHENTIC_OS_VALUES",r);
 if(prctl(PR_SET_CHILD_SUBREAPER,1,0,0,0))return pre_refused("KERNEL_SUBREAPER",-errno);
 struct sigaction sa={0};sa.sa_handler=owner_stop;sigemptyset(&sa.sa_mask);if(sigaction(SIGTERM,&sa,0)||sigaction(SIGINT,&sa,0))return pre_refused("OWNER_SIGNAL_SETUP",-errno);
 struct sigaction pipe_action={0};pipe_action.sa_handler=SIG_IGN;sigemptyset(&pipe_action.sa_mask);if(sigaction(SIGPIPE,&pipe_action,0))return pre_refused("RAW_PIPE_SIGNAL_SETUP",-errno);
 struct stat st;if(fstat(1,&st)||!S_ISFIFO(st.st_mode)||fstat(122,&st)||!S_ISDIR(st.st_mode)||st.st_uid||st.st_gid||st.st_dev!=c.root_dev||st.st_ino!=c.root_ino)return pre_refused("PROTECTED_ROOT_HANDLE",-EPERM);
 r=group_check(120,c.outer_dev,c.outer_ino,FR_OUTER,1,0);if(!r)r=group_check(121,c.inner_dev,c.inner_ino,FR_INNER,4,1);if(r)return pre_refused("KERNEL_RESOURCE_ENVELOPE",r);
 int members[16];if(list_members(120,members)!=1||members[0]!=getpid())return pre_refused("OUTER_KERNEL_MEMBERSHIP",-EPERM);
 r=fr_image_verify(111,122,c.image_sha,c.manifest_sha,c.work_ns);if(r)return pre_refused("EXACT_RUNTIME_IMAGE",r);
 int python=fr_open_beneath(122,"/usr/bin/python3.14",O_RDONLY);if(python<0||fr_sealed(python,1))return pre_refused("HELD_INTERPRETER",python<0?python:-EPERM);
 r=control_input(&c);if(r)return pre_refused("PUBLIC_CONTROL_INPUT",r);
 r=raw_transport_bind();if(r||receiving_case&&!raw_transport_selected)return pre_refused("RAW_OUTPUT_TRANSPORT",r?r:-EBADF);
 /* chroot is scoped to independently supplied, verified protected runtime.
  * No namespace, cgroup, mount, image or authority is created by this launcher. */
 if(fchdir(122)||chroot(".")||chdir("/"))return pre_refused("KERNEL_RUNTIME_ROOT",-errno);
 return run(&c,argv[2],python)?2:0;
}
