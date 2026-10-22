# 本机服务管理台 · Local Service Management

**基于 [OliveTin](https://github.com/OliveTin/OliveTin) 开发和扩展的 macOS 本地服务管理台，非 OliveTin 官方发行版。**

将分散部署的 Docker Compose、Docker 容器、桌面应用、LaunchAgent 和独立网页进程集中管理：查看状态、打开使用入口、启停服务、维护使用说明，以及保留数据卸载和回收站清理。

OliveTin 提供原始命令控制台与生成配置的承载。本项目在其部署方案上新增了独立的 Python 本机 API、自定义前端、服务登记与校验、生命周期适配、外观管理和回收站。新版界面的操作由 Python 适配器执行；原始 OliveTin 控制台作为备用入口。因此它是一个定制集成项目，而非 OliveTin 核心源码的分支。

## 功能

- 服务列表、分类管理、搜索、详情、状态刷新与使用链接。
- 添加和完整编辑服务；运行配置先验证再保存，验证不启动业务。
- 应用图标上传、预览、恢复默认，列表与详情同步；管理台图标、背景、透明度和模糊可独立设置。
- Docker Compose、已有 Docker 容器、`.app`、用户 LaunchAgent、独立进程与专用声音服务适配。
- 卸载后进入独立回收站；恢复或永久移除管理记录。
- 永久删除前列出已识别的数据目录与命名卷；业务数据默认保留，只有主动勾选且可核实归属的项目才会删除。
- 本机来源校验、服务身份检查、每项服务互斥锁、过期表单检查，以及配置保存备份。

## 运行环境

当前适配 **macOS / Apple Silicon**。需要 Python 3（仅使用标准库）、系统 `sips`、`launchctl` 和 `lsof`；开发校验使用 Node.js 的 `node --check`。Docker、Compose、Colima 仅在管理相应类型业务时需要，相关工具按现有 Homebrew 布局 `/opt/homebrew/bin` 查找。

本仓库从空服务清单开始，不包含作者的真实服务、凭据、模型、业务数据或已安装程序。发布副本也移除了个人服务预设和私人壁纸，默认使用通用图标与渐变背景。

## 快速开始

```sh
git clone https://github.com/hytheyanting/local-service-management.git
cd local-service-management
python3 scripts/prepare_local.py
python3 ui_server.py
```

打开 [本机服务管理台](http://127.0.0.1:1337/)。另开一个终端保持状态刷新：

```sh
cd local-service-management
python3 sync.py
```

点击“添加服务”，填写已有部署的信息，先验证，再加入管理。这里的添加是**接管已安装的服务**，不负责自动下载安装第三方软件。安装流程也可以通过 `register-service.py` 提交可信清单；尚不支持自动接管任意安装器。

`prepare_local.py` 只在 `services.json` 不存在时创建空清单，已有服务不会被覆盖。若本机已经有管理台占用 1337，请在验证另一份副本时使用 `python3 ui_server.py --port 1340`，不要同时启动两个相同端口的实例。

### 接入 OliveTin 原始控制台

本机开发曾使用 OliveTin **3000.20.0**。仓库不分发 OliveTin 二进制；从 [OliveTin 官方 Releases](https://github.com/OliveTin/OliveTin/releases) 获取适合平台的发布包，将其解压为以下结构：

```text
vendor/OliveTin-darwin-arm64/OliveTin
vendor/OliveTin-darwin-arm64/webui/
```

`prepare_local.py` 已生成 `config.next.json`，随后运行：

```sh
python3 supervisor.py
```

原始控制台位于 [127.0.0.1:1338](http://127.0.0.1:1338/)。监督程序会先停止旧 OliveTin 子进程，再应用新配置，避免部署时遇到的热重载并发问题。主界面/API 和业务服务不因原始控制台重载而重启。

### 开机登录后运行

先生成当前路径对应的 LaunchAgent 文件（此步骤只生成文件，不安装、不启停）：

```sh
python3 scripts/make_launchagents.py
# 若已安装上述 OliveTin 发布包，可加 --include-olivetin
```

检查 `launchagents/` 中的文件，再按需复制到 `~/Library/LaunchAgents/` 并用 `launchctl bootstrap gui/$(id -u) <plist绝对路径>` 加载。生成器使用当前项目路径，不包含作者的用户名或机器路径。

## 图标和回收站

- **管理台图标**：“外观设置 → 管理台图标”。浏览器标签页的小图标（favicon）同步使用这一图标，支持上传预览、取消、保存后刷新保留和恢复默认。
- **单个应用图标**：“添加／编辑服务 → 应用图标”。支持 JPG、PNG、WebP，最大 8 MB；图片保存在本机。图标修改不会重启业务。
- **回收站**：已卸载服务从普通列表移走，保留原分类关联以供恢复。
- **彻底删除**：先查看数据清单，输入完整服务名称并确认不可恢复后才提交。未勾选的数据保留，但删除登记后不再由回收站管理。

Compose 命名卷需要原 Docker 环境可访问、项目标签匹配且无容器引用。共享、外部或归属不明的资源只展示，不直接清理；检测不会自动启动虚拟机。项目源码、配置、镜像缓存和虚拟机保留。桌面应用数据检测仅覆盖标准数据位置，自定义位置会保留。已选数据若部分清理失败，回收站记录会保留用于重新检测；已删除内容不会被描述为已回滚。

## 项目结构

| 文件 | 用途 |
| --- | --- |
| `ui/`、`ui_server.py` | 自定义前端、本机 API 和后台操作调度 |
| `control.py` | 受登记服务身份约束的启停、打开、卸载和恢复适配 |
| `registration.py`、`service_editor.py` | 新增校验、完整编辑、保存与并发控制 |
| `trash.py` | 回收站数据预览和显式选择清理 |
| `ui_model.py` | 分类、外观、上传图片和本机偏好持久化 |
| `sync.py`、`build_config.py` | 状态刷新、OliveTin 配置生成 |
| `supervisor.py` | OliveTin 配置应用与子进程监督 |
| `examples/`、`scripts/` | 通用示例、初始化与 LaunchAgent 生成 |

`services.json`、`state/`、`logs/`、生成的 OliveTin 配置、上传图片、业务归档和实际 LaunchAgent 配置均不纳入 Git。

## 验证

```sh
python3 -m unittest discover -v
node --check ui/app.js
```

测试覆盖服务登记、完整编辑、图标、互斥锁、身份边界、数据保留、回收站选择删除及 HTTP 来源校验。真实 Docker 清理命令使用模拟执行验证；浏览器上传/保存/刷新在隔离服务清单中验收，未对真实业务执行永久删除。测试主要依赖 macOS 系统工具，其他平台尚未适配。

## 上游与许可

- **OliveTin**：[上游项目](https://github.com/OliveTin/OliveTin)，原许可证为 GNU AGPL v3；许可文本保留在 [LICENSES/OliveTin-AGPL-3.0.txt](LICENSES/OliveTin-AGPL-3.0.txt)。
- **Tabler Icons**：[上游项目](https://github.com/tabler/tabler-icons)，MIT；许可文本保留在 [LICENSES/Tabler-MIT.txt](LICENSES/Tabler-MIT.txt) 及图标目录。
- OliveTin 标识沿用上游部署素材，用于说明项目来源，不表示官方背书。

本仓库保留第三方资源各自的许可与版权声明，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。新增定制源码的独立开源许可证尚未指定；公开仓库不等同于额外授予第三方资源的再许可。
