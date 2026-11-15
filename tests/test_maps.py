"""Synthetic tests for qa.maps label grouping (no data or tesseract needed)."""
from qa.maps import _back, blocks, find_label


def W(rows):
    return [dict(text=t, conf=90, x=x, y=y, w=w, h=h, rot=r) for t, r, x, y, w, h in rows]


def test_stacked_label_is_joined():
    words = W([("FIRE", 0, 3841, 7014, 50, 34), ("ENG", 0, 3854, 7061, 45, 34), ("HO", 0, 3893, 7104, 35, 34), ("ENG", 0, 100, 100, 40, 30)])
    best = find_label(words, "Fire Eng. Ho.")[0]
    assert best["text"] == "FIRE ENG HO" and best["kind"] == "block"


def test_vertical_label_reads_bottom_to_top():
    words = W([("CABINS.", 270, 3631, 7079, 40, 120), ("MINERS", 270, 3622, 7270, 40, 110)])
    assert find_label(words, "Miners Cabins")[0]["text"] == "MINERS CABINS."


def test_far_words_not_joined():
    assert len(blocks(W([("A", 0, 0, 0, 30, 30), ("B", 0, 5000, 5000, 30, 30)]))) == 2


def test_rotation_mapping_roundtrip():
    # a word at (x=10,y=20,w=30,h=8) in a 100x60 crop, rotated CCW 90 -> (xr=y, yr=cw-1-x-w+1) ...
    cw, ch = 100, 60
    x, y, w, h = 10, 20, 30, 8
    rot90 = (y, cw - x - w, h, w)  # box in the CCW-rotated crop
    assert _back(rot90, 90, cw, ch) == (x, y, w, h)
    rot270 = (ch - y - h, x, h, w)  # box in the CW-rotated crop
    assert _back(rot270, 270, cw, ch) == (x, y, w, h)


def test_label_found_inside_noisy_block():
    words = W([("2", 0, 3750, 7049, 65, 95), ("FIRE", 0, 3841, 7014, 92, 56), ("ENG", 0, 3854, 7061, 84, 50), ("HO", 0, 3893, 7104, 60, 43),
               ("xo", 0, 3750, 7162, 70, 48), ("Cac", 0, 3699, 7220, 125, 46)])
    best = find_label(words, "Fire Eng. Ho.")[0]
    assert "FIRE" in best["text"] and "ENG HO" in best["text"] and best["score"] >= 0.85
    assert best["y"] == 7014 and best["h"] >= 133  # box covers the three stacked lines


def test_fill_at_is_relative_to_paper(tmp_path):
    from PIL import Image, ImageDraw

    from qa.maps import fill_at

    im = Image.new("RGB", (400, 300), (227, 221, 209))  # cream paper
    d = ImageDraw.Draw(im)
    d.rectangle((20, 20, 120, 120), fill=(208, 199, 161))   # yellow
    d.rectangle((200, 20, 300, 120), fill=(209, 186, 162))  # pink
    d.rectangle((20, 160, 120, 260), fill=(150, 175, 195))  # blue
    p = tmp_path / "m.png"
    im.save(p)
    assert fill_at(p, (30, 30, 80, 80))["name"] == "yellow"
    assert fill_at(p, (210, 30, 80, 80))["name"] == "pink/red"
    assert fill_at(p, (30, 170, 80, 80))["name"] == "blue"
    assert fill_at(p, (300, 150, 80, 100))["name"] == "white/none"
