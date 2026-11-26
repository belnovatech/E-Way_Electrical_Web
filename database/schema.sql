-- E-Way Electrical schema (Postgres 15+)
-- run: psql -d eway_db -v ON_ERROR_STOP=1 -f database/schema.sql
-- see README.md for the ER diagram and which rules live in the db

BEGIN;

CREATE EXTENSION IF NOT EXISTS citext;      -- case-insensitive e-mail
CREATE EXTENSION IF NOT EXISTS btree_gist;  -- "=" inside EXCLUDE constraints
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- crypt()/gen_salt() for password hashes

-- enums. the UI spells things several ways ('Success'/'successful'), we use one
CREATE TYPE user_type              AS ENUM ('customer', 'admin', 'super_admin');
CREATE TYPE account_status         AS ENUM ('pending', 'active', 'inactive', 'suspended');
CREATE TYPE vehicle_type           AS ENUM ('bike', 'car', 'bus');
CREATE TYPE connector_type         AS ENUM ('CCS2', 'Type 2', 'GB/T', 'CHAdeMO');
CREATE TYPE current_type           AS ENUM ('AC', 'DC');
CREATE TYPE zone_status            AS ENUM ('active', 'degraded', 'offline', 'inactive');
CREATE TYPE station_status         AS ENUM ('active', 'offline', 'inactive');
CREATE TYPE charger_status         AS ENUM ('available', 'charging', 'reserved', 'maintenance', 'offline');
CREATE TYPE role_type              AS ENUM ('system', 'custom');
CREATE TYPE access_level           AS ENUM ('critical', 'elevated', 'operational', 'audit', 'read_only');
CREATE TYPE permission_category    AS ENUM ('grid_operations', 'maintenance', 'finance', 'users', 'security', 'reporting');
CREATE TYPE pricing_model          AS ENUM ('per_kwh', 'per_minute', 'flat', 'time_of_use');
CREATE TYPE tariff_status          AS ENUM ('draft', 'active', 'inactive');
CREATE TYPE reservation_status     AS ENUM ('upcoming', 'confirmed', 'active', 'completed', 'cancelled');
CREATE TYPE session_status         AS ENUM ('starting', 'charging', 'stopping', 'completed', 'cancelled', 'interrupted', 'failed', 'faulted');
CREATE TYPE session_payment_status AS ENUM ('pending', 'in_progress', 'paid', 'failed', 'refunded');
CREATE TYPE payment_method_type    AS ENUM ('card', 'upi', 'netbanking', 'digital_wallet', 'eway_wallet');
CREATE TYPE card_brand             AS ENUM ('visa', 'mastercard', 'rupay', 'amex', 'maestro', 'unknown');
CREATE TYPE payment_provider       AS ENUM ('sandbox', 'razorpay', 'stripe');
CREATE TYPE payment_order_type     AS ENUM ('wallet_topup', 'reservation', 'charging_session');
CREATE TYPE payment_status         AS ENUM ('created', 'authorizing', 'otp_required', 'success', 'failed', 'cancelled', 'refunded');
CREATE TYPE wallet_txn_type        AS ENUM ('topup', 'charging_fee', 'reservation_fee', 'refund', 'adjustment_credit', 'adjustment_debit', 'reversal');
CREATE TYPE txn_direction          AS ENUM ('credit', 'debit');
CREATE TYPE txn_status             AS ENUM ('pending', 'success', 'failed', 'cancelled');
CREATE TYPE collection_type        AS ENUM ('collection', 'charging_session', 'wallet_topup', 'refund', 'reversal');
CREATE TYPE collection_status      AS ENUM ('pending', 'success', 'failed', 'refunded', 'reversed');
CREATE TYPE collection_settlement  AS ENUM ('pending_settlement', 'in_escrow', 'settled', 'non_settled');
CREATE TYPE settlement_status      AS ENUM ('processing', 'pending_review', 'verified', 'disputed');
CREATE TYPE engineer_status        AS ENUM ('available', 'on_site', 'busy', 'off_duty');
CREATE TYPE ticket_severity        AS ENUM ('critical', 'high', 'medium', 'low');
CREATE TYPE ticket_priority        AS ENUM ('P1', 'P2', 'P3', 'P4');
CREATE TYPE maintenance_status     AS ENUM ('open', 'in_progress', 'resolved');
CREATE TYPE timeline_actor         AS ENUM ('system', 'technician', 'admin');
CREATE TYPE link_status            AS ENUM ('online', 'offline', 'degraded');
CREATE TYPE connector_health       AS ENUM ('healthy', 'fault', 'interlocked', 'warning');
CREATE TYPE support_status         AS ENUM ('open', 'in_progress', 'waiting_for_customer', 'resolved', 'closed');
CREATE TYPE support_sender         AS ENUM ('customer', 'support');
CREATE TYPE notification_type      AS ENUM ('charging', 'wallet', 'reservation', 'alert', 'system', 'maintenance', 'financial', 'security');
CREATE TYPE otp_purpose            AS ENUM ('login', 'phone_change');
CREATE TYPE audit_portal           AS ENUM ('customer', 'admin', 'super_admin', 'system');
CREATE TYPE audit_severity         AS ENUM ('info', 'notice', 'warning', 'critical');
CREATE TYPE audit_outcome          AS ENUM ('success', 'warning', 'failed');

-- zones / stations / charging points

-- admin "Zones" and super admin "Areas" are the same thing
CREATE TABLE zones (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code               varchar(20)   NOT NULL UNIQUE,                 -- 'AP-Z01'
    name               varchar(120)  NOT NULL,                        -- 'Vijayawada Central'
    address            text,
    city               varchar(80)   NOT NULL,
    state              varchar(80)   NOT NULL,
    latitude           numeric(9,6)  CHECK (latitude  BETWEEN -90  AND 90),
    longitude          numeric(9,6)  CHECK (longitude BETWEEN -180 AND 180),
    power_capacity_kw  numeric(10,2) NOT NULL DEFAULT 0 CHECK (power_capacity_kw >= 0),
    current_load_kw    numeric(10,2) NOT NULL DEFAULT 0 CHECK (current_load_kw >= 0), -- live telemetry snapshot
    operating_hours    varchar(60)   NOT NULL DEFAULT '24/7',
    contact_number     varchar(20),
    status             zone_status   NOT NULL DEFAULT 'active',
    created_at         timestamptz   NOT NULL DEFAULT now(),
    updated_at         timestamptz   NOT NULL DEFAULT now()
);

CREATE TABLE stations (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    zone_id                 bigint        NOT NULL REFERENCES zones(id) ON DELETE RESTRICT,
    code                    varchar(30)   NOT NULL UNIQUE,            -- 'st-hyd-01'
    name                    varchar(150)  NOT NULL,                   -- 'Vijayawada Central Hub'
    address                 text          NOT NULL,
    city                    varchar(80)   NOT NULL,
    latitude                numeric(9,6)  NOT NULL CHECK (latitude  BETWEEN -90  AND 90),
    longitude               numeric(9,6)  NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    operating_hours         varchar(60)   NOT NULL DEFAULT 'Open 24/7',
    amenities               text[]        NOT NULL DEFAULT '{}',
    supported_vehicle_types vehicle_type[] NOT NULL DEFAULT '{car}' CHECK (cardinality(supported_vehicle_types) > 0),
    status                  station_status NOT NULL DEFAULT 'active',
    created_at              timestamptz   NOT NULL DEFAULT now(),
    updated_at              timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (zone_id, name)
);
CREATE INDEX ix_stations_zone ON stations (zone_id);
CREATE INDEX ix_stations_city ON stations (city);

