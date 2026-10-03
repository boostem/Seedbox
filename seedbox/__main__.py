"""`python -m seedbox` and the frozen Seedbox.app entry point (see seedbox.app for the flags)."""

import sys

if __name__ == "__main__":
    from seedbox.updater import VERIFY_FLAG

    if len(sys.argv) > 2 and sys.argv[1] == VERIFY_FLAG:  # updater self-check child (frozen builds)
        from seedbox.updater import verify_main

        verify_main(sys.argv[2])

    import multiprocessing

    multiprocessing.freeze_support()

    from seedbox.app import main

    sys.exit(main())
