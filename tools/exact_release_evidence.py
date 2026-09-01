#!/usr/bin/env python3
"""Produce exact-release journey evidence from one closed pytest inventory."""

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import importlib
import importlib.machinery
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import sysconfig
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

_INITIAL_PRODUCER_SYS_PATH = tuple(sys.path)
_INITIAL_PRODUCER_SITE_LOADED = "site" in sys.modules
_INITIAL_PRODUCER_EXECUTABLE = sys.executable
_NATIVE_VALIDATION_TOOLING_SITE: Path | None = None
_NATIVE_VALIDATION_INTERPRETER_FD = -1
_NATIVE_VALIDATION_INTERPRETER_ARGV0 = ""

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RECEIPT_SCHEMA = "friday.golden-journey-sanitized-receipt.v2"
CLEAN_ARTIFACT_RECEIPT_SCHEMA = "friday.golden-journey-sanitized-receipt.v4"
PRODUCTION_OBSERVATION_RECEIPT_SCHEMA = "friday.golden-journey-production-read-only-receipt.v1"
PRODUCTION_OBSERVATION_MANIFEST_SCHEMA = "friday.golden-journey-production-read-only-evidence.v1"
RELEASE_CAPTAIN_PRODUCTION_OBSERVATION_SCHEMA = "friday.production-read-only-release-captain-artifact.v1"
PRODUCTION_READ_ONLY_OBSERVATION_SCHEMA = "friday.production-read-only-observation.v1"
PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256 = (
    "726ded0b802ee1c6bf82663fd0918efb7f3d509f382c0d2aaa3540d4a1790561"
)
MANIFEST_SCHEMA = "friday.golden-journey-evidence.v1"
VALIDATION_ATTESTATION_SCHEMA = "friday.exact-release-receipt-validation.v1"
PRODUCER_PATH = "tools/exact_release_evidence.py"
PYTEST_TIMEOUT_SECONDS = 900
VALIDATION_TIMEOUT_SECONDS = PYTEST_TIMEOUT_SECONDS + 60
VALIDATION_TERMINATION_GRACE_SECONDS = 5.0
_EVIDENCE_ROOT = PurePosixPath("evidence/golden_journeys")
_CLEAN_ARTIFACT_CLASS = "clean artifact path"
_RESTART_RECOVERY_CLASS = "restart and recovery evidence"
_PRODUCTION_OBSERVATION_CLASS = "production read-only observation"
_PRODUCTION_OBSERVATION_JOURNEY = "durable_scheduled_work"
_PRODUCTION_OBSERVATION_MAX_BYTES = 32_768
_RELEASE_CAPTAIN_ARTIFACT_MAX_BYTES = 65_536
_MAX_PRODUCTION_AGGREGATE = (1 << 63) - 1
_CANDIDATE_BOUND_FAULT_JOURNEYS = frozenset(
    {
        "durable_scheduled_work",
        "honest_degradation",
    }
)
_SUBPROCESS_POLICY = "cpython_audit_deny"
_TEST_TOOLING_POLICY = "explicit_single_site_v1"
_INTERPRETER_REF = "venv/bin/python"
_VALIDATION_BOOTSTRAP_MODULES = (
    ("encodings", "encodings/__init__.py"),
    ("encodings.aliases", "encodings/aliases.py"),
    ("encodings.utf_8", "encodings/utf_8.py"),
    ("linecache", "linecache.py"),
    ("_sha2", None),
    ("select", None),
)
_TEST_TOOLING_MODULES = (
    "pytest",
    "_pytest",
    "pluggy",
    "pytest_asyncio",
    "xdist",
    "execnet",
    "anyio",
)
_TEST_TOOLING_SNAPSHOT_NAMES = (
    "_pytest",
    "anyio",
    "execnet",
    "iniconfig",
    "packaging",
    "pluggy",
    "py",
    "pygments",
    "pytest",
    "pytest_asyncio",
    "xdist",
)
_TEST_TOOLING_SNAPSHOT_DISTRIBUTIONS = (
    "anyio",
    "execnet",
    "iniconfig",
    "packaging",
    "pluggy",
    "pygments",
    "pytest",
    "pytest_asyncio",
    "pytest_xdist",
)
_TEST_TOOLING_MAX_FILES = 10_000
_TEST_TOOLING_MAX_BYTES = 128 << 20
_TRUSTED_GIT_PATH = Path("/usr/bin/git")
_TRUSTED_GIT_ENVIRONMENT = {
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": "/",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin",
    "TZ": "UTC",
}
_FORBIDDEN_PRODUCER_STARTUP_ENVIRONMENT = frozenset(
    {
        "DYLD_FORCE_FLAT_NAMESPACE",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "GLIBC_TUNABLES",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PYTHONBREAKPOINT",
        "PYTHONCASEOK",
        "PYTHONDEBUG",
        "PYTHONFAULTHANDLER",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONMALLOC",
        "PYTHONPATH",
        "PYTHONPROFILEIMPORTTIME",
        "PYTHONSTARTUP",
        "PYTHONTRACEMALLOC",
        "PYTHONWARNINGS",
    }
)
_SEALED_CHILD_INHERITED_ENVIRONMENT = (
    "FRIDAY_HOME",
    "JERICHO_HOME",
    "FRIDAY_ENV_FILE",
    "JERICHO_ENV_FILE",
    "FRIDAY_DATABASE_PATH",
    "JERICHO_DATABASE_PATH",
    "FRIDAY_DATABASE_MUST_EXIST",
    "JERICHO_DATABASE_MUST_EXIST",
    "FRIDAY_LLM_ENABLED",
    "FRIDAY_EMBEDDINGS_ENABLED",
    "FRIDAY_WORKERS_ENABLED",
    "FRIDAY_CODE_EXECUTION_ENABLED",
    "FRIDAY_TEST_BACKUPS_DIR",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONHASHSEED",
)
_SEALED_CHILD_ENVIRONMENT_KEYS = frozenset(
    {
        *_SEALED_CHILD_INHERITED_ENVIRONMENT,
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTHONPYCACHEPREFIX",
        "TMPDIR",
        "TZ",
        "VIRTUAL_ENV",
    }
)
_PYTEST_BOOTSTRAP = (
    "import pathlib,sys; "
    "root=str(pathlib.Path(sys.argv.pop(1)).resolve(strict=True)); "
    "site=sys.argv.pop(1); "
    "site=str(pathlib.Path(site).resolve(strict=True)) if site!='-' else site; "
    "sys.path[:0]=[site,root] if site!='-' else [root]; "
    "import pytest; raise SystemExit(pytest.main(sys.argv[1:]))"
)
_ISOLATED_VALIDATION_BOOTSTRAP = r"""
import _sha2,_signal,os,select,stat,sys
interpreter_fd=int(sys.argv[1]); producer_fd=int(sys.argv[2])
producer_path=sys.argv[3]; producer_sha256=sys.argv[4]
interpreter_argv0=sys.argv[5]
expected_version=tuple(int(part) for part in sys.argv[6].split("."))
stdlib=os.path.realpath(sys.argv[7],strict=True)
tooling_site=os.path.realpath(sys.argv[8],strict=True)
bootstrap_cache=os.path.realpath(sys.argv[9],strict=False)
binding_names=("encodings","encodings.aliases","encodings.utf_8","linecache","_sha2","select")
binding_values=sys.argv[10:34]; parent_pid=int(sys.argv[34])
fields=("st_dev","st_ino","st_mode","st_nlink","st_uid","st_gid","st_size","st_mtime_ns","st_ctime_ns")
def same(left,right):
    return all(getattr(left,name)==getattr(right,name) for name in fields)
def status_tuple(value):
    return tuple(getattr(value,name) for name in fields)
runtime_anchor=os.path.realpath(os.path.commonpath((os.path.dirname(interpreter_argv0),stdlib)),strict=True)
def controlled_directory(path):
    current=path
    while True:
        value=os.lstat(current)
        protected=current==runtime_anchor or os.path.commonpath((runtime_anchor,current))==runtime_anchor
        if os.path.realpath(current,strict=True)!=current or not stat.S_ISDIR(value.st_mode) or (protected and (value.st_uid not in (0,os.geteuid()) or value.st_mode&0o022)):
            raise RuntimeError("validation_controller_invalid")
        if current==os.sep:
            return
        current=os.path.dirname(current)
def controlled_file(path,descriptor):
    named=os.lstat(path); opened=os.fstat(descriptor)
    if os.path.realpath(path,strict=True)!=path or not same(named,opened) or not stat.S_ISREG(named.st_mode) or named.st_nlink!=1 or named.st_uid not in (0,os.geteuid()) or named.st_mode&0o022:
        raise RuntimeError("validation_controller_invalid")
if len(binding_values)!=24:
    raise RuntimeError("validation_controller_invalid")
bindings=[]
for index,expected_name in enumerate(binding_names):
    name,expected_origin,raw_fd,raw_status=binding_values[index*4:index*4+4]
    descriptor=int(raw_fd); module=sys.modules.get(name)
    origin=getattr(getattr(module,"__spec__",None),"origin",None)
    if name!=expected_name or origin!=expected_origin:
        raise RuntimeError("validation_controller_invalid")
    if origin in ("built-in","frozen"):
        if descriptor!=-1 or raw_status!="-":
            raise RuntimeError("validation_controller_invalid")
    else:
        expected_status=tuple(int(value) for value in raw_status.split(","))
        cached=getattr(module,"__cached__",None)
        if len(expected_status)!=len(fields) or descriptor<0 or descriptor in (interpreter_fd,producer_fd) or any(descriptor==item[2] for item in bindings) or getattr(module,"__file__",None)!=origin:
            raise RuntimeError("validation_controller_invalid")
        controlled_directory(os.path.dirname(origin))
        controlled_file(origin,descriptor)
        if status_tuple(os.fstat(descriptor))!=expected_status or status_tuple(os.lstat(origin))!=expected_status:
            raise RuntimeError("validation_controller_invalid")
        if origin.endswith(".py") and (not isinstance(cached,str) or os.path.commonpath((bootstrap_cache,cached))!=bootstrap_cache or os.path.lexists(cached)):
            raise RuntimeError("validation_controller_invalid")
    bindings.append((name,origin,descriptor))
loaded_file_modules={name for name,module in sys.modules.items() if getattr(getattr(module,"__spec__",None),"origin",None) not in (None,"built-in","frozen")}
if loaded_file_modules!={name for name,origin,_descriptor in bindings if origin not in ("built-in","frozen")}:
    raise RuntimeError("validation_controller_invalid")
expected_path=(os.path.realpath(os.path.join(os.path.dirname(stdlib),f"python{sys.version_info.major}{sys.version_info.minor}.zip"),strict=False),stdlib,os.path.realpath(os.path.join(stdlib,"lib-dynload"),strict=True))
if (
    len(expected_version)!=3 or tuple(sys.version_info[:3])!=expected_version
    or sys.executable!=interpreter_argv0 or os.path.realpath(sys.executable,strict=True)!=interpreter_argv0
    or sys.flags.isolated!=1 or sys.flags.no_site!=1 or sys.flags.no_user_site!=1
    or sys.flags.ignore_environment!=1 or sys.flags.dont_write_bytecode!=1 or not sys.flags.safe_path
    or "site" in sys.modules or tuple(os.path.realpath(value,strict=False) for value in sys.path)!=expected_path
    or set(os.environ)!={"HOME","LANG","LC_ALL","PATH","TMPDIR","TZ"}
    or sys.pycache_prefix!=bootstrap_cache or sys._xoptions!={"pycache_prefix":bootstrap_cache} or os.path.lexists(bootstrap_cache)
    or _signal.getsignal(_signal.SIGCHLD)!=_signal.SIG_DFL
    or os.path.realpath(tooling_site,strict=True)!=tooling_site or not stat.S_ISDIR(os.lstat(tooling_site).st_mode)
):
    raise RuntimeError("validation_controller_invalid")
controlled_directory(os.path.dirname(interpreter_argv0)); controlled_directory(stdlib); controlled_directory(expected_path[2])
try:
    python_zip_status=os.lstat(expected_path[0])
except FileNotFoundError:
    if os.path.lexists(expected_path[0]):
        raise RuntimeError("validation_controller_invalid")
else:
    if os.path.realpath(expected_path[0],strict=True)!=expected_path[0] or not stat.S_ISREG(python_zip_status.st_mode) or python_zip_status.st_uid not in (0,os.geteuid()) or python_zip_status.st_mode&0o022:
        raise RuntimeError("validation_controller_invalid")
for _name,_origin,descriptor in bindings:
    if descriptor>=0:
        os.close(descriptor)
if os.getpgrp()!=os.getpid() or os.getppid()!=parent_pid or not hasattr(os,"pidfd_open"):
    raise RuntimeError("validation_controller_invalid")
parent_fd=os.pidfd_open(parent_pid,0)
if os.getppid()!=parent_pid:
    raise RuntimeError("validation_controller_invalid")
controller_fd=os.pidfd_open(os.getpid(),0)
monitor=os.fork()
if monitor==0:
    for descriptor in (0,1,2,interpreter_fd,producer_fd):
        try: os.close(descriptor)
        except OSError: pass
    select.select((parent_fd,controller_fd),(),())
    os.killpg(os.getpgrp(),_signal.SIGKILL)
    os._exit(1)
os.close(parent_fd); os.close(controller_fd)
running=os.stat("/proc/self/exe"); pinned=os.fstat(interpreter_fd)
if not same(running,pinned) or not stat.S_ISREG(pinned.st_mode) or pinned.st_nlink!=1 or pinned.st_uid not in (0,os.geteuid()) or pinned.st_mode&0o022 or not pinned.st_mode&0o111:
    raise RuntimeError("validation_controller_invalid")
os.lseek(producer_fd,0,os.SEEK_SET); before=os.fstat(producer_fd); chunks=[]; remaining=1048577
while remaining:
    chunk=os.read(producer_fd,min(1048576,remaining))
    if not chunk: break
    chunks.append(chunk); remaining-=len(chunk)
source=b"".join(chunks); after=os.fstat(producer_fd); named=os.lstat(producer_path)
if len(source)>1048576 or not same(before,after) or not same(after,named) or _sha2.sha256(source).hexdigest()!=producer_sha256:
    raise RuntimeError("validation_controller_invalid")
module_name="exact_release_evidence_validation"
if module_name in sys.modules:
    raise RuntimeError("validation_controller_invalid")
module=type(sys)(module_name); module.__file__=producer_path; module.__package__=""
sys.modules[module_name]=module
try:
    exec(compile(source,producer_path,"exec",dont_inherit=True),module.__dict__)
    module._NATIVE_VALIDATION_TOOLING_SITE=module.Path(tooling_site)
    module._NATIVE_VALIDATION_INTERPRETER_FD=interpreter_fd
    module._NATIVE_VALIDATION_INTERPRETER_ARGV0=interpreter_argv0
    result=module.main(sys.argv[35:])
finally:
    sys.modules.pop(module_name,None)
raise SystemExit(result)
"""
_INSTALLED_PYTEST_BOOTSTRAP = r"""
import hashlib,importlib.machinery,importlib.util,json,os,pathlib,posix,stat,sys,sysconfig,types
source_root=pathlib.Path(sys.argv.pop(1)).resolve(strict=True)
release_root=pathlib.Path(sys.argv.pop(1)).resolve(strict=True)
site=pathlib.Path(sys.argv.pop(1)).resolve(strict=True)
site_ref=sys.argv.pop(1)
interpreter_ref=sys.argv.pop(1)
tooling_site=pathlib.Path(sys.argv.pop(1)).resolve(strict=True)
quality_gate_path=pathlib.Path(sys.argv.pop(1)).resolve(strict=True)
quality_gate_sha256=sys.argv.pop(1)
report_path=pathlib.Path(sys.argv.pop(1))
source_commit=sys.argv.pop(1)
wheel_sha256=sys.argv.pop(1)
interpreter=release_root/interpreter_ref
venv=release_root/"venv"
report_parent=report_path.parent.resolve(strict=True)
pycache_prefix=(report_parent/"python-cache").resolve(strict=False)
stdlib=pathlib.Path(sysconfig.get_path("stdlib")).resolve(strict=True)
expected_initial_path=(
    (stdlib.parent/f"python{sys.version_info.major}{sys.version_info.minor}.zip").resolve(strict=False),
    stdlib,
    (stdlib/"lib-dynload").resolve(strict=True),
)
initial_path=tuple(pathlib.Path(value).resolve(strict=False) for value in sys.path)
expected_environment={
    "FRIDAY_HOME","JERICHO_HOME","FRIDAY_ENV_FILE","JERICHO_ENV_FILE",
    "FRIDAY_DATABASE_PATH","JERICHO_DATABASE_PATH",
    "FRIDAY_DATABASE_MUST_EXIST","JERICHO_DATABASE_MUST_EXIST",
    "FRIDAY_LLM_ENABLED","FRIDAY_EMBEDDINGS_ENABLED","FRIDAY_WORKERS_ENABLED",
    "FRIDAY_CODE_EXECUTION_ENABLED","FRIDAY_TEST_BACKUPS_DIR",
    "PYTHONDONTWRITEBYTECODE","PYTHONHASHSEED","HOME","LANG","LC_ALL","PATH",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD","PYTHONPYCACHEPREFIX","TMPDIR","TZ","VIRTUAL_ENV",
}
if (
    site != release_root/site_ref
    or interpreter_ref != "venv/bin/python"
    or pathlib.Path(sys.executable).resolve(strict=True) != interpreter
    or pathlib.Path(sys.prefix).resolve(strict=True) != venv
    or pathlib.Path(os.environ.get("VIRTUAL_ENV","")).resolve(strict=False) != venv
    or "PYTHONHOME" in os.environ
    or "PYTHONPATH" in os.environ
    or site.parent.name != f"python{sys.version_info.major}.{sys.version_info.minor}"
    or sys.flags.isolated != 1
    or sys.flags.no_site != 1
    or sys.flags.no_user_site != 1
    or sys.flags.ignore_environment != 1
    or sys.flags.dont_write_bytecode != 1
    or not sys.flags.safe_path
    or "site" in sys.modules
    or initial_path != expected_initial_path
    or set(os.environ) != expected_environment
    or os.environ["HOME"] != os.environ["FRIDAY_HOME"]
    or os.environ["HOME"] != os.environ["JERICHO_HOME"]
    or os.environ["PATH"] != os.defpath
    or os.environ["LANG"] != "C.UTF-8"
    or os.environ["LC_ALL"] != "C.UTF-8"
    or os.environ["TZ"] != "UTC"
    or report_path.parent != report_parent
    or pathlib.Path(os.environ["PYTHONPYCACHEPREFIX"]).resolve(strict=False) != pycache_prefix
    or pathlib.Path(os.environ["PYTHONPYCACHEPREFIX"]) != pycache_prefix
    or pathlib.Path(sys.pycache_prefix or "").resolve(strict=False) != pycache_prefix
    or sys._xoptions != {"pycache_prefix":str(pycache_prefix)}
    or os.path.lexists(pycache_prefix)
):
    raise RuntimeError("installed_site_binding_invalid")
if (
    tooling_site == source_root
    or tooling_site == release_root
    or tooling_site.is_relative_to(source_root)
    or tooling_site.is_relative_to(release_root)
    or source_root.is_relative_to(tooling_site)
    or release_root.is_relative_to(tooling_site)
):
    raise RuntimeError("test_tooling_binding_invalid")
sys.path.insert(0,str(tooling_site))
blocked={"os.exec","os.fork","os.forkpty","os.posix_spawn","os.posix_spawnp","os.system","pty.spawn","subprocess.Popen"}
violations=set()
source_product_paths=tuple(source_root/name for name in ("friday","friday_host_agent","friday_package_broker"))
source_product_roots=tuple(path.resolve(strict=False) for path in source_product_paths)
def reject_source_alias():
    violations.add("source_first_party_alias_unattested")
    raise RuntimeError("source_first_party_alias_unattested")
def require_source_product_link_isolation():
    for lexical,root in zip(source_product_paths,source_product_roots,strict=True):
        try:
            root_status=lexical.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            violations.add("source_first_party_alias_unattested")
            raise RuntimeError("source_first_party_alias_unattested") from exc
        if (
            lexical != root
            or not stat.S_ISDIR(root_status.st_mode)
            or root.resolve(strict=True) != root
        ):
            reject_source_alias()
        try:
            entries=root.rglob("*")
            for entry in entries:
                status=entry.lstat()
                if stat.S_ISDIR(status.st_mode):
                    if entry.resolve(strict=True) != entry:
                        reject_source_alias()
                elif not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    reject_source_alias()
        except (OSError,RuntimeError) as exc:
            if isinstance(exc,RuntimeError) and str(exc) == "source_first_party_alias_unattested":
                raise
            violations.add("source_first_party_alias_unattested")
            raise RuntimeError("source_first_party_alias_unattested") from exc
def absolute_open_path(raw_path,dir_fd=None):
    candidate=pathlib.Path(os.fsdecode(raw_path))
    if not candidate.is_absolute():
        if dir_fd is None or dir_fd == -1:
            base=pathlib.Path.cwd()
        else:
            try:
                base=pathlib.Path("/proc/self/fd")/str(dir_fd)
                base=base.resolve(strict=True)
            except (OSError,RuntimeError,ValueError) as exc:
                violations.add("dir_fd_open_unattested")
                raise RuntimeError("dir_fd_open_unattested") from exc
        candidate=base/candidate
    return candidate
def resolved_open_path(raw_path,dir_fd=None):
    return absolute_open_path(raw_path,dir_fd).resolve(strict=False)
def resolves_to_source_product(raw_path,dir_fd=None):
    resolved=resolved_open_path(raw_path,dir_fd)
    return any(resolved == root or root in resolved.parents for root in source_product_roots)
def targets_bytecode(raw_path,dir_fd=None):
    resolved=resolved_open_path(raw_path,dir_fd)
    lowered=absolute_open_path(raw_path,dir_fd).name.lower()
    return (
        resolved == pycache_prefix
        or pycache_prefix in resolved.parents
        or lowered.endswith((".pyc",".pyo"))
    )
def bytecode_open_is_denied(raw_path,mode=None,flags=0,dir_fd=None):
    if not targets_bytecode(raw_path,dir_fd):
        return False
    writing=(
        isinstance(mode,str) and any(character in mode for character in "wax+")
    ) or (
        isinstance(flags,int) and (
            flags & os.O_ACCMODE != os.O_RDONLY
            or bool(flags & (os.O_CREAT|os.O_TRUNC|os.O_APPEND|os.O_EXCL))
        )
    )
    if writing:
        return True
    try:
        absolute_open_path(raw_path,dir_fd).lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        violations.add("bytecode_execution_unattested")
        raise RuntimeError("bytecode_execution_unattested") from exc
    return True
def deny_child(event,args):
    if event in blocked:
        violations.add("child_execution_unattested")
        raise RuntimeError("child_execution_unattested")
    if event in {"os.link","os.rename"}:
        for path_index,fd_index in ((0,2),(1,3)):
            if (
                len(args) > path_index
                and isinstance(args[path_index],(str,bytes,os.PathLike))
                and resolves_to_source_product(
                    args[path_index],
                    (args[fd_index] if len(args) > fd_index else None),
                )
            ):
                reject_source_alias()
    if event == "open" and args and isinstance(args[0],(str,bytes,os.PathLike)):
        if bytecode_open_is_denied(
            args[0],
            args[1] if len(args) > 1 else None,
            args[2] if len(args) > 2 else 0,
        ):
            violations.add("bytecode_execution_unattested")
            raise RuntimeError("bytecode_execution_unattested")
        if resolves_to_source_product(args[0]):
            violations.add("source_first_party_read_unattested")
            raise RuntimeError("source_first_party_read_unattested")
sys.addaudithook(deny_child)
require_source_product_link_isolation()
raw_os_open=os.open
def guarded_os_open(path,flags,mode=0o777,*,dir_fd=None):
    if isinstance(path,(str,bytes,os.PathLike)):
        if bytecode_open_is_denied(path,flags=flags,dir_fd=dir_fd):
            violations.add("bytecode_execution_unattested")
            raise RuntimeError("bytecode_execution_unattested")
        if dir_fd is not None and resolves_to_source_product(path,dir_fd):
            violations.add("source_first_party_read_unattested")
            raise RuntimeError("source_first_party_read_unattested")
    return raw_os_open(path,flags,mode,dir_fd=dir_fd)
os.open=guarded_os_open
posix.open=guarded_os_open
origins=set()
allowed_tooling={"_pytest","anyio","execnet","iniconfig","packaging","pluggy","py","pygments","pytest","pytest_asyncio","xdist"}
pinned_source_file_loader=importlib.machinery.SourceFileLoader
pinned_spec_from_file_location=importlib.util.spec_from_file_location
pinned_code_owned_loader_methods=()
def callable_authority(value):
    if isinstance(value,types.FunctionType):
        return (
            "function",id(value.__code__),
            tuple(id(item) for item in (value.__defaults__ or ())),
            tuple(sorted((name,id(item)) for name,item in (value.__kwdefaults__ or {}).items())),
        )
    if isinstance(value,(staticmethod,classmethod)):
        return (type(value).__name__,callable_authority(value.__func__))
    if isinstance(value,property):
        return (
            "property",
            callable_authority(value.fget),
            callable_authority(value.fset),
            callable_authority(value.fdel),
        )
    if isinstance(value,type):
        return (
            "class",
            tuple(
                (name,id(member),callable_authority(member))
                for name,member in sorted(vars(value).items())
                if isinstance(member,(types.FunctionType,staticmethod,classmethod,property))
            ),
        )
    return (type(value).__name__,)
pinned_spec_from_file_location_authority=callable_authority(pinned_spec_from_file_location)
pinned_source_loader_methods=tuple(
    (name,value,callable_authority(value))
    for name in (
        "__init__","create_module","exec_module","get_code","get_data","get_filename",
        "get_resource_reader","get_source","is_package","path_mtime","path_stats","set_data",
    )
    if (value:=getattr(pinned_source_file_loader,name,None)) is not None
)
def reject_import_loader_authority():
    violations.add("import_loader_authority_poisoned")
    raise RuntimeError("import_loader_authority_poisoned")
def require_import_loader_authority():
    if (
        importlib.machinery.SourceFileLoader is not pinned_source_file_loader
        or importlib.util.spec_from_file_location is not pinned_spec_from_file_location
        or callable_authority(pinned_spec_from_file_location)
            != pinned_spec_from_file_location_authority
    ):
        reject_import_loader_authority()
    for name,value,authority in pinned_source_loader_methods:
        current=getattr(pinned_source_file_loader,name,None)
        if current is not value or callable_authority(current) != authority:
            reject_import_loader_authority()
    for owner,name,value,authority in pinned_code_owned_loader_methods:
        current=getattr(owner,name,None)
        if current is not value or callable_authority(current) != authority:
            reject_import_loader_authority()
def first_party(name):
    return name == "friday" or name.startswith("friday.") or name.startswith("friday_")
def confined(path):
    resolved=pathlib.Path(path).resolve(strict=True)
    try:
        relative=resolved.relative_to(site).as_posix()
    except ValueError as exc:
        violations.add("first_party_origin_escaped_release")
        raise RuntimeError("first_party_origin_escaped_release") from exc
    origins.add(relative)
    return resolved
first_party_resolutions={}
first_party_attestations={}
def reject_first_party_attestation():
    violations.add("first_party_module_unattested")
    raise RuntimeError("first_party_module_unattested")
def reject_first_party_search_path():
    violations.add("first_party_origin_escaped_release")
    raise RuntimeError("first_party_origin_escaped_release")
def first_party_source(fullname):
    parts=fullname.split(".")
    if any(not part or not part.isidentifier() for part in parts):
        violations.add("first_party_origin_missing")
        raise RuntimeError("first_party_origin_missing")
    base=site.joinpath(*parts)
    package_origin=base/"__init__.py"
    module_origin=base.with_suffix(".py")
    candidates=[]
    for candidate,is_package in ((package_origin,True),(module_origin,False)):
        try:
            status=candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            violations.add("first_party_origin_missing")
            raise RuntimeError("first_party_origin_missing") from exc
        try:
            if (
                candidate.resolve(strict=True) != candidate
                or not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
            ):
                raise ValueError
        except (OSError,ValueError) as exc:
            violations.add("first_party_origin_escaped_release")
            raise RuntimeError("first_party_origin_escaped_release") from exc
        candidates.append((candidate,is_package))
    if len(candidates) != 1:
        violations.add("first_party_origin_missing")
        raise RuntimeError("first_party_origin_missing")
    return candidates[0]
def require_first_party_resolution(loader):
    binding=first_party_resolutions.get(loader.fullname)
    if (
        binding is None
        or binding[0] is not loader.spec
        or binding[1] is not loader
        or binding[2] is not loader.delegate
        or binding[3] != loader.origin
        or binding[4] != loader.locations
        or loader.spec.name != loader.fullname
        or loader.spec.loader is not loader
    ):
        reject_first_party_attestation()
def exact_module_binding(fullname,module,binding):
    spec,loader,delegate,origin,locations=binding
    try:
        raw_origin=getattr(module,"__file__",None)
        raw_spec_origin=getattr(spec,"origin",None)
        raw_spec_locations=getattr(spec,"submodule_search_locations",None)
        raw_module_locations=getattr(module,"__path__",None)
        spec_locations=(
            None if raw_spec_locations is None else tuple(raw_spec_locations)
        )
        module_locations=(
            None if raw_module_locations is None else tuple(raw_module_locations)
        )
        resolved_origin=(
            None if raw_origin is None else pathlib.Path(raw_origin).resolve(strict=True)
        )
        resolved_spec_origin=(
            None if raw_spec_origin in {None,"built-in","frozen"} else
            pathlib.Path(raw_spec_origin).resolve(strict=True)
        )
        resolved_spec_locations=(
            None if spec_locations is None else
            tuple(pathlib.Path(location).resolve(strict=True) for location in spec_locations)
        )
        resolved_module_locations=(
            None if module_locations is None else
            tuple(pathlib.Path(location).resolve(strict=True) for location in module_locations)
        )
    except (OSError,TypeError,ValueError):
        return False
    expected_location_strings=(
        None if locations is None else tuple(str(location) for location in locations)
    )
    expected_package=fullname if locations is not None else fullname.rpartition(".")[0]
    return (
        sys.modules.get(fullname) is module
        and getattr(module,"__name__",None) == fullname
        and getattr(module,"__package__",None) == expected_package
        and getattr(module,"__spec__",None) is spec
        and getattr(module,"__loader__",None) is loader
        and spec.name == fullname
        and spec.parent == expected_package
        and spec.loader is loader
        and spec.has_location is True
        and loader.spec is spec
        and loader.delegate is delegate
        and loader.origin == origin
        and loader.locations == locations
        and type(delegate) is pinned_source_file_loader
        and delegate.name == fullname
        and delegate.path == str(origin)
        and vars(delegate) == {"name":fullname,"path":str(origin)}
        and raw_origin == str(origin)
        and raw_spec_origin == str(origin)
        and spec_locations == expected_location_strings
        and module_locations == expected_location_strings
        and resolved_origin == origin
        and resolved_spec_origin == origin
        and resolved_spec_locations == locations
        and resolved_module_locations == locations
    )
def require_first_party_module_binding(fullname,module,binding):
    if type(binding[1]) is not FirstPartyLoader or not exact_module_binding(
        fullname,module,binding
    ):
        reject_first_party_attestation()
class FirstPartyLoader:
    __slots__=("fullname","spec","delegate","origin","locations")
    def __init__(self,fullname,spec,delegate,origin,locations):
        self.fullname=fullname
        self.spec=spec
        self.delegate=delegate
        self.origin=origin
        self.locations=locations
    def __getattr__(self,name):
        return getattr(self.delegate,name)
    def create_module(self,spec):
        require_import_loader_authority()
        require_first_party_resolution(self)
        if spec is not self.spec:
            reject_first_party_attestation()
        create=getattr(self.delegate,"create_module",None)
        return None if create is None else create(spec)
    def exec_module(self,module):
        require_import_loader_authority()
        require_first_party_resolution(self)
        require_first_party_module_binding(
            self.fullname,module,
            (self.spec,self,self.delegate,self.origin,self.locations),
        )
        self.delegate.exec_module(module)
        binding=(self.spec,self,self.delegate,self.origin,self.locations)
        require_first_party_module_binding(self.fullname,module,binding)
        first_party_attestations[self.fullname]=(module,*binding)
class FirstPartyGuard:
    def find_spec(self,fullname,path=None,target=None):
        if not first_party(fullname):
            return None
        require_import_loader_authority()
        parent=fullname.rpartition(".")[0]
        if not parent:
            if path is not None:
                reject_first_party_search_path()
        else:
            parent_binding=first_party_resolutions.get(parent)
            expected=None if parent_binding is None else parent_binding[4]
            try:
                raw_path=None if path is None else tuple(path)
                resolved_path=(
                    None if raw_path is None else
                    tuple(pathlib.Path(entry).resolve(strict=True) for entry in raw_path)
                )
            except (OSError,TypeError,ValueError):
                reject_first_party_search_path()
            if (
                expected is None
                or raw_path != tuple(str(location) for location in expected)
                or resolved_path != expected
            ):
                reject_first_party_search_path()
            require_first_party_module_binding(parent,sys.modules.get(parent),parent_binding)
        origin,is_package=first_party_source(fullname)
        origin=confined(origin)
        package_location=origin.parent if is_package else None
        locations=None if package_location is None else (confined(package_location),)
        delegate=pinned_source_file_loader(fullname,str(origin))
        spec=pinned_spec_from_file_location(
            fullname,str(origin),loader=delegate,
            submodule_search_locations=(None if locations is None else [str(locations[0])]),
        )
        if spec is None or spec.loader is not delegate:
            violations.add("first_party_origin_missing")
            raise RuntimeError("first_party_origin_missing")
        loader=FirstPartyLoader(fullname,spec,delegate,origin,locations)
        spec.loader=loader
        first_party_resolutions[fullname]=(spec,loader,delegate,origin,locations)
        return spec
tooling_resolutions={}
tooling_attestations={}
tooling_alias_attestations={}
tooling_callable_attestations={}
tooling_namespace_keys={}
def reject_tooling_attestation():
    violations.add("test_tooling_module_unattested")
    raise RuntimeError("test_tooling_module_unattested")
def reject_tooling_origin(code):
    violations.add(code)
    raise RuntimeError(code)
def tooling_source(fullname):
    parts=fullname.split(".")
    if any(not part or not part.isidentifier() for part in parts):
        reject_tooling_origin("test_tooling_origin_missing")
    base=tooling_site.joinpath(*parts)
    candidates=[]
    for candidate,is_package in ((base/"__init__.py",True),(base.with_suffix(".py"),False)):
        try:
            status=candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            violations.add("test_tooling_origin_missing")
            raise RuntimeError("test_tooling_origin_missing") from exc
        try:
            if (
                candidate.resolve(strict=True) != candidate
                or not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
            ):
                raise ValueError
        except (OSError,ValueError) as exc:
            violations.add("test_tooling_origin_invalid")
            raise RuntimeError("test_tooling_origin_invalid") from exc
        candidates.append((candidate,is_package))
    if len(candidates) != 1:
        reject_tooling_origin("test_tooling_origin_missing")
    return candidates[0]
def require_tooling_resolution(loader):
    binding=tooling_resolutions.get(loader.fullname)
    if (
        binding is None
        or binding[0] is not loader.spec
        or binding[1] is not loader
        or binding[2] is not loader.delegate
        or binding[3] != loader.origin
        or binding[4] != loader.locations
        or loader.spec.name != loader.fullname
        or loader.spec.loader is not loader
    ):
        reject_tooling_attestation()
def require_tooling_module_binding(fullname,module,binding):
    if type(binding[1]) is not ToolingLoader or not exact_module_binding(
        fullname,module,binding
    ):
        reject_tooling_attestation()
class ToolingLoader:
    __slots__=("fullname","spec","delegate","origin","locations")
    def __init__(self,fullname,spec,delegate,origin,locations):
        self.fullname=fullname
        self.spec=spec
        self.delegate=delegate
        self.origin=origin
        self.locations=locations
    def __getattr__(self,name):
        return getattr(self.delegate,name)
    def create_module(self,spec):
        require_import_loader_authority()
        require_tooling_resolution(self)
        if spec is not self.spec:
            reject_tooling_attestation()
        create=getattr(self.delegate,"create_module",None)
        return None if create is None else create(spec)
    def exec_module(self,module):
        require_import_loader_authority()
        require_tooling_resolution(self)
        binding=(self.spec,self,self.delegate,self.origin,self.locations)
        require_tooling_module_binding(self.fullname,module,binding)
        names_before=frozenset(
            name for name in sys.modules if name.partition(".")[0] in allowed_tooling
        )
        self.delegate.exec_module(module)
        current=sys.modules.get(self.fullname)
        require_tooling_module_binding(self.fullname,current,binding)
        tooling_attestations[self.fullname]=(current,*binding)
        tooling_callable_attestations[self.fullname]={
            name:(value,callable_authority(value))
            for name,value in vars(current).items()
            if callable(value)
        }
        tooling_namespace_keys[self.fullname]=frozenset(vars(current))
        names_after=frozenset(
            name for name in sys.modules if name.partition(".")[0] in allowed_tooling
        )
        for alias in names_after-names_before-{self.fullname}:
            if alias in tooling_attestations or alias in tooling_alias_attestations:
                continue
            alias_module=sys.modules[alias]
            canonical=tuple(
                name for name,attestation in tooling_attestations.items()
                if attestation[0] is alias_module
            )
            if len(canonical) != 1:
                reject_tooling_attestation()
            tooling_alias_attestations[alias]=(alias_module,canonical[0],self.fullname)
class FirstPartyDenyGuard:
    def find_spec(self,fullname,path=None,target=None):
        if first_party(fullname):
            violations.add("installed_friday_preloaded")
            raise RuntimeError("installed_friday_preloaded")
        return None
class TestToolingGuard:
    def find_spec(self,fullname,path=None,target=None):
        if first_party(fullname):
            return None
        root=fullname.partition(".")[0]
        if root not in allowed_tooling:
            return None
        require_import_loader_authority()
        parent=fullname.rpartition(".")[0]
        if not parent:
            if path is not None:
                reject_tooling_origin("test_tooling_origin_invalid")
        else:
            parent_binding=tooling_resolutions.get(parent)
            expected=None if parent_binding is None else parent_binding[4]
            try:
                raw_path=None if path is None else tuple(path)
                resolved_path=(
                    None if raw_path is None else
                    tuple(pathlib.Path(entry).resolve(strict=True) for entry in raw_path)
                )
            except (OSError,TypeError,ValueError) as exc:
                violations.add("test_tooling_origin_invalid")
                raise RuntimeError("test_tooling_origin_invalid") from exc
            if (
                expected is None
                or raw_path != tuple(str(location) for location in expected)
                or resolved_path != expected
            ):
                reject_tooling_origin("test_tooling_origin_invalid")
            require_tooling_module_binding(parent,sys.modules.get(parent),parent_binding)
        origin,is_package=tooling_source(fullname)
        package_location=origin.parent if is_package else None
        locations=None if package_location is None else (package_location,)
        delegate=pinned_source_file_loader(fullname,str(origin))
        spec=pinned_spec_from_file_location(
            fullname,str(origin),loader=delegate,
            submodule_search_locations=(None if locations is None else [str(locations[0])]),
        )
        if spec is None or spec.loader is not delegate:
            reject_tooling_origin("test_tooling_origin_missing")
        loader=ToolingLoader(fullname,spec,delegate,origin,locations)
        spec.loader=loader
        tooling_resolutions[fullname]=(spec,loader,delegate,origin,locations)
        return spec
pinned_code_owned_loader_methods=tuple(
    (owner,name,value,callable_authority(value))
    for owner in (
        FirstPartyLoader,FirstPartyGuard,ToolingLoader,FirstPartyDenyGuard,TestToolingGuard,
    )
    for name,value in vars(owner).items()
    if callable(value)
)
def require_all_tooling_attested():
    current=frozenset(
        name for name in sys.modules if name.partition(".")[0] in allowed_tooling
    )
    if current != frozenset(tooling_attestations)|frozenset(tooling_alias_attestations):
        reject_tooling_attestation()
    for name in tooling_attestations:
        attestation=tooling_attestations[name]
        module=sys.modules[name]
        if attestation[0] is not module:
            reject_tooling_attestation()
        require_tooling_module_binding(name,module,attestation[1:])
        if not tooling_namespace_keys[name].issubset(vars(module)):
            raise RuntimeError("test_tooling_module_poisoned")
        for attribute,(value,authority) in tooling_callable_attestations[name].items():
            if (
                getattr(module,attribute,None) is not value
                or callable_authority(value) != authority
            ):
                raise RuntimeError("test_tooling_callable_poisoned")
    for alias,(module,canonical,owner) in tooling_alias_attestations.items():
        canonical_attestation=tooling_attestations.get(canonical)
        if (
            sys.modules.get(alias) is not module
            or canonical_attestation is None
            or canonical_attestation[0] is not module
            or owner not in tooling_attestations
        ):
            reject_tooling_attestation()
if any(name.partition(".")[0] in allowed_tooling for name in sys.modules):
    raise RuntimeError("test_tooling_preloaded")
first_party_deny_guard=FirstPartyDenyGuard()
test_tooling_guard=TestToolingGuard()
sys.meta_path.insert(0,first_party_deny_guard)
sys.meta_path.insert(1,test_tooling_guard)
import pytest,_pytest,anyio.pytest_plugin,execnet,pluggy,pytest_asyncio.plugin,xdist.plugin
required_tooling=("pytest","_pytest","pluggy","pytest_asyncio.plugin","xdist.plugin","execnet","anyio.pytest_plugin")
def tooling_ref(module):
    raw_origin=getattr(module,"__file__",None)
    if raw_origin is None:
        raise RuntimeError("test_tooling_origin_missing")
    candidate=pathlib.Path(raw_origin)
    resolved=candidate.resolve(strict=True)
    try:
        if candidate != resolved:
            raise ValueError
        return resolved.relative_to(tooling_site).as_posix(),resolved
    except ValueError as exc:
        raise RuntimeError("test_tooling_origin_invalid") from exc
for name in required_tooling:
    tooling_ref(sys.modules[name])
import anyio._core._eventloop as anyio_eventloop
pinned_async_backend=anyio_eventloop.get_async_backend("asyncio")
if anyio_eventloop.loaded_backends.get("asyncio") is not pinned_async_backend:
    raise RuntimeError("test_tooling_state_poisoned")
if "tools" in sys.modules or "tools.quality_gate" in sys.modules:
    raise RuntimeError("quality_gate_preloaded")
tools_path=source_root/"tools"
gate_path=quality_gate_path
try:
    tools_status=tools_path.lstat()
    gate_status=gate_path.lstat()
    gate_raw=gate_path.read_bytes()
except OSError as exc:
    raise RuntimeError("quality_gate_origin_invalid") from exc
if (
    tools_path.resolve(strict=True) != tools_path
    or gate_path.resolve(strict=True) != gate_path
    or tools_path.name != "tools"
    or gate_path.name != "quality_gate.py"
    or not stat.S_ISDIR(tools_status.st_mode)
    or not stat.S_ISREG(gate_status.st_mode)
    or gate_status.st_nlink != 1
    or len(quality_gate_sha256) != 64
    or any(character not in "0123456789abcdef" for character in quality_gate_sha256)
    or hashlib.sha256(gate_raw).hexdigest() != quality_gate_sha256
):
    raise RuntimeError("quality_gate_origin_invalid")
tools_module=types.ModuleType("tools")
tools_spec=importlib.machinery.ModuleSpec("tools",loader=None,is_package=True)
tools_spec.submodule_search_locations=[str(tools_path)]
tools_module.__file__=None
tools_module.__loader__=None
tools_module.__package__="tools"
tools_module.__path__=[str(tools_path)]
tools_module.__spec__=tools_spec
gate_loader=importlib.machinery.SourceFileLoader("tools.quality_gate",str(gate_path))
gate_spec=importlib.machinery.ModuleSpec(
    "tools.quality_gate",loader=gate_loader,origin=str(gate_path),is_package=False
)
exact_quality_gate=types.ModuleType("tools.quality_gate")
exact_quality_gate.__file__=str(gate_path)
exact_quality_gate.__loader__=gate_loader
exact_quality_gate.__package__="tools"
exact_quality_gate.__spec__=gate_spec
sys.modules["tools"]=tools_module
sys.modules["tools.quality_gate"]=exact_quality_gate
tools_module.quality_gate=exact_quality_gate
try:
    exec(compile(gate_raw,str(gate_path),"exec",dont_inherit=True),exact_quality_gate.__dict__)
except BaseException:
    sys.modules.pop("tools.quality_gate",None)
    sys.modules.pop("tools",None)
    raise
if (
    pathlib.Path(exact_quality_gate.__file__).resolve(strict=True) != gate_path
    or exact_quality_gate.__loader__ is not gate_loader
    or exact_quality_gate.__spec__ is not gate_spec
    or gate_spec.loader is not gate_loader
):
    raise RuntimeError("quality_gate_origin_invalid")
if any(
    not isinstance(module,types.ModuleType)
    for name,module in sys.modules.items()
    if name.partition(".")[0] in allowed_tooling
):
    raise RuntimeError("test_tooling_module_poisoned")
runner_authority_roots=allowed_tooling|{"asyncio"}
runner_modules={
    name:module for name,module in sys.modules.items()
    if name.partition(".")[0] in runner_authority_roots
}
runner_modules["tools"]=tools_module
runner_modules["tools.quality_gate"]=exact_quality_gate
runner_bindings={
    name:(
        module,
        getattr(module,"__spec__",None),
        getattr(module,"__loader__",None),
        getattr(getattr(module,"__spec__",None),"loader",None),
    )
    for name,module in runner_modules.items()
}
def code_binding(value):
    if isinstance(value,types.FunctionType):
        return (
            "function",id(value.__code__),
            tuple(id(item) for item in (value.__defaults__ or ())),
            tuple(sorted((name,id(item)) for name,item in (value.__kwdefaults__ or {}).items())),
        )
    if isinstance(value,(staticmethod,classmethod)):
        return (type(value).__name__,code_binding(value.__func__))
    if isinstance(value,property):
        return (
            "property",
            code_binding(value.fget),code_binding(value.fset),code_binding(value.fdel),
        )
    if isinstance(value,type):
        return (
            "class",
            tuple(
                (name,id(member),code_binding(member))
                for name,member in sorted(vars(value).items())
                if isinstance(member,(types.FunctionType,staticmethod,classmethod,property))
            ),
        )
    return (type(value).__name__,)
runner_callables={
    (name,attribute):value
    for name,module in runner_modules.items()
    for attribute,value in vars(module).items()
    if callable(value)
}
runner_callable_codes={
    key:code_binding(value) for key,value in runner_callables.items()
}
runner_namespace_keys={
    name:frozenset(vars(module)) for name,module in runner_modules.items()
}
runner_names=frozenset(
    name for name in sys.modules if name.partition(".")[0] in allowed_tooling
)
pytest_main=pytest.main
if (
    any(first_party(name) for name in sys.modules)
    or not sys.meta_path
    or sys.meta_path[0] is not first_party_deny_guard
    or len(sys.meta_path) < 2
    or sys.meta_path[1] is not test_tooling_guard
):
    raise RuntimeError("installed_friday_preloaded")
first_party_guard=FirstPartyGuard()
sys.meta_path[0]=first_party_guard
runner_meta_path=tuple(sys.meta_path)
sys.path.insert(0,str(source_root))
sys.path.insert(0,str(site))
runner_sys_path=tuple(sys.path)
def require_runner_authority(*,closed_names):
    require_import_loader_authority()
    if (
        tuple(sys.meta_path) != runner_meta_path
        or type(first_party_guard) is not FirstPartyGuard
        or vars(first_party_guard)
        or type(test_tooling_guard) is not TestToolingGuard
        or vars(test_tooling_guard)
        or tuple(sys.path) != runner_sys_path
        or sys.modules.get("tools") is not tools_module
        or sys.modules.get("tools.quality_gate") is not exact_quality_gate
        or getattr(tools_module,"quality_gate",None) is not exact_quality_gate
        or tuple(getattr(tools_module,"__path__",())) != (str(tools_path),)
        or pytest.main is not pytest_main
        or os.environ.get("PYTHONPYCACHEPREFIX") != str(pycache_prefix)
        or sys.pycache_prefix != str(pycache_prefix)
        or sys._xoptions != {"pycache_prefix":str(pycache_prefix)}
    ):
        raise RuntimeError("test_tooling_authority_poisoned")
    if closed_names and frozenset(
        name for name in sys.modules if name.partition(".")[0] in allowed_tooling
    ) != runner_names:
        raise RuntimeError("test_tooling_module_poisoned")
    for name,(module,spec,loader,spec_loader) in runner_bindings.items():
        current=sys.modules.get(name)
        if (
            current is not module
            or getattr(current,"__spec__",None) is not spec
            or getattr(current,"__loader__",None) is not loader
            or getattr(getattr(current,"__spec__",None),"loader",None) is not spec_loader
        ):
            raise RuntimeError("test_tooling_module_poisoned")
    for (name,attribute),value in runner_callables.items():
        if (
            getattr(runner_modules[name],attribute,None) is not value
            or code_binding(value) != runner_callable_codes[(name,attribute)]
        ):
            raise RuntimeError("test_tooling_callable_poisoned")
    if any(
        not runner_namespace_keys[name].issubset(vars(module))
        for name,module in runner_modules.items()
    ):
        raise RuntimeError("test_tooling_module_poisoned")
    if (
        anyio_eventloop.loaded_backends.get("asyncio") is not pinned_async_backend
        or set(anyio_eventloop.loaded_backends) != {"asyncio"}
    ):
        raise RuntimeError("test_tooling_state_poisoned")
    require_all_tooling_attested()
require_runner_authority(closed_names=True)
if "friday" in sys.modules:
    raise RuntimeError("installed_friday_preloaded")
code=pytest_main(sys.argv[1:])
require_runner_authority(closed_names=False)
try:
    if os.path.lexists(pycache_prefix) and (
        pycache_prefix.resolve(strict=True) != pycache_prefix
        or not pycache_prefix.is_dir()
        or any(pycache_prefix.iterdir())
    ):
        violations.add("bytecode_execution_unattested")
except OSError as exc:
    violations.add("bytecode_execution_unattested")
    raise RuntimeError("bytecode_execution_unattested") from exc
if violations:
    raise RuntimeError(sorted(violations)[0])
current_first_party=frozenset(name for name in sys.modules if first_party(name))
if (
    not current_first_party
    or current_first_party != frozenset(first_party_attestations)
):
    reject_first_party_attestation()
for name in sorted(current_first_party):
    module=sys.modules[name]
    attestation=first_party_attestations.get(name)
    if attestation is None or attestation[0] is not module:
        reject_first_party_attestation()
    require_first_party_module_binding(name,module,attestation[1:])
    raw_origin=getattr(module,"__file__",None)
    if raw_origin is None:
        raise RuntimeError("first_party_origin_missing")
    confined(raw_origin)
if not origins:
    raise RuntimeError("first_party_origin_missing")
friday_module=sys.modules.get("friday")
if friday_module is not None and pathlib.Path(friday_module.__file__).resolve(strict=True) != site/"friday"/"__init__.py":
    raise RuntimeError("installed_friday_origin_invalid")
tooling_files={}
for name,module in sorted(sys.modules.items()):
    raw_origin=getattr(module,"__file__",None)
    if raw_origin is None:
        continue
    candidate=pathlib.Path(raw_origin)
    try:
        candidate.relative_to(tooling_site)
    except ValueError:
        continue
    try:
        resolved=candidate.resolve(strict=True)
        relative=resolved.relative_to(tooling_site).as_posix()
    except ValueError as exc:
        raise RuntimeError("test_tooling_origin_invalid") from exc
    if (
        candidate != resolved
        or not resolved.is_file()
        or name.partition(".")[0] not in allowed_tooling
    ):
        raise RuntimeError("test_tooling_origin_invalid")
    tooling_files[relative]=hashlib.sha256(resolved.read_bytes()).hexdigest()
for name in required_tooling:
    tooling_ref(sys.modules[name])
tooling_projection=[{"origin":name,"sha256":digest} for name,digest in sorted(tooling_files.items())]
if len(tooling_projection) < len(required_tooling):
    raise RuntimeError("test_tooling_origin_missing")
tooling_bytes=json.dumps(tooling_projection,ensure_ascii=True,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
ordered_origins=sorted(origins)
origin_bytes=json.dumps(ordered_origins,ensure_ascii=True,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
report={
    "interpreter_ref":interpreter_ref,
    "module_count":len(ordered_origins),
    "module_origins_sha256":hashlib.sha256(origin_bytes).hexdigest(),
    "schema":"friday.clean-artifact-import-origin.v1",
    "site_packages_ref":site_ref,
    "source_commit":source_commit,
    "subprocess_policy":"cpython_audit_deny",
    "tooling_module_count":len(tooling_projection),
    "tooling_modules_sha256":hashlib.sha256(tooling_bytes).hexdigest(),
    "tooling_policy":"explicit_single_site_v1",
    "wheel_sha256":wheel_sha256,
}
raw=json.dumps(report,ensure_ascii=True,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
descriptor=os.open(report_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC,0o600)
try:
    view=memoryview(raw)
    while view:
        written=os.write(descriptor,view)
        if written < 1:
            raise RuntimeError("origin_report_write_failed")
        view=view[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
raise SystemExit(code)
"""

