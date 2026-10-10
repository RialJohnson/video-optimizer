"""Convert video folders to 1080p x265 HEVC MP4 without touching the originals."""

from __future__ import annotations

import csv
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from collections import deque

from tkinter import filedialog, messagebox, ttk
import tkinter as tk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # Drag-and-drop is optional so the window still opens.
    DND_FILES = None
    TkinterDnD = None

# Hide the console window ffmpeg would open on Windows. Linux has no equivalent flag.
_SUBPROCESS_KWARGS = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

# Common containers ffmpeg can read. Unknown extensions are still accepted when dropped as files.
_VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".asf",
    ".avi",
    ".divx",
    ".dvr-ms",
    ".f4v",
    ".flv",
    ".m2ts",
    ".m2v",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".mxf",
    ".nut",
    ".ogv",
    ".rm",
    ".rmvb",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
    ".wtv",
    ".xvid",
    ".y4m",
}

# Quality-based encode (not a bitrate cap). CRF 18 is visually high quality for x265.
_X265_CRF = "18"
_X265_PRESET = "medium"
# Cap at 1080p without upscaling, then force even dimensions for x265.
# Avoid force_divisible_by so older ffmpeg builds still work.
_SCALE_FILTER = (
    "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,"
    "scale=trunc(iw/2)*2:trunc(ih/2)*2"
)


class Cancelled(Exception):
    pass


def _app_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _open_folder(path):
    if sys.platform == "win32":
        os.startfile(path)
        return
    subprocess.Popen(["xdg-open", path], **_SUBPROCESS_KWARGS)


def destination_for(source: str) -> str:
    source = os.path.abspath(source)
    if _is_zip_file(source) or os.path.isfile(source):
        source = os.path.splitext(source)[0]
    return source.rstrip("\\/") + "_optimized"


def _is_zip_file(path: str) -> bool:
    return os.path.isfile(path) and path.lower().endswith(".zip")


def _is_video_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in _VIDEO_EXTENSIONS


def _is_source(path: str) -> bool:
    # Folders and zips are always sources. Individual files are accepted so the
    # user can drop any container ffmpeg can read, even with an uncommon extension.
    return os.path.isdir(path) or _is_zip_file(path) or os.path.isfile(path)


def _directory_size(path):
    total = 0
    if not os.path.isdir(path):
        return 0
    for directory, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in dirnames if name != "__MACOSX"]
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                continue
    return total


def _uncompressed_size(path):
    """Size of a folder, a single file, or a zip's contents after extraction."""
    if _is_zip_file(path):
        return _zip_uncompressed_size(path)
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    if not os.path.isdir(path):
        return 0
    total = 0
    for directory, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in dirnames if name != "__MACOSX"]
        for name in filenames:
            full = os.path.join(directory, name)
            if name.lower().endswith(".zip"):
                total += _zip_uncompressed_size(full)
            else:
                try:
                    total += os.path.getsize(full)
                except OSError:
                    continue
    return total


def _zip_uncompressed_size(path, depth=0):
    if depth > 8:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    with archive:
        for info in archive.infolist():
            parts = _safe_zip_parts(info.filename)
            if not parts or "__MACOSX" in parts or info.is_dir():
                continue
            if parts[-1].lower().endswith(".zip"):
                nested = None
                try:
                    nested = tempfile.NamedTemporaryFile(prefix="video-optimizer-size-", delete=False)
                    nested.close()
                    with archive.open(info, "r") as src, open(nested.name, "wb") as out:
                        shutil.copyfileobj(src, out)
                    total += _zip_uncompressed_size(nested.name, depth + 1)
                except (OSError, RuntimeError, zipfile.BadZipFile):
                    total += info.file_size
                finally:
                    if nested is not None:
                        try:
                            os.remove(nested.name)
                        except OSError:
                            pass
            else:
                total += info.file_size
    return total


def _format_bytes(size):
    size = max(0, int(size))
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f KB" % (size / 1024)
    if size < 1024 * 1024 * 1024:
        return "%.1f MB" % (size / (1024 * 1024))
    return "%.2f GB" % (size / (1024 * 1024 * 1024))


def _size_report(source_bytes, output_bytes):
    if source_bytes <= 0:
        return "Output size %s" % _format_bytes(output_bytes), None
    delta = source_bytes - output_bytes
    percent = abs(delta) * 100.0 / source_bytes
    if delta > 0:
        change = "Reduced by %s (%.0f%%)" % (_format_bytes(delta), percent)
        direction = "down"
    elif delta < 0:
        change = "Larger by %s (%.0f%%)" % (_format_bytes(-delta), percent)
        direction = "up"
    else:
        change = "Unchanged at %s" % _format_bytes(output_bytes)
        direction = None
    if direction is None:
        return change, None
    return "%s: %s uncompressed -> %s" % (change, _format_bytes(source_bytes), _format_bytes(output_bytes)), direction


def _program_beside(stem):
    """Find stem next to this program, then on PATH. Windows also checks stem.exe."""
    names = [stem + ".exe", stem] if sys.platform == "win32" else [stem]
    directories = []
    if getattr(sys, "frozen", False):
        directories.append(os.path.dirname(sys.executable))
    directories.append(os.path.dirname(os.path.abspath(__file__)))
    for directory in directories:
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return shutil.which(stem)


def find_ffmpeg() -> str | None:
    return _program_beside("ffmpeg")


def find_ffprobe() -> str | None:
    return _program_beside("ffprobe")


def inventory_csv_path(folder: str) -> str:
    folder = os.path.abspath(folder).rstrip("\\/")
    return folder + "_inventory.csv"


