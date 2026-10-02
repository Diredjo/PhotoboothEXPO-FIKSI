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

# Cloudinary for cloud photo upload & download QR
try:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_AVAILABLE = True
    cloudinary.config(
        cloud_name="qnwklkqx",
        api_key="634594678846185",
        api_secret="MxaoT9luKZEPlN8mOp_SSbi5OHE",
        secure=True
    )
    print("[CLOUD] Cloudinary initialized successfully.")
except Exception as e:
    CLOUDINARY_AVAILABLE = False
    print(f"[CLOUD WARN] Cloudinary init failed: {e}")

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
PHOTOBOOTH_PRICE = 5000       # Price in IDR (Rp 5.000 untuk semua pilihan bingkai)
QRIS_RAW_DATA = "00020101021126610014COM.GO-JEK.WWW01189360091439590811730210G9590811730303UMI51440014ID.CO.QRIS.WWW0215ID10265981288940303UMI5204733853033605802ID5925Rencana Tuhan Studio Paym6008SIDOARJO61056127262070703A0163042877"
PAYMENT_TIMEOUT_SEC = 300     # Payment expiry in seconds
PAYMENT_POLL_INTERVAL = 2.5   # Seconds between status polls

# ---- MIDTRANS ----
MIDTRANS_SERVER_KEY = ""
MIDTRANS_CLIENT_KEY = ""
MIDTRANS_IS_PRODUCTION = False
MIDTRANS_BASE_URL = "https://api.sandbox.midtrans.com" if not MIDTRANS_IS_PRODUCTION else "https://api.midtrans.com"

# ---- PRINTER ----
PRINTER_NAME = ""             # Empty string = system default printer
PRINT_WIDTH_MM = 297          # A4 paper width (landscape: 297mm)
PRINT_HEIGHT_MM = 210         # A4 paper height (landscape: 210mm)
PRINT_DPI = 300               # Print resolution
PRINT_STRIP_HEIGHT_CM = 12.0  # Output print strip height in cm (width adjusts proportionally)
PRINT_STRIP_COPIES = 1        # Number of strips printed on A4 sheet (1 or 2)
PRINT_MEDIA_TYPE = "glossy"   # "glossy" (Photo Paper Glossy - Art Paper), "plain", "matte", "driver_default"
PRINT_QUALITY = "high"        # "high" (High Quality Photo), "standard", "driver_default"
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
FRAME_HEIGHT = 3557

