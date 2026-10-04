"""File-backed payload/metadata, parent-owned immutable preimage prefix.

All four real file-backed births/cuts remain. The prefix contains their FULL
actual before-image bytes in birth order, not another physical late-store copy.
Child offsets reference those same immutable objects; BOTH actual parent
receivers rebuild the full prefix and validate the physical payload/journal.
Primary and late payloads have distinct physical maps and zero-based journals.
The late body's terminal-cost prefix is outside its complete payload region.
This finite layout proposal is not required-workload/cost acceptance.
"""
import hashlib
import json
import os
import struct

_MAGIC = b"F252BNK1"
_COST_MAGIC = b"F99COST1"
_COST_DATA = struct.Struct(">8sQQQQQQ")
_COST = struct.Struct(">8sQQQQQQ32s")
_COST_AT = 0
_COST_UNUSED = 1
_COST_FINAL = 2
_HEAD = struct.Struct(">8sQQQQ32s")
_REGION = struct.Struct(">QQII")
_COUNT = struct.Struct(">I")
_SPAN = struct.Struct(">BQQ")
_PACKET_CEILING = 2_000_000
_HEADER = 128
_KIND_PAYLOAD = 1
_KIND_PREIMAGE = 2
_NODE_CEILING = 262144
_SCAN_WORKSPACE = 131072


class _Payload:
    def __init__(self, mapping, capacity, start):
        self.map = mapping
        self.capacity = capacity
        self.start = start
        self.count = 0


