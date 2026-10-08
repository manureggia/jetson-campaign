"""Assemble the report's chart pages in a PDF, preserving their layout."""
import json
from pathlib import Path
import sys
from reportlab.pdfgen import canvas


def export(directory):
    directory = Path(directory)
    destination = directory / 'confronto_core0_core3.pdf'
    pages = json.loads((directory / 'pages.json').read_text())
    size = (1008, 604.8)
    pdf = canvas.Canvas(str(destination), pagesize=size)
    pdf.setTitle('Jetson Orin - confronto CPU 0 e CPU 3')
    pdf.setAuthor('Campagna Jetson')
    pdf.setSubject('Latenze, PMU, cache e interrupt; dati validi e appendice parziale')
    for page in pages:
        pdf.bookmarkPage(page['name'])
        pdf.addOutlineEntry(page['title'], page['name'])
        pdf.drawImage(str(directory / (page['name'] + '.png')), 0, 0, width=size[0], height=size[1])
        pdf.showPage()
    pdf.save()
    print(destination)


if __name__ == '__main__':
    export(sys.argv[1])
