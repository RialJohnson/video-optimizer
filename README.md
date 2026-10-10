# Video Optimizer

Drop folders, zip files, or individual videos onto the window. They are converted one at a time into `originalName_optimized`. Originals are not changed.

- Output is **x265 HEVC** in an **`.mp4`** container (Apple-friendly `hvc1` tag).
- Encoding uses a quality-based CRF (default 18), not a bitrate ceiling, so bitrate is not deliberately capped downward.
- Resolution is capped at **1080p**. Sources already at or below 1080p keep their resolution; larger sources are scaled down with aspect ratio preserved.
- Any video container ffmpeg can read is accepted as input when you drop or choose a file. Inside folders and zips, common video extensions are converted; every other file is copied.
- When **Rename from title/year metadata** is checked (the default), a file with both a title and a release year becomes `Movie Title [releaseYear].mp4`. If either tag is missing, the original base name is kept (with a `.mp4` extension).

The **Inventory CSV** tab scans a folder (and subfolders) with ffprobe and writes `FolderName_inventory.csv` beside it. Columns: path, format, resolution, codec, bitrate, and needs_conversion (`Yes` when the file is not already an HEVC `.mp4` at or below 1080p). Inventory scans run on their own thread, so they do not interrupt an active conversion.

The conversion uses ffmpeg. It must be on your PATH, or the `ffmpeg` program must sit in the same folder as Video Optimizer. On Windows that file is named `ffmpeg.exe`. `ffprobe` is required for the inventory tab and is used when available for metadata renaming.

## Build

Use 64-bit Python 3.9 or newer.

Close Video Optimizer before building again. If it is still open, the build cannot replace the program.

### Windows

Install [Python](https://www.python.org/downloads/windows/) and [ffmpeg](https://ffmpeg.org/download.html). ffmpeg must be on your PATH when you run the program, or `ffmpeg.exe` must sit next to `VideoOptimizer.exe`.

From this folder:

```powershell
py -3.9 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe build.py
```

The program is written to `dist\VideoOptimizer.exe`.

### Linux

Install Python, Tk, and ffmpeg. On Debian or Ubuntu:

```bash
sudo apt install python3 python3-venv python3-tk python3-pip ffmpeg
```

From this folder:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python build.py
```

The program is written to `dist/VideoOptimizer`. Run it with `./dist/VideoOptimizer`.

## Run from source

Windows:

```powershell
.\.venv\Scripts\python.exe video_optimizer.py
```

Linux:

```bash
.venv/bin/python video_optimizer.py
```
