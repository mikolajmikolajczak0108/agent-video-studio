"""Local, reviewable social-video tools. All paths in an edit are workspace-relative.
No API uploads. Originals are read-only. Every render gets a fresh directory.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from fractions import Fraction

ROOT = Path(__file__).resolve().parents[1]
VIDEO = {'.mp4', '.mov', '.mkv', '.m4v', '.webm', '.avi', '.mts'}
HDR = {'smpte2084', 'arib-std-b67'}

def stamp():
    return datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]

def path(value):
    p = Path(value)
    return p.resolve() if p.is_absolute() else (ROOT / p).resolve()

def save(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')

def read(p):
    return json.loads(Path(p).read_text(encoding='utf-8-sig'))

STALL_S = 90

def _run_watched(cmd):
    """Local video analysis and editing helper."""


    import threading, time
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    out, err, last = [], [], [time.time()]
    def pump(stream, sink):
        for chunk in iter(lambda: stream.read1(4096) if hasattr(stream, 'read1') else stream.read(4096), b''):
            sink.append(chunk); last[0] = time.time()
    ts = [threading.Thread(target=pump, args=(p.stdout, out), daemon=True),
          threading.Thread(target=pump, args=(p.stderr, err), daemon=True)]
    for t in ts:
        t.start()
    stalled = False
    while p.poll() is None:
        time.sleep(1)
        if time.time() - last[0] > STALL_S:
            p.kill(); stalled = True
            break
    p.wait()
    for t in ts:
        t.join(timeout=5)
    dec = lambda b: b''.join(b).decode('utf-8', errors='replace')
    return subprocess.CompletedProcess(cmd, p.returncode, dec(out), dec(err)), stalled

def run(args, log=None):
    cmd = [str(a) for a in args]
    for attempt in range(3):
        p, stalled = _run_watched(cmd)
        if not stalled:
            break
        print(f'  ffmpeg stalled {STALL_S}s without output, retry {attempt + 1}/2', flush=True)

        out = Path(cmd[-1])
        if cmd[0] == 'ffmpeg' and out.suffix and out.exists():
            out.unlink()
    else:
        raise RuntimeError(f'{cmd[0]} stalled three times: {" ".join(cmd)[:300]}')
    if log:
        Path(log).write_text(p.stdout + '\n' + p.stderr, encoding='utf-8')
    if p.returncode:
        raise RuntimeError(f'{args[0]} returned {p.returncode}:\n{p.stdout[-1000:]}\n{p.stderr[-3500:]}')
    return p

def ff(args, log=None, ft=0):


    return run(['ffmpeg', '-hide_banner', '-nostdin', '-n', '-filter_threads', str(ft),
                '-filter_complex_threads', str(ft), *args], log)

def probe(p):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', p]).stdout)

def video(meta):
    return next(s for s in meta['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic'))

def has_audio(meta):
    return any(s['codec_type'] == 'audio' for s in meta['streams'])

def duration(meta):
    return float(meta['format'].get('duration') or video(meta)['duration'])

def digest(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def number(value, lo, hi, name):
    value = float(value)
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f'{name}: expected {lo}..{hi}, got {value}')
    return value

def safe_name(s):
    return re.sub(r'[^a-zA-Z0-9_-]', '_', s)[:80] or 'film'

def summary(meta):
    v = video(meta)
    rotation = next((s.get('rotation', 0) for s in v.get('side_data_list', []) if 'rotation' in s), 0)
    return {k: v.get(k) for k in ['codec_name', 'width', 'height', 'pix_fmt', 'avg_frame_rate', 'r_frame_rate', 'color_space', 'color_transfer', 'color_primaries', 'color_range']} | {
        'duration': duration(meta), 'rotation': rotation, 'audio': has_audio(meta),
        'hdr_metadata': v.get('color_transfer') in HDR,
        'color_note': 'Samsung Log cannot be reliably identified from these tags alone. Confirm camera mode.',
        'vfr_hint': v.get('avg_frame_rate') != v.get('r_frame_rate')}

def doctor(_):
    result = {'date': datetime.now().isoformat(), 'python': sys.version, 'root': str(ROOT),
              'ffmpeg': shutil.which('ffmpeg'), 'ffprobe': shutil.which('ffprobe'),
              'disk_free_gb': round(shutil.disk_usage(ROOT).free / 1e9, 1),
              'packages': {p: importlib.metadata.version(p) for p in ['faster-whisper', 'ctranslate2', 'scenedetect', 'pillow', 'opencv-python']},
              'whisper_turbo_downloaded': (ROOT/'models/whisper-turbo/model.bin').exists()}
    result['ffmpeg_version'] = run(['ffmpeg', '-version']).stdout.splitlines()[0]
    filters = run(['ffmpeg', '-hide_banner', '-filters']).stdout
    required = ['zscale', 'tonemap', 'loudnorm', 'subtitles', 'xfade', 'sidechaincompress', 'blackdetect', 'freezedetect']
    result['filters'] = {f: bool(re.search(r'\s' + f + r'\s', filters)) for f in required}
    try:
        result['gpu'] = run(['nvidia-smi', '--query-gpu=name,driver_version,memory.total', '--format=csv,noheader']).stdout.strip()
        ff(['-f', 'lavfi', '-i', 'testsrc2=size=1080x1920:rate=30', '-t', '0.2', '-c:v', 'h264_nvenc', '-f', 'null', '-'])
        result['nvenc_test'] = 'passed'
    except (RuntimeError, FileNotFoundError) as e:
        result['nvenc_test'] = str(e)

    api = Path(os.environ.get('PROGRAMDATA', 'C:/ProgramData'))/'Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting'
    result['resolve_api_docs'] = str(api/'README.txt') if api.exists() else None
    result['resolve_status'] = 'Installed API is optional; running app + compatible edition/settings required. See README.md.'
    result['log_lut_ready'] = any('LUT_3D_SIZE' in p.read_text(errors='replace')[:3000] for p in (ROOT/'02_ASSETS/luts').glob('*.cube'))
    result['core_ready'] = all(result['filters'].values())
    save(ROOT/'tools/doctor.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['core_ready'] else 1

def init(args):
    name = safe_name(args.name) + '_' + stamp()
    project = ROOT/'01_PROJECTS'/name
    for folder in ['originals', 'analysis', 'transcripts', 'edits', 'review']:
        (project/folder).mkdir(parents=True, exist_ok=True)
    sources = [path(x) for x in args.files]
    if not sources:
        sources = sorted(p for p in (ROOT/'00_INBOX').rglob('*') if p.suffix.lower() in VIDEO and p.is_file())
    manifest = []
    for i, source in enumerate(sources):
        if not source.is_file():
            raise FileNotFoundError(source)
        before = source.stat()
        expected = digest(source)
        dest = project/'originals'/f'{i+1:03d}_{source.name}'
        shutil.copy2(source, dest)
        after = source.stat()
        if expected != digest(dest) or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise RuntimeError(f'Source changed during ingest: {source}')
        manifest.append({'source': str(source), 'copy': str(dest.relative_to(ROOT)), 'sha256': expected, 'bytes': dest.stat().st_size, 'media': summary(probe(dest))})
    save(project/'manifest.json', manifest)
    shutil.copy2(ROOT/'templates/BRIEF.md', project/'BRIEF.md')
    shutil.copy2(ROOT/'templates/REVIEW.md', project/'review/REVIEW.md')
    print(project)
    if not manifest:
        print('Empty project; place clips in 00_INBOX or specify files.')

def preview_filter(v, mode):
    if mode == 'samsung_log':
        return color_filter({'color': mode}, v) + ','
    if v.get('color_transfer') in HDR:
        return color_filter({'color': 'hdr'}, v) + ','
    return ''

def inspect(args):
    source = path(args.file)
    out = ROOT/'01_PROJECTS'/'_analysis'/(safe_name(source.stem)+'_'+stamp())
    out.mkdir(parents=True)
    meta = probe(source)
    save(out/'ffprobe.json', meta)
    report = summary(meta)

    cmd = ['-v', 'info', '-xerror', '-i', source, '-map', '0:v:0', '-vf', 'vfrdet,blackdetect=d=0.15:pix_th=0.08,freezedetect=n=-50dB:d=1']
    if has_audio(meta):
        cmd += ['-map', '0:a:0', '-af', 'silencedetect=noise=-40dB:d=0.5']
    cmd += ['-f', 'null', '-']
    if args.quick:
        report['full_decode'] = 'SKIPPED (--quick); no full VFR/error validation'
    else:
        try:
            log = ff(cmd, out/'decode.log').stderr
            report['full_decode'] = 'passed'
            report['candidates'] = [l for l in log.splitlines() if any(x in l for x in ['VFR:', 'black_start:', 'freeze_', 'silence_'])]
        except RuntimeError as e:
            report['full_decode'] = 'FAILED'
            report['error'] = str(e)
    from PIL import Image, ImageDraw, ImageFont
    times = [duration(meta) * (i + .5) / 12 for i in range(12)]
    sheet = Image.new('RGB', (960, 4*350), '#171923')
    draw = ImageDraw.Draw(sheet)
    for i, t in enumerate(times):
        frame = out/f'frame_{i+1:02d}.jpg'
        vf = preview_filter(video(meta), args.color) + 'scale=300:310:force_original_aspect_ratio=decrease,setsar=1'
        ff(['-ss', f'{t:.6f}', '-i', source, '-frames:v', '1', '-vf', vf, '-q:v', '2', frame])
        with Image.open(frame) as im:
            x, y = (i%3)*320 + (320-im.width)//2, (i//3)*350
            sheet.paste(im, (x,y))
        draw.text(((i%3)*320+10, (i//3)*350+320), f'{i+1:02d}  {t:.2f}s', fill='white')
    sheet.save(out/'contact_sheet.jpg', quality=94)
    report['contact_sheet_note'] = 'Sampled frames only; not a substitute for playback. Log preview requires --color samsung_log and official LUT.'
    save(out/'report.json', report)
    print(out)
    return 1 if report['full_decode'] == 'FAILED' else 0

def scenes(args):
    from scenedetect import detect, ContentDetector
    result = detect(str(path(args.file)), ContentDetector(threshold=args.threshold))
    out = ROOT/'01_PROJECTS'/'_analysis'/('scenes_'+stamp()+'.json')
    save(out, [{'start': a.get_seconds(), 'end': b.get_seconds()} for a,b in result])
    print(out)

def tc(t, ass=False):
    factor = 100 if ass else 1000
    n = max(0, round(t*factor))
    sec, sub = divmod(n, factor)
    hour, sec = divmod(sec,3600)
    minute, sec = divmod(sec,60)
    return f'{hour}:{minute:02d}:{sec:02d}.{sub:02d}' if ass else f'{hour:02d}:{minute:02d}:{sec:02d},{sub:03d}'

def ass_text(s):
    return s.replace('\\', 'ďĽŹ').replace('{', '(').replace('}', ')').replace('\n', ' ')

def captions_files(data, out):

    groups = []
    for seg in data:
        words = seg.get('words') or [{'start': seg['start'], 'end': seg['end'], 'word': seg['text']}]
        chunk = []
        for w in words:
            if chunk and (len(chunk)>=5 or len(''.join(x['word'] for x in chunk))+len(w['word'])>32 or w['end']-chunk[0]['start']>2.2):
                groups.append(chunk); chunk=[]
            chunk.append(w)
        if chunk:
            groups.append(chunk)
    lines, events = [], []
    for i, g in enumerate(groups):
        a, b = g[0]['start'], g[-1]['end']
        if b <= a:
            continue
        s = ''.join(x['word'] for x in g).strip()
        lines.append(f'{len(lines)+1}\n{tc(a)} --> {tc(b)}\n{s}\n')
        events.append(f'Dialogue: 0,{tc(a,True)},{tc(b,True)},Default,,0,0,0,,{ass_text(s)}')
    (out/'captions.srt').write_text('\n'.join(lines), encoding='utf-8')
    header = '''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Arial,64,&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,100,180,420,1
[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
'''
    (out/'captions.ass').write_text(header+'\n'.join(events)+'\n', encoding='utf-8')

def transcribe(args):
    from faster_whisper import WhisperModel
    source = path(args.file)
    meta = probe(source)
    if not has_audio(meta):
        raise ValueError('No audio stream to transcribe')
    out = ROOT/'01_PROJECTS'/'_transcripts'/(safe_name(source.stem)+'_'+stamp())
    out.mkdir(parents=True)
    wav = out/'audio_16k.wav'
    ff(['-i', source, '-map', '0:a:0', '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', wav], out/'extract.log')
    model_path = path(args.model) if args.model else ROOT/'models/whisper-turbo'
    if not (model_path/'model.bin').is_file():
        raise ValueError('Model missing. Run the download-model command.')
    model = WhisperModel(str(model_path), device=args.device, compute_type='int8' if args.device=='cpu' else 'float16', cpu_threads=12, local_files_only=True)
    segments, info = model.transcribe(str(wav), language=args.language, beam_size=5, word_timestamps=True, vad_filter=True, condition_on_previous_text=False)
    data = [{'start': s.start, 'end': s.end, 'text': s.text, 'words': [{'start': w.start, 'end': w.end, 'word': w.word, 'probability': w.probability} for w in (s.words or [])]} for s in segments]
    save(out/'transcript.json', {'source': str(source), 'language': info.language, 'segments': data, 'review_required': True})
    captions_files(data, out)
    print(out)

def filter_path(p):


    s = str(Path(p).resolve()).replace('\\', '/')
    if any(c in s for c in ["'", '[', ']', ',', ';']):
        raise ValueError('FFmpeg filter asset path contains unsupported punctuation. Use an ASCII workspace path.')
    return "'" + s.replace(':', '\\:') + "'"

def lut_path(clip):
    p = path(clip.get('lut', '02_ASSETS/luts/Samsung_Log_to_Rec709_v1.0.cube'))
    if not p.exists() or 'LUT_3D_SIZE' not in p.read_text(errors='replace')[:3000]:
        raise ValueError('Samsung Log requires an actual official .cube LUT (not a login HTML). Supply your own appropriately licensed LUT.')
    return p

def color_filter(clip, v):
    mode = clip.get('color')
    if mode == 'hdr':
        if v.get('color_transfer') not in HDR or v.get('color_primaries') != 'bt2020' or v.get('color_space') != 'bt2020nc':
            raise ValueError('HDR mode requires verified BT.2020 nonconstant + PQ/HLG metadata; repair/confirm metadata first.')
        if v.get('color_transfer') == 'arib-std-b67':


            return 'zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=mobius:param=0.5:desat=0,zscale=t=bt709:m=bt709:r=limited,format=yuv422p10le'
        return 'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=mobius:desat=2,zscale=t=bt709:m=bt709:r=limited,format=yuv422p10le'
    if mode == 'samsung_log':
        return f'format=gbrpf32le,lut3d=file={filter_path(lut_path(clip))}:interp=tetrahedral,scale=out_color_matrix=bt709:out_range=tv,format=yuv422p10le'
    if mode != 'sdr709':
        raise ValueError('Every clip needs explicit color: sdr709, hdr, samsung_log. Never infer Log from flat appearance.')
    if v.get('color_transfer') in HDR or v.get('color_primaries') not in [None, 'unknown', 'bt709'] or v.get('color_space') not in [None, 'unknown', 'bt709']:
        raise ValueError('SDR709 requested but input metadata indicates another color space. Inspect first.')
    return 'scale=in_color_matrix=bt709:out_color_matrix=bt709:out_range=tv,format=yuv422p10le'


GRADES={
    'neutral': '',
    'clean_warm': "curves=r='0/0 0.25/0.262 0.75/0.784 1/1':g='0/0 0.25/0.252 0.75/0.766 1/1':b='0/0 0.25/0.243 0.75/0.744 1/1',eq=saturation=1.06:contrast=1.03",
    'crisp_cool': "curves=r='0/0 0.25/0.243 0.75/0.752 1/1':g='0/0 0.25/0.251 0.75/0.764 1/1':b='0/0 0.25/0.262 0.75/0.781 1/1',eq=saturation=1.04:contrast=1.05",


    'studio_warm': "curves=r='0/0 0.25/0.262 0.75/0.784 1/1':g='0/0 0.25/0.252 0.75/0.766 1/1':b='0/0 0.25/0.243 0.75/0.744 1/1',eq=saturation=1.06:contrast=1.03,"
                   "selectivecolor=whites='0 0.07 0.02 0':neutrals='0 0.03 0.01 0':reds='0 -0.03 0 0':correction_method=relative,"
                   "curves=all='0/0.012 0.12/0.10 0.5/0.5 0.85/0.87 1/0.965'",
    'filmic_soft': "curves=r='0/0.012 0.25/0.258 0.75/0.771 1/0.988':g='0/0.012 0.25/0.252 0.75/0.762 1/0.988':b='0/0.02 0.25/0.25 0.75/0.752 1/0.984',eq=saturation=0.98:contrast=1.02",
}


TRANSITIONS={'fade','fadefast','fadeslow','fadeblack','fadewhite','fadegrays','dissolve',
    'smoothleft','smoothright','smoothup','smoothdown',
    'slideleft','slideright','slideup','slidedown',
    'wipeleft','wiperight','wipeup','wipedown','wipetl','wipetr','wipebl','wipebr',
    'circleopen','circleclose','circlecrop','radial','zoomin','pixelize','hblur',
    'squeezeh','squeezev','diagtl','diagtr','diagbl','diagbr',
    'hlslice','vuslice','hlwind','vuwind'}


RAMP_FIELDS={'kind','dur','amount','dir'}

def validate_ramp(r, name):
    if not isinstance(r,dict) or set(r)-RAMP_FIELDS:
        raise ValueError(f'{name} must be an object with {sorted(RAMP_FIELDS)}')
    if r.get('kind','zoom') not in ('zoom','whip','whip_v'):
        raise ValueError(f'{name}.kind must be zoom, whip or whip_v')
    number(r.get('dur',0.18),0.06,0.6,f'{name}.dur')
    number(r.get('amount',0.22),0.02,0.6,f'{name}.amount')
    if r.get('dir',1) not in (1,-1):
        raise ValueError(f'{name}.dir must be 1 or -1')

def ramp_filter(c, length, w, h, fps):
    """Animated entry/exit for a segment: a zoom-through or a whip pan.

    Cutting between two takes with the same framing is a jump cut; a crossfade
    softens it but needs silence on both sides and overlaps audio. A ramp is a
    hard cut with motion on each side instead: the outgoing take accelerates
    into a zoom (or pan), the incoming one decelerates out of it. No overlap, so
    no handles and no risk of mixing words. Motion blur is only switched on while
    the ramp is moving.
    """
    fx=number(c.get('focus_x',.5),0,1,'focus_x'); fy=number(c.get('focus_y',.5),0,1,'focus_y')
    z, x, y, blur = zoom_exprs(c, length, w)


    vf = (f",crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})':'(iw-ow)*{fx}':'(ih-oh)*{fy}'"
          f",zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps},setsar=1")
    return vf + blur_filters(blur, min(w,h))

def blur_filters(blur, w):

    vf=''
    for kind, cond in blur:
        if kind == 'tmix':


            vf += (f",tpad=stop=2:stop_mode=clone,tmix=frames=3:weights='1 2 1':enable='{cond}'"
                   f",trim=start_frame=2,setpts=PTS-STARTPTS")
        elif kind == 'whip_v':
            vf += f",gblur=sigma=0.6:sigmaV={26*w/1080:.1f}:enable='{cond}'"
        else:
            vf += f",gblur=sigma={26*w/1080:.1f}:sigmaV=0.6:enable='{cond}'"
    return vf

def zoom_exprs(c, length, w):
    """zoompan-style expressions for a segment's zoom, ramps and drift: (z, x, y, blur).

    x/y are written in zoompan's variables (iw, ih, zoom, it); the GPU path substitutes its own.
    blur lists the motion-blur filters a ramp switches on while it moves (they run on the CPU in both
    paths - a few frames per cut).
    """
    base=number(c.get('zoom',1),1,1.5,'zoom')
    fx=number(c.get('focus_x',.5),0,1,'focus_x'); fy=number(c.get('focus_y',.5),0,1,'focus_y')
    rin, rout = c.get('ramp_in'), c.get('ramp_out')
    terms_z, terms_x, terms_y, blur = [], [], [], []
    for r, head in ((rin, True), (rout, False)):
        if not r:
            continue
        d=number(r.get('dur',0.18),0.06,0.6,'ramp.dur'); a=number(r.get('amount',0.22),0.02,0.6,'ramp.amount')
        sgn=int(r.get('dir',1))

        p = f"if(lt(it,{d}),pow(1-it/{d},2),0)" if head else f"if(gt(it,{length-d:.4f}),pow((it-{length-d:.4f})/{d},2),0)"
        if r.get('kind','zoom')=='zoom':
            terms_z.append(f'{a}*{p}')
            blur.append(('tmix', f'lt(t,{d*0.8:.3f})' if head else f'gt(t,{length-d*0.8:.4f})'))
        else:
            vert = r.get('kind') == 'whip_v'
            terms_z.append(f'{a*0.5}*{p}')
            (terms_y if vert else terms_x).append(f'{sgn}*{p}')
            blur.append(('whip_v' if vert else 'whip', f'lt(t,{d*0.6:.3f})' if head else f'gt(t,{length-d*0.6:.4f})'))
    dr = c.get('drift')
    if dr:


        a=number(dr.get('amount',0.06),0.01,0.3,'drift.amount')
        t0=number(dr.get('t0',0),0,600,'drift.t0'); span=number(dr.get('span',length),0.2,600,'drift.span')
        prog=f"min(1,max(0,(it+{t0})/{span}))"
        if dr.get('ease'):

            prog=f"(3*pow({prog},2)-2*pow({prog},3))"
        kind=dr.get('kind','push')
        if kind=='push':
            terms_z.append(f'{a}*{prog}')
        elif kind=='pull':
            terms_z.append(f'{a}*(1-{prog})')
        elif kind in ('rise','sink'):
            terms_z.append(f'{a}')
            terms_y.append(f"{-1 if kind=='rise' else 1}*(2*{prog}-1)")
        else:
            terms_z.append(f'{a}')
            terms_x.append(f"{1 if kind=='pan_right' else -1}*(2*{prog}-1)")
    fl = c.get('float')
    if fl:

        terms_z.append(f'{2*fl}')
        terms_x.append(f'0.45*sin(6.2832*it/5.3)')
        terms_y.append(f'0.45*sin(6.2832*it/7.1+1.3)')
    for pu in c.get('punch',[]) or []:

        t=float(pu['t']); pa=float(pu.get('amount',0.07)); pd=float(pu.get('dur',0.16))
        u=f"min(1,max(0,(it-{t})/{pd}))"
        terms_z.append(f'{pa}*(1-pow(1-{u},3))')
    z = f"{base}*(1+{'+'.join(terms_z) or '0'})"
    x0 = f"(iw-iw/zoom)*{fx}"
    x = f"max(0,min(iw-iw/zoom,{x0}+(iw-iw/zoom)*0.5*({'+'.join(terms_x)})))" if terms_x else x0
    y0 = f"(ih-ih/zoom)*{fy}"
    y = f"max(0,min(ih-ih/zoom,{y0}+(ih-ih/zoom)*0.5*({'+'.join(terms_y)})))" if terms_y else y0
    return z, x, y, blur

def skin_filter(amount, k=1.0):

    lr=round(min(5.0,(1.0+1.4*amount)*k),3); ls=round(0.25+0.55*amount,3); lt=round(10+9*amount,1)
    cr=round(min(5.0,(1.4+1.8*amount)*k),3); cs=round(0.40+0.55*amount,3); ct=round(14+10*amount,1)
    return (f'smartblur=luma_radius={lr}:luma_strength={ls}:luma_threshold={lt}'
            f':chroma_radius={cr}:chroma_strength={cs}:chroma_threshold={ct}')


GPU_DIR=ROOT/'01_PROJECTS/_analysis/gpu'
_VK=None
import threading


_ASSET_LOCK=threading.Lock()
_GPU_SEM=threading.BoundedSemaphore(2)

def vk_ok():
    global _VK
    if _VK is None:
        try:
            ff(['-init_hw_device','vulkan=vk','-filter_hw_device','vk','-f','lavfi','-i','testsrc2=size=320x180:rate=30',
                '-t','0.1','-vf',"hwupload,libplacebo=w=160:h=90:format=yuv422p10le:extra_opts='skip_target_clearing=yes',"
                "hwdownload,format=yuv422p10le",'-f','null','-'])
            _VK=not os.environ.get('STUDIO_CPU')
        except Exception:
            _VK=False
    return _VK

def gpu_ok(c, w=None):


    return ((w is None or w>=1080) and vk_ok() and c.get('color')=='sdr709' and chunkable(c) and c.get('framing','crop')=='crop'
            and not number(c.get('sharpen',0),0,1.2,'sharpen')
            and (c.get('brightness',0),c.get('contrast',1),c.get('saturation',1))==(0,1,1))

def grade_lut(grade):
    """The grade's per-pixel RGB mapping baked into a 3D LUT by pushing an identity lattice through the
    exact CPU filters (curves, eq), so the GPU applies what the CPU applied."""
    with _ASSET_LOCK:
        return _grade_lut(grade)

def _grade_lut(grade):
    expr=GRADES[grade]
    if not expr:
        return None
    GPU_DIR.mkdir(parents=True,exist_ok=True)
    dest=GPU_DIR/f"grade_{hashlib.sha1(expr.encode()).hexdigest()[:12]}.cube"
    if dest.is_file():
        return dest
    import numpy as np
    n=65
    g=np.linspace(0,65535,n).round().astype('<u2')
    b,gg,r=np.meshgrid(g,g,g,indexing='ij')
    raw=np.stack([r,gg,b],-1).reshape(n*n,n,3).astype('<u2').tobytes()
    out=subprocess.run(['ffmpeg','-v','error','-f','rawvideo','-pix_fmt','rgb48le','-s',f'{n}x{n*n}','-i','-',
                        '-vf',f'format=gbrp16le,{expr},format=rgb48le','-f','rawvideo','-'],
                       input=raw,capture_output=True,check=True).stdout
    o=np.frombuffer(out,'<u2').reshape(n**3,3)/65535.0
    tmp=dest.with_suffix('.tmp')
    tmp.write_text(f'LUT_3D_SIZE {n}\n'+''.join(f'{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n' for v in o),encoding='ascii')
    tmp.replace(dest)
    return dest

def _taps(radius, strength):


    n=int(radius*3.0+0.5)|1
    mid=(n-1)/2
    c=[math.exp(-(i-mid)**2/(2*radius*radius)) for i in range(n)]
    s=sum(c)
    c=[strength*v/s for v in c]
    c[n//2]+=1-strength
    return c

def _skin_pass(hook, src, taps, axis, save=None, threshold=None):
    r=len(taps)//2
    lines=[f'//!HOOK {hook}','//!BIND HOOKED']+([f'//!BIND {src}'] if src!='HOOKED' else [])
    lines+=([f'//!SAVE {save}'] if save else [])+[f'//!DESC skin {hook.lower()} {"h" if axis==0 else "v"}',
            'vec4 hook() {','    vec4 s = vec4(0.0);']
    lines+=[f'    s += {w:.8f} * {src}_texOff(vec2({i-r if axis==0 else 0}, {i-r if axis==1 else 0}));'
            for i,w in enumerate(taps)]
    if threshold is None:
        lines.append('    return s;')
    else:


        t=threshold/255.0
        lines+=['    vec4 o = HOOKED_texOff(vec2(0.0));','    vec4 d = o - s;','    vec4 ad = abs(d);',
                f'    vec4 r = mix(s, o - sign(d) * {t:.8f}, step(vec4({t:.8f}), ad));',
                f'    r = mix(r, o, step(vec4({2*t:.8f}), ad));','    return vec4(r.xyz, o.w);']
    return '\n'.join(lines+['}',''])

def skin_shader(amount, k, scale, chroma):
    """smartblur(skin_filter(amount, k)) as an mpv user shader on the source planes. `scale` is output
    pixels per source pixel (the zoom), `chroma` the source chroma plane size relative to luma (w, h):
    the CPU ran smartblur on 4:2:2 chroma at output size, the GPU runs on the source's own planes."""
    with _ASSET_LOCK:
        return _skin_shader(amount, k, scale, chroma)

