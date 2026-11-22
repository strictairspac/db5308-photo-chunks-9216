-- 0002 占用账单
-- 既有部署升级时执行；新建库由 src/models/tables.py 在启动时创建同结构。
-- 所有金额/字节均为整数；账单发布后不再 UPDATE，只追加差额账单。

CREATE TABLE IF NOT EXISTS billing_records (
  id INT NOT NULL AUTO_INCREMENT,
  record_key VARCHAR(128) NOT NULL,
  month VARCHAR(7) NOT NULL,
  production_group VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL,
  payload JSON NOT NULL,
  blocks JSON NOT NULL,
  digest VARCHAR(64) NOT NULL,
  author VARCHAR(40) NOT NULL,
  note VARCHAR(500) NULL,
  created_at VARCHAR(32) NOT NULL,
  reversed_at VARCHAR(32) NULL,
  reversed_by VARCHAR(40) NULL,
  reverse_note VARCHAR(500) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_billing_record_key (record_key),
  KEY ix_billing_records_month (month),
  KEY ix_billing_records_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS billing_block_facts (
  id INT NOT NULL AUTO_INCREMENT,
  month VARCHAR(7) NOT NULL,
  address VARCHAR(64) NOT NULL,
  bytes INT NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_billing_block_fact (month, address),
  KEY ix_billing_block_facts_month (month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS billing_periods (
  id INT NOT NULL AUTO_INCREMENT,
  month VARCHAR(7) NOT NULL,
  status VARCHAR(16) NOT NULL,
  settlement_seq INT NOT NULL,
  latest_settlement_id INT NULL,
  closed_at VARCHAR(32) NULL,
  closed_by VARCHAR(40) NULL,
  updated_at VARCHAR(32) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_billing_period_month (month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS billing_settlements (
  id INT NOT NULL AUTO_INCREMENT,
  month VARCHAR(7) NOT NULL,
  seq INT NOT NULL,
  kind VARCHAR(16) NOT NULL,
  `trigger` VARCHAR(16) NOT NULL,
  cutoff_record_id INT NOT NULL,
  input_digest VARCHAR(64) NOT NULL,
  totals JSON NOT NULL,
  actor VARCHAR(40) NOT NULL,
  created_at VARCHAR(32) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_billing_settlement_seq (month, seq),
  KEY ix_billing_settlements_month (month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS billing_bills (
  id INT NOT NULL AUTO_INCREMENT,
  settlement_id INT NOT NULL,
  month VARCHAR(7) NOT NULL,
  production_group VARCHAR(64) NOT NULL,
  kind VARCHAR(16) NOT NULL,
  total_bytes INT NOT NULL,
  basis JSON NOT NULL,
  created_at VARCHAR(32) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_billing_bill_group (settlement_id, production_group),
  KEY ix_billing_bills_month (month),
  KEY ix_billing_bills_group (production_group),
  CONSTRAINT fk_billing_bill_settlement FOREIGN KEY (settlement_id) REFERENCES billing_settlements (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS billing_allocations (
  id INT NOT NULL AUTO_INCREMENT,
  bill_id INT NOT NULL,
  month VARCHAR(7) NOT NULL,
  production_group VARCHAR(64) NOT NULL,
  address VARCHAR(64) NOT NULL,
  bytes INT NOT NULL,
  PRIMARY KEY (id),
  KEY ix_billing_allocations_bill (bill_id),
  KEY ix_billing_allocations_month (month),
  CONSTRAINT fk_billing_allocation_bill FOREIGN KEY (bill_id) REFERENCES billing_bills (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
