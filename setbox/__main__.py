"""`python -m setbox`: the set builder app (see seedbox.app for the flags; Setbox runs the same launcher)."""

import sys

if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()

    from seedbox.app import main

    sys.exit(main(app="setbox"))
