"""
Trading Bot Main Application
Готов к деплою на Render с полной архитектурой и интеграцией всех сервисов
"""

import asyncio
import signal
import sys
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# Внутренние импорты
from app.config.settings import Settings, get_settings
from core.trading_engine import TradingEngine
from core.bybit_client import BybitLinearClient
from core.data_manager import DataManager
from data.database import Database

# Импорты сервисов - используем удобные функции
from services import (
    create_all_services, 
    shutdown_all_services,
    check_all_services_health,
    OpenAIService,
    TelegramService,
    NotificationService,
    BacktestEngine
)

from strategies.strategy_registry import StrategyRegistry
from data.models import Signal, BacktestResult, Base
from utils.logger import setup_logger


# ============================================================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ============================================================================

settings = get_settings()
logger = setup_logger(__name__)

# Глобальные компоненты приложения
trading_engine: Optional[TradingEngine] = None
database: Optional[Database] = None
services: Dict[str, any] = {}
background_tasks_running = False


# ============================================================================
# PYDANTIC МОДЕЛИ ДЛЯ API (расширены)
# ============================================================================

class StrategyConfig(BaseModel):
    name: str
    symbol: str
    params: Dict
    timeframe: str = "1m"
    active: bool = True

class BacktestRequest(BaseModel):
    strategy_name: str
    symbol: str
    start_date: str
    end_date: str
    params: Dict
    initial_balance: float = 10000.0

class SystemStatus(BaseModel):
    status: str
    uptime: str
    active_strategies: int
    active_symbols: List[str]
    websocket_connected: bool
    last_signal_time: Optional[str]
    services_status: Dict[str, Dict] = {}

class SignalResponse(BaseModel):
    id: int
    symbol: str
    strategy_name: str
    signal_type: str
    confidence: float
    price: float
    timestamp: str
    ai_analysis: Optional[str]

class NotificationRequest(BaseModel):
    type: str = "custom"
    title: str = ""
    message: str
    priority: str = "normal"
    channels: List[str] = ["telegram"]

class AIAnalysisRequest(BaseModel):
    analysis_type: str
    symbol: str
    timeframe: str = "5m"
    additional_context: Dict = {}

class ServiceStats(BaseModel):
    total_services: int
    healthy_services: int
    service_details: Dict[str, Dict]


