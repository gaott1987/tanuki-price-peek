import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# ========== 全局配置 ==========
CONFIG_PATH = "config.json"
DB_PATH = "price_history.db"
HEADLESS = True  # 如果无法稳定穿过 Queue-it，可以设置成 False 保持有头模式

TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""


# ========== 配置与数据库 ==========
def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            sku TEXT NOT NULL,
            price INTEGER,
            availability TEXT NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def get_last_record(conn, sku):
    row = conn.execute(
        "SELECT price, availability, checked_at FROM prices WHERE sku = ? ORDER BY rowid DESC LIMIT 1",
        (sku,),
    ).fetchone()
    if not row:
        return None
    return {"price": row[0], "availability": row[1], "checked_at": row[2]}


def get_hist_min(conn, sku):
    row = conn.execute(
        "SELECT MIN(price) FROM prices WHERE sku = ? AND price IS NOT NULL", (sku,)
    ).fetchone()
    return row[0] if row else None


def save_record(conn, sku, price, availability):
    conn.execute(
        "INSERT INTO prices (sku, price, availability, checked_at) VALUES (?, ?, ?, ?)",
        (sku, price, availability, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ========== 抓取 ==========
def fetch_product(url, target_skus):
    """返回 {sku: {name, price, availability}}。页面级失败抛异常；SKU 缺失则不在返回字典中。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-images",
                "--js-flags=--max-old-space-size=256"
            ]
        )
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_url("**/item/software/**", timeout=120000)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(5)
        ld_texts = page.evaluate(
            """() => [...document.querySelectorAll('script[type="application/ld+json"]')].map(s => s.textContent)"""
        )
        browser.close()

    for txt in ld_texts:
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            continue
        types = data.get("@type")
        is_group = types == "ProductGroup" or (
            isinstance(types, list) and "ProductGroup" in types
        )
        if not is_group:
            continue
        variants = {}
        for v in data.get("hasVariant", []):
            sku = v.get("sku")
            if sku not in target_skus:
                continue
            offers = v.get("offers", {})
            price_raw = offers.get("price")
            price = (
                int(str(price_raw).replace(",", "")) if price_raw is not None else None
            )
            avail = offers.get("availability", "") or ""
            if "/" in avail:
                avail = avail.rstrip("/").split("/")[-1]
            variants[sku] = {
                "name": v.get("name", sku),
                "price": price,
                "availability": avail,
            }
        return variants
    raise RuntimeError("未找到 ProductGroup 的 JSON-LD")


# ========== 判断 ==========
def judge(last, cur_price, cur_avail, target):
    """返回该 SKU 本次需要提醒的原因列表。"""
    reasons = []

    if cur_avail == "MISSING":
        if last is None or last["availability"] != "MISSING":
            reasons.append("SKU 消失或未在页面中找到")
        return reasons

    # 价格穿越 / 首次低于目标价
    if cur_price is not None and cur_price <= target:
        if last is None:
            reasons.append(f"首次记录，当前价已低于目标价 {target} JPY")
        elif last["availability"] == "MISSING":
            reasons.append(f"SKU 重新出现，当前价低于目标价 {target} JPY")
        elif last["price"] is not None and last["price"] > target:
            reasons.append(
                f"价格跌破目标价：{last['price']} → {cur_price} JPY（目标 {target}）"
            )

    # 缺货与恢复
    if cur_avail == "OutOfStock":
        if last is None or last["availability"] != "OutOfStock":
            reasons.append("当前缺货")
    elif cur_avail == "InStock":
        if last is not None and last["availability"] == "OutOfStock":
            reasons.append("已恢复有货")

    return reasons


# ========== 通知 ==========
def send_email(email_cfg, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("Nintendo Monitor", email_cfg["from_addr"]))
    msg["To"] = ", ".join(email_cfg["to_addrs"])

    host = email_cfg["smtp_host"]
    port = email_cfg["smtp_port"]
    user = email_cfg["smtp_user"]
    password = email_cfg["smtp_password"]

    try:
        if email_cfg.get("use_ssl", True):
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        server.login(user, password)
        server.sendmail(email_cfg["from_addr"], email_cfg["to_addrs"], msg.as_string())
        server.quit()
        print("邮件已发送")
    except Exception as e:
        print("邮件发送失败:", type(e).__name__, e)


# ========== 主流程 ==========
def main():
    config = load_config()
    email_cfg = config.get("email")
    conn = init_db()
    alerts = []
    errors = []

    for product in config["products"]:
        name = product["name"]
        url = product["url"]
        target = product["target_price"]
        skus = product["skus"]

        try:
            variants = fetch_product(url, skus)
        except Exception as e:
            errors.append(f"【{name}】抓取失败：{type(e).__name__}: {e}")
            continue

        for sku in skus:
            last = get_last_record(conn, sku)
            hist_min = get_hist_min(conn, sku)

            if sku in variants:
                info = variants[sku]
                cur_price = info["price"]
                cur_avail = info["availability"]
                reasons = judge(last, cur_price, cur_avail, target)

                if reasons:
                    lines = [
                        f"【{name}】{info['name']} ({sku})",
                        f"  当前价格：{cur_price} JPY",
                        f"  目标价格：{target} JPY",
                        f"  上次价格：{last['price'] if last and last['price'] is not None else '无'} JPY",
                        f"  历史最低：{hist_min if hist_min is not None else '无'} JPY",
                        f"  库存状态：{cur_avail}",
                        f"  提醒原因：{'；'.join(reasons)}",
                        f"  {url}",
                    ]
                    alerts.append("\n".join(lines))
                else:
                    print(
                        f"[{name}] {sku} 无提醒：当前 {cur_price} JPY / {cur_avail}，"
                        f"上次 {last['price'] if last else '无'} JPY / {last['availability'] if last else '无'}"
                    )

                save_record(conn, sku, cur_price, cur_avail)
            else:
                reasons = judge(last, None, "MISSING", target)
                if reasons:
                    alerts.append(f"⚠️【{name}】SKU {sku} 消失或未在页面中找到\n  {url}")
                else:
                    print(f"[{name}] {sku} 仍缺失，不重复告警")
                save_record(conn, sku, None, "MISSING")

    conn.close()

    if alerts or errors:
        parts = []
        if errors:
            parts.append("⚠️ 抓取失败\n" + "\n".join(errors))
        if alerts:
            parts.append("🎮 任天堂价格监控提醒\n\n" + "\n\n".join(alerts))
        body = "\n\n".join(parts)

        if email_cfg:
            subject = f"【任天堂价格提醒】{len(alerts)} 条商品提醒"
            if errors and not alerts:
                subject = "【任天堂监控】抓取失败告警"
            send_email(email_cfg, subject, body)
        else:
            print("未配置 email，仅输出到控制台：")
            print(body)
    else:
        print("本次运行无提醒。")


if __name__ == "__main__":
    main()
