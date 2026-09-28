"""Local video analysis and editing helper."""


from __future__ import annotations
import json
import re
import sys
import unicodedata
from pathlib import Path

FONT = 'Arial Black'
BOLD, SPACING, UPPER = 0, 0, False
SIZE, SIZE_KEY, SIZE_LIST = 66, 74, 82
ACTIVE_W, IDLE_W = r'\c&H00FFFFFF&', r'\c&H00C6C6C6&'
ACTIVE_K, IDLE_K = r'\c&H0000C4FF&', r'\c&H00009AC8&'
AMBER, WHITE = r'{\c&H0000C4FF&}', r'{\c&H00FFFFFF&}'
MAX_CHARS, MAX_DUR, HOLD = 30, 2.0, 0.30
USABLE_PX, ADVANCE, SPACE = 900, 0.64, 0.30
ANCHOR = {'Cap': 1920 - 430, 'List': 1920 - 640}
ENTRANCES = ('pop', 'rise', 'fade', 'pop', 'drop', 'fade')


def fold(word):


    w = word.lower().replace('Ĺ‚', 'l')
    w = unicodedata.normalize('NFKD', w)
    w = ''.join(c for c in w if not unicodedata.combining(c))


    return re.sub(r"[^a-z0-9']", '', w)


def tc(t):
    n = max(0, round(t * 100))
    sec, cs = divmod(n, 100)
    h, sec = divmod(sec, 3600)
    m, sec = divmod(sec, 60)
    return f'{h}:{m:02d}:{sec:02d}.{cs:02d}'


def group(words, max_words, breaks=()):
    """breaks: timeline times (cuts between speakers). A caption never spans one, and a lone word
    after a break is not merged back into the previous caption (it belongs to the other speaker)."""
    def side(t):
        return sum(1 for b in breaks if t >= b - 0.02)

    out, cur = [], []
    for w in words:
        if cur and side(w['start']) != side(cur[0]['start']):
            out.append(cur)
            cur = []
        cur.append(w)
        txt = ''.join(x['word'] for x in cur).strip()
        span = cur[-1]['end'] - cur[0]['start']
        strong = txt.endswith(('.', '?', '!'))
        weak = txt.endswith((',', ';', ':'))
        full = len(cur) >= max_words or len(txt) >= MAX_CHARS or span >= MAX_DUR
        if strong or (weak and len(cur) >= 3) or full:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    merged = []
    for g in out:
        if merged and len(g) == 1 and len(merged[-1]) + 1 <= max_words\
                and side(g[0]['start']) == side(merged[-1][0]['start']):
            prev = ''.join(x['word'] for x in merged[-1]).strip()
            if len(prev) + len(g[0]['word']) <= MAX_CHARS:
                merged[-1] = merged[-1] + g
                continue
        merged.append(g)
    return merged


def width(tokens, base):
    total = 0.0
    for i, (word, key, _) in enumerate(tokens):
        size = SIZE_KEY if key else base
        total += len(word) * ADVANCE * size + (SPACE * size if i else 0)
    return total


def layout(tokens, base):
    if len(tokens) == 1 or width(tokens, base) <= USABLE_PX:
        return [tokens]
    best, split = None, 1
    for i in range(1, len(tokens)):
        a, b = width(tokens[:i], base), width(tokens[i:], base)
        cost = max(a, b) + abs(a - b) * 0.5
        if best is None or cost < best:
            best, split = cost, i
    return [tokens[:split], tokens[split:]]


def entrance(kind, y, x=540):
    if kind == 'pop':
        return rf'\pos({x},{y})\fad(40,0)\fscx88\fscy88\t(0,120,\fscx100\fscy100)'
    if kind == 'rise':
        return rf'\move({x},{y + 38},{x},{y},0,140)\fad(70,0)'
    if kind == 'drop':
        return rf'\move({x},{y - 30},{x},{y},0,130)\fad(70,0)'
    if kind == 'none':
        return rf'\pos({x},{y})'
    return rf'\pos({x},{y})\fad(110,0)'


