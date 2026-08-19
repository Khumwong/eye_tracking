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

ปุ่ม **REVIEW** ใน FLOW panel (ฝั่งขวา) ไม่เกี่ยวกับ debug mode — เปิดดูไฟล์ recording ล่าสุดด้วยโปรแกรมเล่นวิดีโอเริ่มต้นของเครื่อง (`xdg-open`) ใช้งานได้หลังติ๊ก **Record video (mp4)** แล้วผ่าน run มาแล้วอย่างน้อยหนึ่งครั้ง (ดูหัวข้อ RECORD)

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
- สร้าง UI 3 คอลัมน์: sidebar ซ้าย (SYSTEM STATUS ปักหมุดบนเสมอ + SETTING + DEBUG, เลื่อนได้) / camera feed ตรงกลาง / sidebar ขวา (FLOW: Target Position, Beam status, ☑ Enable / LAUNCH DAQ / START / STOP / CANCEL DAQ / KILL BEAM, Recording — ปักหมุดไม่เลื่อน)
- จัดการ source tab: **CAMERA** / **VIDEO**
- ซิงค์ค่า UI ไปยัง `self._p` (plain dict) ผ่าน tkinter traces — capture thread อ่านจากนี้โดยไม่ต้องแตะ tkinter
- เรียก `ArduinoController.connect_async()` หลัง mainloop เริ่มแล้ว (ไม่ block UI)
- **Auto-preview**: เปิดกล้อง (unarmed) ทันทีที่เลือก Camera เป็น Input Source — ปรับ Eye Selection/Detection/Threshold/Target Position ได้ก่อนติ๊ก ENABLE ด้วยซ้ำ ไม่ต้องกดอะไรเพื่อ "ดูภาพ"
- **ENABLE**: เช็คแค่ Arduino connected + กล้อง reachable เท่านั้น ไม่เปิด/ปิดกล้อง (พรีวิวเปิดอยู่แล้วเป็นอิสระ)
- **START**: ถ้าพรีวิวรันอยู่แล้ว (โหมดกล้อง) แค่ *arm* ของเดิม (`capture.armed = True`) ไม่เปิดกล้องซ้ำ — ถ้ายังไม่มีพรีวิวรัน (เช่นโหมด Video) เปิดใหม่แบบ armed ทันที
- **STOP**: โหมดกล้อง = แค่ *disarm* (ส่ง `B0`, ภาพพรีวิวไม่ดับ) / โหมด Video = ปิดกล้อง/วิดีโอเต็มรูปแบบ
- คลิกที่ camera feed: ปรับ target position — หรือถ้าคลิกโดนกรอบ inset เล็ก จะสลับจอหลักแทน (ดู "ภาพกล้อง 3 มุมมอง" ใน `capture.py`)
- รับ frame + metrics จาก queue แสดงผลบนหน้าจอทุก 30ms
- จัดการปุ่ม: ☑ ENABLE, LAUNCH DAQ, `start without DAQ`, START, STOP, CANCEL DAQ, KILL BEAM, ☑ Record video

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
- คำสั่งที่ใช้: `B1\n`/`B0\n` = เปิด/ปิด beam, `E1\n`/`E0\n` = เปิด/ปิด Enable (ส่งตอนติ๊ก/ปลดติ๊ก ENABLE)
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

### ITS3 monitor ในแอป

**แถบจุด 6 planes ใต้ "Detector"** โชว์ตลอด — เขียว = RUNNING, เหลือง = state อื่น (CONFIGURED/STARTING), แดง = ERROR, เทา = ไม่พบ plane นั้น บรรทัด `Detector` ด้านบนบอกว่ามีบอร์ดกี่ตัว ส่วนแถบนี้บอกว่า**ตัวไหนเก็บข้อมูลอยู่จริง** ซึ่งคือความต่างระหว่าง run กับ run ที่เงียบๆ ไม่ได้บันทึกอะไรเลย

**แท็บ `ITS3` มุมล่างซ้ายของภาพกล้อง** กดแล้วแผ่น RunControl **ลอยทับลงบนภาพ** ขนาดพอดี 80×40 ตัวอักษร — เห็นทั้งจอ RunControl ในหน้าเดียว ไม่ต้องเลื่อน ไม่มีขอบว่าง

**ใช้ `place()` ไม่ใช่ `pack()`** — `pack()` ให้แผ่นมีแถวของตัวเอง ซึ่งบีบภาพกล้องให้เตี้ยลงทุกครั้งที่เปิด ส่วน `place()` วางทับลงไปเฉยๆ **ภาพกล้องไม่ขยับ ไม่ย่อ** ภาพที่กระโดดทุกครั้งที่เปิดดู diagnostic เป็นภาพที่ไม่ควรใช้เล็งลำโปรตอน

ขนาดกำหนดเป็น**จำนวนตัวอักษร ไม่ใช่พิกเซล** แล้วปล่อยให้กรอบหดพอดีเนื้อหาเอง วางมุมล่างซ้ายเพราะตาคนไข้มักอยู่กลางเฟรม มุมล่างจึงเป็นส่วนที่เสียไปแล้วกระทบน้อยที่สุด

**สีทาเองไม่ได้ดึง ANSI มา** — ชื่อ plane navy ตัวหนา / `RUNNING` เขียว / `ERROR` แดง / state อื่นเหลือง / เส้นกรอบเทาจาง ใช้ชุดสีเดียวกับจุด 6 จุดใน sidebar เห็นจุดแดงแล้วกางลงมาจะเจอสีเดียวกันตรงบรรทัดนั้น การ parse escape code จาก alternate-screen pane จะได้แค่สีที่ TUI เลือกไว้ ส่วนการ tag เองทำให้สีไปอยู่บนสิ่งที่กำลังตามหาจริง

**อยู่ล่าง ไม่ใช่คอลัมน์ข้าง** — ตาราง RunControl กว้าง 80 ตัวอักษร เต็มความกว้างจอแล้วอ่านได้ครบทุกคอลัมน์โดยไม่ต้องเลื่อน ซึ่งคอลัมน์ที่แคบพอจะยอมเสียพื้นที่ถาวรทำไม่ได้ และการยุบลงล่างคืนความสูงให้ภาพกล้องได้เต็ม

ใช้ scroll แนวนอนแทนการ wrap (wrap จะทำให้ state หลุดออกจากบรรทัดของ producer ที่มันเป็นเจ้าของ) — ปกติไม่ได้ใช้ จะมีผลเมื่อย่อหน้าต่างให้แคบกว่าตารางเท่านั้น และหลัง update จะ scroll ลงล่างสุดแต่**ตรึงไว้ที่ขอบซ้าย** เพราะ `see(END)` เลื่อนไปทางขวาด้วย ซึ่งจะดันชื่อ plane ตกจอ

ทั้งสองอย่างกินข้อมูลจาก `tmux capture-pane` **ครั้งเดียวต่อรอบ** (`alpide_daq.pane_text()` / `plane_states()` ซึ่ง `wait_for_running()` ก็ใช้ตัวเดียวกัน) poll บน worker thread แล้วส่งกลับด้วย `root.after(0, ...)` **ตาม pattern เดียวกับ `_alpide_status_tick` ที่ poll `lsusb` อยู่แล้ว** — ไม่มี subprocess ไหนรันบน UI thread

ตอนแถบยุบอยู่จะ**ไม่แตะ `tk.Text` เลย** และตอนกางอยู่ก็เขียนเฉพาะเมื่อข้อความเปลี่ยน (ตัวที่แพงคือการยัด text เข้า widget ไม่ใช่ตัว poll)

**จับเฉพาะหน้าจอปัจจุบันของ pane ไม่ดึง scrollback** — RunControl วาดตารางทับที่เดิมแบบ TUI การขอ history จาก alternate-screen pane จะได้สิ่งที่อยู่ก่อนหน้า TUI แทนที่จะได้ตาราง

