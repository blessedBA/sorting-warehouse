"""Распознавание основного цвета одного предмета на фотографии."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np


color_names = {
    "black": "чёрный", "white": "белый", "gray": "серый",
    "red": "красный", "orange": "оранжевый", "yellow": "жёлтый",
    "green": "зелёный", "cyan": "голубой", "blue": "синий",
    "purple": "фиолетовый", "pink": "розовый", "brown": "коричневый",
}


class ObjectNotFound(ValueError):
    """В кадре нет достаточно крупного предмета, отличающегося от фона."""


def readImage(image_path: Path) -> np.ndarray:
    """Загрузить BGR-изображение, в том числе по пути с кириллицей."""
    image = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Не удалось прочитать изображение: {image_path}")
    return image


def foregroundMask(image: np.ndarray, brightness_threshold: float) -> np.ndarray:
    """Светлые и насыщенные пиксели, отличающиеся от чёрной ленты."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    saturation, value = hsv[:, :, 1], hsv[:, :, 2]
    chroma = image.max(axis=2).astype(np.int16) - image.min(axis=2)
    colorful = (saturation >= 65) & (value >= 65) & (chroma >= 40)
    return (value > brightness_threshold) | colorful


def segmentObject(image: np.ndarray, roi: tuple | None = None) -> np.ndarray:
    """Выделить светлый/цветной предмет на чёрном фоне внутри ROI."""
    height, width = image.shape[:2]
    if min(height, width) < 32:
        raise ValueError("Изображение должно быть размером не менее 32×32 пикселей.")
    if roi is None:
        roi = (0, 0, width, height)
    x, y, roi_width, roi_height = roi
    if (x < 0 or y < 0 or roi_width < 3 or roi_height < 3
            or x + roi_width > width or y + roi_height > height):
        raise ValueError("ROI должна находиться внутри снимка и иметь размер не менее 3×3.")

    crop = image[y:y + roi_height, x:x + roi_width]
    # Полное разрешение сохраняет тонкие предметы, медиана подавляет фактуру ленты.
    filtered = cv2.medianBlur(crop, 3)
    hsv = cv2.cvtColor(filtered, cv2.COLOR_BGR2HSV)
    value = hsv[:, :, 2]
    otsu_threshold, _ = cv2.threshold(value, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    brightness_threshold = max(95, otsu_threshold)
    # Яркость для белого/серого, насыщенность и разность каналов для тёмных цветов.
    foreground = foregroundMask(filtered, brightness_threshold).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(foreground, connectivity=8)
    # Стол и другие области, выходящие за край кадра/ROI, не считаются предметами.
    border_labels = np.unique(np.concatenate((labels[0], labels[-1], labels[:, 0], labels[:, -1])))
    min_area = max(25, roi_width * roi_height * 0.00005)
    candidates = [label for label in range(1, count)
                  if label not in border_labels and stats[label, cv2.CC_STAT_AREA] >= min_area]
    if not candidates:
        raise ObjectNotFound("Объект не найден на чёрном фоне. Поместите предмет целиком внутри "
                             "кадра/ROI; проверьте освещение и контраст с лентой.")
    object_label = max(candidates, key=lambda label: stats[label, cv2.CC_STAT_AREA])
    selected = labels == object_label
    # Проверяем исходные пиксели: сглаживание не должно добавлять тёмный фон в маску.
    original_foreground = foregroundMask(crop, brightness_threshold)
    object_mask = np.zeros((height, width), dtype=np.uint8)
    object_mask[y:y + roi_height, x:x + roi_width] = (selected & original_foreground).astype(np.uint8) * 255
    if np.count_nonzero(object_mask) < 25:
        raise ObjectNotFound("После исключения фона объект слишком мал для определения цвета.")
    return object_mask


def classifyPixels(pixels: np.ndarray) -> np.ndarray:
    """Базовые классы цвета по HSV; H в OpenCV лежит в диапазоне 0…179."""
    hsv = cv2.cvtColor(pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    hue, saturation, value = hsv.T
    names = np.full(len(pixels), "red", dtype="<U6")
    hue_ranges = [
        (10, 23, "orange"), (23, 35, "yellow"), (35, 85, "green"),
        (85, 100, "cyan"), (100, 130, "blue"),
        (130, 155, "purple"), (155, 170, "pink"),
    ]
    for lower, upper, name in hue_ranges:
        names[(hue >= lower) & (hue < upper)] = name
    names[(hue >= 5) & (hue < 25) & (value < 170)] = "brown"
    names[((hue < 10) | (hue >= 155)) & (saturation < 130) & (value >= 170)] = "pink"
    names[saturation < 45] = "gray"
    names[(saturation < 45) & (value >= 200)] = "white"
    names[(value < 45) | ((saturation < 60) & (value < 95))] = "black"
    return names


def detectColor(image: np.ndarray, roi: tuple | None = None) -> tuple[dict, np.ndarray]:
    """Вернуть описание цвета и маску; можно вызывать для кадров камеры."""
    object_mask = segmentObject(image, roi)
    # Отступ от контура уменьшает влияние фона и сглаженных краёв.
    kernel_size = max(3, round(min(image.shape[:2]) * 0.005) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    sample_mask = cv2.erode(object_mask, kernel)
    if np.count_nonzero(sample_mask) < 25:
        sample_mask = object_mask
    pixels = image[sample_mask > 0]
    pixel_names = classifyPixels(pixels)
    names, counts = np.unique(pixel_names, return_counts=True)
    order = np.argsort(-counts)
    dominant_name = str(names[order[0]])
    dominant_share = float(counts[order[0]] / len(pixels))
    median_bgr = np.median(pixels[pixel_names == dominant_name], axis=0).astype(int)
    x, y, width, height = cv2.boundingRect(object_mask)
    result = {
        "color": dominant_name,
        "color_ru": color_names[dominant_name],
        "dominant_share": round(dominant_share, 4),
        "ambiguous": dominant_share < 0.6,
        "rgb": [int(channel) for channel in median_bgr[::-1]],
        "bbox": {"x": x, "y": y, "width": width, "height": height},
        "color_shares": {str(names[index]): round(float(counts[index] / len(pixels)), 4)
                         for index in order},
    }
    return result, object_mask


def saveImage(path: Path, image: np.ndarray) -> None:
    success, encoded = cv2.imencode(path.suffix, image)
    if not success:
        raise ValueError(f"Не удалось сохранить изображение: {path}")
    encoded.tofile(path)


def drawResult(image: np.ndarray, result: dict, object_mask: np.ndarray) -> np.ndarray:
    """Нарисовать контур и цвет одинаково для фотографий и видео."""
    annotated = image.copy()
    contours, _ = cv2.findContours(object_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    line_width = max(2, round(min(image.shape[:2]) / 400))
    cv2.drawContours(annotated, contours, -1, (0, 200, 0), line_width)
    bbox = result["bbox"]
    x, y = bbox["x"], bbox["y"]
    font_scale = max(0.5, min(image.shape[:2]) / 1000)
    label = f"{result['color']} ({result['dominant_share']:.0%} of object pixels)"
    if result["ambiguous"]:
        label += " - mixed"
    # Латиница нужна только на картинке: стандартный шрифт OpenCV без кириллицы.
    text_size, baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, line_width)
    label_y = max(text_size[1] + 12, y - 12)
    cv2.rectangle(annotated, (x, label_y - text_size[1] - 8),
                  (min(image.shape[1] - 1, x + text_size[0] + 8), label_y + baseline), (255, 255, 255), -1)
    cv2.putText(annotated, label, (x + 4, label_y), cv2.FONT_HERSHEY_SIMPLEX,
                font_scale, (0, 0, 0), line_width, cv2.LINE_AA)
    return annotated


def saveResults(image: np.ndarray, result: dict, object_mask: np.ndarray,
                output_dir: Path, stem: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    annotated = drawResult(image, result, object_mask)
    saveImage(output_dir / f"{stem}_result.jpg", annotated)
    saveImage(output_dir / f"{stem}_mask.png", object_mask)
    (output_dir / f"{stem}_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def createRunDirectory(output_dir: Path) -> Path:
    """Создать отдельную папку запуска, не перезаписывая прошлые результаты."""
    output_dir.mkdir(parents=True, exist_ok=True)
    run_name = datetime.now(timezone(timedelta(hours=3))).strftime("ph_%m.%d %H.%M")
    run_number = 1
    while True:
        suffix = f"_{run_number:02d}" if run_number > 1 else ""
        run_dir = output_dir / f"{run_name}{suffix}"
        try:
            run_dir.mkdir()
            return run_dir
        except FileExistsError:
            run_number += 1


def main() -> None:
    project_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, nargs="?", default=project_dir / "photos",
                        help="Фотография или папка; по умолчанию папка photos рядом с программой")
    parser.add_argument("--roi", type=int, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"),
                        help="Прямоугольник вокруг объекта в пикселях с небольшим запасом фона")
    parser.add_argument("--output-dir", type=Path, default=project_dir / "output",
                        help="Каталог, в котором создаются папки отдельных запусков")
    args = parser.parse_args()
    try:
        batch_mode = args.image.is_dir()
        if batch_mode:
            image_extensions = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
            image_paths = sorted(
                path for path in args.image.iterdir()
                if path.is_file() and path.suffix.lower() in image_extensions
            )
        else:
            image_paths = [args.image]
    except OSError as error:
        parser.exit(1, f"Ошибка: {error}\n")
    if not image_paths:
        print(f"В папке {args.image.resolve()} пока нет фотографий.")
        print("Добавьте JPG, JPEG, PNG, BMP, WEBP или TIFF и запустите программу снова.")
        return

    try:
        run_dir = createRunDirectory(args.output_dir)
    except OSError as error:
        parser.exit(1, f"Не удалось создать папку запуска: {error}\n")
    print(f"Папка запуска: {run_dir.resolve()}", flush=True)
    error_count = 0
    for image_number, image_path in enumerate(image_paths, start=1):
        print(f"\nФотография: {image_path.name}", flush=True)
        try:
            image = readImage(image_path)
            result, object_mask = detectColor(image, args.roi)
            image_suffix = f"_{image_number:02d}" if len(image_paths) > 1 else ""
            output_stem = f"{run_dir.name}{image_suffix}"
            result["source_file"] = image_path.name
            saveResults(image, result, object_mask, run_dir, output_stem)
        except (OSError, ValueError, cv2.error) as error:
            print(f"Ошибка: {error}", flush=True)
            error_count += 1
            continue
        print(f"Цвет объекта: {result['color_ru']} ({result['color']})")
        print(f"Доля основного цвета: {result['dominant_share']:.1%}")
        print(f"RGB основного цвета: {result['rgb']}")
        print(f"Файлы: {output_stem}_result.jpg / _mask.png / _result.json")
        if result["ambiguous"]:
            print("Нет выраженного основного цвета: проверьте маску и освещение.")
    print(f"\nОбработано: {len(image_paths) - error_count}; ошибок: {error_count}.")
    if error_count < len(image_paths):
        print(f"Результаты: {run_dir.resolve()}")
    if error_count:
        parser.exit(1)


if __name__ == "__main__":
    main()
