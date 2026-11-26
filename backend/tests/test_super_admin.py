import pytest


def ids(sql, table, col, value):
    return sql(f"SELECT id FROM {table} WHERE {col} = %s", (value,))[0][0]


def topup(client, headers, amount):
    intent = client.post("/v1/wallet/topup/intents", headers=headers, json={"amountINR": amount}).json()
    r = client.post("/v1/wallet/topup/confirm", headers=headers, json={"paymentId": intent["id"], "methodType": "upi",
                                                                       "upiVpa": "bala@okhdfcbank"})
    assert r.json()["success"]


def test_areas_admins_customers(client, super_admin):
    areas = client.get("/v1/super-admin/areas", headers=super_admin).json()
    z01 = next(a for a in areas if a["code"] == "AP-Z01")
    assert len(areas) == 8 and z01["assignedAdminNames"] == ["Ravi Kumar Varma"] and z01["totalChargers"] == 4

    admins = client.get("/v1/super-admin/admins", headers=super_admin).json()
    ravi = next(a for a in admins if a["code"] == "ADM-AP01")
    assert ravi["assignedAreaCodes"] == ["AP-Z01", "AP-Z08"] and ravi["commissionRatePercent"] == 5
    assert ravi["bankDetails"]["ifsc"] == "SBIN0001824" and ravi["avatar"] == "RK"

    custs = client.get("/v1/super-admin/customers", headers=super_admin, params={"areaId": z01["id"]}).json()
    assert {c["areaCode"] for c in custs} == {"AP-Z01"} and any(c["code"] == "CUST-1001" for c in custs)


def test_settlement_generation_and_verification(client, super_admin, bala, sql):
    topup(client, bala, 1000)                                          # Bala -> ADM-AP01 / AP-Z01, 5 % commission
    admin_id, zone_id = ids(sql, "users", "public_code", "ADM-AP01"), ids(sql, "zones", "code", "AP-Z01")
    today = sql("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date::text")[0][0]
    week_ago = sql("SELECT ((now() AT TIME ZONE 'Asia/Kolkata')::date - 6)::text")[0][0]

    txns = client.get("/v1/super-admin/transactions", headers=super_admin, params={"adminId": admin_id}).json()
    assert txns[0]["type"] == "Wallet Topup" and txns[0]["settlementStatus"] == "Pending Settlement"

    st = client.post("/v1/super-admin/settlements", headers=super_admin, json={
        "adminId": admin_id, "areaId": zone_id, "periodStart": week_ago, "periodEnd": today})
    assert st.status_code == 201, st.text
    st = st.json()
    assert st["status"] == "Processing" and st["grossCollectionsINR"] >= 1000
    assert st["netAmountPayableToBusinessINR"] == round(st["grossCollectionsINR"] - st["commissionDeductedINR"], 2)

    topup(client, bala, 200)
    overlap = client.post("/v1/super-admin/settlements", headers=super_admin, json={
        "adminId": admin_id, "areaId": zone_id, "periodStart": today, "periodEnd": today})
    assert overlap.status_code == 409 and "already covers" in overlap.json()["message"]
    wrong_area = client.post("/v1/super-admin/settlements", headers=super_admin, json={
        "adminId": admin_id, "areaId": ids(sql, "zones", "code", "AP-Z03"), "periodStart": today, "periodEnd": today})
    assert wrong_area.status_code == 422

    too_much = client.post(f"/v1/super-admin/settlements/{st['id']}/verify", headers=super_admin,
                           json={"utrNumber": "SBIN0099887766", "verifiedAmountINR": st["netAmountPayableToBusinessINR"] + 1})
    assert too_much.status_code == 422
    partial = client.post(f"/v1/super-admin/settlements/{st['id']}/verify", headers=super_admin,
                          json={"utrNumber": "SBIN0099887766", "verifiedAmountINR": 100}).json()
    assert partial["status"] == "Pending Review" and partial["outstandingBalanceINR"] == round(st["netAmountPayableToBusinessINR"] - 100, 2)
    done = client.post(f"/v1/super-admin/settlements/{st['id']}/verify", headers=super_admin,
                       json={"utrNumber": "SBIN0099887766", "verifiedAmountINR": st["netAmountPayableToBusinessINR"]}).json()
    assert done["status"] == "Verified" and done["outstandingBalanceINR"] == 0 and done["verifiedBy"] == "E-Way Super Admin"
    assert sql("SELECT DISTINCT settlement_status::text FROM admin_collections WHERE settlement_id = %s", (int(st["id"]),)) == [("settled",)]

    reused = client.post("/v1/super-admin/settlements/" + str(ids(sql, "admin_settlements", "batch_number", "VG-STL-AP01-202610B")) + "/verify",
                         headers=super_admin, json={"utrNumber": "SBIN0099887766", "verifiedAmountINR": 10})
    assert reused.status_code == 409                                    # UTR numbers are unique


