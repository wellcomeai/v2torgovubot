"""
Trading Bot Database Models
SQLAlchemy ORM модели для всех сущностей системы
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from enum import Enum as PyEnum
from decimal import Decimal

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, Text, 
    ForeignKey, Index, UniqueConstraint, CheckConstraint,
    Enum, JSON, DECIMAL, BigInteger
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, validates
from sqlalchemy.sql import func
import uuid


# ============================================================================
# BASE CONFIGURATION
# ============================================================================

Base = declarative_base()

def generate_uuid():
    """Генерация UUID для записей"""
    return str(uuid.uuid4())

def utc_now():
    """Текущее время в UTC"""
    return datetime.now(timezone.utc)


# ============================================================================
# ENUMS
# ============================================================================

class SignalType(PyEnum):
    """Типы торговых сигналов"""
    BUY = "BUY"
    SELL = "SELL" 
    HOLD = "HOLD"
    STRONG_BUY = "STRONG_BUY"
    STRONG_SELL = "STRONG_SELL"


class PositionSide(PyEnum):
    """Стороны позиции"""
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class OrderStatus(PyEnum):
    """Статусы ордеров"""
    NEW = "New"
    PARTIALLY_FILLED = "PartiallyFilled" 
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"
    UNTRIGGERED = "Untriggered"


class StrategyStatus(PyEnum):
    """Статусы стратегий"""
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class BacktestStatus(PyEnum):
    """Статусы бэктестов"""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NotificationType(PyEnum):
    """Типы уведомлений"""
    SIGNAL = "SIGNAL"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"
    TRADE = "TRADE"
    SYSTEM = "SYSTEM"


# ============================================================================
# ТОРГОВЫЕ СИГНАЛЫ
# ============================================================================

class Signal(Base):
    """
    Торговые сигналы от стратегий
    """
    __tablename__ = 'signals'
    
    # Primary fields
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, default=generate_uuid, nullable=False)
    
    # Signal data
    symbol = Column(String(20), nullable=False, index=True)
    strategy_name = Column(String(50), nullable=False, index=True)
    signal_type = Column(Enum(SignalType), nullable=False)
    confidence = Column(Float, nullable=False)
    
    # Price data
    entry_price = Column(DECIMAL(20, 8), nullable=False)
    stop_loss = Column(DECIMAL(20, 8), nullable=True)
    take_profit = Column(DECIMAL(20, 8), nullable=True)
    
    # Technical indicators data
    indicators_data = Column(JSON, nullable=True)
    
    # AI Analysis
    ai_analysis = Column(Text, nullable=True)
    ai_confidence = Column(Float, nullable=True)
    ai_processed_at = Column(DateTime(timezone=True), nullable=True)
    
    # Notifications
    telegram_sent = Column(Boolean, default=False, nullable=False)
    telegram_sent_at = Column(DateTime(timezone=True), nullable=True)
    email_sent = Column(Boolean, default=False, nullable=False)
    
    # Metadata
    timeframe = Column(String(10), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    
    # Relationships
    strategy_runs = relationship("StrategyRun", back_populates="signals")
    
    # Constraints and indexes
    __table_args__ = (
        Index('idx_symbol_strategy_time', 'symbol', 'strategy_name', 'created_at'),
        Index('idx_signal_type_confidence', 'signal_type', 'confidence'),
        CheckConstraint('confidence >= 0.0 AND confidence <= 1.0', name='check_confidence_range'),
        CheckConstraint('entry_price > 0', name='check_entry_price_positive'),
    )
    
    def __repr__(self):
        return f"<Signal(id={self.id}, symbol={self.symbol}, type={self.signal_type}, confidence={self.confidence})>"


# ============================================================================
# СТРАТЕГИИ
# ============================================================================

class StrategyConfig(Base):
    """
    Конфигурация активных стратегий
    """
    __tablename__ = 'strategy_configs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, default=generate_uuid, nullable=False)
    
    # Strategy identification
    name = Column(String(50), nullable=False)
    symbol = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    
    # Configuration
    parameters = Column(JSON, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    status = Column(Enum(StrategyStatus), default=StrategyStatus.ACTIVE, nullable=False)
    
    # Risk management
    max_position_size = Column(DECIMAL(20, 8), nullable=True)
    stop_loss_pct = Column(Float, nullable=True)
    take_profit_pct = Column(Float, nullable=True)
    
    # Performance tracking
    total_signals = Column(Integer, default=0, nullable=False)
    profitable_signals = Column(Integer, default=0, nullable=False)
    last_signal_at = Column(DateTime(timezone=True), nullable=True)
    
    # Metadata
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    created_by = Column(String(50), default="system", nullable=False)
    
    # Relationships
    strategy_runs = relationship("StrategyRun", back_populates="config", cascade="all, delete-orphan")
    
    # Constraints
    __table_args__ = (
        UniqueConstraint('name', 'symbol', 'timeframe', name='unique_strategy_symbol_timeframe'),
        Index('idx_strategy_status_enabled', 'status', 'enabled'),
        CheckConstraint('max_position_size > 0', name='check_max_position_positive'),
        CheckConstraint('stop_loss_pct >= 0 AND stop_loss_pct <= 1', name='check_stop_loss_range'),
        CheckConstraint('take_profit_pct >= 0 AND take_profit_pct <= 1', name='check_take_profit_range'),
    )
    
    @validates('parameters')
    def validate_parameters(self, key, parameters):
        """Валидация параметров стратегии"""
        if not isinstance(parameters, dict):
            raise ValueError("Strategy parameters must be a dictionary")
        return parameters
    
    @property
    def win_rate(self) -> float:
        """Процент выигрышных сигналов"""
        if self.total_signals == 0:
            return 0.0
        return (self.profitable_signals / self.total_signals) * 100
    
    def __repr__(self):
        return f"<StrategyConfig(id={self.id}, name={self.name}, symbol={self.symbol}, status={self.status})>"


class StrategyRun(Base):
    """
    История выполнения стратегий
    """
    __tablename__ = 'strategy_runs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, default=generate_uuid, nullable=False)
    
    # Foreign keys
    strategy_config_id = Column(Integer, ForeignKey('strategy_configs.id'), nullable=False)
    
    # Run data
    started_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(Enum(StrategyStatus), default=StrategyStatus.ACTIVE, nullable=False)
    
    # Performance metrics
    signals_generated = Column(Integer, default=0, nullable=False)
    successful_signals = Column(Integer, default=0, nullable=False)
    total_pnl = Column(DECIMAL(20, 8), default=0, nullable=False)
    
    # Error handling
    error_count = Column(Integer, default=0, nullable=False)
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime(timezone=True), nullable=True)
    
    # Relationships
    config = relationship("StrategyConfig", back_populates="strategy_runs")
    signals = relationship("Signal", back_populates="strategy_runs")
    
    def __repr__(self):
        return f"<StrategyRun(id={self.id}, strategy={self.strategy_config_id}, status={self.status})>"


# ============================================================================
# БЭКТЕСТИНГ
# ============================================================================

class BacktestResult(Base):
    """
    Результаты бэктестинга стратегий
    """
    __tablename__ = 'backtest_results'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, default=generate_uuid, nullable=False)
    
    # Backtest configuration
    strategy_name = Column(String(50), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    start_date = Column(DateTime(timezone=True), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=False)
    
    # Parameters
    strategy_parameters = Column(JSON, nullable=False)
    initial_balance = Column(DECIMAL(20, 8), nullable=False)
    commission = Column(Float, default=0.001, nullable=False)
    slippage = Column(Float, default=0.0001, nullable=False)
    
    # Performance metrics
    final_balance = Column(DECIMAL(20, 8), nullable=True)
    total_return = Column(Float, nullable=True)
    annualized_return = Column(Float, nullable=True)
    sharpe_ratio = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    calmar_ratio = Column(Float, nullable=True)
    
    # Trading statistics
    total_trades = Column(Integer, default=0, nullable=False)
    winning_trades = Column(Integer, default=0, nullable=False)
    losing_trades = Column(Integer, default=0, nullable=False)
    win_rate = Column(Float, nullable=True)
    profit_factor = Column(Float, nullable=True)
    
    # Additional metrics
    avg_trade_duration = Column(Float, nullable=True)  # in hours
    max_consecutive_wins = Column(Integer, default=0, nullable=False)
    max_consecutive_losses = Column(Integer, default=0, nullable=False)
    
    # Execution info
    status = Column(Enum(BacktestStatus), default=BacktestStatus.PENDING, nullable=False)
    execution_time = Column(Float, nullable=True)  # seconds
    error_message = Column(Text, nullable=True)
    
    # Trade details (JSON array of trades)
    trades_data = Column(JSON, nullable=True)
    daily_returns = Column(JSON, nullable=True)
    drawdown_periods = Column(JSON, nullable=True)
    
    # Metadata
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(50), default="system", nullable=False)
    
    # Relationships
    backtest_trades = relationship("BacktestTrade", back_populates="backtest", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        Index('idx_backtest_strategy_symbol', 'strategy_name', 'symbol'),
        Index('idx_backtest_date_range', 'start_date', 'end_date'),
        Index('idx_backtest_performance', 'total_return', 'sharpe_ratio'),
        CheckConstraint('initial_balance > 0', name='check_initial_balance_positive'),
        CheckConstraint('commission >= 0 AND commission <= 0.1', name='check_commission_range'),
        CheckConstraint('win_rate >= 0 AND win_rate <= 100', name='check_win_rate_range'),
    )
    
    @property
    def duration_days(self) -> int:
        """Продолжительность бэктеста в днях"""
        return (self.end_date - self.start_date).days
    
    @property
    def is_profitable(self) -> bool:
        """Прибыльный ли бэктест"""
        return self.total_return is not None and self.total_return > 0
    
    def __repr__(self):
        return f"<BacktestResult(id={self.id}, strategy={self.strategy_name}, return={self.total_return})>"


class BacktestTrade(Base):
    """
    Отдельные сделки в бэктесте
    """
    __tablename__ = 'backtest_trades'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    backtest_id = Column(Integer, ForeignKey('backtest_results.id'), nullable=False)
    
    # Trade info
    trade_number = Column(Integer, nullable=False)
    side = Column(Enum(PositionSide), nullable=False)
    
    # Entry
    entry_time = Column(DateTime(timezone=True), nullable=False)
    entry_price = Column(DECIMAL(20, 8), nullable=False)
    entry_reason = Column(String(100), nullable=True)
    
    # Exit
    exit_time = Column(DateTime(timezone=True), nullable=True)
    exit_price = Column(DECIMAL(20, 8), nullable=True)
    exit_reason = Column(String(100), nullable=True)
    
    # Trade metrics
    quantity = Column(DECIMAL(20, 8), nullable=False)
    pnl = Column(DECIMAL(20, 8), nullable=True)
    pnl_pct = Column(Float, nullable=True)
    commission_paid = Column(DECIMAL(20, 8), default=0, nullable=False)
    
    # Duration
    duration_minutes = Column(Integer, nullable=True)
    
    # Relationships
    backtest = relationship("BacktestResult", back_populates="backtest_trades")
    
    # Indexes
    __table_args__ = (
        Index('idx_trade_backtest_number', 'backtest_id', 'trade_number'),
        Index('idx_trade_entry_time', 'entry_time'),
        CheckConstraint('entry_price > 0', name='check_entry_price_positive'),
        CheckConstraint('quantity > 0', name='check_quantity_positive'),
    )
    
    @property
    def is_open(self) -> bool:
        """Открыта ли сделка"""
        return self.exit_time is None
    
    def __repr__(self):
        return f"<BacktestTrade(id={self.id}, backtest={self.backtest_id}, side={self.side}, pnl={self.pnl})>"


# ============================================================================
# РЫНОЧНЫЕ ДАННЫЕ
# ============================================================================

class MarketData(Base):
    """
    Кеш рыночных данных (свечи)
    """
    __tablename__ = 'market_data'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Market info
    symbol = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False, index=True)
    
    # OHLCV data
    open_time = Column(BigInteger, nullable=False)  # Unix timestamp in milliseconds
    close_time = Column(BigInteger, nullable=False)
    open_price = Column(DECIMAL(20, 8), nullable=False)
    high_price = Column(DECIMAL(20, 8), nullable=False)
    low_price = Column(DECIMAL(20, 8), nullable=False)
    close_price = Column(DECIMAL(20, 8), nullable=False)
    volume = Column(DECIMAL(20, 8), nullable=False)
    
    # Additional data
    quote_volume = Column(DECIMAL(20, 8), nullable=True)
    trades_count = Column(Integer, nullable=True)
    
    # Metadata
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    
    # Constraints and indexes
    __table_args__ = (
        UniqueConstraint('symbol', 'timeframe', 'open_time', name='unique_market_data_candle'),
        Index('idx_market_data_time', 'symbol', 'timeframe', 'open_time'),
        CheckConstraint('open_price > 0', name='check_open_price_positive'),
        CheckConstraint('high_price >= open_price', name='check_high_price_valid'),
        CheckConstraint('low_price <= open_price', name='check_low_price_valid'),
        CheckConstraint('close_price > 0', name='check_close_price_positive'),
        CheckConstraint('volume >= 0', name='check_volume_non_negative'),
    )
    
    @property
    def datetime(self) -> datetime:
        """Конвертация timestamp в datetime"""
        return datetime.fromtimestamp(self.open_time / 1000, tz=timezone.utc)
    
    def __repr__(self):
        return f"<MarketData(symbol={self.symbol}, timeframe={self.timeframe}, time={self.datetime})>"


# ============================================================================
# ПОЗИЦИИ И ОРДЕРА
# ============================================================================

class Position(Base):
    """
    Снимки позиций с Bybit
    """
    __tablename__ = 'positions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Position info
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(Enum(PositionSide), nullable=False)
    position_idx = Column(Integer, default=0, nullable=False)  # 0: one-way, 1: long, 2: short
    
    # Size and value
    size = Column(DECIMAL(20, 8), nullable=False)
    position_value = Column(DECIMAL(20, 8), nullable=True)
    entry_price = Column(DECIMAL(20, 8), nullable=True)
    mark_price = Column(DECIMAL(20, 8), nullable=True)
    
    # PnL
    unrealised_pnl = Column(DECIMAL(20, 8), nullable=True)
    cumulative_realised_pnl = Column(DECIMAL(20, 8), nullable=True)
    
    # Risk management
    leverage = Column(Float, nullable=True)
    stop_loss = Column(DECIMAL(20, 8), nullable=True)
    take_profit = Column(DECIMAL(20, 8), nullable=True)
    
    # Timestamps
    created_time = Column(BigInteger, nullable=True)
    updated_time = Column(BigInteger, nullable=True)
    snapshot_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    
    # Indexes
    __table_args__ = (
        Index('idx_position_symbol_time', 'symbol', 'snapshot_at'),
        CheckConstraint('size >= 0', name='check_position_size_non_negative'),
        CheckConstraint('leverage > 0', name='check_leverage_positive'),
    )
    
    @property
    def is_long(self) -> bool:
        return self.side == PositionSide.LONG
    
    @property
    def is_short(self) -> bool:
        return self.side == PositionSide.SHORT
    
    def __repr__(self):
        return f"<Position(symbol={self.symbol}, side={self.side}, size={self.size})>"


class Order(Base):
    """
    История ордеров
    """
    __tablename__ = 'orders'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Bybit order info
    order_id = Column(String(50), unique=True, nullable=False, index=True)
    order_link_id = Column(String(50), nullable=True, index=True)
    
    # Order details
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # Buy/Sell
    order_type = Column(String(20), nullable=False)  # Market/Limit
    qty = Column(DECIMAL(20, 8), nullable=False)
    price = Column(DECIMAL(20, 8), nullable=True)
    
    # Status
    order_status = Column(Enum(OrderStatus), nullable=False, index=True)
    
    # Execution
    cumulative_exec_qty = Column(DECIMAL(20, 8), default=0, nullable=False)
    cumulative_exec_value = Column(DECIMAL(20, 8), default=0, nullable=False)
    avg_price = Column(DECIMAL(20, 8), nullable=True)
    
    # Timestamps
    created_time = Column(BigInteger, nullable=True)
    updated_time = Column(BigInteger, nullable=True)
    snapshot_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    
    # Strategy attribution
    strategy_name = Column(String(50), nullable=True, index=True)
    signal_id = Column(Integer, ForeignKey('signals.id'), nullable=True)
    
    # Indexes
    __table_args__ = (
        Index('idx_order_symbol_status', 'symbol', 'order_status'),
        Index('idx_order_strategy', 'strategy_name', 'created_time'),
        CheckConstraint('qty > 0', name='check_order_qty_positive'),
        CheckConstraint('cumulative_exec_qty >= 0', name='check_exec_qty_non_negative'),
    )
    
    def __repr__(self):
        return f"<Order(id={self.order_id}, symbol={self.symbol}, status={self.order_status})>"


# ============================================================================
# УВЕДОМЛЕНИЯ
# ============================================================================

class Notification(Base):
    """
    История уведомлений
    """
    __tablename__ = 'notifications'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, default=generate_uuid, nullable=False)
    
    # Notification data
    type = Column(Enum(NotificationType), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    
    # Targeting
    recipient = Column(String(100), nullable=True)  # Telegram chat_id, email, etc.
    
    # Status
    sent = Column(Boolean, default=False, nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    delivery_attempts = Column(Integer, default=0, nullable=False)
    last_error = Column(Text, nullable=True)
    
    # Associated data
    signal_id = Column(Integer, ForeignKey('signals.id'), nullable=True)
    strategy_name = Column(String(50), nullable=True)
    symbol = Column(String(20), nullable=True)
    
    # Priority
    priority = Column(Integer, default=1, nullable=False)  # 1=low, 5=high
    
    # Metadata
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    
    # Indexes
    __table_args__ = (
        Index('idx_notification_type_sent', 'type', 'sent'),
        Index('idx_notification_priority_created', 'priority', 'created_at'),
        CheckConstraint('priority >= 1 AND priority <= 5', name='check_priority_range'),
        CheckConstraint('delivery_attempts >= 0', name='check_attempts_non_negative'),
    )
    
    def __repr__(self):
        return f"<Notification(id={self.id}, type={self.type}, sent={self.sent})>"


# ============================================================================
# СИСТЕМА И ЛОГИ
# ============================================================================

class SystemLog(Base):
    """
    Системные логи и метрики
    """
    __tablename__ = 'system_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Log data
    level = Column(String(20), nullable=False, index=True)  # INFO, WARNING, ERROR, etc.
    message = Column(Text, nullable=False)
    logger_name = Column(String(100), nullable=True)
    
    # Context
    component = Column(String(50), nullable=True, index=True)  # trading_engine, websocket_manager, etc.
    function_name = Column(String(100), nullable=True)
    
    # Additional data
    extra_data = Column(JSON, nullable=True)
    exception_trace = Column(Text, nullable=True)
    
    # Timestamp
    timestamp = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    
    # Indexes
    __table_args__ = (
        Index('idx_log_level_timestamp', 'level', 'timestamp'),
        Index('idx_log_component_timestamp', 'component', 'timestamp'),
    )
    
    def __repr__(self):
        return f"<SystemLog(id={self.id}, level={self.level}, component={self.component})>"


class SystemMetric(Base):
    """
    Метрики производительности системы
    """
    __tablename__ = 'system_metrics'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Metric info
    metric_name = Column(String(100), nullable=False, index=True)
    metric_value = Column(Float, nullable=False)
    metric_unit = Column(String(20), nullable=True)
    
    # Context
    component = Column(String(50), nullable=True, index=True)
    tags = Column(JSON, nullable=True)  # Additional tags as key-value pairs
    
    # Timestamp
    timestamp = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    
    # Indexes
    __table_args__ = (
        Index('idx_metric_name_timestamp', 'metric_name', 'timestamp'),
        Index('idx_metric_component_timestamp', 'component', 'timestamp'),
    )
    
    def __repr__(self):
        return f"<SystemMetric(name={self.metric_name}, value={self.metric_value}, timestamp={self.timestamp})>"


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def create_all_tables(engine):
    """
    Создание всех таблиц в базе данных
    """
    Base.metadata.create_all(bind=engine)


def drop_all_tables(engine):
    """
    Удаление всех таблиц (осторожно!)
    """
    Base.metadata.drop_all(bind=engine)


def get_table_names():
    """
    Получение списка всех таблиц
    """
    return [table.name for table in Base.metadata.tables.values()]


def get_model_by_tablename(tablename: str):
    """
    Получение модели по имени таблицы
    """
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if hasattr(model, '__tablename__') and model.__tablename__ == tablename:
            return model
    return None


# ============================================================================
# SUMMARY INFO
# ============================================================================

"""
📊 SUMMARY: Database Models Created

✅ Core Trading Models:
- Signal: Торговые сигналы от стратегий
- StrategyConfig: Конфигурация активных стратегий
- StrategyRun: История выполнения стратегий

✅ Backtesting Models:
- BacktestResult: Результаты бэктестинга
- BacktestTrade: Отдельные сделки в бэктесте

✅ Market Data Models:
- MarketData: Кеш рыночных данных (свечи)
- Position: Снимки позиций с Bybit
- Order: История ордеров

✅ System Models:
- Notification: История уведомлений
- SystemLog: Системные логи
- SystemMetric: Метрики производительности

🔧 Features:
- Полная типизация с Enums
- JSON поля для гибких данных
- Proper indexes для производительности  
- Constraints для data integrity
- Relationships между моделями
- UUID поля для уникальности
- Timezone-aware timestamps
- Validation методы
- Helper properties and methods

🚀 Ready for production deployment!
"""
