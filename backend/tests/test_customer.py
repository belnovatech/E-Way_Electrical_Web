import time


def ledger_matches_balance(sql, phone):
    return sql("""
        SELECT w.balance = COALESCE((SELECT SUM(CASE WHEN direction = 'credit' THEN amount ELSE -amount END)
                                     FROM wallet_transactions t WHERE t.wallet_id = w.id AND t.status = 'success'), 0)
        FROM wallets w JOIN users u ON u.id = w.customer_id WHERE u.user_type = 'customer' AND u.phone_number = %s
    """, (phone,))[0][0]


def tomorrow(sql):
    return sql("SELECT ((now() AT TIME ZONE 'Asia/Kolkata')::date + 1)::text")[0][0]


def test_profile_dashboard_and_legacy_endpoints(client, bala, sql):
    p = client.get("/v1/customer/profile", headers=bala).json()
    assert p["name"] == "Bala Krishna" and p["zoneCode"] == "AP-Z01" and p["avatarInitials"] == "BK"
    assert ledger_matches_balance(sql, "8074407557")

    vehicles = client.get("/v1/customer/vehicles", headers=bala).json()
    assert [v["licensePlate"] for v in vehicles][0] == "AP-39-AB-1234" and vehicles[0]["isDefault"]
    assert vehicles[0]["ratePerKWh"] == 12 and vehicles[0]["type"] == "Car"

    d = client.get("/v1/customer/dashboard", headers=bala).json()
    assert d["walletBalance"] == p["walletBalance"] and d["nearbyStations"] and d["unreadNotificationsCount"] >= 1

    assert client.get("/v1/vehicles", headers=bala).json()[0]["iconEmoji"] == "🚗"
    summary = client.get("/v1/dashboard/summary", headers=bala).json()
    assert summary["currentVehicle"]["licensePlate"] == "AP-39-AB-1234" and summary["nearbyZones"]
    assert client.get("/v1/wallet/balance", headers=bala).json()["balance"] == p["walletBalance"]


def test_station_search_and_qr(client):
    near = client.get("/v1/stations", params={"centerLat": 16.5062, "centerLng": 80.6480, "radiusKm": 50}).json()
    assert near[0]["zoneCode"] == "AP-Z01" and near[0]["distanceKm"] < 1 and len(near[0]["chargers"]) == 4
    assert all("Bus" in s["vehicleTypes"] for s in client.get("/v1/stations", params={"vehicleType": "Bus"}).json())
    tirupati = client.get("/v1/stations", params={"city": "Tirupati"}).json()[0]
    assert tirupati["ratePerKWh"] == 14                                  # zone override T-004 beats the global car rate

    ok = client.get("/v1/chargers/AP-Z01-CP01/validate").json()
    assert ok["success"] and ok["data"]["ratePerKwh"] == 12 and ok["data"]["connectorType"] == "CCS2"
    broken = client.get("/v1/chargers/AP-Z05-CP02/validate").json()   # under maintenance in the seed
    assert not broken["success"] and broken["data"]["statusText"] == "Charger is offline"
    assert not client.get("/v1/chargers/NOPE-123/validate").json()["success"]
    assert client.get("/v1/operations/summary").json()["totalChargers"] >= 32