**`debug/eudaq.log`** — เก็บข้อความ pane ก้อนสุดท้ายตอน STOP **ก่อน** `stop_run()` จะฆ่า tmux session (scrollback หายไปพร้อม session) เก็บ**ก่อน** early return ของเคส `pid is None` ด้วย เพราะ run ที่ไม่เคยขึ้นคือเคสที่ต้องการ log มากที่สุด และเก็บเฉพาะเมื่อมีโฟลเดอร์ session อยู่แล้ว ไม่งั้นการปิดแอปเฉยๆ จะสร้าง session ว่างทิ้งไว้ทุกครั้ง

**ไม่มี send-keys** ต่างจาก `EmbeddedTerminal` ของ Kcmh-Tricker — ช่องยิงคีย์เข้า DAQ ที่กำลังเก็บข้อมูลไม่ควรอยู่ห่างแค่คลิกเดียวระหว่างรักษา

### ภาพหน้าจอตอนบีมดับ: `output/session_<ts>/cuts/`

ทุกครั้งที่บีมถูกสั่งดับ (`B0` ตอนเปลี่ยนสถานะ ไม่ใช่ periodic refresh) จะเก็บ**ภาพเดียวกับที่อยู่บนจอ** ไว้หนึ่งใบ พร้อม `cuts.csv`:

```
index,host_time_iso,host_monotonic,deviation_mm,threshold_mm,reason,file
```

`host_monotonic` เป็นนาฬิกาเดียวกับ `beam_events.csv`, `*_frames.csv` และ `track.csv` → join ไปหา pulse count และข้อมูล ALPIDE ได้ตรงตัว **`reason` มาจาก `_gate_reason()` เมธอดเดียวกับที่ `track.csv` ใช้** (`unarmed` / `kill` / `no_face` / `blink` / `deviation` / `hold`) ไม่ใช่คำนวณแยกจาก trigger/deviation เองแบบเดิม — เดิมเคยดู trigger กับ deviation อย่างเดียว จนเจอของจริง: STOP ปลด armed กลางคันตอนตาอยู่บนเป้าพอดี cut ที่เกิดขึ้นถูกติดป้าย `deviation` ทั้งที่ `deviation_mm` ต่ำกว่า threshold มาก ตอนนี้เช็ค `armed`/`kill_latched` ก่อนเสมอ ตรงลำดับเดียวกับ `_gate_open()` เอง `cuts.csv` กับ `track.csv` จึงไม่มีทางขัดกันอีก (`test_reason_is_why_the_beam_actually_went_off` ใน `test_cut_capture.py`)

**ผูกกับการ arm ไม่ใช่ปุ่ม RECORD** — 5 จาก 8 session แรกไม่มีวิดีโอเลยเพราะไม่มีใครกด RECORD และบีมที่ตัดโดยไม่มีภาพของตาอธิบายทีหลังไม่ได้

**เขียนไฟล์บน thread แยก** ลูปที่ตัดสินใจเรื่องบีมส่งเฟรมเข้า `queue.Queue(maxsize=8)` ด้วย `put_nowait` แล้วไปต่อทันที คิวเต็ม = ทิ้งเฟรมนั้นแล้วนับไว้ (บันทึกท้าย `cuts.csv`) **ห้าม block เด็ดขาด** เพราะคาบของลูปนี้คือคาบตัดสินใจ gate ซึ่งเป็นก้อนใหญ่ที่สุดใน latency ที่กำลังวัด ส่วน `cv2.imwrite` JPEG 1080p กิน ~11-17 ms

**ไม่ copy เฟรม** — `_compose_display` สร้าง array ใหม่ทุกรอบ และผู้ใช้ปลายทางทั้งสองอ่านอย่างเดียว (video writer ใช้ `raw`, UI resize ไป array ใหม่) วัดแล้ว copy กิน 4.4 ms ของ budget ลูปโดยไม่ได้อะไรกลับมา — ถ้าวันหนึ่งมีใครวาดทับเฟรมที่ display ต้องกลับมาใส่ copy

จำกัดที่ `CUT_MAX_PER_SESSION` (500) กันเคสกระพริบรัวถมดิสก์ พอชนเพดานจะบันทึกไว้ใน CSV ว่าหยุดเก็บแล้ว

**`test_cut_capture.py`** วัดเวลาของ `put_nowait` เองว่าต่ำกว่า 1 ms แม้ตอนคิวเต็มและ writer กำลังเขียนอยู่ — บั๊กแบบ "เขียนไฟล์บน gate thread" ไม่ทำให้อะไรพัง แค่ทำให้ตัวเลข latency ผิดเงียบๆ จึงต้องมีเทสต์จับ

### `track.csv` — log ตำแหน่งตาต่อเฟรม ครอบทั้งช่วง armed

ก่อนหน้านี้ `log.csv` มีเฉพาะ `EYE_TRACKING_DEBUG=1` + กด START DEBUG ซึ่งไม่ใช่สิ่งที่เปิดตอนใช้งานจริง — run จริงเลยเหลือร่องรอยตำแหน่งตาแค่ใน `cuts.csv` (เฉพาะเฟรมที่บีมดับ) ตอบไม่ได้ว่าตาห่าง threshold แค่ไหนตลอด run และลอง `min_off_s`/threshold อื่นย้อนหลังไม่ได้ **`track.csv`** เขียนทุกเฟรมตลอดช่วง armed (ผูกกับ START เหมือน `cuts/`):

```
frame,host_time_iso,t_capture,t_decided,deviation_mm,iris_px,detect,gate,beam_state,reason
```

`t_capture` คือ monotonic ตอน `cap.read()` คืนค่า, `t_decided` คือหลัง `arduino.send()` ของเฟรมนั้นแล้ว — **`t_decided - t_capture` คือ latency ฝั่งแอปเอง (detect + ตัดสินใจ) ต่อเฟรม** ซึ่งไม่เคยมีตัวเลขนี้มาก่อนเลย ส่วน `reason` มาจาก `_gate_reason()` เมธอดแยกที่ derive จาก input ชุดเดียวกับ `_gate_open()` **ไม่แตะ `_gate_open()` เอง** (เมธอดนั้นมีเทสต์คุมอยู่แล้วและเป็นจุดตัดสินใจเรื่องบีมทั้งหมด)

เขียนตรงจาก detection loop เอง ไม่ผ่าน queue+thread แบบ `cuts/` (นั่นมีไว้สู้ JPEG encode 11-17 ms ส่วนนี่คือ `csv.writerow` string สั้นๆ) แต่ยัง flush เป็นช่วง (ทุก 30 แถว ≈ 1 วิ) ไม่ใช่ทุกแถว — เทสต์ต้นทุนต่อเฟรมอยู่ใน `test_cut_capture.py` เช่นกัน (`test_track_does_not_block`)

### `debug/app.log` — event log ของแอปเอง อยู่รอด `launch.sh` ไม่ redirect stdout

`launch.sh` ไม่ redirect stdout — เปิดจาก desktop icon แล้ว `[ALPIDE] …`, `[BEAM] killed by operator`, `[Arduino] board reset mid-session` หายไปกับอากาศ ทั้งที่นี่คือที่เดียวที่อธิบาย run ที่ออกมาแปลก

`app.log(msg)` แทน `print()` ทุกจุดใน `app.py` — พิมพ์ออก stdout เหมือนเดิม **และ** ต่อท้าย `collections.deque(maxlen=2000)` เสมอ พอโฟลเดอร์ session ถูกสร้างครั้งแรก (`_session_dir()`) จะดัมพ์ buffer ทั้งก้อนลง `debug/app.log` แล้วเขียนต่อท้ายไปเรื่อยๆ — **ต้องมี ring buffer เพราะข้อความสำคัญเกิดก่อนโฟลเดอร์มีอยู่เสมอ** (Arduino connect, flash FX3 ตอนติ๊ก ENABLE, กล้องเปิดไม่ขึ้น ล้วนเกิดก่อน LAUNCH) ปิดไฟล์ (`_close_app_log()`) ทุกจุดที่ `_session_root` ถูกล้าง เพื่อให้ run ถัดไปได้ `app.log` ของตัวเอง ไม่ใช่ต่อท้ายของเก่า

