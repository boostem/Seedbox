"""`python -m seedbox2`: Seedbox 2 (its flags are listed in seedbox2/app.py)."""

import multiprocessing
import sys

if __name__ == "__main__":
    from seedbox2.ytdlp import VERIFY_FLAG

    if len(sys.argv) > 3 and sys.argv[1] == VERIFY_FLAG:  # the yt-dlp updater's self-check child (frozen builds)
        from seedbox2.ytdlp import verify_main

        verify_main(sys.argv[2], sys.argv[3])

    multiprocessing.freeze_support()  # job workers are spawned processes (#39); needed in the frozen app
    from seedbox2.app import main

    sys.exit(main())