ENVIRONMENT_BY_CLASS = {
    "deterministic contract": "deterministic_contract",
    "integration path": "integration",
    "clean artifact path": "clean_artifact",
    "synthetic live path": "synthetic_live",
    "production read-only observation": "production_read_only",
    "physical device evidence": "physical_android",
    "restart and recovery evidence": "restart_recovery",
    "rollback evidence": "rollback",
    "backup and restore evidence": "backup_restore",
}
_CHECK_SUFFIXES = {
    "deterministic contract": ("contract_suite",),
    "integration path": ("integration_suite",),
    "clean artifact path": ("installed_journey_suite",),
    "synthetic live path": ("synthetic_live_battery",),
    "production read-only observation": ("database_integrity", "schema_attestation", "service_health"),
    "physical device evidence": ("android_round_trip", "real_conflict_preserved"),
    "restart and recovery evidence": ("cancellation", "expiry", "restart_resume"),
    "rollback evidence": ("activation_rollback",),
    "backup and restore evidence": ("clean_restore",),
}


def _parameterized_refs(base: str, parameter_ids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{base}[{parameter_id}]" for parameter_id in parameter_ids)


_PROMOTED_WINDOW_REFS = _parameterized_refs(
    "tests/test_message_window_runtime_integration.py::"
    "test_promoted_exact_window_is_deterministic_scoped_and_receipted",
    ("2-complete", "21-partial", "0-empty"),
)
_MESSAGE_ARCHIVE_REFS = _parameterized_refs(
    "tests/test_archive_search_runtime_publication.py::"
    "test_selected_message_archive_evidence_replays_after_restart_then_fails_closed",
    (
        "exact-document-reference-search-denied",
        "exact-document-reference-corpus-denied",
        "exact-document-reference-source-drifted",
        "natural-content-reference-search-denied",
        "natural-content-reference-corpus-denied",
        "natural-content-reference-source-drifted",
    ),
)
_ARCHIVE_REPLAY_FAILURE_REFS = _parameterized_refs(
    "tests/test_archive_search_runtime_publication.py::"
    "test_selected_archive_replay_failure_is_source_free_and_suspends",
    ("denied-denied", "drifted-drifted"),
)
_OBSIDIAN_MESSAGE_MATRIX_REFS = _parameterized_refs(
    "tests/test_agent_obsidian_acceptance_message_matrix.py::"
    "test_every_exact_tier_a_b_message_routes_through_full_chat_once",
    (
        "OBS-NOTE-01",
        "OBS-NOTE-02",
        "OBS-DAILY-01",
        "OBS-TASK-01-add",
        "OBS-TASK-01-query",
        "OBS-META-01",
        "OBS-SEARCH-01",
        "OBS-SEARCH-02",
        "OBS-SYNC-01",
        "OBS-LINK-01",
        "OBS-MOVE-01-move",
        "OBS-MOVE-01-backlinks",
        "OBS-TEMPLATE-01",
        "OBS-WORK-01-save",
        "OBS-WORK-01-links",
        "OBS-BASE-01",
        "OBS-OFFLINE-01",
        "OBS-CONFLICT-01-replace",
        "OBS-CONFLICT-01-preview",
        "OBS-RECOVERY-01-append",
        "OBS-RECOVERY-01-resume",
        "OBS-DELETE-01-delete",
        "OBS-DELETE-01-search",
    ),
)
_SUPERVISOR_REVIEW_REFS = _parameterized_refs(
    "tests/test_supervisor_assist_controller.py::test_review_and_web_recovery_are_strictly_bounded",
    ("0", "1"),
)

_CONVERSATION_CLEAN_ARTIFACT_REFS = (
    *_PROMOTED_WINDOW_REFS,
    *_parameterized_refs(
        "tests/test_message_window_runtime_integration.py::"
        "test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried",
        ("content", "snapshot", "insert"),
    ),
    "tests/test_archive_search_runtime_publication.py::"
    "test_real_router_preserves_two_exact_archive_pages_through_final_answer",
)
_DOCUMENT_CLEAN_ARTIFACT_REFS = (
    "tests/test_v12_file_evidence_reader.py::test_current_turn_native_files_form_one_process_owned_bundle",
    "tests/test_v12_file_evidence_reader.py::test_reader_contract_matches_real_ingestion_projections",
    "tests/test_archive_search_runtime_publication.py::"
    "test_natural_selected_document_question_uses_bound_preingestion_v12_without_ordinary_paths",
)
_DURABLE_CLEAN_ARTIFACT_REFS = (
    "tests/test_a_reminder_is_set_before_the_model_speaks.py::"
    "test_the_reminder_is_set_without_asking_the_model",
    "tests/test_durable_scheduled_work_recovery.py::test_two_workers_only_one_claims_pending_task",
    *_parameterized_refs(
        "tests/test_durable_scheduled_work_recovery.py::"
        "test_post_checkpoint_failure_is_uncertain_and_never_replayed",
        ("exception", "cancelled"),
    ),
    "tests/test_reminder_send_edge_storage.py::test_two_storage_workers_get_one_due_reminder_body",
    *_parameterized_refs(
        "tests/test_reminder_send_edge_storage.py::"
        "test_pending_reminder_cannot_be_settled_without_send_edge_claim",
        ("sent", "failed", "uncertain"),
    ),
    "tests/test_reminder_delivery_fence.py::test_lost_ack_reacks_off_page_after_restart_without_resend",
    "tests/test_release_bound_reminder_scan.py::"
    "test_release_evidence_scan_stops_at_exact_ten_pages_of_two_hundred",
    "tests/test_release_bound_reminder_scan.py::"
    "test_release_evidence_scan_stops_when_continuation_cursor_is_missing",
)
_HONEST_DEGRADATION_CLEAN_ARTIFACT_REFS = (
    "tests/test_search_provider_refusal_is_not_emptiness.py::"
    "test_202_from_duckduckgo_is_a_refusal_not_an_empty_result[asyncio]",
    "tests/test_search_provider_refusal_is_not_emptiness.py::"
    "test_a_provider_that_honestly_found_nothing_is_not_a_refusal[asyncio]",
    "tests/test_search_provider_refusal_is_not_emptiness.py::"
    "test_the_chain_moves_on_when_the_first_provider_refuses[asyncio]",
    *_parameterized_refs(
        "tests/test_message_window_runtime_integration.py::"
        "test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried",
        ("content", "snapshot", "insert"),
    ),
    "tests/test_message_window_work_item_runtime.py::"
    "test_post_boundary_admission_race_returns_atomic_clarification_without_execution",
)


_PROOF_REFS_BY_JOURNEY_CLASS = {
    ("conversation_recall", "deterministic contract"): (*_PROMOTED_WINDOW_REFS,),
    ("conversation_recall", "integration path"): (
        *_PROMOTED_WINDOW_REFS,
        *_MESSAGE_ARCHIVE_REFS,
    ),
    ("conversation_recall", "restart and recovery evidence"): (
        "tests/test_message_window_work_item_runtime.py::test_restart_temporal_followup_reuses_identity_role_and_zone_with_one_cas_update",
        *_MESSAGE_ARCHIVE_REFS,
    ),
    ("conversation_recall", "clean artifact path"): _CONVERSATION_CLEAN_ARTIFACT_REFS,
    ("document_recall_answer", "deterministic contract"): (
        "tests/test_v12_file_evidence_reader.py::test_current_turn_native_files_form_one_process_owned_bundle",
    ),
    ("document_recall_answer", "integration path"): (
        "tests/test_v12_file_evidence_reader.py::test_reader_contract_matches_real_ingestion_projections",
        "tests/test_archive_search_runtime_publication.py::test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart",
        "tests/test_archive_search_runtime_publication.py::test_locate_select_and_explain_document_survives_both_runtime_restarts",
    ),
    ("document_recall_answer", "synthetic live path"): (
        "tests/test_document_contour_live_battery.py::test_manifest_is_exactly_ten_unique_document_scenarios",
    ),
    ("document_recall_answer", "restart and recovery evidence"): (
        "tests/test_archive_search_runtime_publication.py::test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart",
        "tests/test_archive_search_runtime_publication.py::test_locate_select_and_explain_document_survives_both_runtime_restarts",
        *_ARCHIVE_REPLAY_FAILURE_REFS,
    ),
    ("document_recall_answer", "clean artifact path"): _DOCUMENT_CLEAN_ARTIFACT_REFS,
    ("obsidian_write_sync", "deterministic contract"): (
        "tests/test_obsidian_structured_acceptance_core.py::test_conflict_preview_is_non_destructive_and_contains_both_versions",
    ),
    ("obsidian_write_sync", "integration path"): (*_OBSIDIAN_MESSAGE_MATRIX_REFS,),
    ("obsidian_write_sync", "synthetic live path"): (
        "tests/test_obsidian_syncthing_live.py::test_pinned_syncthing_generates_and_accepts_the_managed_rest_contract",
    ),
    ("obsidian_write_sync", "restart and recovery evidence"): (
        "tests/test_obsidian_runtime.py::test_resume_reuses_daily_operation_identity_without_duplicate_text",
    ),
    ("durable_scheduled_work", "deterministic contract"): (
        "tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_tool_is_removed_so_nobody_is_woken_twice",
    ),
    ("durable_scheduled_work", "integration path"): (
        "tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_reminder_is_set_without_asking_the_model",
    ),
    ("durable_scheduled_work", "synthetic live path"): (
        "tests/test_synthetic_live_battery.py::test_exact_reminder_oracle_owns_the_model_boundary",
    ),
    ("durable_scheduled_work", "restart and recovery evidence"): (
        "tests/test_mission_budgets_and_recovery.py::test_spent_budget_survives_a_restart",
    ),
    ("durable_scheduled_work", "clean artifact path"): _DURABLE_CLEAN_ARTIFACT_REFS,
    ("honest_degradation", "deterministic contract"): (
        "tests/test_search_provider_refusal_is_not_emptiness.py::test_202_from_duckduckgo_is_a_refusal_not_an_empty_result[asyncio]",
    ),
    ("honest_degradation", "integration path"): (
        "tests/test_search_provider_refusal_is_not_emptiness.py::test_the_chain_moves_on_when_the_first_provider_refuses[asyncio]",
    ),
    ("honest_degradation", "synthetic live path"): (
        "tests/test_synthetic_live_battery.py::test_package_a_oracle_does_not_share_the_mutated_production_predicate",
    ),
    ("honest_degradation", "restart and recovery evidence"): (
        "tests/test_message_window_work_item_runtime.py::test_post_boundary_admission_race_returns_atomic_clarification_without_execution",
    ),
    ("honest_degradation", "clean artifact path"): _HONEST_DEGRADATION_CLEAN_ARTIFACT_REFS,
    ("current_file_web_comparison", "deterministic contract"): (
        "tests/test_compare_current_file_web_work_graph_schema45.py::test_schema45_exact_binding_is_durable_immutable_and_revision_cas",
    ),
    ("current_file_web_comparison", "integration path"): (*_SUPERVISOR_REVIEW_REFS,),
    ("current_file_web_comparison", "restart and recovery evidence"): (
        "tests/test_supervisor_assist_graph_adapter.py::test_terminal_cancel_and_startup_reconcile_publish_closed_receipts",
    ),
}
_GENERIC_OPERATOR_REFS = frozenset(
    {
        "tools/immutable_release_operator.py",
        "tests/test_immutable_release_operator.py::test_installed_surface_smoke_uses_one_hermetic_environment_and_cleans_it",
        "tests/test_immutable_release_operator.py::test_backend_start_uncertainty_never_restores_backup_or_runs_schema33",
        "tests/test_immutable_release_operator.py::test_obsidian_root_is_restored_exactly_with_database_and_inbox",
        "tests/test_storage_and_lifecycle.py::test_verified_backup_restore_is_atomic_and_creates_safety_copy",
    }
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TEST_PATH = re.compile(r"tests/(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_]{1,180}\.py\Z")
_TEST_NAME = re.compile(r"test_[A-Za-z0-9_]{1,159}\Z")
_PARAMETER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SAFE_ID = re.compile(r"[a-z][a-z0-9_.:-]{1,127}\Z")
_RECEIPT_FIELDS = frozenset(
    {
        "$schema",
        "journey_id",
        "evidence_class",
        "result",
        "environment",
        "observed_at_utc",
        "check_ids",
        "release",
        "execution",
        "proofs",
        "owner_smoke",
    }
)
_MANIFEST_FIELDS = frozenset({"$schema", "journey_id", "evidence_class", "result", "release", "observation"})
_MANIFEST_OBSERVATION_FIELDS = frozenset(
    {
        "environment",
        "observed_at_utc",
        "check_ids",
        "artifact_ref",
        "artifact_schema",
        "artifact_sha256",
    }
)
_EXECUTION_FIELDS = frozenset(
    {
        "collection_sha256",
        "exit_code",
        "outcome_projection_sha256",
        "producer_path",
        "producer_source_sha256",
        "runner",
    }
)
_ARTIFACT_IMPORT_FIELDS = frozenset(
    {
        "interpreter_ref",
        "origin_report_sha256",
        "site_packages_ref",
        "subprocess_policy",
        "tooling_modules_sha256",
        "tooling_policy",
        "tooling_snapshot_sha256",
    }
)
_PRODUCTION_OBSERVATION_RECEIPT_FIELDS = frozenset(
    {
        "$schema",
        "journey_id",
        "evidence_class",
        "environment",
        "check_ids",
        "release",
        "observation",
        "checks",
        "result",
    }
)
_PRODUCTION_OBSERVATION_BINDING_FIELDS = frozenset(
    {
        "backend_process_epoch_sha256",
        "challenge_sha256",
        "endpoint_response_schema",
        "endpoint_response_sha256",
        "health_after_sha256",
        "health_before_sha256",
        "release_binding_sha256",
    }
)
_PRODUCTION_OBSERVATION_CHECK_FIELDS = frozenset({"check_id", "outcome"})
_PRODUCTION_OBSERVATION_MANIFEST_OBSERVATION_FIELDS = frozenset(
    {
        "environment",
        "check_ids",
        "artifact_ref",
        "artifact_schema",
        "artifact_sha256",
    }
)
_RELEASE_CAPTAIN_ARTIFACT_FIELDS = frozenset(
    {
        "schema",
        "release",
        "release_binding_sha256",
        "endpoint_response",
        "endpoint_response_sha256",
        "challenge_sha256",
        "backend_process_epoch_sha256",
        "health_before_sha256",
        "health_after_sha256",
    }
)
_PRODUCTION_RESPONSE_FIELDS = frozenset(
    {
        "schema",
        "challenge_sha256",
        "backend_process_epoch_sha256",
        "backend_lease_owned",
        "database",
        "scheduled_work",
        "hard_contradictions",
    }
)
_PRODUCTION_DATABASE_FIELDS = frozenset(
    {
        "schema_version",
        "schema_attestation_sha256",
        "integrity",
        "foreign_key_violations",
    }
)
_PRODUCTION_SCHEDULED_WORK_FIELDS = frozenset({"missions", "mission_tasks", "reminders", "workers"})
_PRODUCTION_MISSION_STATES = (
    "proposed",
    "ready",
    "running",
    "paused",
    "blocked",
    "completed",
    "failed",
    "cancelled",
)
_PRODUCTION_TASK_STATES = (
    "pending",
    "running",
    "done",
    "failed",
    "skipped",
    "uncertain",
    "compensated",
)
_PRODUCTION_REMINDER_STATES = ("pending", "uncertain", "sent", "failed", "dismissed")
_PRODUCTION_WORKER_STATES = (
    "scheduled",
    "running",
    "ok",
    "error",
    "timeout",
    "skipped",
    "unknown",
)
_PRODUCTION_WORKER_FIELDS = frozenset({"present", "missing", "health_states"})


class ExactReleaseEvidenceError(ValueError):
    """One closed validation or production failure."""


_EXECUTION_WITNESS_AUTHORITY = object()
_EVIDENCE_BUNDLE_AUTHORITY = object()
_RELEASE_RUNTIME_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class _ExecutionWitness:
    """Process-local proof returned only by the closed pytest runner."""

    outcomes: tuple[str, ...]
    exit_code: int
    collection_sha256: str
    outcome_projection_sha256: str
    authority: object
    artifact_origin_sha256: str | None = None
    interpreter_ref: str | None = None
    site_packages_ref: str | None = None
    subprocess_policy: str | None = None
    tooling_modules_sha256: str | None = None
    tooling_policy: str | None = None
    tooling_snapshot_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Process-authenticated canonical receipt/manifest bytes and references."""

    receipt_ref: str
    receipt: bytes
    receipt_sha256: str
    manifest_ref: str
    manifest: bytes
    manifest_sha256: str
    result: str
    authority: object


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    source_commit: str
    tree_sha256: str
    wheel_sha256: str
    database_schema: int

    def __post_init__(self) -> None:
        if (
            type(self) is not ReleaseIdentity
            or type(self.source_commit) is not str
            or type(self.tree_sha256) is not str
            or type(self.wheel_sha256) is not str
            or _COMMIT.fullmatch(self.source_commit) is None
            or _SHA256.fullmatch(self.tree_sha256) is None
            or _SHA256.fullmatch(self.wheel_sha256) is None
            or type(self.database_schema) is not int
            or self.database_schema < 1
        ):
            raise ExactReleaseEvidenceError("release_identity_invalid")

    def payload(self) -> dict[str, object]:
        return _release_payload(self)


@dataclass(frozen=True, slots=True)
class _AuthenticatedReleaseRuntime:
    """Process-local authority for one sealed installed package surface."""

    root: Path
    identity: ReleaseIdentity
    interpreter: Path
    interpreter_ref: str
    site_packages: Path
    site_packages_ref: str
    package_root: Path
    authority: object


@dataclass(frozen=True, slots=True)
class AuthenticatedOwnerSmokeBinding:
    """Expected binding supplied only after a separate authenticator accepted it.

    Constructing this value is not authentication.  A receipt embedding the
    same fields is rejected unless the validator receives this external
    expected binding.
    """

    schema: str
    authority: str
    artifact_ref: str
    artifact_sha256: str

    def payload(self) -> dict[str, str]:
        payload = _owner_smoke_payload(self)
        assert payload is not None
        return payload


@dataclass(frozen=True, slots=True)
class AuthenticatedProductionObservationBinding:
    """Exact expected values accepted by a separate Release Captain.

    Constructing this immutable value is not authentication.  Production
    evidence is valid only when its validator receives the same exact binding
    independently, including the canonical endpoint bytes.  The endpoint body
    is kept out of reprs and published evidence.
    """

    release: ReleaseIdentity
    endpoint_response: bytes = field(repr=False)
    challenge_sha256: str
    backend_process_epoch_sha256: str
    health_before_sha256: str
    health_after_sha256: str

    def __post_init__(self) -> None:
        _production_binding_payload(self)

    def payload(self) -> dict[str, str]:
        return _production_binding_payload(self)


def _release_payload(identity: ReleaseIdentity) -> dict[str, object]:
    if (
        type(identity) is not ReleaseIdentity
        or type(identity.source_commit) is not str
        or type(identity.tree_sha256) is not str
        or type(identity.wheel_sha256) is not str
        or _COMMIT.fullmatch(identity.source_commit) is None
        or _SHA256.fullmatch(identity.tree_sha256) is None
        or _SHA256.fullmatch(identity.wheel_sha256) is None
        or type(identity.database_schema) is not int
        or identity.database_schema < 1
    ):
        raise ExactReleaseEvidenceError("release_identity_invalid")
    return {
        "database_schema": identity.database_schema,
        "source_commit": identity.source_commit,
        "tree_sha256": identity.tree_sha256,
        "wheel_sha256": identity.wheel_sha256,
    }


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def proof_refs(journey_id: str, evidence_class: str) -> tuple[str, ...]:
    if type(journey_id) is not str or type(evidence_class) is not str:
        raise ExactReleaseEvidenceError("proof_inventory_invalid")
    refs = _PROOF_REFS_BY_JOURNEY_CLASS.get((journey_id, evidence_class), ())
    if not refs or any(ref in _GENERIC_OPERATOR_REFS for ref in refs) or len(set(refs)) != len(refs):
        raise ExactReleaseEvidenceError("proof_inventory_invalid")
    return refs


def _requires_candidate_runtime(journey_id: object, evidence_class: object) -> bool:
    """Return the only journey/class pairs whose proofs execute the sealed wheel."""

    return bool(
        type(journey_id) is str
        and type(evidence_class) is str
        and (
            evidence_class == _CLEAN_ARTIFACT_CLASS
            or (evidence_class == _RESTART_RECOVERY_CLASS and journey_id in _CANDIDATE_BOUND_FAULT_JOURNEYS)
        )
    )


def receipt_schema(evidence_class: str, *, journey_id: str | None = None) -> str:
    if type(evidence_class) is not str or evidence_class not in ENVIRONMENT_BY_CLASS:
        raise ExactReleaseEvidenceError("evidence_class_invalid")
    if journey_id is not None and type(journey_id) is not str:
        raise ExactReleaseEvidenceError("journey_id_invalid")
    if evidence_class == _PRODUCTION_OBSERVATION_CLASS:
        if journey_id not in {None, _PRODUCTION_OBSERVATION_JOURNEY}:
            raise ExactReleaseEvidenceError("production_observation_scope_invalid")
        return PRODUCTION_OBSERVATION_RECEIPT_SCHEMA
    candidate_runtime = (
        evidence_class == _CLEAN_ARTIFACT_CLASS
        if journey_id is None
        else _requires_candidate_runtime(journey_id, evidence_class)
    )
    return CLEAN_ARTIFACT_RECEIPT_SCHEMA if candidate_runtime else RECEIPT_SCHEMA


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExactReleaseEvidenceError("receipt_json_invalid")
        result[key] = value
    return result


def _load_canonical_object(
    raw: bytes,
    *,
    maximum_bytes: int,
    failure_code: str,
) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        raise ExactReleaseEvidenceError(failure_code)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ExactReleaseEvidenceError(failure_code)),
        )
        canonical = canonical_json_bytes(value)
    except (TypeError, UnicodeError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ExactReleaseEvidenceError(failure_code) from exc
    if type(value) is not dict or raw != canonical:
        raise ExactReleaseEvidenceError(failure_code)
    return value


def _load_canonical_receipt(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > 65_536:
        raise ExactReleaseEvidenceError("receipt_json_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ExactReleaseEvidenceError("receipt_json_invalid")
            ),
        )
    except (UnicodeError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ExactReleaseEvidenceError("receipt_json_invalid") from exc
    if type(value) is not dict:
        raise ExactReleaseEvidenceError("receipt_json_invalid")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExactReleaseEvidenceError("receipt_json_invalid") from exc
    if raw != canonical:
        raise ExactReleaseEvidenceError("receipt_json_invalid")
    return value


def _load_canonical_manifest(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > 65_536:
        raise ExactReleaseEvidenceError("manifest_json_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ExactReleaseEvidenceError("manifest_json_invalid")
            ),
        )
    except (UnicodeError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ExactReleaseEvidenceError("manifest_json_invalid") from exc
    if type(value) is not dict:
        raise ExactReleaseEvidenceError("manifest_json_invalid")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExactReleaseEvidenceError("manifest_json_invalid") from exc
    if raw != canonical:
        raise ExactReleaseEvidenceError("manifest_json_invalid")
    return value


def release_binding_sha256(identity: ReleaseIdentity) -> str:
    """Hash all four exact release fields for deterministic artifact names."""

    return hashlib.sha256(canonical_json_bytes(_release_payload(identity))).hexdigest()


def _evidence_ref(
    *,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
    result: str,
    kind: str,
) -> str:
    if evidence_class == _PRODUCTION_OBSERVATION_CLASS:
        if journey_id != _PRODUCTION_OBSERVATION_JOURNEY:
            raise ExactReleaseEvidenceError("production_observation_scope_invalid")
    else:
        proof_refs(journey_id, evidence_class)
    if result not in {"VERIFIED", "FAILED"} or kind not in {"receipts", "manifests"}:
        raise ExactReleaseEvidenceError("evidence_ref_invalid")
    environment = ENVIRONMENT_BY_CLASS.get(evidence_class)
    if environment is None or _SAFE_ID.fullmatch(environment) is None:
        raise ExactReleaseEvidenceError("evidence_ref_invalid")
    filename = f"{journey_id}--{environment}--{result.lower()}--{release_binding_sha256(identity)}.json"
    return str(_EVIDENCE_ROOT / kind / filename)


def _validate_production_observation_receipt_structure(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
    expected_journey_id: str,
) -> dict[str, Any]:
    if expected_journey_id != _PRODUCTION_OBSERVATION_JOURNEY:
        raise ExactReleaseEvidenceError("production_observation_scope_invalid")
    expected_release_payload = _release_payload(expected_release)
    if expected_release.database_schema != 50:
        raise ExactReleaseEvidenceError("production_observation_release_invalid")
    value = _load_canonical_receipt(raw)
    observation = value.get("observation")
    checks = value.get("checks")
    check_ids = _check_ids(expected_journey_id, _PRODUCTION_OBSERVATION_CLASS)
    if (
        set(value) != _PRODUCTION_OBSERVATION_RECEIPT_FIELDS
        or value.get("$schema") != PRODUCTION_OBSERVATION_RECEIPT_SCHEMA
        or value.get("journey_id") != expected_journey_id
        or value.get("evidence_class") != _PRODUCTION_OBSERVATION_CLASS
        or value.get("environment") != ENVIRONMENT_BY_CLASS[_PRODUCTION_OBSERVATION_CLASS]
        or value.get("check_ids") != check_ids
        or value.get("release") != expected_release_payload
        or value.get("result") != "VERIFIED"
        or type(observation) is not dict
        or set(observation) != _PRODUCTION_OBSERVATION_BINDING_FIELDS
        or observation.get("endpoint_response_schema") != PRODUCTION_READ_ONLY_OBSERVATION_SCHEMA
        or observation.get("release_binding_sha256") != release_binding_sha256(expected_release)
        or type(checks) is not list
        or len(checks) != len(check_ids)
    ):
        raise ExactReleaseEvidenceError("production_observation_receipt_invalid")
    for key in (
        "backend_process_epoch_sha256",
        "challenge_sha256",
        "endpoint_response_sha256",
        "health_after_sha256",
        "health_before_sha256",
        "release_binding_sha256",
    ):
        _production_digest(
            observation.get(key),
            failure_code="production_observation_receipt_invalid",
        )
    derived_checks = [{"check_id": check_id, "outcome": "PASSED"} for check_id in check_ids]
    if checks != derived_checks or any(
        type(check) is not dict or set(check) != _PRODUCTION_OBSERVATION_CHECK_FIELDS for check in checks
    ):
        raise ExactReleaseEvidenceError("production_observation_result_not_machine_derived")
    return value


def _production_bundle_from_receipt(
    raw: bytes,
    *,
    identity: ReleaseIdentity,
    journey_id: str,
) -> EvidenceBundle:
    receipt = _validate_production_observation_receipt_structure(
        raw,
        expected_release=identity,
        expected_journey_id=journey_id,
    )
    result = "VERIFIED"
    receipt_ref = _evidence_ref(
        identity=identity,
        journey_id=journey_id,
        evidence_class=_PRODUCTION_OBSERVATION_CLASS,
        result=result,
        kind="receipts",
    )
    receipt_sha256 = hashlib.sha256(raw).hexdigest()
    manifest_ref = _evidence_ref(
        identity=identity,
        journey_id=journey_id,
        evidence_class=_PRODUCTION_OBSERVATION_CLASS,
        result=result,
        kind="manifests",
    )
    manifest_value = {
        "$schema": PRODUCTION_OBSERVATION_MANIFEST_SCHEMA,
        "evidence_class": _PRODUCTION_OBSERVATION_CLASS,
        "journey_id": journey_id,
        "observation": {
            "artifact_ref": receipt_ref,
            "artifact_schema": PRODUCTION_OBSERVATION_RECEIPT_SCHEMA,
            "artifact_sha256": receipt_sha256,
            "check_ids": receipt["check_ids"],
            "environment": receipt["environment"],
        },
        "release": _release_payload(identity),
        "result": result,
    }
    manifest = canonical_json_bytes(manifest_value)
    loaded_manifest = _load_canonical_manifest(manifest)
    observation = loaded_manifest.get("observation")
    if (
        set(loaded_manifest) != _MANIFEST_FIELDS
        or type(observation) is not dict
        or set(observation) != _PRODUCTION_OBSERVATION_MANIFEST_OBSERVATION_FIELDS
    ):
        raise ExactReleaseEvidenceError("manifest_fields_invalid")
    return EvidenceBundle(
        receipt_ref=receipt_ref,
        receipt=raw,
        receipt_sha256=receipt_sha256,
        manifest_ref=manifest_ref,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        result=result,
        authority=_EVIDENCE_BUNDLE_AUTHORITY,
    )


def _bundle_from_receipt(
    raw: bytes,
    *,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
) -> EvidenceBundle:
    if evidence_class == _PRODUCTION_OBSERVATION_CLASS:
        return _production_bundle_from_receipt(
            raw,
            identity=identity,
            journey_id=journey_id,
        )
    receipt = _load_canonical_receipt(raw)
    expected_release = _release_payload(identity)
    result = receipt.get("result")
    expected_schema = receipt_schema(evidence_class, journey_id=journey_id)
    if (
        set(receipt) != _RECEIPT_FIELDS
        or receipt.get("$schema") != expected_schema
        or receipt.get("journey_id") != journey_id
        or receipt.get("evidence_class") != evidence_class
        or receipt.get("environment") != ENVIRONMENT_BY_CLASS.get(evidence_class)
        or receipt.get("check_ids") != _check_ids(journey_id, evidence_class)
        or receipt.get("release") != expected_release
        or result not in {"VERIFIED", "FAILED"}
    ):
        raise ExactReleaseEvidenceError("bundle_receipt_invalid")
    observed_at = receipt.get("observed_at_utc")
    if (
        type(observed_at) is not str
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            observed_at,
        )
        is None
    ):
        raise ExactReleaseEvidenceError("bundle_receipt_invalid")
    try:
        datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ExactReleaseEvidenceError("bundle_receipt_invalid") from exc
    receipt_ref = _evidence_ref(
        identity=identity,
        journey_id=journey_id,
        evidence_class=evidence_class,
        result=result,
        kind="receipts",
    )
    receipt_sha256 = hashlib.sha256(raw).hexdigest()
    manifest_ref = _evidence_ref(
        identity=identity,
        journey_id=journey_id,
        evidence_class=evidence_class,
        result=result,
        kind="manifests",
    )
    manifest_value = {
        "$schema": MANIFEST_SCHEMA,
        "evidence_class": evidence_class,
        "journey_id": journey_id,
        "observation": {
            "artifact_ref": receipt_ref,
            "artifact_schema": expected_schema,
            "artifact_sha256": receipt_sha256,
            "check_ids": receipt["check_ids"],
            "environment": receipt["environment"],
            "observed_at_utc": observed_at,
        },
        "release": expected_release,
        "result": result,
    }
    manifest = canonical_json_bytes(manifest_value)
    loaded_manifest = _load_canonical_manifest(manifest)
    observation = loaded_manifest.get("observation")
    if (
        set(loaded_manifest) != _MANIFEST_FIELDS
        or type(observation) is not dict
        or set(observation) != _MANIFEST_OBSERVATION_FIELDS
    ):
        raise ExactReleaseEvidenceError("manifest_fields_invalid")
    return EvidenceBundle(
        receipt_ref=receipt_ref,
        receipt=raw,
        receipt_sha256=receipt_sha256,
        manifest_ref=manifest_ref,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        result=result,
        authority=_EVIDENCE_BUNDLE_AUTHORITY,
    )


_TRUSTED_GIT_STATUS_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_uid",
    "st_gid",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _trusted_git_executable() -> tuple[Path, os.stat_result]:
    try:
        executable = _TRUSTED_GIT_PATH.resolve(strict=True)
        status = executable.lstat()
        if (
            executable != _TRUSTED_GIT_PATH
            or not stat.S_ISREG(status.st_mode)
            or status.st_uid != 0
            or status.st_nlink != 1
            or status.st_mode & 0o022
            or not status.st_mode & 0o111
        ):
            raise ExactReleaseEvidenceError("git_authority_invalid")
        for directory in executable.parents:
            directory_status = directory.lstat()
            if (
                not stat.S_ISDIR(directory_status.st_mode)
                or directory_status.st_uid != 0
                or directory_status.st_mode & 0o022
            ):
                raise ExactReleaseEvidenceError("git_authority_invalid")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("git_authority_invalid") from exc
    return executable, status


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        executable, before = _trusted_git_executable()
        root = Path(repo_root).resolve(strict=True)
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise ExactReleaseEvidenceError("git_identity_unavailable")
        repository_options: tuple[str, ...] = ("-c", "core.fsmonitor=false")
        if "clone" not in args:
            repository_options += ("-c", f"core.worktree={root}")
        completed = subprocess.run(
            (
                str(executable),
                "--no-pager",
                "--no-replace-objects",
                *repository_options,
                *args,
            ),
            cwd=root,
            env=dict(_TRUSTED_GIT_ENVIRONMENT),
            check=False,
            capture_output=True,
            timeout=30,
        )
        after = executable.lstat()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise ExactReleaseEvidenceError("git_identity_unavailable") from exc
    if (
        completed.returncode != 0
        or completed.stderr
        or any(getattr(before, field) != getattr(after, field) for field in _TRUSTED_GIT_STATUS_FIELDS)
    ):
        raise ExactReleaseEvidenceError("git_identity_unavailable")
    return completed.stdout


def _resolve_directory(path: Path, failure_code: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ExactReleaseEvidenceError(failure_code) from exc
    if not resolved.is_dir():
        raise ExactReleaseEvidenceError(failure_code)
    return resolved


def _exact_git_blob(repo_root: Path, commit: str, path: str) -> bytes:
    candidate = PurePosixPath(path)
    if (
        candidate.is_absolute()
        or str(candidate) != path
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ExactReleaseEvidenceError("git_blob_invalid")
    name = f"{commit}:{path}"
    if _git(repo_root, "--no-replace-objects", "cat-file", "-t", name) != b"blob\n":
        raise ExactReleaseEvidenceError("git_blob_invalid")
    return _git(repo_root, "--no-replace-objects", "cat-file", "blob", name)


def _test_source(repo_root: Path, commit: str, test_ref: str) -> bytes:
    if test_ref.count("::") != 1:
        raise ExactReleaseEvidenceError("test_ref_invalid")
    path, name = test_ref.split("::")
    function_name, separator, parameter_id = name.partition("[")
    parameter_valid = not separator or (
        name.endswith("]") and _PARAMETER_ID.fullmatch(parameter_id[:-1]) is not None
    )
    if (
        _TEST_PATH.fullmatch(path) is None
        or _TEST_NAME.fullmatch(function_name) is None
        or not parameter_valid
    ):
        raise ExactReleaseEvidenceError("test_ref_invalid")
    raw = _exact_git_blob(repo_root, commit, path)
    try:
        module = ast.parse(raw.decode("utf-8", errors="strict"), filename=f"{commit}:{path}")
    except (UnicodeError, SyntaxError) as exc:
        raise ExactReleaseEvidenceError("test_ref_invalid") from exc
    names = {node.name for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if function_name not in names:
        raise ExactReleaseEvidenceError("test_ref_invalid")
    return raw


def _check_ids(journey_id: str, evidence_class: str) -> list[str]:
    try:
        values = sorted(f"{journey_id}.{suffix}" for suffix in _CHECK_SUFFIXES[evidence_class])
    except KeyError as exc:
        raise ExactReleaseEvidenceError("evidence_class_invalid") from exc
    if _SAFE_ID.fullmatch(journey_id) is None or any(_SAFE_ID.fullmatch(value) is None for value in values):
        raise ExactReleaseEvidenceError("check_id_invalid")
    return values


def _owner_smoke_payload(value: AuthenticatedOwnerSmokeBinding | None) -> dict[str, str] | None:
    if value is None:
        return None
    if type(value) is not AuthenticatedOwnerSmokeBinding:
        raise ExactReleaseEvidenceError("owner_smoke_not_authenticated")
    if (
        type(value.schema) is not str
        or type(value.authority) is not str
        or type(value.artifact_ref) is not str
        or type(value.artifact_sha256) is not str
    ):
        raise ExactReleaseEvidenceError("owner_smoke_binding_invalid")
    path = PurePosixPath(value.artifact_ref)
    if (
        not value.schema.startswith("friday.")
        or _SAFE_ID.fullmatch(value.authority) is None
        or _SHA256.fullmatch(value.artifact_sha256) is None
        or path.is_absolute()
        or str(path) != value.artifact_ref
        or not path.parts
        or path.parts[0] != "evidence"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ExactReleaseEvidenceError("owner_smoke_binding_invalid")
    return {
        "artifact_ref": value.artifact_ref,
        "artifact_sha256": value.artifact_sha256,
        "authority": value.authority,
        "schema": value.schema,
    }


def _production_digest(value: object, *, failure_code: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None or set(value) == {"0"}:
        raise ExactReleaseEvidenceError(failure_code)
    return value


def _production_count(value: object, *, failure_code: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_PRODUCTION_AGGREGATE:
        raise ExactReleaseEvidenceError(failure_code)
    return value


def _production_counts(
    value: object,
    *,
    names: tuple[str, ...],
    failure_code: str,
) -> dict[str, int]:
    if type(value) is not dict or set(value) != set(names):
        raise ExactReleaseEvidenceError(failure_code)
    return {name: _production_count(value.get(name), failure_code=failure_code) for name in names}


def _validate_production_endpoint_response(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
    expected_challenge_sha256: str,
    expected_process_epoch_sha256: str,
) -> dict[str, Any]:
    """Validate the exact body-free response without granting it authority."""

    release = _release_payload(expected_release)
    challenge = _production_digest(
        expected_challenge_sha256,
        failure_code="production_observation_challenge_invalid",
    )
    process_epoch = _production_digest(
        expected_process_epoch_sha256,
        failure_code="production_observation_process_epoch_invalid",
    )
    value = _load_canonical_object(
        raw,
        maximum_bytes=_PRODUCTION_OBSERVATION_MAX_BYTES,
        failure_code="production_observation_response_invalid",
    )
    database = value.get("database")
    scheduled_work = value.get("scheduled_work")
    if (
        set(value) != _PRODUCTION_RESPONSE_FIELDS
        or value.get("schema") != PRODUCTION_READ_ONLY_OBSERVATION_SCHEMA
        or value.get("challenge_sha256") != challenge
        or value.get("backend_process_epoch_sha256") != process_epoch
        or value.get("backend_lease_owned") is not True
        or type(value.get("hard_contradictions")) is not int
        or value.get("hard_contradictions") != 0
        or type(database) is not dict
        or set(database) != _PRODUCTION_DATABASE_FIELDS
        or type(database.get("schema_version")) is not int
        or database.get("schema_version") != release["database_schema"]
        or database.get("schema_version") != 50
        or database.get("integrity") != "ok"
        or type(database.get("foreign_key_violations")) is not int
        or database.get("foreign_key_violations") != 0
        or type(scheduled_work) is not dict
        or set(scheduled_work) != _PRODUCTION_SCHEDULED_WORK_FIELDS
    ):
        raise ExactReleaseEvidenceError("production_observation_response_invalid")
    if (
        _production_digest(
            database.get("schema_attestation_sha256"),
            failure_code="production_observation_schema_attestation_invalid",
        )
        != PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256
    ):
        raise ExactReleaseEvidenceError("production_observation_schema_attestation_invalid")
    _production_counts(
        scheduled_work.get("missions"),
        names=_PRODUCTION_MISSION_STATES,
        failure_code="production_observation_aggregate_invalid",
    )
    _production_counts(
        scheduled_work.get("mission_tasks"),
        names=_PRODUCTION_TASK_STATES,
        failure_code="production_observation_aggregate_invalid",
    )
    _production_counts(
        scheduled_work.get("reminders"),
        names=_PRODUCTION_REMINDER_STATES,
        failure_code="production_observation_aggregate_invalid",
    )
    workers = scheduled_work.get("workers")
    if type(workers) is not dict or set(workers) != _PRODUCTION_WORKER_FIELDS:
        raise ExactReleaseEvidenceError("production_observation_aggregate_invalid")
    present = _production_count(
        workers.get("present"),
        failure_code="production_observation_aggregate_invalid",
    )
    missing = _production_count(
        workers.get("missing"),
        failure_code="production_observation_aggregate_invalid",
    )
    health_states = _production_counts(
        workers.get("health_states"),
        names=_PRODUCTION_WORKER_STATES,
        failure_code="production_observation_aggregate_invalid",
    )
    if present + missing != 2 or sum(health_states.values()) != present:
        raise ExactReleaseEvidenceError("production_observation_aggregate_invalid")
    return value


def _production_binding_payload(
    value: AuthenticatedProductionObservationBinding,
) -> dict[str, str]:
    if type(value) is not AuthenticatedProductionObservationBinding:
        raise ExactReleaseEvidenceError("production_observation_not_authenticated")
    if type(value.release) is not ReleaseIdentity or type(value.endpoint_response) is not bytes:
        raise ExactReleaseEvidenceError("production_observation_binding_invalid")
    _validate_production_endpoint_response(
        value.endpoint_response,
        expected_release=value.release,
        expected_challenge_sha256=value.challenge_sha256,
        expected_process_epoch_sha256=value.backend_process_epoch_sha256,
    )
    health_before = _production_digest(
        value.health_before_sha256,
        failure_code="production_observation_health_invalid",
    )
    health_after = _production_digest(
        value.health_after_sha256,
        failure_code="production_observation_health_invalid",
    )
    return {
        "backend_process_epoch_sha256": value.backend_process_epoch_sha256,
        "challenge_sha256": value.challenge_sha256,
        "endpoint_response_schema": PRODUCTION_READ_ONLY_OBSERVATION_SCHEMA,
        "endpoint_response_sha256": hashlib.sha256(value.endpoint_response).hexdigest(),
        "health_after_sha256": health_after,
        "health_before_sha256": health_before,
        "release_binding_sha256": release_binding_sha256(value.release),
    }


def binding_from_release_captain_artifact(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
) -> AuthenticatedProductionObservationBinding:
    """Decode one canonical Release Captain artifact into expected values.

    This helper verifies transport integrity and exact release binding; it does
    not turn construction or parsing into authentication.  Callers must first
    establish the artifact's external Release Captain authority.
    """

    expected_release_payload = _release_payload(expected_release)
    value = _load_canonical_object(
        raw,
        maximum_bytes=_RELEASE_CAPTAIN_ARTIFACT_MAX_BYTES,
        failure_code="release_captain_observation_artifact_invalid",
    )
    response = value.get("endpoint_response")
    if (
        set(value) != _RELEASE_CAPTAIN_ARTIFACT_FIELDS
        or value.get("schema") != RELEASE_CAPTAIN_PRODUCTION_OBSERVATION_SCHEMA
        or value.get("release") != expected_release_payload
        or value.get("release_binding_sha256") != release_binding_sha256(expected_release)
        or type(response) is not dict
    ):
        raise ExactReleaseEvidenceError("release_captain_observation_artifact_invalid")
    endpoint_response = canonical_json_bytes(response)
    if value.get("endpoint_response_sha256") != hashlib.sha256(endpoint_response).hexdigest():
        raise ExactReleaseEvidenceError("release_captain_observation_artifact_invalid")
    challenge_sha256 = _production_digest(
        value.get("challenge_sha256"),
        failure_code="release_captain_observation_artifact_invalid",
    )
    process_epoch_sha256 = _production_digest(
        value.get("backend_process_epoch_sha256"),
        failure_code="release_captain_observation_artifact_invalid",
    )
    health_before_sha256 = _production_digest(
        value.get("health_before_sha256"),
        failure_code="release_captain_observation_artifact_invalid",
    )
    health_after_sha256 = _production_digest(
        value.get("health_after_sha256"),
        failure_code="release_captain_observation_artifact_invalid",
    )
    binding = AuthenticatedProductionObservationBinding(
        release=expected_release,
        endpoint_response=endpoint_response,
        challenge_sha256=challenge_sha256,
        backend_process_epoch_sha256=process_epoch_sha256,
        health_before_sha256=health_before_sha256,
        health_after_sha256=health_after_sha256,
    )
    expected = _production_binding_payload(binding)
    if any(value.get(key) != item for key, item in expected.items() if key != "endpoint_response_schema"):
        raise ExactReleaseEvidenceError("release_captain_observation_artifact_invalid")
    return binding


def _require_neutralized_ignored_files(repo_root: Path) -> None:
    raw = _git(
        repo_root,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--full-name",
        "-z",
        "--",
    )
    if raw and not raw.endswith(b"\0"):
        raise ExactReleaseEvidenceError("checkout_ignored_artifact")
    for encoded in raw[:-1].split(b"\0") if raw else ():
        try:
            path = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ExactReleaseEvidenceError("checkout_ignored_artifact") from exc
        candidate = PurePosixPath(path)
        valid = bool(
            path
            and not candidate.is_absolute()
            and str(candidate) == path
            and all(part not in {"", ".", ".."} for part in candidate.parts)
        )
        regular_without_symlink_parent = False
        if valid:
            try:
                parent = repo_root
                parent_is_exact = True
                for part in candidate.parts[:-1]:
                    parent /= part
                    parent_is_exact = parent_is_exact and stat.S_ISDIR(parent.lstat().st_mode)
                regular_without_symlink_parent = parent_is_exact and stat.S_ISREG(
                    (repo_root / path).lstat().st_mode
                )
            except OSError:
                regular_without_symlink_parent = False
        inert_root_cache = len(candidate.parts) > 1 and candidate.parts[0] in {
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
        }
        if not regular_without_symlink_parent or not inert_root_cache:
            raise ExactReleaseEvidenceError("checkout_ignored_artifact")


def _require_exact_checkout(repo_root: Path, commit: str) -> None:
    try:
        head = _git(repo_root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except UnicodeError as exc:
        raise ExactReleaseEvidenceError("git_identity_unavailable") from exc
    status = _git(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if head != commit or status:
        raise ExactReleaseEvidenceError("checkout_not_exact_clean_commit")
    _require_neutralized_ignored_files(repo_root)


def _require_running_producer(repo_root: Path, commit: str) -> None:
    producer_blob = _exact_git_blob(repo_root, commit, PRODUCER_PATH)
    producer_path = repo_root / PRODUCER_PATH
    try:
        producer_bytes = producer_path.read_bytes()
        running_producer = Path(__file__).resolve(strict=True)
        repository_producer = producer_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExactReleaseEvidenceError("producer_source_invalid") from exc
    if running_producer != repository_producer or producer_bytes != producer_blob:
        raise ExactReleaseEvidenceError("producer_source_invalid")


_AUTHENTICATED_PRODUCER_HELPERS: dict[
    str,
    tuple[object, str, object, object, tuple[tuple[str, object], ...], str],
] = {}


def _require_producer_process_authority() -> None:
    """Require the source controller's stdlib-only isolated startup boundary."""

    try:
        stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
        expected_initial = (
            str((stdlib.parent / f"python{sys.version_info.major}{sys.version_info.minor}.zip").resolve()),
            str(stdlib),
            str((stdlib / "lib-dynload").resolve(strict=True)),
        )
        normalized_initial = tuple(
            str(Path(value).resolve(strict=False)) for value in _INITIAL_PRODUCER_SYS_PATH
        )
        tooling_site = Path(sysconfig.get_path("purelib")).resolve(strict=True)
        initial_paths = tuple(Path(value).resolve(strict=False) for value in _INITIAL_PRODUCER_SYS_PATH)
        expected_current = (str(ROOT), *_INITIAL_PRODUCER_SYS_PATH)
        invalid = bool(
            sys.flags.isolated != 1
            or sys.flags.no_site != 1
            or sys.flags.no_user_site != 1
            or sys.flags.ignore_environment != 1
            or sys.flags.dont_write_bytecode != 1
            or not sys.flags.safe_path
            or _INITIAL_PRODUCER_SITE_LOADED
            or "site" in sys.modules
            or normalized_initial != expected_initial
            or tuple(sys.path) != expected_current
            or tooling_site in initial_paths
            or bool(_FORBIDDEN_PRODUCER_STARTUP_ENVIRONMENT.intersection(os.environ))
            or any(name.startswith(("PYTHON", "LD_", "DYLD_", "GLIBC_")) for name in os.environ)
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ExactReleaseEvidenceError("producer_process_authority_invalid") from exc
    if invalid:
        raise ExactReleaseEvidenceError("producer_process_authority_invalid")


def _running_exact_checkout(*, require_isolated_startup: bool = True) -> tuple[Path, str]:
    if require_isolated_startup:
        _require_producer_process_authority()
    root = _resolve_directory(ROOT, "repo_root_invalid")
    try:
        head = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except UnicodeError as exc:
        raise ExactReleaseEvidenceError("git_identity_unavailable") from exc
    if _COMMIT.fullmatch(head) is None:
        raise ExactReleaseEvidenceError("git_identity_unavailable")
    _require_exact_checkout(root, head)
    _require_running_producer(root, head)
    return root, head


def _authenticated_producer_helper(
    module_name: str,
    relative_path: str,
    *,
    require_isolated_startup: bool = True,
) -> Any:
    """Execute one helper only from its authenticated tracked source bytes."""

    cached = _AUTHENTICATED_PRODUCER_HELPERS.get(module_name)
    root, head = _running_exact_checkout(require_isolated_startup=require_isolated_startup)
    source_path = root / relative_path
    expected = _exact_git_blob(root, head, relative_path)
    try:
        if source_path.resolve(strict=True) != source_path or source_path.read_bytes() != expected:
            raise ExactReleaseEvidenceError("producer_helper_invalid")
        if cached is not None:
            module, cached_head, loader, spec, callables, source_sha256 = cached
            if (
                cached_head != head
                or sys.modules.get(module_name) is not module
                or getattr(module, "__loader__", None) is not loader
                or getattr(module, "__spec__", None) is not spec
                or getattr(getattr(module, "__spec__", None), "loader", None) is not loader
                or getattr(module, "__authenticated_source_sha256__", None) != source_sha256
                or any(getattr(module, name, None) is not value for name, value in callables)
            ):
                raise ExactReleaseEvidenceError("producer_helper_invalid")
            return module
        if module_name in sys.modules:
            raise ExactReleaseEvidenceError("producer_helper_preloaded")
        code = compile(expected, str(source_path), "exec", dont_inherit=True)
        module = type(sys)(module_name)
        module.__file__ = str(source_path)
        module.__package__ = module_name.rpartition(".")[0]
        loader = importlib.machinery.SourceFileLoader(module_name, str(source_path))
        module.__loader__ = loader
        module.__spec__ = importlib.machinery.ModuleSpec(
            module_name,
            loader=loader,
            origin=str(source_path),
        )
        original_path = sys.path[:]
        sys.modules[module_name] = module
        try:
            sys.path[:] = [
                entry
                for entry in original_path
                if not (
                    (candidate := Path(entry or os.getcwd()).resolve(strict=False)) == root
                    or candidate.is_relative_to(root)
                )
            ]
            exec(code, module.__dict__)  # noqa: S102 - exact Git blob compiled above
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        finally:
            sys.path[:] = original_path
        _require_exact_checkout(root, head)
        _require_running_producer(root, head)
        if (
            source_path.read_bytes() != expected
            or sys.modules.get(module_name) is not module
            or getattr(module, "__loader__", None) is not loader
            or getattr(getattr(module, "__spec__", None), "loader", None) is not loader
        ):
            raise ExactReleaseEvidenceError("producer_helper_invalid")
        source_sha256 = hashlib.sha256(expected).hexdigest()
        module.__dict__["__authenticated_source_sha256__"] = source_sha256
        callables = tuple(
            sorted(
                ((name, value) for name, value in vars(module).items() if callable(value)),
                key=lambda item: item[0],
            )
        )
        _AUTHENTICATED_PRODUCER_HELPERS[module_name] = (
            module,
            head,
            loader,
            module.__spec__,
            callables,
            source_sha256,
        )
        return module
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("producer_helper_invalid") from exc


def _authenticated_quality_gate(*, require_isolated_startup: bool = True) -> Any:
    module_name = (
        "tools.quality_gate" if require_isolated_startup else "_friday_direct_validation_quality_gate"
    )
    return _authenticated_producer_helper(
        module_name,
        "tools/quality_gate.py",
        require_isolated_startup=require_isolated_startup,
    )


def _authenticated_release_operator() -> Any:
    return _authenticated_producer_helper(
        "tools.immutable_release_operator",
        "tools/immutable_release_operator.py",
    )


@contextmanager
def _validation_source_checkout(
    repo_root: Path,
    source_commit: str,
) -> Iterator[tuple[Path, bool]]:
    """Yield an exact source tree while keeping a later validator HEAD valid."""

    root = _resolve_directory(repo_root, "repo_root_invalid")
    try:
        current_head = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except UnicodeError as exc:
        raise ExactReleaseEvidenceError("git_identity_unavailable") from exc
    if _COMMIT.fullmatch(current_head) is None:
        raise ExactReleaseEvidenceError("git_identity_unavailable")
    _require_exact_checkout(root, current_head)
    _require_running_producer(root, current_head)
    if current_head == source_commit:
        yield root, True
        return
    if _COMMIT.fullmatch(source_commit) is None:
        raise ExactReleaseEvidenceError("release_identity_invalid")

    try:
        with tempfile.TemporaryDirectory(prefix="friday-exact-source-") as temporary:
            detached = Path(temporary) / "repository"
            _git(
                root,
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                "--quiet",
                "--shared",
                "--no-checkout",
                "--",
                str(root),
                str(detached),
            )
            _git(
                detached,
                "-c",
                "core.hooksPath=/dev/null",
                "checkout",
                "--quiet",
                "--detach",
                source_commit,
            )
            _require_exact_checkout(detached, source_commit)
            yield detached, False
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("source_checkout_unavailable") from exc


def _source_proofs(
    repo_root: Path,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
    *,
    require_running_producer: bool = True,
) -> tuple[str, list[dict[str, str]]]:
    producer_blob = _exact_git_blob(repo_root, identity.source_commit, PRODUCER_PATH)
    if require_running_producer:
        _require_running_producer(repo_root, identity.source_commit)
    proofs = []
    for test_ref in proof_refs(journey_id, evidence_class):
        source = _test_source(repo_root, identity.source_commit, test_ref)
        path = repo_root / test_ref.split("::", maxsplit=1)[0]
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise ExactReleaseEvidenceError("test_source_not_exact") from exc
        if current != source:
            raise ExactReleaseEvidenceError("test_source_not_exact")
        proofs.append(
            {
                "outcome": "",
                "runner": "pytest",
                "test_ref": test_ref,
                "test_source_sha256": hashlib.sha256(source).hexdigest(),
            }
        )
    return hashlib.sha256(producer_blob).hexdigest(), proofs


def _xml_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def _pytest_outcomes(
    report_path: Path,
    expected: tuple[str, ...],
    *,
    gate: Any | None = None,
) -> tuple[str, ...]:
    exact_gate = _authenticated_quality_gate() if gate is None else gate
    try:
        summary = exact_gate.junit_summary(report_path)
        root = ET.parse(report_path).getroot()
    except (OSError, RuntimeError, ET.ParseError, ValueError) as exc:
        raise ExactReleaseEvidenceError("pytest_report_invalid") from exc
    if summary.errors or summary.skipped or summary.nodeids != expected:
        raise ExactReleaseEvidenceError("pytest_report_invalid")
    outcomes: dict[str, str] = {}
    for testcase in (element for element in root.iter() if _xml_name(element.tag) == "testcase"):
        values = [
            item.attrib.get("value")
            for item in testcase.iter()
            if _xml_name(item.tag) == "property" and item.attrib.get("name") == "friday_nodeid"
        ]
        failures = [child for child in testcase if _xml_name(child.tag) == "failure"]
        if len(values) != 1 or values[0] in outcomes or len(failures) > 1:
            raise ExactReleaseEvidenceError("pytest_report_invalid")
        outcomes[str(values[0])] = "FAILED" if failures else "PASSED"
    if (
        tuple(outcomes) != expected
        or sum(value == "FAILED" for value in outcomes.values()) != summary.failures
    ):
        raise ExactReleaseEvidenceError("pytest_report_invalid")
    return tuple(outcomes[nodeid] for nodeid in expected)


def _execution_witness(
    outcomes: tuple[str, ...],
    exit_code: int,
    collection_sha256: str,
    outcome_projection_sha256: str,
    *,
    artifact_origin_sha256: str | None = None,
    interpreter_ref: str | None = None,
    site_packages_ref: str | None = None,
    subprocess_policy: str | None = None,
    tooling_modules_sha256: str | None = None,
    tooling_policy: str | None = None,
    tooling_snapshot_sha256: str | None = None,
) -> _ExecutionWitness:
    artifact_values = (
        artifact_origin_sha256,
        interpreter_ref,
        site_packages_ref,
        subprocess_policy,
        tooling_modules_sha256,
        tooling_policy,
        tooling_snapshot_sha256,
    )
    artifact_valid = all(value is None for value in artifact_values) or (
        type(artifact_origin_sha256) is str
        and _SHA256.fullmatch(artifact_origin_sha256) is not None
        and interpreter_ref == _INTERPRETER_REF
        and type(site_packages_ref) is str
        and re.fullmatch(r"venv/lib/python[0-9]+\.[0-9]+/site-packages", site_packages_ref) is not None
        and subprocess_policy == _SUBPROCESS_POLICY
        and type(tooling_modules_sha256) is str
        and _SHA256.fullmatch(tooling_modules_sha256) is not None
        and tooling_policy == _TEST_TOOLING_POLICY
        and type(tooling_snapshot_sha256) is str
        and _SHA256.fullmatch(tooling_snapshot_sha256) is not None
    )
    if (
        type(outcomes) is not tuple
        or not outcomes
        or any(outcome not in {"PASSED", "FAILED"} for outcome in outcomes)
        or type(exit_code) is not int
        or exit_code != (0 if all(outcome == "PASSED" for outcome in outcomes) else 1)
        or type(collection_sha256) is not str
        or type(outcome_projection_sha256) is not str
        or _SHA256.fullmatch(collection_sha256) is None
        or _SHA256.fullmatch(outcome_projection_sha256) is None
        or not artifact_valid
    ):
        raise ExactReleaseEvidenceError("pytest_execution_evidence_invalid")
    return _ExecutionWitness(
        outcomes,
        exit_code,
        collection_sha256,
        outcome_projection_sha256,
        _EXECUTION_WITNESS_AUTHORITY,
        artifact_origin_sha256,
        interpreter_ref,
        site_packages_ref,
        subprocess_policy,
        tooling_modules_sha256,
        tooling_policy,
        tooling_snapshot_sha256,
    )


def _require_execution_witness(value: object) -> _ExecutionWitness:
    if type(value) is not _ExecutionWitness or value.authority is not _EXECUTION_WITNESS_AUTHORITY:
        raise ExactReleaseEvidenceError("pytest_execution_evidence_invalid")
    return _execution_witness(
        value.outcomes,
        value.exit_code,
        value.collection_sha256,
        value.outcome_projection_sha256,
        artifact_origin_sha256=value.artifact_origin_sha256,
        interpreter_ref=value.interpreter_ref,
        site_packages_ref=value.site_packages_ref,
        subprocess_policy=value.subprocess_policy,
        tooling_modules_sha256=value.tooling_modules_sha256,
        tooling_policy=value.tooling_policy,
        tooling_snapshot_sha256=value.tooling_snapshot_sha256,
    )


def _outcome_projection_sha256(nodeids: tuple[str, ...], outcomes: tuple[str, ...]) -> str:
    """Hash the deterministic outcome projection derived from strict JUnit."""

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "nodeids": list(nodeids),
                "outcomes": list(outcomes),
                "version": 1,
            }
        )
    ).hexdigest()