`log()` ถูกเรียกจาก worker thread ด้วย (`_alpide_note`, ตัวอ่าน serial) จึงมี `_log_lock` คุม buffer/ไฟล์ ไม่ให้บรรทัดฉีกกลางคัน

### การวิเคราะห์: `analyze_latency.py`

```
python3 analyze_latency.py output/session_20260817_191837
python3 analyze_latency.py --self-check      # session ล่าสุด ตรวจสุขภาพข้อมูลอย่างเดียว
```

**รันให้เองหลัง STOP แล้ว** — `app.py` spawn subprocess ตัวนี้บน worker thread เดียวกับที่ `stop_run()` เพิ่งคืนค่ามา (ไม่ใช่ UI thread ไม่บล็อก STOP) เป็น **subprocess ไม่ใช่ in-process call** ด้วยเหตุผลเดียวกับที่ ALPIDE acquisition เป็น subprocess — numpy กับ decoder ไม่ควรอยู่ในโปรเซสที่คุมบีม และถ้า analyzer พังก็พังคนเดียว `eudaq.stop()` sleep ~6 วิก่อน kill tmux อยู่แล้วเพื่อให้ RunControl flush `.raw` เสร็จ แต่ path ที่ exception (fallback ของ `stop_run()`) ข้ามการรอนั้นไป จึงมีเช็คขนาดไฟล์นิ่งเพิ่มอีกชั้น (สูงสุด 5 วิ) ก่อนส่งให้ analyzer

อ่าน `session.json` + `beam_events.csv` + `alpide/*.raw` แล้วออกมาเป็นตาราง latency ต่อ transition, สรุปสถิติ และเขียน `latency.csv` ลงในโฟลเดอร์ session เอง เป็น offline ล้วน ไม่แตะโค้ดที่รันตอนวัด

**เขียน `report.txt` + `analysis.json` ทุกครั้งที่รัน ไม่ว่าจะจบทางไหน** — `report.txt` คือข้อความเดียวกับที่เคยพิมพ์ออกจอทั้งก้อน (ผ่าน `_Tee` คลาสเล็กๆ ที่ทำสำเนาทุก `write()`) `analysis.json` มีตัวเลขแบบอ่านด้วยโปรแกรมได้ (`decode_errors`, `planes_out_of_step`, `trigger_period_us`, สถิติ `B0`/`B1`, `latency_budget`) พร้อม **`verdict`**: `ok` / `no_beam_signal` / `daq_sync_problem` / `no_transitions` / `no_raw_file` / `no_decodable_data` / `self_check` / `error` — เขียนแม้ตอน `read_session()`/`read_beam_events()` โยน `SystemExit` หรือแม้แต่ตอน exception ที่ไม่คาดคิด (`main()` จับไว้ เขียนไฟล์ แล้ว re-raise ต่อ traceback ยังขึ้น stderr เหมือนเดิม) **เพราะจุดประสงค์ทั้งไฟล์นี้คือให้ session folder อธิบายตัวเองได้ รวมถึงตอนที่ตัว analyzer เองพังด้วย**

**ส่วน latency budget** อ่าน `track.csv` มาต่อกับผลจาก `.raw`:

```
── latency budget ────────────────────────────
  [3]→[5]  detect + decide     : median  12.34 ms   p95  28.90 ms
           frame period         : median  33.30 ms
  [5]→[6]  command → beam off  : median   40.0 ms  (safety-critical leg)
  ────────────────────────────────────────────────────────────
  measured total, closing leg  :   52.3 ms
  not yet measured [1]→[3] (camera exposure + USB transfer) — needs
  an LED the Arduino lights and stamps itself; see next.md
  opening leg additionally waits min_off_s = 1 s once the beam is cut,
  before any of the above starts
```

ส่วน `[3]→[5]` (detect+decide, คาบเฟรม) โผล่แม้ตอน self-check หรือไม่มีบีมสัญญาณ เพราะเป็นการวัดประสิทธิภาพของแอปเองไม่ใช่ของ `.raw` ส่วน `[5]→[6]` (command → beam off/on) ต้องรอ `rows` จาก `measure()` จึงมีเฉพาะตอนวัดสำเร็จ **`[1]→[3]` (sensor exposure + USB) วัดจากซอฟต์แวร์ไม่ได้เลย** ต้องมีตัวกระตุ้นฮาร์ดแวร์ (LED ที่ Arduino ติดเองแล้วประทับ pulse count เหมือน `beam_events.csv`) — ยังไม่ทำในรอบนี้ ดู next.md

**อ่าน `.raw` ด้วย decoder ที่ port มาเป็น Python** (`decode_block()`) — pyeudaq ที่ build ไว้บนเครื่องนี้มีแค่ `FileReader` **ไม่มี `StdEventConverter`/`StandardEvent`** เพราะงั้นเรียก converter ตัว C++ จาก Python ไม่ได้ (สคริปต์ของ Kcmh-Tricker ใช้ build คนละตัวที่ `/home/sutpct/`) งานนี้ต้องการแค่**จำนวน** hit ไม่ต้องการพิกัด จึง port มาจาก `eudaq2/user/ITS3/module/src/ALPIDERawEvent2StdEventConverter.cc` แทนการ rebuild EUDAQ2

โครงสร้าง: 1 `ITS3global` event = 1 trigger, มี 6 sub-event (plane ละตัว) sub-event ละ 1 block รูปแบบ block:

| ตำแหน่ง | ความหมาย |
|---|---|
| 0-4 | `AA AA AA AA` header |
| 4-8 | trigger number (uint32 LE) — ตรงกับ `GetTriggerN()` ของ sub-event |
| 8-16 | timestamp นับเป็น tick ของ clock 80 MHz (×12500 = ps) |
| 16+ | `0xE0` = empty frame / `0xA0` = chip header แล้วตามด้วย region header `0xC0`, data short `0x40`, data long `0x00` (+ hit map 7 บิต), chip trailer `0xB0`, idle `0xFF` |
| ท้าย | `BB BB BB BB` trailer |

**plane ที่หลุด sync ไม่ทำให้ทั้ง run เสีย** — แต่ละ plane ถูกยื่นเข้าตารางด้วย **trigger number ของตัวเอง** ไม่ใช่บังคับให้ทั้ง 6 ตรงกัน เจอของจริงใน `session_20260818_132638`: plane 1 เดินนำอีก 5 ตัวอยู่ 1 trigger ตั้งแต่ event ที่ 3 ไปจนจบ run ถ้าทิ้ง event ที่ plane ไม่ตรงกันจะเหลือข้อมูล **2 จาก 19,411 event** และ run จะดูเหมือนว่างเปล่า

timestamp เอาจาก plane กลุ่มที่รายงาน trigger number **ต่ำสุด** เท่านั้น (เสียงข้างมากคือตัวตั้งนาฬิกา) — ถ้าปนกับ plane ที่นำอยู่ เวลาที่บันทึกจะเดินถอยหลังระหว่างแถว

รายงานจะบอกชัดว่า plane ไหนหลุดกี่ครั้ง และถ้าเกินครึ่ง run จะระบุว่า**นี่คือปัญหา sync ของ DAQ ไม่ใช่เรื่องไม่มีโปรตอน** — สองอย่างนี้ให้ผลหน้าตาเดียวกันแต่ต้องไปแก้คนละที่

