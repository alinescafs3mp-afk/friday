/* SOURCE ONLY: separately reviewed static x86_64 Linux owner. No setuid bit,
 * capability grant, provisioning, named generated exec, or public command seam. */
#include "A071-NATIVE.h"
#include <linux/sched.h>
#include <linux/magic.h>
#include <linux/capability.h>
#include <linux/securebits.h>
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
static void fail(const char*s,int unknown){if(!failure)failure=s;if(unknown)sticky=1;}
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
static uint64_t rss_pid(int pid,int*ok){char path[64],b[16384];snprintf(path,sizeof(path),"/proc/%d/status",pid);int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0){*ok=0;return 0;}ssize_t n=read(fd,b,sizeof(b)-1);close(fd);if(n<=0||n==(ssize_t)sizeof(b)-1){*ok=0;return 0;}b[n]=0;char*x=strstr(b,"\nVmRSS:");if(x){unsigned long long k;char unit[8];if(sscanf(x+8,"%llu %7s",&k,unit)==2&&!strcmp(unit,"kB"))return k*1024;}if(strstr(b,"\nState:\tZ"))return 0;*ok=0;return 0;}
static void stop_known(void){for(unsigned i=0;i<used;i++){struct owned*r=&records[i];if(r->reaped||r->pid<=0||stop_seen[i])continue;stop_seen[i]=1;if(r->pidfd>=0){
 int seen=fr_pidfd_pid(r->pidfd);if(seen==-1)continue;int parent;uint64_t birth;
 if(seen!=r->pid||fr_proc(r->pid,&parent,&birth)||birth!=r->birth||(parent!=getpid()&&(i==0||parent!=records[0].pid))){fail("OWNED_SIGNAL_GENERATION",1);continue;}
 if(syscall(SYS_pidfd_send_signal,r->pidfd,SIGKILL,0,0)&&errno!=ESRCH)fail("OWNED_SIGNAL_FAILED",1);
 }else if(i==0){int status;struct rusage usage;pid_t p=wait4(r->pid,&status,WNOHANG,&usage);if(p==r->pid){r->reaped=1;r->status=status;r->kernel_status_known=1;}else if(p==0){if(kill(r->pid,SIGKILL)&&errno!=ESRCH)fail("OWNED_SIGNAL_FAILED",1);}else fail("OWNED_WAIT_CUSTODY_LOST",1);}else fail("OWNED_HANDLE_MISSING",1);}}
