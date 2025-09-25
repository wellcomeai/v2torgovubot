"""
Trading Bot Services Module
Внешние сервисы и интеграции
"""

# ============================================================================
# OPENAI SERVICE
# ============================================================================
try:
    from .openai_service import (
        OpenAIService,
        AIAnalysisRequest,
        AIAnalysisResponse,
        AnalysisType,
        AnalysisConfidence
    )
except ImportError:
    pass

# ============================================================================
# TELEGRAM SERVICE
# ============================================================================
try:
    from .telegram_service import (
        TelegramService,
        TelegramMessage,
        MessageType,
        MessagePriority,
        TelegramError
    )
except ImportError:
    pass

# ============================================================================
# NOTIFICATION SERVICE
# ============================================================================
try:
    from .notification_service import (
        NotificationService,
        Notification,
        NotificationType,
        NotificationChannel,
        NotificationStatus
    )
except ImportError:
    pass

# ============================================================================
# BACKTEST ENGINE
# ============================================================================
try:
    from .backtest_engine import (
        BacktestEngine,
        BacktestError,
        BacktestMetrics,
        BacktestPosition,
        BacktestTrade,
        BacktestOrder,
        BacktestPortfolio,
        OrderType as BacktestOrderType
    )
except ImportError:
    pass

# ============================================================================
# MODULE INFO
# ============================================================================

__version__ = "1.0.0"

# Экспортируемые классы
__all__ = [
    # OpenAI Service
    "OpenAIService",
    "AIAnalysisRequest", 
    "AIAnalysisResponse",
    "AnalysisType",
    "AnalysisConfidence",
    
    # Telegram Service
    "TelegramService",
    "TelegramMessage",
    "MessageType",
    "MessagePriority",
    "TelegramError",
    
    # Notification Service
    "NotificationService",
    "Notification",
    "NotificationType", 
    "NotificationChannel",
    "NotificationStatus",
    
    # Backtest Engine
    "BacktestEngine",
    "BacktestError",
    "BacktestMetrics",
    "BacktestPosition",
    "BacktestTrade",
    "BacktestOrder",
    "BacktestPortfolio",
    "BacktestOrderType",
    
    # Convenience functions
    "create_openai_service",
    "create_telegram_service",
    "create_notification_service",
    "create_backtest_engine",
    "create_all_services"
]

# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

async def create_openai_service(settings=None, database=None):
    """Создание OpenAI сервиса"""
    try:
        if 'OpenAIService' in globals():
            service = OpenAIService(settings=settings, database=database)
            await service._create_session()
            return service
        else:
            raise ImportError("OpenAI service not available")
    except Exception as e:
        print(f"❌ Failed to create OpenAI service: {e}")
        return None

async def create_telegram_service(settings=None, database=None):
    """Создание Telegram сервиса"""
    try:
        if 'TelegramService' in globals():
            service = TelegramService(settings=settings, database=database)
            await service.initialize()
            return service
        else:
            raise ImportError("Telegram service not available")
    except Exception as e:
        print(f"❌ Failed to create Telegram service: {e}")
        return None

async def create_notification_service(settings=None, database=None, **kwargs):
    """Создание сервиса уведомлений"""
    try:
        if 'NotificationService' in globals():
            service = NotificationService(
                settings=settings, 
                database=database,
                telegram_service=kwargs.get('telegram_service'),
                openai_service=kwargs.get('openai_service')
            )
            await service.initialize()
            return service
        else:
            raise ImportError("Notification service not available")
    except Exception as e:
        print(f"❌ Failed to create notification service: {e}")
        return None

async def create_backtest_engine(settings=None, database=None, **kwargs):
    """Создание движка бэктестинга"""
    try:
        if 'BacktestEngine' in globals():
            engine = BacktestEngine(
                settings=settings,
                database=database,
                bybit_client=kwargs.get('bybit_client'),
                data_manager=kwargs.get('data_manager')
            )
            return engine
        else:
            raise ImportError("Backtest engine not available")
    except Exception as e:
        print(f"❌ Failed to create backtest engine: {e}")
        return None

