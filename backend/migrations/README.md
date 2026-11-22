# 迁移

`src/models/tables.py` 定义当前关系结构。API 启动时显式创建演示库的表；需要升级既有部署时应执行本目录中的版本化脚本。

| 版本 | 内容 |
| --- | --- |
| 0001 | 初始结构，由 `tables.py` 建表 |
| [0002_billing.sql](0002_billing.sql) | 占用账单：验收记录、账期、结算批次、账单、逐块分摊 |

## 占用账单项表

- `billing_records`：一次验收的不可变输入。`record_key` 唯一，用于重试幂等；`blocks` 保存有序的 `address`/`bytes`。撤销只置 `status=reversed` 与撤销备注，不修改原字段。
- `billing_periods`：每月一行。`status` 由 `open` 变为 `closed` 后不再回转，`settlement_seq` 记录已发布的批次数。
- `billing_settlements`：一个结算批次，`(month, seq)` 唯一。`kind=original` 为关账原始账单，`kind=adjustment` 为迟到记录或撤销触发的差额账单；`input_digest` 是当次记录快照的摘要。
- `billing_bills`：按制作组一张账单，`(settlement_id, production_group)` 唯一。`basis` 固定保存当时引用的记录、逐块实测字节、分摊结果与往期已出账，发布后不再改写。
- `billing_allocations`：账单逐块增量，便于按块核对。某月全部账单的同组逐块增量之和等于该组实占字节。
