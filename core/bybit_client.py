"""
Trading Bot Bybit API Client
HTTP клиент для работы с Bybit REST API
"""

import asyncio
import time
import hmac
import hashlib
import json
from typing import Dict, Any, Optional, List, Union
from dataclasses import dataclass
from enum import Enum
import aiohttp
from urllib.parse import urlencode

from utils.logger import setup_logger
from utils.helpers import (
    get_current_timestamp, safe_float, safe_int,
    generate_signature, retry_async, Timer
)
from app.config.settings import Settings, get_settings


# ============================================================================
# CONSTANTS AND ENUMS
# ============================================================================

# Bybit API endpoints
BYBIT_MAINNET_BASE_URL = "https://api.bybit.com"
BYBIT_TESTNET_BASE_URL = "https://api-testnet.bybit.com"

# Rate limits (requests per minute)
BYBIT_RATE_LIMITS = {
    'default': 120,  # 120 requests per minute
    'kline': 600,    # 600 requests per minute for kline data
    'orderbook': 600, # 600 requests per minute for orderbook
    'account': 120   # 120 requests per minute for account info
}

# API categories
API_CATEGORIES = {
    'linear': 'linear',      # USDT Perpetual
    'inverse': 'inverse',    # Inverse Perpetual
    'spot': 'spot',         # Spot Trading
    'option': 'option'      # Options
}


class OrderSide(str, Enum):
    """Order sides"""
    BUY = "Buy"
    SELL = "Sell"


class OrderType(str, Enum):
    """Order types"""
    MARKET = "Market"
    LIMIT = "Limit"
    LIMIT_MAKER = "PostOnly"


class TimeInForce(str, Enum):
    """Time in force"""
    GTC = "GTC"  # Good Till Cancel
    IOC = "IOC"  # Immediate or Cancel
    FOK = "FOK"  # Fill or Kill
    POST_ONLY = "PostOnly"


@dataclass
class BybitAPIError(Exception):
    """Bybit API error"""
    ret_code: int
    ret_msg: str
    ext_code: Optional[str] = None
    ext_info: Optional[str] = None
    
    def __str__(self):
        return f"Bybit API Error {self.ret_code}: {self.ret_msg}"


# ============================================================================
# BYBIT LINEAR CLIENT
# ============================================================================

