"""Bounded material extraction/install, complete inventory, no package scripts.

Only authenticated same-held bodies reach these routines. Root independently
selects the COMPLETE inventory. An omitted member or cap excess refuses the
operation; no representative archive subset is substituted.
"""
import base64
import bz2
import csv
import contextvars
import hashlib
import io
import lzma
import os
import stat
import struct
import tarfile
import zipfile
import zlib
from common import (Refused, DOCUMENT_MAX, MEMBERS_MAX, integer, sha, text,
                    canonical, domain, digest, exact)
from custody import identity9, open_beneath
from lifetime import OwnedFDs

from selected_owned_values import own_context_var,own_context_set,own_context_reset
_ARCHIVE_OWNER=own_context_var("sol053_actual_archive_owner",default=None)

class ArchiveOwner:
    def __init__(self,meter):
        self.meter=meter;self.objects=[];self.holds=[];self.retiring=False;self.error=None
        meter.retain_local_owner(self)
    def retire(self):
        self.retiring=True
        for obj in reversed(self.objects):obj.close()
        self.objects.clear()
        for hold in self.holds:hold.release()
        self.holds.clear()
        self.meter.retire_local_owner(self)

def archive_scope(function):
    def complete(held,*args,**kwargs):
        if _ARCHIVE_OWNER.get() is not None:return function(held,*args,**kwargs)
        owner=ArchiveOwner(held.meter);token=own_context_set(_ARCHIVE_OWNER,owner)
        try:return function(held,*args,**kwargs)
        except BaseException as exc:
            owner.error=exc;held.meter.retain_error_arena(exc)
            # Nested walker/decoder/path locals remain funded until the exact
            # whole actor arena has been accepted and its table confirmed.
            raise
        finally:
            # The actual parser function frame is gone before decoder/view/
            # ZIP metadata credit retires. Returned installed metadata has its
            # own prospectively acquired Installer ownership lease.
            try:
                if owner.error is None:owner.retire()
            finally:own_context_reset(_ARCHIVE_OWNER,token)
    return complete

def defer_retirement(hold):
    owner=_ARCHIVE_OWNER.get()
    if owner is None:hold.release()
    else:owner.holds.append(hold)


def relative(name):
    text(name,240)
    if name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in name.rstrip("/").split("/")):
        raise Refused("archive_member_path")
    return name.rstrip("/")


class FDView:
    def __init__(self, held, offset=0, size=None):
        self.held,self.offset,self.size,self.at = held,offset,held.size if size is None else size,0
        self.owned=[]
        self.archive_owner=_ARCHIVE_OWNER.get()
        if self.archive_owner is not None:self.archive_owner.objects.append(self)
        if offset < 0 or self.size < 0 or offset+self.size > held.size: raise Refused("archive_range")
    def seekable(self): return True
    def readable(self): return True
    def tell(self): return self.at
    def seek(self, offset, whence=0):
        new = offset if whence==0 else self.at+offset if whence==1 else self.size+offset if whence==2 else -1
        if not 0 <= new <= self.size: raise Refused("archive_seek")
        self.at = new;return new
    def read(self,size=-1):
        if size < 0: size = self.size-self.at
        if size > DOCUMENT_MAX: raise Refused("archive_read")
        size = min(size,self.size-self.at)
        if size==0:return b""
        hold = self.held.meter.reserve("archive-input",reads=size,allocation=size+64)
        try:
            raw=os.pread(self.held.fd,size,self.offset+self.at)
            if len(raw)!=size: raise Refused("archive_short")
            self.at+=size;hold.commit(reads=size);self.held.check()
            self.held.meter.own_result(raw,hold);hold=None
            self.owned.append(raw)
            return raw
        finally:
            if hold is not None:hold.release()
    def close(self):
        if self.archive_owner is not None and not self.archive_owner.retiring:return
        keys=[id(raw) for raw in self.owned]
        self.owned.clear()
        for key in keys:self.held.meter.retire_result_id(key)
    def __enter__(self):return self
    def __exit__(self,*args):self.close();return False


