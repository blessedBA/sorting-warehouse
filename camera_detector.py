"""Живое изображение веб-камеры с контуром предмета и его цветом."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time

import cv2
import numpy as np

from color_detector import ObjectNotFound, createRunDirectory, detectColor, drawResult, saveResults


def processFrame(frame: np.ndarray, roi: tuple | None = None) -> tuple:
    """Обработать текущий кадр, не перенося результат с предыдущего предмета."""
    try:
        result, object_mask = detectColor(frame, roi)
    except ObjectNotFound:
        return frame.copy(), None, None
    return drawResult(frame, result, object_mask), result, object_mask


def drawStatus(frame: np.ndarray, status: str, fps: float) -> None:
    """Подсказки рисуются на копии кадра, после определения цвета."""
    height, width = frame.shape[:2]
    font_scale = min(0.55, width / 1000)
    line_height = max(16, round(font_scale * 40))
    cv2.rectangle(frame, (0, height - 2 * line_height - 10), (width, height), (25, 25, 25), -1)
    for offset, text in enumerate((status, f"{fps:.1f} FPS | Q / Esc: quit | S: save snapshot")):
        cv2.putText(frame, text, (8, height - (1 - offset) * line_height - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), 1, cv2.LINE_AA)


def runCamera(camera_index: int, width: int, height: int, roi: tuple | None,
              output_dir: Path) -> None:
    window_name = "Warehouse - live color"
    backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
    capture = cv2.VideoCapture(camera_index, backend)
    window_created = False
    run_dir = None
    snapshot_number = 0
    try:
        if not capture.isOpened():
            raise RuntimeError(
                f"Не удалось открыть камеру {camera_index}. Проверьте подключение, закройте "
                "другие программы с камерой и попробуйте --camera 1 (или 2). На Mac разрешите "
                "доступ приложению запуска в «Системные настройки → Конфиденциальность "
                "и безопасность → Камера», затем запустите программу снова.")
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        capture.set(cv2.CAP_PROP_FPS, 30)
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        window_created = True
        cv2.resizeWindow(window_name, width, height)
        print("Окно камеры открыто. Q / Esc — выход; S — сохранить текущий результат.", flush=True)
        print("Для горячих клавиш выберите окно камеры и английскую раскладку.", flush=True)
        last_frame_time = time.perf_counter()
        fps = 0.0
        first_frame = True
        while True:
            success, frame = capture.read()
            if not success or frame is None:
                raise RuntimeError("Камера перестала передавать изображение. Проверьте подключение и перезапустите программу.")
            if first_frame:
                print(f"Камера {camera_index}: {frame.shape[1]}×{frame.shape[0]} пикселей.", flush=True)
                first_frame = False
            annotated, result, object_mask = processFrame(frame, roi)
            now = time.perf_counter()
            current_fps = 1 / max(now - last_frame_time, 0.000001)
            fps = current_fps if fps == 0 else 0.9 * fps + 0.1 * current_fps
            last_frame_time = now
            if roi is not None:
                x, y, roi_width, roi_height = roi
                cv2.rectangle(annotated, (x, y), (x + roi_width - 1, y + roi_height - 1), (0, 200, 255), 1)
            status = "No object - place an object fully inside the frame / ROI"
            if result is not None:
                status = f"Color: {result['color']} | Share: {result['dominant_share']:.0%}"
                if result["ambiguous"]:
                    status += " | mixed colors"
            drawStatus(annotated, status, fps)
            cv2.imshow(window_name, annotated)
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                break
            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                break
            if key in (ord("s"), ord("S")):
                if result is None:
                    print("Снимок не сохранён: в текущем кадре нет распознанного предмета.", flush=True)
                    continue
                try:
                    if run_dir is None:
                        run_dir = createRunDirectory(output_dir)
                    snapshot_number += 1
                    stem = f"{run_dir.name}_{snapshot_number:02d}"
                    result["source_file"] = f"camera:{camera_index}"
                    result["captured_at"] = datetime.now(timezone(timedelta(hours=3))).isoformat(timespec="seconds")
                    saveResults(frame, result, object_mask, run_dir, stem)
                    print(f"Сохранено: {run_dir / stem}_result.jpg; цвет — {result['color_ru']}.", flush=True)
                except (OSError, ValueError, cv2.error) as error:
                    print(f"Не удалось сохранить снимок: {error}", flush=True)
    finally:
        capture.release()
        if window_created:
            cv2.destroyAllWindows()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, default=0, help="Индекс камеры: 0, 1, 2… (по умолчанию 0)")
    parser.add_argument("--width", type=int, default=960, help="Запрашиваемая ширина кадра (по умолчанию 960)")
    parser.add_argument("--height", type=int, default=540, help="Запрашиваемая высота кадра (по умолчанию 540)")
    parser.add_argument("--roi", type=int, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"),
                        help="Область конвейера в пикселях фактического кадра камеры")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "output",
                        help="Каталог снимков, сохраняемых клавишей S")
    args = parser.parse_args()
    if args.camera < 0 or min(args.width, args.height) < 32:
        parser.error("Индекс камеры должен быть неотрицательным, размеры кадра — не менее 32×32.")
    try:
        runCamera(args.camera, args.width, args.height, args.roi, args.output_dir)
    except KeyboardInterrupt:
        print("\nКамера остановлена.")
    except (RuntimeError, ValueError, cv2.error) as error:
        parser.exit(1, f"Ошибка: {error}\n")


if __name__ == "__main__":
    main()
