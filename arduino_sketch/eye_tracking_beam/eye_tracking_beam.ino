// Eye Tracking Beam Control
// DB9 interlock: Relay A (pin 4) shorts DB9 pins 4-6 together when Enabled, so
//                pin 6 (Enable) tracks pin 4 (+12V Ref), matching the KCMH/TSS
//                truth table for a standalone connection with no FPGA sharing it.
//                Starts OFF (fail-safe) — stays off until the host explicitly
//                sends E1, mirroring KCMH-Tricker's Enable checkbox instead of
//                asserting the moment the board has power.
//                Relay B (pin 6) switches DB9 pin 1 between pin 3 (GND, OFF) and
//                the pin 4/6 group (ON).
// Commands: B1\n = Beam ON,   B0\n = Beam OFF
//           E1\n = Enable ON, E0\n = Enable OFF

#define RELAY_A 4
#define RELAY_B 6

String input = "";

void setup() {
  Serial.begin(9600);
  pinMode(RELAY_A, OUTPUT);
  pinMode(RELAY_B, OUTPUT);
  digitalWrite(RELAY_A, LOW);   // Enable OFF until host sends E1
  digitalWrite(RELAY_B, LOW);
  input.reserve(8);
  delay(100);
  Serial.println("READY");
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      input.trim();
      if (input == "B1") {
        digitalWrite(RELAY_B, HIGH);
      } else if (input == "B0") {
        digitalWrite(RELAY_B, LOW);
      } else if (input == "E1") {
        digitalWrite(RELAY_A, HIGH);
      } else if (input == "E0") {
        digitalWrite(RELAY_A, LOW);
      }
      input = "";
    } else {
      input += c;
    }
  }
}
