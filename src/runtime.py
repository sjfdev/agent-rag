"""Process-wide runtime flags shared by the API and the admin console."""

import threading

# Set while a full index rebuild is dropping/recreating the collection so chat
# requests fail fast with a 503 instead of querying a half-built index.
MAINTENANCE = threading.Event()


def begin_maintenance() -> None:
    MAINTENANCE.set()


def end_maintenance() -> None:
    MAINTENANCE.clear()


def in_maintenance() -> bool:
    return MAINTENANCE.is_set()