class Decompressed:
    def __init__(self, view, codec, bound, meter):
        self.view,self.bound,self.meter = view,bound,meter
        self.total,self.pending,self.done,self.buffer = 0,b"",False,bytearray()
        integer(bound,DOCUMENT_MAX)
        # Decoder state (including maximum xz dictionary), stream/parser
        # metadata and all returned chunks coexist until archive retirement.
        self.hold=meter.reserve("decoder-and-stream-lifetime-before-construction",
            allocation=256*1024*1024+bound*32+65536)
        self.returned=[];self.closed=False
        self.archive_owner=_ARCHIVE_OWNER.get()
        if self.archive_owner is not None:self.archive_owner.objects.append(self)
        try:
            if codec=="xz": self.decoder=lzma.LZMADecompressor(memlimit=256*1024*1024)
            elif codec=="gz": self.decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
            elif codec=="bz2": self.decoder=bz2.BZ2Decompressor()
            elif codec=="tar": self.decoder=None
            else: raise Refused("archive_codec")
        except BaseException:
            self.hold.release();raise
        self.codec=codec
    def __enter__(self):return self
    def __exit__(self,*args):self.close();return False
    def close(self):
        if self.archive_owner is not None and not self.archive_owner.retiring:return
        if self.closed:return
        self.closed=True;self.decoder=None;self.pending=b"";self.buffer.clear();self.returned.clear()
        self.view.close()
        self.hold.release()
    def read(self,size):
        integer(size,65536)
        if size==0:return b""
        if self.closed:raise Refused("closed_decoder")
        hold=self.meter.reserve("bounded-decompress",allocation=65536*6,reads=131072)
        try:
            while len(self.buffer)<size and not self.done:
                if self.decoder is None:
                    out=self.view.read(min(65536,self.bound-self.total+1))
                    if not out:self.done=True
                else:
                    if self.codec=="gz":
                        data=self.pending or self.view.read(min(65536,self.view.size-self.view.at))
                        out=self.decoder.decompress(data,65536)
                        self.pending=self.decoder.unconsumed_tail
                    else:
                        data=self.view.read(min(65536,self.view.size-self.view.at)) if self.decoder.needs_input else b""
                        out=self.decoder.decompress(data,max_length=65536)
                    if self.decoder.eof:
                        # No concatenated streams/trailing unauthenticated bytes.
                        if self.decoder.unused_data or self.view.at!=self.view.size:
                            raise Refused("archive_trailing")
                        self.done=True
                    elif not data and not out and not self.pending:
                        raise Refused("archive_truncated")
                if self.total+len(out)>self.bound:raise Refused("decompressed_cap")
                self.total+=len(out);self.buffer.extend(out)
                hold.commit(reads=len(out))
                if hold.token is None: raise Refused("decompression_reservation")
            result=bytes(self.buffer[:size]);del self.buffer[:size]
            self.returned.append(result)
            return result
        finally:hold.release()