# ============================================================================
# LIFECYCLE MANAGEMENT
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Управление жизненным циклом приложения
    """
    # STARTUP
    logger.info("🚀 Starting Trading Bot...")
    
    try:
        await startup_sequence()
        logger.info("✅ Trading Bot started successfully")
        yield
    except Exception as e:
        logger.error(f"❌ Failed to start Trading Bot: {e}")
        raise
    finally:
        # SHUTDOWN
        logger.info("🔄 Shutting down Trading Bot...")
        await shutdown_sequence()
        logger.info("✅ Trading Bot shut down gracefully")


async def startup_sequence():
    """
    Последовательность запуска всех компонентов
    """
    global trading_engine, database, background_tasks_running, services
    
    # 1. Инициализация базы данных
    logger.info("📦 Initializing database...")
    database = Database()
    await database.init()
    
    # 2. Проверка API ключей
    logger.info("🔑 Validating API keys...")
    if not all([settings.BYBIT_API_KEY, settings.BYBIT_API_SECRET]):
        raise ValueError("Bybit API keys not configured")
    
    # 3. Создание всех сервисов
    logger.info("🔧 Creating services...")
    services = await create_all_services(
        settings=settings,
        database=database
    )
    
    # Логируем какие сервисы созданы
    available_services = list(services.keys())
    logger.info(f"✅ Created services: {available_services}")
    
    # 4. Инициализация торгового движка
    logger.info("🤖 Initializing Trading Engine...")
    trading_engine = TradingEngine(
        database=database,
        settings=settings
    )
    
    # 5. Передача сервисов в trading engine
    logger.info("🔗 Linking services to Trading Engine...")
    if hasattr(trading_engine, 'set_services'):
        trading_engine.set_services(services)
    else:
        # Fallback - устанавливаем сервисы по отдельности
        if 'notifications' in services:
            trading_engine.notification_service = services['notifications']
        if 'openai' in services:
            trading_engine.openai_service = services['openai']
        if 'telegram' in services:
            trading_engine.telegram_service = services['telegram']
        if 'backtest' in services:
            trading_engine.backtest_engine = services['backtest']
    
    # 6. Подключение к Bybit
    logger.info("🔌 Connecting to Bybit...")
    await trading_engine.connect()
    
    # 7. Загрузка активных стратегий
    logger.info("📊 Loading active strategies...")
    await trading_engine.load_active_strategies()
    
    # 8. Запуск фоновых задач
    logger.info("⚡ Starting background tasks...")
    background_tasks_running = True
    asyncio.create_task(background_market_monitor())
    asyncio.create_task(background_health_monitor())
    asyncio.create_task(background_service_monitor())
    
    # 9. Запуск WebSocket соединений
    logger.info("🌐 Starting WebSocket connections...")
    await trading_engine.start_websockets()
    
    # 10. Отправляем уведомление о запуске
    if 'notifications' in services:
        await services['notifications'].send_system_status(
            status="started",
            components={name: "initialized" for name in services.keys()},
            metrics={'startup_time': datetime.now().isoformat()}
        )


async def shutdown_sequence():
    """
    Последовательность завершения работы
    """
    global trading_engine, database, background_tasks_running, services
    
    background_tasks_running = False
    
    # Отправляем уведомление о завершении работы
    if services.get('notifications'):
        try:
            await services['notifications'].send_system_status(
                status="shutting_down",
                components={name: "stopping" for name in services.keys()},
                metrics={'shutdown_time': datetime.now().isoformat()}
            )
        except:
            pass  # Не критично если не отправится
    
    if trading_engine:
        logger.info("🔌 Disconnecting from Bybit...")
        await trading_engine.disconnect()
    
    # Закрываем все сервисы
    if services:
        logger.info("🔧 Shutting down services...")
        await shutdown_all_services(services)
        services.clear()
    
    if database:
        logger.info("📦 Closing database connections...")
        await database.close()


# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

app = FastAPI(
    title="Advanced Trading Bot",
    description="AI-Powered Multi-Strategy Trading Bot for Bybit USDT Perpetuals",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware для разработки
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# API ENDPOINTS - CORE
# ============================================================================

@app.get("/", response_model=dict)
async def root():
    """Корневой endpoint"""
    return {
        "service": "Advanced Trading Bot",
        "version": "1.0.0",
        "status": "running",
        "services": list(services.keys()),
        "docs": "/docs"
    }


@app.get("/health", response_model=SystemStatus)
async def health_check():
    """Проверка состояния системы"""
    global trading_engine, services
    
    if not trading_engine:
        raise HTTPException(status_code=503, detail="Trading engine not initialized")
    
    # Получаем статус торгового движка
    status = await trading_engine.get_system_status() if hasattr(trading_engine, 'get_system_status') else {}
    
    # Получаем статус сервисов
    services_health = await check_all_services_health(services) if services else {'overall_status': 'unknown', 'services': {}}
    
    return SystemStatus(
        status="healthy" if trading_engine.is_running else "unhealthy",
        uptime=str(datetime.now() - trading_engine.start_time) if hasattr(trading_engine, 'start_time') else "unknown",
        active_strategies=len(getattr(trading_engine, 'active_strategies', [])),
        active_symbols=list(getattr(trading_engine, 'active_symbols', set())),
        websocket_connected=getattr(trading_engine, 'websocket_connected', False),
        last_signal_time=status.get("last_signal_time"),
        services_status=services_health['services']
    )


# ============================================================================
# API ENDPOINTS - SERVICES
# ============================================================================

@app.get("/services/status", response_model=ServiceStats)
async def get_services_status():
    """Получение статуса всех сервисов"""
    if not services:
        raise HTTPException(status_code=503, detail="Services not initialized")
    
    health_report = await check_all_services_health(services)
    
    return ServiceStats(
        total_services=health_report['total_services'],
        healthy_services=health_report['healthy_services'],
        service_details=health_report['services']
    )


@app.get("/services/{service_name}/stats")
async def get_service_stats(service_name: str):
    """Получение статистики конкретного сервиса"""
    if service_name not in services:
        raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found")
    
    service = services[service_name]
    if hasattr(service, 'get_stats'):
        return service.get_stats()
    else:
        return {"message": f"Service '{service_name}' does not provide stats"}


@app.post("/notifications/send")
async def send_notification(request: NotificationRequest):
    """Отправка уведомления"""
    if 'notifications' not in services:
        raise HTTPException(status_code=503, detail="Notification service not available")
    
    notification_service = services['notifications']
    
    success = await notification_service.send_notification({
        'type': request.type,
        'title': request.title,
        'message': request.message,
        'priority': request.priority,
        'channels': request.channels,
        'source': 'api'
    })
    
    return {"success": success, "message": "Notification queued" if success else "Failed to queue notification"}


@app.post("/ai/analyze")
async def ai_analyze(request: AIAnalysisRequest):
    """Запрос AI анализа"""
    if 'openai' not in services:
        raise HTTPException(status_code=503, detail="OpenAI service not available")
    
    openai_service = services['openai']
    
    try:
        # Базовый анализ рыночных данных
        result = await openai_service.analyze_market_data(
            symbol=request.symbol,
            timeframe=request.timeframe,
            market_data=None,  # Можно получить из data_manager
            analysis_type=request.analysis_type,
            additional_context=request.additional_context
        )
        
        if result:
            return result.to_dict()
        else:
            raise HTTPException(status_code=400, detail="Analysis failed")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis error: {str(e)}")


@app.post("/telegram/test")
async def test_telegram():
    """Тестовое сообщение в Telegram"""
    if 'telegram' not in services:
        raise HTTPException(status_code=503, detail="Telegram service not available")
    
    telegram_service = services['telegram']
    
    success = await telegram_service.send_message(
        "🧪 Test message from Trading Bot API",
        priority="normal"
    )
    
    return {"success": success, "message": "Test message sent" if success else "Failed to send test message"}


# ============================================================================
# API ENDPOINTS - EXISTING (Updated)
# ============================================================================

@app.get("/strategies", response_model=List[str])
async def list_available_strategies():
    """Список доступных стратегий"""
    return StrategyRegistry.list_strategies()


@app.post("/strategies/activate")
async def activate_strategy(config: StrategyConfig):
    """Активация стратегии"""
    global trading_engine
    
    if not trading_engine:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    
    try:
        await trading_engine.activate_strategy(
            name=config.name,
            symbol=config.symbol,
            params=config.params,
            timeframe=config.timeframe
        )
        
        # Отправляем уведомление об активации
        if 'notifications' in services:
            await services['notifications'].send_notification({
                'type': 'system_status',
                'title': f'Strategy Activated: {config.name}',
                'message': f'Strategy {config.name} activated for {config.symbol}',
                'priority': 'normal',
                'source': 'strategy_management'
            })
        
        logger.info(f"✅ Strategy {config.name} activated for {config.symbol}")
        return {"message": f"Strategy {config.name} activated successfully"}
        
    except Exception as e:
        logger.error(f"❌ Failed to activate strategy: {e}")
        
        # Отправляем уведомление об ошибке
        if 'notifications' in services:
            await services['notifications'].send_error_alert(
                error_message=str(e),
                component="strategy_management",
                severity="medium"
            )
        
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/strategies/deactivate")
async def deactivate_strategy(strategy_name: str, symbol: str):
    """Деактивация стратегии"""
    global trading_engine
    
    if not trading_engine:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    
    try:
        await trading_engine.deactivate_strategy(strategy_name, symbol)
        
        # Отправляем уведомление о деактивации
        if 'notifications' in services:
            await services['notifications'].send_notification({
                'type': 'system_status',
                'title': f'Strategy Deactivated: {strategy_name}',
                'message': f'Strategy {strategy_name} deactivated for {symbol}',
                'priority': 'normal',
                'source': 'strategy_management'
            })
        
        logger.info(f"⏹️ Strategy {strategy_name} deactivated for {symbol}")
        return {"message": f"Strategy {strategy_name} deactivated successfully"}
        
    except Exception as e:
        logger.error(f"❌ Failed to deactivate strategy: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/signals", response_model=List[SignalResponse])
async def get_recent_signals(limit: int = 50):
    """Получение последних сигналов"""
    global database
    
    if not database:
        raise HTTPException(status_code=503, detail="Database not ready")
    
    signals = await database.get_recent_signals(limit)
    
    return [
        SignalResponse(
            id=signal.id,
            symbol=signal.symbol,
            strategy_name=signal.strategy_name,
            signal_type=signal.signal_type,
            confidence=signal.confidence,
            price=signal.price,
            timestamp=signal.timestamp.isoformat(),
            ai_analysis=signal.ai_analysis
        )
        for signal in signals
    ]


@app.post("/backtest")
async def run_backtest(request: BacktestRequest, background_tasks: BackgroundTasks):
    """Запуск бэктестинга"""
    if 'backtest' not in services:
        raise HTTPException(status_code=503, detail="Backtest service not available")
    
    # Запускаем бэктест в фоне
    background_tasks.add_task(
        execute_backtest_with_notifications,
        request.strategy_name,
        request.symbol,
        request.start_date,
        request.end_date,
        request.params,
        request.initial_balance
    )
    
    return {"message": "Backtest started", "status": "running"}


@app.get("/backtest/results")
async def get_backtest_results(strategy_name: Optional[str] = None, limit: int = 10):
    """Получение результатов бэктестов"""
    global database
    
    if not database:
        raise HTTPException(status_code=503, detail="Database not ready")
    
    results = await database.get_backtest_results(strategy_name, limit)
    return results


@app.post("/manual-signal")
async def send_manual_signal(
    symbol: str,
    signal_type: str,
    message: str = "Manual signal"
):
    """Отправка ручного сигнала (для тестирования)"""
    global trading_engine
    
    if not trading_engine:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    
    await trading_engine.process_manual_signal(
        symbol=symbol,
        signal_type=signal_type,
        message=message
    )
    
    return {"message": "Manual signal processed"}


# ============================================================================
# ФОНОВЫЕ ЗАДАЧИ (Updated)
# ============================================================================

async def background_market_monitor():
    """
    Фоновый мониторинг рыночных данных
    """
    logger.info("📊 Starting market monitor...")
    
    while background_tasks_running:
        try:
            if trading_engine and trading_engine.is_running:
                await trading_engine.process_market_data_batch()
                
            await asyncio.sleep(1)  # Обновление каждую секунду
            
        except Exception as e:
            logger.error(f"❌ Market monitor error: {e}")
            
            # Отправляем уведомление об ошибке
            if services.get('notifications'):
                try:
                    await services['notifications'].send_error_alert(
                        error_message=str(e),
                        component="market_monitor",
                        severity="medium"
                    )
                except:
                    pass  # Не блокируем выполнение
                    
            await asyncio.sleep(5)  # Пауза при ошибке


async def background_health_monitor():
    """
    Фоновый мониторинг здоровья системы
    """
    logger.info("🏥 Starting health monitor...")
    
    while background_tasks_running:
        try:
            if trading_engine:
                # Проверка WebSocket соединений
                if not getattr(trading_engine, 'websocket_connected', True):
                    logger.warning("⚠️ WebSocket disconnected, attempting reconnection...")
                    if hasattr(trading_engine, 'reconnect_websockets'):
                        await trading_engine.reconnect_websockets()
                
                # Проверка активности стратегий
                if hasattr(trading_engine, 'check_strategy_health'):
                    inactive_strategies = await trading_engine.check_strategy_health()
                    if inactive_strategies:
                        logger.warning(f"⚠️ Inactive strategies detected: {inactive_strategies}")
            
            await asyncio.sleep(30)  # Проверка каждые 30 секунд
            
        except Exception as e:
            logger.error(f"❌ Health monitor error: {e}")
            await asyncio.sleep(10)


async def background_service_monitor():
    """
    Фоновый мониторинг сервисов
    """
    logger.info("🔧 Starting service monitor...")
    
    while background_tasks_running:
        try:
            if services:
                health_report = await check_all_services_health(services)
                
                # Проверяем общий статус
                if health_report['overall_status'] == 'error':
                    logger.warning(f"⚠️ Services health issues detected: {health_report['services']}")
                    
                    # Отправляем критическое уведомление
                    if services.get('notifications'):
                        await services['notifications'].send_error_alert(
                            error_message="Multiple services experiencing issues",
                            component="service_monitor",
                            severity="high",
                            additional_info={'health_report': health_report}
                        )
            
            await asyncio.sleep(60)  # Проверка каждую минуту
            
        except Exception as e:
            logger.error(f"❌ Service monitor error: {e}")
            await asyncio.sleep(30)


async def execute_backtest_with_notifications(
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    params: Dict,
    initial_balance: float
):
    """
    Выполнение бэктестинга в фоне с уведомлениями
    """
    try:
        logger.info(f"🧪 Starting backtest: {strategy_name} on {symbol}")
        
        if 'backtest' not in services:
            raise Exception("Backtest service not available")
        
        # Отправляем уведомление о начале
        if services.get('notifications'):
            await services['notifications'].send_notification({
                'type': 'system_status',
                'title': 'Backtest Started',
                'message': f'Started backtest of {strategy_name} on {symbol}',
                'priority': 'low',
                'source': 'backtest_engine'
            })
        
        backtest_engine = services['backtest']
        results = await backtest_engine.run_backtest(
            strategy_name=strategy_name,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            strategy_params=params,
            initial_balance=initial_balance
        )
        
        # Сохраняем результаты
        if database:
            await database.save_backtest_result(results)
        
        # Отправляем уведомление о завершении
        if services.get('notifications'):
            await services['notifications'].send_notification({
                'type': 'custom',
                'title': 'Backtest Completed',
                'message': f'Backtest of {strategy_name} completed\nReturn: {results.get("total_return", 0):.2%}\nTrades: {results.get("total_trades", 0)}',
                'priority': 'normal',
                'source': 'backtest_engine'
            })
        
        logger.info(f"✅ Backtest completed: {strategy_name}")
        
    except Exception as e:
        logger.error(f"❌ Backtest failed: {e}")
        
        # Отправляем уведомление об ошибке
        if services.get('notifications'):
            await services['notifications'].send_error_alert(
                error_message=str(e),
                component="backtest_engine",
                severity="medium",
                additional_info={'strategy': strategy_name, 'symbol': symbol}
            )


# ============================================================================
# GRACEFUL SHUTDOWN (Unchanged)
# ============================================================================

def signal_handler(sig, frame):
    """
    Обработчик сигналов для graceful shutdown
    """
    logger.info(f"📡 Received signal {sig}, initiating graceful shutdown...")
    sys.exit(0)


# Регистрируем обработчики сигналов
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ============================================================================
# MAIN ENTRY POINT (Unchanged)
# ============================================================================

if __name__ == "__main__":
    # Конфигурация для разработки
    if settings.ENVIRONMENT == "development":
        uvicorn.run(
            "app.main:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
            log_level="debug"
        )
    else:
        # Конфигурация для продакшна (Render)
        uvicorn.run(
            "app.main:app",
            host="0.0.0.0",
            port=int(settings.PORT),
            workers=1,  # Одиночный воркер для WebSocket
            log_level="info",
            access_log=True
        )