# Frame slot definitions
FRAME_PRESETS = {
    "default_3slot": {
        "width": 1623,
        "height": 3556,
        "slots": [
            {"x": 40, "y": 36,   "w": 1543, "h": 1060},  # Top
            {"x": 40, "y": 1248, "w": 1543, "h": 1060},  # Middle
            {"x": 40, "y": 2460, "w": 1543, "h": 1060},  # Bottom
        ]
    },
    "FramePhotoBooth1.png": {
        "width": 1623,
        "height": 3557,
        "slots": [
            {"x": 132, "y": 300,  "w": 1359, "h": 785},  # Top Slot
            {"x": 132, "y": 1215, "w": 1359, "h": 780},  # Middle Slot
            {"x": 132, "y": 2128, "w": 1359, "h": 780},  # Bottom Slot
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
    "print_strip_height_cm": PRINT_STRIP_HEIGHT_CM,
    "print_strip_copies": PRINT_STRIP_COPIES,
    "print_media_type": PRINT_MEDIA_TYPE,
    "print_quality": PRINT_QUALITY,
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
    PREVIEW = "preview"
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
        self.a4_image_path: Optional[str] = None # A4 landscape print layout (3 strips horizontal)
        self.cloud_download_url: Optional[str] = None
        self.retake_slot_target: Optional[int] = None # 1, 2, or 3 if single-slot retake in progress
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
        self.is_paid: bool = False
        self.reset()

    def reset(self):
        self.session_id = f"RTS_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6].upper()}"
        self.state = SessionState.LANDING
        self.photos = []
        self.selected_frame = None
        self.final_image_path = None
        self.a4_image_path = None
        self.cloud_download_url = None
        self.retake_slot_target = None
        self.payment_state = "idle"
        self.payment_order_id = None
        self.payment_transaction_id = None
        self.payment_qr_data = None
        self.payment_amount = config["photobooth_price"]
        self.payment_expiry = None
        self.is_paid = False
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
            "has_final": bool(self.final_image_path and os.path.exists(self.final_image_path)),
            "a4_image_path": self.a4_image_path,
            "has_a4": bool(self.a4_image_path and os.path.exists(self.a4_image_path)),
            "cloud_download_url": self.cloud_download_url,
            "payment_state": self.payment_state,
            "is_paid": bool(getattr(self, "is_paid", False) or self.payment_state == "success"),
            "payment_order_id": self.payment_order_id,
            "payment_amount": self.payment_amount,
            "payment_expiry": self.payment_expiry,
            "print_state": self.print_state,
            "print_elapsed_sec": (
                round(time.monotonic() - self.print_started_mono, 1)
                if self.print_started_mono is not None else 0.0
            ),
            "print_fallback_sec": config["print_fallback_sec"],
            "print_strip_height_cm": config.get("print_strip_height_cm", 12.0),
            "print_strip_copies": config.get("print_strip_copies", 1),
            "countdown_active": self.countdown_active,
            "countdown_value": self.countdown_value,
            "photo_slots_filled": len(self.photos),
            "retake_slot_target": self.retake_slot_target,
        }

session = AppSession()
session_lock = threading.RLock()

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
        self.last_fist_seen: float = 0.0
        self.peace_start: Optional[float] = None
        self.last_peace_seen: float = 0.0
        self.recent_gestures = deque(maxlen=5)
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
gesture_lock = threading.RLock()

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
            # If camera engine is initialized/running or we already know devices, reuse safely
            if "camera" in globals() and camera.running:
                if camera.device_id and camera.device_id in self._devices:
                    found.append(self._devices[camera.device_id])
                elif self._devices:
                    found.extend(list(self._devices.values()))
                else:
                    found.append(CameraDeviceInfo(
                        device_id="cam_default",
                        name=camera.device_name if camera.device_name != "Unknown" else "Integrated Camera",
                        native_index=0,
                        backend_name=camera.backend or "DirectShow",
                        is_builtin=True,
                        dshow_index=0,
                        device_type="built-in"
                    ))
            elif self._devices:
                found.extend(list(self._devices.values()))
            else:
                # One-time startup probe of index 0 only when engine has NOT started
                try:
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
                except Exception:
                    pass

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

        # Test candidate open descriptors
        formats_to_try = [
            (int(config.get("camera_width", 1280)), int(config.get("camera_height", 720)), int(config.get("camera_fps", 30))),
            (1280, 720, 30),
            (640, 480, 30)
        ]
        unique_formats = []
        for fmt in formats_to_try:
            if fmt not in unique_formats:
                unique_formats.append(fmt)

        for cand_idx, cand_backend, cand_bname in open_candidates:
            cap = None
            try:
                cap = cv2.VideoCapture(cand_idx, cand_backend)
                if not cap.isOpened():
                    if cap:
                        cap.release()
                    continue

                for req_w, req_h, req_fps in unique_formats:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, req_w)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, req_h)
                    cap.set(cv2.CAP_PROP_FPS, req_fps)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                    # Quick validation with up to 3 frames
                    valid_frames = 0
                    test_frame = None
                    for _ in range(3):
                        ret, f = cap.read()
                        if ret and f is not None and f.size > 0 and f.shape[0] >= 180 and f.shape[1] >= 240:
                            valid_frames += 1
                            test_frame = f
                            break
                        time.sleep(0.010)

                    if valid_frames >= 1 and test_frame is not None:
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
                    dev = camera_device_manager.find_device(self.device_id)
                    if not dev:
                        self.camera_status = "DISCONNECTED"
                        self.error = f"Camera '{self.device_name}' disconnected."
                continue

            ret, frame = self.cap.read()
            if not ret or frame is None or frame.size == 0 or frame.shape[0] < 100:
                self.consecutive_failures += 1
                if self.consecutive_failures <= 3:
                    time.sleep(0.02)
                    continue
                elif self.consecutive_failures <= 8:
                    self.camera_status = "DEGRADED"
                    self.ready = False
                    if not self._logged_unhealthy:
                        print(f"[CAMERA] Stream degraded: frame read failed ({self.device_name})")
                        self._logged_unhealthy = True
                    time.sleep(0.05)
                    continue
                else:
                    if not self._logged_unhealthy:
                        print(f"[CAMERA] Frame read lost, reconnecting {self.device_name}...")
                        self._logged_unhealthy = True
                    self.camera_status = "RECONNECTING"
                    self.ready = False
                    self._release_cap()
                    time.sleep(0.4)
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
                time.sleep(0.006)
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

            time.sleep(0.004)

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

            # Classify raw gesture with 3D joint geometry
            raw_gesture = self._classify_gesture(landmarks)
            gesture_state.recent_gestures.append(raw_gesture)

            # Majority filter (3 of last 5 frames) to absorb momentary sensor noise
            fist_count = sum(1 for g in gesture_state.recent_gestures if g == "fist")
            peace_count = sum(1 for g in gesture_state.recent_gestures if g == "peace")
            palm_count = sum(1 for g in gesture_state.recent_gestures if g == "palm")

            if fist_count >= 3:
                effective_gesture = "fist"
            elif peace_count >= 3:
                effective_gesture = "peace"
            elif palm_count >= 3:
                effective_gesture = "palm"
            else:
                effective_gesture = raw_gesture

            gesture_state.current_gesture = effective_gesture

            # Peace timing with 220ms drop debounce
            peace_thresh = config["peace_threshold_ms"] / 1000.0
            if effective_gesture == "peace":
                if gesture_state.peace_start is None:
                    gesture_state.peace_start = now
                gesture_state.last_peace_seen = now
                elapsed = now - gesture_state.peace_start
                gesture_state.peace_progress = min(1.0, elapsed / peace_thresh)
            else:
                if now - gesture_state.last_peace_seen > 0.22:
                    gesture_state.peace_start = None
                    gesture_state.peace_progress = 0.0

            # Fist timing with 180ms drop debounce
            if effective_gesture == "fist":
                if gesture_state.fist_start is None:
                    gesture_state.fist_start = now
                gesture_state.last_fist_seen = now
            else:
                if now - gesture_state.last_fist_seen > 0.18:
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
        Robust gesture classification using 3D Euclidean distances and
        finger joint flexion/extension ratios. Invariant to hand rotation and perspective.
        """
        def dist_3d(p1, p2):
            z1 = getattr(p1, "z", 0.0)
            z2 = getattr(p2, "z", 0.0)
            return math.sqrt((p1.x - p2.x)**2 + (p1.y - p2.y)**2 + (z1 - z2)**2)

        def dist_2d(p1, p2):
            return math.sqrt((p1.x - p2.x)**2 + (p1.y - p2.y)**2)

        wrist = landmarks[0]
        palm_x = (landmarks[0].x + landmarks[9].x) / 2
        palm_y = (landmarks[0].y + landmarks[9].y) / 2
        palm_z = (getattr(landmarks[0], "z", 0.0) + getattr(landmarks[9], "z", 0.0)) / 2

        class Point3D:
            def __init__(self, x, y, z):
                self.x = x
                self.y = y
                self.z = z

        palm = Point3D(palm_x, palm_y, palm_z)

        # 4 fingers: Index (8,6,5), Middle (12,10,9), Ring (16,14,13), Pinky (20,18,17)
        finger_indices = [
            (8, 6, 5),    # Index
            (12, 10, 9),  # Middle
            (16, 14, 13), # Ring
            (20, 18, 17)  # Pinky
        ]

        extended = []
        folded = []

        for tip_idx, pip_idx, mcp_idx in finger_indices:
            tip = landmarks[tip_idx]
            pip = landmarks[pip_idx]
            mcp = landmarks[mcp_idx]

            d_tip_wrist = dist_3d(tip, wrist)
            d_pip_wrist = dist_3d(pip, wrist)
            d_tip_mcp = dist_3d(tip, mcp)
            d_pip_mcp = max(dist_3d(pip, mcp), 0.01)
            d_tip_palm = dist_3d(tip, palm)
            d_pip_palm = max(dist_3d(pip, palm), 0.01)

            # Extended: fingertip is clearly extended outward away from wrist and MCP
            is_ext = (d_tip_wrist > 1.18 * d_pip_wrist) and (d_tip_mcp > 1.35 * d_pip_mcp)

            # Folded: fingertip curled inward towards MCP / palm / wrist
            is_fld = (not is_ext) and (
                (d_tip_mcp < 1.30 * d_pip_mcp) or
                (d_tip_wrist < 1.15 * d_pip_wrist) or
                (d_tip_palm < 1.15 * d_pip_palm)
            )

            extended.append(is_ext)
            folded.append(is_fld)

        num_ext = sum(1 for e in extended if e)
        num_fld = sum(1 for f in folded if f)

        # PEACE: Index + Middle extended, Ring + Pinky folded
        if extended[0] and extended[1] and folded[2] and folded[3]:
            # Ensure index and middle tips have natural separation
            d_index_middle = dist_2d(landmarks[8], landmarks[12])
            if d_index_middle > 0.035:
                return "peace"

        # FIST: At least 3 fingers are folded AND 0 fingers are extended
        if num_fld >= 3 and num_ext == 0:
            return "fist"

        # PALM: At least 3 fingers extended
        if num_ext >= 3:
            return "palm"

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
                import win32gui
                from PIL import ImageWin

                img = Image.open(image_path)
                iw, ih = img.size

                # Configure printer DEVMODE for Landscape A4
                hdc = None
                try:
                    hPrinter = win32print.OpenPrinter(target_printer)
                    try:
                        devmode = win32print.GetPrinter(hPrinter, 2)['pDevMode']
                        devmode.Orientation = win32con.DMORIENT_LANDSCAPE
                        devmode.PaperSize = win32con.DMPAPER_A4
                        devmode.Fields = devmode.Fields | win32con.DM_ORIENTATION | win32con.DM_PAPERSIZE

                        # Color mode: Full vibrant color
                        devmode.Color = win32con.DMCOLOR_COLOR
                        devmode.Fields = devmode.Fields | win32con.DM_COLOR

                        # Media Type configuration (Photo Paper Glossy for Art Paper / Photo Paper)
                        m_type = config.get("print_media_type", "glossy")
                        if m_type == "glossy":
                            devmode.MediaType = getattr(win32con, "DMMEDIA_GLOSSY", 3)
                            devmode.Fields = devmode.Fields | win32con.DM_MEDIATYPE
                        elif m_type == "plain":
                            devmode.MediaType = getattr(win32con, "DMMEDIA_STANDARD", 1)
                            devmode.Fields = devmode.Fields | win32con.DM_MEDIATYPE
                        elif m_type == "matte":
                            devmode.MediaType = 4  # Standard Matte Paper in Windows DEVMODE
                            devmode.Fields = devmode.Fields | win32con.DM_MEDIATYPE

                        # Print Quality configuration
                        p_qual = config.get("print_quality", "high")
                        if p_qual == "high":
                            devmode.PrintQuality = win32con.DMRES_HIGH
                            devmode.Fields = devmode.Fields | win32con.DM_PRINTQUALITY
                        elif p_qual == "standard":
                            devmode.PrintQuality = win32con.DMRES_MEDIUM
                            devmode.Fields = devmode.Fields | win32con.DM_PRINTQUALITY

                        hdc_handle = win32gui.CreateDC('WINSPOOL', target_printer, devmode)
                        hdc = win32ui.CreateDCFromHandle(hdc_handle)
                        print(f"[PRINT] Initialized DC with Landscape DEVMODE on {target_printer} [Media: {m_type}, Quality: {p_qual}]")
                    finally:
                        try:
                            win32print.ClosePrinter(hPrinter)
                        except Exception:
                            pass
                except Exception as dce:
                    print(f"[PRINT WARN] DevMode landscape init failed ({dce}), fallback to default DC")
                    hdc = win32ui.CreateDC()
                    hdc.CreatePrinterDC(target_printer)

                hdc.StartDoc(f"Photo Booth A4 - {Path(image_path).name}")
                hdc.StartPage()

                pw = hdc.GetDeviceCaps(win32con.HORZRES)
                ph = hdc.GetDeviceCaps(win32con.VERTRES)

                # Fit to page preserving aspect ratio
                scale = min(pw / iw, ph / ih)
                nw, nh = int(iw * scale), int(ih * scale)
                ox, oy = (pw - nw) // 2, (ph - nh) // 2

                dib = ImageWin.Dib(img)
                dib.draw(hdc.GetHandleOutput(), (ox, oy, ox + nw, oy + nh))

                hdc.EndPage()
                hdc.EndDoc()
                hdc.DeleteDC()
                print(f"[PRINT] A4 printed successfully to {target_printer} ({pw}x{ph})")
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
        img = Image.new("RGB", (3508, 2480), color=(255, 255, 255))
        test_path = str(PHOTOS_DIR / "test_print_a4.jpg")
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

    def get_preset_for_frame(self, filename: str) -> Dict:
        """Returns the layout preset (dimensions + slots) for a given frame."""
        if filename in FRAME_PRESETS:
            return FRAME_PRESETS[filename]
        # Also check without directory if any
        base = os.path.basename(filename)
        if base in FRAME_PRESETS:
            return FRAME_PRESETS[base]
        return self.preset

    def composite(self, frame_filename: str, photo_paths: List[str]) -> Optional[str]:
        """
        Composites 3 captured photos UNDER transparent holes in the frame.
        """
        frame_info = self.get_frame(frame_filename)
        if not frame_info:
            print(f"[COMPOSITE ERROR] Frame not found: {frame_filename}")
            return None

        preset = self.get_preset_for_frame(frame_filename)
        slots = preset["slots"]
        total_w = preset["width"]
        total_h = preset["height"]

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

    def generate_a4_sheet(self, single_composite_path: str, session_id: str) -> Optional[str]:
        """
        Creates an A4 Landscape print sheet (3508 x 2480 px @ 300 DPI) containing
        the photostrip scaled to exact target height (default 10.0 cm) with width
        adjusting proportionally according to the frame aspect ratio.
        Includes high-contrast dashed cutting guidelines (garis putus-putus) tracing
        the exact frame perimeter, extension lines, and crop marks for straight,
        easy cutting.
        """
        if not single_composite_path or not os.path.exists(single_composite_path):
            print(f"[A4 COMPOSITE ERROR] Single strip not found: {single_composite_path}")
            return None
        try:
            from PIL import ImageDraw
            with Image.open(single_composite_path) as strip_img:
                strip = strip_img.convert("RGB")

            # Standard A4 Landscape at 300 DPI: 297mm x 210mm -> 3508 x 2480 px
            canvas_w, canvas_h = 3508, 2480
            a4 = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))

            dpi = int(config.get("print_dpi", 300))
            px_per_cm = dpi / 2.54

            # Target strip height: default 12.0 cm (user requirement)
            target_h_cm = float(config.get("print_strip_height_cm", 12.0))
            target_h = int(round(target_h_cm * px_per_cm))

            # Width adjusts proportionally (menyesuaikan aspek rasio)
            scale = target_h / strip.height
            target_w = int(round(strip.width * scale))
            target_w_cm = target_w / px_per_cm

            strip_resized = strip.resize((target_w, target_h), Image.Resampling.LANCZOS)

            copies = int(config.get("print_strip_copies", 1))
            copies = max(1, min(copies, 2))

            # Margin from top-left (safe printer margin: ~6.7mm = 80px)
            margin_x = 80
            margin_y = 80
            gap_x = int(round(0.8 * px_per_cm)) if copies > 1 else 0

            for i in range(copies):
                cur_x = margin_x + i * (target_w + gap_x)
                a4.paste(strip_resized, (cur_x, margin_y))

            draw = ImageDraw.Draw(a4)

            # Dashed line drawing helper for straight lines
            def draw_dashed_line(pt1, pt2, color, width=3, dash=18, gap=12):
                x_start, y_start = pt1
                x_end, y_end = pt2
                dist = math.hypot(x_end - x_start, y_end - y_start)
                if dist == 0:
                    return
                dx = (x_end - x_start) / dist
                dy = (y_end - y_start) / dist
                curr = 0.0
                while curr < dist:
                    seg_end = min(curr + dash, dist)
                    p1 = (round(x_start + dx * curr), round(y_start + dy * curr))
                    p2 = (round(x_start + dx * seg_end), round(y_start + dy * seg_end))
                    draw.line([p1, p2], fill=color, width=width)
                    curr += dash + gap

            # Colors for clear cutting guidance on white paper
            frame_line_color = (120, 125, 135)   # Clear visible dashed cutline along frame border
            ext_line_color = (180, 185, 195)     # Extended lines to guide scissors from paper edges
            crop_mark_color = (80, 85, 95)       # Solid corner crop ticks

            y1 = margin_y
            y2 = margin_y + target_h

            for i in range(copies):
                x1 = margin_x + i * (target_w + gap_x)
                x2 = x1 + target_w

                # 1. Garis putus-putus tepat di sekeliling 4 sisi frame (Top, Right, Bottom, Left)
                draw_dashed_line((x1, y1), (x2, y1), frame_line_color, width=3, dash=18, gap=12) # Atas
                draw_dashed_line((x2, y1), (x2, y2), frame_line_color, width=3, dash=18, gap=12) # Kanan
                draw_dashed_line((x2, y2), (x1, y2), frame_line_color, width=3, dash=18, gap=12) # Bawah
                draw_dashed_line((x1, y2), (x1, y1), frame_line_color, width=3, dash=18, gap=12) # Kiri

                # 2. Tanda potong sudut (Corner Crop Marks) di luar frame
                tl = 30
                off = 3
                draw.line([(x1 - off - tl, y1), (x1 - off, y1)], fill=crop_mark_color, width=2)
                draw.line([(x1, y1 - off - tl), (x1, y1 - off)], fill=crop_mark_color, width=2)
                draw.line([(x2 + off, y1), (x2 + off + tl, y1)], fill=crop_mark_color, width=2)
                draw.line([(x2, y1 - off - tl), (x2, y1 - off)], fill=crop_mark_color, width=2)
                draw.line([(x1 - off - tl, y2), (x1 - off, y2)], fill=crop_mark_color, width=2)
                draw.line([(x1, y2 + off), (x1, y2 + off + tl)], fill=crop_mark_color, width=2)
                draw.line([(x2 + off, y2), (x2 + off + tl, y2)], fill=crop_mark_color, width=2)
                draw.line([(x2, y2 + off), (x2, y2 + off + tl)], fill=crop_mark_color, width=2)

                # 3. Garis panduan perpanjangan ke tepi kertas (membantu potong lurus dari luar)
                draw_dashed_line((x1, 15), (x1, y1 - off), ext_line_color, width=2, dash=14, gap=10)
                draw_dashed_line((x2, 15), (x2, y1 - off), ext_line_color, width=2, dash=14, gap=10)
                draw_dashed_line((x1, y2 + off), (x1, y2 + 130), ext_line_color, width=2, dash=14, gap=10)
                draw_dashed_line((x2, y2 + off), (x2, y2 + 130), ext_line_color, width=2, dash=14, gap=10)

                if i == 0:
                    draw_dashed_line((15, y1), (x1 - off, y1), ext_line_color, width=2, dash=14, gap=10)
                    draw_dashed_line((15, y2), (x1 - off, y2), ext_line_color, width=2, dash=14, gap=10)
                if i == copies - 1:
                    draw_dashed_line((x2 + off, y1), (x2 + 140, y1), ext_line_color, width=2, dash=14, gap=10)
                    draw_dashed_line((x2 + off, y2), (x2 + 140, y2), ext_line_color, width=2, dash=14, gap=10)

            # Garis tengah jika 2 strip kembar
            if copies > 1:
                mid_x = margin_x + target_w + gap_x // 2
                draw_dashed_line((mid_x, 15), (mid_x, y2 + 130), (140, 145, 155), width=2, dash=16, gap=10)

            # Label teks instruksi potong
            cut_label = f"✂ GARIS POTONG (CUT LINE) — Tinggi: {target_h_cm:.1f} cm x Lebar: {target_w_cm:.1f} cm | Potong mengikuti garis putus-putus"
            try:
                draw.text((margin_x, y2 + 25), cut_label, fill=(110, 115, 125))
            except Exception:
                pass

            out_name = f"{session_id}_a4_landscape.jpg"
            out_path = str(PHOTOS_DIR / out_name)
            a4.save(out_path, quality=config["photo_jpeg_quality"], optimize=True)
            print(f"[FRAME] A4 Landscape sheet saved: {out_path} ({canvas_w}x{canvas_h}) [Strip: {target_w_cm:.2f}x{target_h_cm:.2f} cm, {copies} strip(s)] with frame-matching cutlines")
            return out_path
        except Exception as e:
            print(f"[A4 COMPOSITE ERROR] Failed to create A4 sheet: {e}")
            return None

    def generate_preview(self, frame_filename: str, photo_paths: List[str]) -> Optional[str]:
        """Low-resolution preview for interactive frame selection."""
        frame_info = self.get_frame(frame_filename)
        if not frame_info:
            return None
        try:
            preset = self.get_preset_for_frame(frame_filename)
            scale = 0.2
            pw = int(preset["width"] * scale)
            ph = int(preset["height"] * scale)
            canvas = Image.new("RGBA", (pw, ph), (245, 245, 248, 255))

            slots = preset["slots"]
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
            session.retake_slot_target = None
            session.countdown_active = False
            session.countdown_value = 0
        self.transition(SessionState.CAMERA)

    def start_retake_slot(self, slot_idx: int):
        with session_lock:
            session.retake_slot_target = slot_idx
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
            target_slot = session.retake_slot_target
            if target_slot is not None and 1 <= target_slot <= 3:
                filename = f"{session.session_id}_photo_{target_slot}.jpg"
                path = str(PHOTOS_DIR / filename)
                cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, config["photo_jpeg_quality"]])
                idx0 = target_slot - 1
                if idx0 < len(session.photos):
                    session.photos[idx0] = path
                else:
                    session.photos.append(path)
                session.touch()
                print(f"[PHOTO] Retaken single slot #{target_slot}: {path}")
                return True

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
        with session_lock:
            if getattr(session, "is_paid", False) or session.payment_state == "success":
                print(f"[PAYMENT] Sesi {session.session_id} sudah lunas, langsung lanjut ke FRAMES")
                self.transition(SessionState.FRAMES)
                return
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
                        session.is_paid = True
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
        a4_path = None
        if out_path:
            a4_path = frame_manager.generate_a4_sheet(out_path, session.session_id)
        with session_lock:
            session.final_image_path = out_path
            session.a4_image_path = a4_path

        # Upload composite image to Cloudinary with auto-download attachment flag
        if out_path and CLOUDINARY_AVAILABLE:
            try:
                sid = session.session_id
                print(f"[CLOUD] Uploading composite image to Cloudinary: {sid}...")
                upload_res = cloudinary.uploader.upload(
                    out_path,
                    folder="photobooth_expo",
                    public_id=f"photobooth_{sid}",
                    resource_type="image",
                    overwrite=True
                )
                raw_url = upload_res.get("secure_url") or upload_res.get("url")
                if raw_url:
                    # Inject fl_attachment so scanning instantly triggers the download prompt on mobile browsers!
                    download_url = raw_url.replace("/upload/", f"/upload/fl_attachment:photobooth_{sid}/")
                    with session_lock:
                        session.cloud_download_url = download_url
                    print(f"[CLOUD] Upload successful! Public Direct Download URL: {download_url}")
            except Exception as e:
                print(f"[CLOUD ERROR] Upload to Cloudinary failed: {e}")

        if out_path:
            self.transition(SessionState.PREVIEW)
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
        self.transition(SessionState.PREVIEW)

    def _do_print(self):
        with session_lock:
            fpath = session.a4_image_path
            final_path = session.final_image_path
            sid = session.session_id
        if (not fpath or not os.path.exists(fpath)) and final_path and os.path.exists(final_path):
            fpath = frame_manager.generate_a4_sheet(final_path, sid)
            with session_lock:
                session.a4_image_path = fpath
        if not fpath or not os.path.exists(fpath):
            fpath = final_path
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
        should_reset = False
        with session_lock:
            if session.state == SessionState.SUCCESS:
                should_reset = True
        if should_reset:
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
            now = time.time()
            cutoff_raw = now - SESSION_CLEANUP_MIN * 60
            cutoff_final = now - (SESSION_CLEANUP_MIN * 24 * 60)  # Keep print sheets & final photos for 24h
            for f in glob.glob(str(PHOTOS_DIR / "*")):
                if not os.path.isfile(f):
                    continue
                fn = os.path.basename(f).lower()
                mtime = os.path.getmtime(f)
                if "_a4" in fn or "_final" in fn:
                    if mtime < cutoff_final:
                        try:
                            os.remove(f)
                        except Exception:
                            pass
                else:
                    if mtime < cutoff_raw:
                        try:
                            os.remove(f)
                        except Exception:
                            pass
        except Exception:
            pass

def auto_reset_watchdog():
    while True:
        time.sleep(5)
        should_reset = False
        with session_lock:
            if session.state not in (SessionState.LANDING,):
                idle = session.idle_seconds()
                timeout = config["auto_reset_timeout"]
                if idle > timeout:
                    print(f"[SESSION] Auto-reset idle: {idle:.0f}s")
                    should_reset = True
        if should_reset:
            ctrl.full_reset()

def camera_hotplug_watchdog():
    """
    Periodically refreshes camera list to discover plugged / unplugged USB webcams.
    Runs every 5.0 seconds lightweight without interrupting stream.
    """
    while True:
        time.sleep(5.0)
        try:
            if camera.camera_status != "RECONNECTING":
                camera_device_manager.refresh_devices()
        except Exception as e:
            if config.get("debug_mode"):
                print(f"[WATCHDOG] Camera refresh error: {e}")

# ============================================================
# MJPEG STREAM GENERATOR
# ============================================================
def generate_video_frames():
    while True:
        try:
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
        except (GeneratorExit, BrokenPipeError, ConnectionResetError):
            break
        except Exception:
            time.sleep(0.05)

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
            "is_paid": bool(getattr(session, "is_paid", False) or payment_state == "success"),
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
                target_slot = session.retake_slot_target
                count = len(session.photos)
            if ok:
                if target_slot is not None:
                    # Single photo retake completed: clear target and transition back to REVIEW!
                    with session_lock:
                        session.retake_slot_target = None
                    ctrl.transition(SessionState.REVIEW)
                elif count >= 3:
                    ctrl.transition(SessionState.REVIEW)
            return jsonify({"ok": ok, "photo_count": count})

    elif action == "retake_all":
        if session.state == SessionState.REVIEW:
            ctrl.start_camera()
            return jsonify({"ok": True})

    elif action == "retake_slot":
        if session.state == SessionState.REVIEW:
            slot = int(data.get("slot", 1))
            if 1 <= slot <= 3:
                ctrl.start_retake_slot(slot)
                return jsonify({"ok": True, "slot": slot})
            return jsonify({"ok": False, "error": "Invalid slot"})

    elif action == "use_photos":
        if session.state == SessionState.REVIEW:
            with session_lock:
                already_paid = bool(getattr(session, "is_paid", False) or session.payment_state == "success")
            if already_paid:
                print(f"[ACTION] use_photos: sesi {session.session_id} sudah lunas, langsung lanjut ke FRAMES")
                ctrl.transition(SessionState.FRAMES)
            else:
                ctrl.start_payment()
            return jsonify({"ok": True})

    elif action == "back_to_review":
        if session.state in (SessionState.PAYMENT, SessionState.FRAMES, SessionState.PREVIEW):
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
        if session.state in (SessionState.PREVIEW, SessionState.QR, SessionState.PRINTING):
            ctrl.print_photo()
            return jsonify({"ok": True})

    elif action == "back_to_frames":
        if session.state in (SessionState.PREVIEW, SessionState.QR):
            ctrl.transition(SessionState.FRAMES)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Cannot back to frames from current state"})

    elif action == "show_qr":
        if session.state == SessionState.PREVIEW:
            ctrl.transition(SessionState.QR)
            return jsonify({"ok": True})

    elif action == "back_to_preview":
        if session.state == SessionState.QR:
            ctrl.transition(SessionState.PREVIEW)
            return jsonify({"ok": True})

    elif action == "back_from_print":
        if session.state == SessionState.PRINTING:
            ctrl.cancel_print()
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Not printing"})

    elif action == "skip_print":
        if session.state in (SessionState.PREVIEW, SessionState.QR, SessionState.PRINTING):
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
                with session_lock:
                    session.payment_state = "success"
                    session.is_paid = True
                return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Not in demo mode"})

    elif action == "confirm_manual_payment":
        if session.state in (SessionState.PAYMENT, SessionState.REVIEW, SessionState.FRAMES):
            with session_lock:
                session.payment_state = "success"
                session.is_paid = True
                session.payment_expiry = None
                print(f"[PAYMENT] Manual QRIS confirmed by operator: {session.session_id}")
            ctrl.stop_payment_poll()
            if session.state == SessionState.PAYMENT:
                ctrl.transition(SessionState.FRAMES)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Cannot confirm payment in current state"})

    return jsonify({"ok": False, "error": f"Unknown action: {action}"})

@app.route("/assets/<path:filename>")
def serve_assets(filename: str):
    safe_path = (ASSETS_DIR / filename).resolve()
    # Security check: ensure safe_path is within ASSETS_DIR
    if not str(safe_path).startswith(str(ASSETS_DIR.resolve())) or not safe_path.exists():
        abort(404)
    ext = safe_path.suffix.lower()
    mimetypes = {
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
    }
    mtype = mimetypes.get(ext, "application/octet-stream")
    return send_file(str(safe_path), mimetype=mtype)

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
        cloud_url = session.cloud_download_url
    if not fpath or not os.path.exists(fpath):
        return jsonify({"ok": False})

    # Prioritize Cloudinary URL so any smartphone on cellular data can scan & download immediately!
    download_url = cloud_url or f"http://127.0.0.1:5000/download/{sid}"
    qr_b64 = make_qr_b64(download_url, size=400)
    is_cloud = bool(cloud_url)
    return jsonify({
        "ok": True,
        "qr_b64": qr_b64,
        "url": download_url,
        "is_cloud": is_cloud
    })

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
    resp = send_file(path, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/api/a4_preview")
def api_a4_preview():
    with session_lock:
        path = session.a4_image_path
        final_path = session.final_image_path
        sid = session.session_id
    if (not path or not os.path.exists(path)) and final_path and os.path.exists(final_path):
        path = frame_manager.generate_a4_sheet(final_path, sid)
        with session_lock:
            session.a4_image_path = path

    if path and os.path.exists(path):
        resp = send_file(path, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    abort(404)

@app.route("/api/final_photo")
def api_final_photo():
    with session_lock:
        path = session.final_image_path
    if path and os.path.exists(path):
        resp = send_file(path, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    abort(404)

@app.route("/api/saved_photos")
def api_saved_photos():
    """Returns list of all saved photos and print sheets from PHOTOS_DIR."""
    try:
        files = []
        total_bytes = 0
        valid_exts = {".jpg", ".jpeg", ".png", ".pdf"}
        if PHOTOS_DIR.exists():
            for entry in os.scandir(PHOTOS_DIR):
                if entry.is_file():
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in valid_exts:
                        stat = entry.stat()
                        total_bytes += stat.st_size
                        fn = entry.name.lower()
                        if "_a4_landscape" in fn or "_a4" in fn:
                            category = "print_sheet"
                            category_label = "Sheet Cetak A4"
                        elif "_final" in fn:
                            category = "final_strip"
                            category_label = "Strip Foto Final"
                        elif "_photo_" in fn:
                            category = "raw_photo"
                            category_label = "Foto Sesi"
                        else:
                            category = "other"
                            category_label = "File Gambar"

                        size_kb = stat.st_size / 1024
                        size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.2f} MB"
                        mtime_dt = datetime.datetime.fromtimestamp(stat.st_mtime)
                        date_str = mtime_dt.strftime("%d/%m/%Y %H:%M:%S")

                        files.append({
                            "filename": entry.name,
                            "category": category,
                            "category_label": category_label,
                            "size_bytes": stat.st_size,
                            "size_str": size_str,
                            "mtime": stat.st_mtime,
                            "date_str": date_str,
                            "url": f"/api/saved_photo/{urllib.parse.quote(entry.name)}"
                        })

        files.sort(key=lambda x: x["mtime"], reverse=True)
        total_mb = f"{total_bytes / (1024 * 1024):.2f} MB"
        return jsonify({
            "ok": True,
            "photos": files,
            "total_count": len(files),
            "total_size": total_mb,
            "photos_dir": str(PHOTOS_DIR.resolve())
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "photos": []}), 500

@app.route("/api/saved_photo/<path:filename>")
def api_saved_photo(filename):
    """Safely serves saved photo file from PHOTOS_DIR."""
    safe_name = os.path.basename(filename)
    file_path = PHOTOS_DIR / safe_name
    if not file_path.exists():
        abort(404)
    mtype = "application/pdf" if safe_name.lower().endswith(".pdf") else "image/jpeg"
    resp = send_file(str(file_path), mimetype=mtype)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

@app.route("/api/print_saved_photo", methods=["POST"])
def api_print_saved_photo():
    """Prints a specific saved photo file to the configured printer."""
    data = request.json or {}
    filename = data.get("filename")
    if not filename:
        return jsonify({"ok": False, "error": "Filename is required"}), 400
    safe_name = os.path.basename(filename)
    file_path = str(PHOTOS_DIR / safe_name)
    if not os.path.exists(file_path):
        return jsonify({"ok": False, "error": "File does not exist"}), 404
    ok, msg = printer_manager.print_image(file_path)
    return jsonify({
        "ok": ok,
        "message": msg if ok else f"Print failed: {msg}"
    })

@app.route("/api/delete_saved_photo", methods=["POST"])
def api_delete_saved_photo():
    """Deletes a specific saved photo from PHOTOS_DIR."""
    data = request.json or {}
    filename = data.get("filename")
    if not filename:
        return jsonify({"ok": False, "error": "Filename is required"}), 400
    safe_name = os.path.basename(filename)
    file_path = PHOTOS_DIR / safe_name
    if not file_path.exists():
        return jsonify({"ok": False, "error": "File not found"}), 404
    try:
        os.remove(file_path)
        return jsonify({"ok": True, "message": f"Berhasil menghapus {safe_name}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/open_photos_folder", methods=["POST"])
def api_open_photos_folder():
    """Opens the photos directory in Windows File Explorer."""
    try:
        PHOTOS_DIR.mkdir(exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(PHOTOS_DIR.resolve()))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(PHOTOS_DIR.resolve())], check=False)
        else:
            subprocess.run(["xdg-open", str(PHOTOS_DIR.resolve())], check=False)
        return jsonify({"ok": True, "message": "Folder dibuka di file manager"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/open_printer_preferences", methods=["POST"])
def api_open_printer_preferences():
    """Opens native Windows Printer Preferences dialog for the configured or default printer."""
    try:
        data = request.json or {}
        p_name = data.get("printer_name") or config.get("printer_name") or printer_manager.get_default_printer()
        if not p_name:
            return jsonify({"ok": False, "error": "Tidak ada printer yang terdeteksi"}), 400
        if sys.platform == "win32":
            def _open():
                try:
                    subprocess.run(f'rundll32.exe printui.dll,PrintUIEntry /e /n "{p_name}"', shell=True)
                except Exception as ex:
                    print(f"[PRINT PREFS ERROR] {ex}")
            threading.Thread(target=_open, daemon=True).start()
            return jsonify({"ok": True, "message": f"Membuka preferensi printer: {p_name}"})
        return jsonify({"ok": False, "error": "Hanya didukung di Windows"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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
                session.is_paid = True
                ctrl.stop_payment_poll()
    return jsonify({"ok": True})

# ============================================================
# CASHIER MOBILE HELPER ROUTE
# ============================================================
@app.route("/cashier")
def cashier_page():
    return render_template_string(CASHIER_TEMPLATE)

CASHIER_TEMPLATE = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Kasir Booth — Rencana Tuhan Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>
  @font-face {
    font-family: 'Coolvetica';
    src: url('/assets/fonts/coolvetica.woff') format('woff');
    font-weight: 400;
    font-style: normal;
    font-display: swap;
  }
  :root {
    --font-head: 'Coolvetica', 'Poppins', sans-serif;
    --font-body: 'Poppins', sans-serif;
    --col-bg: #0D0F17;
    --col-surface: #171A26;
    --col-surface-2: #212638;
    --col-border: rgba(255, 255, 255, 0.10);
    --col-primary: #584EB8;
    --col-success: #10B981;
    --col-yellow: #FDC00F;
    --col-danger: #EF4444;
    --col-text: #FFFFFF;
    --col-text-muted: #9CA3AF;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: var(--font-body); -webkit-tap-highlight-color: transparent; }
  
  /* Head & Subhead: Coolvetica Regular */
  h1, h2, h3, h4, h5, h6,
  .brand h1,
  .amount-display,
  .status-pill,
  .badge-live {
    font-family: var(--font-head) !important;
    font-weight: normal !important;
    letter-spacing: 0.5px;
  }

  /* Deskripsi & UI: Poppins Regular - Bold */
  body, p, span, div, button, input,
  .btn-pay, .btn-sub, .order-id, .info-box, .toast {
    font-family: var(--font-body);
  }

  body {
    background: var(--col-bg);
    color: var(--col-text);
    min-height: 100vh;
    padding: 16px;
    display: flex;
    flex-direction: column;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    background: var(--col-surface);
    border: 1px solid var(--col-border);
    border-radius: 16px;
    margin-bottom: 16px;
  }
  .brand { display: flex; align-items: center; gap: 8px; }
  .brand h1 { font-size: 17px; color: #fff; }
  .badge-live {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 11px; color: var(--col-success);
    background: rgba(16, 185, 129, 0.12);
    padding: 4px 10px; border-radius: 999px;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--col-success); box-shadow: 0 0 8px var(--col-success); animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

  .card {
    background: var(--col-surface);
    border: 1px solid var(--col-border);
    border-radius: 20px;
    padding: 22px;
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    box-shadow: 0 10px 30px rgba(0,0,0,0.4);
  }
  .status-pill {
    padding: 6px 14px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 14px;
  }
  .status-waiting { background: rgba(253, 192, 15, 0.15); color: var(--col-yellow); border: 1px solid rgba(253, 192, 15, 0.3); }
  .status-idle { background: rgba(156, 163, 175, 0.15); color: var(--col-text-muted); border: 1px solid rgba(156, 163, 175, 0.2); }
  .status-paid { background: rgba(16, 185, 129, 0.15); color: var(--col-success); border: 1px solid rgba(16, 185, 129, 0.3); }

  .amount-display {
    font-size: 38px;
    font-weight: 900;
    color: #fff;
    margin: 4px 0 8px;
  }
  .order-id {
    font-size: 12px;
    color: var(--col-text-muted);
    font-family: monospace;
    background: var(--col-surface-2);
    padding: 4px 10px;
    border-radius: 8px;
    margin-bottom: 18px;
  }

  /* Big Action Button */
  .btn-pay {
    width: 100%;
    padding: 20px;
    font-size: 17px;
    font-weight: 900;
    border: none;
    border-radius: 18px;
    cursor: pointer;
    background: linear-gradient(135deg, #10B981, #059669);
    color: #fff;
    box-shadow: 0 10px 24px rgba(16, 185, 129, 0.35);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    transition: transform 0.1s, opacity 0.2s;
  }
  .btn-pay:active { transform: scale(0.97); }
  .btn-pay:disabled {
    background: #2D3345;
    color: #6B7280;
    box-shadow: none;
    cursor: not-allowed;
    opacity: 0.6;
  }

  .quick-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    width: 100%;
    margin-top: 14px;
  }
  .btn-sub {
    padding: 12px;
    font-size: 12px;
    font-weight: 700;
    border: 1px solid var(--col-border);
    border-radius: 12px;
    background: var(--col-surface-2);
    color: #fff;
    cursor: pointer;
  }
  .btn-sub:active { transform: scale(0.97); }

  .info-box {
    margin-top: auto;
    padding: 14px;
    background: rgba(88, 78, 184, 0.08);
    border: 1px solid rgba(88, 78, 184, 0.2);
    border-radius: 14px;
    font-size: 12px;
    color: var(--col-text-muted);
    text-align: center;
    line-height: 1.5;
  }
  .toast {
    position: fixed;
    top: 20px; left: 50%; transform: translateX(-50%);
    background: #10B981; color: #fff;
    font-weight: 700; font-size: 13px;
    padding: 10px 20px; border-radius: 999px;
    box-shadow: 0 8px 20px rgba(0,0,0,0.5);
    display: none; z-index: 1000;
  }
</style>
</head>
<body>
  <div class="toast" id="toast"></div>

  <div class="header">
    <div class="brand">
      <span style="font-size: 20px;">⚡</span>
      <h1>Kasir Booth Kiosk</h1>
    </div>
    <div class="badge-live">
      <span class="dot"></span>
      <span id="txt-conn">TERHUBUNG</span>
    </div>
  </div>

  <div class="card">
    <div class="status-pill status-idle" id="pill-status">MEMUAT STATUS...</div>
    <div style="font-size: 13px; color: var(--col-text-muted);">Total Tagihan Sesi Ini</div>
    <div class="amount-display" id="amt-display">Rp 5.000</div>
    <div class="order-id" id="txt-order">Order ID: -</div>

    <button class="btn-pay" id="btn-confirm" onclick="confirmPayment()" disabled>
      <span>✅ KONFIRMASI BAYAR</span>
    </button>

    <div class="quick-actions">
      <button class="btn-sub" onclick="resetKiosk()">↺ Reset Booth</button>
      <button class="btn-sub" onclick="triggerTest()">📸 Tes Sesi</button>
    </div>
  </div>

  <div class="info-box">
    💡 <b>Petunjuk Operator:</b><br>
    Saat pengunjung selesai scan QRIS di layar booth dan membayar <b>Rp 5.000</b>, tekan tombol <b>Konfirmasi Bayar</b> di atas. Layar booth akan langsung lanjut otomatis ke pemilihan frame.
  </div>

<script>
  let lastState = '';
  let isConfirming = false;

  async function pollStatus() {
    try {
      const res = await fetch('/api/state');
      if (!res.ok) return;
      const data = await res.json();
      const s = data.session || {};
      const p = data.payment || {};

      const screen = (s.state || '').toLowerCase();
      const payState = (s.payment_state || '').toLowerCase();
      const amt = p.amount || 5000;
      const order = s.payment_order_id || s.session_id || '-';

      document.getElementById('amt-display').textContent = `Rp ${Number(amt).toLocaleString('id-ID')}`;
      document.getElementById('txt-order').textContent = `Order: ${order}`;

      const pill = document.getElementById('pill-status');
      const btn = document.getElementById('btn-confirm');

      if (screen === 'payment' && payState === 'pending') {
        pill.textContent = '⏳ MENUNGGU PEMBAYARAN';
        pill.className = 'status-pill status-waiting';
        btn.disabled = false;
        btn.innerHTML = '<span>✅ TERIMA BAYAR (Rp 5.000)</span>';
        if (lastState !== 'waiting') {
          if (navigator.vibrate) navigator.vibrate([100, 50, 100]);
        }
        lastState = 'waiting';
      } else if (s.is_paid || p.is_paid || payState === 'success') {
        pill.textContent = '✓ SUDAH LUNAS';
        pill.className = 'status-pill status-paid';
        btn.disabled = true;
        btn.innerHTML = '<span>✓ PEMBAYARAN SUDAH LUNAS</span>';
        lastState = 'paid';
      } else {
        pill.textContent = `LAYAR BOOTH: ${screen.toUpperCase()}`;
        pill.className = 'status-pill status-idle';
        btn.disabled = true;
        btn.innerHTML = '<span>MENUNGGU LAYAR BAYAR...</span>';
        lastState = screen;
      }
    } catch(e) {
      document.getElementById('txt-conn').textContent = 'OFFLINE';
    }
  }

  async function confirmPayment() {
    if (isConfirming) return;
    isConfirming = true;
    showToast('Memverifikasi pembayaran...');
    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'confirm_manual_payment'})
      });
      const data = await res.json();
      if (data.ok) {
        showToast('Pembayaran Berhasil Dikonfirmasi! ✓');
        if (navigator.vibrate) navigator.vibrate(200);
      }
    } catch(e) {
      showToast('Gagal memverifikasi');
    } finally {
      setTimeout(() => { isConfirming = false; }, 800);
      pollStatus();
    }
  }

  async function resetKiosk() {
    if (!confirm('Yakin ingin mereset sesi booth ke awal?')) return;
    try {
      await fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'reset'})
      });
      showToast('Booth berhasil direset');
      pollStatus();
    } catch(e) {}
  }

  async function triggerTest() {
    try {
      await fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'start_camera'})
      });
      showToast('Sesi booth dimulai');
      pollStatus();
    } catch(e) {}
  }

  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.style.display = 'block';
    setTimeout(() => { t.style.display = 'none'; }, 2200);
  }

  setInterval(pollStatus, 1000);
  pollStatus();
</script>
</body>
</html>
"""

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
@font-face {
  font-family: 'Coolvetica';
  src: url('/assets/fonts/coolvetica.woff') format('woff');
  font-weight: 400;
  font-style: normal;
  font-display: swap;
}

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

  --font-body: 'Poppins', system-ui, sans-serif;
  --font-head: 'Coolvetica', 'Poppins', system-ui, sans-serif;
  --font: var(--font-body);
  --text-xs: clamp(10px, 1.2vw, 12px);
  --text-sm: clamp(12px, 1.4vw, 14px);
  --text-base: clamp(14px, 1.6vw, 16px);
  --text-lg: clamp(16px, 1.9vw, 20px);
  --text-xl: clamp(20px, 2.4vw, 26px);
  --text-2xl: clamp(26px, 3.2vw, 36px);
  --text-3xl: clamp(36px, 5vw, 56px);
  --text-hero: clamp(56px, 7.5vw, 96px);

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
  font-family: var(--font-body);
  background: var(--col-bg);
  color: var(--col-text);
  touch-action: none;
}

