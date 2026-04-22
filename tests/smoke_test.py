"""端到端烟雾测试：验证 P0 邮件通路 + 去重。

步骤：
1. 清掉历史测试事件
2. 手动插入一条假的 P0 change_event
3. 跑 send_p0_alerts() → 应该收到邮件
4. 再跑一次 → 不应该重发
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.db import get_conn
from backend.engine.alert_manager import send_p0_alerts

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TEST_DEDUPE_KEY = "smoke_test:2026-04-21"


def insert_fake_p0():
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM change_events WHERE dedupe_key=?",
            (TEST_DEDUPE_KEY,),
        )
        conn.execute(
            """
            INSERT INTO change_events
              (event_type, severity, source, title, detail_json, model_name, dedupe_key)
            VALUES (?, 'P0', 'test', ?, ?, 'SmokeTestModel', ?)
            """,
            (
                "rank_crowned",
                "[烟雾测试] SmokeTestModel 登顶 测试榜单 Top 1",
                '{"category": "test_category", "old_rank": 3, "new_rank": 1}',
                TEST_DEDUPE_KEY,
            ),
        )
    print("✓ 已插入一条测试 P0 事件")


def main():
    print("\n=== Step 1: 插入测试 P0 事件 ===")
    insert_fake_p0()

    print("\n=== Step 2: 第一次发送 (应该发出邮件) ===")
    r1 = send_p0_alerts()
    print(f"结果: {r1}")
    assert r1["fetched"] >= 1, "没有抓到未发送的 P0 事件"
    assert r1["sent"] >= 1 and r1["marked"] >= 1, "邮件发送或标记失败"

    print("\n=== Step 3: 第二次发送 (去重验证，应该 0 封) ===")
    r2 = send_p0_alerts()
    print(f"结果: {r2}")
    assert r2["fetched"] == 0, f"去重失败！第二次还抓到 {r2['fetched']} 条"

    print("\n✅ 烟雾测试全部通过")
    print("请检查你的收件邮箱 (EMAIL_RECEIVERS) 是否收到一封邮件")


if __name__ == "__main__":
    main()