**หา noise floor กับ beam level จาก run เดียวกัน ไม่ใช้ค่าคงที่** — noise ต่อ plane ต่างกันได้ถึง 50 เท่า (วัดจริงตอนไม่มีบีม: p4 = 2.99 hits/trigger แต่ p0 = 0.055) `beam_events.csv` บอกอยู่แล้วว่าช่วงไหนบีมควรเปิด/ปิด จึงตัด guard band รอบ transition ออกแล้ววัดสองระดับจากสองกลุ่มนั้น plane ที่บีมไม่ยกขึ้นเหนือ noise ของตัวเองอย่างมีนัยจะถูกตัดทิ้ง ถ้าไม่เหลือ plane ไหนเลย → รายงาน `no beam signal detected` แทนที่จะเดาตัวเลขออกมา

**หาจุดที่บีมดับจริงด้วย maximum-likelihood changepoint ไม่ใช่ threshold บน sliding window** — window mean จะข้ามจุดกึ่งกลางระหว่างสองระดับตอนที่ window เพิ่งผ่านจุดเปลี่ยนจริงมาครึ่งเดียว ทำให้**ตอบเร็วไปประมาณครึ่งหนึ่งของความกว้าง window** ซึ่งที่ window 20 ms คือคลาดไป ~10 ms บนปริมาณที่มีขนาดพอๆ กัน hit count เป็น Poisson จึงเขียน likelihood ตรงๆ ได้ และเมื่อรู้สองระดับแล้ว log-likelihood ของจุดเปลี่ยนที่ `t` ยุบเหลือ suffix sum — จุดที่มากสุดคือคำตอบ ไม่ต้องมี window เลย (เทสต์ด้วยข้อมูลสังเคราะห์: คืนค่าตรงเป๊ะที่ 40/12/40 ms เทียบกับวิธี window ที่ให้ 31/3/32)

**`test_latency.py`** ครอบส่วนที่ข้อมูลจริงพิสูจน์ไม่ได้ (ยังไม่เคยมีโปรตอน จึงไม่เคยมี drop ให้จับเวลา) — ปลูก drop ไว้ที่ offset ที่รู้ค่า, จำลอง event หลุด 3%, บีมอ่อนจนวัดไม่ได้, และ B0 ที่บีมไม่สนใจ

### `session.json` — provenance + run จบยังไง

นอกจากค่าที่ตั้ง (threshold, target, trigger Hz, …) `_write_session_json()` เติมให้เองทุกครั้งที่เขียน แต่ละ field แยก try/except กันเอง (field หนึ่งพังไม่ทำให้ที่เหลือหาย):

| field | มาจากไหน | ทำไมต้องมี |
|---|---|---|
| `git_commit` / `git_dirty` | `git rev-parse --short HEAD` / `git status --porcelain` | ตัวเลข latency ที่ไม่รู้ว่ามาจากโค้ดตัวไหนอ้างอิงไม่ได้ |
| `hostname` | `socket.gethostname()` | รันบนเครื่องไหน |
| `camera.width/height/nominal_fps/achieved_fps` | `cap.get(...)` + `track.csv` (`frames`/เวลาที่ผ่านไปตั้งแต่ START) | เคยวัดได้ 27 fps จากกล้องที่อ้าง 50 — เทอมนี้อยู่ในโซ่ latency ตรงๆ `achieved_fps` โผล่เฉพาะตอน track log ทำงานจริง (ไม่ใช่ตอน LAUNCH ที่ยังไม่มีเฟรมนับ) |
| `frames` | `capture._track_frame_idx` ตอนกด STOP | จำนวนเฟรมที่ประมวลผลจริงใน run นี้ |
| `beam_transitions` | นับแถวใน `beam_events.csv` | เทียบกับจำนวนแถวใน `latency.csv` ได้ว่าวัดได้กี่อันจากทั้งหมด |
| `cuts_saved` / `cuts_dropped` | `capture._cut_saved`/`_cut_dropped` | มีอยู่แล้วในโค้ด แค่ไม่เคยถูกเก็บ |
| `ended` | ผู้เรียก `stop()` ระบุเอง | `stop` / `enable_off` / `arduino_reset` / `app_closed` — **เดิม run ที่แอปตายกลางคันหน้าตาเหมือน run ที่จบปกติ** ต่างกันแค่ไม่มีคีย์ `stopped` ซึ่งอ่อนเกินไป |

`ended='app_closed'` มาจาก `on_close()` เขียน session.json เองก่อนปิดแอป (ไม่ผ่าน `stop()` เพราะไม่ต้องรอ ALPIDE teardown ที่มีอยู่แล้วในนั้น) — เฉพาะตอนที่ปิดกลาง run ที่ armed อยู่เท่านั้น ปิดตอนไม่มี run ไม่เขียนอะไรเพิ่ม

### Flow เทียบกับ Kcmh-Tricker

```
Kcmh-Tricker:  ☑ Enable → Load Run → Launch default → Run   → [Kill beam] → Enable off
                                        └ Cancel
eye_tracking:  ☑ ENABLE →    —     → LAUNCH DAQ     → START → [KILL BEAM] → uncheck ENABLE
                                        ├ start without DAQ (fallback, before LAUNCH)
                                        └ CANCEL DAQ
```

| Kcmh-Tricker | eye_tracking | |
|---|---|---|
| ☑ Enable | ☑ **ENABLE** | ตอนนี้เป็น checkbox จริงเหมือนกันเป๊ะ ไม่ใช่ปุ่มทาสีให้ดูเหมือน — เช็คแล้วส่ง `E1`, เอาเครื่องหมายออกส่ง `E0` (`_on_enable_toggle`) |
| Launch default | **LAUNCH DAQ** | ยก EUDAQ2 ขึ้นเป็นขั้นตอนของตัวเอง |
| Cancel | **CANCEL DAQ** | ยกเลิก launch โดยไม่แตะบีม |
| Run/Stop/Cancel ซ่อนจนกว่าจะ Launch | START/STOP/CANCEL ซ่อนจนกว่าจะ LAUNCH | ตรงกันแล้ว ดูหัวข้อ "แถวที่ซ่อนจนกว่าจะ LAUNCH" |
| Run (`Waiting for ITS3...`) | **START** (`waiting for ITS3…`) | ปุ่มกดไม่ได้จนกว่า detector จะพร้อม เหมือนกัน |
| — | **start without DAQ** | ไม่มีของเทียบ — ทางออกฉุกเฉินที่เรามีแต่เขาไม่มี ดูหัวข้อถัดไป |
| Kill beam (`\xFE`/`\xEF`) | **KILL BEAM** (`B0` + latch) | toggle เหมือนกัน |
| Enable off + dialog 3 ปุ่ม | ปลด ENABLE + dialog 3 ปุ่ม | port มาเป๊ะ |
| Load Run (plan CSV) | — | ยังไม่มี |
| Auto kill beam (10 วิ) | `beam used` counter | ของเราวัดอย่างเดียว ไม่ตัดให้ — ดูหัวข้อ KILL BEAM |

**หน้าที่ของปุ่มที่สามต่างกันคนละเรื่อง** ถึงจะอยู่ตำแหน่งเดียวกันในลำดับ: `Run` ของเขาคือ**แอปเริ่มยิงบีมเอง** (`ProgressWorker` ไล่ไทม์ไลน์ Exposure × Loops) ส่วน `START` ของเราคือ**แอปเริ่มทำหน้าที่ยาม** — คนที่กำหนด t=0 ของบีมคือ console ของโรงพยาบาล ไม่ใช่ปุ่มนี้ นั่นคือที่มาของกฎ 3 วินาที: ปุ่มเราไม่ได้สั่งยิง มันแค่ต้องถูกกด**ก่อน**การยิงเริ่ม

**ทั้งสองระบบ gate บีมอัตโนมัติ ไม่มีฝั่งไหนใช้คนกดเปิดปิด** — ต่างกันแค่สัญญาณที่ใช้ gate: Kcmh-Tricker ใช้ตารางเวลา (`ProgressWorker` วน `\xFE` → รอ `Exposure + Beam delay` → `\xEF` → ขยับ Zaber × `Loops` ตามค่าใน plan CSV) ส่วนเราใช้ตำแหน่งตา ส่วน Kill beam เป็น override ฉุกเฉินทั้งคู่

