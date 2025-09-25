"""
Trading Bot Database Connection and Operations
Управление подключением к SQLite базе данных с async поддержкой
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
from contextlib import asynccontextmanager
import json
import logging
from dataclasses import dataclass

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
import aiosqlite

# Внутренние импорты
from data.models import (
    Base, Signal, StrategyConfig, StrategyRun, BacktestResult, BacktestTrade,
    MarketData, Position, Order, Notification, SystemLog, SystemMetric,
    SignalType, StrategyStatus, BacktestStatus, NotificationType
)
from app.config.settings import Settings, get_settings
from utils.logger import setup_logger


# ============================================================================
# КОНСТАНТЫ
# ============================================================================

DEFAULT_DATABASE_URL = "sqlite:///trading_bot.db"
DEFAULT_ASYNC_DATABASE_URL = "sqlite+aiosqlite:///trading_bot.db"

# SQLite pragma настройки для оптимизации
SQLITE_PRAGMA_SETTINGS = [
    "PRAGMA journal_mode=WAL",        # Write-Ahead Logging для лучшей производительности
    "PRAGMA synchronous=NORMAL",      # Баланс между скоростью и надежностью
    "PRAGMA cache_size=10000",        # Размер кеша страниц
    "PRAGMA temp_store=memory",       # Временные таблицы в памяти
    "PRAGMA mmap_size=134217728",     # Memory-mapped I/O (128MB)
    "PRAGMA optimize",                # Автооптимизация
]


# ============================================================================
# DATACLASSES ДЛЯ РЕЗУЛЬТАТОВ
# ============================================================================

@dataclass
class DatabaseStats:
    """Статистика базы данных"""
    total_signals: int
    total_strategies: int
    total_backtests: int
    total_notifications: int
    database_size_mb: float
    last_signal_time: Optional[datetime]
    active_strategies: int
    

@dataclass
class HealthCheckResult:
    """Результат проверки здоровья БД"""
    is_healthy: bool
    connection_ok: bool
    tables_exist: bool
    recent_activity: bool
    error_message: Optional[str] = None


# ============================================================================
# ОСНОВНОЙ КЛАСС БД
# ============================================================================

class Database:
    """
    Главный класс для работы с базой данных
    Поддерживает как sync, так и async операции
    """
    
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.logger = setup_logger(f"{__name__}.Database")
        
        # Database URLs
        self.database_url = self.settings.DATABASE_URL
        self.async_database_url = self.settings.database_async_url
        
        # Engines
        self.engine = None
        self.async_engine = None
        
        # Session makers
        self.session_factory = None
        self.async_session_factory = None
        
        # Connection status
        self._initialized = False
        self._connected = False
        
        # Stats cache
        self._stats_cache = None
        self._stats_cache_time = None
        self._cache_ttl = timedelta(minutes=5)
    
    async def init(self) -> None:
        """
        Инициализация базы данных
        """
        if self._initialized:
            return
            
        try:
            self.logger.info("🗄️ Initializing database connection...")
            
            # Создаем папку для БД если нужно
            db_path = Path(self.database_url.replace("sqlite:///", ""))
            db_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Создаем sync engine
            self.engine = create_engine(
                self.database_url,
                echo=self.settings.DATABASE_ECHO,
                poolclass=StaticPool,
                connect_args={
                    "check_same_thread": False,
                    "timeout": self.settings.DATABASE_POOL_TIMEOUT,
                },
                pool_pre_ping=True,
                pool_recycle=self.settings.DATABASE_POOL_RECYCLE,
            )
            
            # Создаем async engine
            self.async_engine = create_async_engine(
                self.async_database_url,
                echo=self.settings.DATABASE_ECHO,
                poolclass=StaticPool,
                connect_args={
                    "check_same_thread": False,
                },
                pool_pre_ping=True,
            )
            
            # Создаем session factories
            self.session_factory = sessionmaker(
                bind=self.engine,
                class_=Session,
                expire_on_commit=False
            )
            
            self.async_session_factory = async_sessionmaker(
                bind=self.async_engine,
                class_=AsyncSession,
                expire_on_commit=False
            )
            
            # Создаем таблицы если их нет
            await self._create_tables()
            
            # Оптимизируем SQLite
            await self._optimize_sqlite()
            
            # Проверяем подключение
            await self._check_connection()
            
            self._initialized = True
            self._connected = True
            
            self.logger.info("✅ Database initialized successfully")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize database: {e}")
            raise
    
    async def close(self) -> None:
        """
        Закрытие подключения к БД
        """
        self.logger.info("🔒 Closing database connections...")
        
        if self.async_engine:
            await self.async_engine.dispose()
        
        if self.engine:
            self.engine.dispose()
        
        self._connected = False
        self.logger.info("✅ Database connections closed")
    
    @asynccontextmanager
    async def get_session(self):
        """
        Async context manager для получения сессии БД
        """
        if not self._initialized:
            await self.init()
            
        async with self.async_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception as e:
                await session.rollback()
                self.logger.error(f"❌ Database session error: {e}")
                raise
            finally:
                await session.close()
    
    def get_sync_session(self) -> Session:
        """
        Получение синхронной сессии БД
        """
        if not self._initialized:
            raise RuntimeError("Database not initialized. Call init() first.")
        
        return self.session_factory()
    
    # ============================================================================
    # ПРИВАТНЫЕ МЕТОДЫ ИНИЦИАЛИЗАЦИИ
    # ============================================================================
    
    async def _create_tables(self) -> None:
        """Создание всех таблиц"""
        try:
            # Используем sync engine для создания таблиц
            Base.metadata.create_all(bind=self.engine)
            
            # Проверяем что таблицы созданы
            inspector = inspect(self.engine)
            table_names = inspector.get_table_names()
            
            self.logger.info(f"📊 Database tables: {', '.join(table_names)}")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to create tables: {e}")
            raise
    
    async def _optimize_sqlite(self) -> None:
        """Оптимизация SQLite настроек"""
        try:
            async with aiosqlite.connect(
                self.database_url.replace("sqlite:///", "")
            ) as conn:
                for pragma in SQLITE_PRAGMA_SETTINGS:
                    await conn.execute(pragma)
                await conn.commit()
            
            self.logger.debug("🔧 SQLite optimizations applied")
            
        except Exception as e:
            self.logger.warning(f"⚠️ Failed to apply SQLite optimizations: {e}")
    
    async def _check_connection(self) -> None:
        """Проверка подключения к БД"""
        try:
            async with self.get_session() as session:
                result = await session.execute(text("SELECT 1"))
                result.fetchone()
            
            self.logger.debug("✅ Database connection verified")
            
        except Exception as e:
            self.logger.error(f"❌ Database connection check failed: {e}")
            raise
    
    # ============================================================================
    # СИГНАЛЫ
    # ============================================================================
    
    async def save_signal(self, signal_data: Dict[str, Any]) -> int:
        """
        Сохранение торгового сигнала
        """
        try:
            async with self.get_session() as session:
                signal = Signal(
                    symbol=signal_data['symbol'],
                    strategy_name=signal_data['strategy_name'],
                    signal_type=SignalType(signal_data['signal_type']),
                    confidence=signal_data['confidence'],
                    entry_price=signal_data['entry_price'],
                    stop_loss=signal_data.get('stop_loss'),
                    take_profit=signal_data.get('take_profit'),
                    indicators_data=signal_data.get('indicators_data'),
                    timeframe=signal_data['timeframe'],
                    ai_analysis=signal_data.get('ai_analysis'),
                    ai_confidence=signal_data.get('ai_confidence')
                )
                
                session.add(signal)
                await session.flush()
                signal_id = signal.id
                
                self.logger.debug(
                    f"💾 Signal saved: {signal.symbol} {signal.signal_type} "
                    f"(confidence: {signal.confidence}, id: {signal_id})"
                )
                
                return signal_id
                
        except IntegrityError as e:
            self.logger.error(f"❌ Signal integrity error: {e}")
            raise
        except Exception as e:
            self.logger.error(f"❌ Failed to save signal: {e}")
            raise
    
    async def get_recent_signals(self, limit: int = 50, symbol: Optional[str] = None) -> List[Signal]:
        """
        Получение последних сигналов
        """
        try:
            async with self.get_session() as session:
                query = session.query(Signal).order_by(Signal.created_at.desc())
                
                if symbol:
                    query = query.filter(Signal.symbol == symbol)
                
                result = await session.execute(query.limit(limit))
                signals = result.scalars().all()
                
                self.logger.debug(f"📊 Retrieved {len(signals)} recent signals")
                return list(signals)
                
        except Exception as e:
            self.logger.error(f"❌ Failed to get recent signals: {e}")
            return []
    
    async def update_signal_ai_analysis(self, signal_id: int, ai_analysis: str, ai_confidence: float) -> None:
        """
        Обновление AI анализа сигнала
        """
        try:
            async with self.get_session() as session:
                signal = await session.get(Signal, signal_id)
                if signal:
                    signal.ai_analysis = ai_analysis
                    signal.ai_confidence = ai_confidence
                    signal.ai_processed_at = datetime.utcnow()
                    
                    self.logger.debug(f"🧠 Updated AI analysis for signal {signal_id}")
                else:
                    self.logger.warning(f"⚠️ Signal {signal_id} not found for AI update")
                    
        except Exception as e:
            self.logger.error(f"❌ Failed to update signal AI analysis: {e}")
    
    # ============================================================================
    # СТРАТЕГИИ
    # ============================================================================
    
    async def save_strategy_config(self, config_data: Dict[str, Any]) -> int:
        """
        Сохранение конфигурации стратегии
        """
        try:
            async with self.get_session() as session:
                config = StrategyConfig(
                    name=config_data['name'],
                    symbol=config_data['symbol'],
                    timeframe=config_data['timeframe'],
                    parameters=config_data['parameters'],
                    enabled=config_data.get('enabled', True),
                    max_position_size=config_data.get('max_position_size'),
                    stop_loss_pct=config_data.get('stop_loss_pct'),
                    take_profit_pct=config_data.get('take_profit_pct')
                )
                
                session.add(config)
                await session.flush()
                config_id = config.id
                
                self.logger.info(
                    f"💾 Strategy config saved: {config.name} for {config.symbol} "
                    f"(id: {config_id})"
                )
                
                return config_id
                
        except IntegrityError as e:
            self.logger.error(f"❌ Strategy config already exists: {e}")
            raise
        except Exception as e:
            self.logger.error(f"❌ Failed to save strategy config: {e}")
            raise
    
    async def get_active_strategies(self) -> List[StrategyConfig]:
        """
        Получение активных стратегий
        """
        try:
            async with self.get_session() as session:
                result = await session.execute(
                    session.query(StrategyConfig).filter(
                        StrategyConfig.enabled == True,
                        StrategyConfig.status == StrategyStatus.ACTIVE
                    )
                )
                strategies = result.scalars().all()
                
                self.logger.debug(f"📊 Retrieved {len(strategies)} active strategies")
                return list(strategies)
                
        except Exception as e:
            self.logger.error(f"❌ Failed to get active strategies: {e}")
            return []
    
    async def update_strategy_status(self, strategy_id: int, status: StrategyStatus) -> None:
        """
        Обновление статуса стратегии
        """
        try:
            async with self.get_session() as session:
                strategy = await session.get(StrategyConfig, strategy_id)
                if strategy:
                    strategy.status = status
                    self.logger.debug(f"📊 Strategy {strategy_id} status updated to {status}")
                else:
                    self.logger.warning(f"⚠️ Strategy {strategy_id} not found")
                    
        except Exception as e:
            self.logger.error(f"❌ Failed to update strategy status: {e}")
    
    # ============================================================================
    # БЭКТЕСТИНГ
    # ============================================================================
    
    async def save_backtest_result(self, result_data: Dict[str, Any]) -> int:
        """
        Сохранение результата бэктеста
        """
        try:
            async with self.get_session() as session:
                result = BacktestResult(
                    strategy_name=result_data['strategy_name'],
                    symbol=result_data['symbol'],
                    timeframe=result_data['timeframe'],
                    start_date=result_data['start_date'],
                    end_date=result_data['end_date'],
                    strategy_parameters=result_data['strategy_parameters'],
                    initial_balance=result_data['initial_balance'],
                    commission=result_data.get('commission', 0.001),
                    slippage=result_data.get('slippage', 0.0001),
                    final_balance=result_data.get('final_balance'),
                    total_return=result_data.get('total_return'),
                    sharpe_ratio=result_data.get('sharpe_ratio'),
                    max_drawdown=result_data.get('max_drawdown'),
                    total_trades=result_data.get('total_trades', 0),
                    winning_trades=result_data.get('winning_trades', 0),
                    win_rate=result_data.get('win_rate'),
                    status=BacktestStatus(result_data.get('status', 'COMPLETED')),
                    execution_time=result_data.get('execution_time'),
                    trades_data=result_data.get('trades_data')
                )
                
                session.add(result)
                await session.flush()
                result_id = result.id
                
                self.logger.info(
                    f"💾 Backtest result saved: {result.strategy_name} on {result.symbol} "
                    f"(return: {result.total_return}, id: {result_id})"
                )
                
                return result_id
                
        except Exception as e:
            self.logger.error(f"❌ Failed to save backtest result: {e}")
            raise
    
    async def get_backtest_results(
        self, 
        strategy_name: Optional[str] = None,
        limit: int = 10
    ) -> List[BacktestResult]:
        """
        Получение результатов бэктестов
        """
        try:
            async with self.get_session() as session:
                query = session.query(BacktestResult).order_by(BacktestResult.created_at.desc())
                
                if strategy_name:
                    query = query.filter(BacktestResult.strategy_name == strategy_name)
                
                result = await session.execute(query.limit(limit))
                results = result.scalars().all()
                
                self.logger.debug(f"📊 Retrieved {len(results)} backtest results")
                return list(results)
                
        except Exception as e:
            self.logger.error(f"❌ Failed to get backtest results: {e}")
            return []
    
    # ============================================================================
    # УВЕДОМЛЕНИЯ
    # ============================================================================
    
    async def save_notification(self, notification_data: Dict[str, Any]) -> int:
        """
        Сохранение уведомления
        """
        try:
            async with self.get_session() as session:
                notification = Notification(
                    type=NotificationType(notification_data['type']),
                    title=notification_data['title'],
                    message=notification_data['message'],
                    recipient=notification_data.get('recipient'),
                    signal_id=notification_data.get('signal_id'),
                    strategy_name=notification_data.get('strategy_name'),
                    symbol=notification_data.get('symbol'),
                    priority=notification_data.get('priority', 1)
                )
                
                session.add(notification)
                await session.flush()
                notification_id = notification.id
                
                self.logger.debug(f"💾 Notification saved (id: {notification_id})")
                return notification_id
                
        except Exception as e:
            self.logger.error(f"❌ Failed to save notification: {e}")
            raise
    
    async def mark_notification_sent(self, notification_id: int) -> None:
        """
        Отметка уведомления как отправленного
        """
        try:
            async with self.get_session() as session:
                notification = await session.get(Notification, notification_id)
                if notification:
                    notification.sent = True
                    notification.sent_at = datetime.utcnow()
                    self.logger.debug(f"✅ Notification {notification_id} marked as sent")
                    
        except Exception as e:
            self.logger.error(f"❌ Failed to mark notification as sent: {e}")
    
    # ============================================================================
    # РЫНОЧНЫЕ ДАННЫЕ
    # ============================================================================
    
    async def save_market_data(self, market_data_list: List[Dict[str, Any]]) -> None:
        """
        Сохранение рыночных данных (bulk insert)
        """
        try:
            async with self.get_session() as session:
                market_data_objects = []
                
                for data in market_data_list:
                    market_data = MarketData(
                        symbol=data['symbol'],
                        timeframe=data['timeframe'],
                        open_time=data['open_time'],
                        close_time=data['close_time'],
                        open_price=data['open_price'],
                        high_price=data['high_price'],
                        low_price=data['low_price'],
                        close_price=data['close_price'],
                        volume=data['volume'],
                        quote_volume=data.get('quote_volume'),
                        trades_count=data.get('trades_count')
                    )
                    market_data_objects.append(market_data)
                
                session.add_all(market_data_objects)
                
                self.logger.debug(f"💾 Saved {len(market_data_objects)} market data records")
                
        except Exception as e:
            self.logger.error(f"❌ Failed to save market data: {e}")
    
    async def get_historical_data(
        self, 
        symbol: str, 
        timeframe: str, 
        limit: int = 1000
    ) -> List[MarketData]:
        """
        Получение исторических данных
        """
        try:
            async with self.get_session() as session:
                result = await session.execute(
                    session.query(MarketData)
                    .filter(
                        MarketData.symbol == symbol,
                        MarketData.timeframe == timeframe
                    )
                    .order_by(MarketData.open_time.desc())
                    .limit(limit)
                )
                data = result.scalars().all()
                
                self.logger.debug(f"📊 Retrieved {len(data)} historical data points for {symbol}")
                return list(reversed(data))  # Возвращаем в хронологическом порядке
                
        except Exception as e:
            self.logger.error(f"❌ Failed to get historical data: {e}")
            return []
    
    # ============================================================================
    # СТАТИСТИКА И МОНИТОРИНГ
    # ============================================================================
    
    async def get_database_stats(self, force_refresh: bool = False) -> DatabaseStats:
        """
        Получение статистики базы данных с кешированием
        """
        # Проверяем кеш
        if not force_refresh and self._stats_cache and self._stats_cache_time:
            if datetime.now() - self._stats_cache_time < self._cache_ttl:
                return self._stats_cache
        
        try:
            async with self.get_session() as session:
                # Собираем статистику
                total_signals = await session.scalar(
                    text("SELECT COUNT(*) FROM signals")
                )
                total_strategies = await session.scalar(
                    text("SELECT COUNT(*) FROM strategy_configs")
                )
                total_backtests = await session.scalar(
                    text("SELECT COUNT(*) FROM backtest_results")
                )
                total_notifications = await session.scalar(
                    text("SELECT COUNT(*) FROM notifications")
                )
                active_strategies = await session.scalar(
                    text("SELECT COUNT(*) FROM strategy_configs WHERE enabled = 1 AND status = 'ACTIVE'")
                )
                
                # Последний сигнал
                last_signal_result = await session.execute(
                    text("SELECT MAX(created_at) FROM signals")
                )
                last_signal_time = last_signal_result.scalar()
                
                # Размер БД
                db_path = self.database_url.replace("sqlite:///", "")
                database_size_mb = 0.0
                try:
                    db_file = Path(db_path)
                    if db_file.exists():
                        database_size_mb = db_file.stat().st_size / 1024 / 1024
                except:
                    pass
                
                stats = DatabaseStats(
                    total_signals=total_signals or 0,
                    total_strategies=total_strategies or 0,
                    total_backtests=total_backtests or 0,
                    total_notifications=total_notifications or 0,
                    database_size_mb=round(database_size_mb, 2),
                    last_signal_time=last_signal_time,
                    active_strategies=active_strategies or 0
                )
                
                # Кешируем результат
                self._stats_cache = stats
                self._stats_cache_time = datetime.now()
                
                self.logger.debug("📊 Database stats retrieved and cached")
                return stats
                
        except Exception as e:
            self.logger.error(f"❌ Failed to get database stats: {e}")
            # Возвращаем пустую статистику при ошибке
            return DatabaseStats(0, 0, 0, 0, 0.0, None, 0)
    
    async def health_check(self) -> HealthCheckResult:
        """
        Проверка здоровья базы данных
        """
        try:
            # Проверка подключения
            connection_ok = False
            try:
                async with self.get_session() as session:
                    await session.execute(text("SELECT 1"))
                connection_ok = True
            except Exception as e:
                self.logger.error(f"❌ Database connection failed: {e}")
            
            # Проверка существования таблиц
            tables_exist = False
            try:
                inspector = inspect(self.engine)
                table_names = inspector.get_table_names()
                required_tables = ['signals', 'strategy_configs', 'backtest_results']
                tables_exist = all(table in table_names for table in required_tables)
            except Exception as e:
                self.logger.error(f"❌ Table check failed: {e}")
            
            # Проверка недавней активности
            recent_activity = False
            try:
                async with self.get_session() as session:
                    recent_signals = await session.scalar(
                        text("SELECT COUNT(*) FROM signals WHERE created_at > datetime('now', '-1 hour')")
                    )
                    recent_activity = (recent_signals or 0) > 0
            except:
                pass
            
            is_healthy = connection_ok and tables_exist
            
            result = HealthCheckResult(
                is_healthy=is_healthy,
                connection_ok=connection_ok,
                tables_exist=tables_exist,
                recent_activity=recent_activity
            )
            
            if is_healthy:
                self.logger.debug("✅ Database health check passed")
            else:
                self.logger.warning("⚠️ Database health check failed")
            
            return result
            
        except Exception as e:
            self.logger.error(f"❌ Health check error: {e}")
            return HealthCheckResult(
                is_healthy=False,
                connection_ok=False,
                tables_exist=False,
                recent_activity=False,
                error_message=str(e)
            )
    
    async def cleanup_old_data(self, days_to_keep: int = 30) -> Dict[str, int]:
        """
        Очистка старых данных
        """
        try:
            cleanup_stats = {
                'signals_deleted': 0,
                'notifications_deleted': 0,
                'system_logs_deleted': 0,
                'market_data_deleted': 0
            }
            
            cutoff_date = datetime.now() - timedelta(days=days_to_keep)
            
            async with self.get_session() as session:
                # Удаляем старые уведомления
                notifications_result = await session.execute(
                    text("DELETE FROM notifications WHERE created_at < :cutoff_date AND sent = 1"),
                    {"cutoff_date": cutoff_date}
                )
                cleanup_stats['notifications_deleted'] = notifications_result.rowcount
                
                # Удаляем старые системные логи
                logs_result = await session.execute(
                    text("DELETE FROM system_logs WHERE timestamp < :cutoff_date"),
                    {"cutoff_date": cutoff_date}
                )
                cleanup_stats['system_logs_deleted'] = logs_result.rowcount
                
                # Удаляем старые рыночные данные (кроме дневных свечей)
                market_data_result = await session.execute(
                    text("DELETE FROM market_data WHERE created_at < :cutoff_date AND timeframe NOT IN ('1d', '1w')"),
                    {"cutoff_date": cutoff_date}
                )
                cleanup_stats['market_data_deleted'] = market_data_result.rowcount
                
                # Опционально: очистка очень старых сигналов (оставляем больше времени)
                old_cutoff = datetime.now() - timedelta(days=days_to_keep * 3)
                signals_result = await session.execute(
                    text("DELETE FROM signals WHERE created_at < :old_cutoff"),
                    {"old_cutoff": old_cutoff}
                )
                cleanup_stats['signals_deleted'] = signals_result.rowcount
            
            # Оптимизируем БД после очистки
            async with aiosqlite.connect(
                self.database_url.replace("sqlite:///", "")
            ) as conn:
                await conn.execute("VACUUM")
                await conn.execute("PRAGMA optimize")
            
            total_deleted = sum(cleanup_stats.values())
            self.logger.info(f"🧹 Database cleanup completed: {total_deleted} records deleted")
            
            return cleanup_stats
            
        except Exception as e:
            self.logger.error(f"❌ Database cleanup failed: {e}")
            return {'error': str(e)}
    
    # ============================================================================
    # UTILITY МЕТОДЫ
    # ============================================================================
    
    @property
    def is_connected(self) -> bool:
        """Проверка состояния подключения"""
        return self._connected
    
    async def execute_raw_query(self, query: str, params: Optional[Dict] = None) -> Any:
        """
        Выполнение сырого SQL запроса
        """
        try:
            async with self.get_session() as session:
                result = await session.execute(text(query), params or {})
                return result.fetchall()
                
        except Exception as e:
            self.logger.error(f"❌ Raw query failed: {e}")
            raise


# ============================================================================
# SINGLETON И CONVENIENCE ФУНКЦИИ
# ============================================================================

# Глобальный экземпляр для использования в приложении
_database_instance: Optional[Database] = None


def get_database() -> Database:
    """
    Получение глобального экземпляра базы данных
    """
    global _database_instance
    if _database_instance is None:
        _database_instance = Database()
    return _database_instance


async def init_database(settings: Optional[Settings] = None) -> Database:
    """
    Инициализация базы данных
    """
    db = get_database()
    if settings:
        db.settings = settings
    await db.init()
    return db


# ============================================================================
# ПРИМЕР ИСПОЛЬЗОВАНИЯ
# ============================================================================

if __name__ == "__main__":
    import asyncio
    from app.config.settings import get_settings
    
    async def test_database():
        """Тестирование функций базы данных"""
        settings = get_settings()
        db = Database(settings)
        
        try:
            # Инициализация
            await db.init()
            
            # Тест сохранения сигнала
            signal_data = {
                'symbol': 'BTCUSDT',
                'strategy_name': 'test_strategy',
                'signal_type': 'BUY',
                'confidence': 0.85,
                'entry_price': 50000.0,
                'timeframe': '5m'
            }
            signal_id = await db.save_signal(signal_data)
            print(f"✅ Signal saved with ID: {signal_id}")
            
            # Тест получения сигналов
            signals = await db.get_recent_signals(5)
            print(f"✅ Retrieved {len(signals)} signals")
            
            # Тест статистики
            stats = await db.get_database_stats()
            print(f"✅ Database stats: {stats}")
            
            # Тест health check
            health = await db.health_check()
            print(f"✅ Health check: {health}")
            
        finally:
            await db.close()
    
    # Запуск теста
    asyncio.run(test_database())
