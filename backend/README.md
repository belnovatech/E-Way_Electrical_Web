# E-Way Electrical — Backend API (Python)

A FastAPI service that serves all three portals in `src/`: customer, area admin and super admin. It runs on the PostgreSQL schema in [../database](../database), which remains the source of truth: triggers handle wallet balances, double-booking and charger status, and views provide the dashboard numbers.

- **Stack:** Python 3.12, FastAPI, psycopg 3 (sync pool), Pydantic v2, PyJWT. SQL is hand-written and parameterised; there is no ORM.
- **Response shapes** match the TypeScript types the frontend already uses (`src/types/*.ts`, `src/admin/types/admin.ts`, `src/super-admin/types/superAdmin.ts`), field names included (`ratePerKWh`, `walletBalanceINR` and so on).
- **123 endpoints** under `/v1`. Interactive docs are at `http://localhost:8000/docs`.

## Run it

```powershell
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```

Settings come from `<repo>/.env.local`, then `backend/.env`, then real environment variables. See [.env.example](.env.example). At minimum, set `DATABASE_URL` and `JWT_SECRET`. Keep secrets out of the root `.env`: that file is committed.

If pip fails with "Could not find a suitable TLS CA certificate bundle", the `CURL_CA_BUNDLE` environment variable points to a file that does not exist. Clear it for the install: `$env:CURL_CA_BUNDLE=$null`.

### Point the frontend at it

Do this in `.env.local`, not the committed `.env`:

```
VITE_ENABLE_DEMO_MODE=false
VITE_API_BASE_URL=http://localhost:8000/v1
```

`src/services/apiClient.ts` now sends the token from `sessionStorage` (`vg_auth_token`) as `Authorization: Bearer …`. These frontend calls already go through `apiClient` and match backend paths exactly: `/auth/send-otp`, `/auth/verify-otp`, `/auth/login`, `/vehicles`, `/dashboard/summary`, `/zones/nearby`, `/wallet/balance`, `/customer/dashboard`, `/operations/summary` and `/operations/chargers`.

The other frontend services still read their localStorage demo data. To move a screen to the API, replace the body of its service method with an `apiClient` call to the matching endpoint below. The JSON it returns already has the shape that method returns today.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

The tests build a throwaway `eway_test_db` from `database/schema.sql` and `seed.sql` on the server in `DATABASE_URL`, run 41 end-to-end API tests, and drop the database afterwards. They cover OTP signup, lockout, zone scoping, RBAC and privilege escalation, booking overlaps, exact session billing, idempotent top-ups, refunds, settlements, reports and charger faults.

## Endpoints

| Area | Auth | Endpoints |
|---|---|---|
| Auth | — | `POST /auth/send-otp`, `/auth/verify-otp` (customers), `/auth/login` (staff), `/auth/logout`, `/auth/change-password`; `GET /auth/me` |
| Stations (public) | — | `GET /stations` (search, city, `centerLat`/`centerLng`/`radiusKm`, `vehicleType`, `minPowerKw`), `/stations/{id}`, `/stations/{id}/chargers`, `/chargers/{code}/validate` (QR scan), `/zones/nearby`, `/operations/summary`, `/operations/chargers` |
| Customer | customer | `/customer/profile`, `/customer/settings`, `/customer/dashboard`, `/customer/vehicles` (CRUD, `/default`), `/customer/reservations` (create, cancel), `/customer/charging-history`, `/customer/support-tickets` (create, reply, close), `/vehicles`, `/dashboard/summary` |
| Charging | customer | `POST /charging/sessions` (start from QR code), `GET /charging/sessions/active`, `GET /charging/sessions/{id}`, `POST /charging/sessions/{id}/stop` |
| Wallet | customer | `/wallet/balance`, `/wallet/transactions`, `/wallet/statement.csv`, `/wallet/topup/intents` → `/wallet/topup/confirm`, `/wallet/payment-methods` (cards, UPI, net banking, default, delete) |
| Notifications | any | `GET /notifications`, `POST /notifications/{id}/read`, `/notifications/read-all` |
| Admin | staff + permission | `/admin/dashboard`, `zones`, `stations`, `charging-points`, `users`, `vehicles`, `charging-sessions` (`/stop`, `/refund`), `reservations` (`/cancel`), `tariffs`, `wallet-transactions`, `wallet-adjustments`, `field-engineers`, `maintenance-tickets` (`/assign`, `/notes`, `/resolve`), `audit-logs`, `reports`, `reports/sessions.csv`, `settings`, `support-tickets` |
| Roles | `ROLE_MANAGEMENT` | `/admin/permissions`, `/admin/roles` (CRUD), `/admin/staff`, `PUT /admin/staff/{id}/role`, `PATCH /admin/staff/{id}/status` |
| Super admin | super admin | `/super-admin/dashboard`, `areas`, `admins` (create, edit, status, `PUT …/areas`), `customers`, `transactions`, `settlements` (generate, `/verify`, `/dispute`), `audit-logs`, `reports/{type}` (9 types; add `?format=csv` for CSV) |
| Charger gateway | `X-Charger-Key` | `POST /ocpp/heartbeat`, `/ocpp/meter-values`, `/ocpp/status-notification` |

