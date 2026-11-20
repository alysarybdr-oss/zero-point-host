import json
import os
import uuid
import time
import shutil
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("ZP_DATA", BASE_DIR / "data"))
PROJECTS_DIR = DATA_DIR / "projects"
LOGS_DIR = DATA_DIR / "logs"
REGISTRY_FILE = DATA_DIR / "registry.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _load():
    if not REGISTRY_FILE.exists():
        return {}
    try:
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(db):
    # كتابة ذرية: ملف مؤقت ثم os.replace
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(db, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    os.replace(tmp, REGISTRY_FILE)


def create_project(name, owner="anonymous"):
    pid = uuid.uuid4().hex[:12]
    db = _load()
    db[pid] = {
        "id": pid,
        "name": name,
        "owner": owner,
        "status": "uploaded",
        "size": 0,
        "created": time.time(),
        "entry": None,
        "files": []
    }
    _save(db)
    (PROJECTS_DIR / pid).mkdir(parents=True, exist_ok=True)
    return pid


def get_project(pid):
    return _load().get(pid)


def update_project(pid, **kw):
    db = _load()
    if pid in db:
        db[pid].update(kw)
        _save(db)
    return db.get(pid)


def list_projects():
    return sorted(_load().values(), key=lambda p: p["created"], reverse=True)


def delete_project(pid):
    db = _load()
    if pid in db:
        del db[pid]
        _save(db)
    pdir = PROJECTS_DIR / pid
    if pdir.exists():
        shutil.rmtree(pdir)
    log = LOGS_DIR / f"{pid}.log"
    if log.exists():
        log.unlink()
    return True