"""The save protocol between the host and a coverage process in a container.

The process that owns the ``coverage.Coverage`` object (the injected wrapper,
or the application itself with ``wrapper = false``) follows three rules:

1. After ``cov.start()``, write its pid to :data:`PID_FILE`.
2. On ``SIGUSR1``, call ``cov.save()``, then create :data:`DONE_FILE`.
3. Write data files under :data:`DATA_DIR` whose names start with
   :data:`DATA_PREFIX` (the injected ``.coveragerc`` does this).

The host deletes :data:`DONE_FILE`, sends ``SIGUSR1`` to the pid in
:data:`PID_FILE`, waits for :data:`DONE_FILE`, then copies the data files out.
The sentinel matters because ``cov.save()`` writes SQLite incrementally: a copy
taken while the save is running can be truncated.

Signalling by pid file, not by scanning ``/proc/*/cmdline``, is deliberate: a
scanning ``sh -c`` carries the search token in its own command line, matches
itself, and ``SIGUSR1``'s default action terminates it before it reports.
"""

DATA_DIR = "/tmp"  # noqa: S108 — container-side path, never on the host
DATA_PREFIX = ".coverage.container"
PID_FILE = f"{DATA_DIR}/.cov_container.pid"
DONE_FILE = f"{DATA_DIR}/.cov_container.done"