/* ============================================================
   TYPOGRAPHY SPECIFICATION (POPPINS & COOLVETICA REGULAR)
   Head & Subhead: Coolvetica Regular
   Deskripsi & UI: Poppins Regular - Bold
   ============================================================ */
h1, h2, h3, h4, h5, h6,
.landing-title,
.landing-sub,
.brand-badge,
.screen h1,
.screen h2,
.screen h3,
.camera-error-title,
.camera-count-badge,
.photo-count-badge,
.camera-guidance,
.peace-ring-label,
.review-card-hint,
.photo-zoom-header-title,
.payment-amount,
.frame-card-label,
.preview-header h2,
.preview-dimensions-pill,
.preview-cut-badge,
.qr-box h2,
.settings-header-title,
.settings-group-title,
.diag-card-title,
.gal-stat-value,
.gallery-card-badge,
.controller-status-pill,
#printing-title,
#countdown-number {
  font-family: var(--font-head) !important;
  font-weight: normal !important;
  letter-spacing: 0.5px;
}

/* Deskripsi & UI Elements: Poppins Regular - Bold */
body, p, span, a, label,
button, input, select, textarea,
.btn, .btn-interactive, .landing-cta-btn,
.guide-step,
.camera-error-text,
.settings-label, .settings-hint,
.preview-hint,
.gallery-stats-bar, .gal-stat-label, .gal-stat-pill,
.gal-filter-btn, .gal-act-btn,
.gallery-card-info, .gallery-card-filename, .gallery-card-date, .gallery-card-size,
.camera-card-name, .camera-card-desc,
.toast, #toast,
.info-box, #payment-timer, #payment-status-text, #payment-manual-hint,
#success-countdown, #printing-status, #printing-elapsed, #print-fallback-hint {
  font-family: var(--font-body);
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
  padding: clamp(24px, 4vh, 48px) 32px;
  text-align: center;
  gap: clamp(16px, 2.8vh, 30px);
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
  font-family: var(--font-head) !important;
  font-size: clamp(56px, 7.5vw, 96px) !important;
  font-weight: normal !important;
  line-height: 1.05;
  background: var(--grad-blue);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  letter-spacing: 0.5px;
  margin: 0;
}

