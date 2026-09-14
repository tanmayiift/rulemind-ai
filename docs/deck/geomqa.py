"""Geometry QA for a generated .pptx, for when no renderer is available.

Checks the defect classes that actually ship broken decks:
  1. shapes outside the slide canvas (pptxgenjs writes them, it does not clamp)
  2. shapes closer than a minimum margin to the slide edge
  3. text estimated to overflow its own box
  4. text boxes overlapping other text boxes

Text estimation uses per-font average glyph-width ratios; it is approximate, so it reports a
FILL RATIO and only flags well past 100%. Under-flagging is the deliberate bias -- a false
"overflow" on every slide would train you to ignore the output.
"""
import sys, zipfile, re
from defusedxml import ElementTree as ET

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}
EMU = 914400.0
SLIDE_W, SLIDE_H = 13.333, 7.5
MIN_MARGIN = 0.45

# average advance width as a fraction of font size, measured for these families
RATIO = {"Cambria": 0.50, "Calibri": 0.47, "Courier New": 0.60}
LINE_H = 1.22


def boxes(slide_xml):
    root = ET.fromstring(slide_xml)
    out = []
    for sp in root.iter():
        tag = sp.tag.split("}")[-1]
        if tag not in ("sp", "pic", "graphicFrame"):
            continue
        xfrm = None
        for e in sp.iter():
            if e.tag.split("}")[-1] == "xfrm":
                xfrm = e
                break
        if xfrm is None:
            continue
        off = xfrm.find("a:off", NS)
        ext = xfrm.find("a:ext", NS)
        if off is None or ext is None:
            continue
        x, y = int(off.get("x")) / EMU, int(off.get("y")) / EMU
        w, h = int(ext.get("cx")) / EMU, int(ext.get("cy")) / EMU

        runs, size, font = [], None, None
        for r in sp.iter():
            if r.tag.split("}")[-1] == "t":
                runs.append(r.text or "")
        for rpr in sp.iter():
            if rpr.tag.split("}")[-1] in ("rPr", "defRPr") and rpr.get("sz"):
                size = int(rpr.get("sz")) / 100.0
                lt = rpr.find("a:latin", NS)
                if lt is not None:
                    font = lt.get("typeface")
                break
        out.append({
            "tag": tag, "x": x, "y": y, "w": w, "h": h,
            "text": "".join(runs), "size": size or 12, "font": font or "Calibri",
        })
    return out


def fill_ratio(b):
    """Estimated fraction of the box the text occupies. >1.0 means probable overflow."""
    text = b["text"].strip()
    if not text:
        return 0.0
    ratio = RATIO.get(b["font"], 0.50)
    char_w = b["size"] * ratio / 72.0          # inches
    line_h = b["size"] * LINE_H / 72.0
    if char_w <= 0 or b["w"] <= 0:
        return 0.0
    chars_per_line = max(1, int(b["w"] / char_w))
    # wrap word-wise, which is what actually happens
    lines, cur = 1, 0
    for word in text.split():
        step = len(word) + (1 if cur else 0)
        if cur + step > chars_per_line and cur:
            lines += 1
            cur = len(word)
        else:
            cur += step
    return (lines * line_h) / b["h"] if b["h"] else 0.0


def overlap(a, b):
    ox = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    oy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    return ox > 0.04 and oy > 0.04


def main(path):
    z = zipfile.ZipFile(path)
    names = sorted(
        (n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
        key=lambda n: int(re.search(r"(\d+)", n.split("/")[-1]).group(1)),
    )
    problems = 0
    for n in names:
        num = int(re.search(r"(\d+)", n.split("/")[-1]).group(1))
        bs = boxes(z.read(n))
        issues = []
        for b in bs:
            is_footer = b["h"] < 0.32 and b["y"] > SLIDE_H - 0.95
            if b["x"] < -0.01 or b["y"] < -0.01 or \
               b["x"] + b["w"] > SLIDE_W + 0.01 or b["y"] + b["h"] > SLIDE_H + 0.01:
                issues.append("OUT OF BOUNDS  %-28s x=%.2f y=%.2f w=%.2f h=%.2f"
                              % (b["text"][:26] or "(" + b["tag"] + ")", b["x"], b["y"], b["w"], b["h"]))
            # A footer is a deliberate edge element -- short, in the bottom band. Flagging it
            # every slide would train you to skip this output, which is how a real margin
            # problem gets missed.
            elif not is_footer and (
                    b["x"] < MIN_MARGIN - 0.01 or b["y"] < MIN_MARGIN - 0.01 or
                    SLIDE_W - (b["x"] + b["w"]) < MIN_MARGIN - 0.01 or
                    SLIDE_H - (b["y"] + b["h"]) < MIN_MARGIN - 0.01):
                if b["text"].strip():
                    issues.append("TIGHT MARGIN   %-28s x=%.2f y=%.2f w=%.2f h=%.2f"
                                  % (b["text"][:26], b["x"], b["y"], b["w"], b["h"]))
            fr = fill_ratio(b)
            if fr > 1.12:
                issues.append("OVERFLOW %3d%%  %-28s (%s %.0fpt, box %.2fx%.2f)"
                              % (fr * 100, b["text"][:26], b["font"], b["size"], b["w"], b["h"]))

        texts = [b for b in bs if b["text"].strip()]
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                if overlap(texts[i], texts[j]):
                    issues.append("TEXT OVERLAP   %-24s <> %s"
                                  % (texts[i]["text"][:22], texts[j]["text"][:22]))
        if issues:
            problems += len(issues)
            print("\nslide %d  (%d shapes)" % (num, len(bs)))
            for it in issues:
                print("   " + it)
        else:
            print("slide %-2d  ok  (%d shapes)" % (num, len(bs)))
    print("\n%s" % ("%d issue(s)" % problems if problems else "No geometry issues."))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
