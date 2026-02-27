"""Telegram notification service."""

import asyncio
from typing import Optional

import httpx

from utils.logger import get_logger

logger = get_logger(__name__)


class TelegramNotifier:
    """
    Send notifications via Telegram Bot API.
    """

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str):
        """
        Initialize the Telegram notifier.

        Args:
            bot_token: Telegram bot token
            chat_id: Target chat ID for notifications
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.url = self.BASE_URL.format(token=bot_token)

    async def send(
        self,
        message: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> bool:
        """
        Send a message via Telegram.

        Args:
            message: Message text to send
            parse_mode: Message format (HTML or Markdown)
            disable_notification: If True, send silently

        Returns:
            True if message was sent successfully
        """
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(self.url, json=payload, timeout=10.0)
                response.raise_for_status()
                logger.debug("Telegram message sent", chat_id=self.chat_id)
                return True
        except httpx.HTTPError as e:
            logger.error("Failed to send Telegram message", error=str(e))
            return False

    async def send_alert(self, title: str, details: str) -> bool:
        """
        Send a formatted alert message.

        Args:
            title: Alert title
            details: Alert details

        Returns:
            True if message was sent successfully
        """
        message = f"🚨 <b>{title}</b>\n\n{details}"
        return await self.send(message)

    async def send_trade_notification(
        self,
        market: str,
        profit_pct: float,
        trade_size: float,
        success: bool,
    ) -> bool:
        """
        Send a trade execution notification.

        Args:
            market: Market name/question
            profit_pct: Expected profit percentage
            trade_size: Trade size in USDC
            success: Whether trade was successful

        Returns:
            True if message was sent successfully
        """
        emoji = "✅" if success else "❌"
        status = "SUCCESS" if success else "FAILED"
        message = (
            f"{emoji} <b>Trade {status}</b>\n\n"
            f"📊 Market: {market[:50]}...\n"
            f"💰 Profit: {profit_pct:.2%}\n"
            f"💵 Size: ${trade_size:.2f} USDC"
        )
        return await self.send(message)



class WeChatNotifier:
    """
    Send notifications via Enterprise WeChat Webhook.
    """

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    async def send(
        self,
        message: str,
        parse_mode: str = "markdown",  # Default to markdown
        disable_notification: bool = False,
    ) -> bool:
        """
        Send a message via WeChat Webhook.
        """
        # Convert HTML tags to simpler markdown if necessary, or just send text.
        # WeChat markdown supports subset: bold **text**, links [text](url), etc.
        # Simple cleanup for common HTML tags used in this bot
        text = message.replace("<b>", "**").replace("</b>", "**")
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": text
            }
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(self.webhook_url, json=payload, timeout=10.0)
                response.raise_for_status()
                # WeChat API returns JSON with errcode
                res_json = response.json()
                if res_json.get("errcode") == 0:
                    logger.debug("WeChat message sent")
                    return True
                else:
                    logger.error("WeChat API error", response=res_json)
                    return False
        except Exception as e:
            logger.error("Failed to send WeChat message", error=str(e))
            return False

    async def send_alert(self, title: str, details: str) -> bool:
        """Send a formatted alert message."""
        # Add warning emoji and format with color
        message = f"🚨 <font color=\"warning\">**{title}**</font>\n\n{details}"
        return await self.send(message)

    async def send_trade_notification(
        self,
        market: str,
        profit_pct: float,
        trade_size: float,
        success: bool,
        extra_info: str = "",
    ) -> bool:
        """Send a trade execution notification."""
        emoji = "✅" if success else "❌"
        status = "SUCCESS" if success else "FAILED"
        color = "info" if success else "warning"
        message = (
            f"{emoji} <font color=\"{color}\">**Trade {status}**</font>\n\n"
            f"> 📊 Market: {market[:50]}...\n"
            f"> 💰 Profit: {profit_pct:.2%}\n"
            f"> 💵 Size: ${trade_size:.2f} USDC"
            f"{extra_info}"
        )
        return await self.send(message)


class DummyNotifier:
    """
    Dummy notifier that does nothing (for when Telegram is not configured).
    """

    async def send(self, message: str, **kwargs) -> bool:
        """Log the message instead of sending."""
        logger.info("Notification (not sent)", message=message[:100])
        return True

    async def send_alert(self, title: str, details: str) -> bool:
        """Log the alert instead of sending."""
        logger.warning("Alert (not sent)", title=title, details=details[:100])
        return True

    async def send_trade_notification(self, **kwargs) -> bool:
        """Log the trade notification instead of sending."""
        logger.info("Trade notification (not sent)", **kwargs)
        return True


class CompositeNotifier:
    """
    Sends notifications to multiple backends simultaneously.

    Falls back gracefully: if one channel fails, the others still deliver.
    """

    def __init__(self, notifiers: list):
        self._notifiers = notifiers

    async def send(self, message: str, **kwargs) -> bool:
        results = await asyncio.gather(
            *(n.send(message, **kwargs) for n in self._notifiers),
            return_exceptions=True,
        )
        return any(r is True for r in results)

    async def send_alert(self, title: str, details: str) -> bool:
        results = await asyncio.gather(
            *(n.send_alert(title, details) for n in self._notifiers),
            return_exceptions=True,
        )
        return any(r is True for r in results)

    async def send_trade_notification(self, **kwargs) -> bool:
        results = await asyncio.gather(
            *(n.send_trade_notification(**kwargs) for n in self._notifiers),
            return_exceptions=True,
        )
        return any(r is True for r in results)


def create_notifier(
    bot_token: Optional[str], 
    chat_id: Optional[str],
    wechat_webhook_url: Optional[str] = None,
):
    """
    Create a notifier instance.

    If both Telegram and WeChat are configured, returns a CompositeNotifier
    that sends to both. Otherwise returns the single available channel,
    or a DummyNotifier as fallback.
    """
    notifiers = []

    if bot_token and chat_id:
        notifiers.append(TelegramNotifier(bot_token, chat_id))

    if wechat_webhook_url:
        notifiers.append(WeChatNotifier(webhook_url=wechat_webhook_url))

    if len(notifiers) >= 2:
        logger.info("Using Composite Notifier (Telegram + WeChat)")
        return CompositeNotifier(notifiers)
    elif len(notifiers) == 1:
        name = type(notifiers[0]).__name__
        logger.info(f"Using {name}")
        return notifiers[0]
    else:
        return DummyNotifier()
