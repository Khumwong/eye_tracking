// Eye Tracking Beam Control
// DB9 interlock: Relay A (pin 4) shorts DB9 pins 4-6 together — held ON permanently
//                (acts as a software jumper; never toggled after setup) so pin 6
//                (Enable) always tracks pin 4 (+12V Ref), matching the KCMH/TSS
//                truth table for a standalone connection with no FPGA sharing it.
//                Relay B (pin 6) switches DB9 pin 1 between pin 3 (GND, OFF) and
//                the pin 4/6 group (ON).
// Commands: B1\n = Beam ON, B0\n = Beam OFF

#define RELAY_A 4
#define RELAY_B 6

String input = "";

void setup() {
  Serial.begin(9600);
  pinMode(RELAY_A, OUTPUT);
  pinMode(RELAY_B, OUTPUT);
  digitalWrite(RELAY_A, HIGH);  // permanently short DB9 pins 4-6 — never toggled again
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
      }
      input = "";
    } else {
      input += c;
    }
  }
}
