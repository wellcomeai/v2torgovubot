"""
Trading Bot Data Module
Инициализация модуля данных и экспорт основных классов
"""

from .database import Database, get_database, init_database, DatabaseStats, HealthCheckResult
from .models import (
    Base, Signal, StrategyConfig, StrategyRun, BacktestResult, BacktestTrade,
    MarketData, Position, Order, Notification, SystemLog, SystemMetric,
    SignalType, PositionSide, OrderStatus, StrategyStatus, BacktestStatus, NotificationType
)
from .historical_data import (
    HistoricalDataManager, CandleData, HistoricalDataRequest,
    get_historical_candles, get_ohlcv_dataframe
)

# Версия модуля
__version__ = "1.0.0"

# Список экспортируемых классов
__all__ = [
    # Database
    "Database",
    "get_database", 
    "init_database",
    "DatabaseStats",
    "HealthCheckResult",
    
    # Models
    "Base",
    "Signal",
    "StrategyConfig", 
    "StrategyRun",
    "BacktestResult",
    "BacktestTrade",
    "MarketData",
    "Position",
    "Order", 
    "Notification",
    "SystemLog",
    "SystemMetric",
    
    # Enums
    "SignalType",
    "PositionSide",
    "OrderStatus", 
    "StrategyStatus",
    "BacktestStatus",
    "NotificationType",
    
    # Historical Data
    "HistoricalDataManager",
    "CandleData",
    "HistoricalDataRequest",
    "get_historical_candles",
    "get_ohlcv_dataframe",
]