Every error has the shape `{"success": false, "message": "...", "code": "..."}`. The frontend `apiClient` already reads `message`. Database constraint violations map to `409` or `422` with readable text, for example "This charger is already booked for an overlapping time slot."

## How access works

- **Customers** sign in with phone + OTP. A new number creates the user, the customer profile and a wallet (via a trigger). Setting a city links the customer to that zone and to the zone's area admin.
- **Staff** sign in with email + password, stored as bcrypt via pgcrypto. After 5 failed attempts the account locks for 15 minutes. Admins are signed out after `admin_session_timeout_mins` idle (15 by default) and the super admin after 45.
- Each token is a JWT bound to a row in `user_sessions`. Logout, password change and suspension take effect on the next request.
- **Permissions** come from the staff member's role, using the codes in the seeded permissions table. `ALL_PERMISSIONS` passes every check. Nobody can grant a permission they do not hold, and protected roles cannot be edited.
- **Zone scope:** zone managers and field engineers only see the zones they are assigned. Super admins, auditors and compliance officers see everything. Phone numbers and emails are masked unless the user holds `VIEW_PII`.
- Admin and super-admin changes write to `audit_logs`, with previous and new values where they apply.

## Money flows

- **Top-up:** create an intent, then confirm it. On success the API records the payment, adds a `topup` credit to the ledger and creates an `admin_collections` row for the customer's area admin (commission at their rate). Confirm is idempotent: replaying it returns the stored result and never credits twice.
- **Reservation:** the reservation fee is debited when booking and refunded on cancellation before the slot starts. The database rejects overlapping bookings on a charger or a vehicle.
- **Charging session:** the price is locked at start. Cost is energy × rate + service fee (raised to the tariff minimum if needed) + GST, debited at stop. If the wallet cannot cover the total, the session is marked `pending` (amount due) rather than letting the wallet go negative.
- **Settlements:** a super admin batches an admin's unsettled collections for an area and period. Verifying it with a UTR number marks those collections settled; a partial amount leaves the batch in Pending Review.

## Not built yet

- **SMS:** with `OTP_DEMO_MODE=true` the OTP is always `DEMO_OTP` and is returned in the response, as in the frontend demo. Production needs an SMS provider at the `TODO` in `app/routers/auth.py`, and startup refuses demo mode when `APP_ENV=production`.
- **Payment gateway:** only the sandbox provider exists. Razorpay or Stripe order creation and webhook signature checks still need writing. Saving a card in sandbox validates the number, keeps only the brand, last 4 digits and expiry, and discards the rest.
- **Chargers:** with `CHARGING_SIMULATION=true` (the default), energy is calculated from charger power × elapsed time. It is capped by the customer's auto-stop battery level and by what the wallet can pay for. Real chargers should report through the `/ocpp/*` endpoints; then set `CHARGING_SIMULATION=false`.
