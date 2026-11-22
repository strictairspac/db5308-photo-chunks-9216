import os
import tempfile

# 必须在导入 src.* 之前设置：测试使用独立 SQLite 文件，不依赖 MySQL/Redis。
os.environ.setdefault('DB_DRIVER', 'sqlite')
os.environ.setdefault('DB_PATH', os.path.join(tempfile.mkdtemp(), 'billing-test.db'))
os.environ.setdefault('AUTH_KEY', 'test-key-for-billing')
os.environ.setdefault('SEED_DEMO', 'false')
