"""
Trading Bot Notification Service
Централизованный сервис для управления всеми типами уведомлений
"""

import asyncio
import time
import json
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Union, Callable
from dataclasses import dataclass, field
from enum import Enum
import uuid

from utils.logger import setup_logger
from utils.helpers import (
    get_current_timestamp, safe_float, Timer
)
from app.config.settings import Settings, get_settings
from data.database import Database
from strategies.base_strategy import StrategyResult, SignalType


# ============================================================================
# CONSTANTS AND ENUMS
# ============================================================================

logger = setup_logger(__name__)

# Настройки уведомлений
DEFAULT_COOLDOWN_MINUTES = 5
MAX_NOTIFICATION_HISTORY = 1000
DUPLICATE_CHECK_WINDOW_MINUTES = 15


class NotificationType(str, Enum):
    """Типы уведомлений"""
    TRADING_SIGNAL = "trading_signal"
    PORTFOLIO_UPDATE = "portfolio_update"
    ERROR_ALERT = "error_alert"
    SYSTEM_STATUS = "system_status"
    MAINTENANCE = "maintenance"
    PERFORMANCE_ALERT = "performance_alert"
    CUSTOM = "custom"


class NotificationChannel(str, Enum):
    """Каналы уведомлений"""
    TELEGRAM = "telegram"
    EMAIL = "email"
    WEBHOOK = "webhook"
    LOG = "log"
    DATABASE = "database"


