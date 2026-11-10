import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main
from extras import U, uconv

def test_category():
    assert main.cat(260) == "Category 5 hurricane"
    assert main.cat(10) == "Tropical depression"

def test_distance():
    assert round(main.hav(0, 0, 0, 1)) == 111

def test_continent():
    assert main.in_cont({"lat": 48, "lon": 10}, "Europe")
    assert not main.in_cont({"lat": -30, "lon": 150}, "Europe")

def test_units():
    U["imp"] = True
    try:
        assert uconv("100 km/h") == "62 mph"
    finally:
        U["imp"] = False

def test_archive_valid():
    for e in main.LEGENDS:
        assert -90 <= e["lat"] <= 90 and -180 <= e["lon"] <= 180
        assert e["kind"] in main.KIND_COL