.landing-sub {
  font-family: var(--font-head);
  font-size: clamp(16px, 1.8vw, 22px);
  font-weight: normal;
  color: var(--col-text-2);
  max-width: 640px;
  letter-spacing: 0.3px;
  line-height: 1.4;
}

.landing-cta-btn {
  display: inline-flex; align-items: center; gap: 16px;
  padding: 24px 56px;
  background: var(--grad-yellow);
  color: #111318;
  font-family: var(--font-body);
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
  position: relative;
  cursor: pointer;
  transition: transform var(--tr-fast), border-color var(--tr-fast), box-shadow var(--tr-fast);
}

.review-card:hover, .review-card.hovered {
  transform: translateY(-6px) scale(1.02);
  border-color: var(--col-yellow);
  box-shadow: var(--shadow-yellow);
}

.review-card-hint {
  position: absolute;
  bottom: 10px;
  right: 10px;
  background: rgba(17, 19, 24, 0.75);
  backdrop-filter: blur(4px);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: var(--r-full);
  pointer-events: none;
  display: flex;
  align-items: center;
  gap: 5px;
  opacity: 0.9;
  transition: opacity var(--tr-fast);
}

.review-card:hover .review-card-hint {
  opacity: 1;
  background: var(--col-blue-1);
}

.review-card img {
  width: 100%; height: 100%;
  object-fit: cover;
}

.review-actions {
  display: flex; gap: 20px;
  margin-top: 16px;
}

/* PHOTO ZOOM POPUP MODAL */
#photo-zoom-modal {
  position: fixed;
  inset: 0;
  background: rgba(10, 11, 15, 0.85);
  backdrop-filter: blur(10px);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 24px;
  animation: fadeInModal 0.2s ease-out;
}

#photo-zoom-modal.active {
  display: flex;
}

@keyframes fadeInModal {
  from { opacity: 0; transform: scale(0.97); }
  to { opacity: 1; transform: scale(1); }
}

.photo-zoom-content {
  background: var(--col-surface);
  border: 1px solid var(--col-border);
  border-radius: var(--r-xl);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4);
  max-width: 820px;
  width: 90vw;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}

.photo-zoom-header {
  padding: 18px 24px;
  border-bottom: 1px solid var(--col-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--col-surface-2);
}

.photo-zoom-header-title {
  font-size: var(--text-lg);
  font-weight: 800;
  display: flex;
  align-items: center;
  gap: 10px;
}

.photo-zoom-close-btn {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: 1px solid var(--col-border);
  background: var(--col-surface);
  color: var(--col-text);
  font-size: 18px;
  font-weight: 700;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--tr-fast);
}

.photo-zoom-close-btn:hover {
  background: var(--col-border);
  transform: rotate(90deg);
}

.photo-zoom-body {
  padding: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0b0c10;
  overflow: hidden;
}

.photo-zoom-body img {
  max-width: 100%;
  max-height: 58vh;
  object-fit: contain;
  border-radius: var(--r-md);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
}

.photo-zoom-footer {
  padding: 18px 24px;
  border-top: 1px solid var(--col-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--col-surface-2);
  gap: 16px;
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

/* LIVE PREVIEW SCREEN (A4 LANDSCAPE & SINGLE STRIP) */
#screen-preview {
  background: var(--col-bg);
  padding: 16px 28px;
  gap: 12px;
  display: none;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  width: 100%;
  height: 100vh;
  box-sizing: border-box;
  overflow-y: auto;
}

#screen-preview.active {
  display: flex;
}

.preview-header {
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

.preview-mode-switch {
  display: flex;
  background: var(--col-surface);
  border: 1px solid var(--col-border);
  border-radius: var(--r-full);
  padding: 4px;
  gap: 6px;
  margin-top: 2px;
}

.preview-tab {
  background: transparent;
  border: none;
  color: var(--col-text-muted);
  padding: 7px 18px;
  border-radius: var(--r-full);
  font-size: var(--text-sm);
  font-weight: 700;
  cursor: pointer;
  transition: all var(--tr-fast);
}

.preview-tab:hover, .preview-tab.hovered {
  color: var(--col-text);
  background: rgba(255, 255, 255, 0.08);
}

.preview-tab.active {
  background: var(--grad-blue);
  color: #fff;
  box-shadow: var(--shadow-sm);
}

.preview-stage-container {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  max-width: 960px;
  height: 56vh;
  position: relative;
  margin: 4px 0;
}

.preview-paper {
  position: relative;
  background: #ffffff;
  border-radius: 12px;
  box-shadow: 0 20px 48px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.18);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  transition: transform var(--tr-fast), box-shadow var(--tr-fast);
}

.preview-dimensions-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: rgba(253, 192, 15, 0.12);
  border: 1px solid rgba(253, 192, 15, 0.35);
  color: var(--col-yellow);
  padding: 4px 16px;
  border-radius: var(--r-full);
  font-size: var(--text-xs);
  font-weight: 700;
  margin-top: 3px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
}

.preview-paper.single-strip {
  aspect-ratio: 1623 / 3557;
  height: 100%;
}

.preview-paper.a4-sheet {
  aspect-ratio: 3508 / 2480;
  height: 100%;
  max-width: 92vw;
  background: #ffffff;
  border: 2px solid rgba(255, 255, 255, 0.35);
  box-shadow: 0 18px 45px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(0, 0, 0, 0.12);
}

.preview-paper img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
}

.preview-loader {
  position: absolute;
  inset: 0;
  background: var(--col-surface);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--col-text-2);
  font-size: var(--text-sm);
  font-weight: 600;
  z-index: 2;
}

.preview-cut-badge {
  position: absolute;
  bottom: 8px;
  background: rgba(17, 24, 39, 0.85);
  color: #f3f4f6;
  font-size: 11px;
  font-weight: 600;
  padding: 4px 12px;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.2);
  backdrop-filter: blur(4px);
  pointer-events: none;
}

.preview-actions {
  display: flex;
  gap: 16px;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  margin-top: 4px;
}

.btn-print-cta {
  padding: 16px 40px;
  font-size: var(--text-base);
  font-weight: 800;
  background: var(--grad-yellow);
  color: #111318;
  box-shadow: 0 0 25px rgba(253, 192, 15, 0.5), var(--shadow-yellow);
  animation: pulse-print-cta 2.5s infinite ease-in-out;
}

@keyframes pulse-print-cta {
  0%, 100% {
    box-shadow: 0 0 20px rgba(253, 192, 15, 0.4), var(--shadow-yellow);
  }
  50% {
    box-shadow: 0 0 35px rgba(253, 192, 15, 0.8), 0 0 10px rgba(255, 255, 255, 0.5);
    transform: scale(1.02);
  }
}

.preview-hint {
  font-size: var(--text-xs);
  color: var(--col-text-3);
  font-weight: 600;
  margin-top: 2px;
}

/* PREVIEW QR MODAL */
.preview-modal {
  position: fixed;
  inset: 0;
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.preview-modal-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  backdrop-filter: blur(8px);
}

.preview-modal-card {
  position: relative;
  background: var(--col-surface);
  border: 1px solid var(--col-border);
  border-radius: var(--r-xl);
  padding: 32px 40px;
  box-shadow: var(--shadow-lg), 0 24px 60px rgba(0,0,0,0.6);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  max-width: 420px;
  width: 100%;
  z-index: 1;
  animation: modal-pop 0.25s cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes modal-pop {
  from { opacity: 0; transform: scale(0.92); }
  to { opacity: 1; transform: scale(1); }
}

.modal-close-btn {
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid var(--col-border);
  color: var(--col-text);
  width: 36px;
  height: 36px;
  border-radius: 50%;
  font-size: 16px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--tr-fast);
}

.modal-close-btn:hover, .modal-close-btn.hovered {
  background: rgba(239, 68, 68, 0.2);
  border-color: var(--col-danger);
  color: var(--col-danger);
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
  font-family: var(--font-body);
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

/* ============================================================ */
/* FLOATING MINI CAMERA PREVIEW (TOP-LEFT)                       */
/* Visible on all screens EXCEPT #screen-camera                 */
/* ============================================================ */
#mini-camera-preview {
  position: fixed;
  top: 22px;
  left: 24px;
  width: 192px;
  height: 108px;
  border-radius: 16px;
  overflow: hidden;
  background: #0D0F14;
  border: 2px solid rgba(255, 255, 255, 0.20);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.08);
  z-index: 950;
  display: flex;
  flex-direction: column;
  opacity: 0;
  pointer-events: none;
  transform: translateY(-10px) scale(0.94);
  transition: opacity 280ms cubic-bezier(0.16, 1, 0.3, 1),
              transform 280ms cubic-bezier(0.16, 1, 0.3, 1),
              border-color 200ms ease,
              box-shadow 200ms ease;
}