def test_dashboard_metrics_and_period_comparison(client, super_admin):
    m = client.get("/v1/super-admin/dashboard", headers=super_admin, params={"datePreset": "30D"}).json()
    assert m["totalAreas"] == 8 and m["totalAdmins"] >= 6 and m["totalSuccessfulCustomerCollectionsINR"] > 0
    assert set(m["periodComparison"]) == {"collectionsGrowthPercent", "receiptsGrowthPercent", "customerGrowthPercent",
                                          "settlementChangePercent"}
    one_area = client.get("/v1/super-admin/dashboard", headers=super_admin, params={"areaId": 1, "datePreset": "YTD"}).json()
    assert one_area["totalAreas"] == 1


@pytest.mark.parametrize("report", ["admin_performance", "area_performance", "customer_growth", "customer_payments",
                                    "admin_collections", "business_settlements", "outstanding_balances", "refunds_failed",
                                    "monthly_financial"])
def test_reports(client, super_admin, report):
    r = client.get(f"/v1/super-admin/reports/{report}", headers=super_admin, params={"datePreset": "90D"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] and isinstance(body["rows"], list)
    csv = client.get(f"/v1/super-admin/reports/{report}", headers=super_admin, params={"format": "csv"})
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")


def test_admin_lifecycle(client, super_admin, sql):
    z07 = ids(sql, "zones", "code", "AP-Z07")
    weak = client.post("/v1/super-admin/admins", headers=super_admin, json={
        "name": "Kiran Raju", "email": "kiran.raju@powergrid.in", "phone": "9848077887", "password": "weakpass", "areaIds": [z07]})
    assert weak.status_code == 422
    new = client.post("/v1/super-admin/admins", headers=super_admin, json={
        "name": "Kiran Raju", "email": "kiran.raju@powergrid.in", "phone": "9848077887", "password": "Kiran@2026!",
        "areaIds": [z07], "commissionRatePercent": 4, "bankName": "Canara Bank", "accountLast4": "8812", "ifsc": "CNRB0001234"}).json()
    assert new["code"] == "ADM-AP07" and new["assignedAreaCodes"] == ["AP-Z07"]
    login = client.post("/v1/auth/login", json={"email": "kiran.raju@powergrid.in", "password": "Kiran@2026!"})
    assert login.status_code == 200
    kiran = {"Authorization": f"Bearer {login.json()['token']}"}
    assert [z["code"] for z in client.get("/v1/admin/zones", headers=kiran).json()] == ["AP-Z07"]

    moved = client.put(f"/v1/super-admin/admins/{new['id']}/areas", headers=super_admin,
                       json={"areaIds": [z07, ids(sql, "zones", "code", "AP-Z06")]}).json()
    assert moved["assignedAreaCodes"] == ["AP-Z06", "AP-Z07"]

    srinivas = ids(sql, "users", "public_code", "ADM-AP06")
    sus = client.patch(f"/v1/super-admin/admins/{srinivas}/status", headers=super_admin, json={"status": "Suspended"}).json()
    assert sus["status"] == "Suspended"
    assert client.post("/v1/auth/login", json={"email": "srinivas.reddy@powergrid.in", "password": "Admin@2026!"}).status_code == 403
    client.patch(f"/v1/super-admin/admins/{srinivas}/status", headers=super_admin, json={"status": "Active"})

    logs = client.get("/v1/super-admin/audit-logs", headers=super_admin, params={"module": "Admins"}).json()
    assert any("ADM-AP07" in l["action"] for l in logs) and all(l["actionType"] == "ADMIN_MOD" for l in logs)


def test_non_super_admin_rejected(client, ravi):
    assert client.get("/v1/super-admin/areas", headers=ravi).status_code == 403
