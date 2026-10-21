# 本机 WSL 环境入口

本配置用于在 Windows 上管理 HFlow，在 WSL2 Ubuntu-22.04 中运行本地质检。
只配置环境与合成样本验证；不调用模型 API，不自动读取 Argus 的真实数据。

## 目录与职责

| 内容 | 固定位置 |
| --- | --- |
| 项目入口、代码修改与审查 | `D:\code\元节智能\ai_Quality_Inspection_Division\HFlow` |
| Windows 启动器 | `scripts/wsl/hflow.ps1` |
| WSL 环境与同步逻辑 | `scripts/wsl/run.sh` |
| 环境验证逻辑 | `scripts/wsl/verify.py` |
| WSL 专用根目录 | `/home/carlosgao/workspaces/ai_Quality_Inspection_Division/HFlow` |
| Linux 代码运行副本 | 专用根目录下的 `repo/` |
| Linux Python 虚拟环境 | `repo/.venv/` |
| 后续质检结果与数据根目录 | `data/` |
| 下载缓存 | `cache/` |
| 每次命令的完整日志 | `logs/` |
| 最新环境验证回执 | D 盘项目的 `data/local-wsl-environment/environment.json`，同时保存在 WSL 的 `logs/environment.json` |

WSL 路径按当前 Linux 用户的 `$HOME` 计算。Windows 文件管理器中可访问：
`\\wsl.localhost\Ubuntu-22.04\home\carlosgao\workspaces\ai_Quality_Inspection_Division\HFlow`。

**D 盘代码是唯一编辑入口。不要直接修改 Linux 的 repo 副本。**
`setup`、`sync`、`run` 会将 D 盘代码同步至该副本，包括未提交的代码修改，并删除副本中已不在源目录的普通文件。
同步不复制 `.git`、`.venv`、`data`、`.env`、`.env.*`、Python 缓存和 node_modules；它们也不会因同步被删除。
原始采集文件不能放在 Linux repo 的普通代码目录中，应放在独立的 `data/inputs/` 下。
运行目录带来源标记与互斥锁，避免误用其他目录或并行覆盖运行副本。

## 从 Windows 使用

在 PowerShell 中进入 D 盘 HFlow 项目后执行：

```powershell
# 首次安装或更新锁定依赖，并做环境验证。
.\scripts\wsl\hflow.ps1 -Action setup

# 重新验证当前 Linux 副本；不同步源代码、不重新安装依赖。
.\scripts\wsl\hflow.ps1 -Action check

# 只同步代码，不安装依赖、不运行质检。
.\scripts\wsl\hflow.ps1 -Action sync

# 同步最新代码后，在 WSL 环境运行命令。
.\scripts\wsl\hflow.ps1 -Action run -CommandArgs @('hflow', '--help')
```

`setup` 使用 `uv sync --locked --python 3.11`，按现有锁文件安装项目及开发依赖。
不修改 Ubuntu 系统 Python，不在 Windows 建立 Python 环境。
`run` 使用 `uv run --locked`，锁文件不匹配时会拒绝继续，依赖变化时可能访问网络。
所有命令的退出码会传回 PowerShell；失败时查看 WSL `logs/`，不能把失败当成环境就绪。

## FFmpeg 与其他依赖

使用 HFlow 仓库固定的 Linux FFmpeg/FFprobe 构建，首次调用自动下载并校验 SHA-256。
二进制保存在专用缓存目录，版本和路径写入环境回执；不安装 Ubuntu 22.04 默认的旧版 FFmpeg。
验证脚本为这对已解析的工具在 WSL 专用 `bin/` 目录建立链接，供后续测试通过 PATH 调用。

不需要 GPU、CUDA、Docker、Airflow 或 API Key。
安装的是当前仓库开发依赖集合，其中包含 OpenCV、PyArrow 和测试工具；不安装可选的 MediaPipe 模型。
首次安装需要 WSL 能连接依赖源和 GitHub。若 Windows 使用只监听 localhost 的代理，WSL 的 NAT 网络可能不能直接访问该代理；应按实际网络配置排查，不关闭下载校验。

## 验证范围

`setup` 和 `check` 将：

1. 检查 Python 包、Linux 文件锁能力和项目版本。
2. 获取并记录指定版本的 FFmpeg/FFprobe。
3. 检查 HFlow 命令行入口。
4. 运行仓库自带 quickstart，生成小型合成 MCAP，执行转换和基础检查。
5. 写出成功或失败的 JSON 回执。

合成输入位于 Linux `repo/data/sample/`，运行结果位于专用 `data/`。
回执包含源代码提交号与锁文件 SHA-256；新增环境脚本可能尚未提交，审查时也应结合 `git status`。
`data/` 已被项目忽略，不提交环境回执、采集录像或生成媒体。

环境验证通过不代表 Realman 适配已经完成，不代表两份真实 MCAP 已通过质检。
后续需要单独完成文件完整性、话题与嵌套字段、时间基准、质量阈值和 Argus 放行规则。

## 项目测试

运行上游完整测试时使用：

```powershell
.\scripts\wsl\hflow.ps1 -Action run -CommandArgs @('env', '-u', 'XDG_CACHE_HOME', 'pytest', '-q', '-n', '4')
```

其中一项上游缓存迁移测试要求 `XDG_CACHE_HOME` 未设置，因此仅对测试子进程移除该变量。
这不修改日常运行配置，也不跳过该测试。FFmpeg/FFprobe 仍可通过专用 `bin/` 找到。
测试产生的临时文件使用 pytest 默认临时目录；网络、Docker、外部模型等可选集成测试按上游默认规则跳过。

## 本次配置验证（2026-10-20）

- WSL2 Ubuntu-22.04；Python 3.11.16；HFlow 0.2.21。
- FFmpeg/FFprobe：仓库锁定的 `n8.1.2-50-g1a748fe2cd-20260831`。
- 命令行入口与合成样本 quickstart 通过；检测到样本内 1 秒黑屏片段。
- 完整测试：2552 passed，8 skipped；使用上一节的测试环境命令。
- 新增 Python 脚本 Ruff、格式和类型检查通过；PowerShell、Bash 语法检查通过。
- 完整测试日志副本：D 盘项目 `data/local-wsl-environment/tests.log`。
- 两份真实 MCAP 尚未复制、转换或执行 HFlow 质检；没有调用模型 API。
