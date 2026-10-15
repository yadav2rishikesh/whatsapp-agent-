from app.normalize import normalize_type, parse_inr, parse_location, parse_value_range


def test_types():
    assert normalize_type("Commercial Office") == "Office"
    assert normalize_type("Office") == "Office"
    assert normalize_type("retail") == "Retail"
    assert normalize_type("2 BHK flat") == "Residential"
    assert normalize_type("spaceship") is None


def test_locations():
    assert parse_location("Bandra West, Mumbai")["city"] == "Mumbai"
    assert parse_location("Alibaug, Maharashtra") == {"locality": "Alibaug", "city": "Alibaug", "state": "Maharashtra"}
    assert parse_location("Noida Sector 62, Noida")["city"] == "Noida"
    assert parse_location("Indiranagar")["city"] == "Bengaluru"
    assert parse_location("Koramangala, Bangalore")["city"] == "Bengaluru"


def test_money():
    assert parse_inr("₹4.2 crore") == 42_000_000
    assert parse_inr("12.5 Cr") == 125_000_000
    assert parse_inr("72 lakh") == 7_200_000
    assert parse_inr("Rs 50L") == 5_000_000
    assert parse_inr("1,20,00,000") == 12_000_000
    assert parse_inr(42000000) == 42_000_000
    assert parse_value_range("30000000-80000000") == (30_000_000, 80_000_000)
    assert parse_value_range("50000000") == (50_000_000, 50_000_000)
