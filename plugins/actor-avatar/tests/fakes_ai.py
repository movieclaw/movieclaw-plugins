"""AI 助手的替身：按演员名回预设的文本，记下每次请求。"""

from __future__ import annotations

from types import SimpleNamespace


class FakeAgent:
    def __init__(self, answers: dict[str, str] | None = None, *, raise_exc=None, status="done"):
        self.answers = answers or {}
        self.raise_exc = raise_exc
        self.status = status
        self.calls: list[dict] = []
        self.deleted: list[str] = []

    async def ask(self, content, **kwargs):
        self.calls.append({"content": content, **kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        text = next((v for k, v in self.answers.items() if k in content), "[]")
        return SimpleNamespace(
            session_id=f"s{len(self.calls)}",
            message_id="m",
            status=self.status,
            text=text,
            finish_reason="stop",
            model="fake",
            usage={"prompt_tokens": 120, "completion_tokens": 15, "total_tokens": 135},
            error="boom" if self.status != "done" else None,
        )

    async def delete(self, session_id):
        self.deleted.append(session_id)
        return True
