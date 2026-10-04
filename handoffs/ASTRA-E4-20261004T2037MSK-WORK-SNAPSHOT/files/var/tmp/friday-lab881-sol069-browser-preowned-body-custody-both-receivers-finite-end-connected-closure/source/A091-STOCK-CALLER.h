/* Source-only ordinary public wire actor. BOTH ordinary positive and each
 * wrong-argument contour use fr_send/fr_recv_receipt on the SAME inherited
 * Root socket and the SAME registry() below. No credential ancillary data is
 * fabricated. UID/GID controls are actual separate Root-selected own child
 * actors, selected BEFORE this caller exists; no unprivileged UID flip.
 * This header adds no held runtime Source role and no permission/admission.
 */
struct stock_owned {
 int pid,handle,waited,kernel_status,closed,registered,ready,reap_ack;
 int gate[2],data[2];uint64_t birth,clone_ns,ack_ns,release_ns,ready_ns,wait_ns,reap_ack_ns,close_ns;
 struct stat handle_stat;unsigned body_size;char body[32];
};
/* These are the same static objects used by allocation, registration and
 * private reap below. The called live observer must see their declarations. */
static struct stock_owned stock_children[3];static unsigned stock_used;
static int stock_origin;static uint64_t stock_origin_birth,stock_owner_birth;
/* Observed while every worker remains behind its own G barrier. Values are
 * obtained from the actual native records, proc generations and held image. */
struct stock_live_member {int pid,parent,handle_pid,role;uint64_t birth;struct stat handle_stat;};
static struct stock_live_member stock_live[4];static uint64_t stock_all3_ns;
static struct stat stock_live_image;static char stock_live_image_sha[65],stock_live_argv_sha[65];
static size_t stock_live_argv_size;static char stock_live_argv[4096];
static int stock_same_stat(const struct stat*a,const struct stat*b){
 return a->st_dev==b->st_dev&&a->st_ino==b->st_ino&&a->st_mode==b->st_mode&&a->st_uid==b->st_uid&&a->st_gid==b->st_gid&&a->st_nlink==b->st_nlink&&a->st_size==b->st_size&&a->st_mtim.tv_sec==b->st_mtim.tv_sec&&a->st_mtim.tv_nsec==b->st_mtim.tv_nsec&&a->st_ctim.tv_sec==b->st_ctim.tv_sec&&a->st_ctim.tv_nsec==b->st_ctim.tv_nsec;
}
static void stock_hex(const uint8_t*b,char out[65]){for(unsigned i=0;i<32;i++)snprintf(out+2*i,3,"%02x",b[i]);}
static int stock_stat_json(char*b,size_t cap,const struct stat*s){
 return snprintf(b,cap,"[\"%llu\",\"%llu\",\"%u\",\"%u\",\"%u\",\"%llu\",\"%llu\",\"%llu\",\"%llu\"]",(unsigned long long)s->st_dev,(unsigned long long)s->st_ino,(unsigned)s->st_mode,s->st_uid,s->st_gid,(unsigned long long)s->st_nlink,(unsigned long long)s->st_size,(unsigned long long)s->st_mtim.tv_sec*1000000000ull+s->st_mtim.tv_nsec,(unsigned long long)s->st_ctim.tv_sec*1000000000ull+s->st_ctim.tv_nsec);
}
static int stock_live_binding(const struct fr_cap*c,unsigned slot,int pid,int handle,int parent,uint64_t birth,int role){
 char path[64],argv[4096],status[16384];int seen_parent=0;uint64_t seen_birth=0;
 struct stat hs,image,after,held;uint8_t sha[32];
 if(fr_pidfd_pid(handle)!=pid||fr_proc(pid,&seen_parent,&seen_birth)||seen_parent!=parent||seen_birth!=birth||fstat(handle,&hs))return -ESTALE;
 /* The unprivileged coordinator uses its already-held readable image FD.
  * Root separately inspects the actual protected exe/argv of all four. */
 int fd=-1,r=fr_sealed(110,1)||fstat(110,&held)||fr_hash_fd(110,16*1024*1024,sha)||memcmp(sha,c->pins[9],32)||fstat(110,&after)||!stock_same_stat(&held,&after)?-ESTALE:0;
 if(r)return r;image=held;ssize_t n=0;
 if(!slot){
  snprintf(path,sizeof(path),"/proc/%d/cmdline",pid);fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return -errno;
  n=read(fd,argv,sizeof(argv));if(close(fd))return -errno;if(n<=0||n==(ssize_t)sizeof(argv)||argv[n-1])return -EFBIG;
  stock_live_argv_size=n;memcpy(stock_live_argv,argv,n);struct fr_sha h;fr_sha_init(&h);fr_sha_update(&h,argv,n);fr_sha_end(&h,sha);stock_hex(sha,stock_live_argv_sha);stock_live_image=image;stock_hex(c->pins[9],stock_live_image_sha);
 }else if(!stock_same_stat(&image,&stock_live_image))return -ESTALE;
 snprintf(path,sizeof(path),"/proc/%d/status",pid);fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return -errno;
 n=read(fd,status,sizeof(status)-1);if(close(fd))return -errno;if(n<=0||n==(ssize_t)sizeof(status)-1)return -EFBIG;status[n]=0;
 char*u=strstr(status,"\nUid:\t"),*g=strstr(status,"\nGid:\t");unsigned ids[8];
 if(!u||!g||sscanf(u+6,"%u %u %u %u",ids,ids+1,ids+2,ids+3)!=4||sscanf(g+6,"%u %u %u %u",ids+4,ids+5,ids+6,ids+7)!=4)return -EBADMSG;
 for(unsigned j=0;j<8;j++)if(ids[j]!=(j<4?c->uid:c->gid))return -EPERM;
 if(fr_proc(pid,&seen_parent,&seen_birth)||seen_parent!=parent||seen_birth!=birth||fr_pidfd_pid(handle)!=pid||fstat(handle,&after)||!stock_same_stat(&hs,&after))return -ESTALE;
 stock_live[slot]=(struct stock_live_member){pid,parent,pid,role,birth,hs};return 0;
}
static int stock_observe_all3(const struct fr_cap*c){
 if(stock_used!=3||stock_all3_ns)return -EUCLEAN;
 int self=syscall(SYS_pidfd_open,getpid(),0);if(self<0)return -errno;
 int r=stock_live_binding(c,0,getpid(),self,stock_origin,stock_owner_birth,-1);
 if(close(self)&&!r)r=-errno;if(r)return r;
 for(unsigned i=0;i<3;i++){struct stock_owned*h=&stock_children[i];
  if(!h->registered||!h->ready||h->waited||h->closed)return -EUCLEAN;
  r=stock_live_binding(c,i+1,h->pid,h->handle,getpid(),h->birth,i);if(r)return r;
 }
 stock_all3_ns=fr_now();return stock_all3_ns<c->work_ns?0:-ETIMEDOUT;
}
static uint32_t stock_sequence=2;
static unsigned stock_send_count,stock_receive_count,stock_plain_count,stock_plain_closed;
static int stock_start_ok,stock_creation_errno,stock_signal_attempts,stock_signal_errno;
static uint64_t stock_intent_ack_ns,stock_last_send_ns;
static unsigned stock_last_send_type;static struct fr_evidence stock_ack_evidence;
static struct stat stock_socket_stat;static int stock_transport_closed,stock_transport_errno;
static unsigned stock_transport_attempts;static uint64_t stock_transport_close_ns;
/* One ordinary self-owned REGISTER operand. It is never a child record or
 * cleanup authority for Root; the missing pending state is checked first. */
