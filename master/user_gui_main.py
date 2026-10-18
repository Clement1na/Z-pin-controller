"""Hardware-only user GUI entry point; the original GUI remains the debug entry."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from master.gui_main import main


if __name__ == '__main__':
    sys.exit(main(user_mode=True))
