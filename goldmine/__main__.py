"""`python -m goldmine` == the `goldmine` console script (v2 packaging)."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
