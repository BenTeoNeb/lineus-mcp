# Bundled doodles

`quickdraw.json` holds 184 doodles in 31 categories, taken from the
**[Quick, Draw! Dataset](https://github.com/googlecreativelab/quickdraw-dataset)** by
**Google Inc.**, used under the
**[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/)**
licence.

These are real drawings made by players of the game *Quick, Draw!* They're stored as
**recorded pen strokes**, in the order the person drew them, not as outlines or fills. That's
why they suit a pen plotter: every line is a single stroke, and its width comes only from the
pen. Drawn with `order: "asis"`, the robot replays them stroke by stroke as they were drawn.

## What was changed

- **Selection.** Candidates were filtered (recognised by the game, not scribbled, not too
  crude) and ranked automatically, then every bundled doodle was **chosen by eye** from the
  top 16 of its category. Rejected: scribbles, drawings with the word written in them
  ("duck", "quack", "star"), and drawings that don't read as their subject.
- **Format.** The drawings are unchanged. Each is a list of strokes `[[x...], [y...]]` on the
  dataset's 0–255 grid, y pointing down. The server may smooth them at draw time; the stored
  data isn't modified.

`categories.txt` is the dataset's list of all 345 categories, copied from the same repository
under the same licence. The server uses it to validate names before fetching more doodles
from the web.
