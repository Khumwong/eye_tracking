# Next session

## สถานะปัจจุบันของโค้ด (อัปเดตล่าสุด 2026-08-10) — ยังไม่ commit เลยตั้งแต่เริ่ม redesign UI

### 1. Flow READY / START / STOP
- ปุ่ม **READY** (กดแล้วเป็น "UNREADY") — เช็คแค่ Arduino connected + กล้อง reachable เท่านั้น ไม่เกี่ยวกับการเปิดกล้อง
- **Preview กล้อง auto-start** ทันทีที่เลือก Input Source = Camera (ไม่ต้องกดอะไรเลย) — ปรับ Eye Selection/Threshold/Target Position ได้ก่อนกด READY ด้วยซ้ำ
- `CaptureThread.armed` คุมว่า Arduino จะได้รับ B1 (beam ON) ได้ไหม — `False` ระหว่าง preview เสมอ ไม่ว่า detection จะ trigger หรือไม่ก็ตาม
- **START TRACKING** กดได้เสมอ (ไม่ disable) ถ้ายังไม่ READY จะ popup เตือนจริง ๆ — ถ้า preview รันอยู่แล้ว (โหมดกล้อง) แค่ arm ของเดิม ไม่เปิดกล้องซ้ำ
- **STOP** โหมดกล้อง = แค่ disarm (ภาพไม่ดับ, view/zoom state ที่ตั้งไว้ยังอยู่) / โหมด Video = ปิดเต็มรูปแบบเหมือนเดิม
- Sidebar แบ่ง 3 คอลัมน์: ซ้าย = SYSTEM STATUS (pin บนเสมอ) + SETTING (scroll ได้) + DEBUG / ขวา = FLOW (Target Position, Beam status, Ready/Start/Stop/Pause, Recording — ปักหมุดไม่ scroll) / กลาง = camera feed

### 2. ภาพกล้อง 3 มุมมอง — "จอหลัก" สลับได้ด้วยการคลิก inset (ล่าสุด, แทนที่ดีไซน์ zoom-swap แบบเดิมทั้งหมด)

**มุมมองทั้ง 3**: `wide` (ภาพเต็มหน้า) / `zoom_color` (ซูมตา 3 เท่า สี) / `zoom_gray` (ซูมตา 3 เท่า ขาวดำ — cross-check อิสระ)

**กติกา** (ตัดสินใจร่วมกับ user แล้ว หลังจากดีไซน์เดิมที่ผูก "ตั้ง target = สลับจอ" ทำให้งง):
- จอหลัก (ใหญ่) = 1 ใน 3 มุมมอง ตาม `params['main_view']` ('wide' ตอนเริ่ม) / อีก 2 มุมมองที่เหลือโชว์เป็นกรอบเล็ก (inset) มุมล่างขวา ซ้อนกัน
- **คลิกที่จอหลัก** = ปรับ/ตั้ง target position เท่านั้น **ไม่สลับจอ** (ไม่ว่าจอหลักตอนนั้นจะเป็นมุมไหนก็ตาม)
- **คลิกที่กรอบเล็ก (inset)** = เลื่อนกรอบนั้นขึ้นมาเป็นจอหลักแทน (สลับ 2 ทาง ใช้ได้กับทั้ง 3 มุมมอง) — ไม่ปรับ target
- ปุ่ม **"Reset to center" ถูกเอาออกทั้งปุ่ม** ตามที่ user ขอ (ดูเกินความจำเป็น)
- กราฟ strip chart มุมล่างซ้าย **โชว์เสมอไม่ว่าจอหลักจะเป็นมุมไหน** (เดิมมันวาดก่อนครอปซูมเลยหลุดกรอบตอนซูม — ย้ายมาวาดทีหลังสุดบนภาพที่ compose เสร็จแล้วแทน)

