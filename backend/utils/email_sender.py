"""163 SMTP 邮件发送，含 DoH DNS 降级、IP 直连 TLS SNI、3 次重试。
移植自 trending-tracker/trending_tracker.py:527-573，改成纯 env 驱动。
"""
import json
import logging
import smtplib
import socket
import ssl
import time
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from backend.utils import config

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465


def _resolve_smtp_host(hostname: str) -> tuple[str, bool]:
    """DNS 解析（强制 IPv4），失败时降级到 DoH。返回 (host_or_ip, is_ip)。

    Railway 等 PaaS 容器经常没有 IPv6 出站路由，若拿到 AAAA 记录走 IPv6
    会在 connect() 时报 Errno 101 Network is unreachable。所以这里强制 IPv4
    并直接用 IP 走 SNI 路径，绕开 smtplib 内部的 getaddrinfo 选择。"""
    try:
        infos = socket.getaddrinfo(hostname, SMTP_PORT, family=socket.AF_INET)
        ip = infos[0][4][0]
        return ip, True
    except socket.gaierror:
        logger.warning("DNS 解析 %s 失败（IPv4），尝试 DoH 降级", hostname)

    doh_urls = [
        f"https://dns.google/resolve?name={hostname}&type=A",
        f"https://cloudflare-dns.com/dns-query?name={hostname}&type=A",
    ]
    for doh_url in doh_urls:
        try:
            req = urllib.request.Request(
                doh_url, headers={"Accept": "application/dns-json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                for ans in data.get("Answer", []):
                    if ans.get("type") == 1:
                        ip = ans["data"]
                        logger.info("DoH 解析成功: %s -> %s", hostname, ip)
                        return ip, True
        except Exception as e:
            logger.warning("DoH 请求失败 (%s): %s", doh_url, e)

    raise RuntimeError(f"无法解析 {hostname}：DNS 和 DoH 均失败")


def _smtp_connect(host: str, port: int, timeout: int, is_ip: bool):
    """建立 SMTP SSL 连接，IP 直连时正确设置 SNI hostname。"""
    if not is_ip:
        return smtplib.SMTP_SSL(host, port, timeout=timeout)

    ctx = ssl.create_default_context()
    raw_sock = socket.create_connection((host, port), timeout=timeout)
    ssl_sock = ctx.wrap_socket(raw_sock, server_hostname=SMTP_HOST)

    server = smtplib.SMTP_SSL.__new__(smtplib.SMTP_SSL)
    server.timeout = timeout
    server.source_address = None
    server._host = SMTP_HOST
    server.context = ctx
    server.sock = ssl_sock
    server.file = None
    server.debuglevel = 0
    server._ehlo_resp = None
    server._ehlo_or_helo_if_needed = smtplib.SMTP._ehlo_or_helo_if_needed.__get__(server)
    server.file = server.sock.makefile("rb")
    server.getreply()
    server.ehlo()
    return server


def send_email(
    subject: str,
    html_body: str,
    recipients: list[str] | str | None = None,
) -> bool:
    """发送 HTML 邮件。失败自动重试 3 次。返回是否成功。"""
    if not config.EMAIL_SENDER or not config.EMAIL_PASSWORD:
        logger.error("EMAIL_SENDER / EMAIL_PASSWORD 未配置，跳过发送")
        return False

    if recipients is None:
        recipients = config.EMAIL_RECEIVERS
    if isinstance(recipients, str):
        recipients = [r.strip() for r in recipients.split(",") if r.strip()]
    if not recipients:
        logger.error("收件人列表为空，跳过发送")
        return False

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        resolved_host, is_ip = _resolve_smtp_host(SMTP_HOST)
    except RuntimeError as e:
        logger.error("%s", e)
        return False

    for attempt in range(1, 4):
        try:
            server = _smtp_connect(resolved_host, SMTP_PORT, timeout=30, is_ip=is_ip)
            server.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
            server.sendmail(config.EMAIL_SENDER, recipients, msg.as_string())
            server.quit()
            suffix = f" (DoH 降级: {resolved_host})" if is_ip else ""
            logger.info("邮件已发送至 %s%s", ", ".join(recipients), suffix)
            return True
        except Exception as e:
            logger.error("邮件发送失败 (第 %d/3 次): %s", attempt, e)
            if attempt < 3:
                time.sleep(10)
    logger.error("邮件发送最终失败，放弃重试")
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ok = send_email(
        subject="ModelRadar 邮件通道测试",
        html_body="<h3>这是一封测试邮件</h3><p>如果你收到了这封邮件，说明 163 SMTP 发送通道工作正常。</p>",
    )
    print("结果:", "成功" if ok else "失败")
