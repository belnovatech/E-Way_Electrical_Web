-- integrity checks. run on a seeded db:
--   psql -d eway_db -f database/tests/integrity_tests.sql
-- all inside one transaction that gets rolled back, so nothing is changed.
\set ON_ERROR_STOP on
\pset footer off
BEGIN;

CREATE TEMP TABLE test_results (n serial, test text, result text, detail text) ON COMMIT DROP;

-- statement must be rejected by the database
CREATE PROCEDURE pg_temp.must_fail(p_test text, p_sql text) LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        EXECUTE p_sql;
        INSERT INTO test_results (test, result, detail) VALUES (p_test, 'FAIL', 'statement was accepted');
    EXCEPTION WHEN OTHERS THEN
        INSERT INTO test_results (test, result, detail) VALUES (p_test, 'PASS', SQLSTATE || ' ' || left(SQLERRM, 90));
    END;
END $$;

-- query must return true
CREATE PROCEDURE pg_temp.must_be_true(p_test text, p_sql text) LANGUAGE plpgsql AS $$
DECLARE ok boolean;
BEGIN
    EXECUTE p_sql INTO ok;
    INSERT INTO test_results (test, result, detail) VALUES (p_test, CASE WHEN ok THEN 'PASS' ELSE 'FAIL' END, CASE WHEN ok THEN 'ok' ELSE 'expected true' END);
END $$;

-- ownership relations
CALL pg_temp.must_fail('Reservation with another customer''s vehicle is rejected', $q$
  INSERT INTO reservations (reservation_number, customer_id, vehicle_id, charging_point_id, scheduled_start, scheduled_end, rate_per_kwh)
  SELECT 'T-RSV-1', (SELECT id FROM users WHERE public_code='CUST-1002'),
         (SELECT id FROM vehicles WHERE license_plate='AP-39-AB-1234'),
         (SELECT id FROM charging_points WHERE point_code='AP-Z03-CP01'), now() + interval '3 days', now() + interval '3 days 1 hour', 12 $q$);

CALL pg_temp.must_fail('Session with another customer''s vehicle is rejected', $q$
  INSERT INTO charging_sessions (session_number, customer_id, vehicle_id, charging_point_id, rate_per_kwh)
  SELECT 'T-SES-1', (SELECT id FROM users WHERE public_code='CUST-1002'),
         (SELECT id FROM vehicles WHERE license_plate='AP-39-AB-1234'),
         (SELECT id FROM charging_points WHERE point_code='AP-Z03-CP01'), 12 $q$);

CALL pg_temp.must_fail('Session cannot use another customer''s reservation', $q$
  INSERT INTO charging_sessions (session_number, customer_id, vehicle_id, charging_point_id, reservation_id, rate_per_kwh)
  SELECT 'T-SES-2', u.id, v.id, (SELECT id FROM charging_points WHERE point_code='AP-Z03-CP01'),
         (SELECT id FROM reservations WHERE reservation_number='RSV-20261109-0001'), 12
  FROM users u JOIN vehicles v ON v.owner_id = u.id WHERE u.public_code='CUST-1002' $q$);

CALL pg_temp.must_fail('Wallet debit linked to another customer''s session is rejected', $q$
  INSERT INTO wallet_transactions (wallet_id, customer_id, transaction_ref, txn_type, direction, amount, status, charging_session_id)
  SELECT w.id, w.customer_id, 'T-TXN-1', 'charging_fee', 'debit', 1, 'pending',
         (SELECT id FROM charging_sessions WHERE session_number='SES-20261108-0002')
  FROM wallets w JOIN users u ON u.id = w.customer_id WHERE u.public_code='CUST-1001' $q$);

CALL pg_temp.must_fail('Wallet row and customer_id must match', $q$
  INSERT INTO wallet_transactions (wallet_id, customer_id, transaction_ref, txn_type, direction, amount, created_by)
  SELECT (SELECT w.id FROM wallets w JOIN users u ON u.id=w.customer_id WHERE u.public_code='CUST-1001'),
         (SELECT id FROM users WHERE public_code='CUST-1002'), 'T-TXN-2', 'adjustment_credit', 'credit', 10,
         (SELECT id FROM users WHERE public_code='SA-001') $q$);

