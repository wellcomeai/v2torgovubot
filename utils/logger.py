"""
Trading Bot Logging System
Централизованная система логирования с поддержкой файлов и консоли
"""

import os
import sys
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from functools import lru_cache
import json
import traceback


# ============================================================================
# КОНСТАНТЫ И КОНФИГУРАЦИЯ
# ============================================================================

DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Эмодзи для разных уровней логирования
LOG_EMOJIS = {
    'DEBUG': '🔍',
    'INFO': 'ℹ️',
    'WARNING': '⚠️',
    'ERROR': '❌',
    'CRITICAL': '🔥'
}

# Цвета для консольного вывода
LOG_COLORS = {
    'DEBUG': '\033[36m',     # Cyan
    'INFO': '\033[32m',      # Green
    'WARNING': '\033[33m',   # Yellow
    'ERROR': '\033[31m',     # Red
    'CRITICAL': '\033[35m',  # Magenta
    'RESET': '\033[0m'       # Reset
}


# ============================================================================
# КАСТОМНЫЕ ФОРМАТТЕРЫ
# ============================================================================

class ColoredFormatter(logging.Formatter):
    """
    Форматтер с цветным выводом для консоли
    """
    
    def __init__(self, fmt: Optional[str] = None, datefmt: Optional[str] = None):
        super().__init__(fmt, datefmt)
    
    def format(self, record: logging.LogRecord) -> str:
        # Добавляем цвет и эмодзи
        level_name = record.levelname
        color = LOG_COLORS.get(level_name, LOG_COLORS['RESET'])
        emoji = LOG_EMOJIS.get(level_name, '')
        reset = LOG_COLORS['RESET']
        
        # Создаем копию record чтобы не изменять оригинал
        record_copy = logging.makeLogRecord(record.__dict__)
        record_copy.levelname = f"{color}{emoji} {level_name}{reset}"
        
        return super().format(record_copy)


class JSONFormatter(logging.Formatter):
    """
    JSON форматтер для структурированного логирования
    """
    
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
            'thread': record.thread,
            'process': record.process,
        }
        
        # Добавляем exception info если есть
        if record.exc_info:
            log_data['exception'] = {
                'type': record.exc_info[0].__name__ if record.exc_info[0] else None,
                'message': str(record.exc_info[1]) if record.exc_info[1] else None,
                'traceback': traceback.format_exception(*record.exc_info)
            }
        
        # Добавляем дополнительные поля из extra
        for key, value in record.__dict__.items():
            if key not in ['name', 'msg', 'args', 'levelname', 'levelno', 'pathname',
                          'filename', 'module', 'lineno', 'funcName', 'created',
                          'msecs', 'relativeCreated', 'thread', 'threadName',
                          'processName', 'process', 'exc_info', 'exc_text', 'stack_info']:
                log_data[key] = value
        
        return json.dumps(log_data, default=str, ensure_ascii=False)


class TradingFormatter(logging.Formatter):
    """
    Специальный форматтер для торговых операций
    """
    
    def format(self, record: logging.LogRecord) -> str:
        # Добавляем контекстную информацию для торговых логов
        if hasattr(record, 'symbol'):
            record.symbol_info = f"[{record.symbol}]"
        else:
            record.symbol_info = ""
            
        if hasattr(record, 'strategy'):
            record.strategy_info = f"[{record.strategy}]"
        else:
            record.strategy_info = ""
        
        # Форматируем с дополнительной информацией
        emoji = LOG_EMOJIS.get(record.levelname, '')
        formatted = f"{emoji} {record.asctime} - {record.name} - {record.levelname}"
        
        if record.symbol_info:
            formatted += f" {record.symbol_info}"
        if record.strategy_info:
            formatted += f" {record.strategy_info}"
            
        formatted += f" - {record.getMessage()}"
        
        return formatted


# ============================================================================
# ФИЛЬТРЫ
# ============================================================================

