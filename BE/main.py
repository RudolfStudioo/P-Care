import cv2
import sys
import time
import numpy as np
from ultralytics import YOLO
from typing import Dict, Optional
import threading

from database import log_fall_event

MODEL_FILES = {
    "v8":  "yolov8n-pose.pt",
    "v26": "yolo26n-pose.pt",   # diunduh otomatis kalau belum ada
}
DEFAULT_MODEL = "v26"           # model bawaan
FALLBACK_MODEL = "v8"           # dipakai kalau v26 gagal dimuat saat startup
YOLO_MODEL = MODEL_FILES[DEFAULT_MODEL]

STATE_NORMAL = "NORMAL"
STATE_POTENTIAL = "POTENTIAL_FALL"
STATE_FAINTED = "FAINTED"
STATE_ALERT = "ALERT_SENT"

FALL_TIME_THRESHOLD = 7.0

# ---- Kamera: selalu menyala, otomatis nyambung ulang ----
CAMERA_INDEXES = [0, 1, 2]      # index yang dicoba berurutan
RECONNECT_DELAY = 2.0           # detik jeda sebelum coba buka kamera lagi
MAX_READ_FAILS = 10             # gagal baca berturut-turut sebelum kamera dibuka ulang

# ---- Streaming ke browser (terpisah dari resolusi deteksi) ----
# width   : lebar maksimal gambar yang dikirim ke browser (tidak pernah diperbesar)
# quality : kualitas JPEG (1-100)
STREAM_PROFILES = {
    "low":      {"width": 480,  "quality": 50},
    "medium":   {"width": 800,  "quality": 70},
    "lossless": {"width": 1280, "quality": 100},
}
DEFAULT_STREAM = "medium"

# Profil resolusi: ukuran tangkapan kamera, ukuran input YOLO, kualitas JPEG
RESOLUTION_PROFILES = {
    "lossless": {"width": 1280, "height": 720, "imgsz": 960, "jpeg_quality": 100},
    "high":     {"width": 1280, "height": 720, "imgsz": 640, "jpeg_quality": 85},
    "medium":   {"width": 640,  "height": 480, "imgsz": 480, "jpeg_quality": 70},
    "low":      {"width": 320,  "height": 240, "imgsz": 320, "jpeg_quality": 50},
}
DEFAULT_RESOLUTION = "high"     # disamakan dengan default di database


