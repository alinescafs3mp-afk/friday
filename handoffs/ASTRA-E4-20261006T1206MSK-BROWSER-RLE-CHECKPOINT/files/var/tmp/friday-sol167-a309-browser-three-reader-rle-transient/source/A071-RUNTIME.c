/* Bounded pre-interpreter DATA consumers. SOURCE ONLY, never loaded now.
 * All reads use the very same protected image inodes subsequently consumed by
 * exec/ld/import. Invalid cache/ELF metadata is rejected before a loader runs.
 * This is a defensive closure parser, with no mapping/execution/provisioning. */
#include "A071-NATIVE.h"
#include <elf.h>
#include <sys/stat.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
struct __attribute__((packed)) cache_entry {int32_t flags;uint32_t key,value,version;uint64_t hwcap;};
struct cache {unsigned char *bytes;size_t size;uint32_t count;};
static int exact(int fd,void *buf,size_t size,uint64_t offset,uint64_t limit){
 if(offset>limit||size>limit-offset||offset>INT64_MAX)return -ENOEXEC;
 return pread(fd,buf,size,(off_t)offset)==(ssize_t)size?0:-ENOEXEC;
}
static const char *cache_string(struct cache *c,uint32_t off){
 uint64_t start=48ull+24ull*c->count;
 if(off<start||off>=c->size||!memchr(c->bytes+off,0,c->size-off))return 0;
 const char *p=(const char *)c->bytes+off;
 size_t len=strlen(p);if(!len||len>=512)return 0;
 for(size_t i=0;i<len;i++)if((unsigned char)p[i]<33||(unsigned char)p[i]>126)return 0;
 return p;
}
static int image_member(int root,const char *path,struct fr_member *rows,unsigned count){
 int fd=fr_open_beneath(root,path,O_RDONLY|O_NONBLOCK);if(fd<0)return -ENODATA;
 struct stat st;int result=-ENODATA;
 if(!fstat(fd,&st))for(unsigned i=0;i<count;i++)if(rows[i].kind==FR_FILE&&
    rows[i].dev==(uint64_t)st.st_dev&&rows[i].ino==(uint64_t)st.st_ino&&
    rows[i].size==(uint64_t)st.st_size){result=0;break;}
 close(fd);return result;
}
static int cache_load(int root,struct cache *c){
 fr_stage_set("runtime_cache_format");
 memset(c,0,sizeof(*c));int fd=fr_open_beneath(root,"/etc/ld.so.cache",O_RDONLY|O_NOFOLLOW);
 if(fd<0)return -ENODATA;struct stat st;
 if(fstat(fd,&st)||st.st_size<48||st.st_size>1048576||fr_sealed(fd,1)){close(fd);return -ENODATA;}
 c->size=(size_t)st.st_size;c->bytes=malloc(c->size);
 if(!c->bytes){close(fd);return -ENOMEM;}
 int result=exact(fd,c->bytes,c->size,0,c->size);close(fd);
 if(result||memcmp(c->bytes,"glibc-ld.so.cache1.1",20))return -ENODATA;
 memcpy(&c->count,c->bytes+20,4);
 if(c->count>8192||48ull+24ull*c->count>c->size)return -ENODATA;
 fr_stage_set("runtime_cache_strings");for(unsigned i=0;i<c->count;i++){
  struct cache_entry e;memcpy(&e,c->bytes+48+24*i,sizeof(e));
  if((e.flags&0xff00)==0x300&&(!cache_string(c,e.key)||!cache_string(c,e.value)))return -ENODATA;
 }
 return 0;
}
static int dependency(struct cache *c,int root,const char *name,struct fr_member *rows,unsigned count,uint64_t end){
 fr_stage_set("runtime_cache_resolution");
 if(!name||!*name||strlen(name)>511)return -ENOEXEC;
 for(const char *p=name;*p;p++)if(!((*p>='a'&&*p<='z')||(*p>='A'&&*p<='Z')||
    (*p>='0'&&*p<='9')||strchr("_.+-",*p)))return -ENOEXEC;
 int found=0;uint64_t dev=0,ino=0;
 for(unsigned i=0;i<c->count;i++){
  if(fr_now()>=end)return -ETIMEDOUT;
  struct cache_entry e;memcpy(&e,c->bytes+48+24*i,sizeof(e));
  if((e.flags&0xff00)!=0x300)continue;
  const char *key=cache_string(c,e.key),*value=cache_string(c,e.value);
  if(!key||!value)return -ENODATA;if(strcmp(key,name))continue;
  if(value[0]!='/'||image_member(root,value,rows,count))return -ENODATA;
  int fd=fr_open_beneath(root,value,O_RDONLY|O_NONBLOCK);struct stat st;
  if(fd<0)return -ENODATA;
  int r=fstat(fd,&st);close(fd);if(r)return -ENODATA;
  if(found&&(dev!=(uint64_t)st.st_dev||ino!=(uint64_t)st.st_ino))return -ENODATA;
  dev=st.st_dev;ino=st.st_ino;found=1;
 }
 return found?0:-ENODATA;
}
static int elf_closure(int root,int fd,struct fr_member *rows,unsigned count,struct cache *cache,uint64_t end){
 fr_stage_set("runtime_ELF_format");
 struct stat st;unsigned char magic[4];if(fstat(fd,&st)||st.st_size<0)return -ENOEXEC;
 if(st.st_size<4)return 0;if(exact(fd,magic,4,0,st.st_size))return -ENOEXEC;
 if(memcmp(magic,ELFMAG,4))return 0; /* non-executable stdlib/OS DATA */
 Elf64_Ehdr h;if(exact(fd,&h,sizeof(h),0,st.st_size)||h.e_ident[EI_CLASS]!=ELFCLASS64||
    h.e_ident[EI_DATA]!=ELFDATA2LSB||h.e_ident[EI_VERSION]!=EV_CURRENT||
    (h.e_type!=ET_EXEC&&h.e_type!=ET_DYN)||h.e_machine!=EM_X86_64||
    h.e_phentsize!=sizeof(Elf64_Phdr)||!h.e_phnum||h.e_phnum>128)return -ENOEXEC;
 Elf64_Phdr headers[128];if(exact(fd,headers,h.e_phnum*sizeof(*headers),h.e_phoff,st.st_size))return -ENOEXEC;
 Elf64_Phdr *dynamic=0;unsigned interp=0;
 for(unsigned i=0;i<h.e_phnum;i++){
  Elf64_Phdr *p=&headers[i];if(p->p_offset>(uint64_t)st.st_size||p->p_filesz>(uint64_t)st.st_size-p->p_offset)return -ENOEXEC;
  if(p->p_type==PT_DYNAMIC){if(dynamic)return -ENOEXEC;dynamic=p;}
  if(p->p_type==PT_INTERP){fr_stage_set("runtime_ELF_loader");char path[512];if(interp++||p->p_filesz<2||p->p_filesz>512||
     exact(fd,path,p->p_filesz,p->p_offset,st.st_size)||path[p->p_filesz-1]||
     memchr(path,0,p->p_filesz-1)||path[0]!='/'||image_member(root,path,rows,count))return -ENOEXEC;
   int loader=fr_open_beneath(root,path,O_RDONLY);struct stat loader_st;
   if(loader<0)return -ENOEXEC;int r=fstat(loader,&loader_st);close(loader);
   if(r||!(loader_st.st_mode&0111))return -ENOEXEC;
  }
 }
 fr_stage_set("runtime_ELF_dynamic");if(!dynamic)return 0;
 if(!dynamic->p_filesz||dynamic->p_filesz>65536||dynamic->p_filesz%sizeof(Elf64_Dyn))return -ENOEXEC;
 size_t n=dynamic->p_filesz/sizeof(Elf64_Dyn);Elf64_Dyn *entries=malloc(dynamic->p_filesz);
 if(!entries)return -ENOMEM;int result=exact(fd,entries,dynamic->p_filesz,dynamic->p_offset,st.st_size);
 uint64_t strings=0,size=0;unsigned string_fields=0,size_fields=0,terminated=0;
 for(size_t i=0;!result&&i<n;i++){
  if(entries[i].d_tag==DT_NULL){terminated=1;break;}
  if(entries[i].d_tag==DT_STRTAB){strings=entries[i].d_un.d_ptr;string_fields++;}
  if(entries[i].d_tag==DT_STRSZ){size=entries[i].d_un.d_val;size_fields++;}
  if(entries[i].d_tag==DT_RPATH||entries[i].d_tag==DT_RUNPATH)result=-ENOEXEC;
 }
 if(!terminated||string_fields!=1||size_fields!=1||!size||size>1048576)result=-ENOEXEC;
 uint64_t off=0;unsigned locations=0;
 for(unsigned i=0;!result&&i<h.e_phnum;i++){Elf64_Phdr *p=&headers[i];if(p->p_type==PT_LOAD&&
     strings>=p->p_vaddr&&strings-p->p_vaddr<=p->p_filesz&&size<=p->p_filesz-(strings-p->p_vaddr)){
       off=p->p_offset+strings-p->p_vaddr;locations++;}}
 if(locations!=1)result=-ENOEXEC;
 char *table=0;if(!result){table=malloc(size);if(!table)result=-ENOMEM;else result=exact(fd,table,size,off,st.st_size);}
 for(size_t i=0;!result&&i<n&&entries[i].d_tag!=DT_NULL;i++)if(entries[i].d_tag==DT_NEEDED){
  uint64_t at=entries[i].d_un.d_val;
  if(at>=size||!memchr(table+at,0,size-at))result=-ENOEXEC;
  else result=dependency(cache,root,table+at,rows,count,end);
 }
 free(table);free(entries);return result;
}
int fr_runtime_verify(int root,struct fr_member *rows,unsigned count,uint64_t end){
 struct cache cache;int result=cache_load(root,&cache);
 for(unsigned i=0;!result&&i<count;i++)if(rows[i].kind==FR_FILE){
  if(fr_now()>=end){result=-ETIMEDOUT;break;}
  if(!strcmp(rows[i].path,"/usr/lib/python314.zip")||!strcmp(rows[i].path,"/etc/ld.so.preload")){fr_stage_set("runtime_import_policy");result=-ENODATA;break;}
  int fd=fr_open_beneath(root,rows[i].path,O_RDONLY|O_NOFOLLOW);if(fd<0){result=fd;break;}
  result=elf_closure(root,fd,rows,count,&cache,end);close(fd);
 }
 free(cache.bytes);return result;
}

