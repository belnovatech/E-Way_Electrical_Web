-- dev seed data, same as the demo data in the frontend services.
-- rows are looked up by code (zone code, point code, public_code), not by id.
-- DEV ONLY passwords: super admin SuperAdmin@2026!, everyone else Admin@2026!

BEGIN;

-- zones
INSERT INTO zones (code, name, address, city, state, latitude, longitude, power_capacity_kw, current_load_kw, contact_number) VALUES
('AP-Z01', 'Vijayawada Central',       'MG Road, Near Kanaka Durga Temple',      'Vijayawada',    'Andhra Pradesh', 16.506200, 80.648000, 1200, 420, '+91 86624 55110'),
('AP-Z02', 'Visakhapatnam Port',       'Beach Road, Near Rushikonda',            'Visakhapatnam', 'Andhra Pradesh', 17.686800, 83.218500, 1500, 510, '+91 89125 77220'),
('AP-Z03', 'Tirupati East Hub',        'Tiruchanur Road',                        'Tirupati',      'Andhra Pradesh', 13.628800, 79.419200, 1000, 380, '+91 87722 33440'),
('AP-Z04', 'Guntur Smart City',        'Brodipet 4th Lane, Commercial Hub',      'Guntur',        'Andhra Pradesh', 16.306700, 80.436500,  800, 310, '+91 86322 11990'),
('AP-Z05', 'Nellore NH-16 Hub',        'Mini Bypass Road, NH-16 Junction',       'Nellore',       'Andhra Pradesh', 14.442600, 79.986500, 1000, 240, '+91 86123 44550'),
('AP-Z06', 'Kurnool IT Park',          'Near Highway Junction, IT Corridor',     'Kurnool',       'Andhra Pradesh', 15.828100, 78.037300,  800, 420, '+91 85182 66770'),
('AP-Z07', 'Kakinada Smart Port',      'Main Road Port Gate',                    'Kakinada',      'Andhra Pradesh', 16.989100, 82.247500,  900, 180, '+91 88423 88110'),
('AP-Z08', 'Rajahmundry Godavari Hub', 'Kotipalli Bus Stand, Godavari Bund Rd',  'Rajahmundry',   'Andhra Pradesh', 17.000500, 81.804000,  900, 390, '+91 88324 55990');

-- stations (one flagship per zone)
INSERT INTO stations (zone_id, code, name, address, city, latitude, longitude, amenities, supported_vehicle_types)
SELECT z.id, 'ST-' || z.code, s.name, s.address, z.city, z.latitude, z.longitude, s.amenities, s.vtypes
FROM (VALUES
  ('AP-Z01', 'Vijayawada Central Hub',      'MG Road, Vijayawada',               ARRAY['24x7 Cafe','Restrooms','Free Wi-Fi'],  ARRAY['bike','car','bus']::vehicle_type[]),
  ('AP-Z02', 'Visakhapatnam Port Terminal', 'Port Area, Visakhapatnam',          ARRAY['Restrooms','Security CCTV'],           ARRAY['car','bus']::vehicle_type[]),
  ('AP-Z03', 'Tirupati East Hub',           'Tiruchanur Road, Tirupati',         ARRAY['Food Court','Restrooms'],              ARRAY['bike','car']::vehicle_type[]),
  ('AP-Z04', 'Guntur Smart City Station',   'Brodipet, Guntur',                  ARRAY['Coffee Shop','Free Wi-Fi'],            ARRAY['bike','car']::vehicle_type[]),
  ('AP-Z05', 'Nellore Highway Rest Stop',   'NH-16 Junction, Nellore',           ARRAY['Restaurant','Restrooms','Parking'],    ARRAY['car','bus']::vehicle_type[]),
  ('AP-Z06', 'Kurnool IT Park Station',     'IT Corridor, Kurnool',              ARRAY['Tech Lounge','Free Wi-Fi'],            ARRAY['bike','car']::vehicle_type[]),
  ('AP-Z07', 'Kakinada Smart Port Station', 'Port Gate Road, Kakinada',          ARRAY['Restrooms'],                           ARRAY['car','bus']::vehicle_type[]),
  ('AP-Z08', 'Rajahmundry Godavari Hub',    'Kotipalli Bus Stand, Rajahmundry',  ARRAY['Waiting Lounge','Restrooms'],          ARRAY['bike','car','bus']::vehicle_type[])
) AS s(zone_code, name, address, amenities, vtypes)
JOIN zones z ON z.code = s.zone_code;

-- charging points (4 guns per station)
INSERT INTO charging_points (station_id, point_code, charger_number, display_name, current_type, connector_type, power_kw, ocpp_id, commissioned_on, last_ping_at)
SELECT st.id,
       z.code || '-CP' || lpad(g.n::text, 2, '0'),
       g.n,
       'Gun ' || g.n || ' (' || g.label || ')',
       g.ctype::current_type, g.conn::connector_type, g.kw,
       'OCPP-' || z.code || '-' || lpad(g.n::text, 2, '0'),
       DATE '2024-01-15',
       now() - interval '30 seconds'
FROM stations st
JOIN zones z ON z.id = st.zone_id
CROSS JOIN (VALUES
  (1, 'Hyper DC', 'DC', 'CCS2',   120),
  (2, 'Hyper DC', 'DC', 'CCS2',   120),
  (3, 'Fast DC',  'DC', 'CCS2',    60),
  (4, 'Type 2',   'AC', 'Type 2',  22)
) AS g(n, label, ctype, conn, kw);

