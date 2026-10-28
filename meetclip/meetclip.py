#!/usr/bin/env python3
"""Meeting transcript from an OBS recording: short clips around markers, or the whole recording.

A marker is the OBS hotkey "Add Chapter Marker" (Hybrid MP4 only).
Markers are read from the OBS log, so the script works while recording is still running.
For a finished file there is a fallback: chapters stored in the file itself.

For each marker it cuts audio [marker - before, marker + after], transcribes it
locally with Parakeet and fixes known terms with glossary.tsv.
If the recording has 3+ audio tracks (OBS: 1 = all, 2 = microphone, 3 = meeting app),
tracks 2 and 3 are transcribed separately and labeled "Me" / "Meet".
If the screen at the marker is not black, a .jpg frame is saved next to the text.

    python3 meetclip.py              # new markers of the latest recording
    python3 meetclip.py --list       # only show markers
    python3 meetclip.py --full       # transcribe the whole recording
    python3 meetclip.py FILE --before 300 --after 60

Folders: MEETCLIP_REC_DIR (default ~/Movies/Meet recordings),
MEETCLIP_OUT_DIR (default <recordings folder>/transcripts).
"""
import os
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REC_DIR = Path(os.environ.get("MEETCLIP_REC_DIR") or Path.home() / "Movies" / "Meet recordings").expanduser()
OBS_LOGS = Path.home() / "Library" / "Application Support" / "obs-studio" / "logs"
OUT_ROOT = Path(os.environ.get("MEETCLIP_OUT_DIR") or REC_DIR / "transcripts").expanduser()
MODEL = "mlx-community/parakeet-tdt-0.6b-v3"

WRITING_RE = re.compile(r"Writing Hybrid MP4/MOV file '(.+)'")
CHAPTER_RE = re.compile(r'Adding chapter "(.*)" at (\d+):(\d\d):(\d\d)\.(\d{3})')


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def latest_recording() -> Path:
    files = [p for p in REC_DIR.glob("*") if p.suffix.lower() in (".mp4", ".mov", ".mkv")]
    if not files:
        sys.exit(f"No recordings in {REC_DIR}")
    return max(files, key=lambda p: p.stat().st_mtime)


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", "-show_chapters", str(path)])
    return json.loads(out.stdout)


def markers_from_log(path: Path) -> list:
    """Markers from OBS logs: "Adding chapter" lines after "Writing ... 'path'"."""
    found = None
    for log in sorted(OBS_LOGS.glob("*.txt")):
        cur = None
        for line in log.read_text(errors="replace").splitlines():
            m = WRITING_RE.search(line)
            if m:
                cur = [] if Path(m.group(1)) == path else None
                if cur is not None:
                    found = cur
                continue
            if "Recording Stop" in line:
                cur = None
                continue
            m = CHAPTER_RE.search(line)
            if m and cur is not None:
                h, mi, s, ms = (int(x) for x in m.groups()[1:])
                cur.append((h * 3600 + mi * 60 + s + ms / 1000, m.group(1)))
    return found or []


def markers_from_file(info: dict) -> list:
    return [(float(c["start_time"]), c.get("tags", {}).get("title", ""))
            for c in info.get("chapters", [])]


def windows(points, before, after, duration):
    """Windows around markers; overlapping windows are merged, all markers are kept."""
    out = []
    for t, name in sorted(points):
        a, b = max(0.0, t - before), t + after
        if duration:
            b = min(b, duration)
        if out and a <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], b)
            out[-1]["marks"].append((t, name))
        else:
            out.append({"start": a, "end": b, "marks": [(t, name)]})
    return out


def tracks(info: dict) -> list:
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if len(audio) >= 3:
        return [("Me", "0:a:1"), ("Meet", "0:a:2")]
    if not audio:
        sys.exit("No audio in the recording")
    return [("", "0:a:0")]


def load_glossary():
    rules = []
    for line in (HERE / "glossary.tsv").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        pat, repl = line.split("\t")[:2]
        rules.append((re.compile(pat, re.IGNORECASE), repl))
    return rules


def fix(text, rules):
    for pat, repl in rules:
        text = pat.sub(repl, text)
    return text


def transcribe(src: Path, stream: str, start: float, end: float, tmp: Path) -> list:
    wav = tmp / f"clip-{stream.replace(':', '_')}-{int(start)}.wav"
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
         "-i", str(src), "-map", stream, "-ac", "1", "-ar", "16000", str(wav)])
    run(["uvx", "parakeet-mlx", str(wav), "--model", MODEL,
         "--output-format", "json", "--output-dir", str(tmp)], cwd=tmp)
    data = json.loads(wav.with_suffix(".json").read_text())
    return [(start + s["start"], s["text"].strip()) for s in data["sentences"] if s["text"].strip()]


