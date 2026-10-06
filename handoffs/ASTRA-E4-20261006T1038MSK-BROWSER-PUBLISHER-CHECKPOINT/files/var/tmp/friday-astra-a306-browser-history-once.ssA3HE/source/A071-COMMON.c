#include "A071-NATIVE.h"
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/resource.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <sys/poll.h>
#include <linux/openat2.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <dirent.h>
#include <limits.h>
static const uint32_t K[64]={
 0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
 0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
 0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
 0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
 0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
 0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
 0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
 0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
static const char *primitive_stage="native_admission";
void fr_stage_set(const char *s){primitive_stage=s;}
const char *fr_stage_get(void){return primitive_stage;}
static uint32_t rr(uint32_t v,unsigned n){return (v>>n)|(v<<(32-n));}
static void block(struct fr_sha*s,const uint8_t*b){
 uint32_t w[64],a,c,d,e,f,g,h,t1,t2,bb;
 for(unsigned i=0;i<16;i++)w[i]=((uint32_t)b[4*i]<<24)|((uint32_t)b[4*i+1]<<16)|((uint32_t)b[4*i+2]<<8)|b[4*i+3];
 for(unsigned i=16;i<64;i++)w[i]=w[i-16]+(rr(w[i-15],7)^rr(w[i-15],18)^(w[i-15]>>3))+w[i-7]+(rr(w[i-2],17)^rr(w[i-2],19)^(w[i-2]>>10));
 a=s->h[0];bb=s->h[1];c=s->h[2];d=s->h[3];e=s->h[4];f=s->h[5];g=s->h[6];h=s->h[7];
 for(unsigned i=0;i<64;i++){t1=h+(rr(e,6)^rr(e,11)^rr(e,25))+((e&f)^((~e)&g))+K[i]+w[i];t2=(rr(a,2)^rr(a,13)^rr(a,22))+((a&bb)^(a&c)^(bb&c));h=g;g=f;f=e;e=d+t1;d=c;c=bb;bb=a;a=t1+t2;}
 s->h[0]+=a;s->h[1]+=bb;s->h[2]+=c;s->h[3]+=d;s->h[4]+=e;s->h[5]+=f;s->h[6]+=g;s->h[7]+=h;
}
void fr_sha_init(struct fr_sha*s){static const uint32_t iv[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};memset(s,0,sizeof(*s));memcpy(s->h,iv,sizeof(iv));}
void fr_sha_update(struct fr_sha*s,const void*p,size_t n){const uint8_t*b=p;s->bits+=(uint64_t)n*8;while(n){size_t k=64-s->used;if(k>n)k=n;memcpy(s->b+s->used,b,k);s->used+=k;b+=k;n-=k;if(s->used==64){block(s,s->b);s->used=0;}}}
void fr_sha_end(struct fr_sha*s,uint8_t out[32]){uint64_t bits=s->bits;uint8_t x=0x80,z=0;fr_sha_update(s,&x,1);while(s->used!=56)fr_sha_update(s,&z,1);uint8_t t[8];for(unsigned i=0;i<8;i++)t[7-i]=(uint8_t)(bits>>(8*i));fr_sha_update(s,t,8);for(unsigned i=0;i<8;i++)for(unsigned j=0;j<4;j++)out[4*i+j]=(uint8_t)(s->h[i]>>(24-8*j));}
uint64_t fr_now(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))return UINT64_MAX;return (uint64_t)t.tv_sec*1000000000ull+t.tv_nsec;}
int fr_hash_fd(int fd,uint64_t cap,uint8_t out[32]){
 struct stat a,b;uint8_t chunk[65536];struct fr_sha h;uint64_t off=0;
 if(fstat(fd,&a)||!S_ISREG(a.st_mode)||a.st_size<0||(uint64_t)a.st_size>cap)return -EFBIG;
 fr_sha_init(&h);while(off<(uint64_t)a.st_size){size_t n=(uint64_t)a.st_size-off;if(n>sizeof(chunk))n=sizeof(chunk);ssize_t got=pread(fd,chunk,n,off);if(got<=0)return -EIO;fr_sha_update(&h,chunk,got);off+=got;}
 if(fstat(fd,&b)||a.st_dev!=b.st_dev||a.st_ino!=b.st_ino||a.st_size!=b.st_size||a.st_mtim.tv_sec!=b.st_mtim.tv_sec||a.st_mtim.tv_nsec!=b.st_mtim.tv_nsec||a.st_ctim.tv_sec!=b.st_ctim.tv_sec||a.st_ctim.tv_nsec!=b.st_ctim.tv_nsec)return -ESTALE;
 fr_sha_end(&h,out);return 0;
}
int fr_sealed(int fd,int root){struct stat s;int seals=fcntl(fd,F_GET_SEALS);return fstat(fd,&s)||!S_ISREG(s.st_mode)||(root&&(s.st_uid||s.st_gid))||seals<0||((unsigned)seals&FR_SEALS)!=FR_SEALS?-EPERM:0;}
/* Prospective persistent error slots, not a last-error register or ring.
 * Successful calls reuse the uncommitted slot; every reached failing call
 * commits the actual first/cleanup pair, with process identity and operands. */
