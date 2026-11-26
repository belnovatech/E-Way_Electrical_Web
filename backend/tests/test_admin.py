def point_id(sql, code):
    return sql("SELECT id FROM charging_points WHERE point_code = %s", (code,))[0][0]


def test_zone_manager_only_sees_own_zones(client, ravi, ops_super, sql):
    zones = client.get("/v1/admin/zones", headers=ravi).json()
    assert sorted(z["code"] for z in zones) == ["AP-Z01", "AP-Z08"]
    assert len(client.get("/v1/admin/zones", headers=ops_super).json()) == 8

    points = client.get("/v1/admin/charging-points", headers=ravi).json()
    assert {p["zoneCode"] for p in points} == {"AP-Z01", "AP-Z08"}
    assert client.get(f"/v1/admin/charging-points/{point_id(sql, 'AP-Z02-CP01')}", headers=ravi).status_code == 404

    d = client.get("/v1/admin/dashboard", headers=ravi).json()
    assert d["totalZones"] == 2 and d["totalChargingPoints"] == 8 and len(d["revenueTrend"]) == 6

    users = client.get("/v1/admin/users", headers=ravi).json()
    assert users and {u["zoneCode"] for u in users} <= {"AP-Z01", "AP-Z08"}
    assert all("*" in (u["phone"] or "*") for u in users)            # zone managers lack VIEW_PII -> masked


def test_rbac_blocks_missing_permissions(client, ravi, staff_login):
    tariff = {"vehicleType": "CAR", "zoneCode": "AP-Z01", "ratePerKWhINR": 13, "effectiveFrom": "2026-01-01", "priority": 2}
    r = client.post("/v1/admin/tariffs", headers=ravi, json=tariff)
    assert r.status_code == 403 and "TARIFF_EDIT" in r.json()["message"]
    assert client.get("/v1/admin/wallet-transactions", headers=ravi).status_code == 403

    auditor = staff_login("lakshmi.n@powergrid.in")                  # Financial Auditor: read-only finance, global
    txns = client.get("/v1/admin/wallet-transactions", headers=auditor).json()
    assert txns and all(t["customerPhone"] is None or "*" in t["customerPhone"] for t in txns)
    adj = client.post("/v1/admin/wallet-adjustments", headers=auditor,
                      json={"customerId": 1, "direction": "credit", "amountINR": 10, "reason": "test credit"})
    assert adj.status_code == 403


def test_tariff_lifecycle_and_overlap(client, ops_super, sql):
    dup = client.post("/v1/admin/tariffs", headers=ops_super, json={
        "vehicleType": "CAR", "zoneCode": "Global", "ratePerKWhINR": 11, "effectiveFrom": "2026-06-01", "priority": 3})
    assert dup.status_code == 409 and "already covers" in dup.json()["message"]

    t = client.post("/v1/admin/tariffs", headers=ops_super, json={
        "vehicleType": "CAR", "zoneCode": "AP-Z08", "ratePerKWhINR": 13, "serviceFeeINR": 8, "effectiveFrom": "2026-01-01",
        "effectiveTo": "2026-12-31", "priority": 2, "pricingType": "Time of Use", "peakRatePerKWhINR": 16,
        "peakHoursWindow": "18:00-22:00"}).json()
    assert t["zoneCode"] == "AP-Z08" and t["pricingType"] == "Time of Use" and t["peakHoursWindow"] == "18:00-22:00"
    upd = client.patch(f"/v1/admin/tariffs/{t['id']}", headers=ops_super, json={"ratePerKWhINR": 12.5}).json()
    assert upd["ratePerKWhINR"] == 12.5
    log = sql("SELECT previous_value->>'ratePerKWhINR', new_value->>'ratePerKWhINR' FROM audit_logs WHERE action = %s",
              (f"Updated tariff {t['code']}",))
    assert log == [("13.0", "12.5")]
    assert client.delete(f"/v1/admin/tariffs/{t['id']}", headers=ops_super).json() == {"success": True, "deleted": True, "deactivated": False}


