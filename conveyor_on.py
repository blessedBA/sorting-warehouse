"""Включить конвейер до нажатия Enter или Ctrl+C."""

import argparse

from hardware import Hardware


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="Последовательный порт Arduino")
    args = parser.parse_args()

    hardware = Hardware(com_port=args.port)
    try:
        hardware.conveyor_on()
        input("Конвейер включён. Нажмите Enter для остановки.\n")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        try:
            hardware.conveyor_off()
        finally:
            hardware.disconnect()


if __name__ == "__main__":
    main()
