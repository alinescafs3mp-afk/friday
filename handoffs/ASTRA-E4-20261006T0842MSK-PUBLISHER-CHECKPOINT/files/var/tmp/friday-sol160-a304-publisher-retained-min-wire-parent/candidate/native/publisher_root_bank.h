/* INERT. Preowned evidence storage in the EXISTING gate parent and Root.
 * No process/service/grant is introduced. Native layout is qualified separately.
 * Parent owns the receiving memfd before launch. Actual Root working data stay
 * PRIVATE across fork; the separate terminal mapping is MADV_DONTFORK and has
 * no remaining Root FD. Only the actual Root can copy into it before its exit.
 */
#ifndef FRIDAY_PUBLISHER_ROOT_BANK_H
#define FRIDAY_PUBLISHER_ROOT_BANK_H
#include <stdint.h>
#include <stddef.h>
#define FRIDAY_ROOT_BANK_HEADER 4096ULL
#define FRIDAY_ROOT_BANK_MAGIC UINT64_C(0x314b4e4142445246)
#define FRIDAY_ROOT_BANK_VERSION 6ULL
#define FRIDAY_ROOT_BANK_RESERVATION_MAGIC UINT64_C(0x3156534552445246)
#define FRIDAY_ROOT_BANK_RAM_CAP UINT64_C(8589934592)
#define FRIDAY_ROOT_BANK_READ_CAP UINT64_C(40960000000)
#define FRIDAY_ROOT_BANK_PARENT_SCRATCH UINT64_C(1048576)
#define FRIDAY_ROOT_BANK_TEXT_AT 2304ULL
#define FRIDAY_ROOT_BANK_TEXT_ROWS 8ULL
#define FRIDAY_ROOT_BANK_TEXT_ROW_BYTES 192ULL
#define FRIDAY_ROOT_BANK_BIND_AT 640ULL
#define FRIDAY_ROOT_BANK_BIND_BYTES 1408ULL
#define FRIDAY_ROOT_BANK_BIND_MAGIC UINT64_C(0x31444e4942445246)
#define FRIDAY_ROOT_BANK_BIND_EVENTS 22ULL
#define FRIDAY_ROOT_BANK_PUBLICATION_AT 3840ULL
#define FRIDAY_ROOT_BANK_PUBLICATION_BYTES 256ULL
#define FRIDAY_ROOT_BANK_PUBLICATION_MAGIC UINT64_C(0x3155425042445246)
#define FRIDAY_ROOT_BANK_PUBLICATION_VERSION 2ULL
#define FRIDAY_ROOT_BANK_PUBLICATION_OPERAND_BYTES 65536ULL
#define FRIDAY_ROOT_BANK_PUBLICATION_OPERAND_STORAGE (2ULL*FRIDAY_ROOT_BANK_PUBLICATION_OPERAND_BYTES)
/* A bounded ORIGINAL primitive failure record in existing verified storage.
 * It is NOT a substitute for missing source/pointee/operand bodies or a full
 * success-history ledger. The parent preserves the whole actual destination,
 * including its partial bytes, and STILL raises failure. Clock loss/expiry
 * cannot authorize this new copy; that contour has no complete handoff here. */
typedef struct { uint64_t word[32]; } FridayPublisherRootPublicationRecord;
enum {
    RP_MAGIC,RP_VERSION,RP_BYTES,RP_PID,RP_PARENT,RP_STATE,
    RP_OP,RP_RC,RP_ERRNO,RP_OPERAND0,RP_OPERAND1,RP_EXTENT,RP_DETAIL,
    RP_CLOCK_SEC,RP_CLOCK_NSEC,RP_CLOCK_NS,RP_STARTED,RP_DEADLINE,
    RP_COMMIT_RC,RP_PERFORM_RC,RP_BODY_COPIED,RP_BODY_COMPARED,
    RP_HEADER_COPIED,RP_REQUIRED_BODIES_COMPLETE,
    RP_SECONDARY_OP,RP_SECONDARY_RC,RP_SECONDARY_ERRNO,
    RP_SECONDARY_OPERAND,RP_SECONDARY_EXTENT,RP_CLOCK_VALID,RP_READY,RP_FINAL_DEADLINE
};
/* Published PARTIAL_ERROR variant uses words24..28 for actual retained compare
 * bodies. Local failed-clock secondary facts use the same preowned cells but
 * are NEVER misdecoded as a published frame. No type/status is rewritten. */
