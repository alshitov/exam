#!/usr/bin/env python3
"""Exam runner: capture loop, session prep/cleanup, Cursor worker dispatch."""
from __future__ import annotations

import argparse
import configparser
import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import mss
from PIL import Image


@dataclass(frozen=True)
class AppConfig:
    monitor: int
    x0: int
    y0: int
    width: int
    height: int
    interval_seconds: float
    session_name: str
    screenshots_root: Path

    @property
    def session_dir(self) -> Path:
        return self.screenshots_root / self.session_name

    @property
    def answers_file(self) -> Path:
        return Path("answers") / self.session_name / "answers.txt"

    @property
    def screenshot_region(self) -> dict[str, int]:
        return {
            "left": self.x0,
            "top": self.y0,
            "width": self.width,
            "height": self.height,
            "mon": self.monitor,
        }


def read_config(config_path: Path) -> AppConfig:
    parser = configparser.ConfigParser()
    if not parser.read(config_path):
        raise FileNotFoundError(f"Config file was not found: {config_path}")

    capture = parser["capture"]
    paths = parser["paths"]
    runtime = parser["runtime"]

    return AppConfig(
        monitor=capture.getint("monitor"),
        x0=capture.getint("x0"),
        y0=capture.getint("y0"),
        width=capture.getint("w"),
        height=capture.getint("h"),
        interval_seconds=runtime.getfloat("interval_seconds"),
        session_name=runtime.get("session_name"),
        screenshots_root=Path(paths.get("screenshots_root")),
    )


def load_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def ensure_start_artifacts(config: AppConfig) -> None:
    config.session_dir.mkdir(parents=True, exist_ok=True)
    config.answers_file.parent.mkdir(parents=True, exist_ok=True)
    config.answers_file.touch(exist_ok=True)


def setup_stderr_logger() -> logging.Logger:
    logger = logging.getLogger("exam")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def next_index(session_dir: Path) -> int:
    max_index = 0
    for file_path in session_dir.glob("*.png"):
        try:
            max_index = max(max_index, int(file_path.stem))
        except ValueError:
            continue
    return max_index + 1


def previous_screenshot_size(session_dir: Path, index: int) -> int | None:
    if index <= 1:
        return None
    prev = session_dir / f"{index - 1}.png"
    if not prev.is_file():
        return None
    return prev.stat().st_size


def png_bytes_for_image(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _grab_region_mss_once(region: dict[str, int]) -> Image.Image:
    with mss.mss() as sct:
        raw = sct.grab(region)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def grab_region(region: dict[str, int]) -> Image.Image:
    original_display = os.environ.get("DISPLAY")
    display_candidates = [original_display, ":0", ":0.0", "127.0.0.1:0.0", "localhost:0.0"]

    errors: list[str] = []
    for candidate in display_candidates:
        try:
            if candidate:
                os.environ["DISPLAY"] = candidate
            else:
                os.environ.pop("DISPLAY", None)

            class _MssTimeout(Exception):
                pass

            def _timeout_handler(_: int, __: object) -> None:
                raise _MssTimeout()

            old_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.setitimer(signal.ITIMER_REAL, 3.0)
            try:
                return _grab_region_mss_once(region)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old_handler)
        except _MssTimeout:
            errors.append(f"DISPLAY={candidate!r}: mss timeout after 3s")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"DISPLAY={candidate!r}: {exc}")
    if original_display is None:
        os.environ.pop("DISPLAY", None)
    else:
        os.environ["DISPLAY"] = original_display
    raise RuntimeError("mss capture failed for all DISPLAY variants | " + " | ".join(errors))