CALL pg_temp.must_fail('Customer profile cannot be attached to a staff user', $q$
  INSERT INTO customer_profiles (user_id) SELECT id FROM users WHERE public_code='ADM-AP02' $q$);

CALL pg_temp.must_fail('Staff profile cannot be attached to a customer', $q$
  INSERT INTO staff_profiles (user_id, user_type, role_id)
  SELECT u.id, 'admin', r.id FROM users u, roles r WHERE u.public_code='CUST-1002' AND r.code='ROLE_ZONE_MANAGER' $q$);

-- area-admin relations
CALL pg_temp.must_fail('Customer cannot be assigned to an admin who does not manage their zone', $q$
  UPDATE customer_profiles SET assigned_admin_id = (SELECT id FROM users WHERE public_code='ADM-AP03')
  WHERE user_id = (SELECT id FROM users WHERE public_code='CUST-1001') $q$);

CALL pg_temp.must_fail('Collection in a zone the admin is not assigned to is rejected', $q$
  INSERT INTO admin_collections (reference_number, admin_id, zone_id, customer_id, collection_type, payment_method, amount)
  SELECT 'T-COL-1', (SELECT id FROM users WHERE public_code='ADM-AP03'), (SELECT id FROM zones WHERE code='AP-Z01'),
         (SELECT id FROM users WHERE public_code='CUST-1001'), 'collection', 'upi', 100 $q$);

CALL pg_temp.must_fail('Collection cannot be put in another admin''s settlement batch', $q$
  UPDATE admin_collections SET settlement_id = (SELECT id FROM admin_settlements WHERE batch_number='VG-STL-AP02-202610A')
  WHERE reference_number='VG-REC-20261027-004' $q$);

CALL pg_temp.must_fail('Overlapping settlement periods for the same admin+zone are rejected', $q$
  INSERT INTO admin_settlements (batch_number, admin_id, zone_id, period_start, period_end)
  SELECT 'T-STL-1', a.admin_id, a.zone_id, DATE '2026-10-10', DATE '2026-10-20'
  FROM admin_zone_assignments a JOIN users u ON u.id=a.admin_id JOIN zones z ON z.id=a.zone_id
  WHERE u.public_code='ADM-AP01' AND z.code='AP-Z01' $q$);

CALL pg_temp.must_fail('Settlement cannot be verified without UTR / verifier', $q$
  UPDATE admin_settlements SET status='verified', payment_reference_utr=NULL WHERE batch_number='VG-STL-AP01-202610B' $q$);

CALL pg_temp.must_fail('Admin with collections cannot be hard-deleted (history protected)', $q$
  DELETE FROM users WHERE public_code='ADM-AP01' $q$);

-- booking rules
CALL pg_temp.must_fail('Charger cannot be double-booked for overlapping slots', $q$
  INSERT INTO reservations (reservation_number, customer_id, vehicle_id, charging_point_id, scheduled_start, scheduled_end, rate_per_kwh)
  SELECT 'T-RSV-2', u.id, v.id, r.charging_point_id, r.scheduled_start + interval '30 minutes', r.scheduled_end + interval '30 minutes', 12
  FROM reservations r, users u JOIN vehicles v ON v.owner_id=u.id
  WHERE r.reservation_number='RSV-20261109-0001' AND u.public_code='CUST-1002' $q$);

CALL pg_temp.must_fail('Vehicle cannot hold two overlapping reservations', $q$
  INSERT INTO reservations (reservation_number, customer_id, vehicle_id, charging_point_id, scheduled_start, scheduled_end, rate_per_kwh)
  SELECT 'T-RSV-3', r.customer_id, r.vehicle_id, (SELECT id FROM charging_points WHERE point_code='AP-Z01-CP04'),
         r.scheduled_start, r.scheduled_end, 8
  FROM reservations r WHERE r.reservation_number='RSV-20261109-0001' $q$);

CALL pg_temp.must_be_true('Cancelled slot can be re-booked by someone else', $q$
  WITH c AS (UPDATE reservations SET status='cancelled', cancelled_at=now() WHERE reservation_number='RSV-20261109-0001' RETURNING *),
       n AS (INSERT INTO reservations (reservation_number, customer_id, vehicle_id, charging_point_id, scheduled_start, scheduled_end, rate_per_kwh)
             SELECT 'T-RSV-4', u.id, v.id, c.charging_point_id, c.scheduled_start, c.scheduled_end, 12
             FROM c, users u JOIN vehicles v ON v.owner_id=u.id WHERE u.public_code='CUST-1002' RETURNING 1)
  SELECT count(*) = 1 FROM n $q$);