enum { RP_COMPARE_CAPTURED=24,RP_SOURCE_BODY_AT,RP_DEST_BODY_AT,
       RP_OPERAND_BODY_BYTES,RP_CAPTURE_CHECKED_NS };
enum { RP_PARTIAL_ERROR=1 };
enum {
    RP_OP_COMMIT_STATE=1,RP_OP_COMMIT_RESULT,RP_OP_COMMIT_CLOCK,
    RP_OP_COMMIT_CLOCK_DOMAIN,RP_OP_PUBLISH_STATE,RP_OP_PUBLISH_CLOCK,
    RP_OP_PUBLISH_CLOCK_DOMAIN,RP_OP_BODY_COMPARE,RP_OP_HEADER_COMPARE,
    RP_OP_OUTCOME,RP_OP_FAILURE_CLOCK,RP_OP_FAILURE_CLOCK_DOMAIN
};
/* Facts go into already existing same-Root TLS, not a new error issuer/service.
 * errno is immediate syscall errno ONLY, zero for ordinary validation/compare.
 * No unknown native address is dereferenced by this recorder or by the parent. */
void FridayPublisherRootBankFault(uint64_t,int64_t,int,uint64_t,uint64_t,uint64_t,uint64_t);
/* Fixed ordinary bind facts, not a success/end marker. Original signed rc is
 * transported as its uint64_t modulo encoding; errno is saved immediately and
 * is zero for non-syscall validation/library-status failures. All reached
 * operations fit the fixed table; overflow prevents a complete handoff.
 * The selected ABI and whole original-master cost are REQUIRED_NOT_RUN. */
typedef struct { uint64_t op,rc,error,operand,extent,detail; } FridayPublisherRootBindEvent;
typedef struct {
    uint64_t word[44];
    FridayPublisherRootBindEvent event[FRIDAY_ROOT_BANK_BIND_EVENTS];
} FridayPublisherRootBindRecord;
enum {
    RB_MAGIC,RB_VERSION,RB_BYTES,RB_PID,RB_PARENT,RB_FD,RB_COUNT,RB_FIRST,
    RB_STATE,RB_FD_MATCHED,RB_HEADER_VALID,RB_DEST_PRESENT,RB_PRIVATE_PRESENT,
    RB_CLOSE_ATTEMPTED,RB_STAT_PRESENT,RB_LAYOUT_PRESENT,
    RB_STAT_AT=16,RB_LAYOUT_AT=25,RB_RETURN=34,RB_RECORD_READY,
    RB_CLOCK_SEC,RB_CLOCK_NSEC,RB_CLOCK_VALID,RB_OVERFLOW,RB_CLOCK_ATTEMPTED
};
enum { RB_BOUND=1,RB_FAILED=2 };
enum {
    RB_OP_ATTACHED=1,RB_OP_LAYOUT,RB_OP_FD_DOMAIN,RB_OP_FSTAT,
    RB_OP_FD_IDENTITY,RB_OP_GETFL,RB_OP_GETSEALS,RB_OP_FD_PROPERTIES,
    RB_OP_MAP_DEST,RB_OP_HEADER,RB_OP_DONTFORK,RB_OP_FUTURE_SEAL,
    RB_OP_MAP_PRIVATE,RB_OP_CLOSE_FD,RB_OP_ATTACH,RB_OP_UNMAP_PRIVATE,
    RB_OP_UNMAP_UNVERIFIED_DEST,RB_OP_CLOCK,RB_OP_CLOCK_DOMAIN
};
/* Row: original address/alias, length, presence, full NUL-terminated body160.
 * Only ordinary public native phase/fault strings; no pointer dereference by
 * the parent. Original address equality is retained independently of content. */
