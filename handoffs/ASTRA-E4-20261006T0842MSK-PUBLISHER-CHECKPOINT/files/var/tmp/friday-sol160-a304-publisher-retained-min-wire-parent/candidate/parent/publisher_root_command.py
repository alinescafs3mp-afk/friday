"""Inert paired Publisher Root caller in the EXISTING exclusive gate parent.

Not a scheduler, issuer, release controller or admission bypass. The existing
canonical caller must admit the exact executable/layout/dependencies before this
helper is invoked. No such admission or invocation has been established here.
Complete evidence stays in the caller's existing private retention collection.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, MutableSequence

from .quality_gate_process import OwnedCommandScope, CleanupProof

MAGIC = 0x314B4E4142445246
BANK_VERSION = 6
HEADER = 4096
READ_CAP = 40_960_000_000
RAM_CAP = 8_589_934_592
OUTPUT_CAP = 33_554_432
SCRATCH = 1_048_576
INITIAL_SEALS = fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW
FINAL_SEALS = INITIAL_SEALS | fcntl.F_SEAL_FUTURE_WRITE | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SEAL
BIND_AT = 640
BIND_BYTES = 1408
BIND_MAGIC = 0x31444E4942445246
BIND_EVENTS = 22
RESERVATION_MAGIC = 0x3156534552445246
PUBLICATION_AT = 3840
PUBLICATION_BYTES = 256
PUBLICATION_MAGIC = 0x3155425042445246
PUBLICATION_VERSION = 2
FINAL_DEADLINE_WORD = 65
PUBLICATION_OPERAND_BYTES = 65536
PUBLICATION_OPERAND_STORAGE = 2*PUBLICATION_OPERAND_BYTES


class BankUnconfirmed(RuntimeError):
    """Evidence/cleanup uncertainty, never an ordinary accepted completion."""


@dataclass(frozen=True)
class NativeBindEvent:
    operation: int
    returncode: int
    errno: int
    operand: int
    extent: int
    detail: int


@dataclass(frozen=True)
class NativeBindRecord:
    # Full actual ordinary public DATA, including every reached cleanup row.
    # Addresses are numeric facts only; no dead Root address is dereferenced.
    raw: bytes
    words: tuple[int, ...]
    events: tuple[NativeBindEvent, ...]


class BankNativeFailure(BankUnconfirmed):
    """Operation failed; actual full bind data and kernel end were received.

    This is not BankCompletion, successful operation, source acceptance or GO.
    The original first and secondary native errors remain in the exact record.
    """

    def __init__(self, bank: PublisherBank, record: NativeBindRecord):
        super().__init__("bank_native_bind_failed_data_received")
        self.bank = bank
        self.record = record


@dataclass(frozen=True)
class NativePublicationRecord:
    # Complete primitive record, NOT complete operand/body/error-history custody.
    raw: bytes
    words: tuple[int, ...]
    required_native_bodies_complete: bool = False


class BankPublicationFailure(BankUnconfirmed):
    """Native failed; original primitive and actual partial destination retained.

    Never BankCompletion. Clock failures without the actual record are HELD;
    numeric operands are not dead-owner body handoff or successful publication.
    """
    def __init__(self, bank: PublisherBank, record: NativePublicationRecord):
        super().__init__("bank_publication_failed_partial_native_custody")
        self.bank = bank
        self.record = record


def _identity(fd: int) -> tuple[int, ...]:
    s = os.fstat(fd)
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_nlink,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns)


@dataclass(frozen=True)
class QualifiedBankLayout:
    """Exact compiler/image DATA supplied by the already authorized qualification.

    Construction is not qualification. Caller authentication must bind these
    bytes to the actually admitted executable, ABI and dependency inventory.
    There is deliberately no default or guessed sizeof/private layout.
    """
    root_at: int
    root_bytes: int
    cold_at: int
    cold_bytes: int
    caller_at: int
    caller_bytes: int
    total: int
    read_reserve: int
    allocation: int

    def check(self) -> None:
        values = (self.root_at, self.root_bytes, self.cold_at, self.cold_bytes,
                  self.caller_at, self.caller_bytes, self.total,
                  self.read_reserve, self.allocation)
        if any(type(x) is not int or x <= 0 or x >= 2**64 for x in values):
            raise BankUnconfirmed("bank_layout_integer_domain")
        align = lambda n: (n + 63) & ~63
        if not (
            self.root_at == HEADER
            and self.cold_at == align(self.root_at + self.root_bytes)
            and self.caller_at == align(self.cold_at + self.cold_bytes)
            and self.total == align(self.caller_at + self.caller_bytes)+PUBLICATION_OPERAND_STORAGE
            and self.allocation == 2*self.total + SCRATCH < RAM_CAP
            and self.read_reserve == 7 * self.total + 4 * SCRATCH < READ_CAP
        ):
            raise BankUnconfirmed("bank_layout_original_caps_or_offsets")


@dataclass(frozen=True)
class BankCompletion:
    # Custody completion, NOT operation success, source acceptance or release GO.
    outcome: str
    process_id: int
    returncode: int
    full_bytes_read: int
    data_sha256: str
    header: bytes
    cleanup: CleanupProof
    bank: PublisherBank
    retained_deadline_ns: int
    SourceReady: bool = False
    Root_admission: bool = False
    GO: bool = False


@dataclass(frozen=True)
class OriginalBankReservation:
    """Fixed original bank debit recorded before acquisition, not a new pool.

    No caps/token rows/issuer are configurable here. Native code recomputes the
    same amounts from its own qualified layout before its one pool constructor
    adopts them. This record is NOT image/resource admission or a measured bound.
    """
    parent_pid: int
    started_ns: int
    deadline_ns: int
    read_bytes: int
    allocation_bytes: int
    output_bytes: int
    generation: int = 1


@dataclass(frozen=True)
class BankFinalResult:
    """New final record, published only after data retention and confirmed close.

    Full retained native storage is DATA, not proof that every historical pointer
    denotes a copied object. Whole native/body/alias qualification is still
    required. A failed native operation remains failed after custody succeeds.
    """
    outcome: str
    process_id: int
    returncode: int
    header: bytes
    body: bytes | None
    data_sha256: str | None
    native_bind: NativeBindRecord
    cleanup: CleanupProof
    historical_fd_identity: tuple[int, ...]
    descriptor_closed: bool
    finalized_ns: int
    original_reservation: OriginalBankReservation
    retained_deadline_ns: int
    SourceReady: bool = False
    Root_admission: bool = False
    GO: bool = False


@dataclass(frozen=True)
class BankSetupFailureResult:
    """Parent-only failed setup custody, never native operation completion.

    No Root launch was attempted. The exact original exception and actual raw
    storage survive descriptor retirement; this does not certify an unrelated
    process-scope failure, native data, image admission or semantic whole closure.
    """
    descriptor_state: str
    historical_fd_identity: tuple[int, ...] | None
    body: bytes | None
    primary_error: BaseException
    process_cleanup: CleanupProof | None
    original_reservation: OriginalBankReservation | None
    finalized_ns: int
    SourceReady: bool = False
    Root_admission: bool = False
    GO: bool = False


@dataclass(frozen=True)
class BankPublicationRetirement:
    """New immutable failed-destination record AFTER retention and once close.

    Every actual receiving byte is retained. Uncopied working data, compared
    operands and native body/alias endpoints are NOT thereby made complete.
    """
    process_id: int
    returncode: int
    header: bytes
    destination_body: bytes
    primary_error: BankPublicationFailure
    native_publication: NativePublicationRecord
    native_bind: NativeBindRecord
    cleanup: CleanupProof
    historical_fd_identity: tuple[int, ...]
    descriptor_closed: bool
    finalized_ns: int
    original_reservation: OriginalBankReservation
    comparison_operands: tuple[bytes, bytes] | None
    retained_deadline_ns: int
    required_native_bodies_complete: bool = False
    SourceReady: bool = False
    Root_admission: bool = False
    GO: bool = False


@dataclass(frozen=True)
class BankReceiveFailureResult:
    """Raw receiving-bank custody after a failed decode, NOT native acceptance.

    The actual child end and no-writer seal were confirmed before the one full
    raw intake. Invalid/missing semantic fields stay invalid; retaining their
    bytes cannot turn them into complete native value-body or alias evidence.
    """
    process_id: int
    returncode: int
    header: bytes
    body: bytes
    data_sha256: str
    primary_error: BaseException
    native_bind: NativeBindRecord | None
    cleanup: CleanupProof
    historical_fd_identity: tuple[int, ...]
    descriptor_closed: bool
    finalized_ns: int
    original_reservation: OriginalBankReservation
    retained_deadline_ns: int
    SourceReady: bool = False
    Root_admission: bool = False
    GO: bool = False


class PublisherBank:
    """One bank in the EXISTING parent's evidence lifetime, not an observer chain."""

    def __init__(self, layout: QualifiedBankLayout, image_sha256: str):
        if type(layout) is not QualifiedBankLayout:
            raise BankUnconfirmed("bank_exact_qualified_layout_type")
        layout.check()
        if (type(image_sha256) is not str or len(image_sha256) != 64
                or any(c not in "0123456789abcdef" for c in image_sha256)):
            raise BankUnconfirmed("bank_image_identity_domain")
        self.layout = layout
        self.image_sha256 = image_sha256
        self.owner_pid = os.getpid()
        self.fd = -1
        self.started = self.deadline = 0
        # D0 is immutable; this separate DATA bound can only restrict it.
        # UNREAD permits only the existing original-prefix/prelaunch interval.
        # Once output intake begins, an absent/unreturned bound is HELD.
        self.retained_deadline = 0
        self.min_wire_phase = "UNREAD"
        self.min_wire_error = None
        self.min_header_value = None
        self.min_publication_value = None
        self.input_header = b""
        self.birth = None
        self.final_identity = None
        self.scope = None
        self.process_id = None
        self.cleanup = None
        self.completion = None
        self.creation_attempted = self.receive_attempted = False
        self.close_attempted = self.closed = False
        self.fault = None
        self.native_bind_raw = None
        self.native_bind = None
        self.prebank_header = None
        self.prebank_end_confirmed = False
        self.parent_faults = []
        self.close_fault = None
        self.received_header = None
        self.body_buffer = None
        self.body_bytes_written = 0
        self.read_chunk = None
        self.final_body = None
        self.finish_attempted = False
        self.final_result = None
        self.reservation_attempted = False
        self.original_reservation = None
        self.launch_attempted = False
        self.setup_failure_finish_attempted = False
        self.setup_failure_result = None
        self.setup_failure_seals_before = None
        self.setup_failure_identity_before = None
        self.secondary_fault = None
        self.retirement_fault = None
        self.fault_collection_error = None
        self.publication_raw = None
        self.native_publication = None
        self.publication_fault = None
        self.publication_end_confirmed = False
        self.publication_failure_result = None
        self.comparison_source_body = None
        self.comparison_destination_body = None
        self.raw_receive_attempted = False
        self.raw_body_retained = False
        self.raw_identity_before = None
        self.raw_data_sha256 = None
        self.received_seals_before = None
        self.received_seals_after = None
        self.receive_failure_finish_attempted = False
        self.receive_failure_result = None
        self.deferred_decode_fault = None
        self.prebank_fault = None
        # Registered with this SAME retained bank before acquisition. Failed or
        # unreturned clock attempts are terminal; finalization never retries a
        # clock to manufacture a later timestamp. This is not clock admission.
        self.clock_phase = "UNUSED"
        self.clock_result = None
        self.clock_error = None

    def _owner(self) -> None:
        if self.owner_pid != os.getpid():
            raise BankUnconfirmed("bank_foreign_parent")

    def _remember_fault(self, exc: BaseException) -> None:
        # Native/format first, reached intake error, and failed retirement can
        # all be distinct. A close can report the SAME last exception before
        # the outer catch. Pre-existing slots retain all three before fallible
        # list growth; its own failure has a separate slot and is not retried.
        if exc is self.fault or exc is self.secondary_fault or exc is self.retirement_fault:
            return
        if self.fault is None:
            self.fault = exc
        elif exc is not self.fault and self.secondary_fault is None:
            self.secondary_fault = exc
        elif self.retirement_fault is None:
            self.retirement_fault = exc
        if self.fault_collection_error is None:
            try:
                self.parent_faults.append(exc)
            except BaseException as collection_error:
                self.fault_collection_error = collection_error

    def reserve_original_bank(self) -> None:
        self._owner()
        if self.reservation_attempted or self.creation_attempted:
            raise BankUnconfirmed("bank_original_reservation_once_before_acquisition")
        self.reservation_attempted = True
        self.layout.check()
        started = self._clock("reservation")
        # Existing compiled caps, not values chosen by this caller or Source.
        # Qualification must bind this exact layout/implicit overhead to the
        # actual admitted image. The declaration alone does not grant execution.
        if not (self.layout.read_reserve < READ_CAP
                and self.layout.allocation < RAM_CAP and HEADER <= OUTPUT_CAP):
            raise BankUnconfirmed("bank_original_reservation_caps")
        self.started = started
        self.deadline = started + 4_200_000_000_000
        self.retained_deadline = self.deadline
        self.original_reservation = OriginalBankReservation(
            self.owner_pid, self.started, self.deadline,
            self.layout.read_reserve, self.layout.allocation, HEADER)

    def _reservation(self) -> OriginalBankReservation:
        r = self.original_reservation
        if (type(r) is not OriginalBankReservation
                or r.parent_pid != self.owner_pid or r.generation != 1
                or r.started_ns != self.started or r.deadline_ns != self.deadline
                or r.read_bytes != self.layout.read_reserve
                or r.allocation_bytes != self.layout.allocation
                or r.output_bytes != HEADER):
            raise BankUnconfirmed("bank_original_reservation_missing_or_changed")
        return r

    def create(self) -> None:
        self._owner()
        if self.creation_attempted:
            raise BankUnconfirmed("bank_create_once")
        reservation = self._reservation()
        self._clock()
        self.creation_attempted = True
        # The caller registered THIS object before any fallible acquisition.
        self.fd = os.memfd_create("friday-publisher-parent", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        os.fchmod(self.fd, 0o600)
        os.ftruncate(self.fd, self.layout.total)
        words = [0] * 128
        v = self.layout
        words[:15] = [MAGIC, BANK_VERSION, v.total, v.root_at, v.root_bytes, v.cold_at,
                      v.cold_bytes, v.caller_at, v.caller_bytes, self.owner_pid,
                      self.started, self.deadline, v.read_reserve, 2048, 64]
        words[16:25] = [RESERVATION_MAGIC, 1, reservation.parent_pid,
                        reservation.started_ns, reservation.deadline_ns,
                        reservation.read_bytes, reservation.allocation_bytes,
                        reservation.output_bytes, reservation.generation]
        header = bytearray(HEADER)
        struct.pack_into("<128Q", header, 0, *words)
        header[2048:2112] = self.image_sha256.encode("ascii")
        self.input_header = bytes(header)
        if os.pwrite(self.fd, self.input_header, 0) != HEADER:
            raise BankUnconfirmed("bank_binding_short_write")
        fcntl.fcntl(self.fd, fcntl.F_ADD_SEALS, INITIAL_SEALS)
        if fcntl.fcntl(self.fd, fcntl.F_GET_SEALS) != INITIAL_SEALS:
            raise BankUnconfirmed("bank_initial_seals")
        self.birth = _identity(self.fd)

    def _clock(self, mode: str = "operation") -> int:
        self._owner()
        if self.clock_phase in ("ENTERED", "FAILED"):
            if self.clock_error is None:
                self.clock_error = BankUnconfirmed("bank_clock_previous_attempt_unconfirmed")
            self.clock_phase = "FAILED"
            raise self.clock_error
        try:
            if mode == "reservation":
                if (self.clock_phase != "UNUSED" or not self.reservation_attempted
                        or self.creation_attempted or self.started or self.deadline):
                    raise BankUnconfirmed("bank_original_reservation_clock_once")
            elif mode == "setup_no_reservation":
                # A first timestamp for an ordinary pre-clock setup refusal is
                # DATA only. It neither starts a bank interval nor authorizes
                # acquisition; a previous failed attempt was refused above.
                if (not self.setup_failure_finish_attempted or self.launch_attempted
                        or self.fd >= 0 or self.started or self.deadline):
                    raise BankUnconfirmed("bank_unreserved_failure_clock_prefix")
            elif mode not in ("operation", "launch"):
                raise BankUnconfirmed("bank_clock_mode")
            if mode in ("operation", "launch") and self.min_wire_phase != "UNREAD":
                if self.min_wire_phase not in ("RESTRICTED", "VERIFIED_PREBIND"):
                    if self.min_wire_error is None:
                        self.min_wire_error = BankUnconfirmed("bank_final_min_unreturned_or_missing")
                    raise self.min_wire_error
                if not 0 < self.started <= self.retained_deadline <= self.deadline:
                    raise BankUnconfirmed("bank_retained_min_domain_changed")
            self.clock_phase = "ENTERED"
            self.clock_result = None
            now = time.monotonic_ns()
            self.clock_result = now  # Original returned object, before validation.
            if type(now) is not int or not 0 < now <= 2**64-1:
                raise BankUnconfirmed("bank_clock_result_domain")
            if mode == "reservation":
                if now > 2**64-1-4_200_000_000_000:
                    raise BankUnconfirmed("bank_original_reservation_clock_domain")
            elif mode == "setup_no_reservation":
                pass  # No reservation or new interval is manufactured here.
            elif mode == "launch":
                # Before output intake retained_deadline is exactly D0, so
                # the accepted prelaunch/post-start predicate is unchanged.
                if not 0 < self.started <= now < self.retained_deadline:
                    raise BankUnconfirmed("bank_original_launch_clock_expired")
            elif not 0 < self.started <= now <= self.retained_deadline:
                raise BankUnconfirmed("bank_original_clock_expired")
        except BaseException as exc:
            self.clock_error = exc  # Same first exception and traceback, not text.
            self.clock_phase = "FAILED"
            raise
        self.clock_phase = "RETURNED"
        return now

    def _retain_final_min(self, header: bytes, returncode: int) -> BankUnconfirmed | None:
        """Bounded scalar DATA before body allocation/semantic decoding.

        Any in-domain scalar may only RESTRICT independently sealed raw intake.
        Schema/identity/full semantic acceptance still happens separately. A
        bad/absent field is never interpreted as a successful wider D0. Both
        candidate scalars are retained before fallible interpretation; mismatch
        keeps the smaller restriction and denies semantic completion.
        """
        if self.min_wire_phase != "ENTERED" or len(header) != HEADER:
            self.min_wire_phase = "FAILED"
            self.min_wire_error = BankUnconfirmed("bank_final_min_header_missing")
            raise self.min_wire_error
        self.min_header_value = struct.unpack_from("<Q", header, FINAL_DEADLINE_WORD*8)[0]
        if self.started <= self.min_header_value <= self.deadline:
            self.retained_deadline = min(self.retained_deadline, self.min_header_value)
            self.min_wire_phase = "RESTRICTED"
        self.min_publication_value = struct.unpack_from("<Q", header, PUBLICATION_AT+31*8)[0]
        if self.started <= self.min_publication_value <= self.deadline:
            self.retained_deadline = min(self.retained_deadline, self.min_publication_value)
            self.min_wire_phase = "RESTRICTED"
        if self.min_wire_phase != "RESTRICTED":
            self.min_wire_phase = "FAILED"
            self.min_wire_error = BankUnconfirmed("bank_final_min_no_valid_restrictive_data")
            raise self.min_wire_error
        if returncode in (0, 70):
            valid = (self.min_header_value == self.retained_deadline
                     and self.min_publication_value == 0)
        elif returncode == 79:
            valid = (self.min_publication_value == self.retained_deadline
                     and self.min_header_value in (0, self.retained_deadline))
        else:
            valid = False
        if not valid:
            self.min_wire_error = BankUnconfirmed("bank_final_min_transport_missing_or_inconsistent")
        return self.min_wire_error

    def _bind_record(self, header: bytes, failed: bool) -> NativeBindRecord:
        raw = header[BIND_AT:BIND_AT+BIND_BYTES]
        # Retain actual raw DATA before any fallible decode/validation. Invalid,
        # missing or partial records are evidence, never replaced by empty data.
        self.native_bind_raw = raw
        if len(raw) != BIND_BYTES:
            raise BankUnconfirmed("bank_native_bind_record_missing")
        w = struct.unpack_from("<44Q", raw)
        v = self.layout
        layout = (v.root_at, v.root_bytes, v.cold_at, v.cold_bytes,
                  v.caller_at, v.caller_bytes, v.total, v.read_reserve, v.allocation)
        if not (
            w[:3] == (BIND_MAGIC, 1, BIND_BYTES)
            and w[3] == self.process_id and w[4] == self.owner_pid and w[5] == self.fd
            and 10 <= w[6] <= BIND_EVENTS and w[8] == (2 if failed else 1)
            and w[9] == w[10] == w[11] == w[13] == w[14] == w[15] == 1
            and self.birth is not None and w[16:23] == self.birth[:7]
            and w[25:34] == layout and w[34] == (78 if failed else 0)
            and w[35] == 1 and w[39] == 0 and w[40] == 1 and not any(w[41:])
            and w[12] in (0, 1) and w[38] in (0, 1)
        ):
            raise BankUnconfirmed("bank_native_bind_full_identity_or_layout_missing")
        events = []
        for index in range(BIND_EVENTS):
            fields = struct.unpack_from("<6Q", raw, 44*8 + index*48)
            if index >= w[6]:
                if any(fields):
                    raise BankUnconfirmed("bank_native_bind_unused_row_changed")
                continue
            op, rc, error, operand, extent, detail = fields
            rc = rc - 2**64 if rc >= 2**63 else rc
            if (not 1 <= op <= 19 or error > 2**31-1
                    or (rc >= 0 and error) or (op not in (4, 6, 7, 9, 11, 12, 13, 14, 16, 17, 18) and error)
                    or (rc < 0 and op in (4, 6, 7, 9, 11, 12, 13, 14, 16, 17, 18) and not error)
                    or (op not in (6, 7) and rc not in (-1, 0))):
                raise BankUnconfirmed("bank_native_bind_original_event_domain")
            events.append(NativeBindEvent(op, rc, error, operand, extent, detail))
        first = next((i+1 for i, row in enumerate(events) if row.returncode < 0), 0)
        if first != w[7] or bool(first) != failed:
            raise BankUnconfirmed("bank_native_bind_first_error_relation")
        # Actual fixed caller prefix, not merely a completion flag. All ten
        # checks through verified receiving header must have succeeded.
        if (tuple(row.operation for row in events[:10]) != tuple(range(1, 11))
                or any(row.returncode < 0 for row in events[:10])):
            raise BankUnconfirmed("bank_native_bind_verified_receiver_prefix")
        prefix = (11, 12, 13, 14, 15, 18, 19)
        if not failed:
            if tuple(row.operation for row in events[10:]) != prefix or w[12] != 1 or w[38] != 1:
                raise BankUnconfirmed("bank_native_bind_normal_call_relation")
        else:
            # Stop at the actual first failure; no retry. Reached cleanup is a
            # distinct suffix, then the original clock once if not reached yet.
            if not 11 <= first <= 17:
                raise BankUnconfirmed("bank_native_bind_failure_prefix")
            reached = tuple(row.operation for row in events[10:first])
            if reached != prefix[:len(reached)]:
                raise BankUnconfirmed("bank_native_bind_failure_call_relation")
            cleanup = []
            if events[first-1].operation in (14, 15, 18, 19):
                cleanup.append(16)
            if events[first-1].operation in (11, 12, 13):
                cleanup.append(14)
            if events[first-1].operation not in (18, 19):
                cleanup.append(18)
                if events[len(events)-1].operation == 19:
                    cleanup.append(19)
            if tuple(row.operation for row in events[first:]) != tuple(cleanup):
                raise BankUnconfirmed("bank_native_bind_cleanup_or_retry_relation")
            unmapped = next((row for row in events if row.operation == 16), None)
            private_acquired = next((row for row in events if row.operation == 13 and row.returncode == 0), None)
            if w[12] != int(private_acquired is not None and (unmapped is None or unmapped.returncode < 0)):
                raise BankUnconfirmed("bank_native_bind_mapping_cleanup_relation")
        if sum(row.operation == 14 for row in events) != 1 or sum(row.operation == 18 for row in events) != 1:
            raise BankUnconfirmed("bank_native_bind_once_close_and_clock")
        destination = events[8].operand
        private = next((row.operand for row in events if row.operation == 13 and row.returncode == 0), None)
        if (not destination or destination == 2**64-1
                or events[8].extent != v.total or events[8].detail != self.fd
                or events[9].operand != destination or events[9].extent != HEADER
                or any(row.operand != self.fd for row in events if row.operation in (3, 4, 5, 6, 7, 8, 12, 14))
                or any(row.operand != destination or row.extent != v.total for row in events if row.operation == 11)
                or any(row.extent != v.total for row in events if row.operation == 13)
                or any(private is None or row.operand != private or row.extent != v.total for row in events if row.operation in (15, 16))):
            raise BankUnconfirmed("bank_native_bind_actual_fd_mapping_operands")
        domain = next((row for row in events if row.operation == 19), None)
        if bool(domain is not None and domain.returncode == 0) != bool(w[38]):
            raise BankUnconfirmed("bank_native_bind_clock_fact_relation")
        if w[38] and not (
            w[37] < 1_000_000_000 and w[36] <= (2**64-1-w[37]) // 1_000_000_000
            and domain.operand == w[36]*1_000_000_000+w[37]
            and domain.extent == self.started and domain.detail == self.deadline
            and self.started <= domain.operand <= self.deadline
        ):
            raise BankUnconfirmed("bank_native_bind_original_clock_data")
        record = NativeBindRecord(raw, w, tuple(events))
        self.native_bind = record
        return record

    def _retain_raw_bank(self, returncode: int) -> bytes:
        """One physical intake independent of successful semantic interpretation.

        Same original reservation, no reread after a failed/partial attempt.
        The caller has established actual child end and a no-writer seal. Full
        raw retention does not establish complete historical pointee custody.
        """
        self._owner()
        self._reservation()
        self._clock()
        if (self.raw_receive_attempted or self.received_seals_after is None
                or (self.received_seals_after & (fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SEAL))
                != (fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SEAL)):
            raise BankUnconfirmed("bank_raw_intake_once_after_no_writer")
        self.raw_receive_attempted = True
        before = _identity(self.fd)
        self.raw_identity_before = before
        if self.birth is None or before[:7] != self.birth[:7] or before[6] != self.layout.total:
            raise BankUnconfirmed("bank_raw_intake_original_storage")
        # Exactly the existing bounded initial read, not an earlier authority
        # witness. From ENTERED onward no uncertainty may use a wider D0.
        self.min_wire_phase = "ENTERED"
        header = os.pread(self.fd, HEADER, 0)
        self.received_header = header
        if len(header) != HEADER:
            self.min_wire_phase = "FAILED"
            self.min_wire_error = BankUnconfirmed("bank_complete_header_missing")
            raise self.min_wire_error
        min_error = None
        if returncode != 78:
            min_error = self._retain_final_min(header, returncode)
            # Refuse an already expired restriction BEFORE bounded semantic
            # preparation, not only before the full buffer allocation. The
            # original native/error DATA remain in received_header if HELD.
            self._clock()
        if returncode in (78, 79):
            # Identify a valid native FIRST before fallible body allocation.
            # A semantic refusal is retained but does not discard our own
            # sealed receiving bytes. Unexpected decoder/allocation failures
            # stop immediately; this is not a retry of the decoder.
            try:
                if returncode == 79:
                    self._prepare_publication(header)
                else:
                    self._prepare_prebank(header)
            except BankUnconfirmed as decode_error:
                self.deferred_decode_fault = decode_error
                self._remember_fault(decode_error)
                if returncode == 78:
                    # Only a fully verified pristine bind prefix can retain
                    # D0 without a later Root min. Exit78 alone proves nothing.
                    self.min_wire_phase = "FAILED"
                    self.min_wire_error = decode_error
            else:
                if returncode == 78:
                    self.min_wire_phase = "VERIFIED_PREBIND"
        if min_error is not None:
            if self.deferred_decode_fault is None:
                self.deferred_decode_fault = min_error
            self._remember_fault(min_error)
        self._clock()  # No full allocation after an expired preparation phase.
        # After actual child end, receiving memfd + mutable buffer use the
        # original TWO-bank reservation. Pread/copy/digest plus later freezing
        # use the original four parent passes; there is no additional full read.
        self.body_buffer = bytearray(self.layout.total)
        digest = hashlib.sha256()
        actual = 0
        while actual < self.layout.total:
            self._clock()
            data = os.pread(self.fd, min(65536, self.layout.total - actual), actual)
            self.read_chunk = data
            if not data:
                raise BankUnconfirmed("bank_full_body_short_read")
            self.body_buffer[actual:actual+len(data)] = data
            actual += len(data)
            self.body_bytes_written = actual
            digest.update(data)
        self.read_chunk = None
        self._clock()
        if (_identity(self.fd) != before
                or os.pread(self.fd, HEADER, 0) != header
                or 7*actual + 4*SCRATCH > self.layout.read_reserve):
            raise BankUnconfirmed("bank_full_body_changed_or_credit")
        self.final_identity = before
        self.raw_data_sha256 = digest.hexdigest()
        self.raw_body_retained = True
        return header

    def _prepare_prebank(self, header: bytes) -> None:
        # Preserve a valid native FIRST before fallible full body intake. A
        # typed78 alone is never a received native-failure acceptance.
        self.prebank_header = header
        self.native_bind_raw = header[BIND_AT:BIND_AT+BIND_BYTES]
        if (len(header) != HEADER or header[:BIND_AT] != self.input_header[:BIND_AT]
                or header[2048:] != self.input_header[2048:]):
            raise BankUnconfirmed("bank_prebank_only_full_bind_data_expected")
        record = self._bind_record(header, True)
        future_write = any(row.operation == 12 and row.returncode == 0 for row in record.events)
        expected_seals = INITIAL_SEALS | (fcntl.F_SEAL_FUTURE_WRITE if future_write else 0)
        if self.received_seals_before != expected_seals:
            raise BankUnconfirmed("bank_prebank_actual_seal_prefix")
        if self.received_seals_after != (expected_seals | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SEAL):
            raise BankUnconfirmed("bank_prebank_no_writer_end")
        if record.words[38] != 1:
            raise BankUnconfirmed("bank_prebank_native_original_clock_unconfirmed")
        failure = BankNativeFailure(self, record)
        self.prebank_fault = failure
        self._remember_fault(failure)

    def _receive_prebank(self, proof: CleanupProof, returncode: int) -> None:
        self._clock()
        if self.deferred_decode_fault is not None:
            raise self.deferred_decode_fault
        if not self.raw_body_retained or self.prebank_fault is None:
            raise BankUnconfirmed("bank_prebank_full_raw_or_original_error_missing")
        self.prebank_end_confirmed = True
        # Custody succeeded but the OPERATION failed. No BankCompletion/GO.
        raise self.prebank_fault

    def _publication_record(self, header: bytes) -> NativePublicationRecord:
        raw = header[PUBLICATION_AT:PUBLICATION_AT+PUBLICATION_BYTES]
        self.publication_raw = raw  # Original DATA before fallible decoder.
        if len(raw) != PUBLICATION_BYTES:
            raise BankUnconfirmed("bank_publication_original_record_missing")
        w = struct.unpack_from("<32Q", raw)
        signed = lambda x: x-2**64 if x >= 2**63 else x
        op, rc, error = w[6], signed(w[7]), w[8]
        copied, compared, header_copied = w[20:23]
        v = self.layout
        if not (
            w[:3] == (PUBLICATION_MAGIC, PUBLICATION_VERSION, PUBLICATION_BYTES)
            and w[3:6] == (self.process_id, self.owner_pid, 1)
            and op in (1, 2, 5, 8, 9, 10) and error == 0
            and (rc != 0 if op in (8, 9) else rc == -1)
            and w[14] < 1_000_000_000
            and w[13] <= (2**64-1-w[14]) // 1_000_000_000
            and w[15] == w[13]*1_000_000_000+w[14]
            and w[16:18] == (self.started, self.deadline)
            and self.started <= w[15] <= self.retained_deadline
            and w[31] == self.min_publication_value == self.retained_deadline
            and self.started <= w[31] <= self.deadline
            and (signed(w[18]) < 0 if op in (1, 2) else w[18] == 0)
            and 0 <= compared <= copied <= v.total-HEADER
            and header_copied in (0, HEADER) and w[23] == 0
            and w[24] in (0, 1) and w[29:31] == (1, 1)
        ):
            raise BankUnconfirmed("bank_publication_original_primitive_relation")
        bind = self.native_bind
        if bind is None:
            raise BankUnconfirmed("bank_publication_original_bound_receiver_missing")
        private = next(row.operand for row in bind.events if row.operation == 13)
        destination = bind.events[8].operand
        if op in (1, 2, 5) and (copied or compared or header_copied):
            raise BankUnconfirmed("bank_publication_before_copy_progress_changed")
        if op == 2 and not (
            w[10] == private+v.cold_at and w[9] != w[10]
            and w[11] == v.cold_bytes and w[12] == 0
        ):
            raise BankUnconfirmed("bank_publication_commit_operand_relation")
        if op == 8 and not (
            1 <= w[11] <= 65536 and copied-compared == w[11]
            and w[12] == HEADER+compared and header_copied == 0
            and w[9] == private+w[12] and w[10] == destination+w[12]
        ):
            raise BankUnconfirmed("bank_publication_compare_actual_progress")
        if op in (9, 10) and not (copied == compared == v.total-HEADER and header_copied == HEADER):
            raise BankUnconfirmed("bank_publication_full_copy_primitive_relation")
        if op == 9 and not (
            w[9:12] == (private, destination, HEADER) and w[12] == 0
        ):
            raise BankUnconfirmed("bank_publication_header_compare_operands")
        if op == 10 and not (w[9] == private and w[10] == 0 and w[11] == v.total and w[12] == 0):
            raise BankUnconfirmed("bank_publication_held_outcome_relation")
        payload = v.total-PUBLICATION_OPERAND_STORAGE
        if w[24]:
            if not (
                op in (8, 9) and w[25:28] == (payload, payload+PUBLICATION_OPERAND_BYTES, w[11])
                and 1 <= w[27] <= PUBLICATION_OPERAND_BYTES and w[28] == w[15]
                and private <= w[9] and w[9]-private+w[27] <= v.total
                and destination <= w[10] and w[10]-destination+w[27] <= v.total
            ):
                raise BankUnconfirmed("bank_publication_compare_body_layout")
        elif any(w[25:29]):
            raise BankUnconfirmed("bank_publication_absent_compare_body_metadata")
        record = NativePublicationRecord(raw, w)
        self.native_publication = record
        return record

    def _prepare_publication(self, header: bytes) -> None:
        # Actual header bytes are retained before any fallible interpretation.
        self.publication_raw = header[PUBLICATION_AT:PUBLICATION_AT+PUBLICATION_BYTES]
        if self.received_seals_after != FINAL_SEALS:
            raise BankUnconfirmed("bank_publication_no_writer_unconfirmed")
        if (len(header) != HEADER or header[:256] != self.input_header[:256]
                or header[66*8:BIND_AT] != self.input_header[66*8:BIND_AT]
                or header[2048:2304] != self.input_header[2048:2304]):
            raise BankUnconfirmed("bank_publication_original_input_binding_changed")
        self._bind_record(header, False)
        record = self._publication_record(header)
        failure = BankPublicationFailure(self, record)
        self.publication_fault = failure
        self._remember_fault(failure)  # Native first before full raw allocation.

    def _receive_publication(self, proof: CleanupProof) -> None:
        # Raw intake was performed exactly once by receive, before this final
        # semantic/error route. No decoder/read/copy retry or new clock.
        self._clock()
        if self.deferred_decode_fault is not None:
            raise self.deferred_decode_fault
        record, failure = self.native_publication, self.publication_fault
        if not self.raw_body_retained or record is None or failure is None:
            raise BankUnconfirmed("bank_publication_full_raw_or_original_error_missing")
        if record.words[24]:
            source_at, destination_at, n = record.words[25:28]
            # EACH exact historical operand stays retained if the later copy
            # fails. Native captured BOTH before writing either payload, even
            # when an operand overlaps it. The full received destination is
            # already retained, but its now-used payload must not be presented
            # as the original compared window. No dead pointer is dereferenced.
            self.comparison_source_body = bytes(memoryview(self.body_buffer)[source_at:source_at+n])
            self.comparison_destination_body = bytes(memoryview(self.body_buffer)[destination_at:destination_at+n])
            self._clock()
        self.publication_end_confirmed = True
        raise failure  # Incomplete native custody remains a failed operation.

    def receive(self, proof: CleanupProof, returncode: int) -> BankCompletion:
        self._owner()
        reservation = self._reservation()
        if self.receive_attempted or self.fd < 0 or self.process_id is None:
            raise BankUnconfirmed("bank_receive_once_after_actual_launch")
        self.receive_attempted = True
        self.cleanup = proof
        if not (
            self.scope is not None and proof is self.scope.proof
            and self.scope.process is not None and self.scope.process.pid == self.process_id
            and self.scope.process.returncode == returncode
            and proof.cleanup_clear and proof.leader_reaped
            and proof.leader_returncode == returncode and returncode in (0, 70, 78, 79)
            and not proof.forced_leader and not proof.forced_descendants
        ):
            raise BankUnconfirmed("bank_real_process_end_unconfirmed")
        if self.birth is None or _identity(self.fd)[:7] != self.birth[:7]:
            raise BankUnconfirmed("bank_original_storage_identity")
        self._clock()
        # F_SEAL_WRITE fails if ANY writable shared mapping is still alive.
        # Normal process exit alone is not the data/custody acceptance.
        self.received_seals_before = fcntl.fcntl(self.fd, fcntl.F_GET_SEALS)
        required = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SEAL
        missing = required & ~self.received_seals_before
        if missing:
            fcntl.fcntl(self.fd, fcntl.F_ADD_SEALS, missing)
        self.received_seals_after = fcntl.fcntl(self.fd, fcntl.F_GET_SEALS)
        if self.received_seals_after != (self.received_seals_before | required):
            raise BankUnconfirmed("bank_received_no_writer_seal")
        header = self._retain_raw_bank(returncode)
        if returncode == 79:
            self._receive_publication(proof)
            raise BankUnconfirmed("bank_publication_never_success")
        if returncode == 78:
            self._receive_prebank(proof, returncode)
            raise BankUnconfirmed("bank_prebank_never_success")
        if self.received_seals_after != FINAL_SEALS:
            raise BankUnconfirmed("bank_final_no_writer_seal")
        if self.deferred_decode_fault is not None:
            raise self.deferred_decode_fault
        w = struct.unpack_from("<128Q", header)
        # Immutable input and unused bytes are never a status publication area.
        if (header[:256] != self.input_header[:256]
                or header[66*8:BIND_AT] != self.input_header[66*8:BIND_AT]
                or header[2048:2304] != self.input_header[2048:2304]
                or header[3840:] != self.input_header[3840:]):
            raise BankUnconfirmed("bank_parent_binding_or_reserved_bytes_changed")
        self._bind_record(header, False)
        strings = []
        for index in range(8):
            at = 2304 + 192*index
            original, alias, length, present = struct.unpack_from("<4Q", header, at)
            body = header[at+32:at+192]
            if present == 0:
                if original or alias or length or any(body):
                    raise BankUnconfirmed("bank_absent_phase_body")
            elif present == 1:
                if (not original or not 1 <= alias <= index+1 or length >= 160
                        or body[length] != 0 or b"\0" in body[:length]
                        or any(body[length+1:])):
                    raise BankUnconfirmed("bank_full_phase_body")
                first = next((j for j, row in enumerate(strings) if row[0] == original), index)
                if alias != first+1 or (first < index and strings[first][1:] != (length, body)):
                    raise BankUnconfirmed("bank_original_phase_alias")
            else:
                raise BankUnconfirmed("bank_phase_presence")
            strings.append((original, length, body))
        if not (
            w[32] == self.process_id and w[33] == 1 and w[36] == 0
            and w[65] == self.min_header_value == self.retained_deadline
            and self.started <= w[35] <= self.retained_deadline
            and w[41] == w[42] == w[43] == 1 and w[47] == w[48] == 0
            and w[54] == self.layout.read_reserve <= w[49] <= READ_CAP
            and reservation.output_bytes <= w[50] <= OUTPUT_CAP
            and w[51] >= self.layout.allocation
            and w[51] + w[52] + w[53] <= RAM_CAP
            and w[55] > 0 and w[56] > 0 and w[57] > 0
            and w[55] - w[56] == self.layout.cold_at - self.layout.root_at
            and w[57] - w[56] == self.layout.caller_at - self.layout.root_at
            and w[60] == w[61] == 1
            and w[62] == 1 and w[63] == self.layout.total-HEADER
            and w[35] <= w[64] <= self.retained_deadline
        ):
            raise BankUnconfirmed("bank_full_actual_cold_data_or_original_resources")
        if w[34] == 1:
            if returncode != 0 or not (w[44] == w[45] == w[46] == 1):
                raise BankUnconfirmed("bank_local_end_relation")
            outcome = "LOCAL_END_THEN_PARENT_RECEIVED"
        elif w[34] == 2:
            if not (
                returncode == 70 and w[58] == 1 and w[39] == w[40] == w[44] == w[45] == 0
                and (w[37] == 1 or w[38] == 1)
            ):
                raise BankUnconfirmed("bank_preSource_partial_runtime_relation")
            outcome = "FAILED_EARLY_PREFIX_DATA_RECEIVED_AND_ROOT_PROCESS_ENDED"
        else:
            raise BankUnconfirmed("bank_incomplete_original_custody")

        completion = BankCompletion(outcome, self.process_id, returncode,
                                    self.body_bytes_written, self.raw_data_sha256,
                                    header, proof, self, self.retained_deadline)
        self.completion = completion
        return completion

    def finish_received(self) -> BankFinalResult | BankPublicationRetirement:
        """The actual run caller invokes this on BOTH received native outcomes.

        No old receipt tuple is rewritten. A close/clock/allocation failure leaves
        this registered owner and its complete or reached-prefix DATA retained,
        with no final result. Unknown close is not retried or called confirmed.
        """
        self._owner()
        if self.finish_attempted or self.native_bind is None or not self.raw_body_retained:
            raise BankUnconfirmed("bank_finish_once_after_full_receive")
        self.finish_attempted = True
        if self.completion is not None:
            if (self.body_buffer is None
                    or self.body_bytes_written != self.layout.total
                    or len(self.body_buffer) != self.layout.total
                    or self.received_header != self.completion.header):
                raise BankUnconfirmed("bank_retirement_without_full_retained_body")
            header = self.received_header
            outcome = self.completion.outcome
            returncode = self.completion.returncode
            digest = self.completion.data_sha256
        elif self.publication_end_confirmed:
            if (self.publication_fault is None or self.native_publication is None
                    or self.body_buffer is None or self.body_bytes_written != self.layout.total
                    or len(self.body_buffer) != self.layout.total or self.received_header is None):
                raise BankUnconfirmed("bank_publication_retirement_without_full_destination")
            header = self.received_header
            outcome = "FAILED_PUBLICATION_PARTIAL_NATIVE_CUSTODY"
            returncode = 79
            digest = None
        elif self.prebank_end_confirmed and self.prebank_header is not None:
            header = self.prebank_header
            outcome = "FAILED_BIND_DATA_RECEIVED_AND_ROOT_PROCESS_ENDED"
            returncode = 78
            digest = self.raw_data_sha256
        else:
            raise BankUnconfirmed("bank_finish_without_actual_received_outcome")
        historical_identity = self.final_identity
        # This call checks the still-live descriptor and original finite clock.
        # All checks after it use retained bytes/history and confirmed close,
        # never fstat/pread on a descriptor which must already be closed.
        self.close_consumed()
        self._clock()
        if self.raw_body_retained:
            # The receiving memfd has now ended. At most the two full-size
            # Python buffers coexist during this separately prepaid copy.
            # Keep the mutable original on allocation failure; no lossy digest.
            self.final_body = bytes(self.body_buffer)
            self.body_buffer = None
        now = self._clock()
        if not self.closed or self.fd != -1 or historical_identity is None:
            raise BankUnconfirmed("bank_final_retirement_unconfirmed")
        if self.publication_end_confirmed:
            result = BankPublicationRetirement(
                self.process_id, 79, header, self.final_body, self.publication_fault,
                self.native_publication, self.native_bind, self.cleanup,
                historical_identity, True, now, self._reservation(),
                ((self.comparison_source_body, self.comparison_destination_body)
                 if self.native_publication.words[24] else None), self.retained_deadline)
            self.publication_failure_result = result
            return result  # No old tuple edit and no BankCompletion/final_result.
        result = BankFinalResult(outcome, self.process_id, returncode, header,
                                 self.final_body, digest, self.native_bind,
                                 self.cleanup, historical_identity, True, now,
                                 self._reservation(), self.retained_deadline)
        self.final_result = result  # Distinct publication, not an old-tuple edit.
        return result

    def finish_receive_failure(self) -> BankReceiveFailureResult:
        """Retire fully retained raw DATA when its semantic validation failed.

        No second intake, recovery of an uncertain read, close retry or native
        completion credit. Partial/no-writer/process uncertainty cannot enter.
        The actual generic caller re-raises the same original exception.
        """
        self._owner()
        if (self.receive_failure_finish_attempted or self.finish_attempted
                or self.completion is not None or self.final_result is not None
                or self.fault is None or not self.raw_body_retained):
            raise BankUnconfirmed("bank_receive_failure_endpoint_not_available")
        self.receive_failure_finish_attempted = True
        historical_identity = self.final_identity
        self._close_retained_raw()
        self._clock()
        self.final_body = bytes(self.body_buffer)
        self.body_buffer = None
        now = self._clock()
        if not self.closed or self.fd != -1 or historical_identity is None:
            raise BankUnconfirmed("bank_failed_receive_retirement_unconfirmed")
        result = BankReceiveFailureResult(
            self.process_id, self.cleanup.leader_returncode,
            self.received_header, self.final_body, self.raw_data_sha256,
            self.fault, self.native_bind, self.cleanup, historical_identity,
            True, now, self._reservation(), self.retained_deadline)
        self.receive_failure_result = result
        return result

    def finish_setup_failure(self) -> BankSetupFailureResult:
        """Actual caller's one failed-setup endpoint BEFORE a launch attempt.

        No Popen retry, native failure-marker acceptance or second close. A
        fallible cleanup preserves the registered owner, every reached buffer
        and the original error, and publishes no setup_failure_result.
        """
        self._owner()
        if (self.setup_failure_finish_attempted or self.launch_attempted
                or self.fault is None or self.close_attempted or self.closed
                or self.completion is not None or self.final_result is not None):
            raise BankUnconfirmed("bank_setup_failure_endpoint_not_available")
        self.setup_failure_finish_attempted = True
        if self.started:
            self._clock()  # An empty-fd failure does not refresh an old clock.
        if self.fd < 0:
            # A failed reservation or memfd acquisition produced no descriptor.
            # Do not label an absent descriptor as a successful close.
            result = BankSetupFailureResult(
                "NEVER_ACQUIRED", None, None, self.fault, self.cleanup,
                self.original_reservation,
                self._clock() if self.started else self._clock("setup_no_reservation"))
            self.setup_failure_result = result
            return result

        # Successful fixed reservation necessarily preceded memfd acquisition.
        # Before-launch failures have done no native copies or parent intake;
        # this single read/copy/freeze uses at most THREE of the four already
        # reserved parent passes. No retry of a partially performed intake.
        self._reservation()
        self._clock()
        before = _identity(self.fd)
        self.setup_failure_identity_before = before
        if (not stat.S_ISREG(before[2]) or not 0 <= before[6] <= self.layout.total
                or (self.birth is not None and before[:7] != self.birth[:7])):
            raise BankUnconfirmed("bank_setup_failure_storage_domain")
        seals = fcntl.fcntl(self.fd, fcntl.F_GET_SEALS)
        self.setup_failure_seals_before = seals
        if seals & ~FINAL_SEALS:
            raise BankUnconfirmed("bank_setup_failure_unknown_seals")
        missing = FINAL_SEALS & ~seals
        if missing:
            fcntl.fcntl(self.fd, fcntl.F_ADD_SEALS, missing)
        if fcntl.fcntl(self.fd, fcntl.F_GET_SEALS) != FINAL_SEALS:
            raise BankUnconfirmed("bank_setup_failure_no_writer_unconfirmed")
        sealed = _identity(self.fd)
        if sealed[:7] != before[:7]:
            raise BankUnconfirmed("bank_setup_failure_storage_changed")
        self.final_identity = sealed
        self._clock()
        self.body_buffer = bytearray(sealed[6])
        actual = 0
        while actual < sealed[6]:
            self._clock()
            data = os.pread(self.fd, min(65536, sealed[6] - actual), actual)
            self.read_chunk = data
            if not data:
                raise BankUnconfirmed("bank_setup_failure_full_data_short_read")
            self.body_buffer[actual:actual+len(data)] = data
            actual += len(data)
            self.body_bytes_written = actual
        self.read_chunk = None
        self._clock()
        if _identity(self.fd) != sealed:
            raise BankUnconfirmed("bank_setup_failure_full_data_changed")
        self.close_attempted = True
        try:
            os.close(self.fd)
        except BaseException as exc:
            self.close_fault = exc
            raise
        self.closed = True
        self.fd = -1
        # Historical bytes/identity only from this point; never fstat/pread a
        # closed descriptor. Retain the mutable buffer on copy/clock failure.
        self._clock()
        self.final_body = bytes(self.body_buffer)
        self.body_buffer = None
        now = self._clock()
        result = BankSetupFailureResult(
            "CONFIRMED_CLOSED", sealed, self.final_body, self.fault,
            self.cleanup, self._reservation(), now)
        self.setup_failure_result = result
        return result

    def close_consumed(self) -> None:
        """After the EXISTING caller finishes using/retaining required evidence.

        No automatic destructor, deletion on uncertainty, retry or implicit
        release approval. This is the finite parent endpoint, not another owner.
        """
        if self.completion is None and not self.prebank_end_confirmed and not self.publication_end_confirmed:
            raise BankUnconfirmed("bank_close_without_actual_complete_consumer")
        self._close_retained_raw()

    def _close_retained_raw(self) -> None:
        # Common physical retirement, not a semantic-success predicate. The
        # separate callers determine normal/native failure vs invalid raw DATA.
        self._owner()
        self._clock()
        if (self.close_attempted or not self.raw_body_retained
                or self.body_buffer is None or len(self.body_buffer) != self.layout.total
                or self.body_bytes_written != self.layout.total
                or self.raw_data_sha256 is None or self.cleanup is None
                or self.scope is None or self.cleanup is not self.scope.proof
                or not self.cleanup.cleanup_clear or not self.cleanup.leader_reaped
                or self.cleanup.forced_leader or self.cleanup.forced_descendants
                or self.final_identity is None or _identity(self.fd) != self.final_identity):
            raise BankUnconfirmed("bank_close_without_full_raw_retention_and_actual_end")
        self.close_attempted = True
        try:
            os.close(self.fd)
        except BaseException as exc:
            # Preserve the original last cleanup exception; no retry and no
            # overwrite of the earlier operation/native first error.
            self.close_fault = exc
            self._remember_fault(exc)
            raise
        self.closed = True
        self.fd = -1


def run_preowned_root(
    image: Path,
    image_sha256: str,
    case_id: str,
    layout: QualifiedBankLayout,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    retained_evidence: MutableSequence[PublisherBank],
    stdout=None,
    stderr=None,
) -> BankFinalResult:
    """Existing gate parent: create -> actual Root -> full intake -> actual close.

    The caller's established image/source/admission checks MUST precede this call.
    This function neither grants admission nor selects a new provider/role.
    The canonical exclusive slot and signal/cancellation handling remain external.
    """
    if (not image.is_absolute() or type(case_id) is not str
            or "\0" in case_id or len(case_id.encode("utf-8")) > 2_000_000):
        raise BankUnconfirmed("bank_actual_command_inputs")
    bank = PublisherBank(layout, image_sha256)
    retained_evidence.append(bank)  # Before memfd/acquisition/launch; errors retain it.
    prelaunch_clock_error = None
    try:
        bank.reserve_original_bank()  # Fixed original charge BEFORE the first fd.
        bank.create()
        if (bank.birth is None or any(type(value) is not int or not 0 <= value < 2**64 for value in bank.birth[:7])):
            raise BankUnconfirmed("bank_original_fd_identity_argument_domain")
        scope = OwnedCommandScope()
        bank.scope = scope
        with scope:
            # Check the SAME original interval after scope entry, immediately
            # before launch intent. This is not an admission/minimum witness.
            # Register the exact failure BEFORE context exit can itself fail;
            # no start was attempted and no process end is inferred from exit.
            try:
                bank._clock("launch")
            except BaseException as clock_error:
                prelaunch_clock_error = clock_error
                bank._remember_fault(clock_error)
                raise
            # Conservative intent precedes the fallible actual start. Even a
            # failed Popen is NOT reclassified as never launched by this helper.
            bank.launch_attempted = True
            process = scope.start(
                [str(image), str(bank.fd), case_id, *(str(value) for value in bank.birth[:7])],
                cwd=cwd, environment=environment, stdout=stdout, stderr=stderr,
                child_umask=0o077, inherited_fds=(bank.fd,),
            )
            bank.process_id = process.pid
            # Never reuse the prelaunch sample as a wait allowance: scope.start
            # itself consumed the original interval. A failed/expired post-start
            # check stays attempted and never reaches wait with a nonpositive
            # duration or a newly started clock.
            remaining = (bank.deadline - bank._clock("launch")) / 1_000_000_000
            returncode = scope.wait(remaining)
        if prelaunch_clock_error is not None:
            # Even if context exit suppressed the exception, it cannot turn a
            # refused launch into receive/finalization or a later clock retry.
            raise prelaunch_clock_error
        bank.cleanup = scope.proof
        if scope.proof is None:
            raise BankUnconfirmed("bank_missing_actual_cleanup")
        bank.receive(scope.proof, returncode)
        return bank.finish_received()
    except BaseException as exc:
        if (prelaunch_clock_error is None
                and isinstance(exc, (BankNativeFailure, BankPublicationFailure))):
            # Full native failure is the first outcome, not operation success.
            # Finish its actual received bank as well; a secondary finalization
            # failure is retained without replacing or retrying the original.
            bank._remember_fault(exc)
            try:
                bank.finish_received()
            except BaseException as finish_error:
                if bank.close_fault is not finish_error:
                    bank._remember_fault(finish_error)
            raise
        # The existing controller retains this exact registered owner even on
        # partial create/launch/cleanup/intake. No duplicate process or retry.
        if prelaunch_clock_error is not None:
            bank._remember_fault(prelaunch_clock_error)
        bank._remember_fault(exc)
        if bank.scope is not None:
            bank.cleanup = bank.scope.proof
        if not bank.launch_attempted:
            try:
                bank.finish_setup_failure()
            except BaseException as finish_error:
                # Failure stays failure. Do not retry a close or replace the
                # original exception with secondary data/clock/cleanup errors.
                bank._remember_fault(finish_error)
        elif bank.raw_body_retained and not bank.finish_attempted:
            try:
                bank.finish_receive_failure()
            except BaseException as finish_error:
                bank._remember_fault(finish_error)
        if prelaunch_clock_error is not None:
            # This SAME first exception, including a native-failure-shaped
            # exception from the clock/exit path, is a never-attempted launch
            # failure, not a received native outcome. Keep any exit/finalization
            # exception in the registered bank; never replace the first error.
            if exc is not prelaunch_clock_error:
                raise prelaunch_clock_error from exc
            raise
        if bank.publication_fault is not None and exc is not bank.publication_fault:
            # Preserve native FIRST; the reached parent intake error is a
            # separately retained secondary, never a replacement or close retry.
            raise bank.publication_fault from exc
        if bank.prebank_fault is not None and exc is not bank.prebank_fault:
            raise bank.prebank_fault from exc
        if bank.deferred_decode_fault is not None and exc is not bank.deferred_decode_fault:
            raise bank.deferred_decode_fault from exc
        raise
