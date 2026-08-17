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
├── arduino.py           ← การสื่อสารกับ Arduino (เขียนคำสั่ง + อ่าน reply)
├── alpide_daq.py        ← คุม ALPIDE/EUDAQ2 acquisition
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

## ALPIDE acquisition (วัด latency ของการ gate)

**จุดประสงค์:** ยืนยันว่าตอนตาอยู่ตำแหน่งถูกมีโปรตอนออกมาจริง และตอนตาหลุด **บีมหยุดจริงที่เวลาเท่าไหร่**

### ทำไม trigger ต้องวิ่งต่อเนื่อง ห้าม gate ตามบีม

ถ้า trigger ดับตามบีม จะไม่มี readout ระหว่างบีมดับ → **"ไม่มีโปรตอน" กับ "ไม่ได้อ่านค่า" แยกกันไม่ออก** ซึ่งเป็นการเปรียบเทียบที่การวัดทั้งหมดตั้งอยู่บนนั้น เพราะงั้น trigger วิ่งคงที่ตลอดขณะ armed

### นาฬิกากลาง: จำนวนพัลส์ trigger

Arduino รู้ทั้งสองอย่าง — รับคำสั่ง `B1`/`B0` **และ** เป็นตัวสร้างพัลส์ trigger เอง เพราะงั้นตอนบีมเปลี่ยนสถานะจริง มันจะพิมพ์ `EV B1 <pulses>` / `EV B0 <pulses>` กลับมา (นับจาก `T1`) แล้วแอปบันทึกลง `alpide_output/session_*/beam_events.csv`

**ข้อมูล ALPIDE เก็บหมายเลข trigger มาด้วย** — อ่านผ่าน pyeudaq ที่ระดับ subevent: `ev.GetSubEvents()[i].GetTriggerN()` (ตัว merged event ด้านนอก `GetTriggerN()` เป็น 0 เสมอ ต้องลงไประดับ plane) เพราะงั้น**ต่อให้ event หลุดก็ไม่ทำให้เพี้ยน** อ่านเลข trigger ตรงๆ ได้เลย ไม่ต้องพึ่งการนับเรียงลำดับ

**คอลัมน์ `trig_running`** — ถ้าเป็น `0` แปลว่า `pulse` ในแถวนั้น**ใช้ไม่ได้** เป็นเลขค้างจาก run ก่อนหน้า (`T0` หยุด trigger แต่ไม่ล้างตัวนับ มีแค่ `T1` ที่รีเซ็ต) เกิดกับ transition ที่เกิดก่อน EUDAQ2 ขึ้นเสร็จ **ใช้เฉพาะแถวที่ `trig_running=1`**

**การเทียบ:** `trigN = pulse − 1` — Arduino นับ "คาบที่ครบแล้ว" ส่วน EUDAQ เริ่มนับ trigger แรกที่ได้รับเป็น 0 (ต่างกัน 1 trigger = 1 ms ที่ 1 kHz) และต้อง**ข้าม event หัวไฟล์ (BORE)** ที่ `trigN` เป็นค่าขยะ

> **ลำดับสำคัญ:** `T1` รีเซ็ตตัวนับพัลส์ เพราะงั้นแอปจะส่งมัน**หลัง**จาก RunControl เข้าสถานะ RUNNING แล้วเท่านั้น (`wait_for_running()`) ถ้าส่งก่อน run ขึ้น ตัวนับจะเดินไปหลายพันพัลส์ก่อนที่ EUDAQ จะเริ่มนับ — วัดได้จริงครั้งหนึ่งว่าเพี้ยนไป ~6,000 พัลส์ ทำให้เทียบสองไฟล์ไม่ได้เลย
>
> การเลื่อน `T1` ไปทีหลัง**ไม่หน่วงบีมเลย** เพราะ trigger มีไว้ clock ALPIDE อย่างเดียว ส่วนการ gate บีมใช้ relay ล้วน — arm เกิดทันทีตั้งแต่กด START เหมือนเดิม

