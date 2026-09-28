"""Local video analysis and editing helper."""


import subprocess
import sys
from pathlib import Path

import numpy as np

R = 48000
FRAME = 2400


def load(p):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(p), '-ac', '1', '-ar', str(R),
                          '-f', 'f32le', '-'], capture_output=True).stdout
    return np.frombuffer(raw, np.float32).astype(np.float64)


def frame_db(x):
    fr = x[:len(x) // FRAME * FRAME].reshape(-1, FRAME)
    return 20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-12)


def energy_db(db):
    return 20 * np.log10(np.sqrt((10 ** (db / 10)).mean()))


def main(export_dir):
    d = Path(export_dir)
    mix_p, voice_p = d / 'work/mix.wav', d / 'dialogue_processed.wav'
    for p in (mix_p, voice_p):
        if not p.exists():
            raise SystemExit(f'missing {p} - run this on an export whose work/ directory is still there')
    mix, voice = load(mix_p), load(voice_p)
    n = min(len(mix), len(voice))
    mix, voice = mix[:n], voice[:n]
    music = mix - voice
    if np.abs(music).max() < 1e-6:
        raise SystemExit('mix.wav equals dialogue_processed.wav - this render has no music bed')
    dv, dm = frame_db(voice), frame_db(music)
    speech = dv > np.percentile(dv, 90) - 15
    v_sp = energy_db(dv[speech])
    m_sp = energy_db(dm[speech])
    m_all = energy_db(dm)
    print(f'{d.name}: {n / R:.2f} s, {speech.mean() * 100:.0f} % of frames are speech')
    print(f'  voice (speech frames) {v_sp:6.1f} dB')
    print(f'  music under speech    {m_sp:6.1f} dB  ->  {v_sp - m_sp:5.1f} dB under the voice   '
          f'(target 30-31)')
    print(f'  music overall         {m_all:6.1f} dB  ->  {v_sp - m_all:5.1f} dB under the voice')
    print(f'  music peak frame      {dm.max():6.1f} dB')
    return v_sp - m_sp


if __name__ == '__main__':
    main(sys.argv[1])
