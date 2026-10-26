# WorkBuddy「Buddy 加油站」每日自动签到

一个零第三方依赖的 Python 脚本，配合 GitHub Actions 定时任务，**每天自动领取 WorkBuddy 加油站的免费积分**，本地电脑关机也不受影响。支持 `Bearer token` 与 `Cookie` 两种凭证；邮件告警可选，删掉对应 Secret 即可静音。

> **2026-10 更新**：新版 WorkBuddy 客户端已把登录态改为加密存储（AES-GCM + ACL 校验），本 README 早期版本写的「从 `workbuddy-desktop.info` 提取明文 token」已失效。领取凭证请改用下面的**浏览器 F12 方式**。

---

## ⚠️ 先读这一段：合规与风险

1. **接口是私有未公开接口**。本脚本调用的是 WorkBuddy 客户端内部使用的 HTTP 接口，官方没有公开文档，也没有任何稳定性承诺。官方随时可能改名、加校验或下线，届时脚本会失效（会发邮件告诉你）。
2. **请自行核实是否符合服务条款**。自动化签到可能不符合 WorkBuddy 的《服务条款》或加油站活动规则。使用前请你自己确认，**风险与后果由你自行承担**。
3. **严格遵守单账号、低频、串行**。本脚本设计就是：单账号、每天 1 次、串行请求、请求间隔 ≥1 秒、失败最多重试 3 次。**请勿改成多账号、并发、批量领取**，那会显著提高被风控的概率。
4. **凭证等同账号密码**。`accessToken` 可以完全代表你的账号身份，只存放在 GitHub Secrets 或本地 `config.json`（已被 `.gitignore` 忽略），**绝不要提交进版本库、不要发给别人、不要贴进日志**。
5. **频率控制参数是硬性约定**，`MAX_RETRIES`、`GAP_BETWEEN_CALLS` 不要调高。
6. **凭证有有效期，且会被客户端"轮换失效"**。实测 access token 有效期约 60 天，但**客户端每次自动刷新登录态都会把旧 token 在服务端吊销**——即使 JWT 还没到期也会返回 401。脚本会自动尝试 `/v2/plugin/auth/token/refresh` 续期（需要有效的 refresh token）；若续期失败，就需要重新抓一次凭证。

---

## 工作原理

已在本机实测确认（WorkBuddy 客户端 v5.3.x）：

| 项目 | 值 |
|---|---|
| 域名 | `https://www.workbuddy.cn`（取自登录态文件的 `auth.domain`） |
| 查询状态 | `POST /v2/billing/meter/checkin-activity-status` |
| 执行领取 | `POST /v2/billing/meter/daily-checkin` |
| 鉴权 | Header `Authorization: Bearer <accessToken>` |
| 已领取返回 | `code: 10001`（"今天已签到，请明天再来"）→ 按幂等跳过，不算失败 |

执行流程：**查状态 → 若 `today_checked_in == false` 则领取 → 再查一次状态得到领取后的积分**。已领取时不会发起领取请求。

---

## 文件结构

```
wb-checkin/
├─ checkin.py                          # 主程序（Python 3 标准库，零依赖）
├─ config.example.json                 # 本地配置示例
├─ requirements.txt                    # 依赖说明（无第三方依赖）
├─ .gitignore                          # 忽略 config.json / state/ / *.log
├─ README.md                           # 本文档
└─ .github/workflows/daily-checkin.yml # GitHub Actions 定时任务
```

---

## 第一步：获取凭证（浏览器 F12，约 1 分钟）

### 为什么不用本机登录态文件了

旧版客户端把 token 明文存在
`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`，
可以直接读出来。但新版改成了：

```json
"accessToken": { "$wbEncrypted": 1, "envelope": "eyJzdWl0ZSI6..." }
```

`envelope` 解出来是 `{"suite":1,"keyId":"...","nonce":"...","ciphertext":"..."}`，密钥由客户端内部持有（`oauth-master-key`）并且配套 ACL 文件权限校验（`verifyFileAcl` / `tryRepairAcl`），第三方脚本读不到。所以**改走浏览器这条路，反而更简单也更稳**。