def frame(src: Path, t: float, dest: Path) -> bool:
    """Frame at the marker; a black frame (screen hidden) is not saved."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-y", "-ss", f"{t:.3f}", "-i", str(src),
                        "-frames:v", "1", "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                        "-q:v", "3", str(dest)], capture_output=True, text=True)
    m = re.search(r"YAVG=([\d.]+)", r.stderr)
    if r.returncode != 0 or not m or float(m.group(1)) < 20:
        dest.unlink(missing_ok=True)
        return False
    return True


def clock(t0, sec):
    if t0 is None:
        return fmt_offset(sec)
    return (t0 + dt.timedelta(seconds=sec)).strftime("%H:%M:%S")


def fmt_offset(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", type=Path, help="OBS recording (default: the latest)")
    ap.add_argument("--before", type=float, default=180, help="seconds before marker (180)")
    ap.add_argument("--after", type=float, default=30, help="seconds after marker (30)")
    ap.add_argument("--full", action="store_true", help="transcribe the whole recording")
    ap.add_argument("--list", action="store_true", help="only show markers")
    ap.add_argument("--force", action="store_true", help="redo clips that already exist")
    ap.add_argument("--out", type=Path, help="output folder (default: MEETCLIP_OUT_DIR/DATE)")
    args = ap.parse_args()

    src = (args.file or latest_recording()).resolve()
    info = probe(src)
    duration = float(info["format"].get("duration") or 0) or None
    created = info["format"].get("tags", {}).get("creation_time")
    t0 = dt.datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone() if created else None

    # OBS adds a "Start" chapter at 0:00 by itself, it is not a marker
    points = [p for p in (markers_from_log(src) or markers_from_file(info)) if p[0] >= 1]
    print(f"{src.name}: {fmt_offset(duration or 0)}, markers: {len(points)}")
    for i, (t, name) in enumerate(points, 1):
        print(f"  m{i:02d}  {fmt_offset(t)}  ({clock(t0, t)})  {name}")
    if args.list:
        return
    if args.full:
        wins = [{"start": 0.0, "end": duration or 10 ** 6, "marks": []}]
    else:
        if not points:
            sys.exit("No markers. Press the OBS marker hotkey or run with --full.")
        wins = windows(points, args.before, args.after, duration)

    day = (t0.date() if t0 else dt.date.today()).isoformat()
    out_dir = args.out or OUT_ROOT / day
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem.replace(" ", "_")
    rules = load_glossary()
    trk = tracks(info)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for n, w in enumerate(wins, 1):
            tag = "full" if args.full else f"m{n:02d}-{clock(t0, w['marks'][0][0]).replace(':', '')[:4]}"
            dest = out_dir / f"{stem}-{tag}.md"
            if dest.exists() and not args.force:
                print(f"  already exists: {dest.name}")
                continue
            print(f"  transcribing {fmt_offset(w['start'])}-{fmt_offset(w['end'])} ...", flush=True)
            lines = []
            for who, stream in trk:
                for t, text in transcribe(src, stream, w["start"], w["end"], tmp):
                    lines.append((t, who, text))
            lines.sort()

            head = [f"# {src.name}, {tag}", "",
                    f"Clip {clock(t0, w['start'])}-{clock(t0, w['end'])} "
                    f"(in recording {fmt_offset(w['start'])}-{fmt_offset(w['end'])}). "
                    f"Parakeet, terms fixed with glossary.tsv; raw text in .raw.txt.", ""]
            body, raw, shots = [], [], []
            marks = list(w["marks"])
            for t, who, text in lines:
                while marks and marks[0][0] <= t:
                    mt, name = marks.pop(0)
                    body.append(f"\n**>>> marker {clock(t0, mt)} {name}**\n")
                label = f"{who}: " if who else ""
                body.append(f"[{clock(t0, t)}] {label}{fix(text, rules)}")
                raw.append(f"[{clock(t0, t)}] {label}{text}")
            for mt, name in marks:
                body.append(f"\n**>>> marker {clock(t0, mt)} {name}**\n")
            if any(s["codec_type"] == "video" for s in info["streams"]):
                for mt, _ in w["marks"]:
                    jpg = dest.with_name(f"{dest.stem}-{clock(t0, mt).replace(':', '')}.jpg")
                    if frame(src, mt, jpg):
                        shots.append(f"![screen {clock(t0, mt)}]({jpg.name})")
            dest.write_text("\n".join(head + body + ([""] + shots if shots else [])) + "\n")
            dest.with_suffix(".raw.txt").write_text("\n".join(raw) + "\n")
            print(f"  done: {dest}")


if __name__ == "__main__":
    main()
