"""Консоль управления складом через общий API оборудования."""

import argparse
import serial

from hardware import Hardware


COM_PORT = "/dev/cu.usbserial-3120"
BAUDRATE = 115200


def showResponse(responses: list[str]) -> None:
    for response in responses:
        print("Arduino:", response)


def showWeight(hardware: Hardware, continuous: bool = False) -> None:
    """Вывести массу; в непрерывном режиме Ctrl+C возвращает к командам."""
    if continuous:
        print("Непрерывное взвешивание. Ctrl+C — вернуться к командам.", flush=True)
    try:
        while True:
            print(f"Масса: {hardware.get_weight():.3f} г", flush=True)
            if not continuous:
                return
    except KeyboardInterrupt:
        if not continuous:
            raise
        # Отменить незавершённый WEIGHT? и прочитать ответы до STOPPED.
        hardware.stop()
        print("\nНепрерывное взвешивание остановлено.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=COM_PORT)
    args = parser.parse_args()
    try:
        hardware = Hardware(args.port, BAUDRATE)
    except (serial.SerialException, RuntimeError, TimeoutError) as error:
        print("Ошибка подключения:")
        print(error)
        return

    print()
    print("==============================")
    print("   2-AXIS + SERVO CONTROL     ")
    print("==============================")
    print()
    print("Команды: ")
    print("  zero          - сброс координат в 0")
    print("  goto X Y      - абсолютное перемещение")
    print("  move dX dY    - относительное перемещение")
    print("  pos           - узнать текущие координаты")
    print("  servo <angle> - установить угол сервопривода (0-180)")
    print("  stop          - аварийная остановка")
    print("  tare          - тарировать пустую тару")
    print("  calibrate G   - калибровка грузом известной массы G граммов")
    print("  scale K       - задать сохранённый коэффициент отсчётов/г")
    print("  weight        - узнать массу в граммах")
    print("  weight continuous - непрерывный вывод массы до Ctrl+C")
    print("  quit          - выход")
    print()

    try:
        showResponse(hardware.zero())
        while True:
            command = input("> ").strip()
            if not command:
                continue

            parts = command.split()
            cmd = parts[0].lower()

            # -------------------------
            # ZERO
            # -------------------------
            if cmd in ("tare", "calibrate", "scale", "weight"):
                try:
                    if cmd in ("calibrate", "scale"):
                        if len(parts) != 2:
                            raise ValueError(f"Использование: {cmd} <число>")
                        method = hardware.calibrateWeight if cmd == "calibrate" else hardware.setWeightScale
                        factor = method(float(parts[1]))
                        print(f"Коэффициент: {factor:.9g}; для main.py: --weight-scale={factor:.9g}")
                    elif cmd == "weight":
                        if len(parts) > 2 or (len(parts) == 2 and parts[1].lower() != "continuous"):
                            raise ValueError("Использование: weight [continuous]")
                        showWeight(hardware, continuous=len(parts) == 2)
                    elif len(parts) != 1:
                        raise ValueError(f"Использование: {cmd}")
                    elif cmd == "tare":
                        showResponse(hardware.tareWeight())
                except ValueError as error:
                    print(error)
                except (RuntimeError, TimeoutError) as error:
                    print(error)
                    showResponse(hardware.stop())

            elif cmd == "zero":
                showResponse(hardware.zero())

            # -------------------------
            # GOTO
            # -------------------------
            elif cmd == "goto":
                if len(parts) != 3:
                    print("Использование: goto X Y")
                    continue
                try:
                    x = float(parts[1])
                    y = float(parts[2])
                    showResponse(hardware.goto(x, y))
                except ValueError:
                    print("Координаты должны быть числами")

            # -------------------------
            # MOVE
            # -------------------------
            elif cmd == "move":
                if len(parts) != 3:
                    print("Использование: move dX dY")
                    continue
                try:
                    dx = float(parts[1])
                    dy = float(parts[2])
                    showResponse(hardware.move(dx, dy))
                except ValueError:
                    print("Смещения должны быть числами")

            # -------------------------
            # SERVO
            # -------------------------
            elif cmd == "servo":
                if len(parts) != 2:
                    print("Использование: servo <угол>")
                    continue
                try:
                    # Принимаем как float, чтобы не падало на "90.5", но отправляем int
                    angle = int(float(parts[1]))
                    showResponse(hardware.setServo(angle))
                except ValueError:
                    print("Угол должен быть числом")

            # -------------------------
            # POS
            # -------------------------
            elif cmd == "pos":
                showResponse(hardware.getPosition())

            # -------------------------
            # STOP
            # -------------------------
            elif cmd == "stop":
                showResponse(hardware.stop())

            # -------------------------
            # QUIT
            # -------------------------
            elif cmd in ["quit", "exit"]:
                break

            else:
                print("Неизвестная команда")

    finally:
        hardware.disconnect()
        print("Соединение закрыто.")


if __name__ == "__main__":
    main()