class FallDetectionEngine:
    def __init__(self):
        self.model_choice = DEFAULT_MODEL
        self.model_path = MODEL_FILES[DEFAULT_MODEL]
        try:
            self.model = YOLO(self.model_path)
        except Exception as e:
            print(f"Gagal memuat {self.model_path}: {e}. Pakai model cadangan {FALLBACK_MODEL}.")
            self.model_choice = FALLBACK_MODEL
            self.model_path = MODEL_FILES[FALLBACK_MODEL]
            self.model = YOLO(self.model_path)

        self.tracking_states: Dict = {}
        self.current_frame: Optional[np.ndarray] = None
        self.is_running = False      # thread engine hidup (kamera boleh sedang menyambung ulang)
        self.camera_ok = False       # kamera benar-benar terbuka & mengirim frame
        self.fall_time_threshold = FALL_TIME_THRESHOLD
        self.alert_count = 0
        self.current_fps = 0
        self.last_inference_ms: float = 0.0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cap: Optional[cv2.VideoCapture] = None

        # JPEG siap kirim (dienkode sekali per frame di thread engine)
        self.latest_jpeg: Optional[bytes] = None
        self.frame_id = 0

        # Mode stream (kualitas gambar ke browser)
        self.stream_mode = DEFAULT_STREAM
        self.stream_width = STREAM_PROFILES[DEFAULT_STREAM]["width"]
        self.stream_quality = STREAM_PROFILES[DEFAULT_STREAM]["quality"]

        self.resolution_mode = DEFAULT_RESOLUTION
        self.imgsz = 640
        self._pending_model: Optional[str] = None
        self.model_loading = False   # True selama model baru sedang dimuat
        self.model_error = ""        # pesan error terakhir saat ganti model
        self.jpeg_quality = 85       # dipertahankan untuk kompatibilitas
        self._cam_size = (1280, 720)
        self._profile_dirty = True
        self.set_resolution(DEFAULT_RESOLUTION)

    # ---------- Resolusi ----------
    def set_resolution(self, mode: str):
        p = RESOLUTION_PROFILES.get(mode)
        if not p:
            raise ValueError(f"Mode resolusi tidak dikenal: {mode}")
        self.resolution_mode = mode
        self.imgsz = p["imgsz"]
        self.jpeg_quality = p["jpeg_quality"]
        self._cam_size = (p["width"], p["height"])
        self._profile_dirty = True   # diterapkan di loop kamera tanpa restart

    # ---------- Mode stream ----------
    def set_stream_mode(self, mode: str):
        p = STREAM_PROFILES.get(mode)
        if not p:
            raise ValueError(f"Mode stream tidak dikenal: {mode}")
        self.stream_mode = mode
        self.stream_width = p["width"]
        self.stream_quality = p["quality"]

    # ---------- Model ----------
    def set_model(self, choice: str):
        if choice not in MODEL_FILES:
            raise ValueError(f"Model tidak dikenal: {choice}")
        if choice == self.model_choice and self._pending_model is None:
            return   # sudah memakai model ini
        self._pending_model = choice   # dimuat di loop kamera, tanpa restart

    def _apply_model(self):
        choice = self._pending_model
        self._pending_model = None
        self.model_loading = True
        self.model_error = ""
        try:
            self.model = YOLO(MODEL_FILES[choice])
            self.model_choice = choice
            self.model_path = MODEL_FILES[choice]
            self.tracking_states.clear()   # ID tracker lama tidak valid lagi
        except Exception as e:
            print(f"Gagal memuat model {choice}: {e}")
            # model lama tetap dipakai
            self.model_error = f"Gagal memuat {MODEL_FILES[choice]}"
        finally:
            self.model_loading = False

    def _apply_profile(self):
        if self._cap is not None and self._cap.isOpened():
            # MJPG: kamera mengirim JPEG terkompresi, jauh lebih hemat bandwidth USB di Pi
            self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._cam_size[0])
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._cam_size[1])
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # buang frame lama, ambil yang terbaru
            self._profile_dirty = False

    # ---------- Buka / tutup kamera ----------
    def _open_camera(self) -> bool:
        backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
        for idx in CAMERA_INDEXES:
            cap = cv2.VideoCapture(idx, backend)
            if not cap.isOpened():
                cap.release()
                continue
            self._cap = cap
            self._apply_profile()
            ok, _ = cap.read()          # pastikan benar-benar bisa baca frame
            if ok:
                print(f"✓ Kamera terbuka di index {idx}")
                return True
            cap.release()
            self._cap = None
        return False

    def _close_camera(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    # ---------- Encode untuk streaming ----------
    def _encode_stream(self, frame: np.ndarray):
        h, w = frame.shape[:2]
        if w > self.stream_width:
            frame = cv2.resize(frame, (self.stream_width, int(h * self.stream_width / w)),
                               interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.stream_quality])
        if ok:
            self.latest_jpeg = buf.tobytes()
            self.frame_id += 1

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        _t0 = time.time()
        results = self.model.track(frame, persist=True, classes=0, verbose=False, imgsz=self.imgsz)
        self.last_inference_ms = (time.time() - _t0) * 1000
        current_ids = []

        if results[0].boxes.id is not None:
            boxes = results[0].boxes.xyxy.cpu().numpy()
            track_ids = results[0].boxes.id.int().cpu().tolist()
            keypoints = results[0].keypoints.data.cpu().numpy()

            for box, track_id, kpts in zip(boxes, track_ids, keypoints):
                current_ids.append(track_id)

                x1, y1, x2, y2 = map(int, box)
                w = x2 - x1
                h = y2 - y1

                if track_id not in self.tracking_states:
                    self.tracking_states[track_id] = {'state': STATE_NORMAL, 'timestamp': None, 'max_h': h}

                state_info = self.tracking_states[track_id]

                if state_info['state'] == STATE_NORMAL and h > w:
                    state_info['max_h'] = max(state_info['max_h'] * 0.95 + h * 0.05, h)

                baseline_h = state_info['max_h']

                s_l, s_r = kpts[5], kpts[6]
                h_l, h_r = kpts[11], kpts[12]
                valid_keypoints = all(pt[2] > 0.4 for pt in [s_l, s_r, h_l, h_r])

                is_horizontal = w > (1.3 * h)
                is_crumpled = h < (0.5 * baseline_h)

                keypoint_collapse = False
                if valid_keypoints:
                    shoulder_y = (s_l[1] + s_r[1]) / 2.0
                    hip_y = (h_l[1] + h_r[1]) / 2.0
                    if abs(shoulder_y - hip_y) < (0.2 * baseline_h):
                        keypoint_collapse = True

                is_falling_condition = is_horizontal or is_crumpled or keypoint_collapse
                current_time = time.time()

                if is_falling_condition:
                    if state_info['state'] == STATE_NORMAL:
                        state_info['state'] = STATE_POTENTIAL
                        state_info['timestamp'] = current_time
                        log_fall_event(track_id, STATE_POTENTIAL,
                                       f"Posisi mencurigakan, batas waktu {self.fall_time_threshold:g} detik")

                    elif state_info['state'] == STATE_POTENTIAL:
                        if current_time - state_info['timestamp'] >= self.fall_time_threshold:
                            state_info['state'] = STATE_FAINTED

                    elif state_info['state'] == STATE_FAINTED:

                        state_info['state'] = STATE_ALERT
                        self.alert_count += 1
                        log_fall_event(track_id, STATE_ALERT, "Alarm dikirim ke perangkat darurat")

                else:
                    if state_info['state'] == STATE_ALERT:
                        log_fall_event(track_id, "RECOVERED", "Orang kembali ke posisi normal")
                    state_info['state'] = STATE_NORMAL
                    state_info['timestamp'] = None

                color = (0, 255, 0)
                label = f"ID: {track_id} | {state_info['state']} | H:{h}/{int(baseline_h)}"

                if state_info['state'] == STATE_POTENTIAL:
                    color = (0, 165, 255)
                    elapsed = current_time - state_info['timestamp']
                    label += f" | {max(0, int(self.fall_time_threshold - elapsed))}s"
                elif state_info['state'] in [STATE_FAINTED, STATE_ALERT]:
                    color = (0, 0, 255)

                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame, label, (x1, max(y1 - 10, 0)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        for tid in [t for t in self.tracking_states if t not in current_ids]:
            del self.tracking_states[tid]

        return frame

    def run(self):
        """Loop pengawas: kamera dicabut / macet / error -> otomatis buka ulang,
        sampai stop() dipanggil."""
        self.is_running = True
        prev_time = time.time()
        frame_count = 0

        while not self._stop_event.is_set():
            if not self._open_camera():
                self.camera_ok = False
                print(f"Kamera belum bisa dibuka, coba lagi {RECONNECT_DELAY:g} detik...")
                self._stop_event.wait(RECONNECT_DELAY)
                continue

            self.camera_ok = True
            self.tracking_states.clear()
            fails = 0

            while not self._stop_event.is_set():
                if self._profile_dirty:
                    self._apply_profile()

                if self._pending_model:
                    self._apply_model()

                ret, frame = self._cap.read()
                if not ret:
                    fails += 1
                    if fails >= MAX_READ_FAILS:
                        print("Kamera berhenti mengirim frame, membuka ulang...")
                        break
                    time.sleep(0.05)
                    continue
                fails = 0

                try:
                    processed = self.process_frame(frame)
                except Exception as e:
                    # error inferensi tidak boleh mematikan kamera
                    print(f"Error proses frame: {e}")
                    processed = frame

                self.current_frame = processed
                self._encode_stream(processed)

                frame_count += 1
                if frame_count % 30 == 0:
                    now = time.time()
                    self.current_fps = frame_count / (now - prev_time)
                    prev_time = now
                    frame_count = 0

            self._close_camera()
            self.camera_ok = False
            self.latest_jpeg = None
            self.current_frame = None
            if not self._stop_event.is_set():
                self._stop_event.wait(RECONNECT_DELAY)

        self._close_camera()
        self.camera_ok = False
        self.latest_jpeg = None
        self.current_frame = None
        self.current_fps = 0
        self.is_running = False

    def start(self):
        # kalau thread lama masih berhenti-pelan, tunggu dulu supaya tidak dobel
        if self._thread is not None and self._thread.is_alive():
            if not self._stop_event.is_set():
                return                      # sudah jalan
            self._thread.join(timeout=5)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        self.is_running = True

    def stop(self):
        self._stop_event.set()
        self.is_running = False

    def get_status(self) -> dict:
        return {
            "running": self.is_running,
            "camera_ok": self.camera_ok,
            "active_tracks": len(self.tracking_states),
            "fps": self.current_fps,
            "alerts": self.alert_count,
            "model": self.model_choice,
            "model_loading": self.model_loading,
            "model_error": self.model_error,
            "stream_mode": self.stream_mode,
        }


if __name__ == "__main__":
    engine = FallDetectionEngine()
    engine.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        engine.stop()