class Installer:
    def __init__(self, root, meter, operation, selected, existing_directories=()):
        self.root,self.meter,self.operation = root,meter,operation
        self.rootfd=-1;self.fd_hold=None;self.metadata_hold=None
        self.fdjournal=OwnedFDs(meter=meter);self.pending_holds=[]
        self.reached,self.outputs,self.selected = set(),[],{}
        meter.retain_local_owner(self)
        self.metadata_hold=meter.reserve("installed-returned-metadata-before-construction",allocation=len(selected)*16384+65536)
        # The constructor, nested refusal traceback and escaped sealed rows
        # all stay in this actual actor's arena until the same actor ends.
        meter.own_result(self,self.metadata_hold)
        self.fd_hold=meter.reserve("installer-complete-FD-graph-before-open",slots=4)
        self.fdjournal.grant(self.fd_hold)
        self.selected={}
        for row in selected:
            if type(row) is not dict or row["source_name"] in self.selected:raise Refused("member_selection")
            relative(row["source_name"])
            self.selected[row["source_name"]]=row
        if not 1<=len(self.selected)<=MEMBERS_MAX:raise Refused("member_limit")
        self.existing_directories={r["physical_path"]:r for r in existing_directories if r["kind"]=="directory"}
        self.rootfd=self.fdjournal.acquire(os.open,root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,holder="installer-root",credit=self.fd_hold)
        self.reached,self.outputs = set(),[]

    def prepare_directories(self):
        for name,row in sorted(self.selected.items(),key=lambda pair:pair[1]["destination"].count("/")):
            if row["kind"]=="directory":
                self.consume(name,"directory",0)

    def _parents(self, name):
        parts=relative(name).split("/")
        parent=self.fdjournal.acquire(os.dup,self.rootfd,holder="installer-parent",credit=self.fd_hold)
        prefix=""
        for part in parts[:-1]:
            # Every directory must be selected, created and observed by the
            # same complete plan; implicit untracked parents are refused.
            # The child is owned before the parent close is attempted.
            prefix=part if prefix=="" else prefix+"/"+part
            row=self.selected.get(prefix)
            if type(row) is not dict or row.get("kind")!="directory" or prefix not in self.reached:
                raise Refused("installer_parent_not_selected")
            nxt=self.fdjournal.acquire(os.open,part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent,holder="installer-parent",credit=self.fd_hold)
            self.fdjournal.close_one(parent)
            if parent in self.fdjournal.fds:
                raise Refused("parent_close_unconfirmed", detail={"parent_fd":parent,"child_fd":nxt,"status":"UNKNOWN"})
            parent=nxt
        return parent,parts[-1]

    def consume(self, source_name, kind, size, reader=None, target=None, executable=False):
        name=relative(source_name)
        row=self.selected.get(name)
        if row is None:raise Refused("complete_member_inventory")
        if name in self.reached:
            if kind=="directory" and row["kind"]=="directory" and size==0:return
            raise Refused("complete_member_inventory")
        if row["kind"]!=kind or row["size"]!=size or row["link_target"]!=target:
            raise Refused("material_member_identity")
        destination=relative(row["destination"])
        if kind=="regular" and size>DOCUMENT_MAX:raise Refused("member_body_cap")
        hold=self.meter.reserve("material-install-before-effect",reads=size,output=size,
                                 hash_bytes=size,allocation=65536,slots=1)
        self.pending_holds.append(hold)
        self.fdjournal.grant(hold)
        parent=fd=-1;written=0
        try:
            parent,leaf=self._parents(destination)
            if kind=="directory":
                previous=self.existing_directories.get(self.root+"/"+destination)
                if previous is None:
                    os.mkdir(leaf,0o700,dir_fd=parent)
                else:
                    current=identity9(os.stat(leaf,dir_fd=parent,follow_symlinks=False))
                    if previous["path"]!=row["logical_path"] or current[:5]!=previous["identity9_decimal_strings"][:5]:raise Refused("shared_directory_custody")
            elif kind=="symlink":
                if target is None or target.startswith("/") or "\\" in target or "\0" in target:
                    raise Refused("member_link")
                # Full later hierarchy checks also resolve every link chain.
                # Relative links may contain .. only if complete closure resolves
                # them inside the selected image; no filesystem lookup follows it.
                if len(target)>240 or any(p=="" for p in target.split("/")):raise Refused("member_link")
                os.symlink(target,leaf,dir_fd=parent)
            elif kind=="regular":
                fd=self.fdjournal.acquire(os.open,leaf,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent,holder="installer-output",credit=hold)
                h=hashlib.sha256()
                while written<size:
                    part=reader.read(min(65536,size-written))
                    if not part:raise Refused("material_short","execution")
                    hold.commit(hash_bytes=len(part));h.update(part);hold.commit(reads=len(part))
                    at=0
                    while at<len(part):
                        n=os.write(fd,memoryview(part)[at:])
                        if n<=0:raise Refused("install_short","execution")
                        at+=n;written+=n;hold.commit(output=n)
                if reader.read(1):raise Refused("material_size","execution")
                if h.hexdigest()!=row["sha256"]:raise Refused("material_sha","execution")
                os.fsync(fd)
                os.fchmod(fd,0o555 if executable else 0o444)
            else:raise Refused("member_kind")
            s=os.stat(leaf,dir_fd=parent,follow_symlinks=False)
            if s.st_nlink!=1 and kind!="directory":raise Refused("member_nlink")
            path=row["logical_path"]
            self.outputs.append({"path":path,"physical_path":self.root+"/"+destination,
                "kind":kind,"sha256":row["sha256"],"size":size,"operation":self.operation,
                "link_target":target,"identity9_decimal_strings":identity9(s),
                "source_name":name,"executable":executable})
            self.reached.add(name)
        except BaseException:
            self.meter.note_partial(self.root+"/"+destination,written)
            raise
        finally:
            unconfirmed=False
            for f in (fd,parent):
                if type(f) is int and f>=0:
                    self.fdjournal.close_one(f)
                    if f in self.fdjournal.fds:
                        unconfirmed=True
                        meta=self.fdjournal.meta.get(f, {})
                        self.meter.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","fd":f,"credit":meta.get("credit"),"holder":meta.get("holder"),"identity9_decimal_strings":meta.get("identity9_decimal_strings")})
            if self.fdjournal.fds-{self.rootfd}:
                unconfirmed=True
            if not unconfirmed:
                hold.release();self.pending_holds.remove(hold)

    def seal(self):
        if self.reached!=set(self.selected):raise Refused("complete_member_inventory")
        # Seal deepest directories last, then record their actual final metadata.
        for row in sorted(self.outputs,key=lambda r:r["physical_path"].count("/"),reverse=True):
            if row["kind"]=="directory":
                fd=self.fdjournal.acquire(os.open,row["physical_path"],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,holder="installer-seal",credit=self.fd_hold)
                try:os.fchmod(fd,0o555)
                finally:
                    self.fdjournal.close_one(fd)
                    if fd in self.fdjournal.fds:
                        meta=self.fdjournal.meta.get(fd, {})
                        self.meter.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","fd":fd,"credit":meta.get("credit"),"holder":meta.get("holder"),"identity9_decimal_strings":meta.get("identity9_decimal_strings")})
            row["identity9_decimal_strings"]=identity9(os.lstat(row["physical_path"]))
        result_hold=self.meter.reserve('escaped-installed-row-aliases-before-return',allocation=len(self.outputs)*16384+65536)
        result=list(self.outputs)
        self.meter.own_result(result,result_hold)
        return result

    def close(self):
        for fd in tuple(self.fdjournal.fds):
            self.fdjournal.close_one(fd)
            if fd in self.fdjournal.fds:
                meta=self.fdjournal.meta.get(fd, {})
                self.meter.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","fd":fd,"credit":meta.get("credit"),"holder":meta.get("holder"),"identity9_decimal_strings":meta.get("identity9_decimal_strings")})
            elif fd==self.rootfd:
                self.rootfd=-1
        if self.rootfd not in self.fdjournal.fds:self.rootfd=-1
        if self.fdjournal.fds:
            return
        for hold in self.pending_holds:
            hold.release()
        self.pending_holds=[]
        if self.fd_hold is not None:self.fd_hold.release();self.fd_hold=None
        self.meter.retire_local_owner(self)
        # Escaped/traceback metadata is held by own_result(self), and never
        # retired because only its output FD happened to close.
        # Keep shared sealed/error rows in the actual owning metadata arena;
        # FD retirement alone never retires these result/traceback aliases.