def _artifact_origin_report_sha256(
    path: Path,
    runtime: _AuthenticatedReleaseRuntime,
) -> tuple[str, str]:
    try:
        raw = _stable_file(path, 4096)
        value = json.loads(
            raw.decode("ascii", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ExactReleaseEvidenceError("artifact_origin_report_invalid")
            ),
        )
    except (OSError, UnicodeError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ExactReleaseEvidenceError("artifact_origin_report_invalid") from exc
    fields = {
        "module_count",
        "module_origins_sha256",
        "interpreter_ref",
        "schema",
        "site_packages_ref",
        "source_commit",
        "subprocess_policy",
        "tooling_module_count",
        "tooling_modules_sha256",
        "tooling_policy",
        "wheel_sha256",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or raw != canonical_json_bytes(value)
        or value.get("schema") != "friday.clean-artifact-import-origin.v1"
        or type(value.get("module_count")) is not int
        or not 1 <= value["module_count"] <= 10_000
        or _SHA256.fullmatch(str(value.get("module_origins_sha256") or "")) is None
        or value.get("interpreter_ref") != runtime.interpreter_ref
        or value.get("site_packages_ref") != runtime.site_packages_ref
        or value.get("source_commit") != runtime.identity.source_commit
        or value.get("wheel_sha256") != runtime.identity.wheel_sha256
        or value.get("subprocess_policy") != _SUBPROCESS_POLICY
        or type(value.get("tooling_module_count")) is not int
        or not len(_TEST_TOOLING_MODULES) <= value["tooling_module_count"] <= 10_000
        or _SHA256.fullmatch(str(value.get("tooling_modules_sha256") or "")) is None
        or value.get("tooling_policy") != _TEST_TOOLING_POLICY
    ):
        raise ExactReleaseEvidenceError("artifact_origin_report_invalid")
    return hashlib.sha256(raw).hexdigest(), str(value["tooling_modules_sha256"])


def _test_tooling_site(repo_root: Path, release_root: Path | None) -> Path:
    """Locate tooling explicitly in the producer venv without importing ``site``."""

    source = _resolve_directory(repo_root, "test_tooling_invalid")
    release = None if release_root is None else _resolve_directory(release_root, "test_tooling_invalid")
    try:
        root = (
            Path(sysconfig.get_path("purelib"))
            if _NATIVE_VALIDATION_TOOLING_SITE is None
            else _NATIVE_VALIDATION_TOOLING_SITE
        )
        if root.resolve(strict=True) != root or not stat.S_ISDIR(root.lstat().st_mode):
            raise ExactReleaseEvidenceError("test_tooling_invalid")
        for name in _TEST_TOOLING_MODULES:
            spec = importlib.machinery.PathFinder.find_spec(name, [str(root)])
            raw_origin = None if spec is None else spec.origin
            if type(raw_origin) is not str or raw_origin in {"built-in", "frozen"}:
                raise ExactReleaseEvidenceError("test_tooling_invalid")
            assert spec is not None
            origin = Path(raw_origin)
            resolved_origin = origin.resolve(strict=True)
            locations = tuple(spec.submodule_search_locations or ())
            if origin != resolved_origin or not stat.S_ISREG(origin.lstat().st_mode) or len(locations) != 1:
                raise ExactReleaseEvidenceError("test_tooling_invalid")
            package = Path(locations[0])
            resolved_package = package.resolve(strict=True)
            if (
                package != resolved_package
                or package != origin.parent
                or package.name != name
                or not stat.S_ISDIR(package.lstat().st_mode)
            ):
                raise ExactReleaseEvidenceError("test_tooling_invalid")
            if package.parent != root:
                raise ExactReleaseEvidenceError("test_tooling_invalid")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("test_tooling_invalid") from exc
    try:
        if (
            root == source
            or root.is_relative_to(source)
            or source.is_relative_to(root)
            or (
                release is not None
                and (root == release or root.is_relative_to(release) or release.is_relative_to(root))
            )
        ):
            raise ExactReleaseEvidenceError("test_tooling_invalid")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("test_tooling_invalid") from exc
    return root


_TOOLING_STABLE_STATUS_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_uid",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _same_tooling_status(left: os.stat_result, right: os.stat_result) -> bool:
    return all(getattr(left, field) == getattr(right, field) for field in _TOOLING_STABLE_STATUS_FIELDS)


def _copy_test_tooling_entry(
    source: Path,
    destination: Path,
    budget: dict[str, int],
) -> None:
    try:
        before = source.lstat()
        if stat.S_ISDIR(before.st_mode):
            destination.mkdir(mode=0o700)
            names = tuple(
                entry.name
                for entry in sorted(os.scandir(source), key=lambda item: item.name)
                if entry.name != "__pycache__" and not entry.name.endswith((".pyc", ".pyo"))
            )
            for name in names:
                _copy_test_tooling_entry(source / name, destination / name, budget)
            after = source.lstat()
            current_names = tuple(
                entry.name
                for entry in sorted(os.scandir(source), key=lambda item: item.name)
                if entry.name != "__pycache__" and not entry.name.endswith((".pyc", ".pyo"))
            )
            if not _same_tooling_status(before, after) or current_names != names:
                raise ExactReleaseEvidenceError("test_tooling_changed")
            destination.chmod(0o500)
            return
        if not stat.S_ISREG(before.st_mode):
            raise ExactReleaseEvidenceError("test_tooling_invalid")
        budget["files"] += 1
        budget["bytes"] += before.st_size
        if (
            budget["files"] > _TEST_TOOLING_MAX_FILES
            or budget["bytes"] > _TEST_TOOLING_MAX_BYTES
            or before.st_size < 0
        ):
            raise ExactReleaseEvidenceError("test_tooling_invalid")
        source_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, source_flags)
        target_descriptor = -1
        try:
            opened = os.fstat(source_descriptor)
            if not _same_tooling_status(before, opened):
                raise ExactReleaseEvidenceError("test_tooling_changed")
            target_descriptor = os.open(destination, target_flags, 0o400)
            remaining = before.st_size
            while remaining:
                chunk = os.read(source_descriptor, min(1 << 20, remaining))
                if not chunk:
                    raise ExactReleaseEvidenceError("test_tooling_changed")
                view = memoryview(chunk)
                while view:
                    written = os.write(target_descriptor, view)
                    if written < 1:
                        raise ExactReleaseEvidenceError("test_tooling_invalid")
                    view = view[written:]
                remaining -= len(chunk)
            if os.read(source_descriptor, 1):
                raise ExactReleaseEvidenceError("test_tooling_changed")
            after = os.fstat(source_descriptor)
            if not _same_tooling_status(opened, after):
                raise ExactReleaseEvidenceError("test_tooling_changed")
            os.fchmod(target_descriptor, 0o400)
            os.fsync(target_descriptor)
        finally:
            if target_descriptor >= 0:
                os.close(target_descriptor)
            os.close(source_descriptor)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("test_tooling_invalid") from exc