-- one row per gun / connector
CREATE TABLE charging_points (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station_id           bigint         NOT NULL REFERENCES stations(id) ON DELETE RESTRICT,
    point_code           varchar(30)    NOT NULL UNIQUE,
    charger_number       smallint       NOT NULL CHECK (charger_number > 0),
    display_name         varchar(80),
    current_type         current_type   NOT NULL,
    connector_type       connector_type NOT NULL,
    power_kw             numeric(6,2)   NOT NULL CHECK (power_kw > 0),
    ocpp_id              varchar(50)    UNIQUE,
    status               charger_status NOT NULL DEFAULT 'available',
    last_ping_at         timestamptz,
    last_maintenance_at  timestamptz,
    commissioned_on      date,
    created_at           timestamptz    NOT NULL DEFAULT now(),
    updated_at           timestamptz    NOT NULL DEFAULT now(),
    UNIQUE (station_id, charger_number),
    UNIQUE (id, station_id)                  -- target for composite FK from support_tickets
);
CREATE INDEX ix_charging_points_station ON charging_points (station_id);
CREATE INDEX ix_charging_points_status  ON charging_points (status);

-- roles & permissions

CREATE TABLE permissions (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code          varchar(50)  NOT NULL UNIQUE CHECK (code ~ '^[A-Z0-9_]+$'),  -- 'TARIFF_EDIT'
    name          varchar(120) NOT NULL,
    description   text         NOT NULL,
    category      permission_category NOT NULL,
    is_dangerous  boolean      NOT NULL DEFAULT false
);

CREATE TABLE roles (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code          varchar(50)  NOT NULL UNIQUE CHECK (code ~ '^ROLE_[A-Z0-9_]+$'),  -- 'ROLE_ZONE_MANAGER'
    name          varchar(100) NOT NULL UNIQUE,
    description   text         NOT NULL,
    role_type     role_type    NOT NULL DEFAULT 'custom',
    access_level  access_level NOT NULL DEFAULT 'operational',
    is_protected  boolean      NOT NULL DEFAULT false,      -- protected roles cannot be edited/deleted
    is_active     boolean      NOT NULL DEFAULT true,
    created_by    bigint,                                   -- FK added after users exists
    updated_by    bigint,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE role_permissions (
    role_id        bigint NOT NULL REFERENCES roles(id)       ON DELETE CASCADE,
    permission_id  bigint NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (role_id, permission_id)
);
CREATE INDEX ix_role_permissions_permission ON role_permissions (permission_id);

-- users

-- customers log in with phone + OTP, staff with email + password
CREATE TABLE users (
    id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_type              user_type      NOT NULL,
    public_code            varchar(20)    UNIQUE,              -- 'CUST-1001', 'ADM-AP01', 'STF-003'
    full_name              varchar(120),                       -- NULL until customer completes profile
    country_code           varchar(5)     NOT NULL DEFAULT '+91' CHECK (country_code ~ '^\+[0-9]{1,4}$'),
    phone_number           varchar(15)    CHECK (phone_number ~ '^[0-9]{6,15}$'),
    email                  citext         CHECK (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    password_hash          text,                               -- bcrypt; NULL for OTP-only customers
    recovery_pin_hash      text,
    two_factor_enabled     boolean        NOT NULL DEFAULT false,
    status                 account_status NOT NULL DEFAULT 'active',
    is_verified            boolean        NOT NULL DEFAULT false,
    avatar_url             text,
    failed_login_attempts  smallint       NOT NULL DEFAULT 0 CHECK (failed_login_attempts >= 0),
    locked_until           timestamptz,
    password_changed_at    timestamptz,
    last_login_at          timestamptz,
    last_active_at         timestamptz,
    created_at             timestamptz    NOT NULL DEFAULT now(),
    updated_at             timestamptz    NOT NULL DEFAULT now(),
    UNIQUE (id, user_type),                                    -- target for profile FKs
    CONSTRAINT users_customer_needs_phone CHECK (user_type <> 'customer' OR phone_number IS NOT NULL),
    CONSTRAINT users_staff_needs_login    CHECK (user_type =  'customer' OR (email IS NOT NULL AND password_hash IS NOT NULL))
);
-- same person can be a customer and staff, so unique per user_type
CREATE UNIQUE INDEX ux_users_phone ON users (user_type, country_code, phone_number) WHERE phone_number IS NOT NULL;
CREATE UNIQUE INDEX ux_users_email ON users (user_type, email)                       WHERE email IS NOT NULL;

ALTER TABLE roles
    ADD CONSTRAINT fk_roles_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_roles_updated_by FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL;

-- commission / bank fields are only used for area admins
CREATE TABLE staff_profiles (
    user_id                  bigint       PRIMARY KEY,
    user_type                user_type    NOT NULL DEFAULT 'admin' CHECK (user_type IN ('admin', 'super_admin')),
    role_id                  bigint       NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    department               varchar(100),
    city                     varchar(80),
    commission_rate_percent  numeric(5,2) CHECK (commission_rate_percent BETWEEN 0 AND 100),
    bank_name                varchar(100),
    bank_account_masked      varchar(30),                       -- never the full account number
    bank_ifsc                char(11)     CHECK (bank_ifsc ~ '^[A-Z]{4}0[A-Z0-9]{6}$'),
    settlement_cycle_days    smallint     NOT NULL DEFAULT 15 CHECK (settlement_cycle_days > 0),
    role_assigned_at         timestamptz  NOT NULL DEFAULT now(),
    role_assigned_by         bigint       REFERENCES users(id) ON DELETE SET NULL,
    created_at               timestamptz  NOT NULL DEFAULT now(),
    updated_at               timestamptz  NOT NULL DEFAULT now(),
    FOREIGN KEY (user_id, user_type) REFERENCES users(id, user_type) ON DELETE CASCADE
);
CREATE INDEX ix_staff_profiles_role ON staff_profiles (role_id);

-- don't delete these, deactivate - collections/settlements point at them
CREATE TABLE admin_zone_assignments (
    admin_id       bigint      NOT NULL REFERENCES staff_profiles(user_id) ON DELETE CASCADE,
    zone_id        bigint      NOT NULL REFERENCES zones(id)               ON DELETE CASCADE,
    is_active      boolean     NOT NULL DEFAULT true,
    assigned_at    timestamptz NOT NULL DEFAULT now(),
    assigned_by    bigint      REFERENCES users(id) ON DELETE SET NULL,
    unassigned_at  timestamptz,
    PRIMARY KEY (admin_id, zone_id),
    CHECK (is_active = (unassigned_at IS NULL)),
    CHECK (unassigned_at IS NULL OR unassigned_at >= assigned_at)
);
CREATE INDEX ix_admin_zone_assignments_zone ON admin_zone_assignments (zone_id);

CREATE TABLE customer_profiles (
    user_id                    bigint      PRIMARY KEY,
    user_type                  user_type   NOT NULL DEFAULT 'customer' CHECK (user_type = 'customer'),
    city                       varchar(80),
    state                      varchar(80),
    zone_id                    bigint      REFERENCES zones(id) ON DELETE SET NULL,   -- home area
    assigned_admin_id          bigint,                                             -- responsible area admin
    is_profile_complete        boolean     NOT NULL DEFAULT false,
    company_name               varchar(150),                                       -- GST invoicing
    gstin                      char(15)    CHECK (gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'),
    sms_notifications          boolean     NOT NULL DEFAULT true,
    push_notifications         boolean     NOT NULL DEFAULT true,
    email_invoices             boolean     NOT NULL DEFAULT true,
    auto_stop_battery_percent  smallint    NOT NULL DEFAULT 90 CHECK (auto_stop_battery_percent BETWEEN 50 AND 100),
    preferred_language         varchar(30) NOT NULL DEFAULT 'English',
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (user_id, user_type) REFERENCES users(id, user_type) ON DELETE CASCADE,
    -- assigned admin has to manage the customer's zone
    CONSTRAINT fk_customer_admin_zone FOREIGN KEY (assigned_admin_id, zone_id)
        REFERENCES admin_zone_assignments(admin_id, zone_id) ON DELETE SET NULL (assigned_admin_id),
    CHECK (assigned_admin_id IS NULL OR zone_id IS NOT NULL)
);
CREATE INDEX ix_customer_profiles_zone  ON customer_profiles (zone_id);
CREATE INDEX ix_customer_profiles_admin ON customer_profiles (assigned_admin_id);

-- staff with ROLE_FIELD_ENGINEER
CREATE TABLE field_engineers (
    user_id         bigint          PRIMARY KEY REFERENCES staff_profiles(user_id) ON DELETE CASCADE,
    engineer_code   varchar(20)     NOT NULL UNIQUE,              -- 'ENG-101'
    zone_id         bigint          NOT NULL REFERENCES zones(id) ON DELETE RESTRICT,
    specialization  varchar(150),
    availability    engineer_status NOT NULL DEFAULT 'available',
    created_at      timestamptz     NOT NULL DEFAULT now(),
    updated_at      timestamptz     NOT NULL DEFAULT now()
);
CREATE INDEX ix_field_engineers_zone ON field_engineers (zone_id);

-- auth

CREATE TABLE otp_requests (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    country_code  varchar(5)  NOT NULL DEFAULT '+91',
    phone_number  varchar(15) NOT NULL CHECK (phone_number ~ '^[0-9]{6,15}$'),
    user_type     user_type   NOT NULL DEFAULT 'customer',
    purpose       otp_purpose NOT NULL DEFAULT 'login',
    otp_hash      text        NOT NULL,                       -- never store the plain OTP
    attempts      smallint    NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 10),
    expires_at    timestamptz NOT NULL,
    verified_at   timestamptz,
    ip_address    inet,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at > created_at)
);
CREATE INDEX ix_otp_requests_phone ON otp_requests (country_code, phone_number, created_at DESC);

CREATE TABLE user_sessions (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      text        NOT NULL UNIQUE,
    ip_address      inet,
    device          varchar(200),
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_active_at  timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    CHECK (expires_at > created_at)
);
CREATE INDEX ix_user_sessions_user ON user_sessions (user_id) WHERE revoked_at IS NULL;

-- vehicles, tariffs

CREATE TABLE vehicles (
    id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id              bigint         NOT NULL REFERENCES customer_profiles(user_id) ON DELETE CASCADE,
    nickname              varchar(80),                          -- 'My Nexon'
    manufacturer          varchar(60),
    model                 varchar(80)    NOT NULL,
    manufacture_year      smallint       CHECK (manufacture_year BETWEEN 2000 AND 2100),
    vehicle_type          vehicle_type   NOT NULL,
    license_plate         varchar(20)    NOT NULL,              -- 'AP-39-AB-1234'
    battery_capacity_kwh  numeric(6,2)   CHECK (battery_capacity_kwh > 0),
    connector_type        connector_type,
    battery_percentage    smallint       CHECK (battery_percentage BETWEEN 0 AND 100),  -- last known SoC
    is_default            boolean        NOT NULL DEFAULT false,
    is_verified           boolean        NOT NULL DEFAULT false,
    deleted_at            timestamptz,                          -- soft delete keeps session history
    created_at            timestamptz    NOT NULL DEFAULT now(),
    updated_at            timestamptz    NOT NULL DEFAULT now(),
    UNIQUE (id, owner_id),                                      -- target for "vehicle belongs to customer" FKs
    CHECK (NOT (is_default AND deleted_at IS NOT NULL))
);
-- plate unique ignoring spaces, dashes, case
CREATE UNIQUE INDEX ux_vehicles_plate ON vehicles (upper(regexp_replace(license_plate, '[^A-Za-z0-9]', '', 'g'))) WHERE deleted_at IS NULL;
-- only one default vehicle
CREATE UNIQUE INDEX ux_vehicles_one_default ON vehicles (owner_id) WHERE is_default;
CREATE INDEX ix_vehicles_owner ON vehicles (owner_id);

-- zone_id NULL = global rate. lowest priority number wins
CREATE TABLE tariff_plans (
    id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code                     varchar(20)   NOT NULL UNIQUE,      -- 'T-004'
    name                     varchar(120)  NOT NULL,
    zone_id                  bigint        REFERENCES zones(id) ON DELETE CASCADE,
    vehicle_type             vehicle_type  NOT NULL,
    pricing_model            pricing_model NOT NULL DEFAULT 'per_kwh',
    rate_per_kwh             numeric(8,2)  NOT NULL CHECK (rate_per_kwh >= 0),
    peak_rate_per_kwh        numeric(8,2)  CHECK (peak_rate_per_kwh >= 0),
    peak_start               time,
    peak_end                 time,
    off_peak_rate_per_kwh    numeric(8,2)  CHECK (off_peak_rate_per_kwh >= 0),
    service_fee              numeric(8,2)  NOT NULL DEFAULT 0 CHECK (service_fee >= 0),
    gst_percent              numeric(5,2)  NOT NULL DEFAULT 18 CHECK (gst_percent BETWEEN 0 AND 100),
    min_charge               numeric(8,2)  NOT NULL DEFAULT 0 CHECK (min_charge >= 0),
    idle_fee_per_minute      numeric(8,2)  NOT NULL DEFAULT 0 CHECK (idle_fee_per_minute >= 0),
    parking_fee_per_hour     numeric(8,2)  NOT NULL DEFAULT 0 CHECK (parking_fee_per_hour >= 0),
    reservation_slot_fee     numeric(8,2)  NOT NULL DEFAULT 0 CHECK (reservation_slot_fee >= 0),
    effective_from           date          NOT NULL,
    effective_to             date,
    priority                 smallint      NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 9),
    status                   tariff_status NOT NULL DEFAULT 'active',
    created_by               bigint        REFERENCES users(id) ON DELETE SET NULL,
    updated_by               bigint        REFERENCES users(id) ON DELETE SET NULL,
    created_at               timestamptz   NOT NULL DEFAULT now(),
    updated_at               timestamptz   NOT NULL DEFAULT now(),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK ((peak_start IS NULL) = (peak_end IS NULL)),
    CHECK (pricing_model <> 'time_of_use' OR (peak_rate_per_kwh IS NOT NULL AND peak_start IS NOT NULL)),
    -- no two active plans with the same scope + priority over overlapping dates
    CONSTRAINT ex_tariff_overlap EXCLUDE USING gist (
        (COALESCE(zone_id, 0)) WITH =,
        vehicle_type WITH =,
        priority WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    ) WHERE (status = 'active')
);
CREATE INDEX ix_tariff_plans_zone ON tariff_plans (zone_id);

