"""
Trading Bot Helper Functions
Набор универсальных вспомогательных функций
"""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from functools import wraps, lru_cache
from typing import (
    Any, Dict, List, Optional, Union, Callable, TypeVar, 
    Tuple, Iterator, AsyncIterator
)
import re
import os
import sys
from pathlib import Path
import importlib
import inspect
from contextlib import asynccontextmanager

from utils.logger import setup_logger


# ============================================================================
# TYPE DEFINITIONS
# ============================================================================

T = TypeVar('T')
F = TypeVar('F', bound=Callable[..., Any])
Number = Union[int, float, Decimal]


# ============================================================================
# CONSTANTS
# ============================================================================

# Precision для различных валют
PRICE_PRECISION = {
    'BTCUSDT': 2,
    'ETHUSDT': 2, 
    'ADAUSDT': 4,
    'SOLUSDT': 4,
    'DOTUSDT': 3,
    'LINKUSDT': 3,
    'UNIUSDT': 3,
    'LTCUSDT': 2,
    'BCHUSDT': 2,
    'XRPUSDT': 4,
}

QUANTITY_PRECISION = {
    'BTCUSDT': 6,
    'ETHUSDT': 5,
    'ADAUSDT': 1,
    'SOLUSDT': 2,
    'DOTUSDT': 2,
    'LINKUSDT': 2,
    'UNIUSDT': 2,
    'LTCUSDT': 5,
    'BCHUSDT': 5,
    'XRPUSDT': 1,
}

# Минимальные размеры ордеров
MIN_ORDER_VALUES = {
    'BTCUSDT': 5.0,
    'ETHUSDT': 5.0,
    'ADAUSDT': 5.0,
    'SOLUSDT': 5.0,
    'DOTUSDT': 5.0,
}

# Таймфреймы в миллисекундах
TIMEFRAME_TO_SECONDS = {
    '1m': 60,
    '5m': 300,
    '15m': 900,
    '30m': 1800,
    '1h': 3600,
    '4h': 14400,
    '1d': 86400,
    '1w': 604800,
}

logger = setup_logger(__name__)


# ============================================================================
# DATETIME AND TIME UTILITIES
# ============================================================================

def get_current_timestamp() -> int:
    """Получение текущего timestamp в миллисекундах"""
    return int(time.time() * 1000)


def get_current_utc_datetime() -> datetime:
    """Получение текущего UTC datetime"""
    return datetime.now(timezone.utc)


def timestamp_to_datetime(timestamp: Union[int, float]) -> datetime:
    """Конвертация timestamp в datetime (поддерживает секунды и миллисекунды)"""
    if isinstance(timestamp, (int, float)):
        # Определяем формат timestamp (секунды или миллисекунды)
        if timestamp > 1e10:  # Миллисекунды
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return timestamp


def datetime_to_timestamp(dt: datetime, milliseconds: bool = True) -> int:
    """Конвертация datetime в timestamp"""
    timestamp = dt.timestamp()
    return int(timestamp * 1000) if milliseconds else int(timestamp)


def parse_timeframe_to_seconds(timeframe: str) -> int:
    """Конвертация строки таймфрейма в секунды"""
    return TIMEFRAME_TO_SECONDS.get(timeframe.lower(), 60)


def parse_timeframe_to_milliseconds(timeframe: str) -> int:
    """Конвертация строки таймфрейма в миллисекунды"""
    return parse_timeframe_to_seconds(timeframe) * 1000


