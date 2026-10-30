import io

import pytest
from PIL import Image, ImageDraw

import logo
import shop
import texts


def image_bytes(draw, size=(400, 400), fmt="JPEG"):
    img = Image.new("RGB", size, "white")
    draw(ImageDraw.Draw(img))
    out = io.BytesIO()
    img.save(out, fmt)
    return out.getvalue()


def test_white_background_is_removed():
    art = logo.prepare_logo(image_bytes(lambda d: d.ellipse((100, 100, 300, 300), fill=(20, 90, 200))))
    # Порожні краї обрізано, фон у кутах кола прозорий, середина — ні.
    assert art.size[0] < 230 and art.getpixel((0, 0))[3] == 0
    assert art.getpixel((art.width // 2, art.height // 2))[3] == 255


def test_letter_holes_vanish_but_emblem_details_stay():
    # Однотонне кільце з маленькою дірочкою (як серединка літери «О») — дірочка стає прозорою.
    ring = logo.prepare_logo(image_bytes(lambda d: (d.ellipse((50, 50, 350, 350), fill="black"),
                                                    d.ellipse((180, 180, 220, 220), fill="white"))))
    assert ring.getpixel((ring.width // 2, ring.height // 2))[3] == 0
    # Великий білий елемент на кольоровій емблемі залишається частиною логотипа.
    emblem = logo.prepare_logo(image_bytes(lambda d: (d.ellipse((50, 50, 350, 350), fill=(20, 90, 200)),
                                                      d.rectangle((150, 150, 250, 250), fill="white"))))
    assert emblem.getpixel((emblem.width // 2, emblem.height // 2))[3] == 255


def test_transparent_png_is_kept():
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((50, 50, 150, 150), fill=(255, 255, 255, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    # Білий логотип на прозорому фоні не плутається з білим фоном.
    assert logo.prepare_logo(out.getvalue()).size == (101, 101)


@pytest.mark.parametrize("data", [b"not an image", image_bytes(lambda d: None)])
def test_unreadable_or_empty_logo(data):
    with pytest.raises(ValueError):
        logo.prepare_logo(data)


def test_render_recolors_only_invisible_logo(tmp_path):
    black_logo = logo.prepare_logo(image_bytes(lambda d: d.rectangle((100, 150, 300, 250), fill="black")))
    placement = logo.Placement(120, {"chest": (0.5, 0.3)})
    for color, recolor in (("black", True), ("white", False)):
        photo = tmp_path / f"{color}.jpg"
        Image.new("RGB", (333, 499), color).save(photo)
        jpg, recolored = logo.render(photo, black_logo, placement, "chest", 10)
        assert recolored is recolor
        # Фото в повний зріст показується ближче — частиною з логотипом.
        assert Image.open(io.BytesIO(jpg)).size == (333 * logo.UPSCALE, int(499 * logo.UPSCALE * 0.62))


def test_real_placements_match_catalog():
    """Кожне розмічене фото існує, місця відомі ботові, а точки лежать у межах фото."""
    products, placements = shop.load_products(), logo.load_placements()
    assert placements
    for pid, placement in placements.items():
        assert pid in products and shop.photo_path(pid), pid
        for spot, (x, y) in placement.spots.items():
            assert spot in texts.LOGO_SPOTS and 0 < x < 1 and 0 < y < 1, (pid, spot)
