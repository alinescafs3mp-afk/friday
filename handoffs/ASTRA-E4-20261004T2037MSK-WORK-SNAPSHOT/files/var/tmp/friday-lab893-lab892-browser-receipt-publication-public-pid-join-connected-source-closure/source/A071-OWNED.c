#include "A071-NATIVE.h"
#include <linux/sched.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/audit.h>
#include <sys/syscall.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/poll.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <string.h>
#include <stdio.h>
/* Resolved only from the exact held CPython process in the approved runtime.
 * The static outer does not link this translation unit or these symbols. */
extern void PyOS_BeforeFork(void);
extern void PyOS_AfterFork_Parent(void);
extern void PyOS_AfterFork_Child(void);
/* Creation poison belongs to this exact native owner/generation. It has no
 * reset/export API and cannot be cleared by Python or an inherited child.
 * Cleanup may still consume a known record; it never creates another PID. */
static pid_t native_owner;
static int native_creation_poisoned;
static struct fr_cap session_cap;
static pid_t session_origin;
static uint64_t session_owner_birth,session_origin_birth;
static uint32_t session_sequence=2;
static int session_ready;
static struct stat session_socket_stat;
static struct fr_evidence session_evidence;
static struct fr_evidence *active_evidence=&session_evidence;
static int evidence_error(unsigned stage,int result){if(!active_evidence->stage){active_evidence->stage=stage;active_evidence->primitive_errno=result;}return result;}
static int creation_owner(void){
 if(!session_ready||native_owner!=getpid())return -EPERM;
 if(native_creation_poisoned)return -EUCLEAN;
 native_owner=getpid();return 0;
}
static void poison(struct fr_child*rec){native_creation_poisoned=1;if(rec){rec->state=FR_UNKNOWN;rec->status=0;}}
struct owned_binding {
 struct fr_child*public_copy;struct fr_child actual;pid_t owner;
 int wait_observed,kernel_status,cleanup_reaped,handle_closed,stop_attempted;
 struct stat handle;
 struct fr_evidence evidence;
};
static struct owned_binding owned_bindings[515];
static unsigned owned_binding_count;
/* Separate immutable delegation, not a reset of native_owner/session_ready.
 * It permits this one normal producer and FINISH, not inherited fr_own_* or
 * arbitrary child creation. The original coordinator remains the only waiter. */
