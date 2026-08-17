# Eye Tracking Beam Control

ระบบ eye tracking สำหรับควบคุม laser shutter ผ่าน Arduino โดยใช้ MediaPipe ตรวจจับดวงตา

## รันโปรแกรม

```bash
source venv/bin/activate
python main.py
```

ค่าเริ่มต้นคือ **โหมดใช้งานจริง**: มีแหล่งภาพเดียวคือ Camera (ไม่มีแท็บให้เลือก) และไม่มีส่วน DEBUG แสดงอยู่ในหน้าจอ

### โหมด debug (สำหรับพัฒนา/ทดสอบเท่านั้น)

```bash
EYE_TRACKING_DEBUG=1 python main.py
```

เปิดใช้งานสองอย่างที่ไม่ควรมีตอนใช้งานจริงกับผู้ป่วย:
- แท็บ **Input Source: Camera / Video** — โหมด Video เล่นไฟล์วิดีโอที่บันทึกไว้ซ้ำผ่าน pipeline เดียวกัน มีไว้สำหรับ regression test/สาธิตเมื่อไม่มีกล้อง ไม่มีสถานการณ์ใช้งานจริงที่ยิงลำโปรตอนจากไฟล์วิดีโอ
- ส่วน **DEBUG** (START DEBUG) — บันทึกภาพ crop ตาทุก 3 เฟรม + `log.csv` ลง `debug/session_.../` ไว้ให้ทีมพัฒนาไปจูน algorithm

ปุ่ม **REVIEW** ใน FLOW panel (ฝั่งขวา) ไม่เกี่ยวกับ debug mode — เปิดดูไฟล์ recording ล่าสุดด้วยโปรแกรมเล่นวิดีโอเริ่มต้นของเครื่อง (`xdg-open`) ใช้งานได้ทั้งสองโหมดหลังกด RECORD แล้ว STOP REC อย่างน้อยหนึ่งครั้ง

---

## โครงสร้างไฟล์

```
eye_tracking/
├── main.py              ← จุดเริ่มต้น
├── app.py               ← UI และการเชื่อมต่อ component
├── capture.py           ← การจับภาพ / ตรวจจับดวงตา / บันทึกวิดีโอ
├── arduino.py           ← การสื่อสารกับ Arduino
├── test_relay.py        ← bench test: วนทุก state อัตโนมัติ (ฟังเสียง relay)
├── test_relay_hold.py   ← bench test: ค้าง state ไว้จนกด Enter (ใช้ตอนวัด multimeter)
└── video/               ← ไฟล์วิดีโอที่บันทึก
```

---

## แต่ละไฟล์ทำอะไร

### `main.py`
- สร้าง `tk.Tk()` root window
- สร้าง `EyeTrackingApp` และเปิด `mainloop()`
- ไม่มี logic อื่น

### `app.py` — `EyeTrackingApp`
- สร้าง UI 3 คอลัมน์: sidebar ซ้าย (SYSTEM STATUS ปักหมุดบนเสมอ + SETTING + DEBUG, เลื่อนได้) / camera feed ตรงกลาง / sidebar ขวา (FLOW: Target Position, Beam status, Ready/Start/Stop/Pause, Recording — ปักหมุดไม่เลื่อน)
- จัดการ source tab: **CAMERA** / **VIDEO**
- ซิงค์ค่า UI ไปยัง `self._p` (plain dict) ผ่าน tkinter traces — capture thread อ่านจากนี้โดยไม่ต้องแตะ tkinter
- เรียก `ArduinoController.connect_async()` หลัง mainloop เริ่มแล้ว (ไม่ block UI)
- **Auto-preview**: เปิดกล้อง (unarmed) ทันทีที่เลือก Camera เป็น Input Source — ปรับ Eye Selection/Detection/Threshold/Target Position ได้ก่อนกด READY ด้วยซ้ำ ไม่ต้องกดอะไรเพื่อ "ดูภาพ"
- **READY**: เช็คแค่ Arduino connected + กล้อง reachable เท่านั้น ไม่เปิด/ปิดกล้อง (พรีวิวเปิดอยู่แล้วเป็นอิสระ)
- **START**: ถ้าพรีวิวรันอยู่แล้ว (โหมดกล้อง) แค่ *arm* ของเดิม (`capture.armed = True`) ไม่เปิดกล้องซ้ำ — ถ้ายังไม่มีพรีวิวรัน (เช่นโหมด Video) เปิดใหม่แบบ armed ทันที
- **STOP**: โหมดกล้อง = แค่ *disarm* (ส่ง `B0`, ภาพพรีวิวไม่ดับ) / โหมด Video = ปิดกล้อง/วิดีโอเต็มรูปแบบ
- คลิกที่ camera feed: ปรับ target position — หรือถ้าคลิกโดนกรอบ inset เล็ก จะสลับจอหลักแทน (ดู "ภาพกล้อง 3 มุมมอง" ใน `capture.py`)
- รับ frame + metrics จาก queue แสดงผลบนหน้าจอทุก 30ms
- จัดการปุ่ม: READY/UNREADY, START, STOP, PAUSE/RESUME, REC

