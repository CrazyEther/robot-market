"""Private raster plans and attested two-anchor similarity projection.

Never derive graph edges or route lengths from raster dimensions or pixels.
"""

from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 8_000_000


class FloorPlanError(ValueError):
    pass


def sanitize_floor_plan(upload):
    if upload.size > MAX_SOURCE_BYTES:
        raise FloorPlanError("План превышает 8 МБ.")
    source = upload.read(MAX_SOURCE_BYTES + 1)
    if not source or len(source) > MAX_SOURCE_BYTES:
        raise FloorPlanError("План пуст или превышает 8 МБ.")
    original_sha256 = hashlib.sha256(source).hexdigest()
    try:
        with Image.open(BytesIO(source)) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise FloorPlanError("Загрузите изображение PNG или JPEG.")
            width, height = image.size
            if min(width, height) < 2 or width > 4096 or height > 4096 or width * height > MAX_PIXELS:
                raise FloorPlanError("Размер плана: до 4096 пикселей по стороне и 8 млн пикселей всего.")
            if getattr(image, "n_frames", 1) != 1:
                raise FloorPlanError("Анимированные изображения не допускаются.")
            image = ImageOps.exif_transpose(image)
            rgba = image.convert("RGBA")
            sanitized = Image.new("RGB", rgba.size, "white")
            sanitized.paste(rgba, mask=rgba.getchannel("A"))
            output = BytesIO()
            sanitized.save(output, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, FloorPlanError):
            raise
        raise FloorPlanError("Не удалось безопасно прочитать изображение.") from exc
    raw_png = output.getvalue()
    if len(raw_png) > MAX_SOURCE_BYTES:
        raise FloorPlanError("Нормализованный план превышает 8 МБ.")
    return {
        "image_png": raw_png, "png_sha256": hashlib.sha256(raw_png).hexdigest(),
        "original_sha256": original_sha256, "width_px": sanitized.width,
        "height_px": sanitized.height,
    }


def calibration_for(topology, source, anchor_a, anchor_b, pixels, evidence):
    """Validate same-floor measured anchors and explicit pixel coordinates."""
    nodes = {item["id"]: item for item in topology["nodes"]}
    a, b = nodes.get(anchor_a), nodes.get(anchor_b)
    if a is None or b is None or a is b or a.get("floor") != source.floor or b.get("floor") != source.floor:
        raise FloorPlanError("Выберите разные измеренные точки указанного этажа.")
    if not evidence.strip():
        raise FloorPlanError("Укажите основание сопоставления точек и изображения.")
    if not all(point.get("coordinate_source") and point.get("x_m") is not None
               and point.get("y_m") is not None for point in (a, b)):
        raise FloorPlanError("Для обоих якорей нужны измеренные координаты и их источники.")
    try:
        xy = [Decimal(str(point[key])) for point in (a, b) for key in ("x_m", "y_m")]
        pixel = [Decimal(str(value)) for value in pixels]
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FloorPlanError("Координаты якорей повреждены.") from exc
    if not all(value.is_finite() for value in (*xy, *pixel)):
        raise FloorPlanError("Координаты якорей должны быть конечными числами.")
    if (xy[2] - xy[0]) ** 2 + (xy[3] - xy[1]) ** 2 < Decimal("0.000001"):
        raise FloorPlanError("Измеренные точки должны находиться в разных местах.")
    if ((pixel[2] - pixel[0]) ** 2 + (pixel[3] - pixel[1]) ** 2 < 100
            or not 0 <= pixel[0] <= source.width_px or not 0 <= pixel[2] <= source.width_px
            or not 0 <= pixel[1] <= source.height_px or not 0 <= pixel[3] <= source.height_px):
        raise FloorPlanError("Отметьте две разные точки внутри изображения, отстоящие минимум на 10 пикселей.")
    return {
        "id": str(source.id), "png_sha256": source.png_sha256,
        "width_px": source.width_px, "height_px": source.height_px,
        "anchor_a": anchor_a, "anchor_b": anchor_b,
        "pixel_a": [str(pixel[0]), str(pixel[1])],
        "pixel_b": [str(pixel[2]), str(pixel[3])],
        "evidence": evidence.strip(),
    }


def project_on_plan(topology, floor, plan, nodes):
    """World meters → image pixels → 600×400 viewport (Y world points upward).

    Two anchors determine a uniform similarity transform with image-Y reflected.
    Does not attest the survey, scale, image rectification, or navigation edges.
    """
    lookup = {item["id"]: item for item in topology["nodes"]}
    try:
        a, b = lookup[plan["anchor_a"]], lookup[plan["anchor_b"]]
        if a.get("floor") != floor or b.get("floor") != floor:
            return None
        aw = (Decimal(a["x_m"]), Decimal(a["y_m"]))
        bw = (Decimal(b["x_m"]), Decimal(b["y_m"]))
        ap = tuple(Decimal(v) for v in plan["pixel_a"])
        bp = tuple(Decimal(v) for v in plan["pixel_b"])
        dx, dy = bw[0] - aw[0], bw[1] - aw[1]
        px, py = bp[0] - ap[0], bp[1] - ap[1]
        square = dx * dx + dy * dy
        if square < Decimal("0.000001") or px * px + py * py < 100:
            return None
        width, height = Decimal(plan["width_px"]), Decimal(plan["height_px"])
        if width <= 0 or height <= 0:
            return None
        scale = min(Decimal(580) / width, Decimal(380) / height)
        x0, y0 = (Decimal(600) - width * scale) / 2, (Decimal(400) - height * scale) / 2
        positioned = {}
        for node in nodes:
            vx, vy = Decimal(node["x_m"]) - aw[0], Decimal(node["y_m"]) - aw[1]
            along = (vx * dx + vy * dy) / square
            across = (dx * vy - dy * vx) / square
            img_x = ap[0] + along * px + across * py
            img_y = ap[1] + along * py - across * px
            positioned[node["id"]] = {
                "id": node["id"], "label": node["label"],
                "x": format(x0 + img_x * scale, ".2f"),
                "y": format(y0 + img_y * scale, ".2f"),
            }
        return positioned, {
            "id": plan["id"], "png_sha256": plan["png_sha256"],
            "x": format(x0, ".2f"), "y": format(y0, ".2f"),
            "width": format(width * scale, ".2f"),
            "height": format(height * scale, ".2f"),
        }
    except (KeyError, InvalidOperation, TypeError, ValueError, ZeroDivisionError):
        return None
