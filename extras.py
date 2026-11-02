import os, re, json, time, threading, requests
from concurrent.futures import ThreadPoolExecutor

HDR = {"User-Agent": "WeatherAbnormalityTracker/4.0 (github project)"}
GEO = "https://geocoding-api.open-meteo.com/v1/search"
U = {"imp": False}


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def T(c):
    if c is None:
        return "-"
    return f"{c * 9 / 5 + 32:.0f}\u00b0F" if U["imp"] else f"{c:.0f}\u00b0C"


def S(k):
    if k is None:
        return "-"
    return f"{k * 0.621371:.0f} mph" if U["imp"] else f"{k:.0f} km/h"


def uconv(s):
    if not U["imp"] or not s:
        return s
    s = re.sub(r"(-?\d+(?:\.\d+)?) ?km/h", lambda m: f"{float(m.group(1)) * 0.621371:.0f} mph", s)
    s = re.sub(r"(?<!by )(-?\d+(?:\.\d+)?) ?(?:\u00b0C|C)\b",
               lambda m: f"{float(m.group(1)) * 9 / 5 + 32:.0f} \u00b0F", s)
    return s


# ---- favorites ----
FAVF = os.path.expanduser("~/.config/weather-ab-favs.json")


def favs_load():
    try:
        return json.load(open(FAVF))
    except Exception:
        return []


def favs_save(l):
    try:
        os.makedirs(os.path.dirname(FAVF), exist_ok=True)
        json.dump(l, open(FAVF, "w"), indent=1)
    except OSError:
        pass


# ---- radar (RainViewer free tier: past 2h, max zoom 7, 100 req/min) ----
def radar_frames():
    d = requests.get("https://api.rainviewer.com/public/weather-maps.json", headers=HDR, timeout=15).json()
    return d["host"], [(f["time"], f["path"]) for f in d["radar"]["past"]]


_RL, _RT = threading.Lock(), [0.0]


def rate_wait(gap=0.7):
    with _RL:
        w = _RT[0] + gap - time.time()
        if w > 0:
            time.sleep(w)
        _RT[0] = time.time()


# ---- MeteoAlarm (Europe) ----
MA = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{}"
CC = {"austria": "AT", "belgium": "BE", "bosnia-herzegovina": "BA", "bulgaria": "BG", "croatia": "HR",
      "cyprus": "CY", "czechia": "CZ", "denmark": "DK", "estonia": "EE", "finland": "FI", "france": "FR",
      "germany": "DE", "greece": "GR", "hungary": "HU", "iceland": "IS", "ireland": "IE", "israel": "IL",
      "italy": "IT", "latvia": "LV", "lithuania": "LT", "luxembourg": "LU", "malta": "MT", "moldova": "MD",
      "montenegro": "ME", "netherlands": "NL", "norway": "NO", "poland": "PL", "portugal": "PT",
      "romania": "RO", "serbia": "RS", "slovakia": "SK", "slovenia": "SI", "spain": "ES", "sweden": "SE",
      "switzerland": "CH", "republic-of-north-macedonia": "MK", "ukraine": "UA", "united-kingdom": "GB"}
_FC = {}
_GC = {}


def _ma_kind(t):
    t = t.lower()
    for words, k in ((("wind", "gale"), "Wind"), (("snow", "ice", "avalanche", "blizzard"), "Blizzard"),
                     (("thunder", "storm", "coastal"), "Storm"), (("flood", "rain"), "Flood"),
                     (("high-temp", "high temp", "heat"), "Heat"),
                     (("low-temp", "low temp", "cold", "frost"), "Cold"), (("fire",), "Fire")):
        if any(w in t for w in words):
            return k


def _ma_feed(slug):
    t0, old = _FC.get(slug, (0, []))
    if time.time() - t0 < 600:
        return old
    out = []
    try:
        r = requests.get(MA.format(slug), headers=HDR, timeout=20)
        r.raise_for_status()
        for w in r.json().get("warnings", []):
            a = w.get("alert") or {}
            if a.get("status") not in (None, "Actual"):
                continue
            infos = a.get("info") or []
            info = next((i for i in infos if str(i.get("language", "")).lower().startswith("en")),
                        infos[0] if infos else None)
            if not info or info.get("severity") not in ("Severe", "Extreme"):
                continue
            typ = " ".join(p.get("value", "") for p in info.get("parameter", []) if p.get("valueName") == "awareness_type")
            kind = _ma_kind(typ or info.get("event", ""))
            area = ((info.get("area") or [{}])[0].get("areaDesc") or "").split(",")[0].strip()
            if kind and area:
                out.append({"slug": slug, "area": area, "kind": kind,
                            "sev": 3 if info["severity"] == "Extreme" else 2,
                            "head": (info.get("headline") or info.get("event") or "")[:140]})
        _FC[slug] = (time.time(), out)
    except Exception:
        return old
    return out


def _ma_geo(a):
    k = (a["slug"], a["area"])
    if k not in _GC:
        try:
            res = requests.get(GEO, params={"name": a["area"][:60], "count": 1, "countryCode": CC[a["slug"]]},
                               headers=HDR, timeout=15).json().get("results")
            _GC[k] = (res[0]["latitude"], res[0]["longitude"]) if res else None
        except Exception:
            return None
    return _GC[k]


