"""测试快手评论互动抓取与 LLM 安全回复闭环（P2 方案）。"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot.publish.kuaishou_reply import (
    CommentItem,
    append_reply_log,
    fetch_kuaishou_comments,
    generate_comment_reply,
    reply_single_comment_on_page,
    run_comment_reply_batch,
)


def test_fetch_kuaishou_comments_sniffer():
    mock_page = MagicMock()
    mock_page.url = "https://cp.kuaishou.com/article/comment"
    callbacks = []

    def mock_on(event, cb):
        if event == "response":
            callbacks.append(cb)

    mock_page.on.side_effect = mock_on

    mock_resp = MagicMock()
    mock_resp.url = "https://cp.kuaishou.com/rest/cp/creator/comment/commentList"
    mock_resp.status = 200
    mock_resp.json.return_value = {
        "result": 1,
        "data": {
            "list": [
                {
                    "commentId": "c_101",
                    "photoId": "p_202",
                    "authorName": "粉丝小明",
                    "authorId": "u_303",
                    "content": "这个视频太治愈了，看了好几遍！",
                    "timestamp": 1726625160000,
                    "likedCount": 58,
                    "subCommentCount": 0,
                    "toped": False,
                },
                {
                    "commentId": "c_102",
                    "photoId": "p_202",
                    "authorName": "粉丝小红",
                    "authorId": "u_304",
                    "content": "背景音乐叫什么名字呀？",
                    "timestamp": 1726625170000,
                    "likedCount": 12,
                    "subCommentCount": 1,
                    "toped": False,
                },
            ]
        },
    }

    def trigger_scroll(*args, **kwargs):
        for cb in callbacks:
            cb(mock_resp)

    mock_page.wait_for_timeout.side_effect = trigger_scroll

    comments = fetch_kuaishou_comments(mock_page, timeout_sec=2.0)
    assert len(comments) == 2
    assert comments[0].comment_id == "c_101"
    assert comments[0].author_name == "粉丝小明"
    assert comments[0].liked_count == 58
    assert comments[0].sub_comment_count == 0


def test_generate_comment_reply_fallback_when_no_api_key():
    with patch("bot.publish.kuaishou_reply.load_settings") as mock_load_settings:
        mock_settings = MagicMock()
        mock_settings.ai_api_key = ""
        mock_load_settings.return_value = mock_settings

        reply = generate_comment_reply("太好看了！")
        assert len(reply) > 0
        assert isinstance(reply, str)


def test_generate_comment_reply_with_mock_llm():
    with patch("bot.publish.kuaishou_reply.load_settings") as mock_load_settings:
        mock_settings = MagicMock()
        mock_settings.ai_api_key = "sk-test"
        mock_settings.ai_base_url = "https://api.openai.com/v1"
        mock_settings.ai_model = "gpt-4o-mini"
        mock_load_settings.return_value = mock_settings

        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_choice = MagicMock()
            mock_choice.message.content = "“哈哈谢谢老铁支持！”"
            mock_resp = MagicMock()
            mock_resp.choices = [mock_choice]
            mock_client.chat.completions.create.return_value = mock_resp

            reply = generate_comment_reply("好厉害啊！", "粉丝小李")
            assert reply == "哈哈谢谢老铁支持！"


def test_reply_single_comment_on_page_dry_run():
    mock_page = MagicMock()
    mock_page.evaluate.return_value = {"ok": True, "reason": "dry_run: 目标评论行已定位成功"}

    c = CommentItem(
        comment_id="c_1",
        work_id="w_1",
        author_name="测试粉丝",
        author_id="u_1",
        content="真解压啊这个视频",
        timestamp=1700000000,
    )
    ok, detail = reply_single_comment_on_page(mock_page, c, "谢谢喜欢~", dry_run=True)
    assert ok is True
    assert "dry_run" in detail


def test_run_comment_reply_batch_and_ledger(tmp_path: Path):
    mock_page = MagicMock()
    mock_page.url = "https://cp.kuaishou.com/article/comment"

    # 模拟嗅探拉到一条未回复评论
    comments_data = [
        CommentItem(
            comment_id="c_test_1",
            work_id="w_test_1",
            author_name="好学者",
            author_id="u_test_1",
            content="学到了，很有用！",
            timestamp=1700000000,
            liked_count=20,
            sub_comment_count=0,
            toped=False,
        )
    ]

    with patch("bot.publish.kuaishou_reply.fetch_kuaishou_comments", return_value=comments_data):
        with patch("bot.publish.kuaishou_reply.generate_comment_reply", return_value="开心能帮到你！"):
            with patch("bot.publish.kuaishou_reply.reply_single_comment_on_page", return_value=(True, "回复已成功发送")):
                log_file = tmp_path / "test_reply_log.jsonl"
                res = run_comment_reply_batch(
                    mock_page,
                    dry_run=False,
                    max_replies=3,
                    log_path=log_file,
                )

                assert res["total"] == 1
                assert res["succeeded"] == 1
                assert res["failed"] == 0

                assert log_file.exists()
                lines = log_file.read_text(encoding="utf-8").strip().splitlines()
                assert len(lines) == 1
                record = json.loads(lines[0])
                assert record["comment_id"] == "c_test_1"
                assert record["reply_content"] == "开心能帮到你！"
                assert record["status"] == "ok"
                assert record["mode"] == "execute"


def test_run_comment_reply_batch_fail_stop(tmp_path: Path):
    mock_page = MagicMock()
    # 模拟两条未回复评论
    comments_data = [
        CommentItem(comment_id="c_1", work_id="w_1", author_name="u1", author_id="uid1", content="第一条", timestamp=1, liked_count=10),
        CommentItem(comment_id="c_2", work_id="w_2", author_name="u2", author_id="uid2", content="第二条", timestamp=2, liked_count=5),
    ]

    # 第一条执行失败，应当立即 fail-stop，不再执行第二条
    with patch("bot.publish.kuaishou_reply.fetch_kuaishou_comments", return_value=comments_data):
        with patch("bot.publish.kuaishou_reply.generate_comment_reply", return_value="回复"):
            with patch("bot.publish.kuaishou_reply.reply_single_comment_on_page", return_value=(False, "风控限制")):
                log_file = tmp_path / "fail_stop_log.jsonl"
                res = run_comment_reply_batch(
                    mock_page,
                    dry_run=False,
                    max_replies=2,
                    log_path=log_file,
                )

                assert res["failed"] == 1
                assert res["succeeded"] == 0
                # 只有 1 条台账，说明没有尝试第二条（即时熔断）
                lines = log_file.read_text(encoding="utf-8").strip().splitlines()
                assert len(lines) == 1
                record = json.loads(lines[0])
                assert record["status"] == "error"
                assert "风控" in record["detail"]
