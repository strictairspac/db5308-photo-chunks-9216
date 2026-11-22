"""按验收记录给制作组出占用账单。

口径：
- 每份验收记录归属一个制作组和一个自然月，记录列出本次实测到的内容块及其字节数。
- 每个内容块在账期内只计一次实测字节，并在引用它的制作组之间平分；同组重复引用不重复计费。
- 关账发布原始账单；关账后收到迟到记录或撤销，自动追加差额账单。账单及其依据一经发布不再改写。
- 入账只写记录表、从不持有账期行锁，因此关账并发时仍能持续收单；记录落库后再尝试补差，
  账期被关账事务占用时稍后重试，绝不阻塞其他入账。
- 差额按「本次目标占用 − 往期已出账」逐块计算；输入未变化时返回既有结算，中断重试不会重复出账。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from src.config.settings import IS_MYSQL, IS_SQLITE, engine
from src.constants.workflow import BillingPeriodStatus, BillingRecordStatus, SettlementKind
from src.models.tables import (audit, billing_allocations as allocations,
                               billing_bills as bills, billing_block_facts as facts_table,
                               billing_periods as periods, billing_records as records,
                               billing_settlements as settlements, evidence)
from src.utils.encoding import canonical
from src.utils.time import utc_time

MONTH = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
ADDRESS = re.compile(r'^[0-9a-f]{64}$')
MAX_BLOCK_BYTES = 256 * 1024
CATALOG = '内容块目录与留存建议'
# 记录提交后的对账重试：先非阻塞取锁，避开正在进行的关账事务。
RECONCILE_RETRIES = (0, 0.05, 0.1, 0.2, 0.4)
# 单次对账的复核轮数上限：每次提交后重新读取，捕获刚提交的并发记录。
VERIFY_PASSES = 4


def _log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference),
                                        created_at=utc_time()))


def _month(value):
    if not isinstance(value, str) or not MONTH.fullmatch(value):
        raise HTTPException(422, '归属月份须为 YYYY-MM')
    return value


def _name(value, label, limit=120):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or value != value.strip():
        raise HTTPException(422, f'{label}须为 1 至 {limit} 个字符，且首尾不能有空白')
    return value


def _blocks(rows):
    if not isinstance(rows, list):
        raise HTTPException(422, '内容块清单须为数组')
    seen = {}
    for row in rows:
        if hasattr(row, 'model_dump'):
            row = row.model_dump()
        if not isinstance(row, dict):
            raise HTTPException(422, '内容块须由对象组成')
        address = row.get('address')
        measured = row.get('bytes')
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise HTTPException(422, '内容块地址须为 64 位小写十六进制 SHA-256')
        if type(measured) is not int or not 0 <= measured <= MAX_BLOCK_BYTES:
            raise HTTPException(422, f'内容块实测字节须为 0 至 {MAX_BLOCK_BYTES} 的整数')
        if address in seen and seen[address] != measured:
            raise HTTPException(422, f'同一内容块的实测字节不一致：{address}')
        seen[address] = measured
    # 同一记录内重复地址只保留一次，排序后作为不可变依据。
    return [{'address': address, 'bytes': seen[address]} for address in sorted(seen)]


def _blocks_from_run(run_id):
    if type(run_id) is not int:
        raise HTTPException(422, '执行编号须为整数')
    with engine.connect() as conn:
        row = conn.execute(select(evidence).where(evidence.c.run_id == run_id)).mappings().first()
    if row is None:
        raise HTTPException(404, '该执行没有可入账的业务证据')
    section = next((x for x in row['report'].get('sections', []) if x.get('title') == CATALOG), None)
    if section is None:
        raise HTTPException(422, '证据中没有内容块目录')
    return _blocks([{'address': x.get('内容块 SHA-256'), 'bytes': x.get('入库字节数')}
                    for x in section.get('rows', [])])


def _record_digest(month, production_group, blocks):
    return hashlib.sha256(canonical({'month': month, 'production_group': production_group,
                                     'blocks': blocks})).hexdigest()


def _record(conn, identity):
    row = conn.execute(select(records).where(records.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该验收记录')
    return dict(row)


def _by_key(conn, key):
    return conn.execute(select(records).where(records.c.record_key == key)).mappings().first()


def _same_record(row, month, production_group, blocks):
    if row['month'] != month or row['production_group'] != production_group or row['blocks'] != blocks:
        raise HTTPException(409, '该记录键已用于其它验收内容')
    return dict(row)


def _check_measurements(conn, month, blocks):
    """同一内容块是同一份字节，其长度在账期内必须一致。

    以 (月份, 地址) 唯一的事实表做原子校验：先按地址排序逐条插入，唯一冲突说明
    已有不同记录登记了同一块，此时再比较字节；相同即视为幂等。
    """
    for item in sorted(blocks, key=lambda row: row['address']):
        values = {'month': month, 'address': item['address'], 'bytes': item['bytes']}
        try:
            with conn.begin_nested():
                conn.execute(facts_table.insert().values(**values))
        except IntegrityError:
            # 唯一冲突后必须做当前读：在可重复读隔离级别下，普通查询仍看旧快照。
            known = conn.execute(select(facts_table.c.bytes).where(
                facts_table.c.month == month,
                facts_table.c.address == item['address']).with_for_update()).scalar_one_or_none()
            if known != item['bytes']:
                raise HTTPException(
                    422, f'内容块 {item["address"]} 在账期内实测字节不一致') from None


# ---------------------------------------------------------------------------
# 入账与撤销：只写记录表，不取账期行锁。
# ---------------------------------------------------------------------------

def add_record(form, actor):
    month = _month(form.month)
    production_group = _name(form.production_group, '制作组', 64)
    record_key = _name(form.record_key, '记录键', 128)
    if (form.blocks is None) == (form.run_id is None):
        raise HTTPException(422, '内容块清单与执行编号须且只须提供一项')
    blocks = _blocks(form.blocks) if form.blocks is not None else _blocks_from_run(form.run_id)
    note = _name(form.note, '备注', 500) if form.note is not None else None
    source = {'run_id': form.run_id} if form.run_id is not None else {'manual': True}

    try:
        with engine.begin() as conn:
            existing = _by_key(conn, record_key)
            if existing is not None:
                result = _same_record(existing, month, production_group, blocks)
            else:
                _check_measurements(conn, month, blocks)
                identity = conn.execute(records.insert().values(
                    record_key=record_key, month=month, production_group=production_group,
                    status=BillingRecordStatus.ACTIVE,
                    payload={'month': month, 'production_group': production_group,
                             'blocks': blocks, 'source': source},
                    blocks=blocks, digest=_record_digest(month, production_group, blocks),
                    author=actor, note=note, created_at=utc_time())).inserted_primary_key[0]
                _log(conn, actor, 'billing_record', identity)
                result = _record(conn, identity)
    except IntegrityError:
        # 并发重试命中同一记录键：返回已落库的那条，保证不重复入账。
        with engine.begin() as conn:
            existing = _by_key(conn, record_key)
            if existing is None:
                raise HTTPException(409, '记录写入冲突，请重试') from None
            result = _same_record(existing, month, production_group, blocks)

    _reconcile_after_write(month, actor, 'late_record')
    return result


def reverse_record(identity, note, actor):
    note = _name(note, '撤销说明', 500)
    with engine.begin() as conn:
        row = conn.execute(select(records).where(records.c.id == identity)
                           .with_for_update()).mappings().first()
        if row is None:
            raise HTTPException(404, '找不到该验收记录')
        if row['reversed_at'] is None:
            conn.execute(records.update().where(
                records.c.id == identity, records.c.reversed_at.is_(None)).values(
                status=BillingRecordStatus.REVERSED, reversed_at=utc_time(),
                reversed_by=actor, reverse_note=note))
            _log(conn, actor, 'billing_reverse', identity)
        result = _record(conn, identity)
    _reconcile_after_write(result['month'], actor, 'reverse')
    return result


def _reconcile_after_write(month, actor, trigger):
    """记录落库后补差。

    记录先独立提交，入账路径从不持有账期锁，因此并发关账时新记录照常收下、照常落库。
    提交后再补差：先以 skip_locked 非阻塞尝试；账期正被关账/对账事务占用时短暂重试，
    仍被占用则等其释放后补差。等待只发生在本记录已落库之后，不会停收；这样迟到记录
    与撤销必进差额账单，不会因锁竞争漏账。
    """
    for delay in RECONCILE_RETRIES:
        if delay:
            time.sleep(delay)
        status, _ = _reconcile_once(month, actor, trigger, skip_locked=True)
        if status not in ('conflict', 'skipped'):
            return
    _reconcile(month, actor, trigger, skip_locked=False)


# ---------------------------------------------------------------------------
# 账期与结算：只有关账/补差路径持有账期行锁。
# ---------------------------------------------------------------------------

def _period_values(month):
    return {'month': month, 'status': BillingPeriodStatus.OPEN, 'settlement_seq': 0,
            'updated_at': utc_time()}


def _create_period(conn, month):
    values = _period_values(month)
    if IS_MYSQL:
        statement = mysql_insert(periods).values(**values).on_duplicate_key_update(month=periods.c.month)
    elif IS_SQLITE:
        statement = sqlite_insert(periods).values(**values).on_conflict_do_nothing(index_elements=['month'])
    else:
        statement = postgres_insert(periods).values(**values).on_conflict_do_nothing(index_elements=['month'])
    try:
        with conn.begin_nested():
            conn.execute(statement)
    except IntegrityError:
        pass


def _lock_period(conn, month, skip_locked=False):
    statement = select(periods).where(periods.c.month == month)
    if engine.dialect.name in ('mysql', 'mariadb', 'postgresql'):
        statement = statement.with_for_update(skip_locked=skip_locked)
    return conn.execute(statement).mappings().first()


def _ensure_period(conn, month):
    """关账路径取得账期行锁；不存在时创建后再加锁。

    账期行只由关账路径创建，入账从不写入该行，因此收单不会被关账行锁阻塞。
    """
    row = _lock_period(conn, month)
    if row is not None:
        return row
    _create_period(conn, month)
    return _lock_period(conn, month)


def _month_records(conn, month):
    return [dict(row) for row in conn.execute(
        select(records).where(records.c.month == month).order_by(records.c.id)).mappings()]


def _snapshot(rows):
    return [{'id': row['id'], 'record_key': row['record_key'],
             'production_group': row['production_group'], 'status': row['status'],
             'blocks': [[item['address'], item['bytes']] for item in row['blocks']]}
            for row in rows]


def _input_digest(rows):
    return hashlib.sha256(canonical(_snapshot(rows))).hexdigest()


def _latest(conn, month):
    return conn.execute(select(settlements).where(settlements.c.month == month)
                        .order_by(settlements.c.seq.desc()).limit(1)).mappings().first()


def _allocate(rows):
    """逐 (组, 块) 分摊、逐地址事实与逐组目标字节。"""
    facts = {}
    for row in rows:
        if row['status'] != BillingRecordStatus.ACTIVE:
            continue
        for item in row['blocks']:
            fact = facts.setdefault(item['address'], {'bytes': item['bytes'], 'groups': set()})
            if fact['bytes'] != item['bytes']:
                raise HTTPException(422, f'内容块 {item["address"]} 在账期内实测字节不一致')
            fact['groups'].add(row['production_group'])

    allocated = {}
    target = defaultdict(int)
    for address in sorted(facts):
        fact = facts[address]
        groups = sorted(fact['groups'])
        base, remainder = divmod(fact['bytes'], len(groups))
        fact['groups'] = groups
        fact['allocations'] = {}
        for index, group in enumerate(groups):
            share = base + (1 if index < remainder else 0)
            allocated[(group, address)] = share
            fact['allocations'][group] = share
            target[group] += share
    return allocated, facts, target


def _previous(conn, month):
    rows = conn.execute(select(allocations.c.production_group, allocations.c.address,
                               func.sum(allocations.c.bytes)).where(allocations.c.month == month)
                        .group_by(allocations.c.production_group, allocations.c.address)).all()
    previous = {(group, address): int(value) for group, address, value in rows}
    totals = {group: int(value) for group, value in conn.execute(
        select(bills.c.production_group, func.sum(bills.c.total_bytes)).where(bills.c.month == month)
        .group_by(bills.c.production_group)).all()}
    settled = [row[0] for row in conn.execute(
        select(settlements.c.id).where(settlements.c.month == month).order_by(settlements.c.seq)).all()]
    return previous, totals, settled


def _bill_rows(allocated, facts, target, previous, previous_totals):
    groups = sorted(set(target) | set(previous_totals) | {group for group, _ in previous})
    rows = []
    for group in groups:
        addresses = {address for (owner, address) in previous if owner == group}
        addresses |= {address for address, fact in facts.items() if group in fact['allocations']}
        changes = {}
        for address in sorted(addresses):
            delta = allocated.get((group, address), 0) - previous.get((group, address), 0)
            if delta:
                changes[address] = delta
        total = target.get(group, 0) - previous_totals.get(group, 0)
        if sum(changes.values()) != total:
            raise HTTPException(500, '账单增量与目标占用不一致')
        rows.append({'production_group': group, 'total': total,
                     'changes': changes, 'addresses': sorted(addresses)})
    return rows


def _settle(conn, month, trigger, actor, close, period):
    """在持有账期行锁的事务内发布结算批次；输入未变化时返回既有批次。"""
    rows = _month_records(conn, month)
    if not rows:
        raise HTTPException(422, '该月份没有验收记录')
    digest = _input_digest(rows)
    latest = _latest(conn, month)
    now = utc_time()
    if latest is not None and latest['input_digest'] == digest:
        if close and period['status'] != BillingPeriodStatus.CLOSED:
            conn.execute(periods.update().where(periods.c.month == month).values(
                status=BillingPeriodStatus.CLOSED, closed_at=now, closed_by=actor, updated_at=now))
            _log(conn, actor, 'billing_close', month)
        return latest['id']

    allocated, facts, target = _allocate(rows)
    previous, previous_totals, settled_ids = _previous(conn, month)
    kind = SettlementKind.ADJUSTMENT if latest is not None else SettlementKind.ORIGINAL
    bill_rows = [row for row in _bill_rows(allocated, facts, target, previous, previous_totals)
                 if row['total'] or row['changes']]
    if not bill_rows and latest is not None:
        # 目标占用与已出账一致：不发布空账单，也不推进序号；关账请求仍需落状态。
        if close and period['status'] != BillingPeriodStatus.CLOSED:
            conn.execute(periods.update().where(periods.c.month == month).values(
                status=BillingPeriodStatus.CLOSED, closed_at=now, closed_by=actor, updated_at=now))
            _log(conn, actor, 'billing_close', month)
        return latest['id']
    seq = 1 if latest is None else latest['seq'] + 1
    settlement_id = conn.execute(settlements.insert().values(
        month=month, seq=seq, kind=kind, trigger=trigger,
        cutoff_record_id=max(row['id'] for row in rows), input_digest=digest,
        totals={'month': month, 'records': len(rows),
                'active_records': sum(row['status'] == BillingRecordStatus.ACTIVE for row in rows),
                'blocks': len(facts), 'measured_bytes': sum(fact['bytes'] for fact in facts.values()),
                'groups': {group: target.get(group, 0) for group in sorted(target)}},
        actor=actor, created_at=now)).inserted_primary_key[0]
    _log(conn, actor, f'billing_{kind.value}', settlement_id)

    for row in bill_rows:
        group = row['production_group']
        basis = {
            'month': month,
            'production_group': group,
            'kind': kind.value,
            'settlement_seq': seq,
            'input_digest': digest,
            'previous_settlement_ids': settled_ids,
            'records': [{'id': item['id'], 'record_key': item['record_key'],
                         'production_group': item['production_group'], 'status': item['status']}
                        for item in rows],
            'addresses': [{
                'address': address,
                'measured_bytes': facts[address]['bytes'] if address in facts else None,
                'groups': facts[address]['groups'] if address in facts else [],
                'allocated_bytes': allocated.get((group, address), 0),
                'previously_billed_bytes': previous.get((group, address), 0),
                'delta_bytes': row['changes'].get(address, 0),
            } for address in row['addresses']],
        }
        bill_id = conn.execute(bills.insert().values(
            settlement_id=settlement_id, month=month, production_group=group, kind=kind,
            total_bytes=row['total'], basis=basis, created_at=now)).inserted_primary_key[0]
        if row['changes']:
            conn.execute(allocations.insert(), [
                {'bill_id': bill_id, 'month': month, 'production_group': group,
                 'address': address, 'bytes': delta} for address, delta in row['changes'].items()])

    values = {'settlement_seq': seq, 'latest_settlement_id': settlement_id, 'updated_at': now}
    if close:
        values.update(status=BillingPeriodStatus.CLOSED, closed_at=now, closed_by=actor)
        _log(conn, actor, 'billing_close', month)
    conn.execute(periods.update().where(periods.c.month == month).values(**values))
    return settlement_id


def _reconcile(month, actor, trigger, skip_locked):
    """对账直到结算与当前输入一致，捕获事务快照之后才提交的记录。"""
    status, settlement_id = 'missing', None
    for _ in range(VERIFY_PASSES):
        status, settlement_id = _reconcile_once(month, actor, trigger, skip_locked)
        if status == 'conflict':
            continue
        if status != 'settled':
            return status, settlement_id
        if not _needs_settlement(month):
            return status, settlement_id
    return status, settlement_id


def _needs_settlement(month):
    with engine.begin() as conn:
        period = conn.execute(select(periods).where(periods.c.month == month)).mappings().first()
        if period is None or period['status'] != BillingPeriodStatus.CLOSED:
            return False
        rows = _month_records(conn, month)
        if not rows:
            return False
        latest = _latest(conn, month)
        return latest is None or latest['input_digest'] != _input_digest(rows)


def _reconcile_once(month, actor, trigger, skip_locked):
    """返回 (状态, 结算编号)：missing/open/skipped/unchanged/settled。"""
    try:
        with engine.begin() as conn:
            period = _lock_period(conn, month, skip_locked=skip_locked)
            if period is None:
                if skip_locked:
                    exists = conn.execute(select(periods.c.month).where(periods.c.month == month)).first()
                    if exists is not None:
                        # 账期行存在但正被关账事务锁定。
                        return 'skipped', None
                return 'missing', None
            if period['status'] != BillingPeriodStatus.CLOSED:
                return 'open', None
            latest = _latest(conn, month)
            before = latest['id'] if latest is not None else None
            settlement_id = _settle(conn, month, trigger, actor, False, period)
            return ('unchanged' if settlement_id == before else 'settled'), settlement_id
    except IntegrityError:
        return 'conflict', None


def close_month(month, actor):
    month = _month(month)
    settlement_id = None
    try:
        with engine.begin() as conn:
            period = _ensure_period(conn, month)
            settlement_id = _settle(conn, month, 'close', actor, True, period)
    except IntegrityError:
        settlement_id = None
    # 关账快照之后才提交的迟到记录由这里兜底补差；输入未变化时是对账空操作。
    _reconcile(month, actor, 'late_record', skip_locked=False)
    with engine.begin() as conn:
        if settlement_id is None:
            latest = _latest(conn, month)
            if latest is None:
                raise HTTPException(409, '结算冲突，请重试')
            settlement_id = latest['id']
        return _settlement_detail(conn, settlement_id)


def reconcile_month(month, actor, trigger='reconcile'):
    """显式对账：账期已关账且输入变化时追加差额账单。"""
    month = _month(month)
    status, settlement_id = _reconcile(month, actor, trigger, skip_locked=False)
    return settlement_id if status in ('settled', 'unchanged') else None


def settle_month(month, actor):
    month = _month(month)
    status, settlement_id = _reconcile(month, actor, 'settle', skip_locked=False)
    if status in ('open', 'missing'):
        raise HTTPException(409, '该月份尚未关账，不能结算差额')
    with engine.begin() as conn:
        if settlement_id is None:
            latest = _latest(conn, month)
            if latest is None:
                raise HTTPException(404, '该月份还没有结算批次')
            settlement_id = latest['id']
        return _settlement_detail(conn, settlement_id)


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------

def _bill_rows_for(conn, settlement_id):
    return [dict(row) for row in conn.execute(
        select(bills).where(bills.c.settlement_id == settlement_id)
        .order_by(bills.c.production_group)).mappings()]


def _settlement_detail(conn, identity):
    row = conn.execute(select(settlements).where(settlements.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该结算批次')
    detail = dict(row)
    detail['bills'] = _bill_rows_for(conn, identity)
    return detail


def settlement_detail(identity):
    with engine.begin() as conn:
        return _settlement_detail(conn, identity)


def bill_detail(identity):
    with engine.begin() as conn:
        row = conn.execute(select(bills).where(bills.c.id == identity)).mappings().first()
        if row is None:
            raise HTTPException(404, '找不到该账单')
        detail = dict(row)
        detail['lines'] = [dict(line) for line in conn.execute(
            select(allocations).where(allocations.c.bill_id == identity)
            .order_by(allocations.c.address)).mappings()]
        return detail


def _list_records(conn, month=None):
    statement = select(records).order_by(records.c.id.desc()).limit(200)
    if month is not None:
        statement = statement.where(records.c.month == month)
    return [dict(row) for row in conn.execute(statement).mappings()]


def list_records(month=None):
    month = _month(month) if month is not None else None
    with engine.begin() as conn:
        return _list_records(conn, month)


def list_periods():
    with engine.begin() as conn:
        return [dict(row) for row in conn.execute(
            select(periods).order_by(periods.c.month.desc()).limit(200)).mappings()]


def _list_settlements(conn, month=None):
    statement = select(settlements).order_by(settlements.c.id.desc()).limit(200)
    if month is not None:
        statement = statement.where(settlements.c.month == month)
    rows = []
    for row in conn.execute(statement).mappings():
        detail = dict(row)
        detail['bills'] = _bill_rows_for(conn, row['id'])
        rows.append(detail)
    return rows


def list_settlements(month=None):
    month = _month(month) if month is not None else None
    with engine.begin() as conn:
        return _list_settlements(conn, month)


def list_bills(month=None, production_group=None):
    statement = select(bills).order_by(bills.c.id.desc()).limit(500)
    if month is not None:
        statement = statement.where(bills.c.month == _month(month))
    if production_group is not None:
        statement = statement.where(bills.c.production_group == production_group)
    with engine.begin() as conn:
        return [dict(row) for row in conn.execute(statement).mappings()]


def _summary(conn, month, rows, period):
    allocated, facts, target = _allocate(rows)
    previous, totals, settled_ids = _previous(conn, month)
    groups = sorted(set(target) | set(totals) | {group for group, _ in previous})
    return {
        'month': month,
        'period': dict(period) if period is not None else {
            'month': month, 'status': BillingPeriodStatus.OPEN, 'settlement_seq': 0},
        'measured_bytes': sum(fact['bytes'] for fact in facts.values()),
        'block_count': len(facts),
        'record_count': len(rows),
        'settlements': settled_ids,
        'groups': [{
            'production_group': group,
            'actual_bytes': target.get(group, 0),
            'billed_bytes': totals.get(group, 0),
            'matched': target.get(group, 0) == totals.get(group, 0),
        } for group in groups],
    }


def month_summary(month):
    month = _month(month)
    with engine.begin() as conn:
        rows = _month_records(conn, month)
        period = conn.execute(select(periods).where(periods.c.month == month)).mappings().first()
        return _summary(conn, month, rows, period)


def overview():
    """返回所有已登记月份的对账概览，供工作台一屏查看。"""
    with engine.begin() as conn:
        months = sorted({row[0] for row in conn.execute(select(records.c.month).distinct()).all()} |
                        {row['month'] for row in conn.execute(select(periods)).mappings()}, reverse=True)
        result = []
        for month in months:
            rows = _month_records(conn, month)
            period = conn.execute(select(periods).where(periods.c.month == month)).mappings().first()
            detail = _summary(conn, month, rows, period)
            detail['settlement_rows'] = _list_settlements(conn, month)
            detail['record_rows'] = _list_records(conn, month)
            result.append(detail)
        return result
