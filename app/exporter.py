"""Exporter — ffmpeg stitching with graceful degradation to a materials package."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class Exporter:
    def __init__(self, music: str = ""):
        self.music = music

    def ffmpeg_available(self) -> bool:
        return shutil.which("ffmpeg") is not None

    def export(self, video_files: list[str], run_dir: Path, log) -> dict:
        if self.ffmpeg_available():
            return self._stitch(video_files, run_dir, log)
        return self._materials_package(video_files, run_dir, log)

    def _stitch(self, video_files: list[str], run_dir: Path, log) -> dict:
        list_path = run_dir / "concat.txt"
        list_path.write_text(
            "\n".join(f"file '{Path(f).resolve()}'" for f in video_files), "utf-8")
        out = run_dir / "final.mp4"
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path)]
        if self.music:
            cmd += ["-i", self.music, "-c:v", "copy", "-map", "0:v:0", "-map", "1:a:0",
                    "-shortest"]
        else:
            cmd += ["-c", "copy"]
        cmd += [str(out)]
        log.emit("ffmpeg_start", cmd=cmd)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            log.emit("ffmpeg_failed", stderr=proc.stderr[-2000:])
            # degrade rather than dead-end
            return self._materials_package(video_files, run_dir, log)
        log.emit("ffmpeg_done", file=out.name)
        return {"mode": "ffmpeg", "final_video": out.name, "shots": len(video_files)}

    def _materials_package(self, video_files: list[str], run_dir: Path, log) -> dict:
        pkg = run_dir / "materials"
        pkg.mkdir(parents=True, exist_ok=True)
        copied = []
        for f in video_files:
            dst = pkg / Path(f).name
            shutil.copy(f, dst)
            copied.append(dst.name)
        (pkg / "README.txt").write_text(
            "ffmpeg 未检测到或拼接失败，已导出素材包。请按文件名顺序自行拼接。\n"
            "ffmpeg not available (or stitching failed); materials exported in shot order.\n",
            "utf-8")
        log.emit("materials_exported", files=copied)
        return {"mode": "materials", "files": copied}
