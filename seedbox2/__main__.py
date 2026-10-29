"""`python -m seedbox2`: Seedbox 2, with the same flags as `python -m seedbox` (see seedbox.app) plus --version."""

import sys

if __name__ == "__main__":
    from seedbox2.app import main

    sys.exit(main())
