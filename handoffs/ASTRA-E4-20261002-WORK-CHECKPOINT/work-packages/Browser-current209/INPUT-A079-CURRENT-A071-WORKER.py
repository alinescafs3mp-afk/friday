"""Exact held worker entry, after native clone registration ACK and limits.
The only production transport is unchanged G1 HTTPSConnection/worker.
Benign fixture transport changes external I/O only and has zero network effects.
"""
import fcntl
import hashlib
import io
import json
import os
import resource
import select
import ssl
import stat
import sys
import time
import types

G1_SHA="ba70d584ab4b145195283456b17d98a8ff129a8f7bb3be3595c711fe97d6c223"
CA_SHA="80eedd808e4cbd6fd42e125da2ea225fd1365d8e29edef8bcc45ff8bc7044ce2"
SOURCE="/var/tmp/friday-astra-browser-native-authoritative-caller-a079-g1/A071-CONTRACT.py"
REQUIRED=fcntl.F_SEAL_WRITE|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL

def raw(fd,cap,root):
    st=os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_size>cap or (root and (st.st_uid or st.st_gid)) or fcntl.fcntl(fd,fcntl.F_GET_SEALS)&REQUIRED!=REQUIRED:raise RuntimeError("WORKER_HELD_CUSTODY")
    data=os.pread(fd,cap+1,0)
    if len(data)!=st.st_size:raise RuntimeError("WORKER_HELD_SIZE")
    return data

def expected(index):
    values=(
      ("chrome-linux64.zip",536870912,"storage.googleapis.com","/chrome-for-testing-public/149.0.7827.55/linux64/chrome-linux64.zip"),
      ("chrome-headless-shell-linux64.zip",402653184,"storage.googleapis.com","/chrome-for-testing-public/149.0.7827.55/linux64/chrome-headless-shell-linux64.zip"),
      ("ffmpeg-linux.zip",16777216,"cdn.playwright.dev","/dbazure/download/playwright/builds/ffmpeg/1011/ffmpeg-linux.zip"))
    if type(index) is not int or not 0<=index<3:raise RuntimeError("WORKER_INDEX")
    name,cap,host,path=values[index]
    return {"kind":"browser","url":"https://"+host+path,"host":host,"path":path,
            "relative_path":"archives/playwright/"+name,"cap":cap,"size":None,"sha256":None,"seconds":300}

def benign_transport(m,entry,fixture,index):
    if fixture.get("schema")!="friday.a061.benign-fixture.v1":raise RuntimeError("BENIGN_SCHEMA")
    wire=fixture["responses"][index]
    if type(wire) is not dict or set(wire)!={"mode","payload"} or wire["mode"] not in (
        "positive","http404","certificate","protocol","truncated","short_write","partial_write"):raise RuntimeError("BENIGN_RESPONSE")
    mode=wire["mode"]
    payload=wire["payload"].encode("ascii","strict")
    if not 0<len(payload)<=4096:raise RuntimeError("BENIGN_BODY_BOUND")
    if index!=2:mode="positive"
    data=("HTTP/1.1 "+("404 Missing" if mode=="http404" else "200 OK")+"\r\nContent-Length: "+str(len(payload)+(mode=="truncated"))+"\r\nContent-Type: application/zip\r\n\r\n").encode()+payload
    class Peer:
        def getpeercert(self,binary_form=False):
            if binary_form is not True:raise RuntimeError("BENIGN_CERT_FORMAT")
            return b"owned-benign-peer"
        def version(self):return "TLSv1.1" if mode=="protocol" else "TLSv1.3"
        def makefile(self,which):
            if which!="rb":raise RuntimeError("BENIGN_RAW_HTTP")
            return io.BytesIO(data)
    class Connection:
        def __init__(self,host,port,timeout,context):
            if host!=entry["host"] or port!=443 or timeout!=15 or not context.check_hostname or context.verify_mode!=ssl.CERT_REQUIRED:raise RuntimeError("BENIGN_DIRECT_TLS")
            self.sock=Peer();self.response_class=None
        def connect(self):
            if mode=="certificate":raise ssl.SSLCertVerificationError("owned benign certificate")
        def request(self,method,path,headers):
            if method!="GET" or path!=entry["path"] or headers["Accept-Encoding"]!="identity" or headers["Connection"]!="close" or set(headers)!={"Accept-Encoding","Connection","User-Agent"}:raise RuntimeError("BENIGN_DIRECT_GET")
        def getresponse(self):
            if self.response_class is not m.BoundedResponse:raise RuntimeError("BENIGN_REAL_RESPONSE")
            response=self.response_class(self.sock,method="GET");response.begin();return response
        def close(self):pass
    m.http.client.HTTPSConnection=Connection
    actual_write=os.write
    def write(fd,b):
        if fd==125 and mode=="short_write":return 0
        return actual_write(fd,b[:3] if fd==125 and mode=="partial_write" else b)
    os.write=write
    actual_limit=resource.setrlimit
    def limit(kind,wanted):
        # Controls CPU180 is independently fixed by the native pre-exec parent.
        # Production calls the unchanged worker with its original CPU330 request.
        inherited=resource.getrlimit(kind)[1]
        if kind==resource.RLIMIT_CPU:wanted=(min(wanted[0],inherited),min(wanted[1],inherited))
        return actual_limit(kind,wanted)
    resource.setrlimit=limit
    def deny(event,args):
        if event.startswith(("socket.","subprocess.")) or event in ("os.exec","os.posix_spawn"):
            raise m.Refused("BENIGN_EXTERNAL_EFFECT_DENIED")
    sys.addaudithook(deny)

