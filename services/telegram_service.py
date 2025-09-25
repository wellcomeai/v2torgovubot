"""
Trading Bot Telegram Service
Сервис для отправки уведомлений через Telegram
"""

import asyncio
import time
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Union, Tuple
from dataclasses import dataclass, field
from enum import Enum
import aiohttp
from urllib.parse import urlencode

from utils.logger import setup_logger
from utils.helpers import (
    get_current_timestamp, safe_float, Timer, retry_async
)
from app.config.settings import Settings, get_settings
from data.database import Database
from strategies.base_strategy import StrategyResult, SignalType


# ============================================================================
# CONSTANTS AND ENUMS
# ============================================================================

logger = setup_logger(__name__)

# Telegram API настройки
TELEGRAM_API_URL = "https://api.telegram.org"
DEFAULT_PARSE_MODE = "Markdown"
DEFAULT_TIMEOUT = 10
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

# Rate limits
MAX_MESSAGES_PER_SECOND = 1
MAX_MESSAGES_PER_MINUTE = 20
MAX_MESSAGES_PER_HOUR = 300


class MessageType(str, Enum):
    """Типы сообщений"""
    TEXT = "text"
    PHOTO = "photo"
    DOCUMENT = "document"
    ANIMATION = "animation"
    STICKER = "sticker"