def test_topup_is_idempotent_and_credits_area_admin(client, bala, sql):
    before = client.get("/v1/wallet/balance", headers=bala).json()["balance"]
    intent = client.post("/v1/wallet/topup/intents", headers=bala, json={"amountINR": 500}).json()
    assert intent["amountINR"] == 500 and intent["orderSummary"]["orderType"] == "wallet_topup"

    r = client.post("/v1/wallet/topup/confirm", headers=bala,
                    json={"paymentId": intent["id"], "methodType": "upi", "upiVpa": "bala@okhdfcbank"}).json()
    assert r["success"] and r["receiptNumber"] and r["balance"] == round(before + 500, 2)
    replay = client.post("/v1/wallet/topup/confirm", headers=bala,
                         json={"paymentId": intent["id"], "methodType": "upi", "upiVpa": "bala@okhdfcbank"}).json()
    assert replay["replayed"] and replay["balance"] == r["balance"]  # no double credit

    collection = sql("""SELECT u.public_code, ac.amount, ac.admin_commission FROM admin_collections ac
                        JOIN users u ON u.id = ac.admin_id WHERE ac.payment_id = %s""", (int(intent["id"]),))
    assert [(c[0], float(c[1]), float(c[2])) for c in collection] == [("ADM-AP01", 500.0, 25.0)]

    failed_intent = client.post("/v1/wallet/topup/intents", headers=bala, json={"amountINR": 200}).json()
    failed = client.post("/v1/wallet/topup/confirm", headers=bala,
                         json={"paymentId": failed_intent["id"], "methodType": "card", "cardLast4": "4242", "simulateFailure": True}).json()
    assert not failed["success"] and failed["errorCode"] == "PAYMENT_DECLINED" and failed["balance"] == r["balance"]
    assert client.post("/v1/wallet/topup/intents", headers=bala, json={"amountINR": 5}).status_code == 422
    assert ledger_matches_balance(sql, "8074407557")

    ledger = client.get("/v1/wallet/transactions", headers=bala, params={"type": "recharge"}).json()
    assert ledger[0]["category"] == "wallet_topup" and ledger[0]["status"] == "Success"
    csv = client.get("/v1/wallet/statement.csv", headers=bala)
    assert csv.status_code == 200 and csv.text.startswith("Date,Reference")


def test_reservation_booking_overlap_and_refund(client, bala, customer_login, sql):
    day = tomorrow(sql)
    nexon = client.get("/v1/customer/vehicles", headers=bala).json()[0]
    before = client.get("/v1/wallet/balance", headers=bala).json()["balance"]

    wrong_plug = client.post("/v1/customer/reservations", headers=bala, json={
        "chargerId": "AP-Z01-CP04", "vehicleId": int(nexon["id"]), "date": day, "startTime": "10:00", "durationMinutes": 60})
    assert wrong_plug.status_code == 422 and "connector" in wrong_plug.json()["message"]

    r = client.post("/v1/customer/reservations", headers=bala, json={
        "chargerId": "AP-Z01-CP01", "vehicleId": int(nexon["id"]), "date": day, "startTime": "10:00", "durationMinutes": 60})
    res = r.json()
    assert r.status_code == 201, r.text
    assert res["status"] == "upcoming" and res["slotFeeINR"] == 25 and res["estimatedCostINR"] == 120 and res["durationText"] == "1 hr"
    assert client.get("/v1/wallet/balance", headers=bala).json()["balance"] == round(before - 25, 2)

    lakshmi = customer_login("9876543211")
    intent = client.post("/v1/wallet/topup/intents", headers=lakshmi, json={"amountINR": 300}).json()
    client.post("/v1/wallet/topup/confirm", headers=lakshmi, json={"paymentId": intent["id"], "methodType": "upi", "upiVpa": "lakshmi@ybl"})
    lv = client.get("/v1/customer/vehicles", headers=lakshmi).json()[0]
    clash = client.post("/v1/customer/reservations", headers=lakshmi, json={
        "chargerId": "AP-Z01-CP01", "vehicleId": int(lv["id"]), "date": day, "startTime": "10:30", "durationMinutes": 30})
    assert clash.status_code == 409 and "already booked" in clash.json()["message"]
    not_mine = client.post("/v1/customer/reservations", headers=lakshmi, json={
        "chargerId": "AP-Z01-CP02", "vehicleId": int(nexon["id"]), "date": day, "startTime": "18:00", "durationMinutes": 30})
    assert not_mine.status_code == 404                                  # cannot book with someone else's vehicle

    c = client.post(f"/v1/customer/reservations/{res['id']}/cancel", headers=bala).json()
    assert c["status"] == "cancelled"
    assert client.get("/v1/wallet/balance", headers=bala).json()["balance"] == before   # slot fee refunded
    assert client.post(f"/v1/customer/reservations/{res['id']}/cancel", headers=bala).status_code == 409
    assert ledger_matches_balance(sql, "8074407557")


