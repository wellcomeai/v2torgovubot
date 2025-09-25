"""
Trading Bot Configuration Settings
Полная конфигурация готовая к деплою на Render
"""

import os
import logging
from typing import Optional, List, Dict, Any
from functools import lru_cache
from pydantic import BaseSettings, validator, Field
from enum import Enum


class Environment(str, Enum):
    """Типы окружений"""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LogLevel(str, Enum):
    """Уровни логирования"""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Settings(BaseSettings):
    """
    Главный класс конфигурации с валидацией
    """
    
    # ============================================================================
    # ОСНОВНЫЕ НАСТРОЙКИ ПРИЛОЖЕНИЯ
    # ============================================================================
    
    APP_NAME: str = Field(default="Advanced Trading Bot", description="Название приложения")
    VERSION: str = Field(default="1.0.0", description="Версия приложения")
    ENVIRONMENT: Environment = Field(default=Environment.DEVELOPMENT, description="Окружение")
    DEBUG: bool = Field(default=False, description="Режим отладки")
    
    # Server настройки
    HOST: str = Field(default="0.0.0.0", description="IP адрес сервера")
    PORT: int = Field(default=8000, description="Порт сервера")
    
    # Timezone
    TIMEZONE: str = Field(default="UTC", description="Часовой пояс")
    
    
    # ============================================================================
    # BYBIT API НАСТРОЙКИ
    # ============================================================================
    
    BYBIT_API_KEY: str = Field(..., description="Bybit API ключ")
    BYBIT_API_SECRET: str = Field(..., description="Bybit API секрет")
    BYBIT_TESTNET: bool = Field(default=True, description="Использовать testnet")
    BYBIT_DEMO: bool = Field(default=False, description="Использовать demo аккаунт")
    BYBIT_RSA_AUTHENTICATION: bool = Field(default=False, description="RSA аутентификация")
    
    # Bybit connection settings
    BYBIT_RECV_WINDOW: int = Field(default=5000, description="Receive window (ms)")
    BYBIT_TIMEOUT: int = Field(default=10, description="Request timeout (seconds)")
    BYBIT_MAX_RETRIES: int = Field(default=3, description="Максимум retry попыток")
    BYBIT_RETRY_DELAY: int = Field(default=3, description="Задержка между retry (seconds)")
    
    # Bybit regional settings
    BYBIT_DOMAIN: str = Field(default="bybit", description="Домен (bybit/bytick/bybit-tr)")
    BYBIT_TLD: str = Field(default="com", description="TLD (com/nl/com.hk/kz/eu)")
    
    # WebSocket настройки
    WS_PING_INTERVAL: int = Field(default=20, description="WebSocket ping интервал")
    WS_PING_TIMEOUT: int = Field(default=10, description="WebSocket ping timeout")
    WS_RETRIES: int = Field(default=10, description="WebSocket reconnect retries (0 = infinite)")
    WS_RESTART_ON_ERROR: bool = Field(default=True, description="Автореконнект WebSocket")
    WS_TRACE_LOGGING: bool = Field(default=False, description="Детальное логирование WS")
    
    
    # ============================================================================
    # OPENAI НАСТРОЙКИ
    # ============================================================================
    
    OPENAI_API_KEY: str = Field(..., description="OpenAI API ключ")
    OPENAI_MODEL: str = Field(default="gpt-4", description="OpenAI модель")
    OPENAI_MAX_TOKENS: int = Field(default=1000, description="Максимум токенов в ответе")
    OPENAI_TEMPERATURE: float = Field(default=0.7, description="Temperature для GPT")
    OPENAI_TIMEOUT: int = Field(default=30, description="Timeout для OpenAI запросов")
    
    
    # ============================================================================
    # TELEGRAM НАСТРОЙКИ
    # ============================================================================
    
    TELEGRAM_BOT_TOKEN: str = Field(..., description="Telegram Bot Token")
    TELEGRAM_CHAT_ID: str = Field(..., description="Telegram Chat ID для уведомлений")
    TELEGRAM_PARSE_MODE: str = Field(default="Markdown", description="Режим парсинга сообщений")
    TELEGRAM_DISABLE_NOTIFICATION: bool = Field(default=False, description="Отключить звуки")
    
    # Дополнительные Telegram настройки
    TELEGRAM_MAX_MESSAGE_LENGTH: int = Field(default=4000, description="Макс длина сообщения")
    TELEGRAM_RETRY_ATTEMPTS: int = Field(default=3, description="Retry попытки")
    TELEGRAM_TIMEOUT: int = Field(default=10, description="Timeout запросов")
    
    
    # ============================================================================
    # БАЗА ДАННЫХ НАСТРОЙКИ
    # ============================================================================
    
    DATABASE_URL: str = Field(default="sqlite:///trading_bot.db", description="URL базы данных")
    DATABASE_ECHO: bool = Field(default=False, description="Логировать SQL запросы")
    DATABASE_POOL_SIZE: int = Field(default=5, description="Размер connection pool")
    DATABASE_POOL_TIMEOUT: int = Field(default=30, description="Pool timeout")
    DATABASE_POOL_RECYCLE: int = Field(default=3600, description="Connection recycle time")
    
    # Backup настройки
    DATABASE_BACKUP_ENABLED: bool = Field(default=True, description="Включить бэкапы БД")
    DATABASE_BACKUP_INTERVAL: int = Field(default=24, description="Интервал бэкапов (часы)")
    DATABASE_BACKUP_RETENTION: int = Field(default=7, description="Сколько бэкапов хранить")
    
    
    # ============================================================================
    # ТОРГОВЫЕ НАСТРОЙКИ
    # ============================================================================
    
    # Поддерживаемые торговые пары
    SUPPORTED_SYMBOLS: List[str] = Field(
        default=["BTCUSDT", "ETHUSDT", "ADAUSDT", "SOLUSDT", "DOTUSDT"],
        description="Поддерживаемые торговые пары"
    )
    
    # Таймфреймы
    SUPPORTED_TIMEFRAMES: List[str] = Field(
        default=["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
        description="Поддерживаемые таймфреймы"
    )
    
    # Лимиты
    MAX_POSITION_SIZE: float = Field(default=1000.0, description="Максимальный размер позиции USDT")
    MAX_LEVERAGE: int = Field(default=10, description="Максимальное плечо")
    MIN_ORDER_SIZE: float = Field(default=5.0, description="Минимальный размер ордера USDT")
    
    # Risk Management
    MAX_DAILY_LOSS: float = Field(default=100.0, description="Максимальная дневная потеря USDT")
    MAX_DRAWDOWN: float = Field(default=0.1, description="Максимальная просадка (10%)")
    STOP_LOSS_PERCENTAGE: float = Field(default=0.02, description="Стоп-лосс процент (2%)")
    TAKE_PROFIT_PERCENTAGE: float = Field(default=0.04, description="Тейк-профит процент (4%)")
    
    
    # ============================================================================
    # СТРАТЕГИИ НАСТРОЙКИ
    # ============================================================================
    
    # Активные стратегии по умолчанию
    DEFAULT_ACTIVE_STRATEGIES: Dict[str, List[Dict[str, Any]]] = Field(
        default={
            "BTCUSDT": [
                {
                    "name": "moving_average",
                    "params": {
                        "fast_period": 10,
                        "slow_period": 20,
                        "source": "close"
                    },
                    "timeframe": "5m",
                    "enabled": True
                }
            ]
        },
        description="Стратегии по умолчанию"
    )
    
    # Настройки стратегий
    STRATEGY_MAX_SIGNALS_PER_HOUR: int = Field(default=10, description="Макс сигналов в час")
    STRATEGY_COOLDOWN_MINUTES: int = Field(default=5, description="Время между сигналами (минуты)")
    STRATEGY_CONFIDENCE_THRESHOLD: float = Field(default=0.7, description="Минимальная уверенность")
    
    
    # ============================================================================
    # БЭКТЕСТИНГ НАСТРОЙКИ
    # ============================================================================
    
    BACKTEST_INITIAL_BALANCE: float = Field(default=10000.0, description="Начальный баланс для бэктеста")
    BACKTEST_COMMISSION: float = Field(default=0.001, description="Комиссия (0.1%)")
    BACKTEST_SLIPPAGE: float = Field(default=0.0001, description="Проскальзывание (0.01%)")
    BACKTEST_MAX_HISTORY_DAYS: int = Field(default=365, description="Максимум дней истории")
    
    # Parallel execution
    BACKTEST_MAX_CONCURRENT: int = Field(default=3, description="Макс параллельных бэктестов")
    BACKTEST_TIMEOUT: int = Field(default=300, description="Timeout бэктеста (секунды)")
    
    
    # ============================================================================
    # ЛОГИРОВАНИЕ
    # ============================================================================
    
    LOG_LEVEL: LogLevel = Field(default=LogLevel.INFO, description="Уровень логирования")
    LOG_FORMAT: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        description="Формат логов"
    )
    LOG_DATE_FORMAT: str = Field(default="%Y-%m-%d %H:%M:%S", description="Формат даты в логах")
    
    # File logging
    LOG_TO_FILE: bool = Field(default=True, description="Логировать в файл")
    LOG_FILE_PATH: str = Field(default="logs/trading_bot.log", description="Путь к файлу логов")
    LOG_FILE_MAX_SIZE: int = Field(default=10485760, description="Макс размер файла логов (10MB)")
    LOG_FILE_BACKUP_COUNT: int = Field(default=5, description="Количество архивных файлов логов")
    
    # Request logging
    LOG_REQUESTS: bool = Field(default=False, description="Логировать все запросы к API")
    LOG_RESPONSES: bool = Field(default=False, description="Логировать ответы API")
    LOG_WEBSOCKET_MESSAGES: bool = Field(default=False, description="Логировать WebSocket сообщения")
    
    
    # ============================================================================
    # МОНИТОРИНГ И ALERTING
    # ============================================================================
    
    # Health checks
    HEALTH_CHECK_INTERVAL: int = Field(default=30, description="Интервал health check (секунды)")
    HEALTH_CHECK_TIMEOUT: int = Field(default=5, description="Timeout health check")
    
    # Performance monitoring
    PERFORMANCE_MONITORING: bool = Field(default=True, description="Мониторинг производительности")
    MEMORY_LIMIT_MB: int = Field(default=512, description="Лимит памяти (MB)")
    CPU_USAGE_THRESHOLD: float = Field(default=80.0, description="Порог использования CPU (%)")
    
    # Alerts
    SEND_ERROR_ALERTS: bool = Field(default=True, description="Отправлять алерты об ошибках")
    SEND_PERFORMANCE_ALERTS: bool = Field(default=True, description="Алерты о производительности")
    ALERT_COOLDOWN_MINUTES: int = Field(default=30, description="Время между одинаковыми алертами")
    
    
    # ============================================================================
    # КЕШИРОВАНИЕ
    # ============================================================================
    
    CACHE_ENABLED: bool = Field(default=True, description="Включить кеширование")
    CACHE_TTL_SECONDS: int = Field(default=300, description="TTL кеша (5 минут)")
    CACHE_MAX_SIZE: int = Field(default=1000, description="Максимум элементов в кеше")
    
    # Market data caching
    MARKET_DATA_CACHE_TTL: int = Field(default=60, description="TTL для рыночных данных")
    HISTORICAL_DATA_CACHE_TTL: int = Field(default=3600, description="TTL для исторических данных")
    
    
    # ============================================================================
    # RATE LIMITING
    # ============================================================================
    
    RATE_LIMIT_REQUESTS_PER_SECOND: int = Field(default=5, description="Запросов в секунду")
    RATE_LIMIT_BURST: int = Field(default=10, description="Максимальный burst")
    RATE_LIMIT_ENABLED: bool = Field(default=True, description="Включить rate limiting")
    
    
    # ============================================================================
    # БЕЗОПАСНОСТЬ
    # ============================================================================
    
    SECRET_KEY: str = Field(default="super-secret-key-change-in-production", description="Секретный ключ")
    ALLOWED_HOSTS: List[str] = Field(default=["*"], description="Разрешенные хосты")
    CORS_ORIGINS: List[str] = Field(default=["*"], description="CORS origins")
    
    # API Security
    API_KEY_ENCRYPTION: bool = Field(default=True, description="Шифровать API ключи")
    SESSION_TIMEOUT: int = Field(default=3600, description="Timeout сессии (секунды)")
    
    
    # ============================================================================
    # VALIDATORS
    # ============================================================================
    
    @validator('ENVIRONMENT', pre=True)
    def validate_environment(cls, v):
        if isinstance(v, str):
            return Environment(v.lower())
        return v
    
    @validator('LOG_LEVEL', pre=True)
    def validate_log_level(cls, v):
        if isinstance(v, str):
            return LogLevel(v.upper())
        return v
    
    @validator('BYBIT_API_KEY', 'BYBIT_API_SECRET', 'OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN')
    def validate_required_secrets(cls, v, field):
        if not v or v.strip() == "":
            raise ValueError(f'{field.name} is required')
        return v.strip()
    
    @validator('TELEGRAM_CHAT_ID')
    def validate_telegram_chat_id(cls, v):
        if not v or v.strip() == "":
            raise ValueError('TELEGRAM_CHAT_ID is required')
        # Проверяем что это число или начинается с @
        v = v.strip()
        if not (v.startswith('@') or (v.startswith('-') and v[1:].isdigit()) or v.isdigit()):
            raise ValueError('TELEGRAM_CHAT_ID must be numeric or start with @')
        return v
    
    @validator('SUPPORTED_SYMBOLS')
    def validate_symbols(cls, v):
        if not v:
            raise ValueError('At least one symbol must be supported')
        return [symbol.upper() for symbol in v]
    
    @validator('MAX_LEVERAGE')
    def validate_leverage(cls, v):
        if v < 1 or v > 125:  # Bybit limits
            raise ValueError('Leverage must be between 1 and 125')
        return v
    
    @validator('STOP_LOSS_PERCENTAGE', 'TAKE_PROFIT_PERCENTAGE', 'MAX_DRAWDOWN')
    def validate_percentages(cls, v):
        if v <= 0 or v >= 1:
            raise ValueError('Percentage values must be between 0 and 1')
        return v
    
    
    # ============================================================================
    # COMPUTED PROPERTIES
    # ============================================================================
    
    @property
    def is_production(self) -> bool:
        """Проверка продакшн окружения"""
        return self.ENVIRONMENT == Environment.PRODUCTION
    
    @property
    def is_development(self) -> bool:
        """Проверка dev окружения"""
        return self.ENVIRONMENT == Environment.DEVELOPMENT
    
    @property
    def database_async_url(self) -> str:
        """Async URL для базы данных"""
        if self.DATABASE_URL.startswith('sqlite'):
            return self.DATABASE_URL.replace('sqlite:///', 'sqlite+aiosqlite:///')
        return self.DATABASE_URL
    
    @property
    def log_config(self) -> Dict[str, Any]:
        """Конфигурация для logging"""
        return {
            'version': 1,
            'disable_existing_loggers': False,
            'formatters': {
                'default': {
                    'format': self.LOG_FORMAT,
                    'datefmt': self.LOG_DATE_FORMAT,
                },
            },
            'handlers': {
                'default': {
                    'formatter': 'default',
                    'class': 'logging.StreamHandler',
                    'stream': 'ext://sys.stdout',
                },
            },
            'root': {
                'level': self.LOG_LEVEL,
                'handlers': ['default'],
            },
        }
    
    
    # ============================================================================
    # METHODS
    # ============================================================================
    
    def get_bybit_config(self) -> Dict[str, Any]:
        """Конфигурация для Bybit клиента"""
        return {
            'api_key': self.BYBIT_API_KEY,
            'api_secret': self.BYBIT_API_SECRET,
            'testnet': self.BYBIT_TESTNET,
            'demo': self.BYBIT_DEMO,
            'domain': self.BYBIT_DOMAIN,
            'tld': self.BYBIT_TLD,
            'rsa_authentication': self.BYBIT_RSA_AUTHENTICATION,
            'recv_window': self.BYBIT_RECV_WINDOW,
            'timeout': self.BYBIT_TIMEOUT,
            'max_retries': self.BYBIT_MAX_RETRIES,
            'retry_delay': self.BYBIT_RETRY_DELAY,
            'log_requests': self.LOG_REQUESTS,
        }
    
    def get_websocket_config(self) -> Dict[str, Any]:
        """Конфигурация для WebSocket"""
        return {
            'testnet': self.BYBIT_TESTNET,
            'demo': self.BYBIT_DEMO,
            'domain': self.BYBIT_DOMAIN,
            'tld': self.BYBIT_TLD,
            'api_key': self.BYBIT_API_KEY,
            'api_secret': self.BYBIT_API_SECRET,
            'ping_interval': self.WS_PING_INTERVAL,
            'ping_timeout': self.WS_PING_TIMEOUT,
            'retries': self.WS_RETRIES,
            'restart_on_error': self.WS_RESTART_ON_ERROR,
            'trace_logging': self.WS_TRACE_LOGGING,
        }
    
    def log_startup_config(self, logger: logging.Logger):
        """Безопасное логирование конфигурации при старте"""
        safe_config = {
            'APP_NAME': self.APP_NAME,
            'VERSION': self.VERSION,
            'ENVIRONMENT': self.ENVIRONMENT,
            'HOST': self.HOST,
            'PORT': self.PORT,
            'BYBIT_TESTNET': self.BYBIT_TESTNET,
            'BYBIT_DEMO': self.BYBIT_DEMO,
            'OPENAI_MODEL': self.OPENAI_MODEL,
            'SUPPORTED_SYMBOLS': self.SUPPORTED_SYMBOLS,
            'LOG_LEVEL': self.LOG_LEVEL,
            'DATABASE_URL': self.DATABASE_URL.split('://', 1)[0] + '://***',  # Hide credentials
        }
        
        logger.info("🚀 Trading Bot Configuration:")
        for key, value in safe_config.items():
            logger.info(f"  {key}: {value}")
    
    
    # ============================================================================
    # PYDANTIC CONFIG
    # ============================================================================
    
    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'
        case_sensitive = True
        validate_assignment = True
        extra = 'forbid'  # Запретить неизвестные поля


# ============================================================================
# SINGLETON INSTANCE
# ============================================================================

@lru_cache()
def get_settings() -> Settings:
    """
    Создание единственного экземпляра настроек с кешированием
    """
    return Settings()


# Глобальный экземпляр для удобства импорта
settings = get_settings()
