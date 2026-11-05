// =============================
// Настройки пинов
// =============================
// Ось X
#define X_STEP_PIN 13
#define X_DIR_PIN  12
#define X_EN_PIN   11
// Ось Y
#define Y_STEP_PIN 8
#define Y_DIR_PIN  2
#define Y_EN_PIN   4

#define SERVO_PIN 5

// Драйвер конвейера L298N mini
#define CONVEYOR_PWM_PIN 6   // ENA (ШИМ)
#define CONVEYOR_DIR_PIN 7   // IN1 (направление)

#define HX711_DATA_PIN A5   // DT / DOUT
#define HX711_CLOCK_PIN A4  // S/ CLK

// =============================
// Настройки скорости конвейера
// =============================
// Скважность в процентах (0..100)
int CONVEYOR_DUTY_PERCENT = 60;  // при необходимости увеличьте

// =============================
// Настройки движения
// =============================
const float X_STEPS_PER_MM = 80.0;
const float Y_STEPS_PER_MM = 80.0;
const unsigned long STEP_INTERVAL_US = 1000;
const unsigned long STEP_PULSE_US = 5;

// =============================
// Переменные положения
// =============================
long position_x = 0;
long position_y = 0;
long target_x = 0;
long target_y = 0;

unsigned long last_step_x = 0;
unsigned long last_step_y = 0;

bool movement_in_progress = false;
bool conveyor_state = false;

#include <Servo.h>
Servo my_servo;

// HX711: канал A, усиление 128. Внешняя библиотека не требуется.
// Вес = (среднее АЦП - тара) / коэффициент в отсчётах на грамм.
const unsigned long SCALE_TIMEOUT_MS = 4000;
const unsigned long SCALE_SETTLE_MS = 500;
const uint8_t SCALE_SAMPLES = 10;
const uint8_t SCALE_FAST_SAMPLES = 2;
enum ScaleOperation { ScaleIdle, ScaleStartup, ScaleTare, ScaleWeight, ScaleWeightFast, ScaleCalibrate };
ScaleOperation scale_operation = ScaleIdle;
unsigned long scale_started = 0;
long scale_sum = 0;
uint8_t scale_samples = 0;
bool scale_discard_first = true;
bool scale_tared = false;
float scale_offset = 0;
float scale_factor = 0;  // 0: калибровка ещё не задана.
float calibration_weight = 0;

// Вызывается только после DOUT=LOW, из setup/loop, вне обработчиков прерываний.
long readHx711() {
  uint32_t value = 0;
  // Прерывание Servo не должно растянуть HIGH SCK до режима power-down.
  for (uint8_t bit = 0; bit < 24; ++bit) {
    noInterrupts();
    digitalWrite(HX711_CLOCK_PIN, HIGH);
    delayMicroseconds(1);
    value = (value << 1) | digitalRead(HX711_DATA_PIN);
    digitalWrite(HX711_CLOCK_PIN, LOW);
    interrupts();
    delayMicroseconds(1);
  }
  // 25-й импульс выбирает канал A / gain 128 для следующего измерения.
  noInterrupts();
  digitalWrite(HX711_CLOCK_PIN, HIGH);
  delayMicroseconds(1);
  digitalWrite(HX711_CLOCK_PIN, LOW);
  interrupts();
  if (value & 0x800000UL) value |= 0xFF000000UL;
  return static_cast<int32_t>(value);
}

bool validScaleFactor(float value) {
  return isfinite(value) && fabs(value) >= 1e-6 && fabs(value) <= 1e9;
}

bool parseNumber(String text, float &value) {
  text.trim();
  if (text.length() == 0) return false;
  char *end;
  value = strtod(text.c_str(), &end);
  return *end == '\0' && isfinite(value);
}

void startScale(ScaleOperation operation) {
  scale_operation = operation;
  scale_started = millis();
  scale_sum = 0;
  scale_samples = 0;
  scale_discard_first = true;
  if (operation == ScaleTare || operation == ScaleStartup) scale_tared = false;
}

void failScale(const __FlashStringHelper *message) {
  scale_operation = ScaleIdle;
  Serial.println(message);
}

