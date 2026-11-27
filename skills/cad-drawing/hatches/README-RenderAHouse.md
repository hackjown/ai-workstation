# Architectural drawing symbols

Original generated assets, CC0 1.0 Universal. Free personal and commercial
use, modification and redistribution; no attribution required.
https://creativecommons.org/publicdomain/zero/1.0/

## Contents

- 10 north-arrow styles.
- 16 graphic scales: five metric ratios and three imperial ratios, each in
  alternating-block and line/tick styles.
- 6 annotation blocks: section, four-way elevation, detail, level/datum,
  grid bubble and a clean scalloped revision cloud.
- 4 landscape title-block templates: A1, A3, ARCH D and Tabloid.
- 15 metric hatch patterns, each with a PAT file and a real HATCH DXF used
  to render its swatch. `pat/architectural-metric.pat` combines all 15.

`manifest.json` records every file/byte count, paper size, scale and block
name. `file-sizes.csv` lists per-item file sizes. Group contact sheets are
in `sheets/`. Previews are WebP; PNG masters have a 1000px longest edge.

## CAD and printing

DXFs are AutoCAD R2013 / AC1027. `$INSUNITS=4` and block units are
millimetres. All annotation geometry uses `A-ANNO-SYMB`. Symbols are
named blocks inserted at the origin with unit scale. Insert them into
paper space at 1:1 in millimetres, or convert appropriately for your drawing.
To insert a paper annotation into model space for a 1:100 viewport, for
example, enlarge it by 100 in a millimetre-based model.

Print the PDF/SVG at **actual size / 100%**, not fit-to-page. SVG width and
height are expressed in mm and PDF MediaBoxes match those dimensions.
The scale-bar canvas includes margins: measure the bar itself, not the
full SVG/PDF page width. PNG/WebP files are display assets, not print-scale
references. A print or placed graphic resized to 95% is no longer calibrated.

| Scale | Real length | Bar on paper |
| --- | ---: | ---: |
| 1:50 | 5 m | 100 mm |
| 1:100 | 10 m | 100 mm |
| 1:200 | 20 m | 100 mm |
| 1:500 | 50 m | 100 mm |
| 1:1000 | 100 m | 100 mm |
| 1/4 inch = 1 foot | 16 ft | 101.6 mm / 4 in |
| 1/8 inch = 1 foot | 32 ft | 101.6 mm / 4 in |
| 1 inch = 20 feet | 80 ft | 101.6 mm / 4 in |

Title blocks are drawn in model space in paper-mm coordinates for insertion
into your preferred sheet/layout workflow. Page sizes are 841×594,
420×297, 914.4×609.6 and 431.8×279.4 mm respectively. Borders use a 20 mm
left binding margin and 10 mm remaining margins. The six ATTDEF fields are
PROJECT, DRAWING_TITLE, SCALE, DATE, SHEET_NO and DRAWN_BY. Use EATTEDIT or
your CAD application's attribute editor. SVG fields remain live editable
text with matching `data-attribute` tags. PDFs are plain, not fillable.
No DOCX is needed because the SVGs and CAD attributes are directly editable.
DejaVu Sans is the source font; install it or substitute your office font.

## Hatch installation

Put standalone PAT files in an AutoCAD support search folder. Each filename
matches its pattern name. Alternatively append the definitions from the
combined file to a **copy** of acadiso.pat; do not replace the original file.
All dimensions are millimetres: begin at hatch scale 1 in an mm drawing.
For metre-based drawings use scale 0.001; choose other scales intentionally.
The sample DXFs embed full pattern definitions. Their PNGs are rendered
from the reopened DXFs using ezdxf's drawing frontend, not substitute artwork.
Terrazzo uses enlarged short dashes and polygonal chips to read at normal scale.

Running bond and stretcher belong to the same half-lap bond family; these
two alternatives use different nominal module dimensions (225×75 and
215×65 mm). Hatches are original diagrammatic line patterns, not scanned
textures, product specifications or certification of material construction.
Insulation loops and timber rings use short straight-segment approximations
because PAT syntax encodes straight line families, not native curves.

North arrows, markers and title fields are generic office conventions, not
a claim of compliance with every national drawing standard or ISO 7200.
Rotate north arrows to your project convention; confirm datum and reference
labels yourself. Redraw revision clouds for irregular extents instead of
stretching them non-uniformly. These assets are supplied as-is.

## Rebuild and validation

Uses the existing Python environment with ezdxf, NumPy, Pillow and Matplotlib:

```
/home/ubuntu/cadblocks/.venv/bin/python /home/ubuntu/symbols/generate.py
/home/ubuntu/cadblocks/.venv/bin/python /home/ubuntu/symbols/validate.py
```

The generator uses `/home/ubuntu/symbols` as its output root. No repository,
original model files or existing asset-library outputs are modified.