**เรื่องความถี่กับ event ที่หลุด** — วัดอัตราจริงของ plane ที่ช้าที่สุดได้ดังนี้:

| ความถี่ | อัตราที่ได้จริง | |
|---|---|---|
| 1000 Hz | 997 ev/s (99.7%) | trigN ต่อเนื่อง 0 ช่วงขาด ตลอด 39,922 event |
| 5000 Hz | 4569 ev/s (91%) | เริ่มหลุด |
| 9500 Hz | 3158 ev/s (33%) | พังชัดเจน |

**ทำไม KCMH ใช้ 9500 Hz ได้แต่เราใช้ไม่ได้:** ที่นั่นยิงเป็น burst ~1 วินาทีแล้วพัก (Zaber เลื่อน) บัฟเฟอร์จึงระบายทัน ส่วนเราต้องยิงต่อเนื่องเพื่อให้เห็นช่วงบีมดับ backlog เลยสะสมจนล้น — อัตราต่อเนื่องที่ระบบรับไหวอยู่ราว 3,000 ev/s

คอขวดคือ**จำนวน event ต่อวินาที ไม่ใช่ปริมาณข้อมูล** (การทดสอบนี้ไม่มีบีมเลย ทุกเฟรมแทบว่างเปล่า แต่ยังตามไม่ทัน) ซึ่งเข้ากับการที่ `ALPIDEProducer.py` เขียนด้วย Python — และแปลว่าตอนมีบีมจริงจะหนักกว่านี้

ตอนนี้ event ที่หลุดไม่กระทบความถูกต้องแล้ว (เพราะมี `trigN`) ความถี่จึงเป็นเรื่องคุณภาพ/ความละเอียด ไม่ใช่ความถูกต้อง — `TRIGGER_HZ = 1000` ให้ 1 ms ซึ่งละเอียดกว่า latency ระดับหลายสิบ ms อยู่มาก
>
> **pulse count กับ host timestamp ตรงกันดี** — วัดจริงแล้วต่างกันไม่เกิน 1 ms ทุกช่วง (เทียบ 7 ช่วงติดกันที่ 1000 Hz) เพราะงั้น timestamp ใน CSV ใช้อ้างอิงได้ ไม่ได้เพี้ยน
>
> สิ่งที่ jitter จริงคือ **จังหวะที่แอปออกคำสั่งได้** (Tk `after()` มาช้ากว่าที่สั่งไว้ได้หลายร้อย ms ตอนโหลด MediaPipe สูง) — ทั้งสองคอลัมน์บันทึกเวลาที่เกิดขึ้นจริงตรงกัน ไม่ใช่คอลัมน์ใดผิด
>
> ยังใช้ pulse count เป็นตัวอ้างอิงหลักอยู่ เพราะมันคือดัชนีเดียวกับข้อมูล ALPIDE โดยตรง ไม่ต้องแปลงหน่วยหรือเทียบนาฬิกา

### ขั้นตอนการใช้งานกับ TSS ที่ KCMH

**กด START ในแอปพร้อมๆ กับกด Beam On ที่ console ของ รพ. — ห้ามห่างกันเกิน ~3 วินาที**

ที่มา: จากการไปใช้งานจริงที่ KCMH 3 รอบ ทำแบบนี้ตลอด (เป็นแนวปฏิบัติที่ใช้ได้จริง ไม่ใช่ spec ที่มีเอกสารจากทีม TSS — ถ้าได้เอกสารมาทีหลังควรมาปรับตรงนี้)

ยังไม่ทราบว่าทำไมต้องไม่เกิน 3 วินาที (น่าจะเป็น timeout ของการ request beam ฝั่ง TSS) แต่**ข้อจำกัดนี้เป็นเหตุผลที่ชัดเจนว่าทำไม START ต้อง arm ทันที** — ถ้าโค้ดหน่วงเวลา arm ด้วยเหตุใดก็ตาม หน้าต่าง 3 วินาทีจะหลุด