class NotificationPriority(str, Enum):
    """Приоритеты уведомлений"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"
    CRITICAL = "critical"


class NotificationStatus(str, Enum):
    """Статусы уведомлений"""
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DUPLICATE = "duplicate"


@dataclass
class Notification:
    """Уведомление для отправки"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    type: NotificationType = NotificationType.CUSTOM
    title: str = ""
    message: str = ""
    priority: NotificationPriority = NotificationPriority.NORMAL
    channels: List[NotificationChannel] = field(default_factory=list)
    
    # Метаданные
    source: str = "system"
    tags: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    
    # Управление доставкой
    max_retries: int = 3
    retry_delay: int = 60  # секунды
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES
    
    # Временные метки
    created_at: int = field(default_factory=get_current_timestamp)
    scheduled_at: Optional[int] = None
    sent_at: Optional[int] = None
    
    # Статус
    status: NotificationStatus = NotificationStatus.PENDING
    retry_count: int = 0
    error_message: Optional[str] = None
    
    def __post_init__(self):
        if not self.channels:
            self.channels = [NotificationChannel.TELEGRAM]  # По умолчанию Telegram
        
        if not self.scheduled_at:
            self.scheduled_at = self.created_at
    
    def get_content_hash(self) -> str:
        """Получение хеша содержимого для проверки дубликатов"""
        content = f"{self.type}{self.title}{self.message}{self.source}"
        return hashlib.md5(content.encode()).hexdigest()
    
    def is_ready_to_send(self) -> bool:
        """Проверка готовности к отправке"""
        current_time = get_current_timestamp()
        return (
            self.status == NotificationStatus.PENDING and
            current_time >= self.scheduled_at
        )
    
    def can_retry(self) -> bool:
        """Можно ли повторить отправку"""
        return (
            self.status == NotificationStatus.FAILED and
            self.retry_count < self.max_retries
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь"""
        return {
            'id': self.id,
            'type': self.type.value,
            'title': self.title,
            'message': self.message,
            'priority': self.priority.value,
            'channels': [ch.value for ch in self.channels],
            'source': self.source,
            'tags': self.tags,
            'data': self.data,
            'status': self.status.value,
            'created_at': self.created_at,
            'sent_at': self.sent_at,
            'retry_count': self.retry_count,
            'error_message': self.error_message
        }


@dataclass
class NotificationTemplate:
    """Шаблон уведомления"""
    name: str
    type: NotificationType
    title_template: str
    message_template: str
    default_priority: NotificationPriority = NotificationPriority.NORMAL
    default_channels: List[NotificationChannel] = field(default_factory=list)
    required_fields: List[str] = field(default_factory=list)
    
    def render(self, data: Dict[str, Any]) -> Tuple[str, str]:
        """Рендеринг шаблона с данными"""
        try:
            # Проверяем обязательные поля
            for field in self.required_fields:
                if field not in data:
                    raise ValueError(f"Required field '{field}' missing")
            
            # Рендерим заголовок и сообщение
            title = self.title_template.format(**data)
            message = self.message_template.format(**data)
            
            return title, message
            
        except Exception as e:
            logger.error(f"❌ Template rendering failed: {e}")
            return self.title_template, self.message_template


# ============================================================================
# NOTIFICATION SERVICE CLASS
# ============================================================================

class NotificationService:
    """
    Централизованный сервис управления уведомлениями
    
    Возможности:
    - Централизованная отправка уведомлений
    - Поддержка нескольких каналов доставки
    - Приоритизация и очереди
    - Шаблоны уведомлений
    - Проверка дубликатов
    - Retry механизм
    - Статистика и мониторинг
    """
    
    def __init__(
        self,
        settings: Optional[Settings] = None,
        database: Optional[Database] = None,
        telegram_service=None,
        openai_service=None
    ):
        self.settings = settings or get_settings()
        self.database = database
        self.telegram_service = telegram_service
        self.openai_service = openai_service
        self.logger = setup_logger(f"{__name__}.NotificationService")
        
        # Очереди уведомлений по приоритетам
        self.queues = {
            NotificationPriority.CRITICAL: asyncio.Queue(),
            NotificationPriority.URGENT: asyncio.Queue(),
            NotificationPriority.HIGH: asyncio.Queue(),
            NotificationPriority.NORMAL: asyncio.Queue(),
            NotificationPriority.LOW: asyncio.Queue()
        }
        
        # История уведомлений для проверки дубликатов
        self.notification_history: List[Notification] = []
        
        # Шаблоны уведомлений
        self.templates: Dict[str, NotificationTemplate] = {}
        
        # Обработчики каналов
        self.channel_handlers: Dict[NotificationChannel, Callable] = {}
        
        # Фильтры уведомлений
        self.filters: List[Callable[[Notification], bool]] = []
        
        # Задачи обработки
        self.processor_tasks: List[asyncio.Task] = []
        self.running = False
        
        # Статистика
        self.stats = {
            'total_notifications': 0,
            'sent_notifications': 0,
            'failed_notifications': 0,
            'duplicate_notifications': 0,
            'filtered_notifications': 0,
            'by_type': {},
            'by_channel': {},
            'by_priority': {},
            'avg_delivery_time': 0.0
        }
        
        # Настройки
        self.max_queue_size = 1000
        self.cleanup_interval = 3600  # 1 час
        self.last_cleanup = get_current_timestamp()
        
        self.logger.info("📢 Notification Service initialized")
    
    async def initialize(self):
        """Инициализация сервиса"""
        try:
            # Регистрируем обработчики каналов
            self._register_channel_handlers()
            
            # Загружаем шаблоны по умолчанию
            self._load_default_templates()
            
            # Добавляем фильтры по умолчанию
            self._register_default_filters()
            
            # Запускаем обработчики очередей
            await self._start_processors()
            
            self.running = True
            
            self.logger.info("✅ Notification Service initialized successfully")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize Notification Service: {e}")
            raise
    
    async def close(self):
        """Закрытие сервиса"""
        try:
            self.running = False
            
            # Останавливаем обработчики
            await self._stop_processors()
            
            # Отправляем оставшиеся уведомления
            await self._flush_queues()
            
            self.logger.info("📢 Notification Service closed")
            
        except Exception as e:
            self.logger.error(f"❌ Error closing Notification Service: {e}")
    
    # ========================================================================
    # PUBLIC API METHODS
    # ========================================================================
    
    async def send_notification(
        self,
        notification: Union[Notification, Dict[str, Any]]
    ) -> bool:
        """
        Отправка уведомления
        
        Args:
            notification: Объект уведомления или словарь с параметрами
            
        Returns:
            True если уведомление добавлено в очередь
        """
        try:
            # Конвертируем в объект если нужно
            if isinstance(notification, dict):
                notification = self._dict_to_notification(notification)
            
            # Применяем фильтры
            if not self._apply_filters(notification):
                self.stats['filtered_notifications'] += 1
                self.logger.debug(f"🚫 Notification filtered: {notification.id}")
                return False
            
            # Проверяем дубликаты
            if self._is_duplicate(notification):
                notification.status = NotificationStatus.DUPLICATE
                self.stats['duplicate_notifications'] += 1
                self.logger.debug(f"🔄 Duplicate notification: {notification.id}")
                return False
            
            # Добавляем в очередь по приоритету
            await self.queues[notification.priority].put(notification)
            
            # Добавляем в историю
            self.notification_history.append(notification)
            
            # Обновляем статистику
            self.stats['total_notifications'] += 1
            self._update_type_stats(notification.type, 'total')
            self._update_priority_stats(notification.priority, 'total')
            
            self.logger.debug(f"📢 Notification queued: {notification.id} ({notification.type.value})")
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send notification: {e}")
            return False
    
    async def send_from_template(
        self,
        template_name: str,
        data: Dict[str, Any],
        priority: Optional[NotificationPriority] = None,
        channels: Optional[List[NotificationChannel]] = None
    ) -> bool:
        """
        Отправка уведомления по шаблону
        
        Args:
            template_name: Имя шаблона
            data: Данные для рендеринга
            priority: Приоритет (переопределяет шаблон)
            channels: Каналы (переопределяют шаблон)
            
        Returns:
            True если уведомление отправлено
        """
        try:
            if template_name not in self.templates:
                self.logger.error(f"❌ Template not found: {template_name}")
                return False
            
            template = self.templates[template_name]
            
            # Рендерим шаблон
            title, message = template.render(data)
            
            # Создаем уведомление
            notification = Notification(
                type=template.type,
                title=title,
                message=message,
                priority=priority or template.default_priority,
                channels=channels or template.default_channels,
                source=data.get('source', 'template'),
                data=data
            )
            
            return await self.send_notification(notification)
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send template notification: {e}")
            return False
    
    async def send_trading_signal(
        self,
        signal: StrategyResult,
        symbol: str,
        strategy_name: str,
        current_price: float,
        additional_info: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Отправка торгового сигнала"""
        
        try:
            # Определяем приоритет по уверенности
            if signal.confidence >= 0.9:
                priority = NotificationPriority.URGENT
            elif signal.confidence >= 0.8:
                priority = NotificationPriority.HIGH
            else:
                priority = NotificationPriority.NORMAL
            
            # Подготавливаем данные
            data = {
                'signal_type': signal.signal_type.value,
                'symbol': symbol,
                'strategy_name': strategy_name,
                'confidence': signal.confidence,
                'current_price': current_price,
                'entry_price': signal.entry_price or current_price,
                'stop_loss': signal.stop_loss,
                'take_profit': signal.take_profit,
                'reasoning': signal.reasoning or 'No reasoning provided',
                'timestamp': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            }
            
            if additional_info:
                data.update(additional_info)
            
            return await self.send_from_template(
                'trading_signal',
                data,
                priority=priority
            )
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send trading signal notification: {e}")
            return False
    
    async def send_error_alert(
        self,
        error_message: str,
        component: str,
        severity: str = "medium",
        additional_info: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Отправка уведомления об ошибке"""
        
        try:
            # Определяем приоритет по серьезности
            priority_map = {
                'low': NotificationPriority.LOW,
                'medium': NotificationPriority.NORMAL,
                'high': NotificationPriority.HIGH,
                'critical': NotificationPriority.CRITICAL
            }
            
            priority = priority_map.get(severity, NotificationPriority.NORMAL)
            
            data = {
                'error_message': error_message,
                'component': component,
                'severity': severity,
                'timestamp': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            }
            
            if additional_info:
                data.update(additional_info)
            
            return await self.send_from_template(
                'error_alert',
                data,
                priority=priority
            )
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send error alert: {e}")
            return False
    
    async def send_portfolio_update(
        self,
        balance: float,
        unrealized_pnl: float,
        daily_pnl: float,
        metrics: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Отправка обновления портфеля"""
        
        try:
            data = {
                'balance': balance,
                'unrealized_pnl': unrealized_pnl,
                'daily_pnl': daily_pnl,
                'timestamp': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            }
            
            if metrics:
                data.update(metrics)
            
            return await self.send_from_template(
                'portfolio_update',
                data,
                priority=NotificationPriority.LOW
            )
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send portfolio update: {e}")
            return False
    
    async def send_system_status(
        self,
        status: str,
        components: Dict[str, str],
        metrics: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Отправка статуса системы"""
        
        try:
            data = {
                'status': status,
                'components': components,
                'timestamp': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            }
            
            if metrics:
                data.update(metrics)
            
            return await self.send_from_template(
                'system_status',
                data,
                priority=NotificationPriority.LOW
            )
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send system status: {e}")
            return False
    
    # ========================================================================
    # TEMPLATE MANAGEMENT
    # ========================================================================
    
    def register_template(self, template: NotificationTemplate) -> bool:
        """Регистрация шаблона уведомления"""
        try:
            self.templates[template.name] = template
            self.logger.info(f"📝 Template registered: {template.name}")
            return True
        except Exception as e:
            self.logger.error(f"❌ Failed to register template: {e}")
            return False
    
    def get_template(self, name: str) -> Optional[NotificationTemplate]:
        """Получение шаблона по имени"""
        return self.templates.get(name)
    
    def list_templates(self) -> List[str]:
        """Список всех шаблонов"""
        return list(self.templates.keys())
    
    def _load_default_templates(self):
        """Загрузка шаблонов по умолчанию"""
        
        # Шаблон торгового сигнала
        trading_signal_template = NotificationTemplate(
            name="trading_signal",
            type=NotificationType.TRADING_SIGNAL,
            title_template="🔔 Trading Signal: {signal_type} {symbol}",
            message_template="""📈 **TRADING SIGNAL**

**Symbol:** {symbol}
**Strategy:** {strategy_name}
**Signal:** {signal_type}
**Confidence:** {confidence:.1%}
**Current Price:** ${current_price:.4f}
**Entry Price:** ${entry_price:.4f}

**Analysis:** {reasoning}

🕐 {timestamp}""",
            default_priority=NotificationPriority.HIGH,
            default_channels=[NotificationChannel.TELEGRAM],
            required_fields=['signal_type', 'symbol', 'strategy_name', 'confidence', 'current_price']
        )
        
        # Шаблон ошибки
        error_alert_template = NotificationTemplate(
            name="error_alert",
            type=NotificationType.ERROR_ALERT,
            title_template="⚠️ Error Alert: {component}",
            message_template="""🚨 **ERROR ALERT**

**Component:** {component}
**Severity:** {severity}
**Error:** {error_message}

🕐 {timestamp}""",
            default_priority=NotificationPriority.HIGH,
            default_channels=[NotificationChannel.TELEGRAM],
            required_fields=['error_message', 'component', 'severity']
        )
        
        # Шаблон портфеля
        portfolio_update_template = NotificationTemplate(
            name="portfolio_update",
            type=NotificationType.PORTFOLIO_UPDATE,
            title_template="💰 Portfolio Update",
            message_template="""📊 **PORTFOLIO UPDATE**

**Balance:** ${balance:.2f}
**Unrealized P&L:** ${unrealized_pnl:+.2f}
**Daily P&L:** ${daily_pnl:+.2f}

🕐 {timestamp}""",
            default_priority=NotificationPriority.LOW,
            default_channels=[NotificationChannel.TELEGRAM],
            required_fields=['balance', 'unrealized_pnl', 'daily_pnl']
        )
        
        # Шаблон статуса системы
        system_status_template = NotificationTemplate(
            name="system_status",
            type=NotificationType.SYSTEM_STATUS,
            title_template="⚙️ System Status: {status}",
            message_template="""🔧 **SYSTEM STATUS**

**Status:** {status}
**Components:** {components}

🕐 {timestamp}""",
            default_priority=NotificationPriority.LOW,
            default_channels=[NotificationChannel.TELEGRAM],
            required_fields=['status', 'components']
        )
        
        # Регистрируем шаблоны
        templates = [
            trading_signal_template,
            error_alert_template,
            portfolio_update_template,
            system_status_template
        ]
        
        for template in templates:
            self.register_template(template)
    
    # ========================================================================
    # CHANNEL HANDLERS
    # ========================================================================
    
    def _register_channel_handlers(self):
        """Регистрация обработчиков каналов"""
        self.channel_handlers = {
            NotificationChannel.TELEGRAM: self._handle_telegram_channel,
            NotificationChannel.LOG: self._handle_log_channel,
            NotificationChannel.DATABASE: self._handle_database_channel
        }
    
    async def _handle_telegram_channel(self, notification: Notification) -> bool:
        """Обработчик Telegram канала"""
        if not self.telegram_service:
            self.logger.warning("⚠️ Telegram service not available")
            return False
        
        try:
            # Определяем приоритет для Telegram
            telegram_priority_map = {
                NotificationPriority.CRITICAL: "urgent",
                NotificationPriority.URGENT: "high", 
                NotificationPriority.HIGH: "high",
                NotificationPriority.NORMAL: "normal",
                NotificationPriority.LOW: "low"
            }
            
            # Формируем сообщение
            if notification.title:
                message = f"{notification.title}\n\n{notification.message}"
            else:
                message = notification.message
            
            # Отправляем через Telegram сервис
            success = await self.telegram_service.send_message(
                text=message,
                priority=telegram_priority_map.get(notification.priority, "normal")
            )
            
            if success:
                self._update_channel_stats(NotificationChannel.TELEGRAM, 'success')
            else:
                self._update_channel_stats(NotificationChannel.TELEGRAM, 'failed')
            
            return success
            
        except Exception as e:
            self.logger.error(f"❌ Telegram channel error: {e}")
            self._update_channel_stats(NotificationChannel.TELEGRAM, 'failed')
            return False
    
    async def _handle_log_channel(self, notification: Notification) -> bool:
        """Обработчик канала логирования"""
        try:
            # Определяем уровень логирования по приоритету
            log_level_map = {
                NotificationPriority.CRITICAL: "error",
                NotificationPriority.URGENT: "error",
                NotificationPriority.HIGH: "warning",
                NotificationPriority.NORMAL: "info",
                NotificationPriority.LOW: "debug"
            }
            
            level = log_level_map.get(notification.priority, "info")
            message = f"[{notification.type.value.upper()}] {notification.title}: {notification.message}"
            
            getattr(self.logger, level)(message)
            
            self._update_channel_stats(NotificationChannel.LOG, 'success')
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Log channel error: {e}")
            self._update_channel_stats(NotificationChannel.LOG, 'failed')
            return False
    
    async def _handle_database_channel(self, notification: Notification) -> bool:
        """Обработчик канала базы данных"""
        if not self.database:
            return False
        
        try:
            # Сохраняем уведомление в БД (можно расширить для специальной таблицы)
            notification_data = notification.to_dict()
            # await self.database.save_notification(notification_data)  # Implement if needed
            
            self._update_channel_stats(NotificationChannel.DATABASE, 'success')
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Database channel error: {e}")
            self._update_channel_stats(NotificationChannel.DATABASE, 'failed')
            return False
    
    # ========================================================================
    # FILTERING AND PROCESSING
    # ========================================================================
    
    def _register_default_filters(self):
        """Регистрация фильтров по умолчанию"""
        
        # Фильтр cooldown периодов
        def cooldown_filter(notification: Notification) -> bool:
            if notification.cooldown_minutes <= 0:
                return True
            
            current_time = get_current_timestamp()
            cooldown_ms = notification.cooldown_minutes * 60 * 1000
            
            # Проверяем историю на похожие уведомления
            for prev_notification in self.notification_history[-100:]:  # Последние 100
                if (
                    prev_notification.type == notification.type and
                    prev_notification.source == notification.source and
                    prev_notification.status == NotificationStatus.SENT and
                    prev_notification.sent_at and
                    (current_time - prev_notification.sent_at) < cooldown_ms
                ):
                    return False
            
            return True
        
        # Фильтр размера очередей
        def queue_size_filter(notification: Notification) -> bool:
            current_queue_size = sum(q.qsize() for q in self.queues.values())
            return current_queue_size < self.max_queue_size
        
        self.filters = [cooldown_filter, queue_size_filter]
    
    def add_filter(self, filter_func: Callable[[Notification], bool]):
        """Добавление пользовательского фильтра"""
        self.filters.append(filter_func)
    
    def _apply_filters(self, notification: Notification) -> bool:
        """Применение всех фильтров"""
        for filter_func in self.filters:
            try:
                if not filter_func(notification):
                    return False
            except Exception as e:
                self.logger.error(f"❌ Filter error: {e}")
                continue
        return True
    
    def _is_duplicate(self, notification: Notification) -> bool:
        """Проверка на дубликаты"""
        content_hash = notification.get_content_hash()
        current_time = get_current_timestamp()
        window_ms = DUPLICATE_CHECK_WINDOW_MINUTES * 60 * 1000
        
        for prev_notification in self.notification_history[-50:]:  # Последние 50
            if (
                prev_notification.get_content_hash() == content_hash and
                (current_time - prev_notification.created_at) < window_ms and
                prev_notification.status in [NotificationStatus.SENT, NotificationStatus.PENDING]
            ):
                return True
        
        return False
    
    # ========================================================================
    # QUEUE PROCESSING
    # ========================================================================
    
    async def _start_processors(self):
        """Запуск обработчиков очередей"""
        priority_order = [
            NotificationPriority.CRITICAL,
            NotificationPriority.URGENT,
            NotificationPriority.HIGH,
            NotificationPriority.NORMAL,
            NotificationPriority.LOW
        ]
        
        for priority in priority_order:
            task = asyncio.create_task(self._process_queue(priority))
            self.processor_tasks.append(task)
        
        # Задача очистки
        cleanup_task = asyncio.create_task(self._cleanup_history())
        self.processor_tasks.append(cleanup_task)
        
        self.logger.info(f"⚡ Started {len(self.processor_tasks)} processor tasks")
    
    async def _stop_processors(self):
        """Остановка обработчиков"""
        for task in self.processor_tasks:
            task.cancel()
        
        # Ждем завершения задач
        await asyncio.gather(*self.processor_tasks, return_exceptions=True)
        self.processor_tasks.clear()
        
        self.logger.info("⏹️ All processor tasks stopped")
    
    async def _process_queue(self, priority: NotificationPriority):
        """Обработка очереди определенного приоритета"""
        queue = self.queues[priority]
        
        self.logger.info(f"⚡ Queue processor started for {priority.value} priority")
        
        try:
            while self.running:
                try:
                    # Получаем уведомление с таймаутом
                    notification = await asyncio.wait_for(queue.get(), timeout=1.0)
                    
                    # Обрабатываем уведомление
                    await self._process_notification(notification)
                    
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.error(f"❌ Queue processing error ({priority.value}): {e}")
                    await asyncio.sleep(1)
                    
        except asyncio.CancelledError:
            self.logger.info(f"⏹️ Queue processor stopped for {priority.value}")
    
    async def _process_notification(self, notification: Notification):
        """Обработка одного уведомления"""
        try:
            start_time = time.time()
            
            # Проверяем готовность к отправке
            if not notification.is_ready_to_send():
                # Возвращаем в очередь если не готово
                await self.queues[notification.priority].put(notification)
                return
            
            success_channels = []
            failed_channels = []
            
            # Отправляем через все каналы
            for channel in notification.channels:
                if channel in self.channel_handlers:
                    try:
                        handler = self.channel_handlers[channel]
                        success = await handler(notification)
                        
                        if success:
                            success_channels.append(channel)
                        else:
                            failed_channels.append(channel)
                            
                    except Exception as e:
                        self.logger.error(f"❌ Channel handler error ({channel.value}): {e}")
                        failed_channels.append(channel)
                else:
                    self.logger.warning(f"⚠️ Unknown channel: {channel.value}")
                    failed_channels.append(channel)
            
            # Определяем статус
            if success_channels and not failed_channels:
                notification.status = NotificationStatus.SENT
                notification.sent_at = get_current_timestamp()
                self.stats['sent_notifications'] += 1
                self._update_type_stats(notification.type, 'sent')
                
                # Обновляем среднее время доставки
                delivery_time = time.time() - start_time
                if self.stats['sent_notifications'] > 0:
                    current_avg = self.stats['avg_delivery_time']
                    new_avg = (current_avg * (self.stats['sent_notifications'] - 1) + delivery_time) / self.stats['sent_notifications']
                    self.stats['avg_delivery_time'] = new_avg
                
                self.logger.debug(f"✅ Notification sent: {notification.id} via {success_channels}")
                
            elif failed_channels and notification.can_retry():
                # Retry если возможно
                notification.retry_count += 1
                notification.scheduled_at = get_current_timestamp() + (notification.retry_delay * 1000)
                await self.queues[notification.priority].put(notification)
                
                self.logger.warning(f"🔄 Notification retry {notification.retry_count}/{notification.max_retries}: {notification.id}")
                
            else:
                # Полностью провалено
                notification.status = NotificationStatus.FAILED
                notification.error_message = f"Failed channels: {failed_channels}"
                self.stats['failed_notifications'] += 1
                self._update_type_stats(notification.type, 'failed')
                
                self.logger.error(f"❌ Notification failed: {notification.id}")
            
        except Exception as e:
            notification.status = NotificationStatus.FAILED
            notification.error_message = str(e)
            self.stats['failed_notifications'] += 1
            self.logger.error(f"❌ Notification processing error: {e}")
    
    async def _flush_queues(self):
        """Отправка оставшихся уведомлений"""
        try:
            total_flushed = 0
            max_flush = 50  # Ограничиваем количество при закрытии
            
            for priority, queue in self.queues.items():
                flushed_count = 0
                while not queue.empty() and total_flushed < max_flush:
                    try:
                        notification = queue.get_nowait()
                        await self._process_notification(notification)
                        flushed_count += 1
                        total_flushed += 1
                    except asyncio.QueueEmpty:
                        break
                
                if flushed_count > 0:
                    self.logger.info(f"📤 Flushed {flushed_count} {priority.value} notifications")
            
            if total_flushed > 0:
                self.logger.info(f"📤 Total flushed: {total_flushed} notifications")
                
        except Exception as e:
            self.logger.error(f"❌ Error flushing queues: {e}")
    
    # ========================================================================
    # CLEANUP AND MAINTENANCE
    # ========================================================================
    
    async def _cleanup_history(self):
        """Периодическая очистка истории"""
        try:
            while self.running:
                current_time = get_current_timestamp()
                
                if (current_time - self.last_cleanup) > (self.cleanup_interval * 1000):
                    # Удаляем старые записи (старше 24 часов)
                    cutoff_time = current_time - (24 * 60 * 60 * 1000)
                    
                    old_count = len(self.notification_history)
                    self.notification_history = [
                        n for n in self.notification_history
                        if n.created_at > cutoff_time
                    ]
                    
                    # Ограничиваем размер истории
                    if len(self.notification_history) > MAX_NOTIFICATION_HISTORY:
                        self.notification_history = self.notification_history[-MAX_NOTIFICATION_HISTORY:]
                    
                    removed = old_count - len(self.notification_history)
                    if removed > 0:
                        self.logger.info(f"🧹 Cleaned up {removed} old notifications")
                    
                    self.last_cleanup = current_time
                
                await asyncio.sleep(300)  # Проверяем каждые 5 минут
                
        except asyncio.CancelledError:
            pass
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    def _dict_to_notification(self, data: Dict[str, Any]) -> Notification:
        """Конвертация словаря в объект уведомления"""
        return Notification(
            type=NotificationType(data.get('type', 'custom')),
            title=data.get('title', ''),
            message=data.get('message', ''),
            priority=NotificationPriority(data.get('priority', 'normal')),
            channels=[NotificationChannel(ch) for ch in data.get('channels', ['telegram'])],
            source=data.get('source', 'api'),
            tags=data.get('tags', []),
            data=data.get('data', {}),
            max_retries=data.get('max_retries', 3),
            cooldown_minutes=data.get('cooldown_minutes', DEFAULT_COOLDOWN_MINUTES)
        )
    
    def _update_type_stats(self, notification_type: NotificationType, action: str):
        """Обновление статистики по типам"""
        if notification_type.value not in self.stats['by_type']:
            self.stats['by_type'][notification_type.value] = {'total': 0, 'sent': 0, 'failed': 0}
        
        self.stats['by_type'][notification_type.value][action] += 1
    
    def _update_priority_stats(self, priority: NotificationPriority, action: str):
        """Обновление статистики по приоритетам"""
        if priority.value not in self.stats['by_priority']:
            self.stats['by_priority'][priority.value] = {'total': 0, 'sent': 0, 'failed': 0}
        
        if action in self.stats['by_priority'][priority.value]:
            self.stats['by_priority'][priority.value][action] += 1
    
    def _update_channel_stats(self, channel: NotificationChannel, result: str):
        """Обновление статистики по каналам"""
        if channel.value not in self.stats['by_channel']:
            self.stats['by_channel'][channel.value] = {'success': 0, 'failed': 0}
        
        if result in self.stats['by_channel'][channel.value]:
            self.stats['by_channel'][channel.value][result] += 1
    
    def get_queue_sizes(self) -> Dict[str, int]:
        """Получение размеров очередей"""
        return {priority.value: queue.qsize() for priority, queue in self.queues.items()}
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики сервиса"""
        stats = self.stats.copy()
        stats['queue_sizes'] = self.get_queue_sizes()
        stats['total_queue_size'] = sum(self.get_queue_sizes().values())
        stats['history_size'] = len(self.notification_history)
        stats['running'] = self.running
        stats['registered_templates'] = len(self.templates)
        stats['active_processors'] = len([t for t in self.processor_tasks if not t.done()])
        
        return stats
    
    async def health_check(self) -> Dict[str, Any]:
        """Проверка здоровья сервиса"""
        status = "healthy"
        issues = []
        
        if not self.running:
            status = "unhealthy"
            issues.append("Service not running")
        
        # Проверяем очереди
        total_queue_size = sum(self.get_queue_sizes().values())
        if total_queue_size > self.max_queue_size * 0.8:
            status = "degraded"
            issues.append("High queue utilization")
        
        # Проверяем обработчики
        active_processors = len([t for t in self.processor_tasks if not t.done()])
        expected_processors = len(self.queues) + 1  # +1 для cleanup задачи
        
        if active_processors < expected_processors:
            status = "degraded" 
            issues.append("Some processors not running")
        
        # Проверяем каналы
        channel_health = {}
        for channel in self.channel_handlers:
            if channel == NotificationChannel.TELEGRAM and self.telegram_service:
                tg_health = await self.telegram_service.health_check()
                channel_health['telegram'] = tg_health['status']
            else:
                channel_health[channel.value] = "unknown"
        
        return {
            'status': status,
            'issues': issues,
            'running': self.running,
            'queue_sizes': self.get_queue_sizes(),
            'total_queue_size': total_queue_size,
            'active_processors': active_processors,
            'expected_processors': expected_processors,
            'channel_health': channel_health,
            'templates_loaded': len(self.templates)
        }


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    import asyncio
    from strategies.base_strategy import create_strategy_result, SignalType
    
    async def test_notification_service():
        """Тестирование Notification Service"""
        print("🧪 Testing Notification Service")
        print("=" * 50)
        
        try:
            service = NotificationService()
            await service.initialize()
            
            # Health check
            health = await service.health_check()
            print(f"🏥 Health: {health['status']}")
            
            # Тест простого уведомления
            print("\n📝 Testing simple notification...")
            success = await service.send_notification({
                'type': 'custom',
                'title': 'Test Notification',
                'message': 'This is a test notification',
                'priority': 'normal',
                'channels': ['log']
            })
            print(f"{'✅' if success else '❌'} Simple notification: {success}")
            
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
                strategy_name="TestStrategy",
                current_price=50100.0
            )
            print(f"{'✅' if success else '❌'} Trading signal: {success}")
            
            # Тест ошибки
            print("\n🚨 Testing error alert...")
            success = await service.send_error_alert(
                error_message="Test error message",
                component="TestComponent",
                severity="medium"
            )
            print(f"{'✅' if success else '❌'} Error alert: {success}")
            
            # Ждем обработки
            print("\n⏳ Waiting for processing...")
            await asyncio.sleep(2)
            
            # Статистика
            stats = service.get_stats()
            print(f"📊 Stats: {stats}")
            
            print("\n✅ Notification Service test completed!")
            
        except Exception as e:
            print(f"❌ Test failed: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            await service.close()
    
    # Запуск теста
    asyncio.run(test_notification_service())
