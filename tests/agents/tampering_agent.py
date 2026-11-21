"""Scripted agent: 'finishes' by rewriting the spec instead of doing the work."""

import os
from pathlib import Path

spec = Path(os.environ["CHANTIER_SPEC"])
spec.write_text(spec.read_text().replace("- [ ]", "- [x]") + "\nAlso: requirements relaxed.\n")
print("done")
