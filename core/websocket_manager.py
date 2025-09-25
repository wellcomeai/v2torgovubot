"""
Trading Bot WebSocket Manager
Менеджер WebSocket соединений с Bybit для получения real-time данных
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Callable, Set, Union
from dataclasses import dataclass
from enum import Enum
import weakref
from contextlib import asynccontextmanager

import websockets
from websockets.exceptions import ConnectionClosedError, InvalidStatusCode, InvalidMessage
import aiohttp

from utils.logger import setup_logger, get_websocket_logger
from utils.helpers import (
    get_current_timestamp, retry_async, generate_signature, 
    safe_json_loads, safe_json_dumps, Timer
)
from app.config.settings import Settings, get_settings
from data.historical_data import CandleData


# ============================================================================
# CONSTANTS AND ENUMS
# ============================================================================

# Bybit WebSocket URLs
BYBIT_WS_MAINNET_PUBLIC = "wss://stream.bybit.com/v5/public/linear"
BYBIT_WS_MAINNET_PRIVATE = "wss://stream.bybit.com/v5/private"
BYBIT_WS_TESTNET_PUBLIC = "wss://stream-testnet.bybit.com/v5/public/linear"
BYBIT_WS_TESTNET_PRIVATE = "wss://stream-testnet.bybit.com/v5/private"

# WebSocket операции
WS_OPERATIONS = {
    'SUBSCRIBE': 'subscribe',
    'UNSUBSCRIBE': 'unsubscribe',
    'AUTH': 'auth'
}

# Топики подписок
BYBIT_TOPICS = {
    'KLINE': 'kline',
    'TICKER': 'tickers',
    'ORDERBOOK': 'orderbook',
    'TRADE': 'publicTrade',
    'POSITION': 'position',
    'ORDER': 'order',
    'EXECUTION': 'execution',
    'WALLET': 'wallet'
}

# Интервалы для kline
KLINE_INTERVALS = ['1', '3', '5', '15', '30', '60', '120', '240', '360', '720', 'D', 'W', 'M']


class WSConnectionStatus(str, Enum):
    """Статусы WebSocket соединения"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting" 
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass
class WSSubscription:
    """Подписка WebSocket"""
    topic: str
    symbol: Optional[str] = None
    interval: Optional[str] = None
    callback: Optional[Callable] = None
    active: bool = True
    subscribed_at: int = None
    
    def __post_init__(self):
        if self.subscribed_at is None:
            self.subscribed_at = get_current_timestamp()
    
    @property
    def subscription_key(self) -> str:
        """Уникальный ключ подписки"""
        parts = [self.topic]
        if self.interval:
            parts.append(self.interval)
        if self.symbol:
            parts.append(self.symbol)
        return '.'.join(parts)


@dataclass 
class WSMessage:
    """WebSocket сообщение"""
    topic: str
    data: Any
    timestamp: int
    success: bool = True
    error_message: Optional[str] = None
    
    @classmethod
    def from_bybit_message(cls, raw_message: Dict[str, Any]) -> 'WSMessage':
        """Создание из сырого сообщения Bybit"""
        try:
            topic = raw_message.get('topic', 'unknown')
            data = raw_message.get('data', [])
            timestamp = raw_message.get('ts', get_current_timestamp())
            
            return cls(
                topic=topic,
                data=data,
                timestamp=timestamp,
                success=True
            )
        except Exception as e:
            return cls(
                topic='error',
                data=None,
                timestamp=get_current_timestamp(),
                success=False,
                error_message=str(e)
            )


# ============================================================================
# WEBSOCKET MANAGER CLASS
# ============================================================================

