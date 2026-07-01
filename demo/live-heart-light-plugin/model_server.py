"""Local Open-rppg capture server for the live heart light plugin.

Run from the repository root:

    python demo/live-heart-light-plugin/model_server.py

Then open:

    http://127.0.0.1:8020/
"""

from __future__ import annotations

import atexit
import json
import sys
import threading
import time
from collections import deque
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import rppg


HOST = "127.0.0.1"
PORT = 8020
PLUGIN_DIR = Path(__file__).resolve().parent
FACE_WIDTH = 36
FACE_HEIGHT = 36
FACE_CHANNELS = 3
METRIC_INTERVAL_SECONDS = 0.45
INPUT_FPS_WINDOW_SECONDS = 3.0
MAX_CLIENT_TS_GAP_SECONDS = 1.5
OUTPUT_SQI_THRESHOLD = 0.38
PREVIEW_SQI_THRESHOLD = 0.2
OUTPUT_TTL_SECONDS = 3.0
DEVICE_SCAN_MAX = 5
CAPTURE_NO_FRAME_TIMEOUT_SECONDS = 2.0
DEFAULT_CAPTURE = {"device_index": 0, "width": 1280, "height": 720, "fps": 30}
MJPEG_BOUNDARY = "live-heart-frame"


def safe_float(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(value) or np.isinf(value):
        return None
    return value


def camera_api():
    return cv2.CAP_DSHOW if sys.platform.startswith("win32") else 0


class OpenRppgRuntime:
    def __init__(self, model_name: str = "FacePhys.rlap"):
        self.model_name = model_name
        self.model = None
        self.state = "idle"
        self.error = None
        self.frame_count = 0
        self.last_result = None
        self.last_box = None
        self.last_metric_at = 0.0
        self.last_input_at = None
        self.last_client_ts = None
        self.last_reset_at = None
        self.reset_reason = None
        self.last_request_ms = None
        self.last_update_ms = None
        self.last_metric_ms = None
        self.request_count = 0
        self.recent_input_times = []
        self.input_mode = None
        self.last_face_at = None
        self.last_no_face_at = None
        self._last_null_count = 0
        self.started_at = None
        self._context = None
        self._start_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._start_thread = None

    def start_async(self):
        with self._start_lock:
            if self.state in {"loading", "ready"}:
                return
            self.state = "loading"
            self.error = None
            self._start_thread = threading.Thread(target=self._start, daemon=True)
            self._start_thread.start()

    def is_ready(self):
        return self.state == "ready" and self.model is not None

    def _start(self):
        try:
            model = rppg.Model(self.model_name)
            model.face_detection_threads = 1
            model.face_resampling_threads = 1
            model.face_detect_per_n = 3
            self._context = model.__enter__()
            self.model = model
            self.started_at = time.time()
            self.state = "ready"
        except Exception as exc:  # pragma: no cover - surfaced to UI
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "failed"

    def stop(self):
        self._close_model()
        self.state = "stopped"

    def reset_session(self, reason: str = "manual_reset"):
        with self._start_lock:
            with self._frame_lock:
                if self.model is not None:
                    self._restart_ready_context(reason)
                else:
                    self._clear_stream_state(reason)
                    self.state = "idle"
                    self.started_at = None

    def _close_model(self):
        if self.model is not None and self._context is not None:
            try:
                self.model.__exit__(None, None, None)
            except Exception:
                pass
        self.model = None
        self._context = None

    def _clear_stream_state(self, reason: str | None):
        self.error = None
        self.frame_count = 0
        self.last_result = None
        self.last_box = None
        self.last_metric_at = 0.0
        self.last_input_at = None
        self.last_client_ts = None
        self.last_request_ms = None
        self.last_update_ms = None
        self.last_metric_ms = None
        self.request_count = 0
        self.recent_input_times = []
        self.input_mode = None
        self.last_face_at = None
        self.last_no_face_at = None
        self._last_null_count = 0
        self.last_reset_at = time.time()
        self.reset_reason = reason

    def _restart_ready_context(self, reason: str | None):
        model = self.model
        if model is None:
            self._clear_stream_state(reason)
            self.state = "idle"
            return
        if self._context is not None:
            try:
                model.__exit__(None, None, None)
            except Exception:
                pass
        self._clear_stream_state(reason)
        self._context = model.__enter__()
        self.started_at = time.time()
        self.state = "ready"

    def _ready_or_status(self):
        if self.state == "idle":
            self.start_async()
        if self.state != "ready" or self.model is None:
            return False, self.status()
        return True, None

    def submit_capture_frame(self, rgb: np.ndarray, ts: float):
        request_started = time.perf_counter()
        ready, status = self._ready_or_status()
        if not ready:
            return status

        with self._frame_lock:
            try:
                if self._has_large_client_gap(ts):
                    self._restart_ready_context("capture_gap")
                update_started = time.perf_counter()
                self.model.update_frame(rgb, ts)
                self.last_client_ts = ts
                self.last_update_ms = (time.perf_counter() - update_started) * 1000
                self._mark_input("capture")
                self.last_box = self._box_to_list(getattr(self.model, "box", None))
                self._update_metric()
            except Exception as exc:  # pragma: no cover - surfaced to UI
                self.error = f"{type(exc).__name__}: {exc}"
                self.state = "failed"

        self._mark_request(request_started)
        return self.status()

    def submit_jpeg(self, payload: bytes, ts: float | None):
        request_started = time.perf_counter()
        ready, status = self._ready_or_status()
        if not ready:
            return status

        arr = np.frombuffer(payload, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return {"ok": False, "error": "invalid image", **self.status()}
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

        with self._frame_lock:
            try:
                if self._has_large_client_gap(ts):
                    self._restart_ready_context("input_gap")
                update_started = time.perf_counter()
                self.model.update_frame(rgb, ts or time.time())
                self.last_client_ts = ts
                self.last_update_ms = (time.perf_counter() - update_started) * 1000
                self._mark_input("frame")
                self.last_box = self._box_to_list(getattr(self.model, "box", None))
                self._update_metric()
            except Exception as exc:  # pragma: no cover - surfaced to UI
                self.error = f"{type(exc).__name__}: {exc}"
                self.state = "failed"

        self._mark_request(request_started)
        return {"ok": True, **self.status()}

    def submit_face(self, payload: bytes, ts: float | None, width: int, height: int, hasface: bool):
        request_started = time.perf_counter()
        ready, status = self._ready_or_status()
        if not ready:
            return status

        face = None
        if hasface:
            expected = width * height * FACE_CHANNELS
            if len(payload) != expected:
                return {"ok": False, "error": f"expected {expected} RGB bytes", **self.status()}
            face = np.frombuffer(payload, dtype=np.uint8).reshape((height, width, FACE_CHANNELS))
            target_h, target_w = self.model.input[1:3]
            if (height, width) != (target_h, target_w):
                face = cv2.resize(face, (target_w, target_h), interpolation=cv2.INTER_AREA)
            face = partial(lambda x: x, face)

        with self._frame_lock:
            try:
                if self._has_large_client_gap(ts):
                    self._restart_ready_context("input_gap")
                update_started = time.perf_counter()
                self.model.update_face_resized(face, ts=ts or time.time(), hasface=hasface)
                self.last_client_ts = ts
                self.last_update_ms = (time.perf_counter() - update_started) * 1000
                self._mark_input("face" if hasface else "noface")
                self.last_box = None
                self._update_metric()
            except Exception as exc:  # pragma: no cover - surfaced to UI
                self.error = f"{type(exc).__name__}: {exc}"
                self.state = "failed"

        self._mark_request(request_started)
        return {"ok": True, **self.status()}

    def _has_large_client_gap(self, ts: float | None):
        if ts is None or self.last_client_ts is None or self.frame_count <= 0:
            return False
        return ts - self.last_client_ts > MAX_CLIENT_TS_GAP_SECONDS

    def _mark_request(self, request_started: float):
        self.last_request_ms = (time.perf_counter() - request_started) * 1000
        self.request_count += 1

    def _mark_input(self, mode: str):
        now = time.time()
        self.frame_count += 1
        self.input_mode = mode
        self.last_input_at = now
        self.recent_input_times.append(now)
        cutoff = now - INPUT_FPS_WINDOW_SECONDS
        while self.recent_input_times and self.recent_input_times[0] < cutoff:
            self.recent_input_times.pop(0)

    def _update_metric(self):
        now = time.time()
        if now - self.last_metric_at <= METRIC_INTERVAL_SECONDS:
            return
        self.last_metric_at = now
        metric_started = time.perf_counter()
        try:
            self.last_result = self.model.hr(start=-10, return_hrv=False)
        except Exception:
            self.last_result = None
        self.last_metric_ms = (time.perf_counter() - metric_started) * 1000

    def status(self):
        result = self.last_result or {}
        hr = result.get("hr") if isinstance(result, dict) else None
        sqi = result.get("SQI") if isinstance(result, dict) else None
        latency = result.get("latency") if isinstance(result, dict) else None
        internal = self._internal_status()
        has_face = self._refresh_face_state(internal)
        return {
            "model": self.model_name,
            "state": self.state,
            "ready": self.state == "ready",
            "error": self.error,
            "frame_count": self.frame_count,
            "started_at": self.started_at,
            "box": self.last_box,
            "has_face": has_face,
            "last_face_at": self.last_face_at,
            "last_no_face_at": self.last_no_face_at,
            "no_face_count": internal["statistic"].get("null", 0),
            "input_mode": self.input_mode,
            "input_fps": safe_float(self._input_fps()),
            "last_input_at": self.last_input_at,
            "last_reset_at": self.last_reset_at,
            "reset_reason": self.reset_reason,
            "perf": {
                "request_ms": safe_float(self.last_request_ms),
                "update_ms": safe_float(self.last_update_ms),
                "metric_ms": safe_float(self.last_metric_ms),
                "request_count": self.request_count,
            },
            "internal": internal,
            "result": {
                "hr": safe_float(hr),
                "SQI": safe_float(sqi),
                "latency": safe_float(latency),
            },
        }

    def _internal_status(self):
        model = self.model
        if model is None:
            return {
                "n_frame": 0,
                "n_signal": 0,
                "face_buffer": 0,
                "signal_keys": [],
                "statistic": {},
            }
        signal_buff = getattr(model, "signal_buff", {}) or {}
        statistic = getattr(model, "statistic", {}) or {}
        return {
            "n_frame": int(getattr(model, "n_frame", 0) or 0),
            "n_signal": int(getattr(model, "n_signal", 0) or 0),
            "face_buffer": len(getattr(model, "face_buff", []) or []),
            "signal_keys": list(signal_buff.keys()),
            "statistic": {key: int(value or 0) for key, value in statistic.items()},
        }

    def _refresh_face_state(self, internal: dict):
        model = self.model
        if model is None:
            return False
        now = time.time()
        self.last_box = self._box_to_list(getattr(model, "box", None))
        null_count = int((internal.get("statistic") or {}).get("null", 0) or 0)
        if null_count > self._last_null_count:
            self.last_no_face_at = now
            self._last_null_count = null_count
        if self.last_box is not None and (
            self.last_no_face_at is None or now - self.last_no_face_at > 1.0
        ):
            self.last_face_at = now
        if self.last_face_at is None:
            return False
        if now - self.last_face_at > 2.0:
            return False
        if self.last_no_face_at is not None and self.last_no_face_at > self.last_face_at:
            return False
        try:
            hasface_until = float(getattr(model, "hasface", 0) or 0)
            now = float(getattr(model, "now", 0) or 0)
            return hasface_until > now or self.last_box is not None
        except Exception:
            return self.last_box is not None

    def _input_fps(self):
        if len(self.recent_input_times) < 2:
            return None
        duration = self.recent_input_times[-1] - self.recent_input_times[0]
        if duration <= 0:
            return None
        return (len(self.recent_input_times) - 1) / duration

    @staticmethod
    def _box_to_list(box):
        if box is None:
            return None
        try:
            return np.asarray(box).astype(int).tolist()
        except Exception:
            return None


class CaptureWorker:
    def __init__(self, runtime: OpenRppgRuntime):
        self.runtime = runtime
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        self._last_frame_bgr = None
        self._recent_frame_times = deque()
        self.state = "idle"
        self.error = None
        self.device_index = DEFAULT_CAPTURE["device_index"]
        self.width = DEFAULT_CAPTURE["width"]
        self.height = DEFAULT_CAPTURE["height"]
        self.fps = DEFAULT_CAPTURE["fps"]
        self.actual_width = None
        self.actual_height = None
        self.actual_fps = None
        self.frames_read = 0
        self.dropped_frames = 0
        self.last_frame_at = None
        self.started_at = None
        self.stopped_at = None
        self.last_read_ms = None
        self.last_model_state = None

    def start(self, config: dict | None = None):
        config = config or {}
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()

            self.device_index = self._as_int(config.get("device_index"), DEFAULT_CAPTURE["device_index"])
            self.width = self._as_int(config.get("width"), DEFAULT_CAPTURE["width"])
            self.height = self._as_int(config.get("height"), DEFAULT_CAPTURE["height"])
            self.fps = self._as_int(config.get("fps"), DEFAULT_CAPTURE["fps"])
            self.error = None
            self.state = "starting"
            self.frames_read = 0
            self.dropped_frames = 0
            self.last_frame_at = None
            self.started_at = time.time()
            self.stopped_at = None
            self.last_read_ms = None
            self.last_model_state = None
            self.actual_width = None
            self.actual_height = None
            self.actual_fps = None
            self._last_frame_bgr = None
            self._recent_frame_times.clear()
            self._stop_event.clear()
            self.runtime.start_async()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return self.status()

    def stop(self, reset_model: bool = True):
        thread = None
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self.state = "stopping"
                self._stop_event.set()
                thread = self._thread
            else:
                self.state = "idle"
                self.stopped_at = time.time()

        if thread is not None:
            thread.join(timeout=4.0)

        with self._lock:
            if self.state == "stopping":
                self.state = "idle"
            self.stopped_at = time.time()

        if reset_model:
            self.runtime.reset_session("capture_stop")
        return self.status()

    def status(self):
        with self._lock:
            return {
                "state": self.state,
                "error": self.error,
                "device_index": self.device_index,
                "width": self.actual_width or self.width,
                "height": self.actual_height or self.height,
                "fps": self.actual_fps or self.fps,
                "target_fps": self.fps,
                "input_fps": safe_float(self._input_fps_locked()),
                "frames_read": self.frames_read,
                "dropped_frames": self.dropped_frames,
                "last_frame_at": self.last_frame_at,
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "read_ms": safe_float(self.last_read_ms),
                "model_state": self.last_model_state,
            }

    def snapshot_jpeg(self):
        with self._lock:
            frame = None if self._last_frame_bgr is None else self._last_frame_bgr.copy()
            state = self.state
            message = self.error or "Waiting for capture"

        if frame is None:
            frame = self._placeholder_frame(state, message)
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            return b""
        return encoded.tobytes()

    def _run(self):
        cap = None
        model_base_mono = None
        try:
            cap = cv2.VideoCapture(self.device_index, camera_api())
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)

            if not cap.isOpened():
                self._set_error("camera_error", f"Cannot open camera device {self.device_index}")
                return

            self._set_running(cap)
            last_frame_mono = None
            min_period = 1.0 / max(1, self.fps)

            while not self._stop_event.is_set():
                loop_started = time.perf_counter()
                ok, bgr = cap.read()
                now_mono = time.monotonic()
                if not ok or bgr is None:
                    self._mark_drop()
                    if last_frame_mono is None or now_mono - last_frame_mono > CAPTURE_NO_FRAME_TIMEOUT_SECONDS:
                        self._set_error("camera_error", "Camera stopped returning frames")
                        return
                    time.sleep(0.02)
                    continue

                last_frame_mono = now_mono
                read_ms = (time.perf_counter() - loop_started) * 1000
                self._mark_frame(bgr, read_ms)

                if self.runtime.is_ready():
                    if model_base_mono is None:
                        model_base_mono = now_mono
                    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                    status = self.runtime.submit_capture_frame(rgb, now_mono - model_base_mono)
                    self._set_model_state(status.get("state"))
                else:
                    self.runtime.start_async()
                    self._set_model_state(self.runtime.state)

                elapsed = time.perf_counter() - loop_started
                if elapsed < min_period:
                    time.sleep(min_period - elapsed)
        except Exception as exc:  # pragma: no cover - surfaced to UI
            self._set_error("camera_error", f"{type(exc).__name__}: {exc}")
        finally:
            if cap is not None:
                cap.release()
            with self._lock:
                if self.state in {"running", "starting", "stopping"}:
                    self.state = "idle" if self._stop_event.is_set() else self.state
                self.stopped_at = time.time()

    def _set_running(self, cap):
        with self._lock:
            self.state = "running"
            self.error = None
            self.actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or self.width)
            self.actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or self.height)
            actual_fps = cap.get(cv2.CAP_PROP_FPS)
            self.actual_fps = safe_float(actual_fps) or self.fps

    def _set_error(self, state: str, message: str):
        with self._lock:
            self.state = state
            self.error = message
            self.stopped_at = time.time()

    def _mark_frame(self, bgr: np.ndarray, read_ms: float):
        now = time.time()
        with self._lock:
            self.frames_read += 1
            self.last_frame_at = now
            self.last_read_ms = read_ms
            self._last_frame_bgr = bgr.copy()
            self._recent_frame_times.append(now)
            cutoff = now - INPUT_FPS_WINDOW_SECONDS
            while self._recent_frame_times and self._recent_frame_times[0] < cutoff:
                self._recent_frame_times.popleft()

    def _mark_drop(self):
        with self._lock:
            self.dropped_frames += 1

    def _set_model_state(self, state: str | None):
        with self._lock:
            self.last_model_state = state

    def _input_fps_locked(self):
        if len(self._recent_frame_times) < 2:
            return None
        duration = self._recent_frame_times[-1] - self._recent_frame_times[0]
        if duration <= 0:
            return None
        return (len(self._recent_frame_times) - 1) / duration

    @staticmethod
    def _as_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _placeholder_frame(state: str, message: str):
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[:] = (18, 22, 25)
        cv2.putText(frame, "rPPG Camera Source", (64, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.8, (240, 247, 247), 4)
        cv2.putText(frame, state.upper(), (64, 190), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (91, 193, 245), 3)
        cv2.putText(frame, message[:70], (64, 250), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (181, 179, 168), 2)
        return frame


class OverlaySettings:
    def __init__(self):
        self._lock = threading.RLock()
        self.pulse = True
        self.brightness = 72
        self.temperature = 4800

    def update(self, payload: dict):
        with self._lock:
            if "pulse" in payload:
                self.pulse = bool(payload["pulse"])
            if "brightness" in payload:
                self.brightness = self._clamp_int(payload["brightness"], 20, 100, self.brightness)
            if "temperature" in payload:
                self.temperature = self._clamp_int(payload["temperature"], 2700, 6500, self.temperature)
            return self.status()

    def status(self):
        with self._lock:
            return {
                "pulse": self.pulse,
                "brightness": self.brightness,
                "temperature": self.temperature,
            }

    @staticmethod
    def _clamp_int(value, min_value, max_value, default):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return min(max_value, max(min_value, value))


runtime = OpenRppgRuntime()
capture = CaptureWorker(runtime)
settings = OverlaySettings()


def overlay_state():
    capture_status = capture.status()
    model_status = runtime.status()
    result = model_status.get("result", {})
    hr = result.get("hr")
    sqi = result.get("SQI")
    now = time.time()
    recent_input = (
        model_status.get("last_input_at") is not None
        and now - model_status["last_input_at"] <= OUTPUT_TTL_SECONDS
    )
    capture_running = capture_status["state"] == "running"
    has_hr = isinstance(hr, (int, float)) and 30 <= hr <= 180
    has_sqi = isinstance(sqi, (int, float))
    has_face = bool(model_status.get("has_face"))

    bpm = None
    confidence = safe_float(sqi) or 0.0
    status = "waiting"
    reason = "capture_idle"
    source = "none"

    if not capture_running:
        reason = capture_status.get("error") or "capture_idle"
    elif not model_status.get("ready"):
        status = "warming"
        reason = model_status.get("state") or "model_loading"
        source = "open-rppg"
    elif not recent_input:
        status = "warming"
        reason = "no_recent_input"
        source = "open-rppg"
    elif not has_face:
        status = "no_face"
        reason = "no_face"
        source = "open-rppg"
    elif has_hr and has_sqi and sqi >= OUTPUT_SQI_THRESHOLD:
        bpm = round(hr)
        status = "stable"
        reason = "ready"
        source = "open-rppg"
    elif has_hr and has_sqi and sqi >= PREVIEW_SQI_THRESHOLD:
        status = "preview"
        reason = "low_sqi_preview"
        source = "open-rppg"
    elif has_hr and has_sqi:
        status = "low_sqi"
        reason = "low_sqi"
        source = "open-rppg"
    else:
        status = "warming"
        reason = "building_window"
        source = "open-rppg"

    return {
        "capture": capture_status,
        "model": {
            "state": model_status.get("state"),
            "ready": model_status.get("ready"),
            "model": model_status.get("model"),
            "frame_count": model_status.get("frame_count"),
            "input_fps": model_status.get("input_fps"),
            "last_input_at": model_status.get("last_input_at"),
            "hr": safe_float(hr),
            "SQI": safe_float(sqi),
            "latency": safe_float(result.get("latency")),
            "box": model_status.get("box"),
            "has_face": has_face,
            "no_face_count": model_status.get("no_face_count", 0),
            "error": model_status.get("error"),
            "perf": model_status.get("perf", {}),
            "internal": model_status.get("internal", {}),
        },
        "output": {
            "bpm": bpm,
            "confidence": safe_float(confidence) or 0.0,
            "status": status,
            "reason": reason,
            "source": source,
            "updated_at": int(now * 1000),
        },
        "settings": settings.status(),
    }


def list_devices():
    devices = []
    for index in range(DEVICE_SCAN_MAX + 1):
        cap = cv2.VideoCapture(index, camera_api())
        available = bool(cap.isOpened())
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) if available else None
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) if available else None
        fps = safe_float(cap.get(cv2.CAP_PROP_FPS)) if available else None
        cap.release()
        devices.append({
            "device_index": index,
            "available": available,
            "width": width,
            "height": height,
            "fps": fps,
        })
    return {"devices": devices}