-- reservations, charging sessions

CREATE TABLE reservations (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reservation_number  varchar(30)  NOT NULL UNIQUE,            -- 'RSV-20261108-0001'
    customer_id         bigint       NOT NULL REFERENCES customer_profiles(user_id) ON DELETE RESTRICT,
    vehicle_id          bigint       NOT NULL,
    charging_point_id   bigint       NOT NULL REFERENCES charging_points(id) ON DELETE RESTRICT,
    tariff_plan_id      bigint       REFERENCES tariff_plans(id) ON DELETE SET NULL,
    scheduled_start     timestamptz  NOT NULL,
    scheduled_end       timestamptz  NOT NULL,
    rate_per_kwh        numeric(8,2) NOT NULL CHECK (rate_per_kwh >= 0),     -- price locked at booking
    slot_fee            numeric(10,2) NOT NULL DEFAULT 0 CHECK (slot_fee >= 0),
    estimated_cost      numeric(10,2) CHECK (estimated_cost >= 0),
    status              reservation_status NOT NULL DEFAULT 'upcoming',
    cancelled_at        timestamptz,
    cancellation_reason text,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (id, customer_id),
    CONSTRAINT fk_reservation_vehicle_owner FOREIGN KEY (vehicle_id, customer_id)
        REFERENCES vehicles(id, owner_id) ON DELETE RESTRICT,
    CHECK (scheduled_end > scheduled_start),
    CHECK (scheduled_end - scheduled_start <= interval '12 hours'),
    CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL)),
    -- a charger cannot be double-booked
    CONSTRAINT ex_reservation_charger_overlap EXCLUDE USING gist (
        charging_point_id WITH =,
        tstzrange(scheduled_start, scheduled_end) WITH &&
    ) WHERE (status IN ('upcoming', 'confirmed', 'active')),
    -- one vehicle cannot hold two overlapping bookings
    CONSTRAINT ex_reservation_vehicle_overlap EXCLUDE USING gist (
        vehicle_id WITH =,
        tstzrange(scheduled_start, scheduled_end) WITH &&
    ) WHERE (status IN ('upcoming', 'confirmed', 'active'))
);
CREATE INDEX ix_reservations_customer ON reservations (customer_id, scheduled_start DESC);
CREATE INDEX ix_reservations_point    ON reservations (charging_point_id, scheduled_start);

