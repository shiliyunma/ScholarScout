# GitHub 在中国网络下的下载方案

## 问题
`git clone` 到 `github.com:443` 在香港/中国大陆的网络环境下会频繁超时（`Failed to connect to github.com port 443 after 21000+ ms`）。

## 解决方案

### 方案 A：codeload.github.com ZIP 下载（推荐）
使用 `curl` 直接从 `codeload.github.com` 下载仓库 ZIP，实测速度可达 8MB/s+。

```bash
curl -L -o repo.zip "https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
```

示例：
```bash
# master 分支
curl -L -o starter-kit.zip "https://codeload.github.com/HKUSTDial/kddcup2026-data-agents-starter-kit/zip/refs/heads/master"

# main 分支
curl -L -o awesome-list.zip "https://codeload.github.com/HKUSTDial/awesome-data-agents/zip/refs/heads/main"
```

**优点**：速度快、命令简单、无需 git 安装
**缺点**：下载的是 ZIP 快照（无 git 历史），需要解压后手动移动目录结构

### 方案 B：raw.githubusercontent.com 单文件下载
用于下载仓库中的单个文件：

```bash
curl -L -o README.md "https://raw.githubusercontent.com/{owner}/{repo}/{branch}/path/to/file"
```

示例：
```bash
curl -L -o README.md "https://raw.githubusercontent.com/HKUSTDial/kddcup2026-data-agents-starter-kit/master/README.md"
```

### 方案 C：浏览器直连
浏览器内置网络栈对于 GitHub 直连更稳定，可以：
- 直接访问 GitHub 页面
- 通过 "Code → Download ZIP" 下载
- 访问 `raw.githubusercontent.com` 获取文件内容

### 不推荐的方案
- `git clone` via HTTPS（always times out on port 443）
- `ghproxy.com` / `gh-proxy.com` / `hub.fastgit.xyz` 等镜像代理（不可靠，常超时或返回空仓库）

## 解压后目录布局处理
ZIP 下载后需要手动调整目录结构：

```bash
unzip repo.zip -d temp_dir
# 将顶层子目录内容移到目标目录
mv temp_dir/repo-name/* target_dir/
mv temp_dir/repo-name/.* target_dir/ 2>/dev/null
rm -rf temp_dir
```
