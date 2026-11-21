"""Scripted agent: the classic local-model failure, the same tool call forever."""

import time

print('CHANTIER_USAGE {"input_tokens": 5000, "output_tokens": 500}', flush=True)
for i in range(1000):
    print(f"applying edit to src/app.py attempt {i}", flush=True)
    time.sleep(0.01)
