import os

import storage
import settings as cfg

storage.initialize()
os.execvp("gunicorn", [
    "gunicorn", "--no-control-socket", "--bind=0.0.0.0:8080", "--worker-class=gthread",
    "--workers=2", "--threads=4", "--timeout=" + str(max(90, cfg.AI_TIMEOUT_SECONDS + 45)), "app:application",
])