นี่คือเหตุผลที่งาน ALPIDE ทั้งหมด (flash firmware, launch EUDAQ2 ~10 วินาที, เปิด trigger) ถูกโยนไป background thread และ**ไม่มีอะไรในนั้นบล็อก `start()` ได้เลย** — ไม่ใช่แค่หลักการออกแบบลอยๆ แต่มีข้อจำกัดหน้างานรองรับ

### ขั้นตอนอัตโนมัติ

| กด | ทำอะไร |
|---|---|
| **READY** | `E1` + flash FX3 firmware ถ้าบอร์ดอยู่ใน DFU mode |
| **START** | ตั้งความถี่ + `T1` → launch EUDAQ2 (background) → arm ทันทีไม่รอ |
| **STOP** | `B0` → `T0` → หยุด EUDAQ2 + ปิด CSV |

**START ไม่รอให้ EUDAQ2 ขึ้นก่อน arm** — EUDAQ2 ใช้เวลา ~10 วินาทีจึงพร้อม การหน่วงบีมไว้นานเท่านั้นแย่กว่าการเสียข้อมูลช่วงต้นไม่กี่วินาที และ transition ที่จะวัดเกิดซ้ำตลอด run อยู่แล้ว

**ทุกอย่างในส่วน ALPIDE เป็น best-effort** — ถ้าบอร์ดไม่อยู่/flash ไม่ผ่าน/EUDAQ2 พัง จะขึ้นข้อความในบรรทัดสถานะเท่านั้น **ไม่ขวาง READY/START/STOP หรือการ gate บีมเลย**

### กับดักที่เจอมาแล้ว (สำคัญ)

1. **EUDAQ2 รันใน venv ของ eye_tracking ไม่ได้** — สคริปต์ใช้ `#!/usr/bin/env python3` และต้องการ `urwid` + `alpidedaqboard` (editable install) จาก user site-packages ซึ่ง venv ไม่มี `alpide_daq.clean_env()` ถอด `VIRTUAL_ENV`/`PATH` ของ venv ออกก่อน spawn ทุกครั้ง
   **แย่กว่านั้น: tmux pane สืบทอด env จาก tmux server ตัวที่เริ่มไว้ก่อน** เพราะงั้นถ้า launch ครั้งแรกด้วย env ที่ปนเปื้อน มันจะพังซ้ำๆ จนกว่าจะ `tmux kill-server`
2. **FX3 firmware อยู่ใน RAM** บอร์ดกลับเป็น DFU mode ทุกครั้งที่ไฟหลุดหรือ run ถูกตัดกลางคัน — ต้อง flash ทุก session (แอปทำให้อัตโนมัติตอน READY)
3. **tmux session ชื่อ `ITS3` กับบอร์ด 6 ตัวเป็นทรัพยากรร่วมกับ KCMH-Tricker** — **ห้ามรัน acquisition สองแอปพร้อมกัน**

   `session_state()` แยก session ที่**กำลังทำงานจริง** (producer ครบ 6) ออกจาก**ซากที่ค้าง** (run จบหรือ crash แล้วแต่ tmux ยังอยู่) — ซากจะถูกเก็บกวาดอัตโนมัติแล้ว launch ต่อ ส่วน session ที่ทำงานอยู่จริงจะไม่แตะ

   เดิมเช็คแค่ "มี session ไหม" ซึ่งทำให้**ซากที่ตายแล้วบล็อกทุก run ถัดไปเงียบๆ** — แอปยังเขียนไฟล์ครบ ดูปกติทุกอย่าง แต่ไม่มี `.raw` เลย และรู้ตัวตอนมาวิเคราะห์ (เสีย run 59 วินาทีไปหนึ่งครั้ง)

### ค่าที่ตั้งใน `config.py`

`ALPIDE_NUM`, `ALPIDE_EVENTS`, `ALPIDE_STROBE`, `ALPIDE_ITHR`, `TRIGGER_HZ`, `TRIGGER_DUTY`, `KCMH_TRICKER_DIR`, `EUDAQ_DIR` — อยู่ในไฟล์ไม่ใช่ UI เพราะเปลี่ยนตาม campaign ไม่ใช่ตาม session (มีแค่ความถี่ trigger ที่โผล่มาใน UI เพราะต้องปรับหน้างาน)

