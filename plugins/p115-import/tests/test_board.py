"""看板归类逻辑的自测：只喂宿主任务字段样例，不碰真实数据。"""
import unittest

import p115board as b


def job(job_type="p115-import:request", status="running", ref="r1", subject="115 链接入库",
        title=None, progress=None, result=None, error=None, dismissed=None, updated=None):
    resolved = dict(result) if isinstance(result, dict) else {"request_ref": ref}
    if title:
        resolved["title"] = title
    return {
        "id": "job_" + (ref or "x") + "_" + job_type.split(":")[-1],
        "job_type": job_type,
        "subject": subject,
        "status": status,
        "input_data": ({"request_ref": ref} if job_type == "p115-import:request"
                       else {"plan_ref": "transfer:" + ref}),
        "progress": progress or {},
        "result": resolved,
        "error": error,
        "dismissed_at": dismissed,
        "attempt": 1,
        "max_attempts": 3,
        "created_at": "2026-10-20T06:00:00+00:00",
        "updated_at": updated or "2026-10-20T06:00:05+00:00",
        "finished_at": None,
    }


class Board(unittest.TestCase):
    def test_ref_extracted_from_both_types(self):
        self.assertEqual(b.ref_of(job()), "r1")
        self.assertEqual(b.ref_of(job("p115-import:transfer")), "r1")

    def test_active_request_with_child_counts_live_only(self):
        parent = job(status="succeeded", result={"request_ref": "r1", "status": "dispatched",
                                                 "title": "剧", "transfer_job_id": "job_child"},
                     progress={"phase": "completed", "message": "已交接"})
        parent["input_data"] = {"request_ref": "r1"}
        child = job("p115-import:transfer", status="running", ref="r1", title="剧",
                    progress={"phase": "cloud", "message": "等待网盘放行", "percent": 40})
        feed = b.build_feed([parent, child])
        self.assertEqual([r["job_id"] for r in feed["inbox"]], [])
        self.assertEqual([r["job_id"] for r in feed["live"]], [child["id"]])
        self.assertEqual(feed["live"][0]["phase_label"], "等待网盘放行文件")

    def test_candidates_only_read_from_error_details(self):
        parent = job(status="blocked", ref="r2",
                     error={"message": "需要确认作品", "details": {"candidates": [
                         {"tmdb_id": 290193, "kind": "tv", "title": "鼠惑", "year": 2026},
                         {"title": "没有编号"},  # 缺 tmdb_id：丢弃
                     ]}})
        feed = b.build_feed([parent])
        self.assertEqual(feed["inbox"], [])
        row = feed["attention"][0]
        self.assertEqual([c["tmdb_id"] for c in row["candidates"]], [290193])

    def test_dismissed_failed_not_in_attention(self):
        row = job(status="failed", ref="r3", error={"message": "上游失败"}, dismissed="2026-10-20T07:00:00+00:00")
        feed = b.build_feed([row])
        self.assertEqual(feed["attention"], [])

    def test_stale_failure_with_newer_active_run_hidden(self):
        old = job(status="failed", ref="r4", error={"message": "旧失败"})
        new = job(status="running", ref="r4")
        feed = b.build_feed([old, new])
        self.assertEqual(feed["attention"], [])
        self.assertEqual([r["job_id"] for r in feed["inbox"]], [new["id"]])

    def test_recent_merges_parent_and_child_by_ref(self):
        parent = job(status="succeeded", ref="r5", title="剧",
                     progress={"phase": "completed", "message": "已交接"},
                     result={"request_ref": "r5", "status": "dispatched", "title": "剧"})
        child = job("p115-import:transfer", status="succeeded", ref="r5", title="剧",
                    result={"request_ref": "r5", "title": "剧", "inventory_verified": True,
                            "library_name": "日韩剧", "library_id": 12, "media_item_id": 1594})
        child["finished_at"] = "2026-10-20T06:10:00+00:00"
        feed = b.build_feed([parent, child])
        refs = [r["ref"] for r in feed["recent"]]
        self.assertEqual(refs.count("r5"), 1)
        self.assertTrue(feed["recent"][0]["inventory_verified"])

    def test_cleanup_task_never_claims_inventory(self):
        row = job("p115-import:cleanup-staging", status="succeeded", ref="r6")
        row["input_data"] = {"library_id": 12}
        feed = b.build_feed([row])
        self.assertEqual(feed["recent"][0]["job_type"], "p115-import:cleanup-staging")
        self.assertFalse(feed["recent"][0]["inventory_verified"])

    def test_cancelled_not_nagged(self):
        row = job(status="cancelled", ref="r8", error={"message": "用户取消"})
        feed = b.build_feed([row])
        self.assertEqual(feed["attention"], [])

    def test_counts_stable(self):
        feed = b.build_feed([job(status="running"), job(status="failed", ref="r7")])
        self.assertEqual(feed["counts"]["inbox"], 1)
        self.assertEqual(feed["counts"]["attention"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
