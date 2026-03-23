import stat
from pathlib import Path

from pytest_cov_container.models import DriverConfig, InjectionResult

_COVERAGERC_TEMPLATE = """\
[run]
data_file = /tmp/.coverage.container
relative_files = true
parallel = true
sigterm = true
include =
    *.py
omit =
    _cov_wrapper.py
"""

_COV_WRAPPER_TEMPLATE = """\
import os
import signal
import subprocess

import coverage

cov = coverage.Coverage(
    config_file=os.environ.get("COVERAGE_PROCESS_START", "/var/task/.coveragerc")
)
cov.start()


def _flush(*_):
    cov.save()


signal.signal(signal.SIGUSR1, _flush)

proc = subprocess.Popen(
    ["sh", "-c", os.environ.get("CONTAINER_COV_ENTRYPOINT", "{entrypoint}")]
)
proc.wait()
cov.stop()
cov.save()
"""

_RUN_SH_TEMPLATE = """\
#!/bin/bash
exec python /var/task/_cov_wrapper.py
"""


class PythonDriver:
    name: str = "python"

    def inject(self, target_dir: Path, config: DriverConfig) -> InjectionResult:
        files_written: list[Path] = []

        # .coveragerc
        coveragerc = target_dir / ".coveragerc"
        coveragerc.write_text(_COVERAGERC_TEMPLATE)
        files_written.append(coveragerc)

        # _cov_wrapper.py
        wrapper = target_dir / "_cov_wrapper.py"
        wrapper.write_text(_COV_WRAPPER_TEMPLATE.format(entrypoint=config.entrypoint))
        files_written.append(wrapper)

        # run.sh
        run_sh = target_dir / "run.sh"
        run_sh.write_text(_RUN_SH_TEMPLATE)
        run_sh.chmod(run_sh.stat().st_mode | stat.S_IEXEC)
        files_written.append(run_sh)

        return InjectionResult(
            files_written=files_written,
            env_vars={"COVERAGE_PROCESS_START": "/var/task/.coveragerc"},
        )