void updateScale() {
  if (scale_operation == ScaleIdle) return;
  unsigned long elapsed = millis() - scale_started;
  if (elapsed >= SCALE_TIMEOUT_MS) {
    failScale(F("ERROR HX711_TIMEOUT"));
    return;
  }
  if (digitalRead(HX711_DATA_PIN) != LOW) return;
  long sample = readHx711();
  // Удаляем старый отсчёт, затем непрерывно читаем во время успокоения.
  unsigned long settle_ms = scale_operation == ScaleWeightFast ? 0 : SCALE_SETTLE_MS;
  if (scale_discard_first || elapsed < settle_ms) {
    scale_discard_first = false;
    return;
  }
  if (sample == -8388608L || sample == 8388607L) {
    failScale(F("ERROR HX711_OVERLOAD"));
    return;
  }
  scale_sum += sample;  // 10 * 24 бита укладываются в signed long.
  uint8_t samples_required = scale_operation == ScaleWeightFast ? SCALE_FAST_SAMPLES : SCALE_SAMPLES;
  if (++scale_samples < samples_required) return;

  float average = static_cast<float>(scale_sum) / samples_required;
  ScaleOperation completed = scale_operation;
  scale_operation = ScaleIdle;
  if (completed == ScaleStartup || completed == ScaleTare) {
    scale_offset = average;
    scale_tared = true;
    Serial.println(completed == ScaleStartup ? F("READY") : F("TARED"));
  } else if (completed == ScaleCalibrate) {
    float factor = (average - scale_offset) / calibration_weight;
    if (!validScaleFactor(factor)) {
      Serial.println(F("ERROR HX711_CALIBRATION"));
      return;
    }
    scale_factor = factor;
    Serial.print(F("SCALE "));
    Serial.println(scale_factor, 6);
  } else {
    float weight = (average - scale_offset) / scale_factor;
    if (!isfinite(weight)) {
      Serial.println(F("ERROR HX711_WEIGHT"));
      return;
    }
    Serial.print(F("WEIGHT "));
    Serial.println(weight, 3);
  }
}

// =============================
// Управление конвейером
// =============================
void setConveyor(bool on) {
  conveyor_state = on;
  if (on) {
    // Включаем направление (IN1 = HIGH, IN2 = GND)
    digitalWrite(CONVEYOR_DIR_PIN, HIGH);
    // Преобразуем проценты в 0..255
    int pwm_value = map(CONVEYOR_DUTY_PERCENT, 0, 100, 0, 255);
    analogWrite(CONVEYOR_PWM_PIN, pwm_value);
  } else {
    // Полностью останавливаем
    analogWrite(CONVEYOR_PWM_PIN, 0);
    digitalWrite(CONVEYOR_DIR_PIN, LOW);
  }
}

// =============================
// Настройка
// =============================
void setup() {
  pinMode(X_STEP_PIN, OUTPUT);
  pinMode(X_DIR_PIN, OUTPUT);
  pinMode(X_EN_PIN, OUTPUT);
  pinMode(Y_STEP_PIN, OUTPUT);
  pinMode(Y_DIR_PIN, OUTPUT);
  pinMode(Y_EN_PIN, OUTPUT);

  pinMode(CONVEYOR_PWM_PIN, OUTPUT);
  pinMode(CONVEYOR_DIR_PIN, OUTPUT);
  setConveyor(false);

  // A4988 активируется LOW
  digitalWrite(X_EN_PIN, LOW);
  digitalWrite(Y_EN_PIN, LOW);

  digitalWrite(X_STEP_PIN, LOW);
  digitalWrite(Y_STEP_PIN, LOW);

  Serial.begin(115200);

  my_servo.attach(SERVO_PIN);
  my_servo.write(37);  // Закрытая пустая тара перед стартовым тарированием.

  pinMode(HX711_DATA_PIN, INPUT_PULLUP);
  pinMode(HX711_CLOCK_PIN, OUTPUT);
  // Сброс HX711 гарантирует канал A / gain 128 после перезапуска Arduino.
  digitalWrite(HX711_CLOCK_PIN, HIGH);
  delayMicroseconds(80);
  digitalWrite(HX711_CLOCK_PIN, LOW);
  startScale(ScaleStartup);  // READY отправляется только после успешного тарирования.
}