@archive_scope
def tar_install(held, installer, codec, decompressed_max):
    integer(decompressed_max,DOCUMENT_MAX)
    installer.prepare_directories()
    with Decompressed(FDView(held),codec,decompressed_max,held.meter) as reader, tarfile.open(fileobj=reader,mode="r|") as archive:
        count=0
        for member in archive:
            count+=1
            if count>MEMBERS_MAX:raise Refused("member_limit")
            # POSIX/deb tar commonly names a container root '.' and prefixes
            # entries './'. It is structural archive metadata, not a new path
            # effect. The complete raw archive remains a same-held preimage.
            if member.name in (".","./"):
                if not member.isdir() or member.size!=0:raise Refused("archive_root_header")
                continue
            raw_name=member.name[2:] if member.name.startswith("./") else member.name
            name=relative(raw_name)
            if member.isdir():
                installer.consume(name,"directory",0)
            elif member.issym():
                installer.consume(name,"symlink",0,target=member.linkname)
            elif member.isreg():
                integer(member.size,DOCUMENT_MAX)
                stream=archive.extractfile(member)
                if stream is None:raise Refused("archive_member")
                with stream:
                    installer.consume(name,"regular",member.size,stream,executable=bool(member.mode&0o111))
            else:
                # No hardlinks, device nodes, FIFO or executable maintainer hook.
                raise Refused("archive_member_kind")
    held.check()
    return installer.seal()


