#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
RENCANA TUHAN STUDIO — PHOTO BOOTH KIOSK
================================================================================
Single-file web-based photo booth kiosk application.
Run: python main.py
Open browser: http://127.0.0.1:5000

REQUIRED PACKAGES:
    pip install flask opencv-python mediapipe pillow numpy qrcode[pil] pywin32 cv2-enumerate-cameras

OPTIONAL:
    pywin32 — for Windows printing (graceful fallback if not available)
    cv2-enumerate-cameras — for robust camera device name enumeration

USAGE:
    python main.py
    Then open Chrome/Edge in kiosk mode:
    chrome.exe --kiosk http://127.0.0.1:5000

ADMIN:
    Press F10 in browser to open hidden Settings panel.

FOLDER STRUCTURE (external assets, auto-created if missing):
    frames/         — PNG frame files with transparent photo holes (1623x3556 RGBA)
    photos/         — Temporary session photos (auto-cleaned)
    assets/         — Optional logo, etc.
================================================================================
"""

# ============================================================
# DEPENDENCY CHECK
# ============================================================
import sys
import types
import subprocess

# Optimization: Prevent mediapipe drawing_utils from loading heavy matplotlib (~300MB RAM)
if "matplotlib" not in sys.modules:
    _m = types.ModuleType("matplotlib")
    _m.pyplot = types.ModuleType("pyplot")
    sys.modules["matplotlib"] = _m
    sys.modules["matplotlib.pyplot"] = _m.pyplot

def check_deps():
    required = {
        "flask": "Flask",
        "cv2": "opencv-python",
        "mediapipe": "mediapipe",
        "PIL": "pillow",
        "numpy": "numpy",
        "qrcode": "qrcode[pil]",
    }
    missing = []
    for mod, pkg in required.items():
        try:
            __import__(mod)
        except ImportError as e:
            missing.append((mod, pkg, str(e)))
    if missing:
        print("=" * 64)
        print("MISSING REQUIRED PACKAGES:")
        print(f"Python Executable: {sys.executable}")
        print(f"Python Version:    {sys.version}")
        for mod, pkg, err in missing:
            print(f"  - Module '{mod}' ({pkg}): {err}")
            print(f"    Install via: py -m pip install {pkg}")
        print("=" * 64)
        sys.exit(1)

check_deps()

import os

# Suppress OpenCV MSMF/WARN noise and TensorFlow/MediaPipe C++ noise
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

# ============================================================
# IMPORTS
# ============================================================
import io
import re
import json
import math
import time
import uuid
import glob
import shutil
import base64
import threading
import traceback
import datetime
import hashlib
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

import cv2
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from flask import (Flask, Response, request, jsonify, send_file,
                   render_template_string, abort, stream_with_context)
import qrcode
import qrcode.image.pil

# Camera Enumeration (cv2-enumerate-cameras)
CV2_ENUM_AVAILABLE = False
CV2_ENUM_ERROR = ""
try:
    from cv2_enumerate_cameras import enumerate_cameras
    CV2_ENUM_AVAILABLE = True
    print("[CAMERA] cv2-enumerate-cameras available and initialized.")
except ImportError as e:
    CV2_ENUM_AVAILABLE = False
    CV2_ENUM_ERROR = str(e)
    print(f"[CAMERA] cv2-enumerate-cameras not available ({e}). Using fallback enumeration.")

# Optional: pywin32 for printing
try:
    import win32print
    import win32api
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

# MediaPipe Tasks API (v1.0+)
try:
    from mediapipe.tasks.python.vision import (
        HandLandmarker, HandLandmarkerOptions, HandLandmarkerResult, RunningMode
    )
    from mediapipe.tasks.python.vision import FaceDetector, FaceDetectorOptions
    from mediapipe.tasks.python.core.base_options import BaseOptions
    import mediapipe as mp
    MEDIAPIPE_OK = True
except ImportError as e:
    MEDIAPIPE_OK = False
    print(f"[WARN] MediaPipe import failed: {e}. Gesture detection disabled.")

# ============================================================
# CONFIG — Defaults; all overridable via F10 Settings
# ============================================================
BASE_DIR = Path(__file__).parent.resolve()
FRAMES_DIR = BASE_DIR / "frames"
PHOTOS_DIR = BASE_DIR / "photos"
ASSETS_DIR = BASE_DIR / "assets"

# Model paths
HAND_MODEL_PATH = str(BASE_DIR / "hand_landmarker.task")
FACE_MODEL_PATH = str(BASE_DIR / "blaze_face_short_range.tflite")

# ---- CAMERA ----
SELECTED_CAMERA_ID = ""       # Stable camera device identifier (e.g. 'cam_vid_0c45_pid_64d0')
CAMERA_INDEX = 0              # Internal OpenCV index fallback
CAMERA_WIDTH = 1280           # Capture width
CAMERA_HEIGHT = 720           # Capture height
CAMERA_FPS = 30               # Target FPS
CAMERA_MIRROR = True          # Horizontally flip preview
CAMERA_ROTATE = 0             # 0, 90, 180, 270 degrees

# ---- CONTROLLER & GESTURE ----
CONTROLLER_MODE = "gesture_only"  # 'gesture_only' (full hand gesture, touchpad/mouse disabled) or 'hybrid'
BLOCK_TOUCHPAD = True             # Strictly block touchpad and physical mouse on kiosk
HAND_CONFIDENCE = 0.55        # Min detection confidence
GESTURE_CONFIDENCE = 0.60     # Min gesture recognition confidence
GESTURE_SMOOTHING = 0.28      # Cursor lerp factor (0=no movement, 1=instant)
CLICK_THRESHOLD_MS = 350      # Fist must be stable N ms to trigger click
CLICK_COOLDOWN_MS = 700       # Lock after click N ms
PEACE_THRESHOLD_MS = 1200     # Peace hold duration
HAND_MIN_SIZE = 0.05          # Min hand bbox relative to frame
PRIMARY_USER_SENSITIVITY = 0.35 # How much better newcomer must be to take over
PRIMARY_USER_GRACE_MS = 2000  # Grace period before switching primary user
INFERENCE_WIDTH = 640         # Gesture inference resolution width (full-res = slow)
INFERENCE_HEIGHT = 360        # Gesture inference resolution height
GESTURE_FACE_INTERVAL = 2     # Run face detection every N gesture frames, reuse in between

# ---- PHOTO ----
COUNTDOWN_DURATION = 3        # Seconds per countdown
FLASH_DURATION_MS = 180       # Flash overlay duration
RETAKE_ENABLED = True
FRAME_SELECTION_ENABLED = True
PHOTO_JPEG_QUALITY = 92       # Capture JPEG quality

# ---- PAYMENT ----
# Payment mode used by DEFAULT.
#   'manual'   -> static QRIS image (assets/qris.png) + operator confirms with F9 x3. No gateway/internet.
#   'demo'     -> simulated demo payment (legacy, for dev/testing).
#   'midtrans' -> legacy Midtrans QRIS gateway (requires credentials/internet).
PAYMENT_MODE = "manual"
DEMO_MODE = False             # Legacy demo toggle kept for the demo provider path
PHOTOBOOTH_PRICE = 25000      # Price in IDR (Rp)
PAYMENT_TIMEOUT_SEC = 300     # Payment expiry in seconds
PAYMENT_POLL_INTERVAL = 2.5   # Seconds between status polls

# ---- MIDTRANS ----
MIDTRANS_SERVER_KEY = ""
MIDTRANS_CLIENT_KEY = ""
MIDTRANS_IS_PRODUCTION = False
MIDTRANS_BASE_URL = "https://api.sandbox.midtrans.com" if not MIDTRANS_IS_PRODUCTION else "https://api.midtrans.com"

# ---- PRINTER ----
PRINTER_NAME = ""             # Empty string = system default printer
PRINT_WIDTH_MM = 100          # 4R paper width
PRINT_HEIGHT_MM = 150         # 4R paper height
PRINT_DPI = 300               # Print resolution
AUTO_PRINT = False            # Auto-print after compositing without confirmation
PRINT_FALLBACK_SEC = 60       # After this many seconds in PRINTING, show Back/Next escape

# ---- SYSTEM ----
AUTO_RESET_TIMEOUT = 120      # Idle seconds before auto-returning to landing
SESSION_CLEANUP_MIN = 60      # Delete temporary photos older than N minutes
DEBUG_MODE = False            # Show verbose debug logs & raw indexes in settings
DARK_MODE = False             # Visual theme: False = light (studio), True = dark
SHOW_FPS = False              # Show FPS in stream
SHOW_LANDMARKS = False        # Overlay hand landmarks
SHOW_BOUNDING_BOX = False     # Overlay primary user face box
SHOW_GESTURE_LABEL = False    # Show recognized gesture text on camera preview

# Frame dimensions (matching standard kiosk format)
FRAME_WIDTH = 1623
FRAME_HEIGHT = 3556

# Frame slot definitions (Blue=top, Red=mid, Green=bottom)
FRAME_PRESETS = {
    "default_3slot": {
        "width": FRAME_WIDTH,
        "height": FRAME_HEIGHT,
        "slots": [
            {"x": 40, "y": 36,   "w": 1543, "h": 1060},  # Top
            {"x": 40, "y": 1248, "w": 1543, "h": 1060},  # Middle
            {"x": 40, "y": 2460, "w": 1543, "h": 1060},  # Bottom
        ]
    }
}

PHOTO_FIT_MODE = "cover"
ADMIN_PIN = ""

# ============================================================
# GLOBALS / STATE
# ============================================================
app = Flask(__name__)
app.secret_key = os.urandom(32)

config = {
    "selected_camera_id": SELECTED_CAMERA_ID,
    "camera_index": CAMERA_INDEX,
    "camera_width": CAMERA_WIDTH,
    "camera_height": CAMERA_HEIGHT,
    "camera_fps": CAMERA_FPS,
    "camera_mirror": CAMERA_MIRROR,
    "camera_rotate": CAMERA_ROTATE,
    "hand_confidence": HAND_CONFIDENCE,
    "gesture_confidence": GESTURE_CONFIDENCE,
    "gesture_smoothing": GESTURE_SMOOTHING,
    "click_threshold_ms": CLICK_THRESHOLD_MS,
    "click_cooldown_ms": CLICK_COOLDOWN_MS,
    "peace_threshold_ms": PEACE_THRESHOLD_MS,
    "hand_min_size": HAND_MIN_SIZE,
    "primary_user_sensitivity": PRIMARY_USER_SENSITIVITY,
    "primary_user_grace_ms": PRIMARY_USER_GRACE_MS,
    "inference_width": INFERENCE_WIDTH,
    "inference_height": INFERENCE_HEIGHT,
    "gesture_face_interval": GESTURE_FACE_INTERVAL,
    "countdown_duration": COUNTDOWN_DURATION,
    "flash_duration_ms": FLASH_DURATION_MS,
    "retake_enabled": RETAKE_ENABLED,
    "frame_selection_enabled": FRAME_SELECTION_ENABLED,
    "photo_jpeg_quality": PHOTO_JPEG_QUALITY,
    "demo_mode": DEMO_MODE,
    "payment_mode": PAYMENT_MODE,
    "photobooth_price": PHOTOBOOTH_PRICE,
    "payment_timeout_sec": PAYMENT_TIMEOUT_SEC,
    "printer_name": PRINTER_NAME,
    "print_width_mm": PRINT_WIDTH_MM,
    "print_height_mm": PRINT_HEIGHT_MM,
    "print_dpi": PRINT_DPI,
    "auto_print": AUTO_PRINT,
    "print_fallback_sec": PRINT_FALLBACK_SEC,
    "dark_mode": DARK_MODE,
    "show_fps": SHOW_FPS,
    "show_landmarks": SHOW_LANDMARKS,
    "show_bounding_box": SHOW_BOUNDING_BOX,
    "show_gesture_label": SHOW_GESTURE_LABEL,
    "debug_mode": DEBUG_MODE,
    "auto_reset_timeout": AUTO_RESET_TIMEOUT,
    "controller_mode": CONTROLLER_MODE,
    "block_touchpad": BLOCK_TOUCHPAD,
}

# ============================================================
# SESSION STATE MACHINE
# ============================================================
class SessionState:
    LANDING = "landing"
    CAMERA = "camera"
    REVIEW = "review"
    PAYMENT = "payment"
    FRAMES = "frames"
    COMPOSITING = "compositing"
    QR = "qr"
    PRINTING = "printing"
    SUCCESS = "success"

class AppSession:
    def __init__(self):
        self.session_id: str = ""
        self.state: str = SessionState.LANDING
        self.photos: List[str] = []             # Captured photo file paths
        self.selected_frame: Optional[str] = None
        self.final_image_path: Optional[str] = None
        self.payment_state: str = "idle"        # idle, creating, pending, success, failed, expired
        self.payment_order_id: Optional[str] = None
        self.payment_transaction_id: Optional[str] = None
        self.payment_qr_data: Optional[str] = None
        self.payment_amount: int = PHOTOBOOTH_PRICE
        self.payment_expiry: Optional[float] = None
        self.print_state: str = "idle"          # idle, printing, success, failed, cancelled
        self.print_started_mono: Optional[float] = None  # monotonic timestamp when printing started
        self.last_activity: float = time.time()
        self.countdown_active: bool = False
        self.countdown_value: int = 0
        self._payment_stop_event: Optional[threading.Event] = None
        self.reset()

    def reset(self):
        self.session_id = f"RTS_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6].upper()}"
        self.state = SessionState.LANDING
        self.photos = []
        self.selected_frame = None
        self.final_image_path = None
        self.payment_state = "idle"
        self.payment_order_id = None
        self.payment_transaction_id = None
        self.payment_qr_data = None
        self.payment_amount = config["photobooth_price"]
        self.payment_expiry = None
        self.print_state = "idle"
        self.print_started_mono = None
        self.last_activity = time.time()
        self.countdown_active = False
        self.countdown_value = 0
        if self._payment_stop_event:
            self._payment_stop_event.set()
            self._payment_stop_event = None

    def touch(self):
        self.last_activity = time.time()

    def idle_seconds(self):
        return time.time() - self.last_activity

    @property
    def photo_paths(self):
        return self.photos

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "state": self.state,
            "photo_count": len(self.photos),
            "photos_needed": 3,
            "selected_frame": self.selected_frame,
            "final_image_path": self.final_image_path,
            "payment_state": self.payment_state,
            "payment_order_id": self.payment_order_id,
            "payment_amount": self.payment_amount,
            "payment_expiry": self.payment_expiry,
            "print_state": self.print_state,
            "print_elapsed_sec": (
                round(time.monotonic() - self.print_started_mono, 1)
                if self.print_started_mono is not None else 0.0
            ),
            "print_fallback_sec": config["print_fallback_sec"],
            "countdown_active": self.countdown_active,
            "countdown_value": self.countdown_value,
            "photo_slots_filled": len(self.photos),
        }

session = AppSession()
session_lock = threading.Lock()

# ============================================================
# GESTURE STATE
# ============================================================
class GestureState:
    def __init__(self):
        self.current_gesture = "none"  # none, unknown, palm, fist, peace
        self.cursor_x = 0.5
        self.cursor_y = 0.5
        self.cursor_raw_x = 0.5
        self.cursor_raw_y = 0.5
        self.fist_start: Optional[float] = None
        self.peace_start: Optional[float] = None
        self.last_click_time: float = 0
        self.primary_user_locked = False
        self.primary_face_bbox: Optional[Tuple] = None  # (x,y,w,h) normalized
        self.primary_last_seen: float = 0
        self.subject_score: float = 0.0
        self.hand_detected = False
        self.hand_valid_for_primary = False
        self.hand_bbox: Optional[Tuple] = None  # normalized
        self.fps_counter = deque(maxlen=30)
        self.frame_guidance = ""  # "STEP BACK", "MOVE LEFT", etc.
        self.peace_progress = 0.0  # 0.0–1.0
        self.last_error: Optional[str] = None
        self.inference_fps = 0.0
        self.inference_ms = 0.0
        self.loop_fps = 0.0
        self.face_count = 0
        self.admin_mode = False

gesture_state = GestureState()
gesture_lock = threading.Lock()

# ============================================================
# CAMERA DEVICE MANAGER
# ============================================================
class CameraDeviceInfo:
    def __init__(self, device_id: str, name: str, native_index: int, backend_name: str,
                 is_builtin: bool = False, vid: Optional[int] = None, pid: Optional[int] = None,
                 path: str = "", dshow_index: Optional[int] = None, msmf_index: Optional[int] = None,
                 device_type: str = "external"):
        self.device_id = device_id
        self.name = name
        self.native_index = native_index
        self.backend_name = backend_name
        self.is_builtin = is_builtin
        self.vid = vid
        self.pid = pid
        self.path = path
        self.dshow_index = dshow_index
        self.msmf_index = msmf_index
        self.device_type = device_type
        self.connected = True
        self.ready = False
        self.actual_width = 0
        self.actual_height = 0
        self.actual_fps = 0.0
        self.last_open_descriptor: Optional[Dict[str, Any]] = None

    def to_dict(self, selected: bool = False, debug: bool = False) -> dict:
        desc = ("Built-in laptop camera" if self.device_type == "built-in" else
                ("Virtual camera" if self.device_type == "virtual" else "External USB webcam"))
        d = {
            "id": self.device_id,
            "name": self.name,
            "type": self.device_type,
            "description": desc,
            "connected": self.connected,
            "ready": self.ready,
            "selected": selected,
            "backend": self.backend_name,
            "resolution": f"{self.actual_width}x{self.actual_height}" if self.actual_width else "1280x720",
            "fps": round(self.actual_fps, 1) if self.actual_fps else 30.0,
        }
        if debug:
            d["native_index"] = self.native_index
            d["dshow_index"] = self.dshow_index
            d["msmf_index"] = self.msmf_index
            d["vid"] = f"0x{self.vid:04x}" if self.vid is not None else None
            d["pid"] = f"0x{self.pid:04x}" if self.pid is not None else None
            d["path"] = self.path
            d["last_open_descriptor"] = self.last_open_descriptor
        return d


class CameraDeviceManager:
    """
    Manages discovery, device identity, friendly names, backend selection,
    and hot-plugging of cameras.
    Does NOT open VideoCapture directly when CameraEngine is active.
    """
    def __init__(self):
        self._devices: Dict[str, CameraDeviceInfo] = {}
        self._lock = threading.RLock()
        self.selected_device_id: Optional[str] = None
        self._last_refresh_time: float = 0.0
        self.refresh_devices()

    def enumerate_devices(self) -> List[CameraDeviceInfo]:
        """
        Discovers all available video capture devices using cv2_enumerate_cameras.
        Extracts native backend indexes (DSHOW, MSMF) cleanly without index arithmetic.
        """
        found: List[CameraDeviceInfo] = []

        if CV2_ENUM_AVAILABLE:
            try:
                dshow_cams = []
                msmf_cams = []
                try:
                    dshow_cams = list(enumerate_cameras(cv2.CAP_DSHOW))
                except Exception as ex_ds:
                    if config.get("debug_mode"):
                        print(f"[CAMERA] DSHOW enum error: {ex_ds}")
                try:
                    msmf_cams = list(enumerate_cameras(cv2.CAP_MSMF))
                except Exception as ex_ms:
                    if config.get("debug_mode"):
                        print(f"[CAMERA] MSMF enum error: {ex_ms}")

                # Group by physical device
                grouped: Dict[str, Dict[str, Any]] = {}

                for c in dshow_cams:
                    key = self._get_device_key(c.vid, c.pid, c.path, c.name)
                    if key not in grouped:
                        grouped[key] = {
                            "name": c.name, "vid": c.vid, "pid": c.pid, "path": c.path,
                            "dshow_idx": c.index, "msmf_idx": None
                        }
                    else:
                        grouped[key]["dshow_idx"] = c.index
                        if not grouped[key]["vid"] and c.vid:
                            grouped[key]["vid"] = c.vid
                        if not grouped[key]["pid"] and c.pid:
                            grouped[key]["pid"] = c.pid
                        if not grouped[key]["path"] and c.path:
                            grouped[key]["path"] = c.path

                for c in msmf_cams:
                    key = self._get_device_key(c.vid, c.pid, c.path, c.name)
                    if key not in grouped:
                        grouped[key] = {
                            "name": c.name, "vid": c.vid, "pid": c.pid, "path": c.path,
                            "dshow_idx": None, "msmf_idx": c.index
                        }
                    else:
                        grouped[key]["msmf_idx"] = c.index
                        if not grouped[key]["vid"] and c.vid:
                            grouped[key]["vid"] = c.vid
                        if not grouped[key]["pid"] and c.pid:
                            grouped[key]["pid"] = c.pid
                        if not grouped[key]["path"] and c.path:
                            grouped[key]["path"] = c.path

                for key, g in grouped.items():
                    name = g["name"]
                    name_lower = name.lower()
                    is_virt = ("obs" in name_lower or "virtual" in name_lower or "vmulticlient" in name_lower)
                    is_built = (not is_virt and any(kw in name_lower for kw in [
                        "integrated", "built-in", "builtin", "internal", "laptop",
                        "hd user facing", "user facing", "facetime", "webcam"
                    ]))
                    dev_type = "virtual" if is_virt else ("built-in" if is_built else "external")

                    # Primary backend choice: prefer DirectShow on Windows if available
                    if g["dshow_idx"] is not None:
                        bname = "DirectShow"
                        native_idx = g["dshow_idx"]
                    elif g["msmf_idx"] is not None:
                        bname = "MSMF"
                        native_idx = g["msmf_idx"]
                    else:
                        bname = "Auto"
                        native_idx = 0

                    dev_id = f"cam_{key}"
                    info = CameraDeviceInfo(
                        device_id=dev_id,
                        name=name,
                        native_index=native_idx,
                        backend_name=bname,
                        is_builtin=is_built,
                        vid=g["vid"],
                        pid=g["pid"],
                        path=g["path"] or "",
                        dshow_index=g["dshow_idx"],
                        msmf_index=g["msmf_idx"],
                        device_type=dev_type
                    )
                    found.append(info)
            except Exception as e:
                print(f"[CAMERA] Enumeration error: {e}. Falling back.")

        # Fallback if no devices found via cv2-enumerate-cameras
        if not found:
            # If camera is already open and running, do NOT probe to avoid disrupting the stream
            if "camera" in globals() and camera.running and camera.cap is not None:
                if camera.device_id in self._devices:
                    found.append(self._devices[camera.device_id])
                else:
                    found.append(CameraDeviceInfo(
                        device_id="cam_default",
                        name=camera.device_name or "Integrated Camera",
                        native_index=0,
                        backend_name=camera.backend or "DirectShow",
                        is_builtin=True,
                        dshow_index=0,
                        device_type="built-in"
                    ))
            else:
                # Safe probe of index 0
                cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
                if cap.isOpened():
                    cap.release()
                    found.append(CameraDeviceInfo(
                        device_id="cam_idx_0",
                        name="Integrated Camera",
                        native_index=0,
                        backend_name="DirectShow",
                        is_builtin=True,
                        dshow_index=0,
                        device_type="built-in"
                    ))
                else:
                    cap = cv2.VideoCapture(0)
                    if cap.isOpened():
                        cap.release()
                        found.append(CameraDeviceInfo(
                            device_id="cam_idx_0",
                            name="Webcam 0",
                            native_index=0,
                            backend_name="Auto",
                            is_builtin=True,
                            device_type="built-in"
                        ))

        def sort_key(d: CameraDeviceInfo):
            if d.is_builtin:
                return 0
            if d.device_type == "virtual":
                return 2
            return 1

        found.sort(key=sort_key)
        return found

    def _get_device_key(self, vid, pid, path, name):
        if vid and pid:
            return f"vid_{vid:04x}_pid_{pid:04x}"
        if path:
            m = re.search(r'vid_([0-9a-f]{4})&pid_([0-9a-f]{4})', path, re.IGNORECASE)
            if m:
                return f"vid_{m.group(1).lower()}_pid_{m.group(2).lower()}"
            base = re.sub(r'#\{[0-9a-f\-]{36}\}.*$', '', path, flags=re.IGNORECASE)
            return f"path_{hashlib.md5(base.encode()).hexdigest()[:10]}"
        return f"name_{hashlib.md5(name.encode()).hexdigest()[:10]}"

    def refresh_devices(self):
        with self._lock:
            new_list = self.enumerate_devices()
            new_map = {d.device_id: d for d in new_list}

            for dev_id, dev in new_map.items():
                if dev_id in self._devices:
                    dev.ready = self._devices[dev_id].ready
                    dev.actual_width = self._devices[dev_id].actual_width
                    dev.actual_height = self._devices[dev_id].actual_height
                    dev.actual_fps = self._devices[dev_id].actual_fps
                    dev.last_open_descriptor = self._devices[dev_id].last_open_descriptor

            self._devices = new_map
            self._last_refresh_time = time.time()

            if not self.selected_device_id or self.selected_device_id not in self._devices:
                def_dev = self.get_default_device()
                if def_dev:
                    self.selected_device_id = def_dev.device_id
                    config["selected_camera_id"] = def_dev.device_id
                    config["camera_index"] = def_dev.native_index

    def get_devices(self) -> List[CameraDeviceInfo]:
        with self._lock:
            return list(self._devices.values())

    def get_default_device(self) -> Optional[CameraDeviceInfo]:
        devices = self.get_devices()
        if not devices:
            return None
        for d in devices:
            if d.is_builtin:
                return d
        for d in devices:
            if d.device_type != "virtual":
                return d
        return devices[0]

    def select_device(self, device_id: str) -> bool:
        with self._lock:
            if device_id in self._devices:
                self.selected_device_id = device_id
                config["selected_camera_id"] = device_id
                config["camera_index"] = self._devices[device_id].native_index
                return True
            return False

    def find_device(self, device_id: str) -> Optional[CameraDeviceInfo]:
        with self._lock:
            return self._devices.get(device_id)

    def is_device_connected(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._devices


camera_device_manager = CameraDeviceManager()

# ============================================================
# CAMERA ENGINE
# ============================================================
class CameraEngine:
    FORMAT_CANDIDATES = [
        (1280, 720, 30),
        (1280, 720, 24),
        (1280, 720, 15),
        (1920, 1080, 30),
        (1920, 1080, 24),
        (640, 480, 30),
    ]
    RECONNECT_DELAYS = [0.25, 0.5, 1.0, 2.0, 5.0]

    def __init__(self):
        self.cap: Optional[cv2.VideoCapture] = None
        self.running = False
        self.ready = False
        self.camera_status = "DISCONNECTED"  # CONNECTED, DEGRADED, RECONNECTING, DISCONNECTED
        self.frame: Optional[np.ndarray] = None
        self.frame_sequence: int = 0
        self.latest_frame_timestamp: float = 0.0
        self.frame_lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None
        self.fps = 0.0
        self.actual_width = 0
        self.actual_height = 0
        self.backend = "DirectShow"
        self.device_id: str = ""
        self.device_name: str = "Unknown"
        self.consecutive_failures = 0
        self.reconnect_attempts = 0
        self.last_successful_frame_time = 0.0
        self._logged_unhealthy = False
        self._backoff_idx = 0

    def start(self):
        if self.running:
            return
        self.error = None
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="CameraEngine")
        self.thread.start()

    def stop(self):
        self.running = False
        self.ready = False
        self.camera_status = "DISCONNECTED"
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.5)
        self._release_cap()
        with self.frame_lock:
            self.frame = None
        print("[CAMERA] Camera engine stopped cleanly")

    def restart(self):
        self.stop()
        time.sleep(0.3)
        self.start()

    def _release_cap(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception as e:
                if config.get("debug_mode"):
                    print(f"[CAMERA DEBUG] Cap release exception: {e}")
            self.cap = None

    def _open_camera(self, target_dev_id: Optional[str] = None) -> bool:
        if not target_dev_id:
            target_dev_id = camera_device_manager.selected_device_id
        
        dev = camera_device_manager.find_device(target_dev_id) if target_dev_id else None
        if not dev:
            dev = camera_device_manager.get_default_device()

        if not dev:
            self.error = "No camera devices found."
            self.camera_status = "DISCONNECTED"
            self.ready = False
            return False

        self.device_id = dev.device_id
        self.device_name = dev.name

        # Build candidate opening methods: (native_index, backend_enum, backend_display_name)
        open_candidates = []

        # 1. Reuse last known good open descriptor first
        if dev.last_open_descriptor:
            d = dev.last_open_descriptor
            open_candidates.append((d["index"], d["backend"], d["backend_name"]))

        # 2. DirectShow native candidate
        if dev.dshow_index is not None:
            c_dshow = (dev.dshow_index, cv2.CAP_DSHOW, "DirectShow")
            if c_dshow not in open_candidates:
                open_candidates.append(c_dshow)

        # 3. MSMF native candidate
        if dev.msmf_index is not None:
            c_msmf = (dev.msmf_index, cv2.CAP_MSMF, "MSMF")
            if c_msmf not in open_candidates:
                open_candidates.append(c_msmf)

        # 4. CAP_ANY fallback
        c_any = (dev.native_index if dev.native_index is not None else 0, cv2.CAP_ANY, "Auto")
        if c_any not in open_candidates:
            open_candidates.append(c_any)

        # Try candidates and validate with real frames
        for cand_idx, cand_backend, cand_bname in open_candidates:
            for req_w, req_h, req_fps in self.FORMAT_CANDIDATES:
                cap = None
                try:
                    cap = cv2.VideoCapture(cand_idx, cand_backend)
                    if not cap.isOpened():
                        if cap:
                            cap.release()
                        break  # Backend rejected device index, proceed to next candidate

                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, req_w)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, req_h)
                    cap.set(cv2.CAP_PROP_FPS, req_fps)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                    # Validate by capturing several consecutive real frames
                    valid_frames = 0
                    test_frame = None
                    for _ in range(5):
                        ret, f = cap.read()
                        if ret and f is not None and f.size > 0 and f.shape[0] >= 240 and f.shape[1] >= 320:
                            valid_frames += 1
                            test_frame = f
                        time.sleep(0.015)

                    if valid_frames >= 4 and test_frame is not None:
                        self.cap = cap
                        self.actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or test_frame.shape[1]
                        self.actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or test_frame.shape[0]
                        act_fps = cap.get(cv2.CAP_PROP_FPS)
                        self.fps = act_fps if (act_fps and 5.0 <= act_fps <= 120.0) else float(req_fps)
                        self.backend = cand_bname
                        self.camera_status = "CONNECTED"
                        self.ready = True
                        self.error = None
                        self.consecutive_failures = 0
                        self._backoff_idx = 0

                        # Update device descriptor
                        dev.ready = True
                        dev.actual_width = self.actual_width
                        dev.actual_height = self.actual_height
                        dev.actual_fps = self.fps
                        dev.last_open_descriptor = {
                            "index": cand_idx,
                            "backend": cand_backend,
                            "backend_name": cand_bname
                        }
                        print(f"[CAMERA] Connected: {dev.name} ({self.actual_width}x{self.actual_height} @ {self.fps:.0f} FPS, {self.backend})")
                        return True
                    else:
                        cap.release()
                except Exception as ex:
                    if cap:
                        try:
                            cap.release()
                        except Exception:
                            pass
                    if config.get("debug_mode"):
                        print(f"[CAMERA DEBUG] Candidate probe error ({cand_bname}:{cand_idx}): {ex}")

        self.error = f"Camera '{dev.name}' failed format and frame capture validation."
        self.camera_status = "DISCONNECTED"
        self.ready = False
        return False

    def switch_device(self, target_dev_id: str) -> bool:
        prev_id = self.device_id
        self.stop()
        time.sleep(0.3)
        if self._open_camera(target_dev_id):
            self.start()
            return True
        else:
            print(f"[CAMERA] Failed opening {target_dev_id}, reverting to {prev_id}")
            self._open_camera(prev_id)
            self.start()
            return False

    def test_active_camera(self) -> Tuple[bool, str]:
        if not self.running:
            return False, "Camera engine is not running"
        if not self.cap or not self.cap.isOpened() or self.camera_status != "CONNECTED":
            return False, f"FAIL: Camera '{self.device_name}' is not connected ({self.camera_status})"
        
        valid = 0
        for _ in range(5):
            f = self.get_frame()
            if f is not None and f.size > 0 and f.shape[0] >= 240:
                valid += 1
            time.sleep(0.033)

        if valid >= 4:
            return True, f"PASS: {self.device_name} ({self.actual_width}x{self.actual_height} @ {self.fps:.0f} FPS, {self.backend}) — {valid}/5 valid frames"
        else:
            return False, f"FAIL: {self.device_name} only delivered {valid}/5 valid frames"

    def _run(self):
        if self.cap is None or not self.cap.isOpened():
            if not self._open_camera():
                print(f"[CAMERA] Initial open failed: {self.error}")

        frame_times = deque(maxlen=30)
        while self.running:
            if not self.cap or not self.cap.isOpened():
                self.camera_status = "RECONNECTING"
                self.ready = False
                delay = self.RECONNECT_DELAYS[min(self._backoff_idx, len(self.RECONNECT_DELAYS) - 1)]
                time.sleep(delay)
                self._backoff_idx = min(self._backoff_idx + 1, len(self.RECONNECT_DELAYS) - 1)
                self.reconnect_attempts += 1

                if self._open_camera(self.device_id):
                    self.camera_status = "CONNECTED"
                    self._backoff_idx = 0
                    self._logged_unhealthy = False
                    print(f"[CAMERA] Camera recovered: {self.device_name}")
                else:
                    camera_device_manager.refresh_devices()
                    dev = camera_device_manager.find_device(self.device_id)
                    if not dev:
                        self.camera_status = "DISCONNECTED"
                        self.error = f"Camera '{self.device_name}' disconnected."
                continue

            ret, frame = self.cap.read()
            if not ret or frame is None or frame.size == 0 or frame.shape[0] < 100:
                self.consecutive_failures += 1
                if self.consecutive_failures <= 2:
                    time.sleep(0.015)
                    continue
                elif self.consecutive_failures in (3, 4):
                    self.camera_status = "DEGRADED"
                    self.ready = False
                    if not self._logged_unhealthy:
                        print(f"[CAMERA] Stream degraded: frame read failed ({self.device_name})")
                        self._logged_unhealthy = True
                    time.sleep(0.025)
                    continue
                else:
                    print(f"[CAMERA] {self.consecutive_failures} consecutive failures, reconnecting {self.device_name}...")
                    self.camera_status = "RECONNECTING"
                    self.ready = False
                    self._release_cap()
                    self._backoff_idx = 0
                    continue

            if self.consecutive_failures > 0:
                if self.consecutive_failures >= 3:
                    print(f"[CAMERA] Camera recovered: {self.device_name}")
                self.consecutive_failures = 0
                self._logged_unhealthy = False
                self._backoff_idx = 0

            self.camera_status = "CONNECTED"
            self.ready = True
            self.error = None
            now = time.time()
            self.last_successful_frame_time = now

            rot = config["camera_rotate"]
            if rot == 90:
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
            elif rot == 180:
                frame = cv2.rotate(frame, cv2.ROTATE_180)
            elif rot == 270:
                frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

            if config["camera_mirror"]:
                frame = cv2.flip(frame, 1)

            with self.frame_lock:
                self.frame = frame.copy()
                self.frame_sequence += 1
                self.latest_frame_timestamp = now

            now_perf = time.perf_counter()
            frame_times.append(now_perf)
            if len(frame_times) >= 2:
                self.fps = (len(frame_times) - 1) / (frame_times[-1] - frame_times[0])

        self._release_cap()

    def get_frame(self) -> Optional[np.ndarray]:
        with self.frame_lock:
            return self.frame.copy() if self.frame is not None else None

    def capture_hires(self) -> Optional[np.ndarray]:
        if not self.is_ok:
            return None
        return self.get_frame()

    @property
    def is_ok(self) -> bool:
        return self.running and self.ready and self.cap is not None and self.frame is not None and self.camera_status == "CONNECTED"

camera = CameraEngine()

# ============================================================
# MEDIAPIPE GESTURE ENGINE
# ============================================================
class GestureEngine:
    def __init__(self):
        self.hand_landmarker: Optional[Any] = None
        self.face_detector: Optional[Any] = None
        self.ready = False
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self._fps_times = deque(maxlen=30)
        self._last_frame_seq: int = -1
        self._face_skip_counter: int = 0
        self._last_primary_face = None
        self._last_face_count: int = 0

    def init(self):
        if not MEDIAPIPE_OK:
            print("[GESTURE] MediaPipe not available. Gesture detection disabled.")
            return False
        try:
            if not os.path.exists(HAND_MODEL_PATH):
                print(f"[GESTURE] Downloading hand model to {HAND_MODEL_PATH}...")
                self._download_model(
                    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
                    HAND_MODEL_PATH
                )

            if os.path.exists(HAND_MODEL_PATH):
                opts = HandLandmarkerOptions(
                    base_options=BaseOptions(model_asset_path=HAND_MODEL_PATH),
                    running_mode=RunningMode.IMAGE,
                    num_hands=2,
                    min_hand_detection_confidence=config["hand_confidence"],
                    min_hand_presence_confidence=config["hand_confidence"],
                    min_tracking_confidence=0.5,
                )
                self.hand_landmarker = HandLandmarker.create_from_options(opts)
                print("[GESTURE] Hand landmarker initialized")

            if not os.path.exists(FACE_MODEL_PATH):
                print(f"[GESTURE] Downloading face model to {FACE_MODEL_PATH}...")
                self._download_model(
                    "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
                    FACE_MODEL_PATH
                )

            if os.path.exists(FACE_MODEL_PATH):
                face_opts = FaceDetectorOptions(
                    base_options=BaseOptions(model_asset_path=FACE_MODEL_PATH),
                    running_mode=RunningMode.IMAGE,
                    min_detection_confidence=0.5,
                )
                self.face_detector = FaceDetector.create_from_options(face_opts)
                print("[GESTURE] Face detector initialized")

            self.ready = (self.hand_landmarker is not None)
            return self.ready
        except Exception as e:
            err_msg = f"GestureEngine init failed: {e}"
            print(f"[ERROR] {err_msg}")
            with gesture_lock:
                gesture_state.last_error = err_msg
            return False

    def _download_model(self, url, path):
        try:
            import urllib.request
            urllib.request.urlretrieve(url, path)
            print(f"[GESTURE] Saved model to {path}")
        except Exception as e:
            print(f"[ERROR] Model download failed: {e}")

    def start(self):
        if not self.ready:
            self.init()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="GestureEngine")
        self.thread.start()
        print("[GESTURE] Gesture engine started")

    def stop(self):
        self.running = False

    def _run(self):
        while self.running:
            if not camera.is_ok:
                with gesture_lock:
                    gesture_state.hand_detected = False
                    gesture_state.hand_valid_for_primary = False
                    gesture_state.current_gesture = "none"
                    gesture_state.fist_start = None
                    gesture_state.peace_start = None
                    gesture_state.peace_progress = 0.0
                time.sleep(0.05)
                continue

            frame = camera.get_frame()
            if frame is None:
                time.sleep(0.005)
                continue

            # Only process the latest frame; skip immediately if it is stale.
            with camera.frame_lock:
                seq = camera.frame_sequence
            if seq == self._last_frame_seq:
                time.sleep(0.001)
                continue
            self._last_frame_seq = seq

            t0 = time.perf_counter()
            try:
                self._process_frame(frame)
                with gesture_lock:
                    gesture_state.last_error = None
            except Exception as e:
                err = str(e)
                with gesture_lock:
                    gesture_state.last_error = err
                if config["debug_mode"]:
                    print(f"[GESTURE ERROR] {err}")

            t1 = time.perf_counter()
            infer_ms = (t1 - t0) * 1000.0
            self._fps_times.append(t1)
            if len(self._fps_times) >= 2:
                with gesture_lock:
                    fps = (len(self._fps_times) - 1) / (self._fps_times[-1] - self._fps_times[0])
                    gesture_state.inference_fps = fps
                    gesture_state.inference_ms = infer_ms
                    gesture_state.loop_fps = fps

            # No fixed sleep: loop throttles naturally via inference cost + latest-frame gate.
            time.sleep(0.001)

    def _process_frame(self, frame: np.ndarray):
        h, w = frame.shape[:2]
        det_w = int(config["inference_width"])
        det_h = int(config["inference_height"])

        # Single downscale for ALL inference (hand + face). Preview stays full-res.
        if (w, h) != (det_w, det_h):
            small = cv2.resize(frame, (det_w, det_h), interpolation=cv2.INTER_AREA)
        else:
            small = frame
        rgb_small = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        mp_image_small = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_small)

        # Face detection is throttled (every N frames) and last result reused.
        primary_face = self._last_primary_face
        face_count = self._last_face_count
        self._face_skip_counter += 1
        face_interval = max(1, int(config["gesture_face_interval"]))
        if self.face_detector and self._face_skip_counter % face_interval == 0:
            try:
                face_result = self.face_detector.detect(mp_image_small)
                if face_result and face_result.detections:
                    face_count = len(face_result.detections)
                    self._last_face_count = face_count
                    primary_face = self._score_primary_face(face_result.detections, det_w, det_h)
                    self._last_primary_face = primary_face
                else:
                    face_count = 0
                    self._last_face_count = 0
                    primary_face = None
                    self._last_primary_face = None
            except Exception as e:
                with gesture_lock:
                    gesture_state.last_error = f"Face detection error: {e}"

        hand_result = None
        if self.hand_landmarker:
            try:
                hand_result = self.hand_landmarker.detect(mp_image_small)
            except Exception as e:
                with gesture_lock:
                    gesture_state.last_error = f"Hand detection error: {e}"

        self._update_state(hand_result, primary_face, face_count, w, h)

    def _score_primary_face(self, detections, det_w, det_h):
        best = None
        best_score = -1.0
        for det in detections:
            bb = det.bounding_box
            x = bb.origin_x / det_w
            y = bb.origin_y / det_h
            bw = bb.width / det_w
            bh = bb.height / det_h
            area = bw * bh
            cx = x + bw / 2
            cy = y + bh / 2

            # Area score: larger faces are closer
            area_score = min(1.0, area / 0.12)
            # Center proximity score
            center_dist = math.sqrt((cx - 0.5)**2 + (cy - 0.5)**2)
            center_score = max(0.0, 1.0 - 2.0 * center_dist)

            score = 0.55 * area_score + 0.45 * center_score
            if score > best_score:
                best_score = score
                best = (x, y, bw, bh, score)
        return best

    def _update_state(self, hand_result, primary_face, face_count, frame_w, frame_h):
        with gesture_lock:
            now = time.time()
            smth = config["gesture_smoothing"]
            gesture_state.face_count = face_count

            # Primary user locking & grace period
            if primary_face:
                px, py, pw, ph, pscore = primary_face
                prev_locked = gesture_state.primary_user_locked
                if not prev_locked:
                    if pscore > 0.25:
                        gesture_state.primary_face_bbox = (px, py, pw, ph)
                        gesture_state.primary_user_locked = True
                        gesture_state.subject_score = pscore
                        gesture_state.primary_last_seen = now
                else:
                    ex, ey, ew, eh = gesture_state.primary_face_bbox or (0, 0, 0, 0)
                    dist = math.sqrt((px + pw/2 - (ex + ew/2))**2 + (py + ph/2 - (ey + eh/2))**2)
                    if dist < 0.20:
                        # Same person
                        gesture_state.primary_face_bbox = (px, py, pw, ph)
                        gesture_state.primary_last_seen = now
                        gesture_state.subject_score = max(gesture_state.subject_score, pscore)
                    else:
                        # Check hysteresis
                        takeover_thresh = gesture_state.subject_score * (1.0 + config["primary_user_sensitivity"])
                        if pscore > takeover_thresh:
                            gesture_state.primary_face_bbox = (px, py, pw, ph)
                            gesture_state.primary_last_seen = now
                            gesture_state.subject_score = pscore
            else:
                if gesture_state.primary_user_locked:
                    grace_sec = config["primary_user_grace_ms"] / 1000.0
                    if now - gesture_state.primary_last_seen > grace_sec:
                        gesture_state.primary_user_locked = False
                        gesture_state.primary_face_bbox = None
                        gesture_state.subject_score = 0.0

            # Framing guidance
            if gesture_state.primary_face_bbox:
                gesture_state.frame_guidance = self._get_framing_guidance(gesture_state.primary_face_bbox)
            else:
                gesture_state.frame_guidance = ""

            # Hand processing
            if not hand_result or not hand_result.hand_landmarks:
                gesture_state.hand_detected = False
                gesture_state.hand_valid_for_primary = False
                gesture_state.current_gesture = "none"
                gesture_state.fist_start = None
                gesture_state.peace_start = None
                gesture_state.peace_progress = 0.0
                return

            best_hand = self._associate_hand_with_primary(hand_result)
            if best_hand is None:
                gesture_state.hand_detected = False
                gesture_state.hand_valid_for_primary = False
                gesture_state.current_gesture = "none"
                gesture_state.fist_start = None
                gesture_state.peace_start = None
                gesture_state.peace_progress = 0.0
                return

            landmarks = best_hand
            gesture_state.hand_detected = True
            gesture_state.hand_valid_for_primary = True

            xs = [lm.x for lm in landmarks]
            ys = [lm.y for lm in landmarks]
            hx_min, hx_max = min(xs), max(xs)
            hy_min, hy_max = min(ys), max(ys)
            hbw = hx_max - hx_min
            hbh = hy_max - hy_min
            gesture_state.hand_bbox = (hx_min, hy_min, hbw, hbh)

            # Palm center (midpoint between wrist [0] and middle MCP [9])
            wrist = landmarks[0]
            mid_mcp = landmarks[9]
            palm_cx = (wrist.x + mid_mcp.x) / 2
            palm_cy = (wrist.y + mid_mcp.y) / 2

            # Coordinate consistency:
            # When camera is mirrored in software (CameraEngine), landmarks from the mirrored frame
            # are already flipped to match the viewer. If config mirror is active, palm_cx directly
            # maps horizontally: x = palm_cx.
            raw_x = palm_cx
            raw_y = palm_cy
            gesture_state.cursor_raw_x = raw_x
            gesture_state.cursor_raw_y = raw_y
            gesture_state.cursor_x += smth * (raw_x - gesture_state.cursor_x)
            gesture_state.cursor_y += smth * (raw_y - gesture_state.cursor_y)

            # Classify gesture with robust geometry
            gesture = self._classify_gesture(landmarks)
            gesture_state.current_gesture = gesture

            # Peace timing
            peace_thresh = config["peace_threshold_ms"] / 1000.0
            if gesture == "peace":
                if gesture_state.peace_start is None:
                    gesture_state.peace_start = now
                elapsed = now - gesture_state.peace_start
                gesture_state.peace_progress = min(1.0, elapsed / peace_thresh)
            else:
                gesture_state.peace_start = None
                gesture_state.peace_progress = 0.0

            # Fist timing
            if gesture == "fist":
                if gesture_state.fist_start is None:
                    gesture_state.fist_start = now
            else:
                gesture_state.fist_start = None

    def _associate_hand_with_primary(self, hand_result):
        """
        Associates detected hands with the primary user.
        Rejects clear background hands but allows primary user's raised/extended arm.
        """
        if not hand_result.hand_landmarks:
            return None

        candidates = []
        for lms in hand_result.hand_landmarks:
            xs = [lm.x for lm in lms]
            ys = [lm.y for lm in lms]
            hbw = max(xs) - min(xs)
            hbh = max(ys) - min(ys)
            hand_diag = math.sqrt(hbw**2 + hbh**2)

            if hand_diag < config["hand_min_size"]:
                if config.get("debug_mode"):
                    print(f"[GESTURE DEBUG] Hand rejected: too small (diag={hand_diag:.3f} < min={config['hand_min_size']})")
                continue

            hcx = sum(xs) / len(xs)
            hcy = sum(ys) / len(ys)
            candidates.append((lms, hcx, hcy, hand_diag))

        if not candidates:
            return None

        # If primary user locked: associate with proximity check
        if gesture_state.primary_user_locked and gesture_state.primary_face_bbox:
            fx, fy, fw, fh = gesture_state.primary_face_bbox
            fcx = fx + fw / 2
            fcy = fy + fh / 2
            face_h = max(fh, 0.05)

            best_hand = None
            best_association_score = -999.0

            for lms, hcx, hcy, hand_diag in candidates:
                dist = math.sqrt((hcx - fcx)**2 + (hcy - fcy)**2)
                scale_ratio = hand_diag / face_h

                # Relaxed thresholds: allow user to extend arm fully toward camera.
                # 0.90 normalized distance covers the full reach of an extended arm.
                # Scale ratio min 0.15: allow hand smaller than face (arm closer to edge).
                MAX_DIST = max(0.90, 4.0 * face_h)
                MIN_SCALE = 0.15

                if dist > MAX_DIST:
                    if config.get("debug_mode"):
                        print(f"[GESTURE DEBUG] Hand rejected: dist={dist:.3f} > max={MAX_DIST:.3f}")
                    continue
                if scale_ratio < MIN_SCALE:
                    if config.get("debug_mode"):
                        print(f"[GESTURE DEBUG] Hand rejected: scale_ratio={scale_ratio:.3f} < min={MIN_SCALE}")
                    continue

                # Score: closer = better, larger scale = better
                assoc_score = 1.0 - (dist / MAX_DIST) * 0.5 + min(scale_ratio, 1.0) * 0.5
                if assoc_score > best_association_score:
                    best_association_score = assoc_score
                    best_hand = lms

            if best_hand is None and config.get("debug_mode"):
                print(f"[GESTURE DEBUG] All hands rejected for primary user (face_h={face_h:.3f})")
            return best_hand
        else:
            # No primary user locked: accept largest hand
            best_hand = None
            max_size = 0.0
            for lms, hcx, hcy, hand_diag in candidates:
                if hand_diag > max_size:
                    max_size = hand_diag
                    best_hand = lms
            return best_hand

    def _classify_gesture(self, landmarks) -> str:
        """
        Robust gesture classification using 2D Euclidean distances,
        joint flexion/extension ratios, and fingertip-to-palm geometry.
        Invariant to arbitrary in-plane hand rotations.
        """
        def dist(p1, p2):
            return math.sqrt((p1.x - p2.x)**2 + (p1.y - p2.y)**2)

        wrist = landmarks[0]
        palm_center_x = (landmarks[0].x + landmarks[9].x) / 2
        palm_center_y = (landmarks[0].y + landmarks[9].y) / 2
        class P:
            def __init__(self, x, y):
                self.x = x
                self.y = y
        palm = P(palm_center_x, palm_center_y)

        palm_scale = max(dist(wrist, landmarks[9]), 0.01)

        # Fingers: index=8/6/5, middle=12/10/9, ring=16/14/13, pinky=20/18/17
        finger_indices = [
            (8, 6, 5),   # Index
            (12, 10, 9), # Middle
            (16, 14, 13),# Ring
            (20, 18, 17) # Pinky
        ]

        extended = []
        folded = []

        for tip_idx, pip_idx, mcp_idx in finger_indices:
            tip = landmarks[tip_idx]
            pip = landmarks[pip_idx]
            mcp = landmarks[mcp_idx]

            d_tip_wrist = dist(tip, wrist)
            d_pip_wrist = dist(pip, wrist)
            d_tip_palm = dist(tip, palm)
            d_pip_palm = dist(pip, palm)

            # Extended if tip is clearly further from wrist/palm than pip
            is_ext = (d_tip_wrist > 1.25 * d_pip_wrist) and (d_tip_palm > 1.20 * d_pip_palm)
            # Folded if tip is close to palm/wrist
            is_fld = (d_tip_wrist < 1.12 * d_pip_wrist) or (d_tip_palm < 1.05 * d_pip_palm)

            extended.append(is_ext)
            folded.append(is_fld)

        # Thumb (4, 3, 2)
        thumb_tip = landmarks[4]
        pinky_mcp = landmarks[17]
        thumb_extended = dist(thumb_tip, pinky_mcp) > 1.3 * dist(landmarks[3], pinky_mcp)

        num_ext = sum(extended)
        num_fld = sum(folded)

        # PEACE: Index + Middle extended, Ring + Pinky folded
        if extended[0] and extended[1] and folded[2] and folded[3]:
            return "peace"

        # PALM: All 4 fingers extended
        if num_ext >= 4:
            return "palm"

        # FIST: All 4 fingers folded
        if num_fld >= 4:
            return "fist"

        return "unknown"

    def _get_framing_guidance(self, face_bbox) -> str:
        x, y, bw, bh = face_bbox
        cx = x + bw / 2
        cy = y + bh / 2
        area = bw * bh

        if area > 0.22:
            return "STEP BACK"
        if area < 0.02:
            return "STEP CLOSER"
        if cx < 0.30:
            return "MOVE RIGHT"
        if cx > 0.70:
            return "MOVE LEFT"
        if cy < 0.18:
            return "MOVE DOWN"
        return ""

gesture_engine = GestureEngine()

# ============================================================
# PAYMENT PROVIDERS
# ============================================================
class PaymentProvider:
    def create_qris_payment(self, order_id: str, amount: int) -> Dict:
        raise NotImplementedError

    def get_payment_status(self, order_id: str, transaction_id: str) -> Dict:
        raise NotImplementedError

    def cancel_payment(self, order_id: str, transaction_id: str) -> bool:
        raise NotImplementedError

class DemoPaymentProvider(PaymentProvider):
    _demo_payments: Dict = {}

    def create_qris_payment(self, order_id: str, amount: int) -> Dict:
        tid = f"DEMO-{uuid.uuid4().hex[:12].upper()}"
        expiry = time.time() + config["payment_timeout_sec"]
        DemoPaymentProvider._demo_payments[order_id] = {
            "transaction_id": tid,
            "status": "pending",
            "amount": amount,
            "expiry": expiry,
        }
        qr_data = f"DEMO_PAYMENT:{order_id}:{amount}"
        print(f"[PAYMENT] Demo QRIS created: {order_id} / Rp{amount:,}")
        return {
            "ok": True,
            "transaction_id": tid,
            "qr_data": qr_data,
            "amount": amount,
            "expiry": expiry,
        }

    def get_payment_status(self, order_id: str, transaction_id: str) -> Dict:
        p = DemoPaymentProvider._demo_payments.get(order_id, {})
        if not p:
            return {"ok": False, "status": "error", "message": "Not found"}
        if time.time() > p["expiry"]:
            p["status"] = "expired"
        return {"ok": True, "status": p["status"], "amount": p.get("amount", 0)}

    def cancel_payment(self, order_id: str, transaction_id: str) -> bool:
        p = DemoPaymentProvider._demo_payments.pop(order_id, None)
        return p is not None

    def simulate_success(self, order_id: str):
        if order_id in DemoPaymentProvider._demo_payments:
            DemoPaymentProvider._demo_payments[order_id]["status"] = "settlement"
            print(f"[PAYMENT] Demo payment verified settlement: {order_id}")

class MidtransPaymentProvider(PaymentProvider):
    def create_qris_payment(self, order_id: str, amount: int) -> Dict:
        import urllib.request
        if not MIDTRANS_SERVER_KEY:
            return {"ok": False, "status": "error", "message": "Midtrans server key not set"}

        auth = base64.b64encode(f"{MIDTRANS_SERVER_KEY}:".encode()).decode()
        payload = json.dumps({
            "payment_type": "qris",
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount
            },
            "qris": {"acquirer": "gopay"}
        }).encode()

        try:
            req = urllib.request.Request(
                f"{MIDTRANS_BASE_URL}/v2/charge",
                data=payload,
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            status = data.get("status_code", "")
            if str(status) in ("200", "201"):
                qr_url = None
                for action in data.get("actions", []):
                    if action.get("name") == "generate-qr-code":
                        qr_url = action.get("url")
                        break
                expiry_str = data.get("expiry_time", "")
                try:
                    expiry = datetime.datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S").timestamp()
                except Exception:
                    expiry = time.time() + config["payment_timeout_sec"]
                return {
                    "ok": True,
                    "transaction_id": data.get("transaction_id", ""),
                    "qr_data": qr_url or "",
                    "amount": amount,
                    "expiry": expiry,
                }
            else:
                return {"ok": False, "status": "error", "message": data.get("status_message", "Unknown")}
        except Exception as e:
            return {"ok": False, "status": "error", "message": str(e)}

    def get_payment_status(self, order_id: str, transaction_id: str) -> Dict:
        import urllib.request
        if not MIDTRANS_SERVER_KEY:
            return {"ok": False, "status": "error", "message": "Midtrans server key not set"}
        auth = base64.b64encode(f"{MIDTRANS_SERVER_KEY}:".encode()).decode()
        try:
            req = urllib.request.Request(
                f"{MIDTRANS_BASE_URL}/v2/{order_id}/status",
                headers={
                    "Authorization": f"Basic {auth}",
                    "Accept": "application/json",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            return {
                "ok": True,
                "status": data.get("transaction_status", ""),
                "fraud_status": data.get("fraud_status", ""),
            }
        except Exception as e:
            return {"ok": False, "status": "error", "message": str(e)}

    def cancel_payment(self, order_id: str, transaction_id: str) -> bool:
        import urllib.request
        if not MIDTRANS_SERVER_KEY:
            return False
        auth = base64.b64encode(f"{MIDTRANS_SERVER_KEY}:".encode()).decode()
        try:
            req = urllib.request.Request(
                f"{MIDTRANS_BASE_URL}/v2/{order_id}/cancel",
                data=b"",
                headers={"Authorization": f"Basic {auth}"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 201)
        except Exception:
            return False

def get_payment_provider() -> PaymentProvider:
    mode = config.get("payment_mode", "manual")
    if mode == "demo":
        return DemoPaymentProvider()
    return MidtransPaymentProvider()

# ============================================================
# PRINTER MANAGER
# ============================================================
class PrinterManager:
    def __init__(self):
        self.available = WIN32_AVAILABLE

    def get_printers(self) -> List[str]:
        if not self.available:
            return []
        try:
            printers = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)
            return [p[2] for p in printers]
        except Exception as e:
            print(f"[PRINT] EnumPrinters error: {e}")
            return []

    def get_default_printer(self) -> str:
        if not self.available:
            return ""
        try:
            return win32print.GetDefaultPrinter()
        except Exception:
            return ""

    def print_image(self, image_path: str, printer_name: Optional[str] = None) -> Tuple[bool, str]:
        if not os.path.exists(image_path):
            return False, f"File not found: {image_path}"

        target_printer = printer_name or config["printer_name"] or self.get_default_printer()

        if self.available and target_printer:
            try:
                import win32ui
                import win32con
                from PIL import ImageWin

                img = Image.open(image_path)
                hdc = win32ui.CreateDC()
                hdc.CreatePrinterDC(target_printer)
                hdc.StartDoc(f"Photo Booth - {Path(image_path).name}")
                hdc.StartPage()

                pw = hdc.GetDeviceCaps(win32con.HORZRES)
                ph = hdc.GetDeviceCaps(win32con.VERTRES)

                iw, ih = img.size
                scale = min(pw / iw, ph / ih)
                nw, nh = int(iw * scale), int(ih * scale)
                ox, oy = (pw - nw) // 2, (ph - nh) // 2

                dib = ImageWin.Dib(img)
                dib.draw(hdc.GetHandleOutput(), (ox, oy, ox + nw, oy + nh))

                hdc.EndPage()
                hdc.EndDoc()
                hdc.DeleteDC()
                print(f"[PRINT] Printed successfully to {target_printer}")
                return True, f"Printed to {target_printer}"
            except Exception as e:
                err = f"Windows print error: {e}"
                print(f"[PRINT ERROR] {err}")
                return False, err
        else:
            # Fallback
            try:
                if sys.platform == "win32":
                    os.startfile(image_path, "print")
                return True, "Print command sent to default viewer"
            except Exception as e:
                return False, f"Fallback print failed: {e}"

    def test_print(self) -> Tuple[bool, str]:
        img = Image.new("RGB", (600, 900), color=(255, 255, 255))
        test_path = str(PHOTOS_DIR / "test_print.jpg")
        img.save(test_path, quality=90)
        ok, msg = self.print_image(test_path)
        try:
            if os.path.exists(test_path):
                os.remove(test_path)
        except Exception:
            pass
        return ok, msg

printer_manager = PrinterManager()

# ============================================================
# FRAME MANAGER
# ============================================================
class FrameManager:
    """
    Manages 1623x3556 RGBA frame templates and compositing 3 photos underneath.
    """
    def __init__(self):
        self.frames: Dict[str, Dict] = {}
        self.preset = FRAME_PRESETS["default_3slot"]
        self._thumb_cache: Dict[str, Optional[bytes]] = {}

    def get_thumbnail_bytes(self, fname: str) -> Optional[bytes]:
        """Cached thumbnail. Returns PNG bytes or None. Never crashes on bad files."""
        if fname in self._thumb_cache:
            return self._thumb_cache[fname]
        frame = self.frames.get(fname)
        if not frame:
            return None
        try:
            with Image.open(frame["path"]) as img:
                thumb = img.copy()
                thumb.thumbnail((120, 260), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                thumb.save(buf, format="PNG")
            data = buf.getvalue()
        except Exception as e:
            print(f"[FRAME SKIP] Thumbnail failed for {fname}: {e}")
            data = None
        self._thumb_cache[fname] = data
        return data

    def clear_thumb_cache(self):
        self._thumb_cache.clear()

    def scan(self):
        self.frames.clear()
        self.clear_thumb_cache()
        try:
            entries = os.listdir(FRAMES_DIR)
        except OSError as e:
            print(f"[FRAME ERROR] Cannot read frames dir: {e}")
            return
        for fname in sorted(entries):
            if not fname.lower().endswith(".png"):
                continue
            p = str(FRAMES_DIR / fname)
            try:
                # Validate image integrity without crashing on corrupt files.
                with Image.open(p) as probe:
                    probe.verify()
                with Image.open(p) as img:
                    w, h = img.size
                    has_alpha = (img.mode == "RGBA") or ("A" in img.mode)

                    self.frames[fname] = {
                        "filename": fname,
                        "label": Path(fname).stem.replace("_", " ").title(),
                        "path": p,  # kept internally for compositing only
                        "width": w,
                        "height": h,
                        "has_alpha": has_alpha,
                        "slot_count": 3,
                    }
                    print(f"[FRAME] Loaded frame: {fname} ({w}x{h}, alpha={has_alpha})")
            except Exception as e:
                print(f"[FRAME SKIP] {fname} invalid/corrupt: {e}")

    def get_frame_list(self) -> List[Dict]:
        # Never expose absolute filesystem paths to the frontend.
        out = []
        for f in self.frames.values():
            out.append({
                "filename": f["filename"],
                "label": f["label"],
                "width": f["width"],
                "height": f["height"],
                "has_alpha": f["has_alpha"],
                "slot_count": f["slot_count"],
            })
        return out

    def get_frame(self, filename: str) -> Optional[Dict]:
        return self.frames.get(filename)

    def composite(self, frame_filename: str, photo_paths: List[str]) -> Optional[str]:
        """
        Composites 3 captured photos UNDER transparent holes in the 1623x3556 frame.
        """
        frame_info = self.get_frame(frame_filename)
        if not frame_info:
            print(f"[COMPOSITE ERROR] Frame not found: {frame_filename}")
            return None

        slots = self.preset["slots"]
        total_w = self.preset["width"]
        total_h = self.preset["height"]

        canvas = Image.new("RGBA", (total_w, total_h), (255, 255, 255, 255))

        for idx, (slot, ppath) in enumerate(zip(slots, photo_paths)):
            if not ppath or not os.path.exists(ppath):
                continue
            try:
                photo = Image.open(ppath).convert("RGBA")
                pw, ph = photo.size
                sw, sh = slot["w"], slot["h"]

                # Cover fit
                scale = max(sw / pw, sh / ph)
                nw, nh = int(pw * scale), int(ph * scale)
                photo_resized = photo.resize((nw, nh), Image.Resampling.LANCZOS)

                # Center crop to exact slot dimension
                ox = (nw - sw) // 2
                oy = (nh - sh) // 2
                photo_cropped = photo_resized.crop((ox, oy, ox + sw, oy + sh))

                canvas.paste(photo_cropped, (slot["x"], slot["y"]))
            except Exception as e:
                print(f"[COMPOSITE ERROR] Slot {idx}: {e}")

        # Paste frame overlay ON TOP
        try:
            frame_img = Image.open(frame_info["path"]).convert("RGBA")
            if frame_img.size != (total_w, total_h):
                frame_img = frame_img.resize((total_w, total_h), Image.Resampling.LANCZOS)
            canvas.alpha_composite(frame_img)
        except Exception as e:
            print(f"[COMPOSITE ERROR] Frame overlay: {e}")

        final_rgb = canvas.convert("RGB")
        out_name = f"{session.session_id}_final.jpg"
        out_path = str(PHOTOS_DIR / out_name)
        final_rgb.save(out_path, quality=config["photo_jpeg_quality"], optimize=True)
        print(f"[FRAME] Final composite saved: {out_path} ({total_w}x{total_h})")
        return out_path

    def generate_preview(self, frame_filename: str, photo_paths: List[str]) -> Optional[str]:
        """Low-resolution preview for interactive frame selection."""
        frame_info = self.get_frame(frame_filename)
        if not frame_info:
            return None
        try:
            scale = 0.2
            pw = int(self.preset["width"] * scale)
            ph = int(self.preset["height"] * scale)
            canvas = Image.new("RGBA", (pw, ph), (245, 245, 248, 255))

            slots = self.preset["slots"]
            for slot, path in zip(slots, photo_paths):
                if not path or not os.path.exists(path):
                    continue
                photo = Image.open(path).convert("RGBA")
                sw = int(slot["w"] * scale)
                sh = int(slot["h"] * scale)
                sx = int(slot["x"] * scale)
                sy = int(slot["y"] * scale)

                scale_p = max(sw / photo.width, sh / photo.height)
                nw, nh = int(photo.width * scale_p), int(photo.height * scale_p)
                p_res = photo.resize((nw, nh), Image.Resampling.BILINEAR)
                ox = (nw - sw) // 2
                oy = (nh - sh) // 2
                canvas.paste(p_res.crop((ox, oy, ox + sw, oy + sh)), (sx, sy))

            frame_img = Image.open(frame_info["path"]).convert("RGBA")
            frame_resized = frame_img.resize((pw, ph), Image.Resampling.BILINEAR)
            canvas.alpha_composite(frame_resized)

            buf = io.BytesIO()
            canvas.convert("RGB").save(buf, format="JPEG", quality=80)
            return base64.b64encode(buf.getvalue()).decode()
        except Exception as e:
            print(f"[PREVIEW ERROR] {e}")
            return None

frame_manager = FrameManager()

# ============================================================
# SESSION CONTROLLER
# ============================================================
class SessionController:
    def transition(self, to_state: str):
        with session_lock:
            from_state = session.state
            session.state = to_state
            session.touch()
            print(f"[SESSION] Transition: {from_state.upper()} -> {to_state.upper()}")

    def start_camera(self):
        with session_lock:
            session.photos = []
            session.countdown_active = False
            session.countdown_value = 0
        self.transition(SessionState.CAMERA)

    def capture_photo(self) -> bool:
        if not camera.is_ok or camera.camera_status != "CONNECTED":
            print("[CAMERA ERROR] Frame capture rejected: camera is not connected or unhealthy")
            return False

        frame = camera.capture_hires()
        if frame is None or frame.size == 0 or frame.shape[0] < 240:
            print("[CAMERA ERROR] Frame capture failed: invalid frame buffer")
            return False

        with session_lock:
            count = len(session.photos) + 1
            if count > 3:
                return False
            filename = f"{session.session_id}_photo_{count}.jpg"
            path = str(PHOTOS_DIR / filename)
            cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, config["photo_jpeg_quality"]])
            session.photos.append(path)
            session.touch()
            print(f"[PHOTO] Captured #{count}: {path}")
            return True

    def start_payment(self):
        self.transition(SessionState.PAYMENT)
        with session_lock:
            session.payment_state = "creating"
            order_id = session.session_id  # session_id already has RTS_ prefix
            session.payment_order_id = order_id
            amount = config["photobooth_price"]
            session.payment_amount = amount

        mode = config.get("payment_mode", "manual")
        if mode == "manual":
            # Manual QRIS: no gateway, no internet, no polling. Operator confirms with F9 x3.
            with session_lock:
                if session.payment_order_id == order_id:
                    session.payment_state = "pending"
                    session.payment_transaction_id = None
                    session.payment_qr_data = None
                    session.payment_expiry = None
                    print(f"[PAYMENT] Manual QRIS pending: {order_id} / Rp{amount:,} (confirm via F9 x3)")
            return

        # Legacy paths (demo / midtrans): create provider payment + start polling.
        threading.Thread(target=self._init_payment, args=(order_id, amount), daemon=True).start()

    def _init_payment(self, order_id: str, amount: int):
        provider = get_payment_provider()
        res = provider.create_qris_payment(order_id, amount)
        with session_lock:
            if session.payment_order_id != order_id:
                return
            if res.get("ok"):
                session.payment_state = "pending"
                session.payment_transaction_id = res.get("transaction_id")
                session.payment_qr_data = res.get("qr_data")
                session.payment_expiry = res.get("expiry")
                session._payment_stop_event = threading.Event()
                stop_ev = session._payment_stop_event
                # Start polling
                threading.Thread(target=self._poll_payment, args=(order_id, stop_ev), daemon=True).start()
            else:
                session.payment_state = "error"
                print(f"[PAYMENT ERROR] {res.get('message')}")

    def _poll_payment(self, order_id: str, stop_event: threading.Event):
        provider = get_payment_provider()
        while not stop_event.is_set():
            time.sleep(PAYMENT_POLL_INTERVAL)
            if stop_event.is_set():
                break
            with session_lock:
                tid = session.payment_transaction_id
            try:
                res = provider.get_payment_status(order_id, tid)
                if not res.get("ok"):
                    continue
                status = res.get("status", "")
                with session_lock:
                    if session.payment_order_id != order_id:
                        break
                    if status in ("settlement", "capture"):
                        session.payment_state = "success"
                        stop_event.set()
                        print(f"[PAYMENT] Verified: {order_id}")
                        threading.Thread(
                            target=lambda: (time.sleep(1.5), self.transition(SessionState.FRAMES)),
                            daemon=True
                        ).start()
                    elif status == "expire":
                        session.payment_state = "expired"
                        stop_event.set()
                    elif status in ("deny", "cancel"):
                        session.payment_state = "failed"
                        stop_event.set()
            except Exception as e:
                print(f"[PAYMENT POLL ERROR] {e}")

    def stop_payment_poll(self):
        with session_lock:
            if session._payment_stop_event:
                session._payment_stop_event.set()
                session._payment_stop_event = None

    def select_frame(self, filename: str):
        safe = os.path.basename(filename)
        if safe not in frame_manager.frames:
            print(f"[FRAME ERROR] Invalid frame: {filename}")
            return
        with session_lock:
            session.selected_frame = safe
        print(f"[FRAME] Selected: {safe}")

    def composite_final(self):
        self.transition(SessionState.COMPOSITING)
        threading.Thread(target=self._do_composite, daemon=True).start()

    def _do_composite(self):
        with session_lock:
            frame_file = session.selected_frame
            photo_paths = list(session.photo_paths)
        if not frame_file or len(photo_paths) < 3:
            print("[COMPOSITE ERROR] Missing frame or photos")
            self.transition(SessionState.FRAMES)
            return
        out_path = frame_manager.composite(frame_file, photo_paths)
        with session_lock:
            session.final_image_path = out_path
        if out_path:
            self.transition(SessionState.QR)
        else:
            self.transition(SessionState.FRAMES)

    def print_photo(self):
        self.transition(SessionState.PRINTING)
        with session_lock:
            session.print_state = "printing"
            session.print_started_mono = time.monotonic()
        threading.Thread(target=self._do_print, daemon=True).start()

    def cancel_print(self):
        """Emergency escape from a stuck printer. Does not touch final image/QR/session."""
        with session_lock:
            session.print_state = "cancelled"
            # Keep print_started_mono so elapsed keeps displaying; state drives UI.
        self.transition(SessionState.QR)

    def _do_print(self):
        with session_lock:
            fpath = session.final_image_path
        if not fpath or not os.path.exists(fpath):
            with session_lock:
                session.print_state = "failed"
            return
        ok, msg = printer_manager.print_image(fpath)
        with session_lock:
            session.print_state = "success" if ok else "failed"
        if ok:
            # Print completed normally (< fallback window) -> proceed to success.
            time.sleep(1.2)
            with session_lock:
                if session.state == SessionState.PRINTING:
                    self.go_success()
        else:
            print(f"[PRINT ERROR] {msg}")

    def go_success(self):
        self.transition(SessionState.SUCCESS)
        threading.Thread(target=self._auto_reset, args=(10,), daemon=True).start()

    def _auto_reset(self, delay: float):
        time.sleep(delay)
        with session_lock:
            if session.state == SessionState.SUCCESS:
                self.full_reset()

    def full_reset(self):
        print(f"[SESSION] Full reset: {session.session_id}")
        self.stop_payment_poll()
        for path in session.photo_paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        session.reset()
        self.transition(SessionState.LANDING)

ctrl = SessionController()

# ============================================================
# BACKGROUND CLEANUP & HOT-PLUG WATCHDOGS
# ============================================================
def cleanup_old_files():
    while True:
        time.sleep(300)
        try:
            cutoff = time.time() - SESSION_CLEANUP_MIN * 60
            for f in glob.glob(str(PHOTOS_DIR / "*.jpg")):
                if os.path.getmtime(f) < cutoff:
                    try:
                        os.remove(f)
                    except Exception:
                        pass
        except Exception:
            pass

def auto_reset_watchdog():
    while True:
        time.sleep(5)
        with session_lock:
            if session.state not in (SessionState.LANDING,):
                idle = session.idle_seconds()
                timeout = config["auto_reset_timeout"]
                if idle > timeout:
                    print(f"[SESSION] Auto-reset idle: {idle:.0f}s")
                    ctrl.full_reset()

def camera_hotplug_watchdog():
    """
    Periodically refreshes camera list to discover plugged / unplugged USB webcams.
    Runs every 4 seconds lightweight without interrupting stream.
    """
    while True:
        time.sleep(4.0)
        try:
            if CV2_ENUM_AVAILABLE or not camera.is_ok:
                camera_device_manager.refresh_devices()
        except Exception as e:
            if config.get("debug_mode"):
                print(f"[WATCHDOG] Camera refresh error: {e}")

# ============================================================
# MJPEG STREAM GENERATOR
# ============================================================
def generate_video_frames():
    while True:
        if not camera.is_ok:
            placeholder = np.ones((360, 640, 3), dtype=np.uint8) * 24
            status_title = f"CAMERA {camera.camera_status}"
            if camera.camera_status == "RECONNECTING":
                sub_text = f"Attempting reconnect... ({camera.reconnect_attempts})"
            elif camera.camera_status == "DEGRADED":
                sub_text = "Stream degraded, recovering..."
            else:
                sub_text = "Searching for camera..."
            cv2.putText(placeholder, status_title, (140, 160),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (230, 230, 230), 2)
            cv2.putText(placeholder, sub_text, (150, 200),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (160, 160, 160), 1)
            ret, buf = cv2.imencode(".jpg", placeholder, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ret:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
            time.sleep(0.1)
            continue

        frame = camera.get_frame()
        if frame is None:
            time.sleep(0.033)
            continue

        # Optional overlays in debug
        if config["show_fps"]:
            cv2.putText(frame, f"FPS: {camera.fps:.1f}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        if config["show_landmarks"]:
            with gesture_lock:
                hbbox = gesture_state.hand_bbox
            if hbbox:
                h, w = frame.shape[:2]
                x, y, bw, bh = hbbox
                cv2.rectangle(frame,
                    (int(x * w), int(y * h)),
                    (int((x + bw) * w), int((y + bh) * h)),
                    (0, 255, 255), 2)

        if config["show_bounding_box"]:
            with gesture_lock:
                fbox = gesture_state.primary_face_bbox
            if fbox:
                h, w = frame.shape[:2]
                x, y, bw, bh = fbox
                cv2.rectangle(frame,
                    (int(x * w), int(y * h)),
                    (int((x + bw) * w), int((y + bh) * h)),
                    (0, 255, 0), 2)

        if config["show_gesture_label"]:
            with gesture_lock:
                g = gesture_state.current_gesture
            cv2.putText(frame, g.upper(), (10, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 255), 2)

        stream_w, stream_h = 640, 360
        stream_frame = cv2.resize(frame, (stream_w, stream_h))
        ret, buf = cv2.imencode(".jpg", stream_frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
        if ret:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
        time.sleep(0.033)

def make_qr_b64(data: str, size: int = 300) -> str:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img = img.resize((size, size), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()

# ============================================================
# FLASK ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/video_feed")
def video_feed():
    return Response(
        stream_with_context(generate_video_frames()),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )

@app.route("/api/cursor")
def api_cursor():
    """Lightweight fast-poll endpoint for cursor/gesture so the frontend can move
    the virtual cursor smoothly without waiting for the (heavier) /api/state."""
    with gesture_lock:
        return jsonify({
            "cursor_x": gesture_state.cursor_x,
            "cursor_y": gesture_state.cursor_y,
            "gesture": gesture_state.current_gesture,
            "hand_detected": gesture_state.hand_detected,
            "hand_valid_for_primary": gesture_state.hand_valid_for_primary,
            "primary_user_locked": gesture_state.primary_user_locked,
            "fist_stable_ms": (
                int((time.time() - gesture_state.fist_start) * 1000)
                if gesture_state.fist_start else 0
            ),
            "peace_progress": gesture_state.peace_progress,
            "last_click_age_ms": int((time.time() - gesture_state.last_click_time) * 1000),
            "inference_fps": round(gesture_state.inference_fps, 1),
            "inference_ms": round(gesture_state.inference_ms, 1),
            "face_count": gesture_state.face_count,
            "camera_fps": round(camera.fps, 1) if camera.is_ok else 0.0,
        })

@app.route("/api/state")
def api_state():
    with session_lock:
        s = session.to_dict()
    with gesture_lock:
        g = {
            "gesture": gesture_state.current_gesture,
            "cursor_x": gesture_state.cursor_x,
            "cursor_y": gesture_state.cursor_y,
            "hand_detected": gesture_state.hand_detected,
            "hand_valid_for_primary": gesture_state.hand_valid_for_primary,
            "primary_user_locked": gesture_state.primary_user_locked,
            "face_count": gesture_state.face_count,
            "subject_score": round(gesture_state.subject_score, 2),
            "peace_progress": gesture_state.peace_progress,
            "fist_stable_ms": (
                int((time.time() - gesture_state.fist_start) * 1000)
                if gesture_state.fist_start else 0
            ),
            "last_click_age_ms": int((time.time() - gesture_state.last_click_time) * 1000),
            "frame_guidance": gesture_state.frame_guidance,
            "last_gesture_error": gesture_state.last_error,
            "inference_fps": round(gesture_state.inference_fps, 1),
            "inference_ms": round(gesture_state.inference_ms, 1),
            "admin_mode": gesture_state.admin_mode,
        }
    cam = {
        "ok": camera.is_ok,
        "ready": camera.ready,
        "status": camera.camera_status,
        "device_id": camera.device_id,
        "device_name": camera.device_name,
        "backend": camera.backend,
        "actual_width": camera.actual_width,
        "actual_height": camera.actual_height,
        "actual_fps": round(camera.fps, 1),
        "error": camera.error,
        "consecutive_failures": camera.consecutive_failures,
        "reconnecting": (camera.camera_status == "RECONNECTING"),
        "reconnect_attempts": camera.reconnect_attempts,
        "last_successful_frame": camera.last_successful_frame_time,
    }
    frames = frame_manager.get_frame_list()
    with session_lock:
        payment_qr = session.payment_qr_data
        payment_amount = session.payment_amount
        payment_state = session.payment_state
        payment_expiry = session.payment_expiry
        final_path = session.final_image_path

    return jsonify({
        "session": s,
        "gesture": g,
        "camera": cam,
        "frames": frames,
        "payment": {
            "state": payment_state,
            "amount": payment_amount,
            "expiry": payment_expiry,
            "has_qr": bool(payment_qr),
            "mode": config.get("payment_mode", "manual"),
        },
        "debug": {
            "show_fps": config["show_fps"],
            "show_landmarks": config["show_landmarks"],
            "show_bounding_box": config["show_bounding_box"],
            "show_gesture_label": config["show_gesture_label"],
            "demo_mode": config["demo_mode"],
            "debug_mode": config["debug_mode"],
        },
        "config": {
            "click_threshold_ms": config["click_threshold_ms"],
            "click_cooldown_ms": config["click_cooldown_ms"],
            "peace_threshold_ms": config["peace_threshold_ms"],
            "countdown_duration": config["countdown_duration"],
            "flash_duration_ms": config["flash_duration_ms"],
            "dark_mode": config["dark_mode"],
        },
        "has_final": bool(final_path and os.path.exists(final_path)),
        "final_session_id": session.session_id,
    })

@app.route("/api/cameras", methods=["GET"])
def api_cameras():
    is_debug = config.get("debug_mode", False)
    devices = camera_device_manager.get_devices()
    selected_id = camera_device_manager.selected_device_id
    dev_dicts = [d.to_dict(selected=(d.device_id == selected_id), debug=is_debug) for d in devices]
    return jsonify({
        "devices": dev_dicts,
        "debug_mode": is_debug,
        "selected_device_id": selected_id
    })

@app.route("/api/cameras/select", methods=["POST"])
def api_camera_select():
    data = request.json or {}
    dev_id = data.get("device_id", "")
    if not dev_id:
        return jsonify({"ok": False, "error": "device_id required"})

    if not camera_device_manager.select_device(dev_id):
        return jsonify({"ok": False, "error": "Device not found"})

    ok = camera.switch_device(dev_id)
    if ok:
        return jsonify({"ok": True, "device_id": dev_id, "name": camera.device_name, "backend": camera.backend})
    else:
        return jsonify({"ok": False, "error": f"Failed to switch to camera {dev_id}: {camera.error}"})

@app.route("/api/cameras/refresh", methods=["POST"])
def api_camera_refresh():
    camera_device_manager.refresh_devices()
    return jsonify({"ok": True})

@app.route("/api/admin_mode", methods=["POST"])
def api_admin_mode():
    data = request.json or {}
    active = bool(data.get("active", False))
    with gesture_lock:
        gesture_state.admin_mode = active
        # Reset transient gesture state on admin mode toggle
        gesture_state.current_gesture = "none"
        gesture_state.fist_start = None
        gesture_state.peace_start = None
        gesture_state.peace_progress = 0.0
    return jsonify({"ok": True, "admin_mode": active})

@app.route("/api/test_camera", methods=["POST"])
def api_test_camera():
    ok, msg = camera.test_active_camera()
    return jsonify({
        "ok": ok,
        "ready": camera.ready,
        "device_name": camera.device_name,
        "backend": camera.backend,
        "resolution": f"{camera.actual_width}x{camera.actual_height}",
        "fps": round(camera.fps, 1),
        "status": camera.camera_status,
        "message": msg
    })

@app.route("/api/diagnostics", methods=["GET"])
def api_diagnostics():
    with gesture_lock:
        g_data = {
            "hand_detected": gesture_state.hand_detected,
            "gesture": gesture_state.current_gesture,
            "confidence": config["hand_confidence"],
            "primary_locked": gesture_state.primary_user_locked,
            "face_count": gesture_state.face_count,
            "primary_score": round(gesture_state.subject_score, 2),
            "hand_association": "VALID" if gesture_state.hand_valid_for_primary else ("REJECTED (Background Hand)" if gesture_state.hand_detected else "NONE"),
            "cursor_x": round(gesture_state.cursor_x, 3),
            "cursor_y": round(gesture_state.cursor_y, 3),
            "inference_fps": round(gesture_state.inference_fps, 1),
            "last_error": gesture_state.last_error or "None",
        }
        g_ready = gesture_engine.ready
        g_running = gesture_engine.running
        g_fps = round(gesture_state.inference_fps, 1)
        g_faces = gesture_state.face_count
        g_prim = gesture_state.primary_user_locked
        g_hand = gesture_state.hand_detected
        g_hand_valid = gesture_state.hand_valid_for_primary
        g_curr = gesture_state.current_gesture
        g_err = gesture_state.last_error or "None"
        g_admin = gesture_state.admin_mode

    with session_lock:
        s_state = session.state

    mp_ver = getattr(mp, "__version__", "unknown") if MEDIAPIPE_OK else "unavailable"

    c_data = {
        "device_name": camera.device_name,
        "status": camera.camera_status,
        "resolution": f"{camera.actual_width}x{camera.actual_height}",
        "fps": round(camera.fps, 1),
        "backend": camera.backend,
        "consecutive_failures": camera.consecutive_failures,
        "reconnect_attempts": camera.reconnect_attempts,
        "last_error": camera.error or "None",
    }

    return jsonify({
        # Comprehensive system & subsystem diagnostics
        "python_executable": sys.executable,
        "python_version": sys.version,
        "opencv_version": cv2.__version__,
        "mediapipe_version": mp_ver,
        "cv2_enumerate_available": CV2_ENUM_AVAILABLE,
        "cv2_enumerate_error": CV2_ENUM_ERROR if not CV2_ENUM_AVAILABLE else None,
        "camera_status": camera.camera_status,
        "camera_name": camera.device_name,
        "camera_backend": camera.backend,
        "camera_resolution": f"{camera.actual_width}x{camera.actual_height}",
        "camera_fps": round(camera.fps, 1),
        "camera_last_error": camera.error or "None",
        "camera_consecutive_failures": camera.consecutive_failures,
        "camera_reconnect_attempts": camera.reconnect_attempts,
        "gesture_ready": g_ready,
        "gesture_running": g_running,
        "gesture_fps": g_fps,
        "face_count": g_faces,
        "primary_user_locked": g_prim,
        "hand_detected": g_hand,
        "hand_valid_for_primary": g_hand_valid,
        "current_gesture": g_curr,
        "last_gesture_error": g_err,
        "settings_open": g_admin,
        "session_state": s_state,
        "printer_ready": WIN32_AVAILABLE,

        # Backward compatibility
        "camera": c_data,
        "gesture": g_data,
    })

@app.route("/api/action", methods=["POST"])
def api_action():
    data = request.json or {}
    action = data.get("action", "")
    session.touch()

    if action == "start_camera":
        if session.state == SessionState.LANDING:
            ctrl.start_camera()
            return jsonify({"ok": True})

    elif action == "capture_photo":
        if session.state == SessionState.CAMERA:
            ok = ctrl.capture_photo()
            with session_lock:
                count = len(session.photos)
            if ok and count >= 3:
                ctrl.transition(SessionState.REVIEW)
            return jsonify({"ok": ok, "photo_count": count})

    elif action == "retake_all":
        if session.state == SessionState.REVIEW:
            ctrl.start_camera()
            return jsonify({"ok": True})

    elif action == "use_photos":
        if session.state == SessionState.REVIEW:
            ctrl.start_payment()
            return jsonify({"ok": True})

    elif action == "back_to_review":
        if session.state in (SessionState.PAYMENT, SessionState.FRAMES):
            ctrl.stop_payment_poll()
            ctrl.transition(SessionState.REVIEW)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Cannot back to review from current state"})

    elif action == "select_frame":
        filename = data.get("filename", "")
        if filename and session.state == SessionState.FRAMES:
            ctrl.select_frame(filename)
            return jsonify({"ok": True})

    elif action == "confirm_frame":
        if session.state == SessionState.FRAMES:
            with session_lock:
                sf = session.selected_frame
            if sf:
                ctrl.composite_final()
                return jsonify({"ok": True})
            return jsonify({"ok": False, "error": "No frame selected"})

    elif action == "print_photo":
        if session.state in (SessionState.QR, SessionState.PRINTING):
            ctrl.print_photo()
            return jsonify({"ok": True})

    elif action == "back_from_print":
        if session.state == SessionState.PRINTING:
            ctrl.cancel_print()
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Not printing"})

    elif action == "skip_print":
        if session.state in (SessionState.QR, SessionState.PRINTING):
            ctrl.go_success()
            return jsonify({"ok": True})

    elif action == "go_success":
        ctrl.go_success()
        return jsonify({"ok": True})

    elif action == "reset":
        ctrl.full_reset()
        return jsonify({"ok": True})

    elif action == "retry_camera":
        camera.restart()
        return jsonify({"ok": True})

    elif action == "register_click":
        with gesture_lock:
            gesture_state.last_click_time = time.time()
        return jsonify({"ok": True})

    elif action == "check_payment":
        with session_lock:
            oid = session.payment_order_id
            tid = session.payment_transaction_id
        if oid:
            provider = get_payment_provider()
            res = provider.get_payment_status(oid, tid or "")
            return jsonify({"ok": True, "status": res.get("status")})
        return jsonify({"ok": False})

    elif action == "simulate_payment_success":
        mode = config.get("payment_mode", "manual")
        if mode == "demo":
            with session_lock:
                oid = session.payment_order_id
            if oid:
                DemoPaymentProvider().simulate_success(oid)
                return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Not in demo mode"})

    elif action == "confirm_manual_payment":
        if session.state == SessionState.PAYMENT:
            with session_lock:
                session.payment_state = "success"
                session.payment_expiry = None
                print(f"[PAYMENT] Manual QRIS confirmed by operator: {session.session_id}")
            ctrl.stop_payment_poll()
            ctrl.transition(SessionState.FRAMES)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Not in payment state"})

    return jsonify({"ok": False, "error": f"Unknown action: {action}"})

@app.route("/api/qris_image")
def api_qris_image():
    qris_path = ASSETS_DIR / "qris.png"
    if not qris_path.exists():
        return jsonify({"ok": False, "error": "QRIS image not found"}), 404
    return send_file(str(qris_path), mimetype="image/png")

@app.route("/api/payment_qr")
def api_payment_qr():
    with session_lock:
        qr_data = session.payment_qr_data
        amount = session.payment_amount
        order_id = session.payment_order_id
    mode = config.get("payment_mode", "manual")
    if mode == "manual":
        # Manual QRIS: static asset. Frontend loads /api/qris_image directly.
        return jsonify({
            "ok": True,
            "mode": "manual",
            "qr_image_url": "/api/qris_image",
            "amount": amount,
            "order_id": order_id,
        })
    if not qr_data:
        return jsonify({"ok": False})
    qr_b64 = make_qr_b64(qr_data, size=400)
    return jsonify({
        "ok": True,
        "qr_b64": qr_b64,
        "amount": amount,
        "order_id": order_id,
    })

@app.route("/api/final_qr")
def api_final_qr():
    with session_lock:
        sid = session.session_id
        fpath = session.final_image_path
    if not fpath or not os.path.exists(fpath):
        return jsonify({"ok": False})
    download_url = f"http://127.0.0.1:5000/download/{sid}"
    qr_b64 = make_qr_b64(download_url, size=400)
    return jsonify({"ok": True, "qr_b64": qr_b64, "url": download_url})

@app.route("/download/<session_id>")
def download_photo(session_id: str):
    if not re.match(r'^RTS_\d{8}_\d{6}_[A-Z0-9]+$', session_id):
        abort(404)
    path = str(PHOTOS_DIR / f"{session_id}_final.jpg")
    if not os.path.exists(path):
        with session_lock:
            if session.session_id == session_id and session.final_image_path:
                path = session.final_image_path
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg",
                     as_attachment=True,
                     download_name=f"photobooth_{session_id}.jpg")

@app.route("/api/frame_preview", methods=["POST"])
def api_frame_preview():
    data = request.json or {}
    filename = data.get("filename", "")
    if not filename:
        return jsonify({"ok": False})
    with session_lock:
        photo_paths = list(session.photo_paths)
    preview_b64 = frame_manager.generate_preview(filename, photo_paths)
    if preview_b64:
        return jsonify({"ok": True, "preview_b64": preview_b64})
    return jsonify({"ok": False})

@app.route("/api/photo_preview/<int:idx>")
def api_photo_preview(idx: int):
    with session_lock:
        paths = list(session.photo_paths)
    if idx < 1 or idx > len(paths):
        abort(404)
    path = paths[idx - 1]
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    global config
    if request.method == "GET":
        printers = printer_manager.get_printers()
        default_p = printer_manager.get_default_printer()
        devices = camera_device_manager.get_devices()
        return jsonify({
            "config": config,
            "printers": printers,
            "default_printer": default_p,
            "cameras": [d.to_dict(selected=(d.device_id == camera_device_manager.selected_device_id), debug=config["debug_mode"]) for d in devices],
            "win32_available": WIN32_AVAILABLE,
            "mediapipe_ok": MEDIAPIPE_OK,
        })
    else:
        updates = request.json or {}
        allowed = set(config.keys())
        for k, v in updates.items():
            if k in allowed:
                config[k] = v
        if updates.get("selected_camera_id"):
            camera_device_manager.select_device(updates["selected_camera_id"])
        if updates.get("restart_camera"):
            camera.restart()
        if updates.get("test_printer"):
            ok, msg = printer_manager.test_print()
            return jsonify({"ok": ok, "message": msg})
        if updates.get("reset_session"):
            ctrl.full_reset()
        if updates.get("reload_frames"):
            frame_manager.scan()
        return jsonify({"ok": True, "config": config})

@app.route("/api/frame_thumb/<filename>")
def api_frame_thumb(filename: str):
    safe = os.path.basename(filename)
    frame = frame_manager.get_frame(safe)
    if not frame:
        abort(404)
    try:
        data = frame_manager.get_thumbnail_bytes(safe)
        if data is None:
            abort(404)
        return Response(data, mimetype="image/png")
    except Exception:
        abort(404)

@app.route("/payment/webhook", methods=["POST"])
def payment_webhook():
    data = request.json or {}
    order_id = data.get("order_id", "")
    status = data.get("transaction_status", "")
    with session_lock:
        if session.payment_order_id == order_id:
            if status in ("settlement", "capture"):
                session.payment_state = "success"
                ctrl.stop_payment_poll()
    return jsonify({"ok": True})

# ============================================================
# HTML / CSS / JS TEMPLATE
# ============================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Rencana Tuhan Studio — Photo Booth</title>
<meta name="description" content="Premium photo booth experience by Rencana Tuhan Studio">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
/* ============================================================
   DESIGN SYSTEM — RENCANA TUHAN STUDIO PHOTO BOOTH
   ============================================================ */
:root {
  --col-bg: #FFFFFF;
  --col-bg-2: #F7F7FA;
  --col-bg-3: #EDEDF2;
  --col-surface: #FFFFFF;
  --col-surface-2: #F7F7FA;
  --col-border: rgba(88,78,184,0.12);
  --col-text: #111318;
  --col-text-2: #6B6D76;
  --col-text-3: #9B9DAA;
  --col-blue-1: #584EB8;
  --col-blue-2: #3C478E;
  --col-yellow: #FDC00F;
  --col-yellow-dark: #D9A200;
  --col-success: #22C55E;
  --col-error: #EF4444;

  --grad-blue: linear-gradient(135deg, #584EB8 0%, #3C478E 100%);
  --grad-blue-soft: linear-gradient(135deg, rgba(88,78,184,0.08) 0%, rgba(60,71,142,0.06) 100%);
  --grad-yellow: linear-gradient(135deg, #FDC00F 0%, #F5A623 100%);
  --grad-hero: radial-gradient(ellipse at 40% 50%, rgba(88,78,184,0.15) 0%, transparent 60%);

  --shadow-sm: 0 1px 3px rgba(17,19,24,0.08);
  --shadow-md: 0 4px 16px rgba(17,19,24,0.10);
  --shadow-lg: 0 16px 48px rgba(17,19,24,0.14);
  --shadow-blue: 0 8px 32px rgba(88,78,184,0.22);
  --shadow-yellow: 0 4px 20px rgba(253,192,15,0.35);

  --font: 'Poppins', system-ui, sans-serif;
  --text-xs: clamp(10px, 1.2vw, 12px);
  --text-sm: clamp(12px, 1.4vw, 14px);
  --text-base: clamp(14px, 1.6vw, 16px);
  --text-lg: clamp(16px, 1.9vw, 20px);
  --text-xl: clamp(20px, 2.4vw, 26px);
  --text-2xl: clamp(26px, 3.2vw, 36px);
  --text-3xl: clamp(36px, 5vw, 56px);

  --r-sm: 8px; --r-md: 16px; --r-lg: 24px; --r-xl: 32px; --r-full: 999px;
  --tr-fast: 150ms ease;
  --tr-med: 250ms ease;
}

body.dark-mode {
  --col-bg: #0D0F14;
  --col-bg-2: #161820;
  --col-bg-3: #1E202B;
  --col-surface: #161820;
  --col-surface-2: #1E202B;
  --col-border: rgba(255,255,255,0.10);
  --col-text: #F0F2F8;
  --col-text-2: #9B9DAA;
  --col-text-3: #5A5C68;
}

*, *::before, *::after {
  box-sizing: border-box;
  margin: 0; padding: 0;
  -webkit-user-select: none;
  user-select: none;
}

html, body {
  width: 100vw; height: 100vh;
  overflow: hidden;
  font-family: var(--font);
  background: var(--col-bg);
  color: var(--col-text);
  touch-action: none;
}

/* KIOSK FULL GESTURE CONTROLLER: HIDE & BLOCK PHYSICAL CURSOR */
body.gesture-control,
body.gesture-control *,
body.gesture-control .screen,
body.gesture-control .btn,
body.gesture-control .btn-interactive,
body.gesture-control .landing-cta-btn,
body.gesture-control .frame-card {
  cursor: none !important;
}

/* Allow mouse cursor inside Admin Settings Modal for operator */
#settings-modal,
#settings-modal * {
  cursor: default !important;
}

/* Prevent native mouse hover scaling when in gesture mode; only virtual cursor hover triggers */
body.gesture-control .btn:hover:not(.hovered),
body.gesture-control .landing-cta-btn:hover:not(.hovered),
body.gesture-control .frame-card:hover:not(.hovered) {
  transform: none !important;
  box-shadow: inherit !important;
}

/* Virtual cursor hover effect */
.btn.hovered, .landing-cta-btn.hovered, .frame-card.hovered {
  transform: scale(1.05) !important;
  box-shadow: 0 0 24px rgba(88, 78, 184, 0.45) !important;
}

.btn.gesture-clicked, .landing-cta-btn.gesture-clicked, .frame-card.gesture-clicked {
  transform: scale(0.96) !important;
  filter: brightness(1.25);
}

/* App Shell */
#app {
  position: relative;
  width: 100vw; height: 100vh;
  overflow: hidden;
}

.screen {
  position: absolute;
  inset: 0;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  opacity: 0; pointer-events: none;
  transition: opacity 300ms ease;
  z-index: 10;
}

.screen.active {
  opacity: 1; pointer-events: all;
  z-index: 20;
}

/* LANDING SCREEN */
#screen-landing {
  background: var(--col-bg);
  background-image: var(--grad-hero);
  padding: 48px;
  text-align: center;
  gap: 32px;
}

.brand-badge {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 20px;
  background: var(--grad-blue-soft);
  border: 1px solid var(--col-border);
  border-radius: var(--r-full);
  font-size: var(--text-sm); font-weight: 600;
  color: var(--col-blue-1);
  letter-spacing: 2px; text-transform: uppercase;
}

.landing-title {
  font-size: var(--text-3xl); font-weight: 900;
  line-height: 1.1;
  background: var(--grad-blue);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

.landing-sub {
  font-size: var(--text-lg);
  color: var(--col-text-2);
  max-width: 580px;
}

.landing-cta-btn {
  display: inline-flex; align-items: center; gap: 16px;
  padding: 24px 56px;
  background: var(--grad-yellow);
  color: #111318;
  font-size: var(--text-xl); font-weight: 800;
  border-radius: var(--r-full);
  border: none;
  box-shadow: var(--shadow-yellow);
  cursor: pointer;
  transition: transform var(--tr-fast), box-shadow var(--tr-fast);
}

.landing-cta-btn:hover, .landing-cta-btn.hovered {
  transform: scale(1.04);
  box-shadow: 0 8px 32px rgba(253,192,15,0.55);
}

.gesture-guide-pill {
  display: flex; align-items: center; gap: 24px;
  padding: 16px 32px;
  background: var(--col-surface);
  border: 1px solid var(--col-border);
  border-radius: var(--r-full);
  box-shadow: var(--shadow-sm);
}

.guide-step {
  display: flex; align-items: center; gap: 8px;
  font-size: var(--text-sm); font-weight: 600;
  color: var(--col-text-2);
}

.guide-step-icon { font-size: 20px; }

/* CAMERA SCREEN */
#screen-camera {
  background: #000;
  padding: 0;
}

.camera-container {
  position: relative;
  width: 100vw; height: 100vh;
  display: flex; align-items: center; justify-content: center;
  background: #0D0F14;
}

#camera-stream {
  width: 100%; height: 100%;
  object-fit: contain;
}

.camera-header-hud {
  position: absolute;
  top: 32px; left: 40px; right: 40px;
  display: flex; justify-content: space-between; align-items: center;
  z-index: 30;
}

.camera-dots {
  display: flex; gap: 12px;
}

.photo-dot {
  width: 14px; height: 14px;
  border-radius: 50%;
  background: rgba(255,255,255,0.3);
  border: 2px solid rgba(255,255,255,0.6);
  transition: all var(--tr-med);
}

.photo-dot.done {
  background: var(--col-yellow);
  border-color: var(--col-yellow);
  box-shadow: 0 0 12px var(--col-yellow);
}

.photo-dot.current {
  background: #fff;
  border-color: #fff;
  transform: scale(1.2);
}

.photo-count-badge {
  padding: 8px 20px;
  background: rgba(0,0,0,0.6);
  backdrop-filter: blur(8px);
  border: 1px solid rgba(255,255,255,0.2);
  border-radius: var(--r-full);
  color: #fff;
  font-size: var(--text-sm); font-weight: 700;
  letter-spacing: 1px;
}

.camera-guidance {
  position: absolute;
  bottom: 40px;
  padding: 12px 32px;
  background: rgba(0,0,0,0.65);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: var(--r-full);
  color: var(--col-yellow);
  font-size: var(--text-base); font-weight: 700;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  z-index: 30;
  display: none;
}

.camera-guidance.visible { display: block; }

/* Camera Error Overlay */
#camera-error {
  position: absolute;
  inset: 0;
  background: rgba(13,15,20,0.92);
  backdrop-filter: blur(12px);
  display: none; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 16px;
  z-index: 40;
}

.camera-error-icon { font-size: 48px; }
.camera-error-title { font-size: var(--text-2xl); font-weight: 800; color: #fff; }
.camera-error-text { font-size: var(--text-base); color: var(--col-text-3); max-width: 480px; text-align: center; }

/* Peace progress ring */
#peace-ring-overlay {
  position: absolute;
  top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  width: 140px; height: 140px;
  pointer-events: none;
  display: none;
  z-index: 35;
}

#peace-ring-overlay.visible { display: flex; align-items: center; justify-content: center; }

.peace-ring-svg {
  transform: rotate(-90deg);
  width: 140px; height: 140px;
}

.peace-ring-circle-bg {
  fill: none;
  stroke: rgba(255,255,255,0.2);
  stroke-width: 8;
}

.peace-ring-circle-fg {
  fill: none;
  stroke: var(--col-yellow);
  stroke-width: 8;
  stroke-linecap: round;
  stroke-dasharray: 376;
  stroke-dashoffset: 376;
  transition: stroke-dashoffset 80ms linear;
}

.peace-ring-label {
  position: absolute;
  color: #fff;
  font-size: var(--text-sm); font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 1px;
}

/* Countdown overlay */
#countdown-overlay {
  position: absolute;
  inset: 0;
  background: rgba(0,0,0,0.45);
  display: none;
  align-items: center; justify-content: center;
  z-index: 50;
}

#countdown-overlay.active { display: flex; }

#countdown-number {
  font-size: clamp(80px, 20vw, 200px);
  font-weight: 900;
  color: var(--col-yellow);
  text-shadow: 0 8px 48px rgba(0,0,0,0.8);
  animation: countdownPulse 1s ease infinite;
}

@keyframes countdownPulse {
  0% { transform: scale(1.4); opacity: 0; }
  40% { transform: scale(1.0); opacity: 1; }
  100% { transform: scale(0.9); opacity: 0.8; }
}

/* Flash overlay */
#flash-overlay {
  position: fixed; inset: 0;
  background: #fff;
  opacity: 0; pointer-events: none;
  z-index: 100;
  transition: opacity 80ms ease-out;
}

/* REVIEW SCREEN */
#screen-review {
  background: var(--col-bg);
  padding: 32px 48px;
  gap: 24px;
}

.review-grid {
  display: flex; gap: 20px;
  width: 100%; max-width: 960px;
  justify-content: center;
}

.review-card {
  flex: 1;
  background: var(--col-surface);
  border: 2px solid var(--col-border);
  border-radius: var(--r-lg);
  overflow: hidden;
  box-shadow: var(--shadow-md);
  aspect-ratio: 1543 / 1060;
  display: flex; align-items: center; justify-content: center;
}

.review-card img {
  width: 100%; height: 100%;
  object-fit: cover;
}

.review-actions {
  display: flex; gap: 20px;
  margin-top: 16px;
}

/* PAYMENT SCREEN */
#screen-payment {
  background: var(--col-bg);
  padding: 40px;
  gap: 24px;
}

.payment-card {
  background: var(--col-surface);
  border: 1px solid var(--col-border);
  border-radius: var(--r-xl);
  box-shadow: var(--shadow-lg);
  padding: 36px 48px;
  display: flex; flex-direction: column; align-items: center;
  gap: 20px;
  max-width: 480px; width: 100%;
}

.payment-qr-wrapper {
  width: 260px; height: 260px;
  display: flex; align-items: center; justify-content: center;
  background: #fff;
  border-radius: var(--r-md);
  border: 1px solid var(--col-border);
  padding: 12px;
}

.payment-qr-wrapper img { width: 100%; height: 100%; object-fit: contain; }

.payment-amount {
  font-size: var(--text-2xl); font-weight: 900;
  color: var(--col-blue-1);
}

.payment-status-badge {
  padding: 8px 24px;
  border-radius: var(--r-full);
  font-weight: 700; font-size: var(--text-sm);
  background: var(--grad-blue-soft);
  color: var(--col-blue-1);
}

/* FRAMES SCREEN */
#screen-frames {
  background: var(--col-bg);
  padding: 32px 48px;
  gap: 24px;
}

.frames-grid {
  display: flex; gap: 24px;
  overflow-x: auto;
  max-width: 100%;
  padding: 16px 8px;
}

.frame-card {
  width: 180px; height: 380px;
  background: var(--col-surface);
  border: 3px solid var(--col-border);
  border-radius: var(--r-md);
  overflow: hidden;
  cursor: pointer;
  position: relative;
  transition: transform var(--tr-fast), border-color var(--tr-fast), box-shadow var(--tr-fast);
  flex-shrink: 0;
  display: flex; flex-direction: column;
}

.frame-card:hover, .frame-card.hovered {
  transform: translateY(-8px);
  border-color: var(--col-yellow);
  box-shadow: var(--shadow-yellow);
}

.frame-card.selected {
  border-color: var(--col-blue-1);
  box-shadow: 0 0 0 3px var(--col-yellow), var(--shadow-blue);
}

.frame-card-preview {
  flex: 1;
  position: relative;
  overflow: hidden;
  background: #f0f0f4;
  display: flex; align-items: center; justify-content: center;
}

.frame-card-preview img {
  width: 100%; height: 100%;
  object-fit: contain;
}

.frame-card-label {
  padding: 12px;
  text-align: center;
  font-weight: 700;
  font-size: var(--text-sm);
  background: var(--col-surface);
}

/* COMPOSITING SCREEN */
#screen-compositing {
  background: var(--col-bg);
  gap: 24px;
}

.spinner {
  width: 64px; height: 64px;
  border: 6px solid var(--col-border);
  border-top-color: var(--col-yellow);
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* QR DOWNLOAD SCREEN */
#screen-qr {
  background: var(--col-bg);
  padding: 40px;
  gap: 28px;
}

.qr-box {
  background: var(--col-surface);
  border: 1px solid var(--col-border);
  border-radius: var(--r-xl);
  padding: 36px 48px;
  box-shadow: var(--shadow-lg);
  display: flex; flex-direction: column; align-items: center;
  gap: 20px;
}

/* SUCCESS SCREEN */
#screen-success {
  background: var(--col-bg);
  background-image: var(--grad-hero);
  text-align: center;
  gap: 24px;
}

/* BUTTONS */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 12px;
  padding: 16px 36px;
  border-radius: var(--r-full);
  font-size: var(--text-base); font-weight: 700;
  border: none; cursor: pointer;
  transition: transform var(--tr-fast), box-shadow var(--tr-fast);
}

.btn-primary {
  background: var(--grad-yellow);
  color: #111318;
  box-shadow: var(--shadow-yellow);
}

.btn-secondary {
  background: var(--grad-blue);
  color: #fff;
  box-shadow: var(--shadow-blue);
}

.btn-ghost {
  background: var(--col-surface);
  border: 1.5px solid var(--col-border);
  color: var(--col-text);
}

.btn:hover, .btn.hovered {
  transform: scale(1.04);
}

/* VIRTUAL CURSOR */
#cursor {
  position: fixed;
  width: 52px; height: 52px;
  margin-left: -26px; margin-top: -26px;
  border-radius: 50%;
  background: rgba(253, 192, 15, 0.25);
  border: 2.5px solid var(--col-yellow);
  box-shadow: 0 0 24px rgba(253,192,15,0.8), 0 0 8px rgba(253,192,15,0.5);
  pointer-events: none;
  z-index: 9999;
  transition: background 120ms ease, border-color 120ms ease, box-shadow 120ms ease, transform 100ms ease;
  opacity: 0;
  will-change: left, top;
}

/* SVG Charging Ring for Fist & Peace */
.cursor-ring-svg {
  position: absolute;
  inset: -3px;
  width: calc(100% + 6px);
  height: calc(100% + 6px);
  transform: rotate(-90deg);
  pointer-events: none;
}
.cursor-ring-bg {
  fill: none;
  stroke: rgba(255, 255, 255, 0.15);
  stroke-width: 3.5;
}
.cursor-ring-fg {
  fill: none;
  stroke: var(--col-yellow);
  stroke-width: 4;
  stroke-dasharray: 138.23;
  stroke-dashoffset: 138.23;
  stroke-linecap: round;
  transition: stroke-dashoffset 40ms linear, stroke 150ms ease;
}

/* Center dot */
.cursor-center-dot {
  position: absolute;
  top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  width: 8px; height: 8px;
  background: var(--col-yellow);
  border-radius: 50%;
  box-shadow: 0 0 6px rgba(253,192,15,1);
  transition: background 150ms ease;
}

#cursor.fist {
  background: rgba(88, 78, 184, 0.45);
  border-color: #A78BFA;
  box-shadow: 0 0 28px rgba(88,78,184,0.9);
  transform: scale(0.85);
}
#cursor.fist .cursor-ring-fg {
  stroke: #A78BFA;
}
#cursor.fist .cursor-center-dot {
  background: #fff;
  box-shadow: 0 0 8px rgba(255,255,255,1);
}

#cursor.fist-clicked {
  transform: scale(1.32) !important;
  box-shadow: 0 0 40px rgba(88,78,184,1), 0 0 15px #fff !important;
  background: rgba(88, 78, 184, 0.85) !important;
}

#cursor.peace {
  background: rgba(34, 197, 94, 0.35);
  border-color: #22C55E;
  box-shadow: 0 0 24px rgba(34,197,94,0.85);
}
#cursor.peace .cursor-ring-fg {
  stroke: #22C55E;
}
#cursor.peace .cursor-center-dot {
  background: #22C55E;
  box-shadow: 0 0 8px rgba(34,197,94,1);
}

/* Gesture Debug Overlay (shown when show_gesture_label=true) */
#gesture-debug-overlay {
  position: fixed;
  top: 16px; right: 16px;
  background: rgba(0,0,0,0.80);
  backdrop-filter: blur(8px);
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: var(--r-md);
  padding: 10px 16px;
  font-size: 11px; font-weight: 700;
  font-family: 'Courier New', monospace;
  color: #fff;
  z-index: 9998;
  line-height: 1.8;
  pointer-events: none;
  display: none;
}
#gesture-debug-overlay.visible { display: block; }
#gesture-debug-overlay .gd-row { display: flex; gap: 8px; }
#gesture-debug-overlay .gd-key { color: rgba(255,255,255,0.5); min-width: 80px; }
#gesture-debug-overlay .gd-val { color: var(--col-yellow); }
#gesture-debug-overlay .gd-val.ok { color: #22C55E; }
#gesture-debug-overlay .gd-val.err { color: #EF4444; }

/* GESTURE HUD INDICATOR */
#gesture-hud {
  position: fixed;
  bottom: 24px; left: 24px;
  display: flex; align-items: center; gap: 12px;
  background: rgba(255,255,255,0.88);
  backdrop-filter: blur(12px);
  border: 1px solid var(--col-border);
  border-radius: var(--r-full);
  padding: 8px 18px;
  box-shadow: var(--shadow-sm);
  z-index: 1000;
  transition: border-color var(--tr-fast), transform var(--tr-fast);
}

body.dark-mode #gesture-hud {
  background: rgba(22,24,32,0.88);
}

@keyframes hudShakeAlert {
  0%, 100% { transform: translateX(0); }
  20%, 60% { transform: translateX(-6px); box-shadow: 0 0 20px rgba(239, 68, 68, 0.6); }
  40%, 80% { transform: translateX(6px); box-shadow: 0 0 20px rgba(239, 68, 68, 0.6); }
}

#gesture-hud.hud-alert-pulse {
  animation: hudShakeAlert 400ms ease;
  border-color: var(--col-error) !important;
}

.hud-item {
  display: flex; align-items: center; gap: 6px;
  font-size: var(--text-xs); font-weight: 700;
  color: var(--col-text-3);
  opacity: 0.55;
  transition: opacity var(--tr-fast), color var(--tr-fast);
}

.hud-item.active {
  opacity: 1;
  color: var(--col-blue-1);
}

.hud-badge {
  background: rgba(88, 78, 184, 0.12);
  border: 1px solid rgba(88, 78, 184, 0.30);
  color: var(--col-blue-1) !important;
  border-radius: var(--r-full);
  padding: 3px 10px;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.5px;
  opacity: 1 !important;
}

.hud-divider {
  width: 1px;
  height: 16px;
  background: var(--col-border);
}

.controller-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 16px;
  background: rgba(88, 78, 184, 0.08);
  border: 1px solid rgba(88, 78, 184, 0.25);
  border-radius: var(--r-full);
  font-size: var(--text-xs);
  font-weight: 700;
  color: var(--col-blue-1);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.controller-status-pill .ctrl-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--col-success);
  box-shadow: 0 0 8px var(--col-success);
}

/* F10 SETTINGS MODAL */
#settings-modal {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.65);
  backdrop-filter: blur(12px);
  display: none; align-items: center; justify-content: center;
  z-index: 10000;
}

#settings-modal.open { display: flex; }

.settings-panel {
  background: var(--col-surface);
  border-radius: var(--r-xl);
  width: 90vw; max-width: 940px;
  height: 85vh; max-height: 780px;
  display: flex; flex-direction: column;
  box-shadow: var(--shadow-lg);
  border: 1px solid var(--col-border);
  overflow: hidden;
}

.settings-header {
  padding: 24px 32px;
  border-bottom: 1px solid var(--col-border);
  display: flex; justify-content: space-between; align-items: center;
}

.settings-body {
  flex: 1; display: flex;
  overflow: hidden;
}

.settings-nav {
  width: 220px;
  border-right: 1px solid var(--col-border);
  padding: 16px 8px;
  display: flex; flex-direction: column; gap: 4px;
  overflow-y: auto;
}

.settings-nav-item {
  padding: 12px 18px;
  border-radius: var(--r-md);
  font-size: var(--text-sm); font-weight: 600;
  color: var(--col-text-2);
  cursor: pointer;
  transition: background var(--tr-fast), color var(--tr-fast);
}

.settings-nav-item:hover { background: var(--col-bg-2); }
.settings-nav-item.active { background: var(--grad-blue-soft); color: var(--col-blue-1); font-weight: 700; }

.settings-content {
  flex: 1;
  padding: 24px 32px;
  overflow-y: auto;
}

.settings-section { display: none; }
.settings-section.active { display: block; }

.settings-group {
  margin-bottom: 28px;
}

.settings-group-title {
  font-size: var(--text-sm); font-weight: 800;
  letter-spacing: 1px; text-transform: uppercase;
  color: var(--col-blue-1);
  margin-bottom: 16px;
}

.settings-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid var(--col-border);
}

.settings-label {
  font-size: var(--text-sm); font-weight: 600;
}

.settings-hint {
  font-size: var(--text-xs); color: var(--col-text-3); display: block;
}

.settings-control input[type="text"],
.settings-control input[type="number"],
.settings-control select {
  padding: 8px 14px;
  border-radius: var(--r-sm);
  border: 1px solid var(--col-border);
  background: var(--col-bg-2);
  color: var(--col-text);
  font-family: var(--font); font-size: var(--text-sm);
  min-width: 140px;
}

.settings-toggle {
  width: 48px; height: 26px;
  background: var(--col-bg-3);
  border-radius: var(--r-full);
  position: relative; cursor: pointer;
  transition: background var(--tr-med);
}

.settings-toggle.on { background: var(--col-blue-1); }

.settings-toggle::after {
  content: '';
  position: absolute;
  top: 3px; left: 3px;
  width: 20px; height: 20px;
  background: #fff;
  border-radius: 50%;
  transition: transform var(--tr-med);
}

.settings-toggle.on::after { transform: translateX(22px); }

/* Camera Device Cards in Settings */
.camera-cards-list {
  display: flex; flex-direction: column; gap: 12px;
  margin-bottom: 16px;
}

.camera-device-card {
  padding: 16px 20px;
  border-radius: var(--r-md);
  border: 2px solid var(--col-border);
  background: var(--col-bg-2);
  display: flex; justify-content: space-between; align-items: center;
  cursor: pointer;
  transition: all var(--tr-fast);
}

.camera-device-card:hover {
  border-color: var(--col-blue-1);
}

.camera-device-card.selected {
  border-color: var(--col-blue-1);
  background: var(--grad-blue-soft);
  box-shadow: 0 0 0 2px var(--col-yellow);
}

.camera-card-info {
  display: flex; flex-direction: column; gap: 4px;
}

.camera-card-name {
  font-weight: 700; font-size: var(--text-base);
}

.camera-card-desc {
  font-size: var(--text-xs); color: var(--col-text-2);
}

.camera-card-badge {
  padding: 4px 12px;
  border-radius: var(--r-full);
  font-size: var(--text-xs); font-weight: 800;
  text-transform: uppercase;
}

.camera-card-badge.ready {
  background: rgba(34,197,94,0.15);
  color: var(--col-success);
}

.camera-card-badge.available {
  background: rgba(88,78,184,0.12);
  color: var(--col-blue-1);
}

/* Camera Live Test Area */
.camera-test-panel {
  background: #000;
  border-radius: var(--r-md);
  overflow: hidden;
  position: relative;
  aspect-ratio: 16 / 9;
  max-width: 520px;
  margin-top: 12px;
}

.camera-test-panel img {
  width: 100%; height: 100%;
  object-fit: cover;
}

/* Diagnostics Grid */
.diag-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px; margin-bottom: 24px;
}

.diag-card {
  padding: 16px;
  background: var(--col-bg-2);
  border: 1px solid var(--col-border);
  border-radius: var(--r-md);
  display: flex; flex-direction: column; gap: 6px;
}

.diag-card-title {
  font-size: var(--text-xs); font-weight: 700;
  color: var(--col-text-3); text-transform: uppercase;
}

.diag-card-value {
  font-size: var(--text-lg); font-weight: 800;
  color: var(--col-text);
}

.settings-footer {
  padding: 16px 32px;
  border-top: 1px solid var(--col-border);
  display: flex; justify-content: flex-end; gap: 16px;
}

/* Toast */
#toast {
  position: fixed;
  top: 32px; left: 50%;
  transform: translateX(-50%) translateY(-60px);
  padding: 12px 28px;
  background: #111318; color: #fff;
  border-radius: var(--r-full);
  font-size: var(--text-sm); font-weight: 700;
  box-shadow: var(--shadow-lg);
  opacity: 0; pointer-events: none;
  transition: all 250ms cubic-bezier(0.16, 1, 0.3, 1);
  z-index: 99999;
}

#toast.show {
  transform: translateX(-50%) translateY(0);
  opacity: 1;
}
</style>
</head>
<body id="body">
<div id="app">

  <!-- LANDING -->
  <div class="screen active" id="screen-landing">
    <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap; justify-content: center;">
      <div class="brand-badge">Rencana Tuhan Studio</div>
      <div class="controller-status-pill"><span class="ctrl-dot"></span><span>🎮 FULL GESTURE CONTROLLER</span></div>
    </div>
    <h1 class="landing-title">Capture Your Joy,<br>Touch-Free.</h1>
    <p class="landing-sub">Step into the future of studio photography. Use natural hand gestures to create and print your memories.</p>
    
    <button class="landing-cta-btn btn-interactive" onclick="doAction('start_camera')">
      <span>START PHOTO SESSION</span>
      <span>→</span>
    </button>

    <div class="gesture-guide-pill">
      <div class="guide-step">
        <span class="guide-step-icon">✋</span>
        <span>Open Palm = Move Cursor</span>
      </div>
      <div class="guide-step">
        <span class="guide-step-icon">✊</span>
        <span>Hold Fist = Select / Click</span>
      </div>
      <div class="guide-step">
        <span class="guide-step-icon">✌️</span>
        <span>Hold Peace = Snap Photo</span>
      </div>
    </div>
    <div style="font-size: 11px; color: var(--col-text-3); font-weight: 600; margin-top: -12px;">
      🚫 Touchpad & mouse dinonaktifkan • Kontrol 100% menggunakan gestur tangan
    </div>
  </div>

  <!-- CAMERA SCREEN -->
  <div class="screen" id="screen-camera">
    <div class="camera-container">
      <img id="camera-stream" src="/video_feed" alt="Live Feed">

      <div class="camera-header-hud">
        <div class="camera-dots">
          <div class="photo-dot" id="dot-1"></div>
          <div class="photo-dot" id="dot-2"></div>
          <div class="photo-dot" id="dot-3"></div>
        </div>
        <div class="photo-count-badge" id="photo-num-label">PHOTO 01 / 03</div>
      </div>

      <div class="camera-guidance" id="guidance-label">STEP CLOSER</div>

      <!-- Peace Ring Overlay -->
      <div id="peace-ring-overlay">
        <svg class="peace-ring-svg">
          <circle class="peace-ring-circle-bg" cx="70" cy="70" r="60"></circle>
          <circle class="peace-ring-circle-fg" id="peace-ring-fill" cx="70" cy="70" r="60"></circle>
        </svg>
        <div class="peace-ring-label">HOLD ✌️</div>
      </div>

      <!-- Countdown -->
      <div id="countdown-overlay">
        <div id="countdown-number">3</div>
      </div>

      <!-- Camera Error Overlay -->
      <div id="camera-error">
        <div class="camera-error-icon">📷</div>
        <div class="camera-error-title">CAMERA UNAVAILABLE</div>
        <div class="camera-error-text">Searching for camera device. Please ensure your camera is connected.</div>
        <button class="btn btn-primary btn-interactive" onclick="retryCamera()">RETRY CAMERA</button>
      </div>
    </div>
  </div>

  <!-- REVIEW SCREEN -->
  <div class="screen" id="screen-review">
    <div class="brand-badge">Photo Review</div>
    <h2 style="font-size: var(--text-2xl); font-weight: 800;">Review Your Photos</h2>
    <div class="review-grid">
      <div class="review-card"><img id="rev-photo-1" src="" alt="Photo 1"></div>
      <div class="review-card"><img id="rev-photo-2" src="" alt="Photo 2"></div>
      <div class="review-card"><img id="rev-photo-3" src="" alt="Photo 3"></div>
    </div>
    <div class="review-actions">
      <button class="btn btn-ghost btn-interactive" onclick="doAction('retake_all')">↺ Retake All</button>
      <button class="btn btn-primary btn-interactive" onclick="doAction('use_photos')">Continue to Payment →</button>
    </div>
  </div>

  <!-- PAYMENT SCREEN -->
  <div class="screen" id="screen-payment">
    <div class="brand-badge">QRIS Payment</div>
    <div class="payment-card">
      <div class="payment-amount" id="payment-amount-display">Rp 25.000</div>
      <div class="payment-qr-wrapper">
        <img id="payment-qr-img" src="" alt="QRIS Code">
        <div id="payment-spinner" class="spinner"></div>
      </div>
      <div class="payment-status-badge" id="payment-status-text">Preparing your payment...</div>
      <div style="font-size: var(--text-xs); color: var(--col-text-3);" id="payment-timer"></div>
      <div id="payment-manual-hint" style="display:none; font-size: var(--text-xs); color: var(--col-text-3); text-align:center; border-top:1px solid var(--col-border); padding-top:14px;">
        Operator: tekan <b>F9 sebanyak 3x</b> setelah pembayaran selesai untuk mengonfirmasi.
      </div>

      <div style="display: flex; gap: 12px; margin-top: 8px; flex-wrap: wrap; justify-content: center;" id="demo-payment-row">
        <button class="btn btn-ghost btn-interactive" onclick="doAction('back_to_review')">← Back to Review</button>
        <button class="btn btn-ghost btn-interactive" onclick="simulatePayment()">⚡ Simulate Payment (Demo)</button>
        <button class="btn btn-ghost btn-interactive" id="btn-retry-payment" style="display:none;" onclick="retryPayment()">Retry</button>
      </div>
    </div>
  </div>

  <!-- FRAMES SCREEN -->
  <div class="screen" id="screen-frames">
    <div class="brand-badge">Frame Design</div>
    <h2 style="font-size: var(--text-2xl); font-weight: 800;">Choose Your Studio Frame</h2>
    <div class="frames-grid" id="frames-grid"></div>
    <div style="display: flex; gap: 16px; margin-top: 12px; flex-wrap: wrap; justify-content: center;">
      <button class="btn btn-ghost btn-interactive" onclick="doAction('back_to_review')">← Back</button>
      <button class="btn btn-primary btn-interactive" id="btn-confirm-frame" onclick="doAction('confirm_frame')" disabled style="opacity:0.5;">
        Confirm Frame & Composite →
      </button>
    </div>
  </div>

  <!-- COMPOSITING SCREEN -->
  <div class="screen" id="screen-compositing">
    <div class="spinner"></div>
    <h2 style="font-size: var(--text-xl); font-weight: 800;">Crafting Your High-Res Photo...</h2>
    <p style="color: var(--col-text-2);">Applying frame styling and optimizing print colors.</p>
  </div>

  <!-- QR DOWNLOAD SCREEN -->
  <div class="screen" id="screen-qr">
    <div class="brand-badge">Download & Print</div>
    <div class="qr-box">
      <h2 style="font-size: var(--text-xl); font-weight: 800;">Scan QR to Download Photo</h2>
      <div class="payment-qr-wrapper">
        <img id="final-qr-img" src="" alt="Download QR">
        <div id="final-qr-loading" class="spinner"></div>
      </div>
      <div style="display: flex; gap: 16px; margin-top: 12px;">
        <button class="btn btn-secondary btn-interactive" onclick="doAction('print_photo')">🖨 Print Photo</button>
        <button class="btn btn-primary btn-interactive" onclick="doAction('go_success')">Done ✓</button>
      </div>
    </div>
  </div>

  <!-- PRINTING SCREEN -->
  <div class="screen" id="screen-printing">
    <div class="spinner"></div>
    <h2 style="font-size: var(--text-xl); font-weight: 800;" id="printing-title">Printing Your Photo...</h2>
    <p style="color: var(--col-text-2);" id="printing-status">Please wait while the printer prepares your photo.</p>
    <div style="font-size: var(--text-xs); color: var(--col-text-3); margin-top: 8px;" id="printing-elapsed"></div>
    <div id="print-fallback" style="display:none; margin-top: 28px; align-items:center; gap:16px; flex-direction:row;">
      <button class="btn btn-ghost btn-interactive" style="background:rgba(107,109,118,0.15); color:var(--col-text-2); border-color:rgba(107,109,118,0.4);" onclick="doAction('back_from_print')">← Back</button>
      <button class="btn btn-ghost btn-interactive" style="background:rgba(107,109,118,0.15); color:var(--col-text-2); border-color:rgba(107,109,118,0.4);" onclick="doAction('skip_print')">Next →</button>
    </div>
    <div style="font-size: var(--text-xs); color: var(--col-text-3); margin-top: 12px;" id="print-fallback-hint"></div>
  </div>

  <!-- SUCCESS SCREEN -->
  <div class="screen" id="screen-success">
    <div style="font-size: 64px;">✨</div>
    <h1 style="font-size: var(--text-3xl); font-weight: 900; color: var(--col-blue-1);">Thank You!</h1>
    <p style="font-size: var(--text-lg); color: var(--col-text-2); max-width: 560px; text-align: center;">We hope you loved your Rencana Tuhan Studio photo experience.</p>
    <div style="font-size: var(--text-sm); font-weight: 700; color: var(--col-yellow-dark);" id="success-countdown">Kembali ke Home dalam 10 detik...</div>
    <div style="margin-top: 24px; display: flex; flex-direction: column; align-items: center; gap: 12px;">
      <button class="btn btn-primary btn-interactive" onclick="goHome()" style="padding: 14px 42px; font-size: var(--text-base); font-weight: 800; display: inline-flex; align-items: center; gap: 10px;">
        <span>🏠 Kembali ke Home</span>
        <span>→</span>
      </button>
      <div style="font-size: var(--text-xs); color: var(--col-text-3); font-weight: 600;">
        Arahkan kursor & kepalkan tangan (✊) atau tahan pose Peace (✌️)
      </div>
    </div>
  </div>

</div>

<!-- Virtual Cursor -->
<div id="cursor">
  <svg class="cursor-ring-svg" viewBox="0 0 52 52">
    <circle class="cursor-ring-bg" cx="26" cy="26" r="22"></circle>
    <circle class="cursor-ring-fg" id="cursor-progress-fill" cx="26" cy="26" r="22"></circle>
  </svg>
  <div class="cursor-center-dot"></div>
</div>

<!-- Flash overlay -->
<div id="flash-overlay"></div>

<!-- Gesture Debug Overlay (visible when show_gesture_label=true) -->
<div id="gesture-debug-overlay">
  <div class="gd-row"><span class="gd-key">HAND</span><span class="gd-val" id="gdo-hand">NO</span></div>
  <div class="gd-row"><span class="gd-key">PRIMARY</span><span class="gd-val" id="gdo-primary">NO</span></div>
  <div class="gd-row"><span class="gd-key">FACE</span><span class="gd-val" id="gdo-face">0</span></div>
  <div class="gd-row"><span class="gd-key">GESTURE</span><span class="gd-val" id="gdo-gesture">NONE</span></div>
  <div class="gd-row"><span class="gd-key">CURSOR</span><span class="gd-val" id="gdo-cursor">-</span></div>
  <div class="gd-row"><span class="gd-key">FPS</span><span class="gd-val" id="gdo-fps">-</span></div>
  <div class="gd-row"><span class="gd-key">CAM</span><span class="gd-val" id="gdo-cam">-</span></div>
</div>

<!-- Gesture HUD -->
<div id="gesture-hud">
  <div class="hud-item hud-badge" id="hud-ctrl-mode" title="Mode Controller Aktif">🎮 <span id="hud-ctrl-text">FULL GESTURE</span></div>
  <div class="hud-divider"></div>
  <div class="hud-item" id="gest-hand" title="Hand Detected">👋<span id="gest-hand-txt">NO HAND</span></div>
  <div class="hud-item" id="gest-palm">✋<span>PALM</span></div>
  <div class="hud-item" id="gest-fist">✊<span>FIST</span></div>
  <div class="hud-item" id="gest-peace">✌️<span>PEACE</span></div>
</div>

<!-- Toast -->
<div id="toast"></div>

<!-- F10 SETTINGS MODAL -->
<div id="settings-modal">
  <div class="settings-panel" onclick="event.stopPropagation()">
    <div class="settings-header">
      <h2 style="font-size: var(--text-lg); font-weight: 800;">⚙️ Admin Settings</h2>
      <button class="btn btn-ghost" style="padding: 6px 14px;" onclick="closeSettings()">✕</button>
    </div>
    <div class="settings-body">
      <div class="settings-nav">
        <div class="settings-nav-item active" onclick="showSettingsSection('controller')">🎮 Controller</div>
        <div class="settings-nav-item" onclick="showSettingsSection('camera')">📷 Camera</div>
        <div class="settings-nav-item" onclick="showSettingsSection('diagnostics')">🧪 Diagnostics</div>
        <div class="settings-nav-item" onclick="showSettingsSection('gesture')">🤚 Gesture</div>
        <div class="settings-nav-item" onclick="showSettingsSection('display')">🖥 Display</div>
        <div class="settings-nav-item" onclick="showSettingsSection('print')">🖨 Print</div>
        <div class="settings-nav-item" onclick="showSettingsSection('photo')">🎞 Photo</div>
        <div class="settings-nav-item" onclick="showSettingsSection('payment')">💳 Payment</div>
        <div class="settings-nav-item" onclick="showSettingsSection('system')">⚡ System</div>
        <div class="settings-nav-item" onclick="showSettingsSection('debug')">🐛 Debug</div>
      </div>
      <div class="settings-content">
        <!-- CONTROLLER SETTINGS -->
        <div class="settings-section active" id="settings-controller">
          <div class="settings-group">
            <div class="settings-group-title">Pengaturan Controller Kiosk</div>
            <div class="settings-row">
              <div class="settings-label">
                <strong>Mode Controller</strong>
                <div style="font-size: 11px; color: var(--col-text-2);">Kiosk hanya bisa dioperasikan lewat gestur tangan (touchpad terkunci).</div>
              </div>
              <div class="settings-control">
                <select id="cfg-controller_mode">
                  <option value="gesture_only">Full Gesture Tangan (Touchpad & Mouse Diblokir)</option>
                  <option value="hybrid">Hybrid (Gesture + Touchpad/Mouse Diizinkan)</option>
                </select>
              </div>
            </div>
            <div class="settings-row">
              <div class="settings-label">
                <strong>Blokir Touchpad & Mouse Fisik</strong>
                <div style="font-size: 11px; color: var(--col-text-2);">Nonaktifkan semua klik dan gerakan touchpad pada layar photobooth.</div>
              </div>
              <div class="settings-control">
                <div class="settings-toggle on" id="cfg-block_touchpad" onclick="toggleSetting(this)"></div>
              </div>
            </div>
          </div>

          <div class="settings-group">
            <div class="settings-group-title">Status Controller Saat Ini</div>
            <div class="diag-grid">
              <div class="diag-card">
                <div class="diag-card-title">Controller Mode</div>
                <div class="diag-card-value" style="color: var(--col-blue-1); font-size: 15px;" id="diag-ctrl-mode">FULL GESTURE</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Touchpad Hardware</div>
                <div class="diag-card-value" style="color: var(--col-error); font-size: 15px;" id="diag-ctrl-touchpad">BLOCKED (OFF)</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Kursor Fisik</div>
                <div class="diag-card-value" style="font-size: 15px;" id="diag-ctrl-cursor">TERSEMBUNYI</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Hand Tracker</div>
                <div class="diag-card-value" style="color: var(--col-success); font-size: 15px;" id="diag-ctrl-tracker">ONLINE</div>
              </div>
            </div>
          </div>

          <div class="settings-group">
            <div class="settings-group-title">Daftar Kontrol Gestur Tangan</div>
            <div style="display: flex; flex-direction: column; gap: 8px;">
              <div style="display: flex; align-items: center; gap: 12px; padding: 12px 16px; background: var(--col-bg-2); border-radius: var(--r-md); border: 1px solid var(--col-border);">
                <span style="font-size: 26px;">✋</span>
                <div>
                  <strong style="font-size: 13px;">Telapak Tangan Terbuka (Open Palm)</strong>
                  <div style="font-size: 11px; color: var(--col-text-2);">Menggerakkan kursor virtual di layar. Arahkan ke tombol yang ingin dipilih.</div>
                </div>
              </div>
              <div style="display: flex; align-items: center; gap: 12px; padding: 12px 16px; background: var(--col-bg-2); border-radius: var(--r-md); border: 1px solid var(--col-border);">
                <span style="font-size: 26px;">✊</span>
                <div>
                  <strong style="font-size: 13px;">Kepalan Tangan (Hold Fist 350ms)</strong>
                  <div style="font-size: 11px; color: var(--col-text-2);">Tahan kepalan tangan untuk mengisi ring dan mengeklik tombol yang diarahkan.</div>
                </div>
              </div>
              <div style="display: flex; align-items: center; gap: 12px; padding: 12px 16px; background: var(--col-bg-2); border-radius: var(--r-md); border: 1px solid var(--col-border);">
                <span style="font-size: 26px;">✌️</span>
                <div>
                  <strong style="font-size: 13px;">Pose Dua Jari (Peace Sign 1.2s)</strong>
                  <div style="font-size: 11px; color: var(--col-text-2);">Tahan pose peace untuk memulai countdown foto 3 detik atau lanjut dari review.</div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- CAMERA -->
        <div class="settings-section" id="settings-camera">
          <div class="settings-group">
            <div class="settings-group-title">Camera Device</div>
            <div id="camera-devices-container" class="camera-cards-list"></div>
            <div style="display: flex; gap: 12px; margin-top: 8px;">
              <button class="btn btn-ghost" style="font-size: var(--text-xs); padding: 8px 18px;" onclick="refreshCameraList()">🔄 Refresh Cameras</button>
              <button class="btn btn-ghost" style="font-size: var(--text-xs); padding: 8px 18px;" onclick="testCameraDevice()">🧪 Test Camera</button>
            </div>
          </div>
          <div class="settings-group">
            <div class="settings-group-title">Live Camera Test</div>
            <div class="camera-test-panel">
              <img src="/video_feed" alt="Camera Test Preview">
            </div>
            <div style="font-size: var(--text-xs); color: var(--col-text-2); margin-top: 8px;" id="camera-test-info">
              Checking status...
            </div>
          </div>
          <div class="settings-group">
            <div class="settings-group-title">Format & Orientation</div>
            <div class="settings-row">
              <div class="settings-label">Mirror (Horizontal Flip)</div>
              <div class="settings-control"><div class="settings-toggle" id="cfg-camera_mirror" onclick="toggleSetting(this)"></div></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Rotation</div>
              <div class="settings-control">
                <select id="cfg-camera_rotate">
                  <option value="0">0° (Normal)</option>
                  <option value="90">90° Clockwise</option>
                  <option value="180">180° (Upside Down)</option>
                  <option value="270">270° / 90° CCW</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        <!-- DIAGNOSTICS -->
        <div class="settings-section" id="settings-diagnostics">
          <div class="settings-group">
            <div class="settings-group-title">Real-Time Diagnostics</div>
            <div class="diag-grid">
              <div class="diag-card">
                <div class="diag-card-title">Camera Status</div>
                <div class="diag-card-value" id="diag-camera-status">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Hand Detection</div>
                <div class="diag-card-value" id="diag-hand-status">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Gesture Recognized</div>
                <div class="diag-card-value" id="diag-gesture-val" style="color: var(--col-yellow);">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Primary User</div>
                <div class="diag-card-value" id="diag-primary-lock">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Faces Detected</div>
                <div class="diag-card-value" id="diag-face-count">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Hand Association</div>
                <div class="diag-card-value" id="diag-hand-assoc">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Inference FPS</div>
                <div class="diag-card-value" id="diag-inference-fps">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Last Gesture Error</div>
                <div class="diag-card-value" id="diag-last-err" style="font-size: var(--text-sm); font-weight:600;">None</div>
              </div>
            </div>

            <div class="settings-group-title" style="margin-top: 16px;">System & Hardware Diagnostics</div>
            <div class="diag-grid">
              <div class="diag-card">
                <div class="diag-card-title">Active Camera</div>
                <div class="diag-card-value" id="diag-cam-name" style="font-size: var(--text-sm);">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Backend & Resolution</div>
                <div class="diag-card-value" id="diag-cam-res" style="font-size: var(--text-sm);">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Camera FPS / Failures</div>
                <div class="diag-card-value" id="diag-cam-fps">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Camera Enumeration</div>
                <div class="diag-card-value" id="diag-cam-enum">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">OpenCV Version</div>
                <div class="diag-card-value" id="diag-opencv-ver">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">MediaPipe Version</div>
                <div class="diag-card-value" id="diag-mp-ver">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Python Runtime</div>
                <div class="diag-card-value" id="diag-py-ver" style="font-size: 11px; word-break: break-all;">-</div>
              </div>
              <div class="diag-card">
                <div class="diag-card-title">Printer Status</div>
                <div class="diag-card-value" id="diag-printer-status">-</div>
              </div>
            </div>

            <div style="display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px;">
              <button class="btn btn-ghost" onclick="testCameraDevice()">Test Camera</button>
              <button class="btn btn-ghost" onclick="loadDiagnosticsFromServer()">Refresh Diagnostics</button>
              <button class="btn btn-ghost" onclick="showToast('Hold palm to move cursor')">Test Hand</button>
              <button class="btn btn-ghost" onclick="showToast('Hold fist 350ms to test click')">Test Fist</button>
              <button class="btn btn-ghost" onclick="showToast('Hold peace 1200ms to test snap')">Test Peace</button>
              <button class="btn btn-ghost" onclick="settingsAction('test_printer')">Test Printer</button>
            </div>
          </div>
        </div>

        <!-- GESTURE -->
        <div class="settings-section" id="settings-gesture">
          <div class="settings-group">
            <div class="settings-group-title">Gesture Sensitivity</div>
            <div class="settings-row">
              <div class="settings-label">Hand Confidence (0.1–1.0)</div>
              <div class="settings-control"><input type="number" id="cfg-hand_confidence" min="0.1" max="1.0" step="0.05"></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Cursor Smoothing (0.1=slow, 1.0=instant)</div>
              <div class="settings-control"><input type="number" id="cfg-gesture_smoothing" min="0.05" max="1.0" step="0.05"></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Click Hold (ms)</div>
              <div class="settings-control"><input type="number" id="cfg-click_threshold_ms" min="100" max="2000"></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Click Cooldown (ms)</div>
              <div class="settings-control"><input type="number" id="cfg-click_cooldown_ms" min="200" max="2000"></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Peace Hold (ms)</div>
              <div class="settings-control"><input type="number" id="cfg-peace_threshold_ms" min="300" max="5000"></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Grace Period (ms)</div>
              <div class="settings-control"><input type="number" id="cfg-primary_user_grace_ms" min="500" max="10000"></div>
            </div>
          <div class="settings-group">
            <div class="settings-group-title">Gesture Performance</div>
            <div class="settings-row">
              <div class="settings-label">Inference Width (px)</div>
              <div class="settings-control"><input type="number" id="cfg-inference_width" min="160" max="1280" step="16"></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Inference Height (px)</div>
              <div class="settings-control"><input type="number" id="cfg-inference_height" min="90" max="720" step="16"></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Face Detect Interval (frames)</div>
              <div class="settings-control"><input type="number" id="cfg-gesture_face_interval" min="1" max="10" step="1"></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Print Fallback Escape (sec)</div>
              <div class="settings-control"><input type="number" id="cfg-print_fallback_sec" min="10" max="600"></div>
            </div>
          </div>
        </div>

        <!-- DISPLAY -->
        <div class="settings-section" id="settings-display">
          <div class="settings-group">
            <div class="settings-group-title">Theme</div>
            <div class="settings-row">
              <div class="settings-label">Dark Mode</div>
              <div class="settings-control"><div class="settings-toggle" id="cfg-dark_mode" onclick="toggleSetting(this)"></div></div>
            </div>
          </div>
        </div>

        <!-- PRINT -->
        <div class="settings-section" id="settings-print">
          <div class="settings-group">
            <div class="settings-group-title">Printer Configuration</div>
            <div class="settings-row">
              <div class="settings-label">Printer Name</div>
              <div class="settings-control">
                <select id="cfg-printer_name">
                  <option value="">(System Default)</option>
                </select>
              </div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Auto Print on Complete</div>
              <div class="settings-control"><div class="settings-toggle" id="cfg-auto_print" onclick="toggleSetting(this)"></div></div>
            </div>
          </div>
        </div>

        <!-- PHOTO -->
        <div class="settings-section" id="settings-photo">
          <div class="settings-group">
            <div class="settings-group-title">Capture Timing</div>
            <div class="settings-row">
              <div class="settings-label">Countdown Duration (sec)</div>
              <div class="settings-control"><input type="number" id="cfg-countdown_duration" min="1" max="10"></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Flash Duration (ms)</div>
              <div class="settings-control"><input type="number" id="cfg-flash_duration_ms" min="50" max="500"></div>
            </div>
          </div>
        </div>

        <!-- PAYMENT -->
        <div class="settings-section" id="settings-payment">
          <div class="settings-group">
            <div class="settings-group-title">Payment Mode</div>
            <div class="settings-row">
              <div class="settings-label">Payment Mode (manual = QRIS + F9 konfirmasi)</div>
              <div class="settings-control">
                <select id="cfg-payment_mode">
                  <option value="manual">Manual QRIS (default)</option>
                  <option value="demo">Demo (simulate)</option>
                  <option value="midtrans">Midtrans (legacy gateway)</option>
                </select>
              </div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Demo Mode</div>
              <div class="settings-control"><div class="settings-toggle" id="cfg-demo_mode" onclick="toggleSetting(this)"></div></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Price (IDR)</div>
              <div class="settings-control"><input type="number" id="cfg-photobooth_price" min="0"></div>
            </div>
          </div>
        </div>

        <!-- SYSTEM -->
        <div class="settings-section" id="settings-system">
          <div class="settings-group">
            <div class="settings-group-title">System Actions</div>
            <div style="display: flex; flex-wrap: wrap; gap: 8px;">
              <button class="btn btn-ghost" onclick="settingsAction('restart_camera')">🔄 Restart Camera</button>
              <button class="btn btn-ghost" onclick="settingsAction('test_printer')">🖨 Test Printer</button>
              <button class="btn btn-ghost" onclick="settingsAction('reset_session')">↺ Reset Session</button>
              <button class="btn btn-ghost" onclick="settingsAction('reload_frames')">🖼 Reload Frames</button>
              <button class="btn btn-ghost" style="color:var(--col-error);" onclick="location.reload()">⚡ Reload App</button>
            </div>
          </div>
        </div>

        <!-- DEBUG -->
        <div class="settings-section" id="settings-debug">
          <div class="settings-group">
            <div class="settings-group-title">Developer Overlays</div>
            <div class="settings-row">
              <div class="settings-label">Show FPS</div>
              <div class="settings-control"><div class="settings-toggle" id="cfg-show_fps" onclick="toggleSetting(this)"></div></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Show Landmarks</div>
              <div class="settings-control"><div class="settings-toggle" id="cfg-show_landmarks" onclick="toggleSetting(this)"></div></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Show Primary User Box</div>
              <div class="settings-control"><div class="settings-toggle" id="cfg-show_bounding_box" onclick="toggleSetting(this)"></div></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Show Gesture Label</div>
              <div class="settings-control"><div class="settings-toggle" id="cfg-show_gesture_label" onclick="toggleSetting(this)"></div></div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Debug Mode (verbose logs)</div>
              <div class="settings-control"><div class="settings-toggle" id="cfg-debug_mode" onclick="toggleSetting(this)"></div></div>
            </div>
          </div>
        </div>

      </div>
    </div>
    <div class="settings-footer">
      <button class="btn btn-ghost" onclick="closeSettings()">Cancel</button>
      <button class="btn btn-primary" onclick="saveSettings()">Save Settings</button>
    </div>
  </div>
</div>

<script>
// ============================================================
// APPLICATION JAVASCRIPT
// ============================================================
let appState = {
  screen: 'landing',
  gesture: 'none',
  cursor: { x: 0.5, y: 0.5 },
  handDetected: false,
  primaryUserLocked: false,
  peaceProgress: 0,
  fistStableMs: 0,
  lastClickAgeMs: 9999,
  inferenceFps: 0,
  inferenceMs: 0,
  cameraFps: 0,
  faceCount: 0,
  config: { controller_mode: 'gesture_only', block_touchpad: true },
  frames: [],
  selectedFrame: null,
  paymentState: 'idle',
  paymentMode: 'manual',
  paymentAmount: 0,
  paymentExpiry: null,
  photoCount: 0,
  hasFinal: false,
  sessionId: '',
  cameraOk: true,
  cameraError: null,
  printState: 'idle',
  guidance: '',
  adminMode: false,
};

let pollInterval = null;
let cursorPollInterval = null;
let paymentTimerInterval = null;
let successTimerInterval = null;
let qrLoaded = false;
let framePreviewsLoaded = {};
const POLL_MS = 350;
const CURSOR_POLL_MS = 50;

// Gesture action latches
let fistArmed = true;
let peaceArmed = true;
let inCooldown = false;
let countdownRunning = false;
let isGestureDispatching = false;
let lastTouchpadWarning = 0;

function setupTouchpadBlocker() {
  const BLOCKED_EVENTS = [
    'click', 'dblclick', 'mousedown', 'mouseup', 'mousemove',
    'pointerdown', 'pointerup', 'pointermove',
    'touchstart', 'touchend', 'touchmove', 'contextmenu', 'wheel'
  ];

  BLOCKED_EVENTS.forEach(evt => {
    window.addEventListener(evt, (e) => {
      // If admin settings modal is open and the event is inside it, allow operator to configure
      if (settingsOpen && e.target && e.target.closest && e.target.closest('#settings-modal')) {
        return;
      }

      // If full gesture mode is active (default)
      const isGestureOnly = (appState.config.controller_mode !== 'hybrid') && (appState.config.block_touchpad !== false);
      if (!isGestureOnly) return;

      // Allow programmatic gesture-initiated dispatch
      if (isGestureDispatching || !e.isTrusted) {
        return;
      }

      // Block physical hardware events from touchpad / mouse
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();

      // Show warning toast if user tries to tap or click touchpad
      if (evt === 'click' || evt === 'mousedown' || evt === 'pointerdown' || evt === 'touchstart') {
        const now = Date.now();
        if (now - lastTouchpadWarning > 1800) {
          lastTouchpadWarning = now;
          showToast('⚠️ Touchpad Dinonaktifkan! Kontrol full dengan gestur tangan ✋ (Arahkan) & ✊ (Klik)', 3000);
          const hud = document.getElementById('gesture-hud');
          if (hud) {
            hud.classList.add('hud-alert-pulse');
            setTimeout(() => hud.classList.remove('hud-alert-pulse'), 500);
          }
        }
      }
      return false;
    }, true);
  });
}

function applyControllerConfig(cfg) {
  if (!cfg) return;
  const isGestureOnly = (cfg.controller_mode !== 'hybrid') && (cfg.block_touchpad !== false);
  document.body.classList.toggle('gesture-control', isGestureOnly);

  const ctrlTxt = document.getElementById('hud-ctrl-text');
  if (ctrlTxt) {
    ctrlTxt.textContent = isGestureOnly ? 'FULL GESTURE' : 'HYBRID MODE';
  }

  const diagMode = document.getElementById('diag-ctrl-mode');
  if (diagMode) {
    diagMode.textContent = isGestureOnly ? 'FULL GESTURE' : 'HYBRID';
    diagMode.style.color = isGestureOnly ? 'var(--col-blue-1)' : 'var(--col-yellow)';
  }

  const diagPad = document.getElementById('diag-ctrl-touchpad');
  if (diagPad) {
    diagPad.textContent = isGestureOnly ? 'BLOCKED (OFF)' : 'ALLOWED (ON)';
    diagPad.style.color = isGestureOnly ? 'var(--col-error)' : 'var(--col-success)';
  }

  const diagCur = document.getElementById('diag-ctrl-cursor');
  if (diagCur) {
    diagCur.textContent = isGestureOnly ? 'TERSEMBUNYI' : 'TERLIHAT';
  }

  const diagTrack = document.getElementById('diag-ctrl-tracker');
  if (diagTrack) {
    diagTrack.textContent = appState.handDetected ? 'DETECTED' : 'ONLINE';
    diagTrack.style.color = appState.handDetected ? 'var(--col-success)' : 'var(--col-text-2)';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  showScreen('landing');
  setupTouchpadBlocker();
  applyControllerConfig(appState.config);
  startPolling();
  startCursorPolling();
  setupKeyboard();
  requestAnimationFrame(renderLoop);
  loadFrameData();
});

function startPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(fetchState, POLL_MS);
}

function startCursorPolling() {
  if (cursorPollInterval) clearInterval(cursorPollInterval);
  cursorPollInterval = setInterval(fetchCursor, CURSOR_POLL_MS);
}

async function fetchCursor() {
  try {
    const res = await fetch('/api/cursor');
    if (!res.ok) return;
    const d = await res.json();
    appState.cursor = { x: d.cursor_x, y: d.cursor_y };
    appState.gesture = d.gesture;
    appState.handDetected = d.hand_detected;
    appState.primaryUserLocked = d.primary_user_locked;
    appState.peaceProgress = d.peace_progress;
    appState.fistStableMs = d.fist_stable_ms;
    appState.lastClickAgeMs = d.last_click_age_ms || 9999;
    appState.inferenceFps = d.inference_fps;
    appState.inferenceMs = d.inference_ms;
    appState.cameraFps = d.camera_fps;
    updateGestureHUD();
    updateDebugOverlay();
  } catch (e) {}
}

function updateDebugOverlay() {
  const ov = document.getElementById('gesture-debug-overlay');
  if (!ov) return;
  ov.classList.toggle('visible', !!appState.config.show_gesture_label);
  const set = (id, val, cls) => {
    const el = document.getElementById(id);
    if (el) { el.textContent = val; el.className = 'gd-val' + (cls ? ' ' + cls : ''); }
  };
  set('gdo-hand', appState.handDetected ? 'YES' : 'NO', appState.handDetected ? 'ok' : '');
  set('gdo-primary', appState.primaryUserLocked ? 'LOCK' : 'NO', appState.primaryUserLocked ? 'ok' : '');
  set('gdo-face', appState.faceCount ?? 0);
  set('gdo-gesture', (appState.gesture || 'none').toUpperCase());
  set('gdo-cursor', `${(appState.cursor.x*100).toFixed(0)},${(appState.cursor.y*100).toFixed(0)}`);
  set('gdo-fps', `${appState.inferenceFps ?? '-'} fps / ${appState.inferenceMs ?? '-'}ms`);
  set('gdo-cam', `${appState.cameraFps ?? '-'} fps`);
}

async function fetchState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) return;
    const data = await res.json();
    updateState(data);
  } catch (e) {}
}

function updateState(data) {
  const s = data.session;
  const g = data.gesture;
  const cam = data.camera;
  const cfg = data.config;
  const dbg = data.debug;

  appState.cursor = { x: g.cursor_x, y: g.cursor_y };
  appState.gesture = g.gesture;
  appState.handDetected = g.hand_detected;
  appState.primaryUserLocked = g.primary_user_locked;
  appState.peaceProgress = g.peace_progress;
  appState.fistStableMs = g.fist_stable_ms;
  appState.lastClickAgeMs = g.last_click_age_ms || 9999;
  appState.guidance = g.frame_guidance || '';
  appState.adminMode = g.admin_mode || settingsOpen;
  appState.faceCount = g.face_count || 0;
  appState.inferenceFps = g.inference_fps || 0;
  appState.inferenceMs = g.inference_ms || 0;
  appState.cameraFps = cam.actual_fps || 0;

  appState.photoCount = s.photo_slots_filled;
  appState.paymentState = s.payment_state;
  appState.paymentAmount = data.payment.amount;
  appState.paymentExpiry = data.payment.expiry;
  appState.paymentMode = data.payment.mode || 'manual';
  appState.hasFinal = data.has_final;
  appState.sessionId = data.final_session_id;
  appState.printState = s.print_state;

  appState.cameraOk = cam.ok;
  appState.cameraError = cam.error;
  appState.config = cfg;

  if (data.frames && data.frames.length > 0 && appState.frames.length === 0) {
    appState.frames = data.frames;
    renderFrameCards();
  }

  document.getElementById('body').classList.toggle('dark-mode', cfg.dark_mode);
  applyControllerConfig(cfg);

  const targetScreen = s.state;
  if (appState.screen !== targetScreen) {
    transitionToScreen(targetScreen);
    appState.screen = targetScreen;
  }

  updateScreenContent(s, data);

  // Diagnostics update if panel open
  if (settingsOpen) {
    updateDiagnosticsView(cam, g);
  }

  processGestures(cfg);
}

function updateDiagnosticsView(cam, g) {
  const cStat = document.getElementById('diag-camera-status');
  if (cStat) cStat.textContent = cam.ok ? 'READY' : (cam.error || 'OFFLINE');
  const hStat = document.getElementById('diag-hand-status');
  if (hStat) hStat.textContent = g.hand_detected ? 'DETECTED' : 'NONE';
  const gVal = document.getElementById('diag-gesture-val');
  if (gVal) gVal.textContent = g.gesture.toUpperCase();
  const pLock = document.getElementById('diag-primary-lock');
  if (pLock) pLock.textContent = g.primary_user_locked ? `LOCKED (${g.subject_score})` : 'UNLOCKED';
  const fCnt = document.getElementById('diag-face-count');
  if (fCnt) fCnt.textContent = g.face_count || 0;
  const hAssoc = document.getElementById('diag-hand-assoc');
  if (hAssoc) hAssoc.textContent = g.hand_valid_for_primary ? 'VALID' : (g.hand_detected ? 'REJECTED' : '-');
  const infFps = document.getElementById('diag-inference-fps');
  if (infFps) infFps.textContent = `${g.inference_fps} FPS / ${g.inference_ms || 0} ms`;
  const lErr = document.getElementById('diag-last-err');
  if (lErr) lErr.textContent = g.last_gesture_error || 'None';
}

function showScreen(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  const target = document.getElementById(`screen-${name}`);
  if (target) {
    target.classList.add('active');
    appState.screen = name;
  }
}

function transitionToScreen(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  const target = document.getElementById(`screen-${name}`);
  if (!target) return;
  target.classList.add('active');

  if (appState.screen === 'payment' && name !== 'payment') resetF9();
  if (name === 'qr' && !qrLoaded) loadFinalQR();
  if (name === 'payment') initPaymentScreen();
  if (name === 'success') startSuccessCountdown();
  if (name === 'landing') {
    qrLoaded = false;
    framePreviewsLoaded = {};
    if (successTimerInterval) {
      clearInterval(successTimerInterval);
      successTimerInterval = null;
    }
  }
  if (name === 'frames') {
    framePreviewsLoaded = {};
    loadFramePreviews();
  }
}

function updateScreenContent(s, data) {
  if (appState.screen === 'camera') {
    updateCameraDots(s.photo_slots_filled);
    updatePhotoLabel(s.photo_slots_filled + 1);
    updateGuidance(appState.guidance);
    const camErr = document.getElementById('camera-error');
    if (!appState.cameraOk) {
      camErr.style.display = 'flex';
      if (appState.cameraError) camErr.querySelector('.camera-error-text').textContent = appState.cameraError;
    } else {
      camErr.style.display = 'none';
    }
  }

  if (appState.screen === 'review') {
    for (let i = 1; i <= 3; i++) {
      const el = document.getElementById(`rev-photo-${i}`);
      if (el && (!el.src || el.src.endsWith('/'))) {
        el.src = `/api/photo_preview/${i}?t=${Date.now()}`;
      }
    }
  }

  if (appState.screen === 'payment') updatePaymentUI(data);

  if (appState.screen === 'printing') updatePrintingUI(s);
}

function updatePrintingUI(s) {
  const title = document.getElementById('printing-title');
  const status = document.getElementById('printing-status');
  const elapsedEl = document.getElementById('printing-elapsed');
  const fallback = document.getElementById('print-fallback');
  const hint = document.getElementById('print-fallback-hint');
  const elapsed = s.print_elapsed_sec || 0;
  const fallbackSec = s.print_fallback_sec || 60;

  if (title) title.textContent = 'Printing Your Photo...';
  if (status) {
    if (appState.printState === 'failed') status.textContent = 'Printing encountered an issue. You can retry from Back.';
    else if (appState.printState === 'success') status.textContent = 'Print completed.';
    else status.textContent = 'Please wait while the printer prepares your photo.';
  }
  if (elapsedEl) elapsedEl.textContent = elapsed >= 1 ? `Elapsed: ${Math.floor(elapsed)}s` : '';

  // Fallback escape: show once >= PRINT_FALLBACK_SEC or when printing failed.
  const showFallback = (elapsed >= fallbackSec) || appState.printState === 'failed';
  if (fallback) fallback.style.display = showFallback ? 'flex' : 'none';
  if (hint) hint.textContent = showFallback ? 'Printer terlalu lama? Gunakan Back untuk kembali, atau Next untuk lanjut ke layar selesai.' : '';
}

function updateCameraDots(filled) {
  for (let i = 1; i <= 3; i++) {
    const dot = document.getElementById(`dot-${i}`);
    if (!dot) continue;
    dot.className = 'photo-dot';
    if (i <= filled) dot.classList.add('done');
    else if (i === filled + 1) dot.classList.add('current');
  }
}

function updatePhotoLabel(next) {
  const el = document.getElementById('photo-num-label');
  if (el) el.textContent = `PHOTO 0${Math.min(next, 3)} / 03`;
}

function updateGuidance(text) {
  const el = document.getElementById('guidance-label');
  if (!el) return;
  if (text) {
    el.textContent = text;
    el.classList.add('visible');
  } else {
    el.classList.remove('visible');
  }
}

function updatePaymentUI(data) {
  const amtEl = document.getElementById('payment-amount-display');
  if (amtEl) amtEl.textContent = `Rp ${(appState.paymentAmount||0).toLocaleString('id-ID')}`;
  const statusEl = document.getElementById('payment-status-text');
  const spinnerEl = document.getElementById('payment-spinner');
  const qrImg = document.getElementById('payment-qr-img');
  const manualHint = document.getElementById('payment-manual-hint');
  const demoRow = document.getElementById('demo-payment-row');
  const state = appState.paymentState;
  const mode = appState.paymentMode;
  const isManual = mode === 'manual';

  // Manual QRIS: operator hint is kept hidden from public screen; operator confirms via F9 x3
  if (manualHint) manualHint.style.display = 'none';
  if (demoRow) demoRow.style.display = (mode === 'demo') ? 'flex' : 'none';

  if (state === 'pending') {
    if (statusEl) statusEl.textContent = 'Scan QRIS & Bayar';
    if (spinnerEl) spinnerEl.style.display = 'none';
    if (qrImg && !qrImg.src.startsWith('data:') && !qrImg.src.includes('/api/qris_image')) loadPaymentQR();
    if (appState.paymentExpiry && !paymentTimerInterval) {
      paymentTimerInterval = setInterval(updatePaymentTimer, 1000);
    }
  } else if (state === 'success') {
    if (statusEl) statusEl.textContent = 'Payment Verified! ✓';
    if (paymentTimerInterval) { clearInterval(paymentTimerInterval); paymentTimerInterval = null; }
  }
}

async function loadPaymentQR() {
  try {
    const res = await fetch('/api/payment_qr');
    const data = await res.json();
    const img = document.getElementById('payment-qr-img');
    if (!img) return;
    if (data.mode === 'manual') {
      img.src = '/api/qris_image';
    } else if (data.ok && data.qr_b64) {
      img.src = 'data:image/png;base64,' + data.qr_b64;
    }
  } catch(e) {}
}

function initPaymentScreen() {
  const img = document.getElementById('payment-qr-img');
  if (img) img.src = '';
  paymentTimerInterval = null;
}

function updatePaymentTimer() {
  const el = document.getElementById('payment-timer');
  if (!el || !appState.paymentExpiry) return;
  const rem = Math.max(0, Math.ceil(appState.paymentExpiry - Date.now()/1000));
  const m = Math.floor(rem/60);
  const s = rem % 60;
  el.textContent = `Expires in ${m}:${String(s).padStart(2,'0')}`;
}

function retryPayment() { doAction('use_photos'); }
function simulatePayment() {
  doAction('simulate_payment_success');
  showToast('Demo payment confirmed!');
}

async function loadFrameData() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    if (data.frames) {
      appState.frames = data.frames;
      renderFrameCards();
    }
  } catch(e) {}
}

function renderFrameCards() {
  const grid = document.getElementById('frames-grid');
  if (!grid || appState.frames.length === 0) return;
  grid.innerHTML = '';
  appState.frames.forEach((frame) => {
    const card = document.createElement('div');
    card.className = 'frame-card btn-interactive';
    card.id = `frame-card-${frame.filename}`;
    card.dataset.filename = frame.filename;
    card.onclick = () => selectFrame(frame.filename);
    card.innerHTML = `
      <div class="frame-card-preview">
        <img id="frame-prev-${frame.filename}" src="/api/frame_thumb/${frame.filename}" alt="${frame.label}">
      </div>
      <div class="frame-card-label">${frame.label}</div>
    `;
    grid.appendChild(card);
  });
  if (!appState.selectedFrame && appState.frames.length > 0) {
    selectFrame(appState.frames[0].filename);
  }
}

async function loadFramePreviews() {
  for (const frame of appState.frames) {
    if (framePreviewsLoaded[frame.filename]) continue;
    try {
      const res = await fetch('/api/frame_preview', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({filename: frame.filename})
      });
      const data = await res.json();
      if (data.ok && data.preview_b64) {
        const img = document.getElementById(`frame-prev-${frame.filename}`);
        if (img) img.src = 'data:image/png;base64,' + data.preview_b64;
        framePreviewsLoaded[frame.filename] = true;
      }
    } catch(e) {}
  }
}

function selectFrame(filename) {
  appState.selectedFrame = filename;
  document.querySelectorAll('.frame-card').forEach(c => c.classList.remove('selected'));
  document.getElementById(`frame-card-${filename}`)?.classList.add('selected');
  const btn = document.getElementById('btn-confirm-frame');
  if (btn) {
    btn.disabled = false;
    btn.style.opacity = '1';
  }
  doAction('select_frame', {filename});
}

async function loadFinalQR() {
  qrLoaded = true;
  try {
    const res = await fetch('/api/final_qr');
    const data = await res.json();
    if (data.ok && data.qr_b64) {
      document.getElementById('final-qr-img').src = 'data:image/png;base64,' + data.qr_b64;
      document.getElementById('final-qr-loading').style.display = 'none';
    }
  } catch(e) {}
}

function goHome() {
  if (successTimerInterval) {
    clearInterval(successTimerInterval);
    successTimerInterval = null;
  }
  showScreen('landing');
  doAction('reset');
}

function startSuccessCountdown() {
  if (successTimerInterval) {
    clearInterval(successTimerInterval);
    successTimerInterval = null;
  }
  let rem = 10;
  const el = document.getElementById('success-countdown');
  const update = () => {
    if (el) el.textContent = `Kembali ke Home dalam ${rem} detik...`;
    if (rem <= 0) {
      if (successTimerInterval) {
        clearInterval(successTimerInterval);
        successTimerInterval = null;
      }
      goHome();
      return;
    }
    rem--;
  };
  update();
  successTimerInterval = setInterval(update, 1000);
}

async function doAction(action, extra = {}) {
  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, ...extra})
    });
    return await res.json();
  } catch(e) {
    return {ok: false};
  }
}

function retryCamera() { doAction('retry_camera'); }

// ============================================================
// GESTURE PROCESSING PIPELINE
// ============================================================
// Explicit fist state machine: 'ARMED', 'PRESSING', 'TRIGGERED', 'WAIT_RELEASE'
let fistState = 'ARMED';
let fistPressStart = 0;
let lastFistClickTime = 0;

// Explicit peace state machine: 'NONE', 'HOLDING', 'TRIGGERED', 'WAIT_RELEASE'
let peaceState = 'NONE';
let peaceHoldStart = 0;

function resetGestureLatches() {
  fistState = 'ARMED';
  peaceState = 'NONE';
  fistPressStart = 0;
  peaceHoldStart = 0;
  clearGestureUI();
}

function updateGestureHUD() {
  const g = appState.gesture;
  const handEl = document.getElementById('gest-hand');
  const handTxt = document.getElementById('gest-hand-txt');
  if (handEl) handEl.classList.toggle('active', !!appState.handDetected);
  if (handTxt) handTxt.textContent = appState.handDetected ? 'HAND READY' : 'NO HAND';

  document.getElementById('gest-palm')?.classList.toggle('active', g === 'palm');
  document.getElementById('gest-fist')?.classList.toggle('active', g === 'fist');
  document.getElementById('gest-peace')?.classList.toggle('active', g === 'peace');
}

function updateCursorProgress(progress, isFist = true) {
  const ring = document.getElementById('cursor-progress-fill');
  if (!ring) return;
  const maxDash = 138.23;
  if (progress <= 0) {
    ring.style.strokeDashoffset = maxDash;
  } else {
    ring.style.strokeDashoffset = maxDash * (1 - Math.min(1.0, progress));
  }
}

function processGestures(cfg) {
  // If settings modal is open (admin mode), disable booth gesture actions
  if (appState.adminMode || settingsOpen) {
    resetGestureLatches();
    return;
  }

  if (!appState.handDetected || !appState.cameraOk) {
    resetGestureLatches();
    return;
  }

  const g = appState.gesture;
  const clickThresh = cfg.click_threshold_ms || 350;
  const cooldown = cfg.click_cooldown_ms || 700;
  const now = Date.now();

  // Update HUD
  updateGestureHUD();

  // Peace progress ring
  const ringOverlay = document.getElementById('peace-ring-overlay');
  const ringFill = document.getElementById('peace-ring-fill');
  if (g === 'peace' && appState.peaceProgress > 0) {
    ringOverlay.classList.add('visible');
    const offset = 376 * (1 - Math.min(1.0, appState.peaceProgress));
    ringFill.style.strokeDashoffset = offset;
    updateCursorProgress(appState.peaceProgress, false);
  } else {
    ringOverlay.classList.remove('visible');
    ringFill.style.strokeDashoffset = 376;
    if (g !== 'fist') updateCursorProgress(0.0);
  }

  // Explicit Peace State Machine: NONE -> HOLDING -> TRIGGERED -> WAIT_RELEASE
  if (g === 'peace') {
    if (peaceState === 'NONE') {
      peaceState = 'HOLDING';
      peaceHoldStart = now;
    } else if (peaceState === 'HOLDING') {
      if (appState.peaceProgress >= 1.0) {
        peaceState = 'TRIGGERED';
        handlePeaceAction();
        peaceState = 'WAIT_RELEASE';
      }
    }
    // While in WAIT_RELEASE and user still holds peace sign, do not re-trigger!
  } else {
    peaceState = 'NONE';
  }

  // Explicit Fist State Machine: ARMED -> PRESSING -> TRIGGERED -> WAIT_RELEASE
  if (g === 'fist') {
    if (fistState === 'ARMED') {
      fistState = 'PRESSING';
      fistPressStart = now;
      updateCursorProgress(0.05, true);
    } else if (fistState === 'PRESSING') {
      const held = appState.fistStableMs || (now - fistPressStart);
      const ratio = Math.min(1.0, held / clickThresh);
      updateCursorProgress(ratio, true);
      if (held >= clickThresh && (now - lastFistClickTime >= cooldown)) {
        fistState = 'TRIGGERED';
        lastFistClickTime = now;
        updateCursorProgress(1.0, true);
        handleFistClick();
        doAction('register_click');
        fistState = 'WAIT_RELEASE';
      }
    }
    // While in WAIT_RELEASE and user still holds fist, do not re-trigger!
  } else {
    // Fist released
    if (fistState === 'WAIT_RELEASE' || fistState === 'TRIGGERED') {
      if (now - lastFistClickTime >= cooldown) {
        fistState = 'ARMED';
        updateCursorProgress(0.0, true);
      }
    } else {
      fistState = 'ARMED';
      updateCursorProgress(0.0, true);
    }
  }
}

function clearGestureUI() {
  const palm = document.getElementById('gest-palm');
  if (palm) palm.classList.remove('active');
  const fist = document.getElementById('gest-fist');
  if (fist) fist.classList.remove('active');
  const peace = document.getElementById('gest-peace');
  if (peace) peace.classList.remove('active');
  const ring = document.getElementById('peace-ring-overlay');
  if (ring) ring.classList.remove('visible');
  const fill = document.getElementById('peace-ring-fill');
  if (fill) fill.style.strokeDashoffset = 376;
  updateCursorProgress(0.0);
}

function handlePeaceAction() {
  if (appState.screen === 'camera') {
    if (!countdownRunning) startClientCountdown();
  } else if (appState.screen === 'review') {
    doAction('use_photos');
  } else if (appState.screen === 'frames') {
    if (appState.selectedFrame) doAction('confirm_frame');
  } else if (appState.screen === 'qr') {
    doAction('go_success');
  } else if (appState.screen === 'success') {
    goHome();
  }
}

function handleFistClick() {
  const x = appState.cursor.x * window.innerWidth;
  const y = appState.cursor.y * window.innerHeight;
  const el = getInteractiveAt(x, y);
  if (el) {
    const cursor = document.getElementById('cursor');
    if (cursor) {
      cursor.classList.add('fist-clicked');
      setTimeout(() => cursor.classList.remove('fist-clicked'), 320);
    }
    isGestureDispatching = true;
    try {
      el.click();
      el.classList.add('hovered', 'gesture-clicked');
      setTimeout(() => el.classList.remove('gesture-clicked'), 300);
    } catch(err) {
      console.error('Gesture click error:', err);
    } finally {
      setTimeout(() => {
        isGestureDispatching = false;
      }, 60);
    }
  }
}

function getInteractiveAt(x, y) {
  const candidates = document.querySelectorAll('.btn-interactive, .frame-card, .landing-cta-btn');
  for (const el of candidates) {
    const rect = el.getBoundingClientRect();
    const pad = 16;
    if (x >= rect.left - pad && x <= rect.right + pad &&
        y >= rect.top - pad && y <= rect.bottom + pad) {
      if (!el.disabled && el.closest('.screen.active')) return el;
    }
  }
  return null;
}

async function startClientCountdown() {
  if (countdownRunning) return;
  countdownRunning = true;
  const overlay = document.getElementById('countdown-overlay');
  const numEl = document.getElementById('countdown-number');
  const duration = appState.config.countdown_duration || 3;
  overlay.classList.add('active');

  for (let i = duration; i >= 1; i--) {
    numEl.textContent = i;
    numEl.style.animation = 'none';
    void numEl.offsetWidth;
    numEl.style.animation = 'countdownPulse 1s ease';
    await new Promise(r => setTimeout(r, 1000));
    if (!countdownRunning) break;
  }

  overlay.classList.remove('active');
  if (countdownRunning) await triggerCapture();
  countdownRunning = false;
}

async function triggerCapture() {
  flashEffect();
  const res = await doAction('capture_photo');
  if (res.ok) {
    await new Promise(r => setTimeout(r, 800));
  }
}

function flashEffect() {
  const flash = document.getElementById('flash-overlay');
  flash.style.opacity = '1';
  setTimeout(() => { flash.style.opacity = '0'; }, appState.config.flash_duration_ms || 180);
}

// ============================================================
// RENDER LOOP — VIRTUAL CURSOR
// ============================================================
function renderLoop() {
  const cursor = document.getElementById('cursor');
  if (!appState.handDetected || appState.adminMode || settingsOpen || !appState.cameraOk) {
    cursor.style.opacity = '0';
  } else {
    cursor.style.opacity = '1';
    const x = appState.cursor.x * window.innerWidth;
    const y = appState.cursor.y * window.innerHeight;
    cursor.style.left = `${x}px`;
    cursor.style.top = `${y}px`;
    cursor.className = '';
    if (appState.gesture === 'fist') cursor.classList.add('fist');
    else if (appState.gesture === 'peace') cursor.classList.add('peace');
  }

  // Hover detection
  if (appState.handDetected && !settingsOpen && !appState.adminMode) {
    const x = appState.cursor.x * window.innerWidth;
    const y = appState.cursor.y * window.innerHeight;
    document.querySelectorAll('.btn-interactive, .frame-card, .landing-cta-btn').forEach(el => {
      const rect = el.getBoundingClientRect();
      const pad = 16;
      const inside = x >= rect.left - pad && x <= rect.right + pad &&
                     y >= rect.top - pad && y <= rect.bottom + pad;
      el.classList.toggle('hovered', inside && !!el.closest('.screen.active'));
    });
  }

  requestAnimationFrame(renderLoop);
}

// ============================================================
// KEYBOARD & F10 SETTINGS
// ============================================================
let settingsOpen = false;
let keyboardInitialized = false;

// F9 triple-press confirmation (manual QRIS). 3 separate keydowns within 2s.
let f9Presses = [];
let f9ResetTimer = null;
const F9_WINDOW_MS = 2000;
const F9_REQUIRED = 3;

function resetF9() {
  f9Presses = [];
  if (f9ResetTimer) { clearTimeout(f9ResetTimer); f9ResetTimer = null; }
}

function setupKeyboard() {
  if (keyboardInitialized) return;
  keyboardInitialized = true;

  window.addEventListener('keydown', (e) => {
    if (e.key === 'F9' || e.code === 'F9' || e.keyCode === 120) {
      e.preventDefault();
      e.stopPropagation();

      // F9 only active during payment state.
      if (appState.screen !== 'payment' || appState.paymentState !== 'pending') {
        resetF9();
        return false;
      }

      const now = Date.now();
      f9Presses = f9Presses.filter(t => now - t <= F9_WINDOW_MS);
      f9Presses.push(now);
      if (f9ResetTimer) { clearTimeout(f9ResetTimer); f9ResetTimer = null; }
      f9ResetTimer = setTimeout(resetF9, F9_WINDOW_MS);

      if (f9Presses.length >= F9_REQUIRED) {
        resetF9();
        showToast('Pembayaran Dikonfirmasi ✓');
        doAction('confirm_manual_payment');
      }
      return false;
    }
    if (e.key === 'F10' || e.code === 'F10' || e.keyCode === 121) {
      e.preventDefault();
      e.stopPropagation();
      toggleSettings();
      return false;
    }
    if (e.key === 'Escape' || e.code === 'Escape' || e.keyCode === 27) {
      if (settingsOpen) {
        e.preventDefault();
        e.stopPropagation();
        closeSettings();
        return false;
      }
    }
  }, true);
}

function toggleSettings() {
  if (settingsOpen) {
    closeSettings();
  } else {
    openSettings();
  }
}

function openSettings() {
  const modal = document.getElementById('settings-modal');
  if (!modal) return;
  modal.classList.add('open');
  settingsOpen = true;
  appState.adminMode = true;
  resetGestureLatches();

  fetch('/api/admin_mode', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({active: true})
  }).catch(() => {});

  loadSettingsFromServer();
  loadCamerasFromServer();
  loadDiagnosticsFromServer();
}

function closeSettings() {
  const modal = document.getElementById('settings-modal');
  if (!modal) return;
  modal.classList.remove('open');
  settingsOpen = false;
  appState.adminMode = false;
  resetGestureLatches();

  fetch('/api/admin_mode', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({active: false})
  }).catch(() => {});
}

async function loadSettingsFromServer() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    const cfg = data.config;

    for (const [key, val] of Object.entries(cfg)) {
      const input = document.getElementById(`cfg-${key}`);
      if (!input) continue;
      if (input.classList.contains('settings-toggle')) {
        input.classList.toggle('on', !!val);
      } else if (input.tagName === 'SELECT') {
        input.value = String(val);
      } else {
        input.value = val;
      }
    }

    const pSel = document.getElementById('cfg-printer_name');
    if (pSel && data.printers) {
      pSel.innerHTML = '<option value="">(System Default)</option>';
      data.printers.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p; opt.textContent = p;
        if (p === cfg.printer_name) opt.selected = true;
        pSel.appendChild(opt);
      });
    }
    applyControllerConfig(cfg);
  } catch(e) {}
}

async function loadCamerasFromServer() {
  try {
    const res = await fetch('/api/cameras');
    const data = await res.json();
    const container = document.getElementById('camera-devices-container');
    if (!container || !data.devices) return;
    container.innerHTML = '';

    data.devices.forEach(dev => {
      const card = document.createElement('div');
      card.className = `camera-device-card ${dev.selected ? 'selected' : ''}`;
      card.onclick = () => selectCameraDevice(dev.id);

      const statusBadge = dev.ready ? 'READY' : (dev.connected ? 'CONNECTED' : 'DISCONNECTED');
      const badgeCls = dev.ready ? 'ready' : (dev.connected ? 'available' : 'error');

      let dbgHtml = '';
      if (data.debug_mode || appState.debugMode) {
        dbgHtml = `<div style="font-size: 10px; color: var(--col-text-3); font-family: monospace; margin-top: 4px;">ID: ${dev.id} | DSHOW: ${dev.dshow_index ?? 'N/A'} | MSMF: ${dev.msmf_index ?? 'N/A'}</div>`;
      }

      card.innerHTML = `
        <div class="camera-card-info">
          <div class="camera-card-name">${dev.name}</div>
          <div class="camera-card-desc">${dev.description} • ${dev.resolution} @ ${dev.fps} FPS • ${dev.backend}</div>
          ${dbgHtml}
        </div>
        <div class="camera-card-badge ${badgeCls}">
          ${statusBadge}
        </div>
      `;
      container.appendChild(card);
    });

    const activeDev = data.devices.find(d => d.selected) || data.devices[0];
    if (activeDev) {
      const testInfo = document.getElementById('camera-test-info');
      if (testInfo) {
        testInfo.textContent = `Active: ${activeDev.name} (${activeDev.resolution} @ ${activeDev.fps} FPS, ${activeDev.backend}) — ${activeDev.connected ? 'CONNECTED' : 'OFFLINE'}`;
      }
    }
  } catch(e) {}
}

async function loadDiagnosticsFromServer() {
  try {
    const res = await fetch('/api/diagnostics');
    const d = await res.json();
    const cStat = document.getElementById('diag-camera-status');
    if (cStat) cStat.textContent = d.camera_status;
    const cName = document.getElementById('diag-cam-name');
    if (cName) cName.textContent = d.camera_name || 'None';
    const cRes = document.getElementById('diag-cam-res');
    if (cRes) cRes.textContent = `${d.camera_backend} • ${d.camera_resolution}`;
    const cFps = document.getElementById('diag-cam-fps');
    if (cFps) cFps.textContent = `${d.camera_fps} FPS (Fails: ${d.camera_consecutive_failures}, Rec: ${d.camera_reconnect_attempts})`;
    const cEnum = document.getElementById('diag-cam-enum');
    if (cEnum) cEnum.textContent = d.cv2_enumerate_available ? 'cv2-enumerate-cameras OK' : 'Fallback Mode';
    const cvVer = document.getElementById('diag-opencv-ver');
    if (cvVer) cvVer.textContent = d.opencv_version || '-';
    const mpVer = document.getElementById('diag-mp-ver');
    if (mpVer) mpVer.textContent = d.mediapipe_version || '-';
    const pyVer = document.getElementById('diag-py-ver');
    if (pyVer) pyVer.textContent = d.python_executable || '-';
    const pStat = document.getElementById('diag-printer-status');
    if (pStat) pStat.textContent = d.printer_ready ? 'Available (win32)' : 'Fallback (Shell)';
  } catch(e) {}
}

async function selectCameraDevice(deviceId) {
  showToast('Switching camera...');
  try {
    const res = await fetch('/api/cameras/select', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({device_id: deviceId})
    });
    const data = await res.json();
    if (data.ok) {
      showToast(`Selected: ${data.name}`);
      loadCamerasFromServer();
      loadDiagnosticsFromServer();
    } else {
      showToast('Camera switch failed');
    }
  } catch(e) {
    showToast('Camera switch error');
  }
}

async function refreshCameraList() {
  showToast('Refreshing camera list...');
  await fetch('/api/cameras/refresh', {method: 'POST'});
  loadCamerasFromServer();
  loadDiagnosticsFromServer();
}

async function testCameraDevice() {
  showToast('Testing camera stream...');
  try {
    const res = await fetch('/api/test_camera', {method: 'POST'});
    const data = await res.json();
    showToast(data.message || (data.ok ? 'Camera verified' : 'Test failed'), 3500);
    loadCamerasFromServer();
    loadDiagnosticsFromServer();
  } catch(e) {
    showToast('Camera test error');
  }
}

async function saveSettings() {
  const updates = {};
  document.querySelectorAll('[id^="cfg-"]').forEach(el => {
    const key = el.id.replace('cfg-', '');
    if (el.classList.contains('settings-toggle')) {
      updates[key] = el.classList.contains('on');
    } else if (el.type === 'number') {
      updates[key] = parseFloat(el.value) || 0;
    } else {
      updates[key] = el.value;
    }
  });

  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(updates)
    });
    Object.assign(appState.config, updates);
    applyControllerConfig(appState.config);
    showToast('Settings saved!');
    closeSettings();
  } catch(e) {
    showToast('Failed to save settings');
  }
}

function toggleSetting(el) { el.classList.toggle('on'); }

function showSettingsSection(name) {
  document.querySelectorAll('.settings-section').forEach(s => s.classList.remove('active'));
  document.getElementById(`settings-${name}`)?.classList.add('active');
  document.querySelectorAll('.settings-nav-item').forEach(item => {
    item.classList.toggle('active', item.textContent.toLowerCase().includes(name));
  });
}

async function settingsAction(action) {
  const res = await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({[action]: true})
  });
  const data = await res.json();
  if (action === 'test_printer') {
    showToast(data.ok ? `Print sent: ${data.message}` : `Print failed: ${data.message}`);
  } else if (action === 'reset_session') {
    showToast('Session reset');
    closeSettings();
  } else if (action === 'reload_frames') {
    appState.frames = [];
    showToast('Frames refreshed');
    await fetchState();
    renderFrameCards();
  } else {
    showToast('Action executed');
  }
}

let toastTimer = null;
function showToast(msg, dur = 2500) {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), dur);
}
</script>
</body>
</html>
"""

