"""API камеры конвейера для управляющего скрипта и живого просмотра."""

import sys
from time import monotonic

import cv2
import numpy as np

from color_detector import ObjectNotFound, detectColor, drawResult, validateSizeLimits
from sorter_log import reportMessage


# Прямоугольная область по отметке на скриншоте 30.09.2026, без заголовка окна.
# Доли фактического кадра: x, y, ширина, высота.
DEFAULT_ROI_FRACTION = (0.23, 0.16, 0.52, 0.64)


def processFrame(frame: np.ndarray, roi: tuple | None = None,
                 background: str = "white", *, min_area_fraction: float = 0.00005,
                 min_width: int = 1, min_height: int = 1) -> tuple:
    """Обработать текущий кадр, не перенося результат с предыдущего предмета."""
    try:
        result, object_mask = detectColor(frame, roi, background,
                                         min_area_fraction=min_area_fraction,
                                         min_width=min_width, min_height=min_height)
    except ObjectNotFound:
        return frame.copy(), None, None
    return drawResult(frame, result, object_mask), result, object_mask


class Camera:
    """Камера без окна просмотра; ROI задаётся как (x, y, width, height).

    При roi=None используется DEFAULT_ROI_FRACTION с масштабированием под кадр.
    Явная ROI задаётся в пикселях фактического кадра.
    Режим background задаёт белый или чёрный фон конвейера.
    Предмет подтверждается по confirmation_frames последовательным кадрам.
    Размеры заданы в пикселях фактического кадра, площадь — в долях ROI.
    """

    def __init__(self, camera_index: int = 0, width: int = 960,
                 height: int = 540, roi: tuple | None = None,
                 background: str = "white", *, min_area_fraction: float = 0.002,
                 min_width: int = 8, min_height: int = 8, confirmation_frames: int = 3,
                 message_sink=None):
        if camera_index < 0 or min(width, height) < 32:
            raise ValueError("Индекс камеры должен быть неотрицательным, размеры кадра — не менее 32×32.")
        if background not in ("white", "black"):
            raise ValueError("Цвет фона должен быть white или black.")
        validateSizeLimits(min_area_fraction, min_width, min_height)
        if (not isinstance(confirmation_frames, int) or isinstance(confirmation_frames, bool)
                or confirmation_frames < 1):
            raise ValueError("Число кадров подтверждения должно быть целым положительным числом.")
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.roi = roi
        self.background = background
        self.min_area_fraction = min_area_fraction
        self.min_width = min_width
        self.min_height = min_height
        self.confirmation_frames = confirmation_frames
        self.message_sink = message_sink
        self._last_missing_message_time = float("-inf")
        self.resetDetection()
        self.capture = None
        self.init()

    def init(self) -> None:
        """Подключиться к камере; повторный вызов при открытой камере ничего не делает."""
        if self.capture is not None and self.capture.isOpened():
            return
        self.disconnect()
        backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_V4L2
        self.capture = cv2.VideoCapture(self.camera_index, backend)
        try:
            if not self.capture.isOpened():
                raise RuntimeError(
                    f"Не удалось открыть камеру {self.camera_index}. Проверьте подключение, закройте "
                    "другие программы с камерой и попробуйте другой индекс камеры. На Mac разрешите "
                    "доступ приложению запуска в «Системные настройки → Конфиденциальность "
                    "и безопасность → Камера», затем запустите программу снова.")
            self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.capture.set(cv2.CAP_PROP_FPS, 30)
        except BaseException:
            self.disconnect()
            raise

    def getFrame(self) -> np.ndarray:
        """Считать следующий BGR-кадр; ошибка камеры отличается от отсутствия предмета."""
        if self.capture is None:
            self.resetDetection()
            raise RuntimeError("Камера не подключена. Вызовите init().")
        success, frame = self.capture.read()
        if not success or frame is None:
            self.resetDetection()
            raise RuntimeError("Камера перестала передавать изображение. Проверьте подключение и перезапустите программу.")
        return frame

    def getRoi(self, frame: np.ndarray) -> tuple:
        """Одна область для распознавания и отображения её границы."""
        if self.roi is not None:
            return tuple(self.roi)
        height, width = frame.shape[:2]
        return tuple(round(fraction * size) for fraction, size in
                     zip(DEFAULT_ROI_FRACTION, (width, height, width, height)))

    def processFrame(self, frame: np.ndarray | None = None) -> tuple:
        """Вернуть обводку, результат и маску только подтверждённого предмета.

        При передаче кадров вызывающий код должен передавать очередной кадр
        ровно один раз: повторная обработка одного снимка тоже увеличит счётчик.
        """
        if frame is None:
            frame = self.getFrame()
        roi = self.getRoi(frame)
        context = (frame.shape, roi, self.background, self.min_area_fraction,
                   self.min_width, self.min_height, self.confirmation_frames)
        if context != self._detection_context:
            self.resetDetection()
            self._detection_context = context
        try:
            annotated, result, object_mask = processFrame(
                frame, roi, self.background, min_area_fraction=self.min_area_fraction,
                min_width=self.min_width, min_height=self.min_height)
        except (ValueError, cv2.error):
            self.resetDetection()
            raise
        if result is None:
            self.resetDetection()
            return annotated, None, None
        if self.matchesPrevious(result):
            self.confirmation_count = min(self.confirmation_count + 1, self.confirmation_frames)
        else:
            self.confirmation_count = 1
        self._previous_bbox = result["bbox"].copy()
        self._previous_color = result["color"]
        if self.confirmation_count < self.confirmation_frames:
            return frame.copy(), None, None
        return annotated, result, object_mask

    def resetDetection(self) -> None:
        """Забыть предыдущий предмет и накопленное подтверждение."""
        self.confirmation_count = 0
        self._previous_bbox = None
        self._previous_color = None
        self._detection_context = None

    def matchesPrevious(self, result: dict) -> bool:
        """Допустить движение, но отвергнуть скачок положения, размера или цвета."""
        previous = self._previous_bbox
        current = result["bbox"]
        if previous is None or result["color"] != self._previous_color:
            return False
        for dimension in ("width", "height"):
            if min(previous[dimension], current[dimension]) / max(previous[dimension], current[dimension]) < 0.6:
                return False
        overlap_width = max(0, min(previous["x"] + previous["width"], current["x"] + current["width"])
                            - max(previous["x"], current["x"]))
        overlap_height = max(0, min(previous["y"] + previous["height"], current["y"] + current["height"])
                             - max(previous["y"], current["y"]))
        intersection = overlap_width * overlap_height
        union = (previous["width"] * previous["height"]
                 + current["width"] * current["height"] - intersection)
        return intersection / union >= 0.25

    def detectObject(self, frame: np.ndarray | None = None) -> dict | None:
        """Вернуть данные предмета для управляющего скрипта или None, если его нет.

        Поля: color, color_ru, dominant_share, ambiguous, rgb, bbox,
        color_shares, background.
        Без аргумента считывается новый кадр; переданный кадр позволяет повторно
        использовать уже сделанный снимок. До подтверждения возвращается None;
        при исчезновении предмета результат и счётчик сразу сбрасываются.
        """
        _, result, _ = self.processFrame(frame)
        if result is None:
            current_time = monotonic()
            if current_time - self._last_missing_message_time >= 2.0:
                reportMessage("Объект или цвет не обнаружен!", self.message_sink)
                self._last_missing_message_time = current_time
        return result

    def isObjectReady(self, result: dict | None) -> bool:
        """TODO: проверить прибытие объекта в нужное место по данным detectObject.

        Нужно определить целевое положение, допуск и подтверждение по нескольким
        кадрам. Наличие предмета в ROI само по себе не подтверждает готовность.
        """
        # FIXME
        return True
        raise NotImplementedError("Проверка положения объекта на конвейере пока не реализована")

    def disconnect(self) -> None:
        """Освободить камеру; повторный вызов допустим."""
        self.resetDetection()
        if self.capture is not None:
            self.capture.release()
            self.capture = None
