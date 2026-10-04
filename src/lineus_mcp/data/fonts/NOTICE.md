# Bundled single-line fonts

These are **single-line (stroke) fonts**: each glyph is one pen path, not a filled
outline. That matters here — Hershey's "bold" and "serif" faces fake weight by retracing
every stem two or three times (`rowmant` draws a capital H with **27 strokes**), which on
a pen plotter reads as a sketchy scribble rather than handwriting. These draw each letter
in roughly the number of strokes a person uses.

Taken from **[oskay/svg-fonts](https://gitlab.com/oskay/svg-fonts)**, the font set for
Hershey Text v3. The EMS faces were created by **Sheldon B. Michaels**, converted to SVG
font format by **Windell H. Oskay** (Evil Mad Scientist), and are derivatives of the
following originals. All are used under the **SIL Open Font License** — see `OFL.txt`.

| bundled as | style | derived from | original designer |
|---|---|---|---|
| `EMSCasualHand` | print | Covered By Your Grace | Kimberly Geswein |
| `EMSTech` | print | Architects Daughter | Kimberly Geswein |
| `EMSPancakes` | print | Short Stack | James Grieshaber |
| `EMSDelight` | print | Delius | Natalia Raices |
| `EMSReadability` | print, neutral | Source Sans Pro Light | Paul D. Hunt, Adobe |
| `EMSCapitol` | cursive | Sacramento | Brian J. Bonislawsky |
| `EMSBrush` | cursive | Alex Brush | Rob Leuschke |
| `EMSAllure` | cursive | Allura | Rob Leuschke |
| `EMSSwiss` | cursive | Italianno | Rob Leuschke |

Measured on this machine: every one of these draws a letter in
0.8–1.2x the strokes a human uses, against 3.0x for Hershey `timesr` and 7.2x for
`rowmant`. The cursive faces have letter exit/entry gaps of 0.05–0.10 em, small enough to
weld into one continuous stroke per word.