def get_candle_open_time(timestamp: int, timeframe: str) -> int:
    """Получение времени открытия свечи для данного timestamp"""
    timeframe_ms = parse_timeframe_to_milliseconds(timeframe)
    return (timestamp // timeframe_ms) * timeframe_ms


def get_next_candle_time(current_time: int, timeframe: str) -> int:
    """Получение времени следующей свечи"""
    timeframe_ms = parse_timeframe_to_milliseconds(timeframe)
    candle_open = get_candle_open_time(current_time, timeframe)
    return candle_open + timeframe_ms


def format_duration(seconds: float) -> str:
    """Форматирование длительности в читаемый вид"""
    if seconds < 60:
        return f"{seconds:.2f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}m"
    elif seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    else:
        return f"{seconds / 86400:.1f}d"


def is_market_open(symbol: str = "BTCUSDT") -> bool:
    """Проверка открыт ли рынок (для крипто всегда True)"""
    # Криптовалютные рынки работают 24/7
    return True


def get_market_session() -> str:
    """Определение текущей торговой сессии"""
    now_utc = get_current_utc_datetime()
    hour = now_utc.hour
    
    if 0 <= hour < 8:
        return "ASIA"
    elif 8 <= hour < 16:
        return "LONDON" 
    elif 16 <= hour < 24:
        return "NEW_YORK"
    else:
        return "UNKNOWN"


# ============================================================================
# NUMERIC UTILITIES
# ============================================================================

def safe_float(value: Any, default: float = 0.0) -> float:
    """Безопасная конвертация в float"""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Безопасная конвертация в int"""
    try:
        if value is None or value == "":
            return default
        return int(float(value))  # Через float для обработки "123.0"
    except (ValueError, TypeError):
        return default


def safe_decimal(value: Any, default: Decimal = Decimal('0')) -> Decimal:
    """Безопасная конвертация в Decimal"""
    try:
        if value is None or value == "":
            return default
        return Decimal(str(value))
    except (ValueError, TypeError, decimal.InvalidOperation):
        return default


def round_price(price: Number, symbol: str) -> float:
    """Округление цены с учетом precision символа"""
    precision = PRICE_PRECISION.get(symbol, 4)
    if isinstance(price, Decimal):
        return float(price.quantize(Decimal('0.1') ** precision, rounding=ROUND_HALF_UP))
    return round(float(price), precision)


def round_quantity(quantity: Number, symbol: str) -> float:
    """Округление количества с учетом precision символа"""
    precision = QUANTITY_PRECISION.get(symbol, 6)
    if isinstance(quantity, Decimal):
        return float(quantity.quantize(Decimal('0.1') ** precision, rounding=ROUND_DOWN))
    return round(float(quantity), precision)


def calculate_percentage_change(old_value: Number, new_value: Number) -> float:
    """Расчет изменения в процентах"""
    try:
        old_val = float(old_value)
        new_val = float(new_value)
        
        if old_val == 0:
            return 0.0
        
        return ((new_val - old_val) / old_val) * 100
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def calculate_pnl(
    entry_price: Number,
    exit_price: Number, 
    quantity: Number,
    side: str = "BUY"
) -> float:
    """Расчет PnL"""
    try:
        entry = float(entry_price)
        exit = float(exit_price)
        qty = float(quantity)
        
        if side.upper() == "BUY":
            return (exit - entry) * qty
        else:  # SELL
            return (entry - exit) * qty
            
    except (ValueError, TypeError):
        return 0.0


def calculate_position_size(
    account_balance: Number,
    risk_percent: Number,
    entry_price: Number,
    stop_loss_price: Number
) -> float:
    """Расчет размера позиции на основе риска"""
    try:
        balance = float(account_balance)
        risk_pct = float(risk_percent) / 100  # Конвертируем проценты
        entry = float(entry_price)
        stop_loss = float(stop_loss_price)
        
        risk_amount = balance * risk_pct
        price_diff = abs(entry - stop_loss)
        
        if price_diff == 0:
            return 0.0
        
        position_size = risk_amount / price_diff
        return position_size
        
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def is_valid_price(price: Number, symbol: str) -> bool:
    """Проверка валидности цены"""
    try:
        p = float(price)
        return p > 0 and not (p != p)  # Проверка на NaN
    except (ValueError, TypeError):
        return False


def is_valid_quantity(quantity: Number, symbol: str, price: Number = None) -> bool:
    """Проверка валидности количества"""
    try:
        qty = float(quantity)
        if qty <= 0:
            return False
        
        # Проверка минимального размера ордера
        if price is not None:
            min_order_value = MIN_ORDER_VALUES.get(symbol, 5.0)
            order_value = qty * float(price)
            return order_value >= min_order_value
        
        return True
        
    except (ValueError, TypeError):
        return False


def clamp(value: Number, min_value: Number, max_value: Number) -> float:
    """Ограничение значения в диапазоне"""
    return max(float(min_value), min(float(value), float(max_value)))


# ============================================================================
# STRING UTILITIES
# ============================================================================

def safe_string(value: Any, default: str = "") -> str:
    """Безопасная конвертация в строку"""
    try:
        if value is None:
            return default
        return str(value)
    except:
        return default


def truncate_string(text: str, max_length: int, suffix: str = "...") -> str:
    """Обрезка строки с добавлением суффикса"""
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix


def clean_symbol(symbol: str) -> str:
    """Очистка символа от лишних пробелов и приведение к верхнему регистру"""
    return symbol.strip().upper()


def normalize_timeframe(timeframe: str) -> str:
    """Нормализация таймфрейма"""
    tf = timeframe.strip().lower()
    
    # Конвертация различных форматов
    conversions = {
        '1min': '1m',
        '5min': '5m', 
        '15min': '15m',
        '30min': '30m',
        '1hour': '1h',
        '4hour': '4h',
        '1day': '1d',
        '1week': '1w',
    }
    
    return conversions.get(tf, tf)


def format_number(
    value: Number, 
    decimals: int = 2, 
    thousands_sep: bool = True
) -> str:
    """Форматирование числа для отображения"""
    try:
        num = float(value)
        
        if thousands_sep:
            return f"{num:,.{decimals}f}"
        else:
            return f"{num:.{decimals}f}"
            
    except (ValueError, TypeError):
        return "0.00"


def format_percentage(value: Number, decimals: int = 2) -> str:
    """Форматирование процента"""
    try:
        pct = float(value)
        return f"{pct:.{decimals}f}%"
    except (ValueError, TypeError):
        return "0.00%"


def parse_symbol_pair(symbol: str) -> Tuple[str, str]:
    """Разбор торговой пары на базовую и котируемую валюты"""
    symbol = clean_symbol(symbol)
    
    # Список известных quote валют в порядке приоритета
    quote_currencies = ['USDT', 'USDC', 'BUSD', 'BTC', 'ETH', 'BNB', 'USD']
    
    for quote in quote_currencies:
        if symbol.endswith(quote):
            base = symbol[:-len(quote)]
            return base, quote
    
    # Если не нашли известную quote валюту, возвращаем как есть
    return symbol, ""


def extract_numbers(text: str) -> List[float]:
    """Извлечение всех чисел из строки"""
    pattern = r'-?\d+\.?\d*'
    matches = re.findall(pattern, text)
    return [float(match) for match in matches]


def generate_random_string(length: int = 10) -> str:
    """Генерация случайной строки"""
    return str(uuid.uuid4())[:length]


# ============================================================================
# DICTIONARY AND LIST UTILITIES
# ============================================================================

def safe_get(data: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Безопасное получение значения из словаря с поддержкой вложенных ключей"""
    try:
        if '.' in key:
            keys = key.split('.')
            value = data
            for k in keys:
                if isinstance(value, dict) and k in value:
                    value = value[k]
                else:
                    return default
            return value
        else:
            return data.get(key, default)
    except (AttributeError, TypeError, KeyError):
        return default


def deep_merge(dict1: Dict, dict2: Dict) -> Dict:
    """Глубокое слияние двух словарей"""
    result = dict1.copy()
    
    for key, value in dict2.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    
    return result


def filter_dict(data: Dict[str, Any], allowed_keys: List[str]) -> Dict[str, Any]:
    """Фильтрация словаря по списку разрешенных ключей"""
    return {k: v for k, v in data.items() if k in allowed_keys}


def remove_none_values(data: Dict[str, Any]) -> Dict[str, Any]:
    """Удаление ключей с None значениями"""
    return {k: v for k, v in data.items() if v is not None}


def flatten_dict(data: Dict[str, Any], separator: str = '.') -> Dict[str, Any]:
    """Превращение вложенного словаря в плоский"""
    def _flatten(obj, parent_key=''):
        items = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                new_key = f"{parent_key}{separator}{k}" if parent_key else k
                if isinstance(v, dict):
                    items.extend(_flatten(v, new_key).items())
                else:
                    items.append((new_key, v))
        else:
            items.append((parent_key, obj))
        return dict(items)
    
    return _flatten(data)


def chunk_list(data: List[T], chunk_size: int) -> Iterator[List[T]]:
    """Разбиение списка на чанки"""
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]


