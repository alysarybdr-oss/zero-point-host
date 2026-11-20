import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("ZP_DATA", BASE_DIR / "data"))
PROJECTS_DIR = DATA_DIR / "projects"
LOGS_DIR = DATA_DIR / "logs"
REGISTRY_FILE = DATA_DIR / "registry.json"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))

PORT_RANGE = range(9100, 9200)
MAX_UPLOAD_MB = 200
STARTUP_TIMEOUT_SEC = 10