// Bench tool: find which Arduino pin drives the J1 coax connector.
//
// วิธีใช้:
//   1. ถอด DB9 ออกจาก TSS ก่อน (สคริปต์นี้ไม่แตะ D4/D6 แต่กันไว้ก่อน)
//   2. upload sketch นี้ลงบอร์ด
//   3. เปิด Serial Monitor 9600 baud
//   4. ตั้งมัลติมิเตอร์โหมด "วัดแรงดัน DC" (V⎓)
//        สายดำ -> GND ของ Arduino
//        สายแดง -> แกนกลางของ J1  (จิ้มค้างไว้จุดเดียว ไม่ต้องขยับ)
//   5. ดู Serial Monitor คู่กับมิเตอร์ — ขาไหนที่ทำให้ J1 ขึ้น ~5V คือขานั้น
//
// เสร็จแล้วอย่าลืม upload eye_tracking_beam.ino กลับเข้าไป

// ข้าม D4 (Relay A / Enable) กับ D6 (Relay B / Beam) เพื่อไม่ให้ relay ทำงาน
const int PINS[] = {2, 3, 5, 7, 8, 9, 10, 11, 12, 13,
                    14, 15, 16, 17, 18, 19, 20, 21, 22, 23};
const int N = sizeof(PINS) / sizeof(PINS[0]);

const unsigned long HOLD_MS = 3000;   // ค้างแต่ละขา 3 วินาที ให้อ่านมิเตอร์ทัน

void setup() {
  Serial.begin(9600);
  for (int i = 0; i < N; i++) {
    pinMode(PINS[i], OUTPUT);
    digitalWrite(PINS[i], LOW);
  }
  delay(500);
  Serial.println();
  Serial.println("=== find_lemo_pin ===");
  Serial.println("จิ้มมิเตอร์ (V DC) ที่แกนกลาง J1, สายดำที่ GND");
  Serial.println("ขาไหนทำให้ J1 ขึ้น ~5V = ขานั้นคือตัวขับ J1");
  Serial.println();
}

void loop() {
  for (int i = 0; i < N; i++) {
    int p = PINS[i];
    Serial.print("  D");
    Serial.print(p);
    Serial.println("  HIGH  <-- ดูมิเตอร์ตอนนี้");
    digitalWrite(p, HIGH);
    delay(HOLD_MS);
    digitalWrite(p, LOW);
    delay(300);
  }
  Serial.println();
  Serial.println("--- ครบรอบแล้ว วนใหม่ ---");
  Serial.println();
}