CREATE TABLE charging_sessions (
    id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_number        varchar(30)    NOT NULL UNIQUE,         -- 'SES-20261108-0001'
    customer_id           bigint         NOT NULL REFERENCES customer_profiles(user_id) ON DELETE RESTRICT,
    vehicle_id            bigint         NOT NULL,
    charging_point_id     bigint         NOT NULL REFERENCES charging_points(id) ON DELETE RESTRICT,
    reservation_id        bigint         UNIQUE,                  -- a reservation is used at most once
    tariff_plan_id        bigint         REFERENCES tariff_plans(id) ON DELETE SET NULL,
    status                session_status NOT NULL DEFAULT 'starting',
    started_at            timestamptz    NOT NULL DEFAULT now(),
    ended_at              timestamptz,
    start_soc             smallint       CHECK (start_soc BETWEEN 0 AND 100),
    current_soc           smallint       CHECK (current_soc BETWEEN 0 AND 100),   -- final SoC once ended
    energy_delivered_kwh  numeric(10,3)  NOT NULL DEFAULT 0 CHECK (energy_delivered_kwh >= 0),
    peak_power_kw         numeric(6,2)   CHECK (peak_power_kw >= 0),
    rate_per_kwh          numeric(8,2)   NOT NULL CHECK (rate_per_kwh >= 0),      -- price snapshot
    energy_cost           numeric(10,2)  NOT NULL DEFAULT 0 CHECK (energy_cost >= 0),
    service_fee           numeric(10,2)  NOT NULL DEFAULT 0 CHECK (service_fee >= 0),
    idle_fee              numeric(10,2)  NOT NULL DEFAULT 0 CHECK (idle_fee >= 0),
    gst_amount            numeric(10,2)  NOT NULL DEFAULT 0 CHECK (gst_amount >= 0),
    total_amount          numeric(10,2)  GENERATED ALWAYS AS (energy_cost + service_fee + idle_fee + gst_amount) STORED,
    payment_status        session_payment_status NOT NULL DEFAULT 'pending',
    stop_reason           varchar(120),
    created_at            timestamptz    NOT NULL DEFAULT now(),
    updated_at            timestamptz    NOT NULL DEFAULT now(),
    UNIQUE (id, customer_id),
    CONSTRAINT fk_session_vehicle_owner FOREIGN KEY (vehicle_id, customer_id)
        REFERENCES vehicles(id, owner_id) ON DELETE RESTRICT,
    -- reservation must belong to the same customer
    CONSTRAINT fk_session_reservation_owner FOREIGN KEY (reservation_id, customer_id)
        REFERENCES reservations(id, customer_id) ON DELETE SET NULL (reservation_id),
    CHECK ((status IN ('starting', 'charging', 'stopping')) = (ended_at IS NULL)),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
-- one live session per charger and per vehicle
CREATE UNIQUE INDEX ux_sessions_live_point   ON charging_sessions (charging_point_id) WHERE status IN ('starting', 'charging', 'stopping');
CREATE UNIQUE INDEX ux_sessions_live_vehicle ON charging_sessions (vehicle_id)        WHERE status IN ('starting', 'charging', 'stopping');
CREATE INDEX ix_sessions_customer ON charging_sessions (customer_id, started_at DESC);
CREATE INDEX ix_sessions_point    ON charging_sessions (charging_point_id, started_at DESC);
CREATE INDEX ix_sessions_started  ON charging_sessions (started_at);

-- wallet & payments

-- created by trigger when the customer profile is inserted
CREATE TABLE wallets (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id  bigint        NOT NULL UNIQUE REFERENCES customer_profiles(user_id) ON DELETE CASCADE,
    balance      numeric(12,2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    currency     char(3)       NOT NULL DEFAULT 'INR',
    created_at   timestamptz   NOT NULL DEFAULT now(),
    updated_at   timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (id, customer_id)
);

-- only the gateway token + display bits. NEVER store full card no / cvv
CREATE TABLE payment_methods (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id        bigint              NOT NULL REFERENCES customer_profiles(user_id) ON DELETE CASCADE,
    method_type        payment_method_type NOT NULL CHECK (method_type IN ('card', 'upi', 'netbanking')),
    title              varchar(80)         NOT NULL,              -- 'HDFC Visa'
    subtitle           varchar(120),                              -- 'Expires 08/28'
    card_brand         card_brand,
    card_last4         char(4)             CHECK (card_last4 ~ '^[0-9]{4}$'),
    card_expiry_month  smallint            CHECK (card_expiry_month BETWEEN 1 AND 12),
    card_expiry_year   smallint            CHECK (card_expiry_year BETWEEN 2000 AND 2100),
    upi_vpa            varchar(100)        CHECK (upi_vpa ~ '^[A-Za-z0-9._-]{2,}@[A-Za-z]{2,}$'),
    bank_code          varchar(20),
    bank_name          varchar(100),
    gateway_token      text,
    is_default         boolean             NOT NULL DEFAULT false,
    created_at         timestamptz         NOT NULL DEFAULT now(),
    updated_at         timestamptz         NOT NULL DEFAULT now(),
    UNIQUE (id, customer_id),
    CHECK (method_type <> 'card'       OR (card_brand IS NOT NULL AND card_last4 IS NOT NULL AND card_expiry_month IS NOT NULL AND card_expiry_year IS NOT NULL)),
    CHECK (method_type <> 'upi'        OR upi_vpa IS NOT NULL),
    CHECK (method_type <> 'netbanking' OR bank_code IS NOT NULL)
);
CREATE UNIQUE INDEX ux_payment_methods_one_default ON payment_methods (customer_id) WHERE is_default;

-- one row per gateway order
CREATE TABLE payments (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id         bigint              NOT NULL REFERENCES customer_profiles(user_id) ON DELETE RESTRICT,
    order_type          payment_order_type  NOT NULL,
    reservation_id      bigint,
    charging_session_id bigint,
    provider            payment_provider    NOT NULL DEFAULT 'sandbox',
    method_type         payment_method_type,
    payment_method_id   bigint,                                   -- saved method used, if any
    method_summary      varchar(120),                             -- 'Visa **** 4242', 'user@okhdfc'
    base_amount         numeric(10,2)       NOT NULL CHECK (base_amount > 0),
    convenience_fee     numeric(10,2)       NOT NULL DEFAULT 0 CHECK (convenience_fee >= 0),
    gst_amount          numeric(10,2)       NOT NULL DEFAULT 0 CHECK (gst_amount >= 0),
    discount_amount     numeric(10,2)       NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
    total_amount        numeric(10,2)       GENERATED ALWAYS AS (base_amount + convenience_fee + gst_amount - discount_amount) STORED,
    currency            char(3)             NOT NULL DEFAULT 'INR',
    idempotency_key     varchar(80)         NOT NULL UNIQUE,
    gateway_order_id    varchar(100)        UNIQUE,
    gateway_payment_id  varchar(100)        UNIQUE,
    gateway_signature   text,
    receipt_number      varchar(40)         UNIQUE,
    status              payment_status      NOT NULL DEFAULT 'created',
    error_code          varchar(60),
    error_message       text,
    created_at          timestamptz         NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    updated_at          timestamptz         NOT NULL DEFAULT now(),
    UNIQUE (id, customer_id),
    CONSTRAINT fk_payment_saved_method FOREIGN KEY (payment_method_id, customer_id)
        REFERENCES payment_methods(id, customer_id) ON DELETE SET NULL (payment_method_id),
    CONSTRAINT fk_payment_reservation  FOREIGN KEY (reservation_id, customer_id)
        REFERENCES reservations(id, customer_id) ON DELETE RESTRICT,
    CONSTRAINT fk_payment_session      FOREIGN KEY (charging_session_id, customer_id)
        REFERENCES charging_sessions(id, customer_id) ON DELETE RESTRICT,
    CHECK (discount_amount <= base_amount + convenience_fee + gst_amount),
    CHECK (
        (order_type = 'wallet_topup'     AND reservation_id IS NULL     AND charging_session_id IS NULL) OR
        (order_type = 'reservation'      AND reservation_id IS NOT NULL AND charging_session_id IS NULL) OR
        (order_type = 'charging_session' AND charging_session_id IS NOT NULL)
    ),
    CHECK (status <> 'success' OR (completed_at IS NOT NULL AND gateway_payment_id IS NOT NULL))
);
CREATE INDEX ix_payments_customer ON payments (customer_id, created_at DESC);

-- the ledger. trigger fills balance_* when status -> success.
-- posted rows can't be changed, add a reversal instead
CREATE TABLE wallet_transactions (
    id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    wallet_id                bigint          NOT NULL,
    customer_id              bigint          NOT NULL,
    transaction_ref          varchar(40)     NOT NULL UNIQUE,     -- 'TXN-...'
    invoice_number           varchar(40)     UNIQUE,
    txn_type                 wallet_txn_type NOT NULL,
    direction                txn_direction   NOT NULL,
    amount                   numeric(12,2)   NOT NULL CHECK (amount > 0),
    balance_before           numeric(12,2),
    balance_after            numeric(12,2)   CHECK (balance_after >= 0),
    status                   txn_status      NOT NULL DEFAULT 'pending',
    title                    varchar(120),
    description              text,
    payment_id               bigint          UNIQUE,
    charging_session_id      bigint,
    reservation_id           bigint,
    reverses_transaction_id  bigint          UNIQUE REFERENCES wallet_transactions(id) ON DELETE RESTRICT,
    created_by               bigint          REFERENCES users(id) ON DELETE SET NULL,  -- admin for manual adjustments
    created_at               timestamptz     NOT NULL DEFAULT now(),
    posted_at                timestamptz,
    CONSTRAINT fk_wallet_txn_wallet  FOREIGN KEY (wallet_id, customer_id)
        REFERENCES wallets(id, customer_id) ON DELETE RESTRICT,
    CONSTRAINT fk_wallet_txn_payment FOREIGN KEY (payment_id, customer_id)
        REFERENCES payments(id, customer_id) ON DELETE RESTRICT,
    CONSTRAINT fk_wallet_txn_session FOREIGN KEY (charging_session_id, customer_id)
        REFERENCES charging_sessions(id, customer_id) ON DELETE RESTRICT,
    CONSTRAINT fk_wallet_txn_reservation FOREIGN KEY (reservation_id, customer_id)
        REFERENCES reservations(id, customer_id) ON DELETE RESTRICT,
    CHECK (
        (txn_type IN ('topup', 'refund', 'adjustment_credit')               AND direction = 'credit') OR
        (txn_type IN ('charging_fee', 'reservation_fee', 'adjustment_debit') AND direction = 'debit')  OR
        (txn_type = 'reversal')
    ),
    CHECK (txn_type <> 'topup'           OR payment_id IS NOT NULL),
    CHECK (txn_type <> 'charging_fee'    OR charging_session_id IS NOT NULL),
    CHECK (txn_type <> 'reservation_fee' OR reservation_id IS NOT NULL),
    CHECK ((txn_type = 'reversal') = (reverses_transaction_id IS NOT NULL)),
    CHECK (txn_type NOT IN ('adjustment_credit', 'adjustment_debit') OR created_by IS NOT NULL),
    CHECK ((status = 'success') = (balance_after IS NOT NULL AND balance_before IS NOT NULL AND posted_at IS NOT NULL)),
    CHECK (balance_after IS NULL OR
           balance_after = balance_before + CASE WHEN direction = 'credit' THEN amount ELSE -amount END)
);
CREATE INDEX ix_wallet_txn_wallet  ON wallet_transactions (wallet_id, created_at DESC);
CREATE INDEX ix_wallet_txn_session ON wallet_transactions (charging_session_id);

-- area admin collections + settlements

CREATE TABLE admin_settlements (
    id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_number              varchar(40)   NOT NULL UNIQUE,      -- 'VG-STL-AP01-202610A'
    admin_id                  bigint        NOT NULL,
    zone_id                   bigint        NOT NULL,
    period_start              date          NOT NULL,
    period_end                date          NOT NULL,
    gross_collections         numeric(14,2) NOT NULL DEFAULT 0 CHECK (gross_collections >= 0),
    refunds_deducted          numeric(14,2) NOT NULL DEFAULT 0 CHECK (refunds_deducted >= 0),
    commission_deducted       numeric(14,2) NOT NULL DEFAULT 0 CHECK (commission_deducted >= 0),
    other_adjustments         numeric(14,2) NOT NULL DEFAULT 0,
    net_payable               numeric(14,2) GENERATED ALWAYS AS
                                  (gross_collections - refunds_deducted - commission_deducted + other_adjustments) STORED,
    verified_amount_received  numeric(14,2) NOT NULL DEFAULT 0 CHECK (verified_amount_received >= 0),
    outstanding_balance       numeric(14,2) GENERATED ALWAYS AS
                                  (gross_collections - refunds_deducted - commission_deducted + other_adjustments
                                   - verified_amount_received) STORED,
    status                    settlement_status NOT NULL DEFAULT 'processing',
    payment_reference_utr     varchar(30)   UNIQUE,
    settlement_date           date,
    verified_by               bigint        REFERENCES users(id) ON DELETE SET NULL,
    verified_at               timestamptz,
    notes                     text,
    created_at                timestamptz   NOT NULL DEFAULT now(),
    updated_at                timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (id, admin_id, zone_id),
    CONSTRAINT fk_settlement_admin_zone FOREIGN KEY (admin_id, zone_id)
        REFERENCES admin_zone_assignments(admin_id, zone_id) ON DELETE RESTRICT,
    CHECK (period_end >= period_start),
    CHECK (status <> 'verified' OR (verified_by IS NOT NULL AND verified_at IS NOT NULL AND payment_reference_utr IS NOT NULL)),
    -- an admin's settlement periods for one zone never overlap
    CONSTRAINT ex_settlement_period_overlap EXCLUDE USING gist (
        admin_id WITH =,
        zone_id WITH =,
        daterange(period_start, period_end, '[]') WITH &&
    )
);

-- money collected per area admin (super admin "Transactions")
CREATE TABLE admin_collections (
    id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reference_number       varchar(40)        NOT NULL UNIQUE,    -- 'VG-REC-20261028-001'
    admin_id               bigint             NOT NULL,
    zone_id                bigint             NOT NULL,
    customer_id            bigint             NOT NULL REFERENCES customer_profiles(user_id) ON DELETE RESTRICT,
    collection_type        collection_type    NOT NULL,
    payment_method         payment_method_type NOT NULL,
    amount                 numeric(12,2)      NOT NULL CHECK (amount > 0),
    admin_commission       numeric(12,2)      NOT NULL DEFAULT 0 CHECK (admin_commission >= 0),
    net_payable            numeric(12,2)      GENERATED ALWAYS AS (amount - admin_commission) STORED,
    transaction_status     collection_status  NOT NULL DEFAULT 'pending',
    settlement_status      collection_settlement NOT NULL DEFAULT 'pending_settlement',
    settlement_id          bigint,
    settlement_due_date    date,
    payment_id             bigint,
    charging_session_id    bigint,
    notes                  text,
    collected_at           timestamptz        NOT NULL DEFAULT now(),
    created_at             timestamptz        NOT NULL DEFAULT now(),
    updated_at             timestamptz        NOT NULL DEFAULT now(),
    CHECK (admin_commission <= amount),
    CONSTRAINT fk_collection_admin_zone FOREIGN KEY (admin_id, zone_id)
        REFERENCES admin_zone_assignments(admin_id, zone_id) ON DELETE RESTRICT,
    -- the settlement batch must be for the same admin and zone
    CONSTRAINT fk_collection_settlement FOREIGN KEY (settlement_id, admin_id, zone_id)
        REFERENCES admin_settlements(id, admin_id, zone_id) ON DELETE SET NULL (settlement_id),
    CONSTRAINT fk_collection_payment FOREIGN KEY (payment_id, customer_id)
        REFERENCES payments(id, customer_id) ON DELETE RESTRICT,
    CONSTRAINT fk_collection_session FOREIGN KEY (charging_session_id, customer_id)
        REFERENCES charging_sessions(id, customer_id) ON DELETE RESTRICT,
    CHECK (settlement_status <> 'settled' OR settlement_id IS NOT NULL)
);
CREATE INDEX ix_collections_admin_zone ON admin_collections (admin_id, zone_id, collected_at DESC);
CREATE INDEX ix_collections_customer   ON admin_collections (customer_id);
CREATE INDEX ix_collections_settlement ON admin_collections (settlement_id);

-- maintenance

CREATE TABLE maintenance_tickets (
    id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_code           varchar(30)        NOT NULL UNIQUE,      -- 'MT-1024'
    charging_point_id     bigint             NOT NULL REFERENCES charging_points(id) ON DELETE RESTRICT,
    fault_code            varchar(60),                             -- 'OCPP_CONNECTOR_LOCK_FAIL'
    issue_description     text               NOT NULL,
    severity              ticket_severity    NOT NULL,
    priority              ticket_priority    NOT NULL DEFAULT 'P3',
    status                maintenance_status NOT NULL DEFAULT 'open',
    reported_at           timestamptz        NOT NULL DEFAULT now(),
    reported_by           bigint             REFERENCES users(id) ON DELETE SET NULL,  -- NULL = raised by OCPP telemetry
    assigned_engineer_id  bigint             REFERENCES field_engineers(user_id) ON DELETE SET NULL,
    assigned_at           timestamptz,
    sla_minutes           integer            NOT NULL DEFAULT 240 CHECK (sla_minutes > 0),
    resolved_at           timestamptz,
    resolution_notes      text,
    notes                 text,
    created_at            timestamptz        NOT NULL DEFAULT now(),
    updated_at            timestamptz        NOT NULL DEFAULT now(),
    CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)),
    CHECK (resolved_at IS NULL OR resolved_at >= reported_at),
    CHECK ((assigned_engineer_id IS NULL) = (assigned_at IS NULL))
);
CREATE INDEX ix_maintenance_point    ON maintenance_tickets (charging_point_id);
CREATE INDEX ix_maintenance_engineer ON maintenance_tickets (assigned_engineer_id) WHERE status <> 'resolved';
CREATE INDEX ix_maintenance_open     ON maintenance_tickets (status, priority) WHERE status <> 'resolved';

