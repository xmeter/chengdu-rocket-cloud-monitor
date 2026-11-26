# 🚀 成都区域“火箭云”多源自动监测与邮件预警系统

结合 **Next Spaceflight**、**The Space Devs (Launch Library 2)** 与 **RocketLaunch.Live** 等全球航天发射数据库，通过三维天体几何模型实时推演每一次中国及周边火箭发射是否可能在**成都及四川盆地**产生“**火箭云（暮光水母云 / Twilight Plume）**”，并在发现新的潜在可见窗口时通过 **GitHub Actions + 电子邮件**自动向你发送观测预警（电脑关机也可全天候运行，支持自动去重与窗口变更重提醒）。

---

## ✨ 核心特性

1. **多源数据交叉融合（零付费 API 依赖）**
   - **Launch Library 2 API (`thespacedevs.com`)**：获取精确到分钟/秒的 `NET` 点火时间、工位经纬度、任务轨道（SSO / GTO / LEO / 空间站）及时间精度标识（自动过滤仅有日期的 `00:00 UTC` 占位符，杜绝误报）。
   - **Next Spaceflight (`nextspaceflight.com/launches/`)**：直接解析网页卡片，第一时间捕获根据航警（NOTAM）更新的国内商业与国家队发射窗口。
   - **RocketLaunch.Live API**：补充近期临近发射窗口交叉校验。
   - **跨源去重合并**：同一枚火箭在多个网站同时出现时自动合并为一条记录。

2. **真实三维天体光学与地球曲率推演（成都基准：`30.66°N, 104.06°E`）**
   - **高精度太阳高度角计算**：基于 Jean Meeus 天文算法（精度 `~0.01°`），实时计算发射后 `T+90s ~ T+480s` 成都地面太阳高度角与火箭轨迹星下点太阳高度角。
   - **三维羽流受光条件**：考虑地球曲率与 `12 km` 对流层浓密云雾遮光高度，精确计算 `40 ~ 260 km` 高空火箭尾迹是否暴露在阳光直射下。
   - **四大发射场射向走廊与成都视线角/方位角推演**：
     - **西昌卫星发射中心**（距成都 ~335 km）：东南 GTO 航向 / 正南 SSO 航向，成都最高仰角可达 `15°~35°`，极易出现震撼满天的大型水母云。
     - **酒泉卫星发射中心**（距成都 ~1190 km）：东南空间站/近地航向（向陕甘川飞近）或西南偏南 SSO 航向，计算中段高空羽流在成都偏北/西北天空的地平仰角。
     - **太原卫星发射中心**（距成都 ~1140 km）：南偏西 SSO 航向（向晋陕南下），计算成都东北天空仰角。
     - **文昌 / 海阳等远距发射场**：自动评估地球曲率遮挡，仅在超高空二级羽流越过成都地平线（`>= 1.5°`）且受光时标记为极限目击。
   - **三级概率评级**：`高概率`、`中等概率`、`低概率（极限异地目击）`。

3. **智能去重与改期重提醒**
   - 已发送预警的任务会持久化记录在 [`data/notified_state.json`](data/notified_state.json) 中，后续定时巡检**绝不重复轰炸**。
   - 若某次已通知的发射任务**推迟或提前超过 30 分钟**且新窗口依然能产生火箭云，系统会自动发送带 `【窗口更新】` 标记的提醒邮件。

4. **零第三方依赖纯标准库实现**
   - 仅依赖 Python 标准库，GitHub Actions 冷启动仅需约 3 秒，无任何依赖过期风险。

---

## 🛠️ 三步部署到 GitHub Actions（全自动云端运行）

### 第一步：推送代码到你的 GitHub 仓库

建议创建一个**公开仓库**（GitHub 免费版对公开仓库提供无限 Actions 分钟数；私有仓库每月也有 2000 分钟免费额度，本项目每月仅消耗约 6 分钟）：

```bash
git init
git add .
git commit -m "feat: initial Chengdu rocket cloud monitor"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<你的仓库名>.git
git push -u origin main
```

### 第二步：在 GitHub 仓库中添加邮箱 Secrets

打开你的 GitHub 仓库页面，依次点击 **`Settings`** → **`Secrets and variables`** → **`Actions`** → **`New repository secret`**，添加以下 5 个必填项：

| Secret 名称 | 说明 | 示例（以 QQ 邮箱为例） |
| :--- | :--- | :--- |
| `SMTP_HOST` | 发件邮箱 SMTP 服务器地址 | `smtp.qq.com`（163 填 `smtp.163.com`，Gmail 填 `smtp.gmail.com`） |
| `SMTP_PORT` | SMTP 端口（465 自动启用 SSL，587 自动启用 STARTTLS） | `465` |
| `SMTP_USER` | 发件邮箱完整账号 | `12345678@qq.com` |
| `SMTP_PASS` | 邮箱 **SMTP 授权码 / App Password**（注意不是网页登录密码） | `abcdefghijklmnop` |
| `NOTIFY_EMAIL_TO` | 接收预警通知的邮箱（多个收件人用英文逗号 `,` 分隔） | `me@example.com` |

> **如何获取邮箱授权码？**
> - **QQ 邮箱**：网页登录 QQ 邮箱 → 顶部「设置」→「账户」→ 往下找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务」→ 开启服务并点击「生成授权码」。
> - **163 邮箱**：网页登录 →「设置」→「POP3/SMTP/IMAP」→ 开启「IMAP/SMTP服务」→ 新增授权密码。
> - **Gmail**：开启 Google 账号两步验证后，访问 *Google Account -> Security -> App passwords* 生成 16 位专用密码。

### 第三步：一键发送模拟测试邮件验证通道

1. 进入 GitHub 仓库顶部的 **`Actions`** 标签页。
2. 在左侧点击 **`Chengdu Rocket Cloud Monitor`**。
3. 点击右侧 **`Run workflow`** 按钮：
   - 勾选 ✅ **`发送一封西昌曙光模拟火箭云测试邮件`** (`simulate_test_email`)
   - 点击绿色的 **`Run workflow`**。
4. 等待约 10 秒运行变绿，你的邮箱就会收到一封排版精美的模拟火箭云观测预警邮件！此后工作流将**每 6 小时**自动巡检一次。

---

## 💻 本地命令行调试

无需安装任何第三方库，在本地终端可直接运行：

```bash
# 1. 抓取真实全球发射数据，评估近期中国及周边发射计划（仅终端打印，不发邮件）
python3 monitor.py --dry-run

# 2. 预览模拟西昌曙光火箭云的完整评估报告与邮件正文（不实际发信）
python3 monitor.py --simulate --dry-run

# 3. 运行全部单元测试
python3 -m unittest discover -s tests -v
```
