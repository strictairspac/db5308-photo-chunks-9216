from base64 import b64decode, b64encode
from collections import Counter, defaultdict
from hashlib import sha256

from .chunk_store import ingest, inject_faults, restore


MAX_BYTES = 256 * 1024
MAX_BLOCK_REFERENCES = 2048
MAX_SNAPSHOTS = 100

SPEC = {
    'name': '光坞影像·底片块库验收',
    'summary': '核算跨快照去重，验证指定底片的还原结果，并按仍须保留的快照提出释放建议。',
    'customer': '光坞影像·数字底片归档组',
    'entity_labels': ['底片快照', '还原查验', '留存清单'],
    'field_labels': {
        'chunk_size': '固定块大小（字节）',
        'restore_snapshot': '还原快照编号',
        'snapshots': '底片快照导出记录',
        'retained_snapshots': '须保留的快照',
        'faults': '故障注入',
        'snapshot_id': '快照编号',
        'asset': '底片文件名',
        'encoding': '内容编码',
        'content': '内容',
        'chunk_index': '块序号（从 1 开始）',
        'fault': '故障类型',
    },
    'guidance': '内容编码用 utf8 或 base64。按快照和块序号定位故障，missing 模拟缺失，corrupt 模拟损坏，共享同一地址的块会一起受影响。',
    'sample': {
        'chunk_size': 4,
        'restore_snapshot': 'L17-1121',
        'snapshots': [
            {'snapshot_id': 'L17-1119', 'asset': 'L17-frame07.dpx', 'encoding': 'utf8', 'content': 'LITEGRAYSCANOLD!'},
            {'snapshot_id': 'L17-1120', 'asset': 'L17-frame07.dpx', 'encoding': 'utf8', 'content': 'LITEGRAYSCANEDIT'},
            {'snapshot_id': 'L17-1121', 'asset': 'L17-frame07.dpx', 'encoding': 'utf8', 'content': 'LITESCANEDITSCANEND'},
        ],
        'retained_snapshots': [{'snapshot_id': 'L17-1120'}, {'snapshot_id': 'L17-1121'}],
        'faults': [
            {'snapshot_id': 'L17-1120', 'chunk_index': 3, 'fault': 'missing'},
            {'snapshot_id': 'L17-1121', 'chunk_index': 3, 'fault': 'corrupt'},
        ],
    },
    'expect': {
        '逻辑字节数': 51,
        '唯一块数': 6,
        '入库字节数': 23,
        '去重节省字节数': 28,
        '基线校验通过': 1,
        '缺失块位置数': 2,
        '损坏块位置数': 1,
        '异常保留块数': 2,
        '孤立候选字节数': 4,
        '建议释放字节数': 0,
    },
}


def _rows(payload, key, minimum, maximum):
    rows = payload.get(key)
    label = SPEC['field_labels'][key]
    if not isinstance(rows, list) or not minimum <= len(rows) <= maximum:
        raise ValueError(f'「{label}」须提供 {minimum} 至 {maximum} 条记录')
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f'「{label}」须由对象记录组成')
    return rows


def _name(value, label, limit=120):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or value != value.strip():
        raise ValueError(f'{label}须为 1 至 {limit} 个字符，且首尾不能有空白')
    return value


def _decode(row, snapshot_id):
    content = row.get('content')
    if not isinstance(content, str):
        raise ValueError(f'快照 {snapshot_id} 的内容须为字符串')
    if len(content) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError(f'底片内容合计最多 {MAX_BYTES} 字节')
    encoding = row.get('encoding')
    try:
        if encoding == 'utf8':
            return content.encode('utf-8')
        if encoding == 'base64':
            return b64decode(content, validate=True)
    except ValueError:
        raise ValueError(f'快照 {snapshot_id} 的 {encoding} 内容无效') from None
    raise ValueError(f'快照 {snapshot_id} 的内容编码须为 utf8 或 base64')


def _prepare(payload):
    if not isinstance(payload, dict):
        raise ValueError('业务输入须为对象')
    chunk_size = payload.get('chunk_size')
    if type(chunk_size) is not int or not 1 <= chunk_size <= MAX_BYTES:
        raise ValueError(f'固定块大小须为 1 至 {MAX_BYTES} 之间的整数')

    store, manifests = {}, {}
    size = references = 0
    for row in _rows(payload, 'snapshots', 1, MAX_SNAPSHOTS):
        snapshot_id = _name(row.get('snapshot_id'), '快照编号', 64)
        if snapshot_id in manifests:
            raise ValueError(f'快照编号重复：{snapshot_id}')
        asset = _name(row.get('asset'), '底片文件名')
        content = _decode(row, snapshot_id)
        size += len(content)
        references += (len(content) + chunk_size - 1) // chunk_size
        if size > MAX_BYTES or references > MAX_BLOCK_REFERENCES:
            raise ValueError(f'底片内容合计最多 {MAX_BYTES} 字节，清单中的块引用最多 {MAX_BLOCK_REFERENCES} 次')
        manifests[snapshot_id] = ingest(snapshot_id, asset, content, chunk_size, store)

    selected = _name(payload.get('restore_snapshot'), '还原快照编号', 64)
    if selected not in manifests:
        raise ValueError(f'还原快照不存在：{selected}')

    retained = set()
    for row in _rows(payload, 'retained_snapshots', 1, MAX_SNAPSHOTS):
        snapshot_id = _name(row.get('snapshot_id'), '保留快照编号', 64)
        if snapshot_id not in manifests or snapshot_id in retained:
            raise ValueError(f'保留快照不存在或重复：{snapshot_id}')
        retained.add(snapshot_id)

    faults = {}
    for row in _rows(payload, 'faults', 0, MAX_BLOCK_REFERENCES):
        snapshot_id = _name(row.get('snapshot_id'), '故障快照编号', 64)
        manifest = manifests.get(snapshot_id)
        if manifest is None:
            raise ValueError(f'故障快照不存在：{snapshot_id}')
        position = row.get('chunk_index')
        if type(position) is not int or not 1 <= position <= len(manifest.addresses):
            raise ValueError(f'快照 {snapshot_id} 的故障块序号超出清单范围')
        fault = row.get('fault')
        if fault not in ('missing', 'corrupt'):
            raise ValueError('故障类型须为 missing 或 corrupt')
        address = manifest.addresses[position - 1]
        if address in faults:
            raise ValueError(f'同一内容块只能注入一次故障：{address}')
        faults[address] = fault
    return store, manifests, selected, retained, faults