@archive_scope
def deb_data(held):
    view=FDView(held)
    if view.read(8)!=b"!<arch>\n":raise Refused("deb_magic")
    data=None;names=set()
    while view.at<view.size:
        header=view.read(60)
        if len(header)!=60 or header[58:]!=b"\x60\n":raise Refused("deb_header")
        try:
            name=header[:16].decode("ascii").strip().rstrip("/")
            size_text=header[48:58].decode("ascii").strip()
            if not size_text.isdigit():raise Refused("deb_size")
            size=int(size_text)
        except UnicodeError as exc:raise Refused("deb_header") from exc
        if name in names or name not in ("debian-binary","control.tar.gz","control.tar.xz","control.tar.zst",
            "data.tar","data.tar.gz","data.tar.xz","data.tar.bz2"):
            raise Refused("deb_members")
        names.add(name)
        if size>view.size-view.at:raise Refused("deb_size")
        if name=="debian-binary":
            if view.read(size)!=b"2.0\n":raise Refused("deb_version")
        else:
            if name.startswith("data.tar"):
                if data is not None:raise Refused("deb_data")
                data=(view.at,size,"tar" if name=="data.tar" else name.rsplit(".",1)[1])
            view.seek(size,1)
        if size&1:
            if view.read(1)!=b"\n":raise Refused("deb_padding")
    if "debian-binary" not in names or data is None:raise Refused("deb_data")
    return data


@archive_scope
def deb_install(held, installer, decompressed_max):
    installer.prepare_directories()
    offset,size,codec=deb_data(held)
    with Decompressed(FDView(held,offset,size),codec,decompressed_max,held.meter) as bounded, tarfile.open(fileobj=bounded,mode="r|") as archive:
        count=0
        for member in archive:
            count+=1
            if count>MEMBERS_MAX:raise Refused("member_limit")
            name=member.name[2:] if member.name.startswith("./") else member.name
            if name in (".","./"):continue
            name=relative(name)
            if member.isdir():installer.consume(name,"directory",0)
            elif member.issym():installer.consume(name,"symlink",0,target=member.linkname)
            elif member.isreg():
                integer(member.size,DOCUMENT_MAX)
                stream=archive.extractfile(member)
                if stream is None:raise Refused("archive_member")
                with stream:installer.consume(name,"regular",member.size,stream,executable=bool(member.mode&0o111))
            else:raise Refused("archive_member_kind")
    held.check();return installer.seal()


@archive_scope
def zip_preflight(held):
    count=min(65557,held.size)
    hold=held.meter.reserve("zip-footer-before-read",reads=count,allocation=count*2)
    try:
        tail=os.pread(held.fd,count,max(0,held.size-65557));hold.commit(reads=len(tail))
    finally:defer_retirement(hold)
    at=tail.rfind(b"PK\x05\x06")
    if at<0 or len(tail)-at<22:raise Refused("zip_directory")
    _,disk,start_disk,disk_count,total,size,offset,comment=struct.unpack("<4s4H2LH",tail[at:at+22])
    if disk or start_disk or disk_count!=total or not 1<=total<=MEMBERS_MAX or size>INPUT_MAX_LOCAL or offset+size>held.size or at+22+comment!=len(tail):
        raise Refused("zip_capacity")
    return total,size


INPUT_MAX_LOCAL=2_000_000


@archive_scope
def zip_install(held,installer):
    """Actual complete browser ZIP installation, not a tar-codec placeholder."""
    installer.prepare_directories()
    count,size=zip_preflight(held)
    hold=held.meter.reserve("zip-directory-before-allocation",reads=size,allocation=size*16+count*16384+65536*16)
    try:
        with zipfile.ZipFile(FDView(held)) as archive:
            infos=archive.infolist()
            if len(infos)!=count:raise Refused("complete_zip_inventory")
            names=set()
            for info in infos:
                name=relative(info.filename)
                if name in names or info.flag_bits&1 or info.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):raise Refused("zip_member")
                names.add(name);integer(info.file_size,DOCUMENT_MAX)
                mode=info.external_attr>>16;kind=stat.S_IFMT(mode)
                if info.is_dir():installer.consume(name,"directory",0)
                elif kind in (0,stat.S_IFREG):
                    with archive.open(info) as f:installer.consume(name,"regular",info.file_size,f,executable=bool(mode&0o111))
                elif kind==stat.S_IFLNK:
                    if info.file_size>240:raise Refused("zip_link")
                    with archive.open(info) as f:target=f.read(241).decode("utf-8")
                    installer.consume(name,"symlink",0,target=target)
                else:raise Refused("zip_member_kind")
        held.check();return installer.seal()
    finally:defer_retirement(hold)


