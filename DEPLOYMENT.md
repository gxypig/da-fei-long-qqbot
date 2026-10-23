# 部署细节与故障排查

本说明对应本仓库固定的 AstrBot 4.29.1、主动插件 1.6.0、图库插件 2.8.6 本地修改版。上游文档：[AstrBot Docker](https://docs.astrbot.app/deploy/astrbot/docker.html)、[AstrBot 源码](https://github.com/AstrBotDevs/AstrBot)、[OneBot 接入](https://docs.astrbot.app/deploy/platform/aiocqhttp/)。升级前先看维护文档，不要直接覆盖运行数据。

## 一、链路与边界

```text
QQ 客户端/登录账号
  ↓ 独立的 QQ 连接端（支持 OneBot v11）
  ↓ 反向 WebSocket + Token
AstrBot :6199/ws
  ├─ 对话模型 API（由部署者提供）
  ├─ 图片理解模型 / 图片输出能力（由部署者提供）
  ├─ 搜索 API → 公共梗记忆
  └─ 可选发送前 AI 二次审阅 → 原 QQ 会话
AstrBot :6185 → 管理页面、图库、定时分类、健康检查
```

本仓库没有 Codex 反代，也不要求部署者登录作者用过的模型产品。标准 OneBot v11 没有统一的 QQ 空间发布接口；本插件调用 `send_qzone_msg` 扩展。**接入端不实现此扩展时，群聊/私聊照常使用，动态功能不可用，应关闭动态开关。** 不得把不支持接口误诊为整个 AstrBot 坏了。

### SnowLuma

作者本机采用 SnowLuma 2.1.0。其 [源码许可](https://github.com/SnowLuma/SnowLuma/blob/main/LICENSE) 与 [EULA](https://github.com/SnowLuma/SnowLuma/blob/main/EULA.md) 对专有组件、自动化部署和再分发有专门限制。本仓库不代为接受许可，不打包/修改/自动下载安装这些组件。需要使用时，由部署者自行阅读条款、取得所需授权并按照 [官方项目](https://github.com/SnowLuma/SnowLuma) 和 [发行页](https://github.com/SnowLuma/SnowLuma/releases) 完成安装，然后手动填写 OneBot 连接信息。

连接端选型必须确认：运行系统、QQ 登录支持、OneBot v11 反向连接、图片收发、好友与群列表，以及需要时的空间扩展。不要因为兼容聊天接口就假定它支持所有扩展。

## 二、Linux 主机直接部署

建议从有受支持 Python 3.12 的 64 位 Linux 开始。以下命令以 Ubuntu 24.04 系列为例；其他系统使用对应的包管理器。资源至少能运行 Python 服务和依赖；QQ 连接端是否同机运行会显著影响资源需求。

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git ca-certificates libgomp1
git clone https://github.com/gxypig/da-fei-long-qqbot.git
cd da-fei-long-qqbot
python3.12 bootstrap.py
```

填写新生成的 `local/settings.json`，然后：

```bash
python3.12 bootstrap.py --check
python3.12 bootstrap.py --install
.venv/bin/python manage.py start
.venv/bin/python manage.py deep
```

这时只完成 AstrBot，不代表 QQ 已登录。先连接 OneBot，再按验收表验证。程序监听回环地址，不要为了方便改成对所有公网地址开放。

从自己电脑建立 SSH 隧道访问云端控制台：

```bash
ssh -L 6185:127.0.0.1:6185 your-user@your-server
```

本机浏览器打开 `http://127.0.0.1:6185`。如果本机已有 AstrBot 占用 6185，用 `-L 16185:127.0.0.1:6185`，浏览器访问 16185。

## 三、QQ 在 Windows，AstrBot 在云端

这是保留现有 Windows QQ 接入端、提前迁移 AstrBot 的实用方案。QQ 电脑必须保持开机、登录和联网。SSH 隧道由部署者在 QQ 所在电脑建立：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 16199:127.0.0.1:6199 your-user@your-server
```

QQ 端反向连接改为 `ws://127.0.0.1:16199/ws`，token 与云端匹配。16199 只监听本机；云服务器无需对公网开放 6199。不要把同一 QQ 端同时连向旧、新两个 AstrBot，否则可能重复回复。切换前先停旧服务，保留数据备份，验证新链路后再清理。

持续运行需要自己配置 SSH 连接恢复或使用自己控制的安全内网。隧道断开是网络链路故障，不应循环重装 QQ 或删除账号数据。私有 VPN 也可使用，但应限定来源和端口。

## 四、Docker

先按 [Docker 官方文档](https://docs.docker.com/engine/install/) 安装 Docker Engine 与 Compose。仓库不会静默替你接受软件许可或改系统安全设置。

```bash
git clone https://github.com/gxypig/da-fei-long-qqbot.git
cd da-fei-long-qqbot
mkdir -p local
cp settings.example.json local/settings.json
# 使用本地编辑器填写 local/settings.json
docker compose build
docker compose up -d
docker compose logs --tail=100 astrbot
docker compose exec astrbot python manage.py deep
```

镜像以 Python 3.12 构建，从 PyPI 安装固定 AstrBot 版本，不包含 QQ/SnowLuma 或模型反代。第一次启动根据设置生成配置，两插件随镜像装入持久卷。后续启动不会重置运行配置。更新镜像时会更新仓库自带的插件源码，因此改过插件的人应先备份并保存自己的补丁。

Compose 仅把 6185、6199 映射到宿主机回环地址；容器内部监听 0.0.0.0 是为了端口映射，不等于把端口开放到公网。QQ 端在同一宿主机时连接宿主 `127.0.0.1:6199/ws`；QQ 在另一台电脑时使用上一节隧道。

**容器里的 `127.0.0.1` 是容器本身。** 若 API 在宿主机本地，不能直接填宿主回环地址；需要选择容器可访问的私网地址及受限防火墙规则，或 Docker 提供的宿主网关机制。远程 HTTPS API 不受此问题影响。

```bash
docker compose stop
docker compose start
docker compose down
```

`down` 不加 `-v` 保留命名卷。**不要运行 `down -v`，它会删除聊天记录、配置和图库所在的数据卷。** 用 Docker 的 stop/start 管服务，避免在停止后又在容器内运行 start 产生多个守护。

本次发布在 Windows 验证源码与配置生成；Docker 文件提供可执行部署路径，未将未经实际运行的云端镜像宣称为实测成功。部署 agent 必须在目标机器实际构建、检查日志并完成验收。

## 五、验收：不要只看端口亮了

| 项目 | 正常现象 | 检查方式 |
| --- | --- | --- |
| 面板鉴权 | 正确密码可登录；未登录 API 拒绝访问 | 浏览器登录，退出后不能读取设置 |
| 插件 | 两个插件加载，无 ImportError | 插件页面、`logs/astrbot.log` |
| QQ 链路 | OneBot 在线且 `good=true` | 控制面板健康检查 |
| 模型 | 实际返回一小段文本 | 深度检查；有计费 |
| 私聊 | 普通消息正常回复 | 由部署者自己的 QQ 测试 |
| 群唤醒 | @、句首/句中/句尾唤醒都进入回复流程 | 只在授权测试群发送 |
| 概率群 | 选中群受概率控制，其他群原规则 | 先用测试群和可控概率 |
| 固定消息 | 所有勾选对象都有各自状态和下次时间 | 测试对象设 5 分钟，确认后改回目标间隔 |
| 固定动态 | 使用独立间隔、只在支持扩展时成功 | 先仅自己可见；测试发布需授权 |
| 图库 | 上传图片出现细分标签，可查找现有图片 | 使用有权上传的测试图 |
| 生图 | 当前模型真正返回图片，仍发回同一会话 | 文本模型不具备此能力时明确标记不支持 |
| 梗记忆 | 有搜索来源才写入知识记忆 | 搜索 key 就绪，查看梗记忆状态 |
| 可选审阅 | 开启时改写/阻断；关闭时不增加审阅调用 | `review_preview` 仅预览不发送 |

## 六、故障表

| 症状 | 优先检查 | 自动修复边界 |
| --- | --- | --- |
| 提示管理员权限不足 | `admins_id` 是否填个人 QQ；用该 QQ 发指令 | 不从群内消息猜测管理员 |
| 设 30 分钟却不发 | 固定分类是否开启、勾选、渠道允许；是否免打扰 | 原 AI 自由模式可不发，固定模式才逐个执行 |
| 第一人收到，第二人没收到 | 固定状态是否失败/等待，API 耗时、QQ 是否允许私聊 | 每个目标独立重试，不共用名额 |
| 401/403 | API key、base、模型权限 | 不重启外部服务来“修复”无效 key |
| 429 | 额度、供应商限速、并发 | 等待/调整间隔/补足额度，不能靠重启解决 |
| 超时 | 出网、DNS、API 可用性、模型生成耗时 | 诊断并有限重试，避免重复发送 |
| 面板空白 | 强制刷新、AstrBot 版本是否匹配 | 仅补丁匹配的 4.29.1 页面；未知构建不强改 |
| 插件恢复成上游版 | 在插件市场更新覆盖了本地修改 | 备份后恢复本仓库源码并重载 |
| QQ 已登录却没消息 | 反向 WS URL/端口/token、是否连到了错误实例 | 先修连接配置，不删登录数据 |
| 容器连不到 API | 是否把宿主127.0.0.1填给容器 | 改成容器可达地址，保持访问限制 |
| 新梗查不到 | 搜索 key、当前搜索服务、网络 | 不把猜测永久当成已核实知识 |
| 动态报不支持 | 接入端是否实现 `send_qzone_msg` | 关闭动态，保留其他渠道 |

守护只管理本项目自己启动的 AstrBot。进程退出/本地控制接口连续不可达时有限恢复（每小时最多三次）；外部 API、QQ 二维码、协议限制、欠费、磁盘损坏等必须按报告处理。任何“无论发生什么都能自动修好”的承诺都不可靠。