### LAUNCH DAQ — ทำไมต้องแยกออกจาก START

EUDAQ2 ใช้เวลา ~10 วินาทีจึงพร้อม เดิมงานนี้อยู่ใน START แปลว่า**บีมถูก gate อยู่โดยไม่มี detector บันทึกอะไรเลยตลอดช่วงนั้น** วัดจาก run จริงสองครั้ง:

| session | ช่วงที่ armed แต่ไม่มีข้อมูล | transition ที่หายไป |
|---|---|---|
| `20260817_191837` (threshold 8.5 mm) | 7.23 วิ | 3 จาก 19 = **16%** |
| `20260818_132638` (threshold 3.0 mm) | 6.09 วิ | 20 จาก 71 = **28%** |

ยิ่ง threshold ต่ำยิ่งกระพริบถี่ ยิ่งเสีย transition ในช่วงนั้นเยอะ

และมันทำให้กฎสองข้อขัดกัน: *"กด START พร้อม Beam On ห้ามห่างเกิน 3 วินาที"* กับ *"รอจนสถานะขึ้น recording ก่อนเก็บข้อมูลจริง"* — ทำพร้อมกันไม่ได้

**กด LAUNCH ตอนเซ็ตอัพ รอจุด 6 จุดเขียวครบ แล้ว START เหลือแค่ `T1` + arm** ข้อมูลเริ่มที่ pulse 0 และ START ยังทันกฎ 3 วินาทีเหมือนเดิม (เร็วกว่าเดิมด้วย)

**ไม่กด LAUNCH ก็ยัง START ได้** — จะทำ bring-up เองทั้งดุ้นแบบเดิม ALPIDE เป็นส่วนเสริมห้ามเป็นเงื่อนไขของการ gate บีม

**โฟลเดอร์ session ผูกกับ LAUNCH** เพราะ `start_run()` ฝัง output path ลง EUDAQ2 config ตอน launch → 1 launch = 1 `.raw` หลัง STOP ต้อง LAUNCH ใหม่สำหรับ run ถัดไป

**LAUNCH คือขั้นที่เปิด sensor จริง** ไม่ใช่ START:

| กด | แตะอะไร |
|---|---|
| ENABLE | บอร์ด DAQ (FX3) เท่านั้น — flash firmware ถ้าอยู่ DFU **ยังไม่แตะชิป ALPIDE** |
| LAUNCH | **ชิปทั้ง 6** — `gen_its3_conf()` เขียน STROBE/ITHR → RunControl configure ชิป → `StartRun` → readout วิ่ง รอ trigger |
| START | `T1` — trigger วิ่ง ชิป latch เฟรมทุกพัลส์ ข้อมูลลง `.raw` |

เทียบเป็นกล้อง: LAUNCH = เปิดกล้อง ตั้งค่า เล็ง / START = เริ่มกดชัตเตอร์ 1000 ครั้งต่อวินาที — **หลัง LAUNCH ชิปติดไฟและกินกำลังอยู่แล้ว** ทิ้งค้างไว้ไม่ฟรี

### แถวที่ซ่อนจนกว่าจะ LAUNCH — และทางออกฉุกเฉินที่ไม่ได้ซ่อน

START/STOP/CANCEL DAQ อยู่ในกรอบเดียวกัน (`self._run_group`) และ**ไม่โผล่เลยจนกว่าจะกด LAUNCH** ตรงกับที่ Kcmh-Tricker ซ่อน Run/Stop/Cancel ไว้จนกว่าจะกด Launch default — ก่อนหน้านี้ START กดได้เสมอแม้ไม่ผ่าน LAUNCH (จะ bring-up เองทั้งดุ้น) ซึ่งฟังดูปลอดภัยกว่าแต่จริงๆ แล้วทำให้คนเห็นปุ่ม START สว่างอยู่ตลอดโดยไม่รู้ว่าควรกด LAUNCH ก่อน — พฤติกรรมตอนนี้คือ **1 LAUNCH DAQ → (ปรากฏเป็นกลุ่ม) 2 START → 3 STOP** โดย CANCEL DAQ ซ่อนตัวเองอีกชั้นหนึ่งเมื่อ armed (ดูหัวข้อถัดไป)

```python
show_group = armed or launching or self._daq_ready or self._alpide_pid is not None
```

**แต่ ALPIDE ยังคงเป็น best-effort อยู่เหมือนเดิม — แค่ทางเข้าเปลี่ยนจาก "ปุ่มเดิมกดได้เสมอ" เป็น "ปุ่มเล็กแยกต่างหาก"** ปุ่ม **`start without DAQ`** โผล่เฉพาะตอน ENABLE ติ๊กแล้วแต่ยังไม่มี launch (`self.ready and not show_group`) เรียก `start()` ตัวเดิมทุกอย่างเหมือนเดิม (bring-up เองใน background thread, arm ทันทีไม่รอ) เพียงแต่ตั้งใจให้**เล็กกว่าและแยกออกจาก flow หลัก** — ปุ่มไม่ใช่ทางที่แนะนำ เป็นทางที่มีไว้สำหรับตอน ALPIDE ใช้งานไม่ได้เท่านั้น (บอร์ดหาย/flash ไม่ผ่าน/tmux ชนกับ Kcmh-Tricker) การกดคุมบีมของแอปนี้**ต้องไม่มีวันถูกขังไว้เบื้องหลัง detector ที่พัง**

### START รอ ITS3 — ปุ่มกดไม่ได้จนกว่า detector จะพร้อม

ระหว่าง `launching` ปุ่ม 2 (ในกลุ่มที่โผล่มาแล้ว) ขึ้น `waiting for ITS3…` และกดไม่ได้ ตรงกับ `Waiting for ITS3...` ของ Kcmh-Tricker ที่ poll หา `StartRun` ใน `rc.log` (ของเราใช้ `_daq_ready` จาก `wait_for_running()` ซึ่งเป็นสัญญาณเดียวกัน) พร้อม `root.bell()` ตอนพร้อม เพราะคนกด LAUNCH ไปมองอย่างอื่นมาสิบวินาทีแล้ว

**ไม่ขัดกับกฎ 3 วินาที** เพราะการรอเกิดที่ LAUNCH ไม่ใช่ที่ START — LAUNCH กดตอนเซ็ตอัพ พอถึงจังหวะที่ต้องซิงก์กับ console DAQ ขึ้นไปนานแล้ว การกันไว้ตรงนี้แค่บังคับว่าจะไปถึงจังหวะนั้นโดยยังไม่พร้อมไม่ได้

**สิ่งที่ห้ามเกิดคือ detector ขัง gate ไว้** จึงมีทางออกสี่ทาง: launch สำเร็จ (`ready`), launch ล้ม (`_launch_done(False)` → `idle`), launch ที่**ไม่ตอบเลย** (`_launch_watchdog()` คืนปุ่มให้ที่ 60 วินาที — นานกว่า worst case ของ `_alpide_bring_up()` ซึ่ง `wait_for_running` 40 วิเป็นตัวยาวสุด — โดยไม่ไปยุ่งกับ worker ถ้ามันตอบมาทีหลัง `_launch_done()` ก็ยังทำงานปกติ และ START ที่กดไประหว่างนั้นยังถูกเก็บใน `_pending_start_hz` เหมือนเดิม), และทางที่สี่คือ **`start without DAQ`** ที่ไม่ผ่าน LAUNCH เลยตั้งแต่แรก (หัวข้อก่อนหน้า)

### CANCEL DAQ — ยกเลิก LAUNCH โดยไม่แตะบีม

โผล่เฉพาะตอนที่มีอะไรให้ยกเลิก (`launching`, `ready` หรือ `_alpide_pid` ค้าง) และหายไปตอน armed — ระหว่าง run มีปุ่มเดียวที่จบได้คือ STOP

