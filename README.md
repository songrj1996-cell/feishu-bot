# 飞书调研分析 Bot — 使用与运维手册

> 这份文档面向**不懂代码、不懂服务器**的同事写。每个步骤都是「打开什么、点什么、敲什么命令」，照做就行。看到不懂的术语先往下读，文末有名词解释。

---

## 一、这个 Bot 是干嘛的

收到飞书表格链接 → 自动读表 → 通过 Dify 生成分析报告 → 输出成飞书文档发给你。

**用法（在飞书里和机器人聊天）：**

| 你发什么 | 机器人做什么 |
|---|---|
| `/help` | 显示用法说明 |
| `/ping` | 测试机器人在不在线，会回 `pong` |
| `/reset` | 清掉当前对话上下文，重新开始 |
| `/调研分析 <飞书表格链接>` | 读取表格 → 询问澄清问题（卡片）→ 你回答 → 生成调研报告（飞书文档） |

> 如果想读表格中的某个特定 sheet，复制飞书表格链接时确保 URL 里带 `?sheet=xxx`，机器人会自动识别。

**多个分析并发使用时**：
- 同一个人可以同时跑多个 `/调研分析`
- 如果机器人问了多个澄清问题，**回答时必须用飞书的「回复」功能**针对具体那条卡片回答，否则机器人不知道你回答的是哪一个分析

---

## 二、整体架构（看不懂可以跳过）

```
你的飞书 ──消息──▶ 飞书服务器
                       │
                       │  长连接 WebSocket
                       │  (公司内网服务器主动连出去)
                       ▼
              [公司内网 Linux 服务器]
              用户：nan1
              路径：/home/nan1/feishu-bot
              进程守护：systemd
                       │
                       ├─▶ 飞书 API（发消息、建文档）
                       └─▶ Dify API（调研分析、报告生成）
```

**关键点：**
- 内网服务器**不暴露任何端口**，只主动出向连飞书
- 不需要域名、HTTPS 证书、ngrok
- 服务器进程崩了 systemd 自动重启，开机自启

---

## 三、项目文件结构

```
feishu-bot/
├── main.py              # 主入口：长连接客户端 + 路由 + 业务编排
├── feishu.py            # 飞书 API 调用：发消息、查用户名
├── feishu_docs.py       # 飞书文档创建：上传 md → 转 docx → 转移所有权
├── feishu_sheets.py     # 飞书表格读取：识别链接 + 拉数据 → markdown
├── dify.py              # 调用 Dify Chat API（流式）
├── commands.py          # 指令定义（/help、/ping、/调研分析 等）
├── config.py            # 环境变量加载
├── requirements.txt     # Python 依赖列表
├── .env                 # 密钥配置（不进 git，每台机器各自维护）
├── .env.example         # .env 模板
└── README.md            # 这份文档
```

---

## 四、服务器登录步骤

> 公司用的是**Web 终端**（浏览器里直接打开命令行）。

1. 打开公司的服务器管理页面（具体地址问你同事 / IT）
2. 找到这台 Ubuntu 服务器（IP 通常是 `192.168.40.xxx` 这种内网 IP）
3. 点击连接，浏览器会打开一个**黑底白字的命令行界面**
4. 默认登录的是 `root` 用户（提示符是 `root@MTSub-192:~#`）

**两个常用身份：**

- **`root`**：管理员，能改系统配置（比如改 systemd 服务文件、装包）
- **`nan1`**：跑机器人的专用用户，机器人代码就在这个用户的家目录 `/home/nan1/`

**身份切换：**

```bash
# 从 root 切到 nan1（不需要密码，因为 root 权力最大）
su - nan1

# 从 nan1 退回 root
exit
```

切换后看提示符就知道自己当前是谁：
- `root@MTSub-192:~#` ← 你是 root
- `nan1@MTSub-192:~$` ← 你是 nan1

---

## 五、日常运维命令（最常用）

> **以下命令全部在 root 用户下跑**（如果你登进去就是 root，那就直接跑；如果是 nan1，先 `exit` 回到 root）。

### 5.1 查服务状态

```bash
systemctl status feishu-bot
```

**看什么：**
- `Active: active (running)` 绿色 = 服务在跑 ✅
- `Active: failed` 红色 = 服务挂了 ❌（往下翻看错误，或用 `journalctl` 查日志）
- `Active: inactive (dead)` = 服务被停了

### 5.2 看日志

```bash
# 实时跟随日志（推荐！发条消息就能看到 bot 收到了）
journalctl -u feishu-bot -f

# Ctrl+C 退出实时跟随（不会停服务，只是退出查看）

# 看最近 100 行
journalctl -u feishu-bot -n 100 --no-pager

# 看今天的所有日志
journalctl -u feishu-bot --since today

# 看从某个时间点开始的日志
journalctl -u feishu-bot --since "2026-05-19 14:00"
```

**正常的日志长什么样：**

