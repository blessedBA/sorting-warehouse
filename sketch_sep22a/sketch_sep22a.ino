// =============================
// Настройки пинов
// =============================
// Ось X
#define X_STEP_PIN 13
#define X_DIR_PIN  12
#define X_EN_PIN   11
// Ось Y
#define Y_STEP_PIN 2
#define Y_DIR_PIN  3
#define Y_EN_PIN   4

#define SERVO_PIN 5

// Реле конвейера
#define RELAY_PIN 6

// Уровни реле:
// Если модуль реле активен HIGH — оставьте так.
// Если активен LOW — поменяйте местами:
// #define RELAY_ON  LOW
// #define RELAY_OFF HIGH
#define RELAY_ON  HIGH
#define RELAY_OFF LOW

// =============================
// Настройки программного ШИМ для реле конвейера
// =============================
// Период ШИМ (мс)
unsigned long CONVEYOR_PERIOD_MS = 300;
// Скважность в процентах (0..100): сколько времени внутри периода
// реле должно быть включено (HIGH).
int CONVEYOR_DUTY_PERCENT = 1;

// =============================
// Настройки движения
// =============================
// Временно! Нужно откалибровать под вашу механику
const float X_STEPS_PER_MM = 80.0;
const float Y_STEPS_PER_MM = 80.0;
// Интервал между шагами
const unsigned long STEP_INTERVAL_US = 1000;
// Длительность STEP-импульса
const unsigned long STEP_PULSE_US = 5;

// =============================
// Переменные положения
// =============================
long positionX = 0;
long positionY = 0;
long targetX = 0;
long targetY = 0;

// Время последнего шага
unsigned long lastStepX = 0;
unsigned long lastStepY = 0;

// Флаг выполнения движения
bool movementInProgress = false;

// Состояние конвейера
bool conveyorState = false;

// Переменные для программного ШИМ реле
unsigned long conveyorCycleStart = 0;

#include <Servo.h>
Servo myServo;

// =============================
// Управление конвейером
// =============================
void setConveyor(bool on) {
  conveyorState = on;
  if (on) {
    // Стартуем цикл ШИМ с текущего момента
    conveyorCycleStart = millis();
  } else {
    // При выключении гарантированно снимаем сигнал
    digitalWrite(RELAY_PIN, RELAY_OFF);
  }
}