def validate(payload):
    _prepare(payload)


def _restore_row(stage, manifest, restored):
    if restored.issues:
        verdict = '块查验失败'
    elif restored.content is None:
        verdict = '整份文件校验失败'
    else:
        verdict = '校验通过'
    return {
        '阶段': stage,
        '快照': manifest.snapshot_id,
        '预期字节数': manifest.size,
        '实测字节数': restored.size,
        '预期 SHA-256': manifest.digest,
        '实测 SHA-256': restored.digest,
        '判定': verdict,
        '还原内容（Base64）': b64encode(restored.content).decode('ascii') if restored.content is not None else None,
    }


def _retention_plan(store, observed, manifests, retained):
    references = Counter()
    retained_by_address = defaultdict(list)
    for manifest in manifests.values():
        references.update(manifest.addresses)
        if manifest.snapshot_id in retained:
            for address in set(manifest.addresses):
                retained_by_address[address].append(manifest.snapshot_id)

    states = {}
    for address in store:
        block = observed.get(address)
        if block is None:
            states[address] = '缺失'
        else:
            states[address] = '正常' if sha256(block).hexdigest() == address else '损坏'
    broken_retained = sum(states[address] != '正常' for address in retained_by_address)

    candidate_bytes = release_bytes = 0
    rows = []
    for address, block in store.items():
        retained_ids = retained_by_address.get(address, [])
        available = observed.get(address, b'')
        if retained_ids:
            decision = '保留' if states[address] == '正常' else '保留并修复'
        else:
            candidate_bytes += len(block)
            if address not in observed:
                decision = '孤立，当前无可释放字节'
            elif broken_retained:
                decision = '孤立，暂缓释放'
            else:
                decision = '孤立，可提请释放'
                release_bytes += len(available)
        rows.append({
            '内容块 SHA-256': address,
            '入库字节数': len(block),
            '当前可读取字节数': len(available),
            '全部引用次数': references[address],
            '保留快照': '、'.join(retained_ids),
            '块状态': states[address],
            '留存建议': decision,
        })
    return rows, broken_retained, candidate_bytes, release_bytes


def solve(payload, engine):
    store, manifests, selected, retained, faults = _prepare(payload)
    manifest = manifests[selected]
    baseline = restore(manifest, store)
    observed = inject_faults(store, faults) if faults else store
    restored = restore(manifest, observed) if faults else baseline
    if faults:
        restore_rows = [_restore_row('注入前基线', manifest, baseline), _restore_row('注入后还原', manifest, restored)]
    else:
        restore_rows = [_restore_row('无故障还原', manifest, restored)]

    retention_rows, broken_retained, candidate_bytes, release_bytes = _retention_plan(store, observed, manifests, retained)
    logical_bytes = sum(item.size for item in manifests.values())
    stored_bytes = sum(map(len, store.values()))
    failures = Counter(row['状态'] for row in restored.issues)
    retention_verdict = (
        f'{broken_retained} 个保留块待修复，暂缓释放。' if broken_retained
        else f'建议复核后释放 {release_bytes} 字节。'
    )
    manifest_rows = [{
        '快照': item.snapshot_id,
        '底片文件': item.asset,
        '文件字节数': item.size,
        '文件 SHA-256': item.digest,
        '块引用次数': len(item.addresses),
        '保留策略': '保留' if item.snapshot_id in retained else '到期',
    } for item in manifests.values()]
    return {
        'summary': f'快照 {selected} {restore_rows[-1]["判定"]}。{retention_verdict}',
        'metrics': {
            '逻辑字节数': logical_bytes,
            '唯一块数': len(store),
            '入库字节数': stored_bytes,
            '去重节省字节数': logical_bytes - stored_bytes,
            '基线校验通过': int(baseline.content is not None),
            '还原校验通过': int(restored.content is not None),
            '缺失块位置数': failures['缺失'],
            '损坏块位置数': failures['损坏'],
            '异常保留块数': broken_retained,
            '孤立候选字节数': candidate_bytes,
            '建议释放字节数': release_bytes,
        },
        'sections': [
            {'title': '底片快照清单', 'rows': manifest_rows},
            {'title': '还原查验', 'rows': restore_rows},
            {'title': '缺失与损坏位置', 'rows': restored.issues},
            {'title': '内容块目录与留存建议', 'rows': retention_rows},
        ],
    }