static unsigned controller_prepared;
static int controller_actual_fork_child;
static uint64_t controller_actual_birth;
struct controller_context {
 int attempted,ready,finished,poisoned;
 pid_t owner,parent,origin;uint64_t birth,parent_birth,origin_birth;
 unsigned selector;struct fr_cap cap;struct stat socket;
 struct fr_evidence start,finish;
};
static struct controller_context controller_context;
static void poison_generation(void){
 native_creation_poisoned=1;
 for(unsigned i=0;i<owned_binding_count;i++)if(owned_bindings[i].owner==getpid()&&owned_bindings[i].actual.state!=FR_REAPED){
  if(!owned_bindings[i].evidence.stage){owned_bindings[i].evidence.stage=active_evidence->stage;owned_bindings[i].evidence.primitive_errno=active_evidence->primitive_errno;}
  poison(&owned_bindings[i].actual);
 }
}
static struct owned_binding *binding_find(struct fr_child*copy){
 for(unsigned i=0;i<owned_binding_count;i++)if(owned_bindings[i].public_copy==copy&&owned_bindings[i].owner==getpid())return &owned_bindings[i];
 return 0;
}
static struct owned_binding *binding_new(struct fr_child*copy,unsigned role){
 struct owned_binding *old=binding_find(copy);
 if(old){if(old->actual.pid||old->actual.state!=FR_EMPTY)return 0;memset(&old->actual,0,sizeof(old->actual));old->actual.pidfd=-1;old->actual.role=role;return old;}
 if(owned_binding_count>=515)return 0;
 struct owned_binding *entry=&owned_bindings[owned_binding_count++];memset(entry,0,sizeof(*entry));
 entry->public_copy=copy;entry->owner=getpid();entry->actual.pidfd=-1;entry->actual.role=role;return entry;
}
static struct fr_child *binding_actual(struct fr_child*copy){
 struct owned_binding *entry=binding_find(copy);
 if(!entry||memcmp(copy,&entry->actual,sizeof(*copy))){if(entry){active_evidence=&entry->evidence;evidence_error(FR_E_COPY,-EPERM);active_evidence=&session_evidence;}poison(entry?&entry->actual:0);return 0;}
 return &entry->actual;
}
static struct owned_binding *binding_record(struct fr_child*actual){
 for(unsigned i=0;i<owned_binding_count;i++)if(&owned_bindings[i].actual==actual&&owned_bindings[i].owner==getpid())return &owned_bindings[i];
 return 0;
}
static int publish_fixture(struct fr_child*copy,struct fr_child*actual,int result){*copy=*actual;active_evidence=&session_evidence;return result;}
static int cap_get(struct fr_cap*c){struct stat s;int parent;uint64_t birth;
 if(!session_ready||native_owner!=getpid()||fr_sealed(100,1)||fstat(100,&s)||s.st_size!=sizeof(*c)||pread(100,c,sizeof(*c),0)!=sizeof(*c)||memcmp(c,&session_cap,sizeof(*c))||getuid()!=c->uid||getgid()!=c->gid||fr_proc(getpid(),&parent,&birth)||parent!=session_origin||birth!=session_owner_birth)return -EPERM;
 /* Stored original clock; receipt-only calls may cross WORK, never HARD.
  * Creation still checks WORK in its actual native INTENT/spawn path. */
 if(fr_now()>=c->hard_ns-1000000000ull)return -ETIMEDOUT;
 return 0;
}
static int session_socket(int sock){struct stat st;struct ucred peer;socklen_t n=sizeof(peer);int type=0,pass=0;socklen_t z=sizeof(int);int parent;uint64_t birth;
 if(!session_ready||native_owner!=getpid()||sock!=FR_REG_FD||fstat(sock,&st)||!S_ISSOCK(st.st_mode)||st.st_dev!=session_socket_stat.st_dev||st.st_ino!=session_socket_stat.st_ino||getsockopt(sock,SOL_SOCKET,SO_PEERCRED,&peer,&n)||n!=sizeof(peer)||peer.pid!=session_origin||peer.uid||peer.gid||getsockopt(sock,SOL_SOCKET,SO_TYPE,&type,&z)||type!=SOCK_SEQPACKET||getsockopt(sock,SOL_SOCKET,SO_PASSCRED,&pass,&z)||pass!=1||fr_proc(session_origin,&parent,&birth)||birth!=session_origin_birth)return -ESTALE;
 return 0;
}
int fr_session_start(int sock,const char*sha,struct fr_packet*start){
 /* A refused restart, including an inherited ready context, cannot change
  * the owner paired with the stored birth/session/socket/native bindings. */
 active_evidence=&session_evidence;session_evidence.version=1;session_evidence.phase=FR_START;
 if(session_ready||native_creation_poisoned||!start||sock!=FR_REG_FD){int result=evidence_error(FR_E_INPUT,-EPERM);poison_generation();return result;}
 native_owner=getpid();
 struct fr_cap c;struct stat st;struct ucred peer;socklen_t n=sizeof(peer),z=sizeof(int);int type=0,pass=0,parent=0,outer_parent=0;uint64_t own_birth=0,outer_birth=0;
 int r=fr_cap_read(100,sha,&c);if(r){evidence_error(FR_E_CAPSULE,r);goto bad;}
 if(getuid()!=c.uid||getgid()!=c.gid||fr_proc(getpid(),&parent,&own_birth)||parent!=getppid()||fr_proc(parent,&outer_parent,&outer_birth)){r=evidence_error(FR_E_OWNER_PROC,-EPERM);goto bad;}
 if(fstat(sock,&st)||!S_ISSOCK(st.st_mode)||getsockopt(sock,SOL_SOCKET,SO_PEERCRED,&peer,&n)||n!=sizeof(peer)||peer.pid!=parent||peer.uid||peer.gid||getsockopt(sock,SOL_SOCKET,SO_TYPE,&type,&z)||type!=SOCK_SEQPACKET||getsockopt(sock,SOL_SOCKET,SO_PASSCRED,&pass,&z)||pass!=1){r=evidence_error(FR_E_SOCKET,-EPERM);goto bad;}
 struct fr_packet p;struct fr_receive_receipt receipt;int fd=-1,pid=-1,uid=-1,gid=-1;uint64_t end=fr_now()+15000000000ull;if(end>c.work_ns)end=c.work_ns;
 r=fr_recv_receipt(sock,&p,&fd,&pid,&uid,&gid,end,&receipt);
 session_evidence.observed=p;session_evidence.observed_pid=pid;session_evidence.observed_uid=uid;session_evidence.observed_gid=gid;session_evidence.expected_pid=parent;
 session_evidence.rights=receipt.rights;session_evidence.rights_closed=receipt.closed;session_evidence.rights_close_errno=receipt.close_errno;
 struct fr_packet*w=&session_evidence.expected;memset(w,0,sizeof(*w));memcpy(w->magic,"FRA061P1",8);memcpy(w->session,c.session,32);w->version=1;w->type=FR_START;w->sequence=1;w->pid=getpid();w->owner=parent;w->birth=own_birth;w->owner_birth=outer_birth;w->deadline_ns=c.work_ns;
 if(fd>=0){if(close(fd)){session_evidence.rights_close_errno=errno;r=-EIO;}else session_evidence.rights_closed++;}
 if(r){evidence_error(receipt.rights>1?FR_E_RIGHTS:session_evidence.rights_close_errno?FR_E_RIGHT_CLOSE:FR_E_RECEIVE,r);goto bad;}
 unsigned stage=fd>=0?FR_E_RIGHTS:pid!=parent?FR_E_ORIGIN_PID:uid?FR_E_ORIGIN_UID:gid?FR_E_ORIGIN_GID:
  memcmp(p.magic,w->magic,8)||memcmp(p.session,w->session,32)||p.version!=1?FR_E_FRAME:p.type!=w->type?FR_E_TYPE:p.sequence!=1?FR_E_SEQUENCE:p.role?FR_E_ROLE:
  p.pid!=w->pid?FR_E_PID:p.owner!=w->owner?FR_E_OWNER:p.birth!=w->birth?FR_E_BIRTH:p.owner_birth!=w->owner_birth?FR_E_OWNER_BIRTH:p.deadline_ns!=w->deadline_ns?FR_E_DEADLINE:p.status||p.detail?FR_E_STATUS:0;
 if(stage){r=evidence_error(stage,-EBADMSG);goto bad;}
 session_cap=c;session_origin=parent;session_origin_birth=outer_birth;session_owner_birth=own_birth;session_socket_stat=st;native_owner=getpid();session_ready=1;*start=p;return 0;
bad:native_creation_poisoned=1;return r;
}
static void packet(struct fr_packet*p,const struct fr_cap*c,unsigned type,unsigned role){memset(p,0,sizeof(*p));memcpy(p->magic,"FRA061P1",8);memcpy(p->session,c->session,32);p->version=1;p->type=type;p->role=role;p->sequence=session_sequence;p->owner=native_owner;p->owner_birth=session_owner_birth;p->deadline_ns=c->work_ns;}
static int ack(int sock,const struct fr_packet*q,unsigned expected,uint64_t end){
 struct fr_packet p;struct fr_receive_receipt receipt;int fd=-1,pid=-1,uid=-1,gid=-1;active_evidence->phase=q->type;active_evidence->expected=*q;active_evidence->expected.type=expected;active_evidence->expected_pid=session_origin;active_evidence->expected_uid=active_evidence->expected_gid=0;
 int r=session_socket(sock);if(r)return evidence_error(FR_E_SOCKET,r);
 r=fr_recv_receipt(sock,&p,&fd,&pid,&uid,&gid,end,&receipt);active_evidence->observed=p;active_evidence->observed_pid=pid;active_evidence->observed_uid=uid;active_evidence->observed_gid=gid;
 active_evidence->rights=receipt.rights;active_evidence->rights_closed=receipt.closed;active_evidence->rights_close_errno=receipt.close_errno;
 if(fd>=0){if(close(fd)){active_evidence->rights_close_errno=errno;r=-EIO;}else active_evidence->rights_closed++;}
 if(r)return evidence_error(receipt.rights>1?FR_E_RIGHTS:active_evidence->rights_close_errno?FR_E_RIGHT_CLOSE:FR_E_RECEIVE,r);
 unsigned stage=fd>=0?FR_E_RIGHTS:pid!=session_origin?FR_E_ORIGIN_PID:uid?FR_E_ORIGIN_UID:gid?FR_E_ORIGIN_GID:
  memcmp(p.magic,q->magic,8)||memcmp(p.session,q->session,32)||p.version!=1?FR_E_FRAME:p.type!=expected?FR_E_TYPE:p.role!=q->role?FR_E_ROLE:p.sequence!=q->sequence||q->sequence!=session_sequence?FR_E_SEQUENCE:
  p.owner!=q->owner?FR_E_OWNER:p.owner_birth!=q->owner_birth?FR_E_OWNER_BIRTH:p.pid!=q->pid?FR_E_PID:p.birth!=q->birth?FR_E_BIRTH:p.status!=q->status||p.detail!=q->detail?FR_E_STATUS:p.deadline_ns!=q->deadline_ns?FR_E_DEADLINE:0;
 if(stage)return evidence_error(stage,-EBADMSG);
 if(expected==FR_INTENT_ACK)active_evidence->intent_ack_ns=fr_now();else if(expected==FR_REGISTER_ACK)active_evidence->register_ack_ns=fr_now();else if(expected==FR_REAP_ACK)active_evidence->reap_ack_ns=fr_now();
 if(session_sequence==UINT32_MAX)return -EOVERFLOW;session_sequence++;return 0;
}
static int abort_creation(int sock,struct fr_packet*q,int error,uint64_t end,struct fr_child*rec){
 q->type=FR_ABORT;q->sequence=session_sequence;q->detail=error;
 int r=fr_send(sock,q,-1,end);if(!r)r=ack(sock,q,FR_ABORT_ACK,end);
 if(rec){rec->creation_errno=error;if(r)poison(rec);}
 return r?r:-error;
}
static void close_except(int body,int event,int config,int python,int gate,int plane){
 /* Descriptor permutation is performed before closing; all fixed destinations
  * are above stdin/stdout/stderr and cannot alias caller allocations. */
 int src[6]={body,event,config,python,plane,gate},dst[6]={125,126,124,130,FR_BODY_FD,128},tmp[6];
 int count=gate>=0?6:5;
 for(int i=0;i<count;i++){tmp[i]=fcntl(src[i],F_DUPFD_CLOEXEC,400);if(tmp[i]<0)_exit(125);}
 for(int i=0;i<count;i++){if(dup3(tmp[i],dst[i],0)<0)_exit(125);close(tmp[i]);}
 for(int fd=0;fd<512;fd++){int keep=fd==125||fd==126||fd==124||fd==130||fd==FR_BODY_FD||(fd==128&&gate>=0)||fd==100||fd==105||fd==109||fd==111||fd==112||fd==116||fd==117||fd==118||fd==119;if(keep){if(fcntl(fd,F_SETFD,0)<0)_exit(125);}else close(fd);}
}
static int own_spawn_impl(int sock,unsigned role,int config,int body,int event,int python,int gate,uint64_t scheduler_end,struct fr_child*rec){
 if(!rec)return -EINVAL;memset(rec,0,sizeof(*rec));rec->pidfd=-1;rec->role=role;
 struct fr_cap c;if(cap_get(&c)||session_socket(sock)||role>=3||fr_sealed(config,0)||(c.mode!=FR_EXECUTE&&c.mode!=FR_BENIGN)||(c.mode==FR_BENIGN?gate<0:gate>=0)){poison(rec);return -EPERM;}
 int body_result=fr_body_source_validate(FR_BODY_FD+1+(int)role,role+1,&c,1);if(body_result){poison(rec);return body_result;}
 if(fr_now()>=c.work_ns){native_creation_poisoned=1;return -ETIMEDOUT;}
 int owner_result=creation_owner();if(owner_result)return owner_result;
 uint64_t end=fr_now()+15000000000ull;if(end>c.work_ns)end=c.work_ns;if(end>scheduler_end)end=scheduler_end;if(end<=fr_now()){native_creation_poisoned=1;return -ETIMEDOUT;}
 struct fr_packet q;packet(&q,&c,FR_INTENT,role);if(!q.owner_birth)return -ESTALE;
 int r=fr_send(sock,&q,-1,end);if(r){poison(rec);return r;}r=ack(sock,&q,FR_INTENT_ACK,end);if(r){poison(rec);return r;}
 int barrier[2];if(pipe2(barrier,O_CLOEXEC)){int saved=errno;return abort_creation(sock,&q,saved,end,rec);}
 int pidfd=-1;struct clone_args a={0};a.flags=CLONE_PIDFD;a.pidfd=(uintptr_t)&pidfd;a.exit_signal=SIGCHLD;
 pid_t pid=syscall(SYS_clone3,&a,sizeof(a));
 if(pid>0){rec->pid=pid;rec->pidfd=pidfd;rec->state=FR_CREATED;struct owned_binding *entry=binding_record(rec);if(!entry||fstat(pidfd,&entry->handle))native_creation_poisoned=1;}
 if(pid<0){int saved=errno;int a=close(barrier[0]),b=close(barrier[1]);if(a||b){poison(rec);return -EIO;}return abort_creation(sock,&q,saved,end,rec);}
 if(pid==0){close(barrier[1]);struct pollfd p={barrier[0],POLLIN,0};int released=0;while(fr_now()<end){int n=poll(&p,1,10);if(n>0){unsigned char b=0;if(read(barrier[0],&b,1)==1&&b==0xa5)released=1;break;}if(n<0&&errno!=EINTR)break;}close(barrier[0]);if(!released)_exit(124);
  if(fr_limits(FR_WORKER,330)||prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0))_exit(125);
  close_except(body,event,config,python,gate,FR_BODY_FD+1+(int)role);
  char*argv[]={"/usr/bin/python3.14","-I","-S","-B","-c",
   "import os,sys;d=os.pread(112,1048577,0);exec(compile(d,'/var/tmp/friday-astra-browser-receiving-native-controls-a091-g1/source/A071-WORKER.py','exec'),{'__name__':'__main__','__file__':'/var/tmp/friday-astra-browser-receiving-native-controls-a091-g1/source/A071-WORKER.py'})",0};
  char*env[]={"PATH=/usr/bin:/bin","LANG=C","LC_ALL=C",0};fexecve(130,argv,env);_exit(125);
 }
 if(close(barrier[0])){int saved=errno;close(barrier[1]);poison(rec);return -saved;}int parent=0;r=fr_proc(pid,&parent,&rec->birth);
 if(r||native_creation_poisoned||parent!=getpid()||pidfd<0||fr_pidfd_pid(pidfd)!=pid){close(barrier[1]);poison(rec);return r?r:-ESTALE;}
 q.type=FR_REGISTER;q.sequence=session_sequence;q.pid=pid;q.birth=rec->birth;r=fr_send(sock,&q,pidfd,end);
 if(!r)r=ack(sock,&q,FR_REGISTER_ACK,end);
 if(!r){rec->state=FR_REGISTERED;unsigned char release=0xa5;if(write(barrier[1],&release,1)!=1)r=-EIO;}
 if(close(barrier[1])&&!r)r=-errno;if(r)poison(rec);return r;
}
static int own_wait_impl(int sock,struct fr_child*rec,int nonblock){
 if(!rec||rec->pid<=0||rec->state==FR_EMPTY)return -EINVAL;if(rec->state==FR_UNKNOWN)return -EUCLEAN;if(rec->state==FR_REAPED)return 1;
 struct owned_binding *entry=binding_record(rec);if(!entry||native_owner!=getpid()){poison(rec);return evidence_error(FR_E_OWNER_PROC,-EPERM);}
 if(nonblock!=0&&nonblock!=1){poison(rec);return evidence_error(FR_E_INPUT,-EINVAL);}
 int socket_result=session_socket(sock);if(socket_result){poison(rec);return evidence_error(FR_E_SOCKET,socket_result);}
 if(fr_now()>=session_cap.hard_ns-1000000000ull)return evidence_error(FR_E_DEADLINE,-ETIMEDOUT);
 int status=0;struct rusage usage;pid_t p=wait4(rec->pid,&status,WNOHANG,&usage);if(p==0)return 0;if(p<0){int saved=errno;poison(rec);return evidence_error(FR_E_WAIT4,-saved);}
 /* The kernel result is PRIVATE and durable. Public status is UNKNOWN until
  * the exact fixed-origin REAP_ACK; cleanup cannot manufacture that ACK. */
 entry->wait_observed=entry->cleanup_reaped=1;entry->kernel_status=status;rec->status=0;rec->state=FR_UNKNOWN;
 entry->evidence.wait4_ns=fr_now();
 struct fr_cap c;if(cap_get(&c)){poison(rec);return -EPERM;}struct fr_packet q;packet(&q,&c,FR_REAP,rec->role);q.pid=rec->pid;q.birth=rec->birth;q.status=status;
 uint64_t end=fr_now()+1000000000ull;if(end>c.hard_ns-1000000000ull)end=c.hard_ns-1000000000ull;
 int r=fr_send(sock,&q,-1,end);if(r){poison(rec);return r;}
 r=ack(sock,&q,FR_REAP_ACK,end);if(r){poison(rec);return r;}
 rec->status=entry->kernel_status;rec->state=FR_REAPED;return 1;
}
static int handle_identity(struct owned_binding*entry){struct stat st;int parent;uint64_t birth;
 return entry->handle_closed||entry->actual.pidfd<0||fstat(entry->actual.pidfd,&st)||st.st_dev!=entry->handle.st_dev||st.st_ino!=entry->handle.st_ino||fr_pidfd_pid(entry->actual.pidfd)!=entry->actual.pid||fr_proc(entry->actual.pid,&parent,&birth)||parent!=entry->owner||birth!=entry->actual.birth?-ESTALE:0;
}
static int close_cleanup_handle(struct owned_binding*entry){
 if(entry->handle_closed||entry->actual.pidfd<0)return -EUCLEAN;
 struct stat st;int fd=entry->actual.pidfd;
 if(fstat(fd,&st)||st.st_dev!=entry->handle.st_dev||st.st_ino!=entry->handle.st_ino)return -ESTALE;
 entry->evidence.close_attempts++;
 if(close(fd)){int saved=errno;evidence_error(FR_E_HANDLE_CLOSE,-saved);return -saved;}
 entry->handle_closed=1;entry->actual.pidfd=-1;entry->evidence.close_ns=fr_now();return -EUCLEAN;
}
static int local_cleanup(struct owned_binding*entry,uint64_t end){
 struct fr_child *rec=&entry->actual;if(entry->owner!=getpid()||rec->pid<=0)return -EPERM;
 if(entry->cleanup_reaped)return close_cleanup_handle(entry);
 /* Sticky UNKNOWN still permits finite disposal of this exact native clone.
  * Failed handle checks never fall back to a guessed numeric PID. */
 if(!entry->stop_attempted){entry->stop_attempted=1;
  int status=0;struct rusage usage;pid_t p=wait4(rec->pid,&status,WNOHANG,&usage);
  if(p==rec->pid){entry->wait_observed=entry->cleanup_reaped=1;entry->kernel_status=status;entry->evidence.wait4_ns=fr_now();return close_cleanup_handle(entry);}
  if(p<0)return -errno;
  /* An unverified handle is never signalled. Direct-child wait custody still
   * permits observing the barrier-aborted actual clone within the deadline. */
  if(!handle_identity(entry)){entry->evidence.signal_attempts++;entry->evidence.signal_ns=fr_now();if(syscall(SYS_pidfd_send_signal,rec->pidfd,SIGKILL,0,0)&&errno!=ESRCH)return evidence_error(FR_E_SIGNAL,-errno);}
 }
 while(fr_now()<end){int status=0;struct rusage usage;pid_t p=wait4(rec->pid,&status,WNOHANG,&usage);
  if(p==rec->pid){entry->wait_observed=entry->cleanup_reaped=1;entry->kernel_status=status;entry->evidence.wait4_ns=fr_now();return close_cleanup_handle(entry);}
  if(p<0)return -errno;poll(0,0,5);}
 return -ETIMEDOUT;
}
static int own_stop_impl(int sock,struct fr_child*rec,uint64_t end){
 if(!rec||rec->pid<=0)return -EINVAL;struct owned_binding *entry=binding_record(rec);if(!entry)return -EPERM;
 if(end>session_cap.hard_ns-1000000000ull)end=session_cap.hard_ns-1000000000ull;
 if(rec->state==FR_UNKNOWN){poison(rec);return local_cleanup(entry,end);}if(rec->state==FR_REAPED)return 1;
 int r=own_wait_impl(sock,rec,1);if(r<0){local_cleanup(entry,end);return r;}if(r)return r;
 if(rec->pidfd>=0){
  if(handle_identity(entry)){poison(rec);return evidence_error(FR_E_HANDLE,-ESTALE);}
  if(!entry->stop_attempted){entry->stop_attempted=1;
   entry->evidence.signal_attempts++;entry->evidence.signal_ns=fr_now();if(syscall(SYS_pidfd_send_signal,rec->pidfd,SIGKILL,0,0)&&errno!=ESRCH){int saved=errno;evidence_error(FR_E_SIGNAL,-saved);poison(rec);local_cleanup(entry,end);return -saved;}}}
 else { /* Exact unreaped direct clone intention is the sole numeric fallback. */
  int status;struct rusage usage;pid_t p=wait4(rec->pid,&status,WNOHANG,&usage);if(p<0){poison(rec);return -errno;}if(p>0){entry->wait_observed=entry->cleanup_reaped=1;entry->kernel_status=status;poison(rec);return -EUCLEAN;}if(kill(rec->pid,SIGKILL)&&errno!=ESRCH){poison(rec);return -errno;}
 }
 while(fr_now()<end){r=own_wait_impl(sock,rec,1);if(r<0){local_cleanup(entry,end);return r;}if(r)return r;poll(0,0,5);}poison(rec);return -ETIMEDOUT;
}
int fr_own_spawn(int sock,unsigned role,int config,int body,int event,int python,int gate,uint64_t end,struct fr_child*copy){
 if(!copy)return -EINVAL;int owner_result=creation_owner();if(owner_result)return owner_result;
 struct owned_binding *entry=binding_new(copy,role);if(!entry){native_creation_poisoned=1;return -EUCLEAN;}
 entry->evidence=session_evidence;active_evidence=&entry->evidence;int r=own_spawn_impl(sock,role,config,body,event,python,gate,end,&entry->actual);*copy=entry->actual;active_evidence=&session_evidence;return r;
}
int fr_own_wait(int sock,struct fr_child*copy,int nonblock){
 if(!copy)return -EINVAL;struct fr_child *actual=binding_actual(copy);if(!actual)return -EPERM;
 struct owned_binding *entry=binding_record(actual);active_evidence=&entry->evidence;int r=own_wait_impl(sock,actual,nonblock);if(r<0&&!entry->evidence.stage)evidence_error(r==-EPERM?FR_E_SOCKET:FR_E_WAIT4,r);*copy=*actual;active_evidence=&session_evidence;return r;
}
int fr_own_stop(int sock,struct fr_child*copy,uint64_t end){
 if(!copy)return -EINVAL;struct owned_binding *entry=binding_find(copy);if(!entry){native_creation_poisoned=1;return -EPERM;}
 struct fr_child *actual=&entry->actual;if(memcmp(copy,actual,sizeof(*copy))){active_evidence=&entry->evidence;evidence_error(FR_E_COPY,-EPERM);poison(actual);}
 active_evidence=&entry->evidence;
 if(end<=fr_now()){evidence_error(FR_E_DEADLINE,-ETIMEDOUT);poison(actual);uint64_t cleanup=fr_now()+1000000000ull;if(cleanup>session_cap.hard_ns-1000000000ull)cleanup=session_cap.hard_ns-1000000000ull;int r=local_cleanup(entry,cleanup);*copy=*actual;active_evidence=&session_evidence;return r;}
 int r=own_stop_impl(sock,actual,end);*copy=*actual;active_evidence=&session_evidence;return r;
}
int fr_own_release(struct fr_child*copy){
 if(!copy)return -EINVAL;struct fr_child *rec=binding_actual(copy);if(!rec)return -EPERM;
 struct owned_binding *entry=binding_record(rec);
 active_evidence=&entry->evidence;
 if(rec->state==FR_UNKNOWN&&entry->cleanup_reaped){int r=close_cleanup_handle(entry);*copy=*rec;active_evidence=&session_evidence;return r;}
 if(rec->state!=FR_REAPED||!entry->wait_observed||!entry->cleanup_reaped){evidence_error(FR_E_WAIT4,-EUCLEAN);poison(rec);active_evidence=&session_evidence;return -EUCLEAN;}
 if(!entry->handle_closed&&rec->pidfd>=0){struct stat st;
  if(fstat(rec->pidfd,&st)||st.st_dev!=entry->handle.st_dev||st.st_ino!=entry->handle.st_ino){evidence_error(FR_E_HANDLE,-ESTALE);poison(rec);*copy=*rec;active_evidence=&session_evidence;return -ESTALE;}
  int fd=rec->pidfd;entry->evidence.close_attempts++;
  if(close(fd)){int saved=errno;evidence_error(FR_E_HANDLE_CLOSE,-saved);poison(rec);*copy=*rec;active_evidence=&session_evidence;return -saved;}
  rec->pidfd=-1;entry->handle_closed=1;entry->evidence.close_ns=fr_now();}
 *copy=*rec;active_evidence=&session_evidence;return 0;
}
static struct fr_child fixture_children[512];
static unsigned fixture_count;
static pid_t fixture_owner;
static int fixture_poisoned;
static int sol068_source_selected(const struct fr_cap*c,int selector){
 if(c->mode==FR_CONTROLS)return 1;
 if(c->mode!=FR_EXECUTE)return 0;
 struct stat st;uint8_t sha[32];char head[128],tail[128];
 const char suffix[]=",\"source_interface\":\"" FR_SOL068_INTERFACE "\"}\n";
 if(fr_sealed(119,1)||fstat(119,&st)||st.st_size<1||st.st_size>1048576||
    fr_hash_fd(119,1048576,sha)||memcmp(sha,c->pins[17],32))return 0;
 ssize_t n=pread(119,head,sizeof(head)-1,0);if(n<1)return 0;head[n]=0;
 const char prefix[]="{\"schema\":\"friday.a158.whole216-input.v1\",\"position\":";
 unsigned position=0;int consumed=0;
 if(strncmp(head,prefix,sizeof(prefix)-1)||sscanf(head+sizeof(prefix)-1,"%u,%n",&position,&consumed)!=1||
    !consumed||!fr_sol068_position(position)||(selector>=0&&position!=(unsigned)selector))return 0;
 size_t length=sizeof(suffix)-1;
 return st.st_size>=(off_t)length&&pread(119,tail,length,st.st_size-length)==(ssize_t)length&&!memcmp(tail,suffix,length);
}
int fr_own_fork(int sock,unsigned role,uint64_t scheduler_end,struct fr_child*copy){
 if(!copy)return -EINVAL;int owner_result=creation_owner();if(owner_result)return owner_result;struct fr_cap c;int r=cap_get(&c);if(r){evidence_error(FR_E_CAPSULE,r);poison_generation();return r;}
 r=session_socket(sock);if(r){native_creation_poisoned=1;return evidence_error(FR_E_SOCKET,r);}
 if(c.mode!=FR_CONTROLS&&c.mode!=FR_EXECUTE&&c.mode!=FR_BENIGN){int result=evidence_error(FR_E_INPUT,-EPERM);poison_generation();return result;}
 if(role>=(c.mode==FR_CONTROLS?512u:3u)){int result=evidence_error(FR_E_ROLE,-EINVAL);poison_generation();return result;}
 if(fr_now()>=c.work_ns){native_creation_poisoned=1;fixture_poisoned=1;return -ETIMEDOUT;}
 struct owned_binding *entry=binding_new(copy,role);if(!entry){native_creation_poisoned=1;fixture_poisoned=1;return -EUCLEAN;}
 memset(&entry->evidence,0,sizeof(entry->evidence));entry->evidence.version=1;active_evidence=&entry->evidence;
 struct fr_child*rec=&entry->actual;memset(rec,0,sizeof(*rec));rec->pidfd=-1;rec->role=role;
 uint64_t end=fr_now()+15000000000ull;if(end>c.work_ns)end=c.work_ns;if(end>scheduler_end)end=scheduler_end;
 if(end<=fr_now()){int result=evidence_error(FR_E_DEADLINE,-ETIMEDOUT);poison_generation();active_evidence=&session_evidence;return publish_fixture(copy,rec,result);}
 struct fr_packet q;packet(&q,&c,FR_INTENT,role);if(!q.owner_birth)return -ESTALE;
 fixture_poisoned=1;
 r=fr_send(sock,&q,-1,end);if(r){poison(rec);evidence_error(FR_E_RECEIVE,r);return publish_fixture(copy,rec,r);}r=ack(sock,&q,FR_INTENT_ACK,end);if(r){poison(rec);return publish_fixture(copy,rec,r);}
 int barrier[2];if(pipe2(barrier,O_CLOEXEC)){int saved=errno;int result=abort_creation(sock,&q,saved,end,rec);if(rec->state!=FR_UNKNOWN)fixture_poisoned=0;return publish_fixture(copy,rec,result);}
 int pidfd=-1;struct clone_args a={0};a.flags=CLONE_PIDFD;a.pidfd=(uintptr_t)&pidfd;a.exit_signal=SIGCHLD;
 PyOS_BeforeFork();pid_t pid=syscall(SYS_clone3,&a,sizeof(a));int clone_errno=pid<0?errno:0;
 if(pid>0){rec->pid=pid;rec->pidfd=pidfd;rec->state=FR_CREATED;entry->evidence.clone_ns=fr_now();if(fstat(pidfd,&entry->handle))native_creation_poisoned=1;}
 if(pid!=0)PyOS_AfterFork_Parent();
 if(pid<0){int saved=clone_errno;int a=close(barrier[0]),b=close(barrier[1]);if(a||b){poison(rec);return publish_fixture(copy,rec,-EIO);}int result=abort_creation(sock,&q,saved,end,rec);if(rec->state!=FR_UNKNOWN)fixture_poisoned=0;return publish_fixture(copy,rec,result);}
 if(pid==0){close(barrier[1]);struct pollfd p={barrier[0],POLLIN,0};int released=0;while(fr_now()<end){int n=poll(&p,1,10);if(n>0){unsigned char b=0;released=read(barrier[0],&b,1)==1&&b==0xa5;break;}if(n<0&&errno!=EINTR)break;}close(barrier[0]);if(!released)_exit(124);PyOS_AfterFork_Child();
  if(controller_prepared&&role==0){int parent=0;uint64_t birth=0;if(fr_proc(getpid(),&parent,&birth)||parent!=native_owner)_exit(125);controller_actual_fork_child=1;controller_actual_birth=birth;}
  return 0;}
 if(close(barrier[0])){int saved=errno;close(barrier[1]);poison(rec);uint64_t cleanup=fr_now()+1000000000ull;if(cleanup>c.hard_ns-1000000000ull)cleanup=c.hard_ns-1000000000ull;local_cleanup(entry,cleanup);return publish_fixture(copy,rec,-saved);}int parent;r=fr_proc(pid,&parent,&rec->birth);
 if(r||native_creation_poisoned||parent!=getpid()||pidfd<0||fr_pidfd_pid(pidfd)!=pid){close(barrier[1]);poison(rec);uint64_t cleanup=fr_now()+1000000000ull;if(cleanup>c.hard_ns-1000000000ull)cleanup=c.hard_ns-1000000000ull;local_cleanup(entry,cleanup);return publish_fixture(copy,rec,r?r:-ESTALE);}
 q.type=FR_REGISTER;q.sequence=session_sequence;q.pid=pid;q.birth=rec->birth;entry->evidence.register_send_ns=fr_now();r=fr_send(sock,&q,pidfd,end);if(!r)r=ack(sock,&q,FR_REGISTER_ACK,end);
 if(!r){rec->state=FR_REGISTERED;unsigned char release=0xa5;entry->evidence.release_ns=fr_now();if(write(barrier[1],&release,1)!=1)r=evidence_error(FR_E_BARRIER,-EIO);else entry->evidence.ready_released=1;}
 if(close(barrier[1])&&!r)r=-errno;if(r){poison(rec);/* The exact native intention survives until finite disposal. */
  uint64_t stop=fr_now()+1000000000ull;if(stop>c.hard_ns-1000000000ull)stop=c.hard_ns-1000000000ull;own_stop_impl(sock,rec,stop);return publish_fixture(copy,rec,r);}
 fixture_poisoned=0;active_evidence=&session_evidence;return publish_fixture(copy,rec,pid);
}
int fr_fixture_fork(int sock){
 struct fr_cap c;if(cap_get(&c)||!sol068_source_selected(&c,-1)){native_creation_poisoned=1;return -EPERM;}
 if(fixture_count>=(c.mode==FR_CONTROLS?512u:3u)||(fixture_owner&&fixture_owner!=getpid())){native_creation_poisoned=1;return -EPERM;}
 if(fixture_poisoned)return -EUCLEAN;fixture_owner=getpid();
 struct fr_child*copy=&fixture_children[fixture_count];int result=fr_own_fork(sock,fixture_count,session_cap.work_ns,copy);
 if(result!=0&&copy->pid>0)fixture_count++;return result;
}
int fr_fixture_reap(int sock,int pid,int status){
 /* Retained ABI symbol cannot turn a caller-supplied status into a kernel
  * wait receipt. The actual fixed coordinator now consumes fr_fixture_wait. */
 (void)sock;(void)status;fixture_poisoned=1;native_creation_poisoned=1;
 for(unsigned i=0;i<owned_binding_count;i++)if(owned_bindings[i].owner==getpid()&&owned_bindings[i].actual.pid==pid&&owned_bindings[i].actual.state!=FR_REAPED){active_evidence=&owned_bindings[i].evidence;evidence_error(FR_E_INPUT,-EPERM);poison(&owned_bindings[i].actual);*owned_bindings[i].public_copy=owned_bindings[i].actual;}
 active_evidence=&session_evidence;
 for(unsigned i=0;i<fixture_count;i++)if(fixture_children[i].pid==pid&&fixture_children[i].state!=FR_REAPED){struct owned_binding *entry=binding_find(&fixture_children[i]);if(entry)poison(&entry->actual);fixture_children[i].state=FR_UNKNOWN;}
 return -EPERM;
}
int fr_fixture_wait(int sock,int pid,int flags,int *status){
 if(!status||fixture_owner!=getpid()||(pid<=0&&pid!=-1)||(flags!=0&&flags!=WNOHANG)){
  fixture_poisoned=1;native_creation_poisoned=1;
  for(unsigned i=0;i<fixture_count;i++)if(fixture_children[i].pid==pid){struct owned_binding *entry=binding_find(&fixture_children[i]);if(entry){poison(&entry->actual);fixture_children[i]=entry->actual;}}
  return -EINVAL;}
 struct fr_cap c;if(cap_get(&c)||!sol068_source_selected(&c,-1))return -EPERM;
 uint64_t end=c.hard_ns-1000000000ull;
 do {
  if(fr_now()>=end){fixture_poisoned=1;native_creation_poisoned=1;return -ETIMEDOUT;}
  unsigned candidates=0;
  for(unsigned i=0;i<fixture_count;i++){
   struct fr_child *rec=&fixture_children[i];
   if(pid!=-1&&rec->pid!=pid)continue;
   if(rec->state==FR_UNKNOWN){fixture_poisoned=1;native_creation_poisoned=1;return -EUCLEAN;}
   if(rec->state==FR_REAPED||rec->state==FR_EMPTY)continue;
   candidates++;int r=fr_own_wait(sock,rec,1);
   if(r<0){fixture_poisoned=1;return r;}
   if(r==1){*status=rec->status;int owned_pid=rec->pid;
    if(fr_own_release(rec)){*status=0;fixture_poisoned=1;return -EUCLEAN;}return owned_pid;}
  }
  if(!candidates)return -ECHILD;
  if(flags==WNOHANG){*status=0;return 0;}
  if(fr_now()>=end){fixture_poisoned=1;native_creation_poisoned=1;return -ETIMEDOUT;}
  poll(0,0,5);
 } while(1);
}
int fr_public_command(unsigned kind){
 struct fr_cap c;if(cap_get(&c)||session_socket(FR_REG_FD)||(kind!=FR_DRAIN&&kind!=FR_FINISH)){native_creation_poisoned=1;fixture_poisoned=1;return -EPERM;}
 if(native_creation_poisoned)return -EUCLEAN;
 struct fr_packet q;packet(&q,&c,kind,0);uint64_t end=c.hard_ns-1000000000ull;
 int r=fr_send(123,&q,-1,end);if(!r)r=ack(123,&q,kind==FR_DRAIN?FR_DRAIN_ACK:FR_FINISH_ACK,end);
 if(r){native_creation_poisoned=1;fixture_poisoned=1;}return r;
}
static void observe(struct owned_binding*entry,struct fr_observation*out){
 memset(out,0,sizeof(*out));out->owner=native_owner;out->origin=session_origin;out->uid=session_cap.uid;out->gid=session_cap.gid;out->owner_birth=session_owner_birth;out->origin_birth=session_origin_birth;
 out->next_sequence=session_sequence;out->session_ready=session_ready&&native_owner==getpid();out->creation_poisoned=native_creation_poisoned;out->pidfd=-1;
 if(entry){struct fr_child*rec=&entry->actual;out->pid=rec->pid;out->pidfd=rec->pidfd;out->role=rec->role;out->state=rec->state;out->birth=rec->birth;out->wait_observed=entry->wait_observed;out->cleanup_reaped=entry->cleanup_reaped;out->handle_closed=entry->handle_closed;out->stop_attempted=entry->stop_attempted;
  out->status_known=rec->state==FR_REAPED&&entry->wait_observed;out->status=out->status_known?entry->kernel_status:0;}
}
int fr_session_observe(struct fr_observation*out){if(!out||native_owner!=getpid())return -EPERM;observe(0,out);return 0;}
int fr_session_projection(struct fr_observation*out){
 /* Inert read-only inherited projection: ready is false in a non-owner.
  * No handle, status, binding or creation authority is exported/adopted. */
 if(!out)return -EINVAL;observe(0,out);return 0;
}
int fr_fixture_observe(int pid,struct fr_observation*out){
 if(!out||fixture_owner!=getpid())return -EPERM;
 for(unsigned i=0;i<fixture_count;i++)if(fixture_children[i].pid==pid){struct owned_binding*entry=binding_find(&fixture_children[i]);if(!entry)return -EUCLEAN;observe(entry,out);return 0;}return -ECHILD;
}
int fr_fixture_evidence(int pid,struct fr_evidence*out){
 /* Read-only evidence for the same existing fixture/native generation. No
  * copied PID/status can create a wait, handle, selection or disposal right. */
 if(!out||fixture_owner!=getpid()||native_owner!=getpid())return -EPERM;
 for(unsigned i=0;i<fixture_count;i++)if(fixture_children[i].pid==pid){
  struct owned_binding*entry=binding_find(&fixture_children[i]);if(!entry)return -EUCLEAN;
  *out=entry->evidence;
  if(entry->actual.state!=FR_REAPED&&entry->wait_observed){out->expected.status=out->observed.status=0;out->status_redacted=1;}
  return 0;
 }return -ECHILD;
}
int fr_fixture_stop(int sock,int pid,uint64_t end){
 if(fixture_owner!=getpid())return -EPERM;
 for(unsigned i=0;i<fixture_count;i++)if(fixture_children[i].pid==pid){struct fr_child*rec=&fixture_children[i];struct owned_binding*entry=binding_find(rec);if(!entry)return -EUCLEAN;
  int r=fr_own_stop(sock,rec,end);if(r==1){int release=fr_own_release(rec);if(release)return release;}else fixture_poisoned=1;return r;}
 native_creation_poisoned=1;fixture_poisoned=1;return -ECHILD;
}
int fr_own_observe(struct fr_child*copy,struct fr_observation*out){
 struct owned_binding*entry=binding_find(copy);if(!copy||!out||!entry)return -EPERM;observe(entry,out);return 0;
}
int fr_own_evidence(struct fr_child*copy,struct fr_evidence*out){
 struct owned_binding*entry=binding_find(copy);if(!copy||!out||!entry)return -EPERM;*out=entry->evidence;
 if(entry->actual.state!=FR_REAPED&&entry->wait_observed){out->expected.status=out->observed.status=0;out->status_redacted=1;}
 return 0;
}
int fr_session_evidence(struct fr_evidence*out){if(!out||native_owner!=getpid())return -EPERM;*out=session_evidence;return 0;}
int fr_session_close(void){
 active_evidence=&session_evidence;session_evidence.transport_close_attempts++;
 int r=session_socket(FR_REG_FD);if(r){native_creation_poisoned=1;return evidence_error(FR_E_SOCKET,r);}
 if(close(FR_REG_FD)){int result=evidence_error(FR_E_TRANSPORT_CLOSE,-errno);poison_generation();return result;}
 session_evidence.transport_close_ns=fr_now();poison_generation();return 0;
}
int fr_controller_prepare(unsigned selector){
 /* The coordinator cannot choose a new Root grant: fd119, fd100 and the
  * ordinary session were externally selected, sealed and pinned already. */
 struct fr_cap c;struct stat st;uint8_t sha[32];char prefix[128],actual[128];
 if(selector>=216||controller_prepared||creation_owner()||cap_get(&c)||!sol068_source_selected(&c,(int)selector)||
    owned_binding_count||fr_sealed(119,1)||fstat(119,&st)||st.st_size<1||st.st_size>1048576||
    fr_hash_fd(119,1048576,sha)||memcmp(sha,c.pins[17],32))return -EPERM;
 int n=snprintf(prefix,sizeof(prefix),"{\"schema\":\"friday.a158.whole216-input.v1\",\"position\":%u,",selector);
 if(n<=0||(size_t)n>=sizeof(prefix)||pread(119,actual,n,0)!=n||memcmp(actual,prefix,n))return -EBADMSG;
 controller_prepared=selector+1;return 0;
}
static int controller_root_socket(struct controller_context*d){
 struct stat st;struct ucred peer;socklen_t n=sizeof(peer),z=sizeof(int);int type=0,pass=0,parent=0;uint64_t birth=0;
 if(fstat(FR_REG_FD,&st)||!S_ISSOCK(st.st_mode)||st.st_dev!=d->socket.st_dev||st.st_ino!=d->socket.st_ino||
    getsockopt(FR_REG_FD,SOL_SOCKET,SO_PEERCRED,&peer,&n)||n!=sizeof(peer)||peer.pid!=d->origin||peer.uid||peer.gid||
    getsockopt(FR_REG_FD,SOL_SOCKET,SO_TYPE,&type,&z)||type!=SOCK_SEQPACKET||
    getsockopt(FR_REG_FD,SOL_SOCKET,SO_PASSCRED,&pass,&z)||pass!=1||
    fr_proc(d->origin,&parent,&birth)||birth!=d->origin_birth)return -ESTALE;
 return 0;
}
static int controller_receive(struct controller_context*d,struct fr_evidence*e,struct fr_packet*w,uint64_t end){
 struct fr_packet p={0};struct fr_receive_receipt receipt={0};int fd=-1,pid=-1,uid=-1,gid=-1;
 e->version=1;e->phase=w->type;e->expected=*w;e->expected_pid=d->origin;e->expected_uid=e->expected_gid=0;
 int r=controller_root_socket(d);
 if(!r)r=fr_recv_receipt(FR_REG_FD,&p,&fd,&pid,&uid,&gid,end,&receipt);
 e->observed=p;e->observed_pid=pid;e->observed_uid=uid;e->observed_gid=gid;
 e->rights=receipt.rights;e->rights_closed=receipt.closed;e->rights_close_errno=receipt.close_errno;
 if(fd>=0){if(close(fd)){e->rights_close_errno=errno;r=-EIO;}else e->rights_closed++;}
 if(!r&&(fd>=0||receipt.credentials!=1||pid!=d->origin||uid||gid||memcmp(&p,w,sizeof(p))))r=-EBADMSG;
 if(r){e->stage=FR_E_RECEIVE;e->primitive_errno=r;d->poisoned=1;return r;}
 return 0;
}
int fr_controller_join(unsigned selector,struct fr_evidence*out){
 struct controller_context*d=&controller_context;
 if(!out||selector>=216||d->attempted||!controller_actual_fork_child||controller_prepared!=selector+1||
    native_owner==getpid()||native_creation_poisoned||!session_ready)return -EPERM;
 d->attempted=1;d->owner=getpid();d->parent=native_owner;d->origin=session_origin;
 d->birth=controller_actual_birth;d->parent_birth=session_owner_birth;d->origin_birth=session_origin_birth;
 d->selector=selector;d->cap=session_cap;d->socket=session_socket_stat;
 struct fr_cap cap;struct stat st;int parent=0;uint64_t birth=0;
 if(fr_sealed(100,1)||fstat(100,&st)||st.st_size!=sizeof(cap)||pread(100,&cap,sizeof(cap),0)!=sizeof(cap)||
    memcmp(&cap,&d->cap,sizeof(cap))||cap.mode!=FR_CONTROLS||getuid()!=cap.uid||getgid()!=cap.gid||
    fr_proc(getpid(),&parent,&birth)||parent!=d->parent||birth!=d->birth||fr_now()>=cap.work_ns){d->poisoned=1;return -EPERM;}
 struct fr_packet expected={0};memcpy(expected.magic,"FRA061P1",8);memcpy(expected.session,cap.session,32);
 expected.version=1;expected.type=FR_START;expected.sequence=1;expected.role=0;
 expected.pid=d->owner;expected.owner=d->parent;expected.birth=d->birth;expected.owner_birth=d->parent_birth;
 expected.detail=selector+1;expected.deadline_ns=cap.work_ns;
 uint64_t end=fr_now()+1000000000ull;if(end>cap.work_ns)end=cap.work_ns;
 int r=controller_receive(d,&d->start,&expected,end);*out=d->start;
 if(!r){d->ready=1;d->start.register_ack_ns=fr_now();*out=d->start;}
 return r;
}
int fr_controller_finish(struct fr_evidence*out){
 struct controller_context*d=&controller_context;
 if(!out||!d->ready||d->finished||d->poisoned||native_creation_poisoned||d->owner!=getpid()||fr_now()>=d->cap.hard_ns-3000000000ull)return -EPERM;
 struct fr_packet q={0};memcpy(q.magic,"FRA061P1",8);memcpy(q.session,d->cap.session,32);
 q.version=1;q.type=FR_FINISH;q.sequence=2;q.role=0;q.pid=d->owner;q.owner=d->owner;
 q.birth=q.owner_birth=d->birth;q.detail=d->selector+1;q.deadline_ns=d->cap.work_ns;
 uint64_t end=fr_now()+1000000000ull;if(end>d->cap.hard_ns-3000000000ull)end=d->cap.hard_ns-3000000000ull;
 int r=controller_root_socket(d);if(!r)r=fr_send(FR_REG_FD,&q,-1,end);
 struct fr_packet expected=q;expected.type=FR_FINISH_ACK;
 if(!r)r=controller_receive(d,&d->finish,&expected,end);
 else {d->finish.version=1;d->finish.phase=FR_FINISH;d->finish.stage=FR_E_RECEIVE;d->finish.primitive_errno=r;d->poisoned=1;}
 if(!r){d->finished=1;d->ready=0;d->finish.reap_ack_ns=fr_now();}
 *out=d->finish;return r;
}
int fr_controller_observe(struct fr_observation*out){
 struct controller_context*d=&controller_context;if(!out||!d->attempted||d->owner!=getpid())return -EPERM;
 memset(out,0,sizeof(*out));out->owner=d->owner;out->origin=d->origin;out->uid=d->cap.uid;out->gid=d->cap.gid;
 out->owner_birth=d->birth;out->origin_birth=d->origin_birth;out->pid=d->owner;out->pidfd=-1;
 out->role=0;out->state=d->poisoned?FR_UNKNOWN:FR_REGISTERED;
 out->birth=d->birth;out->next_sequence=d->finished?3:2;out->session_ready=d->ready;out->creation_poisoned=d->poisoned;
 /* FINISH is not wait4/REAP_ACK. All wait/status/cleanup fields remain zero. */
 return 0;
}
int fr_local_restrict(unsigned operation,int fd){
 if(creation_owner()||operation<1||operation>2||(operation==2&&fd<0))return -EPERM;
 /* Filter the actual syscall in this current native owner; no userspace errno
  * injection, authority impersonation, LSM change or permission restoration. */
 struct sock_filter filter[]={
  BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,arch)),
  BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,AUDIT_ARCH_X86_64,1,0),
  BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
  BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr)),
  BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,operation==1?SYS_pidfd_send_signal:SYS_close,0,3),
  BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0])),
  BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,operation==1?0:(unsigned)fd,0,1),
  BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM),
  BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW)
 };
 /* For signal restriction there is no fd-specific allow branch. */
 if(operation==1){filter[5]=(struct sock_filter)BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);}
 struct sock_fprog program={sizeof(filter)/sizeof(filter[0]),filter};
 if(prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0)||prctl(PR_SET_SECCOMP,SECCOMP_MODE_FILTER,&program))return -errno;
 return 0;
}
