"""前端静态一致性测试（零依赖）。

防止「改了接口/改了 DOM 但前端没跟上」这类跨层回归：
- HTML 引用的静态资源都存在，DOM id 唯一；
- JS 里用到的 ``#id`` 选择器在 HTML 中都有定义；
- JS 调用的 HTTP 路径在后端路由表里都有注册；
- 不使用 innerHTML 拼接接口数据、不引入外部 CDN；
- ``node --check`` 语法校验（有 node 时）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "xiaoai_hub" / "web"


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html() -> str:
    return read("index.html")


@pytest.fixture(scope="module")
def app_js() -> str:
    return read("app.js")


def html_ids(html: str) -> set:
    return set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))


# ---------------------------------------------------------------------------
# 资源与结构
# ---------------------------------------------------------------------------
def test_web_files_exist():
    for name in ("index.html", "app.js", "style.css", "favicon.svg"):
        assert (WEB / name).is_file(), name


def test_index_references_local_assets(index_html):
    assert 'href="/style.css"' in index_html
    assert 'href="/favicon.svg"' in index_html
    assert 'src="/app.js"' in index_html or 'src="/app.js"' in index_html
    assert 'lang="zh-CN"' in index_html
    assert 'name="viewport"' in index_html
    assert '<script src="/app.js"' in index_html


def test_no_external_cdn(index_html, app_js):
    for text in (index_html, app_js):
        lowered = text.lower()
        for marker in ("cdn.jsdelivr", "unpkg.com", "cdnjs.", "fonts.googleapis",
                       "cdn.bootcdn", "staticfile.org"):
            assert marker not in lowered, marker


def test_dom_ids_are_unique(index_html):
    ids = re.findall(r'id="([A-Za-z0-9_\-]+)"', index_html)
    duplicates = {item for item in ids if ids.count(item) > 1}
    assert duplicates == set()


# ---------------------------------------------------------------------------
# JS ↔ HTML 一致性
# ---------------------------------------------------------------------------
def test_js_selectors_exist_in_html(index_html, app_js):
    selectors = set(re.findall(r"""\$\('#([A-Za-z0-9_\-]+)'\)""", app_js))
    # 动态拼接的选择器（#tab-<name> / #panel-<name>）单独校验
    dynamic_prefixes = {"tab-", "panel-"}
    missing = sorted(
        selector
        for selector in selectors
        if selector not in dynamic_prefixes and selector not in html_ids(index_html)
    )
    assert missing == [], f"JS 引用了不存在的 DOM id: {missing}"

    ids = html_ids(index_html)
    for prefix in dynamic_prefixes:
        for name in ("search", "playlists", "queue", "local", "downloads", "settings"):
            assert f"{prefix}{name}" in ids


# ---------------------------------------------------------------------------
# JS ↔ 后端路由一致性
# ---------------------------------------------------------------------------
def _backend_paths() -> set:
    from xiaoai_hub import config
    from xiaoai_hub.api.server import build_router

    ctx = SimpleNamespace(
        settings=config.get(),
        db=object(),
        prefs=object(),
        player=object(),
        netease=object(),
        library=object(),
        downloader=object(),
        dlna=object(),
        version="test",
        login={},
        sync={},
        started_at=0.0,
        uptime=lambda: 0.0,
    )
    router = build_router(ctx)
    paths = set()
    for route in router.routes:
        paths.add(re.sub(r"\{[a-zA-Z_]+\}", "\x00", route.pattern))
    return paths


def _frontend_paths(app_js: str) -> set:
    calls = set()
    for match in re.finditer(r"""api\(\s*['"]([^'"]+)['"]""", app_js):
        path = match.group(1).split("?")[0]
        calls.add(path)
    for match in re.finditer(r"""fetch\(\s*['"]([^'"]+)['"]""", app_js):
        path = match.group(1).split("?")[0]
        calls.add(path)
    return calls


def test_frontend_paths_exist_in_backend(app_js):
    backend = _backend_paths()
    missing = []
    for path in sorted(_frontend_paths(app_js)):
        if not path.startswith("/"):
            continue
        matched = path in backend
        if not matched:
            pattern = re.sub(r"/[0-9]+", "/\x00", path)
            matched = pattern in backend
        if not matched:
            missing.append(path)
    assert missing == [], f"前端调用了后端不存在的接口: {missing}"


# ---------------------------------------------------------------------------
# 安全约定
# ---------------------------------------------------------------------------
def test_no_innerhtml_with_dynamic_data(app_js):
    assert "innerHTML" not in app_js, (
        "前端禁止使用 innerHTML 拼接接口数据，请用 textContent/createElement"
    )


def test_js_syntax_ok():
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 node，跳过语法检查")
    result = subprocess.run(
        [node, "--check", str(WEB / "app.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_css_has_no_import_of_remote():
    css = read("style.css")
    assert "@import" not in css
    lowered = css.lower()
    for marker in ("http://", "https://"):
        assert marker not in lowered, "样式不应依赖远程资源"