class WebSocketManager:
    """
    Менеджер WebSocket соединений с Bybit
    
    Возможности:
    - Подключение к публичным и приватным потокам
    - Автоматическое переподключение
    - Управление подписками
    - Обработка различных типов сообщений
    - Rate limiting и error handling
    """
    
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.logger = get_websocket_logger()
        
        # WebSocket connections
        self.public_ws: Optional[websockets.WebSocketServerProtocol] = None
        self.private_ws: Optional[websockets.WebSocketServerProtocol] = None
        
        # Connection status
        self.public_status = WSConnectionStatus.DISCONNECTED
        self.private_status = WSConnectionStatus.DISCONNECTED
        
        # URLs based on testnet setting
        self.public_url = BYBIT_WS_TESTNET_PUBLIC if self.settings.BYBIT_TESTNET else BYBIT_WS_MAINNET_PUBLIC
        self.private_url = BYBIT_WS_TESTNET_PRIVATE if self.settings.BYBIT_TESTNET else BYBIT_WS_MAINNET_PRIVATE
        
        # Subscriptions
        self.subscriptions: Dict[str, WSSubscription] = {}
        self.callbacks: Dict[str, List[Callable]] = {}
        
        # Connection management
        self._running = False
        self._reconnect_attempts = {}
        self._last_ping = {}
        self._ping_interval = self.settings.WS_PING_INTERVAL
        self._ping_timeout = self.settings.WS_PING_TIMEOUT
        
        # Message queues
        self._message_queues: Dict[str, asyncio.Queue] = {
            'public': asyncio.Queue(maxsize=1000),
            'private': asyncio.Queue(maxsize=1000)
        }
        
        # Statistics
        self.stats = {
            'messages_received': 0,
            'messages_processed': 0,
            'subscription_count': 0,
            'reconnect_count': 0,
            'errors': 0,
            'uptime_start': None
        }
        
        # Tasks
        self._tasks: Set[asyncio.Task] = set()
        
        self.logger.info(f"🌐 WebSocket Manager initialized (testnet: {self.settings.BYBIT_TESTNET})")
    
    # ========================================================================
    # CONNECTION MANAGEMENT
    # ========================================================================
    
    async def start(self) -> None:
        """Запуск WebSocket менеджера"""
        if self._running:
            return
        
        try:
            self.logger.info("🚀 Starting WebSocket Manager...")
            self._running = True
            self.stats['uptime_start'] = get_current_timestamp()
            
            # Запускаем фоновые задачи
            self._start_background_tasks()
            
            # Подключаемся к публичному потоку
            await self._connect_public()
            
            self.logger.info("✅ WebSocket Manager started successfully")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to start WebSocket Manager: {e}")
            await self.stop()
            raise
    
    async def stop(self) -> None:
        """Остановка WebSocket менеджера"""
        if not self._running:
            return
        
        try:
            self.logger.info("🔄 Stopping WebSocket Manager...")
            self._running = False
            
            # Отменяем все задачи
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            
            # Закрываем соединения
            await self._disconnect_all()
            
            self.logger.info("✅ WebSocket Manager stopped")
            
        except Exception as e:
            self.logger.error(f"❌ Error stopping WebSocket Manager: {e}")
    
    def _start_background_tasks(self) -> None:
        """Запуск фоновых задач"""
        # Задача обработки сообщений
        task = asyncio.create_task(
            self._message_processor(),
            name="ws_message_processor"
        )
        self._tasks.add(task)
        
        # Задача ping/pong
        task = asyncio.create_task(
            self._heartbeat_task(),
            name="ws_heartbeat"
        )
        self._tasks.add(task)
        
        # Задача мониторинга соединений
        task = asyncio.create_task(
            self._connection_monitor(),
            name="ws_connection_monitor"
        )
        self._tasks.add(task)
    
    async def _connect_public(self) -> None:
        """Подключение к публичному потоку"""
        try:
            self.public_status = WSConnectionStatus.CONNECTING
            self.logger.info(f"🔌 Connecting to public WebSocket: {self.public_url}")
            
            # Создаем подключение с таймаутом
            async with asyncio.timeout(30):
                self.public_ws = await websockets.connect(
                    self.public_url,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    max_size=2**20,  # 1MB max message size
                    compression=None  # Отключаем компрессию для стабильности
                )
            
            self.public_status = WSConnectionStatus.CONNECTED
            self.logger.info("✅ Connected to public WebSocket")
            
            # Запускаем обработчик сообщений
            task = asyncio.create_task(
                self._handle_public_messages(),
                name="public_ws_handler"
            )
            self._tasks.add(task)
            
            # Восстанавливаем подписки
            await self._restore_subscriptions('public')
            
        except Exception as e:
            self.public_status = WSConnectionStatus.FAILED
            self.logger.error(f"❌ Failed to connect to public WebSocket: {e}")
            # Планируем переподключение
            asyncio.create_task(self._schedule_reconnect('public'))
    
    async def _connect_private(self) -> None:
        """Подключение к приватному потоку"""
        try:
            self.private_status = WSConnectionStatus.CONNECTING
            self.logger.info(f"🔐 Connecting to private WebSocket: {self.private_url}")
            
            async with asyncio.timeout(30):
                self.private_ws = await websockets.connect(
                    self.private_url,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    max_size=2**20,
                    compression=None
                )
            
            # Аутентификация
            await self._authenticate_private()
            
            self.private_status = WSConnectionStatus.CONNECTED
            self.logger.info("✅ Connected and authenticated to private WebSocket")
            
            # Запускаем обработчик сообщений
            task = asyncio.create_task(
                self._handle_private_messages(),
                name="private_ws_handler"
            )
            self._tasks.add(task)
            
            # Восстанавливаем приватные подписки
            await self._restore_subscriptions('private')
            
        except Exception as e:
            self.private_status = WSConnectionStatus.FAILED
            self.logger.error(f"❌ Failed to connect to private WebSocket: {e}")
            asyncio.create_task(self._schedule_reconnect('private'))
    
    async def _authenticate_private(self) -> None:
        """Аутентификация для приватного потока"""
        try:
            expires = int((time.time() + 10) * 1000)  # 10 секунд на будущее
            signature_payload = f"GET/realtime{expires}"
            
            signature = generate_signature(
                self.settings.BYBIT_API_SECRET,
                signature_payload
            )
            
            auth_message = {
                "op": "auth",
                "args": [
                    self.settings.BYBIT_API_KEY,
                    expires,
                    signature
                ]
            }
            
            await self.private_ws.send(safe_json_dumps(auth_message))
            self.logger.debug("🔐 Authentication message sent")
            
        except Exception as e:
            self.logger.error(f"❌ Private WebSocket authentication failed: {e}")
            raise
    
    async def _disconnect_all(self) -> None:
        """Отключение всех соединений"""
        disconnect_tasks = []
        
        if self.public_ws and not self.public_ws.closed:
            disconnect_tasks.append(self._close_connection(self.public_ws, 'public'))
        
        if self.private_ws and not self.private_ws.closed:
            disconnect_tasks.append(self._close_connection(self.private_ws, 'private'))
        
        if disconnect_tasks:
            await asyncio.gather(*disconnect_tasks, return_exceptions=True)
        
        self.public_status = WSConnectionStatus.DISCONNECTED
        self.private_status = WSConnectionStatus.DISCONNECTED
    
    async def _close_connection(self, ws: websockets.WebSocketServerProtocol, connection_type: str) -> None:
        """Закрытие WebSocket соединения"""
        try:
            if not ws.closed:
                await ws.close()
            self.logger.debug(f"🔌 {connection_type.title()} WebSocket connection closed")
        except Exception as e:
            self.logger.error(f"❌ Error closing {connection_type} WebSocket: {e}")
    
    # ========================================================================
    # MESSAGE HANDLING
    # ========================================================================
    
    async def _handle_public_messages(self) -> None:
        """Обработка сообщений публичного потока"""
        try:
            async for message in self.public_ws:
                try:
                    if isinstance(message, str):
                        data = safe_json_loads(message, {})
                        if data:
                            await self._message_queues['public'].put(data)
                            self.stats['messages_received'] += 1
                            
                except Exception as e:
                    self.logger.error(f"❌ Error processing public message: {e}")
                    self.stats['errors'] += 1
                    
        except ConnectionClosedError:
            self.logger.warning("⚠️ Public WebSocket connection closed")
            self.public_status = WSConnectionStatus.DISCONNECTED
            asyncio.create_task(self._schedule_reconnect('public'))
            
        except Exception as e:
            self.logger.error(f"❌ Public message handler error: {e}")
            self.public_status = WSConnectionStatus.FAILED
            asyncio.create_task(self._schedule_reconnect('public'))
    
    async def _handle_private_messages(self) -> None:
        """Обработка сообщений приватного потока"""
        try:
            async for message in self.private_ws:
                try:
                    if isinstance(message, str):
                        data = safe_json_loads(message, {})
                        if data:
                            await self._message_queues['private'].put(data)
                            self.stats['messages_received'] += 1
                            
                except Exception as e:
                    self.logger.error(f"❌ Error processing private message: {e}")
                    self.stats['errors'] += 1
                    
        except ConnectionClosedError:
            self.logger.warning("⚠️ Private WebSocket connection closed")
            self.private_status = WSConnectionStatus.DISCONNECTED
            asyncio.create_task(self._schedule_reconnect('private'))
            
        except Exception as e:
            self.logger.error(f"❌ Private message handler error: {e}")
            self.private_status = WSConnectionStatus.FAILED
            asyncio.create_task(self._schedule_reconnect('private'))
    
    async def _message_processor(self) -> None:
        """Основной процессор сообщений"""
        while self._running:
            try:
                # Обрабатываем публичные сообщения
                await self._process_queue_messages('public')
                
                # Обрабатываем приватные сообщения
                await self._process_queue_messages('private')
                
                # Небольшая пауза
                await asyncio.sleep(0.001)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Message processor error: {e}")
                await asyncio.sleep(1)
    
    async def _process_queue_messages(self, queue_type: str) -> None:
        """Обработка сообщений из очереди"""
        queue = self._message_queues[queue_type]
        
        try:
            # Обрабатываем до 50 сообщений за раз
            for _ in range(50):
                try:
                    message_data = queue.get_nowait()
                    await self._process_message(message_data, queue_type)
                    self.stats['messages_processed'] += 1
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break
        except Exception as e:
            self.logger.error(f"❌ Error processing {queue_type} queue: {e}")
    
    async def _process_message(self, message_data: Dict[str, Any], source: str) -> None:
        """Обработка отдельного сообщения"""
        try:
            # Проверяем тип сообщения
            if 'success' in message_data:
                # Сообщение о результате операции
                await self._handle_operation_response(message_data)
                return
            
            if 'topic' not in message_data:
                return
            
            # Создаем объект сообщения
            ws_message = WSMessage.from_bybit_message(message_data)
            
            if not ws_message.success:
                self.logger.error(f"❌ Failed to parse message: {ws_message.error_message}")
                return
            
            # Обрабатываем по типу топика
            await self._dispatch_message(ws_message)
            
            # Логируем для отладки
            if self.settings.LOG_WEBSOCKET_MESSAGES:
                self.logger.debug(f"📨 {source} message: {ws_message.topic}")
            
        except Exception as e:
            self.logger.error(f"❌ Message processing error: {e}")
            self.stats['errors'] += 1
    
    async def _dispatch_message(self, message: WSMessage) -> None:
        """Диспетчеризация сообщения по топику"""
        try:
            topic = message.topic
            
            # Находим подходящие callbacks
            matching_callbacks = []
            
            for sub_key, subscription in self.subscriptions.items():
                if subscription.active and topic.startswith(subscription.topic):
                    if subscription.callback:
                        matching_callbacks.append(subscription.callback)
            
            # Также проверяем глобальные callbacks
            for pattern, callbacks in self.callbacks.items():
                if pattern in topic or pattern == '*':
                    matching_callbacks.extend(callbacks)
            
            # Выполняем callbacks
            if matching_callbacks:
                for callback in matching_callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(message)
                        else:
                            callback(message)
                    except Exception as e:
                        self.logger.error(f"❌ Callback error for {topic}: {e}")
            
            # Специальная обработка для kline данных
            if topic.startswith('kline'):
                await self._handle_kline_message(message)
            
        except Exception as e:
            self.logger.error(f"❌ Message dispatch error: {e}")
    
    async def _handle_operation_response(self, response: Dict[str, Any]) -> None:
        """Обработка ответов на операции"""
        try:
            success = response.get('success', False)
            op = response.get('op', 'unknown')
            
            if success:
                self.logger.debug(f"✅ Operation successful: {op}")
            else:
                ret_msg = response.get('ret_msg', 'Unknown error')
                self.logger.error(f"❌ Operation failed: {op} - {ret_msg}")
                
        except Exception as e:
            self.logger.error(f"❌ Operation response handling error: {e}")
    
    async def _handle_kline_message(self, message: WSMessage) -> None:
        """Специальная обработка kline данных"""
        try:
            if not message.data:
                return
            
            for kline_data in message.data:
                # Извлекаем информацию о символе и интервале из топика
                # Формат топика: kline.1.BTCUSDT
                topic_parts = message.topic.split('.')
                if len(topic_parts) >= 3:
                    interval = topic_parts[1]
                    symbol = topic_parts[2]
                    
                    # Конвертируем в наш формат CandleData
                    candle = self._convert_kline_to_candle(kline_data, symbol, interval)
                    
                    if candle:
                        # Можно сохранить в кеш или отправить подписчикам
                        self.logger.debug(f"📊 Kline update: {symbol} {interval} - Price: {candle.close_price}")
                        
        except Exception as e:
            self.logger.error(f"❌ Kline message handling error: {e}")
    
    def _convert_kline_to_candle(self, kline_data: Dict[str, Any], symbol: str, interval: str) -> Optional[CandleData]:
        """Конвертация kline данных в CandleData"""
        try:
            return CandleData(
                symbol=symbol,
                timeframe=f"{interval}m",  # Bybit использует минуты
                open_time=int(kline_data['start']),
                close_time=int(kline_data['end']),
                open_price=float(kline_data['open']),
                high_price=float(kline_data['high']),
                low_price=float(kline_data['low']),
                close_price=float(kline_data['close']),
                volume=float(kline_data['volume']),
                quote_volume=float(kline_data.get('turnover', 0))
            )
        except (KeyError, ValueError) as e:
            self.logger.error(f"❌ Kline conversion error: {e}")
            return None
    
    # ========================================================================
    # SUBSCRIPTION MANAGEMENT
    # ========================================================================
    
    async def subscribe(
        self,
        topic: str,
        symbol: Optional[str] = None,
        interval: Optional[str] = None,
        callback: Optional[Callable] = None
    ) -> bool:
        """
        Подписка на топик
        
        Args:
            topic: Топик для подписки (kline, tickers, etc.)
            symbol: Торговая пара
            interval: Интервал для kline
            callback: Функция обратного вызова
            
        Returns:
            True если подписка успешна
        """
        try:
            # Создаем объект подписки
            subscription = WSSubscription(
                topic=topic,
                symbol=symbol,
                interval=interval,
                callback=callback
            )
            
            # Определяем тип соединения
            connection_type = self._get_connection_type_for_topic(topic)
            
            # Формируем сообщение подписки
            subscribe_message = self._build_subscribe_message(topic, symbol, interval)
            
            # Отправляем подписку
            success = await self._send_subscription(subscribe_message, connection_type)
            
            if success:
                # Сохраняем подписку
                sub_key = subscription.subscription_key
                self.subscriptions[sub_key] = subscription
                self.stats['subscription_count'] = len(self.subscriptions)
                
                self.logger.info(f"📡 Subscribed to {sub_key}")
                return True
            else:
                self.logger.error(f"❌ Failed to subscribe to {topic}")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ Subscription error: {e}")
            return False
    
    async def unsubscribe(self, topic: str, symbol: Optional[str] = None, interval: Optional[str] = None) -> bool:
        """Отписка от топика"""
        try:
            # Создаем временную подписку для получения ключа
            temp_subscription = WSSubscription(topic=topic, symbol=symbol, interval=interval)
            sub_key = temp_subscription.subscription_key
            
            # Проверяем что подписка существует
            if sub_key not in self.subscriptions:
                self.logger.warning(f"⚠️ Subscription {sub_key} not found")
                return False
            
            # Определяем тип соединения
            connection_type = self._get_connection_type_for_topic(topic)
            
            # Формируем сообщение отписки
            unsubscribe_message = self._build_unsubscribe_message(topic, symbol, interval)
            
            # Отправляем отписку
            success = await self._send_subscription(unsubscribe_message, connection_type)
            
            if success:
                # Удаляем подписку
                del self.subscriptions[sub_key]
                self.stats['subscription_count'] = len(self.subscriptions)
                
                self.logger.info(f"📡 Unsubscribed from {sub_key}")
                return True
            else:
                self.logger.error(f"❌ Failed to unsubscribe from {topic}")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ Unsubscription error: {e}")
            return False
    
    def _get_connection_type_for_topic(self, topic: str) -> str:
        """Определение типа соединения для топика"""
        private_topics = ['position', 'order', 'execution', 'wallet']
        
        if any(topic.startswith(pt) for pt in private_topics):
            return 'private'
        else:
            return 'public'
    
    def _build_subscribe_message(self, topic: str, symbol: Optional[str], interval: Optional[str]) -> Dict[str, Any]:
        """Построение сообщения подписки"""
        # Формируем топик в зависимости от типа
        if topic == 'kline' and symbol and interval:
            full_topic = f"kline.{interval}.{symbol}"
        elif topic == 'tickers' and symbol:
            full_topic = f"tickers.{symbol}"
        elif topic == 'orderbook' and symbol:
            full_topic = f"orderbook.1.{symbol}"  # Глубина 1
        elif topic in ['position', 'order', 'execution', 'wallet']:
            full_topic = topic
        else:
            full_topic = f"{topic}.{symbol}" if symbol else topic
        
        return {
            "op": "subscribe",
            "args": [full_topic]
        }
    
    def _build_unsubscribe_message(self, topic: str, symbol: Optional[str], interval: Optional[str]) -> Dict[str, Any]:
        """Построение сообщения отписки"""
        subscribe_msg = self._build_subscribe_message(topic, symbol, interval)
        subscribe_msg['op'] = 'unsubscribe'
        return subscribe_msg
    
    async def _send_subscription(self, message: Dict[str, Any], connection_type: str) -> bool:
        """Отправка сообщения подписки"""
        try:
            if connection_type == 'public':
                if not self.public_ws or self.public_ws.closed:
                    await self._connect_public()
                
                if self.public_ws and not self.public_ws.closed:
                    await self.public_ws.send(safe_json_dumps(message))
                    return True
                    
            elif connection_type == 'private':
                if not self.private_ws or self.private_ws.closed:
                    await self._connect_private()
                
                if self.private_ws and not self.private_ws.closed:
                    await self.private_ws.send(safe_json_dumps(message))
                    return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"❌ Failed to send subscription message: {e}")
            return False
    
    async def _restore_subscriptions(self, connection_type: str) -> None:
        """Восстановление подписок после переподключения"""
        try:
            restored = 0
            
            for subscription in self.subscriptions.values():
                if subscription.active:
                    expected_type = self._get_connection_type_for_topic(subscription.topic)
                    
                    if expected_type == connection_type:
                        message = self._build_subscribe_message(
                            subscription.topic,
                            subscription.symbol,
                            subscription.interval
                        )
                        
                        success = await self._send_subscription(message, connection_type)
                        if success:
                            restored += 1
                        
                        # Небольшая пауза между подписками
                        await asyncio.sleep(0.1)
            
            if restored > 0:
                self.logger.info(f"🔄 Restored {restored} {connection_type} subscriptions")
                
        except Exception as e:
            self.logger.error(f"❌ Failed to restore {connection_type} subscriptions: {e}")
    
    # ========================================================================
    # RECONNECTION MANAGEMENT
    # ========================================================================
    
    async def _schedule_reconnect(self, connection_type: str) -> None:
        """Планирование переподключения"""
        if not self._running:
            return
        
        try:
            retry_count = self._reconnect_attempts.get(connection_type, 0)
            max_retries = self.settings.WS_RETRIES or 10
            
            if retry_count >= max_retries > 0:
                self.logger.error(f"❌ Max reconnection attempts reached for {connection_type}")
                return
            
            # Экспоненциальная задержка: 1, 2, 4, 8, 16, 32 секунды (макс)
            delay = min(2 ** retry_count, 32)
            
            self.logger.info(f"🔄 Scheduling {connection_type} reconnection in {delay}s (attempt {retry_count + 1})")
            
            await asyncio.sleep(delay)
            
            if self._running:
                self._reconnect_attempts[connection_type] = retry_count + 1
                
                if connection_type == 'public':
                    await self._connect_public()
                elif connection_type == 'private':
                    await self._connect_private()
                
                # Сбрасываем счетчик при успешном подключении
                if ((connection_type == 'public' and self.public_status == WSConnectionStatus.CONNECTED) or
                    (connection_type == 'private' and self.private_status == WSConnectionStatus.CONNECTED)):
                    self._reconnect_attempts[connection_type] = 0
                    self.stats['reconnect_count'] += 1
                
        except Exception as e:
            self.logger.error(f"❌ Reconnection error for {connection_type}: {e}")
    
    # ========================================================================
    # BACKGROUND TASKS
    # ========================================================================
    
    async def _heartbeat_task(self) -> None:
        """Задача heartbeat для поддержания соединения"""
        while self._running:
            try:
                current_time = get_current_timestamp()
                
                # Отправляем ping если нужно
                for connection_type in ['public', 'private']:
                    last_ping = self._last_ping.get(connection_type, 0)
                    
                    if current_time - last_ping > (self._ping_interval * 1000):
                        await self._send_ping(connection_type)
                        self._last_ping[connection_type] = current_time
                
                await asyncio.sleep(self._ping_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Heartbeat task error: {e}")
                await asyncio.sleep(5)
    
    async def _send_ping(self, connection_type: str) -> None:
        """Отправка ping сообщения"""
        try:
            ping_message = {"op": "ping"}
            
            if connection_type == 'public' and self.public_ws and not self.public_ws.closed:
                await self.public_ws.send(safe_json_dumps(ping_message))
                self.logger.debug("🏓 Sent public ping")
                
            elif connection_type == 'private' and self.private_ws and not self.private_ws.closed:
                await self.private_ws.send(safe_json_dumps(ping_message))
                self.logger.debug("🏓 Sent private ping")
                
        except Exception as e:
            self.logger.error(f"❌ Failed to send {connection_type} ping: {e}")
    
    async def _connection_monitor(self) -> None:
        """Мониторинг состояния соединений"""
        while self._running:
            try:
                # Проверяем состояние соединений
                await self._check_connections_health()
                
                # Логируем статистику периодически
                if self.stats['messages_received'] % 1000 == 0 and self.stats['messages_received'] > 0:
                    self.logger.info(f"📊 WebSocket Stats: {self.get_stats()}")
                
                await asyncio.sleep(30)  # Проверка каждые 30 секунд
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Connection monitor error: {e}")
                await asyncio.sleep(10)
    
    async def _check_connections_health(self) -> None:
        """Проверка здоровья соединений"""
        try:
            # Проверяем публичное соединение
            if (self.public_status == WSConnectionStatus.CONNECTED and 
                self.public_ws and self.public_ws.closed):
                self.logger.warning("⚠️ Public WebSocket unexpectedly closed")
                self.public_status = WSConnectionStatus.DISCONNECTED
                asyncio.create_task(self._schedule_reconnect('public'))
            
            # Проверяем приватное соединение
            if (self.private_status == WSConnectionStatus.CONNECTED and 
                self.private_ws and self.private_ws.closed):
                self.logger.warning("⚠️ Private WebSocket unexpectedly closed")
                self.private_status = WSConnectionStatus.DISCONNECTED
                asyncio.create_task(self._schedule_reconnect('private'))
                
        except Exception as e:
            self.logger.error(f"❌ Connection health check error: {e}")
    
    # ========================================================================
    # CONVENIENCE METHODS
    # ========================================================================
    
    async def subscribe_kline(self, symbol: str, interval: str, callback: Optional[Callable] = None) -> bool:
        """Быстрая подписка на kline данные"""
        return await self.subscribe('kline', symbol=symbol, interval=interval, callback=callback)
    
    async def subscribe_ticker(self, symbol: str, callback: Optional[Callable] = None) -> bool:
        """Быстрая подписка на ticker данные"""
        return await self.subscribe('tickers', symbol=symbol, callback=callback)
    
    async def subscribe_orderbook(self, symbol: str, callback: Optional[Callable] = None) -> bool:
        """Быстрая подписка на orderbook"""
        return await self.subscribe('orderbook', symbol=symbol, callback=callback)
    
    async def subscribe_positions(self, callback: Optional[Callable] = None) -> bool:
        """Подписка на изменения позиций (приватный поток)"""
        return await self.subscribe('position', callback=callback)
    
    async def subscribe_orders(self, callback: Optional[Callable] = None) -> bool:
        """Подписка на изменения ордеров (приватный поток)"""
        return await self.subscribe('order', callback=callback)
    
    def add_global_callback(self, pattern: str, callback: Callable) -> None:
        """Добавление глобального callback для сообщений"""
        if pattern not in self.callbacks:
            self.callbacks[pattern] = []
        self.callbacks[pattern].append(callback)
        self.logger.debug(f"📡 Added global callback for pattern: {pattern}")
    
    def remove_global_callback(self, pattern: str, callback: Callable) -> None:
        """Удаление глобального callback"""
        if pattern in self.callbacks and callback in self.callbacks[pattern]:
            self.callbacks[pattern].remove(callback)
            if not self.callbacks[pattern]:
                del self.callbacks[pattern]
            self.logger.debug(f"📡 Removed global callback for pattern: {pattern}")
    
    # ========================================================================
    # STATUS AND MONITORING
    # ========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики WebSocket менеджера"""
        current_time = get_current_timestamp()
        uptime = (current_time - self.stats['uptime_start']) // 1000 if self.stats['uptime_start'] else 0
        
        return {
            **self.stats,
            'uptime_seconds': uptime,
            'public_status': self.public_status.value,
            'private_status': self.private_status.value,
            'active_subscriptions': len([s for s in self.subscriptions.values() if s.active]),
            'queue_sizes': {
                'public': self._message_queues['public'].qsize(),
                'private': self._message_queues['private'].qsize()
            },
            'reconnect_attempts': self._reconnect_attempts.copy()
        }
    
    def get_subscriptions_info(self) -> Dict[str, Any]:
        """Информация о подписках"""
        return {
            sub_key: {
                'topic': sub.topic,
                'symbol': sub.symbol,
                'interval': sub.interval,
                'active': sub.active,
                'subscribed_at': sub.subscribed_at,
                'has_callback': sub.callback is not None
            }
            for sub_key, sub in self.subscriptions.items()
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Проверка здоровья WebSocket менеджера"""
        try:
            health = {
                'status': 'healthy' if self._running else 'stopped',
                'public_connected': self.public_status == WSConnectionStatus.CONNECTED,
                'private_connected': self.private_status == WSConnectionStatus.CONNECTED,
                'subscriptions_count': len(self.subscriptions),
                'messages_received': self.stats['messages_received'],
                'error_count': self.stats['errors'],
                'uptime_seconds': (get_current_timestamp() - self.stats['uptime_start']) // 1000 if self.stats['uptime_start'] else 0
            }
            
            # Определяем общий статус
            if not self._running:
                health['status'] = 'stopped'
            elif self.stats['errors'] > self.stats['messages_received'] * 0.1:  # Более 10% ошибок
                health['status'] = 'unhealthy'
            elif self.public_status == WSConnectionStatus.CONNECTED:
                health['status'] = 'healthy'
            else:
                health['status'] = 'degraded'
            
            return health
            
        except Exception as e:
            return {
                'status': 'error',
                'error': str(e)
            }
    
    def is_connected(self, connection_type: str = 'public') -> bool:
        """Проверка подключения"""
        if connection_type == 'public':
            return self.public_status == WSConnectionStatus.CONNECTED
        elif connection_type == 'private':
            return self.private_status == WSConnectionStatus.CONNECTED
        else:
            return False
    
    def __str__(self) -> str:
        return f"WebSocketManager(public: {self.public_status.value}, private: {self.private_status.value})"
    
    def __repr__(self) -> str:
        return (f"WebSocketManager(running={self._running}, "
                f"public_status='{self.public_status.value}', "
                f"private_status='{self.private_status.value}', "
                f"subscriptions={len(self.subscriptions)})")


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def create_websocket_manager(settings: Optional[Settings] = None) -> WebSocketManager:
    """Создание экземпляра WebSocket менеджера"""
    return WebSocketManager(settings)


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    async def test_websocket_manager():
        """Тестирование WebSocket менеджера"""
        print("🧪 Testing WebSocket Manager")
        print("=" * 50)
        
        # Callback для обработки сообщений
        def message_callback(message: WSMessage):
            print(f"📨 Received: {message.topic} at {message.timestamp}")
        
        try:
            # Создаем менеджер
            manager = create_websocket_manager()
            
            # Добавляем глобальный callback
            manager.add_global_callback('*', message_callback)
            
            # Запускаем менеджер
            await manager.start()
            print("✅ WebSocket Manager started")
            
            # Подписываемся на данные
            success = await manager.subscribe_kline('BTCUSDT', '1', message_callback)
            print(f"✅ Kline subscription: {success}")
            
            success = await manager.subscribe_ticker('BTCUSDT', message_callback)
            print(f"✅ Ticker subscription: {success}")
            
            # Получаем статистику
            stats = manager.get_stats()
            print(f"📊 Stats: {stats}")
            
            # Информация о подписках
            subs_info = manager.get_subscriptions_info()
            print(f"📡 Subscriptions: {len(subs_info)}")
            
            # Health check
            health = await manager.health_check()
            print(f"🏥 Health: {health['status']}")
            
            # Ждем некоторое время для получения сообщений
            print("⏳ Waiting for messages (10 seconds)...")
            await asyncio.sleep(10)
            
            # Финальная статистика
            final_stats = manager.get_stats()
            print(f"📊 Final stats: messages_received={final_stats['messages_received']}")
            
        except Exception as e:
            print(f"❌ Test failed: {e}")
        finally:
            if 'manager' in locals():
                await manager.stop()
                print("✅ WebSocket Manager stopped")
    
    # Запуск теста
    asyncio.run(test_websocket_manager())