def get_meteoalarm():
    with ThreadPoolExecutor(max_workers=8) as ex:
        al = [x for f in ex.map(_ma_feed, CC) for x in f]
    seen, uniq = set(), []
    for a in sorted(al, key=lambda x: -x["sev"]):
        k = (a["slug"], a["area"], a["kind"])
        if k not in seen:
            seen.add(k)
            uniq.append(a)
    uniq = uniq[:40]
    with ThreadPoolExecutor(max_workers=8) as ex:
        pts = list(ex.map(_ma_geo, uniq))
    out = []
    for a, p in zip(uniq, pts):
        if p:
            out.append({"lat": p[0], "lon": p[1], "sev": a["sev"],
                        "title": f"{a['kind']} warning, {a['area']}"[:60],
                        "desc": a["head"] + " (MeteoAlarm)", "kind": a["kind"],
                        "radius": 40, "track": [], "fc": [], "d0": _today(), "d1": _today()})
    return out


# ---- past earthquakes (USGS catalog) ----
import datetime as _dt
_QC = {}


def get_quakes_past(y, m, whole):
    key = (y, m, whole)
    if key in _QC:
        return list(_QC[key])
    if whole:
        a, b, mm = f"{y}-01-01", f"{y + 1}-01-01", 6.0
    else:
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        a, b, mm = f"{y}-{m:02d}-01", f"{ny}-{nm:02d}-01", 5.5
    r = requests.get("https://earthquake.usgs.gov/fdsnws/event/1/query", headers=HDR, timeout=30,
                     params={"format": "geojson", "starttime": a, "endtime": b, "minmagnitude": mm,
                             "orderby": "magnitude", "limit": 200})
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f["properties"]
        mag = p.get("mag") or 0
        lon, lat = f["geometry"]["coordinates"][:2]
        day = _dt.datetime.fromtimestamp((p.get("time") or 0) / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")
        out.append({"lat": lat, "lon": lon, "sev": 3 if mag >= 6.5 else 2,
                    "title": p.get("place") or "Earthquake", "kind": "Quake",
                    "desc": f"M{mag:.1f} earthquake, {day}.", "radius": int(20 + mag * 15),
                    "track": [], "fc": [], "d0": day, "d1": day})
    _QC[key] = out
    return list(out)


# ---- past weather extremes (Open-Meteo archive, daily values on a grid) ----
import calendar as _cal
ARCH = "https://archive-api.open-meteo.com/v1/archive"
_EX = {}


def get_extremes_past(points, y, m, whole):
    if not points:
        return []
    key = (y, m, whole, len(points), round(points[0][0]), round(points[0][1]), round(points[-1][1]))
    if key in _EX:
        return list(_EX[key])
    if whole:
        a, b = f"{y}-01-01", f"{y}-12-31"
    else:
        a, b = f"{y}-{m:02d}-01", f"{y}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}"
    b = min(b, (_dt.date.today() - _dt.timedelta(days=6)).isoformat())
    if a > b:
        return []
    r = requests.get(ARCH, headers=HDR, timeout=60, params={
        "latitude": ",".join(f"{p[0]:.2f}" for p in points),
        "longitude": ",".join(f"{p[1]:.2f}" for p in points),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_gusts_10m_max,snowfall_sum",
        "start_date": a, "end_date": b, "timezone": "GMT"})
    r.raise_for_status()
    data = r.json()
    data = data if isinstance(data, list) else [data]
    out = []
    for (la, lo), d in zip(points, data):
        dy = d.get("daily") or {}
        days = dy.get("time") or []

        def peak(k, low=False):
            v = [(x, i) for i, x in enumerate(dy.get(k) or []) if x is not None]
            if not v:
                return None, None
            x, i = (min if low else max)(v)
            return x, days[i]

        def add(sev, title, kind, desc, day):
            out.append({"lat": la, "lon": lo, "sev": sev, "title": title, "kind": kind, "radius": 90,
                        "desc": desc, "track": [], "fc": [], "d0": day, "d1": day})

        g, gd = peak("wind_gusts_10m_max")
        if g is not None and g >= 120:
            add(3 if g >= 150 else 2, "Violent winds", "Wind", f"Gusts up to {g:.0f} km/h on {gd}. Trees, roofs and power lines at risk.", gd)
        p, pd_ = peak("precipitation_sum")
        if p is not None and p >= 100:
            add(3 if p >= 150 else 2, "Extreme rainfall", "Flood", f"{p:.0f} mm of rain in one day ({pd_}). Flash flooding likely.", pd_)
        t, td = peak("temperature_2m_max")
        if t is not None and t >= 43:
            add(3 if t >= 47 else 2, "Extreme heat", "Heat", f"{t:.0f} \u00b0C air temperature on {td}. Dangerous heat.", td)
        c, cd = peak("temperature_2m_min", True)
        if c is not None and c <= -45:
            add(3 if c <= -52 else 2, "Extreme cold", "Cold", f"{c:.0f} \u00b0C air temperature on {cd}. Frostbite within minutes.", cd)
        sn, sd = peak("snowfall_sum")
        if sn is not None and sn >= 30:
            add(3 if sn >= 50 else 2, "Heavy snowfall", "Blizzard", f"{sn:.0f} cm of snow in one day ({sd}).", sd)
    _EX[key] = out
    return list(out)
