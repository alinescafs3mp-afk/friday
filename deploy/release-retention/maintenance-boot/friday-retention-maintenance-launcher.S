.section .rodata
runner_path:
    .asciz "/usr/libexec/friday/release_artifact_retention_maintenance_runner.sh"

.section .text
.global _start
.type _start, @function
_start:
    mov %rsp, %r12

    # This binary is selected directly by rdinit=.  Any invocation that is
    # not the first userspace PID has no premount authority.
    mov $39, %rax
    syscall
    cmp $1, %rax
    jne fatal
    mov $110, %rax
    syscall
    test %rax, %rax
    jne fatal

    mov (%r12), %rcx
    lea 16(%r12,%rcx,8), %rdx
    lea 8(%r12), %rsi
    lea runner_path(%rip), %rdi
    mov %rdi, (%rsi)
    mov $59, %rax
    syscall

fatal:
    # Exiting PID 1 is intentionally fatal.  There is no ordinary-init
    # fallback and no second process that can continue booting.
    mov $125, %rdi
    mov $60, %rax
    syscall
    ud2
.size _start, .-_start

.section .note.GNU-stack,"",@progbits