class BybitLinearClient:
    """
    Клиент для работы с Bybit Linear (USDT Perpetual) API
    
    Поддерживает:
    - Получение рыночных данных
    - Управление позициями и ордерами
    - Получение информации об аккаунте
    - Автоматический retry при ошибках
    - Rate limiting
    """
    
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.logger = setup_logger(f"{__name__}.BybitLinearClient")
        
        # API credentials
        self.api_key = self.settings.BYBIT_API_KEY
        self.api_secret = self.settings.BYBIT_API_SECRET
        
        # Connection settings
        self.testnet = self.settings.BYBIT_TESTNET
        self.demo = self.settings.BYBIT_DEMO
        self.base_url = BYBIT_TESTNET_BASE_URL if self.testnet else BYBIT_MAINNET_BASE_URL
        
        # Request settings
        self.recv_window = self.settings.BYBIT_RECV_WINDOW
        self.timeout = self.settings.BYBIT_TIMEOUT
        self.max_retries = self.settings.BYBIT_MAX_RETRIES
        self.retry_delay = self.settings.BYBIT_RETRY_DELAY
        
        # Rate limiting
        self._rate_limiter = {}
        self._last_request_time = {}
        
        # Session
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Statistics
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'rate_limited_requests': 0,
            'retry_attempts': 0
        }
        
        self.logger.info(f"🔌 Bybit client initialized (testnet: {self.testnet}, demo: {self.demo})")
    
    async def __aenter__(self):
        """Async context manager entry"""
        await self._create_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
    
    async def _create_session(self):
        """Создание HTTP сессии"""
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            connector = aiohttp.TCPConnector(
                limit=20,  # Max connections
                ttl_dns_cache=300,  # DNS cache TTL
                use_dns_cache=True
            )
            
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'TradingBot/1.0.0'
                }
            )
    
    async def close(self):
        """Закрытие сессии"""
        if self._session:
            await self._session.close()
            self._session = None
            self.logger.info("🔌 Bybit client session closed")
    
    def _generate_signature(self, params: Dict[str, Any], timestamp: int) -> str:
        """Генерация подписи для аутентификации"""
        try:
            # Сортируем параметры по ключу
            sorted_params = dict(sorted(params.items()))
            
            # Добавляем timestamp и recv_window
            sorted_params['timestamp'] = timestamp
            sorted_params['recv_window'] = self.recv_window
            
            # Создаем query string
            query_string = urlencode(sorted_params)
            
            # Генерируем HMAC signature
            signature = hmac.new(
                self.api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            return signature
            
        except Exception as e:
            self.logger.error(f"❌ Failed to generate signature: {e}")
            raise
    
    def _check_rate_limit(self, endpoint_type: str = 'default') -> bool:
        """Проверка rate limit"""
        try:
            limit = BYBIT_RATE_LIMITS.get(endpoint_type, BYBIT_RATE_LIMITS['default'])
            current_time = time.time()
            minute_window = int(current_time // 60)
            
            if endpoint_type not in self._rate_limiter:
                self._rate_limiter[endpoint_type] = {}
            
            # Очищаем старые окна
            keys_to_remove = [k for k in self._rate_limiter[endpoint_type].keys() if k < minute_window - 1]
            for key in keys_to_remove:
                del self._rate_limiter[endpoint_type][key]
            
            # Проверяем текущее окно
            current_count = self._rate_limiter[endpoint_type].get(minute_window, 0)
            
            if current_count >= limit:
                self.stats['rate_limited_requests'] += 1
                return False
            
            # Увеличиваем счетчик
            self._rate_limiter[endpoint_type][minute_window] = current_count + 1
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Rate limit check failed: {e}")
            return True  # В случае ошибки разрешаем запрос
    
    async def _wait_for_rate_limit(self, endpoint_type: str = 'default'):
        """Ожидание сброса rate limit"""
        while not self._check_rate_limit(endpoint_type):
            await asyncio.sleep(1)  # Ждем 1 секунду
    
    async def _make_request(
        self, 
        method: str, 
        endpoint: str, 
        params: Optional[Dict[str, Any]] = None,
        auth_required: bool = False,
        endpoint_type: str = 'default'
    ) -> Dict[str, Any]:
        """
        Выполнение HTTP запроса к Bybit API
        
        Args:
            method: HTTP метод (GET, POST, etc.)
            endpoint: API endpoint
            params: Параметры запроса
            auth_required: Требуется ли аутентификация
            endpoint_type: Тип endpoint для rate limiting
            
        Returns:
            Ответ API в виде словаря
        """
        try:
            if not self._session:
                await self._create_session()
            
            self.stats['total_requests'] += 1
            
            # Ожидаем rate limit
            await self._wait_for_rate_limit(endpoint_type)
            
            url = f"{self.base_url}{endpoint}"
            params = params or {}
            headers = {}
            
            # Аутентификация если требуется
            if auth_required:
                timestamp = get_current_timestamp()
                signature = self._generate_signature(params, timestamp)
                
                headers.update({
                    'X-BAPI-API-KEY': self.api_key,
                    'X-BAPI-SIGN': signature,
                    'X-BAPI-TIMESTAMP': str(timestamp),
                    'X-BAPI-RECV-WINDOW': str(self.recv_window)
                })
            
            # Выполняем запрос с таймером
            with Timer(f"Bybit API {method} {endpoint}"):
                if method.upper() == 'GET':
                    async with self._session.get(url, params=params, headers=headers) as response:
                        response_data = await self._handle_response(response)
                
                elif method.upper() == 'POST':
                    async with self._session.post(url, json=params, headers=headers) as response:
                        response_data = await self._handle_response(response)
                
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")
            
            # Проверяем ответ на ошибки
            self._validate_response(response_data)
            
            self.stats['successful_requests'] += 1
            
            if self.settings.LOG_RESPONSES:
                self.logger.debug(f"📤 API Response: {response_data}")
            
            return response_data
            
        except Exception as e:
            self.stats['failed_requests'] += 1
            self.logger.error(f"❌ API request failed: {method} {endpoint} - {e}")
            raise
    
    async def _handle_response(self, response: aiohttp.ClientResponse) -> Dict[str, Any]:
        """Обработка HTTP ответа"""
        try:
            response_text = await response.text()
            
            if response.content_type == 'application/json':
                response_data = json.loads(response_text)
            else:
                response_data = {'data': response_text}
            
            # Добавляем статус код
            response_data['_status_code'] = response.status
            
            return response_data
            
        except Exception as e:
            self.logger.error(f"❌ Failed to handle response: {e}")
            raise
    
    def _validate_response(self, response_data: Dict[str, Any]):
        """Валидация ответа API"""
        try:
            # Проверяем HTTP статус
            status_code = response_data.get('_status_code', 200)
            if status_code >= 400:
                raise BybitAPIError(
                    ret_code=status_code,
                    ret_msg=f"HTTP Error {status_code}"
                )
            
            # Проверяем ответ Bybit API
            ret_code = response_data.get('retCode', 0)
            if ret_code != 0:
                raise BybitAPIError(
                    ret_code=ret_code,
                    ret_msg=response_data.get('retMsg', 'Unknown error'),
                    ext_code=response_data.get('retExtInfo', {}).get('code'),
                    ext_info=response_data.get('retExtInfo', {}).get('msg')
                )
            
        except BybitAPIError:
            raise
        except Exception as e:
            self.logger.error(f"❌ Response validation failed: {e}")
            raise BybitAPIError(ret_code=-1, ret_msg=f"Validation error: {str(e)}")
    
    # ========================================================================
    # MARKET DATA ENDPOINTS
    # ========================================================================
    
    async def get_kline(
        self,
        symbol: str,
        interval: str,
        start: Optional[int] = None,
        end: Optional[int] = None,
        limit: int = 200,
        category: str = 'linear'
    ) -> Dict[str, Any]:
        """
        Получение данных свечей (kline)
        
        Args:
            symbol: Торговая пара (например, 'BTCUSDT')
            interval: Интервал ('1m', '5m', '15m', '30m', '1h', '4h', '1d')
            start: Время начала (timestamp в миллисекундах)
            end: Время окончания (timestamp в миллисекундах)
            limit: Максимальное количество свечей (по умолчанию 200)
            category: Категория торговли ('linear' для USDT Perpetual)
            
        Returns:
            Словарь с данными свечей
        """
        params = {
            'category': category,
            'symbol': symbol,
            'interval': interval,
            'limit': min(limit, 1000)  # Bybit максимум 1000
        }
        
        if start:
            params['start'] = start
        if end:
            params['end'] = end
        
        return await self._make_request(
            'GET', 
            '/v5/market/kline',
            params=params,
            endpoint_type='kline'
        )
    
    async def get_orderbook(
        self,
        symbol: str,
        limit: int = 50,
        category: str = 'linear'
    ) -> Dict[str, Any]:
        """
        Получение книги ордеров
        
        Args:
            symbol: Торговая пара
            limit: Глубина книги (1, 50, 200)
            category: Категория торговли
            
        Returns:
            Словарь с данными книги ордеров
        """
        params = {
            'category': category,
            'symbol': symbol,
            'limit': limit
        }
        
        return await self._make_request(
            'GET',
            '/v5/market/orderbook', 
            params=params,
            endpoint_type='orderbook'
        )
    
    async def get_tickers(
        self,
        symbol: Optional[str] = None,
        category: str = 'linear'
    ) -> Dict[str, Any]:
        """
        Получение данных тикеров
        
        Args:
            symbol: Торговая пара (опционально, если None - все пары)
            category: Категория торговли
            
        Returns:
            Словарь с данными тикеров
        """
        params = {
            'category': category
        }
        
        if symbol:
            params['symbol'] = symbol
        
        return await self._make_request(
            'GET',
            '/v5/market/tickers',
            params=params
        )
    
    async def get_instruments_info(
        self,
        symbol: Optional[str] = None,
        category: str = 'linear'
    ) -> Dict[str, Any]:
        """
        Получение информации о торговых инструментах
        
        Args:
            symbol: Торговая пара (опционально)
            category: Категория торговли
            
        Returns:
            Словарь с информацией об инструментах
        """
        params = {
            'category': category
        }
        
        if symbol:
            params['symbol'] = symbol
        
        return await self._make_request(
            'GET',
            '/v5/market/instruments-info',
            params=params
        )
    
    # ========================================================================
    # ACCOUNT ENDPOINTS
    # ========================================================================
    
    async def get_account_info(self) -> Dict[str, Any]:
        """
        Получение информации об аккаунте
        
        Returns:
            Словарь с данными аккаунта
        """
        return await self._make_request(
            'GET',
            '/v5/account/info',
            auth_required=True,
            endpoint_type='account'
        )
    
    async def get_wallet_balance(self, account_type: str = 'UNIFIED') -> Dict[str, Any]:
        """
        Получение баланса кошелька
        
        Args:
            account_type: Тип аккаунта ('UNIFIED', 'CONTRACT')
            
        Returns:
            Словарь с балансом
        """
        params = {
            'accountType': account_type
        }
        
        return await self._make_request(
            'GET',
            '/v5/account/wallet-balance',
            params=params,
            auth_required=True,
            endpoint_type='account'
        )
    
    async def get_positions(
        self,
        symbol: Optional[str] = None,
        category: str = 'linear'
    ) -> Dict[str, Any]:
        """
        Получение информации о позициях
        
        Args:
            symbol: Торговая пара (опционально)
            category: Категория торговли
            
        Returns:
            Словарь с данными о позициях
        """
        params = {
            'category': category
        }
        
        if symbol:
            params['symbol'] = symbol
        
        return await self._make_request(
            'GET',
            '/v5/position/list',
            params=params,
            auth_required=True,
            endpoint_type='account'
        )
    
    # ========================================================================
    # ORDER MANAGEMENT ENDPOINTS
    # ========================================================================
    
    async def place_order(
        self,
        symbol: str,
        side: Union[str, OrderSide],
        order_type: Union[str, OrderType],
        qty: str,
        price: Optional[str] = None,
        time_in_force: Union[str, TimeInForce] = TimeInForce.GTC,
        reduce_only: bool = False,
        close_on_trigger: bool = False,
        category: str = 'linear'
    ) -> Dict[str, Any]:
        """
        Размещение ордера
        
        Args:
            symbol: Торговая пара
            side: Сторона ордера (Buy/Sell)
            order_type: Тип ордера (Market/Limit)
            qty: Количество
            price: Цена (для лимитных ордеров)
            time_in_force: Время жизни ордера
            reduce_only: Только для закрытия позиции
            close_on_trigger: Закрыть при срабатывании
            category: Категория торговли
            
        Returns:
            Словарь с информацией о размещенном ордере
        """
        params = {
            'category': category,
            'symbol': symbol,
            'side': side.value if isinstance(side, OrderSide) else side,
            'orderType': order_type.value if isinstance(order_type, OrderType) else order_type,
            'qty': str(qty),
            'timeInForce': time_in_force.value if isinstance(time_in_force, TimeInForce) else time_in_force
        }
        
        if price:
            params['price'] = str(price)
        
        if reduce_only:
            params['reduceOnly'] = True
            
        if close_on_trigger:
            params['closeOnTrigger'] = True
        
        return await self._make_request(
            'POST',
            '/v5/order/create',
            params=params,
            auth_required=True
        )
    
    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        order_link_id: Optional[str] = None,
        category: str = 'linear'
    ) -> Dict[str, Any]:
        """
        Отмена ордера
        
        Args:
            symbol: Торговая пара
            order_id: ID ордера (один из order_id или order_link_id обязателен)
            order_link_id: Пользовательский ID ордера
            category: Категория торговли
            
        Returns:
            Словарь с информацией об отмененном ордере
        """
        if not order_id and not order_link_id:
            raise ValueError("Either order_id or order_link_id must be provided")
        
        params = {
            'category': category,
            'symbol': symbol
        }
        
        if order_id:
            params['orderId'] = order_id
        if order_link_id:
            params['orderLinkId'] = order_link_id
        
        return await self._make_request(
            'POST',
            '/v5/order/cancel',
            params=params,
            auth_required=True
        )
    
    async def get_open_orders(
        self,
        symbol: Optional[str] = None,
        category: str = 'linear',
        limit: int = 50
    ) -> Dict[str, Any]:
        """
        Получение открытых ордеров
        
        Args:
            symbol: Торговая пара (опционально)
            category: Категория торговли
            limit: Максимальное количество ордеров
            
        Returns:
            Словарь с открытыми ордерами
        """
        params = {
            'category': category,
            'limit': limit
        }
        
        if symbol:
            params['symbol'] = symbol
        
        return await self._make_request(
            'GET',
            '/v5/order/realtime',
            params=params,
            auth_required=True
        )
    
    async def get_order_history(
        self,
        symbol: Optional[str] = None,
        category: str = 'linear',
        limit: int = 50
    ) -> Dict[str, Any]:
        """
        Получение истории ордеров
        
        Args:
            symbol: Торговая пара (опционально)
            category: Категория торговли
            limit: Максимальное количество ордеров
            
        Returns:
            Словарь с историей ордеров
        """
        params = {
            'category': category,
            'limit': limit
        }
        
        if symbol:
            params['symbol'] = symbol
        
        return await self._make_request(
            'GET',
            '/v5/order/history',
            params=params,
            auth_required=True
        )
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    async def test_connection(self) -> bool:
        """
        Тестирование подключения к API
        
        Returns:
            True если подключение успешно
        """
        try:
            await self.get_instruments_info(symbol='BTCUSDT')
            self.logger.info("✅ Bybit API connection test successful")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Bybit API connection test failed: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики клиента"""
        total = self.stats['total_requests']
        if total == 0:
            success_rate = 0
        else:
            success_rate = (self.stats['successful_requests'] / total) * 100
        
        return {
            **self.stats,
            'success_rate_pct': round(success_rate, 2),
            'testnet_mode': self.testnet,
            'demo_mode': self.demo
        }
    
    def reset_stats(self):
        """Сброс статистики"""
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'rate_limited_requests': 0,
            'retry_attempts': 0
        }
        self.logger.info("📊 Client statistics reset")


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

async def create_bybit_client(settings: Optional[Settings] = None) -> BybitLinearClient:
    """Создание и инициализация Bybit клиента"""
    client = BybitLinearClient(settings)
    await client._create_session()
    return client


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    async def test_bybit_client():
        """Тестирование Bybit клиента"""
        print("🧪 Testing Bybit Linear Client")
        print("=" * 50)
        
        try:
            settings = get_settings()
            
            # Создаем клиента
            async with BybitLinearClient(settings) as client:
                print(f"✅ Client created: testnet={client.testnet}")
                
                # Тест 1: Тестирование соединения
                print("\n📝 Test 1: Testing connection...")
                connection_ok = await client.test_connection()
                print(f"✅ Connection test: {'PASSED' if connection_ok else 'FAILED'}")
                
                # Тест 2: Получение информации об инструментах
                print("\n📝 Test 2: Getting instruments info...")
                instruments = await client.get_instruments_info(symbol='BTCUSDT')
                if 'result' in instruments and 'list' in instruments['result']:
                    print(f"✅ Instruments info: {len(instruments['result']['list'])} instruments")
                else:
                    print("❌ No instruments data")
                
                # Тест 3: Получение тикеров
                print("\n📝 Test 3: Getting tickers...")
                tickers = await client.get_tickers(symbol='BTCUSDT')
                if 'result' in tickers and 'list' in tickers['result']:
                    ticker_data = tickers['result']['list'][0]
                    print(f"✅ BTCUSDT ticker: {ticker_data.get('lastPrice', 'N/A')}")
                else:
                    print("❌ No ticker data")
                
                # Тест 4: Получение данных свечей
                print("\n📝 Test 4: Getting kline data...")
                klines = await client.get_kline(
                    symbol='BTCUSDT',
                    interval='1h',
                    limit=10
                )
                if 'result' in klines and 'list' in klines['result']:
                    candles_count = len(klines['result']['list'])
                    print(f"✅ Kline data: {candles_count} candles")
                    if candles_count > 0:
                        latest_candle = klines['result']['list'][0]
                        print(f"   Latest candle: {latest_candle}")
                else:
                    print("❌ No kline data")
                
                # Тест 5: Статистика клиента
                print("\n📝 Test 5: Client statistics...")
                stats = client.get_stats()
                print(f"✅ Client stats: {stats}")
                
                print("\n✅ All Bybit client tests completed!")
                
        except Exception as e:
            print(f"❌ Test failed: {e}")
            import traceback
            traceback.print_exc()
    
    # Запуск тестирования
    asyncio.run(test_bybit_client())
