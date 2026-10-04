#include "A071-NATIVE.h"
#include <linux/sched.h>
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
};
static struct owned_binding owned_bindings[515];
static unsigned owned_binding_count;
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
 if(!entry||memcmp(copy,&entry->actual,sizeof(*copy))){poison(entry?&entry->actual:0);return 0;}
 return &entry->actual;
}
static struct owned_binding *binding_record(struct fr_child*actual){
 for(unsigned i=0;i<owned_binding_count;i++)if(&owned_bindings[i].actual==actual&&owned_bindings[i].owner==getpid())return &owned_bindings[i];
 return 0;
}
static int publish_fixture(struct fr_child*copy,struct fr_child*actual,int result){*copy=*actual;return result;}
static int cap_get(struct fr_cap*c){struct stat s;int parent;uint64_t birth;
 if(!session_ready||native_owner!=getpid()||fr_sealed(100,1)||fstat(100,&s)||s.st_size!=sizeof(*c)||pread(100,c,sizeof(*c),0)!=sizeof(*c)||memcmp(c,&session_cap,sizeof(*c))||getuid()!=c->uid||getgid()!=c->gid||fr_proc(getpid(),&parent,&birth)||parent!=session_origin||birth!=session_owner_birth)return -EPERM;
 return 0;
}
static int session_socket(int sock){struct stat st;struct ucred peer;socklen_t n=sizeof(peer);int type=0,pass=0;socklen_t z=sizeof(int);int parent;uint64_t birth;
 if(!session_ready||native_owner!=getpid()||sock!=FR_REG_FD||fstat(sock,&st)||!S_ISSOCK(st.st_mode)||st.st_dev!=session_socket_stat.st_dev||st.st_ino!=session_socket_stat.st_ino||getsockopt(sock,SOL_SOCKET,SO_PEERCRED,&peer,&n)||n!=sizeof(peer)||peer.pid!=session_origin||peer.uid||peer.gid||getsockopt(sock,SOL_SOCKET,SO_TYPE,&type,&z)||type!=SOCK_SEQPACKET||getsockopt(sock,SOL_SOCKET,SO_PASSCRED,&pass,&z)||pass!=1||fr_proc(session_origin,&parent,&birth)||birth!=session_origin_birth)return -ESTALE;
 return 0;
}
int fr_session_start(int sock,const char*sha,struct fr_packet*start){
 if(session_ready||native_creation_poisoned||!start||sock!=FR_REG_FD){native_creation_poisoned=1;return -EPERM;}
 struct fr_cap c;struct stat st;struct ucred peer;socklen_t n=sizeof(peer),z=sizeof(int);int type=0,pass=0,parent=0,outer_parent=0;uint64_t own_birth=0,outer_birth=0;
 int r=fr_cap_read(100,sha,&c);if(r)goto bad;
 if(getuid()!=c.uid||getgid()!=c.gid||fr_proc(getpid(),&parent,&own_birth)||parent!=getppid()||fr_proc(parent,&outer_parent,&outer_birth)||fstat(sock,&st)||!S_ISSOCK(st.st_mode)||getsockopt(sock,SOL_SOCKET,SO_PEERCRED,&peer,&n)||n!=sizeof(peer)||peer.pid!=parent||peer.uid||peer.gid||getsockopt(sock,SOL_SOCKET,SO_TYPE,&type,&z)||type!=SOCK_SEQPACKET||getsockopt(sock,SOL_SOCKET,SO_PASSCRED,&pass,&z)||pass!=1){r=-EPERM;goto bad;}
 struct fr_packet p;int fd=-1,pid=-1,uid=-1,gid=-1;uint64_t end=fr_now()+15000000000ull;if(end>c.work_ns)end=c.work_ns;
 r=fr_recv(sock,&p,&fd,&pid,&uid,&gid,end);
 if(fd>=0&&close(fd))r=-EIO;
 if(r)goto bad;
 if(fd>=0||pid!=parent||uid||gid||memcmp(p.magic,"FRA061P1",8)||memcmp(p.session,c.session,32)||p.version!=1||p.type!=FR_START||p.sequence!=1||p.role||p.pid!=getpid()||p.owner!=parent||p.status||p.detail||p.birth!=own_birth||p.owner_birth!=outer_birth||p.deadline_ns!=c.work_ns){r=-EBADMSG;goto bad;}
 session_cap=c;session_origin=parent;session_origin_birth=outer_birth;session_owner_birth=own_birth;session_socket_stat=st;native_owner=getpid();session_ready=1;*start=p;return 0;
bad:native_creation_poisoned=1;return r;
}
static void packet(struct fr_packet*p,const struct fr_cap*c,unsigned type,unsigned role){memset(p,0,sizeof(*p));memcpy(p->magic,"FRA061P1",8);memcpy(p->session,c->session,32);p->version=1;p->type=type;p->role=role;p->sequence=session_sequence;p->owner=native_owner;p->owner_birth=session_owner_birth;p->deadline_ns=c->work_ns;}
static int ack(int sock,const struct fr_packet*q,unsigned expected,uint64_t end){
 struct fr_packet p;int fd=-1,pid=-1,uid=-1,gid=-1;int r=session_socket(sock);if(r)return r;
 r=fr_recv(sock,&p,&fd,&pid,&uid,&gid,end);if(fd>=0&&close(fd))r=-EIO;if(r)return r;
 if(pid!=session_origin||uid!=0||gid!=0||fd>=0||memcmp(p.magic,q->magic,8)||memcmp(p.session,q->session,32)||p.version!=1||p.type!=expected||p.role!=q->role||p.sequence!=q->sequence||q->sequence!=session_sequence||p.owner!=q->owner||p.owner_birth!=q->owner_birth||p.pid!=q->pid||p.birth!=q->birth||p.status!=q->status||p.detail!=q->detail||p.deadline_ns!=q->deadline_ns)return -EBADMSG;
 if(session_sequence==UINT32_MAX)return -EOVERFLOW;session_sequence++;return 0;
}
static int abort_creation(int sock,struct fr_packet*q,int error,uint64_t end,struct fr_child*rec){
 q->type=FR_ABORT;q->sequence=session_sequence;q->detail=error;
 int r=fr_send(sock,q,-1,end);if(!r)r=ack(sock,q,FR_ABORT_ACK,end);
 if(rec){rec->creation_errno=error;if(r)poison(rec);}
 return r?r:-error;
}
static void close_except(int body,int event,int config,int python,int gate){
 /* Descriptor permutation is performed before closing; all fixed destinations
  * are above stdin/stdout/stderr and cannot alias caller allocations. */
 int src[5]={body,event,config,python,gate},dst[5]={125,126,124,130,128},tmp[5];
 int count=gate>=0?5:4;
 for(int i=0;i<count;i++){tmp[i]=fcntl(src[i],F_DUPFD_CLOEXEC,400);if(tmp[i]<0)_exit(125);}
 for(int i=0;i<count;i++){if(dup3(tmp[i],dst[i],0)<0)_exit(125);close(tmp[i]);}
 for(int fd=0;fd<512;fd++){int keep=fd==125||fd==126||fd==124||fd==130||(fd==128&&gate>=0)||fd==100||fd==105||fd==109||fd==111||fd==112||fd==116||fd==117||fd==118||fd==119;if(keep){if(fcntl(fd,F_SETFD,0)<0)_exit(125);}else close(fd);}
}
static int own_spawn_impl(int sock,unsigned role,int config,int body,int event,int python,int gate,uint64_t scheduler_end,struct fr_child*rec){
 if(!rec)return -EINVAL;memset(rec,0,sizeof(*rec));rec->pidfd=-1;rec->role=role;
 struct fr_cap c;if(cap_get(&c)||session_socket(sock)||role>=3||fr_sealed(config,0)||(c.mode!=FR_EXECUTE&&c.mode!=FR_BENIGN)||(c.mode==FR_BENIGN?gate<0:gate>=0)){poison(rec);return -EPERM;}
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
  close_except(body,event,config,python,gate);
  char*argv[]={"/usr/bin/python3.14","-I","-S","-B","-c",
   "import os,sys;d=os.pread(112,1048577,0);exec(compile(d,'/var/tmp/friday-astra-browser-native-authoritative-caller-a079-g1/A071-WORKER.py','exec'),{'__name__':'__main__','__file__':'/var/tmp/friday-astra-browser-native-authoritative-caller-a079-g1/A071-WORKER.py'})",0};
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
 struct owned_binding *entry=binding_record(rec);if(!entry||native_owner!=getpid()||(nonblock!=0&&nonblock!=1)||session_socket(sock)){poison(rec);return -EPERM;}
 int status=0;struct rusage usage;pid_t p=wait4(rec->pid,&status,WNOHANG,&usage);if(p==0)return 0;if(p<0){int saved=errno;poison(rec);return -saved;}
 /* The kernel result is PRIVATE and durable. Public status is UNKNOWN until
  * the exact fixed-origin REAP_ACK; cleanup cannot manufacture that ACK. */
 entry->wait_observed=entry->cleanup_reaped=1;entry->kernel_status=status;rec->status=0;rec->state=FR_UNKNOWN;
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
 entry->handle_closed=1;entry->actual.pidfd=-1;
 return close(fd)?-errno:-EUCLEAN;
}
static int local_cleanup(struct owned_binding*entry,uint64_t end){
 struct fr_child *rec=&entry->actual;if(entry->owner!=getpid()||rec->pid<=0)return -EPERM;
 if(entry->cleanup_reaped)return close_cleanup_handle(entry);
 /* Sticky UNKNOWN still permits finite disposal of this exact native clone.
  * Failed handle checks never fall back to a guessed numeric PID. */
 if(!entry->stop_attempted){entry->stop_attempted=1;
  int status=0;struct rusage usage;pid_t p=wait4(rec->pid,&status,WNOHANG,&usage);
  if(p==rec->pid){entry->wait_observed=entry->cleanup_reaped=1;entry->kernel_status=status;return close_cleanup_handle(entry);}
  if(p<0)return -errno;
  /* An unverified handle is never signalled. Direct-child wait custody still
   * permits observing the barrier-aborted actual clone within the deadline. */
  if(!handle_identity(entry)&&syscall(SYS_pidfd_send_signal,rec->pidfd,SIGKILL,0,0)&&errno!=ESRCH)return -errno;
 }
 while(fr_now()<end){int status=0;struct rusage usage;pid_t p=wait4(rec->pid,&status,WNOHANG,&usage);
  if(p==rec->pid){entry->wait_observed=entry->cleanup_reaped=1;entry->kernel_status=status;return close_cleanup_handle(entry);}
  if(p<0)return -errno;poll(0,0,5);}
 return -ETIMEDOUT;
}
static int own_stop_impl(int sock,struct fr_child*rec,uint64_t end){
 if(!rec||rec->pid<=0)return -EINVAL;struct owned_binding *entry=binding_record(rec);if(!entry)return -EPERM;
 if(end>session_cap.hard_ns-1000000000ull)end=session_cap.hard_ns-1000000000ull;
 if(rec->state==FR_UNKNOWN){poison(rec);return local_cleanup(entry,end);}if(rec->state==FR_REAPED)return 1;
 int r=own_wait_impl(sock,rec,1);if(r<0){local_cleanup(entry,end);return r;}if(r)return r;
 if(rec->pidfd>=0){
  if(handle_identity(entry)){poison(rec);return -ESTALE;}
  if(!entry->stop_attempted){entry->stop_attempted=1;
   if(syscall(SYS_pidfd_send_signal,rec->pidfd,SIGKILL,0,0)&&errno!=ESRCH){int saved=errno;poison(rec);local_cleanup(entry,end);return -saved;}}}
 else { /* Exact unreaped direct clone intention is the sole numeric fallback. */
  int status;struct rusage usage;pid_t p=wait4(rec->pid,&status,WNOHANG,&usage);if(p<0){poison(rec);return -errno;}if(p>0){entry->wait_observed=entry->cleanup_reaped=1;entry->kernel_status=status;poison(rec);return -EUCLEAN;}if(kill(rec->pid,SIGKILL)&&errno!=ESRCH){poison(rec);return -errno;}
 }
 while(fr_now()<end){r=own_wait_impl(sock,rec,1);if(r<0){local_cleanup(entry,end);return r;}if(r)return r;poll(0,0,5);}poison(rec);return -ETIMEDOUT;
}
int fr_own_spawn(int sock,unsigned role,int config,int body,int event,int python,int gate,uint64_t end,struct fr_child*copy){
 if(!copy)return -EINVAL;int owner_result=creation_owner();if(owner_result)return owner_result;
 struct owned_binding *entry=binding_new(copy,role);if(!entry){native_creation_poisoned=1;return -EUCLEAN;}
 int r=own_spawn_impl(sock,role,config,body,event,python,gate,end,&entry->actual);*copy=entry->actual;return r;
}
int fr_own_wait(int sock,struct fr_child*copy,int nonblock){
 if(!copy)return -EINVAL;struct fr_child *actual=binding_actual(copy);if(!actual)return -EPERM;
 int r=own_wait_impl(sock,actual,nonblock);*copy=*actual;return r;
}
int fr_own_stop(int sock,struct fr_child*copy,uint64_t end){
 if(!copy)return -EINVAL;struct owned_binding *entry=binding_find(copy);if(!entry){native_creation_poisoned=1;return -EPERM;}
 struct fr_child *actual=&entry->actual;if(memcmp(copy,actual,sizeof(*copy)))poison(actual);
 if(end<=fr_now()){poison(actual);uint64_t cleanup=fr_now()+1000000000ull;if(cleanup>session_cap.hard_ns-1000000000ull)cleanup=session_cap.hard_ns-1000000000ull;int r=local_cleanup(entry,cleanup);*copy=*actual;return r;}
 int r=own_stop_impl(sock,actual,end);*copy=*actual;return r;
}
int fr_own_release(struct fr_child*copy){
 if(!copy)return -EINVAL;struct fr_child *rec=binding_actual(copy);if(!rec)return -EPERM;
 struct owned_binding *entry=binding_record(rec);
 if(rec->state==FR_UNKNOWN&&entry->cleanup_reaped){int r=close_cleanup_handle(entry);*copy=*rec;return r;}
 if(rec->state!=FR_REAPED||!entry->wait_observed||!entry->cleanup_reaped){poison(rec);return -EUCLEAN;}
 if(!entry->handle_closed&&rec->pidfd>=0){struct stat st;
  if(fstat(rec->pidfd,&st)||st.st_dev!=entry->handle.st_dev||st.st_ino!=entry->handle.st_ino){poison(rec);*copy=*rec;return -ESTALE;}
  int fd=rec->pidfd;rec->pidfd=-1;entry->handle_closed=1;
  if(close(fd)){int saved=errno;poison(rec);*copy=*rec;return -saved;}}
 *copy=*rec;return 0;
}
static struct fr_child fixture_children[512];
static unsigned fixture_count;
static pid_t fixture_owner;
static int fixture_poisoned;
int fr_fixture_fork(int sock){
 struct fr_cap c;if(cap_get(&c)||session_socket(sock)||c.mode!=FR_CONTROLS||fixture_count>=512||(fixture_owner&&fixture_owner!=getpid())){native_creation_poisoned=1;return -EPERM;}
 if(fr_now()>=c.work_ns){native_creation_poisoned=1;fixture_poisoned=1;return -ETIMEDOUT;}
 if(fixture_poisoned)return -EUCLEAN;
 int owner_result=creation_owner();if(owner_result)return owner_result;
 fixture_owner=getpid();
 unsigned role=fixture_count;struct fr_child*copy=&fixture_children[role];
 struct owned_binding *entry=binding_new(copy,role);if(!entry){native_creation_poisoned=1;fixture_poisoned=1;return -EUCLEAN;}
 struct fr_child*rec=&entry->actual;memset(rec,0,sizeof(*rec));rec->pidfd=-1;rec->role=role;
 uint64_t end=fr_now()+15000000000ull;if(end>c.work_ns)end=c.work_ns;
 struct fr_packet q;packet(&q,&c,FR_INTENT,role);if(!q.owner_birth)return -ESTALE;
 fixture_poisoned=1;
 int r=fr_send(sock,&q,-1,end);if(r){poison(rec);return publish_fixture(copy,rec,r);}r=ack(sock,&q,FR_INTENT_ACK,end);if(r){poison(rec);return publish_fixture(copy,rec,r);}
 int barrier[2];if(pipe2(barrier,O_CLOEXEC)){int saved=errno;int result=abort_creation(sock,&q,saved,end,rec);if(rec->state!=FR_UNKNOWN)fixture_poisoned=0;return publish_fixture(copy,rec,result);}
 int pidfd=-1;struct clone_args a={0};a.flags=CLONE_PIDFD;a.pidfd=(uintptr_t)&pidfd;a.exit_signal=SIGCHLD;
 PyOS_BeforeFork();pid_t pid=syscall(SYS_clone3,&a,sizeof(a));int clone_errno=pid<0?errno:0;
 if(pid>0){rec->pid=pid;rec->pidfd=pidfd;rec->state=FR_CREATED;fixture_count++;if(fstat(pidfd,&entry->handle))native_creation_poisoned=1;}
 if(pid!=0)PyOS_AfterFork_Parent();
 if(pid<0){int saved=clone_errno;int a=close(barrier[0]),b=close(barrier[1]);if(a||b){poison(rec);return publish_fixture(copy,rec,-EIO);}int result=abort_creation(sock,&q,saved,end,rec);if(rec->state!=FR_UNKNOWN)fixture_poisoned=0;return publish_fixture(copy,rec,result);}
 if(pid==0){close(barrier[1]);struct pollfd p={barrier[0],POLLIN,0};int released=0;while(fr_now()<end){int n=poll(&p,1,10);if(n>0){unsigned char b=0;released=read(barrier[0],&b,1)==1&&b==0xa5;break;}if(n<0&&errno!=EINTR)break;}close(barrier[0]);if(!released)_exit(124);PyOS_AfterFork_Child();return 0;}
 if(close(barrier[0])){int saved=errno;close(barrier[1]);poison(rec);uint64_t cleanup=fr_now()+1000000000ull;if(cleanup>c.hard_ns-1000000000ull)cleanup=c.hard_ns-1000000000ull;local_cleanup(entry,cleanup);return publish_fixture(copy,rec,-saved);}int parent;r=fr_proc(pid,&parent,&rec->birth);
 if(r||native_creation_poisoned||parent!=getpid()||pidfd<0||fr_pidfd_pid(pidfd)!=pid){close(barrier[1]);poison(rec);uint64_t cleanup=fr_now()+1000000000ull;if(cleanup>c.hard_ns-1000000000ull)cleanup=c.hard_ns-1000000000ull;local_cleanup(entry,cleanup);return publish_fixture(copy,rec,r?r:-ESTALE);}
 q.type=FR_REGISTER;q.sequence=session_sequence;q.pid=pid;q.birth=rec->birth;r=fr_send(sock,&q,pidfd,end);if(!r)r=ack(sock,&q,FR_REGISTER_ACK,end);
 if(!r){rec->state=FR_REGISTERED;unsigned char release=0xa5;if(write(barrier[1],&release,1)!=1)r=-EIO;}
 if(close(barrier[1])&&!r)r=-errno;if(r){poison(rec);/* The exact native intention survives until finite disposal. */
  uint64_t stop=fr_now()+1000000000ull;if(stop>c.hard_ns-1000000000ull)stop=c.hard_ns-1000000000ull;own_stop_impl(sock,rec,stop);return publish_fixture(copy,rec,r);}
 fixture_poisoned=0;return publish_fixture(copy,rec,pid);
}
int fr_fixture_reap(int sock,int pid,int status){
 /* Retained ABI symbol cannot turn a caller-supplied status into a kernel
  * wait receipt. The actual fixed coordinator now consumes fr_fixture_wait. */
 (void)sock;(void)status;fixture_poisoned=1;native_creation_poisoned=1;
 for(unsigned i=0;i<fixture_count;i++)if(fixture_children[i].pid==pid&&fixture_children[i].state!=FR_REAPED){struct owned_binding *entry=binding_find(&fixture_children[i]);if(entry)poison(&entry->actual);fixture_children[i].state=FR_UNKNOWN;}
 return -EPERM;
}
int fr_fixture_wait(int sock,int pid,int flags,int *status){
 if(!status||fixture_owner!=getpid()||(pid<=0&&pid!=-1)||(flags!=0&&flags!=WNOHANG)){
  fixture_poisoned=1;native_creation_poisoned=1;
  for(unsigned i=0;i<fixture_count;i++)if(fixture_children[i].pid==pid){struct owned_binding *entry=binding_find(&fixture_children[i]);if(entry){poison(&entry->actual);fixture_children[i]=entry->actual;}}
  return -EINVAL;}
 struct fr_cap c;if(cap_get(&c)||c.mode!=FR_CONTROLS)return -EPERM;
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
int fr_fixture_observe(int pid,struct fr_observation*out){
 if(!out||fixture_owner!=getpid())return -EPERM;
 for(unsigned i=0;i<fixture_count;i++)if(fixture_children[i].pid==pid){struct owned_binding*entry=binding_find(&fixture_children[i]);if(!entry)return -EUCLEAN;observe(entry,out);return 0;}return -ECHILD;
}
int fr_fixture_stop(int sock,int pid,uint64_t end){
 if(fixture_owner!=getpid())return -EPERM;
 for(unsigned i=0;i<fixture_count;i++)if(fixture_children[i].pid==pid){struct fr_child*rec=&fixture_children[i];struct owned_binding*entry=binding_find(rec);if(!entry)return -EUCLEAN;
  int r=fr_own_stop(sock,rec,end);if(r==1){int release=fr_own_release(rec);if(release)return release;}else fixture_poisoned=1;return r;}
 native_creation_poisoned=1;fixture_poisoned=1;return -ECHILD;
}