-- permissions & roles
INSERT INTO permissions (code, name, description, category, is_dangerous) VALUES
('VIEW_ZONES',           'View Electrical Zones & Hubs',            'Inspect state power zones, substation health, and charging point availability.',          'grid_operations', false),
('MANAGE_GRID',          'Manage Grid Balancing & Load Shedding',   'Control feeder capacity allocations, peak power throttling, and emergency load shedding.', 'grid_operations', true),
('RESTART_CHARGER',      'Remote Charger Hard Reset',               'Execute remote firmware restart and OCPP hard reset on live charging hardware.',           'grid_operations', true),
('VIEW_SESSIONS',        'View Live Charging Sessions',             'Inspect active vehicle charging telemetry, energy throughput, and session durations.',     'grid_operations', false),
('OVERRIDE_LOAD',        'Override Dynamic Load Balancing',         'Bypass automatic grid limiter policies during urgent commercial fleet operations.',        'grid_operations', true),
('VIEW_MAINTENANCE',     'View Maintenance & Diagnostics',          'Access field operations console, open hardware alerts, and error code telemetry.',         'maintenance',     false),
('UPDATE_TICKET',        'Update & Triage Work Orders',             'Change maintenance incident states, modify severity, and record technician field notes.',  'maintenance',     false),
('HARDWARE_TEST',        'Execute Diagnostics & Self-Test',         'Run remote solenoid latch tests, contactor cycle tests, and ground fault simulations.',    'maintenance',     true),
('DISPATCH_MAINTENANCE', 'Dispatch Field Technicians',              'Assign certified zone engineers and schedule on-site emergency recovery visits.',         'maintenance',     false),
('CLEAR_LOCKOUT',        'Clear Safety Lockouts',                   'Override OCPP safety fault lockouts and return isolated hardware back to service.',        'maintenance',     true),
('VIEW_REVENUE',         'View Revenue & Wallet Ledgers',           'Inspect daily gross collections, wallet balances, transaction logs, and payout streams.',  'finance',         false),
('TARIFF_EDIT',          'Modify Pricing & Tariff Rates',           'Create, update, and publish per-kWh tariffs, peak multipliers, and parking penalties.',    'finance',         true),
('PROCESS_REFUNDS',      'Authorize Session Refunds',               'Approve disputed charging transactions and issue direct wallet and gateway refunds.',      'finance',         true),
('WALLET_ADJUST',        'Adjust Driver Wallet Balances',           'Perform manual debit/credit ledger adjustments on customer and corporate fleet accounts.', 'finance',         true),
('EXPORT_FINANCIALS',    'Export Tax & GST Settlement Books',       'Download audited GST statements, settlement batches, and revenue reconciliation records.', 'finance',         false),
('USER_MANAGEMENT',      'Manage Driver & Corporate Accounts',      'Create, update, suspend, or terminate driver profiles, RFID tags, and vehicle associations.', 'users',        true),
('VIEW_PII',             'Access Sensitive Identity Details',       'Inspect unmasked personal phone numbers, billing addresses, and national identity tags.',  'users',           false),
('RESET_2FA',            'Reset Multi-Factor Credentials',          'Revoke and re-provision two-factor authentication tokens for staff and users.',            'users',           true),
('ROLE_MANAGEMENT',      'Configure RBAC & Role Policies',          'Create security roles, define platform access matrices, and grant granular permissions.',  'security',        true),
('AUDIT_LOGS',           'Inspect System Audit Logs',               'View immutable chronological activity logs, admin operations, and security event trails.', 'security',        false),
('MANAGE_SECURITY',      'Configure Security & IP Whitelists',      'Manage API gateway keys, IP cidr restrictions, and enterprise SSO integrations.',          'security',        true),
('ALL_PERMISSIONS',      'Platform Superuser Bypass',               'Unrestricted authorization bypass across all present and future platform endpoints.',      'security',        true),
('VIEW_REPORTS',         'View Operational Analytics & Reports',    'Inspect energy delivery charts, charger utilization heatmaps, and station performance metrics.', 'reporting', false),
('EXPORT_REPORTS',       'Export CSV & Excel Data Reports',         'Export telemetry datasets, session summaries, and SLA operational reports.',               'reporting',       false),
('COMPLIANCE_EXPORT',    'Export Regulatory Compliance Audits',     'Download verified state energy compliance and grid carbon offset verification reports.',   'reporting',       false);