CREATE TABLE maintenance_ticket_events (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id      bigint         NOT NULL REFERENCES maintenance_tickets(id) ON DELETE CASCADE,
    actor_user_id  bigint         REFERENCES users(id) ON DELETE SET NULL,
    actor_name     varchar(120)   NOT NULL,                        -- snapshot, survives user deletion
    actor_type     timeline_actor NOT NULL,
    action         varchar(200)   NOT NULL,
    note           text,
    created_at     timestamptz    NOT NULL DEFAULT now()
);
CREATE INDEX ix_ticket_events_ticket ON maintenance_ticket_events (ticket_id, created_at);

-- charger telemetry snapshots, shown on the ticket page
CREATE TABLE charger_diagnostics (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    charging_point_id  bigint           NOT NULL REFERENCES charging_points(id) ON DELETE CASCADE,
    ticket_id          bigint           REFERENCES maintenance_tickets(id) ON DELETE SET NULL,
    connection_status  link_status      NOT NULL,
    connector_status   connector_health NOT NULL,
    power_level_kw     numeric(6,2)     CHECK (power_level_kw >= 0),
    rated_power_kw     numeric(6,2)     CHECK (rated_power_kw > 0),
    temperature_c      numeric(5,1),
    ground_leakage_ma  numeric(6,2)     CHECK (ground_leakage_ma >= 0),
    voltage_v          numeric(7,2),
    current_a          numeric(7,2),
    ocpp_error_code    varchar(60),
    vendor_error_code  varchar(60),
    recorded_at        timestamptz      NOT NULL DEFAULT now()
);
CREATE INDEX ix_diagnostics_point ON charger_diagnostics (charging_point_id, recorded_at DESC);