@archive_scope
def wheel_install(held, installer, expected):
    installer.prepare_directories()
    count,directory_size=zip_preflight(held)
    hold=held.meter.reserve("zip-metadata-before-allocation",allocation=directory_size*16+count*16384+65536*16,reads=directory_size)
    try:
        with zipfile.ZipFile(FDView(held)) as archive:
            infos=archive.infolist()
            if len(infos)!=count:raise Refused("zip_directory")
            by_name={}
            for info in infos:
                name=relative(info.filename)
                if name in by_name or info.flag_bits&1 or info.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):
                    raise Refused("wheel_member")
                integer(info.file_size,DOCUMENT_MAX)
                if stat.S_IFMT(info.external_attr>>16) in (stat.S_IFLNK,stat.S_IFCHR,stat.S_IFBLK):
                    raise Refused("wheel_member_kind")
                by_name[name]=info
            prefixes={n.rsplit("/",1)[0] for n in by_name if n.endswith(".dist-info/WHEEL")}
            if len(prefixes)!=1:raise Refused("wheel_metadata")
            prefix=next(iter(prefixes))
            def small(name):
                info=by_name.get(name)
                if info is None or info.file_size>65536:raise Refused("wheel_metadata")
                with archive.open(info) as f:return f.read(65537)
            metadata=small(prefix+"/METADATA").decode("utf-8")
            wheel=small(prefix+"/WHEEL").decode("ascii")
            fields={}
            for line in metadata.splitlines():
                if not line:break
                if ":" not in line:raise Refused("wheel_metadata")
                k,v=line.split(":",1)
                fields.setdefault(k,[]).append(v.strip())
            required_python=expected["requires_python"]
            metadata_python=fields.get("Requires-Python")
            # Absent wheel METADATA is distinct from an explicit empty literal
            # in retained PyPI JSON; original literal null/empty remains below.
            python_ok=metadata_python is None if required_python is None else metadata_python==[required_python]
            if fields.get("Name")!=[expected["name"]] or fields.get("Version")!=[expected["version"]] or not python_ok:
                raise Refused("wheel_identity")
            tags=[line[5:].strip() for line in wheel.splitlines() if line.startswith("Tag: ")]
            if sorted(tags)!=sorted(expected["tags"]):raise Refused("python_abi")
            record=small(prefix+"/RECORD").decode("utf-8")
            signed={}
            for row in csv.reader(io.StringIO(record,newline="")):
                if len(row)!=3 or row[0] in signed:raise Refused("wheel_record")
                signed[relative(row[0])]=row[1:]
            if set(signed)!={n for n,i in by_name.items() if not i.is_dir()}:
                raise Refused("wheel_record_inventory")
            for name,info in by_name.items():
                if info.is_dir():installer.consume(name,"directory",0);continue
                encoded,size=signed[name]
                if name==prefix+"/RECORD":
                    if encoded or size:raise Refused("wheel_record")
                else:
                    if not encoded.startswith("sha256=") or not size.isdigit() or int(size)!=info.file_size:
                        raise Refused("wheel_record")
                    expected_hash=base64.urlsafe_b64decode(encoded[7:]+"="*((4-len(encoded[7:])%4)%4)).hex()
                    if expected_hash!=installer.selected[name]["sha256"]:raise Refused("wheel_record_hash")
                # Every wheel member is installed; .data mapping is independently
                # selected in destination and checked by the complete member plan.
                with archive.open(info) as f:
                    installer.consume(name,"regular",info.file_size,f,executable=bool(info.external_attr>>16&0o111))
        held.check();return installer.seal()
    finally:defer_retirement(hold)