static _Thread_local struct fr_pair_history native_pairs;
static struct fr_pair_row *pair_begin(unsigned operation,int operand){
 native_pairs.version=1;
 if(native_pairs.count>=FR_PAIR_CAP){native_pairs.overflow=1;return 0;}
 struct fr_pair_row *r=&native_pairs.row[native_pairs.count];memset(r,0,sizeof(*r));
 r->operation=operation;r->sequence=native_pairs.count+1;r->owner_pid=getpid();r->operand=operand;r->opened_fd=-1;r->read_result=-1;return r;
}
static int pair_end(struct fr_pair_row*r,int result){
 r->final_result=result;
 if(r->open_errno||r->read_errno||r->close_errno||r->parse_result)native_pairs.count++;
 return result;
}
int fr_pair_export(struct fr_pair_history*out){if(!out)return -EINVAL;*out=native_pairs;out->version=1;return 0;}
int fr_evidence_bytes(void){return sizeof(struct fr_evidence);}
int fr_root_row_bytes(void){return sizeof(struct fr_root_row);}
int fr_root_row_codec_version(void){return 2;}
int fr_proc(int pid,int*parent,uint64_t*birth){
 struct fr_pair_row*r=pair_begin(1,pid);if(!r)return -EOVERFLOW;
 if(!parent||!birth){r->parse_result=-EINVAL;return pair_end(r,-EINVAL);}
 char p[64],b[4096];snprintf(p,sizeof(p),"/proc/%d/stat",pid);r->flags|=1;
 int fd=open(p,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);r->opened_fd=fd;
 if(fd<0){r->open_errno=errno;return pair_end(r,-r->open_errno);}r->flags|=2;
 r->flags|=4;ssize_t n=read(fd,b,sizeof(b)-1);r->read_result=n;r->read_errno=n<0?errno:0;
 int first=n<0?-r->read_errno:(n==0||n==(ssize_t)sizeof(b)-1?-EIO:0);
 if(first&&!r->read_errno)r->parse_result=first;
 r->flags|=8;r->close_result=close(fd);r->close_errno=r->close_result?errno:0;
 if(first)return pair_end(r,first);if(r->close_errno)return pair_end(r,-r->close_errno);
 r->flags|=16;b[n]=0;char*x=strrchr(b,')');
 if(!x||x[1]!=' '){r->parse_result=-EBADMSG;return pair_end(r,r->parse_result);}
 x+=2;char*save=0,*t=strtok_r(x," ",&save);unsigned field=3;*parent=0;*birth=0;
 while(t){char*end;errno=0;
  if(field==4){long v=strtol(t,&end,10);r->parse_errno=errno;if(errno||*end||v<=0||v>INT_MAX){r->parse_result=-EBADMSG;return pair_end(r,r->parse_result);}*parent=v;r->parent=*parent;}
  if(field==22){unsigned long long v=strtoull(t,&end,10);r->parse_errno=errno;if(errno||*end||!v){r->parse_result=-EBADMSG;return pair_end(r,r->parse_result);}*birth=v;r->birth=*birth;break;}
  field++;t=strtok_r(0," ",&save);
 }
 if(!*parent||!*birth){r->parse_result=-EBADMSG;return pair_end(r,r->parse_result);}
 r->flags|=32;r->parent=*parent;r->birth=*birth;return pair_end(r,0);
}
int fr_pidfd_pid(int fd){
 struct fr_pair_row*r=pair_begin(2,fd);if(!r)return -EOVERFLOW;
 char p[64],b[2048];snprintf(p,sizeof(p),"/proc/self/fdinfo/%d",fd);r->flags|=1;
 int f=open(p,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);r->opened_fd=f;
 if(f<0){r->open_errno=errno;return pair_end(r,-r->open_errno);}r->flags|=2;
 r->flags|=4;ssize_t n=read(f,b,sizeof(b)-1);r->read_result=n;r->read_errno=n<0?errno:0;
 int first=n<0?-r->read_errno:(n==0||n==(ssize_t)sizeof(b)-1?-EIO:0);
 if(first&&!r->read_errno)r->parse_result=first;
 r->flags|=8;r->close_result=close(f);r->close_errno=r->close_result?errno:0;
 if(first)return pair_end(r,first);if(r->close_errno)return pair_end(r,-r->close_errno);
 r->flags|=16;b[n]=0;char*t=strstr(b,"\nPid:\t");
 if(!t){r->parse_result=-EBADMSG;return pair_end(r,r->parse_result);}
 char*end;errno=0;long value=strtol(t+6,&end,10);r->parse_errno=errno;
 if(errno||(*end!='\n'&&*end!=' '&&*end!='\t')||value>INT_MAX||value< -1){r->parse_result=-EBADMSG;return pair_end(r,r->parse_result);}
 r->flags|=32;return pair_end(r,(int)value); /* -1 is valid DATA, not an errno. */
}
static int hex32(const char*s,uint8_t out[32]){if(!s||strlen(s)!=64)return -EINVAL;for(unsigned i=0;i<32;i++){unsigned v=0;for(unsigned j=0;j<2;j++){char c=s[2*i+j];if(c>='0'&&c<='9')v=v*16+c-'0';else if(c>='a'&&c<='f')v=v*16+c-'a'+10;else return -EINVAL;}out[i]=v;}return 0;}
int fr_cap_read(int fd,const char*sha,struct fr_cap*c){uint8_t wanted[32],seen[32];struct stat s;if(hex32(sha,wanted)||fr_sealed(fd,1)||fstat(fd,&s)||s.st_size!=sizeof(*c)||fr_hash_fd(fd,sizeof(*c),seen)||memcmp(seen,wanted,32)||pread(fd,c,sizeof(*c),0)!=sizeof(*c))return -EPERM;
 if(memcmp(c->magic,"FRA061C1",8)||c->version!=1||c->generation!=1||c->sources!=FR_SOURCES||c->uid!=1000||c->gid!=1000||c->mode<1||c->mode>4||!memcmp(c->session,(uint8_t[32]){0},32))return -EBADMSG;
 uint64_t wall=(c->mode==FR_CONTROLS||c->mode==FR_BENIGN)?180ull:1200ull,reserve=(wall==180)?10:60;
 if(c->start_ns>fr_now()||fr_now()>=c->work_ns||c->hard_ns-c->start_ns!=wall*1000000000ull||c->hard_ns-c->work_ns!=reserve*1000000000ull)return -ETIMEDOUT;return 0;}
