"""Map per-take word timings onto the final timeline of an edit plan.

Captions must follow the *final* timeline. Re-transcribing a rendered export
works but costs a full render cycle. Since the renderer never changes speed,
every source word lands at a deterministic timeline position:

    timeline = segment_start + (source_time - segment_in)

where segment starts replicate studio.py exactly: lengths are rounded to whole
frames, and each transition pulls the next segment back by its duration.

    python scripts/map_transcript.py PLAN.json OUT_transcript.json [--corrections FIX.json]

Per-take transcripts are found in 01_PROJECTS/_transcripts by the source file's
stem. Words whose midpoint falls in a removed gap are reported: a real word in
that list means a cut clipped speech.

Corrections file: [{"from": "Antropic", "to": "Anthropic"},
                   {"from": "Antropiki", "to": ["Anthropic", "i"]},
                   {"join": ["cyber", "security."], "to": "cybersecurity."},
                   {"retime": "o", "at": 39.23, "start": 46.90, "end": 47.00},
                   {"from": "z", "to": "Z", "at": 90.87}]

"from"/"to" keep the original punctuation, so write "to" without it ("Qwen4",
not "Qwen4.", or the caption reads "Qwen4.."). "at" (timeline seconds) limits a
replacement to the one word starting there.

"retime" moves one source word (matched by text and its Whisper start, +-0.05 s)
to where it is really spoken, before mapping. Needed on one-take recordings with
long pauses: Whisper hangs a short word at the start of a pause onto the pause
("o" 39.23-40.17 when it was said at 46.9 after a 7 s silence), and the word
then lands in a removed gap. Find the true time with voice-activity edges.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPTS = ROOT/'01_PROJECTS'/'_transcripts'


def transcript_for(source: Path):
    hits = sorted(TRANSCRIPTS.glob(source.stem + '_*/transcript.json'))
    if not hits:

        hits = sorted(TRANSCRIPTS.glob(re.sub(r'[^\w-]', '_', source.stem) + '_*/transcript.json'))
    if not hits:

        hits = sorted(TRANSCRIPTS.glob(re.sub(r'[^\w-]', '_', source.stem)[:60] + '*/transcript.json'))
    if not hits:
        raise SystemExit(f'No transcript for {source.name}; run studio.py transcribe on it first.')
    return json.loads(hits[-1].read_text(encoding='utf-8'))


def retime(words, fixes):
    out = []
    for w in words:
        for fx in fixes:
            if 'retime' in fx and w['word'].strip() == fx['retime'] and abs(w['start'] - fx['at']) < 0.05:
                w = {**w, 'start': fx['start'], 'end': fx['end'], 'retimed_from': round(w['start'], 3)}
        out.append(w)
    return out


def correct(words, fixes):
    out = list(words)
    for fx in fixes:
        if 'retime' in fx:
            continue
        if 'join' in fx:
            seq = fx['join']
            i = 0
            while i <= len(out) - len(seq):
                if [w['word'].strip() for w in out[i:i + len(seq)]] == seq:
                    merged = {'word': ' ' + fx['to'], 'start': out[i]['start'], 'end': out[i + len(seq) - 1]['end'],
                              'probability': min(w.get('probability', 1) for w in out[i:i + len(seq)])}
                    out[i:i + len(seq)] = [merged]
                i += 1
            continue
        new = []
        for w in out:

            if 'at' in fx and abs(w['start'] - fx['at']) > 0.15:
                new.append(w)
                continue
            if w['word'].strip().rstrip('.,?!:;') == fx['from'].rstrip('.,?!:;'):
                punct = w['word'].strip()[len(w['word'].strip().rstrip('.,?!:;')):]
                to = fx['to'] if isinstance(fx['to'], list) else [fx['to']]
                span = (w['end'] - w['start']) / len(to)
                for j, t in enumerate(to):
                    new.append({'word': ' ' + t + (punct if j == len(to) - 1 else ''),
                                'start': w['start'] + j * span, 'end': w['start'] + (j + 1) * span,
                                'probability': w.get('probability', 1), 'corrected_from': w['word'].strip()})
            else:
                new.append(w)
        out = new
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plan')
    ap.add_argument('out')
    ap.add_argument('--corrections')
    a = ap.parse_args()

    plan = json.loads(Path(a.plan).read_text(encoding='utf-8'))
    fps = float(plan.get('fps', 30))
    fixes = json.loads(Path(a.corrections).read_text(encoding='utf-8')) if a.corrections else []

    cache = {}
    segments = []
    elapsed = 0.0
    for i, c in enumerate(plan['clips']):
        src = (ROOT/c['file']).resolve()
        length = round((c['out'] - c['in']) * fps) / fps
        trans = round(float(c.get('transition', 0)) * fps) / fps
        start = 0.0 if i == 0 else elapsed - trans
        elapsed = length if i == 0 else elapsed + length - trans
        segments.append({'src': src, 'in': c['in'], 'length': length, 'start': start})
        if c.get('mute'):
            continue
        if src not in cache:
            cache[src] = retime([w for s in transcript_for(src)['segments'] for w in s['words']], fixes)

    mapped, dropped = [], []
    for src, words in cache.items():
        for w in words:


            dur = max(w['end'] - w['start'], 1e-3)
            best, seg = 0.0, None
            for s in segments:
                if s['src'] != src:
                    continue
                ov = min(w['end'], s['in'] + s['length']) - max(w['start'], s['in'])
                if ov > best:
                    best, seg = ov, s
            if seg is None or best < 0.25 * dur:
                dropped.append((src.name, round(w['start'], 2), w['word'].strip(), f'{best / dur:.0%} kept'))
                continue
            lo, hi = seg['in'], seg['in'] + seg['length']
            t0 = seg['start'] + (max(w['start'], lo) - lo)
            t1 = seg['start'] + (min(w['end'], hi) - lo)
            mapped.append({**w, 'start': round(t0, 3), 'end': round(max(t1, t0 + 0.04), 3)})
    mapped.sort(key=lambda w: w['start'])
    mapped = correct(mapped, fixes)

    Path(a.out).write_text(json.dumps({'source': str(Path(a.plan)), 'language': 'pl', 'mapped_from_takes': True,
                                       'duration': round(elapsed, 3),
                                       'segments': [{'start': mapped[0]['start'], 'end': mapped[-1]['end'],
                                                     'text': ''.join(w['word'] for w in mapped), 'words': mapped}],
                                       'human_corrections': fixes}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(mapped)} words mapped, timeline {elapsed:.3f}s -> {a.out}')
    if dropped:
        print('WORDS IN REMOVED GAPS (a real word here means a cut clipped speech):')
        for d in dropped:
            print('   ', d)
    return 1 if dropped else 0


if __name__ == '__main__':
    sys.exit(main())
