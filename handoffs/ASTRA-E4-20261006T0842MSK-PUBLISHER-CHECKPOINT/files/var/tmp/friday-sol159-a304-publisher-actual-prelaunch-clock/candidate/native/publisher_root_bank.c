/* INERT paired command entry, not execution authority. One existing Root only. */
#define _GNU_SOURCE
#define PY_SSIZE_T_CLEAN
#include "publisher_root_bank.h"
#include "publisher_root_command.h"
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <errno.h>
#include <limits.h>
#include <string.h>
#include <stdlib.h>
#include <time.h>
_Static_assert(sizeof(FridayPublisherRootBankHeader)==FRIDAY_ROOT_BANK_HEADER,"bank header");
_Static_assert(sizeof(FridayPublisherRootBankText)==FRIDAY_ROOT_BANK_TEXT_ROW_BYTES,"bank text row");
_Static_assert(sizeof(uintptr_t)==8,"selected native pointer ABI must be qualified");
_Static_assert(sizeof(FridayPublisherRootBindRecord)==FRIDAY_ROOT_BANK_BIND_BYTES,"bind record");
_Static_assert(sizeof(FridayPublisherRootPublicationRecord)==FRIDAY_ROOT_BANK_PUBLICATION_BYTES,"publication primitive");
_Static_assert(FRIDAY_ROOT_BANK_PUBLICATION_AT+FRIDAY_ROOT_BANK_PUBLICATION_BYTES==FRIDAY_ROOT_BANK_HEADER,"publication tail");
_Static_assert(FRIDAY_ROOT_BANK_BIND_AT>=65ULL*8ULL&&
    FRIDAY_ROOT_BANK_BIND_AT+FRIDAY_ROOT_BANK_BIND_BYTES==2048ULL,"bind region");
static _Thread_local FridayPublisherRootBankHeader *bank_working,*bank_destination;
static _Thread_local uint64_t bank_extent,bank_final_deadline;
static _Thread_local pid_t bank_owner;
static _Thread_local FridayPublisherRootBindRecord bind_record;
static _Thread_local FridayPublisherRootPublicationRecord publication_record;
/* Same original scratch reservation, prepaid before launch. These TWO bounded
 * temporary operand bodies are materialized before either receiving payload is
 * overwritten. They are not a third full bank, late allocation or new pool.
 * The parent cannot start its scratch/intake phase until this Root has ended.
 * Selected TLS/COW/loader/implicit whole costs still require qualification. */
static _Thread_local unsigned char compare_source_scratch[FRIDAY_ROOT_BANK_PUBLICATION_OPERAND_BYTES];
static _Thread_local unsigned char compare_destination_scratch[FRIDAY_ROOT_BANK_PUBLICATION_OPERAND_BYTES];
_Static_assert(sizeof(compare_source_scratch)+sizeof(compare_destination_scratch)+
    sizeof(bind_record)+sizeof(publication_record)+sizeof(bank_final_deadline)<=FRIDAY_ROOT_BANK_PARENT_SCRATCH,
    "explicit native capture storage within original scratch reservation");
