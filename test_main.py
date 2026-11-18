from fastapi.testclient import TestClient

from main import app

client = TestClient(app)  # not used as a context manager, so the background worker doesn't start in tests


def test_create_and_list_lead():
    r = client.post("/leads", json={"name": "Rahul", "phone": "9876543210", "campaign": "launch-oct"})
    assert r.status_code == 201
    assert r.json()["status"] == "queued"

    leads = client.get("/leads").json()
    assert leads[0]["name"] == "Rahul"
    assert leads[0]["campaign"] == "launch-oct"


def test_dashboard_page_is_served():
    r = client.get("/")
    assert r.status_code == 200 and "Lead Intake Gateway" in r.text


def test_rejects_bad_input():
    assert client.post("/leads", json={"name": "", "phone": "98"}).status_code == 422
    assert client.post("/leads", json={"phone": "9876543210"}).status_code == 422
    assert client.post("/leads", json={"name": "X", "phone": "1234567890"}).status_code == 422  # mobiles start 6-9


def test_phone_is_normalized():
    r = client.post("/leads", json={"name": "Asha", "phone": "+91 91234-56789", "campaign": "norm"})
    assert r.status_code == 201
    lead = [x for x in client.get("/leads").json() if x["campaign"] == "norm"][0]
    assert lead["phone"] == "+919123456789"


def test_duplicate_in_any_format_is_blocked():
    first = client.post("/leads", json={"name": "Vikram", "phone": "98111 22233", "campaign": "dup"})
    again = client.post("/leads", json={"name": "Vikram S", "phone": "+91-9811122233", "campaign": "dup"})
    other_campaign = client.post("/leads", json={"name": "Vikram", "phone": "09811122233", "campaign": "other"})

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json() == {"id": first.json()["id"], "status": "duplicate"}
    assert other_campaign.status_code == 201  # same person, different project = a real new lead