async def create_all_services(settings=None, database=None, **dependencies):
    """Создание всех сервисов"""
    services = {}
    
    # OpenAI Service
    openai_service = await create_openai_service(settings, database)
    if openai_service:
        services['openai'] = openai_service
    
    # Telegram Service
    telegram_service = await create_telegram_service(settings, database)
    if telegram_service:
        services['telegram'] = telegram_service
    
    # Notification Service
    notification_service = await create_notification_service(
        settings=settings,
        database=database,
        telegram_service=telegram_service,
        openai_service=openai_service
    )
    if notification_service:
        services['notifications'] = notification_service
    
    # Backtest Engine
    backtest_engine = await create_backtest_engine(
        settings=settings,
        database=database,
        **dependencies
    )
    if backtest_engine:
        services['backtest'] = backtest_engine
    
    return services

async def shutdown_all_services(services: dict):
    """Корректное завершение работы всех сервисов"""
    shutdown_order = ['notifications', 'telegram', 'openai', 'backtest']
    
    for service_name in shutdown_order:
        if service_name in services:
            service = services[service_name]
            try:
                if hasattr(service, 'close'):
                    await service.close()
                elif hasattr(service, 'cleanup'):
                    await service.cleanup()
                elif hasattr(service, 'shutdown'):
                    await service.shutdown()
                print(f"✅ {service_name} service shut down")
            except Exception as e:
                print(f"❌ Error shutting down {service_name}: {e}")

# ============================================================================
# SERVICE CONSTANTS
# ============================================================================

SERVICE_CONSTANTS = {
    # Timeouts
    'DEFAULT_REQUEST_TIMEOUT': 30,
    'OPENAI_REQUEST_TIMEOUT': 60,
    'TELEGRAM_REQUEST_TIMEOUT': 10,
    
    # Retry settings
    'DEFAULT_MAX_RETRIES': 3,
    'DEFAULT_RETRY_DELAY': 2,
    
    # Rate limits
    'OPENAI_REQUESTS_PER_MINUTE': 50,
    'TELEGRAM_MESSAGES_PER_MINUTE': 20,
    
    # Cache settings
    'DEFAULT_CACHE_TTL': 300,  # 5 minutes
    'ANALYSIS_CACHE_TTL': 3600,  # 1 hour
    
    # Notification settings
    'MAX_MESSAGE_LENGTH': 4000,
    'NOTIFICATION_RETRY_ATTEMPTS': 3,
    'NOTIFICATION_BATCH_SIZE': 10
}

def get_service_constant(name: str, default=None):
    """Получение константы сервиса"""
    return SERVICE_CONSTANTS.get(name, default)

# ============================================================================
# SERVICE HEALTH CHECK
# ============================================================================

async def check_all_services_health(services: dict) -> dict:
    """Проверка здоровья всех сервисов"""
    health_report = {}
    
    for name, service in services.items():
        try:
            if hasattr(service, 'health_check'):
                health = await service.health_check()
                health_report[name] = health
            else:
                health_report[name] = {'status': 'unknown', 'message': 'No health check available'}
        except Exception as e:
            health_report[name] = {'status': 'error', 'error': str(e)}
    
    # Определяем общий статус
    all_statuses = [h.get('status', 'unknown') for h in health_report.values()]
    
    if all(status == 'healthy' for status in all_statuses):
        overall_status = 'healthy'
    elif any(status == 'error' for status in all_statuses):
        overall_status = 'error'
    else:
        overall_status = 'degraded'
    
    return {
        'overall_status': overall_status,
        'services': health_report,
        'total_services': len(services),
        'healthy_services': sum(1 for h in health_report.values() if h.get('status') == 'healthy')
    }

# ============================================================================
# BACKWARDS COMPATIBILITY
# ============================================================================

# Алиасы для совместимости
try:
    if 'OpenAIService' in globals():
        AIService = OpenAIService
        
    if 'TelegramService' in globals():
        TelegramBot = TelegramService
        
    if 'NotificationService' in globals():
        Notifier = NotificationService
        
    if 'BacktestEngine' in globals():
        Backtester = BacktestEngine
        
except:
    pass