def render(tokens, active, base):
    parts = []
    for line in layout(tokens, base):
        chunk = []
        for word, key, idx in line:
            on = idx == active
            colour = (ACTIVE_K if on else IDLE_K) if key else (ACTIVE_W if on else IDLE_W)
            chunk.append('{' + colour + (rf'\fs{SIZE_KEY}' if key else rf'\fs{base}') + '}' + word)
        parts.append(' '.join(chunk))
    return r'\N'.join(parts)


def markup(text):
    """*word* -> accent colour; everything else white."""
    out = []
    for i, piece in enumerate(re.split(r'\*', text)):
        if piece:
            out.append((AMBER if i % 2 else WHITE) + piece)
    return ''.join(out)


def svg_logo(svg_path, width, x, y):
    """Brand mark from an SVG made of straight segments -> ASS vector drawings.

    Dark fills are drawn white (the mark sits on video, where a dark logo
    vanishes on dark stock); coloured fills keep their colour. Returns one ASS
    override+drawing string per path. Supports M L H V Z, absolute and relative.
    """
    svg = Path(svg_path).read_text(encoding='utf-8')
    vb = [float(v) for v in re.search(r'viewBox="([^"]+)"', svg).group(1).split()]
    s = width / vb[2]
    out = []
    for d, fill in re.findall(r'<path[^>]*\sd="([^"]+)"[^>]*fill="([^"]+)"', svg):
        toks = re.findall(r'[MLHVZmlhvz]|-?\d*\.?\d+', d)
        subpaths, pts, cx, cy, cmd, i = [], [], 0.0, 0.0, None, 0
        while i < len(toks):
            if toks[i].isalpha():
                cmd = toks[i]
                i += 1
                if cmd in 'Zz':
                    if pts:
                        subpaths.append(pts)
                    pts = []
                continue
            if cmd in 'MmLl':
                nx, ny = float(toks[i]), float(toks[i + 1])
                i += 2
                if cmd in 'Mm' and pts:
                    subpaths.append(pts)
                    pts = []
                cx, cy = (cx + nx, cy + ny) if cmd in 'ml' else (nx, ny)
                if cmd in 'Mm':
                    cmd = 'l' if cmd == 'm' else 'L'
            elif cmd in 'Hh':
                v = float(toks[i])
                i += 1
                cx = cx + v if cmd == 'h' else v
            elif cmd in 'Vv':
                v = float(toks[i])
                i += 1
                cy = cy + v if cmd == 'v' else v
            else:
                raise ValueError(f'unsupported SVG path command {cmd!r} (straight segments only)')
            pts.append((cx, cy))
        if pts:
            subpaths.append(pts)
        drawing = ' '.join(
            f'm {round(p[0][0] * s)} {round(p[0][1] * s)} l ' + ' '.join(f'{round(a * s)} {round(b * s)}' for a, b in p[1:])
            for p in subpaths)
        rgb = fill.lstrip('#')
        r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
        if max(r, g, b) - min(r, g, b) < 40 and 0.2126 * r + 0.7152 * g + 0.0722 * b < 90:
            r = g = b = 255
        colour = f'&H{b:02X}{g:02X}{r:02X}&'
        out.append(rf'{{\an7\pos({x},{y})\1c{colour}\1a&H26&\bord1.5\3c&H101010&\3a&H70&\shad1.5\4a&H90&\p1}}{drawing}{{\p0}}')
    return out


def resolve(spec, words, duration):
    if isinstance(spec, (int, float)):
        return float(spec)
    if spec == 'end':
        return duration
    target = [fold(p) for p in spec['phrase'].split()]
    folded = [fold(w['word']) for w in words]
    for i in range(len(folded) - len(target) + 1):
        if folded[i:i + len(target)] == target:
            t = words[i]['start'] if spec.get('edge', 'start') == 'start' else words[i + len(target) - 1]['end']
            return t + float(spec.get('offset', 0))
    raise SystemExit(f'phrase not found in transcript: {spec["phrase"]!r}')