typedef struct { uint64_t original,alias,bytes,present; unsigned char body[160]; } FridayPublisherRootBankText;
enum {
    B_MAGIC, B_VERSION, B_BYTES, B_ROOT_AT, B_ROOT_BYTES, B_COLD_AT,
    B_COLD_BYTES, B_CALLER_AT, B_CALLER_BYTES, B_PARENT_PID, B_STARTED,
    B_DEADLINE, B_READ_RESERVE, B_IMAGE_ID_AT, B_IMAGE_ID_BYTES,
    B_RESERVATION_MAGIC=16, B_RESERVATION_VERSION, B_RESERVATION_OWNER,
    B_RESERVATION_STARTED, B_RESERVATION_DEADLINE, B_RESERVATION_READ,
    B_RESERVATION_RAM, B_RESERVATION_OUTPUT, B_RESERVATION_GENERATION,
    B_BINDING_WORDS,
    /* Words 0..24 are immutable parent inputs. The reservation is the native
     * image's fixed original bank charge, not Python-issued pool tokens/caps.
     * Native code recomputes all amounts before adopting it once.
     * Completion is a distinct record.
     * It is never consumed while this child or a descendant can still write. */
    B_CHILD_PID=32, B_PUBLISHED, B_OUTCOME, B_COMPLETION_NS, B_COLD_RC,
    B_PREINIT_ATTEMPTED, B_INIT_ATTEMPTED, B_INIT_COMPLETE, B_PERFORM_ATTEMPTED,
    B_COLD_DATA_COMPLETE, B_SOURCE_DATA_COMPLETE, B_CONFIG_END,
    B_LOCAL_COMMAND_END, B_RUNTIME_LOCAL_END, B_UTILITY_LOCAL_END,
    B_RUNTIME_EXTERNAL, B_CLOCK_FAILED, B_READ_SPENT, B_OUTPUT_SPENT,
    B_NATIVE_ALLOCATION, B_RETAINED_ALLOCATION, B_OBSERVED_RAM,
    B_PARENT_READ_RESERVED, B_COLD_ADDRESS, B_ROOT_ADDRESS, B_CALLER_ADDRESS,
    B_PARTIAL_PREFIX, B_STATUS_SAVED, B_STATUS_COMPLETE, B_BUILTIN_COMPLETE,
    B_FINAL_COPY_COMPLETE, B_FINAL_COPY_BYTES, B_FINAL_COPY_NS,
    /* Word65 was zero-reserved in v5. Restrictive final DATA, never admission.
     * Original B_DEADLINE/reservation remain immutable. Zero is not D0. */
    B_FINAL_DEADLINE,
    B_WORDS
};
enum { BANK_HELD=0, BANK_LOCAL_END=1, BANK_EARLY_PREFIX=2 };
typedef struct { uint64_t word[128]; unsigned char reserved[3072]; } FridayPublisherRootBankHeader;
/* Stable-layout DATA only. Calling it still requires the existing source/image
 * admission; these declarations are not permission to load or execute it. */
int FridayPublisherRootBankLayout(uint64_t out[9]);
int FridayPublisherRootBankAttach(void *root,void *cold,void *caller,
    FridayPublisherRootBankHeader *,uint64_t);
int FridayPublisherRootBankAttached(void);
void *FridayPublisherRootBankColdStorage(void);
uint64_t FridayPublisherRootBankStart(void);
uint64_t FridayPublisherRootBankDeadline(void);
/* Final native phases use the already established original/tightened pool
 * deadline. Zero means unavailable/inconsistent or a retained failed clock;
 * it never authorizes a fresh clock, renewed interval or outside acceptance. */
uint64_t FridayPublisherRootBankFinalDeadline(void);
uint64_t FridayPublisherRootBankExtraAllocation(void);
uint64_t FridayPublisherRootBankReadReserve(void);
int FridayPublisherRootBankReservationMatches(uint64_t,uint64_t,uint64_t);
int FridayPublisherRootBankCommit(int,const void *);
/* Actual command caller: bind before ColdPerform; return only after new record.
 * The sole main below terminates the SAME Root. Only the existing outside parent
 * can observe process end and accept a complete early-failure transfer. */
/* expected[7] is the existing parent's exact prelaunch fd stat identity, not
 * admission or a new capability. No write is allowed on a mismatching fd/map. */
int FridayPublisherRootBankCommand(int,const char *,const uint64_t expected[7]);
#endif
