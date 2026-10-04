"""SQLite 存储层。

版本、迁移结果、审阅依赖、完成记录与门禁策略修订均只追加（append-only）：
不提供任何 UPDATE/DELETE 路径，人工确认以新行追加到 confirmations 表
（同一 location 至多一条，由唯一索引 idx_confirm_loc_unique 保证），
同名门禁策略的每次提交都以递增 revision 追加到 policy_revisions 表。
审阅包（review_packages）创建时冻结快照并落库，成功回传后仅补记首次
结果（applied_at / result_json 只写一次），同一 package_id 的重复回传
原样返回首次结果，不重复追加确认或完成。发布候选（release_candidates）
同理：创建即冻结不可变快照，确认时仅在六类事件水位未变且门禁仍通过时
补记一次 released 结果，同一 candidate_id 的重复确认原样返回首次结果。
app_meta 保存服务级元数据（目前为版本范围检索游标的 HMAC 签名密钥），
由版本化迁移生成一次，重启不变，不随游标外泄。

schema 的建立与升级不再使用整块建表脚本，而是走 :mod:`app.migrations`
的版本化、可审计迁移：启动时预检、在单个写事务内按编号执行未完成迁移
并向 schema_migrations 只追加审计行（编号 / 状态 / 错误），任一步失败
回滚该步并拒绝启动，迁移后再做结构校验。
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

from . import migrations

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "app.db"))


def connect() -> sqlite3.Connection:
    # isolation_level=None：自动提交；多语句写入用 transaction() 显式包裹
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    # 旧部署库可能仍是 rollback-journal 模式。journal_mode 切换是一次
    # 元数据写入，不服从 busy_timeout：多实例并发首启时会立即抛
    # "database is locked"，这里做短暂忙等重试。
    deadline = time.monotonic() + 30.0
    while True:
        try:
            con.execute("PRAGMA journal_mode = WAL")
            break
        except sqlite3.OperationalError as exc:
            transient = ("locked" in str(exc).lower()
                         or "busy" in str(exc).lower())
            if not transient or time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
    # 切换后立即做一次 checkpoint，保证旧 journal 中已提交的全部只追加数据
    # 在 WAL 下可见（否则预检可能读不到历史行，迁移审计也可能基于不完整快照）。
    con.execute("PRAGMA wal_checkpoint(FULL)")
    return con


@contextmanager
def transaction(con: sqlite3.Connection):
    """BEGIN IMMEDIATE 事务：写操作串行化，保证版本号等自增逻辑无竞态。"""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


@contextmanager
def read_transaction(con: sqlite3.Connection):
    """BEGIN 只读事务：多次 SELECT 落在同一快照（WAL 下快照隔离）。

    供迁移归档导出使用：文档元数据、版本、批注、迁移记录与人工确认
    必须在同一个读事务中取出，避免导出过程中并发写入造成行间不一致。
    """
    con.execute("BEGIN")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def init_db() -> migrations.BootstrapResult:
    """执行版本化迁移（预检 → 单写事务顺序迁移 → 校验）。

    任一步失败或校验不通过都会抛出 :class:`migrations.MigrationError`，
    调用方（应用 lifespan）必须中止启动，不得接受任何业务请求。

    多实例并发启动 / 容器重启时，审计表幂等 DDL 与迁移写事务需要写锁；
    对瞬时 ``database is locked`` / ``database is busy`` 做整体幂等重试
    （重跑前重新预检并重新读取版本，已 applied 的迁移不会重复执行），
    最长等待约 30 秒；真正的迁移 / 校验错误立即抛出，不重试。
    """
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    deadline = time.monotonic() + 30.0
    while True:
        con = connect()
        try:
            return migrations.run_migrations(con)
        except sqlite3.OperationalError as exc:
            transient = ("locked" in str(exc).lower()
                         or "busy" in str(exc).lower())
            if not transient or time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
        finally:
            con.close()