int fr_limits(uint64_t as,unsigned cpu){struct {int k;rlim_t v;} rows[]={{RLIMIT_AS,as},{RLIMIT_CPU,cpu},{RLIMIT_FSIZE,2147483648ull},{RLIMIT_NOFILE,512},{RLIMIT_CORE,0},{RLIMIT_STACK,8*1024*1024}};for(unsigned i=0;i<sizeof(rows)/sizeof(rows[0]);i++){struct rlimit inherited;if(getrlimit(rows[i].k,&inherited))return -errno;if(rows[i].v>inherited.rlim_max){if(rows[i].k!=RLIMIT_CPU)return -EPERM;rows[i].v=inherited.rlim_max;}struct rlimit x={rows[i].v,rows[i].v},actual;if(setrlimit(rows[i].k,&x)||getrlimit(rows[i].k,&actual)||actual.rlim_cur!=x.rlim_cur||actual.rlim_max!=x.rlim_max)return -errno;}umask(077);return 0;}
static int ready(int fd,short events,uint64_t end){while(fr_now()<end){uint64_t left=end-fr_now();int ms=left/1000000;if(ms>10)ms=10;if(ms<1)ms=1;struct pollfd p={fd,events,0};int r=poll(&p,1,ms);if(r>0){if(p.revents&(POLLERR|POLLNVAL))return -EIO;if(p.revents&(events|POLLHUP))return 0;}if(r<0&&errno!=EINTR)return -errno;}return -ETIMEDOUT;}
int fr_send_rights(int fd,const struct fr_packet*p,const int*passes,unsigned count,uint64_t end){
 if(!p||count>2||(count&&!passes))return -EINVAL;
 int r=ready(fd,POLLOUT,end);if(r)return r;
 struct iovec io={(void*)p,sizeof(*p)};char control[CMSG_SPACE(sizeof(int)*2)]={0};struct msghdr m={0};m.msg_iov=&io;m.msg_iovlen=1;
 if(count){m.msg_control=control;m.msg_controllen=CMSG_SPACE(sizeof(int)*count);struct cmsghdr*c=CMSG_FIRSTHDR(&m);c->cmsg_level=SOL_SOCKET;c->cmsg_type=SCM_RIGHTS;c->cmsg_len=CMSG_LEN(sizeof(int)*count);memcpy(CMSG_DATA(c),passes,sizeof(int)*count);}
 ssize_t n=sendmsg(fd,&m,MSG_NOSIGNAL|MSG_DONTWAIT);return n==sizeof(*p)?0:n<0?-errno:-EIO;
}
int fr_send(int fd,const struct fr_packet*p,int pass,uint64_t end){return fr_send_rights(fd,p,pass>=0?&pass:0,pass>=0?1:0,end);}
int fr_receipt_close(int fd,struct fr_receive_receipt*t){
 if(!t)return -EINVAL;
 unsigned i;for(i=0;i<t->right_count;i++)if(t->right[i].fd==fd)break;
 if(i==t->right_count||i>=16)return -EINVAL;
 if(t->right[i].close_attempted)return -EALREADY;
 int order=1;for(unsigned k=0;k<t->right_count;k++)if(t->right[k].close_attempted>=order)order=t->right[k].close_attempted+1;
 t->right[i].close_attempted=order;t->right[i].close_result=close(fd);
 t->right[i].close_errno=t->right[i].close_result?errno:0;
 if(t->right[i].close_errno){t->right[i].disposition=3;if(!t->close_errno)t->close_errno=t->right[i].close_errno;return -t->right[i].close_errno;}
 t->right[i].disposition=2;t->closed++;return 0;
}
int fr_recv_receipt(int fd,struct fr_packet*p,int*pass,int*pid,int*uid,int*gid,uint64_t end,struct fr_receive_receipt*t){
 if(!p||!pass||!pid||!uid||!gid||!t)return -EINVAL;
 memset(t,0,sizeof(*t));memset(p,0,sizeof(*p));*pass=*pid=*uid=*gid=-1;
 t->control_capacity=sizeof(t->control);t->bytes=-1;
 int r=ready(fd,POLLIN,end);t->ready_result=r;t->complete=1;if(r)return r;
 struct iovec io={p,sizeof(*p)};struct msghdr m={0};m.msg_iov=&io;m.msg_iovlen=1;m.msg_control=t->control;m.msg_controllen=sizeof(t->control);
 t->recv_reached=1;ssize_t n=recvmsg(fd,&m,MSG_DONTWAIT|MSG_CMSG_CLOEXEC);t->bytes=n;
 if(n<0){t->recv_errno=errno;return -t->recv_errno;}t->flags=m.msg_flags;
 t->control_length=m.msg_controllen<=sizeof(t->control)?m.msg_controllen:sizeof(t->control);
 t->control_complete=m.msg_controllen<=sizeof(t->control)&&!(m.msg_flags&MSG_CTRUNC);
 int bad=n!=sizeof(*p)||(m.msg_flags&(MSG_TRUNC|MSG_CTRUNC));
 for(struct cmsghdr*c=CMSG_FIRSTHDR(&m);c;c=CMSG_NXTHDR(&m,c)){
  size_t at=(const uint8_t*)c-t->control;
  if(at>t->control_length||t->control_length-at<sizeof(*c)||c->cmsg_len<CMSG_LEN(0)||c->cmsg_len>t->control_length-at){bad=1;t->control_complete=0;break;}
  if(c->cmsg_level!=SOL_SOCKET){bad=1;continue;}
  if(c->cmsg_type==SCM_CREDENTIALS&&c->cmsg_len==CMSG_LEN(sizeof(struct ucred))){struct ucred v;memcpy(&v,CMSG_DATA(c),sizeof(v));*pid=v.pid;*uid=v.uid;*gid=v.gid;t->credentials++;}
  else if(c->cmsg_type==SCM_RIGHTS&&c->cmsg_len>=CMSG_LEN(0)){size_t bytes=c->cmsg_len-CMSG_LEN(0);if(bytes%sizeof(int))bad=1;size_t count=bytes/sizeof(int);int*v=(void*)CMSG_DATA(c);for(size_t i=0;i<count;i++){
   if(t->right_count>=16){bad=1;t->complete=0;break;}unsigned k=t->right_count++;
   t->right[k].fd=v[i];t->right[k].disposition=1;t->rights++;
   if(*pass<0)*pass=v[i];else fr_receipt_close(v[i],t);
  }}
  else bad=1;
 }
 if(t->credentials!=1||t->rights>1)bad=1;
 if(bad||t->close_errno){if(*pass>=0)fr_receipt_close(*pass,t);*pass=-1;return t->close_errno?-EIO:-EBADMSG;}
 return 0;
}
int fr_recv(int fd,struct fr_packet*p,int*pass,int*pid,int*uid,int*gid,uint64_t end){struct fr_receive_receipt t;return fr_recv_receipt(fd,p,pass,pid,uid,gid,end,&t);}
struct fr_history_row *fr_history_begin(struct fr_evidence*e){
 if(!e)return 0;e->history_version=1;
 if(e->history_count>=FR_HISTORY_CAP){e->history_overflow=1;return 0;}
 struct fr_history_row*r=&e->history[e->history_count++];memset(r,0,sizeof(*r));
 memset(&e->observed,0,sizeof(e->observed));e->observed_pid=e->observed_uid=e->observed_gid=-1;
 e->rights=e->rights_closed=0;e->rights_close_errno=0;
 memcpy(r->legacy,e,FR_EVIDENCE_LEGACY_BYTES);return r;
}
int fr_history_end(struct fr_evidence*e,struct fr_history_row*r,int result){
 if(!e||!r)return -EOVERFLOW;memcpy(r->legacy,e,FR_EVIDENCE_LEGACY_BYTES);r->result=result;r->complete=1;return result;
}
static int native_data_write(int fd,const void*data,size_t bytes,uint64_t end){
 int flags=fcntl(fd,F_GETFL);if(flags<0)return -errno;
 if(fcntl(fd,F_SETFL,flags|O_NONBLOCK)<0)return -errno;
 const char*p=data;while(bytes){int r=ready(fd,POLLOUT,end);if(r)return r;
  ssize_t n=write(fd,p,bytes);if(n>0){p+=n;bytes-=n;}else if(n<0&&(errno==EINTR||errno==EAGAIN))continue;else return n<0?-errno:-EIO;
 }return 0;
}
static int native_data_hex(int fd,const void*data,size_t bytes,uint64_t end){
 const uint8_t*p=data;char b[1024];static const char hex[]="0123456789abcdef";
 while(bytes){size_t n=bytes<sizeof(b)/2?bytes:sizeof(b)/2;for(size_t i=0;i<n;i++){b[2*i]=hex[p[i]>>4];b[2*i+1]=hex[p[i]&15];}
  int r=native_data_write(fd,b,2*n,end);if(r)return r;p+=n;bytes-=n;
 }return 0;
}
int fr_native_json(int fd,const struct fr_evidence*e,uint64_t end){
 char b[384];unsigned count=e?e->history_count:0;
 if(count>FR_HISTORY_CAP||native_pairs.count>FR_PAIR_CAP)return -EOVERFLOW;
 int n=snprintf(b,sizeof(b),"{\"version\":1,\"evidence_bytes\":344,\"receipt_bytes\":440,\"row_bytes\":792,\"capacity\":16,\"count\":%u,\"overflow\":%u,\"records\":[",count,e?e->history_overflow:0);
 if(n<=0||(size_t)n>=sizeof(b))return -EFBIG;int r=native_data_write(fd,b,n,end);if(r)return r;
 for(unsigned i=0;i<count;i++){r=native_data_write(fd,i?",\"":"\"",i?2:1,end);if(r)return r;r=native_data_hex(fd,&e->history[i],sizeof(e->history[i]),end);if(r)return r;r=native_data_write(fd,"\"",1,end);if(r)return r;}
 n=snprintf(b,sizeof(b),"],\"pairs\":{\"version\":1,\"row_bytes\":72,\"capacity\":512,\"count\":%u,\"overflow\":%u,\"records\":[",native_pairs.count,native_pairs.overflow);
 if(n<=0||(size_t)n>=sizeof(b))return -EFBIG;r=native_data_write(fd,b,n,end);if(r)return r;
 for(unsigned i=0;i<native_pairs.count;i++){r=native_data_write(fd,i?",\"":"\"",i?2:1,end);if(r)return r;r=native_data_hex(fd,&native_pairs.row[i],sizeof(native_pairs.row[i]),end);if(r)return r;r=native_data_write(fd,"\"",1,end);if(r)return r;}
 return native_data_write(fd,"]}}",3,end);
}
int fr_receipt_json(int fd,const struct fr_receive_receipt*t,uint64_t end){
 if(!t)return -EINVAL;int r=native_data_write(fd,"\"",1,end);if(r)return r;
 r=native_data_hex(fd,t,sizeof(*t),end);if(r)return r;return native_data_write(fd,"\"",1,end);
}
int fr_root_row_json(int fd,const struct fr_root_row*row,uint64_t end){
 if(!row)return -EINVAL;
 /* SOL147 v2: tag1 + exact159-bit presence map + canonical NONZERO ULEB32.
  * Absent bits reconstruct literal zero cells, not omitted DATA. If that
  * representation is not shorter, tag0 + ALL636 original bytes. The canonical
  * choice bounds one row to637 coded/852 base64 bytes for ANY bit pattern.
  * No semantic projection, dictionary/pointer/hash, malloc or cap increase. */
 uint8_t coded[816]={1};char text[852];size_t used=21,out=0;
 const uint8_t*raw=(const void*)row;
 for(size_t at=0;at<sizeof(*row);at+=4){
  uint32_t v=(uint32_t)raw[at]|(uint32_t)raw[at+1]<<8|
             (uint32_t)raw[at+2]<<16|(uint32_t)raw[at+3]<<24;
  if(!v)continue;
  unsigned cell=(unsigned)(at/4);coded[1+cell/8]|=(uint8_t)(1u<<(cell%8));
  do{uint8_t b=v&127;v>>=7;coded[used++]=b|(v?128:0);}while(v);
 }
 if(used>=1+sizeof(*row)){coded[0]=0;memcpy(coded+1,raw,sizeof(*row));used=1+sizeof(*row);}
 static const char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
 for(size_t at=0;at<used;at+=3){
  unsigned n=used-at<3?used-at:3;
  uint32_t v=(uint32_t)coded[at]<<16;
  if(n>1)v|=(uint32_t)coded[at+1]<<8;if(n>2)v|=coded[at+2];
  text[out++]=alphabet[v>>18];text[out++]=alphabet[(v>>12)&63];
  text[out++]=n>1?alphabet[(v>>6)&63]:'=';text[out++]=n>2?alphabet[v&63]:'=';
 }
 int r=native_data_write(fd,"\"",1,end);if(r)return r;
 r=native_data_write(fd,text,out,end);if(r)return r;return native_data_write(fd,"\"",1,end);
}
int fr_open_beneath(int root,const char*path,int flags){if(!path||path[0]!='/'||strlen(path)>=512)return -EINVAL;struct open_how h={.flags=(uint64_t)(flags|O_CLOEXEC),.resolve=RESOLVE_IN_ROOT|RESOLVE_NO_MAGICLINKS};int fd=syscall(SYS_openat2,root,path,&h,sizeof(h));return fd<0?-errno:fd;}
static int grammar(const char*p){if(p[0]!='/'||!memchr(p,0,512)||strlen(p)>511)return 0;if(!strcmp(p,"/"))return 1;const char*x=p+1;while(*x){const char*e=strchr(x,'/');size_t n=e?(size_t)(e-x):strlen(x);if(!n||(n==1&&*x=='.')||(n==2&&x[0]=='.'&&x[1]=='.'))return 0;for(size_t i=0;i<n;i++)if(!((x[i]>='a'&&x[i]<='z')||(x[i]>='A'&&x[i]<='Z')||(x[i]>='0'&&x[i]<='9')||strchr("_.+-",x[i])))return 0;if(!e)break;x=e+1;}return 1;}
static int child_named(struct fr_member*r,unsigned count,const char*parent,const char*name){char p[512];int n=snprintf(p,sizeof(p),!strcmp(parent,"/")?"/%s":"%s/%s",!strcmp(parent,"/")?name:parent,name);if(n<0||n>=512)return 0;for(unsigned i=0;i<count;i++)if(!strcmp(r[i].path,p))return 1;return 0;}
int fr_image_verify(int image,int root,const uint8_t*wanted,const uint8_t*manifest,uint64_t end){struct fr_image h;uint8_t digest[32];struct stat st;struct statvfs_dummy {int unused;};
 fr_stage_set("image_header");
 if(fr_sealed(image,1)||fr_hash_fd(image,16*1024*1024,digest)||memcmp(digest,wanted,32)||pread(image,&h,sizeof(h),0)!=sizeof(h)||memcmp(h.magic,"FRA061I1",8)||h.version!=1||!h.count||h.count>FR_MAX_MEMBERS||h.total>FR_IMAGE_MAX||memcmp(h.manifest_sha,manifest,32)||fstat(image,&st)||(uint64_t)st.st_size!=sizeof(h)+(uint64_t)h.count*sizeof(struct fr_member))return -EBADMSG;
 struct fr_member*r=calloc(h.count,sizeof(*r));if(!r)return -ENOMEM;int result=-EBADMSG;uint64_t total=0;int interp=0,stdlib=0,cache=0;
 if(pread(image,r,h.count*sizeof(*r),sizeof(h))!=(ssize_t)(h.count*sizeof(*r)))goto out;
 for(unsigned i=0;i<h.count;i++){fr_stage_set("image_member_metadata");if(fr_now()>=end){result=-ETIMEDOUT;goto out;}if(!grammar(r[i].path)||!memchr(r[i].target,0,512)||r[i].mode>07777u||(i&&strcmp(r[i-1].path,r[i].path)>=0))goto out;
  fr_stage_set("image_member_identity");int fd=fr_open_beneath(root,r[i].path,O_RDONLY|O_NONBLOCK|(r[i].kind==FR_ALIAS?0:O_NOFOLLOW));if(fd<0){result=fd;goto out;}if(fstat(fd,&st)||st.st_dev!=r[i].dev||st.st_ino!=r[i].ino||((unsigned)st.st_mode&07777u)!=r[i].mode){close(fd);result=-ESTALE;goto out;}
  if(r[i].kind!=4){struct statvfs fs;if(fstatvfs(fd,&fs)||!(fs.f_flag&ST_RDONLY)||!(fs.f_flag&ST_NOSUID)){close(fd);result=-EROFS;goto out;}}
  if(r[i].kind==FR_FILE){fr_stage_set("image_file_bytes");if(!S_ISREG(st.st_mode)||fr_sealed(fd,1)||st.st_size!=r[i].size||fr_hash_fd(fd,r[i].size,digest)||memcmp(digest,r[i].sha,32)){close(fd);result=-EPERM;goto out;}total+=r[i].size;if(total>h.total){close(fd);goto out;}interp|=!strcmp(r[i].path,"/usr/bin/python3.14");cache|=!strcmp(r[i].path,"/etc/ld.so.cache");}
  else if(r[i].kind==FR_DIR){fr_stage_set("image_directory_membership");if(!S_ISDIR(st.st_mode)||st.st_uid||st.st_gid||(st.st_mode&022)){close(fd);result=-EPERM;goto out;}stdlib|=!strcmp(r[i].path,"/usr/lib/python3.14");DIR*d=fdopendir(fd);if(!d){close(fd);goto out;}struct dirent*e;unsigned seen=0;errno=0;while((e=readdir(d))){if(!strcmp(e->d_name,".")||!strcmp(e->d_name,".."))continue;if(++seen>FR_MAX_MEMBERS||!child_named(r,h.count,r[i].path,e->d_name)){closedir(d);goto out;}}if(errno){closedir(d);goto out;}closedir(d);fd=-1;}
  else if(r[i].kind==FR_ALIAS){fr_stage_set("image_alias_closure");if(!r[i].target[0]){close(fd);goto out;}int target_known=0;for(unsigned j=0;j<h.count;j++)if((r[j].kind==FR_FILE||r[j].kind==FR_DIR)&&r[j].dev==r[i].dev&&r[j].ino==r[i].ino)target_known=1;if(!target_known){close(fd);goto out;}char parent[512],leaf[512],actual[512];strcpy(parent,r[i].path);char*s=strrchr(parent,'/');strcpy(leaf,s+1);if(s==parent)s[1]=0;else*s=0;int pd=fr_open_beneath(root,parent,O_RDONLY|O_DIRECTORY);if(pd<0){close(fd);goto out;}ssize_t n=readlinkat(pd,leaf,actual,sizeof(actual)-1);close(pd);if(n<0||n>=511){close(fd);goto out;}actual[n]=0;if(strcmp(actual,r[i].target)){close(fd);goto out;}}
  else if(r[i].kind==4){if(strcmp(r[i].path,"/proc")&&strcmp(r[i].path,"/sys")&&strcmp(r[i].path,"/var/tmp")){close(fd);goto out;}if(!S_ISDIR(st.st_mode)){close(fd);goto out;}}
  else {close(fd);goto out;}if(fd>=0)close(fd);
 }
 fr_stage_set("image_total_required_roles");if(total!=h.total||!interp||!stdlib||!cache)goto out;result=fr_runtime_verify(root,r,h.count,end);
out:free(r);return result;
}