static void reap_owned(void){for(unsigned i=0;i<used;i++){struct owned*r=&records[i];if(r->reaped||r->pid<=0)continue;int status;struct rusage usage;pid_t p=wait4(r->pid,&status,WNOHANG,&usage);if(p==r->pid){r->reaped=1;r->status=status;r->kernel_status_known=1;}else if(p<0&&errno!=ECHILD)fail("OWNED_WAIT_FAILED",1);else if(p<0&&i==0)fail("OWNED_WAIT_CUSTODY_LOST",1);/* ECHILD and caller labels grant no kernel status credit. */}}
static int send_ack(int sock,struct fr_packet*q,unsigned type,uint64_t end){q->type=type;int r=fr_send(sock,q,-1,end);if(!r){if(registry_sequence==UINT32_MAX)return -EOVERFLOW;registry_sequence++;}return r;}
static int registry(int sock,const struct fr_cap*c,int*pending,uint64_t*pending_end,int*finished,int*draining){struct fr_packet q;int pass=-1,pid=-1,uid=-1,gid=-1;int r=fr_recv(sock,&q,&pass,&pid,&uid,&gid,fr_now()+10000000ull);if(r)return r;
 const char*cause=0;int idx=-1,parent;uint64_t birth;unsigned max=c->mode==FR_CONTROLS?512:3;
 if(failure){cause="REG_GENERATION_ALREADY_POISONED";goto out;}
 if(memcmp(q.magic,"FRA061P1",8)||memcmp(q.session,c->session,32)||q.version!=1||q.deadline_ns!=c->work_ns||q.role>=max||q.sequence!=registry_sequence||pid!=records[0].pid||uid!=(int)c->uid||gid!=(int)c->gid||q.owner!=records[0].pid||q.owner_birth!=records[0].birth){cause="REG_SESSION_CREDENTIAL_GENERATION";goto out;}
 if(records[0].reaped||fr_proc(pid,&parent,&birth)||parent!=getpid()||birth!=records[0].birth){cause="REG_SESSION_CREDENTIAL_GENERATION";goto out;}
 idx=find_role(q.role);
 if(q.type==FR_INTENT){if(pass>=0||q.pid||q.birth||q.status||q.detail||idx>=0||*pending>=0||used>=513||*draining||fr_now()>=c->work_ns){cause="REG_INTENT_STATE";goto out;}*pending=q.role;*pending_end=fr_now()+15000000000ull;if(*pending_end>c->work_ns)*pending_end=c->work_ns;if(send_ack(sock,&q,FR_INTENT_ACK,c->work_ns))cause="REG_INTENT_ACK";}
 else if(q.type==FR_REGISTER){if(*pending!=(int)q.role||fr_now()>=*pending_end||idx>=0||pass<0||q.pid<=0||q.status||q.detail||fr_pidfd_pid(pass)!=q.pid){cause="REG_PIDFD_IDENTITY";goto out;}if(fr_proc(q.pid,&parent,&birth)||parent!=q.owner||birth!=q.birth){cause="REG_PARENT_START_GENERATION";goto out;}
  for(unsigned i=0;i<used;i++)if(records[i].pid==q.pid){cause="REG_PIDFD_IDENTITY";goto out;}
  records[used]=(struct owned){q.pid,pass,0,0,1,(int)q.role,q.birth};pass=-1;used++;started_workers++;*pending=-1;if(send_ack(sock,&q,FR_REGISTER_ACK,c->work_ns))cause="REG_ACK_UNCONFIRMED";
 }
 else if(q.type==FR_ABORT){if(pass>=0||*pending!=(int)q.role||fr_now()>=*pending_end||q.pid||q.birth||q.status||q.detail<=0||membership(121,0)){cause="REG_ABORT_CREATION_UNKNOWN";goto out;}*pending=-1;if(send_ack(sock,&q,FR_ABORT_ACK,c->work_ns))cause="REG_ABORT_ACK_UNCONFIRMED";}
 else if(q.type==FR_REAP){if(pass>=0||idx<0||records[idx].reaped||q.detail||q.status<0||q.status>65535||(!WIFEXITED(q.status)&&!WIFSIGNALED(q.status))||q.pid!=records[idx].pid||q.birth!=records[idx].birth||fr_pidfd_pid(records[idx].pidfd)!=-1){cause="REG_REAP_KERNEL_UNCONFIRMED";goto out;}
  if(send_ack(sock,&q,FR_REAP_ACK,c->hard_ns-1000000000ull))cause="REG_REAP_ACK_UNCONFIRMED";
  else {records[idx].reaped=1;/* The borrowed native receipt is not Root wait4. */records[idx].status=0;records[idx].kernel_status_known=0;}}
 else if(q.type==FR_DRAIN){if(pass>=0||q.pid||q.birth||q.status||q.detail||*pending>=0||*draining||fr_now()>=c->work_ns){cause="REG_DRAIN_STATE";goto out;}for(unsigned i=1;i<used;i++)if(!records[i].reaped){cause="REG_DRAIN_LIVE";goto out;}*draining=1;if(send_ack(sock,&q,FR_DRAIN_ACK,c->hard_ns-1000000000ull))cause="REG_DRAIN_ACK";}
 else if(q.type==FR_FINISH){if(pass>=0||q.pid||q.birth||q.status||q.detail||*pending>=0||!*draining){cause="REG_FINISH_STATE";goto out;}for(unsigned i=1;i<used;i++)if(!records[i].reaped){cause="REG_FINISH_LIVE";goto out;}if((c->mode==FR_EXECUTE||c->mode==FR_BENIGN)&&started_workers!=3){cause="REG_EXACT3";goto out;}if(c->mode==FR_PREFLIGHT&&started_workers){cause="REG_PREFLIGHT_EFFECT";goto out;}*finished=1;if(send_ack(sock,&q,FR_FINISH_ACK,c->hard_ns-1000000000ull))cause="REG_FINISH_ACK";}
 else cause="REG_PACKET_TYPE";
 out:if(pass>=0&&close(pass)){if(!cause)cause="REG_RIGHT_CLOSE_UNCONFIRMED";}if(cause){fail(cause,1);return -EBADMSG;}return 0;}
