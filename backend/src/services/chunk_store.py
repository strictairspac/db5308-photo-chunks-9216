from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class Manifest:
    snapshot_id: str
    asset: str
    size: int
    digest: str
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class Restored:
    size: int | None
    digest: str | None
    content: bytes | None
    issues: list[dict]


def ingest(snapshot_id, asset, content, chunk_size, store):
    addresses = []
    for start in range(0, len(content), chunk_size):
        block = content[start:start + chunk_size]
        address = sha256(block).hexdigest()
        if store.setdefault(address, block) != block:
            raise ValueError('内容块 SHA-256 地址冲突')
        addresses.append(address)
    return Manifest(snapshot_id, asset, len(content), sha256(content).hexdigest(), tuple(addresses))


def inject_faults(store, faults):
    observed = store.copy()
    for address, fault in faults.items():
        if fault == 'missing':
            del observed[address]
        else:
            block = observed[address]
            observed[address] = bytes([block[0] ^ 1]) + block[1:]
    return observed


def restore(manifest, store):
    blocks = []
    issues = []
    for position, address in enumerate(manifest.addresses, start=1):
        block = store.get(address)
        observed_digest = sha256(block).hexdigest() if block is not None else None
        if observed_digest == address:
            blocks.append(block)
        else:
            issues.append({
                '块序号': position,
                '状态': '缺失' if block is None else '损坏',
                '预期 SHA-256': address,
                '实测 SHA-256': observed_digest,
            })
    if issues:
        return Restored(None, None, None, issues)

    content = b''.join(blocks)
    digest = sha256(content).hexdigest()
    size = len(content)
    verified = digest == manifest.digest and size == manifest.size
    return Restored(size, digest, content if verified else None, [])
