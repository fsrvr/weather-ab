import os, io, json, math, time, random, webbrowser, queue, threading, datetime as dt
import tkinter as tk
import requests, socket
import urllib3.util.connection as _uc
_uc.allowed_gai_family = lambda: socket.AF_INET
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageTk, ImageDraw

GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
EONET = "https://eonet.gsfc.nasa.gov/api/v3/events"
NWS = "https://api.weather.gov/alerts/active"
NWS_EVENTS = "Tornado Warning,Extreme Wind Warning,Flash Flood Emergency,Hurricane Warning,Storm Surge Warning"
HDR = {"User-Agent": "WeatherAbnormalityTracker/4.0 (github project)"}
RESAMPLE = Image.Resampling.BILINEAR
SES = requests.Session()
SES.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=32))
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map")
import calendar

ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/"
IMAGERY = ESRI + "World_Imagery/MapServer/tile/{z}/{y}/{x}"
LABELS = ESRI + "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
LABEL_ALPHA, LABEL_DARKEN = 0.5, 0.55

BG, SURF, SURF2, BORDER = "#141210", "#1d1a18", "#2a2622", "#38322c"
TEXT, MUTED, ACCENT, RED = "#f0ece4", "#9a948a", "#d97757", "#e5534b"
SANS, SERIF = "Sans", "Serif"
SEV = {2: ("#e8873a", "WARNING"), 3: ("#e5534b", "EXTREME")}

UIQ = queue.Queue()
def ui(fn): UIQ.put(fn)

EVF = os.path.join(CACHE, "events.json")
def ev_load(k, ttl):
    try:
        e = json.load(open(EVF)).get(k)
        if e and time.time() - e["t"] < ttl:
            return e["ev"]
    except Exception:
        pass
def ev_save(k, ev):
    try:
        try: d = json.load(open(EVF))
        except Exception: d = {}
        d[k] = {"t": time.time(), "ev": ev}
        os.makedirs(CACHE, exist_ok=True)
        json.dump(d, open(EVF, "w"))
    except Exception:
        pass

NWS_INFO = {
    "Tornado Warning": "A tornado is occurring or imminent. Shelter now in an interior room on the lowest floor.",
    "Extreme Wind Warning": "Sustained winds of 185+ km/h expected, usually a hurricane eyewall. Shelter immediately.",
    "Flash Flood Emergency": "Catastrophic flash flooding is underway. Move to higher ground now.",
    "Hurricane Warning": "Hurricane conditions expected within 36 hours. Finish preparations and follow evacuation orders.",
    "Storm Surge Warning": "Life-threatening flooding from rising seawater expected near the coast.",
}
def cat(k):
    for lim, n in ((252, "Category 5 hurricane"), (209, "Category 4 hurricane"), (178, "Category 3 hurricane"),
                   (154, "Category 2 hurricane"), (119, "Category 1 hurricane"), (63, "Tropical storm")):
        if k >= lim:
            return n
    return "Tropical depression"


