KEY = {"X-Charger-Key": "test-gateway-key"}


def test_gateway_requires_key(client):
    assert client.post("/v1/ocpp/heartbeat", json={"chargerId": "AP-Z06-CP01"}).status_code == 401
    assert client.post("/v1/ocpp/heartbeat", headers={"X-Charger-Key": "nope"}, json={"chargerId": "AP-Z06-CP01"}).status_code == 401
    assert client.post("/v1/ocpp/heartbeat", headers=KEY, json={"chargerId": "AP-Z06-CP01"}).json()["success"]


def test_fault_opens_one_ticket_and_blocks_charger(client, sql):
    r = client.post("/v1/ocpp/status-notification", headers=KEY, json={
        "chargerId": "AP-Z06-CP01", "status": "Faulted", "errorCode": "GroundFailure", "temperatureC": 71.5,
        "groundLeakageMa": 31, "info": "RCD tripped"}).json()
    assert r["ticketId"]
    again = client.post("/v1/ocpp/status-notification", headers=KEY, json={
        "chargerId": "AP-Z06-CP01", "status": "Faulted", "errorCode": "GroundFailure"}).json()
    assert again["ticketId"] == r["ticketId"]                       # no duplicate tickets
    assert sql("SELECT status::text FROM charging_points WHERE point_code = 'AP-Z06-CP01'") == [("maintenance",)]
    assert sql("SELECT COUNT(*) FROM charger_diagnostics WHERE ticket_id = %s", (int(r["ticketId"]),)) == [(2,)]

    client.post("/v1/ocpp/status-notification", headers=KEY, json={"chargerId": "AP-Z06-CP01", "status": "Available"})
    assert sql("SELECT status::text FROM charging_points WHERE point_code = 'AP-Z06-CP01'") == [("maintenance",)]  # ticket still open
