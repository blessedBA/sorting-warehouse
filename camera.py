"""API камеры конвейера для управляющего скрипта и живого просмотра."""

import sys

import cv2
import numpy as np

from color_detector import ObjectNotFound, detectColor, drawResult


def processFrame(frame: np.ndarray, roi: tuple | None = None,
                 background: str = "white") -> tuple:
    """Обработать текущий кадр, не перенося результат с предыдущего предмета."""
    try:
        result, object_mask = detectColor(frame, roi, background)
    except ObjectNotFound:
        return frame.copy(), None, None
    return drawResult(frame, result, object_mask), result, object_mask


class Camera:
    """Камера без окна просмотра; ROI задаётся как (x, y, width, height).

    Режим background задаёт белый или чёрный фон конвейера.
    Каждый вызов обрабатывает один кадр, без отслеживания отдельных объектов.
    """

    def __init__(self, camera_index: int = 0, width: int = 960,
                 height: int = 540, roi: tuple | None = None,
                 background: str = "white"):
        if camera_index < 0 or min(width, height) < 32:
            raise ValueError("Индекс камеры должен быть неотрицательным, размеры кадра — не менее 32×32.")
        if background not in ("white", "black"):
            raise ValueError("Цвет фона должен быть white или black.")
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.roi = roi
        self.background = background
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
            raise RuntimeError("Камера не подключена. Вызовите init().")
        success, frame = self.capture.read()
        if not success or frame is None:
            raise RuntimeError("Камера перестала передавать изображение. Проверьте подключение и перезапустите программу.")
        return frame

    def processFrame(self, frame: np.ndarray | None = None) -> tuple:
        """Вернуть (кадр с обводкой, результат, маска) для переданного или нового кадра."""
        if frame is None:
            frame = self.getFrame()
        return processFrame(frame, self.roi, self.background)

    def detectObject(self, frame: np.ndarray | None = None) -> dict | None:
        """Вернуть данные предмета для управляющего скрипта или None, если его нет.

        Поля: color, color_ru, dominant_share, ambiguous, rgb, bbox,
        color_shares, background.
        Без аргумента считывается новый кадр; переданный кадр позволяет повторно
        использовать уже сделанный снимок. Старые результаты не сохраняются.
        """
        _, result, _ = self.processFrame(frame)
        if result is not None:
            print(result["color"])
        else:
            print("Объект или цвет не обнаружен!")
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
        if self.capture is not None:
            self.capture.release()
            self.capture = None
