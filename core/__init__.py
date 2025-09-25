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
# TRADING ENGINE
# ============================================================================
try:
    from .trading_engine import (
        TradingEngine,
        EngineStatus,
        TradingMode,
        PositionSide,
        OrderStatus,
        Position,
        TradingOrder,
        TradingStats,
        RiskManager,
        PositionManager,
        OrderManager,
        StrategyManager,
        create_trading_engine,
        create_configured_engine
    )
except ImportError:
    pass

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
    
    # Trading Engine
    "TradingEngine",
    "EngineStatus",
    "TradingMode", 
    "PositionSide",
    "OrderStatus",
    "Position",
    "TradingOrder",
    "TradingStats",
    "RiskManager",
    "PositionManager", 
    "OrderManager",
    "StrategyManager",
    "create_trading_engine",
    "create_configured_engine",
    
    # Core constants
    "CORE_CONSTANTS",
    "get_core_constant",
    
    # System management functions
    "create_core_components",
    "shutdown_core_components",
    "start_trading_system",
    "create_and_start_trading_system",
    "get_core_status",
    "get_system_health",
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
    
    # Trading engine settings
    'TRADING_CYCLE_INTERVAL': 60,        # секунды между циклами
    'MAX_POSITIONS': 5,                   # максимум открытых позиций
    'MAX_DAILY_TRADES': 20,               # лимит сделок в день
    'DEFAULT_POSITION_SIZE_PCT': 0.02,    # размер позиции (2% от баланса)
    'MIN_CONFIDENCE_THRESHOLD': 0.7,      # минимальная уверенность сигнала
    
    # Risk management
    'MAX_POSITION_SIZE_USD': 1000,        # максимальный размер позиции
    'MAX_DAILY_LOSS_USD': 100,            # максимальная дневная потеря  
    'MAX_DRAWDOWN_PCT': 0.1,              # максимальная просадка (10%)
    'STOP_LOSS_PCT': 0.02,                # стоп-лосс (2%)
    'TAKE_PROFIT_PCT': 0.04,              # тейк-профит (4%)
    
    # Order management
    'ORDER_EXECUTION_TIMEOUT': 60,        # таймаут исполнения ордера
    'ORDER_RETRY_ATTEMPTS': 3,            # попытки повтора ордера
    'ORDER_CLEANUP_HOURS': 24,            # часы до очистки старых ордеров
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
        
        # Создаем Trading Engine
        if 'create_trading_engine' in globals():
            trading_engine = create_trading_engine(
                settings=settings,
                database=database
            )
            
            # Инжектируем зависимости в trading engine
            if 'bybit_client' in components:
                trading_engine.set_bybit_client(components['bybit_client'])
            if 'data_manager' in components:
                trading_engine.set_data_manager(components['data_manager'])
            if 'signal_processor' in components:
                trading_engine.set_signal_processor(components['signal_processor'])
            if 'websocket_manager' in components:
                trading_engine.set_websocket_manager(components['websocket_manager'])
                
            components['trading_engine'] = trading_engine
        
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
        'trading_engine',      # Первым останавливаем координатор
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


async def start_trading_system(components: dict) -> None:
    """
    Запуск всей торговой системы в правильном порядке
    
    Args:
        components: Словарь с компонентами от create_core_components
    """
    startup_order = [
        'bybit_client',
        'websocket_manager', 
        'data_manager',
        'signal_processor',
        'trading_engine'  # Последним запускаем координатор
    ]
    
    for component_name in startup_order:
        if component_name in components:
            component = components[component_name]
            try:
                if hasattr(component, 'start'):
                    await component.start()
                elif hasattr(component, 'initialize'):
                    await component.initialize()
                elif hasattr(component, '__aenter__'):
                    await component.__aenter__()
                print(f"✅ {component_name} started")
            except Exception as e:
                print(f"❌ Failed to start {component_name}: {e}")
                # При ошибке останавливаем уже запущенные компоненты
                await shutdown_core_components(components)
                raise


async def create_and_start_trading_system(settings=None, database=None):
    """
    Создание и запуск полной торговой системы одной командой
    
    Returns:
        Словарь с запущенными компонентами
    """
    try:
        # Создаем компоненты
        components = await create_core_components(settings, database)
        
        # Запускаем систему
        await start_trading_system(components)
        
        print("🚀 Trading system started successfully!")
        return components
        
    except Exception as e:
        print(f"❌ Failed to start trading system: {e}")
        raise


async def get_system_health(components: dict) -> dict:
    """
    Получение полного health check всей системы
    
    Args:
        components: Словарь с компонентами
        
    Returns:
        Подробный отчет о здоровье системы
    """
    health_report = {
        'overall_status': 'unknown',
        'components': {},
        'trading_engine': {},
        'system_metrics': {}
    }
    
    try:
        healthy_components = 0
        total_components = len(components)
        
        # Проверяем каждый компонент
        for name, component in components.items():
            try:
                if hasattr(component, 'health_check'):
                    component_health = await component.health_check()
                    health_report['components'][name] = component_health
                    
                    # Специальная обработка для trading engine
                    if name == 'trading_engine':
                        health_report['trading_engine'] = component_health
                        # Добавляем торговую статистику
                        if hasattr(component, 'get_trading_stats'):
                            health_report['system_metrics'] = component.get_trading_stats()
                    
                    if component_health.get('status') in ['healthy', 'running']:
                        healthy_components += 1
                else:
                    health_report['components'][name] = {'status': 'no_health_check'}
                    healthy_components += 1  # Считаем что OK если нет проверки
                    
            except Exception as e:
                health_report['components'][name] = {'status': 'error', 'error': str(e)}
        
        # Определяем общий статус
        if healthy_components == total_components:
            health_report['overall_status'] = 'healthy'
        elif healthy_components > total_components / 2:
            health_report['overall_status'] = 'degraded'
        else:
            health_report['overall_status'] = 'unhealthy'
            
        health_report['healthy_components'] = healthy_components
        health_report['total_components'] = total_components
        
        return health_report
        
    except Exception as e:
        health_report['overall_status'] = 'error'
        health_report['error'] = str(e)
        return health_report
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
            elif hasattr(component, 'get_trading_stats') and name == 'trading_engine':
                # Специальная обработка для trading engine
                status[name] = component.get_trading_stats()
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
        
    if 'TradingEngine' in globals():
        Engine = TradingEngine
        TradingBot = TradingEngine  # Популярный алиас
        
    # Алиасы для менеджеров из trading engine
    if 'PositionManager' in globals():
        PortfolioManager = PositionManager
        
    if 'RiskManager' in globals():
        RiskController = RiskManager
        
except:
    pass
