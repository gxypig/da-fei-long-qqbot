# 更新、备份与回滚

## 更新前

1. 记下 `git rev-parse HEAD`；保存自己对插件源码的改动。
2. 停止本项目：Windows 双击一键结束，Linux用`.venv/bin/python manage.py stop`，Docker用`docker compose stop`。
3. 备份整个 `AstrBot/data` 和 `local`。这里有密钥、数据库、图库、梗记忆和定时状态，不能放进公开 GitHub。等待服务确实停止再复制SQLite文件，避免不一致备份。
4. Docker用数据卷备份方式备份 `bot_data`；不要执行 `down -v`。用 `docker volume ls` 找到实际卷名，再按Docker官方方式导出到本地备份目录。

## 拉取更新

```bash
git status
git pull --ff-only
```

有本地源码修改时先保存补丁/提交到自己的分支；发生冲突先处理，不要强制覆盖。更新Python依赖用项目解释器：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py start
.venv/bin/python manage.py deep
```

Windows将解释器替换为`.venv\Scripts\python.exe`。Docker执行`docker compose up -d --build`，查看日志和deep报告。已有配置不会被 settings.example.json 覆盖。

二次审阅开关在现有插件配置中改，不需要重装环境。群/好友定时目标、原有提示词、图库和记忆属于运行数据，不通过 Git 更新覆盖。

## 回滚

停止服务，切回之前记录的版本或在另一个目录检出旧版本，恢复与之配套的数据备份和依赖，再启动验收。不要在服务运行时覆盖数据库，不要对已有用户分支执行未经确认的破坏性reset。只回滚源码不一定能回滚上游数据库结构升级；升级AstrBot前尤其要保留完整备份。

## 维护者发布更新

每次发布同步维护 [CHANGELOG.md](CHANGELOG.md)：概述新增功能、问题修复和必要的升级提醒，细小内部改动可以合并。未发布的内容先放“待发布”；核实GitHub源码后填写发布日期与提交链接，不将开发中或未验证的功能写成已发布。

1. 在独立目录测试，保留来源、许可证和本地修改说明。
2. 检查公开树没有`.env`、settings、运行config、数据库、日志、图片、cookie或账号信息。
3. `python tools/release_check.py --write-manifest`，然后`python tools/release_check.py`。
4. 运行测试。`git diff --check`，查看待提交差异。
5. `git add`已核对的源码文件，`git commit`描述具体变化，`git push`到自己的仓库。
6. 核对GitHub展示的commit与本地一致，再发布说明。没有成功推送时不能说“已经同步”。

初次Git身份可使用GitHub账号设置里提供的noreply邮箱，避免把私人邮箱写入公开提交。登录GitHub网页不一定等于命令行已经登录；需要时由账号所有者完成官方Git凭据工具登录授权，或使用GitHub网页上传。不要从浏览器提取cookie/token。

不建议把“整个运行目录自动上传”作为备份。自动化同步只能针对经过审核的源码清单，密钥和聊天数据始终另行备份。