def scan_folder_inventory(folder, ffprobe, on_progress, cancel_event):
    """Probe every video under folder and write a CSV beside it.

    Columns: path, format, resolution, codec, bitrate, needs_conversion.
    needs_conversion is Yes when the file is not already an HEVC .mp4 at or
    below 1080p — the same target Video Optimizer converts toward.
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise NotADirectoryError("'%s' is not a folder." % folder)

    on_progress(0, 0, "Scanning…")
    videos = _list_videos_under(folder, cancel_event)
    total = len(videos)
    rows = []
    failed = 0
    for index, path in enumerate(videos, start=1):
        if cancel_event.is_set():
            raise Cancelled()
        relative = os.path.relpath(path, folder)
        on_progress(index - 1, total, relative)
        try:
            info = _probe_video_info(path, ffprobe)
        except Exception as exc:
            failed += 1
            detail = str(exc).strip() or exc.__class__.__name__
            rows.append({
                "path": relative,
                "format": os.path.splitext(path)[1].lstrip(".").lower() or "",
                "resolution": "",
                "codec": "",
                "bitrate": "",
                "needs_conversion": "Error",
                "error": detail,
            })
            on_progress(index, total, "Failed: %s — %s" % (relative, detail))
            continue
        needs = _needs_conversion(info)
        rows.append({
            "path": relative,
            "format": info["format"],
            "resolution": info["resolution"],
            "codec": info["codec"],
            "bitrate": info["bitrate"],
            "needs_conversion": "Yes" if needs else "No",
        })
        on_progress(index, total, "Probed %s" % relative)

    csv_path = inventory_csv_path(folder)
    fieldnames = ["path", "format", "resolution", "codec", "bitrate", "needs_conversion"]
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    on_progress(total, total, "Done.")
    needs_count = sum(1 for row in rows if row.get("needs_conversion") == "Yes")
    return {
        "folder": folder,
        "csv_path": csv_path,
        "total": total,
        "failed": failed,
        "needs_conversion": needs_count,
    }


def _list_videos_under(folder, cancel_event):
    found = []
    for directory, dirnames, filenames in os.walk(folder):
        if cancel_event.is_set():
            raise Cancelled()
        dirnames[:] = sorted(
            name for name in dirnames if name != "__MACOSX" and not name.endswith("_optimized")
        )
        for name in sorted(filenames, key=str.lower):
            if _is_video_name(name):
                found.append(os.path.join(directory, name))
    return found


def _kbps_from_bps(value):
    try:
        bps = float(value)
    except (TypeError, ValueError):
        return None
    if bps <= 0:
        return None
    return int(round(bps / 1000.0))


def _probe_video_info(path, ffprobe):
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,bit_rate",
        "-show_entries",
        "format=format_name,bit_rate",
        "-of",
        "json",
        path,
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_SUBPROCESS_KWARGS,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "ffprobe exited with code %s." % completed.returncode)
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Could not parse ffprobe output.") from exc

    stream = {}
    for candidate in payload.get("streams") or []:
        stream = candidate
        break
    fmt = payload.get("format") or {}
    extension = os.path.splitext(path)[1].lstrip(".").lower()
    format_name = (fmt.get("format_name") or "").split(",")[0].strip().lower()
    width = stream.get("width")
    height = stream.get("height")
    try:
        width_i = int(width) if width not in (None, "") else None
        height_i = int(height) if height not in (None, "") else None
    except (TypeError, ValueError):
        width_i = None
        height_i = None
    if width_i and height_i:
        resolution = "%sx%s" % (width_i, height_i)
    else:
        resolution = ""
    bitrate_bps = _kbps_from_bps(stream.get("bit_rate"))
    if not bitrate_bps:
        bitrate_bps = _kbps_from_bps(fmt.get("bit_rate"))
    return {
        "format": extension or format_name or "",
        "format_name": format_name,
        "resolution": resolution,
        "width": width_i,
        "height": height_i,
        "codec": (stream.get("codec_name") or "").lower(),
        "bitrate": ("%s kbps" % bitrate_bps) if bitrate_bps else "",
        "bitrate_kbps": bitrate_bps,
    }


def _needs_conversion(info):
    codec = (info.get("codec") or "").lower()
    if codec not in ("hevc", "h265"):
        return True
    if (info.get("format") or "").lower() != "mp4":
        return True
    width = info.get("width") or 0
    height = info.get("height") or 0
    if width > 1920 or height > 1080:
        return True
    return False


def convert_folder(
    source,
    ffmpeg,
    on_progress,
    cancel_event,
    rename_from_metadata=True,
):
    """Convert video and copy every other file.

    Subfolders are included. Zip files are extracted into the output folder
    and converted the same way. A single video file may also be a source.
    The source is not modified.

    Default encode is x265 HEVC in an .mp4 container. Resolution is capped at
    1080p without upscaling. Encoding uses CRF (quality-based), not a bitrate
    ceiling. When rename_from_metadata is true and both title and year tags are
    present, the output is named "Movie Title [releaseYear].mp4".
    """
    source = os.path.abspath(source)
    if not _is_source(source):
        raise NotADirectoryError("'%s' is not a folder, zip, or video file." % source)

    destination = destination_for(source)
    if os.path.isdir(source) and _is_inside(destination, source):
        raise RuntimeError("The output folder would be inside the folder being converted.")

    os.makedirs(destination, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix="video-optimizer-")
    converted = 0
    copied = 0
    failed = 0
    try:
        on_progress(0, 0, "Scanning…")
        jobs = []
        if _is_zip_file(source):
            _collect_zip(source, destination, jobs, temp_dir, cancel_event)
        elif os.path.isfile(source):
            jobs.append({
                "kind": "video",
                "label": os.path.basename(source),
                "path": source,
                "dest": os.path.join(destination, os.path.splitext(os.path.basename(source))[0] + ".mp4"),
            })
        else:
            _collect_dir(source, destination, jobs, temp_dir, cancel_event)
        _label_jobs(destination, jobs)
        used_destinations = {os.path.normcase(job["dest"]) for job in jobs if job.get("dest")}
        ffprobe = _ffprobe_beside(ffmpeg)

        total = len(jobs)
        for index, job in enumerate(jobs, start=1):
            if cancel_event.is_set():
                raise Cancelled()

            label = job["label"]
            on_progress(index - 1, total, label)
            try:
                if job["kind"] == "error":
                    raise RuntimeError(job["error"])
                if job["kind"] == "video":
                    _convert_video_job(
                        job,
                        job["path"],
                        ffmpeg,
                        ffprobe,
                        used_destinations,
                        destination,
                        cancel_event,
                        rename_from_metadata=rename_from_metadata,
                    )
                    label = job["label"]
                    converted += 1
                    on_progress(index, total, "Converted %s" % label)
                elif job["kind"] == "copy":
                    os.makedirs(os.path.dirname(job["dest"]), exist_ok=True)
                    shutil.copy2(job["path"], job["dest"])
                    job["written"] = True
                    copied += 1
                    on_progress(index, total, "Copied %s" % label)
                elif job["kind"] == "zip-video":
                    extracted = _extract_zip_member(job["zip_path"], job["member"], temp_dir)
                    try:
                        _convert_video_job(
                            job,
                            extracted,
                            ffmpeg,
                            ffprobe,
                            used_destinations,
                            destination,
                            cancel_event,
                            rename_from_metadata=rename_from_metadata,
                        )
                        label = job["label"]
                    finally:
                        if os.path.isfile(extracted):
                            os.remove(extracted)
                    converted += 1
                    on_progress(index, total, "Converted %s" % label)
                elif job["kind"] == "zip-copy":
                    os.makedirs(os.path.dirname(job["dest"]), exist_ok=True)
                    _extract_zip_member(job["zip_path"], job["member"], job["dest"])
                    job["written"] = True
                    copied += 1
                    on_progress(index, total, "Copied %s" % label)
                else:
                    raise RuntimeError("Unknown job %s" % job["kind"])
            except Cancelled:
                raise
            except Exception as exc:
                failed += 1
                detail = str(exc).strip() or exc.__class__.__name__
                on_progress(index, total, "Failed: %s — %s" % (label, detail))
                if os.path.isfile(job.get("dest", "")):
                    try:
                        os.remove(job["dest"])
                    except OSError:
                        pass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    on_progress(total, total, "Done.")
    source_bytes = _uncompressed_size(source)
    output_bytes = _directory_size(destination)
    size_report, size_direction = _size_report(source_bytes, output_bytes)
    return {
        "converted": converted,
        "copied": copied,
        "failed": failed,
        "destination": destination,
        "total": total,
        "source_bytes": source_bytes,
        "output_bytes": output_bytes,
        "size_report": size_report,
        "size_direction": size_direction,
    }


def _collect_dir(directory, dest_root, jobs, temp_dir, cancel_event):
    if cancel_event.is_set():
        raise Cancelled()

    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        jobs.append({"kind": "error", "label": directory, "error": str(exc), "dest": ""})
        return

    entries.sort(key=lambda entry: entry.name.lower())
    for entry in entries:
        if cancel_event.is_set():
            raise Cancelled()
        if entry.name == "__MACOSX":
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError as exc:
            jobs.append({"kind": "error", "label": entry.name, "error": str(exc), "dest": ""})
            continue
        if is_dir:
            _collect_dir(
                entry.path,
                os.path.join(dest_root, entry.name),
                jobs,
                temp_dir,
                cancel_event,
            )
        elif is_file and entry.name.lower().endswith(".zip"):
            stem = os.path.splitext(entry.name)[0]
            _collect_zip(
                entry.path,
                os.path.join(dest_root, stem),
                jobs,
                temp_dir,
                cancel_event,
            )
        elif is_file and _is_video_name(entry.name):
            stem = os.path.splitext(entry.name)[0]
            jobs.append({
                "kind": "video",
                "label": entry.name,
                "path": entry.path,
                "dest": os.path.join(dest_root, stem + ".mp4"),
            })
        elif is_file:
            jobs.append({
                "kind": "copy",
                "label": entry.name,
                "path": entry.path,
                "dest": os.path.join(dest_root, entry.name),
            })


def _collect_zip(zip_path, dest_root, jobs, temp_dir, cancel_event, depth=0):
    if cancel_event.is_set():
        raise Cancelled()
    if depth > 8:
        jobs.append({
            "kind": "error",
            "label": os.path.basename(zip_path),
            "error": "Zip is nested too deeply.",
            "dest": "",
        })
        return

    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        jobs.append({
            "kind": "error",
            "label": os.path.basename(zip_path),
            "error": str(exc),
            "dest": "",
        })
        return

    with archive:
        members = list(archive.infolist())
        members.sort(key=lambda info: info.filename.lower())
        for info in members:
            if cancel_event.is_set():
                raise Cancelled()
            parts = _safe_zip_parts(info.filename)
            if parts is None:
                jobs.append({
                    "kind": "error",
                    "label": info.filename,
                    "error": "Skipped an unsafe path in the zip.",
                    "dest": "",
                })
                continue
            if not parts or "__MACOSX" in parts:
                continue
            if info.is_dir():
                continue
            dest = os.path.join(dest_root, *parts)
            name = parts[-1]
            if name.lower().endswith(".zip"):
                nested_dest = os.path.join(dest_root, *parts[:-1], os.path.splitext(name)[0])
                nested_path = _extract_zip_member(archive, info.filename, temp_dir)
                _collect_zip(
                    nested_path,
                    nested_dest,
                    jobs,
                    temp_dir,
                    cancel_event,
                    depth + 1,
                )
            elif _is_video_name(name):
                stem = os.path.splitext(name)[0]
                jobs.append({
                    "kind": "zip-video",
                    "label": name,
                    "zip_path": zip_path,
                    "member": info.filename,
                    "dest": os.path.join(os.path.dirname(dest), stem + ".mp4"),
                })
            else:
                jobs.append({
                    "kind": "zip-copy",
                    "label": name,
                    "zip_path": zip_path,
                    "member": info.filename,
                    "dest": dest,
                })


def _label_jobs(dest_root, jobs):
    for job in jobs:
        dest = job.get("dest")
        if dest and os.path.isabs(dest):
            try:
                job["label"] = os.path.relpath(dest, dest_root)
            except ValueError:
                pass


def _safe_zip_parts(name: str):
    name = name.replace("\\", "/").strip()
    if not name or name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        return None
    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return parts


def _extract_zip_member(archive_or_path, member, target):
    """Write one zip member to target. target may be a directory or a file path."""
    if os.path.isdir(target):
        filename = os.path.basename(member.replace("\\", "/")) or "member"
        handle, target = tempfile.mkstemp(prefix="member-", suffix="-" + filename, dir=target)
        os.close(handle)

    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    opened_here = False
    archive = archive_or_path
    if isinstance(archive_or_path, str):
        archive = zipfile.ZipFile(archive_or_path)
        opened_here = True
    try:
        with archive.open(member, "r") as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
    finally:
        if opened_here:
            archive.close()
    return target


def _is_inside(path, parent):
    path = os.path.abspath(path)
    parent = os.path.abspath(parent)
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:
        return False


def _ffprobe_beside(ffmpeg):
    folder = os.path.dirname(os.path.abspath(ffmpeg))
    names = ["ffprobe.exe", "ffprobe"] if sys.platform == "win32" else ["ffprobe"]
    for name in names:
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("ffprobe")


def _set_job_label(job, dest, output_root):
    try:
        job["label"] = os.path.relpath(dest, output_root)
    except ValueError:
        job["label"] = os.path.basename(dest)


def _unique_dest(directory, filename, used_destinations, current):
    root, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    current_key = os.path.normcase(current)
    number = 2
    while True:
        key = os.path.normcase(candidate)
        if key == current_key or key not in used_destinations:
            return candidate
        candidate = os.path.join(directory, "%s (%d)%s" % (root, number, ext))
        number += 1


def _clean_filename(value):
    chars = []
    for char in str(value).replace("\x00", ""):
        if char in '<>:"/\\|?*' or ord(char) < 32:
            chars.append(" ")
        else:
            chars.append(char)
    title = " ".join("".join(chars).split()).strip(" .")
    return title


def _parse_year(value):
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(value))
    if not match:
        return None
    return match.group(1)


def _read_format_tags(path, ffprobe, ffmpeg):
    tags = {}
    if ffprobe:
        command = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags",
            "-of",
            "json",
            path,
        ]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **_SUBPROCESS_KWARGS,
                check=False,
            )
        except OSError:
            completed = None
        if completed is not None and completed.returncode == 0:
            try:
                payload = json.loads(completed.stdout.decode("utf-8", errors="replace") or "{}")
            except json.JSONDecodeError:
                payload = {}
            for key, value in ((payload.get("format") or {}).get("tags") or {}).items():
                tags.setdefault(str(key).replace(" ", "").replace("_", "").lower(), value)
            for stream in payload.get("streams") or []:
                for key, value in (stream.get("tags") or {}).items():
                    tags.setdefault(str(key).replace(" ", "").replace("_", "").lower(), value)
    if tags:
        return tags

    # Fallback: ffmpeg ffmetadata dump when ffprobe is missing.
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        path,
        "-f",
        "ffmetadata",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_SUBPROCESS_KWARGS,
            check=False,
        )
    except OSError:
        return tags
    if completed.returncode != 0:
        return tags
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        if not line or line.startswith(";") or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        tags.setdefault(key.replace(" ", "").replace("_", "").lower(), value)
    return tags


def _movie_name_from_tags(path, ffprobe, ffmpeg):
    """Return 'Movie Title [year].mp4' when both title and year are present."""
    tags = _read_format_tags(path, ffprobe, ffmpeg)
    title = _clean_filename(tags.get("title") or tags.get("name") or tags.get("show") or "")
    year = None
    for key in (
        "date",
        "year",
        "releasedate",
        "release_date",
        "originaldate",
        "originalyear",
        "creation_time",
        "creationtime",
    ):
        year = _parse_year(tags.get(key) or "")
        if year:
            break
    if not title or not year:
        return None
    return "%s [%s].mp4" % (title, year)


def _apply_movie_name(job, source_path, ffprobe, ffmpeg, used_destinations, output_root):
    filename = _movie_name_from_tags(source_path, ffprobe, ffmpeg)
    if not filename:
        return
    old_dest = job["dest"]
    new_dest = _unique_dest(os.path.dirname(old_dest), filename, used_destinations, old_dest)
    old_key = os.path.normcase(old_dest)
    new_key = os.path.normcase(new_dest)
    if new_key == old_key:
        return
    used_destinations.discard(old_key)
    used_destinations.add(new_key)
    job["dest"] = new_dest
    _set_job_label(job, new_dest, output_root)


def _convert_video_job(
    job,
    source_path,
    ffmpeg,
    ffprobe,
    used_destinations,
    output_root,
    cancel_event,
    rename_from_metadata,
):
    if rename_from_metadata:
        _apply_movie_name(job, source_path, ffprobe, ffmpeg, used_destinations, output_root)
    os.makedirs(os.path.dirname(job["dest"]), exist_ok=True)
    _convert_video(ffmpeg, source_path, job["dest"], cancel_event)


def _convert_video(ffmpeg, infile, outfile, cancel_event):
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        infile,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-vf",
        _SCALE_FILTER,
        "-c:v",
        "libx265",
        "-crf",
        _X265_CRF,
        "-preset",
        _X265_PRESET,
        "-tag:v",
        "hvc1",
        "-c:a",
        "aac",
        "-b:a",
        "320k",
        "-movflags",
        "+faststart",
        outfile,
    ]
    _run_ffmpeg(command, cancel_event)


def _run_ffmpeg(command, cancel_event):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_SUBPROCESS_KWARGS,
    )
    while True:
        try:
            _, stderr = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancel_event.is_set():
                process.kill()
                process.communicate()
                raise Cancelled()

    if cancel_event.is_set():
        raise Cancelled()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = "ffmpeg exited with code %s." % process.returncode
        raise RuntimeError(detail)


class App:
    def __init__(self):
        if TkinterDnD is not None:
            self.root = TkinterDnD.Tk()
        else:
            self.root = tk.Tk()

        self.root.title("Video Optimizer")
        self.root.geometry("700x680")
        self.root.minsize(560, 560)
        self.root.configure(bg="white")
        self._set_window_icon()

        self._events = queue.Queue()
        self._pending = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._cancel = threading.Event()
        self._current = None
        self._worker = None
        self._ffmpeg = None
        self._destination = None
        self._drag_over = False
        self._inventory_drag_over = False
        self._rename_from_metadata = True

        self._inventory_events = queue.Queue()
        self._inventory_cancel = threading.Event()
        self._inventory_worker = None
        self._inventory_busy = False
        self._inventory_csv = None

        self._build()
        self.root.after(100, self._poll)
        self.root.after(100, self._poll_inventory)

        initial = [arg for arg in sys.argv[1:] if _is_source(arg)]
        if initial:
            self.root.after(200, lambda: self.enqueue(initial))

    def _set_window_icon(self):
        base = _app_dir()
        ico = os.path.join(base, "icon.ico")
        png = os.path.join(base, "icon.png")
        if sys.platform == "win32" and os.path.isfile(ico):
            try:
                self.root.iconbitmap(default=ico)
            except tk.TclError:
                pass
        if os.path.isfile(png):
            try:
                self._icon_image = tk.PhotoImage(file=png)
                self.root.iconphoto(True, self._icon_image)
            except tk.TclError:
                self._icon_image = None

    def _build(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background="white", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 6), font=("Segoe UI", 9))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(12, 12))

        self.convert_tab = tk.Frame(self.notebook, bg="white")
        self.inventory_tab = tk.Frame(self.notebook, bg="white")
        self.notebook.add(self.convert_tab, text="Convert")
        self.notebook.add(self.inventory_tab, text="Inventory CSV")

        self._build_convert_tab()
        self._build_inventory_tab()

    def _build_convert_tab(self):
        parent = self.convert_tab
        header = tk.Frame(parent, bg="white")
        header.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(
            header,
            text="1080p x265 HEVC",
            font=("Segoe UI", 16, "bold"),
            bg="white",
            fg="#0f172a",
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            header,
            text=(
                "Drop folders, zips, or video files. They convert one at a time to .mp4.\n"
                "Sources above 1080p are scaled down; smaller sources keep their size.\n"
                "Results go in originalName_optimized. Originals are not changed."
            ),
            font=("Segoe UI", 9),
            bg="white",
            fg="#475569",
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        buttons = tk.Frame(parent, bg="white")
        buttons.pack(fill="x", padx=12, pady=(12, 4))
        self.choose_button = tk.Button(buttons, text="Choose folder", command=self.choose_folder)
        self.choose_button.pack(side="left")
        self.choose_files_button = tk.Button(buttons, text="Choose files", command=self.choose_files)
        self.choose_files_button.pack(side="left", padx=(8, 0))
        self.cancel_button = tk.Button(buttons, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.open_button = tk.Button(
            buttons, text="Open output folder", command=self.open_destination, state="disabled"
        )
        self.open_button.pack(side="left", padx=(8, 0))

        self.drop = tk.Frame(
            parent,
            bg="#f8fafc",
            highlightthickness=2,
            highlightbackground="#cbd5e1",
            height=120,
            cursor="hand2",
        )
        self.drop.pack(fill="x", padx=12, pady=(0, 4))
        self.drop.pack_propagate(False)
        self.drop_label = tk.Label(
            self.drop,
            text="Drop folders, zips, or videos here\nThey convert one at a time",
            font=("Segoe UI", 12, "bold"),
            bg="#f8fafc",
            fg="#0f172a",
            cursor="hand2",
        )
        self.drop_label.place(relx=0.5, rely=0.5, anchor="center")
        self.drop.bind("<Button-1>", lambda _event: self.choose_folder())
        self.drop_label.bind("<Button-1>", lambda _event: self.choose_folder())

        if DND_FILES is not None:
            for widget in (self.drop, self.drop_label):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<DragEnter>>", self._drag_enter)
                widget.dnd_bind("<<DragLeave>>", self._drag_leave)
                widget.dnd_bind("<<Drop>>", self._on_drop)

        queue_host = tk.Frame(parent, bg="white")
        queue_host.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(
            queue_host,
            text="Queue",
            font=("Segoe UI", 9, "bold"),
            bg="white",
            fg="#334155",
            anchor="w",
        ).pack(fill="x")
        queue_row = tk.Frame(queue_host, bg="white")
        queue_row.pack(fill="x", pady=(2, 0))
        self.queue_list = tk.Listbox(
            queue_row,
            height=4,
            font=("Segoe UI", 9),
            bg="#f8fafc",
            fg="#0f172a",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#e2e8f0",
            activestyle="none",
        )
        queue_scroll = ttk.Scrollbar(queue_row, command=self.queue_list.yview)
        self.queue_list.configure(yscrollcommand=queue_scroll.set)
        queue_scroll.pack(side="right", fill="y")
        self.queue_list.pack(side="left", fill="x", expand=True)

        options = tk.Frame(parent, bg="white")
        options.pack(fill="x", padx=12, pady=(8, 4))
        self.rename_var = tk.BooleanVar(value=True)
        self.rename_var.trace_add("write", self._on_rename_toggle)
        tk.Checkbutton(
            options,
            text="Rename from title/year metadata",
            variable=self.rename_var,
            bg="white",
            activebackground="white",
            font=("Segoe UI", 9),
        ).pack(side="left")

        self.progress = ttk.Progressbar(parent, mode="determinate")
        self.progress.pack(fill="x", padx=12, pady=(8, 0))

        self.status = tk.Label(
            parent,
            text=(
                "Waiting for a folder, zip, or video."
                if DND_FILES is not None
                else "Choose a folder, zip, or video to begin."
            ),
            font=("Segoe UI", 9),
            bg="white",
            fg="#334155",
            anchor="w",
        )
        self.status.pack(fill="x", padx=12, pady=(8, 0))

        log_host = tk.Frame(parent, bg="white")
        log_host.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.log = tk.Text(
            log_host,
            height=10,
            wrap="word",
            font=("Consolas", 9),
            bg="#f8fafc",
            fg="#0f172a",
            relief="flat",
            state="disabled",
        )
        scroll = ttk.Scrollbar(log_host, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("size-down", foreground="#15803d")
        self.log.tag_configure("size-up", foreground="#b91c1c")

    def _build_inventory_tab(self):
        parent = self.inventory_tab
        header = tk.Frame(parent, bg="white")
        header.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(
            header,
            text="Folder inventory",
            font=("Segoe UI", 16, "bold"),
            bg="white",
            fg="#0f172a",
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            header,
            text=(
                "Drop a folder to list every video in it and its subfolders.\n"
                "Writes a CSV with format, resolution, codec, bitrate, and whether\n"
                "conversion is recommended (not already HEVC .mp4 at ≤1080p)."
            ),
            font=("Segoe UI", 9),
            bg="white",
            fg="#475569",
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        buttons = tk.Frame(parent, bg="white")
        buttons.pack(fill="x", padx=12, pady=(12, 4))
        self.inventory_choose_button = tk.Button(
            buttons, text="Choose folder", command=self.choose_inventory_folder
        )
        self.inventory_choose_button.pack(side="left")
        self.inventory_cancel_button = tk.Button(
            buttons, text="Cancel", command=self.cancel_inventory, state="disabled"
        )
        self.inventory_cancel_button.pack(side="left", padx=(8, 0))
        self.inventory_open_button = tk.Button(
            buttons, text="Open CSV", command=self.open_inventory_csv, state="disabled"
        )
        self.inventory_open_button.pack(side="left", padx=(8, 0))

        self.inventory_drop = tk.Frame(
            parent,
            bg="#f8fafc",
            highlightthickness=2,
            highlightbackground="#cbd5e1",
            height=140,
            cursor="hand2",
        )
        self.inventory_drop.pack(fill="x", padx=12, pady=(0, 4))
        self.inventory_drop.pack_propagate(False)
        self.inventory_drop_label = tk.Label(
            self.inventory_drop,
            text="Drop a folder here\nto build an inventory CSV",
            font=("Segoe UI", 12, "bold"),
            bg="#f8fafc",
            fg="#0f172a",
            cursor="hand2",
        )
        self.inventory_drop_label.place(relx=0.5, rely=0.5, anchor="center")
        self.inventory_drop.bind("<Button-1>", lambda _event: self.choose_inventory_folder())
        self.inventory_drop_label.bind("<Button-1>", lambda _event: self.choose_inventory_folder())

        if DND_FILES is not None:
            for widget in (self.inventory_drop, self.inventory_drop_label):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<DragEnter>>", self._inventory_drag_enter)
                widget.dnd_bind("<<DragLeave>>", self._inventory_drag_leave)
                widget.dnd_bind("<<Drop>>", self._on_inventory_drop)

        self.inventory_progress = ttk.Progressbar(parent, mode="determinate")
        self.inventory_progress.pack(fill="x", padx=12, pady=(12, 0))

        self.inventory_status = tk.Label(
            parent,
            text="Waiting for a folder." if DND_FILES is not None else "Choose a folder to begin.",
            font=("Segoe UI", 9),
            bg="white",
            fg="#334155",
            anchor="w",
        )
        self.inventory_status.pack(fill="x", padx=12, pady=(8, 0))

        log_host = tk.Frame(parent, bg="white")
        log_host.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.inventory_log = tk.Text(
            log_host,
            height=10,
            wrap="word",
            font=("Consolas", 9),
            bg="#f8fafc",
            fg="#0f172a",
            relief="flat",
            state="disabled",
        )
        scroll = ttk.Scrollbar(log_host, command=self.inventory_log.yview)
        self.inventory_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.inventory_log.pack(side="left", fill="both", expand=True)

    def mainloop(self):
        self.root.mainloop()

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder to convert")
        if folder:
            self.enqueue([folder])

    def choose_files(self):
        paths = filedialog.askopenfilenames(
            title="Choose video files to convert",
            filetypes=[
                ("Video files", " ".join("*" + ext for ext in sorted(_VIDEO_EXTENSIONS))),
                ("All files", "*.*"),
            ],
        )
        if paths:
            self.enqueue(list(paths))

    def choose_inventory_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder to inventory")
        if folder:
            self.start_inventory(folder)

    def enqueue(self, paths):
        sources = []
        for path in paths:
            path = os.path.abspath(os.path.normpath(path))
            if _is_source(path) and path not in sources:
                sources.append(path)
        if not sources:
            self._set_status("Drop a folder, zip, or video file.")
            return

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            messagebox.showwarning(
                "Video Optimizer",
                "ffmpeg was not found. Install ffmpeg and add it to PATH, "
                "or place ffmpeg in the same folder as this program.",
            )
            return
        self._ffmpeg = ffmpeg

        added = []
        with self._wake:
            known = {os.path.normcase(item) for item in self._pending}
            if self._current:
                known.add(os.path.normcase(self._current))
            for source in sources:
                key = os.path.normcase(source)
                if key in known:
                    continue
                self._pending.append(source)
                known.add(key)
                added.append(source)
            if added and self._current is None:
                self._cancel.clear()
            self._wake.notify()

        if not added:
            self._set_status("Already in the queue.")
            self._sync_controls()
            return

        self._ensure_worker()
        self._sync_controls()
        if self._current is None:
            self._set_status("Queued %s." % ", ".join(os.path.basename(path) for path in added))

    def start_inventory(self, folder):
        folder = os.path.abspath(os.path.normpath(folder))
        if not os.path.isdir(folder):
            self._set_inventory_status("Drop a folder to inventory.")
            return
        if self._inventory_busy:
            self._set_inventory_status("An inventory scan is already running.")
            return

        ffprobe = find_ffprobe()
        if not ffprobe:
            ffmpeg = find_ffmpeg()
            if ffmpeg:
                ffprobe = _ffprobe_beside(ffmpeg)
        if not ffprobe:
            messagebox.showwarning(
                "Video Optimizer",
                "ffprobe was not found. Install ffmpeg/ffprobe and add them to PATH, "
                "or place ffprobe in the same folder as this program.",
            )
            return

        self._inventory_busy = True
        self._inventory_cancel.clear()
        self._inventory_csv = None
        self.inventory_open_button.configure(state="disabled")
        self.inventory_cancel_button.configure(state="normal")
        self.inventory_progress.configure(value=0, maximum=1)
        self._set_inventory_status("Starting inventory of %s…" % os.path.basename(folder))
        self._append_inventory_log(folder)

        def work():
            try:
                result = scan_folder_inventory(
                    folder,
                    ffprobe,
                    lambda done, total, message: self._inventory_events.put(
                        ("progress", done, total, message)
                    ),
                    self._inventory_cancel,
                )
                self._inventory_events.put(("result", result))
            except Cancelled:
                self._inventory_events.put(("cancelled", folder))
            except Exception as exc:
                self._inventory_events.put(("error", str(exc)))

        self._inventory_worker = threading.Thread(target=work, daemon=True)
        self._inventory_worker.start()

    def _ensure_worker(self):
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run_queue, daemon=True)
            self._worker.start()

    def _run_queue(self):
        while True:
            with self._wake:
                if not self._pending:
                    self._current = None
                    self._events.put(("state", None))
                while not self._pending:
                    self._wake.wait()
                source = self._pending.popleft()
                self._current = source
                self._cancel.clear()

            self._events.put(("begin", source))
            try:
                result = convert_folder(
                    source,
                    self._ffmpeg,
                    lambda done, total, message: self._events.put(("progress", done, total, message)),
                    self._cancel,
                    rename_from_metadata=self._rename_from_metadata,
                )
                self._events.put(("result", result))
            except Cancelled:
                self._events.put(("cancelled", source))
            except Exception as exc:
                self._events.put(("error", str(exc)))

    def _on_rename_toggle(self, *_args):
        self._rename_from_metadata = bool(self.rename_var.get())

    def cancel(self):
        with self._wake:
            self._pending.clear()
            self._cancel.set()
            self._wake.notify_all()
        self._set_status("Cancelling…")
        self._sync_controls()

    def cancel_inventory(self):
        self._inventory_cancel.set()
        self._set_inventory_status("Cancelling…")

    def open_destination(self):
        if self._destination and os.path.isdir(self._destination):
            _open_folder(self._destination)

    def open_inventory_csv(self):
        if self._inventory_csv and os.path.isfile(self._inventory_csv):
            _open_folder(os.path.dirname(self._inventory_csv))
            if sys.platform == "win32":
                try:
                    os.startfile(self._inventory_csv)
                except OSError:
                    pass

    def _on_drop(self, event):
        self._set_drag_over(False)
        raw_paths = self.root.tk.splitlist(event.data)
        sources = []
        for path in raw_paths:
            path = os.path.normpath(path)
            if os.path.isdir(path) or _is_zip_file(path) or os.path.isfile(path):
                source = path
            else:
                continue
            if source not in sources:
                sources.append(source)
        self.enqueue(sources)

    def _on_inventory_drop(self, event):
        self._set_inventory_drag_over(False)
        raw_paths = self.root.tk.splitlist(event.data)
        for path in raw_paths:
            path = os.path.normpath(path)
            if os.path.isdir(path):
                self.start_inventory(path)
                return
            if os.path.isfile(path):
                self.start_inventory(os.path.dirname(path))
                return
        self._set_inventory_status("Drop a folder to inventory.")

    def _drag_enter(self, _event):
        self._set_drag_over(True)

    def _drag_leave(self, _event):
        self._set_drag_over(False)

    def _set_drag_over(self, active):
        if active == self._drag_over:
            return
        self._drag_over = active
        bg = "#eff6ff" if active else "#f8fafc"
        border = "#2563eb" if active else "#cbd5e1"
        self.drop.configure(bg=bg, highlightbackground=border)
        self.drop_label.configure(bg=bg)

    def _inventory_drag_enter(self, _event):
        self._set_inventory_drag_over(True)

    def _inventory_drag_leave(self, _event):
        self._set_inventory_drag_over(False)

    def _set_inventory_drag_over(self, active):
        if active == self._inventory_drag_over:
            return
        self._inventory_drag_over = active
        bg = "#eff6ff" if active else "#f8fafc"
        border = "#2563eb" if active else "#cbd5e1"
        self.inventory_drop.configure(bg=bg, highlightbackground=border)
        self.inventory_drop_label.configure(bg=bg)

    def _poll(self):
        try:
            while True:
                item = self._events.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, done, total, message = item
                    if message == "Scanning…":
                        self._set_status("Scanning…" + self._waiting_suffix())
                        continue
                    maximum = total if total else 1
                    self.progress.configure(maximum=maximum, value=min(done, maximum))
                    if message != "Done.":
                        if message.startswith(("Converted ", "Copied ", "Failed:", "Note:")):
                            self._append_log(message)
                        shown = done if message.startswith(("Converted ", "Copied ", "Failed:")) else done + 1
                        self._set_status(
                            "(%s/%s) %s%s" % (min(shown, maximum), maximum, message, self._waiting_suffix())
                        )
                elif kind == "result":
                    result = item[1]
                    self._destination = result["destination"]
                    self.open_button.configure(state="normal")
                    if result["total"] == 0:
                        summary = "No files found."
                    else:
                        summary = "Done. %s converted, %s copied, %s failed." % (
                            result["converted"],
                            result["copied"],
                            result["failed"],
                        )
                    self._set_status(summary + self._waiting_suffix())
                    self._append_log(summary)
                    size_tag = {"down": "size-down", "up": "size-up"}.get(result.get("size_direction"))
                    self._append_log(result.get("size_report") or "", size_tag)
                    self._append_log(result["destination"])
                    self._sync_controls()
                elif kind == "begin":
                    self.progress.configure(value=0, maximum=1)
                    if self.log.index("end-1c") != "1.0":
                        self._append_log("")
                    self._append_log(item[1])
                    self._set_status("Starting %s%s" % (os.path.basename(item[1]), self._waiting_suffix()))
                    self._sync_controls()
                elif kind == "cancelled":
                    self._set_status("Cancelled.")
                    self._append_log("Cancelled.")
                    self._sync_controls()
                elif kind == "error":
                    self._set_status(item[1])
                    self._append_log(item[1])
                    self._sync_controls()
                elif kind == "state":
                    self._sync_controls()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _poll_inventory(self):
        try:
            while True:
                item = self._inventory_events.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, done, total, message = item
                    if message == "Scanning…":
                        self._set_inventory_status("Scanning…")
                        continue
                    maximum = total if total else 1
                    self.inventory_progress.configure(maximum=maximum, value=min(done, maximum))
                    if message != "Done.":
                        if message.startswith(("Probed ", "Failed:")):
                            self._append_inventory_log(message)
                        shown = done if message.startswith(("Probed ", "Failed:")) else done + 1
                        self._set_inventory_status("(%s/%s) %s" % (min(shown, maximum), maximum, message))
                elif kind == "result":
                    result = item[1]
                    self._inventory_busy = False
                    self._inventory_csv = result["csv_path"]
                    self.inventory_cancel_button.configure(state="disabled")
                    self.inventory_open_button.configure(state="normal")
                    if result["total"] == 0:
                        summary = "No video files found."
                    else:
                        summary = (
                            "Done. %s videos, %s need conversion, %s failed. CSV written."
                            % (result["total"], result["needs_conversion"], result["failed"])
                        )
                    self._set_inventory_status(summary)
                    self._append_inventory_log(summary)
                    self._append_inventory_log(result["csv_path"])
                elif kind == "cancelled":
                    self._inventory_busy = False
                    self.inventory_cancel_button.configure(state="disabled")
                    self._set_inventory_status("Cancelled.")
                    self._append_inventory_log("Cancelled.")
                elif kind == "error":
                    self._inventory_busy = False
                    self.inventory_cancel_button.configure(state="disabled")
                    self._set_inventory_status(item[1])
                    self._append_inventory_log(item[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_inventory)

    def _snapshot(self):
        with self._lock:
            return self._current, list(self._pending)

    def _waiting_suffix(self):
        _, pending = self._snapshot()
        if not pending:
            return ""
        noun = "item" if len(pending) == 1 else "items"
        return " — %s %s waiting" % (len(pending), noun)

    def _sync_controls(self):
        current, pending = self._snapshot()
        self.cancel_button.configure(state="normal" if current or pending else "disabled")
        self.queue_list.delete(0, "end")
        if current:
            self.queue_list.insert("end", "Converting   %s" % os.path.basename(current))
        for path in pending:
            self.queue_list.insert("end", "Waiting      %s" % os.path.basename(path))

    def _set_status(self, message):
        self.status.configure(text=message)

    def _set_inventory_status(self, message):
        self.inventory_status.configure(text=message)

    def _append_log(self, line, tag=None):
        self.log.configure(state="normal")
        if self.log.index("end-1c") != "1.0":
            self.log.insert("end", "\n")
        if tag:
            self.log.insert("end", line, tag)
        else:
            self.log.insert("end", line)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _append_inventory_log(self, line):
        self.inventory_log.configure(state="normal")
        if self.inventory_log.index("end-1c") != "1.0":
            self.inventory_log.insert("end", "\n")
        self.inventory_log.insert("end", line)
        self.inventory_log.see("end")
        self.inventory_log.configure(state="disabled")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
