# xiaoai-music-hub 常用开发命令
#
# 约定：所有 Python 命令都走仓库内的虚拟环境（.venv），不污染系统 Python。
# 每个目标都可独立运行：依赖目标会先确保 venv / 开发依赖就绪。

PY := .venv/bin/python
PIP := $(PY) -m pip
VENV_PY := python3

.PHONY: venv install dev run test cov lint fmt clean docker

## venv: 创建仓库内虚拟环境并升级 pip / 构建工具（PEP 621 需要 setuptools>=61）
venv:
	$(VENV_PY) -m venv .venv
	$(PIP) install -U pip setuptools wheel

## install: 以可编辑模式安装运行依赖（等价于 pip install -e .）
install: venv
	$(PIP) install -e .

## dev: 安装开发依赖（pytest / pytest-cov / ruff）
dev: venv
	$(PIP) install -e ".[dev]"

## run: 启动服务（默认 http://0.0.0.0:8010）
run: install
	$(PY) app.py

## test: 运行测试（配置见 pyproject.toml 的 [tool.pytest.ini_options]）
test: dev
	$(PY) -m pytest

## cov: 运行测试并输出覆盖率
cov: dev
	$(PY) -m pytest --cov=xiaoai_hub --cov-report=term-missing

## lint: 代码检查（ruff，规则见 pyproject.toml）
lint: dev
	$(PY) -m ruff check .

## fmt: 自动格式化 + 自动修复可修复的 lint 问题
fmt: dev
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

## clean: 清理构建与缓存产物（不动 .venv / data）
clean:
	rm -rf build dist htmlcov .pytest_cache .ruff_cache .coverage *.egg-info
	find xiaoai_hub tests -type d -name __pycache__ -prune -exec rm -rf {} +
	find xiaoai_hub tests -type f -name "*.py[co]" -delete

## docker: 构建容器镜像（需 Docker）
docker:
	docker build -f deploy/Dockerfile -t xiaoai-music-hub:latest .