class MessagePriority(str, Enum):
    """Приоритеты сообщений"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TelegramError(Exception):
    """Ошибки Telegram API"""
    def __init__(self, code: int, description: str):
        self.code = code
        self.description = description
        super().__init__(f"Telegram Error {code}: {description}")


@dataclass
class TelegramMessage:
    """Сообщение для отправки в Telegram"""
    text: str
    chat_id: Optional[str] = None
    message_type: MessageType = MessageType.TEXT
    parse_mode: str = DEFAULT_PARSE_MODE
    priority: MessagePriority = MessagePriority.NORMAL
    disable_notification: bool = False
    reply_to_message_id: Optional[int] = None
    
    # Для медиа сообщений
    photo_url: Optional[str] = None
    document_url: Optional[str] = None
    caption: Optional[str] = None
    
    # Метаданные
    timestamp: int = field(default_factory=get_current_timestamp)
    retry_count: int = 0
    max_retries: int = 3
    
    def validate(self) -> bool:
        """Валидация сообщения"""
        if not self.text and not self.photo_url and not self.document_url:
            return False
        
        if self.text and len(self.text) > MAX_MESSAGE_LENGTH:
            return False
        
        if self.caption and len(self.caption) > MAX_CAPTION_LENGTH:
            return False
        
        return True
    
    def truncate_if_needed(self) -> None:
        """Обрезка сообщения если необходимо"""
        if self.text and len(self.text) > MAX_MESSAGE_LENGTH:
            self.text = self.text[:MAX_MESSAGE_LENGTH - 3] + "..."
        
        if self.caption and len(self.caption) > MAX_CAPTION_LENGTH:
            self.caption = self.caption[:MAX_CAPTION_LENGTH - 3] + "..."


# ============================================================================
# TELEGRAM SERVICE CLASS
# ============================================================================

class TelegramService:
    """
    Сервис для работы с Telegram Bot API
    
    Возможности:
    - Отправка текстовых сообщений
    - Отправка изображений и документов
    - Форматирование торговых уведомлений
    - Rate limiting
    - Retry механизм
    - Очереди сообщений
    """
    
    def __init__(
        self,
        settings: Optional[Settings] = None,
        database: Optional[Database] = None
    ):
        self.settings = settings or get_settings()
        self.database = database
        self.logger = setup_logger(f"{__name__}.TelegramService")
        
        # API настройки
        self.bot_token = self.settings.TELEGRAM_BOT_TOKEN
        self.default_chat_id = self.settings.TELEGRAM_CHAT_ID
        self.parse_mode = self.settings.TELEGRAM_PARSE_MODE
        self.disable_notification = self.settings.TELEGRAM_DISABLE_NOTIFICATION
        self.timeout = self.settings.TELEGRAM_TIMEOUT
        self.max_retries = self.settings.TELEGRAM_RETRY_ATTEMPTS
        
        # HTTP сессия
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Очереди сообщений
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._priority_queue: asyncio.Queue = asyncio.Queue()
        self._processing_task: Optional[asyncio.Task] = None
        
        # Rate limiting
        self._last_message_time = 0.0
        self._messages_this_minute = []
        self._messages_this_hour = []
        
        # Статистика
        self.stats = {
            'total_messages': 0,
            'successful_messages': 0,
            'failed_messages': 0,
            'rate_limited_messages': 0,
            'queue_size': 0,
            'avg_response_time': 0.0
        }
        
        if not self.bot_token:
            self.logger.warning("⚠️ Telegram bot token not configured")
        else:
            self.logger.info("📱 Telegram Service initialized")
    
    async def initialize(self):
        """Инициализация сервиса"""
        try:
            await self._create_session()
            
            # Проверяем токен
            if self.bot_token:
                bot_info = await self._get_me()
                if bot_info:
                    self.logger.info(f"✅ Connected to Telegram bot: @{bot_info.get('username', 'unknown')}")
                else:
                    self.logger.error("❌ Failed to verify Telegram bot token")
            
            # Запускаем обработчик очередей
            self._processing_task = asyncio.create_task(self._process_message_queues())
            
            self.logger.info("📱 Telegram Service initialized successfully")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize Telegram service: {e}")
            raise
    
    async def close(self):
        """Закрытие сервиса"""
        try:
            # Останавливаем обработчик очередей
            if self._processing_task:
                self._processing_task.cancel()
                try:
                    await self._processing_task
                except asyncio.CancelledError:
                    pass
            
            # Отправляем оставшиеся сообщения
            await self._flush_queues()
            
            # Закрываем сессию
            if self._session:
                await self._session.close()
                self._session = None
            
            self.logger.info("📱 Telegram Service closed")
            
        except Exception as e:
            self.logger.error(f"❌ Error closing Telegram service: {e}")
    
    async def _create_session(self):
        """Создание HTTP сессии"""
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    'User-Agent': 'TradingBot/1.0.0'
                }
            )
    
    # ========================================================================
    # PUBLIC API METHODS
    # ========================================================================
    
    async def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        priority: MessagePriority = MessagePriority.NORMAL,
        parse_mode: Optional[str] = None,
        disable_notification: Optional[bool] = None
    ) -> bool:
        """
        Отправка текстового сообщения
        
        Args:
            text: Текст сообщения
            chat_id: ID чата (по умолчанию из настроек)
            priority: Приоритет сообщения
            parse_mode: Режим парсинга (Markdown/HTML)
            disable_notification: Отключить звук уведомления
            
        Returns:
            True если сообщение добавлено в очередь
        """
        try:
            message = TelegramMessage(
                text=text,
                chat_id=chat_id or self.default_chat_id,
                priority=priority,
                parse_mode=parse_mode or self.parse_mode,
                disable_notification=disable_notification if disable_notification is not None else self.disable_notification,
                max_retries=self.max_retries
            )
            
            return await self._queue_message(message)
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send message: {e}")
            return False
    
    async def send_photo(
        self,
        photo_url: str,
        caption: Optional[str] = None,
        chat_id: Optional[str] = None,
        priority: MessagePriority = MessagePriority.NORMAL
    ) -> bool:
        """
        Отправка изображения
        
        Args:
            photo_url: URL изображения
            caption: Подпись к изображению
            chat_id: ID чата
            priority: Приоритет сообщения
            
        Returns:
            True если сообщение добавлено в очередь
        """
        try:
            message = TelegramMessage(
                text="",  # Для фото текст не обязателен
                message_type=MessageType.PHOTO,
                photo_url=photo_url,
                caption=caption,
                chat_id=chat_id or self.default_chat_id,
                priority=priority
            )
            
            return await self._queue_message(message)
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send photo: {e}")
            return False
    
    async def send_trading_signal(
        self,
        signal: StrategyResult,
        symbol: str,
        strategy_name: str,
        current_price: float,
        additional_info: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Отправка уведомления о торговом сигнале
        
        Args:
            signal: Торговый сигнал
            symbol: Торговая пара
            strategy_name: Название стратегии
            current_price: Текущая цена
            additional_info: Дополнительная информация
            
        Returns:
            True если сообщение отправлено
        """
        try:
            formatted_message = self._format_trading_signal(
                signal, symbol, strategy_name, current_price, additional_info
            )
            
            priority = MessagePriority.HIGH if signal.confidence > 0.8 else MessagePriority.NORMAL
            
            return await self.send_message(
                text=formatted_message,
                priority=priority
            )
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send trading signal: {e}")
            return False
    
    async def send_portfolio_update(
        self,
        balance: float,
        unrealized_pnl: float,
        daily_pnl: float,
        open_positions: List[Dict[str, Any]],
        recent_trades: List[Dict[str, Any]]
    ) -> bool:
        """
        Отправка обновления портфеля
        
        Args:
            balance: Текущий баланс
            unrealized_pnl: Нереализованная прибыль/убыток
            daily_pnl: Дневная прибыль/убыток
            open_positions: Список открытых позиций
            recent_trades: Недавние сделки
            
        Returns:
            True если сообщение отправлено
        """
        try:
            formatted_message = self._format_portfolio_update(
                balance, unrealized_pnl, daily_pnl, open_positions, recent_trades
            )
            
            return await self.send_message(
                text=formatted_message,
                priority=MessagePriority.NORMAL
            )
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send portfolio update: {e}")
            return False
    
    async def send_error_alert(
        self,
        error_message: str,
        component: str,
        severity: str = "medium",
        additional_info: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Отправка уведомления об ошибке
        
        Args:
            error_message: Сообщение об ошибке
            component: Компонент где произошла ошибка
            severity: Серьезность (low/medium/high/critical)
            additional_info: Дополнительная информация
            
        Returns:
            True если сообщение отправлено
        """
        try:
            formatted_message = self._format_error_alert(
                error_message, component, severity, additional_info
            )
            
            # Определяем приоритет по серьезности
            priority_map = {
                'low': MessagePriority.LOW,
                'medium': MessagePriority.NORMAL,
                'high': MessagePriority.HIGH,
                'critical': MessagePriority.URGENT
            }
            
            priority = priority_map.get(severity, MessagePriority.NORMAL)
            
            return await self.send_message(
                text=formatted_message,
                priority=priority
            )
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send error alert: {e}")
            return False
    
    async def send_system_status(
        self,
        status: str,
        uptime: str,
        components: Dict[str, str],
        metrics: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Отправка статуса системы
        
        Args:
            status: Общий статус системы
            uptime: Время работы
            components: Статус компонентов
            metrics: Дополнительные метрики
            
        Returns:
            True если сообщение отправлено
        """
        try:
            formatted_message = self._format_system_status(
                status, uptime, components, metrics
            )
            
            return await self.send_message(
                text=formatted_message,
                priority=MessagePriority.LOW
            )
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send system status: {e}")
            return False
    
    # ========================================================================
    # MESSAGE FORMATTING
    # ========================================================================
    
    def _format_trading_signal(
        self,
        signal: StrategyResult,
        symbol: str,
        strategy_name: str,
        current_price: float,
        additional_info: Optional[Dict[str, Any]] = None
    ) -> str:
        """Форматирование торгового сигнала"""
        
        # Эмодзи для типов сигналов
        signal_emojis = {
            SignalType.STRONG_BUY: "🟢🚀",
            SignalType.BUY: "🟢",
            SignalType.HOLD: "🟡",
            SignalType.SELL: "🔴",
            SignalType.STRONG_SELL: "🔴💥"
        }
        
        emoji = signal_emojis.get(signal.signal_type, "⚪")
        
        # Основная информация
        message_parts = [
            f"{emoji} **TRADING SIGNAL** {emoji}",
            "",
            f"**Symbol:** {symbol}",
            f"**Strategy:** {strategy_name}",
            f"**Signal:** {signal.signal_type.value.upper()}",
            f"**Current Price:** ${current_price:.4f}",
            f"**Confidence:** {signal.confidence:.1%}",
        ]
        
        # Цены входа/выхода
        if signal.entry_price:
            message_parts.append(f"**Entry Price:** ${signal.entry_price:.4f}")
        
        if signal.stop_loss:
            message_parts.append(f"**Stop Loss:** ${signal.stop_loss:.4f}")
        
        if signal.take_profit:
            message_parts.append(f"**Take Profit:** ${signal.take_profit:.4f}")
        
        # Reasoning
        if signal.reasoning:
            message_parts.extend([
                "",
                f"**Analysis:** {signal.reasoning}"
            ])
        
        # Дополнительная информация
        if additional_info:
            if 'ai_analysis' in additional_info:
                message_parts.extend([
                    "",
                    f"**AI Analysis:** {additional_info['ai_analysis'][:200]}..."
                ])
            
            if 'risk_level' in additional_info:
                message_parts.append(f"**Risk Level:** {additional_info['risk_level']}")
        
        # Временная метка
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        message_parts.extend([
            "",
            f"🕐 {timestamp}"
        ])
        
        return "\n".join(message_parts)
    
    def _format_portfolio_update(
        self,
        balance: float,
        unrealized_pnl: float,
        daily_pnl: float,
        open_positions: List[Dict[str, Any]],
        recent_trades: List[Dict[str, Any]]
    ) -> str:
        """Форматирование обновления портфеля"""
        
        # Определяем статус по PnL
        if daily_pnl > 0:
            status_emoji = "📈"
        elif daily_pnl < 0:
            status_emoji = "📉"
        else:
            status_emoji = "📊"
        
        message_parts = [
            f"{status_emoji} **PORTFOLIO UPDATE** {status_emoji}",
            "",
            f"**Balance:** ${balance:.2f}",
            f"**Unrealized P&L:** ${unrealized_pnl:+.2f}",
            f"**Daily P&L:** ${daily_pnl:+.2f}",
        ]
        
        # Открытые позиции
        if open_positions:
            message_parts.extend([
                "",
                f"**Open Positions:** {len(open_positions)}"
            ])
            
            for i, position in enumerate(open_positions[:3]):  # Показываем максимум 3
                symbol = position.get('symbol', 'Unknown')
                side = position.get('side', 'unknown')
                size = position.get('size', 0)
                pnl = position.get('unrealized_pnl', 0)
                
                side_emoji = "📈" if side == "long" else "📉"
                message_parts.append(
                    f"{side_emoji} {symbol}: {size:.6f} (${pnl:+.2f})"
                )
            
            if len(open_positions) > 3:
                message_parts.append(f"... and {len(open_positions) - 3} more")
        
        # Недавние сделки
        if recent_trades:
            message_parts.extend([
                "",
                f"**Recent Trades:** {len(recent_trades)}"
            ])
            
            for trade in recent_trades[:2]:  # Показываем максимум 2
                symbol = trade.get('symbol', 'Unknown')
                side = trade.get('side', 'unknown')
                pnl = trade.get('pnl', 0)
                
                side_emoji = "✅" if pnl > 0 else "❌"
                message_parts.append(
                    f"{side_emoji} {symbol} {side.upper()}: ${pnl:+.2f}"
                )
        
        # Временная метка
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        message_parts.extend([
            "",
            f"🕐 {timestamp}"
        ])
        
        return "\n".join(message_parts)
    
    def _format_error_alert(
        self,
        error_message: str,
        component: str,
        severity: str,
        additional_info: Optional[Dict[str, Any]] = None
    ) -> str:
        """Форматирование уведомления об ошибке"""
        
        # Эмодзи по серьезности
        severity_emojis = {
            'low': "⚠️",
            'medium': "🟠",
            'high': "🔴",
            'critical': "🚨"
        }
        
        emoji = severity_emojis.get(severity, "⚠️")
        
        message_parts = [
            f"{emoji} **ERROR ALERT** {emoji}",
            "",
            f"**Component:** {component}",
            f"**Severity:** {severity.upper()}",
            f"**Error:** {error_message[:500]}",  # Ограничиваем длину
        ]
        
        if additional_info:
            if 'timestamp' in additional_info:
                message_parts.append(f"**Time:** {additional_info['timestamp']}")
            
            if 'suggestion' in additional_info:
                message_parts.append(f"**Suggestion:** {additional_info['suggestion']}")
        
        # Временная метка
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        message_parts.extend([
            "",
            f"🕐 {timestamp}"
        ])
        
        return "\n".join(message_parts)
    
    def _format_system_status(
        self,
        status: str,
        uptime: str,
        components: Dict[str, str],
        metrics: Optional[Dict[str, Any]] = None
    ) -> str:
        """Форматирование статуса системы"""
        
        # Эмодзи по статусу
        status_emoji = "✅" if status == "healthy" else "⚠️" if status == "degraded" else "❌"
        
        message_parts = [
            f"{status_emoji} **SYSTEM STATUS** {status_emoji}",
            "",
            f"**Status:** {status.upper()}",
            f"**Uptime:** {uptime}",
        ]
        
        # Статус компонентов
        if components:
            message_parts.extend([
                "",
                "**Components:**"
            ])
            
            for component, comp_status in components.items():
                comp_emoji = "✅" if comp_status == "healthy" else "❌"
                message_parts.append(f"{comp_emoji} {component}: {comp_status}")
        
        # Метрики
        if metrics:
            message_parts.append("")
            
            if 'active_strategies' in metrics:
                message_parts.append(f"**Active Strategies:** {metrics['active_strategies']}")
            
            if 'total_trades' in metrics:
                message_parts.append(f"**Total Trades:** {metrics['total_trades']}")
            
            if 'success_rate' in metrics:
                message_parts.append(f"**Success Rate:** {metrics['success_rate']:.1%}")
        
        # Временная метка
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        message_parts.extend([
            "",
            f"🕐 {timestamp}"
        ])
        
        return "\n".join(message_parts)
    
    # ========================================================================
    # MESSAGE QUEUE PROCESSING
    # ========================================================================
    
    async def _queue_message(self, message: TelegramMessage) -> bool:
        """Добавление сообщения в очередь"""
        try:
            if not message.validate():
                self.logger.error("❌ Invalid message format")
                return False
            
            message.truncate_if_needed()
            
            # Выбираем очередь по приоритету
            if message.priority in [MessagePriority.HIGH, MessagePriority.URGENT]:
                await self._priority_queue.put(message)
            else:
                await self._message_queue.put(message)
            
            self.stats['queue_size'] = self._message_queue.qsize() + self._priority_queue.qsize()
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to queue message: {e}")
            return False
    
    async def _process_message_queues(self):
        """Обработка очередей сообщений"""
        self.logger.info("📱 Message queue processor started")
        
        try:
            while True:
                try:
                    # Сначала обрабатываем приоритетную очередь
                    if not self._priority_queue.empty():
                        message = await asyncio.wait_for(self._priority_queue.get(), timeout=0.1)
                    else:
                        message = await self._message_queue.get()
                    
                    await self._process_single_message(message)
                    await asyncio.sleep(0.1)  # Небольшая задержка между сообщениями
                    
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.error(f"❌ Message processing error: {e}")
                    await asyncio.sleep(1)
                    
        except asyncio.CancelledError:
            self.logger.info("📱 Message queue processor stopped")
    
    async def _process_single_message(self, message: TelegramMessage):
        """Обработка одного сообщения"""
        try:
            # Проверяем rate limits
            if not self._check_rate_limits():
                # Возвращаем сообщение в очередь если лимит превышен
                await self._message_queue.put(message)
                await asyncio.sleep(1)
                return
            
            # Отправляем сообщение
            success = await self._send_telegram_message(message)
            
            if success:
                self.stats['successful_messages'] += 1
                self.logger.debug(f"📱 Message sent successfully to {message.chat_id}")
            else:
                # Пытаемся повторить если не превышено количество попыток
                if message.retry_count < message.max_retries:
                    message.retry_count += 1
                    await self._message_queue.put(message)
                    self.logger.warning(f"⚠️ Retrying message (attempt {message.retry_count})")
                else:
                    self.stats['failed_messages'] += 1
                    self.logger.error(f"❌ Message failed after {message.max_retries} retries")
                    
            self.stats['total_messages'] += 1
            self.stats['queue_size'] = self._message_queue.qsize() + self._priority_queue.qsize()
            
        except Exception as e:
            self.logger.error(f"❌ Failed to process message: {e}")
    
    async def _send_telegram_message(self, message: TelegramMessage) -> bool:
        """Отправка сообщения через Telegram API"""
        if not self.bot_token or not message.chat_id:
            return False
        
        try:
            with Timer(f"telegram_send_{message.message_type.value}"):
                if message.message_type == MessageType.TEXT:
                    return await self._send_text_message(message)
                elif message.message_type == MessageType.PHOTO:
                    return await self._send_photo_message(message)
                else:
                    self.logger.warning(f"⚠️ Unsupported message type: {message.message_type}")
                    return False
                    
        except Exception as e:
            self.logger.error(f"❌ Telegram API error: {e}")
            return False
    
    async def _send_text_message(self, message: TelegramMessage) -> bool:
        """Отправка текстового сообщения"""
        url = f"{TELEGRAM_API_URL}/bot{self.bot_token}/sendMessage"
        
        payload = {
            'chat_id': message.chat_id,
            'text': message.text,
            'parse_mode': message.parse_mode,
            'disable_notification': message.disable_notification
        }
        
        if message.reply_to_message_id:
            payload['reply_to_message_id'] = message.reply_to_message_id
        
        try:
            async with self._session.post(url, json=payload) as response:
                if response.status == 200:
                    return True
                else:
                    error_data = await response.json()
                    error_desc = error_data.get('description', 'Unknown error')
                    raise TelegramError(response.status, error_desc)
                    
        except TelegramError:
            raise
        except Exception as e:
            raise TelegramError(-1, str(e))
    
    async def _send_photo_message(self, message: TelegramMessage) -> bool:
        """Отправка сообщения с изображением"""
        url = f"{TELEGRAM_API_URL}/bot{self.bot_token}/sendPhoto"
        
        payload = {
            'chat_id': message.chat_id,
            'photo': message.photo_url,
            'disable_notification': message.disable_notification
        }
        
        if message.caption:
            payload['caption'] = message.caption
            payload['parse_mode'] = message.parse_mode
        
        try:
            async with self._session.post(url, json=payload) as response:
                if response.status == 200:
                    return True
                else:
                    error_data = await response.json()
                    error_desc = error_data.get('description', 'Unknown error')
                    raise TelegramError(response.status, error_desc)
                    
        except TelegramError:
            raise
        except Exception as e:
            raise TelegramError(-1, str(e))
    
    # ========================================================================
    # RATE LIMITING
    # ========================================================================
    
    def _check_rate_limits(self) -> bool:
        """Проверка лимитов отправки"""
        current_time = time.time()
        
        # Проверяем интервал между сообщениями
        if current_time - self._last_message_time < 1.0 / MAX_MESSAGES_PER_SECOND:
            self.stats['rate_limited_messages'] += 1
            return False
        
        # Очищаем старые записи
        minute_ago = current_time - 60
        hour_ago = current_time - 3600
        
        self._messages_this_minute = [t for t in self._messages_this_minute if t > minute_ago]
        self._messages_this_hour = [t for t in self._messages_this_hour if t > hour_ago]
        
        # Проверяем лимиты
        if len(self._messages_this_minute) >= MAX_MESSAGES_PER_MINUTE:
            self.stats['rate_limited_messages'] += 1
            return False
        
        if len(self._messages_this_hour) >= MAX_MESSAGES_PER_HOUR:
            self.stats['rate_limited_messages'] += 1
            return False
        
        # Записываем время отправки
        self._last_message_time = current_time
        self._messages_this_minute.append(current_time)
        self._messages_this_hour.append(current_time)
        
        return True
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    async def _get_me(self) -> Optional[Dict[str, Any]]:
        """Получение информации о боте"""
        if not self.bot_token:
            return None
        
        url = f"{TELEGRAM_API_URL}/bot{self.bot_token}/getMe"
        
        try:
            async with self._session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('result')
                else:
                    return None
                    
        except Exception as e:
            self.logger.error(f"❌ Failed to get bot info: {e}")
            return None
    
    async def _flush_queues(self):
        """Отправка всех оставшихся сообщений в очередях"""
        try:
            messages_sent = 0
            max_flush_messages = 10  # Ограничиваем количество при закрытии
            
            while (not self._priority_queue.empty() or not self._message_queue.empty()) and messages_sent < max_flush_messages:
                try:
                    if not self._priority_queue.empty():
                        message = self._priority_queue.get_nowait()
                    else:
                        message = self._message_queue.get_nowait()
                    
                    await self._process_single_message(message)
                    messages_sent += 1
                    await asyncio.sleep(0.1)
                    
                except asyncio.QueueEmpty:
                    break
            
            if messages_sent > 0:
                self.logger.info(f"📱 Flushed {messages_sent} messages from queues")
                
        except Exception as e:
            self.logger.error(f"❌ Error flushing queues: {e}")
    
    def get_queue_size(self) -> int:
        """Получение размера очередей"""
        return self._message_queue.qsize() + self._priority_queue.qsize()
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики сервиса"""
        stats = self.stats.copy()
        stats['queue_size'] = self.get_queue_size()
        stats['bot_token_configured'] = bool(self.bot_token)
        stats['default_chat_configured'] = bool(self.default_chat_id)
        
        # Рассчитываем успешность
        total = stats['total_messages']
        if total > 0:
            stats['success_rate'] = stats['successful_messages'] / total
        else:
            stats['success_rate'] = 0.0
        
        return stats
    
    async def health_check(self) -> Dict[str, Any]:
        """Проверка здоровья сервиса"""
        status = "healthy"
        issues = []
        
        if not self.bot_token:
            status = "unhealthy"
            issues.append("Bot token not configured")
        
        if not self.default_chat_id:
            status = "degraded"
            issues.append("Default chat ID not configured")
        
        if self.get_queue_size() > 100:
            status = "degraded"
            issues.append("Message queue too large")
        
        # Проверяем подключение к API
        bot_info = None
        if self.bot_token:
            bot_info = await self._get_me()
            if not bot_info:
                status = "degraded"
                issues.append("Cannot connect to Telegram API")
        
        return {
            'status': status,
            'issues': issues,
            'bot_token_configured': bool(self.bot_token),
            'bot_info': bot_info,
            'queue_size': self.get_queue_size(),
            'session_active': self._session is not None,
            'processor_running': self._processing_task is not None and not self._processing_task.done()
        }


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    import asyncio
    from strategies.base_strategy import create_strategy_result, SignalType
    
    async def test_telegram_service():
        """Тестирование Telegram Service"""
        print("🧪 Testing Telegram Service")
        print("=" * 50)
        
        try:
            service = TelegramService()
            await service.initialize()
            
            # Health check
            health = await service.health_check()
            print(f"🏥 Health: {health['status']}")
            
            if not health['bot_token_configured']:
                print("⚠️ Bot token not configured, skipping message tests")
                return
            
            # Тест отправки простого сообщения
            print("\n📝 Testing simple message...")
            success = await service.send_message("🧪 Test message from Trading Bot!")
            print(f"{'✅' if success else '❌'} Simple message: {success}")
            
            # Тест торгового сигнала
            print("\n📈 Testing trading signal...")
            test_signal = create_strategy_result(
                SignalType.BUY,
                confidence=0.85,
                entry_price=50000.0,
                reasoning="Strong upward momentum"
            )
            
            success = await service.send_trading_signal(
                signal=test_signal,
                symbol="BTCUSDT",
                strategy_name="MovingAverage",
                current_price=50100.0
            )
            print(f"{'✅' if success else '❌'} Trading signal: {success}")
            
            # Ждем обработки очередей
            print("\n⏳ Waiting for message processing...")
            await asyncio.sleep(3)
            
            # Статистика
            stats = service.get_stats()
            print(f"📊 Stats: {stats}")
            
            print("\n✅ Telegram Service test completed!")
            
        except Exception as e:
            print(f"❌ Test failed: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            await service.close()
    
    # Запуск теста
    asyncio.run(test_telegram_service())
