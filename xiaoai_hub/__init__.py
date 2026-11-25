"""xiaoai-music-hub：把小爱音箱当成 DLNA 播放器的音乐中枢。

模块分层（依赖只能自上而下）::

    api/  →  player / downloader / library / prefs  →  netease / dlna  →  db / config

对外入口：``xiaoai_hub.app:main``（见根目录 ``app.py``）。
"""

__version__ = "3.0.0"
__all__ = ["__version__"]
