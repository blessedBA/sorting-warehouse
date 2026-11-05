"""Автоматический цикл склада. Протокол платы изолирован в драйвере оборудования."""

import argparse
from bisect import bisect_right
from contextlib import ExitStack
from dataclasses import dataclass, field
import math
import random
from time import monotonic

from sorter_log import reportMessage


class NegativeWeightError(ValueError):
    """Отрицательная масса: пропустить предмет и продолжить сортировку."""


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
    # Время подачи после взвешивания до падения в тару, подбирается на установке.
    feed_duration: float = 1.4
    # Время сбора показаний при остановленной ленте; начатый запрос дочитывается.
    weight_duration: float = 3.0
    # Границы трёх рядов: <5 г, [5; 12) г, >=12 г.
    weight_limits: tuple[float, float] = (5.0, 12.0)
    min_weight: float = 0.0

    def validate(self) -> None:
        validateWeightLimits(self.weight_limits)
        if not math.isfinite(self.min_weight) or not 0 <= self.min_weight < self.weight_limits[0]:
            raise ValueError("min_weight должен быть неотрицательным и меньше первой границы веса")
        for name in ("feed_duration", "weight_duration"):
            duration = getattr(self, name)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError(f"Задайте положительную конечную {name} в WarehouseConfig")
        if set(self.cell_positions) != set(range(1, 10)):
            raise ValueError("Нужны координаты всех девяти ячеек с номерами 1–9")
        positions = {"loading_position": self.loading_position, **self.cell_positions}
        for name, position in positions.items():
            if position is None or len(position) != 2 or not all(math.isfinite(value) for value in position):
                raise ValueError(f"Задайте две конечные координаты для {name}")


def validateWeightLimits(weight_limits: tuple[float, ...]) -> None:
    if len(weight_limits) != 2 or any(not math.isfinite(value) or value <= 0 for value in weight_limits):
        raise ValueError("Задайте две положительные конечные границы веса для трёх рядов")
    if any(left >= right for left, right in zip(weight_limits, weight_limits[1:])):
        raise ValueError("Границы веса должны строго возрастать")


def selectCell(weight: float, weight_limits: tuple[float, ...] = WarehouseConfig.weight_limits) -> int:
    """Выбрать случайную ячейку весового ряда; граница относится к следующему ряду."""
    validateWeightLimits(weight_limits)
    if not math.isfinite(weight) or weight <= 0:
        raise ValueError("Масса предмета должна быть положительной и конечной")
    first_cell = bisect_right(weight_limits, weight) * 3 + 1
    return random.randint(first_cell, first_cell + 2)


def createHardware(backend: str, port: str, baudrate: int, weight_scale: float | None = None):
    """Единственная точка выбора платы; цикл использует общий набор методов."""
    if backend == "arduino":
        from hardware import Hardware
        hardware = Hardware(com_port=port, baudrate=baudrate)
        try:
            if weight_scale is not None:
                hardware.setWeightScale(weight_scale)
        except BaseException:
            shutdownHardware(hardware)
            raise
        return hardware
    if backend == "raspberry-pi":
        # TODO: вернуть драйвер Raspberry Pi с теми же методами, что у Hardware.
        # GPIO или связь с отдельной платой реализуются в этом драйвере.
        raise NotImplementedError("Драйвер Raspberry Pi вместо Arduino пока не реализован")
    raise ValueError(f"Неизвестный тип оборудования: {backend}")


