"""Check every cut of an edit plan for clipped speech before rendering.

For each segment edge that removes time (a take boundary or a jump cut inside a take) the script
measures the level of the first/last 40 ms against that take's speech level (95th percentile of
10 ms frames). An edge louder than speech - 28 dB means the cut lands on a sound: a clipped word
start, a cut-off tail, or half of a false start. Splits without removed time (punch-ins, where
one segment's `out` equals the next segment's `in` in the same file) are skipped.

Edges at the very start or end of the source file are reported separately as INHERENT: the
recording itself starts or stops mid-sound and no plan change can fix that. Phones start the
audio ~30 ms after the video (digital silence) and then jump to speech level within 2 ms; start
such a segment at the audio onset (source frame 1) with `audio_fade_in` ~0.01 s so the step
does not click.

Usage:
    .venv/Scripts/python.exe scripts/cut_edges.py 01_PROJECTS/<project>/edits/edit_v001.json
Exit code 1 when any fixable edge is flagged.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import studio

SR = 16000
EDGE_S = 0.04
LIMIT_DB = -28
cache = {}
HP = 0


def audio(f):
    if f not in cache:
        af = ['-af', f'highpass=f={HP}:poles=2,highpass=f={HP}:poles=2'] if HP else []
        raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(studio.path(f)), '-map', '0:a:0', *af, '-ac', '1',
                              '-ar', str(SR), '-f', 'f32le', '-'], capture_output=True, check=True).stdout
        a = np.frombuffer(raw, np.float32).astype(np.float64)
        fr = SR // 100
        frames = a[:len(a) // fr * fr].reshape(-1, fr)
        speech = np.percentile(20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-9), 95)
        onset = int(np.argmax(np.abs(a) > 1e-4)) / SR
        cache[f] = (a, speech, onset)
    return cache[f]


def edge_db(f, t0, t1):
    a, speech, _ = audio(f)
    seg = a[int(max(0.0, t0) * SR):int(t1 * SR)]
    return 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9) - speech if len(seg) else -99.0


def main():
    global HP
    plan = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    v = plan.get('voice') or {}
    HP = v.get('highpass_hz', 85) if v and v.get('enabled', True) else 0
    clips = plan['clips']
    fixable = inherent = 0
    for i, c in enumerate(clips):


        if c.get('mute', False):
            continue
        f = c['file']
        dur = len(audio(f)[0]) / SR
        prev = clips[i - 1] if i else None
        nxt = clips[i + 1] if i + 1 < len(clips) else None
        notes = []
        if not (prev and prev['file'] == f and abs(prev['out'] - c['in']) < 0.002):
            e = edge_db(f, c['in'], c['in'] + EDGE_S)
            if e > LIMIT_DB:
                if c['in'] < audio(f)[2] + EDGE_S:
                    fade = c.get('audio_fade_in', 0.005)
                    notes.append(f'START {e:+.0f} dB (INHERENT: recording starts mid-sound, audio_fade_in {fade})')
                    inherent += 1
                else:
                    notes.append(f'START {e:+.0f} dB'); fixable += 1
        if not (nxt and nxt['file'] == f and abs(nxt['in'] - c['out']) < 0.002):
            e = edge_db(f, c['out'] - EDGE_S, c['out'])
            if e > LIMIT_DB:
                if c['out'] > dur - EDGE_S:
                    notes.append(f'END {e:+.0f} dB (INHERENT: file ends mid-sound)'); inherent += 1
                else:
                    notes.append(f'END {e:+.0f} dB'); fixable += 1
        if notes:
            print(f'{i:2d} {Path(f).name[:24]} {c["in"]:.3f}-{c["out"]:.3f}  ' + '; '.join(notes))
    print(f'{fixable} fixable, {inherent} inherent suspicious edges' if fixable or inherent
          else 'all cut edges in silence')
    sys.exit(1 if fixable else 0)


if __name__ == '__main__':
    main()