class SensitiveDataFilter(logging.Filter):
    """
    Фильтр для скрытия чувствительных данных
    """
    
    SENSITIVE_PATTERNS = [
        'api_key', 'api_secret', 'password', 'token', 'secret', 'private_key'
    ]
    
    def filter(self, record: logging.LogRecord) -> bool:
        # Проверяем сообщение на наличие чувствительных данных
        message = record.getMessage().lower()
        
        for pattern in self.SENSITIVE_PATTERNS:
            if pattern in message:
                # Заменяем чувствительную информацию на звездочки
                record.msg = self._mask_sensitive_data(str(record.msg))
                break
        
        return True
    
    def _mask_sensitive_data(self, message: str) -> str:
        """Маскировка чувствительных данных"""
        import re
        
        # Паттерны для маскировки
        patterns = [
            (r'(api_key["\']?\s*[:=]\s*["\']?)([^"\'>\s]+)', r'\1***MASKED***'),
            (r'(api_secret["\']?\s*[:=]\s*["\']?)([^"\'>\s]+)', r'\1***MASKED***'),
            (r'(password["\']?\s*[:=]\s*["\']?)([^"\'>\s]+)', r'\1***MASKED***'),
            (r'(token["\']?\s*[:=]\s*["\']?)([^"\'>\s]+)', r'\1***MASKED***'),
        ]
        
        for pattern, replacement in patterns:
            message = re.sub(pattern, replacement, message, flags=re.IGNORECASE)
        
        return message


class PerformanceFilter(logging.Filter):
    """
    Фильтр для добавления информации о производительности
    """
    
    def filter(self, record: logging.LogRecord) -> bool:
        # Добавляем метку времени с высокой точностью
        import time
        record.high_precision_time = time.perf_counter()
        
        # Добавляем информацию о памяти если доступно
        try:
            import psutil
            process = psutil.Process()
            record.memory_mb = round(process.memory_info().rss / 1024 / 1024, 2)
            record.cpu_percent = round(process.cpu_percent(), 2)
        except ImportError:
            pass
        
        return True


# ============================================================================
# ОСНОВНОЙ КЛАСС ЛОГИРОВАНИЯ
# ============================================================================

class TradingBotLogger:
    """
    Главный класс для управления логированием торгового бота
    """
    
    def __init__(self):
        self._loggers: Dict[str, logging.Logger] = {}
        self._handlers: Dict[str, logging.Handler] = {}
        self._initialized = False
    
    def setup(
        self,
        log_level: str = "INFO",
        log_format: Optional[str] = None,
        log_date_format: Optional[str] = None,
        log_to_file: bool = True,
        log_file_path: str = "logs/trading_bot.log",
        log_file_max_size: int = 10 * 1024 * 1024,  # 10MB
        log_file_backup_count: int = 5,
        colored_console: bool = True,
        json_format: bool = False,
        enable_performance_logging: bool = True,
        **kwargs
    ) -> None:
        """
        Настройка системы логирования
        
        Args:
            log_level: Уровень логирования
            log_format: Формат логов
            log_date_format: Формат даты
            log_to_file: Логировать в файл
            log_file_path: Путь к файлу логов
            log_file_max_size: Максимальный размер файла
            log_file_backup_count: Количество архивных файлов
            colored_console: Цветной вывод в консоль
            json_format: JSON формат для файлов
            enable_performance_logging: Включить performance метрики
        """
        if self._initialized:
            return
        
        # Создаем папку для логов
        log_dir = Path(log_file_path).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # Настройка форматтеров
        log_format = log_format or DEFAULT_FORMAT
        log_date_format = log_date_format or DEFAULT_DATE_FORMAT
        
        # Консольный форматтер
        if colored_console:
            console_formatter = ColoredFormatter(log_format, log_date_format)
        else:
            console_formatter = logging.Formatter(log_format, log_date_format)
        
        # Файловый форматтер
        if json_format:
            file_formatter = JSONFormatter()
        else:
            file_formatter = logging.Formatter(log_format, log_date_format)
        
        # Создаем консольный handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)
        console_handler.addFilter(SensitiveDataFilter())
        
        if enable_performance_logging:
            console_handler.addFilter(PerformanceFilter())
        
        self._handlers['console'] = console_handler
        
        # Создаем файловый handler если нужно
        if log_to_file:
            file_handler = logging.handlers.RotatingFileHandler(
                filename=log_file_path,
                maxBytes=log_file_max_size,
                backupCount=log_file_backup_count,
                encoding='utf-8'
            )
            file_handler.setFormatter(file_formatter)
            file_handler.addFilter(SensitiveDataFilter())
            
            if enable_performance_logging:
                file_handler.addFilter(PerformanceFilter())
            
            self._handlers['file'] = file_handler
        
        # Настраиваем root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, log_level.upper()))
        
        # Очищаем существующие handlers
        root_logger.handlers.clear()
        
        # Добавляем наши handlers
        for handler in self._handlers.values():
            root_logger.addHandler(handler)
        
        self._initialized = True
        
        # Логируем успешную инициализацию
        logger = self.get_logger("system.logger")
        logger.info(f"🚀 Trading Bot Logger initialized (level: {log_level}, file: {log_to_file})")
    
    def get_logger(self, name: str) -> logging.Logger:
        """
        Получение логгера по имени с кешированием
        """
        if name not in self._loggers:
            logger = logging.getLogger(name)
            self._loggers[name] = logger
        
        return self._loggers[name]
    
    def create_trading_logger(self, component: str) -> logging.Logger:
        """
        Создание специального логгера для торговых операций
        """
        name = f"trading.{component}"
        logger = self.get_logger(name)
        
        # Добавляем специальный handler для торговых логов
        if 'trading' not in self._handlers:
            trading_handler = logging.handlers.RotatingFileHandler(
                filename="logs/trading.log",
                maxBytes=5 * 1024 * 1024,  # 5MB
                backupCount=3,
                encoding='utf-8'
            )
            trading_handler.setFormatter(TradingFormatter(datefmt=DEFAULT_DATE_FORMAT))
            trading_handler.addFilter(SensitiveDataFilter())
            self._handlers['trading'] = trading_handler
            logger.addHandler(trading_handler)
        
        return logger
    
    def create_strategy_logger(self, strategy_name: str) -> logging.Logger:
        """
        Создание логгера для конкретной стратегии
        """
        name = f"strategy.{strategy_name}"
        return self.get_logger(name)
    
    def create_websocket_logger(self) -> logging.Logger:
        """
        Создание логгера для WebSocket операций
        """
        return self.get_logger("websocket")
    
    def log_performance_metrics(
        self,
        component: str,
        metrics: Dict[str, Any]
    ) -> None:
        """
        Логирование метрик производительности
        """
        logger = self.get_logger(f"performance.{component}")
        logger.info("📊 Performance metrics", extra={
            'component': component,
            'metrics': metrics,
            'log_type': 'performance'
        })
    
    def log_trading_event(
        self,
        event_type: str,
        symbol: str,
        strategy: Optional[str] = None,
        message: str = "",
        **extra_data
    ) -> None:
        """
        Специальное логирование торговых событий
        """
        logger = self.create_trading_logger("events")
        logger.info(message, extra={
            'event_type': event_type,
            'symbol': symbol,
            'strategy': strategy,
            'log_type': 'trading_event',
            **extra_data
        })
    
    def shutdown(self) -> None:
        """
        Корректное завершение работы логгеров
        """
        for handler in self._handlers.values():
            handler.close()
        
        logging.shutdown()
        self._initialized = False