#mini-camera-preview.visible {
  opacity: 1;
  pointer-events: auto;
  transform: translateY(0) scale(1);
}

/* Dynamic status feedback */
#mini-camera-preview.hand-detected {
  border-color: rgba(34, 197, 94, 0.85);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55), 0 0 18px rgba(34, 197, 94, 0.45);
}

#mini-camera-preview.gesture-action {
  border-color: var(--col-yellow);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55), 0 0 22px rgba(253, 192, 15, 0.6);
}

.mini-cam-media-wrap {
  position: relative;
  width: 100%;
  height: 100%;
  background: #000;
  overflow: hidden;
}

#mini-camera-stream {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.mini-cam-header {
  position: absolute;
  top: 7px;
  left: 7px;
  right: 7px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  pointer-events: none;
  z-index: 2;
}

.mini-cam-live-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  background: rgba(13, 15, 20, 0.78);
  backdrop-filter: blur(8px);
  border-radius: var(--r-full);
  border: 1px solid rgba(255, 255, 255, 0.18);
  font-size: 9.5px;
  font-weight: 800;
  letter-spacing: 0.5px;
  color: #fff;
  text-transform: uppercase;
}

.mini-cam-pulse-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #EF4444;
  box-shadow: 0 0 6px #EF4444;
  animation: miniCamDotPulse 1.4s infinite ease-in-out;
}

@keyframes miniCamDotPulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.35; transform: scale(0.75); }
}

@media (max-width: 900px) {
  #mini-camera-preview {
    top: 14px;
    left: 14px;
    width: 148px;
    height: 84px;
  }
}

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
  background: rgba(10, 12, 18, 0.72);
  backdrop-filter: blur(14px);
  display: none; align-items: center; justify-content: center;
  z-index: 10000;
}

#settings-modal.open { display: flex; }

.settings-panel {
  background: var(--col-surface);
  border-radius: var(--r-xl);
  width: 92vw; max-width: 980px;
  height: 86vh; max-height: 760px;
  display: flex; flex-direction: column;
  box-shadow: 0 24px 64px rgba(0,0,0,0.28), 0 0 0 1px var(--col-border);
  border: 1px solid var(--col-border);
  overflow: hidden;
  animation: modalFadeIn 200ms cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes modalFadeIn {
  from { opacity: 0; transform: scale(0.97) translateY(8px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

.settings-header {
  padding: 18px 28px;
  border-bottom: 1px solid var(--col-border);
  display: flex; justify-content: space-between; align-items: center;
  background: var(--col-surface);
  flex-shrink: 0;
}

.settings-header-left {
  display: flex; align-items: center; gap: 10px;
}

.settings-header-title {
  font-size: 18px; font-weight: 800;
  color: var(--col-text);
  letter-spacing: -0.3px;
  display: flex; align-items: center; gap: 8px;
  margin: 0;
}

.settings-header-badge {
  font-size: 11px; font-weight: 800;
  padding: 3px 9px;
  border-radius: var(--r-full);
  background: rgba(88, 78, 184, 0.10);
  color: var(--col-blue-1);
  border: 1px solid rgba(88, 78, 184, 0.22);
  letter-spacing: 0.5px;
}

.settings-close-btn {
  width: 36px; height: 36px;
  border-radius: 50%;
  border: 1px solid var(--col-border);
  background: var(--col-bg-2);
  color: var(--col-text-2);
  font-size: 16px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: all var(--tr-fast);
}

.settings-close-btn:hover {
  background: var(--col-bg-3);
  color: var(--col-text);
  transform: rotate(90deg);
}

.settings-body {
  flex: 1;
  display: flex;
  overflow: hidden;
  min-height: 0;
}

.settings-nav {
  width: 230px;
  min-width: 230px;
  border-right: 1px solid var(--col-border);
  padding: 14px 10px;
  display: flex; flex-direction: column; gap: 4px;
  overflow-y: auto;
  background: var(--col-surface-2);
  flex-shrink: 0;
}

.settings-nav::-webkit-scrollbar {
  width: 5px;
}
.settings-nav::-webkit-scrollbar-thumb {
  background: rgba(0,0,0,0.12);
  border-radius: 4px;
}

.settings-nav-item {
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 13px; font-weight: 600;
  color: var(--col-text-2);
  cursor: pointer;
  display: flex; align-items: center; gap: 10px;
  transition: all var(--tr-fast);
  user-select: none;
  border: 1px solid transparent;
}

.settings-nav-item:hover {
  background: rgba(88, 78, 184, 0.06);
  color: var(--col-text);
}

.settings-nav-item.active {
  background: #ffffff;
  color: var(--col-blue-1);
  font-weight: 800;
  border-color: rgba(88, 78, 184, 0.20);
  box-shadow: 0 2px 8px rgba(88, 78, 184, 0.08);
}

.settings-content {
  flex: 1;
  padding: 24px 32px;
  overflow-y: auto;
  overflow-x: hidden;
  background: var(--col-bg);
  min-width: 0;
}

.settings-content::-webkit-scrollbar {
  width: 6px;
}
.settings-content::-webkit-scrollbar-thumb {
  background: rgba(0,0,0,0.14);
  border-radius: 4px;
}

.settings-section { display: none; }
.settings-section.active { display: block; animation: sectionFade 150ms ease; }

@keyframes sectionFade {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

.settings-group {
  margin-bottom: 22px;
  background: var(--col-surface);
  border: 1px solid var(--col-border);
  border-radius: 14px;
  padding: 18px 22px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.02);
}

.settings-group:last-child {
  margin-bottom: 0;
}

.settings-group-title {
  font-size: 11px; font-weight: 800;
  letter-spacing: 0.8px; text-transform: uppercase;
  color: var(--col-blue-1);
  margin-bottom: 14px;
  display: flex; align-items: center; gap: 8px;
}

.settings-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid var(--col-border);
  gap: 20px;
}

.settings-row:last-child {
  border-bottom: none;
  padding-bottom: 4px;
}

.settings-label {
  flex: 1;
  min-width: 0;
}

.settings-label strong {
  display: block;
  font-size: 13px; font-weight: 700;
  color: var(--col-text);
  margin-bottom: 2px;
}

.settings-hint {
  font-size: 11px; color: var(--col-text-3);
  line-height: 1.4;
}

.settings-control {
  flex-shrink: 0;
  display: flex; align-items: center; justify-content: flex-end;
}

.settings-control input[type="text"],
.settings-control input[type="number"],
.settings-control select {
  padding: 8px 14px;
  border-radius: 8px;
  border: 1.5px solid var(--col-border);
  background: var(--col-bg-2);
  color: var(--col-text);
  font-family: var(--font); font-size: 13px; font-weight: 600;
  outline: none;
  transition: all var(--tr-fast);
  max-width: 320px;
}

.settings-control input[type="number"] {
  width: 90px;
  text-align: right;
}

.settings-control select {
  cursor: pointer;
  text-overflow: ellipsis;
}

.settings-control select:focus,
.settings-control input:focus {
  border-color: var(--col-blue-1);
  box-shadow: 0 0 0 3px rgba(88, 78, 184, 0.15);
  background: #ffffff;
}

.settings-toggle {
  width: 46px; height: 26px;
  background: var(--col-bg-3);
  border-radius: var(--r-full);
  position: relative; cursor: pointer;
  transition: background var(--tr-med);
  flex-shrink: 0;
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
  box-shadow: 0 1px 3px rgba(0,0,0,0.2);
}

.settings-toggle.on::after { transform: translateX(20px); }

/* Camera Device Cards in Settings */
.camera-cards-list {
  display: flex; flex-direction: column; gap: 10px;
  margin-bottom: 14px;
}

.camera-device-card {
  padding: 14px 18px;
  border-radius: var(--r-md);
  border: 1.5px solid var(--col-border);
  background: var(--col-bg-2);
  display: flex; justify-content: space-between; align-items: center;
  cursor: pointer;
  transition: all var(--tr-fast);
}

.camera-device-card:hover {
  border-color: var(--col-blue-1);
  background: rgba(88, 78, 184, 0.04);
}

.camera-device-card.selected {
  border-color: var(--col-blue-1);
  background: var(--grad-blue-soft);
  box-shadow: 0 0 0 2px var(--col-yellow);
}

.camera-card-info {
  display: flex; flex-direction: column; gap: 3px;
}

.camera-card-name {
  font-weight: 700; font-size: var(--text-sm);
  color: var(--col-text);
}

.camera-card-desc {
  font-size: var(--text-xs); color: var(--col-text-2);
}

.camera-card-badge {
  padding: 4px 10px;
  border-radius: var(--r-full);
  font-size: 10px; font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.camera-card-badge.ready {
  background: rgba(34,197,94,0.15);
  color: var(--col-success);
}

.camera-card-badge.available {
  background: rgba(88,78,184,0.12);
  color: var(--col-blue-1);
}

.camera-card-badge.error {
  background: rgba(239,68,68,0.15);
  color: var(--col-error);
}

/* Camera Live Test Area */
.camera-test-panel {
  background: #000;
  border-radius: var(--r-md);
  overflow: hidden;
  position: relative;
  aspect-ratio: 16 / 9;
  max-width: 520px;
  margin-top: 10px;
  border: 1px solid var(--col-border);
}

.camera-test-panel img {
  width: 100%; height: 100%;
  object-fit: cover;
}

/* Diagnostics Grid */
.diag-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px; margin-bottom: 20px;
}

.diag-card {
  padding: 14px 16px;
  background: var(--col-bg-2);
  border: 1px solid var(--col-border);
  border-radius: 12px;
  display: flex; flex-direction: column; gap: 4px;
}

.diag-card-title {
  font-size: 10px; font-weight: 700;
  color: var(--col-text-3); text-transform: uppercase;
  letter-spacing: 0.5px;
}

.diag-card-value {
  font-size: 15px; font-weight: 800;
  color: var(--col-text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* Settings Modal Footer — Dedicated bottom bar */
.settings-footer {
  height: 68px;
  padding: 0 28px;
  border-top: 1px solid var(--col-border);
  display: flex; justify-content: flex-end; align-items: center; gap: 12px;
  background: var(--col-surface);
  flex-shrink: 0;
  box-sizing: border-box;
}

.settings-footer .btn {
  height: 40px;
  min-width: 120px;
  padding: 0 24px;
  font-size: 13px; font-weight: 700;
  border-radius: var(--r-full);
  display: inline-flex; align-items: center; justify-content: center;
  transition: all var(--tr-fast);
  cursor: pointer;
}

.settings-footer .btn-ghost {
  border: 1.5px solid var(--col-border);
  color: var(--col-text-2);
  background: var(--col-bg-2);
}

.settings-footer .btn-ghost:hover {
  background: var(--col-bg-3);
  color: var(--col-text);
}

.settings-footer .btn-primary {
  background: var(--col-yellow);
  color: #111318;
  border: none;
  font-weight: 800;
  box-shadow: 0 2px 10px rgba(253, 192, 15, 0.35);
}

.settings-footer .btn-primary:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 14px rgba(253, 192, 15, 0.5);
}

/* Dark Mode Overrides for Settings Modal */
body.dark-mode .settings-panel {
  background: #151722;
  border-color: rgba(255, 255, 255, 0.12);
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.6);
}
body.dark-mode .settings-header,
body.dark-mode .settings-footer {
  background: #151722;
  border-color: rgba(255, 255, 255, 0.08);
}
body.dark-mode .settings-header-title {
  color: #fff;
}
body.dark-mode .settings-close-btn {
  background: rgba(255, 255, 255, 0.06);
  border-color: rgba(255, 255, 255, 0.1);
  color: #ccc;
}
body.dark-mode .settings-close-btn:hover {
  background: rgba(255, 255, 255, 0.12);
  color: #fff;
}
body.dark-mode .settings-nav {
  background: #0F1017;
  border-color: rgba(255, 255, 255, 0.08);
}
body.dark-mode .settings-nav-item {
  color: #9B9DAA;
}
body.dark-mode .settings-nav-item:hover {
  background: rgba(255, 255, 255, 0.05);
  color: #fff;
}
body.dark-mode .settings-nav-item.active {
  background: #1E2232;
  color: #A78BFA;
  border-color: rgba(167, 139, 250, 0.3);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}
body.dark-mode .settings-content {
  background: #11131C;
}
body.dark-mode .settings-group {
  background: #171A27;
  border-color: rgba(255, 255, 255, 0.08);
  box-shadow: none;
}
body.dark-mode .settings-row {
  border-color: rgba(255, 255, 255, 0.06);
}
body.dark-mode .settings-label strong {
  color: #fff;
}
body.dark-mode .settings-control select,
body.dark-mode .settings-control input {
  background: #0F1017;
  border-color: rgba(255, 255, 255, 0.12);
  color: #fff;
}
body.dark-mode .settings-control select:focus,
body.dark-mode .settings-control input:focus {
  background: #151722;
  border-color: #A78BFA;
  box-shadow: 0 0 0 3px rgba(167, 139, 250, 0.2);
}
body.dark-mode .diag-card {
  background: #11131C;
  border-color: rgba(255, 255, 255, 0.08);
}
body.dark-mode .diag-card-value {
  color: #fff;
}
body.dark-mode .camera-device-card {
  background: #11131C;
  border-color: rgba(255, 255, 255, 0.08);
}
body.dark-mode .settings-footer .btn-ghost {
  background: rgba(255, 255, 255, 0.06);
  border-color: rgba(255, 255, 255, 0.12);
  color: #ccc;
}
body.dark-mode .settings-footer .btn-ghost:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
}

/* ============================================================ */
/* SETTINGS GALLERY (SAVED PHOTOS & PRINTS)                      */
/* ============================================================ */
.gallery-stats-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  background: rgba(0, 0, 0, 0.04);
  border: 1px solid var(--col-border);
  border-radius: 10px;
  padding: 10px 14px;
  margin-bottom: 16px;
  font-size: 12px;
}

body.dark-mode .gallery-stats-bar {
  background: rgba(0, 0, 0, 0.25);
}

.gal-stat-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--col-text);
}

.gal-stat-label {
  color: var(--col-text-2);
}

.gal-stat-path {
  margin-left: auto;
  max-width: 400px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.gallery-filter-tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}

.gal-filter-btn {
  background: var(--col-surface-2);
  border: 1px solid var(--col-border);
  color: var(--col-text-2);
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  transition: all var(--tr-fast);
}

.gal-filter-btn:hover {
  background: rgba(88, 78, 184, 0.12);
  color: var(--col-text);
}

.gal-filter-btn.active {
  background: var(--col-blue-1);
  border-color: var(--col-blue-1);
  color: #fff;
  box-shadow: 0 2px 8px rgba(88, 78, 184, 0.3);
}

.gallery-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
}

.gallery-card {
  background: var(--col-surface);
  border: 1px solid var(--col-border);
  border-radius: 12px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  transition: transform var(--tr-fast), box-shadow var(--tr-fast), border-color var(--tr-fast);
}

