import os
import subprocess
import sys
import socket
import time
import signal
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("ZP_DATA", BASE_DIR / "data"))
LOGS_DIR = DATA_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

PROCS = {}
PORT_RANGE = range(9100, 9200)


def _free_port():
    used = {v["port"] for v in PROCS.values()}
    for p in PORT_RANGE:
        if p in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("لا يوجد منفذ حر")


def _pid_file(pid: str) -> Path:
    return LOGS_DIR / f"{pid}.pid"


def _save_pid(pid: str, os_pid: int, port: int):
    _pid_file(pid).write_text(json.dumps({"os_pid": os_pid, "port": port}))


def _clear_pid(pid: str):
    f = _pid_file(pid)
    if f.exists():
        f.unlink()


def _load_pid(pid: str):
    f = _pid_file(pid)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _detect_entry(project_dir: Path):
    for name in ("main.py", "app.py", "server.py", "run.py", "__main__.py"):
        if (project_dir / name).exists():
            return name
    return None


def _install_requirements(project_dir: Path):
    req = project_dir / "requirements.txt"
    if not req.exists():
        return {"ok": True, "skipped": True}
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req)],
            capture_output=True, text=True, timeout=600,
            cwd=str(project_dir)
        )
        return {"ok": r.returncode == 0, "stdout": r.stdout[-1500:], "stderr": r.stderr[-1500:]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def start(pid: str, project_dir: Path):
    if pid in PROCS and PROCS[pid]["proc"].poll() is None:
        return {"ok": False, "error": "already running", "port": PROCS[pid]["port"]}

    entry = _detect_entry(project_dir)
    if not entry:
        return {"ok": False, "error": "لم يُعثر على ملف دخول (main.py/app.py/server.py/run.py/__main__.py)"}

    libs = _install_requirements(project_dir)
    if not libs["ok"]:
        return {"ok": False, "error": "فشل تثبيت المتطلبات", "details": libs}

    port = _free_port()
    env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1"}
    log_path = LOGS_DIR / f"{pid}.log"
    log_file = open(log_path, "ab", buffering=0)

    try:
        proc = subprocess.Popen(
            [sys.executable, entry],
            cwd=str(project_dir),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid if os.name != "nt" else None
        )
    except Exception as e:
        log_file.close()
        return {"ok": False, "error": str(e)}

    PROCS[pid] = {"proc": proc, "port": port, "log_file": log_file}
    _save_pid(pid, proc.pid, port)

    for _ in range(40):
        time.sleep(0.25)
        if proc.poll() is not None:
            PROCS.pop(pid, None)
            _clear_pid(pid)
            log_file.close()
            tail = log_path.read_text(errors="replace")[-2000:] if log_path.exists() else ""
            return {"ok": False, "error": "العملية توقفت فوراً", "log": tail}
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return {"ok": True, "port": port, "entry": entry}

    return {"ok": False, "error": "انتهت المهلة دون فتح المنفذ"}


def stop(pid: str):
    info = PROCS.pop(pid, None)
    if info:
        proc = info["proc"]
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            info["log_file"].close()
        except Exception:
            pass
        _clear_pid(pid)
        return {"ok": True}

    # ربما العملية موجودة من تشغيل سابق
    saved = _load_pid(pid)
    if saved:
        try:
            os.kill(saved["os_pid"], signal.SIGTERM)
        except Exception:
            pass
        _clear_pid(pid)
    return {"ok": True, "msg": "not running in memory"}


def status(pid: str):
    info = PROCS.get(pid)
    if info:
        running = info["proc"].poll() is None
        return {"running": running, "port": info["port"] if running else None}
    saved = _load_pid(pid)
    if saved:
        return {"running": False, "port": saved.get("port"), "stale": True}
    return {"running": False}


def logs(pid: str, max_bytes=8000):
    log_path = LOGS_DIR / f"{pid}.log"
    if not log_path.exists():
        return ""
    try:
        data = log_path.read_bytes()
        return data[-max_bytes:].decode(errors="replace")
    except Exception:
        return ""