def ass_colour(rgb, alpha='00'):
    """'RRGGBB' (config, as in CSS) -> ASS &HAABBGGRR."""
    rgb = rgb.lstrip('#')
    return f'&H{alpha}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&'.upper()


def build_header():
    """Local video analysis and editing helper."""
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Cap,{FONT},{SIZE},&H00FFFFFF,&H0000FFFF,&H00101010,&HA0000000,{BOLD},0,0,0,100,100,{SPACING},0,1,5,2,2,90,90,430,238
Style: List,{FONT},{SIZE_LIST},&H00FFFFFF,&H0000FFFF,&H00101010,&HA0000000,{BOLD},0,0,0,100,100,{SPACING},0,1,6,2,2,90,90,640,238
Style: Screen,{FONT},80,&H00FFFFFF,&H0000FFFF,&H00101010,&HA0000000,{BOLD},0,0,0,100,100,{SPACING},0,1,6,3,5,80,80,0,238
Style: ScreenBox,{FONT},80,&H00FFFFFF,&H0000FFFF,&H38101010,&HFF000000,{BOLD},0,0,0,100,100,{SPACING},0,3,20,0,5,80,80,0,238
Style: Mark,Arial,30,&H64FFFFFF,&H0000FFFF,&H78101010,&HA0000000,1,0,0,0,100,100,2,0,1,2,0,7,62,62,150,238

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""


HEADER = ''


