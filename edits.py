"""Run without installing the package: python edits.py --help."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from edits.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
