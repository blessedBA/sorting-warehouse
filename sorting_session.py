"""Фоновая сортировка с единственным владельцем serial и пассивным просмотром веса."""

from dataclasses import dataclass
from threading import Condition, Event, Thread
from time import monotonic

from main import WarehouseConfig, runWarehouse, shutdownHardware
from sorter_log import reportMessage


@dataclass(frozen=True)
class WeightReading:
    sequence: int
    weight: float
    measured_at: float


class SortingSession:
    def __init__(self, hardware, config: WarehouseConfig, *, camera_index: int = 0,
                 roi=None, weight_scale: float | None = None, message_sink=None):
        self.hardware = hardware
        self.config = config
        self.camera_index = camera_index
        self.roi = roi
        self.weight_scale = weight_scale
        self.message_sink = message_sink
        self.stop_event = Event()
        self.condition = Condition()
        self.thread = None
        self.last_reading = None
        self.last_error = None
        self.finished = True

    def isRunning(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.isRunning():
            raise ValueError("Сортировка уже запущена")
        if self.weight_scale is None:
            raise ValueError("Перед запуском выполните scale K или calibrate G либо задайте --weight-scale")
        self.config.validate()
        self.stop_event.clear()
        with self.condition:
            self.last_reading = None
            self.last_error = None
            self.finished = False
        self.thread = Thread(target=self.run, name="warehouse-sorter")
        try:
            self.thread.start()
        except BaseException:
            with self.condition:
                self.finished = True
                self.condition.notify_all()
            raise

    def stop(self) -> None:
        """Просить владельца порта остановиться и дождаться завершения очистки."""
        self.stop_event.set()
        if self.thread is not None:
            while self.thread.is_alive():
                self.thread.join(0.1)

    def publishWeight(self, weight: float) -> None:
        with self.condition:
            sequence = 1 if self.last_reading is None else self.last_reading.sequence + 1
            self.last_reading = WeightReading(sequence, weight, monotonic())
            self.condition.notify_all()

    def getReading(self) -> WeightReading | None:
        with self.condition:
            return self.last_reading

    def waitForReading(self, after_sequence: int) -> WeightReading | None:
        with self.condition:
            self.condition.wait_for(
                lambda: self.finished or (self.last_reading is not None
                                         and self.last_reading.sequence > after_sequence),
                timeout=0.2,
            )
            if self.last_reading is not None and self.last_reading.sequence > after_sequence:
                return self.last_reading
            return None

    def showStatus(self) -> None:
        print("Сортировка работает." if self.isRunning() else "Сортировка остановлена.")
        if self.last_error is not None:
            print(f"Последняя ошибка сортировки: {self.last_error}")

    def run(self) -> None:
        camera = None
        self.hardware.cancel_event = self.stop_event
        try:
            from camera import Camera
            reportMessage("Сортировка запущена.", self.message_sink)
            camera = Camera(camera_index=self.camera_index, roi=self.roi,
                            message_sink=self.message_sink)
            runWarehouse(self.hardware, camera, self.config, stop_event=self.stop_event,
                         on_weight=self.publishWeight, cleanup=False, message_sink=self.message_sink)
        except Exception as error:
            if not (isinstance(error, InterruptedError) and self.stop_event.is_set()):
                self.last_error = str(error)
                reportMessage(f"Ошибка сортировки: {error}", self.message_sink, error=True)
        finally:
            # Очистка выполняется тем же потоком, после отмены текущего ожидания.
            self.hardware.cancel_event = None
            try:
                shutdownHardware(self.hardware, disconnect=False, message_sink=self.message_sink)
            finally:
                try:
                    if camera is not None:
                        camera.disconnect()
                finally:
                    with self.condition:
                        self.finished = True
                        self.condition.notify_all()
                    reportMessage("Сортировка остановлена; контроллер доступен.", self.message_sink)


def showSortingWeight(session: SortingSession, continuous: bool = False) -> None:
    """Показать измерения сортировщика, не посылая дополнительных WEIGHT? или STOP."""
    if not continuous:
        reading = session.getReading()
        if reading is None:
            print("Сортировщик ещё не получил показания веса.")
        else:
            print(f"Последняя масса: {reading.weight:.3f} г "
                  f"({monotonic() - reading.measured_at:.1f} с назад)", flush=True)
        return
    print("Просмотр новых измерений сортировщика. Между взвешиваниями ожидаем данные.\n"
          "Ctrl+C — закончить просмотр; сортировка продолжится.", flush=True)
    sequence = 0
    try:
        while True:
            reading = session.waitForReading(sequence)
            if reading is not None:
                sequence = reading.sequence
                print(f"Масса: {reading.weight:.3f} г "
                      f"({monotonic() - reading.measured_at:.1f} с назад)", flush=True)
            if session.finished:
                return
    except KeyboardInterrupt:
        print("\nПросмотр веса остановлен; сортировка продолжится.")
