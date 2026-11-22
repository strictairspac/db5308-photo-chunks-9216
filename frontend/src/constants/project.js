export const topic = {
  "name": "光坞影像·底片块库验收",
  "direction": "增量备份、块校验与还原",
  "modules": [
    "底片快照",
    "内容块目录",
    "还原查验",
    "留存清单"
  ],
  "algorithm": "以 SHA-256 内容寻址去重，从选中快照复原并列出损坏或缺失块"
};
