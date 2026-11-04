# tanuki-price-peek

> A personal tool that peeks at Nintendo Japan store prices daily and emails you when a target SKU drops below your target price.

这是一个轻便的个人小工具：每天看一眼任天堂日本商店指定商品页的价格，跌破心理价位、缺货或恢复有货时，发一封邮件提醒自己。

不是通用爬虫，也不是商业项目，只是因为最近想入手游戏又想蹲一蹲打折。

> 仅监控公开的商品页，请控制请求频率，每天 1–2 次即可。

## 它是怎么工作的

用 Playwright 打开商品页（会经过任天堂的 Queue-it 排队页），等页面跳转到真实商品页后，从页面内的 JSON-LD 结构化数据中读取各 SKU 的价格和库存，写入本地 SQLite，与上一次记录比较，按规则发邮件。

价格和库存都来自页面本身的结构。**抓取逻辑依赖当前网页结构，任天堂改版后可能失效，不保证长期可用。**

## 环境要求

- Python 3.9+
- 能访问 `store-jp.nintendo.com` 的网络环境
- 一个支持 SMTP 的邮箱（Gmail 需使用应用专用密码，不是登录密码）

## Quick Start

```bash
git clone <仓库地址>
cd tanuki-price-peek

python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux:
source .venv/bin/activate

python -m pip install -r requirements.txt
python -m playwright install chromium

# 按 config.example.json 新建 config.json，填入邮箱和商品
cp config.example.json config.json

python monitor.py
```

首次运行会记录当前价格，并根据“当前是否低于目标价 / 是否缺货”决定是否发提醒。之后每次运行都与上一次记录比较。

## 配置
config.json 从 config.example.json 复制而来，包含两部分：

- email：SMTP 服务器、端口、账号、应用专用密码、收件人。

  - Gmail：smtp.gmail.com，端口 465 配 use_ssl: true，或端口 587 配 use_ssl: false。

  - 密码填 Google 账号的 16 位应用专用密码，需先开启两步验证。

- products：要监控的商品列表，每项包括名称、商品页 URL、目标价、要监控的 SKU 列表。

一个商品页只开一次浏览器，页面内所有目标 SKU 的价格一起读取。加商品只需在 products 里加一项。

config.json 含邮箱/账号密码，不要提交到 Git（已在 .gitignore 中排除）。

## 提醒规则
- 价格从目标价以上跌到目标价及以下：提醒一次。
- 已低于目标价期间继续降价：不重复提醒。
- 涨回目标价以上后再次跌破：重新提醒。
- 缺货：提醒一次；恢复有货：再提醒一次。
- 某个 SKU 在页面中消失：提醒一次，之后不重复。
- 每次运行、每个 SKU 都会记录价格和库存，无论是否发提醒。

## 注意事项
- 任天堂商店使用 Queue-it 排队。脚本以无头模式（HEADLESS = True）运行，已在 Windows 上验证可以穿过排队；迁移到其他环境时如果被排队页拦截，可临时改为 HEADLESS = False 观察页面状态。

- 抓取失败、页面结构变化导致找不到 JSON-LD 或 SKU 时，会发一封告警邮件，而不是静默失败。

- 请保持低频访问，不要把这个脚本改成高频轮询。