// =====  Arduino  =====
// รีเลย์ A: ใช้ต่อหน้าสัมผัส 4-6 (จะเปิดค้างไว้ตลอด)
const int RELAY_A_PIN = 4;

// รีเลย์ B: ใช้สลับว่า 1 จะไปจับกับ 3 หรือจับกับ (4-6)
const int RELAY_B_PIN = 6;

// ถ้าโมดูลรีเลย์ของคุณเป็น active LOW ให้แก้สองบรรทัดนี้
// (ตอนนี้ตั้งเป็น active HIGH ไว้ก่อน ถ้าต่อจริงแล้วทำงานกลับข้าง → สลับได้)
const int RELAY_ON  = HIGH;  // ถ้าเป็น active LOW ให้เปลี่ยนเป็น LOW
const int RELAY_OFF = LOW;   // ถ้าเป็น active LOW ให้เปลี่ยนเป็น HIGH

String inputBuffer = "";

void setup() {
  Serial.begin(9600);

  pinMode(RELAY_A_PIN, OUTPUT);
  pinMode(RELAY_B_PIN, OUTPUT);

  // ===== ขา 4 เปิดค้างไว้ตลอดเวลา =====
  digitalWrite(RELAY_A_PIN, RELAY_ON);   // รีเลย์ A ทำงานตลอด (4-6 CONNECT)

  // เริ่มต้นให้ B0 ก่อน (1 จับกับ 3)
  digitalWrite(RELAY_B_PIN, RELAY_OFF);

  Serial.println("Arduino ready. RelayA (4-6) is ALWAYS ON. Waiting for B0/B1 ...");
}

void loop() {
  // อ่านจาก Serial ทีละตัวจนเจอ '\n'
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\n' || c == '\r') {
      if (inputBuffer.length() > 0) {
        handleCommand(inputBuffer);
        inputBuffer = "";
      }
    } else {
      inputBuffer += c;
    }
  }
}

void handleCommand(const String &cmd) {
  if (cmd.length() < 2) return;

  char channel = cmd.charAt(0);  // คาดหวังว่าเป็น 'B'
  char value   = cmd.charAt(1);  // '0' หรือ '1'

  // เราใช้แค่ B0 / B1 ตอนนี้
  if (channel == 'B') {
    if (value == '1') {
      // B1: ให้ RelayB ทำให้ขา 1 ไปจับกับ (4-6)
      digitalWrite(RELAY_B_PIN, RELAY_ON);
      Serial.println("B1 -> RelayB: 1->(4-6)  / Beam_ON");
    } else if (value == '0') {
      // B0: ให้ RelayB ทำให้ขา 1 ไปจับกับ 3
      digitalWrite(RELAY_B_PIN, RELAY_OFF);
      Serial.println("B0 -> RelayB: 1->3  / Beam_OFF");
    }
  }

  // ถ้าเผื่ออนาคตอยากใช้ A ควบคุม ก็สามารถเพิ่มเงื่อนไข channel=='A' ขึ้นมาได้
}