def test_charging_session_start_stop_and_billing(client, bala, sql):
    nexon = client.get("/v1/customer/vehicles", headers=bala).json()[0]
    assert nexon["batteryPercentage"] == 68
    before = client.get("/v1/wallet/balance", headers=bala).json()["balance"]

    r = client.post("/v1/charging/sessions", headers=bala, json={"chargerId": "AP-Z01-CP01", "vehicleId": int(nexon["id"])})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["status"] == "CHARGING" and s["chargerId"] == "AP-Z01-CP01" and s["ratePerKwh"] == 12
    assert sql("SELECT status::text FROM charging_points WHERE point_code = 'AP-Z01-CP01'") == [("charging",)]
    again = client.post("/v1/charging/sessions", headers=bala, json={"chargerId": "AP-Z01-CP03"})
    assert again.status_code == 409

    time.sleep(0.4)        # with SIMULATION_SPEEDUP=3600 this is plenty to reach the 90 % auto-stop target
    live = client.get("/v1/charging/sessions/active", headers=bala).json()
    assert live["autoStopReason"] == "battery_target" and live["batteryPercentage"] == 90

    summary = client.post(f"/v1/charging/sessions/{s['sessionId']}/stop", headers=bala).json()
    # 40.5 * 22% = 8.91 kWh * 12 = 106.92, +10 fee = 116.92, +18% gst = 137.97
    assert summary["energyDeliveredKwh"] == 8.91 and summary["totalCost"] == 137.97 and summary["paymentStatus"] == "paid"
    assert summary["costBreakdown"] == {"energyCost": 106.92, "serviceFee": 10.0, "idleFee": 0.0, "gstAmount": 21.05, "total": 137.97}
    assert client.get("/v1/wallet/balance", headers=bala).json()["balance"] == round(before - 137.97, 2)
    assert sql("SELECT status::text FROM charging_points WHERE point_code = 'AP-Z01-CP01'") == [("available",)]
    assert client.get("/v1/charging/sessions/active", headers=bala).json() is None
    assert client.get("/v1/customer/vehicles", headers=bala).json()[0]["batteryPercentage"] == 90

    history = client.get("/v1/customer/charging-history", headers=bala).json()
    assert history[0]["sessionNumber"] == s["sessionNumber"] and history[0]["totalCostINR"] == 137.97
    assert ledger_matches_balance(sql, "8074407557")


