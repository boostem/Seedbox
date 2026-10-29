"""`python -m seedbox2`: Seedbox 2 (its flags are listed in seedbox2/app.py)."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()  # job workers are spawned processes (#39); needed in the frozen app
    from seedbox2.app import main

    sys.exit(main())