ทำสี่อย่าง: `_alpide_stop()` (หยุด EUDAQ2 + เก็บ `eudaq.log` + kill tmux) → ปล่อย `_session_root` ให้ run ถัดไปได้โฟลเดอร์ใหม่ → `_pending_start_hz = None` → หยุด mp4 ที่ LAUNCH เผลอเริ่มไว้ถ้ามี (checkbox RECORD ติ๊กไว้แล้วยัง LAUNCH ไม่ทันได้ START ก็ถูก LAUNCH สั่งอัดไปแล้ว ดูหัวข้อ RECORD)

**ไม่แตะฝั่งบีมเลยแม้แต่บรรทัดเดียว** — ไม่ส่ง relay command ไม่เปลี่ยน Enable ไม่ปล่อย kill latch นี่คือความต่างจาก STOP ที่ต้องชัด และ `test_beam_gate.py` assert ไว้ว่าไม่มีอะไรถูกส่งไป Arduino เลย

**บั๊กที่ปุ่มนี้ปิด:** เดิมหลัง LAUNCH ไม่มีทางถอยเลย — STOP กดได้เฉพาะตอน armed ส่วน UNREADY เรียก `stop()` เฉพาะเมื่อ capture armed เพราะงั้น run ที่ launch แล้วไม่ได้ start จะทิ้ง `_alpide_pid` ค้างไว้ ซึ่ง `_launch_daq()` เช็คเป็นเงื่อนไขแรกแล้ว `return` → **LAUNCH ครั้งต่อไปไม่ทำอะไรเลยทั้งที่ปุ่มสว่างเป็นขั้นที่ต้องกด** อาการเดียวกับซาก tmux ที่เคยเสีย run 59 วินาทีไป ตอนนี้ทั้ง CANCEL และ UNREADY เรียก `_daq_teardown()` ตัวเดียวกัน

### RECORD — checkbox ที่ตั้งครั้งเดียว ไม่ใช่ปุ่มที่ต้องกดทุก run

**`Record video (mp4)`** เป็น checkbox บอกเจตนา ไม่ใช่ปุ่มเริ่ม/หยุดเหมือนเดิม — ติ๊กไว้ก่อนแล้ว**LAUNCH DAQ เป็นคนสั่งอัดให้** (`_maybe_auto_record()` เรียกจาก `_launch_daq()`) ปุ่ม `start without DAQ` ก็เรียกซ้ำอีกทีเผื่อ LAUNCH ถูกข้ามไปเลย (guard ด้วย `not self.capture._recording` กันอัดซ้อน)

**เหตุผลเดียวกับที่ cuts capture ผูกกับการ arm** — 5 จาก 8 session แรกไม่มีวิดีโอเลยเพราะไม่มีใครกดปุ่ม RECORD ทัน checkbox แก้ที่ต้นเหตุ: ตั้งครั้งเดียวแล้วมันเริ่มเองทุก run โดยไม่ต้องมีใครจำ

**หยุดที่ STOP หรือ CANCEL DAQ เท่านั้น** — ทั้งสองจุดเรียก `_stop_rec()` ตัวเดียวกัน ซึ่งย้าย mp4 ไปให้ REVIEW เปิดได้ทันที **ไม่มี popup "Saved" อีกต่อไป** เพราะตอนนี้มันเกิดทุก run ไม่ใช่การกดปุ่มด้วยมือนานๆ ครั้งเหมือนก่อน — popup ทุก run จะน่ารำคาญกว่าจะมีประโยชน์

**ล็อกพร้อมค่า config อื่นตอน armed** ผ่าน `_set_config_locked()` (เฉพาะโหมดกล้อง) กันเผลอติ๊ก/ถอดติ๊กกลางคันโดยไม่มีผลจนกว่าจะ run ถัดไปแล้วดูเหมือนมีผลทันที

### KILL BEAM — latch ไม่ใช่กดทีเดียว

ตรงกับ toggle `\xFE`/`\xEF` ของ Kcmh-Tricker: **กดค้าง = บีมดับจนกว่าจะกดปล่อย ตาเอาชนะไม่ได้** ถ้าเป็นแบบกดทีเดียว เฟรมถัดไปที่ตาอยู่บนเป้าจะเปิดบีมกลับภายใน ~66 ms ปุ่มก็จะไม่มีความหมาย

- `CaptureThread.kill_latched` — main thread เขียน capture thread อ่าน (pattern เดียวกับ `armed`)
- การตัดสินใจเรื่องบีมทั้งหมดอยู่ใน `CaptureThread._gate_open()` เมธอดเดียว: `trigger and armed and not kill_latched` — grep เจอที่เดียวและเทสต์ได้โดยไม่ต้องมีกล้อง
- **กดได้ตลอดเมื่อ ENABLE ติ๊กแล้ว ไม่ต้อง armed** — การตัดบีมไม่ควรมีเงื่อนไข
- ปุ่มส่ง `B0` จาก UI thread ทันทีด้วย ไม่รอลูปรอบถัดไป
- ป้าย BEAM ขึ้น **BEAM KILLED / Cut by operator** สีแดง แยกจาก "ดับเพราะตาหลุด" — ตัวหลังหายเองได้ ตัวนี้ไม่หาย ต้องตัดสินใจใน `_beam_off()` เพราะ frame loop เรียกมันทุกเฟรม
- START จะไม่เริ่มถ้า latch ยังค้าง (ไม่งั้นจะดูเหมือนตาไม่เข้าเป้าสักที) STOP/ปลดติ๊ก ENABLE ปล่อย latch เอง

**ไม่ทำ Auto kill beam** — ของ Kcmh-Tricker ตัดที่ 10 วินาทีเพราะแอปเขาเป็นคนสั่งยิงเอง จึงเป็นคนตัดเองได้ **run ของเราก็มีจุดจบที่กำหนดไว้ล่วงหน้าเหมือนกัน คือบีมที่ขอมาต้องยิงให้หมด** (ช่วงทดลองขอสั้นๆ ไม่เกิน 10-20 วินาที) แต่**จุดจบนั้นเป็นของ TSS ไม่ใช่ของเรา** — โดสนับเป็น MU ไม่ใช่วินาที TSS หยุดเองเมื่อครบ timer ในแอปจึงไม่ใช่ตัวคุมโดสและไม่ควรทำหน้าที่นั้น

### `beam used` — เวลาบีมติดสะสม

ป้าย BEAM มีบรรทัด `beam used  N.N s` นับ**เฉพาะเวลาที่ชัตเตอร์เปิดจริง**

จำเป็นเพราะเรา gate บีม: **เวลาบนนาฬิกาไม่ได้บอกว่าใช้บีมไปเท่าไหร่** ขอมา 20 วินาที ถ้าตาหลุดครึ่งเวลาก็ต้องยืนอยู่ 40 วินาที ตัวเลขที่เทียบกับสิ่งที่ขอไว้ที่ console ได้มีตัวเดียวคือเวลาสะสมนี้

- นับใน `_beam_on()`/`_beam_off()` ซึ่ง frame loop เรียกทุกเฟรม จึงคีย์จาก `_beam_on_since` ไม่ใช่จากการถูกเรียก
- รีเซ็ตที่ START (นับต่อ run ไม่ใช่ต่อการเปิดแอป) และลง `session.json` เป็น `beam_on_s` ตอน STOP
- **วัดอย่างเดียว ไม่แตะการตัดสินใจเรื่องบีม** — ต่างจาก Auto kill beam ตรงนี้

### Min beam-off — กันบีมกระพริบตอนตาอยู่ขอบ threshold

ตาที่นิ่งอยู่ตรงขอบพอดีจะทำให้ shutter เปิดปิดรัวๆ วัดจริงได้ **35 ครั้งใน 26 วินาที** ที่ threshold 3.0 mm และ run ก่อนหน้ามี 19 ช่วงที่ปิดสั้นกว่า 100 ms ใน 9 วินาที