def _tooling_snapshot_projection(
    snapshot: Path,
    *,
    failure_code: str,
) -> tuple[dict[str, object], ...]:
    try:
        root = snapshot.resolve(strict=True)
        if root != snapshot:
            raise ExactReleaseEvidenceError(failure_code)
        projection: list[dict[str, object]] = []
        pending = [root]
        total_bytes = 0
        while pending:
            candidate = pending.pop()
            relative = "." if candidate == root else candidate.relative_to(root).as_posix()
            before = candidate.lstat()
            base = {
                "ctime_ns": before.st_ctime_ns,
                "device": before.st_dev,
                "inode": before.st_ino,
                "mode": stat.S_IMODE(before.st_mode),
                "mtime_ns": before.st_mtime_ns,
                "nlink": before.st_nlink,
                "path": relative,
                "uid": before.st_uid,
            }
            if stat.S_ISDIR(before.st_mode):
                if stat.S_IMODE(before.st_mode) != 0o500 or before.st_uid != os.geteuid():
                    raise ExactReleaseEvidenceError(failure_code)
                children = sorted(candidate.iterdir(), key=lambda path: path.name, reverse=True)
                pending.extend(children)
                projection.append({**base, "kind": "directory", "size": before.st_size})
                continue
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_nlink != 1
                or before.st_uid != os.geteuid()
            ):
                raise ExactReleaseEvidenceError(failure_code)
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(candidate, flags)
            try:
                opened = os.fstat(descriptor)
                if not _same_tooling_status(before, opened):
                    raise ExactReleaseEvidenceError(failure_code)
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = os.read(descriptor, 1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            total_bytes += size
            if size != before.st_size or not _same_tooling_status(opened, after):
                raise ExactReleaseEvidenceError(failure_code)
            projection.append({**base, "kind": "file", "sha256": digest.hexdigest(), "size": size})
        if (
            not projection
            or len(projection) > _TEST_TOOLING_MAX_FILES * 3
            or total_bytes > _TEST_TOOLING_MAX_BYTES
        ):
            raise ExactReleaseEvidenceError(failure_code)
        return tuple(sorted(projection, key=lambda item: str(item["path"])))
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError(failure_code) from exc


def _snapshot_test_tooling(tooling_site: Path, scratch: Path) -> tuple[Path, tuple[dict[str, object], ...]]:
    """Copy the closed test-only import surface into one private read-only tree."""

    snapshot = scratch / "test-tooling"
    try:
        snapshot.mkdir(mode=0o700)
        budget = {"bytes": 0, "files": 0}
        for name in _TEST_TOOLING_SNAPSHOT_NAMES:
            package = tooling_site / name
            module = tooling_site / f"{name}.py"
            candidates = tuple(path for path in (package, module) if path.exists())
            if len(candidates) != 1:
                raise ExactReleaseEvidenceError("test_tooling_invalid")
            _copy_test_tooling_entry(candidates[0], snapshot / candidates[0].name, budget)
        for distribution in _TEST_TOOLING_SNAPSHOT_DISTRIBUTIONS:
            candidates = tuple(sorted(tooling_site.glob(f"{distribution}-*.dist-info")))
            if len(candidates) != 1:
                raise ExactReleaseEvidenceError("test_tooling_invalid")
            _copy_test_tooling_entry(candidates[0], snapshot / candidates[0].name, budget)
        snapshot.chmod(0o500)
        projection = _tooling_snapshot_projection(snapshot, failure_code="test_tooling_invalid")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("test_tooling_invalid") from exc
    return snapshot, projection


def _require_tooling_snapshot_unchanged(
    snapshot: Path,
    expected: tuple[dict[str, object], ...],
) -> None:
    if _tooling_snapshot_projection(snapshot, failure_code="test_tooling_changed") != expected:
        raise ExactReleaseEvidenceError("test_tooling_changed")


def _tooling_snapshot_content_sha256(
    projection: tuple[dict[str, object], ...],
) -> str:
    """Bind every snapshotted tooling byte without filesystem-local metadata."""

    content: list[dict[str, object]] = []
    for entry in projection:
        if entry.get("kind") == "directory":
            content.append({"kind": "directory", "path": entry["path"]})
        elif entry.get("kind") == "file":
            content.append(
                {
                    "kind": "file",
                    "path": entry["path"],
                    "sha256": entry["sha256"],
                    "size": entry["size"],
                }
            )
        else:
            raise ExactReleaseEvidenceError("test_tooling_invalid")
    return hashlib.sha256(canonical_json_bytes({"entries": content, "version": 1})).hexdigest()


def _sealed_pytest_environment(
    base_environment: dict[str, str],
    runtime: _AuthenticatedReleaseRuntime,
    scratch: Path,
    python_cache: Path,
) -> dict[str, str]:
    """Build the complete environment accepted by the sealed interpreter."""

    try:
        resolved_scratch = scratch.resolve(strict=True)
        scratch_status = resolved_scratch.stat()
        if (
            scratch != resolved_scratch
            or python_cache.parent != resolved_scratch
            or python_cache.resolve(strict=False) != python_cache
            or os.path.lexists(python_cache)
            or not stat.S_ISDIR(scratch_status.st_mode)
            or scratch_status.st_uid != os.geteuid()
            or stat.S_IMODE(scratch_status.st_mode) != 0o700
        ):
            raise ExactReleaseEvidenceError("sealed_pytest_environment_invalid")
        inherited = {name: base_environment[name] for name in _SEALED_CHILD_INHERITED_ENVIRONMENT}
        if any(type(value) is not str for value in inherited.values()):
            raise ExactReleaseEvidenceError("sealed_pytest_environment_invalid")
        home = Path(inherited["FRIDAY_HOME"])
        if (
            not inherited["FRIDAY_HOME"]
            or inherited["JERICHO_HOME"] != inherited["FRIDAY_HOME"]
            or home.resolve(strict=True) != home
            or not stat.S_ISDIR(home.lstat().st_mode)
        ):
            raise ExactReleaseEvidenceError("sealed_pytest_environment_invalid")
        child_tmp = resolved_scratch / "child-tmp"
        child_tmp.mkdir(mode=0o700)
        if child_tmp.resolve(strict=True) != child_tmp or stat.S_IMODE(child_tmp.stat().st_mode) != 0o700:
            raise ExactReleaseEvidenceError("sealed_pytest_environment_invalid")
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("sealed_pytest_environment_invalid") from exc

    environment = {
        **inherited,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPYCACHEPREFIX": str(python_cache),
        "TMPDIR": str(child_tmp),
        "TZ": "UTC",
        "VIRTUAL_ENV": str(runtime.root / "venv"),
    }
    if set(environment) != _SEALED_CHILD_ENVIRONMENT_KEYS:
        raise ExactReleaseEvidenceError("sealed_pytest_environment_invalid")
    return environment


def _run_closed_pytest(
    repo_root: Path,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
    *,
    require_running_producer: bool = True,
    require_isolated_startup: bool = True,
    release_runtime: _AuthenticatedReleaseRuntime | None = None,
) -> _ExecutionWitness:
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise ExactReleaseEvidenceError("child_process_authority_invalid")
    nodeids = proof_refs(journey_id, evidence_class)
    candidate_runtime_required = _requires_candidate_runtime(journey_id, evidence_class)
    if candidate_runtime_required:
        runtime = _require_release_runtime(release_runtime, identity)
    elif release_runtime is not None:
        raise ExactReleaseEvidenceError("release_runtime_unexpected")
    else:
        runtime = None
    if candidate_runtime_required and not require_isolated_startup:
        raise ExactReleaseEvidenceError("producer_process_authority_invalid")
    source_tooling_site: Path | None = None
    _require_exact_checkout(repo_root, identity.source_commit)
    _source_proofs(
        repo_root,
        identity,
        journey_id,
        evidence_class,
        require_running_producer=require_running_producer,
    )
    gate = _authenticated_quality_gate(require_isolated_startup=require_isolated_startup)
    quality_gate_sha256 = getattr(gate, "__authenticated_source_sha256__", None)
    authenticated_gate_sha256: str | None = None
    authenticated_gate_path: Path | None = None
    if runtime is not None:
        if type(quality_gate_sha256) is not str or _SHA256.fullmatch(quality_gate_sha256) is None:
            raise ExactReleaseEvidenceError("producer_helper_invalid")
        authenticated_gate_sha256 = quality_gate_sha256
        try:
            authenticated_gate_path = Path(str(gate.__file__)).resolve(strict=True)
            gate_status = authenticated_gate_path.lstat()
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise ExactReleaseEvidenceError("producer_helper_invalid") from exc
        if (
            authenticated_gate_path.name != "quality_gate.py"
            or authenticated_gate_path.parent.name != "tools"
            or not stat.S_ISREG(gate_status.st_mode)
            or gate_status.st_nlink != 1
        ):
            raise ExactReleaseEvidenceError("producer_helper_invalid")
        source_tooling_site = _test_tooling_site(repo_root, runtime.root)
    run_error: BaseException | None = None
    result: subprocess.CompletedProcess[bytes] | None = None
    outcomes: tuple[str, ...] = ()
    collection_sha256 = ""
    outcome_projection_sha256 = ""
    artifact_origin_sha256: str | None = None
    tooling_modules_sha256: str | None = None
    tooling_snapshot_sha256: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="friday-exact-evidence-") as temporary:
            scratch = Path(temporary)
            report = scratch / "results.xml"
            collection = scratch / "collection.json"
            python_cache = scratch / "python-cache"
            origin_report = scratch / "artifact-origin.json"
            bootstrap: tuple[str, ...] | None
            if runtime is None:
                bootstrap = None
                artifact_options: tuple[str, ...] = ()
                tooling_site = None
                tooling_projection: tuple[dict[str, object], ...] = ()
            else:
                assert source_tooling_site is not None
                assert authenticated_gate_sha256 is not None
                assert authenticated_gate_path is not None
                tooling_site, tooling_projection = _snapshot_test_tooling(
                    source_tooling_site,
                    scratch,
                )
                tooling_snapshot_sha256 = _tooling_snapshot_content_sha256(tooling_projection)
                bootstrap = (
                    _INSTALLED_PYTEST_BOOTSTRAP,
                    str(repo_root),
                    str(runtime.root),
                    str(runtime.site_packages),
                    runtime.site_packages_ref,
                    runtime.interpreter_ref,
                    str(tooling_site),
                    str(authenticated_gate_path),
                    authenticated_gate_sha256,
                    str(origin_report),
                    identity.source_commit,
                    identity.wheel_sha256,
                )
                artifact_options = ("-o", "pythonpath=", "--import-mode=importlib")
            with gate._isolated_test_environment() as environment:  # noqa: SLF001
                executable = sys.executable
                interpreter_options: tuple[str, ...] = ("-I",)
                if runtime is not None:
                    executable = str(runtime.interpreter)
                    interpreter_options = ("-I", "-S", "-B")
                    environment = _sealed_pytest_environment(
                        environment,
                        runtime,
                        scratch,
                        python_cache,
                    )
                else:
                    selected_site = gate._validated_installed_site(environment)  # noqa: SLF001
                    bootstrap = (
                        _PYTEST_BOOTSTRAP,
                        str(repo_root),
                        "-" if selected_site is None else str(selected_site),
                    )
                    if selected_site is not None:
                        artifact_options = ("-o", "pythonpath=", "--import-mode=importlib")
                    environment.pop("PYTEST_PLUGINS", None)
                    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
                    environment["PYTHONPYCACHEPREFIX"] = str(python_cache)
                assert bootstrap is not None
                result = subprocess.run(
                    (
                        executable,
                        *interpreter_options,
                        "-X",
                        f"pycache_prefix={python_cache}",
                        "-c",
                        *bootstrap,
                        "-q",
                        "-o",
                        "addopts=",
                        "-p",
                        "no:cacheprovider",
                        "-p",
                        "pytest_asyncio.plugin",
                        "-p",
                        "anyio.pytest_plugin",
                        "-p",
                        "xdist.plugin",
                        "-p",
                        "tools.quality_gate",
                        "-n",
                        "0",
                        *artifact_options,
                        f"--junitxml={report}",
                        f"--friday-collection-manifest={collection}",
                        f"--basetemp={scratch / 'pytest'}",
                        *nodeids,
                    ),
                    cwd=repo_root,
                    env=environment,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=PYTEST_TIMEOUT_SECONDS,
                )
            try:
                collected = gate.collection_nodeids(collection)
                collection_raw = collection.read_bytes()
                report.read_bytes()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ExactReleaseEvidenceError("pytest_collection_invalid") from exc
            if collected != nodeids:
                raise ExactReleaseEvidenceError("pytest_collection_invalid")
            outcomes = _pytest_outcomes(report, nodeids, gate=gate)
            expected_code = 0 if all(outcome == "PASSED" for outcome in outcomes) else 1
            if result.returncode != expected_code:
                raise ExactReleaseEvidenceError("pytest_exit_invalid")
            collection_sha256 = hashlib.sha256(collection_raw).hexdigest()
            outcome_projection_sha256 = _outcome_projection_sha256(nodeids, outcomes)
            if runtime is not None:
                assert tooling_site is not None
                artifact_origin_sha256, tooling_modules_sha256 = _artifact_origin_report_sha256(
                    origin_report,
                    runtime,
                )
                _require_tooling_snapshot_unchanged(tooling_site, tooling_projection)
    except (OSError, subprocess.SubprocessError, RuntimeError, ExactReleaseEvidenceError) as exc:
        run_error = exc
    finally:
        _require_exact_checkout(repo_root, identity.source_commit)
        _source_proofs(
            repo_root,
            identity,
            journey_id,
            evidence_class,
            require_running_producer=require_running_producer,
        )
        if runtime is not None:
            try:
                _reauthenticate_release_runtime(runtime)
                if _test_tooling_site(repo_root, runtime.root) != source_tooling_site:
                    raise ExactReleaseEvidenceError("test_tooling_changed")
            except ExactReleaseEvidenceError as exc:
                if run_error is None:
                    run_error = exc
    if run_error is not None:
        if isinstance(run_error, ExactReleaseEvidenceError):
            raise run_error
        raise ExactReleaseEvidenceError("pytest_execution_failed") from run_error
    assert result is not None
    return _execution_witness(
        outcomes,
        result.returncode,
        collection_sha256,
        outcome_projection_sha256,
        artifact_origin_sha256=artifact_origin_sha256,
        interpreter_ref=(None if runtime is None else runtime.interpreter_ref),
        site_packages_ref=(None if runtime is None else runtime.site_packages_ref),
        subprocess_policy=(None if runtime is None else _SUBPROCESS_POLICY),
        tooling_modules_sha256=tooling_modules_sha256,
        tooling_policy=(None if runtime is None else _TEST_TOOLING_POLICY),
        tooling_snapshot_sha256=tooling_snapshot_sha256,
    )


