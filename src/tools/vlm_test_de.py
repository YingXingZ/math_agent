import base64, io, json, urllib.request
from PIL import Image, ImageDraw

# A differential equations general-solution page with explicit handwritten formulas.
img = Image.new('RGB', (640, 820), 'white')
d = ImageDraw.Draw(img)
d.text((10, 10), '4.7 (3) Sol:', fill='black')

# 1
d.text((10, 60),  "(1) y' - 4y = e^(2x)", fill='black')
d.text((10, 95),  "   dy/dx - 4y = e^(2x)", fill='black')
d.text((10, 130), "   y = C e^(4x) + (1/2) e^(2x)", fill='black')

# 2
d.text((10, 180), "(2) y' + y = -2x", fill='black')
d.text((10, 215), "   integrating factor e^x", fill='black')
d.text((10, 250), "   y = C e^(-x) - 2x + 2", fill='black')

# 3
d.text((10, 300), "(3) y' + 4y = x cos x", fill='black')
d.text((10, 335), "   particular y_p = (4x cos x + (x^2-1) sin x)/25", fill='black')
d.text((10, 370), "   y = C e^(-4x) + y_p", fill='black')

# 4
d.text((10, 420), "(4) y'' - 2y' + 2y = e^x cos x", fill='black')
d.text((10, 455), "   char r^2 - 2r + 2 = 0;  r = 1 +/- i", fill='black')
d.text((10, 490), "   y_h = e^x (C1 cos x + C2 sin x)", fill='black')
d.text((10, 525), "   y = e^x (C1 cos x + C2 sin x + (1/2) x sin x)", fill='black')

buf = io.BytesIO(); img.save(buf, 'PNG')
b64 = base64.b64encode(buf.getvalue()).decode()

payload = json.dumps({
    "image_base64": b64,
    "problem_text": "4.7 (3): (1) y'-4y=e^(2x); (2) y'+y=-2x; (3) y'+4y=x cos x; (4) y''-2y'+2y=e^x cos x",
    "section_no": "4.7",
    "problem_no": "3",
    "subquestion_count": 4,
}).encode()

req = urllib.request.Request(
    "http://127.0.0.1:18080/review",
    data=payload,
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        body = r.read().decode()
        open("/tmp/vlm_out_de.json", "w").write(body)
        print("STATUS", r.status, "len", len(body))
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:1000])
except Exception as e:
    print("ERR", repr(e))