def _as_windows_path(path: Path) -> str:
    result = subprocess.run(
        ["wslpath", "-w", str(path.resolve())],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def grab_region_via_powershell(region: dict[str, int], output_path: Path) -> None:
    windows_path = _as_windows_path(output_path)
    script = (
        "Add-Type -AssemblyName System.Drawing; "
        f"$bmp = New-Object System.Drawing.Bitmap({region['width']}, {region['height']}); "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        f"$g.CopyFromScreen({region['left']}, {region['top']}, 0, 0, $bmp.Size); "
        f"$bmp.Save('{windows_path}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$g.Dispose(); "
        "$bmp.Dispose();"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "powershell fallback failed "
            f"(exit={result.returncode}) stdout={result.stdout!r} stderr={result.stderr!r}"
        )


def call_screenshot_worker(config: AppConfig, screenshot_path: Path, env_vars: dict[str, str], logger: logging.Logger) -> None:
    prompt = (
        "Use the exam-screenshot-worker subagent and process exactly one screenshot with these "
        f"parameters: session_name=\"{config.session_name}\", "
        f"screenshot_path=\"{screenshot_path}\", "
        f"answers_file=\"{config.answers_file}\", "
        "config=\"config.ini\". Follow the subagent role instructions exactly."
    )
    env = os.environ.copy()
    env.update(env_vars)
    logger.info("Calling screenshot worker for screenshot: %s", screenshot_path.name)
    result = subprocess.run(
        ["cursor", "agent", "--trust", prompt],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"cursor agent failed (exit={result.returncode}) stderr={result.stderr.strip()}"
        )
    logger.info("Worker completed for screenshot: %s", screenshot_path.name)
    logger.info("Waiting for 5 seconds for user to answer the question")
    time.sleep(5)
    logger.info("5 seconds passed, continuing with the next screenshot")


def cmd_start(config: AppConfig) -> int:
    ensure_start_artifacts(config)
    env_vars = load_env_file(Path(".env"))
    if not env_vars.get("CURSOR_API_KEY"):
        raise RuntimeError("CURSOR_API_KEY is missing in .env")

    logger = setup_stderr_logger()
    should_stop = False

    def _handle_stop(_: int, __: object) -> None:
        nonlocal should_stop
        should_stop = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    index = next_index(config.session_dir)
    logger.info(
        "Started exam loop | session=%s | dir=%s | region=%s | interval=%.2fs",
        config.session_name,
        config.session_dir,
        config.screenshot_region,
        config.interval_seconds,
    )

    while not should_stop:
        cycle_start = time.monotonic()
        output_path = config.session_dir / f"{index}.png"
        try:
            prev_size = previous_screenshot_size(config.session_dir, index)
            try:
                image = grab_region(config.screenshot_region)
                png_bytes = png_bytes_for_image(image)
                if prev_size is not None and len(png_bytes) == prev_size:
                    logger.info(
                        "Skipped screenshot (same size as previous): would-be=%s size=%s",
                        output_path.name,
                        prev_size,
                    )
                else:
                    output_path.write_bytes(png_bytes)
                    logger.info("Saved screenshot: %s", output_path)
                    call_screenshot_worker(config, output_path.resolve(), env_vars, logger)
                    index += 1
            except Exception as exc:  # noqa: BLE001
                if "display" not in str(exc).lower():
                    raise
                logger.warning(
                    "mss capture failed due to display access; falling back to powershell.exe"
                )
                tmp_path = output_path.with_name(f".{output_path.name}.tmp")
                try:
                    grab_region_via_powershell(config.screenshot_region, tmp_path)
                    new_size = tmp_path.stat().st_size
                    if prev_size is not None and new_size == prev_size:
                        logger.info(
                            "Skipped screenshot (same size as previous): would-be=%s size=%s",
                            output_path.name,
                            prev_size,
                        )
                    else:
                        tmp_path.replace(output_path)
                        logger.info("Saved screenshot: %s", output_path)
                        call_screenshot_worker(config, output_path.resolve(), env_vars, logger)
                        index += 1
                finally:
                    if tmp_path.is_file():
                        tmp_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cycle failed, retry on next tick: %s", exc)

        elapsed = time.monotonic() - cycle_start
        sleep_for = max(0.0, config.interval_seconds - elapsed)
        if sleep_for > 0:
            time.sleep(sleep_for)
    logger.info("Exam loop stopped gracefully")
    return 0


def cmd_prepare(config: AppConfig) -> int:
    """Create session dirs and answers file."""
    ensure_start_artifacts(config)
    print(config.session_dir.resolve())
    return 0


def cmd_clean(config: AppConfig) -> int:
    session_dir = config.session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    removed = 0
    for png in session_dir.glob("*.png"):
        png.unlink(missing_ok=True)
        removed += 1
    print(f"cleaned session screenshots: {removed}")
    return 0


def cmd_clean_all() -> int:
    for rel in ["screenshots", "answers"]:
        p = Path(rel)
        if p.exists():
            shutil.rmtree(p)
    for rel in ["screenshots", "answers"]:
        Path(rel).mkdir(parents=True, exist_ok=True)
    legacy_runtime = Path("runtime")
    if legacy_runtime.exists():
        shutil.rmtree(legacy_runtime)
    legacy_logs = Path("logs")
    if legacy_logs.exists():
        shutil.rmtree(legacy_logs)
    print("cleaned all session artifacts")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-process exam runner with Cursor worker calls")
    parser.add_argument("--config", default="config.ini", help="Path to INI config file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="Create session artifacts (dirs, answers file)")
    sub.add_parser("start", help="Run foreground screenshot loop and call exam-screenshot-worker")
    sub.add_parser("clean", help="Delete screenshots for current session")
    sub.add_parser("clean-all", help="Delete all session artifacts")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config = read_config(Path(args.config))
    if args.command == "prepare":
        return cmd_prepare(config)
    if args.command == "start":
        return cmd_start(config)
    if args.command == "clean":
        return cmd_clean(config)
    if args.command == "clean-all":
        return cmd_clean_all()
    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
