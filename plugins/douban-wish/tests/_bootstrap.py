"""让纯函数单测在两种环境里都能跑。

装好了插件运行时的环境（上游 CI）直接导入真实模块；没装 ``pydantic`` / ``fastapi`` /
``httpx`` / ``movieclaw_*`` 的机器上，用 AST 把纯函数（连同依赖的模块级常量）抽出来，
塞进一个桩模块执行。这样单测只依赖标准库，跑得动就是真的跑过。
"""

import ast
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCES = Path(__file__).resolve().parent.parent


def _real_available() -> bool:
    for name in ("pydantic", "fastapi", "httpx", "movieclaw_sdk", "movieclaw_api", "movieclaw_scheduler"):
        if name in sys.modules:
            return True
    try:
        import httpx  # noqa: F401,PLC0415
        import pydantic  # noqa: F401,PLC0415
        import movieclaw_sdk  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001 -- 装不上就走下面的抽取路径
        return False
    return True


if _real_available():
    sys.path.insert(0, str(SOURCES))
else:
    SOURCE = SOURCES / "douban_wish.py"

    # 要抽出来的东西：纯函数 + 它们依赖的模块级常量。
    WANTED_FUNCS = {
        "normalize_user", "normalize_users", "parse_wishes", "cutoff_string",
        "in_window", "baseline_string", "is_permanent_error", "seasons_missing",
        "cjk_number", "season_marker", "plan_for", "pick_candidates",
    }
    WANTED_NAMES = {
        "CST", "WISH_STATUS", "WANTED_KINDS", "_CN_DIGITS", "_CN_SEASON",
        "_EN_SEASON", "_S_SEASON", "_PROFILE", "_BAD_USER", "_SPLIT",
    }

    _tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    _body = []
    for _node in _tree.body:
        if isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _node.name in WANTED_FUNCS:
                _body.append(_node)
        elif isinstance(_node, ast.Assign):
            _targets = {t.id for t in _node.targets if isinstance(t, ast.Name)}
            if _targets and _targets <= WANTED_NAMES:
                _body.append(_node)
        elif isinstance(_node, ast.AnnAssign) and isinstance(_node.target, ast.Name):
            if _node.target.id in WANTED_NAMES:
                _body.append(_node)
    _module = ast.fix_missing_locations(ast.Module(body=_body, type_ignores=[]))

    def _stub(name: str, **attrs):
        created = types.ModuleType(name)
        for _key, _value in attrs.items():
            setattr(created, _key, _value)
        sys.modules[name] = created
        return created

    class _Field:
        def __init__(self, default=None, **_kwargs):
            self.default = default

    class _BaseModel:
        pass

    class _SecretStr(str):
        pass

    class _OpsError(Exception):
        """``OpsError("op", "boom")``：测试里只关心 ``.status``。"""

        def __init__(self, *args, **_kwargs):
            super().__init__(*args)
            self.message = args[-1] if args else ""
            self.status = None

    def _field_validator(*_args, **_kwargs):
        def _wrap(func):
            return func

        return _wrap

    _stub(
        "pydantic",
        BaseModel=_BaseModel,
        Field=_Field,
        SecretStr=_SecretStr,
        ConfigDict=lambda **kwargs: kwargs,
        field_validator=_field_validator,
    )
    _stub("fastapi", APIRouter=lambda **_kwargs: None, HTTPException=Exception)
    _stub("httpx", AsyncClient=object)
    _stub("movieclaw_api")
    _stub("movieclaw_api.plugins")
    _stub("movieclaw_api.plugins.keys", PLUGIN_DATA="data", PLUGIN_ROUTES="routes")
    _stub(
        "movieclaw_scheduler",
        SCHEDULED_TASKS=object(),
        TaskDefinition=object(),
        TriggerType=object(),
    )
    _stub(
        "movieclaw_sdk",
        Context=object,
        plugin=lambda *_a, **_k: (lambda f: f),
        net=types.SimpleNamespace(http_transport=lambda *_a, **_k: None),
    )
    _stub(
        "movieclaw_sdk.events",
        PLUGIN_EVENTS="plugin_events",
        Publisher=object,
        plugin_event=lambda *_a, **_k: None,
    )
    _stub("movieclaw_sdk.ops", HOST_OPS="ops", OpsError=_OpsError)

    _target = types.ModuleType("douban_wish")
    _target.__file__ = str(SOURCE)
    # exec 之前先把纯函数里会用到的名字放进模块命名空间
    _target.re = re
    _target.datetime = datetime
    _target.timedelta = timedelta
    _target.timezone = timezone
    _target.OpsError = _OpsError
    _target.pydantic = sys.modules["pydantic"]
    _target.Field = _Field
    _target.SecretStr = _SecretStr
    _target.field_validator = _field_validator

    class _Wish:
        """``Wish`` 桩：parse_wishes 只往上塞字段，测试只按属性读。"""

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def __eq__(self, other):
            return isinstance(other, _Wish) and self.__dict__ == other.__dict__

        def __repr__(self):
            return f"Wish({self.__dict__})"

    _target.Wish = _Wish

    sys.modules["douban_wish"] = _target
    exec(compile(_module, str(SOURCE), "exec"), _target.__dict__)