"""Автоматический цикл склада. Протокол платы изолирован в драйвере оборудования."""

import argparse
from contextlib import ExitStack
from dataclasses import dataclass, field
import math
import sys
import time


@dataclass
class WarehouseConfig:
    # TODO: координаты в единицах прошивки, после калибровки системы координат.
    loading_position: tuple[float, float] | None = (9.0, 17.0)
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
    conveyor_speed: float | None = 10

    def validate(self) -> None:
        if self.conveyor_speed is None or not math.isfinite(self.conveyor_speed) or self.conveyor_speed <= 0:
            raise ValueError("Задайте положительную conveyor_speed в WarehouseConfig")
        if set(self.cell_positions) != set(range(1, 10)):
            raise ValueError("Нужны координаты всех девяти ячеек с номерами 1–9")
        positions = {"loading_position": self.loading_position, **self.cell_positions}
        for name, position in positions.items():
            if position is None or len(position) != 2 or not all(math.isfinite(value) for value in position):
                raise ValueError(f"Задайте две конечные координаты для {name}")


def selectCell(color: str, weight: float) -> int:
    """TODO: вернуть номер ячейки 1–9 по трём цветам и трём интервалам масс.

    weight — граммы. Здесь нужно захардкодить цвета и границы интервалов,
    определить принадлежность граничных значений и отказ для неизвестного цвета
    или массы вне диапазона. Произвольная ячейка по умолчанию не выбирается.
    """
    raise NotImplementedError("Правила сортировки по цвету и массе пока не заданы")


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


def sortOneObject(hardware, camera, config: WarehouseConfig) -> int:
    """Один цикл. config проверяется до запуска; ошибки прерывают сортировку."""
    # 1. Дождаться объекта в зоне измерения, затем остановить ленту.
    hardware.set_speed(config.conveyor_speed)
    while True:
        result = camera.detectObject()
        if result is not None and camera.isObjectReady(result):
            break
        time.sleep(0.05)
    hardware.set_speed(0)

    # 2. Цвет и масса относятся к объекту, оставшемуся в зоне после остановки.
    result = camera.detectObject()
    if result is None or not camera.isObjectReady(result):
        raise RuntimeError("После остановки ленты объект отсутствует в нужном положении")
    if result["ambiguous"]:
        raise RuntimeError("Цвет объекта неоднозначен; сортировка остановлена")
    weight = hardware.get_weight()
    if not math.isfinite(weight) or weight < 0:
        raise ValueError("Тензодатчик должен вернуть конечную неотрицательную массу в граммах")

    # 3. Выбрать одну из девяти ячеек.
    cell = selectCell(result["color"], weight)
    if type(cell) is not int or cell not in config.cell_positions:
        raise ValueError("selectCell должен вернуть целый номер ячейки от 1 до 9")

    # 4. Подвести тару с закрытым затвором под конвейер.
    hardware.close()
    hardware.waitUntilIdle()
    hardware.goto(*config.loading_position)
    hardware.waitUntilIdle()

    # 5. Дождаться падения только измеренного объекта и остановки ленты.
    hardware.feedOneObject()

    # 6. Переместить объект к выбранной ячейке.
    hardware.goto(*config.cell_positions[cell])
    hardware.waitUntilIdle()

    # 7–8. Открыть затвор, дождаться выгрузки, закрыть затвор.
    hardware.open()
    hardware.waitUntilIdle()
    hardware.waitForUnload()
    hardware.close()
    hardware.waitUntilIdle()
    return cell


def shutdownHardware(hardware) -> None:
    """Попытаться остановить оба механизма и освободить подключение независимо от ошибок."""
    actions = (
        ("остановить конвейер", lambda: hardware.set_speed(0)),
        ("остановить головку", hardware.stop),
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
        hardware.set_speed(0)
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
    parser.add_argument("--roi", type=int, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    args = parser.parse_args()
    try:
        config = WarehouseConfig()
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
