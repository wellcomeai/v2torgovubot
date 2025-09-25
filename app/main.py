"""
Trading Bot Main Application
Готов к деплою на Render с полной архитектурой
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
import sqlite3
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Внутренние импорты (создадим далее)
from app.config.settings import Settings, get_settings
from core.trading_engine import TradingEngine
from core.bybit_client import BybitLinearClient
from core.data_manager import DataManager
from services.openai_service import OpenAIService
from services.telegram_service import TelegramService
from services.backtest_engine import BacktestEngine
from services.notification_service import NotificationService
from strategies.strategy_registry import StrategyRegistry
from data.database import Database, init_database
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
background_tasks_running = False


# ============================================================================
# PYDANTIC МОДЕЛИ ДЛЯ API
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

class SignalResponse(BaseModel):
    id: int
    symbol: str
    strategy_name: str
    signal_type: str
    confidence: float
    price: float
    timestamp: str
    ai_analysis: Optional[str]


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
    global trading_engine, database, background_tasks_running
    
    # 1. Инициализация базы данных
    logger.info("📦 Initializing database...")
    database = Database()
    await database.init()
    
    # 2. Проверка API ключей
    logger.info("🔑 Validating API keys...")
    if not all([settings.BYBIT_API_KEY, settings.BYBIT_API_SECRET]):
        raise ValueError("Bybit API keys not configured")
    
    # 3. Инициализация торгового движка
    logger.info("🤖 Initializing Trading Engine...")
    trading_engine = TradingEngine(
        database=database,
        settings=settings
    )
    
    # 4. Подключение к Bybit
    logger.info("🔌 Connecting to Bybit...")
    await trading_engine.connect()
    
    # 5. Загрузка активных стратегий
    logger.info("📊 Loading active strategies...")
    await trading_engine.load_active_strategies()
    
    # 6. Запуск фоновых задач
    logger.info("⚡ Starting background tasks...")
    background_tasks_running = True
    asyncio.create_task(background_market_monitor())
    asyncio.create_task(background_health_monitor())
    
    # 7. Запуск WebSocket соединений
    logger.info("🌐 Starting WebSocket connections...")
    await trading_engine.start_websockets()


async def shutdown_sequence():
    """
    Последовательность завершения работы
    """
    global trading_engine, database, background_tasks_running
    
    background_tasks_running = False
    
    if trading_engine:
        logger.info("🔌 Disconnecting from Bybit...")
        await trading_engine.disconnect()
    
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
# API ENDPOINTS
# ============================================================================

@app.get("/", response_model=dict)
async def root():
    """Корневой endpoint"""
    return {
        "service": "Advanced Trading Bot",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs"
    }


@app.get("/health", response_model=SystemStatus)
async def health_check():
    """Проверка состояния системы"""
    global trading_engine
    
    if not trading_engine:
        raise HTTPException(status_code=503, detail="Trading engine not initialized")
    
    status = await trading_engine.get_system_status()
    
    return SystemStatus(
        status="healthy" if trading_engine.is_running else "unhealthy",
        uptime=str(datetime.now() - trading_engine.start_time),
        active_strategies=len(trading_engine.active_strategies),
        active_symbols=list(trading_engine.active_symbols),
        websocket_connected=trading_engine.websocket_connected,
        last_signal_time=status.get("last_signal_time")
    )


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
        
        logger.info(f"✅ Strategy {config.name} activated for {config.symbol}")
        return {"message": f"Strategy {config.name} activated successfully"}
        
    except Exception as e:
        logger.error(f"❌ Failed to activate strategy: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/strategies/deactivate")
async def deactivate_strategy(strategy_name: str, symbol: str):
    """Деактивация стратегии"""
    global trading_engine
    
    if not trading_engine:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    
    try:
        await trading_engine.deactivate_strategy(strategy_name, symbol)
        
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
    global trading_engine
    
    if not trading_engine:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    
    # Запускаем бэктест в фоне
    background_tasks.add_task(
        execute_backtest,
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
# ФОНОВЫЕ ЗАДАЧИ
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
                if not trading_engine.websocket_connected:
                    logger.warning("⚠️ WebSocket disconnected, attempting reconnection...")
                    await trading_engine.reconnect_websockets()
                
                # Проверка активности стратегий
                inactive_strategies = await trading_engine.check_strategy_health()
                if inactive_strategies:
                    logger.warning(f"⚠️ Inactive strategies detected: {inactive_strategies}")
            
            await asyncio.sleep(30)  # Проверка каждые 30 секунд
            
        except Exception as e:
            logger.error(f"❌ Health monitor error: {e}")
            await asyncio.sleep(10)


async def execute_backtest(
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    params: Dict,
    initial_balance: float
):
    """
    Выполнение бэктестинга в фоне
    """
    try:
        logger.info(f"🧪 Starting backtest: {strategy_name} on {symbol}")
        
        backtest_engine = BacktestEngine(database)
        results = await backtest_engine.run_backtest(
            strategy_name=strategy_name,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            params=params,
            initial_balance=initial_balance
        )
        
        # Сохраняем результаты
        await database.save_backtest_result(results)
        
        logger.info(f"✅ Backtest completed: {strategy_name}")
        
    except Exception as e:
        logger.error(f"❌ Backtest failed: {e}")


# ============================================================================
# GRACEFUL SHUTDOWN
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
# MAIN ENTRY POINT
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
