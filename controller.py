"""Консоль управления складом через общий API оборудования."""

import argparse
import serial

from hardware import Hardware
from main import addSortingArguments, createWarehouseConfig, shutdownHardware
from sorter_log import SorterLog
from sorting_session import SortingSession, showSortingWeight


COM_PORT = "/dev/cu.usbserial-11220"
BAUDRATE = 115200


def showResponse(responses: list[str]) -> None:
    for response in responses:
        print("Arduino:", response)


def showWeight(hardware: Hardware, continuous: bool = False, *, fast: bool = False) -> None:
    """Вывести массу; в непрерывном режиме Ctrl+C возвращает к командам."""
    if continuous:
        print("Непрерывное взвешивание. Ctrl+C — вернуться к командам.", flush=True)
    if fast:
        print("Быстрый вес: среднее 2 отсчётов без выдержки; показания могут колебаться.", flush=True)
    try:
        while True:
            print(f"Масса: {hardware.get_weight(fast=fast):.3f} г", flush=True)
            if not continuous:
                return
    except KeyboardInterrupt:
        if not continuous:
            raise
        # Отменить незавершённое измерение и прочитать ответы до STOPPED.
        hardware.stop()
        print("\nНепрерывное взвешивание остановлено.")


def showHelp() -> None:
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
    print("  stop          - остановить сортировку; вне неё — головку и измерение")
    print("  conveyor on   - включить конвейер")
    print("  conveyor off  - выключить конвейер")
    print("  tare          - тарировать пустую тару")
    print("  calibrate G   - калибровка грузом известной массы G граммов")
    print("  scale K       - задать сохранённый коэффициент отсчётов/г")
    print("  weight        - узнать массу в граммах")
    print("  weight continuous - непрерывный вывод массы до Ctrl+C")
    print("  weight fast [continuous] - быстрый вес: 2 отсчёта без выдержки")
    print("  main start    - запустить сортировку в фоне (main — то же самое)")
    print("  main stop     - остановить сортировку и вернуться к ручному управлению")
    print("  main status   - состояние сортировки и последняя ошибка")
    print("  main log      - открыть журнал сортировщика в отдельном окне терминала")
    print("  help          - список команд")
    print("  quit          - остановить сортировку и выйти")
    print("При сортировке доступны main, weight [fast] [continuous], help, stop, quit/exit.")
    print("weight с fast и без него показывает быстрые измерения сортировщика; "
          "новые запросы к весам не отправляются.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=COM_PORT)
    parser.add_argument("--sorter-log", default="output/sorter.log",
                        help="Файл журнала фонового сортировщика (по умолчанию output/sorter.log)")
    parser.add_argument("--no-log-window", action="store_true",
                        help="Не открывать окно журнала автоматически; сообщения сохраняются в файл")
    addSortingArguments(parser, require_weight_scale=False)
    args = parser.parse_args()
    try:
        config = createWarehouseConfig(args)
    except ValueError as error:
        parser.error(str(error))
    try:
        sorter_log = SorterLog(args.sorter_log)
    except OSError as error:
        parser.error(f"Не удалось создать журнал сортировщика: {error}")
    try:
        hardware = Hardware(args.port, BAUDRATE)
    except (serial.SerialException, RuntimeError, TimeoutError) as error:
        print("Ошибка подключения:")
        print(error)
        return

    session = SortingSession(hardware, config, camera_index=args.camera, roi=args.roi,
                             weight_scale=args.weight_scale, message_sink=sorter_log.write)
    log_window_opened = False
    showHelp()
    print(f"Журнал сортировщика: {sorter_log.path}")

    try:
        showResponse(hardware.zero())
        if args.weight_scale is not None:
            hardware.setWeightScale(args.weight_scale)
        while True:
            command = input("> ").strip()
            if not command:
                continue

            parts = command.split()
            cmd = parts[0].lower()

            if cmd == "help":
                showHelp()
                continue
            if cmd == "main":
                action = parts[1].lower() if len(parts) == 2 else "start"
                if len(parts) > 2 or action not in ("start", "stop", "status", "log"):
                    print("Использование: main [start|stop|status|log]")
                    continue
                try:
                    if action == "start":
                        session.start()
                        if not args.no_log_window and not log_window_opened:
                            log_window_opened = sorter_log.openWindow()
                    elif action == "stop":
                        session.stop()
                    elif action == "log":
                        log_window_opened = sorter_log.openWindow()
                    session.showStatus()
                except ValueError as error:
                    print(error)
                continue
            if session.isRunning() and cmd not in ("weight", "stop", "quit", "exit"):
                print("Команда недоступна во время сортировки. Доступны: "
                      "weight [fast] [continuous], main status, main log, main stop, help, stop, quit.")
                continue

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
                        session.weight_scale = factor
                        print(f"Коэффициент: {factor:.9g}; для main.py: --weight-scale={factor:.9g}")
                    elif cmd == "weight":
                        options = [part.lower() for part in parts[1:]]
                        if len(options) != len(set(options)) or set(options) - {"fast", "continuous"}:
                            raise ValueError("Использование: weight [fast] [continuous]")
                        fast = "fast" in options
                        continuous = "continuous" in options
                        if session.isRunning():
                            showSortingWeight(session, continuous=continuous)
                        else:
                            showWeight(hardware, continuous=continuous, fast=fast)
                    elif len(parts) != 1:
                        raise ValueError(f"Использование: {cmd}")
                    elif cmd == "tare":
                        showResponse(hardware.tareWeight())
                except ValueError as error:
                    print(error)
                except (RuntimeError, TimeoutError) as error:
                    print(error)
                    if session.isRunning():
                        session.stop()
                    else:
                        showResponse(hardware.stop())

            elif cmd == "zero":
                showResponse(hardware.zero())

            elif cmd == "conveyor":
                if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
                    print("Использование: conveyor on|off")
                    continue
                showResponse(hardware.set_conveyor(parts[1].lower() == "on"))

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
                if session.isRunning():
                    session.stop()
                else:
                    showResponse(hardware.stop())

            # -------------------------
            # QUIT
            # -------------------------
            elif cmd in ["quit", "exit"]:
                break

            else:
                print("Неизвестная команда")

    except (KeyboardInterrupt, EOFError):
        print("\nВыход из контроллера.")
    finally:
        session.stop()
        shutdownHardware(hardware)
        print("Соединение закрыто.")


if __name__ == "__main__":
    main()