static int write_bounded(int fd,const void*buf,size_t count,uint64_t end){const char*p=buf;fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)|O_NONBLOCK);while(count&&fr_now()<end){ssize_t n=write(fd,p,count);if(n>0){p+=n;count-=n;}else if(n<0&&(errno==EAGAIN||errno==EINTR)){struct pollfd f={fd,POLLOUT,0};poll(&f,1,5);}else return -EIO;}return count?-ETIMEDOUT:0;}
static int run(const struct fr_cap*c,const char*pin,int python){int out[2],err[2],reg[2],barrier[2];if(pipe2(out,O_CLOEXEC)||pipe2(err,O_CLOEXEC)||socketpair(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0,reg)||pipe2(barrier,O_CLOEXEC))return -errno;
 int on=1;if(setsockopt(reg[0],SOL_SOCKET,SO_PASSCRED,&on,sizeof(on))||setsockopt(reg[1],SOL_SOCKET,SO_PASSCRED,&on,sizeof(on)))return -errno;
 int pidfd=-1;struct clone_args a={0};a.flags=CLONE_PIDFD|CLONE_INTO_CGROUP;a.pidfd=(uintptr_t)&pidfd;a.cgroup=121;a.exit_signal=SIGCHLD;pid_t pid=syscall(SYS_clone3,&a,sizeof(a));
 if(pid>0){records[0].pid=pid;records[0].pidfd=pidfd;records[0].registered=1;records[0].role=-1;}
 if(pid<0)return -errno;
 if(pid==0){close(out[0]);close(err[0]);close(reg[0]);close(barrier[1]);struct pollfd p={barrier[0],POLLIN,0};int release=0;uint64_t end=fr_now()+15000000000ull;if(end>c->work_ns)end=c->work_ns;while(fr_now()<end){int n=poll(&p,1,10);if(n>0){unsigned char b=0;release=read(barrier[0],&b,1)==1&&b==0xa5;break;}if(n<0&&errno!=EINTR)break;}close(barrier[0]);if(!release)_exit(124);
  if(fr_limits(FR_COORD,(c->mode==FR_CONTROLS||c->mode==FR_BENIGN)?180:1200)||drop_credentials(c->uid,c->gid))_exit(125);
  int src[]={out[1],err[1],reg[1],python},dst[]={1,2,123,130},tmp[4];for(int i=0;i<4;i++){tmp[i]=fcntl(src[i],F_DUPFD_CLOEXEC,400);if(tmp[i]<0)_exit(125);}for(int i=0;i<4;i++){if(dup3(tmp[i],dst[i],0)<0)_exit(125);close(tmp[i]);}
  for(int fd=0;fd<512;fd++){int keep=fd==1||fd==2||fd==100||fd==111||fd==120||fd==121||fd==123||fd==130;for(unsigned i=0;i<FR_SOURCES;i++)if(fd==fr_source_fd[i])keep=1;if(keep){if(fcntl(fd,F_SETFD,0)<0)_exit(125);}else close(fd);}
  /* Root-controlled cgroup descriptors are O_PATH, never writable attach fds. */
  char*argv[]={"/usr/bin/python3.14","-I","-S","-B","-c","import os,sys,types;d=os.pread(103,1048577,0);m=types.ModuleType('a061_supervisor');m.__file__='/var/tmp/friday-astra-browser-native-authoritative-caller-a079-g1/A071-SUPERVISOR.py';sys.modules[m.__name__]=m;exec(compile(d,m.__file__,'exec'),m.__dict__);sys.exit(m.main())",(char*)pin,0};char*env[]={"PATH=/usr/bin:/bin","LANG=C","LC_ALL=C",0};fexecve(130,argv,env);_exit(125);
 }
 close(out[1]);close(err[1]);close(reg[1]);close(barrier[0]);int parent;if(fr_proc(pid,&parent,&records[0].birth)||parent!=getpid()||pidfd<0||fr_pidfd_pid(pidfd)!=pid)fail("COORD_CLONE_IDENTITY",1);
 struct fr_packet start;packet(&start,c,FR_START,0);start.pid=pid;start.birth=records[0].birth;start.owner=getpid();fr_proc(getpid(),&parent,&start.owner_birth);if(!failure&&fr_send(reg[0],&start,-1,c->work_ns))fail("COORD_START_PACKET",1);unsigned char release=0xa5;if(!failure&&write(barrier[1],&release,1)!=1)fail("COORD_START_BARRIER",1);close(barrier[1]);
 fcntl(out[0],F_SETFL,O_NONBLOCK);fcntl(err[0],F_SETFL,O_NONBLOCK);
 uint8_t*buffers[2]={calloc(1,1048577),calloc(1,1048577)};size_t counts[2]={0,0};int pipes[2]={out[0],err[0]},openpipes=2,pending=-1,finished=0,draining=0,stopped=0;uint64_t pending_end=0,peak=0,current_outer=0,current_inner=0;
 if(!buffers[0]||!buffers[1])fail("OUTER_ALLOCATION",1);
 while(fr_now()<c->hard_ns-2000000000ull){
  if(cancelled)fail("OWNER_STOP",0);if(fr_now()>=c->work_ns&&!draining)fail("WORK1140_TIMEOUT",0);if(pending>=0&&fr_now()>=pending_end)fail("REG_PENDING_CREATION_UNKNOWN",1);
  if(membership(121,pending>=0))fail("UNKNOWN_CGROUP_MEMBER",1);
  if(group_check(120,c->outer_dev,c->outer_ino,FR_OUTER,1,0)||group_check(121,c->inner_dev,c->inner_ino,FR_INNER,4,0)||value_at(120,"memory.current",&current_outer)||value_at(121,"memory.current",&current_inner)||current_outer+current_inner>FR_RSS)fail("KERNEL_MEMORY_ENVELOPE",1);
  int ok=1;uint64_t rss=rss_pid(getpid(),&ok);for(unsigned i=0;i<used;i++)if(!records[i].reaped){int seen=1;uint64_t value=rss_pid(records[i].pid,&seen);if(!seen&&records[i].pidfd>=0&&fr_pidfd_pid(records[i].pidfd)==-1)seen=1;rss+=value;if(!seen)ok=0;}if(!ok)fail("RAW_RSS_UNKNOWN",1);if(rss>peak)peak=rss;if(rss>FR_RSS)fail("AGGREGATE_RAW_RSS_CAP",1);
  if(failure&&!stopped){stop_known();stopped=1;}
  struct pollfd f[3]={{reg[0],POLLIN,0},{pipes[0],POLLIN,0},{pipes[1],POLLIN,0}};int polled=poll(f,3,5);if(polled<0&&errno!=EINTR)fail("OUTER_POLL",1);
  if(f[0].revents&POLLIN){if(registry(reg[0],c,&pending,&pending_end,&finished,&draining)&&!failure)fail("REG_TRANSPORT",1);}
  for(int i=0;i<2;i++)if(pipes[i]>=0&&(f[i+1].revents&(POLLIN|POLLHUP))){uint8_t b[65536];ssize_t n=read(pipes[i],b,sizeof(b));if(n>0){if(counts[i]+(size_t)n>1048576)fail("OUTER_PIPE_CAP",0);else if(buffers[i]){memcpy(buffers[i]+counts[i],b,n);counts[i]+=n;}}else if(n==0){close(pipes[i]);pipes[i]=-1;openpipes--;}else if(errno!=EAGAIN&&errno!=EINTR)fail("OUTER_DRAIN_UNCONFIRMED",1);}
  reap_owned();int all=1;for(unsigned i=0;i<used;i++)if(!records[i].reaped)all=0;
  if(records[0].reaped&&(!WIFEXITED(records[0].status)||WEXITSTATUS(records[0].status)))fail("COORD_EXIT",0);
  if(all&&!openpipes)break;
 }
 if(!failure){if(!finished)fail("MISSING_FINISH_REGISTRATION",1);if(pending>=0)fail("REG_PENDING_TERMINAL",1);for(unsigned i=0;i<used;i++)if(!records[i].reaped)fail("OWNED_REAP_UNCONFIRMED",1);if(openpipes)fail("OUTER_DRAIN_UNCONFIRMED",1);if(counts[1])fail("INNER_STDERR",0);int ps[16];if(list_members(121,ps)!=0)fail("INNER_CGROUP_NOT_EMPTY",1);if(!counts[0]||buffers[0][counts[0]-1]!='\n'||memchr(buffers[0],'\n',counts[0]-1))fail("PARTIAL_OR_MULTIPLE_TERMINAL",0);}
 if(failure)stop_known();uint64_t reap_end=fr_now()+500000000ull;if(reap_end>c->hard_ns-1500000000ull)reap_end=c->hard_ns-1500000000ull;while(fr_now()<reap_end){reap_owned();int complete=1;for(unsigned i=0;i<used;i++)if(!records[i].reaped)complete=0;if(complete)break;poll(0,0,5);}reap_owned();for(unsigned i=0;i<used;i++)if(!records[i].reaped)fail("OWNED_REAP_UNCONFIRMED",1);for(int i=0;i<2;i++)if(pipes[i]>=0&&close(pipes[i]))fail("OWNED_PIPE_CLOSE_UNCONFIRMED",1);if(close(reg[0]))fail("REG_CLOSE_UNCONFIRMED",1);
 for(unsigned i=0;i<used;i++)if(records[i].pidfd>=0){int fd=records[i].pidfd;records[i].pidfd=-1;if(close(fd))fail("OWNED_PIDFD_CLOSE_UNCONFIRMED",1);}
 uint8_t sha[32];struct fr_sha hash;fr_sha_init(&hash);if(buffers[0])fr_sha_update(&hash,buffers[0],counts[0]);fr_sha_end(&hash,sha);char hex[65];for(unsigned i=0;i<32;i++)snprintf(hex+2*i,3,"%02x",sha[i]);
 unsigned reaped_workers=0;for(unsigned i=1;i<used;i++)if(records[i].reaped)reaped_workers++;char head[2048];struct rusage raw;getrusage(RUSAGE_SELF,&raw);int n=snprintf(head,sizeof(head),"{\"state\":\"%s\",\"reason\":%s%s%s,\"body_complete\":false,\"acceptance_complete\":false,\"terminal_completion\":%s,\"uncertainty_sticky\":%s,\"registered_workers\":%u,\"reaped_workers\":%u,\"aggregate_raw_RSS_peak_bytes\":%llu,\"raw_self_peak_KiB\":%ld,\"outer_memory_current\":%llu,\"inner_memory_current\":%llu,\"inner_terminal_sha256\":\"%s\",\"inner_terminal\":",
  sticky?"STOP_UNCONFIRMED":failure?"OUTER_FAILED":"OUTER_BOUNDED_DRAINED_FINISHED",failure?"\"":"",failure?failure:"null",failure?"\"":"",failure?"false":"true",sticky?"true":"false",started_workers,reaped_workers,(unsigned long long)peak,raw.ru_maxrss,(unsigned long long)current_outer,(unsigned long long)current_inner,hex);
 int emitted=n>0&&(size_t)n<sizeof(head)&&!write_bounded(1,head,n,c->hard_ns-1000000000ull);
 if(emitted)emitted=!write_bounded(1,failure?"null":(char*)buffers[0],failure?4:counts[0]-1,c->hard_ns-1000000000ull);
 /* Mode1 bounded raw DATA is evidence only. It grants neither a native wait
  * receipt nor terminal success; failure/sticky remain unchanged. Hex framing
  * keeps the Root terminal valid even when the rejected inner JSON is invalid. */
 char observation[256];int no=snprintf(observation,sizeof(observation),",\"registry_next_sequence\":%u,\"coordinator_kernel_status_known\":%s,\"borrowed_status_kernel_credit\":false,\"native_inner_DATA_hex\":",registry_sequence,records[0].kernel_status_known?"true":"false");
 if(emitted)emitted=no>0&&(size_t)no<sizeof(observation)&&!write_bounded(1,observation,no,c->hard_ns-1000000000ull);
 if(emitted&&c->mode==FR_CONTROLS&&buffers[0]&&counts[0]&&counts[0]<=16384){
  static const char digits[]="0123456789abcdef";char encoded[32768];for(size_t i=0;i<counts[0];i++){encoded[2*i]=digits[buffers[0][i]>>4];encoded[2*i+1]=digits[buffers[0][i]&15];}
  emitted=!write_bounded(1,"\"",1,c->hard_ns-1000000000ull)&&!write_bounded(1,encoded,2*counts[0],c->hard_ns-1000000000ull)&&!write_bounded(1,"\"",1,c->hard_ns-1000000000ull);
 }else if(emitted)emitted=!write_bounded(1,"null",4,c->hard_ns-1000000000ull);
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
 struct stat st;if(fstat(1,&st)||!S_ISFIFO(st.st_mode)||fstat(122,&st)||!S_ISDIR(st.st_mode)||st.st_uid||st.st_gid||st.st_dev!=c.root_dev||st.st_ino!=c.root_ino)return pre_refused("PROTECTED_ROOT_HANDLE",-EPERM);
 r=group_check(120,c.outer_dev,c.outer_ino,FR_OUTER,1,0);if(!r)r=group_check(121,c.inner_dev,c.inner_ino,FR_INNER,4,1);if(r)return pre_refused("KERNEL_RESOURCE_ENVELOPE",r);
 int members[16];if(list_members(120,members)!=1||members[0]!=getpid())return pre_refused("OUTER_KERNEL_MEMBERSHIP",-EPERM);
 r=fr_image_verify(111,122,c.image_sha,c.manifest_sha,c.work_ns);if(r)return pre_refused("EXACT_RUNTIME_IMAGE",r);
 int python=fr_open_beneath(122,"/usr/bin/python3.14",O_RDONLY);if(python<0||fr_sealed(python,1))return pre_refused("HELD_INTERPRETER",python<0?python:-EPERM);
 /* chroot is scoped to independently supplied, verified protected runtime.
  * No namespace, cgroup, mount, image or authority is created by this launcher. */
 if(fchdir(122)||chroot(".")||chdir("/"))return pre_refused("KERNEL_RUNTIME_ROOT",-errno);
 return run(&c,argv[2],python)?2:0;
}
