"""Thin launcher: python scripts/win_validate.py --out C:/jarvis-test/results env"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.eval.winvalidate import main
sys.exit(main())
