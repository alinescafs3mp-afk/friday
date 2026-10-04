/* A202 inert Source, based on SOL071. Original selected native provider only,
 * NOT a grant API. No Source/runtime execution is authorized by this file.
 * No service, process, role, JSON permit constructor or clock is added.
 * This is new authored code; it is not a recovered private host artifact.
 */
#ifndef FRIDAY_SCANNER_SELECTED_PARENT_H
#define FRIDAY_SCANNER_SELECTED_PARENT_H
#include <stdint.h>
#include <stddef.h>
#include <Python.h>
#define FRIDAY_SCANNER_PARENT_ABI UINT64_C(0x4652413230320001)
enum friday_scanner_parent_state {
    FRIDAY_SCANNER_PARENT_ABSENT=0,
    FRIDAY_SCANNER_PARENT_PREOWNED=1,
    FRIDAY_SCANNER_PARENT_UNCONFIRMED=2,
    FRIDAY_SCANNER_PARENT_REGISTERED_RETIRED=3
};
struct friday_scanner_parent_result {
    uint64_t abi,generation,original_start_ns,original_end_ns;
    int state,status,primary_errno,receiver_errno;
    int source_reaped,root_reaped,task_exited,late_error;
    int original_guard_armed,original_end_reached;
    int independently_accepted,outside_final_end_confirmed;
    const void *complete_root_backing;
    size_t complete_root_backing_bytes;
    const void *complete_source_backing;
    size_t complete_source_backing_bytes;
    uint64_t source_read_bytes,root_read_bytes,root_output_bytes;
    /* The actual registered Root pointer graph is inside backing, NOT a
     * foreign PyObject* to dereference in a separately selected caller.
     * Static/native addresses need the exact selected image correspondence.
     */
};
struct friday_scanner_selected_enrollment {
    uint64_t abi,generation,original_start_ns,original_end_ns;
    uint64_t original_minimum_consumer_end_ns;
    int root_backing_fd,root_readonly_fd;
    size_t root_backing_bytes;
    unsigned char root_binding_sha256[32];
    int source_backing_fd,source_readonly_fd;
    size_t source_backing_bytes;
    unsigned char source_binding_sha256[32];
    /* Borrowed original privileged native owner. It retains all FOUR descriptions
     * and exact backing before this call and through its own actual end.
     * The same original absolute guard remains armed across a failed or
     * successful return. A return is not the provider's final end or a
     * release of its descriptions, context, enrollment or result storage.
     * They are not passed in Source JSON or created by Source.
     */
    void *original_provider_context;
    /* Called in the original interpreter. Returns the original five-argument
     * tuple (permit, selected_permit, selection, Root contract, Source launch).
     * This must be the separately selected ORIGINAL issuer's actual factory,
     * not a new Python callback/permit reconstructed from bootstrap prose.
     * The implementation/ref/hash of that original factory is an enrollment
     * dependency, explicitly NOT supplied by this Source-only package.
     */
    PyObject *(*original_materials_factory)(void *original_provider_context);
};
int friday_scanner_selected_parent_entry(
    const struct friday_scanner_selected_enrollment *,
    struct friday_scanner_parent_result *);
#endif
