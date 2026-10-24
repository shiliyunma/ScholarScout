# syntax=docker/dockerfile:1
#
# ScholarScout 无界面（headless）镜像 —— 只跑 CLI / 机器人，**不开桌面窗口**。
#
# 为什么不装 pywebview：容器里没有显示器也没有 WebView2 运行时，桌面壳 `--desktop`
# 在这里没有意义。代码侧本来就把 `import webview` 放在 `desktop/app.py` 的函数内
# （见该文件第 60、156 行），`--desktop` 之外的任何入口都不导入它，所以不装也不会
# 让 CLI 启动失败；误用 `--desktop` 只会得到一条"未安装 pywebview"的明确报错。
#
# 基础镜像可用 --build-arg 覆盖（国内拉不动 docker.io 时）：
#   docker build --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim -t scholarscout:dev .
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SCHOLARSCOUT_HOME=/data

# 非 root：容器里的进程只需要读写 /data（卷），代码目录只读即可。
RUN groupadd --system scout \
    && useradd --system --gid scout --home-dir /app --shell /usr/sbin/nologin scout

WORKDIR /app

# 运行期依赖白名单里的 6 项，版本范围与 pyproject.toml 逐条对齐（唯一来源是 pyproject，
# 这里只是把 pywebview 摘掉；新增依赖仍然要过 scripts/check_new_deps.py 的审批）。
RUN pip install --no-cache-dir \
        "httpx>=0.28.1,<1" \
        "PyYAML>=6.0.2,<7" \
        "pymupdf>=1.24,<2" \
        "lark-oapi>=1.7,<2" \
        "python-docx>=1.1,<2" \
        "openpyxl>=3.1,<4"

# 代码按 `scripts/scout.py` 的布局放：它把 `src` 插进 sys.path，所以不需要 pip 安装本包
#（也不该装：editable 安装会踩中文路径的 .pth 编码坑，见 scripts/README.md）。
COPY --chown=scout:scout src/ ./src/
COPY --chown=scout:scout scripts/ ./scripts/
# 提示词按 __file__ 相对项目根定位（runtime/search_intent.py、grading/support.py），必须在 /app/prompts
COPY --chown=scout:scout prompts/ ./prompts/
COPY --chown=scout:scout .env.example config.example.yaml ./

# `paths.skills_dir()` 是 `<home>/skills`，容器里 home 就是 /data，所以技能库落在这儿：
# 镜像内先放一份，空命名卷首次挂载时会从镜像内容初始化，`skill.list` 才不是空的。
COPY --chown=scout:scout skills/ /data/skills/

RUN mkdir -p /data && chown -R scout:scout /data

USER scout

# data/raw、data/text、run/state.db、run/logs 全部在 SCHOLARSCOUT_HOME 之下，一个卷装完。
VOLUME ["/data"]

ENTRYPOINT ["python", "scripts/scout.py"]
CMD ["--chat"]
