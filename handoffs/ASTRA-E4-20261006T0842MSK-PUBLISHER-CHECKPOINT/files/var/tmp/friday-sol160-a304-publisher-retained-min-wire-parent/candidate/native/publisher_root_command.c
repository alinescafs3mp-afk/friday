/* NEW actual SAME Root cold caller. It preowns configuration/error storage,
 * starts the ORIGINAL pool before CPython initialization and calls RootPerform.
 * This INERT command calls CommandReceive on BOTH RootPerform outcomes.
 * Every owned return enters the same native config/status completion path.
 * The final native receiver is the actual SAME cold caller,preowned at birth.
 * Exact image invocation/qualification and release acceptance remain NOT_RUN.
 * No compiler/parser/import/test/runtime execution has been authorized here.
 */
#define PY_SSIZE_T_CLEAN
#include "publisher_root_command.h"
#include "publisher_root_bank.h"
#include <unistd.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <wchar.h>
#include <errno.h>

_Static_assert(PY_MAJOR_VERSION==3&&PY_MINOR_VERSION==14,
    "Reconcile the selected PyConfig field inventory before changing ABI");

PyMODINIT_FUNC PyInit_publisher_owned_custody(void);

static int full_status_text(const char *src,char *out,uint64_t *width) {
    *width=0;
    if(!src){out[0]=0;return 0;}
    /* An over-width native provider message is NOT silently truncated,
     * reclassified, freed, or accepted. The first original PyStatus/config
     * stays held and finalization remains forbidden pending full handoff. */
    size_t n=strnlen(src,FRIDAY_ROOT_COMMAND_TEXT+1);
    if(n>FRIDAY_ROOT_COMMAND_TEXT)return -1;
    memcpy(out,src,n+1);*width=(uint64_t)n;return 0;
}
static int keep_status(FridayPublisherRootColdResult *r,PyStatus status) {
    if(!PyStatus_Exception(status))return 0;
    if(!r->first_status_saved) {
        r->first_status=status;r->historical_status=status;r->first_status_saved=1;
        r->status_func_present=status.func!=NULL;
        r->status_message_present=status.err_msg!=NULL;
        int a=full_status_text(status.func,r->status_func,&r->status_func_bytes);
        int b=full_status_text(status.err_msg,r->status_message,&r->status_message_bytes);
        r->status_bodies_complete=(a==0&&b==0);
        if(r->status_bodies_complete) {
            /* Valid native result after config/runtime retirement. Original
             * provider addresses remain historical, never a later reader. */
            r->first_status.func=r->status_func_present?r->status_func:NULL;
            r->first_status.err_msg=r->status_message_present?r->status_message:NULL;
        }
    }
    /* No later config/import/error factory executes after this first failure.
     * Py_ExitStatusException is deliberately not called: it exits rather
     * than delivering the actual held native result to the command receiver. */
    return -1;
}
static int before_config(FridayPublisherRootColdResult *r,uint64_t own_copy) {
    uint64_t now;
    if(!r->pool||FridayPublisherMasterWorkClock(r->pool,r->pool->work_deadline_ns,
            FRIDAY_CLOCK_CONFIG,&now)<0) {
        r->phase="cold_original_work_clock_expired";
        return FridayPublisherMasterFault(r->pool,r->phase);
    }
    /* This is the concrete copy+native bookkeeping floor, not an invented
     * upper for stock CPython/provider implicit allocations. Those remain
     * part of required whole compiler/ABI/runtime cost qualification. */
    if(own_copy>UINT64_MAX-131072ULL)return -1;
    return FridayPublisherMasterBefore(r->pool,0,0,0,own_copy+131072ULL);
}
static int set_text(FridayPublisherRootColdResult *r,wchar_t **slot,
                    const wchar_t *value,const char *phase) {
    r->phase=phase;
    size_t n=wcslen(value);
    if(n>(UINT64_MAX/sizeof(wchar_t))-1||
       before_config(r,((uint64_t)n+1)*sizeof(wchar_t))<0)return -1;
    return keep_status(r,PyConfig_SetString(&r->config,slot,value));
}

