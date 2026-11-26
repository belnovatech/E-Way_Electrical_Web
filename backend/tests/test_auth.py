from conftest import DEMO_OTP, bearer


def test_new_customer_signup_creates_profile_wallet_and_links_area(client, sql):
    phone = "9000011111"
    r = client.post("/v1/auth/send-otp", json={"phoneNumber": phone})
    assert r.status_code == 200 and r.json()["demoOtpHint"] == DEMO_OTP

    again = client.post("/v1/auth/send-otp", json={"phoneNumber": phone})
    assert again.status_code == 429                                  # resend cooldown

    bad = client.post("/v1/auth/verify-otp", json={"phoneNumber": phone, "otp": "000000"})
    assert bad.status_code == 400 and "attempt" in bad.json()["message"]

    r = client.post("/v1/auth/verify-otp", json={"phoneNumber": phone, "otp": DEMO_OTP})
    body = r.json()
    assert r.status_code == 200 and body["isNewUser"] and not body["isProfileComplete"]
    assert body["user"]["role"] == "customer" and body["user"]["publicCode"].startswith("CUST-")
    h = bearer(body["token"])

    assert client.get("/v1/wallet/balance", headers=h).json()["balance"] == 0      # wallet created by trigger

    r = client.patch("/v1/customer/profile", headers=h, json={"name": "Test Rider", "email": "Rider@Example.com", "city": "Vijayawada"})
    p = r.json()
    assert r.status_code == 200 and p["isProfileComplete"] and p["zoneCode"] == "AP-Z01" and p["email"] == "rider@example.com"
    admin = sql("""SELECT u.public_code FROM customer_profiles c JOIN users u ON u.id = c.assigned_admin_id
                   JOIN users me ON me.id = c.user_id WHERE me.phone_number = %s""", (phone,))
    assert admin == [("ADM-AP01",)]                                   # area admin of AP-Z01


def test_invalid_phone_rejected(client):
    r = client.post("/v1/auth/send-otp", json={"phoneNumber": "12345"})
    assert r.status_code == 422 and "10-digit" in r.json()["message"]


def test_staff_lockout_after_failed_attempts(client):
    email = "bhavani.shankar@powergrid.in"
    for _ in range(4):
        assert client.post("/v1/auth/login", json={"email": email, "password": "wrong"}).status_code == 401
    assert client.post("/v1/auth/login", json={"email": email, "password": "wrong"}).status_code == 423
    r = client.post("/v1/auth/login", json={"email": email, "password": "Admin@2026!"})
    assert r.status_code == 423                                       # even the right password is refused while locked


def test_logout_revokes_token_and_roles_are_enforced(client, staff_login, customer_login):
    client.post("/v1/auth/send-otp", json={"phoneNumber": "9876543215"})
    inactive = client.post("/v1/auth/verify-otp", json={"phoneNumber": "9876543215", "otp": DEMO_OTP})
    assert inactive.status_code == 403                                # CUST-1006 is seeded as inactive

    h = customer_login("9876543212")                                  # CUST-1003
    assert client.get("/v1/auth/me", headers=h).status_code == 200
    assert client.get("/v1/admin/zones", headers=h).status_code == 403
    assert client.post("/v1/auth/logout", headers=h).status_code == 200
    assert client.get("/v1/auth/me", headers=h).status_code == 401
    assert client.get("/v1/customer/profile").status_code == 401

    admin = staff_login("ravi.kumar@powergrid.in")
    me = client.get("/v1/auth/me", headers=admin).json()
    assert me["roleCode"] == "ROLE_ZONE_MANAGER" and len(me["zoneIds"]) == 2
    assert client.get("/v1/customer/profile", headers=admin).status_code == 403


def test_area_admin_cannot_use_super_admin_portal(client):
    r = client.post("/v1/auth/login", json={"email": "ravi.kumar@powergrid.in", "password": "Admin@2026!", "role": "super_admin"})
    assert r.status_code == 401


def test_change_password_policy(client, staff_login):
    h = staff_login("t.harish@powergrid.in")
    weak = client.post("/v1/auth/change-password", headers=h, json={"currentPassword": "Admin@2026!", "newPassword": "password1"})
    assert weak.status_code == 422
    ok = client.post("/v1/auth/change-password", headers=h, json={"currentPassword": "Admin@2026!", "newPassword": "N3w-Passw0rd!"})
    assert ok.status_code == 200
    assert client.post("/v1/auth/login", json={"email": "t.harish@powergrid.in", "password": "N3w-Passw0rd!"}).status_code == 200