INSERT INTO roles (code, name, description, role_type, access_level, is_protected) VALUES
('ROLE_SUPER_ADMIN',         'Super Admin',                 'Complete unrestricted access across grid telemetry, financials, tariffs, security and staff clearance.', 'system', 'critical',    true),
('ROLE_ZONE_MANAGER',        'Zone Operations Manager',     'Supervises regional charging points, live sessions, connector status and dynamic load.',                 'system', 'elevated',    true),
('ROLE_FIELD_ENGINEER',      'Field Service Engineer',      'Handles physical charger maintenance, OCPP hardware alerts and diagnostics.',                            'system', 'operational', false),
('ROLE_FINANCIAL_AUDITOR',   'Financial Auditor',           'Read-only financial governance: wallets, settlements, tariff structures.',                              'system', 'audit',       false),
('ROLE_SECURITY_COMPLIANCE', 'Security Compliance Officer', 'SOC2 & ISO 27001 reviews, privilege escalation monitoring, role audits.',                                'custom', 'elevated',    false);

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM (VALUES
  ('ROLE_SUPER_ADMIN',         NULL),   -- NULL = every permission
  ('ROLE_ZONE_MANAGER',        'VIEW_ZONES'), ('ROLE_ZONE_MANAGER', 'MANAGE_GRID'), ('ROLE_ZONE_MANAGER', 'RESTART_CHARGER'),
  ('ROLE_ZONE_MANAGER',        'VIEW_SESSIONS'), ('ROLE_ZONE_MANAGER', 'OVERRIDE_LOAD'), ('ROLE_ZONE_MANAGER', 'VIEW_MAINTENANCE'),
  ('ROLE_ZONE_MANAGER',        'UPDATE_TICKET'), ('ROLE_ZONE_MANAGER', 'DISPATCH_MAINTENANCE'), ('ROLE_ZONE_MANAGER', 'VIEW_REPORTS'),
  ('ROLE_ZONE_MANAGER',        'EXPORT_REPORTS'),
  ('ROLE_FIELD_ENGINEER',      'VIEW_MAINTENANCE'), ('ROLE_FIELD_ENGINEER', 'UPDATE_TICKET'), ('ROLE_FIELD_ENGINEER', 'HARDWARE_TEST'),
  ('ROLE_FIELD_ENGINEER',      'CLEAR_LOCKOUT'), ('ROLE_FIELD_ENGINEER', 'VIEW_SESSIONS'),
  ('ROLE_FINANCIAL_AUDITOR',   'VIEW_REVENUE'), ('ROLE_FINANCIAL_AUDITOR', 'EXPORT_FINANCIALS'), ('ROLE_FINANCIAL_AUDITOR', 'AUDIT_LOGS'),
  ('ROLE_FINANCIAL_AUDITOR',   'VIEW_REPORTS'), ('ROLE_FINANCIAL_AUDITOR', 'EXPORT_REPORTS'), ('ROLE_FINANCIAL_AUDITOR', 'COMPLIANCE_EXPORT'),
  ('ROLE_SECURITY_COMPLIANCE', 'AUDIT_LOGS'), ('ROLE_SECURITY_COMPLIANCE', 'ROLE_MANAGEMENT'), ('ROLE_SECURITY_COMPLIANCE', 'VIEW_REPORTS'),
  ('ROLE_SECURITY_COMPLIANCE', 'EXPORT_REPORTS'), ('ROLE_SECURITY_COMPLIANCE', 'COMPLIANCE_EXPORT'), ('ROLE_SECURITY_COMPLIANCE', 'VIEW_PII')
) AS m(role_code, perm_code)
JOIN roles r ON r.code = m.role_code
JOIN permissions p ON p.code = m.perm_code OR m.perm_code IS NULL;

-- staff users
INSERT INTO users (user_type, public_code, full_name, phone_number, email, password_hash, two_factor_enabled, is_verified, password_changed_at)
SELECT s.utype::user_type, s.code, s.name, s.phone, s.email,
       crypt(CASE WHEN s.code = 'SA-001' THEN 'SuperAdmin@2026!' ELSE 'Admin@2026!' END, gen_salt('bf', 10)),
       s.tfa, true, now()
FROM (VALUES
  ('super_admin', 'SA-001',   'E-Way Super Admin',  NULL,         'superadmin@ewaytoyourevs.in',     true),
  ('super_admin', 'STF-001',  'Manikantha N',       NULL,         'admin@powergrid.in',              true),
  ('super_admin', 'STF-002',  'Bhavani Shankar',    NULL,         'bhavani.shankar@powergrid.in',    true),
  ('admin',       'ADM-AP01', 'Ravi Kumar Varma',   '9848011221', 'ravi.kumar@powergrid.in',         true),
  ('admin',       'ADM-AP02', 'Suresh Babu',        '9848022332', 'suresh.babu@powergrid.in',        true),
  ('admin',       'ADM-AP03', 'Anil Chowdary',      '9848033443', 'anil.chowdary@powergrid.in',      true),
  ('admin',       'ADM-AP04', 'Ramesh Naidu',       '9848044554', 'ramesh.naidu@powergrid.in',       true),
  ('admin',       'ADM-AP05', 'Venkatesh Kumar',    '9848055665', 'venkatesh.kumar@powergrid.in',    true),
  ('admin',       'ADM-AP06', 'Srinivas Reddy',     '9848066776', 'srinivas.reddy@powergrid.in',     true),
  ('admin',       'ENG-101',  'K. Venkatesh',       '9848012345', 'k.venkatesh@powergrid.in',        true),
  ('admin',       'ENG-102',  'S. Ramana',          '9848123456', 's.ramana@powergrid.in',           false),
  ('admin',       'ENG-103',  'P. Rajesh Kumar',    '9848234567', 'p.rajesh@powergrid.in',           true),
  ('admin',       'ENG-104',  'A. Suresh Reddy',    '9848345678', 'a.suresh@powergrid.in',           true),
  ('admin',       'ENG-105',  'M. Kalyan Varma',    '9848456789', 'm.kalyan@powergrid.in',           true),
  ('admin',       'ENG-106',  'T. Harish Babu',     '9848567890', 't.harish@powergrid.in',           false),
  ('admin',       'STF-012',  'Lakshmi Narayana',   NULL,         'lakshmi.n@powergrid.in',          true),
  ('admin',       'STF-015',  'V. Sandeep Raju',    NULL,         'sandeep.raju@powergrid.in',       true)
) AS s(utype, code, name, phone, email, tfa);