CALL pg_temp.must_fail('Second live session on the same charger is rejected', $q$
  INSERT INTO charging_sessions (session_number, customer_id, vehicle_id, charging_point_id, rate_per_kwh)
  SELECT 'T-SES-3', u.id, v.id, (SELECT id FROM charging_points WHERE point_code='AP-Z02-CP01'), 12
  FROM users u JOIN vehicles v ON v.owner_id=u.id WHERE u.public_code='CUST-1002' $q$);

CALL pg_temp.must_fail('Session cannot start on a charger under maintenance', $q$
  INSERT INTO charging_sessions (session_number, customer_id, vehicle_id, charging_point_id, rate_per_kwh)
  SELECT 'T-SES-4', u.id, v.id, (SELECT id FROM charging_points WHERE point_code='AP-Z05-CP02'), 12
  FROM users u JOIN vehicles v ON v.owner_id=u.id WHERE u.public_code='CUST-1002' $q$);

CALL pg_temp.must_fail('Finished session must have an end time', $q$
  UPDATE charging_sessions SET status='completed' WHERE session_number='SES-20261108-0002' $q$);

CALL pg_temp.must_be_true('Ending a live session frees the charger', $q$
  WITH s AS (UPDATE charging_sessions SET status='completed', ended_at=now() WHERE session_number='SES-20261108-0002' RETURNING charging_point_id)
  SELECT true FROM s $q$);
CALL pg_temp.must_be_true('... charger AP-Z02-CP01 is available again', $q$
  SELECT status = 'available' FROM charging_points WHERE point_code='AP-Z02-CP01' $q$);

-- wallet ledger
CALL pg_temp.must_be_true('Seeded wallet balance equals ledger (1200 - 273.76)', $q$
  SELECT w.balance = 926.24 AND w.balance = (SELECT SUM(CASE WHEN direction='credit' THEN amount ELSE -amount END)
                                               FROM wallet_transactions t WHERE t.wallet_id=w.id AND t.status='success')
  FROM wallets w JOIN users u ON u.id=w.customer_id WHERE u.public_code='CUST-1001' $q$);

CALL pg_temp.must_fail('Debit larger than the balance is rejected', $q$
  INSERT INTO wallet_transactions (wallet_id, customer_id, transaction_ref, txn_type, direction, amount, status, created_by)
  SELECT w.id, w.customer_id, 'T-TXN-3', 'adjustment_debit', 'debit', 5000, 'success', (SELECT id FROM users WHERE public_code='SA-001')
  FROM wallets w JOIN users u ON u.id=w.customer_id WHERE u.public_code='CUST-1001' $q$);

CALL pg_temp.must_fail('Wallet balance cannot be edited directly', $q$
  UPDATE wallets SET balance = 99999 WHERE customer_id=(SELECT id FROM users WHERE public_code='CUST-1001') $q$);

CALL pg_temp.must_fail('Posted wallet transaction cannot be edited', $q$
  UPDATE wallet_transactions SET status='failed' WHERE transaction_ref='TXN-20261028-0001' $q$);

CALL pg_temp.must_fail('Wallet transactions cannot be deleted', $q$
  DELETE FROM wallet_transactions WHERE transaction_ref='TXN-20261028-0001' $q$);

CALL pg_temp.must_fail('Top-up must reference a gateway payment', $q$
  INSERT INTO wallet_transactions (wallet_id, customer_id, transaction_ref, txn_type, direction, amount)
  SELECT w.id, w.customer_id, 'T-TXN-4', 'topup', 'credit', 100 FROM wallets w LIMIT 1 $q$);

CALL pg_temp.must_be_true('Pending -> success posts the balance once', $q$
  WITH p AS (INSERT INTO wallet_transactions (wallet_id, customer_id, transaction_ref, txn_type, direction, amount, status, created_by)
             SELECT w.id, w.customer_id, 'T-TXN-5', 'adjustment_credit', 'credit', 73.76, 'pending', (SELECT id FROM users WHERE public_code='SA-001')
             FROM wallets w JOIN users u ON u.id=w.customer_id WHERE u.public_code='CUST-1001' RETURNING id)
  SELECT count(*) = 1 FROM p $q$);