// =============================
// Выполнение одного шага оси X
// =============================
void makeStepX(int direction) {
  digitalWrite(X_DIR_PIN, direction > 0 ? LOW : HIGH);
  digitalWrite(X_STEP_PIN, HIGH);
  delayMicroseconds(STEP_PULSE_US);
  digitalWrite(X_STEP_PIN, LOW);
  position_x += direction;
}

// =============================
// Выполнение одного шага оси Y
// =============================
void makeStepY(int direction) {
  digitalWrite(Y_DIR_PIN, direction > 0 ? LOW : HIGH);
  digitalWrite(Y_STEP_PIN, HIGH);
  delayMicroseconds(STEP_PULSE_US);
  digitalWrite(Y_STEP_PIN, LOW);
  position_y += direction;
}

// =============================
// Обновление движения оси X
// =============================
void updateX() {
  if (position_x == target_x) return;

  unsigned long current_micros = micros();
  if (current_micros - last_step_x < STEP_INTERVAL_US) return;

  last_step_x = current_micros;
  if (target_x > position_x) makeStepX(1);
  else makeStepX(-1);
}

// =============================
// Обновление движения оси Y
// =============================
void updateY() {
  if (position_y == target_y) return;

  unsigned long current_micros = micros();
  if (current_micros - last_step_y < STEP_INTERVAL_US) return;

  last_step_y = current_micros;
  if (target_y > position_y) makeStepY(1);
  else makeStepY(-1);
}

// =============================
// Проверка окончания движения
// =============================
void checkMovementFinished() {
  bool movement_finished = (position_x == target_x && position_y == target_y);
  if (movement_in_progress && movement_finished) {
    movement_in_progress = false;
    Serial.println("DONE");
  }
}