INSERT INTO staff_profiles (user_id, user_type, role_id, department, city, commission_rate_percent, bank_name, bank_account_masked, bank_ifsc)
SELECT u.id, u.user_type, r.id, s.dept, s.city, s.commission, s.bank, s.acct, s.ifsc
FROM (VALUES
  ('SA-001',   'ROLE_SUPER_ADMIN',         'Executive Operations',       'Vijayawada',    NULL, NULL, NULL, NULL),
  ('STF-001',  'ROLE_SUPER_ADMIN',         'Executive Operations',       'Vijayawada',    NULL, NULL, NULL, NULL),
  ('STF-002',  'ROLE_SUPER_ADMIN',         'Infrastructure Security',    'Visakhapatnam', NULL, NULL, NULL, NULL),
  ('ADM-AP01', 'ROLE_ZONE_MANAGER',        'Grid Operations (AP-Z01)',   'Vijayawada',    5.00, 'State Bank of India', 'XXXX XXXX 4410', 'SBIN0001824'),
  ('ADM-AP02', 'ROLE_ZONE_MANAGER',        'Grid Operations (AP-Z02)',   'Visakhapatnam', 5.00, 'HDFC Bank',           'XXXX XXXX 9812', 'HDFC0000421'),
  ('ADM-AP03', 'ROLE_ZONE_MANAGER',        'Grid Operations (AP-Z03)',   'Tirupati',      4.50, 'ICICI Bank',          'XXXX XXXX 3301', 'ICIC0000109'),
  ('ADM-AP04', 'ROLE_ZONE_MANAGER',        'Grid Operations (AP-Z04)',   'Guntur',        5.00, 'Axis Bank',           'XXXX XXXX 7714', 'UTIB0000882'),
  ('ADM-AP05', 'ROLE_ZONE_MANAGER',        'Grid Operations (AP-Z05)',   'Nellore',       5.00, 'Kotak Mahindra Bank', 'XXXX XXXX 5590', 'KKBK0001201'),
  ('ADM-AP06', 'ROLE_ZONE_MANAGER',        'Grid Operations (AP-Z06)',   'Kurnool',       5.00, 'Punjab National Bank','XXXX XXXX 1289', 'PUNB0004500'),
  ('ENG-101',  'ROLE_FIELD_ENGINEER',      'Field Maintenance (AP-Z05)', 'Nellore',       NULL, NULL, NULL, NULL),
  ('ENG-102',  'ROLE_FIELD_ENGINEER',      'Field Maintenance (AP-Z03)', 'Tirupati',      NULL, NULL, NULL, NULL),
  ('ENG-103',  'ROLE_FIELD_ENGINEER',      'Field Maintenance (AP-Z01)', 'Vijayawada',    NULL, NULL, NULL, NULL),
  ('ENG-104',  'ROLE_FIELD_ENGINEER',      'Field Maintenance (AP-Z02)', 'Visakhapatnam', NULL, NULL, NULL, NULL),
  ('ENG-105',  'ROLE_FIELD_ENGINEER',      'Field Maintenance (AP-Z08)', 'Rajahmundry',   NULL, NULL, NULL, NULL),
  ('ENG-106',  'ROLE_FIELD_ENGINEER',      'Field Maintenance (AP-Z06)', 'Kurnool',       NULL, NULL, NULL, NULL),
  ('STF-012',  'ROLE_FINANCIAL_AUDITOR',   'Finance & Treasury',         'Vijayawada',    NULL, NULL, NULL, NULL),
  ('STF-015',  'ROLE_SECURITY_COMPLIANCE', 'InfoSec & Compliance',       'Vijayawada',    NULL, NULL, NULL, NULL)
) AS s(code, role_code, dept, city, commission, bank, acct, ifsc)
JOIN users u ON u.public_code = s.code
JOIN roles r ON r.code = s.role_code;

INSERT INTO admin_zone_assignments (admin_id, zone_id, assigned_by)
SELECT u.id, z.id, (SELECT id FROM users WHERE public_code = 'SA-001')
FROM (VALUES ('ADM-AP01','AP-Z01'), ('ADM-AP01','AP-Z08'), ('ADM-AP02','AP-Z02'), ('ADM-AP02','AP-Z07'),
             ('ADM-AP03','AP-Z03'), ('ADM-AP04','AP-Z04'), ('ADM-AP05','AP-Z05'), ('ADM-AP06','AP-Z06')) AS a(admin_code, zone_code)
JOIN users u ON u.public_code = a.admin_code
JOIN zones z ON z.code = a.zone_code;

INSERT INTO field_engineers (user_id, engineer_code, zone_id, specialization, availability)
SELECT u.id, e.code, z.id, e.spec, e.avail::engineer_status
FROM (VALUES
  ('ENG-101', 'AP-Z05', 'DC Fast Chargers & Solenoid Hardware',   'on_site'),
  ('ENG-102', 'AP-Z03', 'Thermal Management & Liquid Cooling',    'available'),
  ('ENG-103', 'AP-Z01', 'Grid Interconnect & High Voltage AC/DC', 'available'),
  ('ENG-104', 'AP-Z02', 'OCPP 2.0.1 Protocol & Gateway Comm',     'busy'),
  ('ENG-105', 'AP-Z08', 'Power Electronics & Inverter Modules',   'available'),
  ('ENG-106', 'AP-Z06', 'Preventive Maintenance & Safety Checks', 'available')
) AS e(code, zone_code, spec, avail)
JOIN users u ON u.public_code = e.code
JOIN zones z ON z.code = e.zone_code;

