import os, signal, threading, subprocess
from typing import Optional

_tl = threading.local()
_active_procs: dict[str, subprocess.Popen] = {}
_cancel_events: dict[str, threading.Event] = {}

class JobCancelled(BaseException):
    pass

def set_job_id(job_id): _tl.job_id = job_id
def current_job_id(): return getattr(_tl, "job_id", None)

def register_proc(proc):
    jid = current_job_id()
    if jid: _active_procs[jid] = proc

def deregister_proc():
    jid = current_job_id()
    if jid: _active_procs.pop(jid, None)

def check_cancel():
    jid = current_job_id()
    if jid and is_cancelled(jid): raise JobCancelled(jid)

def is_cancelled(job_id):
    ev = _cancel_events.get(job_id)
    return ev.is_set() if ev else False

def cancel(job_id):
    ev = _cancel_events.get(job_id)
    if ev: ev.set()
    proc = _active_procs.pop(job_id, None)
    if proc and proc.poll() is None:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except: proc.kill()

def make_event(job_id):
    ev = threading.Event()
    _cancel_events[job_id] = ev
    return ev

def cleanup(job_id):
    _cancel_events.pop(job_id, None)
    _active_procs.pop(job_id, None)
