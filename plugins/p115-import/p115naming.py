"""115 文件适配：命名渲染、季集仲裁及扩展名复用 MovieClaw 本体纯函数。

不读取本体内部配置/数据库；模板与库级覆盖由批准的宿主操作提供。
"""
import re
from pathlib import Path

from movieclaw_api.services.library.naming import NamingTemplates, render
from movieclaw_api.services.library.units import resolve_units
from movieclaw_api.services.library.layout import VIDEO_EXTS as CORE_VIDEO_EXTS, season_from_dir as core_season_from_dir

VIDEO_EXTS = frozenset(CORE_VIDEO_EXTS)
DISC_EXTS = frozenset({".iso", ".img", ".bdmv"})
RESOLUTION = re.compile(r"(?<![0-9])(2160p|1440p|1080p|1080i|720p|576p|480p)(?![0-9a-z])", re.I)
DEFAULT_ENTRY_DIR = NamingTemplates().entry_dir
DEFAULT_MOVIE_FILE = NamingTemplates().movie_file
DEFAULT_SEASON_DIR = NamingTemplates().season_dir
DEFAULT_EPISODE_FILE = NamingTemplates().episode_file


class Templates:
    def __init__(self, entry_dir=None, movie_file=None, season_dir=None, episode_file=None):
        self.entry_dir = entry_dir or DEFAULT_ENTRY_DIR
        self.movie_file = movie_file or DEFAULT_MOVIE_FILE
        self.season_dir = season_dir or DEFAULT_SEASON_DIR
        self.episode_file = episode_file or DEFAULT_EPISODE_FILE

    @classmethod
    def from_settings(cls, setting, overrides=None):
        values = dict(setting or {})
        values.update({k:v for k,v in (overrides or {}).items() if v not in (None, "")})
        return cls(entry_dir=values.get("naming_entry_dir"), movie_file=values.get("naming_movie_file"),
                   season_dir=values.get("naming_season_dir"), episode_file=values.get("naming_episode_file"))


def entry_name(templates,title,year=None,tmdb_id=None,original_title=None,**extra):
    return render(templates.entry_dir,dict(title=title,year=year,tmdb_id=tmdb_id,original_title=original_title,**extra))


def movie_file_name(templates,title,year=None,tmdb_id=None,resolution=None,**extra):
    return render(templates.movie_file,dict(title=title,year=year,tmdb_id=tmdb_id,resolution=resolution,**extra))


def season_dir_name(templates,season,title=None,year=None,**extra):
    return render(templates.season_dir,dict(season=season,title=title,year=year,**extra))


def episode_file_name(templates,title,year=None,season=1,episode=1,episode_title=None,resolution=None,tmdb_id=None,**extra):
    return render(templates.episode_file,dict(title=title,year=year,season=season,episode=episode,
                                          episode_title=episode_title,resolution=resolution,tmdb_id=tmdb_id,**extra))


def video_ext(name):
    ext=Path(name or "").suffix.lower()
    return ext if ext in VIDEO_EXTS else None


def is_video(name): return video_ext(name) is not None

def is_disc(name): return Path(name or "").suffix.lower() in DISC_EXTS

def is_sample(name): return "sample" in (name or "").lower()

def resolution_of(name):
    match=RESOLUTION.search(name or "")
    return match.group(1).lower() if match else None


def season_from_dir(name):
    return core_season_from_dir(Path(name or ""))


def batch_units(paths,entry_name=None,known_seasons=None):
    files=[Path(p) for p in paths]
    result=resolve_units(files,entry_name=entry_name,known_seasons=known_seasons)
    return {str(p):(u.season,u.episode) if u.season is not None and u.episode>0 else None
            for p,u in result.items()}


def episode_from_path(rel_path):
    # 兼容旧入口：只返回确定性可得的信息，不再默认第 1 季。
    path=str(Path((rel_path or "").replace("\\","/")))
    unit=batch_units([path]).get(path)
    return unit if unit else (None,None)


def guess_title(text):
    # 兼容外部测试/旧版本调用；新识别统一走 p115recognition。
    from p115recognition import source_clues
    clues=source_clues(text,"movie","兼容入口")
    return clues[0].title if clues else ""


def guess_year(text):
    from p115recognition import parsed
    return parsed(text or "").get("year")
