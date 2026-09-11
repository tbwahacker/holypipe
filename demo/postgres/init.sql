-- Demo data for the HolyPipe "postgres_demo" source container.
CREATE TABLE IF NOT EXISTS customers (
    id SERIAL PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount NUMERIC(10, 2) NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

INSERT INTO customers (full_name, email) VALUES
    ('Amara Obi', 'amara@example.com'),
    ('Kwame Mensah', 'kwame@example.com'),
    ('Zanele Dlamini', 'zanele@example.com');

INSERT INTO orders (customer_id, amount, status) VALUES
    (1, 49.99, 'paid'),
    (2, 120.00, 'pending'),
    (3, 15.50, 'paid');

-- A logical replication slot needs REPLICA IDENTITY FULL (or a primary key,
-- already present here) for updates/deletes to carry enough old-row data.
ALTER TABLE customers REPLICA IDENTITY FULL;
ALTER TABLE orders REPLICA IDENTITY FULL;