def _canonical_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _produce_for_identity(
    *,
    repo_root: Path,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
    owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
    release_runtime: _AuthenticatedReleaseRuntime | None = None,
) -> bytes:
    """Run the code-owned tests; callers cannot provide a result or runner."""

    if evidence_class == _PRODUCTION_OBSERVATION_CLASS:
        raise ExactReleaseEvidenceError("production_observation_external_binding_required")
    release_payload = _release_payload(identity)
    owner_smoke_payload = _owner_smoke_payload(owner_smoke)
    root = _resolve_directory(repo_root, "repo_root_invalid")
    candidate_runtime_required = _requires_candidate_runtime(journey_id, evidence_class)
    if candidate_runtime_required:
        runtime = _require_release_runtime(release_runtime, identity)
    elif release_runtime is not None:
        raise ExactReleaseEvidenceError("release_runtime_unexpected")
    else:
        runtime = None
    producer_sha256, proofs = _source_proofs(root, identity, journey_id, evidence_class)
    if runtime is None:
        witness = _run_closed_pytest(root, identity, journey_id, evidence_class)
    else:
        witness = _run_closed_pytest(
            root,
            identity,
            journey_id,
            evidence_class,
            release_runtime=runtime,
        )
    for proof, outcome in zip(proofs, witness.outcomes, strict=True):
        proof["outcome"] = outcome
    result = "VERIFIED" if witness.exit_code == 0 else "FAILED"
    execution: dict[str, object] = {
        "collection_sha256": witness.collection_sha256,
        "exit_code": witness.exit_code,
        "outcome_projection_sha256": witness.outcome_projection_sha256,
        "producer_path": PRODUCER_PATH,
        "producer_source_sha256": producer_sha256,
        "runner": "pytest",
    }
    if runtime is not None:
        execution["artifact_import"] = {
            "interpreter_ref": witness.interpreter_ref,
            "origin_report_sha256": witness.artifact_origin_sha256,
            "site_packages_ref": witness.site_packages_ref,
            "subprocess_policy": witness.subprocess_policy,
            "tooling_modules_sha256": witness.tooling_modules_sha256,
            "tooling_policy": witness.tooling_policy,
            "tooling_snapshot_sha256": witness.tooling_snapshot_sha256,
        }
    receipt = {
        "$schema": receipt_schema(evidence_class, journey_id=journey_id),
        "check_ids": _check_ids(journey_id, evidence_class),
        "environment": ENVIRONMENT_BY_CLASS[evidence_class],
        "evidence_class": evidence_class,
        "execution": execution,
        "journey_id": journey_id,
        "observed_at_utc": _canonical_utc_now(),
        "owner_smoke": owner_smoke_payload,
        "proofs": proofs,
        "release": release_payload,
        "result": result,
    }
    raw = canonical_json_bytes(receipt)
    _validate_receipt(
        raw,
        expected_release=identity,
        expected_journey_id=journey_id,
        expected_evidence_class=evidence_class,
        repo_root=root,
        authenticated_owner_smoke=owner_smoke,
        execution_witness=witness,
        release_runtime=runtime,
    )
    return raw