def shutdown():
    capture.stop(reset_model=False)
    runtime.stop()


atexit.register(shutdown)


class PluginHandler(SimpleHTTPRequestHandler):
    server_version = "LiveHeartOpenRppg/0.2"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PLUGIN_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/model/status":
            self._send_json(runtime.status())
            return
        if parsed.path == "/api/model/start":
            runtime.start_async()
            self._send_json(runtime.status())
            return
        if parsed.path == "/api/capture/status":
            self._send_json(capture.status())
            return
        if parsed.path == "/api/capture/devices":
            self._send_json(list_devices())
            return
        if parsed.path == "/api/overlay/state":
            self._send_json(overlay_state())
            return
        if parsed.path == "/api/overlay/settings":
            self._send_json(settings.status())
            return
        if parsed.path == "/api/capture/preview.mjpg":
            self._send_mjpeg()
            return
        if parsed.path == "/api/capture/snapshot.jpg":
            self._send_jpeg(capture.snapshot_jpeg())
            return

        self.path = parsed.path
        if self.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/model/start":
            runtime.start_async()
            self._send_json(runtime.status())
            return
        if parsed.path == "/api/model/reset":
            runtime.reset_session()
            self._send_json(runtime.status())
            return
        if parsed.path == "/api/capture/start":
            self._send_json(capture.start(self._read_json_body()))
            return
        if parsed.path == "/api/capture/stop":
            self._send_json(capture.stop(reset_model=True))
            return
        if parsed.path == "/api/overlay/settings":
            self._send_json(settings.update(self._read_json_body()))
            return
        if parsed.path not in {"/api/model/frame", "/api/model/face"}:
            self.send_error(404, "Unknown endpoint")
            return

        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        query = parse_qs(parsed.query)
        ts = None
        if "ts" in query:
            try:
                ts = float(query["ts"][0])
            except ValueError:
                ts = None
        if parsed.path == "/api/model/frame":
            self._send_json(runtime.submit_jpeg(payload, ts))
            return

        width = self._query_int(query, "w", FACE_WIDTH)
        height = self._query_int(query, "h", FACE_HEIGHT)
        hasface = self._query_bool(query, "hasface", True)
        self._send_json(runtime.submit_face(payload, ts, width, height, hasface))

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _query_int(query, key, default):
        try:
            return int(query.get(key, [default])[0])
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _query_bool(query, key, default):
        value = query.get(key, [default])[0]
        if isinstance(value, bool):
            return value
        return str(value).lower() not in {"0", "false", "no", "off"}

    def _send_json(self, body, status=200):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_jpeg(self, data: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_mjpeg(self):
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}")
        self.end_headers()
        try:
            while True:
                frame = capture.snapshot_jpeg()
                self.wfile.write(f"--{MJPEG_BOUNDARY}\r\n".encode("ascii"))
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(1 / 15)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def log_message(self, format, *args):
        return


def main():
    server = ThreadingHTTPServer((HOST, PORT), PluginHandler)
    print(f"Open-rppg live plugin server: http://{HOST}:{PORT}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
