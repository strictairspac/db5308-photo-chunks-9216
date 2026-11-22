import pytest

from src.config.settings import engine
from src.models.tables import evidence, schema
from src.services import billing, domain

MONTH = '2026-11'


class Form:
    def __init__(self, **values):
        self.month = MONTH
        self.production_group = 'A'
        self.record_key = 'r1'
        self.blocks = []
        self.run_id = None
        self.note = None
        self.__dict__.update(values)


@pytest.fixture(autouse=True)
def clean():
    schema.drop_all(engine)
    schema.create_all(engine)
    yield


def block(char, size):
    return {'address': char * 64, 'bytes': size}


def add(group, key, blocks, month=MONTH):
    return billing.add_record(Form(month=month, production_group=group, record_key=key, blocks=blocks), 'operator')


def close(month=MONTH):
    return billing.close_month(month, 'operator')


def bills_of(month=MONTH):
    return billing.list_bills(month)


def test_split_and_dedupe_within_group():
    add('A', 'r1', [block('a', 5), block('b', 4)])
    add('A', 'r2', [block('a', 5)])          # 同组重复引用，不重复计费
    add('B', 'r3', [block('a', 5), block('b', 4)])
    assert bills_of() == []                  # 未关账不发布账单

    settlement = close()
    bills = {row['production_group']: row for row in settlement['bills']}
    # 5 字节在两个组间平分，余数按组名排序分给 A；4 字节各 2。
    assert bills['A']['total_bytes'] == 5
    assert bills['B']['total_bytes'] == 4
    assert sum(row['total_bytes'] for row in bills.values()) == 9
    assert billing.month_summary(MONTH)['groups'] == [
        {'production_group': 'A', 'actual_bytes': 5, 'billed_bytes': 5, 'matched': True},
        {'production_group': 'B', 'actual_bytes': 4, 'billed_bytes': 4, 'matched': True},
    ]


def test_late_record_creates_adjustment_and_original_is_immutable():
    first = add('A', 'r1', [block('a', 10), block('b', 6)])
    add('B', 'r2', [block('b', 6)])
    original = close()
    original_bill = {row['production_group']: row for row in original['bills']}['A']
    snapshot = billing.bill_detail(original_bill['id'])
    assert snapshot['total_bytes'] == 13     # a:10 全部归 A；b:6 平分 3/3

    late = add('B', 'r3', [block('a', 10)])
    adjustment = billing.settlement_detail(billing.list_settlements(MONTH)[0]['id'])
    assert adjustment['kind'] == 'adjustment'
    # 迟到记录由入账路径自动追加差额账单。
    settlements = billing.list_settlements(MONTH)
    assert len(settlements) == 2
    latest = billing.settlement_detail(settlements[0]['id'])
    assert latest['kind'] == 'adjustment'
    delta = {row['production_group']: row['total_bytes'] for row in latest['bills']}
    assert delta == {'A': -5, 'B': 5}

    # 原账单与其依据保持不变，仍可回查。
    assert billing.bill_detail(original_bill['id']) == snapshot
    assert billing.bill_detail(original_bill['id'])['basis']['records'] == snapshot['basis']['records']

    summary = billing.month_summary(MONTH)
    assert all(row['matched'] for row in summary['groups'])
    assert summary['groups'] == [
        {'production_group': 'A', 'actual_bytes': 8, 'billed_bytes': 8, 'matched': True},
        {'production_group': 'B', 'actual_bytes': 8, 'billed_bytes': 8, 'matched': True},
    ]


def test_reversal_creates_negative_adjustment():
    add('A', 'r1', [block('a', 8)])
    add('B', 'r2', [block('a', 8)])
    close()
    target = add('A', 'r3', [block('b', 4)])
    billing.reverse_record(target['id'], '录错了', 'operator')

    settlements = billing.list_settlements(MONTH)
    kinds = [row['kind'] for row in settlements]
    assert kinds == ['adjustment', 'adjustment', 'original']  # 倒序
    totals = {}
    for bill in bills_of():
        totals[bill['production_group']] = totals.get(bill['production_group'], 0) + bill['total_bytes']
    assert totals == {'A': 4, 'B': 4}
    summary = {row['production_group']: row for row in billing.month_summary(MONTH)['groups']}
    assert summary['A']['actual_bytes'] == summary['A']['billed_bytes'] == 4
    assert summary['B']['actual_bytes'] == summary['B']['billed_bytes'] == 4


def test_retry_is_idempotent():
    first = add('A', 'r1', [block('a', 4)])
    again = add('A', 'r1', [block('a', 4)])
    assert first['id'] == again['id']
    assert len(billing.list_records()) == 1

    with pytest.raises(Exception):
        add('B', 'r1', [block('a', 4)])       # 同键不同内容须拒绝

    settled = close()
    closed_again = close()
    assert settled['id'] == closed_again['id']
    assert len(billing.list_settlements(MONTH)) == 1
    assert billing.settle_month(MONTH, 'operator')['id'] == settled['id']
    assert len(billing.list_settlements(MONTH)) == 1


def test_record_before_close_does_not_publish_bill():
    add('A', 'r1', [block('a', 4)])
    summary = billing.month_summary(MONTH)
    assert summary['period']['status'] == 'open'
    assert summary['settlements'] == []
    assert summary['groups'] == [
        {'production_group': 'A', 'actual_bytes': 4, 'billed_bytes': 0, 'matched': False}]
    assert summary['measured_bytes'] == 4