class PrefixBodyMailbox:
    def __init__(self, fd, capacity, parent_pid, prepared, metadata_prepared):
        self.fd = fd
        self.capacity = capacity
        self.parent_pid = parent_pid
        self.prepared = prepared
        self.metadata_prepared = metadata_prepared
        self.retained_fds = (fd, metadata_prepared.fd)
        self.owner_pid = os.getpid()
        self.pid = None
        self.count = 0
        self.aliases = []
        self.span_log = []
        self.parent_spans = []
        self.parent_region_journal = False
        self.parent_journal = None
        self.parent_store = None
        self.parent_payload = None
        self.raw_packet = None
        self.packet = None
        self.attempted = False
        self.accepted = None
        self.errors = []
        self.bound = False
        self.late = None
        self.primary = None
        self.publication_error = None
        self.ack_attempted = False
        self.ack_error = None
        self.ack_completed = False
        self.endpoint = "preowned-native-prefix-bank:" + prepared.name
        self.map = None
        self.meta = None
        self.parent_copies_allocation = 0
        self.publication_credit = None
        self.child_quota = None
        self.child_used = {"allocation": 0, "reads": 0, "hash_bytes": 0, "output": 0}
        self.parent_read_attempted = False
        self.parent_read_error = None
        self.before = os.fstat(fd)
        self.kind = None
        self.unit = None
        self.payload = None
        self.store_map = None
        self.store_birth = None
        self.external = None
        self.store_whole = None
        self.before_effect_cut = None
        self.preimage_bindings = None
        self.attach_attempted = False
        self.attachment_error = None
        self.exec_handoff_attempted = False
        self.child_cost_attempted = False
        self.child_cost_closed = False
        self.child_cost_error = None
        self.parent_end_attempted = False
        self.parent_end_error = None
        self.parent_endpoint_finished = False
        self.parent_completion = None
        self.parent_cost_wire = None
        if capacity < 1 or self.before.st_size != capacity:
            raise RuntimeError("prefix-bank-actual-prepared-size")

    @classmethod
    def prepare(cls, carrier, metadata_carrier):
        obs = carrier.hold.meter
        ledger = getattr(obs, "alljob_cohort", None)
        if (type(ledger) is not dict or ledger.get("row_grant_complete") is not True
                or ledger.get("caps_changed") is not False
                or ledger.get("simulator_is_runtime_measurement") is not False
                or type(ledger.get("fits")) is not bool
                or carrier.hold is not obs.terminal_hold
                or metadata_carrier.hold is not obs.terminal_hold):
            raise RuntimeError("prefix-bank-alljob-cohort-CODE")
        slot = ledger.get("next_slot")
        banks = ledger.get("maximum_banks")
        unit = ledger.get("unit")
        if type(slot) is not int or type(banks) is not int or slot < 0 or slot >= banks:
            raise RuntimeError("prefix-bank-alljob-slot-CODE")
        if type(unit) is not int or unit < max(_HEADER + 1, 2 * _COST.size):
            raise RuntimeError("prefix-bank-alljob-cohort-CODE")
        from alljob_cohort import require_publication_wire, publication_pair_layout
        require_publication_wire((_HEAD.size, _REGION.size, _COST_DATA.size,
            _COST.size, _NODE_CEILING, _SCAN_WORKSPACE, _HEADER))
        kind = "primary" if slot % 2 == 0 else "store"
        layout = publication_pair_layout(unit, unit)
        if layout is None or layout["cost_prefix"] != 2 * _COST.size:
            raise RuntimeError("prefix-bank-disjoint-pair-dimensions-CODE")
        # Late body is a real distinct file-backed mmap, not an anonymous
        # surrogate. Its original actual cut remains one FULL prefix region.
        # No second physical file embeds the other three before images.
        body_width = layout["primary_body"] if kind == "primary" else layout["late_body"]
        # Any extension is before fork and before map/file effects, through
        # Root _reserve in the same signed whole pool, not a cleanup grant.
        obs.rebudget_terminal_prefix_output(body_width + unit)
        ledger["next_slot"] = slot + 1
        cls._materialize(metadata_carrier, unit)
        cls._materialize(carrier, body_width)
        mailbox = cls.__new__(cls)
        carrier.mapping_mailbox = mailbox
        mailbox.__init__(carrier.fd, body_width, os.getpid(), carrier, metadata_carrier)
        mailbox.kind = kind
        mailbox.unit = unit
        mailbox.pair_layout = layout
        mailbox._birth(carrier, body_width, "map_birth", "map")
        mailbox._birth(metadata_carrier, unit, "meta_birth", "meta")
        start = layout["primary_payload_at"] if kind == "primary" else layout["late_payload_at"]
        mailbox.payload = _Payload(mailbox.map, unit, start)
        mailbox.capacity = unit
        if kind == "primary":
            mailbox.store_map = None
        else:
            mailbox.store_map = mailbox.map
            mailbox.store_birth = mailbox.map_birth
        # Root obtains one disjoint ordinary reservation from the SAME whole
        # pending pool before fork. A child only spends its copied local quota;
        # it never calls an inherited Root meter or claims shared admission.
        mailbox._prepare_publication_credit()
        ledger["slots"].append({
            "slot": slot,
            "kind": kind,
            "body_width": body_width,
            "meta_width": unit,
            "payload_start": start,
            "payload_capacity": unit,
            "name": carrier.name,
        })
        return mailbox

    def _prepare_publication_credit(self):
        if os.getpid() != self.parent_pid or self.publication_credit is not None:
            raise RuntimeError("prefix-bank-publication-credit-before-fork")
        # Four complete original before images are retained in parent
        # custody, and inherited by the same child. Hash/read them in full.
        # This is a prospective escrow, not measured child/native cost.
        from alljob_cohort import endpoint_publication_quota
        quota = endpoint_publication_quota(self.unit, self.unit,
            self.pair_layout["full_beforeimages"])
        if quota is None:
            raise RuntimeError("prefix-bank-publication-dimensions-CODE")
        observer = self.prepared.hold.meter
        credit = observer._reserve("prefix-publication-existing-pool-before-fork",
            reads=quota["reads"], output=quota["output"],
            hash_bytes=quota["hash_bytes"], allocation=quota["allocation"],
            slots=0, final=True)
        # Kept as a pending maximum while actual child/receiver aliases remain.
        # Neither unused allocation nor cumulative IO is refunded on ACK.
        self.publication_credit = credit
        self.child_quota = quota

    def _child_debit(self, allocation=0, reads=0, hash_bytes=0, output=0):
        if self.pid != os.getpid() or not self.bound or type(self.child_quota) is not dict:
            raise RuntimeError("prefix-bank-child-local-prepayment")
        if self.child_cost_attempted:
            raise RuntimeError("prefix-bank-child-cost-terminal-once")
        return self._quota_debit(allocation, reads, hash_bytes, output)

    def _quota_debit(self, allocation=0, reads=0, hash_bytes=0, output=0):
        additions = {"allocation": allocation, "reads": reads,
                     "hash_bytes": hash_bytes, "output": output}
        for key, amount in additions.items():
            if type(amount) is not int or amount < 0 or self.child_used[key] + amount > self.child_quota[key]:
                raise RuntimeError("prefix-bank-child-prepaid-envelope")
        for key, amount in additions.items():
            self.child_used[key] += amount

    @staticmethod
    def _digest_regions(*regions):
        digest = hashlib.sha256()
        for region in regions:
            digest.update(region)
        return digest.digest()

    @staticmethod
    def _materialize(carrier, width):
        carrier.prospective(width)
        os.ftruncate(carrier.fd, width)
        carrier.hold.commit(output=width)
        carrier.count = width

    def _birth(self, carrier, width, birth_attr, map_attr):
        from selected_owned_values import OwnedMappingBirth
        birth = OwnedMappingBirth.__new__(OwnedMappingBirth)
        if not hasattr(carrier, "selected_mapping_births"):
            carrier.selected_mapping_births = []
        carrier.selected_mapping_births.append(birth)
        setattr(self, birth_attr, birth)
        birth.__init__(carrier, width, carrier.name)
        setattr(self, map_attr, birth.create())

    def attach_late(self, carrier, metadata):
        if self.kind != "primary" or os.getpid() != self.parent_pid:
            raise RuntimeError("prefix-bank-late-before-fork-only")
        if self.attach_attempted:
            if self.attachment_error is not None:
                raise self.attachment_error
            raise RuntimeError("prefix-bank-attachment-once")
        if self.bound or self.attempted or self.late is not None or self.external is not None:
            raise RuntimeError("prefix-bank-late-before-fork-only")
        self.attach_attempted = True
        try:
            return self._attach_late_once(carrier, metadata)
        except BaseException as error:
            # Preserve the first actual preparation/cut/join fault and partial
            # real birth records. Do not retry a possibly effectful preparation.
            self.attachment_error = error
            raise

    def _attach_late_once(self, carrier, metadata):
        late = type(self).prepare(carrier, metadata)
        self.late = late
        late.primary = self
        if (late.kind != "store" or late.store_map is None
                or late.pair_layout != self.pair_layout
                or late.payload is self.payload or late.map is self.map):
            raise RuntimeError("prefix-bank-alljob-store-CODE")
        # Keep both real owners BEFORE fallible cuts/container construction.
        from selected_owned_values import charge_selected_allocation
        charge_selected_allocation(self.prepared.hold, 4096)
        cuts = [self.meta_birth.cut(self,cohort=True), self.map_birth.cut(self,cohort=True),
                late.meta_birth.cut(late,cohort=True), late.store_birth.cut(late,cohort=True)]
        bindings = []
        slices = []
        offset = 0
        ordered = (self.map_birth, self.meta_birth, late.meta_birth, late.store_birth)
        widths = (self.pair_layout["primary_body"], self.unit, self.unit,
                  self.pair_layout["late_body"])
        for birth, expected_width in zip(ordered, widths):
            raw = getattr(birth, "before_effect_raw", None)
            cut = getattr(birth, "before_effect_cut", None)
            if (type(raw) is not bytes or len(raw) != birth.width
                    or birth.width != expected_width or type(cut) is not dict
                    or cut.get("full_bytes") is not raw):
                raise RuntimeError("prefix-bank-actual-preimage-custody")
            # No write/readback of these bytes into another physical file.
            # Different birth/row/credit owners remain different prefix regions
            # even if their complete bytes happen to compare equal.
            bindings.append((birth, cut, offset, raw))
            slices.append((offset, raw))
            offset += len(raw)
        # Distinct mapping and cursor are retained from each actual factory.
        # A primary publication (including a partial write or ACK failure)
        # must not become an unjournalled prefix of the late packet body.
        late.external = tuple(slices)
        late.preimage_bindings = tuple(bindings)
        late.store_whole = late.store_birth.before_effect_raw
        self.external = late.external
        self.preimage_bindings = late.preimage_bindings
        self.store_whole = late.store_whole
        self.store_map = late.store_map
        self.before_effect_cut = cuts
        late.before_effect_cut = cuts
        self.retained_fds = self.retained_fds + late.retained_fds
        self._require_preimages()
        return late

    def _require_preimages(self):
        bindings = self.preimage_bindings
        if (type(bindings) is not tuple or len(bindings) != 4
                or type(self.external) is not tuple or len(self.external) != 4):
            raise RuntimeError("prefix-bank-actual-preimage-custody")
        offset = 0
        for index, (birth, cut, found, raw) in enumerate(bindings):
            if (found != offset or type(raw) is not bytes or len(raw) != birth.width
                    or getattr(birth, "before_effect_raw", None) is not raw
                    or getattr(birth, "before_effect_cut", None) is not cut
                    or cut.get("full_bytes") is not raw
                    or self.external[index][0] != found or self.external[index][1] is not raw):
                raise RuntimeError("prefix-bank-actual-preimage-custody")
            birth.require_cut(cut)
            offset += len(raw)
        if self.store_whole is not bindings[3][3]:
            raise RuntimeError("prefix-bank-actual-preimage-custody")
        if offset != self.pair_layout["full_beforeimages"]:
            raise RuntimeError("prefix-bank-full-beforeimage-dimensions")
        return offset

    def retain_cohort_before_effect(self):
        if self.before_effect_cut is None:
            raise RuntimeError("alljob-cohort-cut-before-store")
        return self.before_effect_cut

    def _require_cuts(self):
        from selected_owned_values import require_selected_mapping_cuts
        require_selected_mapping_cuts(self)
        other = self.late if self.late is not None else self.primary
        if other is not None:
            require_selected_mapping_cuts(other)
        self._require_preimages()

    def bind_child(self):
        if self.bound or self.attempted or self.child_cost_closed or os.getppid() != self.parent_pid:
            raise RuntimeError("prefix-bank-actual-parent-once")
        self.bound = True
        self.pid = os.getpid()
        self.owner_pid = self.pid
        self._child_debit(allocation=_HEAD.size + 256, output=_HEAD.size)
        self.meta[:_HEAD.size] = _HEAD.pack(_MAGIC, 1, self.pid, 0, 0, b"\0" * 32)

    def _store_width(self):
        if type(self.store_whole) is not bytes or self.payload is None:
            raise RuntimeError("alljob-body-is-external-meta-preimage")
        return self._require_preimages()

    def _image_matches(self, offset, piece):
        if self.external is not None:
            for found, raw in self.external:
                if offset == found and piece == raw:
                    return True
        return False

    def _journal_bytes(self):
        width = _COUNT.size + len(self.span_log) * _SPAN.size
        if width > self.unit - _HEADER:
            raise RuntimeError("prefix-bank-journal-bound-before-allocation")
        parts = [_COUNT.pack(len(self.span_log))]
        for label, offset, nbytes in self.span_log:
            if label == "payload":
                kind = _KIND_PAYLOAD
            elif label == "preimage":
                kind = _KIND_PREIMAGE
            else:
                raise RuntimeError("prefix-bank-full-span")
            if type(offset) is not int or type(nbytes) is not int or offset < 0 or nbytes < 0:
                raise RuntimeError("prefix-bank-full-span")
            parts.append(_SPAN.pack(kind, offset, nbytes))
        return b"".join(parts)

    def add(self, value):
        if self.pid != os.getpid() or self.attempted:
            raise RuntimeError("prefix-bank-child-writer")
        if type(value) not in (bytes, bytearray):
            raise RuntimeError("prefix-bank-body-type")
        store_width = self._store_width()
        if self.external is None:
            raise RuntimeError("alljob-body-is-external-meta-preimage")
        # Every actual byte-value alias has a journal row, including empty
        # bytes and a full before-image alias. Admit that row before its copy,
        # append or payload write; a constant two-span model is not this ABI.
        journal_width = _COUNT.size + (len(self.span_log) + 1) * _SPAN.size
        if (len(self.span_log) >= _NODE_CEILING
                or journal_width > self.unit - _HEADER):
            raise RuntimeError("prefix-bank-journal-bound-before-body-effect")
        # Validate full physical width and prepay a mutable snapshot BEFORE
        # constructing it. Original immutable preimage aliases need no copy.
        width = len(value)
        if type(value) is bytearray and width > self.payload.capacity - self.payload.count:
            raise RuntimeError("prefix-bank-full-body-bound-CODE need=%d have=%d" % (width, self.payload.capacity - self.payload.count))
        self._child_debit(allocation=512 + (width if type(value) is bytearray else 0),
                          reads=width if type(value) is bytearray else 0)
        raw = value if type(value) is bytes else bytes(value)
        for offset, snap in self.external:
            if raw is snap:
                self.span_log.append(("preimage", offset, len(snap)))
                self.aliases.append((value, raw, offset))
                return {"offset": offset, "bytes": len(snap)}
        width = len(raw)
        room = self.payload.capacity - self.payload.count
        if width > room:
            raise RuntimeError("prefix-bank-full-body-bound-CODE need=%d have=%d" % (width, room))
        local = self.payload.count
        self._child_debit(reads=width, output=width)
        physical = self.payload.start + local
        if physical < self.payload.start or physical + width > len(self.payload.map):
            raise RuntimeError("prefix-bank-disjoint-physical-payload-bound")
        self.payload.map[physical:physical + width] = raw
        self.payload.count += width
        self.count = self.payload.count
        offset = store_width + local
        self.span_log.append(("payload", offset, width))
        self.aliases.append((value, raw, offset))
        return {"offset": offset, "bytes": width}

    def finish(self):
        self._require_cuts()
        if self.attempted:
            raise RuntimeError("prefix-bank-body-once")
        for value, raw, offset in self.aliases:
            self._child_debit(reads=len(value) + len(raw))
            if len(value) != len(raw) or memoryview(value) != memoryview(raw):
                raise RuntimeError("prefix-bank-original-alias-drift")
        store_width = self._store_width()
        self.count = self.payload.count
        return {"endpoint": self.endpoint, "bytes": store_width + self.count}

    def publish(self, packet):
        if self.pid != os.getpid() or self.attempted:
            raise RuntimeError("prefix-bank-publication-once")
        self.attempted = True
        # Existing full canonical packet domain, SAME original 2M ceiling.
        # Exact builtin structural width precedes JSON/ASCII/journal allocation.
        # This is not a claim about arbitrary callback/native heap costs.
        from common import encoded_bound
        journal_width = _COUNT.size + len(self.span_log) * _SPAN.size
        room = self.unit - _HEADER
        if journal_width > room:
            raise RuntimeError("prefix-bank-journal-bound-before-allocation")
        self._child_debit(allocation=_SCAN_WORKSPACE)
        packet_width = encoded_bound(packet, maximum=_PACKET_CEILING)
        if packet_width + journal_width > room:
            raise RuntimeError("prefix-bank-metadata-bound-CODE need=%d room=%d ceiling=%d journal=%d" % (
                packet_width, room, _PACKET_CEILING, journal_width))
        store_width = self._store_width()
        total = packet_width + journal_width + self.count + store_width
        self._child_debit(
            allocation=4 * packet_width + 3 * journal_width + self.count + store_width +
                len(self.span_log) * 128 + _SCAN_WORKSPACE,
            reads=total + packet_width, hash_bytes=total,
            output=packet_width + journal_width + _HEAD.size + _REGION.size)
        raw = (json.dumps(packet, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
        if len(raw) != packet_width:
            raise RuntimeError("prefix-bank-canonical-size-drift")
        journal = self._journal_bytes()
        start = self.payload.start
        payload = bytes(self.payload.map[start:start + self.count])
        if len(journal) != journal_width or len(payload) != self.count:
            raise RuntimeError("prefix-bank-physical-full-drift")
        # F98 frame: exact raw, journal, physical payload, then FOUR complete
        # original before images. No virtual-prefix/full-concat construction
        # in the child. Parent verifies against its own actual retained objects.
        digest_state = hashlib.sha256()
        for region in (raw, journal, payload):
            digest_state.update(region)
        for offset, snap in self.external:
            digest_state.update(snap)
        digest = digest_state.digest()
        self.raw_packet = raw
        self.packet = packet
        self.meta[_HEAD.size:_HEAD.size + _REGION.size] = _REGION.pack(store_width, self.count, len(journal), len(self.span_log))
        self.meta[_HEADER:_HEADER + len(raw)] = raw
        self.meta[_HEADER + len(raw):_HEADER + len(raw) + len(journal)] = journal
        self.meta[:_HEAD.size] = _HEAD.pack(_MAGIC, 2, self.pid, len(raw), self.count, digest)
        self.map.flush()
        self.meta.flush()
        if self.store_map is not self.map:
            self.store_map.flush()
        return {"owner_pid": self.pid, "parent_pid": self.parent_pid,
                "full_bytes_supplied": True, "receiver_confirmed": False}

    def _parse_journal(self, blob, span_count):
        if type(span_count) is not int or span_count < 0 or len(blob) != _COUNT.size + span_count * _SPAN.size:
            raise RuntimeError("prefix-bank-physical-full-drift")
        count = _COUNT.unpack_from(blob, 0)[0]
        if count != span_count:
            raise RuntimeError("prefix-bank-physical-full-drift")
        spans = []
        at = _COUNT.size
        for _ in range(count):
            kind, offset, nbytes = _SPAN.unpack_from(blob, at)
            at += _SPAN.size
            if kind == _KIND_PAYLOAD:
                label = "payload"
            elif kind == _KIND_PREIMAGE:
                label = "preimage"
            else:
                raise RuntimeError("prefix-bank-physical-full-drift")
            if offset + nbytes < offset:
                raise RuntimeError("prefix-bank-physical-full-drift")
            spans.append((label, offset, nbytes))
        return spans

    def read_parent(self, pid):
        if self.parent_endpoint_finished:
            return self.packet if self.parent_read_attempted else None
        if os.getpid() != self.parent_pid:
            raise RuntimeError("prefix-bank-parent-reader")
        if self.parent_read_attempted:
            if self.parent_read_error is not None:
                raise self.parent_read_error
            return self.packet
        try:
            return self._read_parent_once(pid)
        except BaseException as error:
            # An actual valid-frame read/decoder failure is retained once.
            # Empty/unpublished headers can still be observed by the original
            # caller; a failed allocation, read or decode is never repeated.
            self.parent_read_attempted = True
            self.parent_read_error = error
            # Existing preconstructed field holds the actual error/traceback;
            # no fallible list growth can mask that first fault.
            raise

    def _read_parent_once(self, pid):
        if os.getpid() != self.parent_pid:
            raise RuntimeError("prefix-bank-parent-reader")
        self._require_cuts()
        if self.parent_read_attempted:
            if self.parent_read_error is not None:
                raise self.parent_read_error
            return self.packet
        if type(self.store_whole) is not bytes or self.store_map is None or self.external is None:
            raise RuntimeError("prefix-bank-physical-full-drift")
        from selected_owned_values import charge_selected_allocation
        head_span = _HEAD.size + _REGION.size
        charge_selected_allocation(self.prepared.hold, head_span * 2)
        self.prepared.hold.commit(reads=head_span)
        first = bytes(self.meta[:_HEAD.size])
        region = bytes(self.meta[_HEAD.size:_HEAD.size + _REGION.size])
        magic, state, owner, n, width, digest = _HEAD.unpack(first)
        store_w, payload_w, journal_len, span_count = _REGION.unpack(region)
        if magic != _MAGIC or state not in (2, 3) or owner != pid:
            return None
        room = self.unit - _HEADER
        if (not 1 <= n <= _PACKET_CEILING or n > room or journal_len < _COUNT.size
                or _HEADER + n + journal_len > self.unit or width != payload_w
                or width > self.capacity or store_w != self._store_width()):
            raise RuntimeError("prefix-bank-physical-length need=%d width=%d room=%d journal=%d" % (n, width, room, journal_len))
        total = n + journal_len + width + store_w
        self.parent_read_attempted = True
        # Full copies, retained joined body and journal row objects are debited
        # before construction. Decoder gets its separate lexical debit below.
        charge_selected_allocation(self.prepared.hold,
            2 * total + store_w + width + span_count * 256 + _SCAN_WORKSPACE)
        self.parent_copies_allocation += 2 * total + store_w + width + span_count * 256 + _SCAN_WORKSPACE
        self.prepared.hold.commit(reads=total + head_span, hash_bytes=total)
        raw = bytes(self.meta[_HEADER:_HEADER + n])
        journal = bytes(self.meta[_HEADER + n:_HEADER + n + journal_len])
        start = self.payload.start
        body = bytes(self.payload.map[start:start + width])
        # Allocation/read/hash debit above precedes the full join. Actual
        # retained immutable objects, not zeros/digests, are authoritative.
        store = b"".join(snap for offset, snap in self.external)
        if (len(store) != store_w
                or self._digest_regions(raw, journal, body, store) != digest
                or bytes(self.meta[:_HEAD.size]) != first):
            raise RuntimeError("prefix-bank-physical-full-drift")
        for offset, snap in self.external:
            if type(snap) is not bytes or store[offset:offset + len(snap)] != snap:
                raise RuntimeError("prefix-bank-physical-full-drift")
        from common import json_preflight
        decode_allocation = json_preflight(raw, maximum=_PACKET_CEILING)
        charge_selected_allocation(self.prepared.hold, decode_allocation)
        self.parent_copies_allocation += decode_allocation
        spans = self._parse_journal(journal, span_count)
        value = json.loads(raw)
        if type(value) is not dict or value.get("owner_pid") != pid or value.get("parent_pid") != self.parent_pid:
            raise RuntimeError("prefix-bank-physical-owner")
        self.raw_packet = raw
        self.packet = value
        self.count = width
        self.parent_store = store
        self.parent_payload = body
        self.parent_body = store + body
        self.parent_journal = journal
        self.parent_spans = spans
        self.parent_region_journal = True
        return value

    def require(self, descriptor):
        if self.parent_region_journal is not True or not hasattr(self, "parent_body") or type(descriptor) is not dict:
            raise RuntimeError("prefix-bank-full-parent-reader")
        endpoint = descriptor.get("endpoint")
        if descriptor.get("bytes") != len(self.parent_body) or endpoint not in (self.endpoint, "preowned-native-prefix-bank"):
            raise RuntimeError("prefix-bank-full-parent-reader")
        return self.parent_body

    def require_held_binding(self,nodes,pin,raw,descriptor):
        if os.getpid()!=self.parent_pid:raise RuntimeError('held_binding_parent_reader_only')
        from observer import full_value_literal
        from selected_owned_values import _own_native,charge_selected_allocation
        original=full_value_literal(nodes,pin);span=nodes[raw][1]
        charge_selected_allocation(self.prepared.hold,span['bytes']+131072)
        body=self.require(descriptor);held=body[span['offset']:span['offset']+span['bytes']]
        native=_own_native()
        if native is None or native.own_held_body_check(original,held) is not True:
            raise RuntimeError('prefix_full_original_held_Source_body')
    def require_runtime_support(self,serial):
        if os.getpid()!=self.parent_pid:raise RuntimeError('runtime_support_parent_reader_only')
        from selected_owned_values import require_runtime_support
        require_runtime_support(serial)
    def validate_spans(self, nodes, descriptor):
        raw = self.require(descriptor)
        if type(nodes) is not list or self.parent_region_journal is not True:
            raise RuntimeError("prefix-bank-full-span")
        store = self.parent_store
        payload = self.parent_payload
        if type(store) is not bytes or type(payload) is not bytes or len(raw) != len(store) + len(payload):
            raise RuntimeError("prefix-bank-full-span")
        # Parent joined bytes already exist. Do not construct another complete
        # store+payload merely to compare it. Keep store-prefix offset meaning.
        self.prepared.hold.commit(reads=2 * len(raw))
        if memoryview(raw)[:len(store)] != memoryview(store) or memoryview(raw)[len(store):] != memoryview(payload):
            raise RuntimeError("prefix-bank-full-span")
        store_width = len(store)
        cursor = store_width
        index = 0
        spans = self.parent_spans
        for row in nodes:
            if type(row) is not list or len(row) != 2:
                raise RuntimeError("prefix-bank-full-span")
            kind, body = row
            if kind not in ("bytes", "bytearray"):
                continue
            if type(body) is not dict or set(body) != {"offset", "bytes"}:
                raise RuntimeError("prefix-bank-full-span")
            if index >= len(spans):
                raise RuntimeError("prefix-bank-full-span")
            kind_span, offset, nbytes = spans[index]
            index += 1
            if body["offset"] != offset or body["bytes"] != nbytes or offset < 0 or nbytes < 0 or offset + nbytes > len(raw):
                raise RuntimeError("prefix-bank-full-span")
            from selected_owned_values import charge_selected_allocation
            # All comparisons below are borrowed views, not new full bodies.
            # A fixed temporary-view charge precedes their construction.
            charge_selected_allocation(self.prepared.hold, 1024)
            self.prepared.hold.commit(reads=8 * nbytes)
            piece = memoryview(raw)[offset:offset + nbytes]
            if kind_span == "payload":
                if offset != cursor or nbytes > len(raw) - cursor:
                    raise RuntimeError("prefix-bank-full-span")
                local = offset - store_width
                if local < 0 or piece != memoryview(payload)[local:local + nbytes]:
                    raise RuntimeError("prefix-bank-full-span")
                cursor += nbytes
            elif kind_span == "preimage":
                if offset + nbytes > store_width or piece != memoryview(store)[offset:offset + nbytes] or not self._image_matches(offset, piece):
                    raise RuntimeError("prefix-bank-full-span")
            else:
                raise RuntimeError("prefix-bank-full-span")
        if index != len(spans) or cursor != len(raw):
            raise RuntimeError("prefix-bank-unreferenced-full-bytes")
        return raw

    def acknowledge(self, pid, accepted):
        if os.getpid() != self.parent_pid or accepted is not self.packet or self.raw_packet is None:
            raise RuntimeError("prefix-bank-actual-accepted-reader")
        if self.ack_attempted:
            if self.ack_error is not None:
                raise self.ack_error
            raise RuntimeError("prefix-bank-actual-ack-once")
        self.ack_attempted = True
        try:
            return self._acknowledge_once(pid, accepted)
        except BaseException as error:
            self.ack_error = error
            raise

    def _acknowledge_once(self, pid, accepted):
        if os.getpid() != self.parent_pid or accepted is not self.packet or self.raw_packet is None:
            raise RuntimeError("prefix-bank-actual-accepted-reader")
        from selected_owned_values import charge_selected_allocation
        head_span = _HEAD.size + _REGION.size
        charge_selected_allocation(self.prepared.hold, head_span * 2 + 1024)
        self.prepared.hold.commit(reads=head_span + len(self.raw_packet) + len(self.parent_journal))
        magic, state, owner, n, width, digest = _HEAD.unpack(self.meta[:_HEAD.size])
        store_w, payload_w, journal_len, span_count = _REGION.unpack(self.meta[_HEAD.size:_HEAD.size + _REGION.size])
        if (magic != _MAGIC or state != 2 or owner != pid or n != len(self.raw_packet)
                or width != self.count or payload_w != width or store_w != len(self.parent_store)
                or width != len(self.parent_payload)
                or journal_len != len(self.parent_journal) or span_count != len(self.parent_spans)
                or memoryview(self.meta)[_HEADER:_HEADER + n] != memoryview(self.raw_packet)
                or memoryview(self.meta)[_HEADER + n:_HEADER + n + journal_len] != memoryview(self.parent_journal)):
            raise RuntimeError("prefix-bank-accepted-physical-drift")
        self.prepared.hold.commit(output=_HEAD.size)
        self.accepted = accepted
        try:
            self.meta[:_HEAD.size] = _HEAD.pack(magic, 3, owner, n, width, digest)
        except BaseException as error:
            self.ack_error = error
            raise
        self.ack_completed = True

    def accepted_by_parent(self):
        if not self.bound or self.pid != os.getpid():
            return False
        if self.child_cost_attempted:
            if self.child_cost_error is not None:
                raise self.child_cost_error
            return self.child_cost_closed
        self._child_debit(allocation=2 * _HEAD.size + 256, reads=_HEAD.size)
        magic, state, owner, n, width, digest = _HEAD.unpack(self.meta[:_HEAD.size])
        accepted = magic == _MAGIC and state == 3 and owner == self.pid and self.attempted and self.publication_error is None
        if accepted:
            self._seal_child_costs()
        return accepted

    def _write_cost_frame(self, phase):
        used = self.child_used
        fields = (_COST_MAGIC, phase, os.getpid(), used["allocation"],
                  used["reads"], used["hash_bytes"], used["output"])
        raw = _COST_DATA.pack(*fields)
        wire = _COST.pack(*fields, hashlib.sha256(raw).digest())
        offset = _COST_AT if self.kind == "primary" else _COST_AT + _COST.size
        self.store_map[offset:offset + _COST.size] = wire
        return wire

    def prepare_exec_handoff(self):
        # The actual child writes this full bounded frame immediately BEFORE
        # its existing execve, after the ordinary full owner reader/G/A.
        # An exec failure must bind a bank (nonzero HEAD) before body effects.
        # A missing/torn frame or bound-but-unsealed failure retains MAX credit.
        if (os.getppid() != self.parent_pid or os.getpid() == self.parent_pid
                or self.bound or self.attempted or self.exec_handoff_attempted):
            raise RuntimeError("prefix-bank-actual-exec-handoff-once")
        self.exec_handoff_attempted = True
        self._quota_debit(allocation=512, hash_bytes=_COST_DATA.size, output=_COST.size)
        try:
            return self._write_cost_frame(_COST_UNUSED)
        except BaseException as error:
            self.child_cost_error = error
            raise

    def seal_unused_after_primary(self):
        # Only the actual child, after a full accepted primary final report,
        # may finish its never-bound late endpoint before the existing _exit.
        if (self.kind != "store" or self.primary is None
                or not self.primary.child_cost_closed or self.bound or self.attempted
                or os.getppid() != self.parent_pid):
            raise RuntimeError("prefix-bank-unused-late-terminal-UNCONFIRMED")
        if self.child_cost_closed:
            return
        if not self.exec_handoff_attempted:
            self.prepare_exec_handoff()
        if self.child_cost_error is not None:
            raise self.child_cost_error
        self.child_cost_attempted = True
        self.child_cost_closed = True

    def _seal_child_costs(self):
        # Finish AFTER the actual accepted-parent header read, and before the
        # child exits. These are conservative prepaid debits, not shared-dict
        # observations. No further read/write/debit is allowed after this frame.
        self._child_debit(allocation=512, hash_bytes=_COST_DATA.size, output=_COST.size)
        self.child_cost_attempted = True
        try:
            wire = self._write_cost_frame(_COST_FINAL)
        except BaseException as error:
            self.child_cost_error = error
            raise
        self.child_cost_closed = True
        return wire

    def read_terminal_costs(self, owner, pid, status):
        observer = self.prepared.hold.meter
        observer._owned()
        wait = owner.parent_table_end
        if (owner.failure_mailbox not in (self, self.primary)
                or owner.parent_child_pid != pid or type(wait) is not dict
                or not any(row is wait for row in observer.waits)
                or wait.get("pid") != pid or wait.get("raw_wait_status") != status
                or pid not in observer.final_io):
            raise RuntimeError("prefix-bank-actual-child-end-cost-join")
        from selected_owned_values import charge_selected_allocation
        charge_selected_allocation(self.prepared.hold, 4096)
        self.prepared.hold.commit(reads=2 * _COST.size + _HEAD.size,
                                  hash_bytes=_COST_DATA.size)
        offset = _COST_AT if self.kind == "primary" else _COST_AT + _COST.size
        wire = bytes(self.store_map[offset:offset + _COST.size])
        fields = _COST.unpack(wire)
        magic, phase, actual_pid, allocation, reads, hash_bytes, output, digest = fields
        if (magic != _COST_MAGIC or actual_pid != pid
                or hashlib.sha256(wire[:_COST_DATA.size]).digest() != digest
                or bytes(self.store_map[offset:offset + _COST.size]) != wire):
            raise RuntimeError("prefix-bank-child-cost-full-frame-UNCONFIRMED")
        amounts = dict(zip(("allocation", "reads", "hash_bytes", "output"),
                           (allocation, reads, hash_bytes, output)))
        if phase == _COST_UNUSED:
            # Full ZERO header from the actual before-cut, plus this Source's
            # actual pre-exec frame. Equality alone would NOT prove no IO.
            head = bytes(self.meta[:_HEAD.size])
            if (head != self.meta_birth.before_effect_raw[:_HEAD.size]
                    or head != b"\0" * _HEAD.size or self.packet is not None
                    or self.parent_read_error is not None or self.ack_attempted):
                raise RuntimeError("prefix-bank-unused-child-cost-UNCONFIRMED")
            # Include one possibly attempted, interrupted first bind header.
            # This is an upper debit, not an assertion of zero/observed IO.
            amounts["output"] += _HEAD.size
        elif phase == _COST_FINAL:
            if (self.accepted is not self.packet or self.packet is None
                    or not self.ack_completed or self.ack_error is not None
                    or self.parent_read_error is not None):
                raise RuntimeError("prefix-bank-final-full-reader-UNCONFIRMED")
            head = _HEAD.unpack(self.meta[:_HEAD.size])
            if head[0] != _MAGIC or head[1] != 3 or head[2] != pid:
                raise RuntimeError("prefix-bank-final-child-cost-UNCONFIRMED")
        else:
            raise RuntimeError("prefix-bank-child-cost-phase-UNCONFIRMED")
        if any(value < 0 or value > self.child_quota[key] for key, value in amounts.items()):
            raise RuntimeError("prefix-bank-child-cost-quota-UNCONFIRMED")
        self.parent_cost_wire = wire
        return {"bank": self, "wire": wire, "phase": phase, "pid": pid,
                "wait": wait, "final_io": observer.final_io[pid],
                "amounts": amounts, "costs_are_observed": False,
                "costs_are_conservative_child_debits": True}
