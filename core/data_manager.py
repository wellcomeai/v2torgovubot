"""
Trading Bot Data Manager
Управление рыночными данными, кешированием и интеграцией с источниками данных
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Callable, Union, Set
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
import threading
from concurrent.futures import ThreadPoolExecutor
import weakref

from utils.logger import setup_logger
from utils.helpers import (
    get_current_timestamp, timestamp_to_datetime, parse_timeframe_to_milliseconds,
    safe_float, validate_symbol, validate_timeframe, Timer
)
from data.historical_data import CandleData, HistoricalDataManager
from data.database import Database
from app.config.settings import Settings, get_settings


# ============================================================================
# TYPES AND CONSTANTS
# ============================================================================

logger = setup_logger(__name__)

# Конфигурация кеша
DEFAULT_CACHE_TTL = 60  # секунды
MAX_CACHE_SIZE = 1000  # максимум записей в кеше
MAX_CANDLES_PER_SYMBOL = 2000  # максимум свечей на символ в памяти

# Интервалы обновления данных
UPDATE_INTERVALS = {
    '1m': 60,      # 1 минута
    '5m': 300,     # 5 минут  
    '15m': 900,    # 15 минут
    '30m': 1800,   # 30 минут
    '1h': 3600,    # 1 час
    '4h': 14400,   # 4 часа
    '1d': 86400,   # 1 день
}


@dataclass
class MarketDataCache:
    """Кеш рыночных данных"""
    data: List[CandleData] = field(default_factory=list)
    last_updated: int = 0
    last_price: float = 0.0
    volume_24h: float = 0.0
    price_change_24h: float = 0.0
    subscribers: Set[str] = field(default_factory=set)
    
    def is_expired(self, ttl_seconds: int = DEFAULT_CACHE_TTL) -> bool:
        """Проверка истечения кеша"""
        return (get_current_timestamp() - self.last_updated) > (ttl_seconds * 1000)
    
    def add_subscriber(self, subscriber_id: str) -> None:
        """Добавление подписчика на обновления"""
        self.subscribers.add(subscriber_id)
    
    def remove_subscriber(self, subscriber_id: str) -> None:
        """Удаление подписчика"""
        self.subscribers.discard(subscriber_id)
    
    def get_latest_candle(self) -> Optional[CandleData]:
        """Получение последней свечи"""
        return self.data[-1] if self.data else None
    
    def get_candles_slice(self, count: int = 100) -> List[CandleData]:
        """Получение среза свечей"""
        return self.data[-count:] if self.data else []


@dataclass
class DataSubscription:
    """Подписка на данные"""
    subscriber_id: str
    symbol: str
    timeframe: str
    callback: Callable[[List[CandleData]], None]
    last_notified: int = 0
    active: bool = True


# ============================================================================
# DATA MANAGER CLASS
# ============================================================================

class DataManager:
    """
    Менеджер рыночных данных
    
    Возможности:
    - Кеширование рыночных данных в памяти
    - Подписки на обновления данных
    - Интеграция с историческими данными
    - Автоматическое обновление данных
    - Управление источниками данных
    """
    
    def __init__(
        self,
        settings: Optional[Settings] = None,
        database: Optional[Database] = None,
        bybit_client=None
    ):
        self.settings = settings or get_settings()
        self.logger = setup_logger(f"{__name__}.DataManager")
        
        # Внешние компоненты
        self.database = database
        self.bybit_client = bybit_client
        self.historical_manager: Optional[HistoricalDataManager] = None
        
        # Кеш данных по символам и таймфреймам
        self._cache: Dict[str, Dict[str, MarketDataCache]] = defaultdict(lambda: defaultdict(MarketDataCache))
        self._cache_lock = threading.RLock()
        
        # Подписчики на обновления
        self._subscriptions: Dict[str, DataSubscription] = {}
        self._subscription_lock = threading.RLock()
        
        # Состояние менеджера
        self._is_running = False
        self._update_tasks: Dict[str, asyncio.Task] = {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="DataManager")
        
        # Статистика
        self.stats = {
            'cache_hits': 0,
            'cache_misses': 0,
            'data_updates': 0,
            'subscriptions_notified': 0,
            'errors': 0,
            'total_symbols': 0,
            'active_subscriptions': 0
        }
        
        # Настройки кеширования
        self.cache_ttl = self.settings.MARKET_DATA_CACHE_TTL
        self.max_cache_size = MAX_CACHE_SIZE
        
        self.logger.info("📊 Data Manager initialized")
    
    async def initialize(self) -> None:
        """Инициализация менеджера"""
        try:
            self.logger.info("🔧 Initializing Data Manager...")
            
            # Инициализация исторических данных
            self.historical_manager = HistoricalDataManager(
                bybit_client=self.bybit_client,
                database=self.database
            )
            
            # Запуск фоновых задач
            self._is_running = True
            asyncio.create_task(self._background_cleanup_task())
            
            self.logger.info("✅ Data Manager initialized successfully")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize Data Manager: {e}")
            raise
    
    async def shutdown(self) -> None:
        """Завершение работы менеджера"""
        try:
            self.logger.info("🔄 Shutting down Data Manager...")
            
            self._is_running = False
            
            # Отменяем все задачи обновления
            for task in self._update_tasks.values():
                task.cancel()
            
            # Ждем завершения задач
            if self._update_tasks:
                await asyncio.gather(*self._update_tasks.values(), return_exceptions=True)
            
            # Закрываем executor
            self._executor.shutdown(wait=True)
            
            self.logger.info("✅ Data Manager shutdown complete")
            
        except Exception as e:
            self.logger.error(f"❌ Data Manager shutdown error: {e}")
    
    # ========================================================================
    # DATA RETRIEVAL METHODS
    # ========================================================================
    
    async def get_market_data(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100,
        force_refresh: bool = False
    ) -> List[CandleData]:
        """
        Получение рыночных данных с кешированием
        
        Args:
            symbol: Торговая пара
            timeframe: Таймфрейм
            count: Количество свечей
            force_refresh: Принудительное обновление
            
        Returns:
            Список свечей
        """
        try:
            with Timer(f"get_market_data_{symbol}_{timeframe}"):
                symbol = symbol.upper()
                timeframe = timeframe.lower()
                
                # Валидация
                if not validate_symbol(symbol) or not validate_timeframe(timeframe):
                    raise ValueError(f"Invalid symbol or timeframe: {symbol}, {timeframe}")
                
                cache_key = f"{symbol}_{timeframe}"
                
                # Проверяем кеш
                if not force_refresh:
                    cached_data = self._get_from_cache(symbol, timeframe, count)
                    if cached_data:
                        self.stats['cache_hits'] += 1
                        self.logger.debug(f"📱 Cache hit: {cache_key}, {len(cached_data)} candles")
                        return cached_data
                
                self.stats['cache_misses'] += 1
                
                # Получаем данные из источника
                market_data = await self._fetch_market_data(symbol, timeframe, count)
                
                # Обновляем кеш
                self._update_cache(symbol, timeframe, market_data)
                
                self.stats['data_updates'] += 1
                self.logger.info(f"📊 Market data updated: {symbol} {timeframe}, {len(market_data)} candles")
                
                return market_data[-count:] if market_data else []
                
        except Exception as e:
            self.stats['errors'] += 1
            self.logger.error(f"❌ Failed to get market data {symbol} {timeframe}: {e}")
            raise
    
    async def get_latest_price(self, symbol: str) -> Optional[float]:
        """Получение последней цены символа"""
        try:
            # Пытаемся получить из кеша
            with self._cache_lock:
                symbol_cache = self._cache.get(symbol.upper())
                if symbol_cache:
                    # Ищем самые свежие данные среди всех таймфреймов
                    latest_price = 0.0
                    latest_time = 0
                    
                    for timeframe_cache in symbol_cache.values():
                        if (timeframe_cache.last_updated > latest_time and 
                            timeframe_cache.last_price > 0):
                            latest_price = timeframe_cache.last_price
                            latest_time = timeframe_cache.last_updated
                    
                    if latest_price > 0:
                        return latest_price
            
            # Если нет в кеше, получаем свежие данные
            data = await self.get_market_data(symbol, "1m", 1)
            return data[-1].close_price if data else None
            
        except Exception as e:
            self.logger.error(f"❌ Failed to get latest price for {symbol}: {e}")
            return None
    
    async def get_price_change_24h(self, symbol: str) -> Dict[str, float]:
        """Получение изменения цены за 24 часа"""
        try:
            # Получаем данные за последние 24 часа
            data = await self.get_market_data(symbol, "1h", 25)  # 25 часов для запаса
            
            if len(data) < 24:
                return {'price_change': 0.0, 'price_change_pct': 0.0}
            
            current_price = data[-1].close_price
            price_24h_ago = data[-24].close_price
            
            price_change = current_price - price_24h_ago
            price_change_pct = (price_change / price_24h_ago) * 100 if price_24h_ago > 0 else 0.0
            
            return {
                'current_price': current_price,
                'price_24h_ago': price_24h_ago,
                'price_change': price_change,
                'price_change_pct': price_change_pct
            }
            
        except Exception as e:
            self.logger.error(f"❌ Failed to get 24h price change for {symbol}: {e}")
            return {'price_change': 0.0, 'price_change_pct': 0.0}
    
    async def get_volume_24h(self, symbol: str) -> float:
        """Получение объема торгов за 24 часа"""
        try:
            data = await self.get_market_data(symbol, "1h", 24)
            return sum(candle.volume for candle in data) if data else 0.0
            
        except Exception as e:
            self.logger.error(f"❌ Failed to get 24h volume for {symbol}: {e}")
            return 0.0
    
    # ========================================================================
    # SUBSCRIPTION METHODS  
    # ========================================================================
    
    def subscribe_to_data(
        self,
        subscriber_id: str,
        symbol: str,
        timeframe: str,
        callback: Callable[[List[CandleData]], None]
    ) -> bool:
        """
        Подписка на обновления данных
        
        Args:
            subscriber_id: Уникальный ID подписчика
            symbol: Торговая пара
            timeframe: Таймфрейм
            callback: Функция обратного вызова
            
        Returns:
            True если подписка успешна
        """
        try:
            symbol = symbol.upper()
            timeframe = timeframe.lower()
            
            with self._subscription_lock:
                subscription_key = f"{subscriber_id}_{symbol}_{timeframe}"
                
                subscription = DataSubscription(
                    subscriber_id=subscriber_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    callback=callback
                )
                
                self._subscriptions[subscription_key] = subscription
                
                # Добавляем подписчика в кеш
                with self._cache_lock:
                    self._cache[symbol][timeframe].add_subscriber(subscriber_id)
                
                self.stats['active_subscriptions'] = len(self._subscriptions)
            
            # Запускаем задачу автообновления если её нет
            self._ensure_update_task(symbol, timeframe)
            
            self.logger.info(f"📡 Subscription added: {subscriber_id} -> {symbol} {timeframe}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to add subscription: {e}")
            return False
    
    def unsubscribe_from_data(
        self,
        subscriber_id: str,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None
    ) -> bool:
        """
        Отписка от обновлений данных
        
        Args:
            subscriber_id: ID подписчика
            symbol: Символ (если None, отписывается от всех)
            timeframe: Таймфрейм (если None, отписывается от всех таймфреймов символа)
            
        Returns:
            True если отписка успешна
        """
        try:
            with self._subscription_lock:
                keys_to_remove = []
                
                for key, subscription in self._subscriptions.items():
                    if subscription.subscriber_id == subscriber_id:
                        if symbol is None or subscription.symbol == symbol.upper():
                            if timeframe is None or subscription.timeframe == timeframe.lower():
                                keys_to_remove.append(key)
                                
                                # Удаляем подписчика из кеша
                                with self._cache_lock:
                                    self._cache[subscription.symbol][subscription.timeframe].remove_subscriber(subscriber_id)
                
                # Удаляем подписки
                for key in keys_to_remove:
                    del self._subscriptions[key]
                
                self.stats['active_subscriptions'] = len(self._subscriptions)
            
            self.logger.info(f"📡 Unsubscribed: {subscriber_id}, removed {len(keys_to_remove)} subscriptions")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to unsubscribe: {e}")
            return False
    
    async def notify_subscribers(
        self,
        symbol: str,
        timeframe: str,
        data: List[CandleData]
    ) -> None:
        """Уведомление подписчиков об обновлении данных"""
        try:
            notifications_sent = 0
            
            with self._subscription_lock:
                for subscription in self._subscriptions.values():
                    if (subscription.symbol == symbol and 
                        subscription.timeframe == timeframe and 
                        subscription.active):
                        
                        try:
                            # Вызываем callback в executor чтобы не блокировать event loop
                            await asyncio.get_event_loop().run_in_executor(
                                self._executor,
                                subscription.callback,
                                data
                            )
                            
                            subscription.last_notified = get_current_timestamp()
                            notifications_sent += 1
                            
                        except Exception as e:
                            self.logger.warning(f"⚠️ Callback failed for {subscription.subscriber_id}: {e}")
                            # Отмечаем подписку как неактивную при ошибках
                            subscription.active = False
            
            if notifications_sent > 0:
                self.stats['subscriptions_notified'] += notifications_sent
                self.logger.debug(f"📢 Notified {notifications_sent} subscribers for {symbol} {timeframe}")
                
        except Exception as e:
            self.logger.error(f"❌ Failed to notify subscribers: {e}")
    
    # ========================================================================
    # CACHE MANAGEMENT
    # ========================================================================
    
    def _get_from_cache(self, symbol: str, timeframe: str, count: int) -> Optional[List[CandleData]]:
        """Получение данных из кеша"""
        try:
            with self._cache_lock:
                cache = self._cache[symbol][timeframe]
                
                if cache.data and not cache.is_expired(self.cache_ttl):
                    return cache.get_candles_slice(count)
                
                return None
                
        except Exception as e:
            self.logger.debug(f"Cache access error: {e}")
            return None
    
    def _update_cache(self, symbol: str, timeframe: str, data: List[CandleData]) -> None:
        """Обновление кеша данных"""
        try:
            with self._cache_lock:
                cache = self._cache[symbol][timeframe]
                
                # Обновляем данные
                cache.data = data[-MAX_CANDLES_PER_SYMBOL:]  # Ограничиваем размер
                cache.last_updated = get_current_timestamp()
                
                if data:
                    latest_candle = data[-1]
                    cache.last_price = latest_candle.close_price
                
                # Обновляем статистику
                self.stats['total_symbols'] = len(self._cache)
                
        except Exception as e:
            self.logger.error(f"❌ Cache update error: {e}")
    
    def clear_cache(
        self,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None
    ) -> None:
        """Очистка кеша"""
        try:
            with self._cache_lock:
                if symbol is None:
                    # Очищаем весь кеш
                    self._cache.clear()
                    self.logger.info("🧹 Cleared entire cache")
                    
                elif timeframe is None:
                    # Очищаем кеш символа
                    if symbol.upper() in self._cache:
                        del self._cache[symbol.upper()]
                        self.logger.info(f"🧹 Cleared cache for {symbol}")
                        
                else:
                    # Очищаем конкретный кеш
                    symbol_cache = self._cache.get(symbol.upper())
                    if symbol_cache and timeframe.lower() in symbol_cache:
                        del symbol_cache[timeframe.lower()]
                        self.logger.info(f"🧹 Cleared cache for {symbol} {timeframe}")
                
                self.stats['total_symbols'] = len(self._cache)
                
        except Exception as e:
            self.logger.error(f"❌ Cache clear error: {e}")
    
    # ========================================================================
    # DATA FETCHING
    # ========================================================================
    
    async def _fetch_market_data(
        self,
        symbol: str,
        timeframe: str,
        count: int
    ) -> List[CandleData]:
        """Получение данных из источников"""
        try:
            # Сначала пытаемся получить из исторических данных
            if self.historical_manager:
                data = await self.historical_manager.get_recent_candles(symbol, timeframe, count)
                if data:
                    return data
            
            # Если нет historical manager или данных, пытаемся получить напрямую с API
            if self.bybit_client:
                response = await self.bybit_client.get_kline(
                    symbol=symbol,
                    interval=timeframe,
                    limit=count
                )
                
                if response and 'result' in response and 'list' in response['result']:
                    candles = []
                    for kline_data in response['result']['list']:
                        try:
                            candle = CandleData.from_bybit_response(kline_data, symbol, timeframe)
                            candles.append(candle)
                        except Exception as e:
                            self.logger.warning(f"⚠️ Failed to parse candle: {e}")
                            continue
                    
                    # Сортируем по времени
                    candles.sort(key=lambda x: x.open_time)
                    return candles
            
            # Если ничего не получилось, возвращаем пустой список
            self.logger.warning(f"⚠️ No data sources available for {symbol} {timeframe}")
            return []
            
        except Exception as e:
            self.logger.error(f"❌ Failed to fetch market data: {e}")
            return []
    
    # ========================================================================
    # BACKGROUND TASKS
    # ========================================================================
    
    def _ensure_update_task(self, symbol: str, timeframe: str) -> None:
        """Обеспечение запуска задачи обновления данных"""
        task_key = f"{symbol}_{timeframe}"
        
        if task_key not in self._update_tasks or self._update_tasks[task_key].done():
            task = asyncio.create_task(
                self._auto_update_data(symbol, timeframe),
                name=f"update_{task_key}"
            )
            self._update_tasks[task_key] = task
            
            self.logger.debug(f"🔄 Started auto-update task for {symbol} {timeframe}")
    
    async def _auto_update_data(self, symbol: str, timeframe: str) -> None:
        """Автоматическое обновление данных"""
        update_interval = UPDATE_INTERVALS.get(timeframe, 60)  # По умолчанию 1 минута
        
        while self._is_running:
            try:
                # Проверяем есть ли активные подписчики
                has_subscribers = False
                with self._cache_lock:
                    cache = self._cache[symbol][timeframe]
                    has_subscribers = len(cache.subscribers) > 0
                
                if not has_subscribers:
                    self.logger.debug(f"📉 No subscribers for {symbol} {timeframe}, stopping updates")
                    break
                
                # Получаем свежие данные
                data = await self._fetch_market_data(symbol, timeframe, 100)
                
                if data:
                    # Обновляем кеш
                    self._update_cache(symbol, timeframe, data)
                    
                    # Уведомляем подписчиков
                    await self.notify_subscribers(symbol, timeframe, data)
                
                # Ждем до следующего обновления
                await asyncio.sleep(update_interval)
                
            except asyncio.CancelledError:
                self.logger.debug(f"🔄 Update task cancelled for {symbol} {timeframe}")
                break
            except Exception as e:
                self.logger.error(f"❌ Auto-update error for {symbol} {timeframe}: {e}")
                await asyncio.sleep(min(update_interval, 60))  # При ошибке ждем меньше
        
        # Удаляем задачу из списка активных
        task_key = f"{symbol}_{timeframe}"
        if task_key in self._update_tasks:
            del self._update_tasks[task_key]
    
    async def _background_cleanup_task(self) -> None:
        """Фоновая очистка неактивных данных и подписок"""
        while self._is_running:
            try:
                await asyncio.sleep(300)  # Каждые 5 минут
                
                # Очищаем неактивные подписки
                with self._subscription_lock:
                    inactive_keys = []
                    for key, subscription in self._subscriptions.items():
                        if not subscription.active:
                            inactive_keys.append(key)
                    
                    for key in inactive_keys:
                        del self._subscriptions[key]
                    
                    if inactive_keys:
                        self.logger.info(f"🧹 Removed {len(inactive_keys)} inactive subscriptions")
                
                # Очищаем старые кеши без подписчиков
                with self._cache_lock:
                    empty_caches = []
                    for symbol, timeframes in self._cache.items():
                        for timeframe, cache in timeframes.items():
                            if (len(cache.subscribers) == 0 and 
                                cache.is_expired(self.cache_ttl * 10)):  # Держим 10x TTL без подписчиков
                                empty_caches.append((symbol, timeframe))
                    
                    for symbol, timeframe in empty_caches:
                        del self._cache[symbol][timeframe]
                        if not self._cache[symbol]:
                            del self._cache[symbol]
                    
                    if empty_caches:
                        self.logger.info(f"🧹 Removed {len(empty_caches)} empty caches")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Background cleanup error: {e}")
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики менеджера"""
        with self._cache_lock, self._subscription_lock:
            cache_memory_usage = sum(
                len(tf_cache.data) for symbol_cache in self._cache.values()
                for tf_cache in symbol_cache.values()
            )
            
            hit_rate = 0
            total_requests = self.stats['cache_hits'] + self.stats['cache_misses']
            if total_requests > 0:
                hit_rate = (self.stats['cache_hits'] / total_requests) * 100
            
            return {
                **self.stats,
                'cache_hit_rate_pct': round(hit_rate, 2),
                'cache_memory_usage': cache_memory_usage,
                'active_update_tasks': len(self._update_tasks),
                'total_cached_symbols': len(self._cache),
                'is_running': self._is_running
            }
    
    def get_cache_info(self) -> Dict[str, Any]:
        """Информация о кеше"""
        with self._cache_lock:
            cache_info = {}
            
            for symbol, timeframes in self._cache.items():
                cache_info[symbol] = {}
                for timeframe, cache in timeframes.items():
                    cache_info[symbol][timeframe] = {
                        'data_points': len(cache.data),
                        'last_updated': cache.last_updated,
                        'last_price': cache.last_price,
                        'subscribers': len(cache.subscribers),
                        'is_expired': cache.is_expired(self.cache_ttl)
                    }
            
            return cache_info
    
    async def health_check(self) -> Dict[str, Any]:
        """Проверка здоровья менеджера"""
        try:
            # Тестовый запрос данных
            test_symbol = "BTCUSDT"
            test_data = await self.get_market_data(test_symbol, "1m", 1)
            
            health = {
                'status': 'healthy' if test_data else 'warning',
                'is_running': self._is_running,
                'active_subscriptions': len(self._subscriptions),
                'active_update_tasks': len(self._update_tasks),
                'cache_symbols': len(self._cache),
                'last_test_data_time': test_data[-1].open_time if test_data else None,
                'stats': self.get_stats()
            }
            
            return health
            
        except Exception as e:
            return {
                'status': 'unhealthy',
                'error': str(e),
                'is_running': self._is_running
            }


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def create_data_manager(
    settings: Optional[Settings] = None,
    database: Optional[Database] = None,
    bybit_client=None
) -> DataManager:
    """Создание экземпляра DataManager"""
    return DataManager(
        settings=settings,
        database=database,
        bybit_client=bybit_client
    )


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    import asyncio
    
    async def test_data_manager():
        """Тестирование Data Manager"""
        print("🧪 Testing Data Manager")
        print("=" * 50)
        
        # Создаем менеджер
        manager = create_data_manager()
        
        try:
            # Инициализируем
            await manager.initialize()
            print("✅ Manager initialized")
            
            # Тестируем подписку
            def test_callback(data: List[CandleData]):
                print(f"📊 Received {len(data)} candles, latest price: {data[-1].close_price}")
            
            success = manager.subscribe_to_data("test_subscriber", "BTCUSDT", "5m", test_callback)
            print(f"✅ Subscription: {success}")
            
            # Получаем данные
            data = await manager.get_market_data("BTCUSDT", "5m", 10)
            print(f"✅ Got {len(data)} candles")
            
            # Статистика
            stats = manager.get_stats()
            print(f"📊 Stats: {stats}")
            
            # Health check
            health = await manager.health_check()
            print(f"🏥 Health: {health['status']}")
            
            await asyncio.sleep(2)  # Даем время на обновления
            
        except Exception as e:
            print(f"❌ Test failed: {e}")
        finally:
            await manager.shutdown()
            print("✅ Manager shutdown complete")
    
    # Запуск теста
    asyncio.run(test_data_manager())
