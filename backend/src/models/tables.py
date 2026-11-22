from sqlalchemy import (JSON, Column, ForeignKey, Integer, MetaData, String,
                        Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))

# 验收记录：一次入库的不可变输入，按制作组与归属月份登记占用。
billing_records = Table('billing_records', schema,
    Column('id', Integer, primary_key=True),
    Column('record_key', String(128), nullable=False, unique=True),
    Column('month', String(7), nullable=False, index=True),
    Column('production_group', String(64), nullable=False),
    Column('status', String(16), nullable=False, index=True),
    Column('payload', JSON, nullable=False),
    Column('blocks', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('author', String(40), nullable=False),
    Column('note', String(500)),
    Column('created_at', String(32), nullable=False),
    Column('reversed_at', String(32)),
    Column('reversed_by', String(40)),
    Column('reverse_note', String(500)))

# 账期内每个内容块的权威实测字节；(月份, 地址) 唯一，供并发入账原子校验。
billing_block_facts = Table('billing_block_facts', schema,
    Column('id', Integer, primary_key=True),
    Column('month', String(7), nullable=False, index=True),
    Column('address', String(64), nullable=False),
    Column('bytes', Integer, nullable=False),
    UniqueConstraint('month', 'address', name='uq_billing_block_fact'))

# 账期：每月一行，关账后状态不再回到 open；序号用于差额结算。
billing_periods = Table('billing_periods', schema,
    Column('id', Integer, primary_key=True),
    Column('month', String(7), nullable=False, unique=True),
    Column('status', String(16), nullable=False),
    Column('settlement_seq', Integer, nullable=False),
    Column('latest_settlement_id', Integer),
    Column('closed_at', String(32)),
    Column('closed_by', String(40)),
    Column('updated_at', String(32), nullable=False))

# 结算批次：每次关账或差额入账生成一个不可变批次。
billing_settlements = Table('billing_settlements', schema,
    Column('id', Integer, primary_key=True),
    Column('month', String(7), nullable=False, index=True),
    Column('seq', Integer, nullable=False),
    Column('kind', String(16), nullable=False),
    Column('trigger', String(16), nullable=False),
    Column('cutoff_record_id', Integer, nullable=False),
    Column('input_digest', String(64), nullable=False),
    Column('totals', JSON, nullable=False),
    Column('actor', String(40), nullable=False),
    Column('created_at', String(32), nullable=False),
    UniqueConstraint('month', 'seq', name='uq_billing_settlement_seq'))

# 账单：已发布后不再改写；差额账单以新的负数或正数行冲抵。
billing_bills = Table('billing_bills', schema,
    Column('id', Integer, primary_key=True),
    Column('settlement_id', ForeignKey('billing_settlements.id'), nullable=False, index=True),
    Column('month', String(7), nullable=False, index=True),
    Column('production_group', String(64), nullable=False, index=True),
    Column('kind', String(16), nullable=False),
    Column('total_bytes', Integer, nullable=False),
    Column('basis', JSON, nullable=False),
    Column('created_at', String(32), nullable=False),
    UniqueConstraint('settlement_id', 'production_group', name='uq_billing_bill_group'))

# 账单明细：按块记录增/减字节，便于按块核对与回查原始依据。
billing_allocations = Table('billing_allocations', schema,
    Column('id', Integer, primary_key=True),
    Column('bill_id', ForeignKey('billing_bills.id'), nullable=False, index=True),
    Column('month', String(7), nullable=False, index=True),
    Column('production_group', String(64), nullable=False),
    Column('address', String(64), nullable=False),
    Column('bytes', Integer, nullable=False))