UPDATE wallet_transactions SET status = 'success' WHERE transaction_ref = 'T-TXN-5';
CALL pg_temp.must_be_true('... balance is now 1000.00', $q$
  SELECT balance = 1000.00 FROM wallets w JOIN users u ON u.id=w.customer_id WHERE u.public_code='CUST-1001' $q$);

CALL pg_temp.must_be_true('New customer gets a wallet automatically', $q$
  WITH u AS (INSERT INTO users (user_type, full_name, phone_number) VALUES ('customer', 'Test User', '9000000001') RETURNING id),
       p AS (INSERT INTO customer_profiles (user_id) SELECT id FROM u RETURNING user_id)
  SELECT count(*) = 1 FROM p $q$);
CALL pg_temp.must_be_true('... and its balance is 0', $q$
  SELECT balance = 0 FROM wallets w JOIN users u ON u.id=w.customer_id WHERE u.phone_number='9000000001' $q$);

CALL pg_temp.must_fail('Payment order type must match its references', $q$
  INSERT INTO payments (customer_id, order_type, reservation_id, base_amount, idempotency_key)
  SELECT r.customer_id, 'wallet_topup', r.id, 100, 'T-IDEM-1' FROM reservations r LIMIT 1 $q$);

-- vehicles, pricing, infra
CALL pg_temp.must_fail('Same plate with different formatting is a duplicate', $q$
  INSERT INTO vehicles (owner_id, model, vehicle_type, license_plate)
  SELECT id, 'Clone', 'car', 'ap 39 ab 1234' FROM users WHERE public_code='CUST-1002' $q$);

CALL pg_temp.must_fail('Only one default vehicle per customer', $q$
  UPDATE vehicles SET is_default = true WHERE license_plate='AP-39-CD-5678' $q$);

CALL pg_temp.must_fail('Overlapping active tariff with same zone/vehicle/priority is rejected', $q$
  INSERT INTO tariff_plans (code, name, vehicle_type, rate_per_kwh, effective_from, effective_to, priority)
  VALUES ('T-999', 'Duplicate global car', 'car', 11, DATE '2026-06-01', DATE '2027-06-01', 3) $q$);

CALL pg_temp.must_fail('Support ticket charger must belong to the chosen station', $q$
  INSERT INTO support_tickets (ticket_number, customer_id, category, subject, station_id, charging_point_id)
  SELECT 'T-TKT-1', (SELECT id FROM users WHERE public_code='CUST-1001'), 'Charging Issues', 'x',
         (SELECT id FROM stations WHERE code='ST-AP-Z01'), (SELECT id FROM charging_points WHERE point_code='AP-Z02-CP01') $q$);

CALL pg_temp.must_fail('Zone with stations cannot be deleted', $q$
  DELETE FROM zones WHERE code='AP-Z01' $q$);

CALL pg_temp.must_fail('Charger with session history cannot be deleted', $q$
  DELETE FROM charging_points WHERE point_code='AP-Z01-CP03' $q$);

CALL pg_temp.must_fail('Field engineer needs the field-engineer role', $q$
  INSERT INTO field_engineers (user_id, engineer_code, zone_id)
  SELECT u.id, 'ENG-999', (SELECT id FROM zones WHERE code='AP-Z01') FROM users u WHERE public_code='STF-012' $q$);

CALL pg_temp.must_fail('Protected role cannot be edited', $q$
  UPDATE roles SET name = 'Hacked' WHERE code='ROLE_SUPER_ADMIN' $q$);

CALL pg_temp.must_fail('Resolved ticket needs resolved_at', $q$
  UPDATE maintenance_tickets SET status='resolved' WHERE ticket_code='MT-1026' $q$);

CALL pg_temp.must_fail('Audit logs are append-only', $q$
  UPDATE audit_logs SET action='tampered' $q$);

SELECT n AS "#", result, test, detail FROM test_results ORDER BY n;
SELECT count(*) FILTER (WHERE result='PASS') AS passed, count(*) FILTER (WHERE result='FAIL') AS failed FROM test_results;

ROLLBACK;
