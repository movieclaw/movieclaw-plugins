"""从宿主登录依赖取得可信账号，不解析登录凭据，不接受客户端指定用户。"""
from fastapi import Depends, HTTPException, Request


def make_account_dependency(require_login):
    async def account_identity(request: Request, principal=Depends(require_login)):
        if principal.plugin is not None or principal.kind not in {"admin", "member"}:
            raise HTTPException(403, "请使用 MovieClaw 登录账号访问私人偏好。")
        if principal.kind == "admin":
            identity = "admin:0"
        else:
            created = getattr(principal.member, "created_at", None)
            if principal.member_id is None or created is None:
                raise HTTPException(403, "暂时无法确认成员账号。")
            identity = f"member:{principal.member_id}:{created.isoformat()}"
        request.state.platform_account_identity = identity
        request.state.platform_account_label = principal.kind + ":" + principal.name
    return account_identity


def host_account_dependency():
    from movieclaw_api.api.deps import require_login
    return make_account_dependency(require_login)