```
[bot] starting long-connection client...
[Lark] [INFO] connected to wss://msg-frontier.feishu.cn/...
[bot] msg | user=Nan1 | reply=N | text='/ping'
[bot] msg | user=Nan1 | reply=N | text='/调研分析 https://...'
[dify] -> POST | conv_id=(new) | query_len=20011
[dify] <- done | chunks=214 | answer_len=599
```

### 5.3 启动 / 停止 / 重启服务

```bash
# 重启（改完代码必须做这一步！）
systemctl restart feishu-bot

# 停服务（不需要时停掉，省资源）
systemctl stop feishu-bot

# 启服务
systemctl start feishu-bot

# 让服务开机自启（已经设过了，不用再做）
systemctl enable feishu-bot

# 取消开机自启
systemctl disable feishu-bot
```

---

## 六、修改代码之后，怎么部署到服务器

> 改代码请在你**本地 Windows** 上做，不要在服务器上直接编辑（容易乱）。

### 6.1 完整流程（手把手版）

#### 第 1 步：本地改完，提交并推到 GitHub

打开你 Windows 上的 PowerShell，进入项目目录：

```powershell
cd C:\Users\admin\Desktop\feishu-bot

# 看看自己改了什么
git status

# 添加所有改动
git add .

# 提交（描述一下改了什么）
git commit -m "改了什么的简短说明"

# 推到 GitHub
git push
```

> 如果是新功能在新分支上，`git push origin 分支名`。日常迭代如果都在 `long-connection` 分支，直接 `git push` 就行。

#### 第 2 步：服务器上拉新代码

打开服务器的 Web 终端，登进去（默认是 root），然后：

```bash
# 切到 nan1 用户（代码所在用户）
su - nan1

# 进项目目录
cd ~/feishu-bot

# 拉最新代码
git pull

# 退回 root
exit
```

#### 第 3 步：重启服务

```bash
systemctl restart feishu-bot
```

#### 第 4 步：验证

```bash
# 看状态：应该是 active (running)
systemctl status feishu-bot

# 看日志：应该看到 connected to wss://
journalctl -u feishu-bot -n 20 --no-pager
```

然后去飞书发条 `/ping`，看机器人能不能回。

### 6.2 如果改了 Python 依赖（动了 requirements.txt）

需要在服务器上重装依赖：

```bash
su - nan1
cd ~/feishu-bot
source .venv/bin/activate
pip install -r requirements.txt
exit
systemctl restart feishu-bot
```

---

## 七、密钥配置（.env）

`.env` 文件在 `/home/nan1/feishu-bot/.env`，**不在 git 里**，每台机器各自维护。

需要的变量：

```ini
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx       # 飞书后台 → 凭证与基础信息 → App ID
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxx     # 飞书后台 → 凭证与基础信息 → App Secret
DIFY_API_BASE=https://api.dify.ai/v1     # Dify API 地址（默认就行，自建 Dify 改这里）
DIFY_API_KEY=app-xxxxxxxxxxxxxxxxx       # Dify 应用的 API Key
```

**改 .env 之后必须重启服务才生效：**

```bash
systemctl restart feishu-bot
```

**不能轻易把 .env 放进 git**——里面是密钥，泄露了别人就能假冒你的机器人 / 用你的 Dify 流量。

---

## 八、常见问题排查

### 8.1 飞书发消息，机器人没反应

按这个顺序检查：

#### 1. 服务在不在跑

```bash
systemctl status feishu-bot
```

不在跑（红色 failed 或 inactive）→ `systemctl start feishu-bot` 启动；如果起不来去看日志。

#### 2. 日志里有没有事件入口

```bash
journalctl -u feishu-bot -f
```

发条消息试试。
- **看到 `[bot] msg | user=...`** → 事件收到了，问题在后续处理（看 traceback）
- **没看到任何动静** → 事件根本没到。继续往下查

#### 3. 长连接是不是连上的

```bash
journalctl -u feishu-bot -n 50 --no-pager | grep wss
```

看到 `connected to wss://` 说明连上了。如果没有，说明长连接挂了，重启服务：

```bash
systemctl restart feishu-bot
```

#### 4. 是不是有第二个 bot 实例在抢事件

飞书规定**同一个 bot app 只能由一个长连接接收事件**——如果同时有两个进程连上飞书（比如你本地 Windows 也跑了 `python main.py` 没关，服务器上又有 systemd 的服务），事件会随机给其中一个，另一个就收不到。

**确认你 Windows 本地的 PowerShell 里没有 `python main.py` 在跑**，关掉所有遗留的本地实例。

#### 5. 飞书后台的事件订阅是不是还正常

打开飞书开放平台 → 你的 bot 应用 → 「事件与回调」（或「事件订阅」）：
- 订阅方式必须是「**使用长连接接收事件**」
- 订阅事件里必须有 `im.message.receive_v1`
- 「版本管理与发布」里**最新版本必须是已发布状态**

如果你最近调过权限、发过新版本，有时候会重置事件订阅配置，需要重新设。

### 8.2 服务器上 `python main.py` 报 ModuleNotFoundError

说明 venv 没激活。两个办法：

