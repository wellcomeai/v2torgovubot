"""
Trading Bot Core Module
Ядро системы - основные компоненты торгового бота
"""

# ============================================================================
# BYBIT CLIENT
# ============================================================================
try:
    from .bybit_client import (
        BybitLinearClient, 
        BybitAPIError,
        OrderSide,
        OrderType,
        TimeInForce,
        create_bybit_client
    )
except ImportError:
    pass

# ============================================================================
# DATA MANAGEMENT
# ============================================================================
try:
    from .data_manager import (
        DataManager, 
        MarketDataCache,
        DataSubscription,
        create_data_manager
    )
except ImportError:
    pass

# ============================================================================
# SIGNAL PROCESSING
# ============================================================================
try: 
    from .signal_processor import (
        SignalProcessor, 
        ProcessedSignal,
        ProcessingStatus,
        FilterReason,
        SignalProcessingConfig,
        create_signal_processor
    )
except ImportError:
    pass

# ============================================================================
# WEBSOCKET MANAGEMENT
# ============================================================================
try:
    from .websocket_manager import (
        WebSocketManager,
        WSConnectionStatus,
        WSSubscription,
        WSMessage,
        create_websocket_manager
    )
except ImportError:
    pass

# ============================================================================
# TRADING ENGINE (TODO: Implement)
# ============================================================================
# try:
#     from .trading_engine import TradingEngine, EngineStatus
# except ImportError:
#     pass

# ============================================================================
# MODULE INFO
# ============================================================================

# Версия модуля
__version__ = "1.0.0"

# Экспортируемые классы и функции
__all__ = [
    # Bybit API Client
    "BybitLinearClient",
    "BybitAPIError", 
    "OrderSide",
    "OrderType",
    "TimeInForce",
    "create_bybit_client",
    
    # Data Management
    "DataManager", 
    "MarketDataCache",
    "DataSubscription",
    "create_data_manager",
    
    # Signal Processing
    "SignalProcessor",
    "ProcessedSignal",
    "ProcessingStatus",
    "FilterReason", 
    "SignalProcessingConfig",
    "create_signal_processor",
    
    # WebSocket Management
    "WebSocketManager",
    "WSConnectionStatus",
    "WSSubscription",
    "WSMessage", 
    "create_websocket_manager",
    
    # Trading Engine (TODO)
    # "TradingEngine",
    # "EngineStatus",
    
    # Core constants
    "CORE_CONSTANTS",
    "get_core_constant",
]

# ============================================================================
# CORE CONSTANTS
# ============================================================================

CORE_CONSTANTS = {
    # Request limits
    'MAX_CONCURRENT_REQUESTS': 10,
    'DEFAULT_TIMEOUT': 30,
    'DEFAULT_RETRIES': 3,
    
    # WebSocket settings
    'WEBSOCKET_RECONNECT_DELAY': 5,
    'MAX_WEBSOCKET_RETRIES': 10,
    'WEBSOCKET_PING_INTERVAL': 20,
    'WEBSOCKET_PING_TIMEOUT': 10,
    
    # Signal processing
    'SIGNAL_PROCESSING_TIMEOUT': 10,
    'MAX_SIGNALS_PER_HOUR': 10,
    'MIN_SIGNAL_CONFIDENCE': 0.7,
    'SIGNAL_COOLDOWN_MINUTES': 5,
    
    # Data management
    'DATA_CACHE_TTL': 60,
    'MAX_CACHED_CANDLES': 2000,
    'MAX_CACHE_SIZE': 1000,
    'CACHE_CLEANUP_INTERVAL': 300,
    
    # Engine health checks
    'ENGINE_HEALTH_CHECK_INTERVAL': 30,
    'COMPONENT_STARTUP_TIMEOUT': 60,
    'COMPONENT_SHUTDOWN_TIMEOUT': 30,
    
    # Rate limiting
    'BYBIT_RATE_LIMIT_DEFAULT': 120,  # requests per minute
    'BYBIT_RATE_LIMIT_KLINE': 600,
    'BYBIT_RATE_LIMIT_ORDERBOOK': 600,
    'BYBIT_RATE_LIMIT_ACCOUNT': 120,
    
    # Performance
    'MAX_CANDLES_PER_REQUEST': 1000,
    'MAX_QUEUE_SIZE': 1000,
    'WORKER_POOL_SIZE': 4,
    'MESSAGE_BATCH_SIZE': 50,
}