-- support, notifications

CREATE TABLE support_tickets (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_number      varchar(30)    NOT NULL UNIQUE,             -- 'TKT-1001'
    customer_id        bigint         NOT NULL REFERENCES customer_profiles(user_id) ON DELETE CASCADE,
    category           varchar(60)    NOT NULL,                    -- 'Charging Issues', 'Wallet & Payments'
    subject            varchar(200)   NOT NULL,
    description        text,
    station_id         bigint         REFERENCES stations(id) ON DELETE SET NULL,
    charging_point_id  bigint,
    status             support_status NOT NULL DEFAULT 'open',
    assigned_to        bigint         REFERENCES staff_profiles(user_id) ON DELETE SET NULL,
    created_at         timestamptz    NOT NULL DEFAULT now(),
    updated_at         timestamptz    NOT NULL DEFAULT now(),
    resolved_at        timestamptz,
    -- if a charger is given it must be at the given station
    CONSTRAINT fk_support_point_station FOREIGN KEY (charging_point_id, station_id)
        REFERENCES charging_points(id, station_id) ON DELETE SET NULL (charging_point_id),
    CHECK (charging_point_id IS NULL OR station_id IS NOT NULL),
    CHECK (resolved_at IS NULL OR status IN ('resolved', 'closed'))
);
CREATE INDEX ix_support_tickets_customer ON support_tickets (customer_id, created_at DESC);

CREATE TABLE support_ticket_replies (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id       bigint         NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    sender          support_sender NOT NULL,
    sender_user_id  bigint         REFERENCES users(id) ON DELETE SET NULL,
    message         text           NOT NULL CHECK (length(trim(message)) > 0),
    created_at      timestamptz    NOT NULL DEFAULT now()
);
CREATE INDEX ix_support_replies_ticket ON support_ticket_replies (ticket_id, created_at);

-- entity_type/entity_id is just for deep links, not a FK on purpose
CREATE TABLE notifications (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       bigint            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type          notification_type NOT NULL,
    title         varchar(200)      NOT NULL,
    message       text              NOT NULL,
    target_route  varchar(200),
    entity_type   varchar(40),
    entity_id     bigint,
    is_read       boolean           NOT NULL DEFAULT false,
    read_at       timestamptz,
    created_at    timestamptz       NOT NULL DEFAULT now(),
    CHECK (is_read = (read_at IS NOT NULL)),
    CHECK ((entity_type IS NULL) = (entity_id IS NULL))
);
CREATE INDEX ix_notifications_unread ON notifications (user_id, created_at DESC) WHERE NOT is_read;
CREATE INDEX ix_notifications_user   ON notifications (user_id, created_at DESC);

-- audit log, settings