```bash
# 办法 1：先激活 venv
source ~/feishu-bot/.venv/bin/activate
python main.py

# 办法 2：直接用 venv 里的 python
~/feishu-bot/.venv/bin/python ~/feishu-bot/main.py
```

> 平时不需要手动跑——systemd 服务里已经写死用 venv 里的 python 跑。

### 8.3 git pull 报错 / 冲突

可能是服务器上有人手动改过文件。粗暴的解决方法：

```bash
su - nan1
cd ~/feishu-bot
git stash         # 把本地改动暂存
git pull          # 拉最新
git stash drop    # 扔掉刚才暂存的（如果不需要）
exit
systemctl restart feishu-bot
```

> 千万别在服务器上手动改代码，永远在本地改 + push。

### 8.4 飞书报"通讯录权限不够"或者机器人显示用户名是一串 ID

机器人需要 `contact:user.base:readonly` 权限来读取用户姓名。去飞书开放平台：

1. 「权限管理」→ 搜 `contact:user.base:readonly` → 申请
2. 「应用可用范围」→ 把要用 bot 的人加进去（或选「全员可用」）
3. 「版本管理与发布」→ 创建新版本 → 提交审核 → 发布
4. 等审核通过后，重启 bot：`systemctl restart feishu-bot`

### 8.5 修改了 Dify 提示词，机器人还是按老的来

Dify 提示词的修改在 Dify 那边的应用里改，**不需要动服务器代码、不需要重启 bot**。在 Dify 后台改完保存即可，下次发请求就生效。

---

## 九、紧急情况：回滚到旧版本

如果新版本有严重 bug，可以临时切回 webhook 版本（在 `main` 分支）。但 webhook 版本需要 ngrok / 公网地址，**不适合在内网服务器跑**——所以"回滚"实际上意味着：

### 9.1 回滚到上一个 commit（最常用）

```bash
su - nan1
cd ~/feishu-bot
git log --oneline -10                    # 找一下要回到哪个 commit
git reset --hard <commit的前7位字符>      # 比如 git reset --hard e49908d
exit
systemctl restart feishu-bot
```

### 9.2 应急关停

```bash
systemctl stop feishu-bot
```

机器人立刻不响应任何消息。修好后 `systemctl start feishu-bot`。

---

## 十、本地开发环境（不用部署，只想本地试）

> 如果你只是改 Dify 提示词不动代码，**用不到本地开发环境**。

### Windows 本地搭建

1. 打开 PowerShell，进项目目录：

```powershell
cd C:\Users\admin\Desktop\feishu-bot
```

2. 创建虚拟环境（只需要一次）：

```powershell
python -m venv .venv
```

3. 激活虚拟环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

激活成功提示符前会有 `(.venv)`。

4. 装依赖：

```powershell
pip install -r requirements.txt
```

5. 准备 `.env`（拷贝服务器上的或者照 `.env.example` 填）

6. 运行：

```powershell
python main.py
```

**重要**：本地跑的时候，**服务器上的 systemd 服务先停掉**（`systemctl stop feishu-bot`），否则两个进程抢飞书事件，结果不可预测。

---

## 十一、名词解释（看不懂术语回这里查）

| 术语 | 解释 |
|---|---|
| **Bot / 机器人** | 飞书里那个能跟你聊天、自动干活的账号 |
| **服务器 / Server** | 一台 24 小时开机、跑 bot 程序的电脑 |
| **systemd** | Linux 系统的「服务管家」，负责把程序跑成后台进程、崩了自动起 |
| **service / 服务** | systemd 管理的一个程序，比如 `feishu-bot` 就是一个 service |
| **长连接 / WebSocket** | 一种网络连接方式，连上之后保持着不断开，对方有消息就推过来 |
| **webhook** | 另一种消息送达方式：对方主动来你这儿敲门（HTTP 请求）。需要你能被外网访问 |
| **journalctl** | Linux 看 systemd 服务日志的命令 |
| **venv** | Python 虚拟环境，把项目的依赖包跟系统的隔离开 |
| **git / GitHub** | 代码版本管理，记录每次改动；GitHub 是远程仓库 |
| **commit** | 一次代码改动的"快照"，可以回滚 |
| **branch / 分支** | 同一份代码的平行版本（比如 `main` / `long-connection`） |
| **push / pull** | push = 把本地改动推到 GitHub；pull = 从 GitHub 拉最新到本地 |
| **.env** | 存密钥/配置的文件，不进 git |
| **Dify** | 一个 AI 应用搭建平台，机器人通过它调 LLM 出报告 |
| **lark-oapi** | 飞书官方的 Python SDK，机器人用它跟飞书通信 |

---

## 十二、有问题找谁

- **代码 / 部署相关**：找写代码的同事
- **飞书后台权限 / 应用配置**：找飞书后台的管理员
- **服务器登录 / 网络问题**：找 IT
- **Dify 提示词效果不好**：自己去 Dify 改（这个不用动代码）

---

> 最后更新：2026-05-19
> 部署版本：long-connection 分支
> 服务器：内网 Ubuntu 22.04，路径 `/home/nan1/feishu-bot`
