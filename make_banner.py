from PIL import Image, ImageDraw, ImageFont

S = 2                      # supersample, then downscale for smooth edges
W, H = 1200 * S, 627 * S
BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
MED = "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc"
REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

def font(path, size):
    return ImageFont.truetype(path, size * S)

img = Image.new("RGB", (W, H), "#0B1220")
d = ImageDraw.Draw(img)

def rr(box, fill=None, outline=None, r=16, width=2):
    x0, y0, x1, y1 = [v * S for v in box]
    d.rounded_rectangle((x0, y0, x1, y1), radius=r * S, fill=fill, outline=outline, width=width * S)

def text(xy, s, f, fill, anchor="la"):
    d.text((xy[0] * S, xy[1] * S), s, font=f, fill=fill, anchor=anchor)

def arrow(x0, y0, x1, y1, color="#5B6B82", head=9, width=3):
    d.line((x0 * S, y0 * S, x1 * S, y1 * S), fill=color, width=width * S)
    # arrow head
    import math
    ang = math.atan2(y1 - y0, x1 - x0)
    for sign in (+1, -1):
        a = ang + math.pi - sign * 0.45
        d.line((x1 * S, y1 * S, (x1 + head * math.cos(a)) * S, (y1 + head * math.sin(a)) * S),
               fill=color, width=width * S)

# accent bar + top tag
d.rectangle((0, 0, 14 * S, H), fill="#2DD4BF")
text((60, 42), "OPEN SOURCE  \u00b7  PROTOTYPE", font(BOLD, 15), "#2DD4BF")

# title + subtitle
text((60, 72), "24/7 Maintenance Triage Agent", font(BOLD, 58), "#FFFFFF")
text((60, 150), "Rules first. Local LLM only when needed. Humans in the loop.", font(MED, 25), "#9FB3C8")

# scenario strip
rr((60, 205, 1140, 262), fill="#111B2E", outline="#22314A", r=12, width=1)
text((84, 233), "11:30 PM", font(BOLD, 20), "#F5B942", anchor="lm")
text((190, 233), "\u201cThere is water coming through the ceiling.\u201d", font(MED, 21), "#E6EDF7", anchor="lm")
text((1116, 233), "Nobody is watching the inbox.", font(REG, 19), "#8FA3BC", anchor="rm")

# flow boxes
boxes = [
    ("Tenant message", ["Webhook: email / SMS", "De-duplicated by id"], "#16233A", "#2A3B57", "#E6EDF7"),
    ("Deterministic rules", ["Instant, zero cost", "Handles obvious cases"], "#0F3B3A", "#1F7A72", "#7CF0DF"),
    ("Local LLM", ["Ollama + Qwen2.5 3B", "Only if no rule matches"], "#26214A", "#5B4FC4", "#C9C2FF"),
    ("Actions + state", ["Tenant reply, vendor", "State machine, audit log"], "#12361F", "#2E8B57", "#8CF0B4"),
]
bw, gap, x, y0, y1 = 230, 53, 60, 300, 415
centers = []
for title, lines, fill, outline, accent in boxes:
    rr((x, y0, x + bw, y1), fill=fill, outline=outline, r=16, width=2)
    text((x + bw / 2, y0 + 36), title, font(BOLD, 23), accent, anchor="mm")
    text((x + bw / 2, y0 + 74), lines[0], font(REG, 17), "#C6D2E3", anchor="mm")
    text((x + bw / 2, y0 + 97), lines[1], font(REG, 17), "#C6D2E3", anchor="mm")
    centers.append(x + bw / 2)
    x += bw + gap
for i in range(3):
    x_from = 60 + i * (bw + gap) + bw
    arrow(x_from + 4, 357, x_from + gap - 4, 357, color="#7E90AB")
    if i == 1:
        text(((x_from + 4 + x_from + gap - 4) / 2, 289), "no rule matched",
             font(REG, 14), "#8FA3BC", anchor="mm")

# human escalation bar
hx0, hx1, hy0, hy1 = 343, 1140, 462, 528
rr((hx0, hy0, hx1, hy1), fill="#3A2A0E", outline="#B7791F", r=14, width=2)
text(((hx0 + hx1) / 2, 484), "Human on-call", font(BOLD, 22), "#F5B942", anchor="mm")
text(((hx0 + hx1) / 2, 511), "Emergencies  \u00b7  model failure or low confidence  \u00b7  vendor timeout", font(REG, 16), "#E9D3A6", anchor="mm")
for cx in centers[1:]:
    arrow(cx, hy0 - 2, cx, y1 + 6, color="#B7791F", head=8, width=2)

# left-bottom stat
rr((60, 462, 313, 528), fill="#111B2E", outline="#22314A", r=14, width=1)
text((186, 484), "23 automated tests", font(BOLD, 19), "#FFFFFF", anchor="mm")
text((186, 511), "CI on every push", font(REG, 16), "#8FA3BC", anchor="mm")

# tech chips
chips = ["Python", "FastAPI", "Pydantic", "SQLite", "Ollama", "pytest"]
cx = 60
for c in chips:
    f = font(MED, 17)
    wtxt = d.textlength(c, font=f) / S
    rr((cx, 560, cx + wtxt + 30, 596), fill="#16233A", outline="#2A3B57", r=18, width=1)
    text((cx + 15 + wtxt / 2, 578), c, f, "#C6D2E3", anchor="mm")
    cx += wtxt + 30 + 12

img = img.resize((1200, 627), Image.LANCZOS)
img.save("banner.png", optimize=True)
print("saved", img.size)