// =============================
// Обработка команд из Python
// =============================
void processCommand(String command) {
  command.trim();
  if (command.length() == 0) return;

  if (scale_operation != ScaleIdle && command != "STOP" && command != "CONVEYOR OFF") {
    Serial.println(F("ERROR HX711_BUSY"));
    return;
  }
  bool weight_request = command == "WEIGHT?" || command == "WEIGHT_FAST?";
  if (command == "TARE" || weight_request ||
      command.startsWith("SCALE ") || command.startsWith("CALIBRATE ")) {
    // Чтение веса разрешено при работающей ленте для диагностики.
    if (movement_in_progress || (conveyor_state && !weight_request)) {
      Serial.println(F("ERROR HX711_MOVING"));
      return;
    }
    if (command == "TARE") {
      startScale(ScaleTare);
    } else if (command.startsWith("SCALE ")) {
      float factor;
      if (!parseNumber(command.substring(6), factor) || !validScaleFactor(factor)) {
        Serial.println(F("ERROR HX711_CALIBRATION"));
        return;
      }
      scale_factor = factor;
      Serial.print(F("SCALE "));
      Serial.println(scale_factor, 6);
    } else if (!scale_tared) {
      Serial.println(F("ERROR HX711_NOT_TARED"));
    } else if (weight_request) {
      if (!validScaleFactor(scale_factor)) {
        Serial.println(F("ERROR HX711_NOT_CALIBRATED"));
        return;
      }
      startScale(command == "WEIGHT_FAST?" ? ScaleWeightFast : ScaleWeight);
    } else {
      if (!parseNumber(command.substring(10), calibration_weight) ||
          calibration_weight <= 0 || calibration_weight > 1e9) {
        Serial.println(F("ERROR HX711_CALIBRATION"));
        return;
      }
      startScale(ScaleCalibrate);
    }
  }
  else if (command.startsWith("GOTO")) {
    String args = command.substring(4);
    args.trim();
    int separator = args.indexOf(' ');
    if (separator == -1) { Serial.println("ERROR"); return; }

    String x_string = args.substring(0, separator);
    String y_string = args.substring(separator + 1);
    x_string.trim();
    y_string.trim();

    float x = x_string.toFloat();
    float y = y_string.toFloat();

    target_x = lround(x * X_STEPS_PER_MM);
    target_y = lround(y * Y_STEPS_PER_MM);
    movement_in_progress = true;
    Serial.println("OK");
  }
  else if (command.startsWith("MOVE")) {
    String args = command.substring(4);
    args.trim();
    if (args.length() < 2) { Serial.println("ERROR"); return; }

    char axis = args.charAt(0);
    String distance_string = args.substring(1);
    distance_string.trim();
    float distance = distance_string.toFloat();

    if (axis == 'X' || axis == 'x') {
      target_x = position_x + lround(distance * X_STEPS_PER_MM);
    } else if (axis == 'Y' || axis == 'y') {
      target_y = position_y + lround(distance * Y_STEPS_PER_MM);
    } else {
      Serial.println("ERROR");
      return;
    }
    movement_in_progress = true;
    Serial.println("OK");
  }
  else if (command.startsWith("SERVO")) {
    String args = command.substring(5);
    args.trim();
    if (args.length() == 0) {
      Serial.println("ERROR");
      return;
    }
    int angle = args.toInt();
    angle = constrain(angle, 0, 180);

    my_servo.write(angle);
    Serial.print("SERVO ");
    Serial.println(angle);
  }
  else if (command.startsWith("CONVEYOR") || command.startsWith("CONV") || command.startsWith("RELAY")) {
    String args;

    if (command.startsWith("CONVEYOR")) {
      args = command.substring(8);
    } else if (command.startsWith("CONV")) {
      args = command.substring(4);
    } else {
      args = command.substring(5);
    }

    args.trim();
    args.toUpperCase();

    if (args == "ON") {
      setConveyor(true);
      Serial.println("CONVEYOR ON");
    } else if (args == "OFF") {
      setConveyor(false);
      Serial.println("CONVEYOR OFF");
    } else if (args == "?" || args.length() == 0) {
      Serial.print("CONVEYOR ");
      Serial.println(conveyor_state ? "ON" : "OFF");
    } else {
      Serial.println("ERROR");
    }
  }
  else if (command == "ZERO") {
    position_x = 0;
    position_y = 0;
    target_x = 0;
    target_y = 0;
    movement_in_progress = false;
    Serial.println("ZEROED");
  }
  else if (command == "POS?") {
    Serial.print("POS ");
    Serial.print(position_x / X_STEPS_PER_MM, 2);
    Serial.print(" ");
    Serial.println(position_y / Y_STEPS_PER_MM, 2);
  }
  else if (command == "STOP") {
    scale_operation = ScaleIdle;  // Отменить измерение: после STOPPED запоздалого WEIGHT нет.
    target_x = position_x;
    target_y = position_y;
    movement_in_progress = false;
    Serial.println("STOPPED");
  }
  else if (command == "X+") { target_x = position_x + 1000000L; movement_in_progress = true; Serial.println("OK"); }
  else if (command == "X-") { target_x = position_x - 1000000L; movement_in_progress = true; Serial.println("OK"); }
  else if (command == "Y+") { target_y = position_y + 1000000L; movement_in_progress = true; Serial.println("OK"); }
  else if (command == "Y-") { target_y = position_y - 1000000L; movement_in_progress = true; Serial.println("OK"); }
  else {
    Serial.println("UNKNOWN");
  }
}

// =============================
// Основной цикл
// =============================
void loop() {
  // Частичная строка не блокирует движение и опрос HX711.
  static String command;
  static bool command_overflow = false;
  while (Serial.available() > 0) {
    char symbol = Serial.read();
    if (symbol == '\n') {
      if (command_overflow) Serial.println("ERROR");
      else processCommand(command);
      command = "";
      command_overflow = false;
    } else if (symbol != '\r') {
      if (command.length() < 80) command += symbol;
      else command_overflow = true;
    }
  }

  updateX();
  updateY();
  checkMovementFinished();
  updateScale();
}
