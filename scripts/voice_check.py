"""Local video analysis and editing helper."""


from __future__ import annotations
import argparse
import sys
import wave
from pathlib import Path

import numpy as np


def load(p):
    with wave.open(str(p)) as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
        sw, ch = w.getsampwidth(), w.getnchannels()
    if sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        a = (b[:, 2] << 16) | (b[:, 1] << 8) | b[:, 0]
        a = np.where(a & 0x800000, a - (1 << 24), a).astype(np.float64)/(1 << 23)
    else:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float64)/32768
    return sr, (a.reshape(-1, ch).mean(1) if ch > 1 else a)


def bandpass(a, sr, lo, hi):
    F = np.fft.rfft(a)
    f = np.fft.rfftfreq(len(a), 1/sr)
    F[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(F, len(a))


def envelope(x):
    n = len(x)
    F = np.fft.fft(x)
    h = np.zeros(n)
    h[0] = 1
    if n % 2 == 0:
        h[n//2] = 1
        h[1:n//2] = 2
    else:
        h[1:(n + 1)//2] = 2
    return np.abs(np.fft.ifft(F*h))


def align(ref, x, sr):
    a0, a1 = sr*5, min(sr*15, len(ref) - 4800)
    r = ref[a0:a1]
    lag = max(range(-4800, 4801, 8), key=lambda L: float(np.dot(r, x[a0 + L:a1 + L])))
    lag = max(range(lag - 8, lag + 9), key=lambda L: float(np.dot(r, x[a0 + L:a1 + L])))
    return (x[lag:] if lag >= 0 else np.concatenate([np.zeros(-lag), x])), lag


def analyse(raw, x, sr, whistle):
    w = int(0.02*sr)
    hb = bandpass(raw, sr, 3000, 10000)
    en = lambda a, i: (a[i:i + w]**2).mean()
    S = [i for i in range(0, len(raw) - w, w) if en(raw, i) > 1e-6 and en(hb, i)/en(raw, i) > 0.45]
    V = [i for i in range(0, len(raw) - w, w) if en(raw, i) > 3e-5 and en(hb, i)/en(raw, i) < 0.08]
    ff = np.fft.rfftfreq(w, 1/sr)

    def crackle(a):
        h = bandpass(a, sr, 3000, 10000)
        seg = np.concatenate([h[i:i + w] for i in S if i + w <= len(h)])
        env = np.convolve(envelope(seg), np.ones(12)/12, 'same')
        e0 = env.mean() + 1e-15
        P = np.abs(np.fft.rfft(env - e0))**2/(e0**2*len(env))
        f = np.fft.rfftfreq(len(env), 1/sr)


        rough = P[(f > 60) & (f < 600)].sum()
        fl = []
        for i in S:
            Q = np.abs(np.fft.rfft(a[i:i + w]*np.hanning(w)))**2
            Q = Q[(ff > 3000) & (ff < 10000)] + 1e-18
            fl.append(np.exp(np.log(Q).mean())/Q.mean())
        return rough, float(np.mean(fl))

    def lvl(a, idx, lo, hi):
        Q = np.mean([np.abs(np.fft.rfft(a[i:i + w]*np.hanning(w)))**2 for i in idx if i + w <= len(a)], 0)
        return 10*np.log10(Q[(ff >= lo) & (ff < hi)].mean() + 1e-18)

    def profile(a):
        ref = lvl(a, V, 800, 1200)
        return {'warmth': lvl(a, V, 120, 300) - ref, 'boxy': lvl(a, V, 400, 600) - ref,
                'whistle': lvl(a, S, *whistle) - ref, 'air': lvl(a, S, 7500, 9000) - ref}

    def floor(a):
        m = len(a)//w
        lv = np.array([20*np.log10(np.sqrt((a[i*w:(i + 1)*w]**2).mean()) + 1e-9) for i in range(m)])
        sp, fl = lv[lv > -38], lv[(lv < -45) & (lv > -90)]
        return sp.mean() - np.median(fl) if len(sp) and len(fl) else float('nan')

    rc, xc = crackle(raw), crackle(x)
    rp, xp = profile(raw), profile(x)
    return {'s_frames': len(S), 'vowel_frames': len(V),
            'roughness': xc[0]/rc[0], 'flatness': xc[1]/rc[1],
            **{k: xp[k] - rp[k] for k in rp}, 'floor_raw': floor(raw), 'floor': floor(x)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('a', help='render directory, or the raw wav')
    ap.add_argument('b', nargs='?', help='processed wav (when a is the raw wav)')
    ap.add_argument('--whistle', nargs=2, type=float, default=[3880, 4040], metavar=('LO', 'HI'))
    args = ap.parse_args()
    if args.b:
        rawp, xp = Path(args.a), Path(args.b)
    else:
        rawp, xp = Path(args.a)/'dialogue.wav', Path(args.a)/'dialogue_processed.wav'
    sr, raw = load(rawp)
    _, x = load(xp)
    x, lag = align(raw, x, sr)
    r = analyse(raw, x, sr, args.whistle)
    print(f'aligned by {lag/sr*1000:.1f} ms; {r["s_frames"]} "s" frames, {r["vowel_frames"]} vowel frames')
    flags = []
    print(f'  roughness {r["roughness"]:.2f}  flatness {r["flatness"]:.2f}  (raw = 1.00)')
    if r['roughness'] > 1.07:
        flags.append('"s" GRIT/CRACKLE: roughness above 1.07 â€” suspect a fast or multiband de-esser')
    if r['flatness'] < 0.92:
        flags.append('"s" PEAKY/WATERY: flatness below 0.92 â€” suspect the denoiser (musical noise)')
    print(f'  warmth {r["warmth"]:+.1f} dB  boxy {r["boxy"]:+.1f} dB  whistle {r["whistle"]:+.1f} dB  air {r["air"]:+.1f} dB')
    if r['warmth'] < -1.0:
        flags.append('THINNER: warmth down more than 1 dB â€” check cuts below 400 Hz and denoising')
    if r['air'] < -3.0:
        flags.append('DULL: air in "s" down more than 3 dB â€” risk of lisping')
    print(f'  speech-to-floor {r["floor"]:.1f} dB (raw {r["floor_raw"]:.1f})')
    for f in flags:
        print('  !!', f)
    if not flags:
        print('  no perceptual flags raised')
    return 1 if flags else 0


if __name__ == '__main__':
    sys.exit(main())
