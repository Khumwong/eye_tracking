// Eye Tracking Beam Control
// Listens for B1\n (beam ON) and B0\n (beam OFF) over serial at 9600 baud.
// Change BEAM_PIN to match your wiring (relay, transistor, or LED).

#define BEAM_PIN 8

String input = "";

void setup() {
  Serial.begin(9600);
  pinMode(BEAM_PIN, OUTPUT);
  digitalWrite(BEAM_PIN, LOW);
  input.reserve(8);
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      input.trim();
      if (input == "B1") {
        digitalWrite(BEAM_PIN, HIGH);
      } else if (input == "B0") {
        digitalWrite(BEAM_PIN, LOW);
      }
      input = "";
    } else {
      input += c;
    }
  }
}
