"""每个 MovieClaw 成员独立的同步偏好；新能力必须由成员主动打开。"""

from pydantic import BaseModel, ConfigDict


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    manual_watched: bool = False
    manual_unwatched: bool = False
    favorites: bool = False


async def preferences(engine, member):
    return Preferences.model_validate(await engine.get(member, "profile", {}))