static _Thread_local int bank_command_attempted;
void FridayPublisherRootBankFault(uint64_t op,int64_t rc,int error,
        uint64_t operand0,uint64_t operand1,uint64_t extent,uint64_t detail) {
    uint64_t *w=publication_record.word;
    if(!w[RP_OP]) {
        w[RP_OP]=op;w[RP_RC]=(uint64_t)rc;w[RP_ERRNO]=(uint64_t)error;
        w[RP_OPERAND0]=operand0;w[RP_OPERAND1]=operand1;
        w[RP_EXTENT]=extent;w[RP_DETAIL]=detail;
    } else if(!w[RP_SECONDARY_OP]) {
        w[RP_SECONDARY_OP]=op;w[RP_SECONDARY_RC]=(uint64_t)rc;
        w[RP_SECONDARY_ERRNO]=(uint64_t)error;
        w[RP_SECONDARY_OPERAND]=operand0;w[RP_SECONDARY_EXTENT]=extent;
    }
    /* No first-error replacement, allocation, pointer-body credit or retry. */
}
static void bind_event(uint64_t op,int64_t rc,int error,uint64_t operand,
        uint64_t extent,uint64_t detail) {
    uint64_t n=bind_record.word[RB_COUNT];
    if(n>=FRIDAY_ROOT_BANK_BIND_EVENTS){bind_record.word[RB_OVERFLOW]=1;return;}
    bind_record.event[n]=(FridayPublisherRootBindEvent){op,(uint64_t)rc,
        (uint64_t)error,operand,extent,detail};
    bind_record.word[RB_COUNT]=n+1;
    if(rc<0&&!bind_record.word[RB_FIRST])bind_record.word[RB_FIRST]=n+1;
}
static void bind_clock(const FridayPublisherRootBankHeader *h) {
    struct timespec t={0,0};
    bind_record.word[RB_CLOCK_ATTEMPTED]=1;
    int rc=clock_gettime(CLOCK_MONOTONIC,&t),saved=rc<0?errno:0;
    bind_event(RB_OP_CLOCK,rc,saved,0,0,CLOCK_MONOTONIC);
    if(rc<0)return;
    bind_record.word[RB_CLOCK_SEC]=(uint64_t)t.tv_sec;
    bind_record.word[RB_CLOCK_NSEC]=(uint64_t)t.tv_nsec;
    int valid=t.tv_sec>=0&&t.tv_nsec>=0&&t.tv_nsec<1000000000L&&
        (uint64_t)t.tv_sec<=(UINT64_MAX-(uint64_t)t.tv_nsec)/1000000000ULL;
    uint64_t now=valid?(uint64_t)t.tv_sec*1000000000ULL+(uint64_t)t.tv_nsec:0;
    valid=valid&&now>=h->word[B_STARTED]&&now<=h->word[B_DEADLINE];
    bind_record.word[RB_CLOCK_VALID]=(uint64_t)valid;
    bind_event(RB_OP_CLOCK_DOMAIN,valid?0:-1,0,now,h->word[B_STARTED],h->word[B_DEADLINE]);
}
static int bind_failure(int fd,FridayPublisherRootBankHeader *h,
        FridayPublisherRootBankHeader *private,uint64_t extent) {
    /* Clean the private allocation once. Every reached rc/errno remains a
     * separate row; cleanup cannot overwrite the original first failure. */
    if(private) {
        int rc=munmap(private,(size_t)extent),saved=rc<0?errno:0;
        bind_event(RB_OP_UNMAP_PRIVATE,rc,saved,(uint64_t)(uintptr_t)private,extent,0);
        if(!rc)bind_record.word[RB_PRIVATE_PRESENT]=0;
    }
    if(bind_record.word[RB_FD_MATCHED]&&!bind_record.word[RB_CLOSE_ATTEMPTED]) {
        bind_record.word[RB_CLOSE_ATTEMPTED]=1;
        int rc=close(fd),saved=rc<0?errno:0;
        bind_event(RB_OP_CLOSE_FD,rc,saved,(uint64_t)fd,0,0);
    }
    bind_record.word[RB_STATE]=RB_FAILED;
    bind_record.word[RB_RETURN]=78;
    if(h&&bind_record.word[RB_HEADER_VALID]) {
        if(!bind_record.word[RB_CLOCK_ATTEMPTED])bind_clock(h);
        bind_record.word[RB_RECORD_READY]=!bind_record.word[RB_OVERFLOW];
        /* Last proven receiving mapping stays alive until this SAME main's
         * _Exit. Do not unmap it and then pretend to know its last cleanup rc.
         * No runtime/Source is entered on this path. The parent must prove
         * actual kernel end/no writers AND read the entire fixed record. */
        memcpy((unsigned char *)h+FRIDAY_ROOT_BANK_BIND_AT,&bind_record,sizeof(bind_record));
    } else if(h) {
        int rc=munmap(h,(size_t)extent),saved=rc<0?errno:0;
        bind_event(RB_OP_UNMAP_UNVERIFIED_DEST,rc,saved,(uint64_t)(uintptr_t)h,extent,0);
        if(!rc)bind_record.word[RB_DEST_PRESENT]=0;
    }
    /* Before a verified receiving mapping exists these exact local facts have
     * NO surviving transport. No pwrite/foreign FD or exit-code substitute.
     * That boundary remains explicitly incomplete in the parent. */
    return -1;
}
static int bank_clock(uint64_t *out,uint64_t operation) {
    uint64_t final_deadline=bank_final_deadline;
    if(!final_deadline) {
        FridayPublisherRootBankFault(operation+1,-1,0,0,0,
            publication_record.word[RP_STARTED],0);
        return -1;
    }
    struct timespec t={0,0};
    int rc=clock_gettime(CLOCK_MONOTONIC,&t),saved=rc<0?errno:0;
    publication_record.word[RP_CLOCK_SEC]=(uint64_t)t.tv_sec;
    publication_record.word[RP_CLOCK_NSEC]=(uint64_t)t.tv_nsec;
    if(rc<0) {
        FridayPublisherRootBankFault(operation,rc,saved,0,0,0,CLOCK_MONOTONIC);
        return -1;
    }
    int valid=t.tv_sec>=0&&t.tv_nsec>=0&&t.tv_nsec<1000000000L&&
        (uint64_t)t.tv_sec<=(UINT64_MAX-(uint64_t)t.tv_nsec)/1000000000ULL;
    uint64_t now=valid?(uint64_t)t.tv_sec*1000000000ULL+(uint64_t)t.tv_nsec:0;
    valid=valid&&bank_working&&bank_owner==getpid()&&
        now>=bank_working->word[B_STARTED]&&now<=final_deadline;
    if(!valid) {
        FridayPublisherRootBankFault(operation+1,-1,0,now,0,
            publication_record.word[RP_STARTED],final_deadline);
        return -1;
    }
    *out=now;publication_record.word[RP_CLOCK_NS]=now;
    return 0;
}
static int bank_bind(int fd,const uint64_t expected[7]) {
    uint64_t v[9]={0};struct stat s;
    FridayPublisherRootBankHeader *private=NULL;
    bind_record.word[RB_MAGIC]=FRIDAY_ROOT_BANK_BIND_MAGIC;
    bind_record.word[RB_VERSION]=1;bind_record.word[RB_BYTES]=sizeof(bind_record);
    bind_record.word[RB_PID]=(uint64_t)getpid();
    bind_record.word[RB_PARENT]=(uint64_t)getppid();bind_record.word[RB_FD]=(uint64_t)(int64_t)fd;
    int rc=FridayPublisherRootBankAttached()?-1:0;
    bind_event(RB_OP_ATTACHED,rc,0,0,0,0);if(rc<0)return -1;
    rc=FridayPublisherRootBankLayout(v);
    memcpy(&bind_record.word[RB_LAYOUT_AT],v,sizeof(v));bind_record.word[RB_LAYOUT_PRESENT]=1;
    bind_event(RB_OP_LAYOUT,rc,0,0,v[6],0);if(rc<0)return -1;
    rc=fd>=3&&expected?0:-1;
    bind_event(RB_OP_FD_DOMAIN,rc,0,(uint64_t)(int64_t)fd,0,0);if(rc<0)return -1;
    rc=fstat(fd,&s);int saved=rc<0?errno:0;
    bind_event(RB_OP_FSTAT,rc,saved,(uint64_t)fd,0,0);if(rc<0)return -1;
    uint64_t actual[9]={(uint64_t)s.st_dev,(uint64_t)s.st_ino,(uint64_t)s.st_mode,
        (uint64_t)s.st_uid,(uint64_t)s.st_gid,(uint64_t)s.st_nlink,(uint64_t)s.st_size,
        (uint64_t)s.st_mtim.tv_sec*1000000000ULL+(uint64_t)s.st_mtim.tv_nsec,
        (uint64_t)s.st_ctim.tv_sec*1000000000ULL+(uint64_t)s.st_ctim.tv_nsec};
    memcpy(&bind_record.word[RB_STAT_AT],actual,sizeof(actual));bind_record.word[RB_STAT_PRESENT]=1;
    rc=memcmp(actual,expected,7*sizeof(uint64_t))? -1:0;
    bind_event(RB_OP_FD_IDENTITY,rc,0,(uint64_t)fd,7,0);if(rc<0)return -1;
    bind_record.word[RB_FD_MATCHED]=1;
    int access=fcntl(fd,F_GETFL);saved=access<0?errno:0;
    bind_event(RB_OP_GETFL,access,saved,(uint64_t)fd,0,F_GETFL);
    if(access<0)return bind_failure(fd,NULL,NULL,v[6]);
    int seals=fcntl(fd,F_GET_SEALS);saved=seals<0?errno:0;
    bind_event(RB_OP_GETSEALS,seals,saved,(uint64_t)fd,0,F_GET_SEALS);
    if(seals<0)return bind_failure(fd,NULL,NULL,v[6]);
    rc=S_ISREG(s.st_mode)&&s.st_uid==getuid()&&(s.st_mode&0777)==0600&&
        s.st_nlink==0&&s.st_size>=0&&(uint64_t)s.st_size==v[6]&&
        (uint64_t)s.st_size<=SIZE_MAX&&(access&O_ACCMODE)==O_RDWR&&
        seals==(F_SEAL_SHRINK|F_SEAL_GROW)?0:-1;
    bind_event(RB_OP_FD_PROPERTIES,rc,0,(uint64_t)fd,v[6],0);
    if(rc<0)return bind_failure(fd,NULL,NULL,v[6]);
    FridayPublisherRootBankHeader *h=mmap(NULL,(size_t)v[6],
        PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
    saved=h==MAP_FAILED?errno:0;
    bind_event(RB_OP_MAP_DEST,h==MAP_FAILED?-1:0,saved,
        (uint64_t)(uintptr_t)h,v[6],(uint64_t)fd);
    if(h==MAP_FAILED)return bind_failure(fd,NULL,NULL,v[6]);
    bind_record.word[RB_DEST_PRESENT]=1;
    const uint64_t *w=h->word;
    uint64_t native_one=1;
    int valid=*(unsigned char *)&native_one==1&&w[B_MAGIC]==FRIDAY_ROOT_BANK_MAGIC&&
        w[B_VERSION]==FRIDAY_ROOT_BANK_VERSION&&w[B_BYTES]==v[6]&&
        w[B_ROOT_AT]==v[0]&&w[B_ROOT_BYTES]==v[1]&&w[B_COLD_AT]==v[2]&&
        w[B_COLD_BYTES]==v[3]&&w[B_CALLER_AT]==v[4]&&w[B_CALLER_BYTES]==v[5]&&
        w[B_PARENT_PID]==(uint64_t)getppid()&&w[B_PARENT_PID]>1&&
        w[B_STARTED]&&w[B_STARTED]<=UINT64_MAX-4200000000000ULL&&
        w[B_DEADLINE]==w[B_STARTED]+4200000000000ULL&&
        w[B_READ_RESERVE]==v[7]&&w[B_IMAGE_ID_AT]==2048&&w[B_IMAGE_ID_BYTES]==64&&
        w[15]==0&&w[B_RESERVATION_MAGIC]==FRIDAY_ROOT_BANK_RESERVATION_MAGIC&&
        w[B_RESERVATION_VERSION]==1&&w[B_RESERVATION_OWNER]==w[B_PARENT_PID]&&
        w[B_RESERVATION_STARTED]==w[B_STARTED]&&
        w[B_RESERVATION_DEADLINE]==w[B_DEADLINE]&&
        w[B_RESERVATION_READ]==v[7]&&w[B_RESERVATION_RAM]==v[8]&&
        w[B_RESERVATION_OUTPUT]==FRIDAY_ROOT_BANK_HEADER&&
        w[B_RESERVATION_GENERATION]==1;
    /* All completion cells are new, not a replayed old receipt. The remaining
     * body is kernel-zero storage from the parent's newly created memfd.
     * This does not assume hostile same-UID parent resistance. */
    for(unsigned i=B_BINDING_WORDS;i<128;i++)if(w[i])valid=0;
    const unsigned char *identity=(const unsigned char *)h+2048;
    for(unsigned i=0;i<64;i++)
        if(!((identity[i]>='0'&&identity[i]<='9')||(identity[i]>='a'&&identity[i]<='f')))valid=0;
    /* The newly assigned bind area is also required to be initially zero. */
    for(uint64_t i=FRIDAY_ROOT_BANK_BIND_AT;i<2048;i++)
        if(((const unsigned char *)h)[i])valid=0;
    bind_event(RB_OP_HEADER,valid?0:-1,0,(uint64_t)(uintptr_t)h,FRIDAY_ROOT_BANK_HEADER,0);
    if(!valid)return bind_failure(fd,h,NULL,v[6]);
    bind_record.word[RB_HEADER_VALID]=1;
    rc=madvise(h,(size_t)v[6],MADV_DONTFORK);saved=rc<0?errno:0;
    bind_event(RB_OP_DONTFORK,rc,saved,(uint64_t)(uintptr_t)h,v[6],MADV_DONTFORK);
    if(rc<0)return bind_failure(fd,h,NULL,v[6]);
    rc=fcntl(fd,F_ADD_SEALS,F_SEAL_FUTURE_WRITE);saved=rc<0?errno:0;
    bind_event(RB_OP_FUTURE_SEAL,rc,saved,(uint64_t)fd,0,F_SEAL_FUTURE_WRITE);
    if(rc<0)return bind_failure(fd,h,NULL,v[6]);
    private=mmap(NULL,(size_t)v[6],
        PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    saved=private==MAP_FAILED?errno:0;
    bind_event(RB_OP_MAP_PRIVATE,private==MAP_FAILED?-1:0,saved,
        (uint64_t)(uintptr_t)private,v[6],0);
    if(private==MAP_FAILED)return bind_failure(fd,h,NULL,v[6]);
    bind_record.word[RB_PRIVATE_PRESENT]=1;
    memcpy(private,h,FRIDAY_ROOT_BANK_HEADER);
    bind_record.word[RB_CLOSE_ATTEMPTED]=1;
    rc=close(fd);saved=rc<0?errno:0;
    bind_event(RB_OP_CLOSE_FD,rc,saved,(uint64_t)fd,0,0);
    if(rc<0)return bind_failure(fd,h,private,v[6]);
    /* Child Source inherits ONLY the original private/COW working data.
     * The receiving map is omitted by the kernel at fork and its FD is gone.
     * No source-child supplier/journal implementation or stopped path changes. */
    bank_working=private;bank_destination=h;bank_extent=v[6];bank_owner=getpid();
    rc=FridayPublisherRootBankAttach((unsigned char *)private+v[0],
        (unsigned char *)private+v[2],(unsigned char *)private+v[4],private,v[6]);
    bind_event(RB_OP_ATTACH,rc,0,(uint64_t)(uintptr_t)private,v[6],0);
    if(rc<0)return bind_failure(fd,h,private,v[6]);
    bind_clock(private);
    if(!bind_record.word[RB_CLOCK_VALID]||bind_record.word[RB_OVERFLOW])
        return bind_failure(fd,h,private,v[6]);
    bind_record.word[RB_STATE]=RB_BOUND;bind_record.word[RB_RECORD_READY]=1;
    memcpy((unsigned char *)private+FRIDAY_ROOT_BANK_BIND_AT,&bind_record,sizeof(bind_record));
    /* Preowned verified destination receives the ordinary bind record BEFORE
     * ColdPerform/Commit. A later incomplete header publication cannot erase
     * this actual normal bind prefix by omission. Original debit/caps remain. */
    memcpy((unsigned char *)h+FRIDAY_ROOT_BANK_BIND_AT,&bind_record,sizeof(bind_record));
    return 0;
}
static int bank_publish(void) {
    uint64_t now;
    if(!bank_destination||!bank_working||bank_owner!=getpid()) {
        FridayPublisherRootBankFault(RP_OP_PUBLISH_STATE,-1,0,
            (uint64_t)(uintptr_t)bank_working,(uint64_t)(uintptr_t)bank_destination,bank_extent,0);
        return -1;
    }
    if(bank_clock(&now,RP_OP_PUBLISH_CLOCK)<0)return -1;
    if(bank_working->word[B_PUBLISHED]!=1||bank_destination->word[B_PUBLISHED]) {
        FridayPublisherRootBankFault(RP_OP_PUBLISH_STATE,-1,0,
            (uint64_t)(uintptr_t)bank_working,(uint64_t)(uintptr_t)bank_destination,
            bank_extent,bank_working->word[B_PUBLISHED]);
        return -1;
    }
    /* Actual complete source is consumed into its preowned outside destination
     * BEFORE Root owner loss. Both banks and copy/compare/read work were charged
     * at the original pool birth; no output-wire cap or replacement grant.
     * Required selected whole physical/implicit costs remain unqualified. */
    for(uint64_t at=FRIDAY_ROOT_BANK_HEADER;at<bank_extent;) {
        if(bank_clock(&now,RP_OP_PUBLISH_CLOCK)<0)return -1;
        size_t n=(size_t)(bank_extent-at>65536ULL?65536ULL:bank_extent-at);
        const unsigned char *source=(const unsigned char *)bank_working+at;
        unsigned char *destination=(unsigned char *)bank_destination+at;
        memcpy(destination,source,n);
        publication_record.word[RP_BODY_COPIED]=at+(uint64_t)n-FRIDAY_ROOT_BANK_HEADER;
        int compared=memcmp(destination,source,n);
        if(compared) {
            FridayPublisherRootBankFault(RP_OP_BODY_COMPARE,compared,0,
                (uint64_t)(uintptr_t)source,(uint64_t)(uintptr_t)destination,(uint64_t)n,at);
            return -1;
        }
        publication_record.word[RP_BODY_COMPARED]=at+(uint64_t)n-FRIDAY_ROOT_BANK_HEADER;
        at+=(uint64_t)n;
    }
    if(bank_clock(&now,RP_OP_PUBLISH_CLOCK)<0)return -1;
    bank_working->word[B_FINAL_COPY_COMPLETE]=1;
    bank_working->word[B_FINAL_COPY_BYTES]=bank_extent-FRIDAY_ROOT_BANK_HEADER;
    bank_working->word[B_FINAL_COPY_NS]=now;
    /* Header is the LAST copy. Parent never reads while any writer is alive;
     * interrupted copies or abnormal exit cannot yield an accepted receipt. */
    memcpy(bank_destination,bank_working,FRIDAY_ROOT_BANK_HEADER);
    publication_record.word[RP_HEADER_COPIED]=FRIDAY_ROOT_BANK_HEADER;
    int compared=memcmp(bank_destination,bank_working,FRIDAY_ROOT_BANK_HEADER);
    if(compared) {
        FridayPublisherRootBankFault(RP_OP_HEADER_COMPARE,compared,0,
            (uint64_t)(uintptr_t)bank_working,(uint64_t)(uintptr_t)bank_destination,
            FRIDAY_ROOT_BANK_HEADER,0);
        return -1;
    }
    return 0;
}
static int bank_failed_publication(void) {
    uint64_t *w=publication_record.word,now;
    uint64_t op=w[RP_OP];
    /* No blind second clock, grace period, late grant or write after an already
     * unavailable/expired clock. These exact errors have NO complete surviving
     * transport in this layout; return79 is rejected, never custody credit. */
    if(op==RP_OP_COMMIT_CLOCK||op==RP_OP_COMMIT_CLOCK_DOMAIN||
       op==RP_OP_PUBLISH_CLOCK||op==RP_OP_PUBLISH_CLOCK_DOMAIN)return 79;
    if(!op||!bank_destination||!bank_working||bank_owner!=getpid())return 79;
    if(bank_clock(&now,RP_OP_FAILURE_CLOCK)<0)return 79;
    if(op==RP_OP_BODY_COMPARE||op==RP_OP_HEADER_COMPARE) {
        uint64_t n=w[RP_EXTENT];
        uint64_t payload=bank_extent-FRIDAY_ROOT_BANK_PUBLICATION_OPERAND_STORAGE;
        uint64_t source=w[RP_OPERAND0],destination=w[RP_OPERAND1];
        uint64_t private_base=(uint64_t)(uintptr_t)bank_working;
        uint64_t outside_base=(uint64_t)(uintptr_t)bank_destination;
        /* Retain BOTH actual operands BEFORE writing either payload. Full
         * same-bank range checks permit a window crossing/inside a receiving
         * payload without reading foreign memory or overwriting an unread
         * compared byte. Source and destination snapshots remain distinct.
         * Four bounded n-byte reads, not an extra full-bank pass or retry. */
        if(n&&n<=FRIDAY_ROOT_BANK_PUBLICATION_OPERAND_BYTES&&
           source>=private_base&&destination>=outside_base&&
           source-private_base<=bank_extent&&n<=bank_extent-(source-private_base)&&
           destination-outside_base<=bank_extent&&n<=bank_extent-(destination-outside_base)) {
            unsigned char *dst=(unsigned char *)bank_destination;
            memcpy(compare_source_scratch,(const void *)(uintptr_t)source,(size_t)n);
            memcpy(compare_destination_scratch,(const void *)(uintptr_t)destination,(size_t)n);
            memcpy(dst+payload,compare_source_scratch,(size_t)n);
            memcpy(dst+payload+FRIDAY_ROOT_BANK_PUBLICATION_OPERAND_BYTES,
                compare_destination_scratch,(size_t)n);
            w[RP_COMPARE_CAPTURED]=1;w[RP_SOURCE_BODY_AT]=payload;
            w[RP_DEST_BODY_AT]=payload+FRIDAY_ROOT_BANK_PUBLICATION_OPERAND_BYTES;
            w[RP_OPERAND_BODY_BYTES]=n;w[RP_CAPTURE_CHECKED_NS]=now;
        }
    }
    w[RP_STATE]=RP_PARTIAL_ERROR;w[RP_CLOCK_VALID]=1;
    w[RP_REQUIRED_BODIES_COMPLETE]=0;w[RP_READY]=1;
    /* The bounded primitive plus any captured actual compare bodies are DATA.
     * An overlapping historical compared window survives in its explicit
     * snapshot, not by claiming the now-used receiving payload is unchanged.
     * No comparison is retried and no operational outcome is repaired. No uncopied
     * full working body/semantic alias closure is claimed. One final record
     * copy, no recursive final-writer retry. Whole costs/clock fit remain open. */
    memcpy((unsigned char *)bank_destination+FRIDAY_ROOT_BANK_PUBLICATION_AT,
        &publication_record,sizeof(publication_record));
    return 79;
}
int FridayPublisherRootBankCommand(int fd,const char *case_id,const uint64_t expected[7]) {
    if(bank_command_attempted||!case_id)return 78;
    bank_command_attempted=1;
    if(bank_bind(fd,expected)<0)return 78;
    uint64_t *pw=publication_record.word;
    pw[RP_MAGIC]=FRIDAY_ROOT_BANK_PUBLICATION_MAGIC;pw[RP_VERSION]=1;
    pw[RP_BYTES]=sizeof(publication_record);pw[RP_PID]=(uint64_t)getpid();
    pw[RP_PARENT]=(uint64_t)getppid();pw[RP_STARTED]=bank_working->word[B_STARTED];
    pw[RP_DEADLINE]=bank_working->word[B_DEADLINE];
    const FridayPublisherRootColdResult *result=NULL;
    int rc=FridayPublisherRootColdPerform(case_id,&result);
    pw[RP_PERFORM_RC]=(uint64_t)(int64_t)rc;
    /* ColdPerform has returned through both actual completion paths. Only
     * final bank construction/copy follows; no later pool mutation
     * or admission refresh may extend this latched cutoff. Zero stays failed.
     * Commit independently reads the SAME retained final pool bound once.
     * Do not repeat pool/body traversal for every 64KiB publication chunk. */
    bank_final_deadline=FridayPublisherRootBankFinalDeadline();
    int committed=FridayPublisherRootBankCommit(rc,result);
    pw[RP_COMMIT_RC]=(uint64_t)(int64_t)committed;
    if(committed<0)return bank_failed_publication();
    if(bank_publish()<0)return bank_failed_publication();
    /* Local code does not claim that this process or its mappings ended.
     * Existing parent retains the bank, confirms actual end and publishes its
     * own new receipt. Failure remains failure even with complete custody. */
    if(bank_working->word[B_OUTCOME]==BANK_LOCAL_END)return 0;
    if(bank_working->word[B_OUTCOME]==BANK_EARLY_PREFIX)return 70;
    FridayPublisherRootBankFault(RP_OP_OUTCOME,-1,0,
        (uint64_t)(uintptr_t)bank_working,0,bank_extent,bank_working->word[B_OUTCOME]);
    return bank_failed_publication();
}
#ifdef FRIDAY_PUBLISHER_ROOT_BANK_MAIN
static int bank_decimal(const char *s,uint64_t *out) {
    if(!s||!s[0])return -1;
    uint64_t n=0;
    for(unsigned i=0;s[i];i++) {
        if(i>=20||s[i]<'0'||s[i]>'9'||n>(UINT64_MAX-(uint64_t)(s[i]-'0'))/10ULL)return -1;
        n=n*10ULL+(uint64_t)(s[i]-'0');
    }
    *out=n;return 0;
}
int main(int argc,char **argv) {
    uint64_t value,expected[7];
    if(argc!=10||bank_decimal(argv[1],&value)<0||value>INT_MAX)_Exit(78);
    for(unsigned i=0;i<7;i++)if(bank_decimal(argv[3+i],&expected[i])<0)_Exit(78);
    int result=FridayPublisherRootBankCommand((int)value,argv[2],expected);
    /* No second initialization, private runtime finalizer, unrelated process,
     * or atexit callback. Parent end evidence AND full retained data are both
     * necessary; this exit code alone is never successful completion. */
    _Exit(result);
}
#endif
