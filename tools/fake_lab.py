"""Standalone fake IOS lab (the same one the dashboard's Demo button starts). Run:  python tools/fake_lab.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from netscope.fakelab import main  # noqa: E402

if __name__ == "__main__":
    main()