# ============================================================
# STARTUP
# ============================================================
def ensure_directories():
    FRAMES_DIR.mkdir(exist_ok=True)
    PHOTOS_DIR.mkdir(exist_ok=True)
    ASSETS_DIR.mkdir(exist_ok=True)
    dummy_src = BASE_DIR / "dummyframe.png"
    dummy_dst = FRAMES_DIR / "dummyframe.png"
    if dummy_src.exists() and not dummy_dst.exists():
        shutil.copy2(str(dummy_src), str(dummy_dst))
        print("[FRAME] Copied dummyframe.png to frames/")
    qris_path = ASSETS_DIR / "qris.png"
    if not qris_path.exists():
        try:
            qr = qrcode.QRCode(version=None,
                               error_correction=qrcode.constants.ERROR_CORRECT_M,
                               box_size=10, border=4)
            qr.add_data(f"RENCANA-TUHAN-STUDIO-MANUAL-QRIS:{uuid.uuid4().hex[:6]}")
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            img = img.resize((600, 600), Image.NEAREST)
            img.save(str(qris_path))
            print(f"[ASSET] Generated placeholder QRIS at {qris_path}")
        except Exception as e:
            print(f"[ASSET] Could not generate QRIS placeholder: {e}")

def startup():
    print("=" * 64)
    print("  RENCANA TUHAN STUDIO — PHOTO BOOTH KIOSK")
    print("=" * 64)
    ensure_directories()
    frame_manager.scan()
    camera.start()
    gesture_engine.start()

    # Background threads
    threading.Thread(target=cleanup_old_files, daemon=True, name="Cleanup").start()
    threading.Thread(target=auto_reset_watchdog, daemon=True, name="Watchdog").start()
    threading.Thread(target=camera_hotplug_watchdog, daemon=True, name="CameraHotplug").start()

    print(f"[SERVER] Starting on http://127.0.0.1:5000")
    print(f"[SERVER] Press F10 in browser for Admin Settings")
    print(f"[SERVER] Active Camera: {camera.device_name} ({camera.backend})")
    print(f"[SERVER] Demo mode: {config['demo_mode']}")
    if not MEDIAPIPE_OK:
        print("[WARN] MediaPipe not available — gesture detection is DISABLED")
    if not WIN32_AVAILABLE:
        print("[WARN] pywin32 not available — using shell printing fallback")
    print("=" * 64)

if __name__ == "__main__":
    startup()
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