def checkStopRequested(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise InterruptedError("Запрошена остановка сортировки")


def waitForObject(hardware, camera, *, skip_current: bool = False, stop_event=None) -> dict:
    """Найти предмет; при пропуске сначала дождаться освобождения области камеры."""
    camera.resetDetection()
    # None в первых кадрах может означать, что предмет ещё подтверждается.
    if not skip_current:
        for _ in range(camera.confirmation_frames):
            checkStopRequested(stop_event)
            result = camera.detectObject()
            if result is not None:
                return result
    try:
        hardware.conveyor_on()
        empty_frames = 0
        while True:
            checkStopRequested(stop_event)
            result = camera.detectObject()
            if skip_current:
                # Неподтверждённый предмет тоже занимает ROI: одного None недостаточно.
                empty_frames = empty_frames + 1 if camera.confirmation_count == 0 else 0
                if empty_frames >= camera.confirmation_frames:
                    skip_current = False
                continue
            if result is not None:
                return result
    finally:
        hardware.conveyor_off()


def measureWeight(hardware, duration: float, *, on_weight=None, stop_event=None) -> float:
    """Усреднить показания за duration секунд на уже остановленной ленте.

    Каждый get_weight() блокируется до получения серии отсчётов HX711.
    Последний запрос дочитывается полностью, даже если время уже истекло.
    """
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Время измерения веса должно быть конечным положительным числом")
    deadline = monotonic() + duration
    average = 0.0
    sample_count = 0
    while True:
        checkStopRequested(stop_event)
        weight = hardware.get_weight()
        if on_weight is not None:
            on_weight(weight)
        if not math.isfinite(weight) or weight == 0:
            raise ValueError(f"Недопустимая масса {weight} г")
        if weight < 0:
            raise NegativeWeightError(f"Недопустимая масса {weight} г")
        sample_count += 1
        average += (weight - average) / sample_count
        if monotonic() >= deadline:
            return average


def sortOneObject(hardware, camera, config: WarehouseConfig, *, stop_event=None,
                  on_weight=None, message_sink=None) -> int:
    """Выполнить цикл с закрытым затвором и остановленной лентой на входе и выходе."""
    # 1. Подвести уже закрытую тару под конвейер.
    checkStopRequested(stop_event)
    hardware.goto(*config.loading_position)
    hardware.waitUntilIdle()

    skip_current = False
    while True:
        # 2–3. Найти предмет и использовать цвет из подтвердившего его кадра.
        result = waitForObject(hardware, camera, skip_current=skip_current, stop_event=stop_event)
        # ambiguous описывает смесь цветов, а не отсутствие предмета.
        color_note = "; смешанные цвета" if result["ambiguous"] else ""
        reportMessage(f"Распознан цвет: {result['color']} ({result['dominant_share']:.1%} пикселей{color_note})",
                      message_sink)

        # 4. Взвесить предмет под камерой на остановленной ленте.
        try:
            weight = measureWeight(hardware, config.weight_duration,
                                   on_weight=on_weight, stop_event=stop_event)
        except NegativeWeightError as error:
            reportMessage(f"{error}. Пропускаем объект и ждём следующий.", message_sink)
            skip_current = True
            continue
        break

    if not math.isfinite(weight) or weight <= 0 or weight < config.min_weight:
        raise ValueError(f"Недопустимая масса {weight} г: минимум {config.min_weight} г")
    cell = selectCell(weight, config.weight_limits)
    if type(cell) is not int or cell not in config.cell_positions:
        raise ValueError("selectCell должен вернуть целый номер ячейки от 1 до 9")
    reportMessage(f"Масса: {weight:.3f} г; ячейка: {cell}", message_sink)

    # 5. Подать взвешенный предмет в тару, затем отвезти и выгрузить как раньше.
    hardware.feedOneObject(config.feed_duration)
    hardware.goto(*config.cell_positions[cell])
    hardware.waitUntilIdle()
    hardware.open()
    hardware.waitUntilIdle()
    hardware.waitForUnload()
    hardware.close()
    hardware.waitUntilIdle()
    # 6. Обновить ноль после выгрузки при неподвижных головке и ленте.
    checkStopRequested(stop_event)
    hardware.tareWeight()
    reportMessage("Тензодатчик оттарирован после выгрузки объекта.", message_sink)
    return cell


def shutdownHardware(hardware, *, disconnect: bool = True, message_sink=None) -> None:
    """Попытаться остановить оба механизма и освободить подключение независимо от ошибок."""
    actions = (
        # STOP также восстанавливает синхронизацию протокола после ошибки.
        ("остановить головку", hardware.stop),
        ("остановить конвейер", hardware.conveyor_off),
    )
    if disconnect:
        actions += (("отключить оборудование", hardware.disconnect),)
    for description, action in actions:
        try:
            action()
        except Exception as error:
            reportMessage(f"Не удалось {description}: {error}", message_sink, error=True)


def runWarehouse(hardware, camera, config: WarehouseConfig, *, stop_event=None,
                 on_weight=None, cleanup: bool = True, message_sink=None) -> None:
    """Повторять цикл; при выходе остановить и отключить оборудование.

    Подходит для любого драйвера с методами Hardware. Перед запуском механика
    должна быть откалибрована: ZERO в текущем протоколе лишь сбрасывает координаты.
    Камеру освобождает вызывающий код. При cleanup=False вызывающий код также
    обязан остановить оборудование; это позволяет контроллеру сохранить порт.
    """
    try:
        config.validate()
        checkStopRequested(stop_event)
        # Подготовка только перед первым циклом; дальше затвор закрывается при выгрузке.
        hardware.conveyor_off()
        hardware.close()
        hardware.waitUntilIdle()
        # Пустая весовая платформа; явное тарирование независимо от перезапуска платы.
        hardware.tareWeight()
        while True:
            cell = sortOneObject(hardware, camera, config,
                                 stop_event=stop_event, on_weight=on_weight, message_sink=message_sink)
            reportMessage(f"Объект помещён в ячейку {cell}", message_sink)
    finally:
        if cleanup:
            shutdownHardware(hardware, message_sink=message_sink)


def addSortingArguments(parser, *, require_weight_scale: bool = True) -> None:
    """Общие параметры сортировки для отдельного запуска и контроллера."""
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--feed-duration", type=float, default=WarehouseConfig.feed_duration,
                        help=f"Время подачи после взвешивания, в секундах (по умолчанию {WarehouseConfig.feed_duration})")
    parser.add_argument("--weight-duration", type=float, default=WarehouseConfig.weight_duration,
                        help="Время сбора показаний веса в секундах (по умолчанию 3); последний запрос дочитывается")
    parser.add_argument("--weight-scale", type=float, required=require_weight_scale,
                        help="Коэффициент калибровки HX711 в отсчётах на грамм")
    parser.add_argument("--weight-limits", type=float, nargs=2,
                        default=WarehouseConfig.weight_limits, metavar=("G1", "G2"),
                        help="Границы трёх весовых рядов в граммах (по умолчанию 5 и 12)")
    parser.add_argument("--min-weight", type=float, default=WarehouseConfig.min_weight,
                        help="Минимальная масса предмета в граммах; нулевой вес всегда отклоняется")
    parser.add_argument("--roi", type=int, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"),
                        help="Заменить область по умолчанию: X Y WIDTH HEIGHT в пикселях фактического кадра")


