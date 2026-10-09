"""Regenerate the graphical report with its optional vector PDF."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_campaign import build


def export(directory):
    directory = Path(directory)
    build(directory.resolve().parents[1], pdf=True)
    return directory / 'confronto_core0_core3.pdf'


if __name__ == '__main__':
    export(sys.argv[1])
