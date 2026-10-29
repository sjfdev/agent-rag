"""Process-wide runtime flags shared by the API and the admin console."""

import threading

# Set while a full index rebuild is dropping/recreating the collection so chat
# requests fail fast with a 503 instead of querying a half-built index.
MAINTENANCE = threading.Event()


def begin_maintenance() -> None:
    MAINTENANCE.set()
    try:
        from src.metrics import set_maintenance

        set_maintenance(True)
    except Exception:  # metrics must never break ingestion
        pass


def end_maintenance() -> None:
    MAINTENANCE.clear()
    try:
        from src.metrics import set_maintenance

        set_maintenance(False)
    except Exception:
        pass


def in_maintenance() -> bool:
    return MAINTENANCE.is_set()
