import urllib.request
import json
import time

for attempt in range(3):
    try:
        r = urllib.request.urlopen('http://127.0.0.1:5000/api/state', timeout=5)
        d = json.loads(r.read())
        g = d['gesture']
        cam = d['camera']
        print('=== CAMERA ===')
        print(f'  ok={cam["ok"]} status={cam["status"]} fps={cam["actual_fps"]}')
        print('=== GESTURE ===')
        print(f'  hand_detected={g["hand_detected"]}')
        print(f'  hand_valid_for_primary={g["hand_valid_for_primary"]}')
        print(f'  primary_user_locked={g["primary_user_locked"]}')
        print(f'  face_count={g["face_count"]}')
        print(f'  gesture={g["gesture"]}')
        print(f'  cursor_x={g["cursor_x"]:.4f}  cursor_y={g["cursor_y"]:.4f}')
        print(f'  inference_fps={g["inference_fps"]}')
        print(f'  peace_progress={g["peace_progress"]}')
        print(f'  last_gesture_error={g["last_gesture_error"]}')
        print(f'  admin_mode={g["admin_mode"]}')
        break
    except Exception as ex:
        print(f'Attempt {attempt+1} failed: {ex}')
        time.sleep(2)
