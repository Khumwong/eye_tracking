// Eye Tracking Beam Control
// DB9 interlock: Relay A (pin 4) controls DB9 pins 4-6
//                Relay B (pin 6) switches DB9 pin 1 between pin 3 (OFF) and pins 4-6 (ON)
// Commands: B1\n = Beam ON, B0\n = Beam OFF

#define RELAY_A 4
#define RELAY_B 6

String input = "";

void setup() {
  Serial.begin(9600);
  pinMode(RELAY_A, OUTPUT);
  pinMode(RELAY_B, OUTPUT);
  digitalWrite(RELAY_A, LOW);
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
        digitalWrite(RELAY_A, HIGH);
        digitalWrite(RELAY_B, HIGH);
      } else if (input == "B0") {
        digitalWrite(RELAY_A, LOW);
        digitalWrite(RELAY_B, LOW);
      }
      input = "";
    } else {
      input += c;
    }
  }
}