def _validate_receipt(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
    expected_journey_id: str,
    expected_evidence_class: str,
    repo_root: Path,
    authenticated_owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
    execution_witness: _ExecutionWitness | None = None,
    require_running_producer: bool = True,
    require_isolated_startup: bool = True,
    release_runtime: _AuthenticatedReleaseRuntime | None = None,
) -> dict[str, Any]:
    """Validate against external release and already-authenticated owner roots.

    ``authenticated_owner_smoke`` is an expected value from a separate
    authenticator.  The embedded object, a boolean, or an artifact path alone
    never establishes owner authority.
    """

    if expected_evidence_class == _PRODUCTION_OBSERVATION_CLASS:
        raise ExactReleaseEvidenceError("production_observation_external_binding_required")
    expected_release_payload = _release_payload(expected_release)
    candidate_runtime_required = _requires_candidate_runtime(
        expected_journey_id,
        expected_evidence_class,
    )
    if candidate_runtime_required:
        runtime = _require_release_runtime(release_runtime, expected_release)
    elif release_runtime is not None:
        raise ExactReleaseEvidenceError("release_runtime_unexpected")
    else:
        runtime = None
    root = _resolve_directory(repo_root, "repo_root_invalid")
    expected_producer_sha256, _source_inventory = _source_proofs(
        root,
        expected_release,
        expected_journey_id,
        expected_evidence_class,
        require_running_producer=require_running_producer,
    )
    value = _load_canonical_receipt(raw)
    if set(value) != _RECEIPT_FIELDS:
        raise ExactReleaseEvidenceError("receipt_fields_invalid")
    refs = proof_refs(expected_journey_id, expected_evidence_class)
    expected_checks = _check_ids(expected_journey_id, expected_evidence_class)
    if (
        value.get("$schema") != receipt_schema(expected_evidence_class, journey_id=expected_journey_id)
        or value.get("journey_id") != expected_journey_id
        or value.get("evidence_class") != expected_evidence_class
        or value.get("environment") != ENVIRONMENT_BY_CLASS[expected_evidence_class]
        or value.get("check_ids") != expected_checks
        or value.get("release") != expected_release_payload
    ):
        raise ExactReleaseEvidenceError("receipt_binding_invalid")

    observed_at = value.get("observed_at_utc")
    if type(observed_at) is not str or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", observed_at
    ):
        raise ExactReleaseEvidenceError("receipt_time_invalid")
    try:
        datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ExactReleaseEvidenceError("receipt_time_invalid") from exc

    execution = value.get("execution")
    execution_fields = set(_EXECUTION_FIELDS)
    if candidate_runtime_required:
        execution_fields.add("artifact_import")
    if type(execution) is not dict or set(execution) != execution_fields:
        raise ExactReleaseEvidenceError("execution_binding_invalid")
    artifact_import = execution.get("artifact_import")
    if candidate_runtime_required:
        assert runtime is not None
        if (
            type(artifact_import) is not dict
            or set(artifact_import) != _ARTIFACT_IMPORT_FIELDS
            or artifact_import.get("interpreter_ref") != runtime.interpreter_ref
            or _SHA256.fullmatch(str(artifact_import.get("origin_report_sha256") or "")) is None
            or artifact_import.get("site_packages_ref") != runtime.site_packages_ref
            or artifact_import.get("subprocess_policy") != _SUBPROCESS_POLICY
            or _SHA256.fullmatch(str(artifact_import.get("tooling_modules_sha256") or "")) is None
            or artifact_import.get("tooling_policy") != _TEST_TOOLING_POLICY
            or _SHA256.fullmatch(str(artifact_import.get("tooling_snapshot_sha256") or "")) is None
        ):
            raise ExactReleaseEvidenceError("artifact_execution_binding_invalid")
    elif artifact_import is not None:
        raise ExactReleaseEvidenceError("artifact_execution_binding_invalid")
    expected_collection = canonical_json_bytes({"nodeids": list(refs), "version": 1})
    if (
        execution.get("producer_path") != PRODUCER_PATH
        or execution.get("producer_source_sha256") != expected_producer_sha256
        or execution.get("runner") != "pytest"
        or execution.get("collection_sha256") != hashlib.sha256(expected_collection).hexdigest()
        or _SHA256.fullmatch(str(execution.get("outcome_projection_sha256") or "")) is None
        or type(execution.get("exit_code")) is not int
        or execution.get("exit_code") not in {0, 1}
    ):
        raise ExactReleaseEvidenceError("execution_binding_invalid")

    proofs = value.get("proofs")
    if type(proofs) is not list or len(proofs) != len(refs):
        raise ExactReleaseEvidenceError("proofs_invalid")
    outcomes: list[str] = []
    for proof, test_ref in zip(proofs, refs, strict=True):
        source = _test_source(root, expected_release.source_commit, test_ref)
        expected_base = {
            "runner": "pytest",
            "test_ref": test_ref,
            "test_source_sha256": hashlib.sha256(source).hexdigest(),
        }
        if (
            type(proof) is not dict
            or set(proof) != {"outcome", *expected_base}
            or any(proof.get(key) != item for key, item in expected_base.items())
            or proof.get("outcome") not in {"PASSED", "FAILED"}
        ):
            raise ExactReleaseEvidenceError("proofs_invalid")
        outcomes.append(str(proof["outcome"]))
    derived_result = "VERIFIED" if all(outcome == "PASSED" for outcome in outcomes) else "FAILED"
    derived_exit = 0 if derived_result == "VERIFIED" else 1
    if value.get("result") != derived_result or execution.get("exit_code") != derived_exit:
        raise ExactReleaseEvidenceError("result_not_machine_derived")
    if execution.get("outcome_projection_sha256") != _outcome_projection_sha256(
        refs,
        tuple(outcomes),
    ):
        raise ExactReleaseEvidenceError("execution_evidence_mismatch")

    expected_smoke = _owner_smoke_payload(authenticated_owner_smoke)
    embedded_smoke = value.get("owner_smoke")
    if embedded_smoke != expected_smoke:
        raise ExactReleaseEvidenceError("owner_smoke_not_authenticated")
    if embedded_smoke is not None and (
        type(embedded_smoke) is not dict
        or set(embedded_smoke) != {"artifact_ref", "artifact_sha256", "authority", "schema"}
    ):
        raise ExactReleaseEvidenceError("owner_smoke_binding_invalid")

    if execution_witness is None and runtime is None:
        witness = _run_closed_pytest(
            root,
            expected_release,
            expected_journey_id,
            expected_evidence_class,
            require_running_producer=require_running_producer,
            require_isolated_startup=require_isolated_startup,
        )
    elif execution_witness is None:
        witness = _run_closed_pytest(
            root,
            expected_release,
            expected_journey_id,
            expected_evidence_class,
            require_running_producer=require_running_producer,
            require_isolated_startup=require_isolated_startup,
            release_runtime=runtime,
        )
    else:
        witness = execution_witness
    artifact_binding = artifact_import if isinstance(artifact_import, dict) else {}
    witness = _require_execution_witness(witness)
    if (
        tuple(outcomes) != witness.outcomes
        or execution.get("exit_code") != witness.exit_code
        or execution.get("collection_sha256") != witness.collection_sha256
        or execution.get("outcome_projection_sha256") != witness.outcome_projection_sha256
        or witness.outcome_projection_sha256 != _outcome_projection_sha256(refs, witness.outcomes)
        or (
            candidate_runtime_required
            and (
                witness.artifact_origin_sha256 != artifact_binding.get("origin_report_sha256")
                or witness.interpreter_ref != artifact_binding.get("interpreter_ref")
                or witness.site_packages_ref != artifact_binding.get("site_packages_ref")
                or witness.subprocess_policy != artifact_binding.get("subprocess_policy")
                or witness.tooling_modules_sha256 != artifact_binding.get("tooling_modules_sha256")
                or witness.tooling_policy != artifact_binding.get("tooling_policy")
                or witness.tooling_snapshot_sha256 != artifact_binding.get("tooling_snapshot_sha256")
            )
        )
        or (
            not candidate_runtime_required
            and any(
                item is not None
                for item in (
                    witness.artifact_origin_sha256,
                    witness.interpreter_ref,
                    witness.site_packages_ref,
                    witness.subprocess_policy,
                    witness.tooling_modules_sha256,
                    witness.tooling_policy,
                    witness.tooling_snapshot_sha256,
                )
            )
        )
    ):
        raise ExactReleaseEvidenceError("execution_evidence_mismatch")
    return value


def validate_receipt(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
    expected_journey_id: str,
    expected_evidence_class: str,
    repo_root: Path,
    authenticated_owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
    release_root: Path | None = None,
) -> dict[str, Any]:
    """Validate structure, then independently rerun the exact closed inventory."""

    if expected_evidence_class == _PRODUCTION_OBSERVATION_CLASS:
        raise ExactReleaseEvidenceError("production_observation_external_binding_required")
    candidate_runtime_required = _requires_candidate_runtime(
        expected_journey_id,
        expected_evidence_class,
    )
    if candidate_runtime_required:
        if release_root is None:
            raise ExactReleaseEvidenceError("release_runtime_required")
        _require_native_validation_context()
        runtime = _authenticate_release_runtime(release_root)
        if runtime.identity != expected_release:
            raise ExactReleaseEvidenceError("release_identity_mismatch")
    elif release_root is not None:
        raise ExactReleaseEvidenceError("release_runtime_unexpected")
    else:
        runtime = None

    with _validation_source_checkout(repo_root, expected_release.source_commit) as (
        source_root,
        require_running_producer,
    ):
        return _validate_receipt(
            raw,
            expected_release=expected_release,
            expected_journey_id=expected_journey_id,
            expected_evidence_class=expected_evidence_class,
            repo_root=source_root,
            authenticated_owner_smoke=authenticated_owner_smoke,
            require_running_producer=require_running_producer,
            require_isolated_startup=candidate_runtime_required,
            release_runtime=runtime,
        )


def _validation_request(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
    expected_journey_id: str,
    expected_evidence_class: str,
    release_root: Path | None,
) -> dict[str, object]:
    _load_canonical_receipt(raw)
    proof_refs(expected_journey_id, expected_evidence_class)
    candidate_runtime_required = _requires_candidate_runtime(
        expected_journey_id,
        expected_evidence_class,
    )
    if candidate_runtime_required != (release_root is not None):
        raise ExactReleaseEvidenceError(
            "release_runtime_required" if candidate_runtime_required else "release_runtime_unexpected"
        )
    return {
        "evidence_class": expected_evidence_class,
        "journey_id": expected_journey_id,
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "release": _release_payload(expected_release),
        "release_root_required": candidate_runtime_required,
    }


def _validation_attestation(
    raw: bytes,
    receipt: dict[str, Any],
    *,
    expected_release: ReleaseIdentity,
    expected_journey_id: str,
    expected_evidence_class: str,
    release_root: Path | None,
) -> dict[str, object]:
    request = _validation_request(
        raw,
        expected_release=expected_release,
        expected_journey_id=expected_journey_id,
        expected_evidence_class=expected_evidence_class,
        release_root=release_root,
    )
    result = receipt.get("result")
    observed_at_utc = receipt.get("observed_at_utc")
    if result not in {"VERIFIED", "FAILED"} or type(observed_at_utc) is not str:
        raise ExactReleaseEvidenceError("validation_attestation_invalid")
    return {
        "$schema": VALIDATION_ATTESTATION_SCHEMA,
        "evidence_class": expected_evidence_class,
        "journey_id": expected_journey_id,
        "observed_at_utc": observed_at_utc,
        "receipt_sha256": request["receipt_sha256"],
        "release": request["release"],
        "request_sha256": hashlib.sha256(canonical_json_bytes(request)).hexdigest(),
        "result": result,
        "status": "VALIDATED",
    }