// =============================
// Обновление программного ШИМ реле
// =============================
void updateConveyor() {
  if (!conveyorState) return;

  unsigned long now = millis();
  unsigned long elapsed = now - conveyorCycleStart;

  // Перезапуск цикла, если период истёк
  if (elapsed >= CONVEYOR_PERIOD_MS) {
    conveyorCycleStart += CONVEYOR_PERIOD_MS;
    elapsed = now - conveyorCycleStart;
  }

  // Время, в течение которого реле должно быть включено
  unsigned long onTime = (CONVEYOR_PERIOD_MS * (unsigned long)CONVEYOR_DUTY_PERCENT) / 100;

  if (elapsed < onTime) {
    digitalWrite(RELAY_PIN, RELAY_ON);
  } else {
    digitalWrite(RELAY_PIN, RELAY_OFF);
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

  pinMode(RELAY_PIN, OUTPUT);
  setConveyor(false);

  // A4988 активируется LOW
  digitalWrite(X_EN_PIN, LOW);
  digitalWrite(Y_EN_PIN, LOW);

  digitalWrite(X_STEP_PIN, LOW);
  digitalWrite(Y_STEP_PIN, LOW);

  Serial.begin(115200);

  myServo.attach(SERVO_PIN);
  myServo.write(90); // Начальное положение 90 градусов

  Serial.println("READY");
}

// =============================
// Выполнение одного шага оси X
// =============================
void makeStepX(int direction) {
  digitalWrite(X_DIR_PIN, direction > 0 ? LOW : HIGH);
  digitalWrite(X_STEP_PIN, HIGH);
  delayMicroseconds(STEP_PULSE_US);
  digitalWrite(X_STEP_PIN, LOW);
  positionX += direction;
}

// =============================
// Выполнение одного шага оси Y
// =============================
void makeStepY(int direction) {
  digitalWrite(Y_DIR_PIN, direction > 0 ? LOW : HIGH);
  digitalWrite(Y_STEP_PIN, HIGH);
  delayMicroseconds(STEP_PULSE_US);
  digitalWrite(Y_STEP_PIN, LOW);
  positionY += direction;
}

// =============================
// Обновление движения оси X
// =============================
void updateX() {
  if (positionX == targetX) return;

  unsigned long currentMicros = micros();
  if (currentMicros - lastStepX < STEP_INTERVAL_US) return;

  lastStepX = currentMicros;
  if (targetX > positionX) makeStepX(1);
  else makeStepX(-1);
}

// =============================
// Обновление движения оси Y
// =============================
void updateY() {
  if (positionY == targetY) return;

  unsigned long currentMicros = micros();
  if (currentMicros - lastStepY < STEP_INTERVAL_US) return;

  lastStepY = currentMicros;
  if (targetY > positionY) makeStepY(1);
  else makeStepY(-1);
}

// =============================
// Проверка окончания движения
// =============================
void checkMovementFinished() {
  bool movementFinished = (positionX == targetX && positionY == targetY);
  if (movementInProgress && movementFinished) {
    movementInProgress = false;
    Serial.println("DONE");
  }
}

// =============================
// Обработка команд из Python
// =============================
void processCommand(String command) {
  command.trim();
  if (command.length() == 0) return;

  // =============================
  // GOTO X Y
  // =============================
  if (command.startsWith("GOTO")) {
    String args = command.substring(4);
    args.trim();
    int separator = args.indexOf(' ');
    if (separator == -1) { Serial.println("ERROR"); return; }

    String xString = args.substring(0, separator);
    String yString = args.substring(separator + 1);
    xString.trim();
    yString.trim();

    float x = xString.toFloat();
    float y = yString.toFloat();

    targetX = lround(x * X_STEPS_PER_MM);
    targetY = lround(y * Y_STEPS_PER_MM);
    movementInProgress = true;
    Serial.println("OK");
  }
  // =============================
  // MOVE X/Y
  // =============================
  else if (command.startsWith("MOVE")) {
    String args = command.substring(4);
    args.trim();
    if (args.length() < 2) { Serial.println("ERROR"); return; }

    char axis = args.charAt(0);
    String distanceString = args.substring(1);
    distanceString.trim();
    float distance = distanceString.toFloat();

    if (axis == 'X' || axis == 'x') {
      targetX = positionX + lround(distance * X_STEPS_PER_MM);
    } else if (axis == 'Y' || axis == 'y') {
      targetY = positionY + lround(distance * Y_STEPS_PER_MM);
    } else {
      Serial.println("ERROR");
      return;
    }
    movementInProgress = true;
    Serial.println("OK");
  }
  // =============================
  // SERVO <angle>
  // =============================
  else if (command.startsWith("SERVO")) {
    String args = command.substring(5);
    args.trim();
    if (args.length() == 0) {
      Serial.println("ERROR");
      return;
    }
    int angle = args.toInt();
    angle = constrain(angle, 0, 180);

    myServo.write(angle);
    Serial.print("SERVO ");
    Serial.println(angle);
  }
  // =============================
  // CONVEYOR ON / CONVEYOR OFF / CONVEYOR?
  // Также поддерживаются: CONV ON/OFF/?, RELAY ON/OFF/?
  // =============================
  else if (command.startsWith("CONVEYOR") || command.startsWith("CONV") || command.startsWith("RELAY")) {
    String args;

    if (command.startsWith("CONVEYOR")) {
      args = command.substring(8);   // "CONVEYOR" = 8 символов
    } else if (command.startsWith("CONV")) {
      args = command.substring(4);   // "CONV" = 4 символа
    } else {
      args = command.substring(5);   // "RELAY" = 5 символов
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
      Serial.println(conveyorState ? "ON" : "OFF");
    } else {
      Serial.println("ERROR");
    }
  }
  // =============================
  // ZERO
  // =============================
  else if (command == "ZERO") {
    positionX = 0;
    positionY = 0;
    targetX = 0;
    targetY = 0;
    movementInProgress = false;
    Serial.println("ZEROED");
  }
  // =============================
  // POS?
  // =============================
  else if (command == "POS?") {
    Serial.print("POS ");
    Serial.print(positionX / X_STEPS_PER_MM, 2);
    Serial.print(" ");
    Serial.println(positionY / Y_STEPS_PER_MM, 2);
  }
  // =============================
  // STOP
  // =============================
  else if (command == "STOP") {
    targetX = positionX;
    targetY = positionY;
    movementInProgress = false;
    Serial.println("STOPPED");
  }
  // =============================
  // Непрерывное движение
  // =============================
  else if (command == "X+") { targetX = positionX + 1000000L; movementInProgress = true; Serial.println("OK"); }
  else if (command == "X-") { targetX = positionX - 1000000L; movementInProgress = true; Serial.println("OK"); }
  else if (command == "Y+") { targetY = positionY + 1000000L; movementInProgress = true; Serial.println("OK"); }
  else if (command == "Y-") { targetY = positionY - 1000000L; movementInProgress = true; Serial.println("OK"); }
  else {
    Serial.println("UNKNOWN");
  }
}

// =============================
// Основной цикл
// =============================
void loop() {
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    processCommand(command);
  }

  updateX();
  updateY();
  updateConveyor();
  checkMovementFinished();
}