def test_maintenance_ticket_flow_controls_charger_status(client, ravi, sql):
    t = client.post("/v1/admin/maintenance-tickets", headers=ravi, json={
        "chargerId": "AP-Z01-CP04", "faultCode": "CONNECTOR_LATCH", "issueDescription": "Latch does not engage", "severity": "High"}).json()
    assert t["status"] == "Open" and t["priority"] == "P2" and t["slaMinutes"] == 120 and t["timeline"][0]["action"] == "Ticket created"
    assert sql("SELECT status::text FROM charging_points WHERE point_code = 'AP-Z01-CP04'") == [("maintenance",)]

    early = client.patch(f"/v1/admin/charging-points/{t['chargerId']}", headers=ravi, json={"status": "Available"})
    assert early.status_code == 409                                   # open ticket keeps the charger out of service

    a = client.post(f"/v1/admin/maintenance-tickets/{t['id']}/assign", headers=ravi, json={"engineerCode": "ENG-103"}).json()
    assert a["status"] == "In Progress" and a["technicianAssigned"] == "P. Rajesh Kumar"
    client.post(f"/v1/admin/maintenance-tickets/{t['id']}/notes", headers=ravi, json={"note": "Replacing latch spring"})
    done = client.post(f"/v1/admin/maintenance-tickets/{t['id']}/resolve", headers=ravi,
                       json={"resolutionNotes": "Latch spring replaced and tested"}).json()
    assert done["status"] == "Resolved" and [e["action"] for e in done["timeline"]][-1] == "Ticket resolved"
    assert sql("SELECT status::text FROM charging_points WHERE point_code = 'AP-Z01-CP04'") == [("available",)]
    assert sql("SELECT availability::text FROM field_engineers WHERE engineer_code = 'ENG-103'") == [("available",)]


def test_field_engineer_scope(client, staff_login):
    eng = staff_login("k.venkatesh@powergrid.in")                   # ENG-101, AP-Z05
    tickets = client.get("/v1/admin/maintenance-tickets", headers=eng).json()
    assert tickets and {t["zoneCode"] for t in tickets} == {"AP-Z05"}
    assert client.get("/v1/admin/tariffs", headers=eng).status_code == 403


def test_charging_point_admin_crud(client, ravi, sql):
    p = client.post("/v1/admin/charging-points", headers=ravi, json={
        "zoneCode": "AP-Z08", "type": "DC (Fast)", "powerKw": "60 kW", "connectorType": "CCS2", "ocppId": "OCPP-TEST-Z08-5"}).json()
    assert p["pointCode"] == "AP-Z08-CP05" and p["chargerNumber"] == 5 and p["powerKw"] == 60 and p["status"] == "Available"
    assert client.post("/v1/admin/charging-points", headers=ravi, json={
        "zoneCode": "AP-Z02", "powerKw": 60}).status_code == 404       # not Ravi's zone
    off = client.patch(f"/v1/admin/charging-points/{p['id']}", headers=ravi, json={"status": "Offline"}).json()
    assert off["status"] == "Offline"
    assert client.delete(f"/v1/admin/charging-points/{p['id']}", headers=ravi).status_code == 200
    in_use = client.delete(f"/v1/admin/charging-points/{point_id(sql, 'AP-Z01-CP03')}", headers=ravi)
    assert in_use.status_code == 409 and "history" in in_use.json()["message"]