-- customers (wallets are created by trigger)
INSERT INTO users (user_type, public_code, full_name, phone_number, email, status, is_verified, created_at)
SELECT 'customer', c.code, c.name, c.phone, c.email, c.status::account_status, true, c.reg::timestamptz
FROM (VALUES
  ('CUST-1001', 'Bala Krishna',            '8074407557', 'bala.krishna@gmail.com',     'active',   '2024-01-15'),
  ('CUST-1002', 'P. Lakshmi',              '9876543211', 'lakshmi.p@yahoo.co.in',      'active',   '2024-02-10'),
  ('CUST-1003', 'Ravi Teja Sharma',        '9876543212', 'raviteja.s@outlook.com',     'active',   '2024-03-05'),
  ('CUST-1004', 'Andhra State Fleet Co.',  '9988776655', 'fleet.ops@apsrtc.ap.gov.in', 'active',   '2023-11-01'),
  ('CUST-1005', 'Srinivas Rao G',          '9876543214', 'srinivas.rao@gmail.com',     'active',   '2024-04-05'),
  ('CUST-1006', 'Kavitha Reddy',           '9876543215', 'kavitha.reddy@techm.com',    'inactive', '2024-01-30'),
  ('CUST-1007', 'Dr. Mohan Varma',         '9849088210', 'dr.mohan@apolloap.in',       'active',   '2024-05-18'),
  ('CUST-1008', 'Chaitanya Krishna',       '9440155321', 'chaitanya.k@godavari.org',   'active',   '2024-06-01')
) AS c(code, name, phone, email, status, reg);

INSERT INTO customer_profiles (user_id, city, state, zone_id, assigned_admin_id, is_profile_complete, company_name, gstin)
SELECT u.id, z.city, z.state, z.id, a.id, true, c.company, c.gstin
FROM (VALUES
  ('CUST-1001', 'AP-Z01', 'ADM-AP01', 'Krishna EV Mobility', '37AAACP1234F1Z5'),
  ('CUST-1002', 'AP-Z02', 'ADM-AP02', NULL, NULL),
  ('CUST-1003', 'AP-Z03', 'ADM-AP03', NULL, NULL),
  ('CUST-1004', 'AP-Z01', 'ADM-AP01', 'Andhra State Fleet Co.', '37AABCA5678K1Z2'),
  ('CUST-1005', 'AP-Z04', 'ADM-AP04', NULL, NULL),
  ('CUST-1006', 'AP-Z06', 'ADM-AP06', NULL, NULL),
  ('CUST-1007', 'AP-Z02', 'ADM-AP02', NULL, NULL),
  ('CUST-1008', 'AP-Z08', 'ADM-AP01', NULL, NULL)
) AS c(code, zone_code, admin_code, company, gstin)
JOIN users u ON u.public_code = c.code
JOIN zones z ON z.code = c.zone_code
JOIN users a ON a.public_code = c.admin_code;

-- vehicles
INSERT INTO vehicles (owner_id, nickname, manufacturer, model, vehicle_type, license_plate, battery_capacity_kwh, connector_type, battery_percentage, is_default, is_verified)
SELECT u.id, v.nick, v.make, v.model, v.vtype::vehicle_type, v.plate, v.kwh, v.conn::connector_type, v.soc, v.def, true
FROM (VALUES
  ('CUST-1001', 'My Nexon',    'Tata Motors',  'Nexon EV Max',          'car',  'AP-39-AB-1234',  40.5, 'CCS2',   68, true),
  ('CUST-1001', 'My Ola',      'Ola Electric', 'S1 Pro',                'bike', 'AP-39-CD-5678',   4.0, 'Type 2', 92, false),
  ('CUST-1002', NULL,          'MG Motor',     'ZS EV Long Range',      'car',  'AP-31-EV-4422',  50.3, 'CCS2',   55, true),
  ('CUST-1003', NULL,          'Ather',        '450X Gen3',             'bike', 'AP-16-BK-1204',   3.7, 'Type 2', 40, true),
  ('CUST-1004', 'Fleet Bus 1', 'Olectra',      'K9 Electric Coach',     'bus',  'AP-16-Z-9901',  324.0, 'CCS2',   35, true),
  ('CUST-1005', NULL,          'Mahindra',     'XUV400',                'car',  'AP-07-CD-3091',  39.4, 'CCS2',   61, true),
  ('CUST-1006', NULL,          'Hyundai',      'Kona Electric',         'car',  'AP-21-EF-8812',  39.2, 'CCS2',   80, true),
  ('CUST-1007', NULL,          'Kia',          'EV6 GT-Line',           'car',  'AP-31-ZZ-0007',  77.4, 'CCS2',   22, true),
  ('CUST-1008', NULL,          'Ola Electric', 'S1 Pro Gen2',           'bike', 'AP-05-MN-4491',   4.0, 'Type 2', 47, true)
) AS v(code, nick, make, model, vtype, plate, kwh, conn, soc, def)
JOIN users u ON u.public_code = v.code;

-- tariffs
INSERT INTO tariff_plans (code, name, zone_id, vehicle_type, rate_per_kwh, service_fee, gst_percent, min_charge, idle_fee_per_minute, reservation_slot_fee, effective_from, effective_to, priority, created_by)
SELECT t.code, t.name, z.id, t.vtype::vehicle_type, t.rate, t.fee, 18, t.minc, t.idle, t.slot, DATE '2026-01-01', DATE '2026-12-31', t.prio,
       (SELECT id FROM users WHERE public_code = 'SA-001')