def main():
    if not(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode and
           dict(os.environ)=={"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"}):raise RuntimeError("WORKER_ENV")
    config_raw=raw(124,4096,False)
    # Parse only after bounded inert source primitive is loaded from fixed105.
    source=raw(105,1048576,True)
    if hashlib.sha256(source).hexdigest()!=G1_SHA:raise RuntimeError("WORKER_G1_PIN")
    m=types.ModuleType("a061_held_g1_worker");m.__file__="/home/jericho/.jericho/runtime/subagent-lifecycle/ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1.py"
    exec(compile(source,m.__file__,"exec"),m.__dict__)
    cfg=m.inert_json(config_raw,4096)
    m.require(type(cfg) is dict and set(cfg)=={"schema","index","entry","capsule_sha"} and
              cfg["schema"]=="friday.a061.worker.v1" and cfg["entry"]==expected(cfg["index"]) and
              all(type(cfg["entry"][k]) is int for k in ("cap","seconds")),"WORKER_EXACT_CONFIGURATION")
    cap_raw=raw(100,4096,True)
    m.require(hashlib.sha256(cap_raw).hexdigest()==cfg["capsule_sha"],"WORKER_CAPSULE_PIN")
    # Fixed wire layout obtains contract SHA from the already root-sealed capsule;
    # native validates its independently pinned whole capsule before exec.
    import struct
    pin_offset=8+6*4+10*8+4*32+16*32
    contract_sha=cap_raw[pin_offset:pin_offset+32].hex()
    contract=raw(118,1048576,True)
    m.require(hashlib.sha256(contract).hexdigest()==contract_sha,"WORKER_CONTRACT_PIN")
    c=types.ModuleType("a061_contract");c.__file__=SOURCE;sys.modules[c.__name__]=c
    exec(compile(contract,SOURCE,"exec"),c.__dict__)
    cap=c.decode_capsule(cap_raw,cfg["capsule_sha"])
    c.need(cap["pins"]["G1"]==G1_SHA and cap["pins"]["CA"]==CA_SHA and cap["mode_id"] in (3,4),"WORKER_ROLE")
    c.need(os.getuid()==cap["uid"] and os.getgid()==cap["gid"] and resource.getrlimit(resource.RLIMIT_AS)==(50331648,50331648),"WORKER_PREEXEC")
    c.held(112,cap["pins"]["worker"],1048576)
    cap["image"]=c.decode_image(c.held(111,cap["image_sha"],16*1048576),cap["manifest_sha"])
    c.check_loaded_runtime(cap)
    ca=c.held(109,CA_SHA,1048576)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.check_hostname=True;context.verify_mode=ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=ca.decode("ascii","strict"))
    if cap["mode_id"]==4:
        fixture=m.inert_json(c.held(119,cap["pins"]["fixture"],1048576),1048576)
        benign_transport(m,cfg["entry"],fixture,cfg["index"])
        if cfg["index"]==2:
            end=min(cap["work_ns"],time.monotonic_ns()+15*10**9)
            ready=False
            while time.monotonic_ns()<end:
                if select.select([128],[],[],.01)[0]:
                    ready=os.read(128,1)==b"G"
                    break
            m.require(ready,"BENIGN_POSITIVE_PREFIX_BARRIER")
        os.close(128)
    m.worker(cfg["entry"],125,126,context)
    return 0

if __name__=="__main__":
    try:sys.exit(main())
    except SystemExit:raise
    except BaseException:os._exit(70)

