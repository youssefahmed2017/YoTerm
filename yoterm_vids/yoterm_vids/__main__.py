"""Allow `python -m yoterm_vids <file>`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