**Implementation** (`capture.py`):
- `_zoom_crop_bbox()` — กรอบ crop รอบตา (ยึดจาก `_last_good_iris` ซึ่ง persist ข้ามเฟรมกระพริบตา) ขนาด fw/3 × fh/3 สัดส่วนเดียวกับเฟรมเต็ม เลื่อนกรอบไม่ให้หลุดขอบแทนการหด กันภาพเบี้ยว
- `_render_zoom(src, ..., marker)` — ครอป+ขยายภาพกลับ (INTER_CUBIC คมกว่า LINEAR เดิม) วาด crosshair/threshold-circle/จุด detected ใน zoom space, ใช้ได้ทั้งกับภาพสีและภาพขาวดำ (ส่ง `src` ต่างกัน)
- `_render_inset()` — ย่อมุมมองใดก็ได้ให้เป็น thumbnail เล็กมีป้ายชื่อ
- `_compose_display(frame, gray_bgr, center, trigger)` — ประกอบภาพสุดท้าย: เลือกจอหลักตาม `params['main_view']`, วางกรอบเล็ก 2 อันที่เหลือ, วาดกราฟทับสุดท้าย, คืน `(frame, view_meta)` โดย `view_meta` มี `crop`/`scale` (ถ้าจอหลักเป็นซูม, ให้ UI แปลงพิกัดคลิกกลับ) + `insets` (list ของ `{'view':.., 'rect':..}` ให้ UI เช็คว่าคลิกโดน inset ไหน)

**Implementation** (`app.py`):
- `_p['main_view']` แทนที่ `zoom_mode` เดิมทั้งหมด, `_last_view_meta` cache จาก metrics ทุกเฟรม
- `_on_feed_click()` เขียนใหม่: เช็ค insets ก่อน (คลิกโดน → สลับ `main_view`, return) ไม่โดนก็แปลงพิกัด (ถ้าจอหลักเป็นซูม ผ่าน crop+scale) แล้วตั้ง target อย่างเดียว
- ลบ `_pan_reset`/`_reset_button` ออกทั้งหมด (ปุ่มไม่มีแล้ว)

### 3. Grayscale cross-check — คำนวณ detection คู่ขนาน (สี + ขาวดำ) แล้วโชว์เทียบกัน

**โจทย์**: user มี Raspberry Pi 5 + Camera Module 3 อยากได้กล้องตัวที่สองไว้ "ยืนยันว่าตาขยับไปจริงเท่าที่วัดได้" (แนวคิด redundancy — เซนเซอร์อิสระ 2 ตัวต้องเห็นตรงกันถึงจะเชื่อ, เทียบกับที่ PSI ในเปเปอร์ใช้กล้อง+กระจกทำ stereo เพื่อความแม่นยำ ซึ่งเป็นคนละเป้าหมายกัน) — **ยังไม่ได้ต่อกล้องจริง** แต่ระหว่างนี้ทำเวอร์ชันเบา ๆ ไปพลาง ๆ: รัน FaceMesh ซ้ำอีกชุดบนภาพขาวดำของ**กล้องตัวเดียวกัน** (ไม่ใช่ hardware อิสระจริง แต่เป็น cross-check ของ algorithm ว่า sensitivity กับสีแค่ไหน) — user รับทราบข้อจำกัดนี้แล้ว โอเคให้ทำแบบนี้ไปก่อน

**Implementation** (`capture.py`):
- FaceMesh instance ที่ 2 (`face_mesh_gray`) แยกจากตัวหลักเด็ดขาด
- **Throttle**: รัน detection ขาวดำแค่ทุก ๆ `_GRAY_EVERY_N = 3` เฟรม (ลดโหลด CPU ตามที่ user เลือก เพราะรัน FaceMesh 2 รอบ/เฟรมทุกเฟรมจะกินพลังบน Raspberry Pi เยอะ) — แปลงภาพเป็นขาวดำทุกเฟรม (ถูก, ไม่กระทบ) แต่ detection (แพง) throttled
- `_detect_gray_iris()` — detection แบบง่าย (facemesh ธรรมดา ไม่ใช่ pupil-refine) แยกอิสระ ไม่แตะ `trigger`/`threshold`/Arduino เลยแม้แต่นิดเดียว เขียนผลลงตัวแปรชุด `_gray` แยกทั้งหมด (`_last_iris_px_gray`, `_last_good_iris_gray` ฯลฯ) — มี blink-persistence เหมือนตัวสี
- ผลลัพธ์ขาวดำถูกส่งผ่าน metrics (`iris_px_gray`, `deviation_mm_gray`) ไปโชว์คู่กับค่าสีใน SYSTEM STATUS ทุกแถว (Iris size, Precision, Deviation) เป็นบรรทัดเล็กกว่าด้านล่างค่าหลัก ("↳ B/W")

