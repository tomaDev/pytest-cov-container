"""The contract between the plugin and the coverage bootstrap in a container.

The plugin injects into each function's build dir:

* :data:`RCFILE` — the ``.coveragerc`` the container's coverage reads;
* :data:`BOOT_DIR` — a pure-Python copy of ``coverage`` plus :data:`BOOT_MODULE`
  and :data:`BOOT_CONFIG` (the function's name);
* :data:`PTH_FILE` — a ``.pth`` file that imports the boot module. The Lambda
  Python runtime calls ``site.addsitedir(LAMBDA_TASK_ROOT)`` before it imports
  the handler, so the ``.pth`` runs at startup. A process that the runtime does
  not start (a web server behind the Lambda Web Adapter) calls
  ``site.addsitedir`` on its task root itself.

The boot module does nothing unless both :data:`RCFILE_ENV` and
:data:`SINK_ENV` are set, so an injected build that reaches production is inert.
When set, it starts coverage, then pushes the process's data file to the sink
(an HTTP endpoint run by the plugin in each pytest process):

1. after every call of the handler named in ``_HANDLER``: ``sam local invoke``
   removes its container right after the invoke, so there is no later chance;
2. on ``SIGUSR1`` to the pid in :data:`PID_FILE`: long-running processes (a web
   server) are flushed by the plugin before the containers are stopped.

Each push is a POST of the SQLite data file with the :data:`ID_HEADER`
(``<hostname>-<pid>``; the hostname is the container id prefix) and
:data:`FUNCTION_HEADER` headers. A process pushes its cumulative data each
time, so the latest push per id supersedes the earlier ones.
"""

RCFILE_ENV = "COVERAGE_PROCESS_START"
SINK_ENV = "COV_CONTAINER_SINK"

BOOT_DIR = ".cov_container"
BOOT_MODULE = "_cov_container_boot"
BOOT_CONFIG = "function.txt"
PTH_FILE = "_cov_container.pth"
RCFILE = ".coveragerc"

DATA_FILE = "/tmp/.coverage.container"  # noqa: S108 — container-side path, never on the host
PID_FILE = "/tmp/.cov_container.pid"  # noqa: S108 — container-side path, never on the host

ID_HEADER = "X-Cov-Id"
FUNCTION_HEADER = "X-Cov-Function"
