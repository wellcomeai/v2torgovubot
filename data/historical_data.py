"""
Trading Bot Historical Data Manager
Получение и кеширование исторических рыночных данных с Bybit
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple, Union
import time
from dataclasses import dataclass
import pandas as pd
import numpy as np
from pathlib import Path
import json
import sqlite3

from utils.logger import setup_logger
from utils.helpers import (
    get_current_timestamp, timestamp_to_datetime, datetime_to_timestamp,
    parse_timeframe_to_milliseconds, get_candle_open_time,
    safe_float, validate_symbol, validate_timeframe,
    Timer, retry_async
)
from app.config.settings import get_settings


# ============================================================================
# КОНСТАНТЫ И ТИПЫ
# ============================================================================

logger = setup_logger(__name__)

# Лимиты для Bybit API
BYBIT_MAX_CANDLES_PER_REQUEST = 200
BYBIT_RATE_LIMIT_DELAY = 0.1  # 100ms между запросами

# Таймфреймы поддерживаемые Bybit
BYBIT_TIMEFRAMES = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d', '1w']

# Максимальное количество дней истории для разных таймфреймов
MAX_HISTORY_DAYS = {
    '1m': 30,
    '5m': 60,
    '15m': 90,
    '30m': 180,
    '1h': 365,
    '4h': 730,
    '1d': 1095,  # 3 года
    '1w': 2190   # 6 лет
}


@dataclass
class CandleData:
    """Данные одной свечи"""
    symbol: str
    timeframe: str
    open_time: int  # Unix timestamp в миллисекундах
    close_time: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    quote_volume: Optional[float] = None
    trades_count: Optional[int] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'timeframe': self.timeframe,
            'open_time': self.open_time,
            'close_time': self.close_time,
            'open_price': self.open_price,
            'high_price': self.high_price,
            'low_price': self.low_price,
            'close_price': self.close_price,
            'volume': self.volume,
            'quote_volume': self.quote_volume,
            'trades_count': self.trades_count
        }
    
    @classmethod
    def from_bybit_response(cls, data: List, symbol: str, timeframe: str) -> 'CandleData':
        """Создание из ответа Bybit API"""
        try:
            return cls(
                symbol=symbol,
                timeframe=timeframe,
                open_time=int(data[0]),
                close_time=int(data[0]) + parse_timeframe_to_milliseconds(timeframe) - 1,
                open_price=float(data[1]),
                high_price=float(data[2]),
                low_price=float(data[3]),
                close_price=float(data[4]),
                volume=float(data[5]),
                quote_volume=safe_float(data[6]) if len(data) > 6 else None,
                trades_count=int(data[7]) if len(data) > 7 else None
            )
        except (IndexError, ValueError) as e:
            logger.error(f"❌ Failed to parse Bybit candle data: {e}")
            raise ValueError(f"Invalid candle data format: {data}")


@dataclass
class HistoricalDataRequest:
    """Запрос исторических данных"""
    symbol: str
    timeframe: str
    start_time: Optional[int] = None  # Unix timestamp в миллисекундах
    end_time: Optional[int] = None
    limit: int = BYBIT_MAX_CANDLES_PER_REQUEST
    
    def __post_init__(self):
        # Валидация
        if not validate_symbol(self.symbol):
            raise ValueError(f"Invalid symbol: {self.symbol}")
        
        if not validate_timeframe(self.timeframe):
            raise ValueError(f"Invalid timeframe: {self.timeframe}")
        
        if self.limit > BYBIT_MAX_CANDLES_PER_REQUEST:
            self.limit = BYBIT_MAX_CANDLES_PER_REQUEST
        
        # Если не указаны времена, используем последние данные
        if self.end_time is None:
            self.end_time = get_current_timestamp()
        
        if self.start_time is None:
            # По умолчанию берем данные за последние дни согласно лимитам
            max_days = MAX_HISTORY_DAYS.get(self.timeframe, 30)
            self.start_time = self.end_time - (max_days * 24 * 60 * 60 * 1000)


# ============================================================================
# ОСНОВНОЙ КЛАСС ДЛЯ ИСТОРИЧЕСКИХ ДАННЫХ
# ============================================================================

class HistoricalDataManager:
    """
    Менеджер исторических данных
    Получает данные с Bybit и кеширует их локально
    """
    
    def __init__(self, bybit_client=None, database=None):
        self.settings = get_settings()
        self.logger = setup_logger(f"{__name__}.HistoricalDataManager")
        
        # Клиенты (будут переданы извне)
        self.bybit_client = bybit_client
        self.database = database
        
        # Кеш данных в памяти
        self._memory_cache: Dict[str, List[CandleData]] = {}
        self._cache_timestamps: Dict[str, int] = {}
        self._cache_ttl = 60000  # 1 минута в миллисекундах
        
        # Статистика
        self.stats = {
            'requests_made': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'data_points_fetched': 0,
            'errors': 0
        }
        
        # Файловый кеш (для оффлайн работы)
        self.cache_dir = Path("data/cache/historical")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_cache_key(self, symbol: str, timeframe: str, start_time: int = None, end_time: int = None) -> str:
        """Генерация ключа для кеша"""
        key_parts = [symbol.upper(), timeframe.lower()]
        
        if start_time:
            key_parts.append(f"start_{start_time}")
        if end_time:
            key_parts.append(f"end_{end_time}")
        
        return "_".join(key_parts)
    
    def _is_cache_valid(self, cache_key: str) -> bool:
        """Проверка актуальности кеша"""
        if cache_key not in self._cache_timestamps:
            return False
        
        cache_time = self._cache_timestamps[cache_key]
        current_time = get_current_timestamp()
        
        return (current_time - cache_time) < self._cache_ttl
    
    async def get_historical_data(
        self,
        symbol: str,
        timeframe: str,
        start_time: Optional[Union[int, str, datetime]] = None,
        end_time: Optional[Union[int, str, datetime]] = None,
        limit: Optional[int] = None,
        use_cache: bool = True
    ) -> List[CandleData]:
        """
        Основной метод получения исторических данных
        
        Args:
            symbol: Торговая пара (например, 'BTCUSDT')
            timeframe: Таймфрейм ('1m', '5m', '1h', etc.)
            start_time: Начальное время (timestamp, datetime или ISO string)
            end_time: Конечное время
            limit: Максимальное количество свечей
            use_cache: Использовать кеш
        
        Returns:
            Список свечей CandleData
        """
        try:
            with Timer(f"get_historical_data_{symbol}_{timeframe}"):
                # Нормализуем параметры
                symbol = symbol.upper()
                timeframe = timeframe.lower()
                
                # Конвертируем времена в timestamp
                start_ts = self._convert_to_timestamp(start_time) if start_time else None
                end_ts = self._convert_to_timestamp(end_time) if end_time else None
                
                # Создаем запрос
                request = HistoricalDataRequest(
                    symbol=symbol,
                    timeframe=timeframe,
                    start_time=start_ts,
                    end_time=end_ts,
                    limit=limit or BYBIT_MAX_CANDLES_PER_REQUEST
                )
                
                # Проверяем кеш
                cache_key = self._get_cache_key(symbol, timeframe, start_ts, end_ts)
                
                if use_cache and self._is_cache_valid(cache_key):
                    self.stats['cache_hits'] += 1
                    self.logger.debug(f"📱 Cache hit for {cache_key}")
                    return self._memory_cache[cache_key]
                
                self.stats['cache_misses'] += 1
                
                # Получаем данные
                candles = await self._fetch_data_from_source(request)
                
                # Кешируем результат
                if use_cache:
                    self._memory_cache[cache_key] = candles
                    self._cache_timestamps[cache_key] = get_current_timestamp()
                
                # Сохраняем в БД если есть подключение
                if self.database and candles:
                    await self._save_to_database(candles)
                
                self.stats['data_points_fetched'] += len(candles)
                self.logger.info(f"✅ Fetched {len(candles)} candles for {symbol} {timeframe}")
                
                return candles
                
        except Exception as e:
            self.stats['errors'] += 1
            self.logger.error(f"❌ Failed to get historical data for {symbol} {timeframe}: {e}")
            
            # Пробуем получить из кеша или БД как fallback
            if use_cache:
                fallback_data = await self._get_fallback_data(symbol, timeframe, start_ts, end_ts)
                if fallback_data:
                    self.logger.warning(f"⚠️ Using fallback data: {len(fallback_data)} candles")
                    return fallback_data
            
            raise
    
    async def _fetch_data_from_source(self, request: HistoricalDataRequest) -> List[CandleData]:
        """Получение данных из источника (Bybit API или файловый кеш)"""
        candles = []
        
        # Если есть Bybit клиент, получаем с API
        if self.bybit_client:
            candles = await self._fetch_from_bybit_api(request)
        
        # Если нет данных с API, пробуем файловый кеш
        if not candles:
            candles = await self._load_from_file_cache(request)
        
        # Если все еще нет данных, создаем mock данные для разработки
        if not candles and self.settings.ENVIRONMENT == "development":
            self.logger.warning("⚠️ No data available, generating mock data for development")
            candles = await self._generate_mock_data(request)
        
        return candles
    
    async def _fetch_from_bybit_api(self, request: HistoricalDataRequest) -> List[CandleData]:
        """Получение данных с Bybit API"""
        try:
            all_candles = []
            current_end_time = request.end_time
            remaining_limit = request.limit
            
            # Если нужно больше данных чем максимум за запрос, делаем несколько запросов
            while remaining_limit > 0 and current_end_time > request.start_time:
                batch_limit = min(remaining_limit, BYBIT_MAX_CANDLES_PER_REQUEST)
                
                self.logger.debug(
                    f"🔄 Fetching batch: {request.symbol} {request.timeframe} "
                    f"limit={batch_limit} end_time={current_end_time}"
                )
                
                # Параметры запроса к Bybit
                api_params = {
                    'category': 'linear',
                    'symbol': request.symbol,
                    'interval': request.timeframe,
                    'limit': batch_limit,
                    'end': current_end_time
                }
                
                if request.start_time:
                    api_params['start'] = max(request.start_time, 
                                            current_end_time - (batch_limit * parse_timeframe_to_milliseconds(request.timeframe)))
                
                # Выполняем запрос с retry логикой
                response = await retry_async(
                    lambda: self.bybit_client.get_kline(**api_params),
                    max_retries=3,
                    delay=1.0,
                    exceptions=(Exception,)
                )
                
                self.stats['requests_made'] += 1
                
                # Парсим ответ
                if response and 'result' in response and 'list' in response['result']:
                    batch_data = response['result']['list']
                    
                    batch_candles = []
                    for candle_data in batch_data:
                        try:
                            candle = CandleData.from_bybit_response(
                                candle_data, request.symbol, request.timeframe
                            )
                            # Проверяем что свеча в нужном диапазоне
                            if request.start_time <= candle.open_time <= request.end_time:
                                batch_candles.append(candle)
                        except Exception as e:
                            self.logger.warning(f"⚠️ Skipping invalid candle data: {e}")
                            continue
                    
                    if not batch_candles:
                        self.logger.debug("📊 No more data available from API")
                        break
                    
                    all_candles.extend(batch_candles)
                    remaining_limit -= len(batch_candles)
                    
                    # Обновляем время для следующего запроса
                    oldest_candle_time = min(c.open_time for c in batch_candles)
                    current_end_time = oldest_candle_time - 1
                    
                    self.logger.debug(f"📊 Fetched {len(batch_candles)} candles in batch")
                    
                    # Rate limiting
                    await asyncio.sleep(BYBIT_RATE_LIMIT_DELAY)
                    
                else:
                    self.logger.warning(f"⚠️ Invalid API response format: {response}")
                    break
            
            # Сортируем по времени (от старых к новым)
            all_candles.sort(key=lambda x: x.open_time)
            
            self.logger.info(f"✅ Fetched {len(all_candles)} candles from Bybit API")
            return all_candles
            
        except Exception as e:
            self.logger.error(f"❌ Bybit API fetch failed: {e}")
            return []
    
    async def _save_to_database(self, candles: List[CandleData]) -> None:
        """Сохранение данных в базу данных"""
        try:
            if not candles:
                return
            
            # Подготавливаем данные для сохранения
            market_data_list = []
            
            for candle in candles:
                market_data = {
                    'symbol': candle.symbol,
                    'timeframe': candle.timeframe,
                    'open_time': candle.open_time,
                    'close_time': candle.close_time,
                    'open_price': candle.open_price,
                    'high_price': candle.high_price,
                    'low_price': candle.low_price,
                    'close_price': candle.close_price,
                    'volume': candle.volume,
                    'quote_volume': candle.quote_volume,
                    'trades_count': candle.trades_count
                }
                market_data_list.append(market_data)
            
            # Сохраняем в БД
            await self.database.save_market_data(market_data_list)
            self.logger.debug(f"💾 Saved {len(market_data_list)} candles to database")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to save to database: {e}")
    
    async def _load_from_file_cache(self, request: HistoricalDataRequest) -> List[CandleData]:
        """Загрузка данных из файлового кеша"""
        try:
            cache_filename = f"{request.symbol}_{request.timeframe}.json"
            cache_file = self.cache_dir / cache_filename
            
            if not cache_file.exists():
                return []
            
            with open(cache_file, 'r') as f:
                cached_data = json.load(f)
            
            # Конвертируем в CandleData объекты
            candles = []
            for data in cached_data:
                candle = CandleData(**data)
                
                # Фильтруем по времени если нужно
                if (request.start_time <= candle.open_time <= request.end_time):
                    candles.append(candle)
            
            # Ограничиваем количество
            if len(candles) > request.limit:
                candles = candles[-request.limit:]  # Берем последние
            
            self.logger.debug(f"📂 Loaded {len(candles)} candles from file cache")
            return candles
            
        except Exception as e:
            self.logger.error(f"❌ Failed to load from file cache: {e}")
            return []
    
    async def _save_to_file_cache(self, candles: List[CandleData], symbol: str, timeframe: str) -> None:
        """Сохранение данных в файловый кеш"""
        try:
            if not candles:
                return
            
            cache_filename = f"{symbol}_{timeframe}.json"
            cache_file = self.cache_dir / cache_filename
            
            # Загружаем существующие данные
            existing_data = []
            if cache_file.exists():
                try:
                    with open(cache_file, 'r') as f:
                        existing_data = json.load(f)
                except:
                    pass
            
            # Добавляем новые данные
            new_data = [candle.to_dict() for candle in candles]
            
            # Объединяем и сортируем по времени
            all_data = existing_data + new_data
            
            # Удаляем дубликаты по open_time
            unique_data = {}
            for item in all_data:
                key = item['open_time']
                unique_data[key] = item
            
            # Сортируем по времени
            sorted_data = sorted(unique_data.values(), key=lambda x: x['open_time'])
            
            # Ограничиваем размер кеша (последние 10000 свечей)
            if len(sorted_data) > 10000:
                sorted_data = sorted_data[-10000:]
            
            # Сохраняем
            with open(cache_file, 'w') as f:
                json.dump(sorted_data, f, indent=2)
            
            self.logger.debug(f"💾 Saved {len(sorted_data)} candles to file cache")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to save to file cache: {e}")
    
    async def _get_fallback_data(
        self, 
        symbol: str, 
        timeframe: str, 
        start_time: int = None, 
        end_time: int = None
    ) -> List[CandleData]:
        """Получение данных из fallback источников при ошибке"""
        try:
            # Сначала пробуем базу данных
            if self.database:
                db_data = await self.database.get_historical_data(symbol, timeframe, 1000)
                if db_data:
                    candles = []
                    for db_candle in db_data:
                        candle = CandleData(
                            symbol=db_candle.symbol,
                            timeframe=db_candle.timeframe,
                            open_time=db_candle.open_time,
                            close_time=db_candle.close_time,
                            open_price=float(db_candle.open_price),
                            high_price=float(db_candle.high_price),
                            low_price=float(db_candle.low_price),
                            close_price=float(db_candle.close_price),
                            volume=float(db_candle.volume),
                            quote_volume=float(db_candle.quote_volume) if db_candle.quote_volume else None,
                            trades_count=db_candle.trades_count
                        )
                        candles.append(candle)
                    
                    self.logger.debug(f"📊 Loaded {len(candles)} candles from database fallback")
                    return candles
            
            # Затем пробуем файловый кеш
            request = HistoricalDataRequest(symbol, timeframe, start_time, end_time)
            file_data = await self._load_from_file_cache(request)
            if file_data:
                return file_data
            
            return []
            
        except Exception as e:
            self.logger.error(f"❌ Fallback data retrieval failed: {e}")
            return []
    
    async def _generate_mock_data(self, request: HistoricalDataRequest) -> List[CandleData]:
        """Генерация mock данных для разработки"""
        try:
            self.logger.warning(f"🔧 Generating mock data for {request.symbol} {request.timeframe}")
            
            candles = []
            timeframe_ms = parse_timeframe_to_milliseconds(request.timeframe)
            
            # Базовая цена в зависимости от символа
            base_prices = {
                'BTCUSDT': 45000.0,
                'ETHUSDT': 3000.0,
                'ADAUSDT': 0.5,
                'SOLUSDT': 100.0,
                'DOTUSDT': 15.0
            }
            
            base_price = base_prices.get(request.symbol, 1000.0)
            current_price = base_price
            current_time = request.start_time or (request.end_time - (request.limit * timeframe_ms))
            
            for i in range(request.limit):
                # Генерируем реалистичные изменения цены
                price_change = np.random.normal(0, 0.02)  # 2% волатильность
                new_price = current_price * (1 + price_change)
                
                # OHLC данные
                open_price = current_price
                close_price = new_price
                high_price = max(open_price, close_price) * (1 + abs(np.random.normal(0, 0.01)))
                low_price = min(open_price, close_price) * (1 - abs(np.random.normal(0, 0.01)))
                volume = np.random.uniform(100, 10000)
                
                candle = CandleData(
                    symbol=request.symbol,
                    timeframe=request.timeframe,
                    open_time=current_time,
                    close_time=current_time + timeframe_ms - 1,
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    volume=volume,
                    quote_volume=volume * ((open_price + close_price) / 2)
                )
                
                candles.append(candle)
                current_price = new_price
                current_time += timeframe_ms
                
                # Прекращаем если достигли end_time
                if current_time > request.end_time:
                    break
            
            # Сохраняем mock данные в файловый кеш для следующих использований
            await self._save_to_file_cache(candles, request.symbol, request.timeframe)
            
            self.logger.info(f"🔧 Generated {len(candles)} mock candles")
            return candles
            
        except Exception as e:
            self.logger.error(f"❌ Mock data generation failed: {e}")
            return []
    
    def _convert_to_timestamp(self, time_input: Union[int, str, datetime]) -> int:
        """Конвертация различных форматов времени в timestamp миллисекунды"""
        if isinstance(time_input, int):
            # Если уже timestamp, проверяем формат (секунды vs миллисекунды)
            if time_input < 1e10:  # Вероятно секунды
                return int(time_input * 1000)
            else:  # Вероятно миллисекунды
                return time_input
                
        elif isinstance(time_input, str):
            # ISO строка
            try:
                dt = pd.to_datetime(time_input)
                return int(dt.timestamp() * 1000)
            except:
                raise ValueError(f"Invalid time string format: {time_input}")
                
        elif isinstance(time_input, datetime):
            # datetime объект
            return datetime_to_timestamp(time_input, milliseconds=True)
        
        else:
            raise ValueError(f"Unsupported time format: {type(time_input)}")
    
    # ========================================================================
    # PUBLIC CONVENIENCE METHODS
    # ========================================================================
    
    async def get_recent_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100
    ) -> List[CandleData]:
        """Получение последних N свечей"""
        return await self.get_historical_data(
            symbol=symbol,
            timeframe=timeframe,
            limit=count
        )
    
    async def get_candles_for_period(
        self,
        symbol: str,
        timeframe: str,
        days: int = 30
    ) -> List[CandleData]:
        """Получение свечей за определенное количество дней"""
        end_time = get_current_timestamp()
        start_time = end_time - (days * 24 * 60 * 60 * 1000)
        
        return await self.get_historical_data(
            symbol=symbol,
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
            limit=10000  # Большой лимит для длинных периодов
        )
    
    async def get_candles_between_dates(
        self,
        symbol: str,
        timeframe: str,
        start_date: Union[str, datetime],
        end_date: Union[str, datetime]
    ) -> List[CandleData]:
        """Получение свечей между конкретными датами"""
        start_ts = self._convert_to_timestamp(start_date)
        end_ts = self._convert_to_timestamp(end_date)
        
        return await self.get_historical_data(
            symbol=symbol,
            timeframe=timeframe,
            start_time=start_ts,
            end_time=end_ts,
            limit=10000
        )
    
    def clear_cache(self, symbol: str = None, timeframe: str = None) -> None:
        """Очистка кеша"""
        if symbol and timeframe:
            # Очистка конкретного кеша
            keys_to_remove = [k for k in self._memory_cache.keys() 
                             if k.startswith(f"{symbol.upper()}_{timeframe.lower()}")]
        else:
            # Полная очистка
            keys_to_remove = list(self._memory_cache.keys())
        
        for key in keys_to_remove:
            self._memory_cache.pop(key, None)
            self._cache_timestamps.pop(key, None)
        
        self.logger.info(f"🧹 Cleared {len(keys_to_remove)} cache entries")
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Получение статистики кеша"""
        return {
            'memory_cache_entries': len(self._memory_cache),
            'cache_hit_rate': (self.stats['cache_hits'] / max(self.stats['cache_hits'] + self.stats['cache_misses'], 1)) * 100,
            'total_requests': self.stats['requests_made'],
            'total_data_points': self.stats['data_points_fetched'],
            'total_errors': self.stats['errors'],
            **self.stats
        }
    
    def to_dataframe(self, candles: List[CandleData]) -> pd.DataFrame:
        """Конвертация свечей в pandas DataFrame"""
        if not candles:
            return pd.DataFrame()
        
        data = []
        for candle in candles:
            data.append({
                'timestamp': candle.open_time,
                'datetime': timestamp_to_datetime(candle.open_time),
                'open': candle.open_price,
                'high': candle.high_price,
                'low': candle.low_price,
                'close': candle.close_price,
                'volume': candle.volume,
                'quote_volume': candle.quote_volume,
                'trades_count': candle.trades_count
            })
        
        df = pd.DataFrame(data)
        df.set_index('datetime', inplace=True)
        df.sort_index(inplace=True)
        
        return df
    
    async def cleanup_old_cache_files(self, max_age_days: int = 7) -> None:
        """Очистка старых файлов кеша"""
        try:
            cutoff_time = time.time() - (max_age_days * 24 * 60 * 60)
            
            cleaned_files = 0
            for cache_file in self.cache_dir.glob("*.json"):
                if cache_file.stat().st_mtime < cutoff_time:
                    cache_file.unlink()
                    cleaned_files += 1
            
            self.logger.info(f"🧹 Cleaned {cleaned_files} old cache files")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to cleanup old cache files: {e}")


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