/* A201 physical Source-only body descriptors were created in the EXISTING
 * outside public caller before bounded Root/coordinator/worker birth.
 * This validates storage, not complete native/Python error object acceptance. */
int fr_body_source_validate(int fd,unsigned slot,const struct fr_cap*c,int fresh){
 struct stat st;struct fr_body_plane p;uint8_t sha[32];
 if(slot>=4||!c||fstat(fd,&st)||!S_ISREG(st.st_mode)||
    st.st_size!=(off_t)(FR_BODY_HEADER+FR_BODY_CAP)||st.st_uid!=c->uid||st.st_gid!=c->gid)return -EPERM;
 int seals=fcntl(fd,F_GET_SEALS);
 if(seals<0||(seals&(F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL))!=(F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL))return -EPERM;
 if(pread(fd,&p,sizeof(p),0)!=(ssize_t)sizeof(p)||fr_hash_fd(FR_CAP_FD,sizeof(*c),sha))return -EIO;
 if(memcmp(p.magic,"FRBOD201",8)||p.version!=1||p.slot!=slot||p.capacity!=FR_BODY_CAP||
    p.generation!=1||memcmp(p.session,c->session,32)||memcmp(p.cap_sha,sha,32))return -ESTALE;
 if(p.end<FR_BODY_HEADER||p.end>FR_BODY_HEADER+FR_BODY_CAP||p.poison||p.retired)return -EIO;
 if(fresh&&(p.sequence||p.end!=FR_BODY_HEADER))return -ESTALE;
 return 0;
}
