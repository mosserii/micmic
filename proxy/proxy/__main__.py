import os
import sys

# Everything the proxy writes is private to it: SQLite in WAL mode creates proxy.db-wal
# and proxy.db-shm with the process umask, and those hold the same usage rows the
# chmod-600 database does.
os.umask(0o077)

from .app import main  # noqa: E402

sys.exit(main())