### `capture.py` — `CaptureThread`
- รันใน background thread แยกต่างหาก
- อ่านค่าจาก `params` dict เท่านั้น (ไม่เรียก tkinter เลย)
- **MediaPipe detection (หลัก)**: สร้าง `FaceMesh` ใน thread นี้เอง (ป้องกัน OpenGL segfault)
  - คำนวณ EAR (Eye Aspect Ratio) — หากกะพริบตา → ปิด beam
  - คำนวณระยะเบี่ยงเบนของ iris/pupil จากจุดศูนย์กลาง (mm) โดยแปลง px→mm จากขนาด iris — หากอยู่ในค่า threshold ที่ตั้งไว้ → trigger
  - **มีแค่ผลลัพธ์ชุดนี้เท่านั้น**ที่มีสิทธิ์สั่งเปิด/ปิด beam จริง
- **ไม่มี MediaPipe**: ปิด beam ตลอด (ไม่มี fallback detection)
- **`armed` flag**: gate ว่า trigger จะส่ง `B1` ไป Arduino ได้จริงไหม — `False` เสมอระหว่างพรีวิว (ก่อนกด START) ไม่ว่า detection จะ trigger หรือไม่ก็ตาม เปิดผ่าน `app.start()` เท่านั้น
- **Grayscale cross-check (เสริม, ไม่กระทบ beam)**: FaceMesh instance ที่ 2 แยกอิสระ รันบนภาพขาวดำของเฟรมเดียวกัน throttle ทุก `_GRAY_EVERY_N` เฟรม (ประหยัด CPU เพราะรัน FaceMesh 2 รอบ/เฟรมกินโหลดเยอะ) ได้แค่ตัวเลขไว้เทียบใน UI (`iris_px_gray`, `deviation_mm_gray`) ไม่แตะ trigger/threshold/Arduino เลย
- **ภาพกล้อง 3 มุมมอง** (`_compose_display()`): `wide` (เต็มหน้า) / `zoom_color` (ซูมตา 3 เท่า สี) / `zoom_gray` (ซูมตา 3 เท่า ขาวดำ) — มุมมองหนึ่งเป็นจอหลักตาม `params['main_view']` อีกสองมุมมองที่เหลือโชว์เป็น inset เล็กมุมล่างขวา คลิก inset ในหน้าจอ (ผ่าน `app._on_feed_click`) เพื่อเลื่อนขึ้นมาเป็นจอหลักแทน คลิกจอหลักเองปรับ target อย่างเดียวไม่สลับจอ
- **Recording**: บันทึก raw frame (ก่อนวาด annotation ใด ๆ) ไปยัง `video/`
- ส่ง progress กลับผ่าน callback (`root.after`) — thread-safe

### `arduino.py` — `ArduinoController`
- `connect_async()`: เชื่อมต่อ serial port ใน background thread พร้อม callback เมื่อเสร็จ
- `send(msg)`: ส่งคำสั่งผ่าน serial พร้อม lock
- คำสั่งที่ใช้: `B1\n`/`B0\n` = เปิด/ปิด beam, `E1\n`/`E0\n` = เปิด/ปิด Enable (ส่งตอนกด READY/UNREADY)
- Arduino ส่ง `READY\n` ตอน boot เพื่อยืนยัน connection (ป้องกัน false positive จาก device อื่น)