/* Selected stock 3.14 public string inventory, including debug-only presite.
 * One list drives BOTH the actual pre-clear body reader and post-clear check.
 * dump_refs_file is separately retained: stock 3.14.4 PyConfig_Clear does not
 * clear that member. This isolated command never sets it; a non-NULL value
 * remains a concrete unretired owner, not an invented successful clear. */
#define COLD_CONFIG_STRINGS(X) \
 X(pycache_prefix,1) X(pythonpath_env,2) X(home,3) X(program_name,4) \
 X(stdlib_dir,5) X(executable,6) X(base_executable,7) X(prefix,8) \
 X(base_prefix,9) X(exec_prefix,10) X(base_exec_prefix,11) X(platlibdir,12) \
 X(sys_path_0,13) X(filesystem_encoding,14) X(filesystem_errors,15) \
 X(stdio_encoding,16) X(stdio_errors,17) X(run_command,18) \
 X(run_module,19) X(run_filename,20) X(check_hash_pycs_mode,21)
#define COLD_CONFIG_LISTS(X) \
 X(orig_argv,101) X(argv,102) X(xoptions,103) X(warnoptions,104) X(module_search_paths,105)

static int completion_clock(FridayPublisherRootColdResult *r,uint64_t *out) {
    if(r->completion_clock_failed)return -1; /* keep FIRST uncertainty;no retry */
    if(FridayPublisherMasterClockBlocked(r->pool)) {
        r->completion_clock_failed=1;return -1;
    }
    if(FridayPublisherNativeClockSample(&r->completion_clock_original,
            r->pool->started_ns,r->pool->deadline_ns,FRIDAY_CLOCK_COMPLETION,out)<0) {
        r->completion_clock_errno=r->completion_clock_original.error;
        r->completion_clock_failed=1;return -1;
    }
    return 0;
}
static int config_text(FridayPublisherColdConfigReceipt *v,uint64_t field,
                       uint64_t ordinal,const wchar_t *p) {
    if(v->text_count>=FRIDAY_ROOT_CONFIG_ROWS) {
        v->fault="config_text_inventory_capacity_originals_retained";return -1;
    }
    uint64_t id=v->text_count++;
    FridayPublisherColdConfigText *q=&v->text[id];
    q->field=field;q->ordinal=ordinal;q->original=p;
    if(!p)return 0;
    for(uint64_t i=0;i<id;i++)if(v->text[i].original==p) {
        q->alias=v->text[i].alias;q->body_at=v->text[i].body_at;
        q->body_bytes=v->text[i].body_bytes;return 0;
    }
    size_t room=(FRIDAY_ROOT_COMMAND_TEXT-v->body_bytes)/sizeof(wchar_t);
    size_t n=wcsnlen(p,room);
    if(n==room) {v->fault="config_full_string_capacity_original_retained";return -1;}
    q->alias=id+1;q->body_at=v->body_bytes;
    q->body_bytes=((uint64_t)n+1)*sizeof(wchar_t);
    memcpy(v->body+q->body_at,p,(size_t)q->body_bytes);
    v->body_bytes+=q->body_bytes;
    /* Actual bounded body correspondence before original owner retirement. */
    if(memcmp(v->body+q->body_at,p,(size_t)q->body_bytes))return -1;
    return 0;
}
static int config_list(FridayPublisherColdConfigReceipt *v,uint64_t field,
                       const PyWideStringList *p) {
    if(v->list_count>=FRIDAY_ROOT_CONFIG_LISTS||p->length<0||
       (uint64_t)p->length>FRIDAY_ROOT_CONFIG_ROWS-v->text_count||
       (p->length&&!p->items)) {
        v->fault="config_actual_list_bounds_originals_retained";return -1;
    }
    uint64_t id=v->list_count++;
    FridayPublisherColdConfigList *q=&v->lists[id];
    q->field=field;q->first=v->text_count;q->original_length=p->length;
    q->original_items=p->items;
    if(p->items) {
        q->array_alias=id+1;
        for(uint64_t i=0;i<id;i++)if(v->lists[i].original_items==p->items) {
            q->array_alias=v->lists[i].array_alias;break;
        }
    }
    for(Py_ssize_t i=0;i<p->length;i++) {
        if(!p->items[i]) {v->fault="config_NULL_list_item_originals_retained";return -1;}
        if(config_text(v,field,(uint64_t)i,p->items[i])<0)return -1;
        q->count++;
    }
    return 0;
}
static int config_snapshot(FridayPublisherRootColdResult *r) {
    FridayPublisherColdConfigReceipt *v=&r->config_receipt;
    if(v->attempted||!r->completion_reserved)return -1;
    v->attempted=1;
    memcpy(&v->historical_preconfig,&r->preconfig,sizeof(r->preconfig));
    memcpy(&v->historical_config,&r->config,sizeof(r->config));
#define SNAP_TEXT(name,id) if(config_text(v,id,0,r->config.name)<0)return -1;
    COLD_CONFIG_STRINGS(SNAP_TEXT)
    SNAP_TEXT(dump_refs_file,22)
#ifdef Py_DEBUG
    SNAP_TEXT(run_presite,23)
#endif
#undef SNAP_TEXT
#define SNAP_LIST(name,id) if(config_list(v,id,&r->config.name)<0)return -1;
    COLD_CONFIG_LISTS(SNAP_LIST)
#undef SNAP_LIST
    /* No intervening stock/Python factory, callback or mutation. The native
     * copied record is the actual command's reader, not a hash/count receipt. */
    if(memcmp(&v->historical_config,&r->config,sizeof(r->config))) {
        v->fault="config_changed_during_actual_copy";return -1;
    }
    for(uint64_t i=0;i<v->body_bytes;i++) {
        if((i&65535)==0) {
            uint64_t now;if(completion_clock(r,&now)<0) {
                v->fault="config_full_read_original_end_clock";return -1;
            }
        }
        v->reader_sink^=v->body[i];
    }
    v->full_bytes_read=v->body_bytes;v->complete=1;return 0;
}
static int config_clearable(FridayPublisherRootColdResult *r) {
    FridayPublisherColdConfigReceipt *v=&r->config_receipt;
    if(!v->complete||v->full_bytes_read!=v->body_bytes)return -1;
    if(r->config.dump_refs_file) {
        v->fault="stock_Clear_does_not_retire_dump_refs_file";return -1;
    }
    /* The stock clearer frees each owning slot. Preserve exact aliases and
     * refuse a double-free shape rather than silently changing the graph. */
    for(uint64_t i=0;i<v->text_count;i++)
        if(v->text[i].original&&v->text[i].alias!=i+1) {
            v->fault="aliased_config_owning_string_slots";return -1;
        }
    for(uint64_t i=0;i<v->list_count;i++)
        if(v->lists[i].original_items&&v->lists[i].array_alias!=i+1) {
            v->fault="aliased_config_owning_list_slots";return -1;
        }
    return 0;
}
static int config_closed(const PyConfig *p) {
#define CHECK_TEXT(name,id) if(p->name)return 0;
    COLD_CONFIG_STRINGS(CHECK_TEXT)
    CHECK_TEXT(dump_refs_file,22)
#ifdef Py_DEBUG
    CHECK_TEXT(run_presite,23)
#endif
#undef CHECK_TEXT
#define CHECK_LIST(name,id) if(p->name.length||p->name.items)return 0;
    COLD_CONFIG_LISTS(CHECK_LIST)
#undef CHECK_LIST
    return p->module_search_paths_set==0;
}
static int install_owned_builtin_table(FridayPublisherRootColdResult *r) {
    r->builtin_phase="owned_table_preflight";
    if(r->builtin_installed||r->builtin_copy_complete||!r->completion_reserved||
       Py_IsInitialized()||!PyImport_Inittab)return -1;
    r->builtin_previous=PyImport_Inittab;
    uint64_t i;
    for(i=0;i<FRIDAY_ROOT_BUILTIN_ROWS;i++) {
        r->builtin_current_index=i;r->builtin_phase="owned_table_original_row";
        struct _inittab q=r->builtin_previous[i];
        r->builtin_current_row=q;
        if(!q.name)break;
        r->builtin_phase="owned_table_full_name_bound";
        size_t room=FRIDAY_ROOT_BUILTIN_NAMES-r->builtin_name_bytes;
        size_t n=strnlen(q.name,room);
        if(n==room)return -1;
        r->builtin_phase="owned_table_provider_name_collision";
        if(n==sizeof("publisher_owned_custody")-1&&
           !memcmp(q.name,"publisher_owned_custody",n))return -1;
        char *owned=r->builtin_names+r->builtin_name_bytes;
        r->builtin_phase="owned_table_full_name_copy";
        memcpy(owned,q.name,n+1);
        if(memcmp(owned,q.name,n+1))return -1;
        r->builtin_entries[i].name=owned;
        r->builtin_entries[i].initfunc=q.initfunc;
        r->builtin_name_bytes+=(uint64_t)n+1;r->builtin_count=i+1;
        r->builtin_read_bytes+=sizeof(q)+(uint64_t)n+1;
    }
    r->builtin_phase="owned_table_terminated_original_inventory";
    if(i==FRIDAY_ROOT_BUILTIN_ROWS)return -1;
    const char own_name[]="publisher_owned_custody";
    if(sizeof(own_name)>FRIDAY_ROOT_BUILTIN_NAMES-r->builtin_name_bytes)return -1;
    char *owned=r->builtin_names+r->builtin_name_bytes;
    memcpy(owned,own_name,sizeof(own_name));
    r->builtin_entries[i].name=owned;
    r->builtin_entries[i].initfunc=PyInit_publisher_owned_custody;
    r->builtin_name_bytes+=sizeof(own_name);r->builtin_count=i+1;
    r->builtin_entries[i+1].name=NULL;r->builtin_entries[i+1].initfunc=NULL;
    r->builtin_phase="owned_table_before_install_clock";
    uint64_t now;if(completion_clock(r,&now)<0)return -1;
    if(PyImport_Inittab!=r->builtin_previous)return -1;
    r->builtin_copy_complete=1;
    /* Public pre-initialization table, owned in this command from birth.
     * Stock CPython copies it into interpreter runtime state at init.
     * The original public table remains BORROWED, never freed or edited.
     * No call to private _PyImport_Fini2 or Py_RunMain is substituted. */
    PyImport_Inittab=r->builtin_entries;r->builtin_installed=1;
    r->builtin_phase="actual_owned_table_installed";return 0;
}
static void restore_owned_builtin_table(FridayPublisherRootColdResult *r) {
    if(!r->builtin_installed||r->builtin_restore_attempted)return;
    /* No init ever used our table, or the actual owned finalizer returned
     * with no initialized runtime. A partially initialized unknown prefix
     * retains the whole native table and all bodies; no false borrow end. */
    if(r->init_attempted&&
       !(r->finalization_attempted&&r->runtime_end_observed))return;
    if(Py_IsInitialized())return;
    r->builtin_restore_attempted=1;
    if(PyImport_Inittab!=r->builtin_entries||!r->builtin_previous) {
        r->completion_phase="cold_owned_builtin_table_identity_UNCONFIRMED";return;
    }
    PyImport_Inittab=r->builtin_previous;
    r->builtin_restored=PyImport_Inittab==r->builtin_previous;
    if(r->builtin_restored) {
        r->builtin_installed=0;r->builtin_borrow_end_confirmed=1;
        r->builtin_phase="actual_owned_table_restored_after_borrow_end";
    }
    /* Preowned entries/names remain the complete historical body. There is
     * no free of a provider table and no read through a freed runtime copy. */
}
static void cold_complete(FridayPublisherRootColdResult *r) {
    if(r->completion_attempted)return;
    r->completion_attempted=1;r->operation_phase=r->phase;
    r->completion_phase="cold_original_completion";
    if(!r->pool||!r->completion_reserved) {
        r->completion_phase="cold_no_effect_cleanup_reserve";goto done;
    }
    if(completion_clock(r,&r->completion_started_ns)<0) {
        r->completion_phase="cold_original_end_clock_UNCONFIRMED";goto done;
    }
    if(r->first_status_saved&&!r->status_bodies_complete) {
        r->completion_phase="cold_full_first_status_retained_UNCONFIRMED";goto done;
    }
    if(r->configuration_owned) {
        if(config_snapshot(r)<0||config_clearable(r)<0) {
            r->completion_phase="cold_full_config_retained_UNCONFIRMED";goto done;
        }
        if(completion_clock(r,&r->completion_ended_ns)<0) {
            r->completion_phase="cold_before_Clear_clock_UNCONFIRMED";goto done;
        }
        r->config_receipt.clear_attempted=1;
        PyConfig_Clear(&r->config);
        r->config_receipt.clear_returned=1;
        /* Phase-correct: inspect now-cleared fields and retained historical
         * bodies; NEVER dereference historical_config/text/list addresses. */
        r->config_receipt.closed_fields_confirmed=config_closed(&r->config);
        if(!r->config_receipt.closed_fields_confirmed) {
            r->completion_phase="cold_Clear_returned_with_unretired_fields";goto done;
        }
        r->configuration_owned=0;r->config_end_confirmed=1;
    } else if(!r->config_receipt.clear_attempted) {
        /* Configuration was never acquired, not a fabricated clear event. */
        r->config_end_confirmed=1;
    }
    r->finalization_runtime_present_before=Py_IsInitialized();
    if(!r->finalization_runtime_present_before) {
        /* This is only the observed initialized flag, not retirement of a
         * preinitialization or failed initialization prefix. BOTH consumers
         * below use the same phase predicate before releasing support. */
        r->runtime_end_observed=1;
        r->completion_phase=(r->preinit_attempted||r->init_attempted)?
            "cold_initialization_prefix_custody_still_held":
            "config_end_no_runtime_attempt";goto done;
    }
    /* No Root effect on failed initialization, or the actual preowned Root
     * receiver completed full data, Source references/errors/FDs and explicit
     * Source registrations. The SAME interpreter's remaining support is the
     * next concrete phase, not a prerequisite that must already have ended.
     * NOT an intake/sealed/count/SourceReady-only shortcut. */
    int root_ended=!r->perform_attempted||
        (r->receiver_attempted&&r->receiver_returned&&r->receiver_rc==0&&
         r->receipt&&r->receipt->sealed&&!r->receipt->receiving&&
         r->receipt->source_owner_end_confirmed&&r->receipt->runtime_finalization_eligible&&
         r->receipt->cold_runtime_bound&&r->receipt->runtime_support_pending);
    PyThreadState *thread=PyThreadState_GetUnchecked();
    r->runtime_end_eligible=r->runtime_owned&&!r->runtime_was_external&&root_ended&&
        r->owner_pid==getpid()&&r->owner_thread==PyThread_get_thread_ident()&&
        thread&&r->initialized_interpreter&&
        PyThreadState_GetInterpreter(thread)==r->initialized_interpreter&&
        r->initialized_interpreter==PyInterpreterState_Main()&&!PyErr_Occurred();
    if(!r->runtime_end_eligible) {
        r->completion_phase="config_end_actual_Root_runtime_custody_still_held";goto done;
    }
    if(completion_clock(r,&r->completion_ended_ns)<0) {
        r->completion_phase="cold_before_Finalize_clock_UNCONFIRMED";goto done;
    }
    r->finalization_attempted=1;
    r->finalization_rc=Py_FinalizeEx();
    r->finalization_runtime_present_after=Py_IsInitialized();
    r->runtime_end_observed=!r->finalization_runtime_present_after;
    if(r->runtime_end_observed){r->runtime_owned=0;r->partial_runtime_owned=0;}
    r->completion_phase=r->runtime_end_observed?
        (r->finalization_rc==0?"actual_owned_runtime_Finalize_returned":
         "actual_owned_runtime_Finalize_returned_failure_retained"):
        "actual_owned_runtime_Finalize_end_UNCONFIRMED";
done:
    restore_owned_builtin_table(r);
    /* This names our direct Source/native registration edges and the actual
     * owned interpreter API end, not zero remaining CPython private heap.
     * Required public finalizer observations/captures and native recipient
     * handoff still belong to the external selected qualification/end path. */
    if(r->pool&&r->completion_reserved&&
       completion_clock(r,&r->completion_ended_ns)<0)
        r->completion_phase="cold_completion_original_clock_UNCONFIRMED";
    /* An ordinary error can leave configuration, Source or a partially
     * initialized runtime owned. Then its utility support must remain held:
     * reaching this label alone is NOT evidence of the last budget user.
     * Never claim Py_IsInitialized()==0 retires a failed init's unknown
     * prefix. An eligible real finalizer return or no preinit/init attempt
     * is required; the final receiver uses this same phase distinction. */
    if(r->pool&&!r->completion_clock_failed) {
        int last_consumer=!r->configuration_owned&&
            FridayPublisherRootColdRuntimeEndConfirmed(r)&&
            (!r->perform_attempted||(r->receipt&&r->receipt->sealed&&
              r->receipt->source_owner_end_confirmed));
        r->utility_receiver_attempted=1;
        r->utility_receiver_rc=FridayPublisherRootUtilityFinish(last_consumer,&r->utility_receipt);
        r->utility_end_confirmed=r->utility_receiver_rc==0&&r->utility_receipt&&
            r->utility_receipt->sealed&&r->utility_receipt->pid==r->owner_pid&&
            r->utility_receipt->complete&&r->utility_receipt->utility_FD_end_confirmed;
        if(!r->utility_end_confirmed)
            r->completion_phase="cold_actual_utility_end_or_observation_UNCONFIRMED";
    }
    r->root_runtime_support_end_confirmed=r->perform_attempted&&
        r->receipt&&r->receipt->sealed&&r->receipt->source_owner_end_confirmed&&
        r->receipt->runtime_finalization_eligible&&r->finalization_attempted&&
        r->runtime_end_observed&&r->builtin_borrow_end_confirmed&&r->utility_end_confirmed;
    /* Configuration/runtime observations are NOT the standalone outside
     * recipient's full native body handoff or whole accepted support end.
     * Final return leaves this preowned native record alive in the SAME Root.
     * No old tuple or already sealed CommandReceipt is modified here. */
    r->command_end_confirmed=0;r->completion_returned=1;
}
int FridayPublisherRootColdPerform(const char *case_id,const FridayPublisherRootColdResult **out) {
    FridayPublisherRootColdResult *r=FridayPublisherRootBankColdStorage();
    if(!out||!FridayPublisherRootBankAttached()||!r||r->attempted)return -1;
    /* Do not expose an in-progress result and later rewrite its final status. */
    *out=NULL;r->attempted=1;r->owner_pid=getpid();
    r->phase="cold_preflight";
    /* Never take over an already-owned interpreter or finalize it later. */
    if(Py_IsInitialized()){r->runtime_was_external=1;r->phase="external_runtime_already_initialized";goto complete;}
    if(!case_id){r->phase="missing_original_case";goto complete;}
    if(FridayPublisherRootColdPool(r,&r->pool)<0) {
        r->phase="original_cold_pool_unavailable";goto complete;
    }
    r->started_ns=r->pool->started_ns;r->deadline_ns=r->pool->deadline_ns;
    /* Reserve the complete bounded first-status read before the first stock
     * factory. A later work-pool refusal cannot erase its error-copy credit.
     * Buffers themselves were included in sizeof(*r) at original bank/pool birth.
     * Overflow remains retained/incomplete, never silently truncated. */
    if(FridayPublisherMasterBefore(r->pool,
            4ULL*(3ULL*(FRIDAY_ROOT_COMMAND_TEXT+1ULL)+
                FRIDAY_ROOT_CONFIG_ROWS*sizeof(void *))+
            (uint64_t)FRIDAY_ROOT_CONFIG_ROWS*(FRIDAY_ROOT_CONFIG_ROWS+1ULL)/2ULL*
                sizeof(FridayPublisherColdConfigText)+
            4ULL*FRIDAY_ROOT_BUILTIN_NAMES+2ULL*sizeof(r->builtin_entries)+
            2ULL*sizeof(r->config)+sizeof(r->preconfig),0,0,0)<0) {
        r->phase="cold_first_status_read_reserve_refused";goto complete;
    }
    r->completion_reserved=1;
    r->phase="cold_original_case_full_copy_before_runtime";
    if(FridayPublisherRootCaseCopy(r->pool,&r->case_input,case_id)<0)goto complete;
    PyPreConfig_InitIsolatedConfig(&r->preconfig);
    r->preconfig.utf8_mode=1;
    r->phase="cold_preinitialization";
    if(before_config(r,sizeof(r->preconfig))<0)goto complete;
    r->preinit_attempted=1;
    if(keep_status(r,Py_PreInitialize(&r->preconfig))<0)goto complete;
    r->preinit_completed=1;
    PyConfig_InitIsolatedConfig(&r->config);r->configuration_owned=1;
    r->config.isolated=1;r->config.use_environment=0;r->config.parse_argv=0;
    r->config.safe_path=1;r->config.site_import=0;r->config.user_site_directory=0;
    r->config.write_bytecode=0;r->config.install_signal_handlers=0;
    r->config.configure_c_stdio=0;r->config.pathconfig_warnings=0;
    /* Explicit selected stock 3.14 paths, not argv/PATH/PYTHONPATH/.pth/site.
     * These names still require exact selected-image/ELF admission before
     * this source may execute. They do not certify the host runtime.
     * Set every path output, not only home, to prevent default path search. */
    if(set_text(r,&r->config.home,L"/usr","cold_home")<0||
       set_text(r,&r->config.program_name,L"/proc/self/exe","cold_program")<0||
       set_text(r,&r->config.executable,L"/proc/self/exe","cold_executable")<0||
       set_text(r,&r->config.base_executable,L"/proc/self/exe","cold_base_executable")<0||
       set_text(r,&r->config.prefix,L"/usr","cold_prefix")<0||
       set_text(r,&r->config.base_prefix,L"/usr","cold_base_prefix")<0||
       set_text(r,&r->config.exec_prefix,L"/usr","cold_exec_prefix")<0||
       set_text(r,&r->config.base_exec_prefix,L"/usr","cold_base_exec_prefix")<0||
       set_text(r,&r->config.platlibdir,L"lib","cold_platlibdir")<0)goto complete;
    r->phase="cold_stock_module_paths";
    wchar_t *paths[]={L"/usr/lib/python3.14",L"/usr/lib/python3.14/lib-dynload"};
    if(before_config(r,sizeof(paths)+
       (wcslen(paths[0])+wcslen(paths[1])+2)*sizeof(wchar_t))<0)goto complete;
    if(keep_status(r,PyConfig_SetWideStringList(&r->config,&r->config.module_search_paths,2,paths))<0)goto complete;
    r->config.module_search_paths_set=1;
    r->phase="cold_fixed_argv";
    wchar_t *argv[]={L"friday-publisher-root"};
    if(before_config(r,sizeof(argv)+(wcslen(argv[0])+1)*sizeof(wchar_t))<0)goto complete;
    if(keep_status(r,PyConfig_SetArgv(&r->config,1,argv))<0)goto complete;
    /* Actual linked native provider, with Root-preowned complete table/names.
     * AppendInittab's separate allocation survives stock Py_FinalizeEx;
     * do not create that hidden final owner and then report it retired. */
    r->phase="cold_builtin_native_provider";
    if(before_config(r,4096)<0)goto complete;
    r->builtin_attempted=1;
    r->builtin_rc=install_owned_builtin_table(r);
    if(r->builtin_rc<0)goto complete;
    r->phase="cold_stock_runtime_initialize";
    if(before_config(r,0)<0)goto complete;
    r->init_attempted=1;
    PyStatus init_status=Py_InitializeFromConfig(&r->config);
    r->runtime_present_after_init=Py_IsInitialized();
    if(r->runtime_present_after_init) {
        r->runtime_owned=1;r->owner_thread=PyThread_get_thread_ident();
        PyThreadState *thread=PyThreadState_GetUnchecked();
        if(thread)r->initialized_interpreter=PyThreadState_GetInterpreter(thread);
    }
    if(keep_status(r,init_status)<0) {
        r->partial_runtime_owned=r->runtime_present_after_init;
        goto complete;
    }
    r->init_completed=1;
    if(!Py_IsInitialized()){r->phase="cold_success_without_initialized_runtime";goto complete;}
    r->phase="cold_bind_actual_owned_main_runtime";r->runtime_bind_attempted=1;
    r->runtime_bind_rc=FridayPublisherRootColdRuntimeBind(r->pool);
    if(r->runtime_bind_rc<0)goto complete;
    /* Configuration stays preowned until the actual command receiver has
     * consumed its native result. No failure-path clear invalidates status.
     * In particular RootPerform does NOT reset the cold clock or any debit. */
    r->phase="cold_actual_RootPerform";r->perform_attempted=1;
    r->perform_rc=FridayPublisherRootPerform(r->case_input.body,&r->terminal);
    if(!r->terminal){r->phase="actual_RootPerform_missing_result";goto complete;}
    /* No Python factory, normalization or status replacement between the
     * actual call and its one preowned receiver. A failing call can still
     * return real terminal/error owners; its rc never bypasses this intake. */
    FridayPublisherRootCommandReceipt *receiver=
        FridayPublisherRootCommandReceiptStorage(r->terminal);
    if(!receiver){r->phase="actual_RootPerform_receiver_storage_missing";goto complete;}
    r->receipt=receiver;r->receiver_attempted=1;
    r->receiver_rc=FridayPublisherRootCommandReceive(r->terminal,receiver);
    r->receiver_returned=1;
    if(r->receiver_rc<0||!receiver->attempted||!receiver->sealed||receiver->receiving||
       receiver->pid!=r->owner_pid||receiver->thread!=r->owner_thread) {
        r->phase="actual_RootPerform_receiver_UNCONFIRMED";goto complete;
    }
    r->phase=receiver->source_owner_end_confirmed?
        "actual_Source_refs_ended_cold_config_runtime_pending":
        "actual_RootPerform_receiver_owns_incomplete_originals";
complete:
    /* The ACTUAL normal and every early/error return converge here once.
     * Neither config cleanup nor runtime eligibility replaces required Root
     * owner retirement or the final full-data outside recipient handoff. */
    cold_complete(r);
    /* BOTH outcomes use the actual prebound native recipient. This is not
     * an optional callback or an uncalled wrapper. The old phase receipts
     * remain unchanged; an incomplete handoff keeps its original owner. */
    (void)FridayPublisherRootFinalReceive(r);
    *out=r;
    return 0;
}