-- actor name etc copied in so the log still makes sense if the user is deleted
CREATE TABLE audit_logs (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portal           audit_portal   NOT NULL,
    actor_user_id    bigint         REFERENCES users(id) ON DELETE SET NULL,
    actor_name       varchar(120)   NOT NULL,
    actor_email      citext,
    actor_role       varchar(100),
    action           varchar(200)   NOT NULL,
    action_type      varchar(40)    NOT NULL,                     -- 'Updated', 'LOGIN', 'SETTLEMENT' ...
    module           varchar(40)    NOT NULL,                     -- 'Pricing', 'Chargers', 'Settlements' ...
    target_resource  varchar(200),
    record_id        varchar(60),
    details          text,
    previous_value   jsonb,
    new_value        jsonb,
    ip_address       inet,
    device           varchar(200),
    severity         audit_severity NOT NULL DEFAULT 'info',
    outcome          audit_outcome  NOT NULL DEFAULT 'success',
    created_at       timestamptz    NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_logs_created ON audit_logs (created_at DESC);
CREATE INDEX ix_audit_logs_actor   ON audit_logs (actor_user_id, created_at DESC);
CREATE INDEX ix_audit_logs_module  ON audit_logs (portal, module, created_at DESC);

CREATE TABLE system_settings (
    key          varchar(80) PRIMARY KEY,
    value        jsonb       NOT NULL,
    description  text,
    updated_by   bigint      REFERENCES users(id) ON DELETE SET NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- triggers

-- updated_at
CREATE FUNCTION fn_set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

DO $$
DECLARE t text;
BEGIN
    FOR t IN
        SELECT c.table_name FROM information_schema.columns c
        JOIN information_schema.tables tb ON tb.table_name = c.table_name AND tb.table_schema = c.table_schema
        WHERE c.table_schema = 'public' AND c.column_name = 'updated_at' AND tb.table_type = 'BASE TABLE'
    LOOP
        EXECUTE format('CREATE TRIGGER trg_%s_updated_at BEFORE UPDATE ON %I
                        FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at()', t, t);
    END LOOP;
END $$;

-- every customer gets exactly one wallet
CREATE FUNCTION fn_create_customer_wallet() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO wallets (customer_id) VALUES (NEW.user_id);
    RETURN NEW;
END $$;
CREATE TRIGGER trg_customer_profiles_wallet AFTER INSERT ON customer_profiles
    FOR EACH ROW EXECUTE FUNCTION fn_create_customer_wallet();

-- posting: lock wallet row, work out balances, update wallet
CREATE FUNCTION fn_wallet_post_transaction() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_balance numeric(12,2);
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.status = 'success' THEN
            RAISE EXCEPTION 'wallet transaction % is already posted; create a reversal instead', OLD.transaction_ref
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF (NEW.amount, NEW.direction, NEW.wallet_id, NEW.customer_id, NEW.txn_type)
           IS DISTINCT FROM (OLD.amount, OLD.direction, OLD.wallet_id, OLD.customer_id, OLD.txn_type) THEN
            RAISE EXCEPTION 'amount, direction, type and wallet of a wallet transaction cannot change'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    IF NEW.status = 'success' THEN
        SELECT balance INTO v_balance FROM wallets WHERE id = NEW.wallet_id FOR UPDATE;
        NEW.balance_before := v_balance;
        NEW.balance_after  := v_balance + CASE WHEN NEW.direction = 'credit' THEN NEW.amount ELSE -NEW.amount END;
        IF NEW.balance_after < 0 THEN
            RAISE EXCEPTION 'insufficient wallet balance: balance %, debit %', v_balance, NEW.amount
                USING ERRCODE = 'check_violation';
        END IF;
        UPDATE wallets SET balance = NEW.balance_after WHERE id = NEW.wallet_id;
        NEW.posted_at := now();
    ELSE
        NEW.balance_before := NULL;
        NEW.balance_after  := NULL;
        NEW.posted_at      := NULL;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_wallet_txn_post BEFORE INSERT OR UPDATE ON wallet_transactions
    FOR EACH ROW EXECUTE FUNCTION fn_wallet_post_transaction();

-- wallet balance may only change through the ledger trigger above
CREATE FUNCTION fn_wallet_guard_balance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.balance IS DISTINCT FROM OLD.balance AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'wallet balance can only change by inserting a wallet_transactions row'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_wallets_guard_balance BEFORE UPDATE OF balance ON wallets
    FOR EACH ROW EXECUTE FUNCTION fn_wallet_guard_balance();

-- append-only tables
CREATE FUNCTION fn_block_modification() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are append-only (% blocked)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END $$;
CREATE TRIGGER trg_audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION fn_block_modification();
CREATE TRIGGER trg_wallet_txn_no_delete BEFORE DELETE ON wallet_transactions
    FOR EACH ROW EXECUTE FUNCTION fn_block_modification();

-- protected roles (Super Admin, Zone Manager) cannot be edited or deleted
CREATE FUNCTION fn_guard_protected_role() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.is_protected AND current_setting('eway.allow_protected_role_change', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'role % is protected', OLD.code USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER trg_roles_protected BEFORE UPDATE OR DELETE ON roles
    FOR EACH ROW EXECUTE FUNCTION fn_guard_protected_role();

-- a field engineer must hold a field-engineer role
CREATE FUNCTION fn_check_field_engineer_role() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM staff_profiles sp JOIN roles r ON r.id = sp.role_id
        WHERE sp.user_id = NEW.user_id AND r.code = 'ROLE_FIELD_ENGINEER'
    ) THEN
        RAISE EXCEPTION 'user % does not have ROLE_FIELD_ENGINEER', NEW.user_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_field_engineers_role BEFORE INSERT OR UPDATE OF user_id ON field_engineers
    FOR EACH ROW EXECUTE FUNCTION fn_check_field_engineer_role();

-- keep charging_points.status in step with live sessions
CREATE FUNCTION fn_sync_charger_status() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IN ('starting', 'charging', 'stopping') THEN
        UPDATE charging_points SET status = 'charging'
         WHERE id = NEW.charging_point_id AND status <> 'charging';
    ELSIF TG_OP = 'UPDATE' AND OLD.status IN ('starting', 'charging', 'stopping') THEN
        UPDATE charging_points
           SET status = CASE WHEN NEW.status = 'faulted' THEN 'maintenance'::charger_status ELSE 'available'::charger_status END
         WHERE id = NEW.charging_point_id AND status = 'charging';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_sessions_sync_charger AFTER INSERT OR UPDATE OF status ON charging_sessions
    FOR EACH ROW EXECUTE FUNCTION fn_sync_charger_status();

-- can't book / start on a charger that's down, or with a deleted vehicle
CREATE FUNCTION fn_check_bookable() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_point_status   charger_status;
    v_station_status station_status;
BEGIN
    SELECT cp.status, s.status INTO v_point_status, v_station_status
      FROM charging_points cp JOIN stations s ON s.id = cp.station_id
     WHERE cp.id = NEW.charging_point_id;

    IF v_point_status IN ('maintenance', 'offline') OR v_station_status <> 'active' THEN
        RAISE EXCEPTION 'charging point % is not usable (charger %, station %)',
              NEW.charging_point_id, v_point_status, v_station_status
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM vehicles WHERE id = NEW.vehicle_id AND deleted_at IS NOT NULL) THEN
        RAISE EXCEPTION 'vehicle % has been removed', NEW.vehicle_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_reservations_bookable BEFORE INSERT ON reservations
    FOR EACH ROW EXECUTE FUNCTION fn_check_bookable();
CREATE TRIGGER trg_sessions_bookable BEFORE INSERT ON charging_sessions
    FOR EACH ROW EXECUTE FUNCTION fn_check_bookable();

-- views (dashboard counts, so they're never stored twice)

-- Admin "Zones" cards / super-admin "Areas"
CREATE VIEW v_zone_overview AS
SELECT z.id, z.code, z.name, z.city, z.state, z.status,
       z.power_capacity_kw, z.current_load_kw,
       COUNT(cp.id)                                           AS total_chargers,
       COUNT(cp.id) FILTER (WHERE cp.status = 'available')    AS available_chargers,
       COUNT(cp.id) FILTER (WHERE cp.status = 'charging')     AS charging_count,
       COUNT(cp.id) FILTER (WHERE cp.status = 'reserved')     AS reserved_count,
       COUNT(cp.id) FILTER (WHERE cp.status = 'maintenance')  AS maintenance_count,
       COUNT(cp.id) FILTER (WHERE cp.status = 'offline')      AS offline_count,
       COALESCE((SELECT SUM(cs.total_amount)
                   FROM charging_sessions cs
                   JOIN charging_points p ON p.id = cs.charging_point_id
                   JOIN stations s2      ON s2.id = p.station_id
                  WHERE s2.zone_id = z.id AND cs.started_at >= date_trunc('day', now())), 0) AS today_revenue,
       (SELECT COUNT(*) FROM customer_profiles c WHERE c.zone_id = z.id)               AS total_customers,
       (SELECT string_agg(u.full_name, ', ' ORDER BY u.full_name)
          FROM admin_zone_assignments a JOIN users u ON u.id = a.admin_id
         WHERE a.zone_id = z.id AND a.is_active)                                     AS assigned_admins
FROM zones z
LEFT JOIN stations s        ON s.zone_id = z.id
LEFT JOIN charging_points cp ON cp.station_id = s.id
GROUP BY z.id;

-- charging points + live session
CREATE VIEW v_charging_point_live AS
SELECT cp.id, cp.point_code, cp.display_name, cp.charger_number, cp.current_type, cp.connector_type,
       cp.power_kw, cp.status, cp.ocpp_id, cp.last_ping_at, cp.last_maintenance_at,
       s.id AS station_id, s.name AS station_name, s.city,
       z.id AS zone_id, z.code AS zone_code, z.name AS zone_name,
       cs.id AS active_session_id, cs.session_number, v.license_plate AS vehicle_plate, v.model AS vehicle_model,
       u.full_name AS driver_name, cs.energy_delivered_kwh, cs.current_soc AS battery_level,
       cs.total_amount AS current_cost,
       (EXTRACT(EPOCH FROM (now() - cs.started_at)) / 60)::int AS session_minutes
FROM charging_points cp
JOIN stations s ON s.id = cp.station_id
JOIN zones z    ON z.id = s.zone_id
LEFT JOIN charging_sessions cs ON cs.charging_point_id = cp.id AND cs.status IN ('starting', 'charging', 'stopping')
LEFT JOIN vehicles v ON v.id = cs.vehicle_id
LEFT JOIN users u    ON u.id = cs.customer_id;

-- Admin "Users" / super-admin "Customers"
CREATE VIEW v_customer_overview AS
SELECT u.id, u.public_code, u.full_name, u.country_code, u.phone_number, u.email, u.status, u.is_verified,
       u.created_at AS registered_at,
       cp.city, cp.state, z.code AS zone_code, z.name AS zone_name,
       cp.assigned_admin_id, au.full_name AS assigned_admin_name,
       w.balance AS wallet_balance,
       (SELECT COUNT(*) FROM vehicles v WHERE v.owner_id = u.id AND v.deleted_at IS NULL)        AS vehicles_count,
       (SELECT COUNT(*) FROM charging_sessions s WHERE s.customer_id = u.id)                     AS total_sessions,
       (SELECT COALESCE(SUM(s.energy_delivered_kwh), 0) FROM charging_sessions s WHERE s.customer_id = u.id) AS energy_consumed_kwh,
       (SELECT COALESCE(SUM(p.total_amount), 0) FROM payments p WHERE p.customer_id = u.id AND p.status = 'success') AS total_amount_paid,
       (SELECT MAX(p.completed_at) FROM payments p WHERE p.customer_id = u.id AND p.status = 'success')            AS last_payment_at
FROM users u
JOIN customer_profiles cp ON cp.user_id = u.id
LEFT JOIN zones z  ON z.id = cp.zone_id
LEFT JOIN users au ON au.id = cp.assigned_admin_id
LEFT JOIN wallets w ON w.customer_id = u.id;

-- Admin "Vehicles"
CREATE VIEW v_vehicle_overview AS
SELECT v.id, v.nickname, v.manufacturer, v.model, v.manufacture_year, v.vehicle_type, v.license_plate,
       v.battery_capacity_kwh, v.connector_type, v.battery_percentage, v.is_default, v.is_verified, v.created_at,
       u.id AS owner_id, u.full_name AS owner_name, u.phone_number AS owner_phone,
       COUNT(s.id)                                  AS sessions_count,
       COALESCE(SUM(s.energy_delivered_kwh), 0)     AS total_energy_kwh
FROM vehicles v
JOIN users u ON u.id = v.owner_id
LEFT JOIN charging_sessions s ON s.vehicle_id = v.id
WHERE v.deleted_at IS NULL
GROUP BY v.id, u.id;

-- Super-admin "Admins" financial summary
CREATE VIEW v_admin_financials AS
SELECT u.id AS admin_id, u.public_code, u.full_name, u.email, u.status,
       sp.commission_rate_percent, sp.bank_name, sp.bank_account_masked, sp.bank_ifsc,
       (SELECT string_agg(z.code, ', ' ORDER BY z.code) FROM admin_zone_assignments a JOIN zones z ON z.id = a.zone_id
         WHERE a.admin_id = u.id AND a.is_active)                                                 AS assigned_zones,
       (SELECT COUNT(*) FROM customer_profiles c WHERE c.assigned_admin_id = u.id)                AS total_customers,
       COALESCE((SELECT SUM(ac.amount) FROM admin_collections ac
                  WHERE ac.admin_id = u.id AND ac.transaction_status = 'success'), 0)             AS total_collections,
       COALESCE((SELECT SUM(ac.admin_commission) FROM admin_collections ac
                  WHERE ac.admin_id = u.id AND ac.transaction_status = 'success'), 0)             AS total_commission,
       COALESCE((SELECT SUM(ac.amount) FROM admin_collections ac
                  WHERE ac.admin_id = u.id AND ac.transaction_status = 'pending'), 0)             AS pending_customer_payments,
       COALESCE((SELECT SUM(ac.amount) FROM admin_collections ac
                  WHERE ac.admin_id = u.id AND ac.transaction_status IN ('refunded', 'reversed')), 0) AS refunds_and_reversals,
       COALESCE((SELECT SUM(st.verified_amount_received) FROM admin_settlements st WHERE st.admin_id = u.id), 0) AS business_receipts,
       COALESCE((SELECT SUM(st.outstanding_balance) FROM admin_settlements st WHERE st.admin_id = u.id), 0)      AS outstanding_settlement
FROM users u
JOIN staff_profiles sp ON sp.user_id = u.id
WHERE EXISTS (SELECT 1 FROM admin_zone_assignments a WHERE a.admin_id = u.id);

-- maintenance queue + SLA
CREATE VIEW v_maintenance_queue AS
SELECT t.id, t.ticket_code, t.fault_code, t.issue_description, t.severity, t.priority, t.status,
       t.reported_at, t.sla_minutes,
       t.reported_at + make_interval(mins => t.sla_minutes)                                AS sla_due_at,
       CASE WHEN t.status = 'resolved' THEN NULL
            ELSE (EXTRACT(EPOCH FROM (t.reported_at + make_interval(mins => t.sla_minutes) - now())) / 60)::int
       END                                                                                 AS sla_remaining_minutes,
       COALESCE(t.resolved_at, now()) > t.reported_at + make_interval(mins => t.sla_minutes) AS sla_breached,
       cp.point_code, s.name AS station_name, z.code AS zone_code, z.name AS zone_name, s.city,
       fe.engineer_code, eu.full_name AS technician_name, eu.phone_number AS technician_phone
FROM maintenance_tickets t
JOIN charging_points cp ON cp.id = t.charging_point_id
JOIN stations s         ON s.id = cp.station_id
JOIN zones z            ON z.id = s.zone_id
LEFT JOIN field_engineers fe ON fe.user_id = t.assigned_engineer_id
LEFT JOIN users eu           ON eu.id = fe.user_id;

CREATE VIEW v_field_engineer_workload AS
SELECT fe.user_id, fe.engineer_code, u.full_name, u.phone_number, u.email, z.code AS zone_code, z.name AS zone_name,
       fe.availability, fe.specialization,
       COUNT(t.id) FILTER (WHERE t.status <> 'resolved') AS active_jobs
FROM field_engineers fe
JOIN users u ON u.id = fe.user_id
JOIN zones z ON z.id = fe.zone_id
LEFT JOIN maintenance_tickets t ON t.assigned_engineer_id = fe.user_id
GROUP BY fe.user_id, u.id, z.id;

CREATE VIEW v_role_overview AS
SELECT r.id, r.code, r.name, r.role_type, r.access_level, r.is_protected, r.is_active, r.updated_at,
       (SELECT COUNT(*) FROM staff_profiles sp WHERE sp.role_id = r.id)                                   AS user_count,
       (SELECT array_agg(p.code ORDER BY p.code) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id
         WHERE rp.role_id = r.id)                                                                         AS permission_codes
FROM roles r;

COMMIT;
