"""API оборудования склада: Arduino/ESP32 с управлением по serial."""

import math
import time

import serial


class Hardware:
    """Перемещение головки по XY, управление серво, конвейером и весами HX711.

    Протокол обновлённого скетча: ASCII, строки с '\\n', 115200 бод, координаты
    в миллиметрах. OK подтверждает команду, DONE — окончание выдачи шагов XY.
    CONVEYOR ON / CONVEYOR OFF подтверждают состояние конвейера.
    Класс рассчитан на последовательные вызовы из одного потока.
    """

    def __init__(self, com_port: str = "COM3", baudrate: int = 115200,
                 timeout: float = 1, startup_timeout: float = 10,
                 movement_timeout: float = 60, servo_settle_time: float | None = 0.5,
                 weight_timeout: float = 5):
        for value in (timeout, startup_timeout, movement_timeout, weight_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Таймауты должны быть конечными положительными числами")
        if servo_settle_time is not None and (not math.isfinite(servo_settle_time) or servo_settle_time <= 0):
            raise ValueError("Выдержка сервопривода должна быть конечным положительным числом")
        if baudrate != 115200:
            raise ValueError("В скетче задана скорость 115200 бод")
        self.com_port = com_port
        self.baudrate = baudrate
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.movement_timeout = movement_timeout
        self.servo_settle_time = servo_settle_time
        self.weight_timeout = weight_timeout
        self.arduino = None
        self._response_buffer = bytearray()
        self._synchronized = False
        self._movement_pending = False
        self._servo_pending = False
        self._servo_command_time = 0.0
        self._conveyor_state = False
        # Событие принадлежит владельцу оборудования; serial по-прежнему читает один поток.
        self.cancel_event = None
        self.init()

    def init(self) -> None:
        """Открыть порт и дождаться READY; после ошибки синхронизироваться через STOP."""
        if self.arduino is not None and self.arduino.is_open:
            if not self._synchronized:
                self.stop()
            return
        self._response_buffer.clear()
        self._synchronized = False
        self._movement_pending = False
        self._servo_pending = False
        self._conveyor_state = False
        self.arduino = serial.Serial(
            self.com_port, self.baudrate, timeout=0, write_timeout=self.timeout
        )
        try:
            self._waitForResponse("READY", self.startup_timeout)
            self._synchronized = True
        except BaseException:
            self.arduino.close()
            raise

    def send(self, command: str) -> None:
        """Низкоуровневая отправка; для ожидания команды используйте методы ниже."""
        self._checkConnection()
        if not command or "\n" in command or "\r" in command:
            raise ValueError("Нужна одна непустая команда без переводов строки")
        data = (command + "\n").encode("ascii")
        if self.arduino.write(data) != len(data):
            self._synchronized = False
            raise RuntimeError("Команда передана в serial-порт не полностью")

    def _checkConnection(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise InterruptedError("Запрошена остановка сортировки")
        if self.arduino is None or not self.arduino.is_open:
            raise RuntimeError("Arduino не подключена. Вызовите init().")

    def _popResponse(self) -> str | None:
        """Извлечь только полную строку; фрагменты остаются до следующего чтения."""
        if b"\n" not in self._response_buffer:
            return None
        line, _, tail = self._response_buffer.partition(b"\n")
        self._response_buffer = bytearray(tail)
        return line.decode("ascii").strip()

    def _readLine(self, deadline: float) -> str:
        while True:
            self._checkConnection()
            response = self._popResponse()
            if response:
                return response
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Истекло время ожидания ответа Arduino")
            data = self.arduino.read(self.arduino.in_waiting or 1)
            if data:
                self._response_buffer.extend(data)
            else:
                time.sleep(min(0.01, remaining))

    def _handleResponse(self, response: str) -> None:
        if response == "OK":
            self._movement_pending = True
        elif response in ("DONE", "STOPPED", "ZEROED"):
            self._movement_pending = False
        elif response.startswith("SERVO "):
            angle = int(response[6:])
            if not 0 <= angle <= 180:
                raise RuntimeError(f"Некорректный ответ сервопривода: {response}")
            self._servo_pending = True
            self._servo_command_time = time.monotonic()
        elif response.startswith("POS "):
            coordinates = response.split()[1:]
            if len(coordinates) != 2 or not all(math.isfinite(float(value)) for value in coordinates):
                raise RuntimeError(f"Некорректные координаты Arduino: {response}")
        elif response in ("CONVEYOR ON", "CONVEYOR OFF"):
            self._conveyor_state = (response == "CONVEYOR ON")
        elif response.startswith(("WEIGHT ", "SCALE ")):
            self._parseMeasurement(response)
        elif response == "TARED":
            pass
        elif response in ("ERROR", "UNKNOWN") or response.startswith("ERROR "):
            raise RuntimeError(f"Arduino отклонила команду: {response}")
        elif response == "READY":
            raise RuntimeError("Arduino перезапустилась; координаты сброшены")
        else:
            raise RuntimeError(f"Неизвестный ответ Arduino: {response}")

    def _waitForResponse(self, expected, timeout: float, recovering: bool = False) -> list[str]:
        deadline = time.monotonic() + timeout
        responses = []
        try:
            while True:
                response = self._readLine(deadline)
                if response == "READY" and expected == "READY":
                    return [response]
                if recovering and (response in ("ERROR", "UNKNOWN") or response.startswith("ERROR ")):
                    responses.append(response)
                    continue
                self._handleResponse(response)
                responses.append(response)
                if isinstance(expected, (list, tuple)):
                    if response in expected:
                        return responses
                elif response == expected or \
                     (expected in ("POS", "CONVEYOR", "WEIGHT", "SCALE")
                      and response.startswith(expected + " ")):
                    return responses
                # DONE может прийти от предыдущего движения до ответа на новую команду.
                # STOPPED служит границей синхронизации после запоздалых ответов.
                if response != "DONE" and not recovering:
                    raise RuntimeError(f"Ожидался ответ {expected}, получен {response}")
        except Exception:
            self._synchronized = False
            raise

    def _request(self, command: str, expected, timeout: float | None = None) -> list[str]:
        self._checkConnection()
        if not self._synchronized and command != "STOP":
            raise RuntimeError("Протокол рассинхронизирован. Вызовите stop() или переподключитесь.")
        try:
            self.send(command)
            return self._waitForResponse(
                expected, self.timeout if timeout is None else timeout, recovering=command == "STOP"
            )
        except Exception:
            self._synchronized = False
            raise

    def readResponse(self) -> list[str]:
        """Прочитать доступные полные строки, сохранив состояние OK/DONE."""
        self._checkConnection()
        responses = []
        try:
            self._response_buffer.extend(self.arduino.read(self.arduino.in_waiting))
            while (response := self._popResponse()) is not None:
                if response:
                    self._handleResponse(response)
                    responses.append(response)
        except Exception:
            self._synchronized = False
            raise
        return responses

    def zero(self) -> list[str]:
        """Остановить головку и сбросить координаты в ноль."""
        responses = self.stop()
        return responses + self._request("ZERO", "ZEROED")

    def goto(self, x: float, y: float) -> list[str]:
        """Задать абсолютную цель в миллиметрах; дождаться OK, но не DONE."""
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("Координаты должны быть конечными числами")
        return self._request(f"GOTO {x} {y}", "OK")

    def move(self, dx: float, dy: float) -> list[str]:
        """Задать смещения в миллиметрах от положения на момент каждой команды MOVE."""
        if not math.isfinite(dx) or not math.isfinite(dy):
            raise ValueError("Смещения должны быть конечными числами")
        responses = []
        if dx != 0:
            responses.extend(self._request(f"MOVE X {dx}", "OK"))
        if dy != 0:
            responses.extend(self._request(f"MOVE Y {dy}", "OK"))
        return responses

    def jog(self, axis: str, direction: int) -> list[str]:
        """Команды X+/X-/Y+/Y-: цель на 1000000 шагов от текущей позиции, до STOP."""
        axis = axis.upper()
        if axis not in ("X", "Y") or direction not in (-1, 1):
            raise ValueError("Ось должна быть X или Y, направление — -1 или 1")
        sign = "+" if direction == 1 else "-"
        return self._request(f"{axis}{sign}", "OK")

    def getPosition(self) -> list[str]:
        """Получить POS x y: программные координаты в мм, округлённые до сотых."""
        return self._request("POS?", "POS")

    def stop(self) -> list[str]:
        """Остановить XY и отменить измерение; серво и конвейер не останавливаются."""
        responses = self._request("STOP", "STOPPED")
        self._synchronized = True
        return responses

    def setServo(self, angle: int) -> list[str]:
        """Задать целый угол 0–180°; SERVO angle подтверждает лишь принятие команды."""
        if type(angle) is not int or not 0 <= angle <= 180:
            raise ValueError("Угол сервопривода должен быть целым числом от 0 до 180")
        return self._request(f"SERVO {angle}", f"SERVO {angle}")

    def open(self) -> None:
        """Открыть затвор: установить заданный угол сервопривода 127 градусов."""
        self.setServo(127)

    def close(self) -> None:
        """Закрыть затвор: установить заданный угол сервопривода 37 градусов."""
        self.setServo(37)

    def conveyor_on(self) -> list[str]:
        """Включить конвейер."""
        return self._request("CONVEYOR ON", "CONVEYOR ON")

    def conveyor_off(self) -> list[str]:
        """Выключить конвейер."""
        return self._request("CONVEYOR OFF", "CONVEYOR OFF")

    def get_conveyor_state(self) -> bool:
        """Запросить состояние конвейера. Возвращает True, если включён, иначе False."""
        responses = self._request("CONVEYOR?", "CONVEYOR")
        for r in responses:
            if r.startswith("CONVEYOR "):
                return r == "CONVEYOR ON"
        raise RuntimeError("В ответе нет состояния конвейера")

    def set_conveyor(self, on: bool) -> list[str]:
        """Установить состояние конвейера: on=True — включить, on=False — выключить."""
        if on:
            return self.conveyor_on()
        else:
            return self.conveyor_off()

    def get_weight(self, *, fast: bool = False) -> float:
        """Вернуть усреднённую массу в граммах; HX711 должен быть откалиброван.

        Обычно прошивка выдерживает 0,5 с и усредняет 10 новых отсчётов.
        fast=True: 2 новых отсчёта без выдержки; показания менее сглажены.
        Быстрый режим требует прошивки с командой WEIGHT_FAST?.
        Головка должна быть остановлена; лента может работать.
        Отрицательный вес не обрезается.
        """
        return self._requestMeasurement("WEIGHT_FAST?" if fast else "WEIGHT?", "WEIGHT")

    @staticmethod
    def _parseMeasurement(response: str) -> float:
        parts = response.split()
        try:
            if len(parts) != 2:
                raise ValueError
            value = float(parts[1])
            if not math.isfinite(value):
                raise ValueError
            if parts[0] == "SCALE" and not 1e-6 <= abs(value) <= 1e9:
                raise ValueError
        except ValueError as error:
            raise RuntimeError(f"Некорректное измерение Arduino: {response}") from error
        return value

    def _requestMeasurement(self, command: str, expected: str) -> float:
        responses = self._request(command, expected, timeout=self.weight_timeout)
        return self._parseMeasurement(responses[-1])

    def tareWeight(self) -> list[str]:
        """Повторно тарировать пустую неподвижную тару, сохранив коэффициент."""
        return self._request("TARE", "TARED", timeout=self.weight_timeout)

    def setWeightScale(self, scale: float) -> float:
        """Задать коэффициент HX711 в отсчётах/г; знак зависит от подключения."""
        if not math.isfinite(scale) or not 1e-6 <= abs(scale) <= 1e9:
            raise ValueError("Модуль коэффициента HX711 должен быть от 1e-6 до 1e9")
        return self._requestMeasurement(f"SCALE {scale:.9g}", "SCALE")

    def calibrateWeight(self, known_weight: float) -> float:
        """После тарирования положить эталон и получить коэффициент отсчётов/г."""
        if not math.isfinite(known_weight) or not 0 < known_weight <= 1e9:
            raise ValueError("Масса эталона должна быть положительной и не больше 1e9 г")
        return self._requestMeasurement(f"CALIBRATE {known_weight:.9g}", "SCALE")

    def waitForMovement(self) -> None:
        """Дождаться DONE по XY в пределах movement_timeout; датчиков позиции нет."""
        self._checkConnection()
        if not self._synchronized:
            raise RuntimeError("Нет синхронизации с Arduino. Вызовите stop().")
        self.readResponse()
        if self._movement_pending:
            self._waitForResponse("DONE", self.movement_timeout)

    def waitUntilIdle(self) -> None:
        """Дождаться DONE по XY и откалиброванной выдержки после команды сервоприводу.

        Прошивка не подтверждает физическое положение серво. Без заданного
        servo_settle_time ожидание после SERVO остаётся плейсхолдером.
        """
        self.waitForMovement()
        if self._servo_pending:
            if self.servo_settle_time is None:
                raise NotImplementedError("Задайте servo_settle_time: прошивка не сообщает о завершении движения серво")
            remaining = self._servo_command_time + self.servo_settle_time - time.monotonic()
            if remaining > 0:
                self.waitForDelay(remaining)
            self._servo_pending = False

    def waitForDelay(self, duration: float) -> None:
        """Выдержать паузу с возможностью остановки владельцем оборудования."""
        if self.cancel_event is None:
            time.sleep(duration)
        elif self.cancel_event.wait(duration):
            raise InterruptedError("Запрошена остановка сортировки")

    def feedOneObject(self, duration: float = 0.3) -> None:
        """Прокрутить ленту duration секунд и остановить, в том числе при прерывании.

        Выдержка отсчитывается после ответа CONVEYOR ON. Она подбирается
        на механике: датчик фактического падения в текущем протоколе отсутствует.
        """
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Время подачи должно быть конечным положительным числом")
        try:
            self.conveyor_on()
            self.waitForDelay(duration)
        finally:
            self.conveyor_off()

    def waitForUnload(self) -> None:
        """Выдержать полсекунды на выпадение предмета перед закрытием затвора."""
        self.waitForDelay(0.5)

    def disconnect(self) -> None:
        """Отправить STOP и освободить порт, даже если отправка не удалась."""
        if self.arduino is not None and self.arduino.is_open:
            try:
                self.stop()
            finally:
                self.arduino.close()
                self._synchronized = False