`Min beam-off` (ช่องพิมพ์ใต้ Threshold — เป็นค่าที่คนคำนวณมาจากช่วงปิดของ run ก่อน ไม่ใช่ค่าที่ต้องลากหาโดยดูภาพสด, ค่าเริ่มต้น `DEFAULT_MIN_OFF_S = 1.0` วิ, ตั้ง 0 เพื่อปิด) บังคับว่า**เมื่อตัดบีมแล้วต้องดับอย่างน้อยเท่านี้ก่อนตาจะเปิดกลับได้**

จุดสำคัญสามข้อ:

1. **หน่วงเฉพาะขาเปิด ขาปิดทันทีเสมอ** — ตรรกะอยู่ใน `_gate_open()` จุดเดียวและมีผลกับการ return `True` เท่านั้น เส้นทางที่ปิดบีมทุกเส้นไม่ถูกแตะ เพราะงั้นมันเอนไปทาง "บีมดับ" ได้อย่างเดียว ไม่มีทางทำให้บีมค้างเปิด
2. **ไม่กระทบการวัด latency ทั้งสองขา** — มันหน่วง**เวลาที่ออกคำสั่ง `B1`** ไม่ใช่ความเร็วที่ฮาร์ดแวร์ตอบสนอง `analyze_latency.py` วัดจากคำสั่งถึงการตอบสนอง ตัวเลขจึงไม่เพี้ยน
3. **ไม่ใช่ hysteresis** — hysteresis เลื่อน threshold ตอน**ปิด** ซึ่งยอมให้บีมค้างเปิดตอนตาเริ่มหลุด อันนั้นเปลี่ยนพฤติกรรมความปลอดภัยจริง ส่วนอันนี้ไม่

ค่าที่ตั้งบันทึกลง `session.json` (`min_off_s`) ทุก run

จากช่วงปิดที่วัดได้จริงทั้งหมด (35, 110, 179, 264, 303, 327, 846, 1330, 1926, 1959, 2154, 2297 ms) — hold 1.0 วิ กลืนไป 7 จาก 12 ช่วง, hold 2.0 วิ กลืน 10 จาก 12

### Theme — ชุดสีเดียวกับ Kcmh-Tricker

palette ใน `config.py` ใช้ค่าจาก `GLOBAL_STYLE` ของ Kcmh-Tricker (`main.py`) เพื่อไม่ให้สองแอปในห้องคุมเดียวกันดูเหมือนคนละผลิตภัณฑ์ — พื้น `#EEF2F7`, panel ขาว, navy `#1E3A5F`, ปุ่มหลัก `#1565C0`, แดง `#C62828`, เขียว `#2E7D32`, amber `#F9A825`

**สีมีสองตระกูลและใช้แทนกันไม่ได้:**

| | ใช้ทำอะไร |
|---|---|
| `*B` / `*L` | พื้นปุ่มหรือ badge สีเข้ม กับ**ตัวหนังสือที่วางบนนั้น** |
| `*_TXT` | สีเดียวกันแต่ใช้เป็น**ตัวหนังสือบนพื้นสว่าง** |

ตอนเป็น dark theme ค่าเดียวรับได้ทั้งสองบทบาท เพราะตัวอ่อนสว่างพอจะอ่านออกบนพื้นมืด พอเปลี่ยนเป็นพื้นสว่างมันขัดกันทันที — ตัวหนังสือบนขาวต้องเข้ม แต่บนปุ่มสีจัดต้องอ่อน `MUTED` (พื้นปุ่ม disabled) กับ `MUTEDT` (ตัวหนังสือจาง) ก็แยกด้วยเหตุผลเดียวกัน

### สี FLOW บอกว่าต้องกดอะไรต่อ

ปุ่มมีเลขกำกับ `1 LAUNCH DAQ → 2 START → 3 STOP` และ**มีปุ่มสว่างแค่ปุ่มเดียวเสมอ คือขั้นที่ต้องกดต่อ** ENABLE เป็น checkbox แยกอยู่นอกเลข — เปิด/ปิดไม่ใช่ "ขั้นตอนที่ต้องกดต่อ" แบบเดียวกับปุ่มอื่น

| บทบาท | สี | ความหมาย |
|---|---|---|
| ขั้นต่อไป | น้ำเงิน `CYAN` `#1565C0` | กดอันนี้ |
| ขั้นต่อไปและมันหยุดบีม | แดง `REDB` | STOP ตอน armed |
| ทำไปแล้ว | เขียว `GREENB` | ไม่ต้องทำอะไรตรงนี้ |
| กดได้ แต่ไม่ใช่ขั้นที่ควรกด | `PANEL` จาง | เช่น START ทั้งที่ DAQ ยังไม่ ready |
| ยังไม่ถึงคิว | `MUTED` | disabled |

ทั้งหมดวาดจาก `_refresh_flow()` **ที่เดียว** ซึ่งอนุมานหน้าตาทุกปุ่มจาก state เดิมแต่ละ handler ตั้งสีเองกระจายอยู่ 17 จุด ทำให้สีไม่ได้สื่ออะไรเลยว่าอะไรมาก่อนหลัง

**START อยู่ในกลุ่มที่ซ่อนจนกว่าจะ LAUNCH** (ดูหัวข้อ "แถวที่ซ่อนจนกว่าจะ LAUNCH") — เมื่อกลุ่มโผล่มาแล้ว ปุ่ม START จาง (ไม่ disable) ตอน DAQ ยังไม่ ready เพราะ**การ bring-up เองยังทำได้** ถ้าอยากข้าม LAUNCH ไปตั้งแต่แรกให้ใช้ `start without DAQ` แทน — ALPIDE ยังคงห้ามเป็นเงื่อนไขของการ gate บีมเสมอ เพียงแต่ทางเข้าตอนนี้แยกเป็นสองปุ่มคนละที่กัน

**ข้อยกเว้นเดียวคือระหว่าง `launching`** ที่ START ขึ้น `waiting for ITS3…` และ disabled จริง — ไม่ใช่การเอา ALPIDE มาเป็นเงื่อนไข แต่เป็นการกันไม่ให้เริ่มระหว่างที่ยังขึ้นไม่เสร็จ ซึ่งมีทางออกครบสี่ทาง (สำเร็จ / ล้ม / watchdog 60 วิ / `start without DAQ`) ดูหัวข้อ START รอ ITS3

`CANCEL DAQ` อยู่ในกลุ่มเดียวกับ START/STOP แต่ไม่มีเลขกำกับ (เหมือน `start without DAQ` และ KILL BEAM ที่อยู่นอกเลขเช่นกัน) — ใช้สี `_OPEN` เสมอ ไม่เคยเป็นปุ่มสว่าง

### ลำดับใน STOP สำคัญ

`stop()` ปลด `capture.armed` **ก่อน** ส่ง `B0` เดิมปลดทีหลังไป 6 คำสั่ง (รวม subprocess ของ eudaq.log) ระหว่างนั้น capture thread ยัง armed และ `_BEAM_REFRESH_N` จะ re-state คำสั่งเดิมทุกเฟรมที่ 10 — **ถ้าตาอยู่บนเป้าพอดีจะส่ง `B1` เปิดบีมกลับหลังกด STOP** ช่องนี้ ~10-30 ms จึงไม่ค่อยโดน แต่ไม่ใช่ศูนย์

`test_beam_gate.py` บันทึกลำดับเหตุการณ์จริงของ `stop()` แล้ว assert ว่า disarm มาก่อน `B0` — สลับลำดับกลับแล้วเทสต์ FAIL จริง

### ขั้นตอนการใช้งานกับ TSS ที่ KCMH

**กด START ในแอปพร้อมๆ กับกด Beam On ที่ console ของ รพ. — ห้ามห่างกันเกิน ~3 วินาที**