**⚠️ ยังไม่ได้ตัดสินใจ/ทำ**: ต่อกล้องที่สองจริง (Pi Camera Module 3 ผ่าน `picamera2` เพราะ `cv2.VideoCapture(0)` ธรรมดาไม่เสถียรกับกล้อง CSI) และยังไม่ได้ตัดสินใจว่าจะเอาผลต่างของ 2 ค่าไป **gate การยิง beam จริง** หรือแค่โชว์ให้ operator ดูเทียบเองเหมือนตอนนี้

### 4. ทดสอบ
- Compile ผ่านทั้งคู่ทุกครั้งที่แก้
- Synthetic test ครอบคลุม (ไม่แตะกล้องจริงเลยตลอด session): round-trip พิกัดคลิกซูม (แม่นยำ sub-pixel), blink-hold (ไม่กระตุกสลับโหมด), เฟรม/กล้องเล็กเกินไป (ไม่ crash), fallback เมื่อไม่มี anchor, **verify ว่า `zoom_gray` view เป็นภาพขาวดำจริง (R≈G≈B) ต่างจาก `zoom_color`**, `_detect_gray_iris` no-face branch
- **ยังไม่เคยทดสอบบนกล้อง/หน้าจริงเลยหลังการ redesign ล่าสุดนี้** (session ก่อนหน้าเคยลองแล้วเจอบั๊กกระพริบตา+ภาพแตก ซึ่งแก้ไปแล้ว แต่การ redesign เป็น 3-view + grayscale cross-check รอบนี้ยังไม่เคยรันจริงเลย)

## งานที่ต้องทำต่อ session หน้า

1. **ทดสอบบนเครื่องจริงแบบเต็ม flow** (สำคัญสุด เพราะยังไม่เคยรันจริงเลยหลัง redesign รอบนี้):
   - เปิดแอป → เห็น wide view เลยไหม, ปรับ Eye Selection/Threshold ได้ก่อนกด READY ไหม
   - คลิกที่ภาพหลัก → ตั้ง target ไหม (ไม่ควรสลับจอ)
   - คลิกที่กรอบเล็ก (มีสองอัน ซ้อนกันมุมล่างขวา) → สลับจอหลักถูกอันไหม, label ถูกไหม (FULL VIEW / EYE ZOOM / EYE ZOOM B/W)
   - คลิกที่จอหลักตอนเป็นซูม (สีหรือขาวดำ) → target ขยับแม่นไหม
   - เลขคู่ (สี vs B/W) ใน SYSTEM STATUS ใกล้เคียงกันไหม เฟรมเรตตกไหมจากการรัน FaceMesh 2 รอบ (ลอง throttle `_GRAY_EVERY_N` เพิ่ม/ลดถ้าจำเป็น)
   - กราฟมุมล่างซ้ายโชว์ตลอดไม่ว่าจอหลักจะเป็นมุมไหนไหม
   - กระพริบตา → จอนิ่งไม่กระตุกไหม
   - กด READY → START (arm) → STOP (ภาพไม่ดับ) → START อีกรอบ / สลับ Camera↔Video tab
2. **ตัดสินใจเรื่องกล้องตัวที่สองจริง (Pi Camera Module 3)** — ยัง pending: จะต่อจริงด้วย `picamera2` ไหม, และถ้าต่อแล้วจะเอาผลต่างไป gate beam จริงหรือแค่โชว์เทียบ
3. **Commit** ทุกอย่าง — ยังไม่เคย commit เลยตั้งแต่เริ่ม redesign UI (หลาย session แล้ว)
4. **ทดสอบ end-to-end flow เต็มรูปแบบ** กับกล้องจับตาสด ๆ ดู relay จริงตอน arm (ค้างมาหลาย session แล้ว ยังไม่แก้)
