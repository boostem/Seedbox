"""`python -m seedbox2`: Seedbox 2 (its flags are listed in seedbox2/app.py)."""

import sys

if __name__ == "__main__":
    from seedbox2.app import main

    sys.exit(main())