.gallery-card:hover {
  transform: translateY(-3px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  border-color: rgba(88, 78, 184, 0.35);
}

body.dark-mode .gallery-card {
  background: #171A27;
  border-color: rgba(255, 255, 255, 0.08);
}

body.dark-mode .gallery-card:hover {
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  border-color: rgba(167, 139, 250, 0.4);
}

.gallery-thumb-wrap {
  position: relative;
  width: 100%;
  height: 165px;
  background: #080A0F;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  cursor: pointer;
}

.gallery-thumb {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  transition: transform var(--tr-fast);
}

.gallery-thumb-wrap:hover .gallery-thumb {
  transform: scale(1.04);
}

.gallery-card-badge {
  position: absolute;
  top: 8px;
  left: 8px;
  font-size: 9.5px;
  font-weight: 800;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 3px 8px;
  border-radius: 4px;
  backdrop-filter: blur(8px);
  z-index: 2;
}

.badge-print-sheet {
  background: rgba(14, 165, 233, 0.90);
  color: #fff;
}

.badge-final-strip {
  background: rgba(234, 179, 8, 0.92);
  color: #111;
}

.badge-raw-photo {
  background: rgba(107, 114, 128, 0.85);
  color: #fff;
}

.badge-other {
  background: rgba(139, 92, 246, 0.85);
  color: #fff;
}

.gallery-card-size {
  position: absolute;
  top: 8px;
  right: 8px;
  font-size: 9.5px;
  font-weight: 700;
  padding: 3px 7px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.70);
  color: #fff;
  backdrop-filter: blur(6px);
  z-index: 2;
}

.gallery-card-info {
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.gallery-card-date {
  font-size: 11px;
  color: var(--col-text-2);
  font-weight: 600;
}

.gallery-card-filename {
  font-family: monospace;
  font-size: 11px;
  font-weight: 700;
  color: var(--col-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.gallery-card-actions {
  display: flex;
  gap: 6px;
  padding: 8px 12px 10px;
  border-top: 1px solid var(--col-border);
  margin-top: auto;
  align-items: center;
}

.gal-act-btn {
  flex: 1;
  padding: 6px 8px;
  font-size: 11px;
  font-weight: 700;
  border-radius: 6px;
  border: 1px solid var(--col-border);
  background: var(--col-surface-2);
  color: var(--col-text);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  transition: all var(--tr-fast);
}

.gal-act-btn:hover {
  background: var(--col-blue-1);
  color: #fff;
  border-color: var(--col-blue-1);
}

.gal-act-btn.btn-print {
  background: rgba(16, 185, 129, 0.12);
  color: var(--col-success);
  border-color: rgba(16, 185, 129, 0.3);
}

.gal-act-btn.btn-print:hover {
  background: var(--col-success);
  color: #fff;
}

.gal-act-btn.btn-del {
  flex: 0 0 32px;
  color: var(--col-error);
  background: rgba(239, 68, 68, 0.08);
  border-color: rgba(239, 68, 68, 0.2);
}

.gal-act-btn.btn-del:hover {
  background: var(--col-error);
  color: #fff;
}

.gallery-empty {
  text-align: center;
  padding: 48px 24px;
  color: var(--col-text-2);
}

.gallery-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 48px 24px;
  color: var(--col-text-2);
  font-size: 13px;
  font-weight: 600;
}

/* GALLERY ZOOM MODAL */
#gallery-zoom-modal {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.88);
  backdrop-filter: blur(12px);
  z-index: 12000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.gallery-zoom-card {
  background: var(--col-surface);
  border: 1px solid var(--col-border);
  border-radius: 16px;
  max-width: 90vw;
  max-height: 90vh;
  width: 820px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.6);
}

body.dark-mode .gallery-zoom-card {
  background: #171A27;
}

.gallery-zoom-header {
  padding: 12px 18px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--col-border);
}

.gallery-zoom-body {
  flex: 1;
  min-height: 0;
  background: #080A0F;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  overflow: auto;
}

#gal-zoom-img {
  max-width: 100%;
  max-height: 62vh;
  object-fit: contain;
  border-radius: 6px;
}

