/* Inert SAME-Root cold caller. Not an execution/admission grant. */
#ifndef FRIDAY_PUBLISHER_ROOT_COMMAND_H
#define FRIDAY_PUBLISHER_ROOT_COMMAND_H
#include "publisher_root_entry.h"
#define FRIDAY_ROOT_COMMAND_TEXT 2000000
#define FRIDAY_ROOT_CONFIG_ROWS 4096
#define FRIDAY_ROOT_CONFIG_LISTS 5
#define FRIDAY_ROOT_BUILTIN_ROWS 1024
#define FRIDAY_ROOT_BUILTIN_NAMES 65536
typedef struct {
    uint64_t field,ordinal,alias,body_at,body_bytes;
    const wchar_t *original; /* PRIVATE historical equality only, never dereference after clear */
} FridayPublisherColdConfigText;
typedef struct {
    uint64_t field,first,count,array_alias;
    Py_ssize_t original_length;
    wchar_t **original_items; /* PRIVATE historical address, not body evidence */
} FridayPublisherColdConfigList;
typedef struct {
    /* Exact public native struct image retains scalar values. Pointer members
     * are historical only: actual pointee bodies and aliases are below.
     * This storage is preowned/charged with the original cold command. */
    PyPreConfig historical_preconfig;
    PyConfig historical_config;
    FridayPublisherColdConfigText text[FRIDAY_ROOT_CONFIG_ROWS];
    FridayPublisherColdConfigList lists[FRIDAY_ROOT_CONFIG_LISTS];
    unsigned char body[FRIDAY_ROOT_COMMAND_TEXT];
    uint64_t text_count,list_count,body_bytes,full_bytes_read;
    const char *fault;
    int attempted,complete,clear_attempted,clear_returned,closed_fields_confirmed;
    unsigned char reader_sink;
} FridayPublisherColdConfigReceipt;
struct FridayPublisherRootColdResult {
    /* Preowned native state survives Python object lifetime. No fresh pool. */
    FridayPublisherMasterPool *pool;
    const FridayPublisherRootTerminal *terminal;
    const FridayPublisherRootCommandReceipt *receipt;
    const FridayPublisherRootUtilityReceipt *utility_receipt;
    FridayPublisherRootCaseInput case_input;
    PyPreConfig preconfig;
    PyConfig config;
    PyStatus first_status;
    PyStatus historical_status; /* original status addresses are private history only */
    FridayPublisherColdConfigReceipt config_receipt;
    const char *phase;
    const char *operation_phase,*completion_phase;
    uint64_t status_func_bytes,status_message_bytes,started_ns,deadline_ns;
    pid_t owner_pid;
    unsigned long owner_thread;
    PyInterpreterState *initialized_interpreter;
    /* This SAME cold owner supplies the public pre-init table. No hidden
     * AppendInittab allocation which survives Py_FinalizeEx is introduced. */
    struct _inittab *builtin_previous;
    struct _inittab builtin_entries[FRIDAY_ROOT_BUILTIN_ROWS+2];
    char builtin_names[FRIDAY_ROOT_BUILTIN_NAMES];
    uint64_t builtin_count,builtin_name_bytes,builtin_read_bytes;
    const char *builtin_phase;
    uint64_t builtin_current_index;
    struct _inittab builtin_current_row; /* full private native prefix, not a wire */
    int builtin_copy_complete,builtin_installed,builtin_restore_attempted;
    int builtin_restored,builtin_borrow_end_confirmed;
    int runtime_bind_attempted,runtime_bind_rc;
    char status_func[FRIDAY_ROOT_COMMAND_TEXT+1];
    char status_message[FRIDAY_ROOT_COMMAND_TEXT+1];
    int attempted,configuration_owned,first_status_saved,status_bodies_complete;
    int completion_reserved,completion_attempted,completion_returned;
    int config_end_confirmed,runtime_was_external,runtime_end_eligible;
    int finalization_runtime_present_before,finalization_runtime_present_after;
    int completion_clock_errno,completion_clock_failed;
    uint64_t completion_started_ns,completion_ended_ns;
    int status_func_present,status_message_present;
    int preinit_attempted,preinit_completed,builtin_attempted,builtin_rc;
    int init_attempted,init_completed,runtime_owned,perform_attempted,perform_rc;
    int receiver_attempted,receiver_returned,receiver_rc;
    int runtime_present_after_init,partial_runtime_owned;
    int finalization_attempted,finalization_rc,runtime_end_observed;
    int root_runtime_support_end_confirmed;
    int utility_receiver_attempted,utility_receiver_rc,utility_end_confirmed;
    int command_end_confirmed,SourceReady,Root_admission,GO;
    /* Final publication is a NEW record,not a mutated Source tuple or sealed
     * CommandReceipt. Everything before this cell is the immutable native
     * cold-operation input of the last receiver,including full retained data. */
    FridayPublisherRootFinalHandoff final_handoff;
};
/* Actual caller of RootPerform, including both returns and failed cold init.
 * 0 = owned result HELD, not successful completion. -1 = invalid/repeated API.
 * Neither interpreter ownership nor a good init status is terminal acceptance.
 */
int FridayPublisherRootColdPerform(const char *,const FridayPublisherRootColdResult **);
#endif