async def get_historical_candles(
    symbol: str,
    timeframe: str,
    limit: int = 100,
    bybit_client=None,
    database=None
) -> List[CandleData]:
    """Быстрое получение исторических данных"""
    manager = HistoricalDataManager(bybit_client, database)
    return await manager.get_historical_data(symbol, timeframe, limit=limit)


async def get_ohlcv_dataframe(
    symbol: str,
    timeframe: str,
    days: int = 30,
    bybit_client=None,
    database=None
) -> pd.DataFrame:
    """Получение данных в формате DataFrame"""
    manager = HistoricalDataManager(bybit_client, database)
    candles = await manager.get_candles_for_period(symbol, timeframe, days)
    return manager.to_dataframe(candles)


# ============================================================================
# EXAMPLE USAGE AND TESTING
# ============================================================================

if __name__ == "__main__":
    async def test_historical_data():
        """Тестирование функций исторических данных"""
        print("🧪 Testing Historical Data Manager")
        print("=" * 50)
        
        # Создаем менеджер без внешних зависимостей
        manager = HistoricalDataManager()
        
        try:
            # Тест 1: Получение недавних данных (будет использовать mock)
            print("📊 Test 1: Getting recent candles...")
            candles = await manager.get_recent_candles('BTCUSDT', '5m', 50)
            print(f"✅ Got {len(candles)} candles")
            
            if candles:
                print(f"First candle: {candles[0].open_time} - Price: {candles[0].close_price}")
                print(f"Last candle: {candles[-1].open_time} - Price: {candles[-1].close_price}")
            
            # Тест 2: Конвертация в DataFrame
            print("\n📊 Test 2: Converting to DataFrame...")
            df = manager.to_dataframe(candles)
            print(f"✅ DataFrame shape: {df.shape}")
            print(df.head())
            
            # Тест 3: Статистика кеша
            print("\n📊 Test 3: Cache statistics...")
            stats = manager.get_cache_stats()
            print(f"✅ Cache stats: {stats}")
            
            # Тест 4: Тестирование файлового кеша
            print("\n📊 Test 4: File cache test...")
            await manager._save_to_file_cache(candles, 'BTCUSDT', '5m')
            print("✅ Data saved to file cache")
            
            # Тест 5: Загрузка из файлового кеша
            request = HistoricalDataRequest('BTCUSDT', '5m')
            cached_candles = await manager._load_from_file_cache(request)
            print(f"✅ Loaded {len(cached_candles)} candles from file cache")
            
            print("\n✅ All historical data tests completed!")
            
        except Exception as e:
            print(f"❌ Test failed: {e}")
            import traceback
            traceback.print_exc()
    
    # Запуск тестов
    asyncio.run(test_historical_data())
