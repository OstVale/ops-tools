# meetclip

Turns an OBS meeting recording into text. You press a hotkey during the call when something
important is said, and later meetclip transcribes only a few minutes around each marker.
Or the whole recording with `--full`.

Transcription is local, with [Parakeet](https://github.com/senstella/parakeet-mlx) on Apple Silicon.
No audio leaves the machine.

```bash
python3 meetclip.py            # new markers of the latest recording
python3 meetclip.py --list     # show markers
python3 meetclip.py --full     # the whole recording
python3 meetclip.py FILE --before 300 --after 60
```

## How it works

- Markers come from the OBS log, so it works while the recording is still running.
  Fallback: chapter markers inside the finished file.
- Default window: 3 minutes before a marker, 30 seconds after (`--before`, `--after`).
  Close markers are merged into one clip.
- With 3+ audio tracks (1 all, 2 microphone, 3 meeting app) it transcribes tracks 2 and 3
  separately and labels lines `Me` / `Meet`.
- Known wrong words are fixed with `glossary.tsv` (regex, tab, replacement). The raw text is kept in `.raw.txt`.
- At each marker it saves a screen frame, unless the frame is black.
- Run it again and it only does new markers (`--force` to redo).

## Setup

Needs macOS on Apple Silicon, `ffmpeg`, and `uv` (Parakeet runs with `uvx parakeet-mlx`).

Folders, both optional:
- `MEETCLIP_REC_DIR` - where OBS saves recordings (default `~/Movies/Meet recordings`)
- `MEETCLIP_OUT_DIR` - where text goes (default `<recordings>/transcripts/DATE/`)

OBS settings:
- Output: Simple mode, format Hybrid MP4 (needed for chapter markers).
- Hotkeys: set "Add Chapter Marker" and start/stop recording.
- For speaker labels: 3 audio tracks - 1 everything, 2 microphone, 3 the browser or meeting app window.