FROM (VALUES
  ('T-001', 'Global Bike Base Rate',      NULL,     'bike',  8,  5,  10, 1, 10, 3),
  ('T-002', 'Global Car Base Rate',       NULL,     'car',  12, 10,  20, 2, 25, 3),
  ('T-003', 'Global Bus Base Rate',       NULL,     'bus',  18, 50, 100, 5, 75, 3),
  ('T-004', 'Tirupati Zone Car Override', 'AP-Z03', 'car',  14, 10,  20, 2, 25, 2)
) AS t(code, name, zone_code, vtype, rate, fee, minc, idle, slot, prio)
LEFT JOIN zones z ON z.code = t.zone_code;

-- payment methods
INSERT INTO payment_methods (customer_id, method_type, title, subtitle, upi_vpa, is_default)
SELECT id, 'upi', 'Google Pay UPI', 'bala.krishna@okhdfcbank', 'bala.krishna@okhdfcbank', true FROM users WHERE public_code = 'CUST-1001';
INSERT INTO payment_methods (customer_id, method_type, title, subtitle, card_brand, card_last4, card_expiry_month, card_expiry_year, gateway_token)
SELECT id, 'card', 'HDFC Visa', 'Expires 08/28', 'visa', '4242', 8, 2028, 'tok_sandbox_visa_4242' FROM users WHERE public_code = 'CUST-1001';

-- Bala: top-up -> wallet credit
INSERT INTO payments (customer_id, order_type, provider, method_type, payment_method_id, method_summary, base_amount,
                      idempotency_key, gateway_order_id, gateway_payment_id, receipt_number, status, created_at, completed_at)
SELECT u.id, 'wallet_topup', 'sandbox', 'upi', pm.id, 'bala.krishna@okhdfcbank', 1200,
       'idem-cust1001-20261028-1', 'order_SBX_1001A', 'pay_SBX_1001A', 'RCPT-20261028-0001', 'success',
       TIMESTAMPTZ '2026-10-28 14:09:00+05:30', TIMESTAMPTZ '2026-10-28 14:10:00+05:30'
FROM users u JOIN payment_methods pm ON pm.customer_id = u.id AND pm.method_type = 'upi'
WHERE u.public_code = 'CUST-1001';

INSERT INTO wallet_transactions (wallet_id, customer_id, transaction_ref, invoice_number, txn_type, direction, amount, status, title, payment_id)
SELECT w.id, w.customer_id, 'TXN-20261028-0001', 'INV-20261028-0001', 'topup', 'credit', 1200, 'success', 'Wallet recharge via UPI', p.id
FROM wallets w JOIN users u ON u.id = w.customer_id JOIN payments p ON p.customer_id = u.id
WHERE u.public_code = 'CUST-1001' AND p.idempotency_key = 'idem-cust1001-20261028-1';

-- Bala: completed session -> wallet debit
-- 18.5 kWh x Rs 12 = 222.00, service fee 10.00, GST 18% on 232.00 = 41.76  => 273.76
INSERT INTO charging_sessions (session_number, customer_id, vehicle_id, charging_point_id, tariff_plan_id, status,
                               started_at, ended_at, start_soc, current_soc, energy_delivered_kwh, peak_power_kw,
                               rate_per_kwh, energy_cost, service_fee, gst_amount, payment_status, stop_reason)
SELECT 'SES-20261107-0001', u.id, v.id, cp.id, t.id, 'completed',
       now() - interval '1 day 2 hours', now() - interval '1 day 1 hour 25 minutes', 22, 68, 18.500, 58.4,
       12, 222.00, 10.00, 41.76, 'paid', 'Target SoC reached'
FROM users u
JOIN vehicles v ON v.owner_id = u.id AND v.license_plate = 'AP-39-AB-1234'
JOIN charging_points cp ON cp.point_code = 'AP-Z01-CP03'
JOIN tariff_plans t ON t.code = 'T-002'
WHERE u.public_code = 'CUST-1001';

INSERT INTO wallet_transactions (wallet_id, customer_id, transaction_ref, invoice_number, txn_type, direction, amount, status, title, charging_session_id)
SELECT w.id, w.customer_id, 'TXN-20261107-0002', 'INV-20261107-0002', 'charging_fee', 'debit', s.total_amount, 'success',
       'Charging at Vijayawada Central Hub', s.id
FROM charging_sessions s JOIN wallets w ON w.customer_id = s.customer_id
WHERE s.session_number = 'SES-20261107-0001';

-- Bala: upcoming reservation (tomorrow 14:00-15:00 IST)
INSERT INTO reservations (reservation_number, customer_id, vehicle_id, charging_point_id, tariff_plan_id,
                          scheduled_start, scheduled_end, rate_per_kwh, slot_fee, estimated_cost, status)