### 操作步骤（Edge / Chrome 通用）

1. 打开 **https://www.workbuddy.cn** 并登录（微信扫码或手机号）。
2. 按 **F12** 打开开发者工具 → 切到 **网络 / Network** 面板。
3. 勾上 **保留日志 / Preserve log**；筛选框输入 `v2`。
4. 在页面上点进**加油站**（或刷新页面），触发几条 XHR 请求。
5. 在请求列表里点任意一条 `v2/` 开头的请求（最好就是 `checkin-activity-status`）。
6. 右侧 → **标头 / Headers** → 往下滚动到 **请求标头 / Request Headers**。
7. 复制其中一项（**右键 → Copy value**，别手动拖选，容易截断）：

   | 看到的字段 | 复制的内容 | 对应 Secret |
   |---|---|---|
   | `Authorization: Bearer eyJhbGciOiJSUzI1NiIs...` | **只复制 `Bearer ` 后面那一长串** | `WB_ACCESS_TOKEN`（推荐） |
   | 没有 Authorization，只有 `Cookie: abc=1; def=2; ...` | 整段 Cookie 值（不含 `Cookie: `） | `WB_COOKIE` |

   两者二选一即可，脚本会优先用 `WB_ACCESS_TOKEN`，没有时自动回退到 `WB_COOKIE`。

> ⚠️ token 通常有几百到上千字符，**务必复制完整**，截断会导致 401。
> ⚠️ 全程不要把凭证打印到终端、贴进聊天窗口或发给别人。

### 顺手测一下（本机即可，只查不领）

```bash
cd wb-checkin
export WB_ACCESS_TOKEN='粘贴你复制的那串'      # 或用 WB_COOKIE='...'
python checkin.py --check-only                 # 只查状态，绝不发起领取请求
```

看到 `[INFO] 今日是否已领取：...` 就说明凭证有效、接口连通。

---

## 第二步：本地验证（只查不领，安全）

```bash
cd wb-checkin
cp config.example.json config.json      # 或直接用环境变量（推荐，不留文件）

# Bearer 凭证
export WB_ACCESS_TOKEN='粘贴 access token'

# 或者 Cookie 凭证（二选一）
export WB_COOKIE='粘贴 cookie'

python checkin.py --check-only          # 只查状态，绝不发起领取请求
python checkin.py --dry-run             # 走完整流程但不发领取请求
```

正常会看到类似输出：

```
2026-09-09 10:06:51 [INFO] access token 剩余有效期：59 天（到期 2026-11-08 08:43）
2026-09-09 10:06:51 [INFO] 活动：第 8 期「开学季」| 有效期 2026-09-01 ~ 2026-09-15
2026-09-09 10:06:51 [INFO] 今日是否已领取：是 | 连续天数：6 | 本期累计：600 积分
2026-09-09 10:06:51 [INFO] 今天已领取，跳过（幂等，不重复领取）
2026-09-09 10:06:51 结果=skipped 动作=skip_already_signed 积分 600 → 600（无变化）连续 6 天
RESULT_JSON {"ts":"...","result":"skipped","action":"skip_already_signed", ...}
```

确认接口连通后，再执行真实签到：`python checkin.py`（首次建议手动跑一次验证）。

---

## 第三步：部署到 GitHub Actions

### 3.1 创建仓库并推送

```bash
cd wb-checkin
git init
git add .
git commit -m "feat: WorkBuddy 加油站每日自动签到"
git branch -M main
git remote add origin https://github.com/<你的用户名>/wb-checkin.git
git push -u origin main
```

> 建议设为 **Private** 仓库。确认 `config.json` 和 `state/` **没有**被提交（`.gitignore` 已排除，可用 `git status` 复查）。

### 3.2 配置 Secrets

进入仓库 → **Settings → Secrets and variables → Actions → New repository secret**，逐个添加：

