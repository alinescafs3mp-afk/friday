#include "A061-NATIVE.h"
#include <linux/sched.h>
#include <sys/syscall.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/prctl.h>
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
static int cap_get(struct fr_cap*c){struct stat s;if(fr_sealed(100,1)||fstat(100,&s)||s.st_size!=sizeof(*c)||pread(100,c,sizeof(*c),0)!=sizeof(*c)||memcmp(c->magic,"FRA061C1",8)||c->version!=1||c->generation!=1||c->sources!=FR_SOURCES||getuid()!=c->uid||getgid()!=c->gid)return -EPERM;return 0;}
static void packet(struct fr_packet*p,const struct fr_cap*c,unsigned type,unsigned role){memset(p,0,sizeof(*p));memcpy(p->magic,"FRA061P1",8);memcpy(p->session,c->session,32);p->version=1;p->type=type;p->role=role;p->sequence=role+1;p->owner=getpid();p->deadline_ns=c->work_ns;int parent=0;if(fr_proc(getpid(),&parent,&p->owner_birth))p->owner_birth=0;}
static int ack(int sock,const struct fr_packet*q,unsigned expected,uint64_t end){struct fr_packet p;int fd=-1,pid=-1,uid=-1,gid=-1;int r=fr_recv(sock,&p,&fd,&pid,&uid,&gid,end);if(fd>=0)close(fd);if(r)return r;if(pid!=getppid()||uid!=0||gid!=0||fd>=0||memcmp(p.magic,q->magic,8)||memcmp(p.session,q->session,32)||p.version!=1||p.type!=expected||p.role!=q->role||p.sequence!=q->sequence||p.owner!=q->owner||p.owner_birth!=q->owner_birth||p.pid!=q->pid||p.birth!=q->birth||p.status||p.detail!=q->detail||p.deadline_ns!=q->deadline_ns)return -EBADMSG;return 0;}
static int abort_creation(int sock,struct fr_packet*q,int error,uint64_t end,struct fr_child*rec){
 q->type=FR_ABORT;q->detail=error;
 int r=fr_send(sock,q,-1,end);if(!r)r=ack(sock,q,FR_ABORT_ACK,end);
 if(rec){rec->creation_errno=error;if(r)rec->state=FR_UNKNOWN;}
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
int fr_own_spawn(int sock,unsigned role,int config,int body,int event,int python,int gate,uint64_t scheduler_end,struct fr_child*rec){
 if(!rec)return -EINVAL;memset(rec,0,sizeof(*rec));rec->pidfd=-1;rec->role=role;
 struct fr_cap c;if(cap_get(&c)||role>=3||fr_sealed(config,0)||fr_now()>=c.work_ns||(c.mode==FR_BENIGN?gate<0:gate>=0))return -EPERM;
 uint64_t end=fr_now()+15000000000ull;if(end>c.work_ns)end=c.work_ns;if(end>scheduler_end)end=scheduler_end;if(end<=fr_now())return -ETIMEDOUT;
 struct fr_packet q;packet(&q,&c,FR_INTENT,role);if(!q.owner_birth)return -ESTALE;
 int r=fr_send(sock,&q,-1,end);if(r)return r;r=ack(sock,&q,FR_INTENT_ACK,end);if(r)return r;
 int barrier[2];if(pipe2(barrier,O_CLOEXEC)){int saved=errno;return abort_creation(sock,&q,saved,end,rec);}
 int pidfd=-1;struct clone_args a={0};a.flags=CLONE_PIDFD;a.pidfd=(uintptr_t)&pidfd;a.exit_signal=SIGCHLD;
 pid_t pid=syscall(SYS_clone3,&a,sizeof(a));
 if(pid>0){rec->pid=pid;rec->pidfd=pidfd;rec->state=FR_CREATED;}
 if(pid<0){int saved=errno;close(barrier[0]);close(barrier[1]);return abort_creation(sock,&q,saved,end,rec);}
 if(pid==0){close(barrier[1]);struct pollfd p={barrier[0],POLLIN,0};int released=0;while(fr_now()<end){int n=poll(&p,1,10);if(n>0){unsigned char b=0;if(read(barrier[0],&b,1)==1&&b==0xa5)released=1;break;}if(n<0&&errno!=EINTR)break;}close(barrier[0]);if(!released)_exit(124);
  if(fr_limits(FR_WORKER,330)||prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0))_exit(125);
  close_except(body,event,config,python,gate);
  char*argv[]={"/usr/bin/python3.14","-I","-S","-B","-c",
   "import os,sys;d=os.pread(112,1048577,0);exec(compile(d,'/var/tmp/friday-astra-browser-whole-control-native-a061-g1/A061-WORKER.py','exec'),{'__name__':'__main__','__file__':'/var/tmp/friday-astra-browser-whole-control-native-a061-g1/A061-WORKER.py'})",0};
  char*env[]={"PATH=/usr/bin:/bin","LANG=C","LC_ALL=C",0};fexecve(130,argv,env);_exit(125);
 }
 close(barrier[0]);int parent=0;r=fr_proc(pid,&parent,&rec->birth);
 if(r||parent!=getpid()||pidfd<0||fr_pidfd_pid(pidfd)!=pid){close(barrier[1]);return r?r:-ESTALE;}
 q.type=FR_REGISTER;q.pid=pid;q.birth=rec->birth;r=fr_send(sock,&q,pidfd,end);
 if(!r)r=ack(sock,&q,FR_REGISTER_ACK,end);
 if(!r){rec->state=FR_REGISTERED;unsigned char release=0xa5;if(write(barrier[1],&release,1)!=1)r=-EIO;}
 close(barrier[1]);return r;
}
int fr_own_wait(int sock,struct fr_child*rec,int nonblock){
 if(!rec||rec->pid<=0||rec->state==FR_EMPTY)return -EINVAL;if(rec->state==FR_UNKNOWN)return -EUCLEAN;if(rec->state==FR_REAPED)return 1;
 int status=0;pid_t p=waitpid(rec->pid,&status,nonblock?WNOHANG:WNOHANG);if(p==0)return 0;if(p<0){rec->state=FR_UNKNOWN;return -errno;}
 rec->status=status;rec->state=FR_REAPED;
 struct fr_cap c;if(cap_get(&c))return -EPERM;struct fr_packet q;packet(&q,&c,FR_REAP,rec->role);q.pid=rec->pid;q.birth=rec->birth;q.status=status;
 uint64_t end=fr_now()+1000000000ull;if(end>c.hard_ns-1000000000ull)end=c.hard_ns-1000000000ull;
 int r=fr_send(sock,&q,-1,end);if(r){rec->state=FR_UNKNOWN;return r;}
 /* REAP_ACK status echoes the actual wait status; this is not a claimed reap. */
 struct fr_packet reply;int pass=-1,pid=-1,uid=-1,gid=-1;r=fr_recv(sock,&reply,&pass,&pid,&uid,&gid,end);if(pass>=0)close(pass);
 if(r||pass>=0||pid!=getppid()||uid||gid||memcmp(reply.magic,q.magic,8)||memcmp(reply.session,q.session,32)||reply.version!=1||reply.type!=FR_REAP_ACK||reply.sequence!=q.sequence||reply.role!=q.role||reply.pid!=q.pid||reply.owner!=q.owner||reply.birth!=q.birth||reply.owner_birth!=q.owner_birth||reply.status!=q.status||reply.detail!=q.detail||reply.deadline_ns!=q.deadline_ns){rec->state=FR_UNKNOWN;return r?r:-EBADMSG;}
 return 1;
}
int fr_own_stop(int sock,struct fr_child*rec,uint64_t end){
 if(!rec||rec->pid<=0)return -EINVAL;if(rec->state==FR_UNKNOWN)return -EUCLEAN;if(rec->state==FR_REAPED)return 1;
 int r=fr_own_wait(sock,rec,1);if(r)return r;
 if(rec->pidfd>=0){if(syscall(SYS_pidfd_send_signal,rec->pidfd,SIGKILL,0,0)&&errno!=ESRCH)return -errno;}
 else { /* Exact unreaped direct clone intention is the sole numeric fallback. */
  int status;pid_t p=waitpid(rec->pid,&status,WNOHANG);if(p<0){rec->state=FR_UNKNOWN;return -errno;}if(p>0){rec->status=status;rec->state=FR_REAPED;return 1;}if(kill(rec->pid,SIGKILL)&&errno!=ESRCH)return -errno;
 }
 while(fr_now()<end){r=fr_own_wait(sock,rec,1);if(r)return r;poll(0,0,5);}rec->state=FR_UNKNOWN;return -ETIMEDOUT;
}
static struct fr_child fixture_children[512];
static unsigned fixture_count;
int fr_fixture_fork(int sock){
 struct fr_cap c;if(cap_get(&c)||c.mode!=FR_CONTROLS||fixture_count>=512||fr_now()>=c.work_ns)return -EPERM;
 unsigned role=fixture_count;struct fr_child*rec=&fixture_children[role];memset(rec,0,sizeof(*rec));rec->pidfd=-1;rec->role=role;
 uint64_t end=fr_now()+15000000000ull;if(end>c.work_ns)end=c.work_ns;
 struct fr_packet q;packet(&q,&c,FR_INTENT,role);if(!q.owner_birth)return -ESTALE;
 int r=fr_send(sock,&q,-1,end);if(r)return r;r=ack(sock,&q,FR_INTENT_ACK,end);if(r)return r;
 int barrier[2];if(pipe2(barrier,O_CLOEXEC)){int saved=errno;return abort_creation(sock,&q,saved,end,rec);}
 int pidfd=-1;struct clone_args a={0};a.flags=CLONE_PIDFD;a.pidfd=(uintptr_t)&pidfd;a.exit_signal=SIGCHLD;
 PyOS_BeforeFork();pid_t pid=syscall(SYS_clone3,&a,sizeof(a));
 if(pid>0){rec->pid=pid;rec->pidfd=pidfd;rec->state=FR_CREATED;fixture_count++;}
 if(pid!=0)PyOS_AfterFork_Parent();
 if(pid<0){int saved=errno;close(barrier[0]);close(barrier[1]);return abort_creation(sock,&q,saved,end,rec);}
 if(pid==0){close(barrier[1]);struct pollfd p={barrier[0],POLLIN,0};int released=0;while(fr_now()<end){int n=poll(&p,1,10);if(n>0){unsigned char b=0;released=read(barrier[0],&b,1)==1&&b==0xa5;break;}if(n<0&&errno!=EINTR)break;}close(barrier[0]);if(!released)_exit(124);PyOS_AfterFork_Child();return 0;}
 close(barrier[0]);int parent;r=fr_proc(pid,&parent,&rec->birth);
 if(r||parent!=getpid()||pidfd<0||fr_pidfd_pid(pidfd)!=pid){close(barrier[1]);return r?r:-ESTALE;}
 q.type=FR_REGISTER;q.pid=pid;q.birth=rec->birth;r=fr_send(sock,&q,pidfd,end);if(!r)r=ack(sock,&q,FR_REGISTER_ACK,end);
 if(!r){rec->state=FR_REGISTERED;unsigned char release=0xa5;if(write(barrier[1],&release,1)!=1)r=-EIO;}
 close(barrier[1]);if(r){/* The exact native intention survives until finite disposal. */
  uint64_t stop=fr_now()+1000000000ull;if(stop>c.hard_ns-1000000000ull)stop=c.hard_ns-1000000000ull;fr_own_stop(sock,rec,stop);return r;}
 return pid;
}
int fr_fixture_reap(int sock,int pid,int status){
 struct fr_child*rec=0;for(unsigned i=0;i<fixture_count;i++)if(fixture_children[i].pid==pid&&fixture_children[i].state!=FR_REAPED){rec=&fixture_children[i];break;}
 if(!rec||rec->pidfd<0||fr_pidfd_pid(rec->pidfd)!=-1)return -ECHILD;
 rec->status=status;rec->state=FR_REAPED;struct fr_cap c;if(cap_get(&c))return -EPERM;
 struct fr_packet q;packet(&q,&c,FR_REAP,rec->role);q.pid=pid;q.birth=rec->birth;q.status=status;
 uint64_t end=fr_now()+1000000000ull;if(end>c.hard_ns-1000000000ull)end=c.hard_ns-1000000000ull;
 int r=fr_send(sock,&q,-1,end);struct fr_packet p;int pass=-1,owner=-1,uid=-1,gid=-1;
 if(!r)r=fr_recv(sock,&p,&pass,&owner,&uid,&gid,end);if(pass>=0)close(pass);
 if(!r&&(pass>=0||owner!=getppid()||uid||gid||p.type!=FR_REAP_ACK||memcmp(p.magic,q.magic,8)||memcmp(p.session,q.session,32)||p.sequence!=q.sequence||p.role!=q.role||p.pid!=pid||p.birth!=rec->birth||p.owner!=q.owner||p.owner_birth!=q.owner_birth||p.status!=status||p.deadline_ns!=q.deadline_ns))r=-EBADMSG;
 close(rec->pidfd);rec->pidfd=-1;return r;
}
int fr_public_command(unsigned kind){
 struct fr_cap c;if(cap_get(&c)||(kind!=FR_DRAIN&&kind!=FR_FINISH))return -EPERM;
 struct fr_packet q;packet(&q,&c,kind,0);uint64_t end=c.hard_ns-1000000000ull;
 int r=fr_send(123,&q,-1,end);return r?r:ack(123,&q,kind==FR_DRAIN?FR_DRAIN_ACK:FR_FINISH_ACK,end);
}
