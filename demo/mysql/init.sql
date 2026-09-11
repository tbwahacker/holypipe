-- Demo data for the HolyPipe "mysql_demo" source container.
CREATE TABLE IF NOT EXISTS products (
    id INT AUTO_INCREMENT PRIMARY KEY,
    sku VARCHAR(64) NOT NULL,
    name VARCHAR(200) NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS inventory (
    id INT AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    warehouse VARCHAR(64) NOT NULL,
    quantity INT NOT NULL DEFAULT 0,
    FOREIGN KEY (product_id) REFERENCES products(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO products (sku, name, price) VALUES
    ('SKU-001', 'Maize Flour 2kg', 3.50),
    ('SKU-002', 'Cooking Oil 1L', 2.75),
    ('SKU-003', 'Rice 5kg', 6.20);

INSERT INTO inventory (product_id, warehouse, quantity) VALUES
    (1, 'Nairobi', 120),
    (2, 'Nairobi', 80),
    (3, 'Kampala', 45);

CREATE USER IF NOT EXISTS 'holypipe'@'%' IDENTIFIED BY 'holypipe';
GRANT ALL PRIVILEGES ON shop.* TO 'holypipe'@'%';
GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'holypipe'@'%';
FLUSH PRIVILEGES;