def test_delta_can_go_negative_and_back():
    add('A', 'r1', [block('a', 9)])
    add('B', 'r2', [block('a', 9)])
    close()
    add('C', 'r3', [block('a', 9)])
    latest = billing.settlement_detail(billing.list_settlements(MONTH)[0]['id'])
    third = {row['production_group']: row['total_bytes'] for row in latest['bills']}
    # 9 / 2：A 得 5（余数给排序在前的组），B 得 4；9 / 3 后各 3，C 得 3。
    assert third == {'A': -2, 'B': -1, 'C': 3}
    summary = {row['production_group']: row for row in billing.month_summary(MONTH)['groups']}
    assert summary['A']['actual_bytes'] == 3 and summary['A']['billed_bytes'] == 3
    assert summary['C']['actual_bytes'] == 3 and summary['C']['billed_bytes'] == 3


def test_records_can_be_derived_from_evidence():
    result = domain.solve(domain.SPEC['sample'], engine)
    with engine.begin() as conn:
        conn.execute(evidence.insert().values(run_id=77, report=result, digest='d' * 64))
    row = billing.add_record(Form(month=MONTH, production_group='A', record_key='ev-77',
                                  blocks=None, run_id=77), 'operator')
    assert len(row['blocks']) == result['metrics']['唯一块数']
    assert sum(item['bytes'] for item in row['blocks']) == result['metrics']['入库字节数']
    billed = billing.close_month(MONTH, 'operator')
    assert sum(bill['total_bytes'] for bill in billed['bills']) == result['metrics']['入库字节数']


def test_concurrent_close_and_records_never_lose_or_duplicate():
    """关账与收单并发：全部记录最终入账，每个组的原始+差额等于修正后实占字节。"""
    import threading

    groups = ['A', 'B', 'C']
    blocks = [round_block(index) for index in range(5)]
    errors = []
    # 先落一条记录，空月份关账本就应拒绝（422）。
    add('A', 'seed', [blocks[0]])

    def writer(slot):
        try:
            for index in range(4):
                group = groups[(slot + index) % len(groups)]
                add(group, f'w{slot}-{index}', [blocks[(slot + index) % len(blocks)]])
        except Exception as exc:  # noqa: BLE001 - 测试需汇总线程错误
            errors.append(exc)

    def closer():
        try:
            for _ in range(6):
                close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(slot,)) for slot in range(3)]
    threads.append(threading.Thread(target=closer))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors

    # 兜底对账，模拟关账提交后复核或人工补差。
    billing.reconcile_month(MONTH, 'operator')

    summary = billing.month_summary(MONTH)
    assert summary['record_count'] == 13
    assert all(row['matched'] for row in summary['groups'])
    # 原始账单与差额账单逐块相加等于目标占用。
    totals = {}
    for bill in billing.list_bills(MONTH):
        totals[bill['production_group']] = totals.get(bill['production_group'], 0) + bill['total_bytes']
    for row in summary['groups']:
        assert totals.get(row['production_group'], 0) == row['actual_bytes'] == row['billed_bytes']
    assert sum(totals.values()) == summary['measured_bytes']

    # 结算批次序号无跳号、无重复。
    seqs = sorted(row['seq'] for row in billing.list_settlements(MONTH))
    assert seqs == list(range(1, len(seqs) + 1))


def round_block(index):
    alphabet = 'abcdef0123456789'
    return {'address': (alphabet[index % 16] * 64), 'bytes': 3 + index}


def test_demo_snapshots_split_between_groups():
    """对照 BUSINESS.md 的 23 字节演示：共享块平分，独有块整块计入。"""
    payload = domain.SPEC['sample']
    store = {}
    manifests = {}
    from src.services.chunk_store import ingest
    for row in payload['snapshots']:
        content = row['content'].encode('utf-8')
        manifests[row['snapshot_id']] = ingest(row['snapshot_id'], row['asset'], content,
                                               payload['chunk_size'], store)
    assert sum(map(len, store.values())) == 23

    def blocks_of(snapshot_id):
        seen = {}
        for address in manifests[snapshot_id].addresses:
            seen[address] = len(store[address])
        return [{'address': address, 'bytes': size} for address, size in seen.items()]

    add('扫描组', 'L17-1119', blocks_of('L17-1119'))
    add('制片组', 'L17-1120', blocks_of('L17-1120'))
    add('制片组', 'L17-1121', blocks_of('L17-1121'))
    settlement = close()
    totals = {bill['production_group']: bill['total_bytes'] for bill in settlement['bills']}
    # 共享的 LITE/GRAY/SCAN 各 4 字节，两组各 2；OLD! 4 归扫描组；EDIT 4、END 3 归制片组。
    assert totals == {'扫描组': 10, '制片组': 13}
    assert sum(totals.values()) == 23


def test_conflicting_measurements_are_rejected_atomically():
    add('A', 'r1', [block('a', 4)])
    with pytest.raises(Exception) as failure:
        add('B', 'r2', [block('a', 5)])
    assert '实测字节不一致' in str(failure.value)
    # 冲突记录未落库，账期保持一致。
    assert len(billing.list_records(MONTH)) == 1
    billing.close_month(MONTH, 'operator')
    assert all(row['matched'] for row in billing.month_summary(MONTH)['groups'])



