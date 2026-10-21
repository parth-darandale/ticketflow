CREATE TABLE IF NOT EXISTS events (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    title text NOT NULL,
    date_label text NOT NULL,
    venue text NOT NULL,
    category text NOT NULL,
    description text NOT NULL,
    total_seats integer NOT NULL CHECK (total_seats > 0),
    available_seats integer NOT NULL CHECK (available_seats >= 0 AND available_seats <= total_seats),
    price_cents integer NOT NULL DEFAULT 0 CHECK (price_cents >= 0),
    accent text NOT NULL DEFAULT 'teal',
    is_demo boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS bookings (
    id uuid PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES events(id),
    idempotency_key uuid NOT NULL UNIQUE,
    customer_name text NOT NULL,
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 6),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','confirmed','rejected')),
    worker_id text,
    attempts integer NOT NULL DEFAULT 0,
    claim_token uuid,
    lease_until timestamptz,
    rejection_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    CHECK ((status = 'processing') = (claim_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS booking_queue ON bookings(created_at) WHERE status IN ('pending','processing');
CREATE INDEX IF NOT EXISTS booking_event ON bookings(event_id, status);

CREATE TABLE IF NOT EXISTS workers (
    id text PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    current_booking_id uuid,
    processed_count integer NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS booking_activity (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    booking_id uuid NOT NULL REFERENCES bookings(id),
    kind text NOT NULL,
    message text NOT NULL,
    worker_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS activity_booking ON booking_activity(booking_id, id);