| Secret 名 | 必填 | 说明 |
|---|---|---|
| `WB_ACCESS_TOKEN` | ✅（二选一） | 第一步复制的 Bearer token（不含 `Bearer ` 前缀） |
| `WB_COOKIE` | ✅（二选一） | 第一步复制的 Cookie 值，没有 Bearer 时使用 |
| `WB_REFRESH_TOKEN` | ⬜ | refresh token。**强烈建议提供**——有它脚本才能调 `/v2/plugin/auth/token/refresh` 自动续期 |
| `WB_DOMAIN` | ⬜ | 接口域名，默认 `www.workbuddy.cn`，一般不用填 |
| `SMTP_HOST` | ⬜ | 邮件服务器，如 `smtp.qq.com` / `smtp.163.com` |
| `SMTP_PORT` | ⬜ | `465`（SSL）或 `587`（STARTTLS），默认 465 |
| `SMTP_USER` | ⬜ | 发信邮箱完整地址 |
| `SMTP_PASS` | ⬜ | **邮箱授权码**（不是登录密码） |
| `ALERT_TO` | ⬜ | 收件邮箱，可与发信邮箱相同 |

**邮件告警是完全可选的。** 只要你把 `SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / ALERT_TO` 这 5 个 Secret 全部删掉，脚本就会彻底静音 —— 代码里的 `mail_ready` 判断任一字段缺失即跳过发信，只在日志里留一行 `邮件未配置完整…跳过发信`。**不用改代码、不用重新推送。**

此时仍有兜底：失败时脚本以非零码退出，GitHub 工作流标红并会通知你的 GitHub 账号邮箱。

如果哪天想恢复邮件，把这 5 个 Secret 补回去即可。
- **QQ 邮箱**：设置 → 账号与安全 → POP3/IMAP/SMTP 服务 → **先「开启服务」再「生成授权码」**（两个按钮是分开的），得到 16 位字母。免费，不是订阅。
- **163 邮箱**：设置 → POP3/SMTP/IMAP → 开启 SMTP → 获取授权码。

### 3.3 启用定时任务

工作流文件已内置定时规则：

```yaml
- cron: '0 1 * * *'   # UTC 01:00 = 北京时间 09:00（主触发）
- cron: '0 4 * * *'   # UTC 04:00 = 北京时间 12:00（兜底）
```

⚠️ **GitHub Actions 的 cron 用 UTC，北京时间要减 8 小时**。想改成北京时间 08:00 就写 `0 0 * * *`。

推送后到 **Actions** 页面，选 `WorkBuddy 每日签到` → **Run workflow** 手动跑一次验证全链路。

---

## 第四步：日常运维

- **看结果**：Actions → 每次运行的日志里有 `结果=success 动作=claimed 积分 600 → 700（+100）连续 7 天` 这样一行，以及一行 `RESULT_JSON`（适合被其他程序解析）。
- **结果含义**：
  - `success / claimed` —— 本次领取成功
  - `skipped / skip_already_signed` —— 今天已领取，跳过（正常，不发邮件）
  - `error` —— 失败，工作流标红（配了邮件才会额外发信）
- **改时间**：编辑 `.github/workflows/daily-checkin.yml` 里的 cron，提交后自动生效。

### 凭证有效期与续期（重要）

实测结论：

- access token 有效期 **60 天**（网页端签发约 28 天）。
- **真正会导致失效的不是到期，而是客户端的自动轮换**：客户端每次刷新登录态，都会把旧 access token 在服务端吊销。所以会出现"JWT 显示还剩 12 天、接口却返回 401"的情况。2026-10-24 那次连续失败就是这个原因。
- **自动续期接口是存在的，路径是 `/v2/plugin/auth/token/refresh`**（网关前缀 `/plugin`，之前用 `/v2/auth/token/refresh` 才会 404）。请求方式：
  - `POST /v2/plugin/auth/token/refresh`，body `{}`
  - 请求头：`X-Refresh-Token: <refreshToken>`、`X-Auth-Refresh-Source: plugin`
  - **不需要**有效的 `Authorization`（刷新时 access 通常已失效）
  - 已实测该路由存在：用伪 refresh token 请求返回 `401 {"code":12153,"msg":"refresh token failed..."}`，而不是 404。
