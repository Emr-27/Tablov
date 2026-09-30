"""Bootstrap the existing MSST installation without writing into it.

MSST's bundled Torch needs its installation directory as the initial Windows
working directory. After Torch loads, move to the per-job directory so MSST's
relative logs and temporary files stay with this application.
"""
import os
from pathlib import Path
import runpy
import sys


def main():
    job = Path(sys.argv[1]).resolve()
    cli = Path(sys.argv[2]).resolve()
    import torch  # noqa: F401 - load native DLLs before changing cwd
    os.chdir(job)
    sys.argv = [str(cli), *sys.argv[3:]]
    runpy.run_path(str(cli), run_name="__main__")


if __name__ == "__main__":
    main()