def test_reserved_charger_blocks_other_customers(client, bala, customer_login, sql):
    nexon = client.get("/v1/customer/vehicles", headers=bala).json()[0]
    now_slot = sql("SELECT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI')")[0][0].split(" ")
    r = client.post("/v1/customer/reservations", headers=bala, json={
        "chargerId": "AP-Z08-CP01", "vehicleId": int(nexon["id"]), "date": now_slot[0], "startTime": now_slot[1], "durationMinutes": 30})
    assert r.status_code == 201, r.text
    other = customer_login("9440155321")                                 # CUST-1008, Ola bike (Type 2)
    car_owner = customer_login("9876543214")                             # CUST-1005, XUV400 (CCS2)
    blocked = client.post("/v1/charging/sessions", headers=car_owner, json={"chargerId": "AP-Z08-CP01"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "charger_reserved"
    assert other  # bike owner exists; Type 2 charger CP04 is free
    client.post(f"/v1/customer/reservations/{r.json()['id']}/cancel", headers=bala)


def test_vehicles_crud(client, bala):
    r = client.post("/v1/customer/vehicles", headers=bala, json={
        "name": "Fleet Van", "manufacturer": "Tata", "model": "Ace EV", "type": "Car", "licensePlate": "ap 39 zz 4321",
        "batteryCapacityKWh": 21.3, "connector": "ccs2"})
    v = r.json()
    assert r.status_code == 201 and v["licensePlate"] == "AP-39-ZZ-4321" and v["connector"] == "CCS2" and not v["isDefault"]
    dup = client.post("/v1/customer/vehicles", headers=bala, json={"model": "Clone", "type": "Car", "licensePlate": "AP39ZZ4321"})
    assert dup.status_code == 409
    assert client.post(f"/v1/customer/vehicles/{v['id']}/default", headers=bala).json()["isDefault"]
    assert client.delete(f"/v1/customer/vehicles/{v['id']}", headers=bala).status_code == 200
    vehicles = client.get("/v1/customer/vehicles", headers=bala).json()
    assert v["id"] not in [x["id"] for x in vehicles] and sum(x["isDefault"] for x in vehicles) == 1


def test_payment_methods_store_no_card_numbers(client, bala, sql):
    bad = client.post("/v1/wallet/payment-methods/cards", headers=bala,
                      json={"cardholderName": "Bala", "cardNumber": "4111 1111 1111 1112", "expiryMonth": 8, "expiryYear": 2029})
    assert bad.status_code == 422
    card = client.post("/v1/wallet/payment-methods/cards", headers=bala,
                       json={"cardholderName": "Bala", "cardNumber": "4111 1111 1111 1111", "expiryMonth": 8, "expiryYear": 2029}).json()
    assert card["brand"] == "visa" and card["last4"] == "1111" and card["expiry"] == "08/29"
    assert not sql("SELECT 1 FROM payment_methods WHERE gateway_token LIKE '%%4111111111111111%%' OR title LIKE '%%41111111%%'")
    assert client.post(f"/v1/wallet/payment-methods/{card['id']}/default", headers=bala).json()["isDefault"]
    methods = client.get("/v1/wallet/payment-methods", headers=bala).json()
    assert methods[0]["id"] == card["id"] and sum(m["isDefault"] for m in methods) == 1
    assert client.delete(f"/v1/wallet/payment-methods/{card['id']}", headers=bala).status_code == 200


def test_notifications_and_support(client, bala):
    n = client.get("/v1/notifications", headers=bala).json()
    assert n["items"] and n["unreadCount"] >= 1
    assert client.post("/v1/notifications/read-all", headers=bala).json()["success"]
    assert client.get("/v1/notifications", headers=bala).json()["unreadCount"] == 0

    t = client.post("/v1/customer/support-tickets", headers=bala, json={
        "subject": "Connector stuck", "category": "Charging Issues", "description": "The gun would not unlock.",
        "chargerId": "AP-Z01-CP03"}).json()
    assert t["status"] == "Open" and t["stationName"] == "Vijayawada Central Hub" and t["chargerId"] == "AP-Z01-CP03"
    t = client.post(f"/v1/customer/support-tickets/{t['id']}/replies", headers=bala, json={"message": "Any update?"}).json()
    assert t["replies"][-1]["sender"] == "customer"
    assert client.patch(f"/v1/customer/support-tickets/{t['id']}", headers=bala, json={"status": "Closed"}).json()["status"] == "Closed"


def test_settings_validation(client, bala):
    s = client.put("/v1/customer/settings", headers=bala, json={"autoStopBatteryPercent": 85, "smsNotifications": False}).json()
    assert s["autoStopBatteryPercent"] == 85 and s["smsNotifications"] is False
    assert client.put("/v1/customer/settings", headers=bala, json={"gstin": "NOT-A-GSTIN"}).status_code == 422
    client.put("/v1/customer/settings", headers=bala, json={"autoStopBatteryPercent": 90})
