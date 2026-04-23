"""邮件发送：Brevo HTTP API（绕开 Railway 的 SMTP 端口封锁）。

Railway / Fly.io / Render 等 PaaS 屏蔽了出站 25/465/587 SMTP 端口以防垃圾邮件，
所以不能用 smtplib。走 HTTPS 443 调 Brevo REST API（https://api.brevo.com/v3/smtp/email）。

环境变量：
  BREVO_API_KEY    — Brevo API key（必填；无则直接跳过）
  EMAIL_SENDER     — 发件人地址（必须是 Brevo 已验证的发件人）
  EMAIL_RECEIVERS  — 收件人列表（逗号分隔）

失败不抛出，只返回 False，避免一封邮件把主程序拉崩。
"""
import logging
import requests

from backend.utils import config

logger = logging.getLogger(__name__)

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
TIMEOUT = 30


def send_email(
    subject: str,
    html_body: str,
    recipients: list[str] | str | None = None,
) -> bool:
    """发送 HTML 邮件，返回是否成功。"""
    if not config.BREVO_API_KEY:
        logger.error("BREVO_API_KEY 未配置，跳过发送")
        return False
    if not config.EMAIL_SENDER:
        logger.error("EMAIL_SENDER 未配置，跳过发送")
        return False

    if recipients is None:
        recipients = config.EMAIL_RECEIVERS
    if isinstance(recipients, str):
        recipients = [r.strip() for r in recipients.split(",") if r.strip()]
    if not recipients:
        logger.error("收件人列表为空，跳过发送")
        return False

    payload = {
        "sender":      {"email": config.EMAIL_SENDER, "name": "ModelRadar"},
        "to":          [{"email": r} for r in recipients],
        "subject":     subject,
        "htmlContent": html_body,
    }
    headers = {
        "accept":       "application/json",
        "content-type": "application/json",
        "api-key":      config.BREVO_API_KEY,
    }

    try:
        resp = requests.post(BREVO_URL, headers=headers, json=payload, timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.error("Brevo 请求异常: %s", e)
        return False

    if resp.status_code in (200, 201, 202):
        try:
            msg_id = resp.json().get("messageId", "")
        except Exception:
            msg_id = ""
        logger.info("邮件已发送至 %s (Brevo messageId=%s)", ", ".join(recipients), msg_id)
        return True

    try:
        err_json = resp.json()
    except Exception:
        err_json = {"raw": resp.text[:500]}
    logger.error("Brevo 发送失败 HTTP %d: %s", resp.status_code, err_json)
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ok = send_email(
        subject="ModelRadar 邮件通道测试（Brevo）",
        html_body="<h3>ModelRadar 邮件通道测试</h3><p>如果你收到了这封邮件，说明 Brevo HTTP API 发送通道工作正常。</p>",
    )
    print("结果:", "成功" if ok else "失败")
