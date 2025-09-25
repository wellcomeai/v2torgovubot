"""
Trading Bot Core Module
Ядро системы - основные компоненты торгового бота
"""

# Импорты будут добавлены по мере создания компонентов
try:
    from .bybit_client import BybitLinearClient, BybitAPIError
except ImportError:
    pass

try:
    from .data_manager import DataManager, MarketDataCache
except ImportError:
    pass

try: 
    from .signal_processor import SignalProcessor, ProcessedSignal
except ImportError:
    pass

try:
    from .trading_engine import TradingEngine, EngineStatus
except ImportError:
    pass

try:
    from .websocket_manager import WebSocketManager, WSConnectionStatus
except ImportError:
    pass

# Версия модуля
__version__ = "1.0.0"

# Экспортируемые классы и функции
__all__ = [
    # Bybit API Client
    "BybitLinearClient",
    "BybitAPIError",
    
    # Data Management
    "DataManager", 
    "MarketDataCache",
    
    # Signal Processing
    "SignalProcessor",
    "ProcessedSignal",
    
    # Trading Engine
    "TradingEngine",
    "EngineStatus", 
    
    # WebSocket Management
    "WebSocketManager",
    "WSConnectionStatus",
]

# Константы ядра системы
CORE_CONSTANTS = {
    'MAX_CONCURRENT_REQUESTS': 10,
    'DEFAULT_TIMEOUT': 30,
    'WEBSOCKET_RECONNECT_DELAY': 5,
    'MAX_WEBSOCKET_RETRIES': 10,
    'SIGNAL_PROCESSING_TIMEOUT': 10,
    'ENGINE_HEALTH_CHECK_INTERVAL': 30,
    'DATA_CACHE_TTL': 60,
    'MAX_CACHED_CANDLES': 1000
}

def get_core_constant(name: str):
    """Получение константы ядра системы"""
    return CORE_CONSTANTS.get(name)