# ============================================================================
# SINGLETON INSTANCE
# ============================================================================

@lru_cache()
def get_logger_instance() -> TradingBotLogger:
    """
    Получение единственного экземпляра логгера
    """
    return TradingBotLogger()


# Глобальный экземпляр
bot_logger = get_logger_instance()


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def setup_logger(
    name: str,
    log_level: str = "INFO",
    **kwargs
) -> logging.Logger:
    """
    Быстрая настройка логгера
    """
    # Инициализируем систему если еще не сделано
    if not bot_logger._initialized:
        bot_logger.setup(log_level=log_level, **kwargs)
    
    return bot_logger.get_logger(name)


def get_trading_logger(component: str) -> logging.Logger:
    """
    Получение логгера для торговых операций
    """
    return bot_logger.create_trading_logger(component)


def get_strategy_logger(strategy_name: str) -> logging.Logger:
    """
    Получение логгера для стратегии
    """
    return bot_logger.create_strategy_logger(strategy_name)


def get_websocket_logger() -> logging.Logger:
    """
    Получение логгера для WebSocket
    """
    return bot_logger.create_websocket_logger()


def log_performance(component: str, **metrics) -> None:
    """
    Быстрое логирование метрик производительности
    """
    bot_logger.log_performance_metrics(component, metrics)


def log_trading_event(
    event_type: str,
    symbol: str,
    message: str,
    strategy: Optional[str] = None,
    **extra
) -> None:
    """
    Быстрое логирование торгового события
    """
    bot_logger.log_trading_event(
        event_type=event_type,
        symbol=symbol,
        strategy=strategy,
        message=message,
        **extra
    )


# ============================================================================
# CONTEXT MANAGERS
# ============================================================================