def _skin_shader(amount, k, scale, chroma):
    lr=round(min(5.0,(1.0+1.4*amount)*k),3); ls=round(0.25+0.55*amount,3); lt=round(10+9*amount)
    cr=round(min(5.0,(1.4+1.8*amount)*k),3); cs=round(0.40+0.55*amount,3); ct=round(14+10*amount)
    cx,cy=chroma
    key=f'{amount}_{k}_{scale:.4f}_{cx}_{cy}'
    GPU_DIR.mkdir(parents=True,exist_ok=True)
    dest=GPU_DIR/f"skin_{hashlib.sha1(key.encode()).hexdigest()[:12]}.glsl"
    if dest.is_file():
        return dest
    lum=_taps(lr/scale,ls)
    chh=_taps(max(0.1,2*cr/scale*cx),cs); chv=_taps(max(0.1,cr/scale*cy),cs)
    text='\n'.join([_skin_pass('LUMA','HOOKED',lum,0,save='SKINLH'),
                    _skin_pass('LUMA','SKINLH',lum,1,threshold=lt),
                    _skin_pass('CHROMA','HOOKED',chh,0,save='SKINCH'),
                    _skin_pass('CHROMA','SKINCH',chv,1,threshold=ct)])
    tmp=dest.with_suffix('.tmp'); tmp.write_text(text,encoding='ascii'); tmp.replace(dest)
    return dest