def get_core_constant(name: str, default=None):
    """
    Получение константы ядра системы
    
    Args:
        name: Имя константы
        default: Значение по умолчанию если константа не найдена
        
    Returns:
        Значение константы или default
    """
    return CORE_CONSTANTS.get(name, default)

# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

async def create_core_components(settings=None, database=None):
    """
    Создание основных компонентов ядра системы
    
    Args:
        settings: Настройки приложения
        database: Подключение к базе данных
        
    Returns:
        Словарь с созданными компонентами
    """
    try:
        components = {}
        
        # Создаем Bybit клиента
        if 'create_bybit_client' in globals():
            bybit_client = await create_bybit_client(settings)
            components['bybit_client'] = bybit_client
        
        # Создаем WebSocket менеджер
        if 'create_websocket_manager' in globals():
            ws_manager = create_websocket_manager(settings)
            components['websocket_manager'] = ws_manager
        
        # Создаем Data Manager
        if 'create_data_manager' in globals():
            data_manager = create_data_manager(
                settings=settings,
                database=database,
                bybit_client=components.get('bybit_client')
            )
            components['data_manager'] = data_manager
        
        # Создаем Signal Processor
        if 'create_signal_processor' in globals():
            signal_processor = create_signal_processor(
                settings=settings,
                database=database
            )
            components['signal_processor'] = signal_processor
            
            # Связываем с data manager
            if 'data_manager' in components:
                signal_processor.set_market_data_provider(components['data_manager'])
        
        return components
        
    except Exception as e:
        # Закрываем созданные компоненты при ошибке
        for component in components.values():
            if hasattr(component, 'close'):
                try:
                    if hasattr(component, '__aenter__'):  # async context manager
                        await component.__aexit__(None, None, None)
                    else:
                        await component.close()
                except:
                    pass
        raise


async def shutdown_core_components(components: dict):
    """
    Корректное завершение работы компонентов ядра
    
    Args:
        components: Словарь с компонентами от create_core_components
    """
    shutdown_order = [
        'signal_processor',
        'data_manager', 
        'websocket_manager',
        'bybit_client'
    ]
    
    for component_name in shutdown_order:
        if component_name in components:
            component = components[component_name]
            try:
                if hasattr(component, 'stop'):
                    await component.stop()
                elif hasattr(component, 'shutdown'):
                    await component.shutdown()
                elif hasattr(component, 'close'):
                    await component.close()
                elif hasattr(component, '__aexit__'):
                    await component.__aexit__(None, None, None)
            except Exception as e:
                # Логируем ошибку но продолжаем shutdown других компонентов
                print(f"Error shutting down {component_name}: {e}")


def get_core_status(components: dict) -> dict:
    """
    Получение статуса всех компонентов ядра
    
    Args:
        components: Словарь с компонентами
        
    Returns:
        Словарь со статусом каждого компонента
    """
    status = {}
    
    for name, component in components.items():
        try:
            if hasattr(component, 'health_check'):
                # Для асинхронных health check нужно вызывать отдельно
                status[name] = {'has_health_check': True}
            elif hasattr(component, 'get_stats'):
                status[name] = component.get_stats()
            elif hasattr(component, 'stats'):
                status[name] = component.stats
            else:
                status[name] = {'status': 'unknown', 'available': True}
        except Exception as e:
            status[name] = {'status': 'error', 'error': str(e)}
    
    return status

# ============================================================================
# CORE UTILITIES
# ============================================================================

class CoreComponentError(Exception):
    """Базовая ошибка компонентов ядра"""
    pass


class CoreInitializationError(CoreComponentError):
    """Ошибка инициализации компонентов ядра"""
    pass


class CoreConnectionError(CoreComponentError):
    """Ошибка соединения компонентов ядра"""
    pass

# ============================================================================
# BACKWARDS COMPATIBILITY
# ============================================================================

# Для совместимости с существующим кодом
try:
    # Алиасы для основных классов
    if 'BybitLinearClient' in globals():
        BybitClient = BybitLinearClient
        
    if 'WebSocketManager' in globals():
        WSManager = WebSocketManager
        
    if 'DataManager' in globals():
        MarketDataManager = DataManager
        
    if 'SignalProcessor' in globals():
        TradingSignalProcessor = SignalProcessor
        
except:
    pass
