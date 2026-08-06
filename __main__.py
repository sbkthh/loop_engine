"""Entry point: python3 ~/.qoder/loop_engine/__main__.py next"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cli import main

if __name__ == "__main__":
    main()
