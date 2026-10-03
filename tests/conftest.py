import os
import tempfile

# 必须在导入 app 之前设置：db 模块在导入时读取 DB_PATH
_fd, _db_path = tempfile.mkstemp(prefix="annotation-test-", suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _db_path