# ---------------- extreme event sources ----------------
def hav(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def project(track, steps=4):
    if len(track) < 2:
        return []
    (a1, o1), (a2, o2) = track[-2], track[-1]
    dla, dlo = a2 - a1, o2 - o1
    if abs(dlo) > 40 or abs(dla) > 40 or (dla == 0 and dlo == 0):
        return []
    return [(max(-85, min(85, a2 + dla * k)), o2 + dlo * k) for k in range(1, steps + 1)]


def get_eonet(y, m, live, whole=False):
    if whole:
        p = {"status": "all", "start": f"{y}-01-01", "end": f"{y}-12-31",
             "category": "severeStorms,floods"}
    elif live:
        p = {"status": "open", "days": 20, "category": "severeStorms,floods"}
    else:
        last = calendar.monthrange(y, m)[1]
        p = {"status": "all", "start": f"{y}-{m:02d}-01", "end": f"{y}-{m:02d}-{last}",
             "category": "severeStorms,floods"}
    for i in range(3):
        try:
            r = (SES if i == 0 else requests).get(EONET, params=p, headers=HDR, timeout=25)
            r.raise_for_status()
            break
        except requests.RequestException:
            if i == 2: raise
            time.sleep(1.5)
    out = []
    for ev in r.json().get("events", []):
        pts = [g for g in ev.get("geometry", []) if g.get("type") == "Point"]
        if not pts:
            continue
        pts.sort(key=lambda g: g.get("date", ""))
        g = pts[-1]
        lon, lat = g["coordinates"][:2]
        flood = ev["categories"][0]["id"] == "floods"
        mag = g.get("magnitudeValue")
        unit = (g.get("magnitudeUnit") or "").lower()
        kmh = (mag * 1.852 if "kt" in unit else mag * 1.609 if "mph" in unit else mag) if mag else None
        day = g.get("date", "")[:10]
        if flood:
            desc = f"Flooding reported, last update {day}. Rivers or coasts are overflowing; follow local authorities."
        elif kmh:
            desc = f"{cat(kmh)} with sustained winds near {kmh:.0f} km/h. Last fix {day}; dashed line is the projected path."
        else:
            desc = f"Active storm system, last fix {day}. Dashed line is the projected path."
        track = [(q["coordinates"][1], q["coordinates"][0]) for q in pts]
        out.append({"lat": lat, "lon": lon, "sev": 2 if flood else 3, "title": ev["title"],
                    "desc": desc, "kind": "Flood" if flood else "Storm",
                    "radius": 70 if flood else 120 + (mag or 60) * 2,
                    "track": track, "fc": [] if flood else project(track)})
    return out


def get_nws():
    r = SES.get(NWS, params={"event": NWS_EVENTS}, headers=HDR, timeout=25)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        geom, pr = f.get("geometry"), f["properties"]
        if not geom:
            continue
        try:
            ring = geom["coordinates"][0] if geom["type"] == "Polygon" else geom["coordinates"][0][0]
            lon = sum(c[0] for c in ring) / len(ring)
            lat = sum(c[1] for c in ring) / len(ring)
            rad = max(hav(lat, lon, c[1], c[0]) for c in ring)
        except Exception:
            continue
        ev = pr["event"]
        out.append({"lat": lat, "lon": lon, "sev": 3 if ev != "Flash Flood Emergency" else 2,
                    "title": ev, "desc": (NWS_INFO.get(ev, "") + " Area: " + (pr.get("areaDesc") or "")[:100]).strip(), "kind": "Alert",
                    "radius": max(10, rad), "track": [], "fc": []})
    return out


def get_winds(points, date, hour):
    today = dt.date.today()
    urls = [FORECAST, ARCHIVE] if date >= today - dt.timedelta(days=80) else [ARCHIVE, FORECAST]
    params = {"latitude": ",".join(f"{p[0]:.3f}" for p in points),
              "longitude": ",".join(f"{p[1]:.3f}" for p in points),
              "hourly": "wind_gusts_10m,precipitation", "start_date": date.isoformat(),
              "end_date": date.isoformat(), "timezone": "GMT"}
    for url in urls:
        try:
            r = SES.get(url, params=params, headers=HDR, timeout=30)
            data = r.json()
            if not r.ok:
                continue
            data = data if isinstance(data, list) else [data]
            out = []
            for (la, lo), d in zip(points, data):
                h = d["hourly"]
                gust = (h.get("wind_gusts_10m") or [None] * 24)[hour]
                rain = (h.get("precipitation") or [None] * 24)[hour]
                if gust is not None and gust >= 100:
                    out.append({"lat": la, "lon": lo, "sev": 3 if gust >= 130 else 2,
                                "title": "Violent winds", "kind": "Wind", "radius": 90,
                                "desc": f"Gusts up to {gust:.0f} km/h, {'hurricane-force' if gust >= 118 else 'damaging'}. Trees, roofs and power lines at risk.", "track": [], "fc": []})
                if rain is not None and rain >= 30:
                    out.append({"lat": la, "lon": lo, "sev": 3 if rain >= 50 else 2,
                                "title": "Extreme rainfall", "kind": "Flood", "radius": 60,
                                "desc": f"{rain:.0f} mm of rain in one hour. Flash flooding likely; streets and low-lying areas at risk.",
                                "track": [], "fc": []})
            return out
        except Exception:
            continue
    return []


KINDS = ["All", "Tornado", "Hurricane", "Blizzard", "Flood", "Heat", "Cold", "Wind"]
KIND_COL = {"Tornado": "#e5534b", "Hurricane": "#4fa8e8", "Blizzard": "#a5d8ff",
            "Flood": "#6f86f5", "Heat": "#f0a030", "Cold": "#6fd3d9", "Wind": "#c58af0"}


def E(name, kind, ym, disp, lat, lon, radius, tag, desc, facts, track=None):
    return {"db": True, "title": name, "kind": kind, "year": int(ym[:4]), "month": int(ym[5:7]),
            "lat": lat, "lon": lon, "radius": radius, "tag": tag, "sev": 3,
            "desc": f"{disp} · {desc}", "facts": facts, "track": track or [], "fc": []}


LEGENDS = [
    E("Bridge Creek-Moore Tornado", "Tornado", "1999-05", "3 May 1999", 35.33, -97.60, 8, "F5",
      "The strongest winds ever measured on Earth: about 484 km/h.",
      "36 deaths · ~$1B damage · measured by Doppler on Wheels",
      [(35.28, -97.84), (35.33, -97.60), (35.37, -97.43)]),
    E("Joplin Tornado", "Tornado", "2011-05", "22 May 2011", 37.084, -94.513, 9, "EF5",
      "A mile-wide EF5 that cut through the middle of the city.",
      "158 deaths · deadliest US tornado since 1947 · ~$2.8B damage",
      [(37.06, -94.62), (37.08, -94.51), (37.10, -94.35)]),
    E("Tri-State Tornado", "Tornado", "1925-03", "18 March 1925", 37.77, -89.33, 25, "F5",
      "Stayed on the ground for about 3.5 hours across three states.",
      "695 deaths · deadliest US tornado ever · 350 km track",
      [(37.23, -90.97), (37.50, -90.00), (37.77, -89.33), (38.00, -88.50), (38.35, -87.57)]),
    E("El Reno Tornado", "Tornado", "2013-05", "31 May 2013", 35.53, -98.10, 8, "EF3",
      "The widest tornado ever recorded, with a 4.2 km wide circulation.",
      "8 deaths · including veteran storm chasers"),
    E("Daulatpur-Saturia Tornado", "Tornado", "1989-04", "26 April 1989", 23.90, 90.00, 8, "Deadliest",
      "The deadliest tornado in recorded history.",
      "~1,300 deaths · ~12,000 injured · Bangladesh"),
    E("1974 Super Outbreak", "Tornado", "1974-04", "3-4 April 1974", 39.678, -83.93, 400, "148 tornadoes",
      "148 tornadoes in under 24 hours across 13 states, 7 of them F5.",
      "315 deaths · Xenia, Ohio was devastated by an F5"),
    E("2011 Super Outbreak", "Tornado", "2011-04", "25-28 April 2011", 33.21, -87.57, 450, "4x EF5",
      "The largest tornado outbreak ever recorded, with four EF5 tornadoes.",
      "Over 340 tornadoes in 4 days · 324 deaths"),

    E("Hurricane Katrina", "Hurricane", "2005-08", "29 August 2005", 29.3, -89.6, 400, "Cat 5 peak",
      "Peaked at Category 5 in the Gulf before flooding New Orleans.",
      "~1,400 deaths · ~$125B damage · 280 km/h peak winds",
      [(23.2, -75.5), (25.9, -80.1), (25.4, -82.0), (26.3, -88.6), (29.3, -89.6), (31.1, -89.6), (35.0, -88.0)]),
    E("Typhoon Haiyan", "Hurricane", "2013-11", "8 November 2013", 11.2, 125.0, 300, "315 km/h",
      "One of the strongest tropical cyclones at landfall ever recorded.",
      "~6,300 deaths · 5-6 m storm surge · Philippines",
      [(6.9, 152.3), (7.5, 142.0), (8.0, 134.0), (10.5, 127.5), (11.2, 125.0), (11.7, 121.0), (12.5, 115.5), (16.0, 110.5), (21.0, 107.5)]),
    E("Typhoon Tip", "Hurricane", "1979-10", "12 October 1979", 16.7, 137.5, 1085, "870 hPa",
      "The largest tropical cyclone ever, 2,170 km across, with the lowest pressure ever measured.",
      "305 km/h peak winds · 2,170 km diameter",
      [(10.5, 150.0), (14.0, 142.0), (16.7, 137.5), (21.0, 134.0), (28.0, 136.0), (34.6, 138.5)]),
    E("Hurricane Patricia", "Hurricane", "2015-10", "23 October 2015", 19.2, -104.9, 150, "345 km/h",
      "The strongest hurricane ever recorded in the Western Hemisphere.",
      "872 hPa · explosive intensification in under 24 hours",
      [(13.4, -105.2), (16.0, -105.9), (18.0, -105.6), (19.2, -104.9), (22.0, -103.2)]),
    E("Hurricane Sandy", "Hurricane", "2012-10", "29 October 2012", 39.4, -74.4, 900, "1,800 km wide",
      "The largest Atlantic hurricane by wind diameter at the time.",
      "233 deaths · ~$65B damage · hit New Jersey and New York",
      [(14.0, -77.5), (18.0, -76.5), (23.5, -75.5), (29.0, -76.0), (35.0, -73.5), (39.4, -74.4), (41.0, -77.0)]),
    E("Hurricane Dorian", "Hurricane", "2019-09", "1 September 2019", 26.5, -77.0, 200, "295 km/h",
      "Stalled over the Bahamas as a Category 5 for about a day.",
      "Among the strongest Atlantic landfalls ever · ~$3.4B damage",
      [(13.5, -55.0), (18.0, -65.0), (24.0, -72.0), (26.5, -77.0), (27.5, -79.0), (32.0, -79.5), (35.2, -75.7), (44.5, -63.5)]),
    E("Cyclone Idai", "Hurricane", "2019-03", "14 March 2019", -19.83, 34.84, 300, "Intense cyclone",
      "Flooded an area the size of a small country around Beira.",
      "1,300+ deaths · among the worst Southern Hemisphere cyclones"),
    E("Cyclone Nargis", "Hurricane", "2008-05", "2 May 2008", 16.0, 95.0, 200, "Cat 4",
      "A storm surge swept across the Irrawaddy Delta.",
      "~138,000 deaths · Myanmar",
      [(12.0, 86.5), (14.0, 87.5), (15.5, 89.5), (16.0, 92.0), (16.0, 95.0), (17.5, 96.5)]),
    E("Bhola Cyclone", "Hurricane", "1970-11", "12 November 1970", 22.0, 90.5, 250, "Cat 3",
      "The deadliest tropical cyclone ever recorded.",
      "300,000-500,000 deaths · East Pakistan, now Bangladesh",
      [(10.0, 88.0), (14.0, 87.5), (18.0, 88.5), (22.0, 90.5)]),
    E("Great Galveston Hurricane", "Hurricane", "1900-09", "8 September 1900", 29.3, -94.8, 150, "Cat 4",
      "The deadliest natural disaster in US history.",
      "8,000-12,000 deaths · the island city was nearly wiped out"),

    E("Great Blizzard of 1888", "Blizzard", "1888-03", "11-14 March 1888", 40.71, -74.0, 400, "Historic",
      "Paralyzed the northeastern US with huge drifts and bitter wind.",
      "400+ deaths · up to 150 cm of snow · prompted NYC subways"),
    E("Storm of the Century", "Blizzard", "1993-03", "12-14 March 1993", 35.0, -83.0, 800, "Superstorm",
      "Hit everywhere from Cuba to Canada with snow, tornadoes and storm surge.",
      "310 deaths · hurricane-force gusts · ~$5.5B damage"),
    E("Iran Blizzard", "Blizzard", "1972-02", "February 1972", 32.31, 54.02, 350, "Deadliest",
      "A week-long blizzard that buried villages under up to 8 m of snow.",
      "~4,000 deaths · deadliest blizzard in history"),
    E("Buffalo Blizzard", "Blizzard", "1977-01", "28 Jan - 1 Feb 1977", 42.89, -78.88, 150, "Whiteout",
      "Old lake snow plus gale winds created days of total whiteout.",
      "Gusts over 100 km/h · snow drifts over rooftops"),

    E("Winter Storm Uri", "Cold", "2021-02", "13-17 February 2021", 31.0, -99.0, 600, "Deep freeze",
      "Record cold froze the Texas power grid.",
      "4.5 million homes lost power · 246 deaths"),
    E("Vostok Station Record", "Cold", "1983-07", "21 July 1983", -78.46, 106.84, 60, "-89.2 C",
      "The coldest air temperature ever recorded on Earth.",
      "-89.2 C · Antarctica"),

    E("Death Valley Record", "Heat", "1913-07", "10 July 1913", 36.46, -116.87, 60, "56.7 C",
      "The hottest air temperature officially recorded on Earth.",
      "56.7 C (134 F) · Furnace Creek, California"),
    E("European Heatwave", "Heat", "2003-08", "August 2003", 46.5, 2.5, 800, "Heatwave",
      "One of the deadliest heatwaves in modern European history.",
      "~70,000 deaths across Europe"),
    E("Lytton Heat Dome", "Heat", "2021-06", "29 June 2021", 50.23, -121.58, 150, "49.6 C",
      "Set Canada's all-time heat record, then a wildfire destroyed the town.",
      "49.6 C · Pacific Northwest heat dome killed ~800"),

    E("Zhengzhou Floods", "Flood", "2021-07", "20 July 2021", 34.75, 113.65, 120, "201.9 mm/h",
      "A record hourly downpour flooded the city and its subway.",
      "398 deaths · ~$18B damage · Henan, China"),
    E("Hurricane Harvey Flooding", "Flood", "2017-08", "25-30 August 2017", 29.76, -95.37, 300, "1,539 mm",
      "Stalled over Texas and dropped the most rain from a US storm.",
      "Up to 1,539 mm of rain · ~$125B damage"),
    E("1931 China Floods", "Flood", "1931-08", "Summer 1931", 30.6, 114.3, 500, "Deadliest",
      "The Yangtze, Huai and Yellow rivers all flooded together.",
      "Estimated 1-4 million deaths"),
    E("Pakistan Floods", "Flood", "2022-08", "Summer 2022", 26.5, 68.0, 500, "33M affected",
      "Monsoon rain put about a third of the country underwater.",
      "~1,700 deaths · 33 million people affected"),

    E("Mount Washington Gust", "Wind", "1934-04", "12 April 1934", 44.27, -71.30, 40, "372 km/h",
      "The strongest surface wind recorded outside a tropical cyclone.",
      "372 km/h (231 mph) · New Hampshire"),
    E("Cyclone Olivia Gust", "Wind", "1996-04", "10 April 1996", -20.8, 115.4, 80, "408 km/h",
      "The fastest wind gust ever measured at the surface.",
      "408 km/h (253 mph) · Barrow Island, Australia"),
    E("Black Sunday Dust Storm", "Wind", "1935-04", "14 April 1935", 36.5, -101.0, 800, "Dust storm",
      "A wall of dust turned day into night across the Great Plains.",
      "Dust Bowl's worst day · millions of tonnes of topsoil"),
]


LEGENDS += [
    E("Jarrell Tornado", "Tornado", "1997-05", "27 May 1997", 30.82, -97.60, 5, "F5",
      "Moved slowly and scoured the ground bare in a Texas neighborhood.",
      "27 deaths · one of the most violent tornadoes on record"),
    E("Hackleburg Tornado", "Tornado", "2011-04", "27 April 2011", 34.25, -87.84, 8, "EF5",
      "Part of the 2011 Super Outbreak, destroyed most of Hackleburg, Alabama.",
      "72 deaths · over 200 km track"),
    E("Greensburg Tornado", "Tornado", "2007-05", "4 May 2007", 37.60, -99.29, 8, "EF5",
      "A 2.7 km wide EF5 that destroyed about 95% of the town.",
      "11 deaths · Kansas · town rebuilt as a green community"),
    E("Moore Tornado", "Tornado", "2013-05", "20 May 2013", 35.33, -97.49, 7, "EF5",
      "A violent EF5 hit the same Oklahoma city as the 1999 tornado.",
      "24 deaths · ~$2B damage"),
    E("Quad-State Tornado", "Tornado", "2021-12", "10 December 2021", 36.74, -88.64, 6, "EF4",
      "A rare December tornado stayed on the ground across four states.",
      "57 deaths · about 266 km track · Mayfield, Kentucky was devastated",
      [(36.45, -89.5), (36.74, -88.64), (37.2, -87.6), (37.4, -87.0)]),

    E("Hurricane Andrew", "Hurricane", "1992-08", "24 August 1992", 25.5, -80.3, 120, "Cat 5",
      "A compact Category 5 that devastated southern Florida.",
      "65 deaths · ~$27B damage · 266 km/h at landfall",
      [(24.0, -69.0), (25.0, -76.0), (25.5, -80.3), (25.9, -88.0), (29.5, -91.5)]),
    E("Hurricane Wilma", "Hurricane", "2005-10", "19 October 2005", 16.7, -82.8, 200, "882 hPa",
      "Lowest pressure ever measured in an Atlantic hurricane.",
      "Went from tropical storm to Cat 5 in about a day · 295 km/h"),
    E("Hurricane Maria", "Hurricane", "2017-09", "20 September 2017", 18.0, -65.9, 180, "Cat 4",
      "Wrecked Puerto Rico and its entire power grid.",
      "~2,975 deaths · ~$90B damage",
      [(14.5, -58.0), (15.3, -61.3), (17.0, -64.5), (18.0, -65.9), (19.5, -67.5), (23.0, -70.0)]),
    E("Typhoon Yutu", "Hurricane", "2018-10", "25 October 2018", 15.0, 145.6, 180, "285 km/h",
      "The strongest storm of 2018 hit the island of Tinian at Cat 5 strength.",
      "Among the strongest landfalls ever recorded in US territory"),
    E("Hurricane Ian", "Hurricane", "2022-09", "28 September 2022", 26.7, -82.2, 250, "Cat 4",
      "Huge storm surge hit southwest Florida.",
      "~150 deaths · ~$113B damage · 241 km/h landfall",
      [(16.0, -80.0), (21.0, -83.0), (24.5, -83.0), (26.7, -82.2), (28.5, -81.5), (32.5, -79.5)]),
    E("Hurricane Milton", "Hurricane", "2024-10", "8 October 2024", 27.2, -82.6, 220, "285 km/h peak",
      "Rapidly intensified to Category 5 in the Gulf before hitting Florida.",
      "Peak winds 285 km/h · tornado outbreak ahead of landfall",
      [(21.5, -92.0), (22.5, -90.0), (25.0, -86.0), (27.2, -82.6), (28.2, -80.5)]),
    E("Cyclone Tracy", "Hurricane", "1974-12", "25 December 1974", -12.46, 130.84, 60, "Cat 4",
      "One of the smallest cyclones ever recorded flattened Darwin on Christmas Day.",
      "71 deaths · ~70% of Darwin buildings destroyed"),
    E("Cyclone Mocha", "Hurricane", "2023-05", "14 May 2023", 20.1, 93.0, 200, "Cat 5 peak",
      "A powerful cyclone struck Myanmar and Bangladesh.",
      "Peak winds around 280 km/h · hit Rakhine State"),

    E("Children's Blizzard", "Blizzard", "1888-01", "12 January 1888", 43.5, -98.0, 400, "Deadly",
      "A sudden blizzard hit schoolchildren walking home on the Great Plains.",
      "~235 deaths · temperatures fell by 20 C in hours"),
    E("Blizzard of 1978", "Blizzard", "1978-02", "5-7 February 1978", 41.8, -71.4, 300, "Historic",
      "Paralyzed southern New England for days.",
      "~100 deaths · more than 1 m of snow · thousands of cars stranded"),
    E("Blizzard of 1996", "Blizzard", "1996-01", "6-8 January 1996", 40.0, -76.0, 600, "Major",
      "Buried the US East Coast from Virginia to Boston.",
      "~150 deaths · up to 120 cm of snow in places"),
    E("Mount Baker Snow Record", "Blizzard", "1999-02", "Winter 1998-99", 48.86, -121.68, 40, "29 m",
      "The most snow ever measured in a single season.",
      "28.96 m (1,140 in) · Mount Baker, Washington"),
    E("Buffalo Christmas Blizzard", "Blizzard", "2022-12", "23-26 December 2022", 42.9, -78.9, 200, "Whiteout",
      "Hurricane-force winds and lake snow trapped people in cars and homes for days.",
      "Dozens of deaths · New York"),

    E("Great Flood of 1993", "Flood", "1993-07", "Summer 1993", 39.5, -91.0, 450, "$15B+",
      "The Mississippi and Missouri rivers overflowed for months.",
      "~50 deaths · about 70,000 km2 flooded"),
    E("Banqiao Dam Failure", "Flood", "1975-08", "8 August 1975", 33.0, 113.5, 120, "62 dams failed",
      "Typhoon Nina rain broke dozens of dams in Henan.",
      "Estimates range from 26,000 to 230,000 deaths"),
    E("Derna Flood", "Flood", "2023-09", "10 September 2023", 32.76, 22.64, 40, "Dam collapse",
      "Storm Daniel rain broke two dams and washed through the city.",
      "Thousands dead · Libya"),
    E("Rio Grande do Sul Floods", "Flood", "2024-05", "May 2024", -30.0, -51.2, 300, "Historic",
      "The worst flooding in the state's history, including most of Porto Alegre.",
      "~180 deaths · over 2 million people affected"),
    E("Valencia Floods", "Flood", "2024-10", "29 October 2024", 39.4, -0.4, 90, "DANA",
      "Around a year of rain fell in hours in parts of eastern Spain.",
      "~230 deaths · Spain's deadliest flood in decades"),

    E("Russian Heatwave", "Heat", "2010-07", "July-August 2010", 55.75, 37.6, 700, "38 C Moscow",
      "Moscow set records and wildfires burned across western Russia.",
      "~55,000 deaths · wheat export ban"),
    E("Black Saturday Heat", "Heat", "2009-02", "7 February 2009", -37.5, 145.0, 250, "46.4 C",
      "Record heat and strong winds drove deadly bushfires in Victoria.",
      "173 deaths · 46.4 C in Melbourne"),
    E("Verkhoyansk Arctic Heat", "Heat", "2020-06", "20 June 2020", 67.55, 133.4, 150, "38 C",
      "A record temperature north of the Arctic Circle.",
      "38 C · Siberia"),
    E("Oymyakon Cold Record", "Cold", "1933-02", "6 February 1933", 63.46, 142.79, 60, "-67.7 C",
      "The coldest recorded temperature in a permanently inhabited place.",
      "-67.7 C · Siberia"),

    E("Great Storm of 1987", "Wind", "1987-10", "16 October 1987", 51.0, 0.5, 350, "220 km/h",
      "Hurricane-force winds hit southern England and flattened millions of trees.",
      "22 deaths · ~15 million trees lost"),
    E("Iowa Derecho", "Wind", "2020-08", "10 August 2020", 41.9, -93.6, 300, "225 km/h",
      "A derecho swept across the Midwest with hurricane-force gusts.",
      "~$11B damage · millions of acres of crops flattened"),
]


TOP = {"Bridge Creek-Moore Tornado", "Tri-State Tornado", "El Reno Tornado",
       "Daulatpur-Saturia Tornado", "2011 Super Outbreak", "Joplin Tornado", "Typhoon Tip",
       "Hurricane Patricia", "Typhoon Haiyan", "Hurricane Katrina", "Bhola Cyclone",
       "Great Galveston Hurricane", "Hurricane Wilma", "Cyclone Nargis", "Great Blizzard of 1888",
       "Iran Blizzard", "Storm of the Century", "Mount Baker Snow Record", "1931 China Floods",
       "Banqiao Dam Failure", "Zhengzhou Floods", "Hurricane Harvey Flooding", "Pakistan Floods",
       "Death Valley Record", "Verkhoyansk Arctic Heat", "Vostok Station Record",
       "Oymyakon Cold Record", "Mount Washington Gust", "Cyclone Olivia Gust",
       "Black Sunday Dust Storm"}
LEGENDS = [e for e in LEGENDS if e["title"] in TOP]

# name: ((south, west, north, east), (center lat, center lon), zoom)
CONT = {
    "Worldwide": None,
    "Africa": ((-35, -18, 37, 52), (3, 20), 3),
    "Asia": ((1, 26, 77, 180), (34, 90), 3),
    "Europe": ((35, -25, 72, 40), (54, 15), 3),
    "North America": ((7, -170, 84, -50), (45, -100), 3),
    "South America": ((-56, -82, 13, -34), (-15, -60), 3),
    "Oceania": ((-48, 110, 0, 180), (-25, 140), 3),
    "Antarctica": ((-90, -180, -60, 180), (-80, 0), 2),
}


def in_cont(ev, name):
    c = CONT.get(name)
    if not c:
        return True
    s_, w_, n_, e_ = c[0]
    return s_ <= ev["lat"] <= n_ and w_ <= ev["lon"] <= e_


TUTORIAL = [
    ("Welcome", "This app maps extreme weather on a satellite map. Quick 30 second tour."),
    ("Live tab", "Spin the month and year wheel. The current month shows live storms, floods, "
                 "US tornado warnings and 100+ km/h winds."),
    ("Archive tab", "About 30 of the most extreme events ever recorded. Filter by type, "
                    "or press Surprise me."),
    ("Continent", "Pick a continent to fly there and only show events inside it."),
    ("The map", "Drag to pan, scroll to zoom at the cursor. Click a circle to see its size, "
                "track and facts."),
    ("Event cards", "Click a card on the left to fly to that event. Esc deselects."),
    ("Shortcuts", "Ctrl+F searches a city, Ctrl+Q quits, F1 opens usage. Reopen this tour "
                  "anytime from Help > Tutorial."),
]


USAGE = """EXTREME WEATHER TRACKER

LIVE TAB
Pick a month and year with the wheel. The current month is live: active storms, floods,
US tornado warnings and 100+ km/h wind spots from the visible map (or the chosen continent).
Tick "Search the whole year" to search every month of that year.
Past months show NASA-tracked storms and floods.

ARCHIVE TAB
About 30 of the most extreme weather events ever recorded, stored locally.
Filter by type and by continent.

CONTINENT
Choose a continent to fly there and only show events inside it.

MAP
Drag to pan, scroll to zoom at the mouse. Click an event to see its size, track and facts.
Click empty map or press Esc to deselect.

SHORTCUTS
Ctrl+F  search a city     Ctrl+Q  quit     Esc  deselect
"""


def style(it):
    color, tag = SEV[it["sev"]]
    if it.get("db"):
        color, tag = KIND_COL.get(it["kind"], color), it["tag"]
    return color, tag


# ---------------- map ----------------
class TileMap(tk.Canvas):
    MINZ, MAXZ = 2, 18

    def __init__(self, master, on_view, on_pick, on_error):
        super().__init__(master, bg=BG, highlightthickness=0, cursor="fleur")
        self.on_view, self.on_pick, self.on_error = on_view, on_pick, on_error
        self.lat, self.lon, self.fz, self.tz = 25.0, 10.0, 2.0, 2.0
        self.pil, self.tkc, self.scaled, self.ovl, self.pending = {}, {}, {}, {}, set()
        self.marks, self.sel, self.anim = [], None, None
        self.phase, self._sel_geom, self.path_px, self.path_cum = 0, None, [], []
        self.pool = ThreadPoolExecutor(max_workers=16)
        self.ppool = ThreadPoolExecutor(max_workers=4)
        self.lpool = ThreadPoolExecutor(max_workers=16)
        self.vel, self.bgq = (0.0, 0.0), set()
        self.fg, self.bg, self.loading = [], [], set()
        self.qcv = threading.Condition()
        for _ in range(24):
            threading.Thread(target=self._worker, daemon=True).start()
        self.after(300, self.preload_world)
        self._rd, self._err = None, False
        self._ftk = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._hover)
        self._cur = "fleur"
        self.bind("<Button-4>", lambda e: self.zoom_at(1, e.x, e.y))
        self.bind("<Button-5>", lambda e: self.zoom_at(-1, e.x, e.y))
        self.bind("<MouseWheel>", lambda e: self.zoom_at(1 if e.delta > 0 else -1, e.x, e.y))
        self.after(33, self._tick)

    @property
    def zoom(self):
        return self.fz

    @staticmethod
    def world(lat, lon, z):
        n = 256 * 2 ** z
        lat = max(-85.05, min(85.05, lat))
        return ((lon + 180) / 360 * n,
                (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)

    @staticmethod
    def unworld(x, y, z):
        n = 256 * 2 ** z
        return (math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))),
                x / n * 360 - 180)

    # ----- animation -----
    def start_anim(self, spec, ms, notify=True):
        self.anim = {"s": spec, "t0": time.time(), "ms": ms, "notify": notify}

    def zoom_at(self, d, ex, ey):
        nz = max(self.MINZ, min(self.MAXZ, round(self.tz) + d))
        if nz == self.tz:
            return
        self.tz = nz
        self.warm(ex, ey, nz)
        w, h = self.winfo_width(), self.winfo_height()
        cx, cy = self.world(self.lat, self.lon, self.fz)
        plat, plon = self.unworld(cx - w / 2 + ex, cy - h / 2 + ey, self.fz)
        self.start_anim({"k": "anchor", "z0": self.fz, "z1": nz, "plat": plat,
                         "plon": plon, "ex": ex, "ey": ey}, 320)

    def fly_to(self, lat, lon, zoom, ms=1100, notify=False):
        zoom = max(self.MINZ, min(self.MAXZ, zoom))
        self.tz = zoom
        a, b = self.world(self.lat, self.lon, 0), self.world(lat, lon, 0)
        hop = min(2.5, math.hypot(b[0] - a[0], b[1] - a[1]) / 256 * 6)
        self.start_anim({"k": "fly", "z0": self.fz, "z1": zoom, "a": a, "b": b, "hop": hop}, ms, notify)

    def _step(self):
        a = self.anim
        t = min(1.0, (time.time() - a["t0"]) * 1000 / a["ms"])
        s = a["s"]
        if s["k"] == "anchor":
            e = 1 - (1 - t) ** 3
            self.fz = s["z0"] + (s["z1"] - s["z0"]) * e if t < 1 else s["z1"]
            w, h = self.winfo_width(), self.winfo_height()
            px, py = self.world(s["plat"], s["plon"], self.fz)
            self.lat, self.lon = self.unworld(px - s["ex"] + w / 2, py - s["ey"] + h / 2, self.fz)
        else:
            e = t * t * (3 - 2 * t)
            cx = s["a"][0] + (s["b"][0] - s["a"][0]) * e
            cy = s["a"][1] + (s["b"][1] - s["a"][1]) * e
            self.lat, self.lon = self.unworld(cx, cy, 0)
            self.fz = (s["z0"] + (s["z1"] - s["z0"]) * e - s["hop"] * math.sin(math.pi * t)
                       if t < 1 else s["z1"])
            self.fz = max(self.MINZ, self.fz)
        self.lat = max(-85, min(85, self.lat))
        self.lon = (self.lon + 180) % 360 - 180
        self.redraw()
        if t >= 1:
            self.anim = None
            self.scaled.clear()
            self.redraw()
            if a["notify"]:
                self.on_view()

    def _tick(self):
        try:
            if self.anim:
                self._step()
            else:
                self.phase += 1
                self._pulse()
        finally:
            self.after(8 if self.anim else 33, self._tick)

    def _pulse(self):
        g = self._sel_geom
        if not g:
            return
        mx, my, r, c = g
        p = (self.phase % 45) / 45
        rr = r + p * 34
        self.coords("pulse", mx - rr, my - rr, mx + rr, my + rr)
        self.itemconfigure("pulse", width=max(1, 3 * (1 - p)))
        if len(self.path_px) > 1:
            tot = self.path_cum[-1]
            tgt = (self.phase % 110) / 110 * tot
            for i in range(1, len(self.path_cum)):
                if self.path_cum[i] >= tgt:
                    seg = self.path_cum[i] - self.path_cum[i - 1] or 1
                    f = (tgt - self.path_cum[i - 1]) / seg
                    (x1, y1), (x2, y2) = self.path_px[i - 1], self.path_px[i]
                    x, y = x1 + (x2 - x1) * f, y1 + (y2 - y1) * f
                    self.coords("mover", x - 4, y - 4, x + 4, y + 4)
                    break

    # ----- view -----
    def bounds(self):
        w, h = self.winfo_width(), self.winfo_height()
        cx, cy = self.world(self.lat, self.lon, self.fz)
        n, west = self.unworld(cx - w / 2, cy - h / 2, self.fz)
        s, east = self.unworld(cx + w / 2, cy + h / 2, self.fz)
        if east - west >= 340:
            west, east = -175, 175
        return max(s, -80), west, min(n, 80), east

    def set_marks(self, marks):
        self.marks, self.sel = marks, None
        self.redraw()

    def add_mark(self, m):
        self.marks.append(m)

    def select(self, i):
        self.sel = i
        self.redraw()

    def px_radius(self, m):
        mpp = 156543.03392 * max(0.05, math.cos(math.radians(m["lat"]))) / 2 ** self.fz
        return max(13, m["radius"] * 1000 / mpp)

    def _press(self, e):
        self.anim = None
        self.tz = self.fz
        self._last, self._moved = (e.x, e.y), 0

    def _drag(self, e):
        dx, dy = e.x - self._last[0], e.y - self._last[1]
        self._last = (e.x, e.y)
        self._moved += abs(dx) + abs(dy)
        cx, cy = self.world(self.lat, self.lon, self.fz)
        self.lat, self.lon = self.unworld(cx - dx, cy - dy, self.fz)
        self.lat = max(-85, min(85, self.lat))
        self.lon = (self.lon + 180) % 360 - 180
        self.redraw()
        self.vel = (self.vel[0] * 0.7 - dx * 0.3, self.vel[1] * 0.7 - dy * 0.3)
        t = time.time()
        if t - getattr(self, "_pf", 0) > 0.15:
            self._pf = t
            m = math.hypot(*self.vel)
            if m > 1:
                self.prefetch(self.vel[0] / m, self.vel[1] / m)

    def _release(self, e):
        if self._moved >= 5:
            return self.on_view()
        w, h = self.winfo_width(), self.winfo_height()
        cx, cy = self.world(self.lat, self.lon, self.fz)
        x0, y0 = cx - w / 2, cy - h / 2
        best, bd = None, 1e9
        for i, m in enumerate(self.marks):
            mx, my = self.world(m["lat"], m["lon"], self.fz)
            d = math.hypot(mx - x0 - e.x, my - y0 - e.y)
            if d <= max(16, self.px_radius(m)) and d < bd:
                best, bd = i, d
        self.select(best)
        self.on_pick(best)

    # ----- tiles -----
    def _hover(self, e):
        if e.state & 0x100:
            return
        w, h = self.winfo_width(), self.winfo_height()
        cx, cy = self.world(self.lat, self.lon, self.fz)
        x0, y0 = cx - w / 2, cy - h / 2
        t = time.time()
        if t - getattr(self, "_hw", 0) > 0.25 and not self.anim:
            self._hw = t
            self.warm(e.x, e.y, int(round(self.fz)))
        hit = False
        for m in self.marks:
            mx, my = self.world(m["lat"], m["lon"], self.fz)
            if math.hypot(mx - x0 - e.x, my - y0 - e.y) <= max(16, self.px_radius(m)):
                hit = True
                break
        c = "hand2" if hit else "fleur"
        if c != self._cur:
            self._cur = c
            self.configure(cursor=c)

    def fetch(self, key, bg=False):
        with self.qcv:
            if key in self.pending:
                if not bg and key in self.bgq:
                    self.bgq.discard(key)
                    self.fg.append(key)
                    self.qcv.notify()
                return
            self.pending.add(key)
            if bg:
                self.bgq.add(key)
                self.bg.append(key)
            else:
                self.fg.append(key)
                if len(self.fg) > 150:
                    for k in self.fg[:50]:
                        self.pending.discard(k)
                    del self.fg[:50]
            self.qcv.notify()

    def _pick(self, lst):
        zc = int(round(self.fz))
        cache = {}
        def score(k):
            z, x, y = k
            if z not in cache:
                cache[z] = self.world(self.lat, self.lon, z)
            cx, cy = cache[z][0] / 256, cache[z][1] / 256
            n = 2 ** z
            dx = abs(x + .5 - cx)
            dx = min(dx, n - dx)
            return abs(z - zc) * 1000 + math.hypot(dx, y + .5 - cy) / n
        best = min(lst, key=score)
        lst.remove(best)
        return best

    def _worker(self):
        while True:
            with self.qcv:
                while not self.fg and not self.bg:
                    self.qcv.wait()
                if not self.fg and getattr(self, "hold", False):
                    self.qcv.wait(0.3)
                    continue
                key = self._pick(self.fg if self.fg else self.bg)
                if key in self.loading:
                    continue
                self.loading.add(key)
            try:
                self._load(key)
            finally:
                self.loading.discard(key)

    def warm(self, ex, ey, z):
        w, h = self.winfo_width(), self.winfo_height()
        cx, cy = self.world(self.lat, self.lon, self.fz)
        plat, plon = self.unworld(cx - w / 2 + ex, cy - h / 2 + ey, self.fz)
        for zz, bg, r in ((z, False, None), (z + 1, True, 450), (z + 2, True, 300), (z - 1, True, None)):
            if zz < self.MINZ or zz > self.MAXZ:
                continue
            n = 2 ** zz
            px, py = self.world(plat, plon, zz)
            if r:
                xa, xb, ya, yb = px - r, px + r, py - r, py + r
            else:
                xa, xb, ya, yb = px - ex, px - ex + w, py - ey, py - ey + h
            for tx in range(int(xa // 256), int(xb // 256) + 1):
                for ty in range(int(ya // 256), int(yb // 256) + 1):
                    if 0 <= ty < n:
                        k = (zz, tx % n, ty)
                        if k not in self.pil:
                            self.fetch(k, bg=bg)

    def preload_world(self):
        for z in (2, 3):
            for x in range(2 ** z):
                for y in range(2 ** z):
                    self.fetch((z, x, y), bg=True)

    def prefetch(self, dx, dy, z=None):
        if len(self.pending) > 60: return
        w, h = self.winfo_width(), self.winfo_height()
        zi = max(self.MINZ, min(self.MAXZ, z if z is not None else int(round(self.fz))))
        n = 2 ** zi
        cx, cy = self.world(self.lat, self.lon, zi)
        cx += dx * w * 0.9
        cy += dy * h * 0.9
        for tx in range(int((cx - w / 2) // 256), int((cx + w / 2) // 256) + 1):
            for ty in range(int((cy - h / 2) // 256), int((cy + h / 2) // 256) + 1):
                key = (zi, tx % n, ty)
                if 0 <= ty < n and key not in self.pil and key not in self.pending:
                    self.fetch(key, bg=True)

    def _load(self, key):
        z, x, y = key
        fp = f"{CACHE}/{z}/{x}/{y}.jpg"
        try:
            if os.path.exists(fp):
                im = Image.open(fp).convert("RGBA")
                im.load()
                return ui(lambda: self._got(key, im))
        except Exception:
            pass
        try:
            lf = self.lpool.submit(lambda: SES.get(LABELS.format(z=z, x=x, y=y), headers=HDR, timeout=15))
            r = SES.get(IMAGERY.format(z=z, x=x, y=y), headers=HDR, timeout=15)
            r.raise_for_status()
            im = Image.open(io.BytesIO(r.content)).convert("RGBA")
            try:
                r2 = lf.result()
                r2.raise_for_status()
                lab = Image.open(io.BytesIO(r2.content)).convert("RGBA").resize(im.size)
                R, G, B, A = lab.split()
                A = A.point(lambda v: int(v * LABEL_ALPHA))
                R, G, B = (c.point(lambda v: int(v * LABEL_DARKEN)) for c in (R, G, B))
                im = Image.alpha_composite(im, Image.merge("RGBA", (R, G, B, A)))
            except Exception:
                pass
            try:
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                im.convert("RGB").save(fp, quality=88)
            except Exception:
                pass
            ui(lambda: self._got(key, im))
        except Exception as e:
            msg = f"Map tiles: {e}"
            ui(lambda: (self.pending.discard(key), self._fail(msg)))

    def _fail(self, msg):
        if not self._err:
            self._err = True
            self.on_error(msg)

    def _got(self, key, im):
        self.pending.discard(key)
        if len(self.pil) > 700:
            self.pil.clear(); self.tkc.clear()
        self.pil[key] = im
        if not self._rd:
            self._rd = self.after(30, self.redraw)

    def tile_pil(self, zi, x, y):
        key = (zi, x, y)
        im = self.pil.get(key)
        if im is not None:
            return im
        self.fetch(key)
        for up in range(1, 7):
            if zi - up < 0:
                break
            p = self.pil.get((zi - up, x >> up, y >> up))
            if p is not None:
                sub = 256 >> up
                ox, oy = (x % (1 << up)) * sub, (y % (1 << up)) * sub
                return p.crop((ox, oy, ox + sub, oy + sub))
        if zi < self.MAXZ:
            kids = [(i, j, self.pil.get((zi + 1, 2 * x + i, 2 * y + j))) for i in (0, 1) for j in (0, 1)]
            if any(k[2] is not None for k in kids):
                c = Image.new("RGBA", (256, 256), (20, 18, 16, 255))
                for i, j, k in kids:
                    if k is not None:
                        c.paste(k.resize((128, 128), Image.Resampling.NEAREST), (i * 128, j * 128))
                return c
        return None

    def tile_img(self, zi, x, y, ts):
        key = (zi, x, y)
        im = self.pil.get(key)
        if im is not None:
            if ts == 256:
                t = self.tkc.get(key)
                if t is None:
                    t = self.tkc[key] = ImageTk.PhotoImage(im)
                return t
            k = (key, ts)
            t = self.scaled.get(k)
            if t is None:
                t = self.scaled[k] = ImageTk.PhotoImage(im.resize((ts, ts), self.rs()))
            return t
        p = self.tile_pil(zi, x, y)
        if p is None:
            return None
        k = (key, ts, "p", len(self.pil))
        t = self.scaled.get(k)
        if t is None:
            t = self.scaled[k] = ImageTk.PhotoImage(p.resize((ts, ts), self.rs()))
        return t

    def fast_frame(self, zi, n, tsf, x0, y0, w, h):
        fr = Image.new("RGB", (w, h), (20, 18, 16))
        for tx in range(int(x0 // tsf), int((x0 + w) // tsf) + 1):
            for ty in range(int(y0 // tsf), int((y0 + h) // tsf) + 1):
                if ty < 0 or ty >= n:
                    continue
                im = self.tile_pil(zi, tx % n, ty)
                if im is None:
                    continue
                px, py = int(round(tx * tsf - x0)), int(round(ty * tsf - y0))
                nx, ny = int(round((tx + 1) * tsf - x0)), int(round((ty + 1) * tsf - y0))
                sz = (max(1, nx - px), max(1, ny - py))
                if im.size != sz:
                    im = im.resize(sz, Image.Resampling.NEAREST)
                fr.paste(im, (px, py))
        if self._ftk is None or self._ftk.width() != w or self._ftk.height() != h:
            self._ftk = ImageTk.PhotoImage(fr)
        else:
            self._ftk.paste(fr)
        self.create_image(0, 0, image=self._ftk, anchor="nw")

    def rs(self):
        return Image.Resampling.NEAREST if self.anim else RESAMPLE

    def ovl_img(self, r, color, sel):
        key = (r, color, sel)
        t = self.ovl.get(key)
        if t: return t
        d = 2 * r + 2
        im = Image.new("RGBA", (d, d), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        a_edge, a_in = (44, 4) if sel else (92, 20)
        steps = max(4, min(28, r // 2))
        for k in range(steps):
            f = k / steps
            rr = r * (1 - f)
            a = int(a_edge + (a_in - a_edge) * f)
            dr.ellipse((r + 1 - rr, r + 1 - rr, r + 1 + rr, r + 1 + rr), fill=rgb + (a,))
        t = self.ovl[key] = ImageTk.PhotoImage(im)
        return t

    # ----- drawing -----
    def redraw(self):
        self._rd = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2: return
        self.delete("all")
        if len(self.scaled) > 300: self.scaled.clear()
        if len(self.ovl) > 80: self.ovl.clear()
        self._sel_geom, self.path_px, self.path_cum = None, [], []
        fz = self.fz
        zi = max(0, min(19, int(round(fz))))
        n = 2 ** zi
        tsf = 256 * 2 ** (fz - zi)
        ts = int(math.ceil(tsf))
        cx, cy = self.world(self.lat, self.lon, fz)
        x0, y0 = cx - w / 2, cy - h / 2
        if self.anim:
            self.fast_frame(zi, n, tsf, x0, y0, w, h)
        else:
            for tx in range(int(x0 // tsf), int((x0 + w) // tsf) + 1):
                for ty in range(int(y0 // tsf), int((y0 + h) // tsf) + 1):
                    if ty < 0 or ty >= n: continue
                    img = self.tile_img(zi, tx % n, ty, ts)
                    if img:
                        self.create_image(tx * tsf - x0, ty * tsf - y0, image=img, anchor="nw")

        def P(lat, lon):
            px, py = self.world(lat, lon, fz)
            return px - x0, py - y0

        order = [i for i in range(len(self.marks)) if i != self.sel]
        if self.sel is not None and self.sel < len(self.marks):
            order.append(self.sel)
        for i in order:
            m = self.marks[i]
            mx, my = P(m["lat"], m["lon"])
            r = self.px_radius(m)
            if not (-r < mx < w + r and -r < my < h + r):
                continue
            c, on = m["color"], i == self.sel
            ri = int(r)
            if not self.anim and 2 * ri <= 900:
                self.create_image(mx, my, image=self.ovl_img(ri, c, on))
            self.create_oval(mx - r, my - r, mx + r, my + r, outline=c, width=3 if on else 2)
            if on:
                pts = [P(a, b) for a, b in m["track"]]
                fc = [P(a, b) for a, b in m["fc"]]
                if len(pts) > 1:
                    flat = [v for p in pts for v in p]
                    self.create_line(*flat, fill=BG, width=7, smooth=True, capstyle="round")
                    self.create_line(*flat, fill=c, width=3, smooth=True, capstyle="round")
                    for px, py in pts[:-1]:
                        self.create_oval(px - 3, py - 3, px + 3, py + 3, fill=BG, outline=c, width=2)
                if fc:
                    seq = [pts[-1]] + fc if pts else fc
                    flat = [v for p in seq for v in p]
                    self.create_line(*flat, fill="white", width=2, dash=(8, 6), arrow=tk.LAST,
                                     arrowshape=(14, 16, 6))
                    for px, py in fc[:-1]:
                        self.create_oval(px - 2.5, py - 2.5, px + 2.5, py + 2.5, fill="white", outline="")
                    ex, ey = fc[-1]
                    self.create_text(ex + 10, ey - 12, text="projected", fill="white",
                                     font=(SANS, 9, "italic"), anchor="w")
                full = pts + fc
                if len(full) > 1:
                    self.path_px = full
                    cum = [0.0]
                    for a, b in zip(full, full[1:]):
                        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
                    self.path_cum = cum
                self.create_oval(0, 0, 0, 0, outline=c, width=2, tags="pulse")
                if self.path_px:
                    self.create_oval(0, 0, 0, 0, fill="white", outline=c, width=2, tags="mover")
                self._sel_geom = (mx, my, r, c)
            d = 5 if on else 3.5
            self.create_oval(mx - d, my - d, mx + d, my + d, fill="white" if on else c,
                             outline=c if on else "white", width=2 if on else 1)
            if on or fz >= 4 or len(self.marks) <= 8:
                t = self.create_text(mx, my - 20, text=m["label"], fill=TEXT, font=(SANS, 10, "bold"))
                bb = self.bbox(t)
                rc = self.create_rectangle(bb[0] - 9, bb[1] - 4, bb[2] + 9, bb[3] + 4,
                                           fill=BG, outline=c, width=2 if on else 1)
                self.tag_lower(rc, t)
        self.create_text(w - 8, h - 8, anchor="se", fill="#ddd", font=(SANS, 8),
                         text="Imagery © Esri, Maxar, Earthstar Geographics")


def blend(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(a, b))


class DatePicker(tk.Canvas):
    RH, W, H = 32, 364, 160
    MONTHS = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]

    def __init__(self, master, on_change):
        super().__init__(master, width=self.W, height=self.H, bg=SURF, highlightthickness=0)
        now = dt.datetime.now(dt.timezone.utc)
        self.years = list(range(2000, now.year + 1))
        self.cx = [120, 270]
        self.fs = 1.0
        self.data = [self.MONTHS, [str(y) for y in self.years]]
        self.pos = [float(now.month - 1), float(len(self.years) - 1)]
        self.tgt = list(self.pos)
        self.on_change, self.run, self.col, self.moved, self.last = on_change, False, 0, 0, 0
        self.bind("<ButtonPress-1>", self.press)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<ButtonRelease-1>", self.release)
        self.bind("<Button-4>", lambda e: self.wheel(e, -1))
        self.bind("<Button-5>", lambda e: self.wheel(e, 1))
        self.bind("<MouseWheel>", lambda e: self.wheel(e, -1 if e.delta > 0 else 1))
        self.draw()

    def cl(self, c, v):
        return max(0, min(len(self.data[c]) - 1, v))

    def col_at(self, x):
        return min(range(2), key=lambda c: abs(self.cx[c] - x))

    def get(self):
        return self.years[int(round(self.tgt[1]))], int(round(self.tgt[0])) + 1

    def hour(self):
        return dt.datetime.now(dt.timezone.utc).hour

    def press(self, e):
        self.col, self.last, self.moved = self.col_at(e.x), e.y, 0

    def drag(self, e):
        c = self.col
        self.moved += abs(e.y - self.last)
        p = self.pos[c] - (e.y - self.last) / self.RH
        self.last = e.y
        self.pos[c] = self.tgt[c] = max(0.0, min(len(self.data[c]) - 1.0, p))
        self.draw()

    def release(self, e):
        c = self.col
        if self.moved < 4:
            self.tgt[c] = float(self.cl(c, round(self.pos[c] + (e.y - self.H / 2) / self.RH)))
        else:
            self.tgt[c] = float(self.cl(c, round(self.pos[c])))
        self.kick()

    def wheel(self, e, d):
        c = self.col_at(e.x)
        self.tgt[c] = float(self.cl(c, round(self.tgt[c]) + d))
        self.kick()

    def kick(self):
        if not self.run:
            self.run = True
            self.after(16, self.loop)

    def loop(self):
        moving = False
        for c in range(2):
            d = self.tgt[c] - self.pos[c]
            if abs(d) > 0.01:
                self.pos[c] += d * 0.3
                moving = True
            else:
                self.pos[c] = self.tgt[c]
        self.draw()
        if moving:
            self.after(16, self.loop)
        else:
            self.run = False
            self.on_change()

    def draw(self):
        self.delete("all")
        W, H, RH = self.W, self.H, self.RH
        mid = H / 2
        self.create_rectangle(8, mid - RH / 2, W - 8, mid + RH / 2, fill=SURF2, outline="")
        for c, cx in enumerate(self.cx):
            items, p = self.data[c], self.pos[c]
            for i in range(int(round(p)) - 3, int(round(p)) + 4):
                if i < 0 or i >= len(items):
                    continue
                d = abs(i - p)
                size = 15 if d < 0.5 else 13 if d < 1.5 else 11
                size = max(7, int(size * self.fs))
                self.create_text(cx, mid + (i - p) * RH, text=items[i],
                                 fill=blend(TEXT, SURF, min(1, d / 3.0)),
                                 font=(SANS, size, "bold" if d < 0.5 else "normal"))
        self.create_rectangle(0, 0, W, 36, fill=SURF, outline="", stipple="gray50")
        self.create_rectangle(0, H - 36, W, H, fill=SURF, outline="", stipple="gray50")

# ---------------- app ----------------
def button(parent, text, cmd, bg=SURF2, fg=TEXT, bold=False, **kw):
    return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=BORDER,
                     activeforeground=TEXT, relief="flat", bd=0, padx=12, pady=6, cursor="hand2",
                     font=(SANS, 12, "bold" if bold else "normal"), **kw)


def entry(parent, **kw):
    return tk.Entry(parent, bg=SURF, fg=TEXT, insertbackground=TEXT, relief="flat",
                    highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                    font=(SANS, 12), **kw)


class App(tk.Tk):
    mode, flt = "live", "All"
    _rj = None
    _draining, _fsid, _last, _wbox = False, None, None, None
    def __init__(self):
        super().__init__()
        self.title("Extreme Weather Tracker")
        self.geometry("1360x820")
        self.configure(bg=BG)
        self.minsize(900, 600)
        self.f, self.sc0 = 1.0, float(self.tk.call("tk", "scaling"))
        self.bind("<Configure>", self._cfg)
        self.job, self.scan_id, self.cards, self.items, self.cur = None, 0, [], [], None
        self.ev_cache = {}

        shell = tk.Frame(self, bg=BG, width=420)
        shell.pack(side="left", fill="y")
        shell.pack_propagate(False)
        self.shell = shell
        self.lsb = tk.Scrollbar(shell, orient="vertical", width=10, bg=SURF2, troughcolor=BG,
                                relief="flat", bd=0, highlightthickness=0)
        self.lcv = tk.Canvas(shell, bg=BG, highlightthickness=0, yscrollincrement=20,
                             yscrollcommand=self.lsb.set)
        self.lsb.configure(command=self.lcv.yview)
        self.lsb.pack(side="right", fill="y")
        self.lcv.pack(side="left", fill="both", expand=True)
        left = tk.Frame(self.lcv, bg=BG)
        left.pack_propagate(False)
        self.lwin = self.lcv.create_window(0, 0, window=left, anchor="nw")
        self.left = left
        self.lcv.bind("<Configure>", lambda e: self.fit_left())

        head = tk.Frame(left, bg=BG)
        head.pack(fill="x", padx=28, pady=(28, 0))
        tk.Label(head, text="●", bg=BG, fg=RED, font=(SANS, 10)).pack(side="left")
        tk.Label(head, text="LIVE", bg=BG, fg=MUTED, font=(SANS, 9, "bold")).pack(side="left", padx=(5, 0))
        tk.Label(left, text="Extreme weather,\nright now.", bg=BG, fg=TEXT, justify="left",
                 anchor="w", font=(SANS, 26, "bold")).pack(fill="x", padx=28, pady=(8, 4))
        tk.Label(left, text="Hurricanes, floods, tornadoes and 100+ km/h winds.", bg=BG, fg=MUTED,
                 justify="left", anchor="w", wraplength=360, font=(SERIF, 13)).pack(fill="x", padx=28)

        self.loc = entry(left)
        self.loc.pack(fill="x", padx=28, pady=(20, 0), ipady=8)
        self.loc.bind("<Return>", lambda e: self.search())
        tk.Label(left, text="Type a city and press Enter to fly there", bg=BG, fg=MUTED,
                 anchor="w", font=(SANS, 9)).pack(fill="x", padx=30, pady=(3, 0))

        tabs = tk.Frame(left, bg=BG)
        tabs.pack(fill="x", padx=28, pady=(16, 0))
        self.tab = {}
        for key, label in (("live", "Live"), ("archive", "Archive")):
            b = tk.Button(tabs, text=label, command=lambda k=key: self.set_mode(k), relief="flat",
                          bd=0, padx=18, pady=6, cursor="hand2", font=(SANS, 11, "bold"),
                          activebackground=BORDER, activeforeground=TEXT)
            b.pack(side="left", padx=(0, 6))
            self.tab[key] = b
        tk.Button(tabs, text="Surprise me  ↻", command=self.surprise, relief="flat", bd=0, padx=14,
                  pady=6, cursor="hand2", font=(SANS, 10), bg=BG, fg=MUTED,
                  activebackground=BORDER, activeforeground=TEXT).pack(side="right")
        self.ctrl = tk.Frame(left, bg=BG)
        self.ctrl.pack(fill="x", padx=28, pady=(12, 0))
        self.pk = tk.Frame(self.ctrl, bg=BG)
        self.pk.pack()
        self.picker = DatePicker(self.pk, lambda: self.schedule(500))
        self.picker.pack()
        self.whole = tk.BooleanVar(value=False)
        tk.Checkbutton(self.pk, text="  Search the whole year", variable=self.whole,
                       command=lambda: self.schedule(300), bg=BG, fg=TEXT, selectcolor=SURF2,
                       activebackground=BG, activeforeground=ACCENT, highlightthickness=0, bd=0,
                       cursor="hand2", font=(SANS, 10)).pack(anchor="w", pady=(10, 0))
        tk.Label(self.pk, text="CONTINENT", bg=BG, fg=MUTED, font=(SANS, 8, "bold")).pack(anchor="w", pady=(12, 4))
        self.cont_menu(self.pk).pack(fill="x")
        self.chips = tk.Frame(self.ctrl, bg=BG)
        self.chip = {}
        for i, k in enumerate(KINDS):
            b = tk.Button(self.chips, text=k, command=lambda k=k: self.set_filter(k), relief="flat",
                          bd=0, padx=8, pady=5, cursor="hand2", font=(SANS, 9, "bold"),
                          activebackground=BORDER, activeforeground=TEXT)
            b.grid(row=i // 4, column=i % 4, padx=(0, 6), pady=(0, 6), sticky="ew")
            self.chips.columnconfigure(i % 4, weight=1)
            self.chip[k] = b
        self.cont_menu(self.chips).grid(row=2, column=0, columnspan=4, sticky='ew', pady=(6, 0))
        self.paint_tabs()

        tk.Frame(left, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(18, 0))
        self.status = tk.Label(left, text="Starting...", bg=BG, fg=ACCENT, anchor="w",
                               justify="left", wraplength=360, font=(SANS, 10, "bold"))
        self.status.pack(fill="x", padx=28, pady=(12, 6))

        wrap = tk.Frame(left, bg=BG)
        wrap.pack(fill="both", expand=True, padx=(22, 10), pady=(0, 14))
        self.cv = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.cv, bg=BG)
        win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfig(win, width=e.width))
        def wheel(e, d):
            if str(e.widget).startswith(str(self.left)) and not isinstance(e.widget, DatePicker):
                (self.cv if str(e.widget).startswith(str(self.cv.master)) else self.lcv).yview_scroll(d, "units")
        self.bind_all("<Button-4>", lambda e: wheel(e, -2), add="+")
        self.bind_all("<Button-5>", lambda e: wheel(e, 2), add="+")

        self.map = TileMap(self, lambda: self.schedule(800), self.pick_card,
                           lambda m: self.status.configure(text=m, fg=RED))
        self.map.pack(side="right", fill="both", expand=True)

        zm = tk.Frame(self.map, bg=BG, padx=4, pady=4)
        zm.place(relx=1, rely=1, x=-16, y=-34, anchor="se")
        c = lambda: (self.map.winfo_width() / 2, self.map.winfo_height() / 2)
        button(zm, "+", lambda: self.map.zoom_at(1, *c()), bold=True).pack()
        button(zm, "−", lambda: self.map.zoom_at(-1, *c()), bold=True).pack(pady=(4, 0))

        lg = tk.Frame(self.map, bg=BG, padx=12, pady=8)
        lg.place(x=16, y=16)
        self.lg = lg
        self.fill_legend(False)

        self.detail = tk.Frame(self.map, bg=BG, highlightthickness=1, highlightbackground=BORDER)
        self.d_tag = tk.Label(self.detail, bg=BG, anchor="w", font=(SANS, 9, "bold"))
        self.d_title = tk.Label(self.detail, bg=BG, fg=TEXT, anchor="w", justify="left",
                                wraplength=320, font=(SANS, 15, "bold"))
        self.d_desc = tk.Label(self.detail, bg=BG, fg=MUTED, anchor="w", justify="left",
                               wraplength=320, font=(SERIF, 12))
        self.d_meta = tk.Label(self.detail, bg=BG, fg=MUTED, anchor="w", justify="left",
                               wraplength=320, font=(SANS, 9))
        for wd, pad in ((self.d_tag, (12, 0)), (self.d_title, (2, 0)), (self.d_desc, (4, 0)),
                        (self.d_meta, (8, 8))):
            wd.pack(fill="x", padx=16, pady=pad)
        bt = tk.Frame(self.detail, bg=BG)
        bt.pack(fill="x", padx=16, pady=(0, 14))
        self.b_wiki = button(bt, "Learn more ↗", self.open_wiki)
        self.b_wiki.configure(font=(SANS, 10), padx=10, pady=4)
        self.b_wiki.pack(side="left")
        self.b_copy = button(bt, "Copy coords", self.copy_coords)
        self.b_copy.configure(font=(SANS, 10), padx=10, pady=4)
        self.b_copy.pack(side="left", padx=(6, 0))

        self.build_menubar()
        self.bind("<Escape>", lambda e: (self.map.select(None), self.pick_card(None)))
        self.after(40, self.poll)
        self.after(900, lambda: self.schedule(0))
        self.after(1500, self.first_run)

    @property
    def hr(self):
        return self.picker.hour()

    def cont_menu(self, parent):
        if not hasattr(self, "cont"):
            self.cont = tk.StringVar(value="Worldwide")
        om = tk.OptionMenu(parent, self.cont, *CONT, command=self.set_cont)
        om.configure(bg=SURF2, fg=TEXT, activebackground=BORDER, activeforeground=TEXT, relief="flat",
                     bd=0, highlightthickness=0, font=(SANS, 11), anchor="w", cursor="hand2", padx=12, pady=6)
        om["menu"].configure(bg=SURF, fg=TEXT, activebackground=ACCENT, activeforeground=BG,
                             bd=0, font=(SANS, 10))
        return om

    def cont_box(self):
        c = CONT.get(self.cont.get())
        return c[0] if c else None

    def set_cont(self, v=None):
        c = CONT.get(self.cont.get())
        if c:
            self.map.fly_to(c[1][0], c[1][1], c[2], 1100)
        else:
            self.map.fly_to(20, 10, 2, 1000)
        if self.mode == "live":
            self.schedule(600)
        else:
            self.show_archive()

    def shift_month(self, d):
        if self.mode != "live":
            self.set_mode("live")
        p = self.picker
        y, m = p.get()
        y, m = divmod(y * 12 + (m - 1) + d, 12)
        if y not in p.years:
            return
        p.tgt = [float(m), float(p.years.index(y))]
        p.kick()

    def this_month(self):
        if self.mode != "live":
            self.set_mode("live")
        p = self.picker
        p.tgt = [float(dt.datetime.now(dt.timezone.utc).month - 1), float(len(p.years) - 1)]
        p.kick()

    def zoom_btn(self, d):
        self.map.zoom_at(d, self.map.winfo_width() / 2, self.map.winfo_height() / 2)

    def toggle_fs(self):
        self._fs = not getattr(self, "_fs", False)
        self.attributes("-fullscreen", self._fs)

    def export_json(self):
        if not self.items:
            return
        from tkinter import filedialog
        f = filedialog.asksaveasfilename(defaultextension=".json", initialfile="events.json",
                                         filetypes=[("JSON", "*.json")])
        if f:
            with open(f, "w") as fh:
                json.dump(self.items, fh, indent=2, default=str)

    def first_run(self):
        flag = os.path.expanduser("~/.config/weather-ab-seen")
        if os.path.exists(flag):
            return
        try:
            os.makedirs(os.path.dirname(flag), exist_ok=True)
            open(flag, "w").write("1")
        except OSError:
            pass
        self.show_tutorial()

    def show_tutorial(self):
        if getattr(self, "_tut", None) and self._tut.winfo_exists():
            return self._tut.lift()
        w = self._tut = tk.Frame(self, bg=BG, width=480, height=280,
                                 highlightthickness=2, highlightbackground=ACCENT)
        w.pack_propagate(False)
        w.place(relx=0.5, rely=0.5, anchor="center")
        w.lift()
        st = {"i": 0}
        n = len(TUTORIAL)
        dots = tk.Label(w, bg=BG, fg=ACCENT, anchor="w", font=(SANS, 10))
        dots.pack(fill="x", padx=28, pady=(24, 0))
        title = tk.Label(w, bg=BG, fg=TEXT, anchor="w", font=(SANS, 20, "bold"))
        title.pack(fill="x", padx=28, pady=(6, 0))
        body = tk.Label(w, bg=BG, fg=MUTED, anchor="nw", justify="left", wraplength=420,
                        font=(SERIF, 13))
        body.pack(fill="both", expand=True, padx=28, pady=(8, 0))
        bar = tk.Frame(w, bg=BG)
        bar.pack(fill="x", padx=28, pady=(0, 22))
        skip = button(bar, "Skip", w.destroy, bg=BG, fg=MUTED)
        skip.pack(side="left")
        nxt = button(bar, "Next", lambda: go(1), bg=ACCENT, fg=BG, bold=True)
        nxt.pack(side="right")
        back = button(bar, "Back", lambda: go(-1))
        back.pack(side="right", padx=(0, 8))

        def go(d):
            if st["i"] + d >= n:
                return w.destroy()
            st["i"] = max(0, st["i"] + d)
            i = st["i"]
            dots.configure(text=" ".join("●" if k == i else "○" for k in range(n)))
            title.configure(text=TUTORIAL[i][0])
            body.configure(text=TUTORIAL[i][1])
            nxt.configure(text="Done" if i == n - 1 else "Next")
            back.configure(state="normal" if i else "disabled")

        go(0)
        w.bind("<Escape>", lambda e: w.destroy())
        w.bind("<Right>", lambda e: go(1))
        w.bind("<Left>", lambda e: go(-1))
        w.focus_set()

    def show_usage(self):
        w = tk.Toplevel(self)
        w.title("Usage")
        w.configure(bg=BG)
        w.geometry("580x560")
        t = tk.Text(w, bg=BG, fg=TEXT, relief="flat", wrap="word", font=(SERIF, 12), padx=24,
                    pady=20, highlightthickness=0, spacing3=4)
        t.insert("1.0", USAGE)
        t.configure(state="disabled")
        t.pack(fill="both", expand=True)

    def toggle_year(self):
        if self.mode != "live":
            self.set_mode("live")
        self.whole.set(not self.whole.get())
        self.schedule(300)

    def cache_size(self):
        t = 0
        for r, _, fs in os.walk(CACHE):
            for f in fs:
                try: t += os.path.getsize(os.path.join(r, f))
                except OSError: pass
        return t

    @staticmethod
    def fmt_size(n):
        for u in ("B", "KB", "MB", "GB"):
            if n < 1024 or u == "GB":
                return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
            n /= 1024

    def cache_label(self):
        m, i = self._cc
        m.entryconfigure(i, label=f"  Clear map cache ({self.fmt_size(self.cache_size())})")

    def clear_cache(self):
        import shutil
        from tkinter import messagebox
        n = self.cache_size()
        if not messagebox.askyesno("Clear map cache", f"Delete {self.fmt_size(n)} of saved map tiles and events?\nThey re-download when needed."):
            return
        shutil.rmtree(CACHE, ignore_errors=True)
        for d in (self.map.pil, self.map.tkc, self.map.scaled):
            d.clear()
        self.ev_cache = {}
        self.map.redraw()
        self.status.configure(text=f"Cleared {self.fmt_size(n)} of cache.", fg=ACCENT)

    def open_folder(self):
        import subprocess
        os.makedirs(CACHE, exist_ok=True)
        subprocess.Popen(["xdg-open", CACHE])

    def about(self):
        from tkinter import messagebox
        messagebox.showinfo("About", "Extreme Weather Tracker\nLive storms, floods, alerts and an archive of record events.\n\nNot for safety decisions. Use official warnings.")

    def build_menubar(self):
        BAR = "#0d0c0b"
        bar = tk.Frame(self, bg=BAR, highlightthickness=1, highlightbackground=BORDER)
        bar.pack(side="top", fill="x", before=self.shell)

        def mk(label, bold=False):
            mb = tk.Menubutton(bar, text=label, bg=BAR, fg=ACCENT if bold else TEXT, activebackground=ACCENT,
                               activeforeground=BG, relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                               font=(SANS, 10, "bold" if bold else "normal"))
            mb.pack(side="left")
            m = tk.Menu(mb, tearoff=0, bg=SURF, fg=TEXT, activebackground=ACCENT, activeforeground=BG,
                        activeborderwidth=0, bd=1, relief="flat", font=(SANS, 10))
            mb.configure(menu=m)
            return m

        def add(m, label, cmd, acc="", key=None):
            m.add_command(label="  " + label, command=cmd, accelerator=acc + "  " if acc else "")
            if key:
                for k in key.split():
                    self.bind(k, lambda e, c=cmd: (c(), "break")[1])

        m = mk("● Tracker", True)
        add(m, "About", self.about)
        m.add_separator()
        add(m, "Quit", self.destroy, "Ctrl+Q", "<Control-q>")

        m = mk("File")
        add(m, "Export events as JSON…", self.export_json, "Ctrl+S", "<Control-s>")
        add(m, "Copy coordinates", self.copy_coords, "Ctrl+Shift+C", "<Control-C>")
        m.add_separator()
        add(m, "Open map folder", self.open_folder)
        add(m, "Clear map cache", self.clear_cache)
        self._cc = (m, m.index("end"))
        m.configure(postcommand=self.cache_label)

        m = mk("View")
        add(m, "Live", lambda: self.set_mode("live"), "Ctrl+1", "<Control-Key-1>")
        add(m, "Archive", lambda: self.set_mode("archive"), "Ctrl+2", "<Control-Key-2>")
        m.add_separator()
        cm = tk.Menu(m, tearoff=0, bg=SURF, fg=TEXT, activebackground=ACCENT, activeforeground=BG,
                     activeborderwidth=0, bd=1, font=(SANS, 10), selectcolor=ACCENT)
        for k in CONT:
            cm.add_radiobutton(label="  " + k, variable=self.cont, value=k, command=lambda: self.set_cont())
        m.add_cascade(label="  Continent", menu=cm)
        m.add_checkbutton(label="  Search the whole year", variable=self.whole, selectcolor=ACCENT,
                          command=lambda: self.schedule(300), accelerator="Ctrl+Y  ")
        self.bind("<Control-y>", lambda e: (self.toggle_year(), "break")[1])
        m.add_separator()
        add(m, "Zoom in", lambda: self.zoom_btn(1), "Ctrl++", "<Control-equal> <Control-plus>")
        add(m, "Zoom out", lambda: self.zoom_btn(-1), "Ctrl+-", "<Control-minus>")
        add(m, "Reset view", lambda: self.map.fly_to(20, 10, 2, 900), "Ctrl+0", "<Control-Key-0>")
        m.add_separator()
        add(m, "Fullscreen", self.toggle_fs, "F11", "<F11>")

        m = mk("Go")
        add(m, "Search a city", lambda: self.loc.focus_set(), "Ctrl+F", "<Control-f>")
        add(m, "Surprise me", self.surprise, "Ctrl+R", "<Control-r>")
        m.add_separator()
        add(m, "Previous month", lambda: self.shift_month(-1), "Alt+←", "<Alt-Left>")
        add(m, "Next month", lambda: self.shift_month(1), "Alt+→", "<Alt-Right>")
        add(m, "Current month", self.this_month, "Alt+Home", "<Alt-Home>")

        m = mk("Help")
        m.add_command(label="Tutorial", command=self.show_tutorial)
        add(m, "Usage", self.show_usage, "F1", "<F1>")
        add(m, "Report an issue", lambda: webbrowser.open("https://github.com/fsrvr/weather-ab/issues"))

        self.clk = tk.Label(bar, bg=BAR, fg=MUTED, font=(SANS, 9), padx=14)
        self.clk.pack(side="right")
        def tick():
            self.clk.configure(text=dt.datetime.now(dt.timezone.utc).strftime("UTC  %H:%M"))
            self.after(20000, tick)
        tick()

    def fit_left(self):
        w, h = self.lcv.winfo_width(), self.lcv.winfo_height()
        H = max(h, int(1100 * self.f))
        self.lcv.itemconfig(self.lwin, width=max(1, w), height=H)
        self.lcv.configure(scrollregion=(0, 0, w, H))

    def scale_fonts(self, root):
        import tkinter.font as tkf
        stack = [root]
        while stack:
            wd = stack.pop()
            stack.extend(wd.winfo_children())
            try:
                if not hasattr(wd, "_bf"):
                    a = tkf.Font(font=wd.cget("font")).actual()
                    wd._bf = (a["family"], abs(a["size"]), a["weight"], a["slant"])
                fam, sz, wt, sl = wd._bf
                wd.configure(font=(fam, max(7, round(sz * self.f)), wt, sl))
            except tk.TclError:
                pass

    def left_w(self):
        return int(420 * self.f)

    def _cfg(self, e):
        if e.widget is not self:
            return
        if self._rj:
            self.after_cancel(self._rj)
        self._rj = self.after(150, self.rescale)

    def rescale(self):
        self._rj = None
        w, h = self.winfo_width(), self.winfo_height()
        f = round(max(0.8, min(1.8, min(w / 1360, h / 820))) * 20) / 20
        if f == self.f:
            return
        self.f = f
        self.scale_fonts(self.left)
        self.scale_fonts(self.detail)
        lw = self.left_w()
        self.shell.configure(width=lw)
        self.fit_left()
        for wd in self.left.winfo_children():
            if isinstance(wd, tk.Label) and int(str(wd.cget("wraplength")) or 0) > 0:
                wd.configure(wraplength=lw - 60)
        for _, _, parts in self.cards:
            parts[1].configure(wraplength=lw - 120)
            parts[2].configure(wraplength=lw - 130)
        dw = int(320 * f)
        for wd in (self.d_title, self.d_desc, self.d_meta):
            wd.configure(wraplength=dw)
        p = self.picker
        p.W, p.H, p.RH = int(364 * f), int(160 * f), int(32 * f)
        p.cx = [int(120 * f), int(270 * f)]
        p.fs = f
        p.configure(width=p.W, height=p.H)
        p.draw()
        self.map.scaled.clear()
        self.map.redraw()

    def paint_tabs(self):
        for k, b in self.tab.items():
            on = k == self.mode
            b.configure(bg=SURF2 if on else BG, fg=ACCENT if on else MUTED)
        for k, b in self.chip.items():
            on = k == self.flt
            b.configure(bg=ACCENT if on else SURF2, fg=BG if on else TEXT)

    def set_mode(self, m):
        if m == self.mode:
            return
        self.mode = m
        self.paint_tabs()
        for w in self.ctrl.winfo_children():
            w.pack_forget()
        self.fill_legend(m == "archive")
        if m == "live":
            self.pk.pack()
            self.schedule(0)
        else:
            self.chips.pack(fill="x")
            self.map.fly_to(20, 10, 2, 900)
            self.show_archive()

    def set_filter(self, k):
        self.flt = k
        self.paint_tabs()
        if self.mode == "archive":
            self.show_archive()

    def show_archive(self):
        items = [e for e in LEGENDS if self.flt in ("All", e["kind"]) and in_cont(e, self.cont.get())]
        self.scan_id += 1
        self.show(items, "ALL-TIME RECORDS", None)

    def surprise(self):
        self.flt = "All"
        if self.mode != "archive":
            self.set_mode("archive")
        else:
            self.show_archive()
        self.paint_tabs()
        if self.items:
            i = random.randrange(len(self.items))
            self.click_card(i)
            self.pick_card(i)

    def fill_legend(self, arch):
        for w in self.lg.winfo_children():
            w.destroy()
        pairs = ([(KIND_COL[k], k.upper()) for k in KINDS[1:]] if arch
                 else [("#e8873a", "WARNING"), ("#e5534b", "EXTREME")])
        for col, name in pairs:
            tk.Label(self.lg, text="●", bg=BG, fg=col, font=(SANS, 10)).pack(side="left")
            tk.Label(self.lg, text=name, bg=BG, fg=MUTED, font=(SANS, 9, "bold")).pack(side="left", padx=(4, 12))

    def open_wiki(self):
        if self.cur is not None:
            webbrowser.open("https://en.wikipedia.org/wiki/Special:Search?search="
                            + requests.utils.quote(self.items[self.cur]["title"]))

    def copy_coords(self):
        if self.cur is None:
            return
        it = self.items[self.cur]
        self.clipboard_clear()
        self.clipboard_append(f"{it['lat']:.5f}, {it['lon']:.5f}")
        self.b_copy.configure(text="Copied ✓")
        self.after(1200, lambda: self.b_copy.configure(text="Copy coords"))

    def poll(self):
        try:
            while True:
                UIQ.get_nowait()()
        except queue.Empty:
            pass
        self.after(40, self.poll)

    def shift(self, n):
        try:
            d = dt.date.fromisoformat(self.date.get().strip()) + dt.timedelta(days=n)
        except ValueError:
            d = dt.date.today()
        self.date.delete(0, "end")
        self.date.insert(0, d.isoformat())
        self.schedule(500)

    def step_hour(self, n, scan=True):
        self.hr = (self.hr + n) % 24
        self.hlbl.configure(text=f"{self.hr:02d}:00 UTC")
        if scan: self.schedule(500)

    def schedule(self, ms):
        if self.mode != "live":
            return
        if self.job: self.after_cancel(self.job)
        self.job = self.after(ms, self.scan)

    def search(self):
        name = self.loc.get().strip()
        if not name: return
        self.status.configure(text="Finding place...", fg=ACCENT)
        def work():
            try:
                res = requests.get(GEO, params={"name": name, "count": 1}, headers=HDR,
                                   timeout=15).json().get("results")
                if not res:
                    return ui(lambda: self.status.configure(text="Place not found.", fg=RED))
                la, lo = res[0]["latitude"], res[0]["longitude"]
                ui(lambda: self.map.fly_to(la, lo, 6, 1200, notify=True))
            except Exception as e:
                msg = f"Search error: {e}"
                ui(lambda: self.status.configure(text=msg, fg=RED))
        threading.Thread(target=work, daemon=True).start()

    def scan(self):
        self.job = None
        if self.mode != "live" or self.map.winfo_width() < 10:
            return
        y, m = self.picker.get()
        now = dt.datetime.now(dt.timezone.utc)
        whole = self.whole.get()
        live = not whole and (y, m) == (now.year, now.month)
        future = y > now.year if whole else (y, m) > (now.year, now.month)
        label = (f"Whole year {y}" if whole else
                 f"{calendar.month_name[m]} {y}" + ("  ·  LIVE" if live else ""))
        pts = []
        if live:
            s, w, n, e = self.cont_box() or self.map.bounds()
            cols, rows = 8, 5
            for r in range(rows):
                for c in range(cols):
                    la = n - (r + 0.5) / rows * (n - s)
                    lo = w + (c + 0.5) / cols * (e - w)
                    pts.append((la, (lo + 180) % 360 - 180))
        cn = self.cont.get()
        skey = (y, m, whole, cn)
        box = (self.cont_box() or self.map.bounds()) if live else None
        vo = bool(self._last and self._last[0] == skey and self._last[1] == self.scan_id)
        if vo:
            wb = self._wbox
            if not live or (wb and box[0] >= wb[0] and box[1] >= wb[1] and box[2] <= wb[2] and box[3] <= wb[3]):
                return
        else:
            self.scan_id += 1
            self.status.configure(text="Looking for extreme events...", fg=ACCENT)
            self.start_eta(self.scan_id, "live" if live else "past")
            self.map.hold = True
            self.after(12000, lambda: setattr(self.map, "hold", False))
        sid = self.scan_id
        self._last = (skey, sid)
        if live:
            self._wbox = box

        def work():
            acc, lock, key = [], threading.Lock(), (y, m, whole)

            def push(new, final=False):
                if not new and not final:
                    return
                with lock:
                    acc.extend(new)
                    its = [x for x in acc if in_cont(x, cn)]
                its.sort(key=lambda x: -x["sev"])
                its = its[:120]
                ui(lambda: sid == self.scan_id and self.feed(its, label, sid, final))

            if future:
                return push([], True)
            jobs = []
            cached = self.ev_cache.get(key)
            dk = f"{y}-{m}-{int(whole)}-{int(live)}"
            if cached is None:
                cached = ev_load(dk, 600 if live else 2592000)
            if vo:
                pass
            elif cached is not None:
                push(cached)
            else:
                jobs.append(("eonet", lambda: get_eonet(y, m, live, whole)))
            if live and not vo:
                jobs.append(("nws", get_nws))
            if live:
                jobs.append(("wind", lambda: get_winds(pts, now.date(), now.hour)))

            def run(name, fn):
                try:
                    r = fn()
                except Exception:
                    r = []
                if name == "eonet":
                    self.ev_cache = {key: r}
                    if r: ev_save(dk, r)
                push(r)

            ths = [threading.Thread(target=run, args=j, daemon=True) for j in jobs]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            if not acc:
                push([], True)
            self.map.hold = False
            ui(lambda: self.eta_done(sid))
        threading.Thread(target=work, daemon=True).start()

    def start_eta(self, sid, kind):
        if not hasattr(self, "_durs"):
            self._durs = {}
        d = self._durs.get(kind, [])[-3:]
        est = sum(d) / len(d) if d else (8 if kind == "live" else 3)
        self._eta = (sid, time.time(), kind)
        def tick():
            if not self._eta or self._eta[0] != sid or self._fsid == sid:
                return
            el = time.time() - self._eta[1]
            t = f" {round(el)}s" if el >= 3 else ""
            self.status.configure(text=f"Looking for extreme events...{t}", fg=ACCENT)
            self.after(500, tick)
        tick()

    def eta_done(self, sid):
        e = getattr(self, "_eta", None)
        if e and e[0] == sid:
            self._durs.setdefault(e[2], []).append(time.time() - e[1])
            self._eta = None

    def begin(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.cards, self.items, self.cur = [], [], None
        self.detail.place_forget()
        self.map.set_marks([])

    def count_status(self, label):
        n = len(self.items)
        self.status.configure(text=f"{n} EXTREME EVENT{'S' if n != 1 else ''} · {label}", fg=TEXT)

    def show(self, items, date, hour):
        self.begin()
        if not items:
            self.status.configure(text=f"Nothing extreme found · {date}", fg="#7fb77e")
            return
        for it in items:
            self.add_card(it)
        self.map.redraw()
        self.count_status(date)
        self.cv.yview_moveto(0)

    def feed(self, its, label, sid, final=False):
        if self._fsid != sid:
            self._fsid, self._draining = sid, False
            self._seen, self._pend, self._label = set(), [], label
            self.begin()
        new = []
        for it in its:
            k = (it["title"], round(it["lat"], 2), round(it["lon"], 2))
            if k not in self._seen:
                self._seen.add(k)
                new.append(it)
        for it in new:
            self.add_card(it)
        self.map.redraw()
        if not self._seen:
            if final:
                self.status.configure(text=f"Nothing extreme found · {label}", fg="#7fb77e")
            return
        self.count_status(label)

    def _drain(self, sid):
        if sid != self._fsid:
            return
        for it in self._pend[:3]:
            self.add_card(it)
        del self._pend[:3]
        self.count_status(self._label)
        if self._pend:
            self.after(45, lambda: self._drain(sid))
        else:
            self._draining = False

    def add_card(self, it):
        i = len(self.items)
        self.items.append(it)
        color, tag = style(it)
        self.map.add_mark({"lat": it["lat"], "lon": it["lon"], "color": color, "label": it["title"],
                           "radius": it["radius"], "track": it["track"], "fc": it["fc"]})
        card = tk.Frame(self.inner, bg=SURF, cursor="hand2")
        card.pack(fill="x", pady=5, padx=(0, 6))
        bar = tk.Frame(card, bg=color, width=4)
        bar.pack(side="left", fill="y")
        body = tk.Frame(card, bg=SURF)
        body.pack(side="left", fill="x", expand=True, padx=14, pady=11)
        parts = [tk.Label(body, text=f"{it['kind'].upper()} · {tag}", bg=SURF, fg=color,
                          anchor="w", font=(SANS, 9, "bold")),
                 tk.Label(body, text=it["title"], bg=SURF, fg=TEXT, anchor="w",
                          font=(SANS, 14, "bold"), wraplength=self.left_w() - 120, justify="left"),
                 tk.Label(body, text=it["desc"], bg=SURF, fg=MUTED, anchor="w", justify="left",
                          wraplength=self.left_w() - 130, font=(SERIF, 12)),
                 tk.Label(body, text=f"{it['lat']:.2f}, {it['lon']:.2f}", bg=SURF, fg=MUTED,
                          anchor="w", font=(SANS, 9))]
        for p in parts: p.pack(fill="x")
        for wd in (card, bar, body, *parts):
            wd.bind("<Button-1>", lambda e, i=i: self.click_card(i))
            wd.bind("<Enter>", lambda e, i=i: self.paint(i, SURF2))
            wd.bind("<Leave>", lambda e, i=i: self.paint(i, SURF2 if i == self.cur else SURF))
        self.cards.append((card, body, parts))
        if self.f != 1.0:
            self.scale_fonts(card)

    def paint(self, i, bg):
        if i >= len(self.cards): return
        card, body, parts = self.cards[i]
        card.configure(bg=bg); body.configure(bg=bg)
        for p in parts: p.configure(bg=bg)

    def click_card(self, i):
        it = self.items[i]
        mpp = it["radius"] * 1000 / 170
        z = round(math.log2(156543.03 * max(0.05, math.cos(math.radians(it["lat"]))) / mpp))
        self.map.select(i)
        self.pick_card(i, scroll=False)
        self.map.fly_to(it["lat"], it["lon"], max(4, min(10, z)), 1200, notify=False)

    def pick_card(self, i, scroll=True):
        old, self.cur = self.cur, i
        if old is not None: self.paint(old, SURF)
        if i is None:
            self.detail.place_forget()
            return
        self.paint(i, SURF2)
        it = self.items[i]
        color, tag = style(it)
        self.d_tag.configure(text=f"{it['kind'].upper()} · {tag}", fg=color)
        self.d_title.configure(text=it["title"])
        self.d_desc.configure(text=it["desc"])
        meta = f"{it['lat']:.2f}, {it['lon']:.2f}  ·  impact radius ~{it['radius']:.0f} km"
        if it["track"] and len(it["track"]) > 1:
            meta += f"\nTrack: {len(it['track'])} points" + (" (approximate)" if it.get("db") else "") + (" · dashed line is the projected path" if it["fc"] else "")
        if it.get("facts"):
            meta += "\n" + it["facts"]
        self.d_meta.configure(text=meta)
        self.detail.place(relx=0, rely=1, x=16, y=-34, anchor="sw")
        if scroll:
            self.update_idletasks()
            y = self.cards[i][0].winfo_y()
            self.cv.yview_moveto(max(0, y - 20) / max(1, self.inner.winfo_height()))


if __name__ == "__main__":
    App().mainloop()
