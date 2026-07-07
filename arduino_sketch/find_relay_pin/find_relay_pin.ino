#define PIN_A 4
#define PIN_B 5

void setup() {
  Serial.begin(9600);
  pinMode(PIN_A, OUTPUT);
  pinMode(PIN_B, OUTPUT);
  digitalWrite(PIN_A, LOW);
  digitalWrite(PIN_B, LOW);
}

void loop() {
  Serial.println("PIN 4 ON");
  digitalWrite(PIN_A, HIGH);
  delay(1000);
  digitalWrite(PIN_A, LOW);
  delay(500);

  Serial.println("PIN 5 ON");
  digitalWrite(PIN_B, HIGH);
  delay(1000);
  digitalWrite(PIN_B, LOW);
  delay(500);
}