SELECT 'RSV-20261109-0001', u.id, v.id, cp.id, t.id,
       (date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') + interval '1 day 14 hours') AT TIME ZONE 'Asia/Kolkata',
       (date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') + interval '1 day 15 hours') AT TIME ZONE 'Asia/Kolkata',
       12, 25, 420, 'confirmed'
FROM users u
JOIN vehicles v ON v.owner_id = u.id AND v.license_plate = 'AP-39-AB-1234'
JOIN charging_points cp ON cp.point_code = 'AP-Z01-CP02'
JOIN tariff_plans t ON t.code = 'T-002'
WHERE u.public_code = 'CUST-1001';

-- Dr. Mohan: live session (trigger marks charger 'charging')
INSERT INTO charging_sessions (session_number, customer_id, vehicle_id, charging_point_id, tariff_plan_id, status,
                               started_at, start_soc, current_soc, energy_delivered_kwh, peak_power_kw, rate_per_kwh, energy_cost)
SELECT 'SES-20261108-0002', u.id, v.id, cp.id, t.id, 'charging',
       now() - interval '24 minutes', 22, 51, 22.450, 118.0, 12, 269.40
FROM users u
JOIN vehicles v ON v.owner_id = u.id
JOIN charging_points cp ON cp.point_code = 'AP-Z02-CP01'
JOIN tariff_plans t ON t.code = 'T-002'
WHERE u.public_code = 'CUST-1007';

-- settlements & collections (super-admin finance)
INSERT INTO admin_settlements (batch_number, admin_id, zone_id, period_start, period_end, gross_collections, refunds_deducted,
                               commission_deducted, verified_amount_received, status, payment_reference_utr, settlement_date,
                               verified_by, verified_at)
SELECT s.batch, a.id, z.id, s.ps::date, s.pe::date, s.gross, s.refunds, s.comm, s.verified, s.status::settlement_status, s.utr,
       s.sdate::date,
       CASE WHEN s.status = 'verified' THEN (SELECT id FROM users WHERE public_code = 'SA-001') END,
       CASE WHEN s.status = 'verified' THEN s.sdate::timestamptz END
FROM (VALUES
  ('VG-STL-AP01-202610A', 'ADM-AP01', 'AP-Z01', '2026-10-01', '2026-10-15', 360000, 2000, 18000, 340000, 'verified',   'SBIN0048192801', '2026-10-17'),
  ('VG-STL-AP01-202610B', 'ADM-AP01', 'AP-Z01', '2026-10-16', '2026-10-28', 339000, 2200, 16950, 289100, 'processing', 'SBIN0048339102', NULL),
  ('VG-STL-AP02-202610A', 'ADM-AP02', 'AP-Z02', '2026-10-01', '2026-10-15', 310000, 1500, 15500, 293000, 'verified',   'HDFC0019283711', '2026-10-17')
) AS s(batch, admin_code, zone_code, ps, pe, gross, refunds, comm, verified, status, utr, sdate)
JOIN users a ON a.public_code = s.admin_code
JOIN zones z ON z.code = s.zone_code;

INSERT INTO admin_collections (reference_number, admin_id, zone_id, customer_id, collection_type, payment_method, amount,
                               admin_commission, transaction_status, settlement_status, settlement_id, settlement_due_date,
                               payment_id, collected_at)
SELECT c.ref, a.id, z.id, cu.id, c.ctype::collection_type, c.pm::payment_method_type, c.amount, c.comm,
       c.tstatus::collection_status, c.sstatus::collection_settlement, st.id, DATE '2026-11-05',
       CASE WHEN c.ref = 'VG-REC-20261028-001'
            THEN (SELECT id FROM payments WHERE idempotency_key = 'idem-cust1001-20261028-1') END,
       c.at::timestamptz
FROM (VALUES
  ('VG-REC-20261028-001', 'ADM-AP01', 'AP-Z01', 'CUST-1001', 'wallet_topup',     'upi',         1200,  60.00, 'success', 'settled',            'VG-STL-AP01-202610B', '2026-10-28 14:10:00+05:30'),
  ('VG-REC-20261028-002', 'ADM-AP02', 'AP-Z02', 'CUST-1007', 'charging_session', 'card',         950,  47.50, 'success', 'in_escrow',          NULL,                  '2026-10-28 13:45:00+05:30'),
  ('VG-REC-20261028-003', 'ADM-AP03', 'AP-Z03', 'CUST-1003', 'charging_session', 'eway_wallet',  450,  20.25, 'pending', 'pending_settlement', NULL,                  '2026-10-28 11:20:00+05:30'),
  ('VG-REC-20261027-004', 'ADM-AP01', 'AP-Z01', 'CUST-1004', 'collection',       'netbanking',  8500, 425.00, 'success', 'settled',            'VG-STL-AP01-202610B', '2026-10-27 16:30:00+05:30')
) AS c(ref, admin_code, zone_code, cust_code, ctype, pm, amount, comm, tstatus, sstatus, batch, at)
JOIN users a  ON a.public_code  = c.admin_code
JOIN zones z  ON z.code         = c.zone_code
JOIN users cu ON cu.public_code = c.cust_code
LEFT JOIN admin_settlements st ON st.batch_number = c.batch;

-- maintenance
UPDATE charging_points SET status = 'maintenance' WHERE point_code IN ('AP-Z05-CP02', 'AP-Z02-CP04');

INSERT INTO maintenance_tickets (ticket_code, charging_point_id, fault_code, issue_description, severity, priority, status,
                                 reported_at, assigned_engineer_id, assigned_at, sla_minutes)
SELECT t.code, cp.id, t.fault, t.descr, t.sev::ticket_severity, t.prio::ticket_priority, t.status::maintenance_status,
       now() - t.age::interval, fe.user_id, CASE WHEN fe.user_id IS NOT NULL THEN now() - t.age::interval + interval '5 minutes' END,
       t.sla
FROM (VALUES
  ('MT-1024', 'AP-Z05-CP02', 'OCPP_CONNECTOR_LOCK_FAIL', 'Connector communication and interlock pin failure on Gun #2', 'critical', 'P1', 'in_progress', '40 minutes', 'ENG-101', 60),
  ('MT-1026', 'AP-Z02-CP04', 'GROUND_FAULT_INTERRUPT',   'RCD ground fault tripped on sub-distribution bus B',          'critical', 'P1', 'open',        '15 minutes', NULL,      90)
) AS t(code, point_code, fault, descr, sev, prio, status, age, eng_code, sla)
JOIN charging_points cp ON cp.point_code = t.point_code
LEFT JOIN field_engineers fe ON fe.engineer_code = t.eng_code;

INSERT INTO maintenance_ticket_events (ticket_id, actor_user_id, actor_name, actor_type, action, note, created_at)
SELECT t.id, NULL, 'OCPP Gateway', 'system'::timeline_actor, 'Fault reported', 'StatusNotification: Faulted / ConnectorLockFailure', t.reported_at
FROM maintenance_tickets t WHERE t.ticket_code = 'MT-1024'
UNION ALL
SELECT t.id, u.id, u.full_name, 'technician'::timeline_actor, 'Technician dispatched', 'ETA 20 minutes', t.assigned_at
FROM maintenance_tickets t JOIN users u ON u.id = t.assigned_engineer_id WHERE t.ticket_code = 'MT-1024';

INSERT INTO charger_diagnostics (charging_point_id, ticket_id, connection_status, connector_status, power_level_kw, rated_power_kw,
                                 temperature_c, ground_leakage_ma, voltage_v, current_a, ocpp_error_code, vendor_error_code)
SELECT t.charging_point_id, t.id, 'degraded', 'interlocked', 0, 120, 41.5, 2.1, 0, 0, 'ConnectorLockFailure', 'VND-E214'
FROM maintenance_tickets t WHERE t.ticket_code = 'MT-1024';

-- support, notifications, audit, settings
INSERT INTO support_tickets (ticket_number, customer_id, category, subject, description, station_id, charging_point_id, status)
SELECT 'TKT-1001', u.id, 'Charging Issues', 'Session stopped early', 'Charging stopped at 68% although target was 90%.', cp.station_id, cp.id, 'in_progress'
FROM users u, charging_points cp WHERE u.public_code = 'CUST-1001' AND cp.point_code = 'AP-Z01-CP03';

INSERT INTO support_ticket_replies (ticket_id, sender, sender_user_id, message)
SELECT t.id, 'customer'::support_sender, t.customer_id, 'Please check, my auto-stop is set to 90%.' FROM support_tickets t WHERE t.ticket_number = 'TKT-1001'
UNION ALL
SELECT t.id, 'support'::support_sender, (SELECT id FROM users WHERE public_code = 'ADM-AP01'), 'We are reviewing the charger logs and will update you shortly.'
FROM support_tickets t WHERE t.ticket_number = 'TKT-1001';

INSERT INTO notifications (user_id, type, title, message, target_route, entity_type, entity_id)
SELECT u.id, 'reservation'::notification_type, 'Reservation confirmed', 'Your slot at Vijayawada Central Hub is confirmed for tomorrow 2:00 PM.',
       '/customer/reservations', 'reservation', r.id
FROM users u JOIN reservations r ON r.customer_id = u.id WHERE u.public_code = 'CUST-1001'
UNION ALL
SELECT u.id, 'wallet'::notification_type, 'Wallet recharged', 'Rs 1,200 added to your E-Way wallet.', '/customer/wallet', 'wallet_transaction', wt.id
FROM users u JOIN wallet_transactions wt ON wt.customer_id = u.id AND wt.txn_type = 'topup' WHERE u.public_code = 'CUST-1001'
UNION ALL
SELECT u.id, 'maintenance'::notification_type, 'Technician Assigned to AP-Z05-CP02', 'Field Engineer K. Venkatesh dispatched for connector latch fault.',
       '/admin/maintenance', 'maintenance_ticket', t.id
FROM users u, maintenance_tickets t WHERE u.public_code = 'ADM-AP05' AND t.ticket_code = 'MT-1024';

INSERT INTO audit_logs (portal, actor_user_id, actor_name, actor_email, actor_role, action, action_type, module, target_resource, record_id,
                        previous_value, new_value, ip_address, device, severity)
SELECT 'admin'::audit_portal, u.id, u.full_name, u.email, 'Super Admin', 'Updated Tirupati car tariff', 'Updated', 'Pricing', 'Tariff T-004', 'T-004',
       '{"rate_per_kwh": 13}'::jsonb, '{"rate_per_kwh": 14}'::jsonb, '10.20.1.15'::inet, 'Chrome 140 / Windows 11', 'notice'::audit_severity
FROM users u WHERE u.public_code = 'STF-001'
UNION ALL
SELECT 'super_admin'::audit_portal, u.id, u.full_name, u.email, 'Super Admin', 'Verified settlement VG-STL-AP01-202610A', 'SETTLEMENT', 'Settlements',
       'Settlement VG-STL-AP01-202610A', 'VG-STL-AP01-202610A', NULL, '{"status": "verified"}'::jsonb, '10.20.1.10'::inet, 'Chrome 140 / Windows 11', 'info'::audit_severity
FROM users u WHERE u.public_code = 'SA-001';

INSERT INTO system_settings (key, value, description) VALUES
('network_name',                '"PowerGrid Andhra Pradesh EV Network"', 'Network title shown in the admin console'),
('auto_load_shedding',          'true',  'Automatically shed load when a zone nears substation capacity'),
('admin_session_timeout_mins',  '15',    'Admin console idle timeout'),
('reservation_grace_minutes',   '15',    'OCPP reservation grace period before a booking is released'),
('otp_resend_cooldown_seconds', '29',    'Customer OTP resend cooldown'),
('super_admin_session_mins',    '45',    'Super-admin auto-lock'),
('max_failed_logins',           '5',     'Failed logins before lockout'),
('lockout_minutes',             '15',    'Lockout duration after too many failed logins');

COMMIT;