---

## Arduino / Hardware

| รายการ | ค่า |
|---|---|
| Board | Arduino Mega 2560 Pro |
| Serial Port | `/dev/ttyUSB1` (เสียบ USB ช่องเดิมทุกครั้ง) |
| Baud Rate | 9600 |
| **Relay Pin (A)** | **Digital Pin 4** — Enable, toggle ตาม E0/E1 (เริ่มที่ LOW ตอน boot — fail-safe) |
| **Relay Pin (B)** | **Digital Pin 6** — Beam, toggle ตาม B0/B1 |
| **Trigger Pin** | **Digital Pin 7** → J1 (coax 5V) — ALPIDE readout clock, toggle ตาม T0/T1 (เริ่มที่ LOW ตอน boot) |
| Sketch | `arduino_sketch/eye_tracking_beam/eye_tracking_beam.ino` |

### คำสั่ง serial ทั้งหมด

| คำสั่ง | ผล |
|---|---|
| `B1` / `B0` | Beam relay เปิด/ปิด (ส่งทุกเฟรมจาก `capture.py` ตามตำแหน่งตา) |
| `E1` / `E0` | Enable relay เปิด/ปิด (ส่งจาก `_toggle_ready()` ตอนกด READY/UNREADY) |
| `T1` / `T0` | ALPIDE trigger เปิด/ปิด |
| `TF<hz>` | ตั้งความถี่ trigger 1–95000 Hz (เช่น `TF9500`) |
| `TD<pct>` | ตั้ง duty cycle 1–99 % (เช่น `TD50`) |
| `T?` | พิมพ์ค่า trigger ปัจจุบัน (ใช้ debug บนโต๊ะเท่านั้น — แอปไม่เคยส่งและไม่เคยอ่าน reply) |
| `TSH` / `TSL` | สั่ง D7 เป็น HIGH/LOW ค้างโดยไม่ผ่าน timer (debug บนโต๊ะ — แยกปัญหาสายกับปัญหาโค้ด timer) |
| `TR?` | dump ค่า register ของ Timer4 จริงจากชิป (debug บนโต๊ะ) |

### ALPIDE trigger (J1)

D7 เป็น **OC4B** = ขา hardware PWM ของ Timer4 ช่อง B ดังนั้นพัลส์ถูกสร้างโดยตัว timer เอง ไม่ได้สร้างใน `loop()` — ความถี่จึงนิ่งเป๊ะไม่ว่า serial จะยุ่งแค่ไหน ครอบคลุม 1 Hz – 95 kHz (ปรับ prescaler อัตโนมัติ) ใช้แทน trigger ที่ปกติกล่อง FPGA ของ KCMH ส่งผ่าน LEMO เมื่อกล่องนั้นเป็นตัวที่เสียบ DB9 อยู่

> ⚠️ **ถ้าจะแก้โค้ด `trigApply()`: ต้องตั้งโหมด Fast PWM ให้เสร็จก่อนแล้วค่อยเขียน `OCR4B`** — `OCR4B` มี double buffer เฉพาะในโหมด PWM ส่วนโหมด CTC/Normal ไม่มี ถ้าเขียนตอนที่ timer ยังอยู่ใน CTC (เช่น หลังสั่ง `TCCR4B = 0` เพื่อหยุด timer) ค่าจะลงแค่ compare register แล้วบัฟเฟอร์ที่ยังไม่ถูกเซ็ตจะทับทิ้งตอน BOTTOM รอบแรก อาการคือ `OCR4B` เหลือแค่ไบต์ล่าง (เคยเจอ: ตั้ง 31249 แล้วได้ 17 = `0x11` → duty เหลือ 0.03% วัดแล้วเหมือนไม่มีสัญญาณเลย) ใช้ `TR?` อ่านค่าจริงจากชิปเทียบทุกครั้งที่แก้
>
> ⚠️ **D6 (Relay B) กับ D7 อยู่บน Timer4 ตัวเดียวกัน** (D6 = OC4A, D7 = OC4B) โค้ดต่อ output เฉพาะช่อง B เท่านั้น (COM4A/COM4C เป็น 0 เสมอ) D6 จึงยังเป็น GPIO ปกติที่ `digitalWrite()` คุมได้ — **ห้ามใช้ `analogWrite()` กับ D6/D7/D8** และ **ห้าม `digitalWrite(D7)` ขณะ trigger ทำงาน** เพราะ `digitalWrite` ของ Arduino จะเคลียร์ COM4B1 ทำให้ trigger ดับเงียบๆ