def gpu_segment_cmd(c, p, m, start, length, w, h, fps, dest, off, clen):
    """ffmpeg arguments for one slice of a segment rendered on the GPU (see the note above vk_ok)."""
    v=video(m)
    rot=round(float(next((s.get('rotation',0) for s in v.get('side_data_list',[]) if 'rotation' in s),0)))%360
    rotf={270:'transpose_vulkan=dir=clock,',90:'transpose_vulkan=dir=cclock,',180:'flip_vulkan,'}.get(rot,'')
    sw,sh=int(v['width']),int(v['height'])
    if rot in (90,270):
        sw,sh=sh,sw
    fx=number(c.get('focus_x',.5),0,1,'focus_x'); fy=number(c.get('focus_y',.5),0,1,'focus_y')
    z,x,y,blur=zoom_exprs(c,length,w)
    bw=f'min(in_w,in_h*{w}/{h})'; bh=f'min(in_h,in_w*{h}/{w})'
    zt=z.replace('it','t')
    sub=lambda e:(e.replace('zoom','(ZZ)').replace('it','t').replace('iw','(BW)').replace('ih','(BH)')
                  .replace('ZZ',zt).replace('BW',bw).replace('BH',bh))
    cw=f'({bw})/({zt})'; ch=f'({bh})/({zt})'
    cxe=f'(in_w-{bw})*{fx}+{sub(x)}'; cye=f'(in_h-{bh})*{fy}+{sub(y)}'
    opts=[f'w={w}',f'h={h}','format=yuv422p10le',f"crop_w='{cw}'",f"crop_h='{ch}'",f"crop_x='{cxe}'",
          f"crop_y='{cye}'",'upscaler=ewa_lanczos','downscaler=ewa_lanczos','colorspace=bt709',
          'color_primaries=bt709','color_trc=bt709','range=tv',"extra_opts='skip_target_clearing=yes'"]
    skin=number(c.get('skin',0),0,1,'skin')
    if skin:
        base=number(c.get('zoom',1),1,1.5,'zoom')
        scale=base*h/min(sh,sw*h/w)
        pf=v.get('pix_fmt','yuv420p')
        chroma=(0.5,0.5) if '420' in pf or pf in ('nv12','p010le') else ((0.5,1.0) if '422' in pf else (1.0,1.0))
        if rot in (90,270):
            chroma=(chroma[1],chroma[0])
        opts.append(f'custom_shader_path={filter_path(skin_shader(skin,min(w,h)/1080,scale,chroma))}')
    lut=grade_lut(c.get('grade','neutral'))
    if lut:
        opts+= [f'lut={filter_path(lut)}','lut_type=normalized']


    up='format=nv12,' if v.get('pix_fmt','yuv420p') in ('yuv420p','yuvj420p','nv12') else 'format=p010le,'
    vf=(f'fps={fps},setpts=PTS-STARTPTS+{off}/TB,sidedata=mode=delete:type=DISPLAYMATRIX,{up}hwupload,{rotf}'
        +('hflip_vulkan,' if c.get('mirror') else '')+'libplacebo='+':'.join(opts)
        +',hwdownload,format=yuv422p10le'+blur_filters(blur,min(w,h))


        +f',setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop=2,setpts=N/({fps}*TB),setsar=1,'
        'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=limited')
    return (['-init_hw_device','vulkan=vk','-filter_hw_device','vk','-hwaccel','cuda',
             '-noautorotate','-ss',start+off,'-i',p,'-map','0:v:0','-frames:v',round(clen*fps),
             '-vf',vf,'-an',*inter(),
             '-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-color_range','tv',
             '-map_metadata','-1',dest])

VOICE_FIELDS={'denoise_db','highpass_hz','deesser','sibilance_db','sibilance_hz','presence_db','mud_db','eq','compress','deess_split','enabled','segment_eq','peak','post'}
COMPRESS_FIELDS={'threshold_db','ratio','attack_ms','release_ms','makeup_db','knee_db'}

def segment_eq_graph(seg, total, chain):
    """Local video analysis and editing helper."""


    R=48000; n_total=round(total*R); cuts=[]; pos=0
    for s in sorted(seg,key=lambda s:s['from']):
        if set(s)-{'from','to','eq','group'}:
            raise ValueError('voice.segment_eq entries accept only from, to, eq, group')
        a=round(number(s['from'],0,total,'segment_eq.from')*R); b=round(number(s['to'],0,total,'segment_eq.to')*R)
        if a<pos or b<=a:
            raise ValueError('voice.segment_eq ranges must increase and not overlap')
        if a>pos:
            cuts.append((pos,a,[]))
        cuts.append((a,b,s['eq'])); pos=b
    if pos<n_total:
        cuts.append((pos,n_total,[]))
    g=[f'[0:a]aresample=48000,asplit={len(cuts)}'+''.join(f'[i{k}]' for k in range(len(cuts)))]
    for k,(a,b,eq) in enumerate(cuts):
        bells=[]
        for e in eq:
            if set(e)-{'f','q','g'}:
                raise ValueError('voice.segment_eq bells accept only f, q, g')
            bells.append(f"equalizer=f={number(e['f'],20,20000,'segment_eq.f')}:t=q:w={number(e.get('q',4.32),.1,10,'segment_eq.q')}"
                         f":g={number(e['g'],-12,12,'segment_eq.g')}")
        g.append(f'[i{k}]atrim=start_sample={a}:end_sample={b},asetpts=PTS-STARTPTS'+''.join(','+x for x in bells)+f'[o{k}]')
    g.append(''.join(f'[o{k}]' for k in range(len(cuts)))+f'concat=n={len(cuts)}:v=0:a=1'+(','+chain if chain else '')+'[out]')
    return ';'.join(g)