def deduplicate_list(data: List[T], key: Callable[[T], Any] = None) -> List[T]:
    """Удаление дубликатов из списка"""
    if key is None:
        return list(dict.fromkeys(data))
    else:
        seen = set()
        result = []
        for item in data:
            k = key(item)
            if k not in seen:
                seen.add(k)
                result.append(item)
        return result


# ============================================================================
# JSON UTILITIES
# ============================================================================

def safe_json_loads(json_str: str, default: Any = None) -> Any:
    """Безопасный парсинг JSON"""
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return default


def safe_json_dumps(data: Any, default: str = "{}") -> str:
    """Безопасная сериализация в JSON"""
    try:
        return json.dumps(data, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return default


class DecimalJSONEncoder(json.JSONEncoder):
    """JSON encoder с поддержкой Decimal"""
    
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        elif isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def json_dumps_decimal(data: Any) -> str:
    """JSON сериализация с поддержкой Decimal"""
    return json.dumps(data, cls=DecimalJSONEncoder, ensure_ascii=False)


# ============================================================================
# ASYNC UTILITIES
# ============================================================================

def run_in_executor(func: Callable) -> Callable:
    """Декоратор для запуска синхронной функции в executor"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, func, *args, **kwargs)
    return wrapper


async def gather_with_limit(limit: int, *coros) -> List[Any]:
    """asyncio.gather с ограничением количества одновременных задач"""
    semaphore = asyncio.Semaphore(limit)
    
    async def _run_with_semaphore(coro):
        async with semaphore:
            return await coro
    
    return await asyncio.gather(*[_run_with_semaphore(coro) for coro in coros])


@asynccontextmanager
async def async_timeout(seconds: float):
    """Асинхронный контекстный менеджер для timeout"""
    try:
        yield await asyncio.wait_for(asyncio.create_task(asyncio.sleep(0)), timeout=seconds)
    except asyncio.TimeoutError:
        logger.warning(f"⏰ Timeout after {seconds}s")
        raise


async def retry_async(
    func: Callable,
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,)
) -> Any:
    """Повторные попытки для асинхронных функций"""
    for attempt in range(max_retries + 1):
        try:
            if asyncio.iscoroutinefunction(func):
                return await func()
            else:
                return func()
        except exceptions as e:
            if attempt == max_retries:
                logger.error(f"❌ Max retries ({max_retries}) exceeded for {func.__name__}")
                raise e
            
            wait_time = delay * (backoff ** attempt)
            logger.warning(f"⚠️ Attempt {attempt + 1} failed, retrying in {wait_time}s: {e}")
            await asyncio.sleep(wait_time)


def create_task_with_logging(coro, name: str = None) -> asyncio.Task:
    """Создание задачи с автоматическим логированием исключений"""
    task = asyncio.create_task(coro, name=name)
    
    def handle_exception(task: asyncio.Task):
        try:
            task.result()
        except Exception as e:
            task_name = task.get_name() or "unnamed_task"
            logger.error(f"❌ Task '{task_name}' failed: {e}")
    
    task.add_done_callback(handle_exception)
    return task


# ============================================================================
# VALIDATION UTILITIES
# ============================================================================

def validate_symbol(symbol: str, supported_symbols: List[str] = None) -> bool:
    """Валидация торгового символа"""
    if not symbol or not isinstance(symbol, str):
        return False
    
    symbol = clean_symbol(symbol)
    
    if supported_symbols:
        return symbol in supported_symbols
    
    # Базовая проверка формата (например, BTCUSDT)
    return bool(re.match(r'^[A-Z]{2,10}USDT?$', symbol))


def validate_timeframe(timeframe: str) -> bool:
    """Валидация таймфрейма"""
    if not timeframe or not isinstance(timeframe, str):
        return False
    
    normalized = normalize_timeframe(timeframe)
    return normalized in TIMEFRAME_TO_SECONDS


def validate_price(price: Any, min_price: float = 0.00000001) -> bool:
    """Валидация цены"""
    try:
        p = float(price)
        return p >= min_price and not (p != p)  # Проверка на NaN
    except (ValueError, TypeError):
        return False


def validate_email(email: str) -> bool:
    """Валидация email адреса"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


# ============================================================================
# ENCRYPTION AND HASHING UTILITIES
# ============================================================================

def generate_signature(
    secret: str,
    message: str,
    algorithm: str = 'sha256'
) -> str:
    """Генерация HMAC подписи"""
    try:
        secret_bytes = secret.encode('utf-8')
        message_bytes = message.encode('utf-8')
        
        if algorithm.lower() == 'sha256':
            signature = hmac.new(secret_bytes, message_bytes, hashlib.sha256)
        elif algorithm.lower() == 'sha512':
            signature = hmac.new(secret_bytes, message_bytes, hashlib.sha512)
        else:
            raise ValueError(f"Unsupported algorithm: {algorithm}")
        
        return signature.hexdigest()
        
    except Exception as e:
        logger.error(f"❌ Failed to generate signature: {e}")
        return ""


def hash_string(text: str, algorithm: str = 'sha256') -> str:
    """Хеширование строки"""
    try:
        text_bytes = text.encode('utf-8')
        
        if algorithm.lower() == 'sha256':
            hasher = hashlib.sha256()
        elif algorithm.lower() == 'md5':
            hasher = hashlib.md5()
        elif algorithm.lower() == 'sha512':
            hasher = hashlib.sha512()
        else:
            raise ValueError(f"Unsupported algorithm: {algorithm}")
        
        hasher.update(text_bytes)
        return hasher.hexdigest()
        
    except Exception as e:
        logger.error(f"❌ Failed to hash string: {e}")
        return ""


# ============================================================================
# FILE AND PATH UTILITIES
# ============================================================================

def ensure_dir_exists(path: Union[str, Path]) -> Path:
    """Создание директории если её нет"""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def get_project_root() -> Path:
    """Получение корневой директории проекта"""
    current_file = Path(__file__).resolve()
    
    # Ищем вверх по дереву директорий файлы, указывающие на корень проекта
    root_indicators = ['requirements.txt', 'setup.py', '.git', 'pyproject.toml']
    
    for parent in current_file.parents:
        if any((parent / indicator).exists() for indicator in root_indicators):
            return parent
    
    # Если не найден, возвращаем родительскую директорию текущего файла
    return current_file.parent.parent


def safe_filename(filename: str) -> str:
    """Создание безопасного имени файла"""
    # Удаляем или заменяем недопустимые символы
    invalid_chars = '<>:"/\\|?*'
    safe_name = filename
    
    for char in invalid_chars:
        safe_name = safe_name.replace(char, '_')
    
    # Ограничиваем длину
    if len(safe_name) > 200:
        safe_name = safe_name[:200]
    
    return safe_name


def get_file_size_mb(file_path: Union[str, Path]) -> float:
    """Получение размера файла в MB"""
    try:
        path = Path(file_path)
        if path.exists():
            return path.stat().st_size / (1024 * 1024)
        return 0.0
    except Exception:
        return 0.0


# ============================================================================
# IMPORT AND MODULE UTILITIES
# ============================================================================

def import_class_from_string(class_path: str) -> type:
    """Импорт класса по строковому пути"""
    try:
        module_path, class_name = class_path.rsplit('.', 1)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (ValueError, ImportError, AttributeError) as e:
        logger.error(f"❌ Failed to import class {class_path}: {e}")
        raise


def get_class_methods(cls: type, include_private: bool = False) -> List[str]:
    """Получение списка методов класса"""
    methods = []
    
    for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
        if include_private or not name.startswith('_'):
            methods.append(name)
    
    return methods


def is_class_available(class_path: str) -> bool:
    """Проверка доступности класса для импорта"""
    try:
        import_class_from_string(class_path)
        return True
    except Exception:
        return False


# ============================================================================
# PERFORMANCE AND PROFILING
# ============================================================================

class Timer:
    """Контекстный менеджер для измерения времени выполнения"""
    
    def __init__(self, name: str = "Operation"):
        self.name = name
        self.start_time = None
        self.end_time = None
    
    def __enter__(self):
        self.start_time = time.perf_counter()
        return self
    
    def __exit__(self, *args):
        self.end_time = time.perf_counter()
        duration = self.end_time - self.start_time
        logger.debug(f"⏱️ {self.name} took {duration:.4f}s")
    
    @property
    def elapsed(self) -> float:
        """Время выполнения в секундах"""
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0


def measure_execution_time(func: F) -> F:
    """Декоратор для измерения времени выполнения"""
    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        with Timer(f"{func.__name__}"):
            return func(*args, **kwargs)
    
    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        with Timer(f"{func.__name__}"):
            return await func(*args, **kwargs)
    
    return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper


def get_memory_usage() -> Dict[str, float]:
    """Получение информации об использовании памяти"""
    try:
        import psutil
        process = psutil.Process()
        memory_info = process.memory_info()
        
        return {
            'rss_mb': memory_info.rss / 1024 / 1024,  # Resident Set Size
            'vms_mb': memory_info.vms / 1024 / 1024,  # Virtual Memory Size
            'percent': process.memory_percent()
        }
    except ImportError:
        return {'error': 'psutil not available'}
    except Exception as e:
        return {'error': str(e)}


# ============================================================================
# CONFIGURATION AND ENVIRONMENT
# ============================================================================

def get_env_variable(
    var_name: str,
    default: Any = None,
    var_type: type = str
) -> Any:
    """Получение переменной окружения с типизацией"""
    value = os.getenv(var_name, default)
    
    if value is None or value == default:
        return default
    
    try:
        if var_type == bool:
            return value.lower() in ('true', '1', 'yes', 'on')
        elif var_type == int:
            return int(value)
        elif var_type == float:
            return float(value)
        elif var_type == list:
            return value.split(',') if isinstance(value, str) else value
        else:
            return var_type(value)
    except (ValueError, TypeError):
        logger.warning(f"⚠️ Invalid value for {var_name}: {value}, using default: {default}")
        return default


def load_config_from_env() -> Dict[str, Any]:
    """Загрузка конфигурации из переменных окружения"""
    config = {}
    
    # Основные настройки
    config['environment'] = get_env_variable('ENVIRONMENT', 'development')
    config['debug'] = get_env_variable('DEBUG', False, bool)
    config['port'] = get_env_variable('PORT', 8000, int)
    
    # API ключи
    config['bybit_api_key'] = get_env_variable('BYBIT_API_KEY')
    config['bybit_api_secret'] = get_env_variable('BYBIT_API_SECRET')
    config['openai_api_key'] = get_env_variable('OPENAI_API_KEY')
    
    return config


# ============================================================================
# EXCEPTION HANDLING
# ============================================================================

class TradingBotError(Exception):
    """Базовое исключение для торгового бота"""
    pass


class ValidationError(TradingBotError):
    """Ошибка валидации данных"""
    pass


class ConfigurationError(TradingBotError):
    """Ошибка конфигурации"""
    pass


class NetworkError(TradingBotError):
    """Сетевая ошибка"""
    pass


class APIError(TradingBotError):
    """Ошибка API"""
    def __init__(self, message: str, status_code: int = None, response_data: Dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data or {}


def handle_exception(func: F) -> F:
    """Декоратор для обработки исключений"""
    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"❌ Exception in {func.__name__}: {e}")
            raise
    
    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"❌ Exception in {func.__name__}: {e}")
            raise
    
    return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper


# ============================================================================
# TESTING UTILITIES
# ============================================================================

def create_mock_data(symbol: str = "BTCUSDT", count: int = 100) -> List[Dict]:
    """Создание mock данных для тестирования"""
    import random
    
    data = []
    base_price = 50000.0 if symbol == "BTCUSDT" else 3000.0
    
    for i in range(count):
        # Имитация реалистичных изменений цены
        price_change = random.uniform(-0.02, 0.02)  # ±2%
        base_price *= (1 + price_change)
        
        high = base_price * random.uniform(1.001, 1.02)
        low = base_price * random.uniform(0.98, 0.999)
        volume = random.uniform(100, 10000)
        
        data.append({
            'symbol': symbol,
            'timestamp': get_current_timestamp() - (count - i) * 60000,  # 1 минута назад каждая свеча
            'open': round(base_price, 2),
            'high': round(high, 2),
            'low': round(low, 2),
            'close': round(base_price * random.uniform(0.995, 1.005), 2),
            'volume': round(volume, 4)
        })
    
    return data


# ============================================================================
# EXAMPLE USAGE AND TESTS
# ============================================================================

if __name__ == "__main__":
    # Примеры использования helper функций
    
    print("🧪 Testing Trading Bot Helper Functions")
    print("=" * 50)
    
    # Тестирование времени
    print(f"Current timestamp: {get_current_timestamp()}")
    print(f"Current UTC datetime: {get_current_utc_datetime()}")
    print(f"Market session: {get_market_session()}")
    
    # Тестирование чисел
    print(f"Round price BTCUSDT: {round_price(50000.123456, 'BTCUSDT')}")
    print(f"Round quantity BTCUSDT: {round_quantity(0.123456789, 'BTCUSDT')}")
    print(f"Percentage change: {calculate_percentage_change(100, 110)}%")
    
    # Тестирование строк
    print(f"Clean symbol: {clean_symbol(' btcusdt ')}")
    print(f"Normalize timeframe: {normalize_timeframe('5min')}")
    print(f"Parse symbol pair: {parse_symbol_pair('BTCUSDT')}")
    
    # Тестирование валидации
    print(f"Valid symbol: {validate_symbol('BTCUSDT')}")
    print(f"Valid timeframe: {validate_timeframe('5m')}")
    print(f"Valid price: {validate_price(50000)}")
    
    # Тестирование производительности
    with Timer("Test operation"):
        time.sleep(0.1)
    
    # Создание mock данных
    mock_data = create_mock_data("BTCUSDT", 5)
    print(f"Mock data sample: {mock_data[0]}")
    
    print("✅ All helper function tests completed!")