def _load_validation_attestation(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not raw.endswith(b"\n") or not 0 < len(raw) <= 4096:
        raise ExactReleaseEvidenceError("validation_attestation_invalid")
    payload_raw = raw[:-1]
    try:
        value = json.loads(
            payload_raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ExactReleaseEvidenceError("validation_attestation_invalid")
            ),
        )
    except (UnicodeError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ExactReleaseEvidenceError("validation_attestation_invalid") from exc
    if type(value) is not dict:
        raise ExactReleaseEvidenceError("validation_attestation_invalid")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExactReleaseEvidenceError("validation_attestation_invalid") from exc
    if payload_raw != canonical:
        raise ExactReleaseEvidenceError("validation_attestation_invalid")
    return value


def _isolated_validation_failure_code(raw: bytes) -> str | None:
    try:
        value = _load_validation_attestation(raw)
    except ExactReleaseEvidenceError:
        return None
    if set(value) != {"failure_code", "status"} or value.get("status") != "failed_closed":
        return None
    failure_code = value.get("failure_code")
    if type(failure_code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{1,95}", failure_code) is None:
        return None
    return failure_code


def _same_validation_status(left: os.stat_result, right: os.stat_result) -> bool:
    return all(getattr(left, field) == getattr(right, field) for field in _TRUSTED_GIT_STATUS_FIELDS)


def _require_native_validation_context() -> None:
    try:
        target = Path(_NATIVE_VALIDATION_INTERPRETER_ARGV0)
        descriptor_status = os.fstat(_NATIVE_VALIDATION_INTERPRETER_FD)
        if (
            _NATIVE_VALIDATION_INTERPRETER_FD < 0
            or not target.is_absolute()
            or target.resolve(strict=True) != target
            or sys.executable != str(target)
            or not _same_validation_status(descriptor_status, target.lstat())
            or not _same_validation_status(descriptor_status, os.stat("/proc/self/exe"))
        ):
            raise ExactReleaseEvidenceError("native_validation_context_invalid")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("native_validation_context_invalid") from exc


def _validation_origin_identity(repo_root: Path) -> tuple[Path, str]:
    """Bind tracked origin bytes; ignored tooling is excluded by the private clone."""

    root = _resolve_directory(repo_root, "repo_root_invalid")
    try:
        head = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except UnicodeError as exc:
        raise ExactReleaseEvidenceError("git_identity_unavailable") from exc
    if _COMMIT.fullmatch(head) is None:
        raise ExactReleaseEvidenceError("git_identity_unavailable")
    if _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise ExactReleaseEvidenceError("checkout_not_exact_clean_commit")
    _require_running_producer(root, head)
    return root, head


def _private_validation_checkout(origin: Path, head: str, scratch: Path) -> Path:
    controller = scratch / "controller"
    git_options = ("-c", "core.hooksPath=/dev/null")
    clone_options = (*git_options, "clone", "--quiet", "--shared", "--no-checkout", "--")
    _git(origin, *clone_options, str(origin), str(controller))
    _git(controller, *git_options, "checkout", "--quiet", "--detach", head)
    os.chmod(controller / PRODUCER_PATH, 0o400, follow_symlinks=False)
    _require_exact_checkout(controller, head)
    return controller


def _canonical_validation_python_version(repo_root: Path, head: str) -> str:
    raw = _exact_git_blob(repo_root, head, "tools/quality_toolchain_preflight.py")
    matches = re.findall(
        rb"(?m)^REQUIRED_PYTHON = \(([0-9]+), ([0-9]+), ([0-9]+)\)$",
        raw,
    )
    version = tuple(int(part) for part in matches[0]) if len(matches) == 1 else ()
    if len(version) != 3 or tuple(sys.version_info[:3]) != version:
        raise ExactReleaseEvidenceError("validation_controller_invalid")
    return ".".join(str(part) for part in version)


def _validation_runtime_directories(target: Path, stdlib: Path) -> tuple[tuple[Path, os.stat_result], ...]:
    lib_dynload = stdlib / "lib-dynload"
    runtime_anchor = Path(os.path.commonpath((target.parent, stdlib))).resolve(strict=True)
    bootstrap_parents = tuple(
        stdlib / Path(relative).parent
        for _module_name, relative in _VALIDATION_BOOTSTRAP_MODULES
        if relative is not None and Path(relative).parent != Path()
    )
    paths = tuple(dict.fromkeys((*target.parents, stdlib, lib_dynload, *bootstrap_parents, *stdlib.parents)))
    try:
        values = tuple((path, path.lstat()) for path in paths)
    except OSError as exc:
        raise ExactReleaseEvidenceError("validation_controller_invalid") from exc
    if any(
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(value.st_mode)
        or (
            (path == runtime_anchor or path.is_relative_to(runtime_anchor))
            and (value.st_uid not in {0, os.geteuid()} or value.st_mode & 0o022)
        )
        for path, value in values
    ):
        raise ExactReleaseEvidenceError("validation_controller_invalid")
    return values


def _validation_python_zip_status(stdlib: Path) -> tuple[Path, os.stat_result | None]:
    path = stdlib.parent / f"python{sys.version_info.major}{sys.version_info.minor}.zip"
    try:
        value = path.lstat()
    except FileNotFoundError:
        if os.path.lexists(path):
            raise ExactReleaseEvidenceError("validation_controller_invalid") from None
        return path, None
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISREG(value.st_mode)
        or value.st_uid not in {0, os.geteuid()}
        or value.st_mode & 0o022
    ):
        raise ExactReleaseEvidenceError("validation_controller_invalid")
    return path, value


def _open_validation_bootstrap_files(
    stdlib: Path,
) -> tuple[tuple[str, str, int, os.stat_result | None], ...]:
    """Pin the finite files which CPython executes before producer validation."""

    values: list[tuple[str, str, int, os.stat_result | None]] = []
    try:
        for module_name, relative in _VALIDATION_BOOTSTRAP_MODULES:
            if relative is not None:
                path = stdlib / relative
                origin = str(path)
                search_root = path.parent.parent if path.name == "__init__.py" else path.parent
                spec = importlib.machinery.PathFinder.find_spec(module_name, [str(search_root)])
                if getattr(spec, "origin", None) != origin:
                    raise ExactReleaseEvidenceError("validation_controller_invalid")
            else:
                spec = importlib.machinery.BuiltinImporter.find_spec(module_name)
                if spec is None:
                    spec = importlib.machinery.FrozenImporter.find_spec(module_name)
                if spec is None:
                    spec = importlib.machinery.PathFinder.find_spec(
                        module_name,
                        [str(stdlib / "lib-dynload")],
                    )
                raw_origin = getattr(spec, "origin", None)
                if raw_origin in {"built-in", "frozen"}:
                    values.append((module_name, str(raw_origin), -1, None))
                    continue
                if type(raw_origin) is not str:
                    raise ExactReleaseEvidenceError("validation_controller_invalid")
                origin = raw_origin
                path = Path(origin)
                if (
                    path.parent != stdlib / "lib-dynload"
                    or not path.name.startswith(f"{module_name}.")
                    or path.suffix != ".so"
                ):
                    raise ExactReleaseEvidenceError("validation_controller_invalid")
            named = path.lstat()
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                opened = os.fstat(descriptor)
                if (
                    not path.is_absolute()
                    or path.resolve(strict=True) != path
                    or not _same_validation_status(named, opened)
                    or not stat.S_ISREG(named.st_mode)
                    or named.st_nlink != 1
                    or named.st_uid not in {0, os.geteuid()}
                    or named.st_mode & 0o022
                ):
                    raise ExactReleaseEvidenceError("validation_controller_invalid")
            except BaseException:
                with suppress(OSError):
                    os.close(descriptor)
                raise
            values.append((module_name, origin, descriptor, named))
    except BaseException as exc:
        for _name, _origin, descriptor, _status in values:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
        if not isinstance(exc, (OSError, RuntimeError, TypeError, ValueError)):
            raise
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("validation_controller_invalid") from exc
    return tuple(values)


def _open_validation_interpreter() -> tuple[
    int,
    Path,
    os.stat_result,
    tuple[tuple[Path, os.stat_result], ...],
    tuple[tuple[str, str, int, os.stat_result | None], ...],
    Path,
    tuple[Path, os.stat_result | None],
]:
    descriptor = -1
    bootstrap_files: tuple[tuple[str, str, int, os.stat_result | None], ...] = ()
    try:
        lexical = Path(_INITIAL_PRODUCER_EXECUTABLE)
        target = lexical.resolve(strict=True)
        target_before = target.lstat()
        descriptor = os.open(
            target,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        opened = os.fstat(descriptor)
        stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
        directories = _validation_runtime_directories(target, stdlib)
        bootstrap_files = _open_validation_bootstrap_files(stdlib)
        python_zip = _validation_python_zip_status(stdlib)
        if (
            sys.executable != _INITIAL_PRODUCER_EXECUTABLE
            or not lexical.is_absolute()
            or target.resolve(strict=True) != target
            or not stat.S_ISREG(target_before.st_mode)
            or target_before.st_nlink != 1
            or target_before.st_uid not in {0, os.geteuid()}
            or target_before.st_mode & 0o022
            or not target_before.st_mode & 0o111
            or not _same_validation_status(target_before, opened)
            or not _same_validation_status(opened, os.stat("/proc/self/exe"))
        ):
            raise ExactReleaseEvidenceError("validation_controller_invalid")
        return descriptor, target, opened, directories, bootstrap_files, stdlib, python_zip
    except BaseException as exc:
        for _name, _origin, bootstrap_descriptor, _status in bootstrap_files:
            if bootstrap_descriptor >= 0:
                with suppress(OSError):
                    os.close(bootstrap_descriptor)
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if not isinstance(exc, (OSError, RuntimeError, TypeError, ValueError)):
            raise
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("validation_controller_invalid") from exc


def _open_validation_producer(controller: Path, head: str) -> tuple[int, os.stat_result, bytes, str]:
    path = controller / PRODUCER_PATH
    descriptor = -1
    try:
        expected = _exact_git_blob(controller, head, PRODUCER_PATH)
        before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        opened = os.fstat(descriptor)
        raw = _read_validation_descriptor(descriptor, 1 << 20)
        after = os.fstat(descriptor)
        if (
            path.resolve(strict=True) != path
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o022
            or not _same_validation_status(before, opened)
            or not _same_validation_status(opened, after)
            or raw != expected
        ):
            raise ExactReleaseEvidenceError("validation_controller_invalid")
        return descriptor, before, expected, hashlib.sha256(expected).hexdigest()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("validation_controller_invalid") from exc


def _read_validation_descriptor(descriptor: int, maximum_bytes: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = maximum_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(1 << 20, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _kill_validation_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise ExactReleaseEvidenceError("isolated_receipt_validation_failed") from exc
    try:
        process.wait(timeout=VALIDATION_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ExactReleaseEvidenceError("isolated_receipt_validation_failed") from exc


def _run_validation_controller(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    raw: bytes,
    interpreter_descriptor: int,
    producer_descriptor: int,
    bootstrap_descriptors: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    output_root = Path(environment["TMPDIR"])
    try:
        with (
            tempfile.TemporaryFile(dir=output_root) as input_file,
            tempfile.TemporaryFile(dir=output_root) as output_file,
            tempfile.TemporaryFile(dir=output_root) as error_file,
        ):
            input_file.write(raw)
            input_file.seek(0)
            process = subprocess.Popen(  # noqa: S603 - exact O_NOFOLLOW interpreter descriptor
                command,
                executable=f"/proc/self/fd/{interpreter_descriptor}",
                cwd=cwd,
                env=environment,
                stdin=input_file,
                stdout=output_file,
                stderr=error_file,
                start_new_session=True,
                pass_fds=(interpreter_descriptor, producer_descriptor, *bootstrap_descriptors),
            )
            pidfd = -1
            try:
                pidfd = os.pidfd_open(process.pid, 0)
                finished, _writeable, _exceptional = select.select(
                    (pidfd,),
                    (),
                    (),
                    VALIDATION_TIMEOUT_SECONDS,
                )
            except BaseException:
                with suppress(ExactReleaseEvidenceError):
                    _kill_validation_process_group(process)
                raise
            finally:
                if pidfd >= 0:
                    os.close(pidfd)
            if not finished:
                _kill_validation_process_group(process)
                raise subprocess.TimeoutExpired(command, VALIDATION_TIMEOUT_SECONDS)
            _kill_validation_process_group(process)
            output_file.seek(0)
            error_file.seek(0)
            stdout = output_file.read(4097)
            stderr = error_file.read(4097)
    except OSError as exc:
        raise ExactReleaseEvidenceError("isolated_receipt_validation_failed") from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout, stderr=stderr)


def _isolated_validation_environment(scratch: Path) -> dict[str, str]:
    try:
        resolved = scratch.resolve(strict=True)
        status = resolved.lstat()
        home = resolved / "home"
        home.mkdir(mode=0o700)
        if (
            resolved != scratch
            or not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o700
            or home.resolve(strict=True) != home
            or stat.S_IMODE(home.lstat().st_mode) != 0o700
        ):
            raise ExactReleaseEvidenceError("validation_scratch_invalid")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("validation_scratch_invalid") from exc
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "TMPDIR": str(resolved),
        "TZ": "UTC",
    }


def validate_receipt_via_native_controller(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
    expected_journey_id: str,
    expected_evidence_class: str,
    repo_root: Path,
    release_root: Path | None = None,
) -> dict[str, Any]:
    """Validate through one exact stdlib-only controller from a native gate."""

    if not _requires_candidate_runtime(expected_journey_id, expected_evidence_class) or release_root is None:
        raise ExactReleaseEvidenceError("native_validation_scope_invalid")
    if (
        _NATIVE_VALIDATION_TOOLING_SITE is not None
        or _NATIVE_VALIDATION_INTERPRETER_FD != -1
        or _NATIVE_VALIDATION_INTERPRETER_ARGV0
        or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
    ):
        raise ExactReleaseEvidenceError("validation_controller_invalid")
    receipt = _load_canonical_receipt(raw)
    bound_release_root = _resolve_directory(release_root, "release_runtime_invalid")
    request = _validation_request(
        raw,
        expected_release=expected_release,
        expected_journey_id=expected_journey_id,
        expected_evidence_class=expected_evidence_class,
        release_root=bound_release_root,
    )
    origin, head = _validation_origin_identity(repo_root)
    interpreter_descriptor = -1
    producer_descriptor = -1
    bootstrap_files: tuple[tuple[str, str, int, os.stat_result | None], ...] = ()
    completed: subprocess.CompletedProcess[bytes] | None = None
    run_error: BaseException | None = None
    try:
        (
            interpreter_descriptor,
            executable_target,
            target_before,
            runtime_directories,
            bootstrap_files,
            validation_stdlib,
            python_zip_before,
        ) = _open_validation_interpreter()
        with tempfile.TemporaryDirectory(prefix="friday-native-validation-", dir="/var/tmp") as temporary:
            scratch = Path(temporary).resolve(strict=True)
            scratch.chmod(0o700)
            environment = _isolated_validation_environment(scratch)
            bootstrap_cache = scratch / "bootstrap-cache"
            if os.path.lexists(bootstrap_cache):
                raise ExactReleaseEvidenceError("validation_controller_invalid")
            controller = _private_validation_checkout(origin, head, scratch)
            python_version = _canonical_validation_python_version(controller, head)
            tooling_site = _test_tooling_site(controller, bound_release_root)
            (
                producer_descriptor,
                producer_before,
                producer_expected,
                producer_sha256,
            ) = _open_validation_producer(controller, head)
            producer = controller / PRODUCER_PATH
            bootstrap_arguments = tuple(
                value
                for module_name, module_origin, descriptor, status_value in bootstrap_files
                for value in (
                    module_name,
                    module_origin,
                    str(descriptor),
                    (
                        ",".join(str(getattr(status_value, field)) for field in _TRUSTED_GIT_STATUS_FIELDS)
                        if status_value is not None
                        else "-"
                    ),
                )
            )
            command = (
                str(executable_target),
                "-I",
                "-S",
                "-B",
                "-X",
                f"pycache_prefix={bootstrap_cache}",
                "-c",
                _ISOLATED_VALIDATION_BOOTSTRAP,
                str(interpreter_descriptor),
                str(producer_descriptor),
                str(producer),
                producer_sha256,
                str(executable_target),
                python_version,
                str(validation_stdlib),
                str(tooling_site),
                str(bootstrap_cache),
                *bootstrap_arguments,
                str(os.getpid()),
                "validate",
                "--repo-root",
                str(controller),
                "--release-root",
                str(bound_release_root),
                "--expected-source-commit",
                expected_release.source_commit,
                "--expected-tree-sha256",
                expected_release.tree_sha256,
                "--expected-wheel-sha256",
                expected_release.wheel_sha256,
                "--expected-database-schema",
                str(expected_release.database_schema),
                "--journey-id",
                expected_journey_id,
                "--evidence-class",
                expected_evidence_class,
            )
            try:
                completed = _run_validation_controller(
                    command,
                    cwd=controller,
                    environment=environment,
                    raw=raw,
                    interpreter_descriptor=interpreter_descriptor,
                    producer_descriptor=producer_descriptor,
                    bootstrap_descriptors=tuple(
                        descriptor
                        for _name, _origin, descriptor, _status in bootstrap_files
                        if descriptor >= 0
                    ),
                )
            finally:
                _require_exact_checkout(controller, head)
                producer_after = os.fstat(producer_descriptor)
                producer_raw = _read_validation_descriptor(producer_descriptor, 1 << 20)
                if (
                    not _same_validation_status(producer_before, producer_after)
                    or not _same_validation_status(producer_after, producer.lstat())
                    or producer_raw != producer_expected
                    or os.path.lexists(bootstrap_cache)
                ):
                    raise ExactReleaseEvidenceError("validation_controller_invalid")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        run_error = exc
    finally:
        if producer_descriptor >= 0:
            with suppress(OSError):
                os.close(producer_descriptor)
        if interpreter_descriptor >= 0:
            try:
                current_directories = _validation_runtime_directories(
                    executable_target,
                    validation_stdlib,
                )
                python_zip_after = _validation_python_zip_status(validation_stdlib)
                if (
                    not _same_validation_status(target_before, executable_target.lstat())
                    or not _same_validation_status(target_before, os.fstat(interpreter_descriptor))
                    or not _same_validation_status(target_before, os.stat("/proc/self/exe"))
                    or len(runtime_directories) != len(current_directories)
                    or python_zip_before[0] != python_zip_after[0]
                    or (
                        (python_zip_before[1] is None) != (python_zip_after[1] is None)
                        or (
                            python_zip_before[1] is not None
                            and python_zip_after[1] is not None
                            and not _same_validation_status(
                                python_zip_before[1],
                                python_zip_after[1],
                            )
                        )
                    )
                    or any(
                        before_path != after_path or not _same_validation_status(before, after)
                        for (before_path, before), (after_path, after) in zip(
                            runtime_directories,
                            current_directories,
                            strict=True,
                        )
                    )
                    or any(
                        status_value is None
                        or descriptor < 0
                        or not _same_validation_status(status_value, os.fstat(descriptor))
                        or not _same_validation_status(status_value, Path(origin_value).lstat())
                        for _name, origin_value, descriptor, status_value in bootstrap_files
                        if origin_value not in {"built-in", "frozen"}
                    )
                ):
                    raise ExactReleaseEvidenceError("validation_controller_invalid")
            except (OSError, RuntimeError) as exc:
                raise ExactReleaseEvidenceError("validation_controller_invalid") from exc
            finally:
                for _name, _origin, descriptor, _status in bootstrap_files:
                    if descriptor >= 0:
                        with suppress(OSError):
                            os.close(descriptor)
                with suppress(OSError):
                    os.close(interpreter_descriptor)
    if run_error is not None:
        if isinstance(run_error, ExactReleaseEvidenceError):
            raise run_error
        raise ExactReleaseEvidenceError("isolated_receipt_validation_failed") from run_error
    if completed is None:
        raise ExactReleaseEvidenceError("isolated_receipt_validation_failed")
    if completed.returncode != 0 or completed.stderr:
        failure_code = _isolated_validation_failure_code(completed.stdout)
        if completed.stderr or failure_code is None:
            raise ExactReleaseEvidenceError("isolated_receipt_validation_failed")
        raise ExactReleaseEvidenceError(failure_code)
    attestation = _load_validation_attestation(completed.stdout)
    expected_attestation = {
        "$schema": VALIDATION_ATTESTATION_SCHEMA,
        "evidence_class": expected_evidence_class,
        "journey_id": expected_journey_id,
        "observed_at_utc": receipt.get("observed_at_utc"),
        "receipt_sha256": request["receipt_sha256"],
        "release": request["release"],
        "request_sha256": hashlib.sha256(canonical_json_bytes(request)).hexdigest(),
        "result": receipt.get("result"),
        "status": "VALIDATED",
    }
    if attestation != expected_attestation:
        raise ExactReleaseEvidenceError("validation_attestation_invalid")
    return receipt


def _stable_file(path: Path, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum_bytes:
            raise ExactReleaseEvidenceError("release_artifact_invalid")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_size", "st_mtime_ns")
        if len(raw) != before.st_size or any(
            getattr(before, name) != getattr(after, name) for name in stable
        ):
            raise ExactReleaseEvidenceError("release_artifact_changed")
        return raw
    except OSError as exc:
        raise ExactReleaseEvidenceError("release_artifact_invalid") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise ExactReleaseEvidenceError("release_artifact_invalid") from exc


def derive_release_identity(release_root: Path) -> ReleaseIdentity:
    """Derive identity from a sealed wheel release and run its installed smoke."""

    release_operator = _authenticated_release_operator()
    try:
        root = Path(os.path.abspath(release_root)).resolve(strict=True)
        manifest = root / "artifacts/release-tree.sha256"
        manifest_raw = _stable_file(manifest, 64 << 20)
        tree_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        installed = release_operator.load_release_identity(root, expected_tree_sha256=tree_sha256)
        smoke_sha256 = release_operator.installed_surface_smoke(installed)
        if _SHA256.fullmatch(smoke_sha256) is None:
            raise ExactReleaseEvidenceError("installed_surface_smoke_invalid")
        release_operator.verify_release_tree(installed)
        if _stable_file(manifest, 64 << 20) != manifest_raw:
            raise ExactReleaseEvidenceError("release_artifact_changed")
        metadata_raw = _stable_file(root / "artifacts/immutable-release.json", 1 << 20)
        if not metadata_raw.endswith(b"\n"):
            raise ExactReleaseEvidenceError("release_metadata_invalid")
        metadata = json.loads(
            metadata_raw[:-1].decode("ascii", errors="strict"),
            object_pairs_hook=_closed_object,
        )
    except (
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        release_operator.ReleaseFailure,
    ) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("release_identity_invalid") from exc
    if type(metadata) is not dict:
        raise ExactReleaseEvidenceError("release_metadata_invalid")
    try:
        metadata_canonical = canonical_json_bytes(metadata)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExactReleaseEvidenceError("release_metadata_invalid") from exc
    if metadata_raw != metadata_canonical + b"\n":
        raise ExactReleaseEvidenceError("release_metadata_invalid")
    wheel_sha256 = metadata.get("wheel_sha256")
    if (
        metadata.get("commit") != installed.commit
        or metadata.get("max_schema") != installed.max_schema
        or type(wheel_sha256) is not str
        or _SHA256.fullmatch(wheel_sha256) is None
    ):
        raise ExactReleaseEvidenceError("release_metadata_invalid")
    return ReleaseIdentity(
        source_commit=installed.commit,
        tree_sha256=tree_sha256,
        wheel_sha256=wheel_sha256,
        database_schema=installed.max_schema,
    )


def _authenticate_release_runtime(release_root: Path) -> _AuthenticatedReleaseRuntime:
    """Authenticate one sealed release and discover its unique installed package."""

    try:
        root = Path(os.path.abspath(release_root)).resolve(strict=True)
        identity = derive_release_identity(root)
        venv = root / "venv"
        library = venv / "lib"
        for directory in (venv, library):
            if not stat.S_ISDIR(directory.lstat().st_mode) or directory.resolve(strict=True) != directory:
                raise ExactReleaseEvidenceError("release_runtime_invalid")
        candidates = tuple(
            path
            for path in sorted(library.glob("python*/site-packages"))
            if re.fullmatch(r"python[0-9]+\.[0-9]+", path.parent.name) is not None
        )
        if len(candidates) != 1:
            raise ExactReleaseEvidenceError("release_runtime_invalid")
        site_packages = candidates[0]
        interpreter = root / _INTERPRETER_REF
        interpreter_status = interpreter.lstat()
        python_directory = site_packages.parent
        expected_python_directory = f"python{sys.version_info.major}.{sys.version_info.minor}"
        package_root = site_packages / "friday"
        package_init = package_root / "__init__.py"
        for directory in (python_directory, site_packages, package_root):
            if not stat.S_ISDIR(directory.lstat().st_mode) or directory.resolve(strict=True) != directory:
                raise ExactReleaseEvidenceError("release_runtime_invalid")
        if (
            not stat.S_ISREG(interpreter_status.st_mode)
            or interpreter.resolve(strict=True) != interpreter
            or not interpreter_status.st_mode & 0o111
            or not os.access(interpreter, os.X_OK)
            or interpreter_status.st_nlink != 1
            or not stat.S_ISREG(package_init.lstat().st_mode)
            or package_init.resolve(strict=True) != package_init
            or python_directory.name != expected_python_directory
        ):
            raise ExactReleaseEvidenceError("release_runtime_invalid")
        distributions = tuple(sorted(site_packages.glob("friday-*.dist-info")))
        if (
            len(distributions) != 1
            or re.fullmatch(r"friday-[A-Za-z0-9_.+-]+\.dist-info", distributions[0].name) is None
            or not stat.S_ISDIR(distributions[0].lstat().st_mode)
            or distributions[0].resolve(strict=True) != distributions[0]
        ):
            raise ExactReleaseEvidenceError("release_runtime_invalid")
        site_packages_ref = site_packages.relative_to(root).as_posix()
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            if str(exc) == "release_artifact_invalid":
                raise ExactReleaseEvidenceError("release_identity_invalid") from exc
            raise
        raise ExactReleaseEvidenceError("release_runtime_invalid") from exc
    return _AuthenticatedReleaseRuntime(
        root=root,
        identity=identity,
        interpreter=interpreter,
        interpreter_ref=_INTERPRETER_REF,
        site_packages=site_packages,
        site_packages_ref=site_packages_ref,
        package_root=package_root,
        authority=_RELEASE_RUNTIME_AUTHORITY,
    )


def _require_release_runtime(
    value: object,
    identity: ReleaseIdentity,
) -> _AuthenticatedReleaseRuntime:
    if (
        type(value) is not _AuthenticatedReleaseRuntime
        or value.authority is not _RELEASE_RUNTIME_AUTHORITY
        or value.identity != identity
        or value.interpreter != value.root / value.interpreter_ref
        or value.interpreter_ref != _INTERPRETER_REF
        or not value.root.is_absolute()
        or value.site_packages != value.root / value.site_packages_ref
        or value.package_root != value.site_packages / "friday"
        or PurePosixPath(value.site_packages_ref).is_absolute()
        or any(part in {"", ".", ".."} for part in PurePosixPath(value.site_packages_ref).parts)
    ):
        raise ExactReleaseEvidenceError("release_runtime_not_authenticated")
    return value


def _reauthenticate_release_runtime(runtime: _AuthenticatedReleaseRuntime) -> None:
    refreshed = _authenticate_release_runtime(runtime.root)
    projection = (
        "root",
        "identity",
        "interpreter",
        "interpreter_ref",
        "site_packages",
        "site_packages_ref",
        "package_root",
    )
    if any(getattr(runtime, field) != getattr(refreshed, field) for field in projection):
        raise ExactReleaseEvidenceError("release_identity_changed")


def validate_production_observation_receipt(
    raw: bytes,
    *,
    authenticated_binding: AuthenticatedProductionObservationBinding,
    expected_journey_id: str = _PRODUCTION_OBSERVATION_JOURNEY,
) -> dict[str, Any]:
    """Validate one production receipt against external expected authority.

    The embedded hashes never authenticate themselves.  Validation requires
    the exact canonical endpoint bytes and expected values supplied separately
    by a Release Captain authenticator.
    """

    binding = _production_binding_payload(authenticated_binding)
    value = _validate_production_observation_receipt_structure(
        raw,
        expected_release=authenticated_binding.release,
        expected_journey_id=expected_journey_id,
    )
    if value.get("observation") != binding:
        raise ExactReleaseEvidenceError("production_observation_not_authenticated")
    return value


def produce_production_observation_bundle(
    *,
    authenticated_binding: AuthenticatedProductionObservationBinding,
    journey_id: str = _PRODUCTION_OBSERVATION_JOURNEY,
) -> EvidenceBundle:
    """Derive a timestamp-free bundle from one authenticated live response.

    There is no result, check outcome, release hash, or response hash parameter:
    all publication claims are derived from the exact expected-value binding.
    """

    if journey_id != _PRODUCTION_OBSERVATION_JOURNEY:
        raise ExactReleaseEvidenceError("production_observation_scope_invalid")
    observation = _production_binding_payload(authenticated_binding)
    check_ids = _check_ids(journey_id, _PRODUCTION_OBSERVATION_CLASS)
    receipt = {
        "$schema": PRODUCTION_OBSERVATION_RECEIPT_SCHEMA,
        "check_ids": check_ids,
        "checks": [{"check_id": check_id, "outcome": "PASSED"} for check_id in check_ids],
        "environment": ENVIRONMENT_BY_CLASS[_PRODUCTION_OBSERVATION_CLASS],
        "evidence_class": _PRODUCTION_OBSERVATION_CLASS,
        "journey_id": journey_id,
        "observation": observation,
        "release": _release_payload(authenticated_binding.release),
        "result": "VERIFIED",
    }
    raw = canonical_json_bytes(receipt)
    validate_production_observation_receipt(
        raw,
        authenticated_binding=authenticated_binding,
        expected_journey_id=journey_id,
    )
    return _production_bundle_from_receipt(
        raw,
        identity=authenticated_binding.release,
        journey_id=journey_id,
    )


def validate_production_observation_bundle(
    bundle: EvidenceBundle,
    *,
    authenticated_binding: AuthenticatedProductionObservationBinding,
    expected_journey_id: str = _PRODUCTION_OBSERVATION_JOURNEY,
) -> dict[str, Any]:
    """Revalidate a canonical bundle with separately supplied authority."""

    exact = _require_evidence_bundle(bundle)
    value = validate_production_observation_receipt(
        exact.receipt,
        authenticated_binding=authenticated_binding,
        expected_journey_id=expected_journey_id,
    )
    expected = _production_bundle_from_receipt(
        exact.receipt,
        identity=authenticated_binding.release,
        journey_id=expected_journey_id,
    )
    if exact != expected:
        raise ExactReleaseEvidenceError("production_observation_bundle_invalid")
    return value


def produce_receipt(
    *,
    repo_root: Path,
    release_root: Path,
    journey_id: str,
    evidence_class: str,
    authenticated_owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
) -> bytes:
    """Produce evidence, optionally binding a separately authenticated smoke token.

    Constructing the token is not authentication.  Callers may pass one only
    after a separate authenticator established the exact expected binding;
    consumers must independently supply that same expected token to validation.
    """

    if evidence_class == _PRODUCTION_OBSERVATION_CLASS:
        raise ExactReleaseEvidenceError("production_observation_external_binding_required")
    if _requires_candidate_runtime(journey_id, evidence_class):
        raise ExactReleaseEvidenceError("clean_artifact_bundle_required")
    identity = derive_release_identity(release_root)
    raw = _produce_for_identity(
        repo_root=repo_root,
        identity=identity,
        journey_id=journey_id,
        evidence_class=evidence_class,
        owner_smoke=authenticated_owner_smoke,
    )
    if derive_release_identity(release_root) != identity:
        raise ExactReleaseEvidenceError("release_identity_changed")
    return raw


def manifest_from_receipt(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
    expected_journey_id: str,
    expected_evidence_class: str,
    repo_root: Path,
    authenticated_owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
    release_root: Path | None = None,
) -> EvidenceBundle:
    """Revalidate machine evidence and derive its only canonical manifest."""

    if expected_evidence_class == _PRODUCTION_OBSERVATION_CLASS:
        raise ExactReleaseEvidenceError("production_observation_external_binding_required")
    validate_receipt(
        raw,
        expected_release=expected_release,
        expected_journey_id=expected_journey_id,
        expected_evidence_class=expected_evidence_class,
        repo_root=repo_root,
        authenticated_owner_smoke=authenticated_owner_smoke,
        release_root=release_root,
    )
    return _bundle_from_receipt(
        raw,
        identity=expected_release,
        journey_id=expected_journey_id,
        evidence_class=expected_evidence_class,
    )


def produce_evidence_bundle(
    *,
    repo_root: Path,
    release_root: Path,
    journey_id: str,
    evidence_class: str,
    authenticated_owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
) -> EvidenceBundle:
    """Run the closed verifier and derive receipt plus manifest without caller claims."""

    if evidence_class == _PRODUCTION_OBSERVATION_CLASS:
        raise ExactReleaseEvidenceError("production_observation_external_binding_required")
    if _requires_candidate_runtime(journey_id, evidence_class):
        runtime = _authenticate_release_runtime(release_root)
        identity = runtime.identity
    else:
        runtime = None
        identity = derive_release_identity(release_root)
    if runtime is None:
        raw = _produce_for_identity(
            repo_root=repo_root,
            identity=identity,
            journey_id=journey_id,
            evidence_class=evidence_class,
            owner_smoke=authenticated_owner_smoke,
        )
    else:
        raw = _produce_for_identity(
            repo_root=repo_root,
            identity=identity,
            journey_id=journey_id,
            evidence_class=evidence_class,
            owner_smoke=authenticated_owner_smoke,
            release_runtime=runtime,
        )
    if runtime is None:
        if derive_release_identity(release_root) != identity:
            raise ExactReleaseEvidenceError("release_identity_changed")
    else:
        _reauthenticate_release_runtime(runtime)
    return _bundle_from_receipt(
        raw,
        identity=identity,
        journey_id=journey_id,
        evidence_class=evidence_class,
    )


def _cleanup_owned_target(target: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        current = os.lstat(target)
        if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            os.unlink(target)
    except OSError:
        pass


def _write_canonical_exclusive(
    path: Path,
    raw: bytes,
    *,
    failure_code: str,
) -> tuple[str, tuple[int, int]]:
    try:
        target = Path(os.path.abspath(path))
        parent = target.parent.resolve(strict=True)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ExactReleaseEvidenceError(failure_code) from exc
    if parent != target.parent or target.name in {"", ".", ".."}:
        raise ExactReleaseEvidenceError(failure_code)
    descriptor = -1
    staging: Path | None = None
    owned_identity: tuple[int, int] | None = None
    linked = False
    try:
        descriptor, staging_text = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=parent,
        )
        staging = Path(staging_text)
        opened = os.fstat(descriptor)
        owned_identity = (opened.st_dev, opened.st_ino)
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("receipt write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
            or status.st_size != len(raw)
        ):
            raise OSError("receipt postcondition failed")
        os.close(descriptor)
        descriptor = -1
        os.link(staging, target, follow_symlinks=False)
        linked = True
        published = os.lstat(target)
        if (
            not stat.S_ISREG(published.st_mode)
            or (published.st_dev, published.st_ino) != owned_identity
            or stat.S_IMODE(published.st_mode) != 0o600
            or published.st_nlink != 2
            or published.st_uid != os.geteuid()
            or published.st_size != len(raw)
        ):
            raise OSError("receipt publication postcondition failed")
        os.unlink(staging)
        staging = None
        published = os.lstat(target)
        if (published.st_dev, published.st_ino) != owned_identity or published.st_nlink != 1:
            raise OSError("receipt publication link count invalid")
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return hashlib.sha256(raw).hexdigest(), owned_identity
    except BaseException as exc:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if staging is not None:
            _cleanup_owned_target(staging, owned_identity)
        if linked:
            _cleanup_owned_target(target, owned_identity)
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise ExactReleaseEvidenceError(failure_code) from exc


def write_receipt_exclusive(path: Path, raw: bytes) -> str:
    """Write one complete canonical receipt without replacing an existing name."""

    receipt = _load_canonical_receipt(raw)
    if receipt.get("$schema") == PRODUCTION_OBSERVATION_RECEIPT_SCHEMA:
        raise ExactReleaseEvidenceError("production_observation_bundle_required")
    if receipt.get("$schema") == CLEAN_ARTIFACT_RECEIPT_SCHEMA or _requires_candidate_runtime(
        receipt.get("journey_id"),
        receipt.get("evidence_class"),
    ):
        raise ExactReleaseEvidenceError("clean_artifact_bundle_required")
    digest, _identity = _write_canonical_exclusive(
        path,
        raw,
        failure_code="receipt_output_invalid",
    )
    return digest


def _require_evidence_bundle(value: object) -> EvidenceBundle:
    if type(value) is not EvidenceBundle or value.authority is not _EVIDENCE_BUNDLE_AUTHORITY:
        raise ExactReleaseEvidenceError("evidence_bundle_invalid")
    if (
        _SHA256.fullmatch(value.receipt_sha256) is None
        or _SHA256.fullmatch(value.manifest_sha256) is None
        or hashlib.sha256(value.receipt).hexdigest() != value.receipt_sha256
        or hashlib.sha256(value.manifest).hexdigest() != value.manifest_sha256
        or value.result not in {"VERIFIED", "FAILED"}
    ):
        raise ExactReleaseEvidenceError("evidence_bundle_invalid")
    receipt = _load_canonical_receipt(value.receipt)
    manifest = _load_canonical_manifest(value.manifest)
    observation = manifest.get("observation")
    release = receipt.get("release")
    production_observation = receipt.get("$schema") == PRODUCTION_OBSERVATION_RECEIPT_SCHEMA
    expected_manifest_schema = (
        PRODUCTION_OBSERVATION_MANIFEST_SCHEMA if production_observation else MANIFEST_SCHEMA
    )
    expected_observation_fields = (
        _PRODUCTION_OBSERVATION_MANIFEST_OBSERVATION_FIELDS
        if production_observation
        else _MANIFEST_OBSERVATION_FIELDS
    )
    if (
        set(manifest) != _MANIFEST_FIELDS
        or manifest.get("$schema") != expected_manifest_schema
        or type(observation) is not dict
        or set(observation) != expected_observation_fields
        or type(release) is not dict
        or set(release) != {"database_schema", "source_commit", "tree_sha256", "wheel_sha256"}
        or manifest.get("result") != value.result
        or observation.get("artifact_ref") != value.receipt_ref
        or observation.get("artifact_sha256") != value.receipt_sha256
        or receipt.get("result") != value.result
        or not value.manifest_ref.startswith(str(_EVIDENCE_ROOT / "manifests") + "/")
        or not value.receipt_ref.startswith(str(_EVIDENCE_ROOT / "receipts") + "/")
    ):
        raise ExactReleaseEvidenceError("evidence_bundle_invalid")
    try:
        identity = ReleaseIdentity(
            source_commit=release["source_commit"],
            tree_sha256=release["tree_sha256"],
            wheel_sha256=release["wheel_sha256"],
            database_schema=release["database_schema"],
        )
        expected = _bundle_from_receipt(
            value.receipt,
            identity=identity,
            journey_id=receipt["journey_id"],
            evidence_class=receipt["evidence_class"],
        )
    except (KeyError, TypeError, ExactReleaseEvidenceError) as exc:
        raise ExactReleaseEvidenceError("evidence_bundle_invalid") from exc
    comparable = (
        "receipt_ref",
        "receipt",
        "receipt_sha256",
        "manifest_ref",
        "manifest",
        "manifest_sha256",
        "result",
    )
    if any(getattr(value, field) != getattr(expected, field) for field in comparable):
        raise ExactReleaseEvidenceError("evidence_bundle_invalid")
    return value


@dataclass(frozen=True, slots=True)
class _PinnedBundleRoot:
    path: Path
    parent_descriptor: int
    parent_parts: tuple[str, ...]
    parent_identities: tuple[tuple[int, int], ...]
    descriptor: int
    identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _PinnedBundleParent:
    descriptor: int
    parts: tuple[str, ...]
    identities: tuple[tuple[int, int], ...]
    name: str


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _status_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _trusted_bundle_parent(value: os.stat_result) -> bool:
    mode = stat.S_IMODE(value.st_mode)
    trusted_private = value.st_uid == os.geteuid() and mode == 0o700
    trusted_sticky = (
        bool(value.st_mode & stat.S_ISVTX) and value.st_uid in {0, os.geteuid()} and bool(mode & 0o002)
    )
    return stat.S_ISDIR(value.st_mode) and (trusted_private or trusted_sticky)


def _open_absolute_directory_chain(
    value: Path,
) -> tuple[int, tuple[str, ...], tuple[tuple[int, int], ...]]:
    """Open one physical absolute directory one no-follow component at a time."""

    parts = tuple(value.parts[1:])
    current = -1
    identities: list[tuple[int, int]] = []
    try:
        if (
            not value.is_absolute()
            or value.anchor != os.sep
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        current = os.open(os.sep, _directory_open_flags())
        root_status = os.fstat(current)
        if not stat.S_ISDIR(root_status.st_mode):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        identities.append(_status_identity(root_status))
        for part in parts:
            child = os.open(part, _directory_open_flags(), dir_fd=current)
            try:
                opened = os.fstat(child)
                named = os.stat(part, dir_fd=current, follow_symlinks=False)
                if not stat.S_ISDIR(opened.st_mode) or _status_identity(opened) != _status_identity(named):
                    raise ExactReleaseEvidenceError("bundle_output_invalid")
                identities.append(_status_identity(opened))
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current, parts, tuple(identities)
    except (OSError, RuntimeError, ValueError) as exc:
        if current >= 0:
            with suppress(OSError):
                os.close(current)
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc


def _require_pinned_absolute_directory(
    descriptor: int,
    parts: tuple[str, ...],
    identities: tuple[tuple[int, int], ...],
) -> os.stat_result:
    """Rewalk every lexical component and bind it to one held directory fd."""

    current = -1
    try:
        if len(identities) != len(parts) + 1:
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        current = os.open(os.sep, _directory_open_flags())
        opened = os.fstat(current)
        if not stat.S_ISDIR(opened.st_mode) or _status_identity(opened) != identities[0]:
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        for part, expected_identity in zip(parts, identities[1:], strict=True):
            child = os.open(part, _directory_open_flags(), dir_fd=current)
            try:
                opened = os.fstat(child)
                named = os.stat(part, dir_fd=current, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or _status_identity(opened) != expected_identity
                    or _status_identity(named) != expected_identity
                ):
                    raise ExactReleaseEvidenceError("bundle_output_invalid")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        held = os.fstat(descriptor)
        if _status_identity(opened) != _status_identity(held):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        return held
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
    finally:
        if current >= 0:
            with suppress(OSError):
                os.close(current)


def _require_pinned_bundle_root(value: _PinnedBundleRoot) -> None:
    lexical_root_descriptor = -1
    try:
        try:
            parent_status = _require_pinned_absolute_directory(
                value.parent_descriptor,
                value.parent_parts,
                value.parent_identities,
            )
            root_status = os.fstat(value.descriptor)
            lexical_root_descriptor = os.open(
                value.path.name,
                _directory_open_flags(),
                dir_fd=value.parent_descriptor,
            )
            lexical_root = os.fstat(lexical_root_descriptor)
            named_root = os.stat(
                value.path.name,
                dir_fd=value.parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
        if (
            not _trusted_bundle_parent(parent_status)
            or _status_identity(root_status) != value.identity
            or _status_identity(lexical_root) != value.identity
            or _status_identity(named_root) != value.identity
            or not stat.S_ISDIR(root_status.st_mode)
            or stat.S_IMODE(root_status.st_mode) != 0o700
            or root_status.st_uid != os.geteuid()
        ):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
    finally:
        if lexical_root_descriptor >= 0:
            with suppress(OSError):
                os.close(lexical_root_descriptor)


@contextmanager
def _exclusive_bundle_root(root: Path) -> Iterator[_PinnedBundleRoot]:
    """Pin and lock one owner-private lexical root for descriptor-only publication."""

    parent_descriptor = -1
    descriptor = -1
    locked = False
    try:
        path = Path(os.path.abspath(root))
        parent = path.parent
        if path.name in {"", ".", ".."}:
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        parent_descriptor, parent_parts, parent_identities = _open_absolute_directory_chain(parent)
        parent_status = os.fstat(parent_descriptor)
        if not _trusted_bundle_parent(parent_status):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        descriptor = os.open(path.name, _directory_open_flags(), dir_fd=parent_descriptor)
        status = os.fstat(descriptor)
        pinned = _PinnedBundleRoot(
            path=path,
            parent_descriptor=parent_descriptor,
            parent_parts=parent_parts,
            parent_identities=parent_identities,
            descriptor=descriptor,
            identity=_status_identity(status),
        )
        _require_pinned_bundle_root(pinned)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        _require_pinned_bundle_root(pinned)
        yield pinned
        _require_pinned_bundle_root(pinned)
    except (OSError, RuntimeError) as exc:
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
    finally:
        if descriptor >= 0:
            if locked:
                with suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            with suppress(OSError):
                os.close(descriptor)
        if parent_descriptor >= 0:
            with suppress(OSError):
                os.close(parent_descriptor)


def _validate_bundle_ref(ref: str) -> PurePosixPath:
    candidate = PurePosixPath(ref)
    if (
        candidate.is_absolute()
        or str(candidate) != ref
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or tuple(candidate.parts[:2]) != ("evidence", "golden_journeys")
    ):
        raise ExactReleaseEvidenceError("bundle_output_invalid")
    return candidate


def _ensure_bundle_parent_at(root_descriptor: int, ref: str) -> _PinnedBundleParent:
    """Open one private parent chain and durably order every namespace edge."""

    candidate = _validate_bundle_ref(ref)
    current = -1
    identities: list[tuple[int, int]] = []
    try:
        current = os.dup(root_descriptor)
        for part in candidate.parts[:-1]:
            with suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=current)
            child = os.open(part, _directory_open_flags(), dir_fd=current)
            try:
                opened = os.fstat(child)
                named = os.stat(part, dir_fd=current, follow_symlinks=False)
                if (
                    _status_identity(opened) != _status_identity(named)
                    or not stat.S_ISDIR(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) != 0o700
                    or opened.st_uid != os.geteuid()
                ):
                    raise ExactReleaseEvidenceError("bundle_output_invalid")
                identities.append(_status_identity(opened))
                os.fsync(child)
                os.fsync(current)
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return _PinnedBundleParent(
            descriptor=current,
            parts=tuple(candidate.parts[:-1]),
            identities=tuple(identities),
            name=candidate.name,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if current >= 0:
            with suppress(OSError):
                os.close(current)
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc


def _require_pinned_bundle_parent(
    root_descriptor: int,
    value: _PinnedBundleParent,
) -> None:
    current = -1
    try:
        current = os.dup(root_descriptor)
        for part, expected_identity in zip(value.parts, value.identities, strict=True):
            child = os.open(part, _directory_open_flags(), dir_fd=current)
            try:
                opened = os.fstat(child)
                named = os.stat(part, dir_fd=current, follow_symlinks=False)
                if (
                    _status_identity(opened) != expected_identity
                    or _status_identity(named) != expected_identity
                    or not stat.S_ISDIR(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) != 0o700
                    or opened.st_uid != os.geteuid()
                ):
                    raise ExactReleaseEvidenceError("bundle_output_invalid")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        if _status_identity(os.fstat(current)) != _status_identity(os.fstat(value.descriptor)):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
    finally:
        if current >= 0:
            with suppress(OSError):
                os.close(current)


def _leaf_status_at(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc


def _stable_file_at(
    parent_descriptor: int,
    name: str,
    maximum_bytes: int,
) -> tuple[bytes, os.stat_result]:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum_bytes:
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        stable = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) != before.st_size
            or any(getattr(before, field) != getattr(after, field) for field in stable)
            or _status_identity(named) != _status_identity(after)
        ):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        return raw, after
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _cleanup_owned_target_at(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int] | None,
) -> None:
    if identity is None:
        return
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISREG(current.st_mode) and _status_identity(current) == identity:
            os.unlink(name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
    except OSError:
        pass


def _durably_remove_owned_manifest_at(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int] | None,
) -> bool:
    """Return true only after the manifest name is absent and that absence is durable."""

    try:
        current = _leaf_status_at(parent_descriptor, name)
        if current is not None:
            if identity is None or not stat.S_ISREG(current.st_mode) or _status_identity(current) != identity:
                return False
            os.unlink(name, dir_fd=parent_descriptor)
        if _leaf_status_at(parent_descriptor, name) is not None:
            return False
        os.fsync(parent_descriptor)
        return _leaf_status_at(parent_descriptor, name) is None
    except (OSError, RuntimeError, ExactReleaseEvidenceError):
        return False


def _write_canonical_exclusive_at(
    parent_descriptor: int,
    name: str,
    raw: bytes,
) -> tuple[str, tuple[int, int]]:
    descriptor = -1
    staging_name: str | None = None
    owned_identity: tuple[int, int] | None = None
    linked = False
    try:
        for _attempt in range(32):
            candidate = f".{name}.{os.urandom(8).hex()}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_descriptor,
                )
                staging_name = candidate
                break
            except FileExistsError:
                continue
        if descriptor < 0 or staging_name is None:
            raise OSError("staging name unavailable")
        opened = os.fstat(descriptor)
        owned_identity = _status_identity(opened)
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("bundle write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
            or status.st_size != len(raw)
        ):
            raise OSError("bundle staging postcondition failed")
        os.close(descriptor)
        descriptor = -1
        os.link(
            staging_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        linked = True
        published = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(published.st_mode)
            or _status_identity(published) != owned_identity
            or stat.S_IMODE(published.st_mode) != 0o600
            or published.st_nlink != 2
            or published.st_uid != os.geteuid()
            or published.st_size != len(raw)
        ):
            raise OSError("bundle publication postcondition failed")
        os.unlink(staging_name, dir_fd=parent_descriptor)
        staging_name = None
        published = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _status_identity(published) != owned_identity or published.st_nlink != 1:
            raise OSError("bundle publication link count invalid")
        os.fsync(parent_descriptor)
        return hashlib.sha256(raw).hexdigest(), owned_identity
    except BaseException as exc:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if staging_name is not None:
            _cleanup_owned_target_at(parent_descriptor, staging_name, owned_identity)
        if linked:
            _cleanup_owned_target_at(parent_descriptor, name, owned_identity)
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc


def _require_exact_existing_bundle_file_at(parent_descriptor: int, name: str, raw: bytes) -> str:
    """Adopt and durably heal only one byte-identical create-only artifact."""

    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_nlink not in {1, 2}
            or status.st_uid != os.geteuid()
            or status.st_size != len(raw)
        ):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        validated_identity = _status_identity(status)
        stable = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        remaining = len(raw) + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        existing = b"".join(chunks)
        after_read = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            existing != raw
            or any(getattr(status, field) != getattr(after_read, field) for field in stable)
            or _status_identity(named) != validated_identity
        ):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        if status.st_nlink == 2:
            prefix = f".{name}."
            candidates = []
            for candidate in os.listdir(parent_descriptor):
                if candidate.startswith(prefix) and candidate.endswith(".tmp"):
                    candidate_status = os.stat(
                        candidate,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    if _status_identity(candidate_status) == _status_identity(status):
                        candidates.append(candidate)
            if len(candidates) != 1:
                raise ExactReleaseEvidenceError("bundle_output_invalid")
            os.unlink(candidates[0], dir_fd=parent_descriptor)
        ready = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            _status_identity(ready) != validated_identity
            or _status_identity(named) != validated_identity
            or not stat.S_ISREG(ready.st_mode)
            or stat.S_IMODE(ready.st_mode) != 0o600
            or ready.st_nlink != 1
            or ready.st_uid != os.geteuid()
            or ready.st_size != len(raw)
        ):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        remaining = len(raw) + 1
        chunks = []
        before_fsync = os.fstat(descriptor)
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        verified = b"".join(chunks)
        after_verify = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            verified != raw
            or any(getattr(before_fsync, field) != getattr(after_verify, field) for field in stable)
            or _status_identity(named) != validated_identity
        ):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        os.fsync(descriptor)
        durable = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            any(getattr(after_verify, field) != getattr(durable, field) for field in stable)
            or _status_identity(named) != validated_identity
        ):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        os.fsync(parent_descriptor)
        final = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            any(getattr(durable, field) != getattr(final, field) for field in stable)
            or _status_identity(named) != validated_identity
        ):
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        return hashlib.sha256(verified).hexdigest()
    except (OSError, RuntimeError, ExactReleaseEvidenceError) as exc:
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _recover_existing_observation_bundle_at(
    parent_descriptor: int,
    name: str,
    fresh: EvidenceBundle,
) -> EvidenceBundle:
    """Reuse only the first timestamp when every other canonical field is identical."""

    status = _leaf_status_at(parent_descriptor, name)
    if status is None:
        return fresh
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_IMODE(status.st_mode) != 0o600
        or status.st_nlink not in {1, 2}
        or status.st_uid != os.geteuid()
    ):
        raise ExactReleaseEvidenceError("bundle_output_invalid")
    try:
        existing_raw, _stable_status = _stable_file_at(parent_descriptor, name, len(fresh.receipt))
        existing = _load_canonical_receipt(existing_raw)
        current = _load_canonical_receipt(fresh.receipt)
        existing_projection = dict(existing)
        current_projection = dict(current)
        existing_projection.pop("observed_at_utc", None)
        current_projection.pop("observed_at_utc", None)
        if existing_projection != current_projection:
            return fresh
        release = current["release"]
        identity = ReleaseIdentity(
            source_commit=release["source_commit"],
            tree_sha256=release["tree_sha256"],
            wheel_sha256=release["wheel_sha256"],
            database_schema=release["database_schema"],
        )
        recovered = _bundle_from_receipt(
            existing_raw,
            identity=identity,
            journey_id=current["journey_id"],
            evidence_class=current["evidence_class"],
        )
    except (KeyError, TypeError, ExactReleaseEvidenceError) as exc:
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
    if recovered.receipt_ref != fresh.receipt_ref or recovered.manifest_ref != fresh.manifest_ref:
        raise ExactReleaseEvidenceError("bundle_output_invalid")
    return recovered


