"""Автоматический цикл склада. Протокол платы изолирован в драйвере оборудования."""

import argparse
from contextlib import ExitStack
from dataclasses import dataclass, field
import math
import sys
import random


@dataclass
class WarehouseConfig:
    # TODO: координаты в единицах прошивки, после калибровки системы координат.
    loading_position: tuple[float, float] | None = (9.0, 17.0)
    zero: tuple[float, float] | None = (0.0, 0.0)
    cell_positions: dict[int, tuple[float, float] | None] = field(
        default_factory=lambda: {
            1: (4, 10),
            2: (9, 10),
            3: (14, 10),
            4: (4, 5),
            5: (9, 5),
            6: (14, 5),
            7: (4, 0),
            8: (9, 0),
            9: (14, 0),
        }
    )
    # Время от распознавания до падения в тару, подбирается на установке.
    feed_duration: float = 0.3

    def validate(self) -> None:
        if not math.isfinite(self.feed_duration) or self.feed_duration <= 0:
            raise ValueError("Задайте положительную конечную feed_duration в WarehouseConfig")
        if set(self.cell_positions) != set(range(1, 10)):
            raise ValueError("Нужны координаты всех девяти ячеек с номерами 1–9")
        positions = {"loading_position": self.loading_position, **self.cell_positions}
        for name, position in positions.items():
            if position is None or len(position) != 2 or not all(math.isfinite(value) for value in position):
                raise ValueError(f"Задайте две конечные координаты для {name}")


def selectCell(color: str) -> int:
    """Выбрать случайную ячейку 1–9; цвет пока не влияет на выбор."""
    return random.randint(1, 9)


def createHardware(backend: str, port: str, baudrate: int):
    """Единственная точка выбора платы; цикл использует общий набор методов."""
    if backend == "arduino":
        from hardware import Hardware
        return Hardware(com_port=port, baudrate=baudrate)
    if backend == "raspberry-pi":
        # TODO: вернуть драйвер Raspberry Pi с теми же методами, что у Hardware.
        # GPIO или связь с отдельной платой реализуются в этом драйвере.
        raise NotImplementedError("Драйвер Raspberry Pi вместо Arduino пока не реализован")
    raise ValueError(f"Неизвестный тип оборудования: {backend}")


def waitForObject(hardware, camera) -> dict:
    """Проверить остановленную ленту, при необходимости подать предмет до камеры."""
    camera.resetDetection()
    # None в первых кадрах может означать, что предмет ещё подтверждается.
    for _ in range(camera.confirmation_frames):
        result = camera.detectObject()
        if result is not None:
            return result
    try:
        hardware.conveyor_on()
        while True:
            result = camera.detectObject()
            if result is not None:
                return result
    finally:
        hardware.conveyor_off()


def sortOneObject(hardware, camera, config: WarehouseConfig) -> int:
    """Выполнить цикл с закрытым затвором и остановленной лентой на входе и выходе."""
    # 1. Подвести уже закрытую тару под конвейер.
    hardware.goto(*config.loading_position)
    hardware.waitUntilIdle()

    # 2–3. Найти предмет и использовать цвет из подтвердившего его кадра.
    result = waitForObject(hardware, camera)
    # ambiguous описывает смесь цветов, а не отсутствие предмета.
    # При случайном выборе ячейки это не мешает подаче и выгрузке.
    cell = selectCell(result["color"])
    if type(cell) is not int or cell not in config.cell_positions:
        raise ValueError("selectCell должен вернуть целый номер ячейки от 1 до 9")
    color_note = "; смешанные цвета" if result["ambiguous"] else ""
    print(f"Распознан цвет: {result['color']} ({result['dominant_share']:.1%} пикселей{color_note}); "
          f"ячейка: {cell}", flush=True)

    # 4. Прокрутить ленту заданное время и остановить до перемещения головки.
    hardware.feedOneObject(config.feed_duration)

    # 5. Отвезти предмет, открыть затвор, дождаться выгрузки и закрыть.
    hardware.goto(*config.cell_positions[cell])
    hardware.waitUntilIdle()
    hardware.open()
    hardware.waitUntilIdle()
    hardware.waitForUnload()
    hardware.close()
    hardware.waitUntilIdle()
    return cell


def shutdownHardware(hardware) -> None:
    """Попытаться остановить оба механизма и освободить подключение независимо от ошибок."""
    actions = (
        # STOP также восстанавливает синхронизацию протокола после ошибки.
        ("остановить головку", hardware.stop),
        ("остановить конвейер", hardware.conveyor_off),
        ("отключить оборудование", hardware.disconnect),
    )
    for description, action in actions:
        try:
            action()
        except Exception as error:
            print(f"Не удалось {description}: {error}", file=sys.stderr)


def runWarehouse(hardware, camera, config: WarehouseConfig) -> None:
    """Повторять цикл; при выходе остановить и отключить оборудование.

    Подходит для любого драйвера с методами Hardware. Перед запуском механика
    должна быть откалибрована: ZERO в текущем протоколе лишь сбрасывает координаты.
    Камеру освобождает вызывающий код.
    """
    try:
        config.validate()
        # Подготовка только перед первым циклом; дальше затвор закрывается при выгрузке.
        hardware.conveyor_off()
        hardware.close()
        hardware.waitUntilIdle()
        while True:
            cell = sortOneObject(hardware, camera, config)
            print(f"Объект помещён в ячейку {cell}", flush=True)
    finally:
        shutdownHardware(hardware)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("arduino", "raspberry-pi"), default="arduino")
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--feed-duration", type=float, default=WarehouseConfig.feed_duration,
                        help="Время прокрутки после распознавания, в секундах (по умолчанию 0.3)")
    parser.add_argument("--roi", type=int, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"),
                        help="Заменить область по умолчанию: X Y WIDTH HEIGHT в пикселях фактического кадра")
    args = parser.parse_args()
    try:
        config = WarehouseConfig(feed_duration=args.feed_duration)
        config.validate()
        from camera import Camera
        with ExitStack() as resources:
            camera = Camera(camera_index=args.camera, roi=args.roi)
            resources.callback(camera.disconnect)
            hardware = createHardware(args.backend, args.port, args.baudrate)
            # runWarehouse останавливает и отключает оборудование даже при ошибке.
            runWarehouse(hardware, camera, config)
    except KeyboardInterrupt:
        print("\nСортировка остановлена.")
    except (NotImplementedError, RuntimeError, ValueError, OSError) as error:
        parser.exit(1, f"Ошибка: {error}\n")


if __name__ == "__main__":
    main()