static int stock_operand_fd=-1,stock_operand_pid,stock_operand_handle_pid=-1,stock_operand_close_errno;
static uint64_t stock_operand_birth;static unsigned stock_operand_opened,stock_operand_closed;
static void stock_packet(struct fr_packet*q,const struct fr_cap*c,unsigned type,unsigned role){
 memset(q,0,sizeof(*q));memcpy(q->magic,"FRA061P1",8);memcpy(q->session,c->session,32);
 q->version=1;q->type=type;q->sequence=stock_sequence;q->role=role;
 q->owner=getpid();q->owner_birth=stock_owner_birth;q->deadline_ns=c->work_ns;
}
static int stock_ack(int sock,const struct fr_packet*q,unsigned type,uint64_t end){
 struct fr_packet expected=*q,p;expected.type=type;struct fr_receive_receipt receipt;
 memset(&stock_ack_evidence,0,sizeof(stock_ack_evidence));stock_ack_evidence.version=1;stock_ack_evidence.phase=q->type;
 stock_ack_evidence.expected=expected;stock_ack_evidence.expected_pid=stock_origin;
 int pass=-1,pid=-1,uid=-1,gid=-1;int r=fr_recv_receipt(sock,&p,&pass,&pid,&uid,&gid,end,&receipt);
 stock_ack_evidence.observed=p;stock_ack_evidence.observed_pid=pid;stock_ack_evidence.observed_uid=uid;stock_ack_evidence.observed_gid=gid;
 stock_ack_evidence.rights=receipt.rights;stock_ack_evidence.rights_closed=receipt.closed;stock_ack_evidence.rights_close_errno=receipt.close_errno;
 if(pass>=0){if(close(pass)){stock_ack_evidence.rights_close_errno=errno;r=-errno;}else stock_ack_evidence.rights_closed++;}
 unsigned stage=r?FR_E_RECEIVE:pid!=stock_origin?FR_E_ORIGIN_PID:uid?FR_E_ORIGIN_UID:gid?FR_E_ORIGIN_GID:receipt.rights?FR_E_RIGHTS:
  memcmp(p.magic,expected.magic,8)||memcmp(p.session,expected.session,32)||p.version!=expected.version?FR_E_FRAME:p.type!=type?FR_E_TYPE:
  p.sequence!=expected.sequence?FR_E_SEQUENCE:p.role!=expected.role?FR_E_ROLE:p.pid!=expected.pid?FR_E_PID:p.owner!=expected.owner?FR_E_OWNER:
  p.birth!=expected.birth?FR_E_BIRTH:p.owner_birth!=expected.owner_birth?FR_E_OWNER_BIRTH:p.status!=expected.status||p.detail!=expected.detail?FR_E_STATUS:p.deadline_ns!=expected.deadline_ns?FR_E_DEADLINE:0;
 if(stage){stock_ack_evidence.stage=stage;stock_ack_evidence.primitive_errno=r?r:-EBADMSG;return stock_ack_evidence.primitive_errno;}
 stock_receive_count++;stock_sequence++;return 0;
}
static int stock_send(int sock,struct fr_packet*q,const int*fd,unsigned count,uint64_t end){
 stock_last_send_ns=fr_now();stock_last_send_type=q->type;int r=fr_send_rights(sock,q,fd,count,end);if(!r)stock_send_count++;return r;
}
static int stock_start(int sock,const struct fr_cap*c){
 struct stat st;struct ucred peer;socklen_t n=sizeof(peer);int parent=0;uint64_t birth=0;
 if(fr_proc(getpid(),&parent,&stock_owner_birth)||fr_proc(parent,&stock_origin,&stock_origin_birth))return -ESTALE;
 stock_origin=parent;
 if(fstat(sock,&st)||!S_ISSOCK(st.st_mode)||getsockopt(sock,SOL_SOCKET,SO_PEERCRED,&peer,&n)||n!=sizeof(peer)||peer.pid!=parent||peer.uid||peer.gid)return -EPERM;
 struct fr_packet p;struct fr_receive_receipt t;int fd=-1,pid=-1,uid=-1,gid=-1;
 int r=fr_recv_receipt(sock,&p,&fd,&pid,&uid,&gid,c->work_ns,&t);if(fd>=0){if(close(fd))return -errno;t.closed++;}
 if(r||t.rights||pid!=parent||uid||gid||memcmp(p.magic,"FRA061P1",8)||memcmp(p.session,c->session,32)||p.version!=1||p.type!=FR_START||p.sequence!=1||p.role||p.pid!=getpid()||p.owner!=parent||p.birth!=stock_owner_birth||p.owner_birth!=stock_origin_birth||p.deadline_ns!=c->work_ns||p.status||p.detail)return r?r:-EBADMSG;
 memset(&stock_ack_evidence,0,sizeof(stock_ack_evidence));stock_ack_evidence.version=1;stock_ack_evidence.phase=FR_START;
 stock_ack_evidence.expected=stock_ack_evidence.observed=p;stock_ack_evidence.expected_pid=parent;stock_ack_evidence.observed_pid=pid;
 stock_ack_evidence.observed_uid=uid;stock_ack_evidence.observed_gid=gid;
 stock_socket_stat=st;stock_start_ok=1;return 0;
}
static int stock_close_transport(int sock){
 if(stock_transport_closed)return 0;struct stat st;
 if(fstat(sock,&st)||st.st_dev!=stock_socket_stat.st_dev||st.st_ino!=stock_socket_stat.st_ino||!S_ISSOCK(st.st_mode))return -ESTALE;
 stock_transport_attempts++;
 if(close(sock)){stock_transport_errno=errno;return -errno;}
 stock_transport_closed=1;stock_transport_close_ns=fr_now();return 0;
}
static int stock_plain(void){
 int fd=syscall(SYS_memfd_create,"ordinary-own-a091-plaintext",MFD_CLOEXEC|MFD_ALLOW_SEALING);
 if(fd<0)return -errno;const char text[]="ordinary-owned-a091\n";
 if(write(fd,text,sizeof(text)-1)!=sizeof(text)-1||fcntl(fd,F_ADD_SEALS,FR_SEALS)<0){int saved=errno;close(fd);return -saved;}
 stock_plain_count++;return fd;
}
static int stock_dispose(struct stock_owned*h,uint64_t end){
 if(h->pid<=0)return 0;
 if(!h->waited){int status=0;struct rusage usage;pid_t waited=wait4(h->pid,&status,WNOHANG,&usage);
  if(waited==h->pid){h->waited=1;h->kernel_status=status;h->wait_ns=fr_now();}
  else if(waited<0)return -errno;
  else {int parent=0;uint64_t birth=0;struct stat st;
   if(h->handle<0||fstat(h->handle,&st)||st.st_dev!=h->handle_stat.st_dev||st.st_ino!=h->handle_stat.st_ino||fr_pidfd_pid(h->handle)!=h->pid||fr_proc(h->pid,&parent,&birth)||parent!=getpid()||birth!=h->birth)return -ESTALE;
   stock_signal_attempts++;
   if(syscall(SYS_pidfd_send_signal,h->handle,SIGKILL,0,0)&&errno!=ESRCH){stock_signal_errno=errno;return -errno;}
  }
 }
 while(!h->waited&&fr_now()<end){int status=0;struct rusage usage;pid_t waited=wait4(h->pid,&status,WNOHANG,&usage);
  if(waited==h->pid){h->waited=1;h->kernel_status=status;h->wait_ns=fr_now();break;}
  if(waited<0)return -errno;poll(0,0,1);
 }
 if(!h->waited)return -ETIMEDOUT;
 if(!h->closed&&h->handle>=0){struct stat st;if(fstat(h->handle,&st)||st.st_dev!=h->handle_stat.st_dev||st.st_ino!=h->handle_stat.st_ino)return -ESTALE;
  if(close(h->handle))return -errno;h->closed=1;h->handle=-1;h->close_ns=fr_now();}
 return 0;
}
static int stock_intent(int sock,const struct fr_cap*c,unsigned role){
 struct fr_packet q;stock_packet(&q,c,FR_INTENT,role);
 if(!strcmp(control_case,"owner"))q.owner++;
 if(!strcmp(control_case,"owner_birth"))q.owner_birth++;
 if(!strcmp(control_case,"frame_session"))q.session[0]^=1;
 if(!strcmp(control_case,"version"))q.version++;
 if(!strcmp(control_case,"type"))q.type=FR_START;
 if(!strcmp(control_case,"role"))q.role=512;
 if(!strcmp(control_case,"sequence_replay"))q.sequence--;
 if(!strcmp(control_case,"sequence_future"))q.sequence++;
 if(!strcmp(control_case,"deadline"))q.deadline_ns--;
 int plain=-1;if(!strcmp(control_case,"intent_right")){plain=stock_plain();if(plain<0)return plain;}
 int r=stock_send(sock,&q,plain>=0?&plain:0,plain>=0,c->work_ns);
 if(plain>=0){if(close(plain))return -errno;stock_plain_closed++;}
 if(!r)r=stock_ack(sock,&q,FR_INTENT_ACK,c->work_ns);
 if(!r)stock_intent_ack_ns=fr_now();return r;
}
static int stock_allocate(int sock,const struct fr_cap*c,unsigned role,struct stock_owned*h){
 memset(h,0,sizeof(*h));h->handle=-1;h->gate[0]=h->gate[1]=h->data[0]=h->data[1]=-1;
 if(pipe2(h->gate,O_CLOEXEC)||pipe2(h->data,O_CLOEXEC))return -errno;
 struct clone_args a={0};a.flags=CLONE_PIDFD;a.pidfd=(uintptr_t)&h->handle;a.exit_signal=SIGCHLD;
 pid_t pid=syscall(SYS_clone3,&a,sizeof(a));int saved=pid<0?errno:0;
 if(pid>0){h->pid=pid;h->clone_ns=fr_now();stock_used++;if(fstat(h->handle,&h->handle_stat))return -errno;}
 if(pid<0)return -saved;
 if(pid==0){
  close(h->gate[1]);close(h->data[0]);unsigned char release=0;
  /* No worker retains another worker's private pipe or pidfd custody. */
  for(unsigned i=0;i<role;i++){struct stock_owned*prior=&stock_children[i];
   if(prior->handle>=0)close(prior->handle);
   for(unsigned j=0;j<2;j++){if(prior->gate[j]>=0)close(prior->gate[j]);if(prior->data[j]>=0)close(prior->data[j]);}
  }
  struct pollfd p={h->gate[0],POLLIN,0};int ready=0;
  while(fr_now()<c->work_ns){int n=poll(&p,1,5);if(n>0){ready=read(h->gate[0],&release,1)==1&&release==0xa5;break;}if(n<0&&errno!=EINTR)break;}
  if(!ready)_exit(124);
  uint64_t fields[4]={(uint64_t)getpid(),(uint64_t)getppid(),0,fr_now()};int parent=0;
  if(fr_proc(getpid(),&parent,&fields[2])||parent!=getppid()||write(h->data[1],fields,sizeof(fields))!=sizeof(fields))_exit(125);
  const char text[]="ordinary-owned-a091\n";if(write(h->data[1],text,sizeof(text)-1)!=sizeof(text)-1)_exit(125);
  if(!strcmp(control_case,"peer_pid")){struct fr_packet q;stock_packet(&q,c,FR_INTENT,1);q.sequence++;q.owner=getppid();q.owner_birth=stock_owner_birth;if(stock_send(sock,&q,0,0,c->work_ns))_exit(125);}
  /* Each ordinary child closes its inherited transport reference. Otherwise
   * closing the coordinator's endpoint could never produce Root EOF. */
  if(close(sock))_exit(125);
  while(fr_now()<c->work_ns){int n=poll(&p,1,5);if(n>0){int got=read(h->gate[0],&release,1);if(got==1&&release=='G')_exit(23+(int)role);if(got==0){
   if(!strcmp(control_case,"Root_signal_positive")||!strcmp(control_case,"Root_signal_denied")){uint64_t until=fr_now()+10000000000ull;if(until>c->work_ns)until=c->work_ns;while(fr_now()<until)poll(0,0,1);}
   _exit(124);
  }}if(n<0&&errno!=EINTR)_exit(125);}
  _exit(124);
 }
 close(h->gate[0]);h->gate[0]=-1;close(h->data[1]);h->data[1]=-1;
 int parent=0;if(h->handle<0||fr_pidfd_pid(h->handle)!=pid||fr_proc(pid,&parent,&h->birth)||parent!=getpid())return -ESTALE;
 return 0;
}
static int stock_register(int sock,const struct fr_cap*c,unsigned role,struct stock_owned*h){
 struct fr_packet q;stock_packet(&q,c,FR_REGISTER,role);q.pid=h->pid;q.birth=h->birth;
 int passes[2]={h->handle,h->handle},plain=-1,self=-1;unsigned count=1;
 if(!strcmp(control_case,"register_zero_rights"))count=0;
 if(!strcmp(control_case,"register_many_rights"))count=2;
 if(!strcmp(control_case,"register_plaintext")){plain=stock_plain();if(plain<0)return plain;passes[0]=plain;}
 if(!strcmp(control_case,"register_other_owned_pidfd")||!strcmp(control_case,"register_parent")){
  self=syscall(SYS_pidfd_open,getpid(),0);if(self<0)return -errno;passes[0]=self;
  if(!strcmp(control_case,"register_parent")){q.pid=getpid();q.birth=stock_owner_birth;}
 }
 if(!strcmp(control_case,"register_birth"))q.birth++;
 int r=stock_send(sock,&q,passes,count,c->work_ns);
 if(plain>=0){if(close(plain))return -errno;stock_plain_closed++;}
 if(self>=0&&close(self))return -errno;
 if(!r)r=stock_ack(sock,&q,FR_REGISTER_ACK,c->work_ns);
 if(!r){h->registered=1;h->ack_ns=fr_now();unsigned char release=0xa5;
  h->release_ns=fr_now();if(write(h->gate[1],&release,1)!=1)return -EIO;
  struct pollfd p={h->data[0],POLLIN,0};if(poll(&p,1,1000)!=1)return -ETIMEDOUT;
  uint64_t fields[4];if(read(h->data[0],fields,sizeof(fields))!=sizeof(fields)||fields[0]!=(uint64_t)h->pid||fields[1]!=(uint64_t)getpid()||fields[2]!=h->birth||fields[3]<h->ack_ns)return -ESTALE;
  const char text[]="ordinary-owned-a091\n";char b[sizeof(text)-1];if(read(h->data[0],b,sizeof(b))!=sizeof(b)||memcmp(b,text,sizeof(b)))return -EBADMSG;
  memcpy(h->body,b,sizeof(b));h->body_size=sizeof(b);h->ready=1;h->ready_ns=fields[3];
 }return r;
}
static int stock_reap(int sock,const struct fr_cap*c,unsigned role,struct stock_owned*h){
 int early=!strcmp(control_case,"reap_live");
 if(!early){unsigned char release='G';if(write(h->gate[1],&release,1)!=1)return -EIO;
  while(fr_now()<c->work_ns){int status=0;struct rusage usage;pid_t waited=wait4(h->pid,&status,WNOHANG,&usage);
   if(waited==h->pid){h->waited=1;h->kernel_status=status;h->wait_ns=fr_now();break;}if(waited<0)return -errno;poll(0,0,1);}
  if(!h->waited)return -ETIMEDOUT;
 }
 struct fr_packet q;stock_packet(&q,c,FR_REAP,role);q.pid=h->pid;q.birth=h->birth;q.status=early?0:h->kernel_status;
 if(!strcmp(control_case,"reap_birth"))q.birth++;
 if(!strcmp(control_case,"reap_pid"))q.pid=getpid();
 if(!strcmp(control_case,"reap_status"))q.status=65536;
 int plain=-1;if(!strcmp(control_case,"reap_right")){plain=stock_plain();if(plain<0)return plain;}
 int r=stock_send(sock,&q,plain>=0?&plain:0,plain>=0,c->hard_ns-2000000000ull);
 if(plain>=0){if(close(plain))return -errno;stock_plain_closed++;}
 if(!r)r=stock_ack(sock,&q,FR_REAP_ACK,c->hard_ns-2000000000ull);
 if(!r){h->reap_ack=1;h->reap_ack_ns=fr_now();if(h->wait_ns>=h->reap_ack_ns)return -ESTALE;}
 return r;
}
static int stock_command(int sock,const struct fr_cap*c,unsigned type){
 struct fr_packet q;stock_packet(&q,c,type,0);int r=stock_send(sock,&q,0,0,c->hard_ns-2000000000ull);
 return r?r:stock_ack(sock,&q,type==FR_DRAIN?FR_DRAIN_ACK:FR_FINISH_ACK,c->hard_ns-2000000000ull);
}
static int stock_creation_abort(int sock,const struct fr_cap*c){
 int r=stock_intent(sock,c,0);if(r)return r;
 int held[256],count=0;struct rlimit old,current;if(getrlimit(RLIMIT_NOFILE,&old))return -errno;
 current=old;current.rlim_cur=256;if(setrlimit(RLIMIT_NOFILE,&current))return -errno;
 while(count<256){int fd=fcntl(100,F_DUPFD_CLOEXEC,3);if(fd<0){if(errno!=EMFILE)return -errno;break;}held[count++]=fd;}
 if(!count)return -EUCLEAN;close(held[--count]);int p[2];int result=pipe2(p,O_CLOEXEC);stock_creation_errno=result<0?errno:0;
 if(result==0){close(p[0]);close(p[1]);return -EUCLEAN;}
 if(stock_creation_errno!=EMFILE)return -stock_creation_errno;
 struct fr_packet q;stock_packet(&q,c,FR_ABORT,0);q.detail=stock_creation_errno;
 if(!strcmp(control_case,"abort_detail"))q.detail=0;
 /* Restore only this caller's pre-existing soft allowance and close its
  * own fills BEFORE communication; the actual creation errno remains real. */
 for(int i=0;i<count;i++)if(close(held[i]))return -errno;
 if(setrlimit(RLIMIT_NOFILE,&old))return -errno;
 int plain=-1;if(!strcmp(control_case,"abort_right")){plain=stock_plain();if(plain<0)return plain;}
 r=stock_send(sock,&q,plain>=0?&plain:0,plain>=0,c->work_ns);
 if(plain>=0){if(close(plain))return -errno;stock_plain_closed++;}
 return r?r:stock_ack(sock,&q,FR_ABORT_ACK,c->work_ns);
}
static int stock_stale_register(int sock,const struct fr_cap*c){
 int r=stock_intent(sock,c,0);if(r)return r;
 struct stock_owned*h=&stock_children[0];r=stock_allocate(sock,c,0,h);if(r)return r;
 /* Actual own blocked child is killed/reaped privately while its exact
  * native pidfd remains OPEN. No EBADF sender failure substitutes receipt. */
 int parent=0;uint64_t birth=0;if(fr_pidfd_pid(h->handle)!=h->pid||fr_proc(h->pid,&parent,&birth)||parent!=getpid()||birth!=h->birth)return -ESTALE;
 if(syscall(SYS_pidfd_send_signal,h->handle,SIGKILL,0,0))return -errno;
 while(fr_now()<c->work_ns){int status=0;struct rusage u;pid_t waited=wait4(h->pid,&status,WNOHANG,&u);if(waited==h->pid){h->waited=1;h->kernel_status=status;h->wait_ns=fr_now();break;}if(waited<0)return -errno;poll(0,0,1);}
 if(!h->waited||fr_pidfd_pid(h->handle)!=-1)return -EUCLEAN;
 struct fr_packet q;stock_packet(&q,c,FR_REGISTER,0);q.pid=h->pid;q.birth=h->birth;
 r=stock_send(sock,&q,&h->handle,1,c->work_ns);return r?r:stock_ack(sock,&q,FR_REGISTER_ACK,c->work_ns);
}
static int stock_register_without_intent(int sock,const struct fr_cap*c){
 /* Do not create an out-of-intention cgroup member. This actual coordinator's
  * own pidfd is an ordinary wrong argument and leaves membership prerequisite
  * valid until registry() reaches its unchanged REG_PENDING predicate. */
 int parent=0;uint64_t birth=0;
 if(fr_proc(getpid(),&parent,&birth)||parent!=stock_origin||birth!=stock_owner_birth)return -ESTALE;
 stock_operand_fd=syscall(SYS_pidfd_open,getpid(),0);if(stock_operand_fd<0)return -errno;
 stock_operand_opened=1;stock_operand_pid=getpid();stock_operand_birth=birth;
 stock_operand_handle_pid=fr_pidfd_pid(stock_operand_fd);
 int r=stock_operand_handle_pid==stock_operand_pid?0:-ESTALE;
 struct fr_packet q;stock_packet(&q,c,FR_REGISTER,0);q.pid=stock_operand_pid;q.birth=stock_operand_birth;
 if(!r)r=stock_send(sock,&q,&stock_operand_fd,1,c->work_ns);
 if(close(stock_operand_fd)){stock_operand_close_errno=errno;return -errno;}
 stock_operand_fd=-1;stock_operand_closed=1;
 return r?r:stock_ack(sock,&q,FR_REGISTER_ACK,c->work_ns);
}
static int stock_program(int sock,const struct fr_cap*c){
 int r=stock_start(sock,c);if(r)return r;
 if(!strcmp(control_case,"transport_closed")){int closed=stock_close_transport(sock);return closed?closed:-EPIPE;}
 if(!strcmp(control_case,"finish_without_drain"))return stock_command(sock,c,FR_FINISH);
 if(!strcmp(control_case,"abort_without_intent")){struct fr_packet q;stock_packet(&q,c,FR_ABORT,0);q.detail=EMFILE;r=stock_send(sock,&q,0,0,c->work_ns);return r?r:stock_ack(sock,&q,FR_ABORT_ACK,c->work_ns);}
 if(!strcmp(control_case,"register_stale_pidfd"))return stock_stale_register(sock,c);
 if(!strcmp(control_case,"register_without_intent"))return stock_register_without_intent(sock,c);
 if(!strncmp(control_case,"abort_",6)){r=stock_creation_abort(sock,c);if(r||strcmp(control_case,"abort_positive"))return r;}
 if(!strcmp(control_case,"positive")){
  /* The same one Root-selected coordinator first establishes all three
   * registered READY workers. No private wait or disposal precedes this phase. */
  for(unsigned i=0;i<3;i++){r=stock_intent(sock,c,i);if(r)return r;r=stock_allocate(sock,c,i,&stock_children[i]);if(r)return r;r=stock_register(sock,c,i,&stock_children[i]);if(r)return r;}
  r=stock_observe_all3(c);if(r)return r;
  for(unsigned i=0;i<3;i++){r=stock_reap(sock,c,i,&stock_children[i]);if(r)return r;r=stock_dispose(&stock_children[i],c->hard_ns-2000000000ull);if(r)return r;}
  r=stock_command(sock,c,FR_DRAIN);return r?r:stock_command(sock,c,FR_FINISH);
 }
 unsigned workers=(!strcmp(control_case,"positive")||!strcmp(control_case,"abort_positive")||!strcmp(control_case,"Root_transport_close_denied"))?3:1;
 for(unsigned i=0;i<workers;i++){
  r=stock_intent(sock,c,i);if(r)return r;
  if(!strcmp(control_case,"pending_timeout")){
   struct fr_packet p;struct fr_receive_receipt t;int pass=-1,pid=-1,uid=-1,gid=-1;
   r=fr_recv_receipt(sock,&p,&pass,&pid,&uid,&gid,c->work_ns,&t);if(pass>=0)close(pass);return r?r:-EBADMSG;
  }
  if(!strcmp(control_case,"register_late")){uint64_t late=stock_intent_ack_ns+15010000000ull;while(fr_now()<late&&fr_now()<c->work_ns)poll(0,0,1);if(fr_now()>=c->work_ns)return -ETIMEDOUT;}
  if(!strcmp(control_case,"drain_pending"))return stock_command(sock,c,FR_DRAIN);
  struct stock_owned*h=&stock_children[i];r=stock_allocate(sock,c,i,h);if(r)return r;
  r=stock_register(sock,c,i,h);if(r)return r;
  if(!strcmp(control_case,"drain_live"))return stock_command(sock,c,FR_DRAIN);
  if(!strcmp(control_case,"peer_pid")){struct fr_packet p;struct fr_receive_receipt t;int pass=-1,pid=-1,uid=-1,gid=-1;r=fr_recv_receipt(sock,&p,&pass,&pid,&uid,&gid,c->work_ns,&t);if(pass>=0)close(pass);return r?r:-EBADMSG;}
  if(!strcmp(control_case,"Root_signal_positive")||!strcmp(control_case,"Root_signal_denied")){
   /* Persist the actual READY/REGISTER witness BEFORE Root's own signal can
    * kill this caller. It is inert pipe DATA, never a wait/status receipt. */
   char pre[1536];int n=snprintf(pre,sizeof(pre),"{\"schema\":\"friday.a091.pre-signal-receipt.v1\",\"case\":\"%s\",\"pid\":%d,\"parent\":%d,\"birth\":%llu,\"uid\":%u,\"gid\":%u,\"child_pid\":%d,\"child_birth\":%llu,\"clone_ns\":%llu,\"register_ack_ns\":%llu,\"release_ns\":%llu,\"ready_ns\":%llu,\"private_wait4\":false,\"status_known\":false,\"status\":0,\"body\":\"ordinary-owned-a091\\n\"}\n",control_case,getpid(),getppid(),(unsigned long long)stock_owner_birth,getuid(),getgid(),h->pid,(unsigned long long)h->birth,(unsigned long long)h->clone_ns,(unsigned long long)h->ack_ns,(unsigned long long)h->release_ns,(unsigned long long)h->ready_ns);
   if(n<=0||(size_t)n>=sizeof(pre)||write(1,pre,n)!=n)return -EIO;
   int closed=stock_close_transport(sock);if(closed)return closed;uint64_t until=fr_now()+5000000000ull;while(fr_now()<until&&fr_now()<c->work_ns)poll(0,0,1);return -EPIPE;
  }
  r=stock_reap(sock,c,i,h);if(r)return r;
  r=stock_dispose(h,c->hard_ns-2000000000ull);if(r)return r;
 }
 r=stock_command(sock,c,FR_DRAIN);return r?r:stock_command(sock,c,FR_FINISH);
}
static int stock_caller(int sock,const struct fr_cap*c){
 for(unsigned i=0;i<3;i++){stock_children[i].handle=-1;stock_children[i].gate[0]=stock_children[i].gate[1]=stock_children[i].data[0]=stock_children[i].data[1]=-1;}
 int result=stock_program(sock,c),cleanup=stock_operand_close_errno?-stock_operand_close_errno:0;
 uint64_t end=fr_now()+1500000000ull;if(end>c->hard_ns-2000000000ull)end=c->hard_ns-2000000000ull;
 for(unsigned i=0;i<3;i++){struct stock_owned*h=&stock_children[i];int r=stock_dispose(h,end);if(r&&!cleanup)cleanup=r;
  for(unsigned j=0;j<2;j++){if(h->gate[j]>=0){if(close(h->gate[j])){if(!cleanup)cleanup=-errno;}else h->gate[j]=-1;}if(h->data[j]>=0){if(close(h->data[j])){if(!cleanup)cleanup=-errno;}else h->data[j]=-1;}}
 }
 if(stock_start_ok){int r=stock_close_transport(sock);if(r&&!cleanup)cleanup=r;}
 for(unsigned i=0;i<stock_used;i++)if(stock_children[i].waited&&!stock_children[i].reap_ack){stock_ack_evidence.expected.status=stock_ack_evidence.observed.status=0;stock_ack_evidence.status_redacted=1;}
 char expected_hex[193],observed_hex[193];const unsigned char*ep=(const void*)&stock_ack_evidence.expected,*op=(const void*)&stock_ack_evidence.observed;
 for(unsigned i=0;i<96;i++){snprintf(expected_hex+2*i,3,"%02x",ep[i]);snprintf(observed_hex+2*i,3,"%02x",op[i]);}
 struct rusage self,children;struct rlimit as,cpu,files,core,nofile;
 if(getrusage(RUSAGE_SELF,&self)||getrusage(RUSAGE_CHILDREN,&children)||getrlimit(RLIMIT_AS,&as)||getrlimit(RLIMIT_CPU,&cpu)||getrlimit(RLIMIT_FSIZE,&files)||getrlimit(RLIMIT_CORE,&core)||getrlimit(RLIMIT_NOFILE,&nofile))return -errno;
 char b[8192];int n=snprintf(b,sizeof(b),"{\"schema\":\"friday.a091.stock-caller-receipt.v1\",\"case\":\"%s\",\"start_ok\":%s,\"uid\":%u,\"gid\":%u,\"pid\":%d,\"parent\":%d,\"birth\":%llu,\"origin_birth\":%llu,\"result\":%d,\"cleanup_errno\":%d,\"creation_errno\":%d,\"next_sequence\":%u,\"send_count\":%u,\"receive_count\":%u,\"intent_ack_ns\":%llu,\"last_send_ns\":%llu,\"last_send_type\":%u,\"plaintext_created\":%u,\"plaintext_closed\":%u,\"status_authority\":false,\"ack_evidence\":{\"version\":%u,\"phase\":%u,\"stage\":%u,\"primitive_errno\":%d,\"peer\":[%d,%d,%d],\"expected_peer\":[%d,0,0],\"rights\":%u,\"rights_closed\":%u,\"rights_close_errno\":%d,\"expected\":\"%s\",\"observed\":\"%s\",\"status_redacted\":%s},\"resources\":{\"self_peak_bytes\":%llu,\"children_peak_bytes\":%llu,\"AS\":[%llu,%llu],\"CPU\":[%llu,%llu],\"FSIZE\":[%llu,%llu],\"CORE\":[%llu,%llu],\"NOFILE\":[%llu,%llu]},\"rows\":[",control_case,stock_start_ok?"true":"false",getuid(),getgid(),getpid(),getppid(),(unsigned long long)stock_owner_birth,(unsigned long long)stock_origin_birth,result,cleanup,stock_creation_errno,stock_sequence,stock_send_count,stock_receive_count,(unsigned long long)stock_intent_ack_ns,(unsigned long long)stock_last_send_ns,stock_last_send_type,stock_plain_count,stock_plain_closed,stock_ack_evidence.version,stock_ack_evidence.phase,stock_ack_evidence.stage,stock_ack_evidence.primitive_errno,stock_ack_evidence.observed_pid,stock_ack_evidence.observed_uid,stock_ack_evidence.observed_gid,stock_ack_evidence.expected_pid,stock_ack_evidence.rights,stock_ack_evidence.rights_closed,stock_ack_evidence.rights_close_errno,expected_hex,observed_hex,stock_ack_evidence.status_redacted?"true":"false",(unsigned long long)self.ru_maxrss*1024,(unsigned long long)children.ru_maxrss*1024,(unsigned long long)as.rlim_cur,(unsigned long long)as.rlim_max,(unsigned long long)cpu.rlim_cur,(unsigned long long)cpu.rlim_max,(unsigned long long)files.rlim_cur,(unsigned long long)files.rlim_max,(unsigned long long)core.rlim_cur,(unsigned long long)core.rlim_max,(unsigned long long)nofile.rlim_cur,(unsigned long long)nofile.rlim_max);
 for(unsigned i=0;i<stock_used&&n>0&&(size_t)n<sizeof(b);i++){struct stock_owned*h=&stock_children[i];n+=snprintf(b+n,sizeof(b)-n,"%s{\"pid\":%d,\"birth\":%llu,\"registered\":%s,\"ready\":%s,\"private_wait4\":%s,\"reap_ack\":%s,\"status_known\":%s,\"status\":%d,\"handle_closed\":%s,\"clone_ns\":%llu,\"register_ack_ns\":%llu,\"release_ns\":%llu,\"ready_ns\":%llu,\"wait4_ns\":%llu,\"reap_ack_ns\":%llu,\"close_ns\":%llu,\"body_size\":%u,\"body_hex\":\"",i?",":"",h->pid,(unsigned long long)h->birth,h->registered?"true":"false",h->ready?"true":"false",h->waited?"true":"false",h->reap_ack?"true":"false",h->waited&&h->reap_ack?"true":"false",h->waited&&h->reap_ack?h->kernel_status:0,h->closed?"true":"false",(unsigned long long)h->clone_ns,(unsigned long long)h->ack_ns,(unsigned long long)h->release_ns,(unsigned long long)h->ready_ns,(unsigned long long)h->wait_ns,(unsigned long long)h->reap_ack_ns,(unsigned long long)h->close_ns,h->body_size);
  for(unsigned j=0;j<h->body_size&&n>0&&(size_t)n<sizeof(b);j++)n+=snprintf(b+n,sizeof(b)-n,"%02x",(unsigned char)h->body[j]);
  if(n>0&&(size_t)n<sizeof(b))n+=snprintf(b+n,sizeof(b)-n,"\"}");
 }
 if(n>0&&(size_t)n<sizeof(b))n+=snprintf(b+n,sizeof(b)-n,"],\"all3_live\":");
 if(stock_all3_ns&&n>0&&(size_t)n<sizeof(b)){
  char image_id[256];int ni=stock_stat_json(image_id,sizeof(image_id),&stock_live_image);if(ni<=0||(size_t)ni>=sizeof(image_id))return -EFBIG;
  n+=snprintf(b+n,sizeof(b)-n,"{\"observed_ns\":%llu,\"inner_processes\":4,\"uid\":%u,\"gid\":%u,\"same_held_image\":true,\"held_image_identity9\":%s,\"held_image_sha256\":\"%s\",\"own_argv_sha256\":\"%s\",\"own_argv_bytes\":%zu,\"members\":[",(unsigned long long)stock_all3_ns,getuid(),getgid(),image_id,stock_live_image_sha,stock_live_argv_sha,stock_live_argv_size);
  for(unsigned i=0;i<4&&n>0&&(size_t)n<sizeof(b);i++){struct stock_live_member*m=&stock_live[i];char id[256];int ni=stock_stat_json(id,sizeof(id),&m->handle_stat);if(ni<=0||(size_t)ni>=sizeof(id))return -EFBIG;
   n+=snprintf(b+n,sizeof(b)-n,"%s{\"slot\":%u,\"role\":%d,\"pid\":%d,\"parent\":%d,\"birth\":%llu,\"handle_pid\":%d,\"pidfd_identity9\":%s}",i?",":"",i,m->role,m->pid,m->parent,(unsigned long long)m->birth,m->handle_pid,id);
  }
  if(n>0&&(size_t)n<sizeof(b))n+=snprintf(b+n,sizeof(b)-n,"]}");
 }else if(n>0&&(size_t)n<sizeof(b))n+=snprintf(b+n,sizeof(b)-n,"null");
 if(n>0&&(size_t)n<sizeof(b))n+=snprintf(b+n,sizeof(b)-n,",\"self_register_operand\":{\"opened\":%u,\"closed\":%u,\"close_errno\":%d,\"retained\":%s,\"pid\":%d,\"birth\":%llu,\"handle_pid\":%d},\"transport\":{\"attempts\":%u,\"close_errno\":%d,\"closed\":%s,\"close_ns\":%llu}}\n",stock_operand_opened,stock_operand_closed,stock_operand_close_errno,stock_operand_fd>=0?"true":"false",stock_operand_pid,(unsigned long long)stock_operand_birth,stock_operand_handle_pid,stock_transport_attempts,stock_transport_errno,stock_transport_closed?"true":"false",(unsigned long long)stock_transport_close_ns);
 if(n>0&&(size_t)n<sizeof(b)){if(write(1,b,n)!=n)return -EIO;}else return -EFBIG;
 return cleanup?cleanup:result;
}
