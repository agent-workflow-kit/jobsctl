"""`jobsctl run --loop` for the re-exec tests (run as ``python -I loop_worker.py <jobsctl args>``).

The installed version is read from the file named by ``LOOP_VERSION_FILE`` so a test can "upgrade"
it; every start of this image (the first one and each re-exec) appends ``<pid> <ppid> <version>``
to the file named by ``LOOP_LOG``. A re-exec runs this very script again with the same arguments.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from jobsctl import upgrade
from jobsctl.cli import main

version_file = Path(os.environ["LOOP_VERSION_FILE"])
upgrade.installed_version = lambda: version_file.read_text(encoding="utf-8").strip()
with open(os.environ["LOOP_LOG"], "a", encoding="utf-8") as log:
    log.write(f"{os.getpid()} {os.getppid()} {upgrade.installed_version()}\n")
sys.exit(main(sys.argv[1:]))