ที่มา: จากการไปใช้งานจริงที่ KCMH 3 รอบ ทำแบบนี้ตลอด (เป็นแนวปฏิบัติที่ใช้ได้จริง ไม่ใช่ spec ที่มีเอกสารจากทีม TSS — ถ้าได้เอกสารมาทีหลังควรมาปรับตรงนี้)

ยังไม่ทราบว่าทำไมต้องไม่เกิน 3 วินาที (น่าจะเป็น timeout ของการ request beam ฝั่ง TSS) แต่**ข้อจำกัดนี้เป็นเหตุผลที่ชัดเจนว่าทำไม START ต้อง arm ทันที** — ถ้าโค้ดหน่วงเวลา arm ด้วยเหตุใดก็ตาม หน้าต่าง 3 วินาทีจะหลุด

นี่คือเหตุผลที่งาน ALPIDE ทั้งหมด (flash firmware, launch EUDAQ2 ~10 วินาที, เปิด trigger) ถูกโยนไป background thread และ**ไม่มีอะไรในนั้นบล็อก `start()` ได้เลย** — ไม่ใช่แค่หลักการออกแบบลอยๆ แต่มีข้อจำกัดหน้างานรองรับ

### ขั้นตอนอัตโนมัติ

| กด | ทำอะไร | ส่งไป Arduino |
|---|---|---|
| ☑ **ENABLE** | flash FX3 firmware ถ้าบอร์ดอยู่ใน DFU mode | `E1` |
| **LAUNCH DAQ** | configure ชิป + ยก EUDAQ2 (background) + เริ่ม RECORD ถ้าติ๊กไว้ | — ไม่ส่งอะไร |
| **CANCEL DAQ** | หยุด EUDAQ2 + ปล่อยโฟลเดอร์ + หยุด RECORD ที่ LAUNCH เริ่มไว้ | — ไม่ส่งอะไร |
| **start without DAQ** | ข้าม LAUNCH ไปเลย ตั้งความถี่ + `T1` (bring-up เองใน background) → arm ทันที | `TF<hz>` `TD` `T1` |
| **START** (ในกลุ่มหลัง LAUNCH) | ตั้งความถี่ + `T1` → arm ทันทีไม่รอ | `TF<hz>` `TD` `T1` |
| **STOP** | disarm → หยุด EUDAQ2 + ปิด CSV + หยุด RECORD | `B0` `T0` |
| ☐ **ENABLE** (ปลด) | หยุด EUDAQ2 ที่ค้างอยู่ด้วย | (`B0`) `E0` |
| **KILL BEAM** | latch | `B0` |

**START ที่ไม่ได้ผ่าน LAUNCH ไม่รอให้ EUDAQ2 ขึ้นก่อน arm** — EUDAQ2 ใช้เวลา ~10 วินาทีจึงพร้อม การหน่วงบีมไว้นานเท่านั้นแย่กว่าการเสียข้อมูลช่วงต้นไม่กี่วินาที และ transition ที่จะวัดเกิดซ้ำตลอด run อยู่แล้ว (ทางที่ถูกคือกด LAUNCH ตอนเซ็ตอัพแล้วรอ ซึ่งตอนนี้ flow บังคับให้อยู่แล้ว — `start without DAQ` มีไว้เฉพาะตอน ALPIDE ใช้งานไม่ได้)

**ทุกอย่างในส่วน ALPIDE เป็น best-effort** — ถ้าบอร์ดไม่อยู่/flash ไม่ผ่าน/EUDAQ2 พัง จะขึ้นข้อความในบรรทัดสถานะเท่านั้น **ไม่ขวาง ENABLE/START/STOP หรือการ gate บีมเลย**

### กับดักที่เจอมาแล้ว (สำคัญ)

1. **EUDAQ2 รันใน venv ของ eye_tracking ไม่ได้** — สคริปต์ใช้ `#!/usr/bin/env python3` และต้องการ `urwid` + `alpidedaqboard` (editable install) จาก user site-packages ซึ่ง venv ไม่มี `alpide_daq.clean_env()` ถอด `VIRTUAL_ENV`/`PATH` ของ venv ออกก่อน spawn ทุกครั้ง
   **แย่กว่านั้น: tmux pane สืบทอด env จาก tmux server ตัวที่เริ่มไว้ก่อน** เพราะงั้นถ้า launch ครั้งแรกด้วย env ที่ปนเปื้อน มันจะพังซ้ำๆ จนกว่าจะ `tmux kill-server`
2. **FX3 firmware อยู่ใน RAM** บอร์ดกลับเป็น DFU mode ทุกครั้งที่ไฟหลุดหรือ run ถูกตัดกลางคัน — ต้อง flash ทุก session (แอปทำให้อัตโนมัติตอนติ๊ก ENABLE)
3. **tmux session ชื่อ `ITS3` กับบอร์ด 6 ตัวเป็นทรัพยากรร่วมกับ KCMH-Tricker** — **ห้ามรัน acquisition สองแอปพร้อมกัน**

   `session_state()` แยก session ที่**กำลังทำงานจริง** (producer ครบ 6) ออกจาก**ซากที่ค้าง** (run จบหรือ crash แล้วแต่ tmux ยังอยู่) — ซากจะถูกเก็บกวาดอัตโนมัติแล้ว launch ต่อ ส่วน session ที่ทำงานอยู่จริงจะไม่แตะ

   เดิมเช็คแค่ "มี session ไหม" ซึ่งทำให้**ซากที่ตายแล้วบล็อกทุก run ถัดไปเงียบๆ** — แอปยังเขียนไฟล์ครบ ดูปกติทุกอย่าง แต่ไม่มี `.raw` เลย และรู้ตัวตอนมาวิเคราะห์ (เสีย run 59 วินาทีไปหนึ่งครั้ง)

### ค่าที่ตั้งใน `config.py`

`ALPIDE_NUM`, `ALPIDE_EVENTS`, `ALPIDE_STROBE`, `ALPIDE_ITHR`, `TRIGGER_HZ`, `TRIGGER_DUTY`, `KCMH_TRICKER_DIR`, `EUDAQ_DIR`, `EUDAQ_LIB`, `ITS3_POLL_MS`, `CUT_QUEUE_MAX`, `CUT_JPEG_QUALITY`, `CUT_MAX_PER_SESSION` — อยู่ในไฟล์ไม่ใช่ UI เพราะเปลี่ยนตาม campaign ไม่ใช่ตาม session (มีแค่ความถี่ trigger ที่โผล่มาใน UI เพราะต้องปรับหน้างาน)

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
| `E1` / `E0` | Enable relay เปิด/ปิด (ส่งจาก `_on_enable_toggle()` ตอนติ๊ก/ปลดติ๊ก ENABLE และส่งซ้ำเป็น heartbeat) |
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
- `app.py` → `_tick_heartbeat()` ส่ง `E1` ทุก ~360 ms ขณะ ENABLE ติ๊กอยู่
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

> **หมายเหตุเรื่อง Enable**: ตั้งแต่การแก้ไขล่าสุด Enable ไม่ใช่ software jumper ที่ค้าง HIGH ตลอดอีกต่อไป แต่ toggle ตามคำสั่ง `E1`/`E0` ที่ส่งมาจาก `_on_enable_toggle()` ใน `app.py` (ติ๊ก ENABLE = Enable ON, ปลดติ๊ก = Enable OFF) เพื่อให้มี human authorization step ก่อน Enable จะขึ้น เป็น checkbox จริงที่มาจาก Enable ของ KCMH-Tricker ตรงๆ แล้ว ไม่ใช่แค่เทียบเท่ากันเหมือนก่อน แทนที่จะ assert ทันทีที่ Arduino มีไฟ — ตอน boot (`setup()`) RELAY_A เริ่มที่ `LOW` เสมอ (fail-safe: ต้องมีคนติ๊ก ENABLE ก่อนเท่านั้นถึงจะ Enable ได้)

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
