"""Консоль управления складом через общий API оборудования."""

import serial

from hardware import Hardware


COM_PORT = "/dev/ttyUSB0"
BAUDRATE = 115200


def showResponse(responses: list[str]) -> None:
    for response in responses:
        print("Arduino:", response)


def main() -> None:
    try:
        hardware = Hardware(COM_PORT, BAUDRATE)
    except serial.SerialException as error:
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
            if cmd == "zero":
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
