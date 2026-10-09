from fastapi import (FastAPI, WebSocket, WebSocketDisconnect, HTTPException,
                     Request, Response, Depends, APIRouter)
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from typing import Dict, Optional, Literal
import asyncio
import calendar
import os
import socket
import threading
import time

import psutil
import requests

from main import FallDetectionEngine
from database import (
    get_all_settings, update_setting, get_setting,
    verify_user, change_password,
    register_local_device, get_all_local_devices, get_local_device_by_id, delete_local_device,
    create_session, get_session_user, delete_session,
    log_fall_event, get_logs, count_logs, clear_logs,
)

app = FastAPI(title="P Care API", version="3.2.0")

# CATATAN: CORS dihapus. FE disajikan dari origin yang sama dengan API, dan
# "allow_origins=*" + cookie login justru membuka celah.

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FE_DIR = os.path.join(BASE_DIR, "..", "FE")

cv_engine = FallDetectionEngine()
active_websockets: Dict[int, WebSocket] = {}

_process = psutil.Process(os.getpid())
_process.cpu_percent(interval=None)
_server_start_time = time.time()

DEVICE_OFFLINE_THRESHOLD_SEC = 30
DEVICE_VIBRATE_PORT = 8765
MAX_LOCAL_DEVICES = 5

COOKIE_NAME = "pcare_session"
SESSION_TTL = 60 * 60 * 12          # 12 jam
MAX_FAILED_LOGINS = 5
LOCK_SECONDS = 60
_failed: Dict[str, list] = {}       # ip -> [jumlah_gagal, terkunci_sampai]
_failed_lock = threading.Lock()

VIDEO_MAX_FPS = 15                  # batas kirim frame ke browser

# ==================== SKEMA ====================

class LoginIn(BaseModel):
    username: str
    password: str

class ChangePasswordIn(BaseModel):
    username: Optional[str] = None   # diabaikan; dipakai user yang sedang login
    old_password: str
    new_password: str = Field(..., min_length=4)

class SettingsUpdateIn(BaseModel):
    cameraStatus: Optional[str] = None
    notifMode: Optional[Literal["getar", "dering", "keduanya"]] = None
    displayFilter: Optional[Literal["normal", "invert", "grayscale"]] = None
    fall_threshold: Optional[float] = Field(None, ge=1, le=60)          # detik
    alarm_duration: Optional[int] = Field(None, ge=20, le=900)
    ringtone_type: Optional[str] = None
    resolution_mode: Optional[Literal["lossless", "high", "medium", "low"]] = None
    stream_mode: Optional[Literal["low", "medium", "lossless"]] = None
    model_choice: Optional[Literal["v8", "v26"]] = None

class ClearLogsIn(BaseModel):
    confirm: str
    older_than_days: Optional[int] = Field(None, ge=1, le=3650)   # None = semua log

class DeviceRegisterIn(BaseModel):
    name: str
    ip: Optional[str] = None         # diabaikan; server memakai IP asli koneksi

# ==================== AUTH HELPER ====================

def current_user(conn) -> Optional[str]:
    """conn = Request atau WebSocket (keduanya punya .cookies)."""
    return get_session_user(conn.cookies.get(COOKIE_NAME))