def speech_level(p):
    """(speech RMS dBFS, sample peak dBFS) of one channel: RMS over 0.4 s windows louder than the 40th
    percentile (i.e. while speaking), so pauses do not pull the level down."""
    import numpy as np
    raw=subprocess.run(['ffmpeg','-v','error','-i',str(p),'-af','pan=mono|c0=c0','-ar','48000','-f','f32le','-'],
                       capture_output=True,check=True).stdout
    x=np.frombuffer(raw,np.float32).astype(np.float64)
    w=int(0.4*48000); n=len(x)//(w//2)-1
    st=np.array([np.sqrt(np.mean(x[i*(w//2):i*(w//2)+w]**2)) for i in range(max(1,n))])
    rms=float(np.sqrt(np.mean(st[st>=np.percentile(st,40)]**2)))
    return 20*math.log10(rms+1e-12),20*math.log10(float(np.abs(x).max())+1e-12)

def voice_chain(v):
    """Speech clean-up applied to the cut dialogue before loudness normalisation."""
    if not v or not v.get('enabled',True):
        return ''
    parts=[]
    hp=number(v.get('highpass_hz',85),0,150,'highpass_hz')
    if hp:


        parts.append(f'highpass=f={hp}:poles=2,highpass=f={hp}:poles=2')
    nr=number(v.get('denoise_db',0),0,20,'denoise_db')
    if nr:


        parts.append(f'afftdn=nr={nr}:nf=-38:tn=1')
    mud=number(v.get('mud_db',0),-6,0,'mud_db')
    if mud:
        parts.append(f'equalizer=f=300:t=q:w=1.1:g={mud}')
    pres=number(v.get('presence_db',0),0,6,'presence_db')
    if pres:


        parts.append(f'equalizer=f=2800:t=q:w=1.2:g={pres}')


    for bell in v.get('eq',[]):
        if set(bell)-{'f','q','g'}:
            raise ValueError("voice.eq entries accept only f, q, g")
        f=number(bell['f'],20,20000,'eq.f')
        q=number(bell.get('q',1.0),.1,10,'eq.q')
        g=number(bell['g'],-12,12,'eq.g')
        parts.append(f'equalizer=f={f}:t=q:w={q}:g={g}')
    if v.get('deesser',False):
        sib=number(v.get('sibilance_db',-2.5),-6,0,'sibilance_db')
        sf=number(v.get('sibilance_hz',7200),3000,12000,'sibilance_hz')
        parts.append('deesser=i=0.4:m=0.5:f=0.5')
        parts.append(f'equalizer=f={sf}:t=q:w=1.8:g={sib}')
    comp=v.get('compress',False)
    if comp:


        for c in ([{}] if comp is True else comp if isinstance(comp,list) else [comp]):
            if set(c)-COMPRESS_FIELDS:
                raise ValueError(f'voice.compress accepts only {sorted(COMPRESS_FIELDS)}')
            th=number(c.get('threshold_db',-20),-40,-6,'compress.threshold_db')
            ra=number(c.get('ratio',1.6),1,8,'compress.ratio')
            at=number(c.get('attack_ms',25),0.5,200,'compress.attack_ms')
            re=number(c.get('release_ms',280),20,2000,'compress.release_ms')
            mk=number(c.get('makeup_db',1),1,12,'compress.makeup_db')
            kn=number(c.get('knee_db',2.83),1,8,'compress.knee_db')
            parts.append(f'acompressor=threshold={th}dB:ratio={ra}:attack={at}:release={re}:makeup={mk}:knee={kn}')
    post=v.get('post')
    if post:


        if set(post)-{'deess','eq'}:
            raise ValueError('voice.post accepts deess, eq')
        if post.get('deess'):
            parts.append(f"deesser=i={number(post['deess'],0.05,0.6,'voice.post.deess')}:m=0.5:f=0.5")
        for bell in post.get('eq',[]):
            if set(bell)-{'f','q','g'}:
                raise ValueError('voice.post.eq entries accept only f, q, g')
            parts.append(f"equalizer=f={number(bell['f'],20,20000,'post.eq.f')}:t=q:w={number(bell.get('q',1.0),.1,10,'post.eq.q')}"
                         f":g={number(bell['g'],-12,12,'post.eq.g')}")
    ds=v.get('deess_split')
    if ds:


        if set(ds)-{'low_hz','high_hz','threshold','ratio','release_ms'}:
            raise ValueError('voice.deess_split accepts low_hz, high_hz, threshold, ratio, release_ms')
        lo=number(ds.get('low_hz',3300),1500,8000,'deess_split.low_hz')
        hi=number(ds.get('high_hz',5200),lo+300,12000,'deess_split.high_hz')
        thr=number(ds.get('threshold',0.003),0.000976563,1,'deess_split.threshold')
        rat=number(ds.get('ratio',10),1,20,'deess_split.ratio')
        rel=number(ds.get('release_ms',45),5,500,'deess_split.release_ms')
        parts.append(f'acrossover=split={lo:g} {hi:g}:order=4th[dsl][dsm][dsh];[dsh]asplit[dsh1][dsk];'
                     f'[dsm][dsk]sidechaincompress=threshold={thr}:ratio={rat}:attack=1:release={rel}:makeup=1:knee=2[dsc];'
                     f'[dsl][dsc][dsh1]amix=inputs=3:normalize=0')
    if parts:
        parts.append('alimiter=limit=0.95:level=disabled')
    return ','.join(parts)

def validate_edit(edit):
    allowed={'name','width','height','fps','clips','music','captions','target_lufs','true_peak_db','encoder','voice','broll','layout','reuse_video_from','resume_parts_from','jobs','hwdec','loudness','overlay','sfx'}
    if set(edit)-allowed:
        raise ValueError(f'Unsupported edit fields: {sorted(set(edit)-allowed)}')
    w,h = int(edit.get('width',1080)), int(edit.get('height',1920))

    if (w,h) not in [(1080,1920),(2160,3840),(360,640),(3840,2160),(1920,1080),(640,360)]:
        raise ValueError('Supported sizes: 1080x1920, 2160x3840, 3840x2160, 1920x1080, 360x640 / 640x360 (test).')
    fps = number(edit.get('fps',30), 23,60,'fps')
    if fps not in [24,25,30,50,60] and min(abs(fps-x) for x in [24000/1001,30000/1001,60000/1001]) > .001:
        raise ValueError('Use a standard frame rate, preferably matching source cadence.')
    if not edit.get('clips'):
        raise ValueError('No clips')
    total = 0
    validated=[]
    previous_len=0
    previous_transition=0
    for i,c in enumerate(edit['clips']):
        allowed_clip={'file','in','out','color','lut','framing','focus_x','focus_y','zoom','brightness','contrast','saturation','audio_gain_db','transition','mute','stabilize','grade','sharpen','transition_type','skin','ramp_in','ramp_out','drift','denoise','audio_fade_in','audio_fade_out','mirror','float','punch'}
        if set(c)-allowed_clip:
            raise ValueError(f'Unsupported clip fields: {sorted(set(c)-allowed_clip)}; use Resolve or implement and test the feature explicitly.')
        if c.get('grade','neutral') not in GRADES:
            raise ValueError(f"grade must be one of {sorted(GRADES)}")
        number(c.get('sharpen',0),0,1.2,'sharpen')
        number(c.get('audio_fade_in',0.005),0.001,0.1,'audio_fade_in')
        number(c.get('skin',0),0,1,'skin')
        for rk in ('ramp_in','ramp_out'):
            if c.get(rk):
                validate_ramp(c[rk], rk)
                if c.get('framing','crop')=='fit':
                    raise ValueError(f'{rk} needs framing crop')
        if c.get('drift'):
            dr=c['drift']
            if not isinstance(dr,dict) or set(dr)-{'kind','amount','t0','span','ease'}:
                raise ValueError('drift must be {kind, amount, t0, span, ease}')
            if dr.get('kind','push') not in ('push','pull','pan_left','pan_right','rise','sink'):
                raise ValueError('drift.kind must be push, pull, pan_left, pan_right, rise or sink')
            number(dr.get('amount',0.06),0.01,0.3,'drift.amount')
        if c.get('float'):
            number(c['float'],0.002,0.03,'float')
        for pu in c.get('punch',[]) or []:
            if not isinstance(pu,dict) or set(pu)-{'t','amount','dur'}:
                raise ValueError('punch entries are {t, amount, dur}')
            number(pu['t'],0,600,'punch.t'); number(pu.get('amount',0.07),0.01,0.25,'punch.amount')
            number(pu.get('dur',0.16),0.05,0.6,'punch.dur')
        st=c.get('stabilize')
        if st not in (None,False):
            if st is True:
                st={}
            if not isinstance(st,dict) or set(st)-{'smoothing','shakiness','zoom'}:
                raise ValueError("stabilize must be false, true, or an object with smoothing/shakiness/zoom")
            number(st.get('smoothing',10),1,60,'stabilize.smoothing')
            number(st.get('shakiness',5),1,10,'stabilize.shakiness')
            number(st.get('zoom',1),0,10,'stabilize.zoom')
        tt=c.get('transition_type','fade')
        if tt not in TRANSITIONS:
            raise ValueError(f'transition_type must be one of {sorted(TRANSITIONS)}')
        if tt!='fade' and not c.get('transition',0):
            raise ValueError('transition_type needs a non-zero transition duration')
        p=path(c['file']); m=probe(p); v=video(m)
        start=number(c.get('in',0),0,duration(m),'in')
        end=number(c.get('out',duration(m)),start,duration(m)+.001,'out')
        nframes=round((end-start)*fps)
        if nframes < 2:
            raise ValueError('Clip must contain at least two output frames')
        length=nframes/fps
        trans=round(number(c.get('transition',0),0,.5,'transition')*fps)/fps
        if i==0 and trans:
            raise ValueError('First clip cannot transition from a previous clip')
        if trans and (length <= trans+.05 or previous_len <= previous_transition+trans+.05):
            raise ValueError('Transition overlaps another transition or consumes a clip')
        color_filter(c,v)
        if c.get('framing','crop') not in ['crop','fit']:
            raise ValueError('framing must be crop or fit')
        validated.append((c,p,m,start,length,trans))
        total+=length-trans
        previous_len,previous_transition=length,trans
    number(edit.get('target_lufs',-14),-24,-9,'target_lufs')
    number(edit.get('true_peak_db',-1.5),-3,-1,'true_peak_db')
    if edit.get('encoder','libx264') not in ['libx264','h264_nvenc']:
        raise ValueError('encoder must be libx264 or h264_nvenc')
    v=edit.get('voice')
    if v is not None:
        if not isinstance(v,dict) or set(v)-VOICE_FIELDS:
            raise ValueError(f'voice accepts only {sorted(VOICE_FIELDS)}')
        voice_chain(v)
    music=edit.get('music')
    if music:
        if set(music)-{'file','gain_db','license_note','start','highpass_hz','voice_dip_db','duck_threshold','duck_ratio','duck_release_ms','boost'}:
            raise ValueError('Unsupported music fields')
        if not path(music['file']).is_file() or not music.get('license_note'):
            raise ValueError('Music requires a file and license_note pointing to the actual license record.')
        number(music.get('gain_db',-22),-60,0,'music gain')
    for s in edit.get('sfx',[]):

        if set(s)-{'file','at','gain_db','in','dur','note'}:
            raise ValueError('sfx entries: file, at, gain_db, in, dur, note')
        if not path(s['file']).is_file():
            raise FileNotFoundError(s['file'])
        number(s['at'],0,36000,'sfx.at'); number(s.get('gain_db',0),-60,12,'sfx.gain_db')
    ov=edit.get('overlay')
    if ov is not None:


        if set(ov)-{'file','x','y'}:
            raise ValueError('Unsupported overlay fields')
        if not path(ov['file']).is_file():
            raise FileNotFoundError(ov['file'])
        number(ov.get('x',0),0,w,'overlay.x'); number(ov.get('y',0),0,h,'overlay.y')
    if edit.get('captions'):
        if not path(edit['captions']).is_file():
            raise FileNotFoundError(edit['captions'])
        if path(edit['captions']).suffix.lower() not in ['.ass','.srt']:
            raise ValueError('captions must be .ass or .srt')
    broll=validate_broll(edit.get('broll',[]), total)
    edit['_layout']=validate_layout(edit.get('layout'), total, h, validated)
    return w,h,fps,total,validated,broll


HWDEC=[]


_INTER=None
_INTER_LOCK=threading.Lock()
def inter():
    """Codec args for an intermediate file: NVENC where the box has it, ProRes 422 HQ otherwise."""
    global _INTER


    with _INTER_LOCK:
        return _inter()

def _inter():
    global _INTER
    if _INTER is None:


        nv=['-c:v','hevc_nvenc','-preset','p4','-rc','constqp','-qp','12','-bf','0',
            '-forced-idr','1','-force_key_frames','expr:1','-pix_fmt','p210le']
        try:
            ff(['-f','lavfi','-i','testsrc2=size=320x180:rate=30','-t','0.1',*nv,'-f','null','-'])
            _INTER=nv+['-tag:v','hvc1']
        except Exception:
            _INTER=['-c:v','prores_ks','-profile:v','3','-threads','8','-pix_fmt','yuv422p10le']
    return list(_INTER)

def normalize_segment(c,p,m,start,length,w,h,fps,dest,ft=0,part=None):
    vf=color_filter(c,video(m))
    vf+=f',fps={fps},setpts=PTS-STARTPTS'


    if c.get('mirror'):
        vf+=',hflip'
    st=c.get('stabilize')
    if st:
        st={} if st is True else st
        sh=number(st.get('shakiness',5),1,10,'stabilize.shakiness')
        sm=number(st.get('smoothing',10),1,60,'stabilize.smoothing')
        zm=number(st.get('zoom',1),0,10,'stabilize.zoom')
        trf=dest.with_suffix('.trf')


        ff(HWDEC+['-ss',start,'-i',p,'-t',length,'-vf',vf+f',vidstabdetect=shakiness={sh}:accuracy=15:result={filter_path(trf)}','-an','-f','null','-'],dest.with_name(dest.stem+'_vidstabdetect.log'),ft=ft)


        import time
        prev=-1
        for _ in range(20):
            size=trf.stat().st_size if trf.exists() else -1
            if size>0 and size==prev:
                break
            prev=size; time.sleep(0.5)
        trf.read_bytes()


        vf+=f',vidstabtransform=input={filter_path(trf)}:smoothing={sm}:optzoom=1:zoom={zm}:interpol=bicubic:crop=black,unsharp=5:5:0.3:3:3:0.0'
    if c.get('framing','crop')=='fit':
        vf+=f',scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black'
    elif c.get('ramp_in') or c.get('ramp_out') or c.get('drift'):
        vf+=ramp_filter(c,length,w,h,fps)
    else:
        zoom=number(c.get('zoom',1),1,1.5,'zoom')
        fx=number(c.get('focus_x',.5),0,1,'focus_x'); fy=number(c.get('focus_y',.5),0,1,'focus_y')
        zw=math.ceil(w*zoom/2)*2; zh=math.ceil(h*zoom/2)*2
        vf+=f',scale={zw}:{zh}:force_original_aspect_ratio=increase:force_divisible_by=2:flags=lanczos,crop={w}:{h}:(iw-ow)*{fx}:(ih-oh)*{fy}'
    dn=number(c.get('denoise',0),0,1,'denoise')
    if dn:


        vf+=f',hqdn3d={3*dn:.2f}:{2.5*dn:.2f}:{6*dn:.2f}:{4.5*dn:.2f}'
    br=number(c.get('brightness',0),-.15,.15,'brightness')
    co=number(c.get('contrast',1),.7,1.3,'contrast')
    sat=number(c.get('saturation',1),0,1.4,'saturation')
    if (br,co,sat)!=(0,1,1):
        expr=f'clip((val-maxval/2)*{co}+maxval/2+{br}*maxval,0,maxval)'
        weights=[.2126,.7152,.0722]
        matrix=[]
        for row,r in enumerate('rgb'):
            for col,cname in enumerate('rgb'):
                matrix.append(f'{r}{cname}={(1-sat)*weights[col]+(sat if row==col else 0)}')
        vf+=f",format=gbrp16le,lutrgb=r='{expr}':g='{expr}':b='{expr}',colorchannelmixer="+':'.join(matrix)
    skin=number(c.get('skin',0),0,1,'skin')
    if skin:


        vf+=','+skin_filter(skin,min(w,h)/1080)
    grade=GRADES[c.get('grade','neutral')]
    if grade:
        vf+=',format=gbrp16le,'+grade
    amount=number(c.get('sharpen',0),0,1.2,'sharpen')
    if amount:
        vf+=f',unsharp=5:5:{amount}:5:5:0.0'
    vf+=',setsar=1,format=yuv422p10le,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=limited'
    cmd=HWDEC+['-ss',start,'-i',p]
    audible=has_audio(m) and not c.get('mute',False)
    if not audible:
        cmd+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
    gain=number(c.get('audio_gain_db',0),-60,20,'audio_gain_db')


    fin=number(c.get('audio_fade_in',0.005),0.001,0.1,'audio_fade_in')
    fout=number(c.get('audio_fade_out',0.005),0.001,0.1,'audio_fade_out')
    af=f'aresample=48000,aformat=channel_layouts=stereo,volume={gain}dB,apad=whole_dur={length},atrim=duration={length},asetpts=PTS-STARTPTS,afade=t=in:d={fin},afade=t=out:st={max(0,length-fout)}:d={fout}'
    tags=['-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-color_range','tv','-map_metadata','-1']
    if part and part[0]=='video':


        _,off,clen=part
        if gpu_ok(c,w):
            try:
                cmd=gpu_segment_cmd(c,p,m,start,length,w,h,fps,dest,off,clen)
                with _GPU_SEM:
                    ff(cmd,dest.with_suffix('.log'))
                return
            except Exception as exc:

                print(f'GPU path failed for {dest.name} ({str(exc).splitlines()[0][:80]}), using CPU',flush=True)
                dest.unlink(missing_ok=True)

        vf=(vf.replace(',setpts=PTS-STARTPTS',f',setpts=PTS-STARTPTS+{off}/TB',1)
            +f',setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop=2,setpts=N/({fps}*TB)')
        ff(HWDEC+['-ss',start+off,'-i',p,'-map','0:v:0','-frames:v',round(clen*fps),'-vf',vf,'-an',*inter(),*tags,dest],
           dest.with_suffix('.log'),ft=ft)
        return
    if part and part[0]=='audio':
        ff(cmd[len(HWDEC):]+['-map','0:a:0' if audible else '1:a:0','-t',length,'-af',af,'-vn',
                             '-c:a','pcm_s16le','-ar','48000','-ac','2','-map_metadata','-1',dest],
           dest.with_suffix('.log'))
        return
    cmd+=['-map','0:v:0','-map','0:a:0' if audible else '1:a:0','-t',length,'-vf',vf,'-af',af,*inter(),'-c:a','pcm_s16le','-ar','48000','-ac','2',*tags,dest]
    ff(cmd,dest.with_suffix('.log'),ft=ft)

def chunkable(c):
    """A segment can be cut into time-slices rendered in parallel when every filter in its chain works
    on one frame at a time (or only reads the clock). Stabilisation measures motion over the whole
    segment and hqdn3d averages across frames, so those segments render whole."""
    return not c.get('stabilize') and not number(c.get('denoise',0),0,1,'denoise')

BROLL_FIELDS={'file','at','duration','framing','fade','kenburns','in','note','card_width','card_y','dim','enter','exit','grade'}

def validate_broll(items, total):
    out=[]
    last_end=0.0; last_exit=None
    for i,b in enumerate(items):
        if set(b)-BROLL_FIELDS:
            raise ValueError(f'Unsupported broll fields: {sorted(set(b)-BROLL_FIELDS)}')
        p=path(b['file'])
        if not p.is_file():
            raise FileNotFoundError(b['file'])
        at=number(b['at'],0,max(total-0.2,0.001),'broll.at')
        dur=number(b['duration'],0.4,12,'broll.duration')
        if at+dur>total+0.001:
            raise ValueError(f'broll {i} runs past the end of the timeline')
        fade=number(b.get('fade',0.16),0,0.5,'broll.fade')


        handoff=last_exit=='cut' and b.get('enter','fade')=='fade' and at>=last_end-fade-0.251
        if at<last_end-0.001 and not handoff:
            raise ValueError('broll inserts must be in order and must not overlap')
        last_end=at+dur; last_exit=b.get('exit','fade')
        if fade*2>=dur:
            raise ValueError('broll fades consume the whole insert')
        if b.get('framing','crop') not in ['crop','fit','card']:
            raise ValueError('broll framing must be crop, fit or card')
        number(b.get('card_width',940),300,1080,'broll.card_width')
        number(b.get('card_y',860),200,1720,'broll.card_y')
        number(b.get('dim',0.45),0,0.9,'broll.dim')
        number(b.get('kenburns',0.06),0,0.3,'broll.kenburns')
        number(b.get('in',0),0,86400,'broll.in')
        for k in ('enter','exit'):
            if b.get(k,'fade') not in ('fade','slide_left','slide_right','zoom','cut'):
                raise ValueError(f'broll.{k} must be fade, slide_left, slide_right, zoom or cut')
        if b.get('exit')=='zoom':
            raise ValueError('broll.exit supports fade or slide_*; zoom is an entrance')
        if b.get('grade','neutral') not in GRADES:
            raise ValueError(f'broll.grade must be one of {sorted(GRADES)}')
        if dur < 1.0 and ({b.get('enter','fade'),b.get('exit','fade')} & {'slide_left','slide_right','zoom'}):
            raise ValueError('broll with slide/zoom moves needs at least 1 s')
        out.append((b,p,at,dur,fade))
    return out

def apply_broll(base, items, w, h, fps, work):
    """Local video analysis and editing helper."""


    n=round(duration(probe(base))*fps)
    groups=[]
    for it in sorted(items,key=lambda t:t[2]):
        k0=max(0,math.floor(it[2]*fps+1e-6)); k1=min(n,math.ceil((it[2]+it[3])*fps-1e-6)+1)
        if groups and k0<=groups[-1][1]+2*fps:
            groups[-1][1]=max(groups[-1][1],k1); groups[-1][2].append(it)
        else:
            groups.append([k0,k1,[it]])


    vmeta=video(probe(base)); nb=int(vmeta.get('nb_frames') or 0)
    gapless=nb==round(float(vmeta.get('duration') or 0)*fps)
    if not gapless or sum(g[1]-g[0] for g in groups)>0.6*n:
        if not gapless:
            print(f'B-roll: timeline has {nb} frames over {float(vmeta.get("duration") or 0):.3f} s, full pass',flush=True)
        return broll_pass(base,items,w,h,fps,work,work/'timeline_broll.mov')


    seek=lambda k:['-ss',f'{(k-0.5)/fps:.6f}'] if k>0 else []
    span=lambda a,b:['-t',f'{(b-a)/fps:.6f}']
    pieces=[]; k=0
    for gi,(k0,k1,its) in enumerate(groups):
        if k0>k:
            p=work/f'broll_keep{gi}.mov'
            ff(['-i',base,*seek(k),*span(k,k0),'-map','0:v:0','-c','copy',p],work/f'broll_keep{gi}.log')
            pieces.append(p)
        shifted=[(b,p,at-k0/fps,dur,fade) for b,p,at,dur,fade in its]
        pieces.append(broll_pass(base,shifted,w,h,fps,work,work/f'broll_part{gi}.mov',seek(k0),span(k0,k1)))
        k=k1
    if k<n:
        p=work/'broll_keep_end.mov'
        ff(['-i',base,*seek(k),'-map','0:v:0','-c','copy',p],work/'broll_keep_end.log')
        pieces.append(p)
    listing=work/'concat_broll.txt'
    listing.write_text(''.join(f"file '{p.name}'\n" for p in pieces),encoding='utf-8')
    dest=work/'timeline_broll.mov'
    ff(['-f','concat','-safe','1','-i',listing,'-i',base,'-map','0:v','-map','1:a:0','-c','copy',dest],work/'broll_join.log')

    vd=lambda p:float(video(probe(p)).get('duration') or 0)
    if abs(vd(dest)-vd(base))>1.5/fps:


        print(f'B-roll splice changed video duration ({vd(dest):.3f} vs {vd(base):.3f} s); retrying full pass',flush=True)
        dest.unlink()
        return broll_pass(base,items,w,h,fps,work,dest)
    return dest

def push_clip(p, w, h, fps, dur, kb, framing):
    """Cache reusable b-roll segments for rendering."""


    key=hashlib.sha1(Path(p).read_bytes()).hexdigest()[:16]+f'_{w}x{h}_{fps}_{dur:.3f}_{kb}_{framing}'
    cache=ROOT/'01_PROJECTS/_analysis/kenburns'
    cache.mkdir(parents=True,exist_ok=True)
    dest=cache/f'{key}.mov'
    if dest.is_file() and dest.stat().st_size>0:
        return dest
    kf=max(2,round(dur*fps)); step=kb/kf
    tmp=dest.with_name(dest.stem+'.part.mov'); tmp.unlink(missing_ok=True)


    if framing=='fit':
        fit=f'scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black'
    else:
        fit=f'scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2:flags=lanczos,crop={w}:{h}'
    vf=(f"{fit},scale={w*2}:{h*2}:flags=lanczos,"
        f"zoompan=z='min(zoom+{step:.6f},{1+kb})':d={kf}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
        f"setsar=1,format=yuv422p10le")
    ff(['-i',p,'-vf',vf,'-frames:v',kf,*inter(),tmp],cache/f'{key}.log')
    tmp.replace(dest)
    return dest

def broll_pass(base, items, w, h, fps, work, dest, seek=(), limit=()):
    """Composite the inserts over `base` (or over the `limit` span of it, e.g. [-t, s], from `seek`)."""
    cmd=[*seek,'-i',base]
    graph=[]
    layer='0:v'
    stills={'png','mjpeg','jpeg','webp','bmp','gif','tiff'}
    todo={i:(p,dur,number(b.get('kenburns',0.06),0,0.3,'broll.kenburns'),b.get('framing','crop'))
          for i,(b,p,at,dur,fade) in enumerate(items)
          if b.get('framing','crop')!='card' and number(b.get('kenburns',0.06),0,0.3,'broll.kenburns')>0
          and video(probe(p)).get('codec_name','') in stills}
    pushes={}
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(12,len(todo))) as ex:
            for i,clip in zip(todo,ex.map(lambda a:push_clip(a[0],w,h,fps,a[1],a[2],a[3]),todo.values())):
                pushes[i]=clip
    for i,(b,p,at,dur,fade) in enumerate(items):
        if i in pushes:

            p=pushes[i]
            b={**b,'in':0,'kenburns':0}
        meta=probe(p)


        codec=video(meta).get('codec_name','')
        is_video=codec not in {'png','mjpeg','jpeg','webp','bmp','gif','tiff'}
        still_push=(not is_video and b.get('framing','crop')!='card'
                    and number(b.get('kenburns',0.06),0,0.3,'broll.kenburns')>0)
        if is_video:
            cmd+=['-ss',b.get('in',0),'-t',dur,'-i',p]
            pre=f'fps={fps},setpts=PTS-STARTPTS'
        elif still_push:


            cmd+=['-i',p]
            pre='setpts=PTS-STARTPTS'
        else:
            cmd+=['-loop','1','-framerate',fps,'-t',dur,'-i',p]
            pre=f'fps={fps},setpts=PTS-STARTPTS'
        if b.get('framing','crop')=='card':


            k=w/1080
            cw=int(number(b.get('card_width',940),300,1080,'broll.card_width')*k)//2*2
            cy=number(b.get('card_y',860),200,1720,'broll.card_y')*k
            dim=number(b.get('dim',0.45),0,0.9,'broll.dim')
            ramp=max(fade,0.12)
            graph.append(f'color=c=black@{dim}:s={w}x{h}:r={fps}:d={dur},format=yuva420p,'
                         f'fade=t=in:st=0:d={fade}:alpha=1,fade=t=out:st={max(0,dur-fade):.4f}:d={fade}:alpha=1,'
                         f'setpts=PTS-STARTPTS+{at}/TB[d{i}]')
            graph.append(f"[{layer}][d{i}]overlay=0:0:eof_action=pass:enable='between(t,{at},{at+dur})'[dv{i}]")
            graph.append(f'[{i+1}:v]{pre},scale={cw}:-2:flags=lanczos,setsar=1,format=yuva420p,'
                         f'fade=t=in:st=0:d={fade}:alpha=1,fade=t=out:st={max(0,dur-fade):.4f}:d={fade}:alpha=1,'
                         f'setpts=PTS-STARTPTS+{at}/TB[b{i}]')
            graph.append(f"[dv{i}][b{i}]overlay=x=(W-w)/2:y='{cy}-h/2+{40*k:.0f}*max(0\\,1-(t-{at})/{ramp})'"
                         f":eval=frame:eof_action=pass:enable='between(t,{at},{at+dur})'[v{i}]")
            layer=f'v{i}'
            continue
        if b.get('framing','crop')=='fit':
            fit=f'scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black'
        else:
            fit=f'scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2:flags=lanczos,crop={w}:{h}'
        kb=number(b.get('kenburns',0.06),0,0.3,'broll.kenburns')
        enter,exit_=b.get('enter','fade'),b.get('exit','fade')
        md=0.28
        motion=''
        if enter=='zoom':

            motion+=f",zoompan=z='1+0.15*pow(max(0\\,1-it/{md}),2)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={fps}"
        if kb and not is_video:


            kf=max(2,round(dur*fps))
            step=kb/kf
            motion=(f',scale={w*2}:{h*2}:flags=lanczos,'
                    f"zoompan=z='min(zoom+{step:.6f},{1+kb})':d={kf}"
                    f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}")

        gr=GRADES[b.get('grade','neutral')]
        gr=f',format=gbrp16le,{gr}' if gr else ''
        fin=fade if enter=='fade' else (0.08 if enter=='zoom' else 0)
        fout=fade if exit_=='fade' else 0
        alpha=''
        if fin:
            alpha+=f',fade=t=in:st=0:d={fin}:alpha=1'
        if fout:
            alpha+=f',fade=t=out:st={max(0,dur-fout):.4f}:d={fout}:alpha=1'
        chain=(f'[{i+1}:v]{pre},{fit}{motion}{gr},setsar=1,format=yuva420p{alpha},'
               f'setpts=PTS-STARTPTS+{at}/TB[b{i}]')
        graph.append(chain)


        e_in=f'pow(max(0\\,1-(t-{at})/{md}),2)'
        e_out=f'pow(max(0\\,(t-{at+dur-md:.4f})/{md}),2)'
        xs=[]

        if enter in ('slide_left','slide_right'):
            xs.append(f"({'' if enter=='slide_left' else '0-'}W*{e_in})")
        if exit_ in ('slide_left','slide_right'):
            xs.append(f"({'0-' if exit_=='slide_left' else ''}W*{e_out})")
        x='+'.join(xs) or '0'
        graph.append(f"[{layer}][b{i}]overlay=x='{x}':y=0:eval=frame:eof_action=pass:enable='between(t,{at},{at+dur})'[v{i}]")
        layer=f'v{i}'
    audio=[*limit,'-an'] if limit else ['-map','0:a:0','-c:a','pcm_s16le','-ar','48000']


    gpath=work/f'{dest.stem}_graph.txt'
    gpath.write_text(';'.join(graph),encoding='utf-8')
    ff(cmd+['-filter_complex_script',gpath,'-map',f'[{layer}]',*audio,
            *inter(),
            '-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',dest],
       work/f'{dest.stem}.log')
    return dest

TOP_FIELDS={'file','start','end','in','fit','motion','note'}
STILL_CODECS={'png','mjpeg','jpeg','webp','bmp','gif','tiff'}

def display_size(meta):
    v=video(meta); iw,ih=int(v['width']),int(v['height'])
    rot=next((s.get('rotation',0) for s in v.get('side_data_list',[]) if 'rotation' in s),0)
    return (ih,iw) if abs(int(rot))%180==90 else (iw,ih)

def zoom_at(c, prog):
    """Zoom factor of a segment at drift progress 0..1 (ramps ignored)."""
    base=float(c.get('zoom',1)); dr=c.get('drift') or {}
    a=float(dr.get('amount',0.06)) if dr else 0
    kind=dr.get('kind','push') if dr else None
    extra={'push':a*prog,'pull':a*(1-prog)}.get(kind, a if kind else 0)
    return base*(1+extra)

def window(c, meta, zoom, w, h):
    """Source-pixel origin and scale (source px per output px) of a crop-framed segment."""
    iw,ih=display_size(meta)
    fx=float(c.get('focus_x',.5)); fy=float(c.get('focus_y',.5))
    cw,ch=min(iw,ih*w/h),min(ih,iw*h/w)
    ox,oy=(iw-cw)*fx,(ih-ch)*fy
    return ox+(cw-cw/zoom)*fx, oy+(ch-ch/zoom)*fy, (cw/zoom)/w

def intro_transform(full_c, split_c, meta, w, h, top_h):
    """Where the full frame has to land so that it lines up with the lower panel.

    Both framings are crops of the same source with the same pixel aspect, so
    the move is a uniform scale plus a shift: s, dx, dy in output pixels.
    """
    zf=zoom_at(full_c,1.0)
    dr=split_c.get('drift') or {}
    span=float(dr.get('span',1) or 1); t0=float(dr.get('t0',0))
    zp=zoom_at(split_c,min(1,max(0,t0/span)))
    xf,yf,kf=window(full_c,meta,zf,w,h)
    xp,yp,kp=window(split_c,meta,zp,w,h-top_h)
    return zf, kf/kp, (xf-xp)/kp, top_h+(yf-yp)/kp

def validate_layout(lay, total, h, clips=None):
    if not lay:
        return None
    if set(lay)-{'mode','top_h','top','fade','grade','intro'}:
        raise ValueError(f"layout accepts mode, top_h, top, fade, grade, intro")
    if lay.get('mode')!='split':
        raise ValueError("layout.mode must be 'split'")

    s=h/1920
    top_h=int(number(lay.get('top_h',768*s),320*s,h-480*s,'layout.top_h'))//2*2
    fade=number(lay.get('fade',0.3),0,1,'layout.fade')
    if lay.get('grade','neutral') not in GRADES:
        raise ValueError(f'layout.grade must be one of {sorted(GRADES)}')
    items=lay.get('top') or []
    if not items:
        raise ValueError('layout.top needs at least one media item')


    intro=lay.get('intro')
    prev=0.0
    if intro:
        if set(intro)-{'clips','dur'}:
            raise ValueError('layout.intro accepts clips, dur')
        n=int(intro.get('clips',1)); dur=number(intro.get('dur',0.45),0.2,1.0,'layout.intro.dur')
        if not clips or not 1<=n<len(clips):
            raise ValueError('layout.intro.clips must leave at least one clip for the split part')
        if any(clips[i][5] for i in range(1,n+1)):
            raise ValueError('layout.intro: no crossfade transitions inside the intro or into the split part')
        if clips[n-1][0].get('ramp_out') or clips[n][0].get('ramp_in'):
            raise ValueError('layout.intro: the intro move replaces the ramp at the intro boundary; remove ramp_out/ramp_in there')
        if clips[n][4] <= dur + 0.1:
            raise ValueError('layout.intro.dur is longer than the first split clip')
        prev=sum(clips[i][4] for i in range(n))
        intro={'clips':n,'dur':dur,'t0':prev}
    for i,t in enumerate(items):
        if set(t)-TOP_FIELDS:
            raise ValueError(f'layout.top[{i}] accepts {sorted(TOP_FIELDS)}')
        if not path(t['file']).is_file():
            raise FileNotFoundError(t['file'])
        s,e=float(t['start']),float(t['end'])


        if abs(s-prev)>0.001:
            raise ValueError(f'layout.top[{i}] starts at {s}, previous item ended at {prev}: the top track must be continuous')
        if e-s<fade+0.2:
            raise ValueError(f'layout.top[{i}] is shorter than its crossfade')
        if t.get('fit','cover') not in ('cover','fit_blur'):
            raise ValueError('layout.top[].fit must be cover or fit_blur')
        number(t.get('motion',0.05),0,0.3,'layout.top[].motion')
        prev=e
    if prev<total-0.05:
        raise ValueError(f'layout.top ends at {prev}s but the film lasts {total:.2f}s')
    return {'top_h':top_h,'fade':fade,'grade':lay.get('grade','neutral'),'items':items,'intro':intro}

def build_top_track(lay, w, fps, total, work):
    """Continuous media for the upper panel of a split-screen short.

    Each item fills its window with slow motion (stills and footage alike), so
    the panel never holds a dead frame; items meet in short crossfades that begin
    exactly at the planned cut. Screenshots use fit_blur: the page is shown whole
    over a blurred, darkened copy of itself instead of being cropped.
    """
    th=lay['top_h']; fade=lay['fade']; items=lay['items']; grade=GRADES[lay['grade']]
    W2,H2=int(w*1.25)//2*2,int(th*1.25)//2*2
    parts=[]
    for i,t in enumerate(items):
        dur=float(t['end'])-float(t['start'])+(fade if i<len(items)-1 else 0)
        p=path(t['file']); meta=probe(p)
        still=video(meta).get('codec_name','') in STILL_CODECS
        src=['-loop','1','-framerate',fps,'-t',dur,'-i',p] if still else ['-ss',t.get('in',0),'-t',dur,'-i',p]
        mo=number(t.get('motion',0.05),0,0.3,'motion')
        push=f"zoompan=z='1+{mo}*it/{dur:.3f}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:s={w}x{th}:fps={fps}"
        if t.get('fit','cover')=='fit_blur':
            vf=(f'fps={fps},setpts=PTS-STARTPTS,split[a][b];'
                f'[a]scale={W2}:{H2}:force_original_aspect_ratio=increase,crop={W2}:{H2},gblur=sigma=40,eq=brightness=-0.18:saturation=0.8[bg];'
                f'[b]scale={int(W2*0.86)}:{int(H2*0.86)}:force_original_aspect_ratio=decrease[fg];'
                f'[bg][fg]overlay=(W-w)/2:(H-h)/2,{push}')
        else:
            vf=f'fps={fps},setpts=PTS-STARTPTS,scale={W2}:{H2}:force_original_aspect_ratio=increase:flags=lanczos,crop={W2}:{H2},{push}'
        if grade:
            vf+=',format=gbrp16le,'+grade
        vf+=f',setsar=1,format=yuv422p10le,trim=duration={dur:.4f},setpts=PTS-STARTPTS'
        part=work/f'top_{i:02d}.mov'
        ff(src+['-filter_complex',vf,'-an',*inter(),part],work/f'top_{i:02d}.log')
        parts.append((part,dur))
    cmd=[]; graph=[]
    for i,(pp,_) in enumerate(parts):
        cmd+=['-i',pp]; graph.append(f'[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[t{i}]')
    lbl='t0'; acc=parts[0][1]
    for i in range(1,len(parts)):
        graph.append(f'[{lbl}][t{i}]xfade=transition=fade:duration={fade}:offset={acc-fade:.4f}[x{i}]')
        lbl=f'x{i}'; acc+=parts[i][1]-fade
    dest=work/'top_track.mov'
    ff(cmd+['-filter_complex',';'.join(graph),'-map',f'[{lbl}]','-t',total,*inter(),dest],work/'top_track.log')
    return dest

def compose_split(top, bottom, work):
    dest=work/'timeline_split.mov'
    ff(['-i',top,'-i',bottom,'-filter_complex','[0:v][1:v]vstack=inputs=2,setsar=1[v]','-map','[v]','-map','1:a:0',
        *inter(),'-c:a','pcm_s16le','-ar','48000',
        '-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',dest],work/'split.log')
    return dest

def same_codec(files):
    """Stream-copy concat is only valid when every piece has the same video codec (see inter())."""
    seen={}
    for f in files:
        seen.setdefault(video(probe(Path(f))).get('codec_name'),[]).append(Path(f).name)
    if len(seen)>1:
        raise RuntimeError(f'intermediates with mixed codecs, refusing to concat: { {k:len(v) for k,v in seen.items()} }')

def concat_parts(parts, clips, total, dest, work, tag):
    same_codec(parts)
    """Join rendered segments (hard cuts, or xfade where a clip asks for a transition)."""
    if not any(c[-1] for c in clips[1:]):
        listing=work/f'concat_{tag}.txt'
        listing.write_text(''.join(f"file '{p.name}'\n" for p in parts),encoding='utf-8')
        ff(['-f','concat','-safe','1','-i',listing,'-c','copy',dest],work/f'{tag}.log')
        return dest
    cmd=[]; graph=[]
    for i,p in enumerate(parts):
        cmd+=['-i',p]
        graph += [f'[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,format=yuv444p10le[v{i}]',f'[{i}:a]asetpts=PTS-STARTPTS[a{i}]']
    vl,al='v0','a0'; elapsed=clips[0][4]
    for i in range(1,len(parts)):
        trans=clips[i][-1]
        if trans:
            kind=clips[i][0].get('transition_type','fade')
            graph += [f'[{vl}][v{i}]xfade=transition={kind}:duration={trans}:offset={elapsed-trans}[vx{i}]',f'[{al}][a{i}]acrossfade=d={trans}:c1=tri:c2=tri[ax{i}]']
        else:
            graph += [f'[{vl}][{al}][v{i}][a{i}]concat=n=2:v=1:a=1[vx{i}][ax{i}]']
        vl,al=f'vx{i}',f'ax{i}'; elapsed+=clips[i][4]-trans
    ff(cmd+['-filter_complex',';'.join(graph),'-map',f'[{vl}]','-map',f'[{al}]','-t',total,*inter(),'-c:a','pcm_s16le','-ar','48000','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',dest],work/f'{tag}.log')
    return dest

def clips_len(clips):
    return sum(c[4] for c in clips)-sum(c[5] for c in clips[1:])

def join_intro(head, split, clips, n, dur, top_h, w, h, fps, total, work):
    """Full-frame intro -> split screen.

    Over `dur` seconds the full frame scales and slides until it covers exactly
    the lower panel (the geometry is computed, not eyeballed: both are crops of
    the same source), while the media panel drops in from above and hides the
    top of the moving frame. The last third crossfades into the real split
    render so any sub-pixel mismatch from drift or skin smoothing disappears.
    Audio is the continuous cut, untouched.
    """
    full_c=clips[n-1][0]; split_c,p,m,start,_,_=clips[n]
    zf,s1,dx,dy=intro_transform(full_c,split_c,m,w,h,top_h)


    ext_c={k:v for k,v in split_c.items() if k not in ('drift','ramp_in','ramp_out')}


    ext_c.update(zoom=zf, focus_x=full_c.get('focus_x',.5), focus_y=full_c.get('focus_y',.5),
                 drift={'kind':'pull','amount':0.01,'t0':600,'span':0.2})
    ext=work/'intro_ext.mov'
    normalize_segment(ext_c,p,m,start,round(dur*fps)/fps+2/fps,w,h,fps,ext)
    k=1.25; W,H=int(w*k)//2*2,int(h*k)//2*2; px,py=(W-w)//2,int(max(0,dy/s1))+24
    if py+h>H:
        H=(py+h+1)//2*2
    D=f'{(round(dur*fps)+1)/fps:.4f}'


    M=f'{dur*0.7:.4f}'
    P=f'(3*pow(min(1,{{t}}/{M}),2)-2*pow(min(1,{{t}}/{M}),3))'
    S=f'(1+{s1-1:.6f}*{P.format(t="it")})'
    z=f'{W/w:.6f}*{S}' if abs(W/w-H/h)<1e-6 else None
    if z is None:
        raise RuntimeError('intro pad must keep the frame aspect')
    x=f'{px}-({dx:.3f}*{P.format(t="it")})/{S}'
    y=f'{py}-({dy:.3f}*{P.format(t="it")})/{S}'
    slide=f'-{top_h}+{top_h}*{P.format(t="t")}'


    off=round(dur*0.7*fps)/fps; xd=round(dur*fps)/fps-off


    Dt=round(dur*fps)/fps
    graph=(f'[0:v]settb=AVTB,setpts=PTS-STARTPTS,format=yuv444p10le,pad={W}:{H}:{px}:{py}:black,'
           f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps},setsar=1,trim=duration={D},setpts=PTS-STARTPTS[fly];"
           f'[1:v]settb=AVTB,setpts=PTS-STARTPTS,format=yuv444p10le,split=2[sa][sb];'
           f'[sa]crop={w}:{top_h}:0:0[topin];'
           f'[sb]trim=start={off:.4f},setpts=PTS-STARTPTS,settb=AVTB,fps={fps}[late];'
           f"[fly][topin]overlay=x=0:y='{slide}':eval=frame:eof_action=pass,settb=AVTB,fps={fps}[mv];"
           f'[mv][late]xfade=transition=fade:duration={xd:.4f}:offset={off:.4f}[v]')
    moved=work/'intro_move.mov'
    ff(['-i',ext,'-t',f'{Dt+2/fps:.4f}','-i',split,'-filter_complex',graph,'-map','[v]','-map','1:a:0','-t',f'{Dt:.4f}',
        *inter(),'-c:a','pcm_s16le','-ar','48000',
        '-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',moved],work/'intro_move.log')
    rest=work/'intro_rest.mov'
    ff(['-ss',f'{Dt:.4f}','-i',split,'-c','copy',rest],work/'intro_rest.log')
    listing=work/'concat_intro_join.txt'
    listing.write_text(f"file '{Path(head).name}'\nfile '{moved.name}'\nfile '{rest.name}'\n",encoding='utf-8')
    dest=work/'timeline_intro_split.mov'
    ff(['-f','concat','-safe','1','-i',listing,'-c','copy',dest],work/'intro_join.log')
    save(work/'intro_transform.json',{'zoom_full':zf,'scale':s1,'dx':dx,'dy':dy,'dur':dur})
    return dest

def loudness(p, log):
    txt=ff(['-i',p,'-map','0:a:0','-af','loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json','-f','null','-'],log).stderr
    blocks=re.findall(r'\{\s*"input_i".*?\}',txt,re.S)
    if not blocks:
        raise RuntimeError('Missing loudness measurements')
    return json.loads(blocks[-1])

def render(args):
    edit=read(path(args.edit))
    w,h,fps,total,clips,broll=validate_edit(edit)
    layout=edit.pop('_layout',None)


    seg_h=h-layout['top_h'] if layout else h
    out=ROOT/'03_EXPORTS'/(safe_name(edit.get('name','film'))+'_'+stamp())
    work=out/'work'; work.mkdir(parents=True)
    save(out/'edit.json',edit)
    save(out/'sources.json',[{'file':str(p),'sha256':digest(p)} for _,p,_,_,_,_ in clips])
    reuse=edit.get('reuse_video_from')
    if reuse:


        src=path(reuse); man=read(src/'render_manifest.json'); prev=read(src/'edit.json')
        for k in ('clips','broll','layout','width','height','fps'):
            if prev.get(k)!=edit.get(k):
                raise ValueError(f'reuse_video_from: {k} differs from {src.name}; render the picture again')
        base=Path(man['master'])
        if not base.is_file():
            raise FileNotFoundError(f'{base} is gone (cleaned up?); render the picture again')
        print(f'Reusing picture from {src.name}',flush=True)
        layout=None; broll=[]
    intro=layout.get('intro') if layout else None
    n_full=intro['clips'] if intro else 0
    parts=[]


    resume=edit.get('resume_parts_from'); old_clips=[]
    if resume:
        old_clips=read(path(resume)/'edit.json').get('clips',[])


    jobs=int(number(edit.get('jobs',1),1,12,'jobs'))
    HWDEC[:]=['-hwaccel',edit['hwdec']] if edit.get('hwdec') else []
    tasks=[]
    for i,(c,p,m,start,length,trans) in enumerate([] if reuse else clips):
        part=work/f'part_{i:03d}.mov'
        parts.append(part)
        old=path(resume)/'work'/part.name if resume else None
        if old and i<len(old_clips) and old_clips[i]==edit['clips'][i] and old.is_file() and old.stat().st_size>0:
            try:
                ok=abs(duration(probe(old))-length)<1.5/fps
            except Exception:
                ok=False
            if ok:
                try:
                    os.link(old,part)
                except OSError:
                    shutil.copy2(old,part)
                print(f'Clip {i+1}/{len(clips)} reused from interrupted render',flush=True)
                continue
        tasks.append((i,c,p,m,start,length,h if i<n_full else seg_h,part))


    from concurrent.futures import ThreadPoolExecutor
    cpus=os.cpu_count() or 8
    SLICE=2.5
    workers=int(min(12,max(jobs,cpus//2)))
    units,joins=[],[]
    for t in tasks:
        i,c,p,m,start,length,hh,part=t
        n=round(length*fps)
        if not chunkable(c):
            units.append((length,t,None,part,4))
            continue

        k=max(1,round(length/(6.0 if gpu_ok(c,w) else SLICE)))
        cuts=[round(n*j/k) for j in range(k+1)]
        pieces=[]
        for j in range(k):
            dest=part.with_name(f'{part.stem}_s{j:02d}.mov')
            units.append(((cuts[j+1]-cuts[j])/fps,t,('video',cuts[j]/fps,(cuts[j+1]-cuts[j])/fps),dest,2))
            pieces.append(dest)
        aud=part.with_name(f'{part.stem}_audio.wav')
        units.append((0.1,t,('audio',),aud,2))
        joins.append((t,pieces,aud))
    units.sort(key=lambda u:-u[0])
    t_seg=time.time()
    def run_unit(u):
        _,(i,c,p,m,start,length,hh,_),kind,dest,ft=u
        normalize_segment(c,p,m,start,length,w,hh,fps,dest,ft=ft,part=kind)
        if kind is None:
            print(f'Clip {i+1}/{len(clips)} done ({time.time()-t_seg:.0f} s)',flush=True)
    print(f'Rendering {len(tasks)} segments as {len(units)} units, {workers} at a time'
          +(f', decode {edit["hwdec"]}' if HWDEC else ''),flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(run_unit,units))
    def join(j):
        (i,c,p,m,start,length,hh,part),pieces,aud=j
        listing=part.with_name(f'{part.stem}_slices.txt')
        listing.write_text(''.join(f"file '{q.name}'\n" for q in pieces),encoding='utf-8')
        ff(['-f','concat','-safe','1','-i',listing,'-i',aud,'-map','0:v','-map','1:a','-c','copy',
            '-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-color_range','tv',
            '-map_metadata','-1',part],part.with_suffix('.log'))
        got=int(video(probe(part)).get('nb_frames') or 0)
        if got and got!=round(length*fps):
            raise RuntimeError(f'segment {i+1}: joined slices hold {got} frames, expected {round(length*fps)}')
        print(f'Clip {i+1}/{len(clips)} done ({time.time()-t_seg:.0f} s, {len(pieces)} slices)',flush=True)

    with ThreadPoolExecutor(max_workers=max(1,len(joins))) as ex:
        list(ex.map(join,joins))
    if reuse:
        pass
    elif intro:
        t0=intro['t0']
        head=concat_parts(parts[:n_full],clips[:n_full],t0,work/'intro.mov',work,'intro')
        bottom=concat_parts(parts[n_full:],clips[n_full:],total-t0,work/'timeline.mov',work,'timeline')
    else:
        base=concat_parts(parts,clips,total,work/'timeline.mov',work,'timeline')
    if layout:
        print(f'Split layout: {len(layout["items"])} top-panel items',flush=True)
        if intro:
            top=build_top_track(layout,w,fps,total-t0,work)
            split=compose_split(top,bottom,work)
            base=join_intro(head,split,clips,n_full,intro['dur'],layout['top_h'],w,h,fps,total,work)
        else:
            top=build_top_track(layout,w,fps,total,work)
            base=compose_split(top,base,work)
    if broll:
        print(f'B-roll: {len(broll)} inserts',flush=True)
        base=apply_broll(base,broll,w,h,fps,work)

    voice=out/'dialogue.wav'
    ff(['-i',base,'-vn','-c:a','pcm_s24le',voice],work/'dialogue.log')
    chain=voice_chain(edit.get('voice'))
    seg=(edit.get('voice') or {}).get('segment_eq') if (edit.get('voice') or {}).get('enabled',True) else None
    if chain or seg:


        processed=out/'dialogue_processed.wav'
        if seg:
            ff(['-i',voice,'-filter_complex',segment_eq_graph(seg,total,chain),'-map','[out]','-ar','48000','-c:a','pcm_s24le',processed],work/'voice_chain.log')
        else:
            ff(['-i',voice,'-af',chain,'-ar','48000','-c:a','pcm_s24le',processed],work/'voice_chain.log')
        voice=processed
    pk=(edit.get('voice') or {}).get('peak')
    if pk and (edit.get('voice') or {}).get('enabled',True):


        if set(pk)-{'plr_db','attack_ms','release_ms'}:
            raise ValueError('voice.peak accepts plr_db, attack_ms, release_ms')
        rms,peak=speech_level(voice)
        ceil=rms+number(pk.get('plr_db',12),6,20,'voice.peak.plr_db')
        rep={'speech_rms_dbfs':round(rms,2),'peak_before_dbfs':round(peak,2),'ceiling_dbfs':round(ceil,2)}
        if peak>ceil:
            limited_v=out/'dialogue_peak.wav'
            ff(['-i',voice,'-af',f"aresample=192000,alimiter=limit={max(0.0625,min(1,10**(ceil/20))):.5f}"
                f":attack={number(pk.get('attack_ms',2),0.5,20,'voice.peak.attack_ms')}:release={number(pk.get('release_ms',60),10,500,'voice.peak.release_ms')}"
                f":level=false:latency=1,aresample=48000",'-c:a','pcm_s24le',limited_v],work/'voice_peak.log')
            voice=out/'dialogue_processed.wav'; shutil.move(limited_v,voice)
            rep['peak_after_dbfs']=round(speech_level(voice)[1],2)
        save(work/'voice_peak.json',rep)
        print(f"Voice peak stage: speech {rep['speech_rms_dbfs']} dBFS, peak {rep['peak_before_dbfs']} -> {rep.get('peak_after_dbfs',rep['peak_before_dbfs'])} (ceiling {rep['ceiling_dbfs']})")
    mix=work/'mix.wav'
    if edit.get('music'):
        mu=edit['music']; music=path(mu['file']); gain=mu.get('gain_db',-22)


        shape=''
        hp=number(mu.get('highpass_hz',0),0,400,'music.highpass_hz')
        if hp:
            shape+=f',highpass=f={hp}:poles=2,highpass=f={hp}:poles=2'
        dip=number(mu.get('voice_dip_db',0),-12,0,'music.voice_dip_db')
        if dip:
            shape+=f',equalizer=f=2500:t=q:w=0.8:g={dip}'


        dth=number(mu.get('duck_threshold',0.025),0.001,1,'music.duck_threshold')
        dra=number(mu.get('duck_ratio',6),1,20,'music.duck_ratio')
        drl=number(mu.get('duck_release_ms',250),20,3000,'music.duck_release_ms')
        start=number(mu.get('start',0),0,3600,'music.start')


        for b in mu.get('boost',[]):
            if set(b)-{'from','to','gain_db','ramp'}:
                raise ValueError('music.boost entries: from, to, gain_db, ramp')
        terms=[]
        for b in mu.get('boost',[]):
            a0=number(b['from'],0,total,'boost.from'); a1=number(min(float(b['to']),total),a0,total,'boost.to')
            g=10**(number(b['gain_db'],-30,30,'boost.gain_db')/20); r=number(b.get('ramp',0.5),0.01,5,'boost.ramp')
            terms.append(f'({g-1:.4f})*clip((t-{a0})/{r},0,1)*clip(({a1}-t)/{r},0,1)')
        if terms:
            shape+=f",volume=eval=frame:volume='1+{'+'.join(terms)}'"
        graph=(f'[0:a]asplit=2[voice][key];[1:a]atrim=start={start},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo{shape},volume={gain}dB,'
               f'atrim=duration={total},afade=t=in:d=0.6,afade=t=out:st={max(0,total-1.2)}:d=1.2[m];'
               f'[m][key]sidechaincompress=threshold={dth}:ratio={dra}:attack=30:release={drl}[duck];'
               f'[voice][duck]amix=inputs=2:duration=first:normalize=0[out]')
        ff(['-i',voice,'-stream_loop','-1','-i',music,'-filter_complex',graph,'-map','[out]','-t',total,'-c:a','pcm_s24le',mix],work/'mix.log')
    else:
        shutil.copy2(voice,mix)
    if edit.get('sfx'):


        sfx=edit['sfx']; ins=['-i',mix]; parts=[]


        import numpy as np
        vpk=speech_level(voice)[1]; capped=[]
        for i,s in enumerate(sfx):
            raw=subprocess.run(['ffmpeg','-v','error','-ss',str(s.get('in',0)),*(['-t',str(s['dur'])] if s.get('dur') else []),
                                '-i',str(path(s['file'])),'-ac','1','-ar','48000','-f','f32le','-'],capture_output=True,check=True).stdout
            fpk=20*math.log10(float(np.abs(np.frombuffer(raw,np.float32)).max())+1e-9)
            g=float(s.get('gain_db',0))
            if fpk+g>vpk-1:
                capped.append((s.get('note',s['file']),round(g,1),round(vpk-1-fpk,1))); g=vpk-1-fpk
            s=dict(s,gain_db=round(g,2)); sfx[i]=s
        if capped:
            print(f'SFX capped under the voice peak ({vpk:.1f} dBFS): '+'; '.join(f'{n} {a}->{b} dB' for n,a,b in capped))
        for i,s in enumerate(sfx):
            ins+=['-i',path(s['file'])]
            trim=f"atrim=start={s.get('in',0)}"+(f":duration={s['dur']}" if s.get('dur') else '')
            d=int(round(float(s['at'])*1000))
            parts.append(f"[{i+1}:a]{trim},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                         f"volume={s.get('gain_db',0)}dB,adelay={d}|{d}[s{i}]")
        graph=';'.join(parts)+';'+'[0:a]'+''.join(f'[s{i}]' for i in range(len(sfx)))+f'amix=inputs={len(sfx)+1}:duration=first:normalize=0[out]'
        mixed=work/'mix_sfx.wav'
        ff([*ins,'-filter_complex',graph,'-map','[out]','-t',total,'-c:a','pcm_s24le',mixed],work/'sfx.log')
        mix=mixed
    I=edit.get('target_lufs',-14); TP=edit.get('true_peak_db',-1.5)
    measurement=loudness(mix,work/'loudness_measure.log')
    save(out/'loudness_before.json',measurement)
    norm=work/'normalized.wav'
    if all(math.isfinite(float(measurement[k])) for k in ['input_i','input_tp','input_lra','input_thresh','target_offset']):


        if edit.get('loudness')=='linear':
            gain_db=I-float(measurement['input_i']); lim_db=TP-0.3
            for attempt in range(4):
                limited=work/f'mix_limited{attempt}.wav'
                ff(['-i',mix,'-af',f'volume={gain_db:.2f}dB,aresample=192000,'
                    f'alimiter=limit={10**(lim_db/20):.4f}:attack=3:release=80:level=disabled:latency=1,aresample=48000',
                    '-c:a','pcm_s24le',limited],work/f'limiter{attempt}.log')
                m=loudness(limited,work/f'limiter{attempt}_measure.log')
                err=I-float(m['input_i']); over=float(m['input_tp'])-TP
                print(f'Linear loudness pass {attempt+1}: {float(m["input_i"]):.2f} LUFS, {float(m["input_tp"]):.2f} dBTP',flush=True)
                if abs(err)<=0.15 and over<=0:
                    break
                gain_db+=err
                if over>0:
                    lim_db-=over+0.05


            import numpy as np
            def mono(pth,pre_gain=0.0):
                r=subprocess.run(['ffmpeg','-v','error','-i',str(pth),'-af',f'volume={pre_gain:.2f}dB,pan=mono|c0=c0','-ar','48000','-f','f32le','-'],capture_output=True,check=True).stdout
                return np.frombuffer(r,np.float32).astype(np.float64)
            pre_x,post_x=mono(mix,gain_db),mono(limited); nwin=min(len(pre_x),len(post_x))//480
            a_=np.abs(pre_x[:nwin*480]).reshape(nwin,480).max(1); b_=np.abs(post_x[:nwin*480]).reshape(nwin,480).max(1)
            grd=20*np.log10((a_[a_>1e-3]+1e-9)/(b_[a_>1e-3]+1e-9))
            lim_stats={'limiter_gr_max_db':round(float(grd.max()),2),'limiter_gr_p99_db':round(float(np.percentile(grd,99)),2),
                       'limiter_active_pct':round(float(100*(grd>1).mean()),2)}
            print(f"Master limiter: max {lim_stats['limiter_gr_max_db']} dB, 99th {lim_stats['limiter_gr_p99_db']} dB, >1 dB in {lim_stats['limiter_active_pct']} % of the film"
                  + ('  WARNING: over-limited - control peaks earlier (voice.compress stages / voice.peak / sfx levels)' if lim_stats['limiter_gr_p99_db']>3 or lim_stats['limiter_gr_max_db']>6 else ''),flush=True)
            save(out/'loudness_linear.json',{'gain_db':round(gain_db,2),'limit_dbtp':round(lim_db,2),**lim_stats,**m})
            shutil.copy2(limited,norm)
        else:

            txt=ff(['-i',mix,'-af',f'loudnorm=I={I}:TP={TP}:LRA=11:print_format=json','-f','null','-'],work/'loudness_target_measure.log').stderr
            m=json.loads(re.findall(r'\{\s*"input_i".*?\}',txt,re.S)[-1])
            af=f"loudnorm=I={I}:TP={TP}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true:print_format=json"
            ff(['-i',mix,'-af',af,'-ar','48000','-c:a','pcm_s24le',norm],work/'normalize.log')
    else:
        shutil.copy2(mix,norm)
    encoder=edit.get('encoder','libx264')
    enc=['-c:v','libx264','-crf','18','-preset','slow'] if encoder=='libx264' else ['-c:v','h264_nvenc','-preset','p7','-tune','hq','-rc','vbr','-cq','18','-b:v','0']
    common=['-t',total,*enc,'-pix_fmt','yuv420p','-r',fps,'-fps_mode','cfr','-g',round(fps*2),'-c:a','aac','-b:a','320k','-ar','48000','-ac','2','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-color_range','tv','-map_metadata','-1','-movflags','+faststart']
    clean=out/'clean.mp4'
    tag='setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=limited'
    ovl=edit.get('overlay')

    def export(dest,chain,log):


        ins=['-i',base,'-i',norm]+(['-i',str(path(ovl['file']))] if ovl else [])
        if ovl:
            x=int(number(ovl.get('x',0),0,w,'overlay.x')); y=int(number(ovl.get('y',0),0,h,'overlay.y'))
            fc=f'[0:v][2:v]overlay={x}:{y}:eof_action=pass:format=auto[ovl];[ovl]{chain}[v]'
            maps=['-filter_complex',fc,'-map','[v]','-map','1:a:0']
        else:
            maps=['-vf',chain,'-map','0:v:0','-map','1:a:0']
        ff([*ins,*maps,*common,dest],log)


    jobs_out=[(clean,tag,work/'export_clean.log')]
    final=clean
    if edit.get('captions'):
        cap=path(edit['captions']); staged=work/('captions'+cap.suffix.lower()); shutil.copy2(cap,staged)
        if cap.suffix.lower() not in ['.ass','.srt']:
            raise ValueError('captions must be .ass or .srt')
        vf=f'subtitles=filename={filter_path(staged)}:fontsdir={filter_path(ROOT/"02_ASSETS/fonts")}'
        if cap.suffix.lower()=='.srt':
            vf+=":force_style='Fontname=Arial,Fontsize=20,Outline=1.2,MarginV=80'"
        final=out/'captioned.mp4'
        jobs_out.append((final,vf+','+tag,work/'export_captioned.log'))
        shutil.copy2(cap,out/cap.name)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(jobs_out)) as ex:
        list(ex.map(lambda j:export(*j),jobs_out))
    save(out/'render_manifest.json',{'duration_expected':total,'frames_expected':round(total*fps),'fps':fps,'width':w,'height':h,'final':str(final),'visual_review':'PENDING','audio_review':'PENDING','platform_preview':'PENDING','master':str(base),'note':'Technical pass is not creative approval.'})
    shutil.copy2(ROOT/'templates/REVIEW.md',out/'REVIEW.md')
    code=qc_file(final,total,out/'qc')
    print(out)
    return code

def qc_file(p, expected, out):
    out.mkdir(parents=True,exist_ok=False)
    meta=probe(p); v=video(meta)
    report={'file':str(p),'sha256':digest(p),'media':summary(meta),'errors':[],'warnings':[], 'visual_review':'PENDING','audio_review':'PENDING'}
    if v.get('codec_name')!='h264': report['errors'].append('Delivery codec is not H.264')
    if v.get('pix_fmt')!='yuv420p': report['errors'].append('Delivery pixel format is not yuv420p')
    if (v.get('width'),v.get('height')) not in [(1080,1920),(2160,3840),(3840,2160),(1920,1080)]: report['warnings'].append('Nonstandard delivery size (test or custom)')
    for k in ['color_primaries','color_transfer','color_space']:
        if v.get(k)!='bt709': report['errors'].append(f'{k} is not bt709')
    if expected is not None and abs(duration(meta)-expected)>.12: report['errors'].append('Duration differs from timeline by >120 ms')
    fps=float(Fraction(v.get('avg_frame_rate','0/1')))
    if expected is not None and v.get('nb_frames') and abs(int(v['nb_frames'])-round(expected*fps))>1: report['errors'].append('Frame count differs from timeline')
    try:
        result=ff(['-v','info','-xerror','-i',p,'-map','0:v:0','-map','0:a:0?','-vf','vfrdet,blackdetect=d=0.1:pix_th=0.08,freezedetect=n=-50dB:d=1','-f','null','-'],out/'decode.log')
        report['decode']='passed'
        report['candidates']=[l for l in result.stderr.splitlines() if any(x in l for x in ['black_start:', 'freeze_', 'VFR:'])]
        if any('black_start:' in l or 'freeze_' in l for l in report['candidates']): report['warnings'].append('Black/freeze candidates: review whether intentional')
        match=re.search(r'VFR:([0-9.]+)',result.stderr)
        if match and float(match[1])>.01: report['errors'].append('Variable frame timing detected')
    except RuntimeError as e:
        report['errors'].append(str(e)); report['decode']='FAILED'
    aud=next((s for s in meta['streams'] if s['codec_type']=='audio'),None)
    if aud:
        report['loudness']=loudness(p,out/'loudness.log')
        tp=float(report['loudness']['input_tp']); il=float(report['loudness']['input_i'])
        if tp > -1: report['errors'].append(f'True peak {tp} dBTP exceeds -1 dBTP')
        if not math.isfinite(il): report['warnings'].append('Silent soundtrack')
        elif not -18<=il<=-10: report['warnings'].append('Loudness outside house review range -18..-10 LUFS')
        if aud.get('sample_rate')!='48000': report['errors'].append('Audio not 48 kHz')
        if abs(float(aud.get('duration',duration(meta)))-float(v.get('duration',duration(meta))))>.12: report['errors'].append('Audio/video duration difference >120 ms')
    else: report['errors'].append('No audio stream')
    report['technical_status']='FAIL' if report['errors'] else 'PASS_WITH_REVIEW_REQUIRED'
    save(out/'report.json',report)
    return 1 if report['errors'] else 0

def qc(args):
    out=ROOT/'01_PROJECTS'/'_qc'/stamp()
    status=qc_file(path(args.file),args.expected_duration,out)
    print(out)
    return status

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('doctor'); p.set_defaults(func=doctor)
    p=sub.add_parser('init'); p.add_argument('name'); p.add_argument('files',nargs='*'); p.set_defaults(func=init)
    p=sub.add_parser('inspect'); p.add_argument('file'); p.add_argument('--quick',action='store_true'); p.add_argument('--color',choices=['auto','samsung_log'],default='auto'); p.set_defaults(func=inspect)
    p=sub.add_parser('scenes'); p.add_argument('file'); p.add_argument('--threshold',type=float,default=27); p.set_defaults(func=scenes)
    p=sub.add_parser('transcribe'); p.add_argument('file'); p.add_argument('--language',default='pl'); p.add_argument('--model'); p.add_argument('--device',choices=['cpu','cuda'],default='cpu'); p.set_defaults(func=transcribe)
    p=sub.add_parser('captions'); p.add_argument('transcript'); p.set_defaults(func=lambda a: captions_files(read(path(a.transcript))['segments'],path(a.transcript).parent))
    p=sub.add_parser('download-model'); p.set_defaults(func=download_model)
    p=sub.add_parser('render'); p.add_argument('edit'); p.set_defaults(func=render)
    p=sub.add_parser('qc'); p.add_argument('file'); p.add_argument('--expected-duration',type=float); p.set_defaults(func=qc)
    args=parser.parse_args()
    return args.func(args) or 0

def download_model(_):
    from faster_whisper.utils import download_model as dl
    print(dl('turbo',output_dir=str(ROOT/'models/whisper-turbo')))

if __name__=='__main__':
    try:
        sys.exit(main())
    except (ValueError,RuntimeError,FileNotFoundError,StopIteration) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        sys.exit(1)
