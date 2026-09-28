"""Local video analysis and editing helper."""


from __future__ import annotations
import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np


def load(path: Path):
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td)/'a.wav'
        subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-y', '-i', str(path),
                        '-vn', '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(wav)], check=True)
        with wave.open(str(wav)) as w:
            sr = w.getframerate()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)/32768
    return sr, a


def frames(sr, a):
    hop = int(0.01*sr)
    n = len(a)//hop
    fr = np.fft.rfftfreq(hop, 1/sr)
    band = (fr > 2500) & (fr < 9000)
    full = np.empty(n)
    hi = np.empty(n)
    for i in range(n):
        seg = a[i*hop:(i+1)*hop]
        full[i] = 20*np.log10(np.sqrt((seg**2).mean()) + 1e-9)
        F = np.fft.rfft(seg)
        F[~band] = 0
        hi[i] = 20*np.log10(np.sqrt((np.fft.irfft(F, hop)**2).mean()) + 1e-9)
    return full, hi


def find(path, t0=None, t1=None, quiet=-42.0, min_gap=0.14, core_db=-44.0, tilt_db=-14.0, shrink=0.02):
    sr, a = load(path)
    full, hi = frames(sr, a)
    tilt = hi - full
    n = len(full)
    lo = int((t0 or 0)*100)
    up = min(n, int((t1 or n/100)*100))
    q = full < quiet
    out = []
    i = lo
    while i < up:
        if not q[i]:
            i += 1
            continue
        j = i
        while j < up and q[j]:
            j += 1
        if (j - i)/100 >= min_gap:
            a0, a1 = max(0, i - 3), min(n, j + 3)
            core = int(((full[a0:a1] > core_db) & (tilt[a0:a1] > tilt_db)).sum())
            g0, g1 = i/100 + shrink, j/100 - shrink
            out.append({'start': round(g0, 2), 'end': round(g1, 2), 'length': round(g1 - g0, 2),
                        'max_db': round(float(full[i:j].max()), 1), 'fricative_frames': core,
                        'verdict': 'refuse' if core >= 2 else 'cut'})
        i = j
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file')
    ap.add_argument('--from', dest='t0', type=float)
    ap.add_argument('--to', dest='t1', type=float)
    ap.add_argument('--quiet', type=float, default=-42.0)
    ap.add_argument('--min-gap', type=float, default=0.14)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    res = find(Path(a.file), a.t0, a.t1, a.quiet, a.min_gap)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    for r in res:
        print(f"{r['start']:6.2f}-{r['end']:6.2f}  {r['length']:.2f}s  max {r['max_db']:6.1f} dBFS  "
              f"fricative frames {r['fricative_frames']:2d}  -> {r['verdict'].upper()}")
    cut = [r for r in res if r['verdict'] == 'cut']
    print(f'{len(cut)} safe cuts, {sum(r["length"] for r in cut):.2f}s removable; '
          f'{len(res) - len(cut)} refused as probable consonants')
    return 0


if __name__ == '__main__':
    sys.exit(main())
