"""Real CPU-only render with synthetic footage; never reads user media."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio as s

folder = s.ROOT / '01_PROJECTS' / '_selftest' / s.stamp()
folder.mkdir(parents=True)
source = folder / 'synthetic.mp4'
s.ff(['-f', 'lavfi', '-i', 'testsrc2=size=360x640:rate=30', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-c:a', 'aac', source])
plan = folder / 'edit.json'
s.save(plan, {'name': 'synthetic-smoke', 'width': 360, 'height': 640, 'fps': 30, 'encoder': 'libx264', 'clips': [{'file': str(source), 'in': 0, 'out': 1, 'color': 'sdr709'}, {'file': str(source), 'in': 1, 'out': 2, 'color': 'sdr709'}]})
result = s.run([sys.executable, s.ROOT / 'scripts/studio.py', 'render', plan])
output = Path(result.stdout.strip().splitlines()[-1])
report = s.read(output / 'qc/report.json')
assert not report['errors'], report['errors']
assert abs(report['media']['duration'] - 2) < .12, report['media']
print('PASS: two-clip CPU render, decode, duration, colour and audio QC')