def main(src, cfg_path, dst):
    global SIZE, SIZE_KEY, SIZE_LIST, HEADER, FONT, BOLD, SPACING, UPPER
    global ACTIVE_W, IDLE_W, ACTIVE_K, IDLE_K, AMBER, WHITE
    data = json.loads(Path(src).read_text(encoding='utf-8'))
    cfg = json.loads(Path(cfg_path).read_text(encoding='utf-8'))


    st = cfg.get('style', {})
    FONT = st.get('font', FONT)
    BOLD = 1 if st.get('bold', False) else 0
    SPACING = int(st.get('spacing', 0))
    UPPER = bool(st.get('uppercase', False))
    SIZE_LIST = int(st.get('list_size', SIZE_LIST))
    ACTIVE_W = r'\c' + ass_colour(st.get('active', 'FFFFFF'))
    IDLE_W = r'\c' + ass_colour(st.get('idle', 'C6C6C6'))
    ACTIVE_K = r'\c' + ass_colour(st.get('active_key', 'FFC400'))
    IDLE_K = r'\c' + ass_colour(st.get('idle_key', 'C89A00'))
    AMBER = '{' + r'\c' + ass_colour(st.get('accent', st.get('active_key', 'FFC400'))) + '}'
    WHITE = '{' + r'\c' + ass_colour(st.get('screen_text', 'FFFFFF')) + '}'
    cap = cfg.get('caption', {})
    SIZE = int(cap.get('size', SIZE))
    SIZE_KEY = int(cap.get('key_size', max(SIZE + 8, SIZE_KEY)))
    cap_y = cap.get('y')
    cap_an = int(cap.get('align', 2))
    if cap_y is not None:
        ANCHOR['Cap'] = int(cap_y)
    HEADER = build_header()
    if cap.get('box'):


        HEADER, n = re.subn(r'(Style: Cap,[^\n]*?,)&H00101010,&HA0000000,([^\n]*?,)1,5,2,',
                            r'\1&H48101010,&HFF000000,\g<2>3,14,0,', HEADER)
        if not n:
            raise SystemExit('caption.box: Cap style line not found')
    words = [w for s in data['segments'] for w in s['words']]
    duration = float(cfg['duration'])
    stems = tuple(fold(k) for k in cfg.get('keywords', []))
    if not all(stems):
        raise SystemExit(f'a keyword folds to nothing: {cfg.get("keywords")}')
    list_stems = tuple(fold(k) for k in cfg.get('list_stems', []))
    start_from = resolve(cfg['captions_from'], words, duration) if cfg.get('captions_from') is not None else 0.0

    def is_key(w):
        f = fold(w)
        return bool(f) and any(f.startswith(s) for s in stems)

    events = []
    logo = cfg.get('watermark_logo')
    if logo:

        root = Path(__file__).resolve().parents[1]
        for d in svg_logo(root/logo['svg'], float(logo.get('width', 124)), int(logo.get('x', 62)), int(logo.get('y', 128))):
            events.append(f'Dialogue: 0,{tc(0)},{tc(duration)},Mark,,0,0,0,,{d}')
    elif cfg.get('watermark'):
        events.append(f'Dialogue: 0,{tc(0)},{tc(duration)},Mark,,0,0,0,,{cfg["watermark"]}')


    breaks = sorted(float(b) for b in cfg.get('breaks', []))
    groups = [g for g in group(words, int(cfg.get('max_words', 5)), breaks) if g[0]['start'] >= start_from - 0.01]
    for gi, g in enumerate(groups):
        tokens = [((w['word'].strip().upper() if UPPER else w['word'].strip()), is_key(w['word']), i)
                  for i, w in enumerate(g)]
        listy = bool(list_stems) and any(fold(t[0]).startswith(list_stems) for t in tokens)
        style, base = ('List', SIZE_LIST) if listy else ('Cap', SIZE)
        nxt = groups[gi + 1][0]['start'] if gi + 1 < len(groups) else None
        tail = g[-1]['end'] + HOLD
        if nxt is not None:
            tail = min(tail, nxt)
        cut = next((b for b in breaks if b > g[-1]['start'] + 0.02), None)
        if cut is not None:
            tail = min(tail, cut)
        tail = min(tail, duration)
        y = ANCHOR[style]


        before = cap.get('before')
        if before and g[0]['start'] < float(before['t']) - 0.01:
            y = int(before['y'])
        rotation = tuple(cfg.get('entrances', ENTRANCES))
        kind = 'rise' if listy else rotation[gi % len(rotation)]
        if g[0]['start'] < 0.05:
            kind = 'none'
        for i, w in enumerate(g):
            start = w['start']
            end = g[i + 1]['start'] if i + 1 < len(g) else tail
            if end <= start:
                continue
            tag = entrance(kind, y) if i == 0 else rf'\pos(540,{y})'
            if style == 'Cap' and cap_an != 2:
                tag = rf'\an{cap_an}' + tag
            events.append(f'Dialogue: 0,{tc(start)},{tc(end)},{style},,0,0,0,,' + '{' + tag + '}' + render(tokens, i, base))

    for s in cfg.get('screen', []):
        t0 = resolve(s['start'], words, duration)
        t1 = min(resolve(s['end'], words, duration), duration)
        y, size = int(s.get('y', 1160)), int(s.get('size', 80))
        kind = s.get('entrance', 'pop')
        fo = int(1000 * float(s.get('fade_out', 0.14)))
        if kind == 'pop':
            anim = rf'\pos(540,{y})\an5\fs{size}\fad(90,{fo})\fscx78\fscy78\t(0,170,\fscx100\fscy100)'
        elif kind == 'rise':
            anim = rf'\move(540,{y + 34},540,{y},0,220)\an5\fs{size}\fad(160,{fo})'
        elif kind == 'none':


            anim = rf'\pos(540,{y})\an5\fs{size}\fad(0,{fo})'
        else:
            anim = rf'\pos(540,{y})\an5\fs{size}\fad(160,{fo})'

        style = 'ScreenBox' if s.get('box') else 'Screen'
        events.append(f'Dialogue: 1,{tc(t0)},{tc(t1)},{style},,0,0,0,,' + '{' + anim + '}' + markup(s['text']))

    Path(dst).write_text(HEADER + '\n'.join(events) + '\n', encoding='utf-8')
    caps = sum(1 for e in events if ',Cap,' in e or ',List,' in e)
    print(f'{len(events)} events ({caps} caption, {len(cfg.get("screen", []))} screen) -> {dst}')
    if groups:
        print(f'captions start at {groups[0][0]["start"]:.2f}s with: {"".join(w["word"] for w in groups[0]).strip()!r}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], sys.argv[3])