def test_wallet_adjustment_and_refund(client, ops_super, sql):
    cust = sql("SELECT id FROM users WHERE public_code = 'CUST-1005'")[0][0]
    r = client.post("/v1/admin/wallet-adjustments", headers=ops_super,
                    json={"customerId": cust, "direction": "credit", "amountINR": 300, "reason": "Goodwill credit"}).json()
    assert r["balanceAfterINR"] == 300
    over = client.post("/v1/admin/wallet-adjustments", headers=ops_super,
                       json={"customerId": cust, "direction": "debit", "amountINR": 301, "reason": "Too much"})
    assert over.status_code == 422 and "insufficient" in over.json()["message"].lower()

    sid = sql("SELECT id FROM charging_sessions WHERE session_number = 'SES-20261107-0001'")[0][0]
    part = client.post(f"/v1/admin/charging-sessions/{sid}/refund", headers=ops_super, json={"amountINR": 50, "reason": "Slow charging"})
    assert part.json()["refundedINR"] == 50
    too_much = client.post(f"/v1/admin/charging-sessions/{sid}/refund", headers=ops_super, json={"amountINR": 300, "reason": "Again"})
    assert too_much.status_code == 422 and "223.76" in too_much.json()["message"]
    rest = client.post(f"/v1/admin/charging-sessions/{sid}/refund", headers=ops_super, json={"reason": "Full refund"}).json()
    assert rest["refundedINR"] == 223.76
    assert sql("SELECT payment_status::text FROM charging_sessions WHERE id = %s", (sid,)) == [("refunded",)]


def test_roles_cannot_escalate(client, ops_super, staff_login):
    roles = client.get("/v1/admin/roles", headers=ops_super).json()
    sa = next(r for r in roles if r["code"] == "ROLE_SUPER_ADMIN")
    assert sa["isProtected"] and len(sa["permissions"]) == 25
    assert client.patch(f"/v1/admin/roles/{sa['id']}", headers=ops_super, json={"description": "changed text"}).status_code == 403

    officer = staff_login("sandeep.raju@powergrid.in")              # holds ROLE_MANAGEMENT but not TARIFF_EDIT
    esc = client.post("/v1/admin/roles", headers=officer, json={
        "name": "Pricing Desk", "description": "Can edit tariffs", "accessLevel": "Elevated", "permissions": ["TARIFF_EDIT"]})
    assert esc.status_code == 403
    ok = client.post("/v1/admin/roles", headers=officer, json={
        "name": "Audit Reader", "description": "Reads audit logs", "accessLevel": "Audit", "permissions": ["AUDIT_LOGS"]})
    assert ok.status_code == 201 and ok.json()["code"] == "ROLE_AUDIT_READER" and ok.json()["type"] == "Custom"
    assert client.delete(f"/v1/admin/roles/{ok.json()['id']}", headers=officer).status_code == 200

    staff = client.get("/v1/admin/staff", headers=ops_super).json()
    eng = next(s for s in staff if s["staffCode"] == "ENG-102")
    fin = next(r for r in roles if r["code"] == "ROLE_FINANCIAL_AUDITOR")
    move = client.put(f"/v1/admin/staff/{eng['id']}/role", headers=ops_super, json={"roleId": int(fin["id"])})
    assert move.status_code == 409                                     # still on the field-engineer roster


def test_reports_audit_and_settings(client, ops_super, ravi):
    rep = client.get("/v1/admin/reports", headers=ravi, params={"period": "3M"}).json()
    assert len(rep["monthlyMetrics"]) == 3 and {c["category"] for c in rep["vehicleCategories"]} == {"BIKE", "CAR", "BUS"}
    assert {z["zoneCode"] for z in rep["zonePerformance"]} == {"AP-Z01", "AP-Z08"}
    csv = client.get("/v1/admin/reports/sessions.csv", headers=ravi)
    assert csv.status_code == 200 and csv.text.startswith("sessionNumber")
    logs = client.get("/v1/admin/audit-logs", headers=ops_super, params={"module": "Pricing"}).json()
    assert logs and all(l["module"] == "Pricing" for l in logs)
    s = client.put("/v1/admin/settings", headers=ops_super, json={"reservationGraceMinutes": 10}).json()
    assert s["reservationGraceMinutes"] == 10
    assert client.put("/v1/admin/settings", headers=ravi, json={"reservationGraceMinutes": 5}).status_code == 403