class LoggingContext:
    """
    Контекстный менеджер для добавления дополнительной информации в логи
    """
    
    def __init__(self, logger: logging.Logger, **context):
        self.logger = logger
        self.context = context
        self.old_factory = None
    
    def __enter__(self):
        old_factory = logging.getLogRecordFactory()
        self.old_factory = old_factory
        
        def record_factory(*args, **kwargs):
            record = old_factory(*args, **kwargs)
            for key, value in self.context.items():
                setattr(record, key, value)
            return record
        
        logging.setLogRecordFactory(record_factory)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        logging.setLogRecordFactory(self.old_factory)


# ============================================================================
# DECORATORS
# ============================================================================

def log_execution_time(logger_name: Optional[str] = None):
    """
    Декоратор для логирования времени выполнения функций
    """
    def decorator(func):
        import time
        from functools import wraps
        
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            logger = bot_logger.get_logger(logger_name or func.__module__)
            start_time = time.perf_counter()
            
            try:
                result = await func(*args, **kwargs)
                execution_time = time.perf_counter() - start_time
                logger.debug(f"⏱️ {func.__name__} executed in {execution_time:.3f}s")
                return result
            except Exception as e:
                execution_time = time.perf_counter() - start_time
                logger.error(f"❌ {func.__name__} failed after {execution_time:.3f}s: {e}")
                raise
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            logger = bot_logger.get_logger(logger_name or func.__module__)
            start_time = time.perf_counter()
            
            try:
                result = func(*args, **kwargs)
                execution_time = time.perf_counter() - start_time
                logger.debug(f"⏱️ {func.__name__} executed in {execution_time:.3f}s")
                return result
            except Exception as e:
                execution_time = time.perf_counter() - start_time
                logger.error(f"❌ {func.__name__} failed after {execution_time:.3f}s: {e}")
                raise
        
        # Возвращаем подходящий wrapper
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def configure_external_loggers(level: str = "WARNING"):
    """
    Настройка уровня логирования для внешних библиотек
    """
    external_loggers = [
        'urllib3.connectionpool',
        'requests.packages.urllib3',
        'websocket',
        'aiohttp.access',
        'sqlalchemy.engine',
        'uvicorn.access',
    ]
    
    for logger_name in external_loggers:
        logging.getLogger(logger_name).setLevel(getattr(logging, level.upper()))


def setup_production_logging():
    """
    Специальная настройка для продакшна
    """
    bot_logger.setup(
        log_level="INFO",
        log_to_file=True,
        log_file_path="logs/production.log",
        log_file_max_size=50 * 1024 * 1024,  # 50MB
        log_file_backup_count=10,
        colored_console=False,
        json_format=True,
        enable_performance_logging=True
    )
    
    # Уменьшаем шум от внешних библиотек
    configure_external_loggers("WARNING")


def setup_development_logging():
    """
    Специальная настройка для разработки
    """
    bot_logger.setup(
        log_level="DEBUG",
        log_to_file=True,
        log_file_path="logs/development.log",
        log_file_max_size=10 * 1024 * 1024,  # 10MB
        log_file_backup_count=3,
        colored_console=True,
        json_format=False,
        enable_performance_logging=True
    )
    
    # Более детальные логи в разработке
    configure_external_loggers("INFO")


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Пример использования
    setup_development_logging()
    
    # Создаем разные типы логгеров
    main_logger = setup_logger("main")
    trading_logger = get_trading_logger("engine")
    strategy_logger = get_strategy_logger("moving_average")
    websocket_logger = get_websocket_logger()
    
    # Тестируем разные уровни
    main_logger.debug("🔍 This is debug message")
    main_logger.info("ℹ️ Bot is starting...")
    main_logger.warning("⚠️ This is a warning")
    main_logger.error("❌ This is an error")
    main_logger.critical("🔥 This is critical!")
    
    # Тестируем торговые события
    log_trading_event(
        event_type="SIGNAL_GENERATED",
        symbol="BTCUSDT",
        strategy="moving_average",
        message="🎯 Strong BUY signal generated",
        confidence=0.85,
        price=50000.0
    )
    
    # Тестируем контекстный менеджер
    with LoggingContext(trading_logger, symbol="ETHUSDT", strategy="rsi"):
        trading_logger.info("📊 Processing market data")
        trading_logger.info("🎯 Signal generated")
    
    # Тестируем performance логирование
    log_performance("trading_engine", 
                   signals_processed=150,
                   avg_processing_time=0.023,
                   memory_usage_mb=234.5)
    
    print("\n✅ Logger system test completed!")