### DB9 wiring (TSS interlock)

| DB9 pin | ความหมาย | ต่อกับอะไร |
|---|---|---|
| 1 | Gating Beam ON | Relay B — COM ของ Relay B |
| 3 | GND | NC ของ relay ทั้งสองตัว |
| 4 | +12V Ref | **รางจ่ายเฉยๆ** — NO ของ relay ทั้งสองตัว |
| 6 | Enable | COM ของ Relay A |

**กฎการต่อสายที่ห้ามผิด** — relay แต่ละตัวสลับ **ขาสัญญาณของตัวเอง** (ขา 1 = Beam, ขา 6 = Enable) ระหว่าง GND (ขา 3) กับรางขา 4 เท่านั้น:

| Relay | COM | NC (relay off) | NO (relay on) |
|---|---|---|---|
| A (Enable, Arduino D4) | DB9 ขา **6** | DB9 ขา 3 (GND) | DB9 ขา **4** |
| B (Beam, Arduino D6) | DB9 ขา **1** | DB9 ขา 3 (GND) | DB9 ขา **4** |

> ⚠️ **ขา 4 (+12V Ref) ต้องไม่ถูกสวิตช์และห้ามต่อลง GND เด็ดขาด** — เคยต่อผิดโดยเอา COM ของ Relay A ไว้ที่ขา 4 (สลับระหว่างขา 3 กับขา 6) ซึ่งให้ผลเหมือนกันทุกประการในสถานะ Enable ON จึงไม่มีใครจับได้เลยตลอดที่ Enable ถูก hardwire ค้างไว้ แต่พอเปิดใช้สถานะ Enable OFF จริงจะกลายเป็นการช็อร์ต +12V ของ TSS ลง GND และปล่อยขา 6 (Enable) ลอย — แก้ไปแล้วเมื่อ 2026-08-14 หลังวัดเทียบกับกล่อง FPGA/KCMH

**Truth table ที่ต้องวัดได้** (ตรงกับกล่อง FPGA/KCMH ที่ใช้งานจริง — ใช้ `test_relay_hold.py` ค้าง state ไว้แล้ววัด continuity):

| state | ขาที่ถึงกัน | ขาที่ลอย |
|---|---|---|
| Beam OFF, Enable OFF (boot / `E0`) | 1 + 3 + 6 | 4 |
| Beam OFF, Enable ON (`E1`) | {1+3} และ {4+6} | — |
| Beam ON, Enable ON (`B1`) | 1 + 4 + 6 | 3 |

> **หมายเหตุเรื่อง Enable**: ตั้งแต่การแก้ไขล่าสุด Enable ไม่ใช่ software jumper ที่ค้าง HIGH ตลอดอีกต่อไป แต่ toggle ตามคำสั่ง `E1`/`E0` ที่ส่งมาจาก `_toggle_ready()` ใน `app.py` (กด READY = Enable ON, UNREADY = Enable OFF) เพื่อให้มี human authorization step ก่อน Enable จะขึ้น เทียบเท่ากับ checkbox Enable ของ KCMH-Tricker แทนที่จะ assert ทันทีที่ Arduino มีไฟ — ตอน boot (`setup()`) RELAY_A เริ่มที่ `LOW` เสมอ (fail-safe: ต้องมีคนกด READY ก่อนเท่านั้นถึงจะ Enable ได้)

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
