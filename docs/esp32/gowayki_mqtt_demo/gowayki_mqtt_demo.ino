/*
 * GoWayki IoT — demo MQTT para ESP32 (PubSubClient)
 *  - Potenciómetro en GPIO 34 -> publica en gowayki/<usuario>/potenciometro (0..4095)
 *  - LED en GPIO 2            <- se suscribe a gowayki/<usuario>/led ("ON"/"OFF")
 * Librería: PubSubClient (Nick O'Leary). Completa los placeholders. No subas credenciales reales al repo.
 */
#include <WiFi.h>
#include <PubSubClient.h>

const char* WIFI_SSID     = "TU_WIFI";
const char* WIFI_PASSWORD = "TU_PASSWORD_WIFI";

const char* MQTT_HOST     = "localhost";       // host que te da el admin
const uint16_t MQTT_PORT  = 1883;
const char* MQTT_USER     = "TU_USUARIO";      // ej. "ana"
const char* MQTT_PASSWORD = "TU_PASSWORD_MQTT";

const int POT_PIN = 34;
const int LED_PIN = 2;
const unsigned long PUBLISH_MS = 500;

WiFiClient wifi;
PubSubClient mqtt(wifi);
String topicPot, topicLed;
unsigned long lastPublish = 0;
int lastValue = -1;

void onMessage(char* topic, byte* payload, unsigned int length) {
  String msg;
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
  msg.trim();
  msg.toUpperCase();
  if (String(topic) == topicLed) {
    bool on = (msg == "ON" || msg == "1" || msg == "TRUE");
    digitalWrite(LED_PIN, on ? HIGH : LOW);
    Serial.printf("LED -> %s\n", on ? "ON" : "OFF");
  }
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nIP: %s\n", WiFi.localIP().toString().c_str());
}

void connectMqtt() {
  while (!mqtt.connected()) {
    String clientId = String("esp32-") + MQTT_USER + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);
    Serial.print("MQTT...");
    if (mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASSWORD)) {
      Serial.println("ok");
      mqtt.subscribe(topicLed.c_str());
    } else {
      // rc=5: usuario/contraseña incorrectos
      Serial.printf("fallo rc=%d, reintento en 3 s\n", mqtt.state());
      delay(3000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  analogReadResolution(12);  // 0..4095
  topicPot = String("gowayki/") + MQTT_USER + "/potenciometro";
  topicLed = String("gowayki/") + MQTT_USER + "/led";
  connectWifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();

  unsigned long now = millis();
  if (now - lastPublish >= PUBLISH_MS) {
    lastPublish = now;
    int value = analogRead(POT_PIN);
    if (lastValue < 0 || abs(value - lastValue) > 20) {  // evita ruido
      lastValue = value;
      mqtt.publish(topicPot.c_str(), String(value).c_str());
      Serial.printf("pot=%d\n", value);
    }
  }
}
