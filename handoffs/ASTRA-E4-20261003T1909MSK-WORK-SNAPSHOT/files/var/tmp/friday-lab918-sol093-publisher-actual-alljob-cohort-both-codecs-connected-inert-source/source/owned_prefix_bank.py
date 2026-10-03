"""Shared payload plus one immutable store, both original receivers.

Four existing carriers per job. The late body is the store and is not written
after the cohort cut. add and read_parent use the primary body mapping.
"""
import hashlib
import json
import os
import struct

_MAGIC = b"F90BANK1"
_HEAD = struct.Struct(">8sQQQQ32s")
_PACKET_CEILING = 2_000_000
_HEADER = 128


class _Payload:
    def __init__(self, mapping, capacity):
        self.map = mapping
        self.capacity = capacity
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
        self.before = os.fstat(fd)
        self.kind = None
        self.unit = None
        self.payload = None
        self.store_map = None
        self.store_birth = None
        self.external = None
        self.store_whole = None
        self.before_effect_cut = None
        if capacity < 1 or self.before.st_size != capacity:
            raise RuntimeError("prefix-bank-actual-prepared-size")

    @classmethod
    def prepare(cls, carrier, metadata_carrier):
        obs = carrier.hold.meter
        ledger = getattr(obs, "alljob_cohort", None)
        if (type(ledger) is not dict or ledger.get("fits") is not True
                or ledger.get("caps_changed") is not False or carrier.hold is not obs.terminal_hold
                or metadata_carrier.hold is not obs.terminal_hold):
            raise RuntimeError("prefix-bank-alljob-cohort-CODE")
        slot = ledger.get("next_slot")
        banks = ledger.get("banks")
        unit = ledger.get("unit")
        if type(slot) is not int or type(banks) is not int or slot < 0 or slot >= banks:
            raise RuntimeError("prefix-bank-alljob-slot-CODE")
        if type(unit) is not int or unit < _HEADER + 1:
            raise RuntimeError("prefix-bank-alljob-cohort-CODE")
        kind = "primary" if slot % 2 == 0 else "store"
        body_width = unit if kind == "primary" else 3 * unit
        ledger["next_slot"] = slot + 1
        cls._materialize(metadata_carrier, unit)
        cls._materialize(carrier, body_width)
        mailbox = cls.__new__(cls)
        carrier.mapping_mailbox = mailbox
        mailbox.__init__(carrier.fd, body_width, os.getpid(), carrier, metadata_carrier)
        mailbox.kind = kind
        mailbox.unit = unit
        mailbox._birth(carrier, body_width, "map_birth", "map")
        mailbox._birth(metadata_carrier, unit, "meta_birth", "meta")
        if kind == "primary":
            mailbox.payload = _Payload(mailbox.map, unit)
            mailbox.capacity = unit
            mailbox.store_map = None
        else:
            mailbox.store_map = mailbox.map
            mailbox.store_birth = mailbox.map_birth
        ledger["slots"].append({
            "slot": slot,
            "kind": kind,
            "body_width": body_width,
            "meta_width": unit,
            "name": carrier.name,
        })
        return mailbox

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
        if (self.kind != "primary" or self.bound or self.attempted or self.late is not None
                or self.external is not None or os.getpid() != self.parent_pid):
            raise RuntimeError("prefix-bank-late-before-fork-only")
        late = type(self).prepare(carrier, metadata)
        if late.kind != "store" or late.store_map is None:
            raise RuntimeError("prefix-bank-alljob-store-CODE")
        cuts = [self.meta_birth.cut(self), self.map_birth.cut(self),
                late.meta_birth.cut(late), late.store_birth.cut(late)]
        slices = []
        for birth, offset in ((self.map_birth, 0), (self.meta_birth, self.unit), (late.meta_birth, 2 * self.unit)):
            raw = getattr(birth, "before_effect_raw", None)
            if type(raw) is not bytes or len(raw) != self.unit:
                raise RuntimeError("alljob-cohort-preimage-width")
            if bytes(late.store_map[offset:offset + self.unit]) != raw:
                raise RuntimeError("alljob-cohort-preimage-image")
            slices.append((offset, raw))
        whole = getattr(late.store_birth, "before_effect_raw", None)
        if type(whole) is not bytes or len(whole) != 3 * self.unit:
            raise RuntimeError("alljob-cohort-preimage-width")
        late.map = self.map
        late.payload = self.payload
        late.capacity = self.unit
        late.external = tuple(slices)
        late.store_whole = whole
        late.primary = self
        self.external = late.external
        self.store_whole = whole
        self.store_map = late.store_map
        self.late = late
        self.before_effect_cut = cuts
        late.before_effect_cut = cuts
        self.retained_fds = self.retained_fds + late.retained_fds
        return late

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

    def bind_child(self):
        if self.bound or self.attempted or os.getppid() != self.parent_pid:
            raise RuntimeError("prefix-bank-actual-parent-once")
        self.bound = True
        self.pid = os.getpid()
        self.owner_pid = self.pid
        self.meta[:_HEAD.size] = _HEAD.pack(_MAGIC, 1, self.pid, 0, 0, b"\0" * 32)

    def _known_preimage(self, offset, nbytes):
        if self.external is None:
            return False
        for found, raw in self.external:
            if offset == found and nbytes == len(raw):
                return True
        return self.store_whole is not None and offset == 0 and nbytes == len(self.store_whole)

    def add(self, value):
        if self.pid != os.getpid() or self.attempted:
            raise RuntimeError("prefix-bank-child-writer")
        if type(value) not in (bytes, bytearray):
            raise RuntimeError("prefix-bank-body-type")
        if self.external is None or self.payload is None:
            raise RuntimeError("alljob-body-is-external-meta-preimage")
        raw = value if type(value) is bytes else bytes(value)
        for offset, snap in self.external:
            if raw is snap:
                self.span_log.append(("preimage", offset, len(snap)))
                self.aliases.append((value, raw, offset))
                return {"offset": offset, "bytes": len(snap)}
        if raw is self.store_whole:
            self.span_log.append(("preimage", 0, len(raw)))
            self.aliases.append((value, raw, 0))
            return {"offset": 0, "bytes": len(raw)}
        width = len(raw)
        room = self.payload.capacity - self.payload.count
        if width > room:
            raise RuntimeError("prefix-bank-full-body-bound-CODE need=%d have=%d" % (width, room))
        offset = self.payload.count
        self.payload.map[offset:offset + width] = raw
        self.payload.count += width
        self.count = self.payload.count
        self.span_log.append(("payload", offset, width))
        self.aliases.append((value, raw, offset))
        return {"offset": offset, "bytes": width}

    def finish(self):
        self._require_cuts()
        if self.attempted:
            raise RuntimeError("prefix-bank-body-once")
        for value, raw, offset in self.aliases:
            if len(value) != len(raw) or memoryview(value) != memoryview(raw):
                raise RuntimeError("prefix-bank-original-alias-drift")
        self.count = self.payload.count if self.payload is not None else self.count
        return {"endpoint": self.endpoint, "bytes": self.count}

    def publish(self, packet):
        if self.pid != os.getpid() or self.attempted:
            raise RuntimeError("prefix-bank-publication-once")
        self.attempted = True
        raw = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
        room = self.unit - _HEADER
        if not 1 <= len(raw) <= room or len(raw) > _PACKET_CEILING:
            raise RuntimeError("prefix-bank-metadata-bound-CODE need=%d room=%d ceiling=%d" % (len(raw), room, _PACKET_CEILING))
        self.raw_packet = raw
        self.packet = packet
        self.count = self.payload.count
        self.meta[_HEADER:_HEADER + len(raw)] = raw
        body = bytes(self.map[:self.count])
        digest = hashlib.sha256(raw + body).digest()
        self.meta[:_HEAD.size] = _HEAD.pack(_MAGIC, 2, self.pid, len(raw), self.count, digest)
        self.map.flush()
        self.meta.flush()
        return {"owner_pid": self.pid, "parent_pid": self.parent_pid,
                "full_bytes_supplied": True, "receiver_confirmed": False}

    def read_parent(self, pid):
        if os.getpid() != self.parent_pid:
            raise RuntimeError("prefix-bank-parent-reader")
        self._require_cuts()
        from selected_owned_values import charge_selected_allocation
        charge_selected_allocation(self.prepared.hold, _HEAD.size * 2)
        self.prepared.hold.commit(reads=_HEAD.size)
        first = bytes(self.meta[:_HEAD.size])
        magic, state, owner, n, width, digest = _HEAD.unpack(first)
        if magic != _MAGIC or state not in (2, 3) or owner != pid:
            return None
        if not 1 <= n <= _PACKET_CEILING or n > self.unit - _HEADER or width > self.capacity:
            raise RuntimeError("prefix-bank-physical-length need=%d width=%d room=%d" % (n, width, self.unit - _HEADER))
        charge_selected_allocation(self.prepared.hold, 6 * (n + width) + 131072)
        self.prepared.hold.commit(reads=n + width + _HEAD.size, hash_bytes=n + width)
        raw = bytes(self.meta[_HEADER:_HEADER + n])
        body = bytes(self.map[:width])
        if hashlib.sha256(raw + body).digest() != digest or bytes(self.meta[:_HEAD.size]) != first:
            raise RuntimeError("prefix-bank-physical-full-drift")
        value = json.loads(raw)
        if type(value) is not dict or value.get("owner_pid") != pid or value.get("parent_pid") != self.parent_pid:
            raise RuntimeError("prefix-bank-physical-owner")
        self.raw_packet = raw
        self.packet = value
        self.count = width
        self.parent_body = body
        return value

    def require(self, descriptor):
        if not hasattr(self, "parent_body") or type(descriptor) is not dict:
            raise RuntimeError("prefix-bank-full-parent-reader")
        endpoint = descriptor.get("endpoint")
        if descriptor.get("bytes") != len(self.parent_body) or endpoint not in (self.endpoint, "preowned-native-prefix-bank"):
            raise RuntimeError("prefix-bank-full-parent-reader")
        return self.parent_body

    def validate_spans(self, nodes, descriptor):
        raw = self.require(descriptor)
        if type(nodes) is not list:
            raise RuntimeError("prefix-bank-full-span")
        cursor = 0
        index = 0
        for row in nodes:
            if type(row) is not list or len(row) != 2:
                raise RuntimeError("prefix-bank-full-span")
            kind, body = row
            if kind not in ("bytes", "bytearray"):
                continue
            if type(body) is not dict or set(body) != {"offset", "bytes"}:
                raise RuntimeError("prefix-bank-full-span")
            if index >= len(self.span_log):
                raise RuntimeError("prefix-bank-full-span")
            kind_span, offset, nbytes = self.span_log[index]
            index += 1
            if body["offset"] != offset or body["bytes"] != nbytes:
                raise RuntimeError("prefix-bank-full-span")
            if kind_span == "payload":
                if offset != cursor or nbytes > len(raw) - cursor:
                    raise RuntimeError("prefix-bank-full-span")
                cursor += nbytes
            elif kind_span == "preimage":
                if not self._known_preimage(offset, nbytes):
                    raise RuntimeError("prefix-bank-full-span")
            else:
                raise RuntimeError("prefix-bank-full-span")
        if index != len(self.span_log) or cursor != len(raw):
            raise RuntimeError("prefix-bank-unreferenced-full-bytes")
        return raw

    def acknowledge(self, pid, accepted):
        if os.getpid() != self.parent_pid or accepted is not self.packet or self.raw_packet is None:
            raise RuntimeError("prefix-bank-actual-accepted-reader")
        magic, state, owner, n, width, digest = _HEAD.unpack(self.meta[:_HEAD.size])
        if (magic != _MAGIC or state != 2 or owner != pid or n != len(self.raw_packet)
                or width != len(self.parent_body)
                or bytes(self.meta[_HEADER:_HEADER + n]) != self.raw_packet):
            raise RuntimeError("prefix-bank-accepted-physical-drift")
        if self.ack_attempted:
            raise RuntimeError("prefix-bank-actual-ack-once")
        self.ack_attempted = True
        self.accepted = accepted
        try:
            self.meta[:_HEAD.size] = _HEAD.pack(magic, 3, owner, n, width, digest)
        except BaseException as error:
            self.ack_error = error
            self.errors.append(error)
            raise
        self.ack_completed = True

    def accepted_by_parent(self):
        magic, state, owner, n, width, digest = _HEAD.unpack(self.meta[:_HEAD.size])
        return magic == _MAGIC and state == 3 and owner == self.pid and self.attempted and self.publication_error is None