def write_evidence_bundle_exclusive(output_root: Path, bundle: EvidenceBundle) -> dict[str, str]:
    """Publish receipt then commit manifest through one pinned private root."""

    exact = _require_evidence_bundle(bundle)
    with _exclusive_bundle_root(output_root) as root:
        _require_pinned_bundle_root(root)
        os.fsync(root.descriptor)
        os.fsync(root.parent_descriptor)
        _require_pinned_bundle_root(root)
        receipt_parent = _ensure_bundle_parent_at(root.descriptor, exact.receipt_ref)
        manifest_parent: _PinnedBundleParent | None = None
        receipt_identity: tuple[int, int] | None = None
        manifest_identity: tuple[int, int] | None = None
        try:
            manifest_parent = _ensure_bundle_parent_at(
                root.descriptor,
                exact.manifest_ref,
            )
            _require_pinned_bundle_parent(root.descriptor, receipt_parent)
            _require_pinned_bundle_parent(root.descriptor, manifest_parent)
            _require_pinned_bundle_root(root)
            receipt_status = _leaf_status_at(receipt_parent.descriptor, receipt_parent.name)
            manifest_status = _leaf_status_at(manifest_parent.descriptor, manifest_parent.name)
            if manifest_status is not None and receipt_status is None:
                raise ExactReleaseEvidenceError("bundle_output_invalid")
            exact = _recover_existing_observation_bundle_at(
                receipt_parent.descriptor,
                receipt_parent.name,
                exact,
            )
            try:
                receipt_digest, receipt_identity = _write_canonical_exclusive_at(
                    receipt_parent.descriptor,
                    receipt_parent.name,
                    exact.receipt,
                )
            except ExactReleaseEvidenceError:
                receipt_digest = _require_exact_existing_bundle_file_at(
                    receipt_parent.descriptor,
                    receipt_parent.name,
                    exact.receipt,
                )
            os.fsync(receipt_parent.descriptor)
            _require_pinned_bundle_parent(root.descriptor, receipt_parent)
            _require_pinned_bundle_parent(root.descriptor, manifest_parent)
            _require_pinned_bundle_root(root)
            try:
                manifest_digest, manifest_identity = _write_canonical_exclusive_at(
                    manifest_parent.descriptor,
                    manifest_parent.name,
                    exact.manifest,
                )
            except ExactReleaseEvidenceError:
                manifest_digest = _require_exact_existing_bundle_file_at(
                    manifest_parent.descriptor,
                    manifest_parent.name,
                    exact.manifest,
                )
            _require_pinned_bundle_parent(root.descriptor, receipt_parent)
            _require_pinned_bundle_parent(root.descriptor, manifest_parent)
            _require_pinned_bundle_root(root)
        except BaseException:
            manifest_absence_is_durable = False
            if manifest_parent is not None:
                manifest_absence_is_durable = _durably_remove_owned_manifest_at(
                    manifest_parent.descriptor,
                    manifest_parent.name,
                    manifest_identity,
                )
            if manifest_absence_is_durable:
                _cleanup_owned_target_at(
                    receipt_parent.descriptor,
                    receipt_parent.name,
                    receipt_identity,
                )
            raise
        finally:
            if manifest_parent is not None:
                with suppress(OSError):
                    os.close(manifest_parent.descriptor)
            with suppress(OSError):
                os.close(receipt_parent.descriptor)
        return {
            "manifest_ref": exact.manifest_ref,
            "manifest_sha256": manifest_digest,
            "receipt_ref": exact.receipt_ref,
            "receipt_sha256": receipt_digest,
            "result": exact.result,
        }


def _external_bundle_output_root(repo_root: Path, output_root: Path) -> Path:
    repository = _resolve_directory(repo_root, "repo_root_invalid")
    try:
        output = Path(os.path.abspath(output_root))
        resolved_output = output.resolve(strict=True)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
    if resolved_output == repository or resolved_output.is_relative_to(repository):
        raise ExactReleaseEvidenceError("bundle_output_must_be_external")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    candidate_runtime_pairs = tuple(
        (journey_id, evidence_class)
        for journey_id, evidence_class in _PROOF_REFS_BY_JOURNEY_CLASS
        if _requires_candidate_runtime(journey_id, evidence_class)
    )
    validate = commands.add_parser("validate")
    validate.add_argument("--repo-root", required=True, type=Path)
    validate.add_argument("--release-root", required=True, type=Path)
    validate.add_argument("--expected-source-commit", required=True)
    validate.add_argument("--expected-tree-sha256", required=True)
    validate.add_argument("--expected-wheel-sha256", required=True)
    validate.add_argument("--expected-database-schema", required=True, type=int)
    validate.add_argument(
        "--journey-id",
        required=True,
        choices=sorted({journey_id for journey_id, _evidence_class in candidate_runtime_pairs}),
    )
    validate.add_argument(
        "--evidence-class",
        required=True,
        choices=sorted({evidence_class for _journey_id, evidence_class in candidate_runtime_pairs}),
    )
    run = commands.add_parser("run")
    run.add_argument("--release-root", required=True, type=Path)
    run.add_argument("--repo-root", required=True, type=Path)
    run.add_argument(
        "--journey-id", required=True, choices=sorted({key[0] for key in _PROOF_REFS_BY_JOURNEY_CLASS})
    )
    run.add_argument(
        "--evidence-class",
        required=True,
        choices=sorted({key[1] for key in _PROOF_REFS_BY_JOURNEY_CLASS} - {"clean artifact path"}),
    )
    run.add_argument("--output", required=True, type=Path)
    bundle = commands.add_parser("bundle")
    bundle.add_argument("--release-root", required=True, type=Path)
    bundle.add_argument("--repo-root", required=True, type=Path)
    bundle.add_argument(
        "--journey-id",
        required=True,
        choices=sorted({journey_id for journey_id, _evidence_class in candidate_runtime_pairs}),
    )
    bundle.add_argument(
        "--evidence-class",
        default=_CLEAN_ARTIFACT_CLASS,
        choices=sorted({evidence_class for _journey_id, evidence_class in candidate_runtime_pairs}),
    )
    bundle.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        _require_producer_process_authority()
        args = build_parser().parse_args(argv)
        if args.command == "validate":
            if (
                not _requires_candidate_runtime(args.journey_id, args.evidence_class)
                or args.release_root is None
            ):
                raise ExactReleaseEvidenceError("native_validation_scope_invalid")
            _require_native_validation_context()
            raw = sys.stdin.buffer.read(65_537)
            expected_release = ReleaseIdentity(
                source_commit=args.expected_source_commit,
                tree_sha256=args.expected_tree_sha256,
                wheel_sha256=args.expected_wheel_sha256,
                database_schema=args.expected_database_schema,
            )
            receipt = validate_receipt(
                raw,
                expected_release=expected_release,
                expected_journey_id=args.journey_id,
                expected_evidence_class=args.evidence_class,
                repo_root=args.repo_root,
                release_root=args.release_root,
            )
            attestation = _validation_attestation(
                raw,
                receipt,
                expected_release=expected_release,
                expected_journey_id=args.journey_id,
                expected_evidence_class=args.evidence_class,
                release_root=args.release_root,
            )
            print(canonical_json_bytes(attestation).decode())
            return 0
        if args.command == "bundle":
            if not _requires_candidate_runtime(args.journey_id, args.evidence_class):
                raise ExactReleaseEvidenceError("candidate_runtime_bundle_scope_invalid")
            output_root = _external_bundle_output_root(args.repo_root, args.output_root)
            bundle = produce_evidence_bundle(
                repo_root=args.repo_root,
                release_root=args.release_root,
                journey_id=args.journey_id,
                evidence_class=args.evidence_class,
            )
            published = write_evidence_bundle_exclusive(output_root, bundle)
            print(canonical_json_bytes(published).decode())
            return 0 if published["result"] == "VERIFIED" else 1
        raw = produce_receipt(
            repo_root=args.repo_root,
            release_root=args.release_root,
            journey_id=args.journey_id,
            evidence_class=args.evidence_class,
        )
        digest = write_receipt_exclusive(args.output, raw)
        receipt = _load_canonical_receipt(raw)
        print(canonical_json_bytes({"receipt_sha256": digest, "result": receipt["result"]}).decode())
        return 0 if receipt["result"] == "VERIFIED" else 1
    except ExactReleaseEvidenceError as exc:
        print(canonical_json_bytes({"failure_code": str(exc), "status": "failed_closed"}).decode())
        return 2
    except Exception:  # Last-resort CLI boundary: never emit a runtime traceback.
        print(
            canonical_json_bytes(
                {"failure_code": "unexpected_runtime_failure", "status": "failed_closed"}
            ).decode()
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