`alpide_daq.py` **import โมดูลของ Kcmh-Tricker ตรงๆ ไม่ copy** — logic การ generate config ที่นั่นมีค่าคงที่ของฮาร์ดแวร์จริง (serial ของ DAQ 6 ตัว, ตาราง VCASN/VCASN2, `EUDAQ_FW_PATTERN`) ถ้า copy มาจะ drift จากกันเงียบๆ

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
| `E1` / `E0` | Enable relay เปิด/ปิด (ส่งจาก `_toggle_ready()` ตอนกด READY/UNREADY และส่งซ้ำเป็น heartbeat) |
| `H` | heartbeat — refresh watchdog เฉยๆ ไม่เปลี่ยนสถานะอะไร (ใช้ในเครื่องมือ bench) |
| `T1` / `T0` | ALPIDE trigger เปิด/ปิด |
| `TF<hz>` | ตั้งความถี่ trigger 1–95000 Hz (เช่น `TF9500`) |
| `TD<pct>` | ตั้ง duty cycle 1–99 % (เช่น `TD50`) |
| `T?` | พิมพ์ค่า trigger ปัจจุบัน (ใช้ debug บนโต๊ะเท่านั้น — แอปไม่เคยส่งและไม่เคยอ่าน reply) |
| `TSH` / `TSL` | สั่ง D7 เป็น HIGH/LOW ค้างโดยไม่ผ่าน timer (debug บนโต๊ะ — แยกปัญหาสายกับปัญหาโค้ด timer) |
| `TR?` | dump ค่า register ของ Timer4 จริงจากชิป (debug บนโต๊ะ) |

### Relay watchdog

**การปิด serial port ไม่ได้ reset บอร์ด** (พิสูจน์แล้วบนของจริง) เพราะงั้นถ้าไม่มี watchdog แอปที่ crash หรือค้างจะทิ้ง relay ค้างสถานะเดิมไว้ — รวมถึง Enable ที่ค้าง ON โดยไม่มีใครคุม

เฟิร์มแวร์จึงมี watchdog: **ถ้าไม่ได้รับคำสั่งใดๆ เกิน 2 วินาที relay ทั้งสองตัวจะตกลงสถานะปลอดภัย** แล้วค้างอยู่แบบนั้นจนกว่า host จะสั่งใหม่ (ไม่คืนสถานะเองอัตโนมัติ)

ฝั่ง host ส่งเจตนาซ้ำเป็นระยะแบบ idempotent ไม่ใช่แค่ ping เปล่า:
- `app.py` → `_tick_heartbeat()` ส่ง `E1` ทุก ~360 ms ขณะ READY
- `capture.py` → ส่งสถานะบีมเดิมซ้ำทุก 10 เฟรม (`_BEAM_REFRESH_N`) เพิ่มจากการส่งตอนเปลี่ยนสถานะ
- ผลคือฮาร์ดแวร์จะ converge เข้าหาสิ่งที่ซอฟต์แวร์ต้องการเสมอ ถ้า watchdog เคยตัดไปแล้วก็กลับมาถูกต้องเอง

**ถ้า capture thread ตายขณะ armed** `_tick_heartbeat()` จะหยุดส่ง heartbeat แล้วเรียก `stop()` + แจ้งเตือน — เพราะแอปไม่มีสิทธิ์อ้างสถานะบีมอีกต่อไปเมื่อ thread ที่ตัดสินใจตายแล้ว

> **watchdog คุมแค่ relay ไม่คุม trigger** — trigger เป็นนาฬิกา readout ของ detector ไม่ใช่ส่วนของ interlock และถ้าให้ timeout 2 วิไปคุมด้วย เครื่องมือวัดบนโต๊ะที่ค้างสถานะเป็นนาทีจะใช้ไม่ได้

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
