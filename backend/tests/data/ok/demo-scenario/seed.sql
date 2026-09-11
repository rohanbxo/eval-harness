CREATE TABLE airports (
  code TEXT PRIMARY KEY,
  city TEXT NOT NULL
);
INSERT INTO airports (code, city) VALUES
  ('DXB', 'Dubai'),
  ('RUH', 'Riyadh'),
  ('JED', 'Jeddah');

CREATE TABLE bookings (
  id INTEGER PRIMARY KEY,
  flight_id TEXT NOT NULL,
  price_aed REAL NOT NULL,
  payload BLOB
);
INSERT INTO bookings (id, flight_id, price_aed, payload) VALUES
  (1, 'FL-101', 640.0, X'0102'),
  (2, 'FL-204', 910.0, NULL);

CREATE VIEW refundable_bookings AS
  SELECT id, flight_id FROM bookings WHERE flight_id = 'FL-204';
