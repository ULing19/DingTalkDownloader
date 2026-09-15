"""Bounded preprocessing for small QR codes in sharing screenshots."""

from itertools import combinations


def qr_recovery_variants(image, detector):
    import cv2
    import numpy as np

    h, w = image.shape[:2]
    if max(h, w) > 2200:
        image = cv2.resize(image, None, fx=2200 / max(h, w), fy=2200 / max(h, w))
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    boxes = []
    try:
        detected, points = detector.detectMulti(image)
        if detected and points is not None:
            for corners in points:
                boxes.append(cv2.boundingRect(np.asarray(corners, dtype=np.float32)))
    except Exception:
        pass
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    finders = []
    # Finder squares have three nested contours. Locate them when the decoder
    # cannot detect the whole code, then crop geometrically plausible triples.
    if hierarchy is not None:
        for i, contour in enumerate(contours):
            child = hierarchy[0][i][2]
            if child < 0 or hierarchy[0][child][2] < 0:
                continue
            x, y, bw, bh = cv2.boundingRect(contour)
            if 8 <= min(bw, bh) <= 240 and 0.7 <= bw / bh <= 1.4:
                finders.append((x + bw / 2, y + bh / 2, max(bw, bh)))
    for triple in combinations(sorted(finders, key=lambda f: f[2], reverse=True)[:24], 3):
        sizes = [f[2] for f in triple]
        if max(sizes) > min(sizes) * 1.6:
            continue
        xs, ys = [f[0] for f in triple], [f[1] for f in triple]
        dx, dy, size = max(xs) - min(xs), max(ys) - min(ys), max(sizes)
        if min(dx, dy) < size * 1.5 or not 0.65 < dx / dy < 1.55:
            continue
        boxes.append((int(min(xs) - size), int(min(ys) - size), int(dx + 2 * size), int(dy + 2 * size)))
        if len(boxes) >= 12:
            break
    seen, regions = set(), []
    for x, y, bw, bh in boxes[:12]:
        pad = max(8, int(max(bw, bh) * 0.08))
        bounds = (max(0, x - pad), max(0, y - pad), min(w, x + bw + pad), min(h, y + bh + pad))
        if bounds in seen:
            continue
        seen.add(bounds)
        x1, y1, x2, y2 = bounds
        if x2 > x1 and y2 > y1:
            regions.append(gray[y1:y2, x1:x2])
    regions.append(gray)
    for region in regions:
        scale = min(4.0, max(1.0, 800 / max(region.shape)))
        scaled = cv2.resize(region, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        thresholded = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
        adaptive = cv2.adaptiveThreshold(scaled, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5)
        for variant in (scaled, thresholded, adaptive):
            yield cv2.copyMakeBorder(variant, 32, 32, 32, 32, cv2.BORDER_CONSTANT, value=255)