- 因此：**只要能拿到一个有效的 `refreshToken`，脚本就能长期自动续期**。刷新成功后新 token 会写入 `state/token.json`，Actions 用 `actions/cache` 持久化。
- 但请注意：**客户端自己也在刷新并轮换 refresh token**。如果客户端先刷新，你手里的 refresh token 就会失效。所以 refresh token 不是"永久"的，只是能显著拉长周期。
- 兜底策略不变：一旦 401 且刷新失败，就重新跑「第一步」抓一次凭证，更新 `WB_ACCESS_TOKEN` / `WB_REFRESH_TOKEN`。

### 提醒：凭证临期时脚本的行为

`Bearer` 凭证脚本能自己解析 JWT 的 exp，剩余不足 **21 天**会在日志里给出 WARN 并通过邮件提醒（若配置了邮件）。`Cookie` 凭证无法本地预判有效期，**只有服务端返回 401/403 时才会暴露** —— 所以用 Cookie 的话更要留意 Actions 的运行结果。

> 可选增强：如果你本机装了 `gh` CLI，可以加一个 Windows 计划任务，在开机时自动执行
> `gh secret set WB_ACCESS_TOKEN --repo <user>/wb-checkin --body-file <token文件>`
> 让本机客户端刷新后的 token 自动同步到 GitHub，进一步减少人工介入。

### GitHub Actions 的两个坑

1. **定时可能延迟**：GitHub 不保证准时，高负载时可能延迟几分钟到几小时（所以加了 12:00 的兜底触发）。
2. **仓库 60 天无活动会停用定时工作流**：只要偶尔 push 一次或手动 Run workflow 就不会触发。

---

## 其他运行环境

脚本只依赖 Python 3.7+ 标准库，任何常在线的环境都能跑：

```bash
# 云服务器 crontab：每天 09:00（北京时间，注意服务器时区）
0 9 * * * cd /opt/wb-checkin && /usr/bin/python3 checkin.py >> /var/log/wb-checkin.log 2>&1
```

- 云函数（腾讯云 SCF / 阿里云 FC）：把 `checkin.py` 作为入口，配置定时触发器，凭证走环境变量即可。
- 有持久磁盘时，脚本会把续期后的 token 写到 `state/token.json`（可用 `WB_STATE_FILE` 改路径）。

---

## 常见问题

**Q：显示 `skip_already_signed`，是不是没成功？**
A：不是。说明今天已经领过了（手动领或脚本领都算），脚本按幂等保护跳过，不会重复领取。

**Q：HTTP 404？**
A：域名错了。必须用 `www.workbuddy.cn`，网上有些文章写的 `copilot.tencent.com` 会 404。

**Q：邮件怎么彻底关掉？**
A：删掉 5 个 SMTP Secret 就行（Settings → Secrets）。代码不用改，也不用重新推送。此时工作流失败仍会让 GitHub 通知你的 GitHub 账号邮箱。

**Q：邮件没收到？**
A：四个邮件变量（`SMTP_HOST`/`SMTP_USER`/`SMTP_PASS`/`ALERT_TO`）必须全部存在才会发信；只在失败或有凭证告警时发。想测试通道可以用 `--notify-on-success` 强制每次都发。

**Q：鉴权失败（HTTP 401）？**
A：凭证过期。重跑「第一步」用浏览器 F12 重新取一次，更新 `WB_ACCESS_TOKEN` 或 `WB_COOKIE`。

**Q：F12 里找不到 Authorization？**
A：用 Cookie 也行，脚本两种都支持。把 `Cookie:` 那一行的值整个复制，填到 `WB_COOKIE` 即可。

**Q：本机 `workbuddy-desktop.info` 里的 token 还能用吗？**
A：新版客户端已把它改成加密存储（`$wbEncrypted` + `envelope`），第三方读不出来。这也是为什么 README 改用浏览器 F12 这条路。

**Q：接口哪天突然失败了？**
A：先本地 `python checkin.py --check-only --verbose` 看原始返回，多半是接口或字段变了，按新返回调整 `checkin.py` 里的解析即可。