def createWarehouseConfig(args) -> WarehouseConfig:
    if args.weight_scale is not None and (
            not math.isfinite(args.weight_scale) or not 1e-6 <= abs(args.weight_scale) <= 1e9):
        raise ValueError("Модуль --weight-scale должен быть от 1e-6 до 1e9")
    config = WarehouseConfig(feed_duration=args.feed_duration,
                             weight_duration=args.weight_duration,
                             weight_limits=tuple(args.weight_limits), min_weight=args.min_weight)
    config.validate()
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("arduino", "raspberry-pi"), default="arduino")
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baudrate", type=int, default=115200)
    addSortingArguments(parser)
    args = parser.parse_args()
    try:
        config = createWarehouseConfig(args)
        from camera import Camera
        with ExitStack() as resources:
            camera = Camera(camera_index=args.camera, roi=args.roi)
            resources.callback(camera.disconnect)
            hardware = createHardware(args.backend, args.port, args.baudrate, args.weight_scale)
            # runWarehouse останавливает и отключает оборудование даже при ошибке.
            runWarehouse(hardware, camera, config)
    except KeyboardInterrupt:
        print("\nСортировка остановлена.")
    except (NotImplementedError, RuntimeError, ValueError, OSError) as error:
        parser.exit(1, f"Ошибка: {error}\n")


if __name__ == "__main__":
    main()