async def require_user(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Belum login")
    return user

async def ws_guard(ws: WebSocket) -> bool:
    if not current_user(ws):
        await ws.close(code=1008)
        return False
    return True

# ==================== SAMPLER PERFORMA ====================
# Satu thread mengukur sistem tiap detik, lalu semua klien WebSocket membaca
# hasil yang sama. Kalau tiap klien memanggil psutil.cpu_percent sendiri,
# hasilnya saling mengganggu (selisih waktu antar-panggilan jadi pendek).
PERF_INTERVAL = 1.0
_perf: Dict = {}
_perf_lock = threading.Lock()
_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024

def _pick_temp():
    """Suhu CPU dalam °C, atau None kalau sensor tidak tersedia."""
    if not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    if not temps:
        return None
    for name in ("cpu_thermal", "cpu-thermal", "coretemp", "k10temp", "soc_thermal"):
        if temps.get(name) and temps[name][0].current is not None:
            return float(temps[name][0].current)
    for entries in temps.values():
        if entries and entries[0].current is not None:
            return float(entries[0].current)
    return None

def _perf_sampler():
    psutil.cpu_percent(interval=None, percpu=True)   # panggilan pertama selalu 0
    _process.cpu_percent(interval=None)
    prev_t = time.time()
    prev_disk = psutil.disk_io_counters()
    prev_net = psutil.net_io_counters()

    while True:
        time.sleep(PERF_INTERVAL)
        try:
            now = time.time()
            dt = max(now - prev_t, 1e-6)
            prev_t = now

            cores = psutil.cpu_percent(interval=None, percpu=True)
            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            try:
                du = psutil.disk_usage(BASE_DIR)
            except Exception:
                du = None

            disk = psutil.disk_io_counters()
            net = psutil.net_io_counters()
            disk_r = disk_w = net_up = net_down = 0.0
            if disk and prev_disk:
                disk_r = max(0.0, (disk.read_bytes - prev_disk.read_bytes) / 1024 / dt)
                disk_w = max(0.0, (disk.write_bytes - prev_disk.write_bytes) / 1024 / dt)
            if net and prev_net:
                net_up = max(0.0, (net.bytes_sent - prev_net.bytes_sent) / 1024 / dt)
                net_down = max(0.0, (net.bytes_recv - prev_net.bytes_recv) / 1024 / dt)
            prev_disk, prev_net = disk, net

            try:
                load = list(os.getloadavg())
            except (AttributeError, OSError):
                load = None

            snap = {
                "cpu": _process.cpu_percent(interval=None),          # proses ini (bisa >100% di multi-core)
                "cpu_sys": sum(cores) / len(cores) if cores else 0.0,
                "cpu_cores": cores,
                "cpu_count": len(cores),
                "load": load,
                "ram_mb": _process.memory_info().rss / _MB,          # proses ini
                "mem_total_mb": vm.total / _MB,
                "mem_used_mb": (vm.total - vm.available) / _MB,
                "mem_percent": vm.percent,
                "swap_total_mb": sw.total / _MB,
                "swap_used_mb": sw.used / _MB,
                "swap_percent": sw.percent,
                "disk_total_gb": du.total / _GB if du else None,
                "disk_used_gb": du.used / _GB if du else None,
                "disk_percent": du.percent if du else None,
                "disk_read_kbs": disk_r,
                "disk_write_kbs": disk_w,
                "net_up_kbs": net_up,
                "net_down_kbs": net_down,
                "temp_c": _pick_temp(),
            }
            with _perf_lock:
                _perf.clear()
                _perf.update(snap)
        except Exception as e:
            print(f"Sampler performa error: {e}")

# ==================== LIFECYCLE ====================

@app.on_event("startup")
async def startup_event():
    cv_engine.fall_time_threshold = float(get_setting("fall_threshold", 7))
    try:
        cv_engine.set_resolution(get_setting("resolution_mode", "high"))
    except ValueError:
        cv_engine.set_resolution("high")
    try:
        cv_engine.set_stream_mode(get_setting("stream_mode", "medium"))
    except ValueError:
        cv_engine.set_stream_mode("medium")

    # Pakai model terakhir yang dipilih user (dimuat di loop kamera)
    try:
        cv_engine.set_model(get_setting("model_choice", "v26"))
    except ValueError:
        pass

    # Kamera selalu menyala sejak server hidup. Kalau kamera belum siap,
    # engine terus mencoba sendiri sampai berhasil.
    cv_engine.start()
    threading.Thread(target=_perf_sampler, daemon=True).start()
    update_setting("cameraStatus", "on")
    log_fall_event(None, "CAMERA_ON", f"Otomatis saat server mulai, resolusi: {cv_engine.resolution_mode}")

    print(f"✓ P Care siap. Threshold={cv_engine.fall_time_threshold:g}s, "
          f"resolusi={cv_engine.resolution_mode}, stream={cv_engine.stream_mode}")

@app.on_event("shutdown")
async def shutdown_event():
    if cv_engine.is_running:
        cv_engine.stop()

# ==================== HALAMAN (WAJIB LOGIN) ====================
# Folder FE TIDAK di-mount sebagai static, supaya file html tidak bisa
# dibuka langsung tanpa login. Hanya global.css/global.js yang publik.

def protected_page(request: Request, filename: str):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(os.path.join(FE_DIR, filename))

@app.get("/")
async def page_camera(request: Request):
    return protected_page(request, "camera.html")

@app.get("/performance")
async def page_performance(request: Request):
    return protected_page(request, "performance.html")

@app.get("/log")
async def page_log(request: Request):
    return protected_page(request, "log.html")

@app.get("/settings")
async def page_settings(request: Request):
    return protected_page(request, "settings.html")

@app.get("/login")
async def page_login(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(os.path.join(FE_DIR, "login.html"))

@app.get("/global.css")
async def asset_css():
    return FileResponse(os.path.join(FE_DIR, "global.css"), media_type="text/css")

@app.get("/global.js")
async def asset_js():
    return FileResponse(os.path.join(FE_DIR, "global.js"), media_type="text/javascript")

# ==================== AUTH (publik) ====================

@app.post("/api/auth/login")
async def login(payload: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _failed_lock:
        rec = _failed.get(ip)
        if rec and rec[1] > now:
            raise HTTPException(status_code=429,
                                detail=f"Terlalu banyak percobaan. Coba lagi {int(rec[1] - now)} detik lagi.")

    if verify_user(payload.username, payload.password):
        with _failed_lock:
            _failed.pop(ip, None)
        token = create_session(payload.username, SESSION_TTL)
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL,
                            httponly=True, samesite="lax")
        log_fall_event(None, "LOGIN", f"User '{payload.username}' dari {ip}")
        return {"message": "Login berhasil"}

    with _failed_lock:
        rec = _failed.setdefault(ip, [0, 0])
        rec[0] += 1
        if rec[0] >= MAX_FAILED_LOGINS:
            rec[0], rec[1] = 0, now + LOCK_SECONDS
    log_fall_event(None, "LOGIN_FAILED", f"User '{payload.username}' dari {ip}")
    raise HTTPException(status_code=401, detail="Username atau password salah")

@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    delete_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME)
    return {"message": "Logout berhasil"}

# ==================== API (wajib login) ====================

api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

@api.put("/auth/password")
async def update_password(payload: ChangePasswordIn, user: str = Depends(require_user)):
    if not change_password(user, payload.old_password, payload.new_password):
        raise HTTPException(status_code=401, detail="Password lama salah")
    return {"message": "Password berhasil diperbarui"}

@api.get("/status")
async def get_status():
    return {
        "status": "running" if cv_engine.is_running else "stopped",
        "camera_ok": cv_engine.camera_ok,
        "active_tracks": len(cv_engine.tracking_states),
        "fps": cv_engine.current_fps,
        "alerts": cv_engine.alert_count,
        "model": cv_engine.model_choice,
        "model_loading": cv_engine.model_loading,
        "model_error": cv_engine.model_error,
        "stream_mode": cv_engine.stream_mode,
    }

@api.post("/start")
async def start_detection():
    if cv_engine.is_running:
        return {"message": "Already running", "status": "running",
                "camera_ok": cv_engine.camera_ok}
    cv_engine.start()
    # tunggu sebentar supaya UI tahu kamera sudah siap; kalau belum,
    # engine tetap mencoba sendiri di latar belakang
    for _ in range(50):
        if cv_engine.camera_ok:
            break
        await asyncio.sleep(0.1)
    update_setting("cameraStatus", "on")
    log_fall_event(None, "CAMERA_ON", f"Resolusi: {cv_engine.resolution_mode}")
    return {"message": "Detection started",
            "status": "running" if cv_engine.camera_ok else "connecting",
            "camera_ok": cv_engine.camera_ok}

@api.post("/stop")
async def stop_detection():
    if cv_engine.is_running:
        cv_engine.stop()
        update_setting("cameraStatus", "off")
        log_fall_event(None, "CAMERA_OFF", "")
        return {"message": "Detection stopped", "status": "stopped"}
    return {"message": "Already stopped", "status": "stopped"}

@api.get("/settings")
async def get_settings():
    return get_all_settings()

@api.put("/settings")
async def update_settings(payload: SettingsUpdateIn):
    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        update_setting(key, value)
        if key == "fall_threshold":
            cv_engine.fall_time_threshold = float(value)
        elif key == "resolution_mode":
            cv_engine.set_resolution(value)
        elif key == "stream_mode":
            cv_engine.set_stream_mode(value)
        elif key == "model_choice":
            cv_engine.set_model(value)
        elif key == "cameraStatus":
            if value == "on" and not cv_engine.is_running:
                cv_engine.start()
            elif value == "off" and cv_engine.is_running:
                cv_engine.stop()
    return {"message": "Settings updated", "settings": get_all_settings()}

@api.delete("/tracking/{track_id}")
async def reset_tracking(track_id: int):
    if track_id in cv_engine.tracking_states:
        del cv_engine.tracking_states[track_id]
        return {"message": f"Track {track_id} reset"}
    raise HTTPException(status_code=404, detail="Track ID not found")

@api.get("/server-ip")
def get_server_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return {"ip": ip}

# ---------- Log ----------
LOG_MAP = {
    "POTENTIAL_FALL": ("warn", "Potensi jatuh terdeteksi"),
    "ALERT_SENT":     ("danger", "Jatuh terdeteksi — alarm dikirim"),
    "RECOVERED":      ("info", "Orang kembali normal"),
    "CAMERA_ON":      ("info", "Kamera dinyalakan"),
    "CAMERA_OFF":     ("info", "Kamera dimatikan"),
    "LOGIN":          ("info", "Login berhasil"),
    "LOGIN_FAILED":   ("warn", "Percobaan login gagal"),
    "LOG_RESET":      ("warn", "Log dibersihkan"),
}

@api.get("/logs")
async def list_logs(limit: int = 200):
    limit = max(1, min(limit, 1000))
    out = []
    for r in get_logs(limit):
        level, msg = LOG_MAP.get(r["status"], ("info", r["status"]))
        detail = r["detail"]
        if r["track_id"] is not None:
            detail = (f"Track ID {r['track_id']} · " + detail).strip(" ·")
        out.append({"id": r["id"], "time": r["timestamp"], "level": level,
                    "message": msg, "detail": detail})
    return {"logs": out}

# ---------- Reset log ----------
# Kalimat konfirmasi juga diperiksa di server, jadi endpoint ini tidak bisa
# terpanggil tanpa sengaja (mis. dari script atau salah klik di API).
LOG_RESET_PHRASE_ALL = "HAPUS SEMUA LOG"
LOG_RESET_PHRASE_OLD = "HAPUS LOG LAMA"

@api.get("/logs/count")
async def logs_count(older_than_days: Optional[int] = None):
    if older_than_days is not None:
        older_than_days = max(1, min(older_than_days, 3650))
    return {"count": count_logs(older_than_days)}

@api.post("/logs/clear")
async def clear_logs_endpoint(payload: ClearLogsIn, user: str = Depends(require_user)):
    expected = LOG_RESET_PHRASE_ALL if payload.older_than_days is None else LOG_RESET_PHRASE_OLD
    if payload.confirm != expected:
        raise HTTPException(status_code=400, detail="Kalimat konfirmasi tidak cocok.")
    deleted = clear_logs(payload.older_than_days)
    scope = ("semua log" if payload.older_than_days is None
             else f"log lebih lama dari {payload.older_than_days} hari")
    # jejak audit: siapa yang membersihkan log dan berapa banyak
    log_fall_event(None, "LOG_RESET", f"{deleted} entri dihapus ({scope}) oleh '{user}'")
    return {"deleted": deleted}

# ---------- Perangkat lokal ----------
def _device_is_online(last_seen_str: str) -> bool:
    # CURRENT_TIMESTAMP di SQLite = UTC, jadi dibandingkan dengan waktu UTC juga
    try:
        ts = calendar.timegm(time.strptime(last_seen_str, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return False
    return (time.time() - ts) < DEVICE_OFFLINE_THRESHOLD_SEC

@api.get("/devices")
async def list_devices():
    devices = [{
        "id": d["id"], "name": d["name"], "ip": d["ip"],
        "connected_at": d["last_seen"], "online": _device_is_online(d["last_seen"]),
    } for d in get_all_local_devices()]
    return {"devices": devices, "total": len(devices), "max_allowed": MAX_LOCAL_DEVICES}

@api.delete("/devices/{device_id}")
async def remove_device(device_id: int):
    if delete_local_device(device_id):
        return {"message": f"Perangkat {device_id} dihapus"}
    raise HTTPException(status_code=404, detail="Perangkat tidak ditemukan")

@api.post("/devices/{device_id}/test")
def test_device_vibration(device_id: int):
    device = get_local_device_by_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Perangkat tidak ditemukan")
    url = f"http://{device['ip']}:{DEVICE_VIBRATE_PORT}/vibrate"
    try:
        r = requests.post(url, json={"pattern": "test", "duration_ms": 400}, timeout=3)
    except requests.exceptions.RequestException:
        raise HTTPException(status_code=502, detail=(
            f"Tidak bisa menghubungi HP '{device['name']}' di {device['ip']}. "
            "Pastikan HP menyala, satu WiFi, dan app dibuka."))
    if r.status_code != 200:
        raise HTTPException(status_code=502,
                            detail=f"HP '{device['name']}' merespons error ({r.status_code}).")
    return {"message": f"Getar test terkirim ke '{device['name']}'"}

app.include_router(api)

# ==================== PUBLIK UNTUK APP ANDROID ====================
# App Android tidak punya cookie login, jadi dua endpoint ini tetap terbuka
# (hanya untuk jaringan lokal/LAN).
# App sebaiknya memanggil register tiap ~10 detik sebagai heartbeat,
# karena status ONLINE hanya bertahan DEVICE_OFFLINE_THRESHOLD_SEC detik.

@app.post("/api/devices/register")
async def register_device(payload: DeviceRegisterIn, request: Request):
    ip = request.client.host if request.client else payload.ip
    if not ip:
        raise HTTPException(status_code=400, detail="IP perangkat tidak diketahui.")
    existing = get_all_local_devices()
    if not any(d["ip"] == ip for d in existing) and len(existing) >= MAX_LOCAL_DEVICES:
        raise HTTPException(status_code=400,
                            detail=f"Batas maksimal {MAX_LOCAL_DEVICES} perangkat telah tercapai.")
    register_local_device(payload.name, ip)
    return {"message": "Perangkat terdaftar", "name": payload.name, "ip": ip}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "cv_engine": "running" if cv_engine.is_running else "stopped",
            "camera_ok": cv_engine.camera_ok, "timestamp": time.time()}

# ==================== WEBSOCKET ====================

@app.websocket("/ws/video")
async def video_stream(websocket: WebSocket):
    if not await ws_guard(websocket):
        return
    await websocket.accept()
    client_id = id(websocket)
    active_websockets[client_id] = websocket
    last_id = -1
    try:
        while True:
            # Kirim JPEG biner yang sudah di-encode engine (sekali per frame),
            # hanya kalau ada frame baru. Sesuai yang dibaca camera.html.
            jpeg = cv_engine.latest_jpeg
            fid = cv_engine.frame_id
            if jpeg is not None and fid != last_id:
                last_id = fid
                await websocket.send_bytes(jpeg)
            await asyncio.sleep(1 / VIDEO_MAX_FPS)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        active_websockets.pop(client_id, None)

@app.websocket("/ws/events")          # terbuka: dipakai app Android
async def event_stream(websocket: WebSocket):
    await websocket.accept()

    async def send_updates():
        last_alert_count = cv_engine.alert_count
        try:
            while True:
                if cv_engine.alert_count > last_alert_count:
                    duration = max(20, min(900, int(get_setting("alarm_duration", 30))))
                    await websocket.send_json({
                        "type": "alert",
                        "message": "Fall detected!",
                        "mode": get_setting("notifMode", "keduanya"),
                        "duration": duration,
                        "ringtone": get_setting("ringtone_type", "alarm"),
                        "timestamp": time.time(),
                        "tracking_data": cv_engine.tracking_states,
                    })
                    last_alert_count = cv_engine.alert_count
                await websocket.send_json({"type": "heartbeat", "timestamp": time.time(),
                                           "active_tracks": len(cv_engine.tracking_states)})
                await asyncio.sleep(1)
        except Exception:
            pass

    async def receive_messages():
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            pass

    tasks = [asyncio.create_task(send_updates()), asyncio.create_task(receive_messages())]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()

@app.websocket("/ws/performance")
async def performance_stream(websocket: WebSocket):
    if not await ws_guard(websocket):
        return
    await websocket.accept()
    client_id = id(websocket)
    active_websockets[client_id] = websocket
    try:
        while True:
            with _perf_lock:
                snap = dict(_perf)
            if snap:
                snap["latency_ms"] = float(getattr(cv_engine, "last_inference_ms", 0.0) or 0.0)
                snap["fps"] = float(cv_engine.current_fps or 0.0)
                snap["uptime_sec"] = time.time() - _server_start_time
                await websocket.send_json(snap)
            await asyncio.sleep(PERF_INTERVAL)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        active_websockets.pop(client_id, None)

if __name__ == "__main__":
    import uvicorn
    # reload=False: reload=True memuat model YOLO dua kali & restart saat file berubah (boros RAM di Pi 3)
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False, log_level="info")