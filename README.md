# Eye Tracking Beam Control

ระบบ eye tracking สำหรับควบคุม laser shutter ผ่าน Arduino โดยใช้ MediaPipe ตรวจจับดวงตา

## รันโปรแกรม

```bash
source venv/bin/activate
python main.py
```

---

## โครงสร้างไฟล์

```
eye_tracking/
├── main.py        ← จุดเริ่มต้น
├── app.py         ← UI และการเชื่อมต่อ component
├── capture.py     ← การจับภาพ / ตรวจจับดวงตา / บันทึกวิดีโอ
├── arduino.py     ← การสื่อสารกับ Arduino
└── video/         ← ไฟล์วิดีโอที่บันทึก
```

---

## แต่ละไฟล์ทำอะไร

### `main.py`
- สร้าง `tk.Tk()` root window
- สร้าง `EyeTrackingApp` และเปิด `mainloop()`
- ไม่มี logic อื่น

### `app.py` — `EyeTrackingApp`
- สร้าง UI ทั้งหมด (sidebar + camera display)
- จัดการ source tab: **CAMERA** / **VIDEO**
- ซิงค์ค่า UI ไปยัง `self._p` (plain dict) ผ่าน tkinter traces — capture thread อ่านจากนี้โดยไม่ต้องแตะ tkinter
- เรียก `ArduinoController.connect_async()` หลัง mainloop เริ่มแล้ว (ไม่ block UI)
- สร้าง/หยุด `CaptureThread` เมื่อกด START/STOP
- รับ frame จาก queue และแสดงผลบนหน้าจอทุก 30ms
- จัดการปุ่ม: START, STOP, PAUSE/RESUME, REC

### `capture.py` — `CaptureThread`
- รันใน background thread แยกต่างหาก
- อ่านค่าจาก `params` dict เท่านั้น (ไม่เรียก tkinter เลย)
- **MediaPipe detection**: สร้าง `FaceMesh` ใน thread นี้เอง (ป้องกัน OpenGL segfault)
  - คำนวณ EAR (Eye Aspect Ratio) — หากกะพริบตา → ปิด beam
  - คำนวณระยะเบี่ยงเบนของ iris/pupil จากจุดศูนย์กลาง (mm) โดยแปลง px→mm จากขนาด iris — หากอยู่ในค่า threshold ที่ตั้งไว้ → เปิด beam
- **ไม่มี MediaPipe**: ปิด beam ตลอด (ไม่มี fallback detection)
- **Recording**: บันทึก raw frame (ก่อนวาด annotation) ไปยัง `video/`
- ส่ง progress กลับผ่าน callback (`root.after`) — thread-safe

### `arduino.py` — `ArduinoController`
- `connect_async()`: เชื่อมต่อ serial port ใน background thread พร้อม callback เมื่อเสร็จ
- `send(msg)`: ส่งคำสั่งผ่าน serial พร้อม lock
- คำสั่งที่ใช้: `B1\n` = เปิด beam, `B0\n` = ปิด beam
- Arduino ส่ง `READY\n` ตอน boot เพื่อยืนยัน connection (ป้องกัน false positive จาก device อื่น)

---

## Arduino / Hardware

| รายการ | ค่า |
|---|---|
| Board | Arduino Mega 2560 Pro |
| Serial Port | `/dev/ttyUSB1` (เสียบ USB ช่องเดิมทุกครั้ง) |
| Baud Rate | 9600 |
| **Relay Pin** | **Digital Pin 4** |
| Sketch | `arduino_sketch/eye_tracking_beam/eye_tracking_beam.ino` |

> **หมายเหตุ**: Relay บน custom PCB ต่ออยู่กับ pin 4 — อย่าเปลี่ยน `BEAM_PIN` ใน sketch

---

## การแก้ไข segfault

| สาเหตุ | การแก้ |
|---|---|
| `serial.Serial()` + `sleep(2)` block main thread ก่อน event loop พร้อม | ใช้ `connect_async()` เชื่อมต่อใน background thread |
| MediaPipe ใช้ OpenGL context — สร้างใน main thread แต่ใช้ใน capture thread | สร้างและปิด `FaceMesh` ใน capture thread เสมอ |
| เรียก `.get()` บน tkinter variables จาก background thread (Tcl ไม่ thread-safe) | ใช้ `self._p` dict แทน — main thread เขียน, capture thread อ่าน |

---

## Dependencies

```
opencv-python
mediapipe
pyserial
Pillow
```

ติดตั้ง:
```bash
pip install opencv-python mediapipe pyserial Pillow
```
