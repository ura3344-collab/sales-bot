"""Візуалізація логотипа клієнта на фото виробу — чиста логіка без Telegram."""
import csv
import io
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

import shop

SPOTS_FILE = Path(__file__).parent / "logo_spots.csv"
UPSCALE = 3          # фото каталогу маленькі (333×499) — збільшуємо, щоб логотип був чітким
MAX_LOGO_PX = 600    # більший логотип однаково зменшиться, а прибирання фону сповільниться
BG_TOLERANCE = 40    # наскільки колір може відрізнятися від фону, щоб вважатися фоном
LOW_CONTRAST = 100   # відстань між кольорами логотипа й тканини (RGB), нижче якої логотип перефарбовується
MONOTONE = 30        # розкид яскравості всередині логотипа, нижче якого він вважається однотонним
SMALL_HOLE = 0.03    # частка площі, менша за яку замкнена ділянка фону вважається серединкою літери


@dataclass(frozen=True)
class Placement:
    """Де на фото товару можна розмістити логотип.
    frame_cm — скільки сантиметрів уміщається в ширину фото; spots — місце → (x, y) частками від розміру фото."""
    frame_cm: float
    spots: dict[str, tuple[float, float]]


def load_placements(path: Path | None = None) -> dict[str, Placement]:
    # Порядок місць у файлі важливий: перше місце товару — місце за замовчуванням.
    placements: dict[str, Placement] = {}
    with open(path or SPOTS_FILE, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            pid = r["id"].strip()
            placement = placements.setdefault(pid, Placement(float(r["ширина_кадру_см"]), {}))
            placement.spots[r["місце"].strip()] = (float(r["x"]), float(r["y"]))
    return placements


def prepare_logo(data: bytes) -> Image.Image:
    """Відкриває логотип, прибирає однотонний фон навколо нього й обрізає порожні краї.
    Прозорий PNG залишається як є. Нічого не видно — ValueError."""
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        img.thumbnail((MAX_LOGO_PX, MAX_LOGO_PX))
        img = img.convert("RGBA")
    except Exception as e:
        raise ValueError("не вдалося відкрити зображення") from e
    if img.getchannel("A").getextrema()[0] > 200:
        rgb = img.convert("RGB")
        alpha, bg = _background_mask(rgb)
        # Краї логотипа змішані з фоном — на темній тканині це біла облямівка. Розтягуємо на них колір логотипа.
        spread = rgb.filter(ImageFilter.MinFilter(3) if sum(bg) > 384 else ImageFilter.MaxFilter(3))
        img = Image.composite(rgb, spread, _solid(alpha))
        img.putalpha(alpha)
    bbox = img.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    if not bbox:
        raise ValueError("на зображенні не знайдено логотипа")
    return img.crop(bbox)


def _background_mask(rgb: Image.Image) -> tuple[Image.Image, tuple[int, ...]]:
    """Прозорість: фон — це кольори, близькі до кольору країв і з'єднані з краєм.
    В однотонному логотипі прозорими стають і дрібні замкнені ділянки фону — серединки літер «А», «О».
    Великі (біла літера на кольоровій емблемі) і будь-які ділянки в багатоколірному логотипі залишаються.
    Повертає прозорість і колір фону."""
    w, h = rgb.size
    edges = Image.new("RGB", (w * 2 + h * 2, 1))
    for i, box in enumerate([(0, 0, w, 1), (0, h - 1, w, h)]):
        edges.paste(rgb.crop(box), (i * w, 0))
    for i, box in enumerate([(0, 0, 1, h), (w - 1, 0, w, h)]):
        edges.paste(rgb.crop(box).rotate(90, expand=True), (2 * w + i * h, 0))
    bg = tuple(int(v) for v in ImageStat.Stat(edges).median)

    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg))
    distance = ImageChops.lighter(ImageChops.lighter(*diff.split()[:2]), diff.split()[2])
    # Значення маски: 255 — схоже на фон, 0 — логотип, 128 — прозоре, 64 — замкнена ділянка, що лишається.
    mask = distance.point(lambda v: 255 if v < BG_TOLERANCE else 0)
    monotone = _is_monotone(rgb, ImageChops.invert(mask))
    step = max(1, min(w, h) // 20)
    seeds = [(x, y) for x in range(0, w, step) for y in (0, h - 1)] + \
            [(x, y) for y in range(0, h, step) for x in (0, w - 1)]
    for seed in seeds:
        if mask.getpixel(seed) == 255:
            ImageDraw.floodfill(mask, seed, 128)
    while (i := mask.tobytes().find(b"\xff")) >= 0:
        seed = (i % w, i // w)
        ImageDraw.floodfill(mask, seed, 200)
        small = mask.histogram()[200] < w * h * SMALL_HOLE
        ImageDraw.floodfill(mask, seed, 128 if monotone and small else 64)
    alpha = mask.point(lambda v: 0 if v == 128 else 255)
    return alpha.filter(ImageFilter.GaussianBlur(0.7)), bg


def _luminance(img: Image.Image, mask: Image.Image | None = None) -> float:
    return ImageStat.Stat(img.convert("L"), mask).mean[0]


def _solid(mask: Image.Image) -> Image.Image:
    """Внутрішня частина логотипа без країв: на краях кольори змішані з фоном і заважають оцінці."""
    solid = mask.point(lambda v: 255 if v > 128 else 0)
    inner = solid.filter(ImageFilter.MinFilter(5))
    return inner if inner.getbbox() else solid


def _is_monotone(img: Image.Image, mask: Image.Image) -> bool:
    """Чи однакова яскравість у всіх пікселях логотипа (mask — 255 там, де логотип)."""
    return ImageStat.Stat(img.convert("L"), _solid(mask)).stddev[0] < MONOTONE


def render(photo: Path, logo: Image.Image, placement: Placement, spot: str, size_cm: float) -> tuple[bytes, bool]:
    """Накладає логотип на фото товару. size_cm — більша сторона логотипа в сантиметрах.
    Повертає JPEG і True, якщо однотонний логотип довелося перефарбувати, бо він зливався з тканиною."""
    base = Image.open(photo).convert("RGB")
    base = base.resize((base.width * UPSCALE, base.height * UPSCALE), Image.LANCZOS)
    x, y = placement.spots[spot]
    cx, cy = int(x * base.width), int(y * base.height)

    px_per_cm = base.width / placement.frame_cm
    scale = size_cm * px_per_cm / max(logo.size)
    art = logo.resize((max(1, round(logo.width * scale)), max(1, round(logo.height * scale))), Image.LANCZOS)
    left, top = cx - art.width // 2, cy - art.height // 2
    under = base.crop((left, top, left + art.width, top + art.height))
    alpha = art.getchannel("A")

    # Колір тканини — з невеликої ділянки в центрі, щоб не врахувати футболку чи фон навколо.
    r = round(3 * px_per_cm)
    patch = base.crop((cx - r, cy - r, cx + r, cy + r))
    rgb = art.convert("RGB")
    # Багатоколірний логотип лишаємо як є — його деталі однаково видно; перефарбовуємо лише однотонний.
    distance = math.dist(ImageStat.Stat(rgb, _solid(alpha)).mean, ImageStat.Stat(patch).mean)
    recolored = _is_monotone(art, alpha) and distance < LOW_CONTRAST
    if recolored:
        rgb = Image.new("RGB", art.size, (245, 245, 245) if _luminance(patch) < 128 else (25, 25, 25))
    # Складки тканини: світлі й темні місця під логотипом проступають крізь нього.
    mean = _luminance(under)
    folds = under.convert("L").filter(ImageFilter.GaussianBlur(2))
    folds = folds.point(lambda v: max(0, min(255, round(128 + (v - mean) * 0.8))))
    rgb = ImageChops.overlay(rgb, Image.merge("RGB", (folds,) * 3))
    base.paste(rgb, (left, top), alpha.filter(ImageFilter.GaussianBlur(0.6)))

    # На фото в повний зріст логотип дрібний — показуємо ближче частину з ним.
    if placement.frame_cm > 100:
        height = int(base.height * 0.62)
        top = min(max(0, cy - height // 2), base.height - height)
        base = base.crop((0, top, base.width, top + height))
    out = io.BytesIO()
    base.save(out, "JPEG", quality=88)
    return out.getvalue(), recolored


def logo_products(products: dict[str, shop.Product], placements: dict[str, Placement]) -> dict[str, list[shop.Product]]:
    """Моделі, на які можна приміряти логотип, і їхні кольори з розміченими фото."""
    result: dict[str, list[shop.Product]] = {}
    for p in products.values():
        if p.id in placements and shop.photo_path(p.id):
            result.setdefault(p.model, []).append(p)
    return result