.gallery-zoom-footer {
  padding: 12px 18px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-top: 1px solid var(--col-border);
  flex-wrap: wrap;
  gap: 10px;
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
      <img id="camera-stream" src="/video_feed" alt="Live Feed" onerror="setTimeout(ensureCameraStream, 1500)">

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
    <h2 style="font-size: var(--text-2xl);">Review Your Photos</h2>
    <p style="font-size: var(--text-sm); color: var(--col-text-2); margin-top: -12px;">Klik pada foto untuk melihat lebih jelas (zoom) atau mengulang foto tersebut.</p>
    <div id="rev-paid-badge" style="display: none; align-items: center; gap: 8px; background: rgba(16, 185, 129, 0.12); border: 1.5px solid rgba(16, 185, 129, 0.35); color: var(--col-success); padding: 8px 20px; border-radius: var(--r-full); font-weight: 700; font-size: var(--text-sm); margin-top: -6px;">
      <span style="display:inline-flex; align-items:center; justify-content:center; width:22px; height:22px; background:var(--col-success); color:#fff; border-radius:50%; font-size:12px; font-weight:900;">✓</span>
      <span>Pembayaran Terkonfirmasi — Foto ulang sepuasnya tanpa bayar lagi</span>
    </div>
    <div class="review-grid">
      <div class="review-card btn-interactive" onclick="openPhotoZoom(1)">
        <img id="rev-photo-1" src="" alt="Photo 1">
        <div class="review-card-hint">🔍 Zoom / Retake</div>
      </div>
      <div class="review-card btn-interactive" onclick="openPhotoZoom(2)">
        <img id="rev-photo-2" src="" alt="Photo 2">
        <div class="review-card-hint">🔍 Zoom / Retake</div>
      </div>
      <div class="review-card btn-interactive" onclick="openPhotoZoom(3)">
        <img id="rev-photo-3" src="" alt="Photo 3">
        <div class="review-card-hint">🔍 Zoom / Retake</div>
      </div>
    </div>
    <div class="review-actions">
      <button class="btn btn-ghost btn-interactive" onclick="doAction('retake_all')">↺ Retake All</button>
      <button class="btn btn-primary btn-interactive" id="btn-use-photos" onclick="doAction('use_photos')">Continue to Payment →</button>
    </div>
  </div>

  <!-- PHOTO ZOOM & RETAKE MODAL -->
  <div id="photo-zoom-modal" onclick="onZoomModalBgClick(event)">
    <div class="photo-zoom-content" onclick="event.stopPropagation()">
      <div class="photo-zoom-header">
        <div class="photo-zoom-header-title">
          <span>🔍</span>
          <span id="photo-zoom-title">Foto 01</span>
        </div>
        <button class="photo-zoom-close-btn btn-interactive" onclick="closePhotoZoom()">✕</button>
      </div>
      <div class="photo-zoom-body">
        <img id="photo-zoom-img" src="" alt="Zoomed Photo">
      </div>
      <div class="photo-zoom-footer">
        <button class="btn btn-secondary btn-interactive" id="btn-retake-single" onclick="retakeCurrentZoomedPhoto()">
          ↺ Retake Foto Ini Saja
        </button>
        <button class="btn btn-ghost btn-interactive" onclick="closePhotoZoom()">Tutup</button>
      </div>
    </div>
  </div>

  <!-- PAYMENT SCREEN -->
  <div class="screen" id="screen-payment">
    <div class="brand-badge">QRIS Payment</div>
    <div class="payment-card">
      <div class="payment-amount" id="payment-amount-display">Rp 5.000</div>
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
    <h2 style="font-size: var(--text-2xl);">Choose Your Studio Frame</h2>
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
    <h2 style="font-size: var(--text-xl);">Crafting Your High-Res Photo...</h2>
    <p style="color: var(--col-text-2);">Applying frame styling and optimizing print colors.</p>
  </div>

  <!-- LIVE PREVIEW SCREEN (SINGLE STRIP FRAME) -->
  <div class="screen" id="screen-preview">
    <div class="preview-header">
      <div class="brand-badge">Live Preview Foto</div>
      <h2 style="font-size: var(--text-2xl); margin: 4px 0 0 0;">Preview Hasil Foto Anda</h2>
      <p style="color: var(--col-text-2); font-size: var(--text-sm); margin: 0;">
        Periksa hasil foto Anda di bawah ini sebelum dicetak.
      </p>
      <div class="preview-dimensions-pill" id="preview-dimensions-tag">
        📏 Ukuran Cetak: <strong>12.0 cm</strong> tinggi (Dilengkapi Garis Potong)
      </div>
    </div>

    <!-- Preview Stage / 1 Lembar Frame Mockup -->
    <div class="preview-stage-container">
      <div class="preview-paper single-strip" id="preview-paper-strip">
        <div class="preview-loader" id="preview-sheet-loader">
          <div class="spinner"></div>
          <span id="preview-loader-text">Menyiapkan Preview Foto...</span>
        </div>
        <img id="preview-sheet-img" src="" alt="Live Preview Foto Strip" onload="onPreviewImgLoaded('sheet')">
        <div class="preview-cut-badge" id="preview-badge-status">✨ Siap Dicetak (1 Lembar Strip • 12 cm)</div>
      </div>
    </div>

    <!-- Action Bar Buttons -->
    <div class="preview-actions">
      <button class="btn btn-ghost btn-interactive" onclick="doAction('back_to_frames')" title="Kembali ke pemilihan frame">
        <span>🖼️ Ubah Frame</span>
      </button>
      <button class="btn btn-secondary btn-interactive" onclick="openDownloadModal()" title="Download foto ke HP lewat QR Code">
        <span>📱 Scan QR / Download</span>
      </button>
      <button class="btn btn-primary btn-interactive btn-print-cta" onclick="doAction('print_photo')" title="Cetak langsung ke printer EPSON A4 (Tinggi 12cm)">
        <span style="font-size: 20px;">🖨️</span>
        <span>Cetak Sekarang (Print 12cm)</span>
      </button>
    </div>

    <div class="preview-hint">
      Arahkan kursor & kepalkan tangan (✊) atau tahan pose Peace (✌️) untuk memilih
    </div>

    <!-- Quick Download Modal -->
    <div id="preview-qr-modal" class="preview-modal" style="display: none;">
      <div class="preview-modal-backdrop" onclick="closeDownloadModal()"></div>
      <div class="preview-modal-card">
        <div style="display:flex; justify-content:space-between; align-items:center; width:100%;">
          <div class="brand-badge" style="margin:0;">Download Digital</div>
          <button class="btn-interactive modal-close-btn" onclick="closeDownloadModal()">✕</button>
        </div>
        <h3 style="font-size: var(--text-xl); margin: 8px 0 0 0;">Scan QR untuk Unduh Foto</h3>
        <p style="color: var(--col-text-2); font-size: var(--text-xs); margin: 0; text-align: center;">
          Scan dengan kamera HP untuk download foto resolusi tinggi
        </p>
        <div class="payment-qr-wrapper" style="margin: 8px 0;">
          <img id="modal-qr-img" src="" alt="Download QR">
          <div id="modal-qr-loading" class="spinner"></div>
        </div>
        <div id="modal-qr-cloud-badge" style="font-size: var(--text-xs); color: #22c55e; font-weight: 600; display: none;">
          ⚡ Direct Download Ready (Auto-download)
        </div>
        <button class="btn btn-primary btn-interactive" onclick="closeDownloadModal()" style="margin-top: 6px; padding: 12px 28px;">
          Tutup & Kembali ke Preview
        </button>
      </div>
    </div>
  </div>

  <!-- QR DOWNLOAD SCREEN -->
  <div class="screen" id="screen-qr">
    <div class="brand-badge">Download & Print</div>
    <div class="qr-box">
      <h2 style="font-size: var(--text-xl);">Scan QR to Download Photo</h2>
      <div class="payment-qr-wrapper">
        <img id="final-qr-img" src="" alt="Download QR">
        <div id="final-qr-loading" class="spinner"></div>
      </div>
      <div id="final-qr-cloud-badge" style="font-size: var(--text-xs); color: #22c55e; font-weight: 600; margin-top: 4px; display: none;">
        ✨ Direct Download Ready (Auto-download on scan)
      </div>
      <div style="display: flex; gap: 16px; margin-top: 12px; flex-wrap: wrap; justify-content: center;">
        <button class="btn btn-ghost btn-interactive" onclick="doAction('back_to_preview')">← Preview Cetak</button>
        <button class="btn btn-secondary btn-interactive" onclick="doAction('print_photo')">🖨 Print Photo</button>
        <button class="btn btn-primary btn-interactive" onclick="doAction('go_success')">Done ✓</button>
      </div>
    </div>
  </div>

  <!-- PRINTING SCREEN -->
  <div class="screen" id="screen-printing">
    <div class="spinner"></div>
    <h2 style="font-size: var(--text-xl);" id="printing-title">Printing Your Photo...</h2>
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
    <h1 style="font-size: var(--text-3xl); color: var(--col-blue-1);">Thank You!</h1>
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

<!-- Floating Mini Camera Preview (Top-Left, visible on all screens except main camera) -->
<div id="mini-camera-preview" class="visible" aria-label="Mini Camera Live Preview">
  <div class="mini-cam-media-wrap">
    <img id="mini-camera-stream" src="/video_feed" alt="Mini Live Feed" onerror="setTimeout(ensureMiniCameraStream, 2000)">
    <div class="mini-cam-header">
      <div class="mini-cam-live-tag">
        <span class="mini-cam-pulse-dot"></span>
        <span>LIVE</span>
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
      <div class="settings-header-left">
        <h2 class="settings-header-title">⚙️ Admin Settings</h2>
        <span class="settings-header-badge">F10 Kiosk Menu</span>
      </div>
      <button class="settings-close-btn" onclick="closeSettings()" title="Tutup (Esc)">✕</button>
    </div>
    <div class="settings-body">
      <div class="settings-nav">
        <div class="settings-nav-item active" onclick="showSettingsSection('controller')">🎮 Controller</div>
        <div class="settings-nav-item" onclick="showSettingsSection('camera')">📷 Camera</div>
        <div class="settings-nav-item" onclick="showSettingsSection('diagnostics')">🧪 Diagnostics</div>
        <div class="settings-nav-item" onclick="showSettingsSection('gesture')">🤚 Gesture</div>
        <div class="settings-nav-item" onclick="showSettingsSection('display')">🖥 Display</div>
        <div class="settings-nav-item" onclick="showSettingsSection('print')">🖨 Print</div>
        <div class="settings-nav-item" data-section="gallery" onclick="showSettingsSection('gallery')" style="background: rgba(253, 192, 15, 0.12); color: var(--col-yellow); font-weight: 700; border: 1px solid rgba(253, 192, 15, 0.25);">📁 Galeri Cetak</div>
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
                <span class="settings-hint">Kiosk dioperasikan melalui gestur tangan (touchpad dapat dikunci).</span>
              </div>
              <div class="settings-control">
                <select id="cfg-controller_mode">
                  <option value="gesture_only">Full Gesture Tangan (Touchpad Diblokir)</option>
                  <option value="hybrid">Hybrid (Gesture + Touchpad/Mouse)</option>
                </select>
              </div>
            </div>
            <div class="settings-row">
              <div class="settings-label">
                <strong>Blokir Touchpad & Mouse Fisik</strong>
                <span class="settings-hint">Nonaktifkan semua klik dan gerakan touchpad pada layar photobooth.</span>
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
              <div class="gesture-guide-card">
                <span style="font-size: 24px;">✋</span>
                <div>
                  <strong style="font-size: 13px;">Telapak Tangan Terbuka (Open Palm)</strong>
                  <div class="settings-hint">Menggerakkan kursor virtual di layar. Arahkan ke tombol yang ingin dipilih.</div>
                </div>
              </div>
              <div class="gesture-guide-card">
                <span style="font-size: 24px;">✊</span>
                <div>
                  <strong style="font-size: 13px;">Kepalan Tangan (Hold Fist 350ms)</strong>
                  <div class="settings-hint">Tahan kepalan tangan untuk mengisi ring dan mengeklik tombol yang diarahkan.</div>
                </div>
              </div>
              <div class="gesture-guide-card">
                <span style="font-size: 24px;">✌️</span>
                <div>
                  <strong style="font-size: 13px;">Pose Dua Jari (Peace Sign 1.2s)</strong>
                  <div class="settings-hint">Tahan pose peace untuk memulai countdown foto 3 detik atau konfirmasi layar.</div>
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
              <img id="settings-camera-stream" src="" alt="Camera Test Preview">
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
              <div class="settings-label">
                <div>Media Kertas (Tipe Kertas)</div>
                <div class="settings-hint" style="font-size: 11px; color: var(--col-text-3);">Pilih Photo Paper (Glossy) untuk kertas Art Paper agar semprotan tinta pekat dan tidak luntur</div>
              </div>
              <div class="settings-control">
                <select id="cfg-print_media_type">
                  <option value="glossy">Photo Paper (Glossy) — Art Paper / Kertas Foto</option>
                  <option value="plain">Plain Paper — Kertas HVS / Kertas Biasa</option>
                  <option value="matte">Kertas Foto (Matte / Doff)</option>
                  <option value="driver_default">Sesuai Bawaan Driver Printer</option>
                </select>
              </div>
            </div>
            <div class="settings-row">
              <div class="settings-label">
                <div>Kualitas Cetak (Print Quality)</div>
                <div class="settings-hint" style="font-size: 11px; color: var(--col-text-3);">High Quality direkomendasikan untuk hasil cetak foto booth</div>
              </div>
              <div class="settings-control">
                <select id="cfg-print_quality">
                  <option value="high">Kualitas Tinggi (High / Foto Pekat & Tajam)</option>
                  <option value="standard">Kualitas Standar (Normal)</option>
                  <option value="driver_default">Sesuai Bawaan Driver Printer</option>
                </select>
              </div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Tinggi Strip Output (cm)</div>
              <div class="settings-control">
                <input type="number" id="cfg-print_strip_height_cm" step="0.5" min="5" max="25" style="width: 80px;">
              </div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Jumlah Strip pada Kertas A4</div>
              <div class="settings-control">
                <select id="cfg-print_strip_copies">
                  <option value="1">1 Strip (Hemat Kertas & Tinta)</option>
                  <option value="2">2 Strips (Twin Strip Kembar)</option>
                </select>
              </div>
            </div>
            <div class="settings-row">
              <div class="settings-label">Auto Print on Complete</div>
              <div class="settings-control"><div class="settings-toggle" id="cfg-auto_print" onclick="toggleSetting(this)"></div></div>
            </div>

            <!-- Quick Driver Access Button -->
            <div style="margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--col-border); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
              <div>
                <strong style="font-size: 12px; display: block; color: var(--col-text);">Pengaturan Driver Windows (EPSON)</strong>
                <span style="font-size: 11px; color: var(--col-text-2);">Buka dialog properti printer bawaan Windows untuk mengatur Paper Tray, Borderless, dll.</span>
              </div>
              <button class="btn btn-ghost" onclick="openPrinterPreferences()" style="font-size: 12px; padding: 8px 16px;">
                ⚙️ Buka Driver Preferences
              </button>
            </div>
          </div>
        </div>

        <!-- GALLERY / SAVED PHOTOS -->
        <div class="settings-section" id="settings-gallery">
          <div class="settings-group">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; margin-bottom: 16px;">
              <div>
                <div class="settings-group-title" style="margin: 0; font-size: 14px;">📁 Galeri Hasil Cetak & Foto Sesi</div>
                <p style="font-size: 12px; color: var(--col-text-2); margin-top: 4px;">
                  Daftar seluruh lembar cetak A4 dan strip foto yang tersimpan di laptop. Anda dapat melihat, mencetak ulang ke printer, atau membuka foldernya langsung.
                </p>
              </div>
              <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                <button class="btn btn-ghost" onclick="loadSavedPhotos()" title="Muat ulang daftar foto" style="padding: 8px 14px; font-size: 12px;">
                  🔄 Refresh
                </button>
                <button class="btn btn-secondary" onclick="openPhotosFolderInExplorer()" title="Buka folder photos di Windows Explorer" style="padding: 8px 14px; font-size: 12px;">
                  📂 Buka Folder di Laptop
                </button>
              </div>
            </div>

            <!-- Stats & Info Bar -->
            <div class="gallery-stats-bar">
              <div class="gal-stat-pill">
                <span class="gal-stat-label">Total File:</span>
                <strong id="gal-total-count">0</strong>
              </div>
              <div class="gal-stat-pill">
                <span class="gal-stat-label">Ukuran Disk:</span>
                <strong id="gal-total-size">0 MB</strong>
              </div>
              <div class="gal-stat-pill gal-stat-path">
                <span class="gal-stat-label">Lokasi:</span>
                <code id="gal-folder-path" title="Lokasi folder penyimpanan">photos/</code>
              </div>
            </div>

            <!-- Category Filter Tabs -->
            <div class="gallery-filter-tabs">
              <button class="gal-filter-btn active" data-filter="all" onclick="filterGallery('all')">
                Semua File (<span id="gal-filter-count-all">0</span>)
              </button>
              <button class="gal-filter-btn" data-filter="print_sheet" onclick="filterGallery('print_sheet')">
                🖨️ Sheet Cetak A4 12cm (<span id="gal-filter-count-print_sheet">0</span>)
              </button>
              <button class="gal-filter-btn" data-filter="final_strip" onclick="filterGallery('final_strip')">
                🎞️ Strip Frame Final (<span id="gal-filter-count-final_strip">0</span>)
              </button>
              <button class="gal-filter-btn" data-filter="raw_photo" onclick="filterGallery('raw_photo')">
                📷 Foto Sesi (<span id="gal-filter-count-raw_photo">0</span>)
              </button>
            </div>

            <!-- Loading & Empty States -->
            <div id="gallery-loading" class="gallery-loading" style="display: none;">
              <div class="spinner"></div>
              <span>Memuat daftar foto dari laptop...</span>
            </div>

            <div id="gallery-empty" class="gallery-empty" style="display: none;">
              <div style="font-size: 36px; margin-bottom: 8px;">📭</div>
              <div style="font-weight: 700; font-size: 14px;">Belum Ada Foto Tersimpan</div>
              <div style="font-size: 12px; color: var(--col-text-3); margin-top: 4px;">
                Foto akan otomatis tersimpan di folder ini setiap kali sesi foto atau tombol print dijalankan.
              </div>
            </div>

            <!-- Grid of Cards -->
            <div class="gallery-grid" id="gallery-grid"></div>
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

<!-- GALLERY FULL IMAGE MODAL -->
<div id="gallery-zoom-modal" style="display: none;" onclick="closeGalleryZoom()">
  <div class="gallery-zoom-card" onclick="event.stopPropagation()">
    <div class="gallery-zoom-header">
      <div style="display: flex; align-items: center; gap: 8px;">
        <span id="gal-zoom-badge" class="badge" style="background:var(--col-blue-1); color:#fff; font-size:10px; padding:3px 8px; border-radius:4px; font-weight:800;">A4 SHEET</span>
        <span id="gal-zoom-filename" style="font-weight: 700; font-size: 13px; font-family: monospace;">filename.jpg</span>
      </div>
      <button class="settings-close-btn" onclick="closeGalleryZoom()">✕</button>
    </div>
    <div class="gallery-zoom-body">
      <img id="gal-zoom-img" src="" alt="Zoom Photo">
    </div>
    <div class="gallery-zoom-footer">
      <div style="font-size: 12px; color: var(--col-text-2);" id="gal-zoom-info">315 KB • 01/10/2026 09:24</div>
      <div style="display: flex; gap: 8px;">
        <a id="gal-zoom-download-btn" href="" target="_blank" download class="btn btn-ghost" style="padding: 7px 16px; font-size: 12px; text-decoration: none;">
          ⬇ Download / Buka
        </a>
        <button id="gal-zoom-print-btn" class="btn btn-primary" onclick="printSavedPhotoFromZoom()" style="padding: 7px 18px; font-size: 12px;">
          🖨 Cetak ke Printer Sekarang
        </button>
      </div>
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

let isFetchingCursor = false;
async function fetchCursor() {
  if (isFetchingCursor) return;
  isFetchingCursor = true;
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
    processGestures(appState.config);
  } catch (e) {
  } finally {
    isFetchingCursor = false;
  }
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

let isFetchingState = false;
async function fetchState() {
  if (isFetchingState) return;
  isFetchingState = true;
  try {
    const res = await fetch('/api/state');
    if (!res.ok) return;
    const data = await res.json();
    updateState(data);
  } catch (e) {
  } finally {
    isFetchingState = false;
  }
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
  appState.isPaid = !!(s.is_paid || (data.payment && data.payment.is_paid) || s.payment_state === 'success');
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

function updateMiniCamera(screenName) {
  const miniCam = document.getElementById('mini-camera-preview');
  const miniStream = document.getElementById('mini-camera-stream');
  if (!miniCam) return;

  const isCamera = (screenName === 'camera');
  if (isCamera) {
    if (miniCam.classList.contains('visible')) {
      miniCam.classList.remove('visible');
    }
    // Stop mini camera stream to save resources when on main camera screen
    if (miniStream && miniStream.src && miniStream.src.indexOf('/video_feed') !== -1) {
      miniStream.src = '';
    }
  } else {
    if (!miniCam.classList.contains('visible')) {
      miniCam.classList.add('visible');
    }
    // Ensure stream is active on other screens
    if (miniStream && (!miniStream.src || miniStream.src.indexOf('/video_feed') === -1)) {
      miniStream.src = '/video_feed?t=' + Date.now();
    }
  }
}

function ensureMiniCameraStream() {
  const miniCam = document.getElementById('mini-camera-preview');
  const miniStream = document.getElementById('mini-camera-stream');
  if (miniCam && miniStream && appState.screen !== 'camera') {
    miniStream.src = '/video_feed?t=' + Date.now();
  }
}

function showScreen(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  const target = document.getElementById(`screen-${name}`);
  if (target) {
    target.classList.add('active');
    appState.screen = name;
    updateMiniCamera(name);
  }
}

function transitionToScreen(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  const target = document.getElementById(`screen-${name}`);
  if (!target) return;
  target.classList.add('active');
  updateMiniCamera(name);

  if (name === 'camera') {
    const camStream = document.getElementById('camera-stream');
    if (camStream && (!camStream.src || camStream.src.indexOf('/video_feed') === -1)) {
      camStream.src = '/video_feed?t=' + Date.now();
    }
  }

  if (appState.screen === 'payment' && name !== 'payment') resetF9();
  if (name === 'preview') loadLivePreview();
  if (name === 'qr' && !qrLoaded) loadFinalQR();
  if (name === 'payment') initPaymentScreen();
  if (name === 'success') startSuccessCountdown();
  if (name === 'landing') {
    qrLoaded = false;
    livePreviewLoaded = false;
    closeDownloadModal();
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
  if (name === 'review') {
    closePhotoZoom();
    for (let i = 1; i <= 3; i++) {
      const el = document.getElementById(`rev-photo-${i}`);
      if (el) el.src = `/api/photo_preview/${i}?t=${Date.now()}`;
    }
  }
}

function updateScreenContent(s, data) {
  if (appState.screen === 'camera') {
    const retakeSlot = s.retake_slot_target;
    if (retakeSlot) {
      updatePhotoLabelText(`RETAKE PHOTO 0${retakeSlot}`);
      updateCameraDotsForRetake(retakeSlot);
    } else {
      updateCameraDots(s.photo_slots_filled);
      updatePhotoLabel(s.photo_slots_filled + 1);
    }
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
    const isPaid = !!(s.is_paid || (data.payment && data.payment.is_paid) || s.payment_state === 'success' || appState.isPaid);
    const btnUse = document.getElementById('btn-use-photos');
    if (btnUse) {
      if (isPaid) {
        btnUse.innerHTML = '<span>Lanjut Pilih Frame (Lunas ✓) →</span>';
      } else {
        btnUse.innerHTML = '<span>Continue to Payment →</span>';
      }
    }
    const badgePaid = document.getElementById('rev-paid-badge');
    if (badgePaid) {
      badgePaid.style.display = isPaid ? 'inline-flex' : 'none';
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

function updatePhotoLabelText(text) {
  const el = document.getElementById('photo-num-label');
  if (el) el.textContent = text;
}

function updateCameraDotsForRetake(targetSlot) {
  for (let i = 1; i <= 3; i++) {
    const dot = document.getElementById(`dot-${i}`);
    if (!dot) continue;
    dot.className = 'photo-dot';
    if (i === targetSlot) dot.classList.add('current');
    else dot.classList.add('done');
  }
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

// ============================================================
// LIVE PRINT PREVIEW (SINGLE STRIP FRAME)
// ============================================================
let livePreviewLoaded = false;

function loadLivePreview() {
  livePreviewLoaded = true;
  const paper = document.getElementById('preview-paper-strip');
  const img = document.getElementById('preview-sheet-img');
  const loader = document.getElementById('preview-sheet-loader');
  const loaderText = document.getElementById('preview-loader-text');
  const badge = document.getElementById('preview-badge-status');

  if (loader) loader.style.display = 'flex';
  if (loaderText) loaderText.textContent = 'Menyiapkan Preview Foto...';
  if (paper) {
    paper.className = 'preview-paper single-strip';
  }
  if (badge) {
    badge.textContent = '✨ Siap Dicetak (1 Lembar Strip • 12 cm)';
  }

  const hCm = (appState && appState.session && appState.session.print_strip_height_cm) || 12.0;
  const dimTag = document.getElementById('preview-dimensions-tag');
  if (dimTag) {
    dimTag.innerHTML = `📏 Ukuran Cetak: <strong>${Number(hCm).toFixed(1)} cm</strong> tinggi (Dilengkapi Garis Potong)`;
  }

  if (img) {
    // Tampilkan hanya 1 lembar frame strip foto
    img.src = '/api/final_photo?t=' + Date.now();
  }

  // Preload QR in background so download modal is instant
  loadFinalQR();
}

function onPreviewImgLoaded(type) {
  const loader = document.getElementById('preview-sheet-loader');
  if (loader) loader.style.display = 'none';
}

function openDownloadModal() {
  const modal = document.getElementById('preview-qr-modal');
  if (modal) {
    modal.style.display = 'flex';
    loadModalQR();
  }
}

function closeDownloadModal() {
  const modal = document.getElementById('preview-qr-modal');
  if (modal) modal.style.display = 'none';
}

async function loadModalQR() {
  try {
    const res = await fetch('/api/final_qr');
    const data = await res.json();
    if (data.ok && data.qr_b64) {
      const mImg = document.getElementById('modal-qr-img');
      const mLoad = document.getElementById('modal-qr-loading');
      const mCloud = document.getElementById('modal-qr-cloud-badge');
      if (mImg) mImg.src = 'data:image/png;base64,' + data.qr_b64;
      if (mLoad) mLoad.style.display = 'none';
      if (mCloud) mCloud.style.display = data.is_cloud ? 'block' : 'none';
    }
  } catch(e) {}
}

async function loadFinalQR() {
  qrLoaded = true;
  try {
    const res = await fetch('/api/final_qr');
    const data = await res.json();
    if (data.ok && data.qr_b64) {
      document.getElementById('final-qr-img').src = 'data:image/png;base64,' + data.qr_b64;
      document.getElementById('final-qr-loading').style.display = 'none';
      const cloudBadge = document.getElementById('final-qr-cloud-badge');
      if (cloudBadge) {
        cloudBadge.style.display = data.is_cloud ? 'block' : 'none';
      }
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

// ============================================================
// PHOTO ZOOM & SINGLE RETAKE POPUP
// ============================================================
let currentZoomedSlot = 1;

function openPhotoZoom(slotIdx) {
  currentZoomedSlot = slotIdx;
  const modal = document.getElementById('photo-zoom-modal');
  const img = document.getElementById('photo-zoom-img');
  const title = document.getElementById('photo-zoom-title');
  const btnRetake = document.getElementById('btn-retake-single');

  if (title) title.textContent = `Foto 0${slotIdx} / 03`;
  if (img) img.src = `/api/photo_preview/${slotIdx}?t=${Date.now()}`;
  if (btnRetake) btnRetake.textContent = `↺ Retake Foto 0${slotIdx} Saja`;
  if (modal) modal.classList.add('active');
}

function closePhotoZoom() {
  const modal = document.getElementById('photo-zoom-modal');
  if (modal) modal.classList.remove('active');
}

function onZoomModalBgClick(e) {
  if (e.target.id === 'photo-zoom-modal') {
    closePhotoZoom();
  }
}

async function retakeCurrentZoomedPhoto() {
  const slot = currentZoomedSlot;
  closePhotoZoom();
  const res = await doAction('retake_slot', {slot});
  if (res.ok) {
    showToast(`Mengulang Foto 0${slot}... Silakan bersiap!`, 2500);
  }
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
let lastFistSeenTime = 0;
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

  // Update floating mini camera feedback so user always knows gesture status
  const miniCam = document.getElementById('mini-camera-preview');
  if (miniCam) {
    const isDetected = !!appState.handDetected;
    const isAction = (g === 'fist' || g === 'peace');
    miniCam.classList.toggle('hand-detected', isDetected && !isAction);
    miniCam.classList.toggle('gesture-action', isAction);
  }
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
    lastFistSeenTime = now;
    if (fistState === 'ARMED') {
      fistState = 'PRESSING';
      fistPressStart = now;
      updateCursorProgress(0.08, true);
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
    // Fist released with 180ms grace period to tolerate momentary dropped polling frames
    if (now - lastFistSeenTime > 180) {
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
  } else if (appState.screen === 'preview') {
    doAction('print_photo');
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
      setTimeout(() => cursor.classList.remove('fist-clicked'), 350);
    }
    isGestureDispatching = true;
    try {
      el.classList.add('hovered', 'gesture-clicked');
      setTimeout(() => el.classList.remove('gesture-clicked'), 350);

      if (typeof el.click === 'function') {
        el.click();
      } else {
        el.dispatchEvent(new MouseEvent('click', {
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: x,
          clientY: y
        }));
      }
    } catch(err) {
      console.error('Gesture click error:', err);
    } finally {
      setTimeout(() => {
        isGestureDispatching = false;
      }, 100);
    }
  }
}

function getInteractiveAt(x, y) {
  // 1. Direct hit check via elementFromPoint
  const hit = document.elementFromPoint(x, y);
  if (hit) {
    const target = hit.closest('button, a, .btn, .btn-interactive, .frame-card, .landing-cta-btn, .review-card, [onclick]');
    if (target && !target.disabled) {
      if (target.closest('.screen.active') || target.closest('#photo-zoom-modal.active')) {
        return target;
      }
    }
  }

  // 2. Proximity search: find closest interactive element within 28px padding
  const candidates = document.querySelectorAll('.btn-interactive, .frame-card, .landing-cta-btn, .review-card, button, [onclick]');
  let best = null;
  let bestDist = Infinity;
  const pad = 28;

  for (const el of candidates) {
    if (el.disabled) continue;
    if (!el.closest('.screen.active') && !el.closest('#photo-zoom-modal.active')) continue;
    const rect = el.getBoundingClientRect();
    if (x >= rect.left - pad && x <= rect.right + pad &&
        y >= rect.top - pad && y <= rect.bottom + pad) {
      const cx = (rect.left + rect.right) / 2;
      const cy = (rect.top + rect.bottom) / 2;
      const dist = Math.hypot(x - cx, y - cy);
      if (dist < bestDist) {
        bestDist = dist;
        best = el;
      }
    }
  }
  return best;
}

// Reconnect camera stream if disconnected or when tab/laptop is refocused
function ensureCameraStream() {
  const streamEl = document.getElementById('camera-stream');
  if (streamEl && (appState.screen === 'camera' || !appState.screen || appState.screen === 'landing')) {
    streamEl.src = '/video_feed?t=' + Date.now();
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    ensureCameraStream();
    ensureMiniCameraStream();
    fetchState();
    fetchCursor();
  }
});

window.addEventListener('focus', () => {
  ensureCameraStream();
  ensureMiniCameraStream();
});

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

  // Hover detection synchronized with getInteractiveAt
  if (appState.handDetected && !settingsOpen && !appState.adminMode) {
    const x = appState.cursor.x * window.innerWidth;
    const y = appState.cursor.y * window.innerHeight;
    const activeTarget = getInteractiveAt(x, y);
    document.querySelectorAll('.btn-interactive, .frame-card, .landing-cta-btn, .review-card, button, [onclick]').forEach(el => {
      const isTarget = (el === activeTarget) && !!(el.closest('.screen.active') || el.closest('#photo-zoom-modal.active'));
      el.classList.toggle('hovered', isTarget);
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

  // Lazy-load camera preview inside settings panel
  const prev = document.getElementById('settings-camera-stream');
  if (prev) prev.src = '/video_feed?t=' + Date.now();

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

  // Stop camera preview inside settings panel to save streaming resources
  const prev = document.getElementById('settings-camera-stream');
  if (prev) prev.src = '';

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
    const isTarget = item.getAttribute('data-section') === name || item.textContent.toLowerCase().includes(name);
    item.classList.toggle('active', isTarget);
  });
  if (name === 'gallery') {
    loadSavedPhotos();
  }
}

// ============================================================
// ADMIN SETTINGS GALLERY (SAVED PHOTOS & PRINTS)
// ============================================================
let savedPhotosList = [];
let currentGalleryFilter = 'all';
let currentZoomPhoto = null;

async function loadSavedPhotos() {
  const loading = document.getElementById('gallery-loading');
  const empty = document.getElementById('gallery-empty');
  const grid = document.getElementById('gallery-grid');
  if (loading) loading.style.display = 'flex';
  if (empty) empty.style.display = 'none';
  if (grid) grid.innerHTML = '';

  try {
    const res = await fetch('/api/saved_photos');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    savedPhotosList = data.photos || [];

    const totalCountEl = document.getElementById('gal-total-count');
    const totalSizeEl = document.getElementById('gal-total-size');
    const folderPathEl = document.getElementById('gal-folder-path');
    if (totalCountEl) totalCountEl.textContent = data.total_count || 0;
    if (totalSizeEl) totalSizeEl.textContent = data.total_size || '0 MB';
    if (folderPathEl) {
      folderPathEl.textContent = data.photos_dir || 'photos/';
      folderPathEl.title = `Lokasi: ${data.photos_dir || 'photos/'}`;
    }

    const printCount = savedPhotosList.filter(p => p.category === 'print_sheet').length;
    const finalCount = savedPhotosList.filter(p => p.category === 'final_strip').length;
    const rawCount = savedPhotosList.filter(p => p.category === 'raw_photo').length;

    const cAll = document.getElementById('gal-filter-count-all');
    const cPrint = document.getElementById('gal-filter-count-print_sheet');
    const cFinal = document.getElementById('gal-filter-count-final_strip');
    const cRaw = document.getElementById('gal-filter-count-raw_photo');
    if (cAll) cAll.textContent = savedPhotosList.length;
    if (cPrint) cPrint.textContent = printCount;
    if (cFinal) cFinal.textContent = finalCount;
    if (cRaw) cRaw.textContent = rawCount;

    renderGallery();
  } catch (e) {
    showToast(`Gagal memuat galeri: ${e.message}`, 3000);
  } finally {
    if (loading) loading.style.display = 'none';
  }
}

function filterGallery(filter) {
  currentGalleryFilter = filter;
  document.querySelectorAll('.gal-filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-filter') === filter);
  });
  renderGallery();
}

function renderGallery() {
  const grid = document.getElementById('gallery-grid');
  const empty = document.getElementById('gallery-empty');
  if (!grid) return;

  const filtered = savedPhotosList.filter(item => {
    if (currentGalleryFilter === 'all') return true;
    return item.category === currentGalleryFilter;
  });

  if (filtered.length === 0) {
    grid.innerHTML = '';
    if (empty) empty.style.display = 'block';
    return;
  }

  if (empty) empty.style.display = 'none';

  grid.innerHTML = filtered.map(p => {
    const isSheet = p.category === 'print_sheet';
    const isFinal = p.category === 'final_strip';
    const badgeClass = isSheet ? 'badge-print-sheet' : (isFinal ? 'badge-final-strip' : 'badge-raw-photo');
    const badgeText = isSheet ? '🖨️ A4 Sheet 12cm' : (isFinal ? '🎞️ Strip Final' : '📷 Foto Sesi');

    return `
      <div class="gallery-card">
        <div class="gallery-thumb-wrap" onclick="openGalleryZoom('${encodeURIComponent(p.filename)}')">
          <span class="gallery-card-badge ${badgeClass}">${badgeText}</span>
          <span class="gallery-card-size">${p.size_str}</span>
          <img class="gallery-thumb" src="${p.url}" alt="${p.filename}" loading="lazy">
        </div>
        <div class="gallery-card-info">
          <div class="gallery-card-date">🕒 ${p.date_str}</div>
          <div class="gallery-card-filename" title="${p.filename}">${p.filename}</div>
        </div>
        <div class="gallery-card-actions">
          <button class="gal-act-btn btn-print" onclick="printSavedPhoto('${encodeURIComponent(p.filename)}')" title="Cetak langsung foto ini ke printer">
            <span>🖨️ Cetak</span>
          </button>
          <button class="gal-act-btn" onclick="openGalleryZoom('${encodeURIComponent(p.filename)}')" title="Lihat ukuran penuh">
            <span>🔍 Zoom</span>
          </button>
          <a class="gal-act-btn" href="${p.url}" target="_blank" download="${p.filename}" title="Buka / Download file" style="text-decoration:none; flex:0 0 30px;">
            <span>⬇</span>
          </a>
          <button class="gal-act-btn btn-del" onclick="deleteSavedPhoto('${encodeURIComponent(p.filename)}')" title="Hapus file ini dari laptop">
            <span>🗑</span>
          </button>
        </div>
      </div>
    `;
  }).join('');
}

function openGalleryZoom(encodedName) {
  const filename = decodeURIComponent(encodedName);
  const photo = savedPhotosList.find(p => p.filename === filename);
  if (!photo) return;
  currentZoomPhoto = photo;

  const modal = document.getElementById('gallery-zoom-modal');
  const img = document.getElementById('gal-zoom-img');
  const fnameEl = document.getElementById('gal-zoom-filename');
  const badgeEl = document.getElementById('gal-zoom-badge');
  const infoEl = document.getElementById('gal-zoom-info');
  const dlBtn = document.getElementById('gal-zoom-download-btn');

  if (img) img.src = photo.url;
  if (fnameEl) fnameEl.textContent = photo.filename;
  if (badgeEl) badgeEl.textContent = photo.category_label;
  if (infoEl) infoEl.textContent = `${photo.size_str} • ${photo.date_str}`;
  if (dlBtn) {
    dlBtn.href = photo.url;
    dlBtn.download = photo.filename;
  }

  if (modal) modal.style.display = 'flex';
}

function closeGalleryZoom() {
  const modal = document.getElementById('gallery-zoom-modal');
  if (modal) modal.style.display = 'none';
  const img = document.getElementById('gal-zoom-img');
  if (img) img.src = '';
  currentZoomPhoto = null;
}

async function printSavedPhoto(encodedName) {
  const filename = decodeURIComponent(encodedName);
  showToast(`Mengirim ${filename} ke printer...`, 2500);
  try {
    const res = await fetch('/api/print_saved_photo', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({filename: filename})
    });
    const data = await res.json();
    if (data.ok) {
      showToast(`✅ Berhasil dicetak: ${data.message}`, 4000);
    } else {
      showToast(`❌ Gagal mencetak: ${data.message || data.error}`, 5000);
    }
  } catch (e) {
    showToast(`❌ Error print: ${e.message}`, 4000);
  }
}

function printSavedPhotoFromZoom() {
  if (currentZoomPhoto) {
    printSavedPhoto(encodeURIComponent(currentZoomPhoto.filename));
  }
}

async function deleteSavedPhoto(encodedName) {
  const filename = decodeURIComponent(encodedName);
  if (!confirm(`Hapus file ${filename} dari laptop?`)) return;

  try {
    const res = await fetch('/api/delete_saved_photo', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({filename: filename})
    });
    const data = await res.json();
    if (data.ok) {
      showToast(`🗑️ ${filename} berhasil dihapus`);
      savedPhotosList = savedPhotosList.filter(p => p.filename !== filename);
      renderGallery();
    } else {
      showToast(`Gagal menghapus: ${data.error}`);
    }
  } catch (e) {
    showToast(`Error: ${e.message}`);
  }
}

async function openPhotosFolderInExplorer() {
  try {
    const res = await fetch('/api/open_photos_folder', {method: 'POST'});
    const data = await res.json();
    if (data.ok) {
      showToast('📂 Folder photos dibuka di Windows Explorer');
    } else {
      showToast(`Gagal membuka folder: ${data.error}`);
    }
  } catch (e) {
    showToast(`Error: ${e.message}`);
  }
}

async function openPrinterPreferences() {
  const pSel = document.getElementById('cfg-printer_name');
  const printerName = pSel ? pSel.value : '';
  showToast('Membuka dialog preferensi printer Windows...');
  try {
    const res = await fetch('/api/open_printer_preferences', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({printer_name: printerName})
    });
    const data = await res.json();
    if (data.ok) {
      showToast('⚙️ Jendela driver printer dibuka di Windows');
    } else {
      showToast(`Gagal: ${data.error}`);
    }
  } catch (e) {
    showToast(`Error: ${e.message}`);
  }
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
            qr.add_data(QRIS_RAW_DATA)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            img = img.resize((600, 600), Image.NEAREST)
            img.save(str(qris_path))
            print(f"[ASSET] Generated QRIS code at {qris_path}")
        except Exception as e:
            print(f"[ASSET] Could not generate QRIS: {e}")

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

    import socket
    local_ips = []
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127."):
                local_ips.append(ip)
    except Exception:
        pass

    print(f"[SERVER] Kiosk Local : http://127.0.0.1:5000")
    if local_ips:
        for ip in local_ips:
            print(f"[SERVER] Kasir HP Link: http://{ip}:5000/cashier")
    else:
        print(f"[SERVER] Kasir HP Link: http://<IP_LAPTOP>:5000/cashier")
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
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
