"""Scripted agent: implements the spec 'correctly'.

Ticks every checkbox of $CHANTIER_SPEC, writes a module inside the spec's
declared scope, reports usage.
"""

import os
import re
from pathlib import Path

spec = Path(os.environ["CHANTIER_SPEC"])
text = spec.read_text()
spec.write_text(re.sub(r"^(\s*- )\[ \]", r"\1[x]", text, flags=re.MULTILINE))
stem = spec.stem
target = Path("src") / f"{stem}.py"
target.parent.mkdir(exist_ok=True)
target.write_text(f"FEATURE = {stem!r}\n")
print("implemented", stem)
print('CHANTIER_USAGE {"input_tokens": 12000, "output_tokens": 3000}')
