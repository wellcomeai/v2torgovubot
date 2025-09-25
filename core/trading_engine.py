"""
Trading Bot Trading Engine
Основной торговый движок - координатор всей торговой системы
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Callable, Set, Union, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict, deque
import threading
from concurrent.futures import ThreadPoolExecutor
import uuid
import time

from utils.logger import setup_logger, log_trading_event
from utils.helpers import (
    get_current_timestamp, safe_float, validate_symbol, Timer,
    round_price, round_quantity, calculate_percentage_change
)
from app.config.settings import Settings, get_settings
from data.database import Database
from data.historical_data import CandleData
from strategies.base_strategy import BaseStrategy, StrategyResult, SignalType, SignalStrength
from strategies.strategy_registry import StrategyRegistry


# ============================================================================
# CONSTANTS AND ENUMS
# ============================================================================

logger = setup_logger(__name__)

# Константы движка
DEFAULT_TRADING_CYCLE_INTERVAL = 60  # секунды
DEFAULT_POSITION_SIZE_PERCENTAGE = 0.02  # 2% от баланса
DEFAULT_MAX_POSITIONS = 5
DEFAULT_MAX_DAILY_TRADES = 20
DEFAULT_MIN_CONFIDENCE = 0.7

# Таймауты
ENGINE_STARTUP_TIMEOUT = 60  # секунды
ENGINE_SHUTDOWN_TIMEOUT = 30  # секунды
STRATEGY_EXECUTION_TIMEOUT = 30  # секунды
ORDER_EXECUTION_TIMEOUT = 60  # секунды


class EngineStatus(str, Enum):
    """Статусы торгового движка"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"
    MAINTENANCE = "maintenance"


class TradingMode(str, Enum):
    """Режимы торговли"""
    LIVE = "live"
    PAPER = "paper"
    BACKTEST = "backtest"
    DRY_RUN = "dry_run"


class PositionSide(str, Enum):
    """Стороны позиции"""
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class OrderStatus(str, Enum):
    """Статусы ордеров"""
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class Position:
    """Торговая позиция"""
    symbol: str
    side: PositionSide
    size: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float
    entry_time: int
    strategy_name: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    
    @property
    def pnl_percentage(self) -> float:
        """PnL в процентах"""
        if self.entry_price == 0:
            return 0.0
        
        if self.side == PositionSide.LONG:
            return ((self.current_price - self.entry_price) / self.entry_price) * 100
        else:
            return ((self.entry_price - self.current_price) / self.entry_price) * 100
    
    @property
    def is_profitable(self) -> bool:
        """Прибыльна ли позиция"""
        return self.unrealized_pnl > 0
    
    def update_price(self, new_price: float) -> None:
        """Обновление текущей цены"""
        self.current_price = new_price
        
        # Пересчитываем unrealized PnL
        if self.side == PositionSide.LONG:
            self.unrealized_pnl = (new_price - self.entry_price) * self.size
        else:
            self.unrealized_pnl = (self.entry_price - new_price) * self.size


@dataclass
class TradingOrder:
    """Торговый ордер"""
    id: str
    symbol: str
    side: str  # Buy/Sell
    order_type: str  # Market/Limit
    quantity: float
    price: Optional[float]
    status: OrderStatus
    strategy_name: str
    created_at: int
    filled_at: Optional[int] = None
    filled_quantity: float = 0.0
    filled_price: Optional[float] = None
    commission: float = 0.0
    
    @property
    def is_filled(self) -> bool:
        """Полностью исполнен ли ордер"""
        return self.status == OrderStatus.FILLED
    
    @property
    def is_active(self) -> bool:
        """Активен ли ордер"""
        return self.status in [OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED]


@dataclass
class TradingStats:
    """Статистика торговли"""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    total_commission: float = 0.0
    max_drawdown: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    current_streak: int = 0
    max_winning_streak: int = 0
    max_losing_streak: int = 0
    daily_pnl: float = 0.0
    
    @property
    def win_rate(self) -> float:
        """Винрейт в процентах"""
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100
    
    @property
    def avg_win(self) -> float:
        """Средний выигрыш"""
        return self.largest_win / max(self.winning_trades, 1)
    
    @property
    def avg_loss(self) -> float:
        """Средний проигрыш"""
        return abs(self.largest_loss) / max(self.losing_trades, 1)
    
    @property
    def profit_factor(self) -> float:
        """Профит-фактор"""
        total_wins = self.winning_trades * self.avg_win if self.winning_trades > 0 else 0
        total_losses = self.losing_trades * self.avg_loss if self.losing_trades > 0 else 1
        return total_wins / total_losses if total_losses > 0 else 0


# ============================================================================
# RISK MANAGER CLASS
# ============================================================================

class RiskManager:
    """Менеджер управления рисками"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = setup_logger(f"{__name__}.RiskManager")
        
        # Лимиты
        self.max_position_size = settings.MAX_POSITION_SIZE
        self.max_daily_loss = settings.MAX_DAILY_LOSS
        self.max_drawdown = settings.MAX_DRAWDOWN
        self.stop_loss_pct = settings.STOP_LOSS_PERCENTAGE
        self.take_profit_pct = settings.TAKE_PROFIT_PERCENTAGE
        
        # Текущие метрики
        self.daily_pnl = 0.0
        self.current_drawdown = 0.0
        self.peak_balance = 0.0
        
    def validate_signal(self, signal: StrategyResult, current_balance: float) -> Tuple[bool, str]:
        """Валидация торгового сигнала"""
        try:
            # Проверка дневных лимитов убытков
            if self.daily_pnl <= -self.max_daily_loss:
                return False, f"Daily loss limit exceeded: {self.daily_pnl:.2f}"
            
            # Проверка максимальной просадки
            if self.current_drawdown >= self.max_drawdown:
                return False, f"Max drawdown exceeded: {self.current_drawdown:.2%}"
            
            # Проверка размера позиции
            if signal.entry_price:
                position_size = self.calculate_position_size(signal, current_balance)
                if position_size > self.max_position_size:
                    return False, f"Position size too large: {position_size:.2f}"
            
            # Проверка уровня уверенности
            if signal.confidence < DEFAULT_MIN_CONFIDENCE:
                return False, f"Confidence too low: {signal.confidence:.2f}"
            
            return True, "Signal validated"
            
        except Exception as e:
            self.logger.error(f"❌ Risk validation error: {e}")
            return False, f"Risk validation error: {e}"
    
    def calculate_position_size(self, signal: StrategyResult, balance: float) -> float:
        """Расчет размера позиции"""
        try:
            if not signal.entry_price or not signal.stop_loss:
                # Если нет стоп-лосса, используем фиксированный процент
                return balance * DEFAULT_POSITION_SIZE_PERCENTAGE
            
            # Рассчитываем риск на сделку
            risk_per_trade = balance * 0.01  # 1% риска на сделку
            
            # Рассчитываем расстояние до стоп-лосса
            if signal.signal_type in [SignalType.BUY, SignalType.STRONG_BUY]:
                risk_distance = signal.entry_price - signal.stop_loss
            else:
                risk_distance = signal.stop_loss - signal.entry_price
            
            if risk_distance <= 0:
                return balance * DEFAULT_POSITION_SIZE_PERCENTAGE
            
            # Размер позиции = Риск на сделку / Расстояние до стоп-лосса
            position_size = risk_per_trade / (risk_distance / signal.entry_price)
            
            # Ограничиваем максимальным размером
            return min(position_size, self.max_position_size)
            
        except Exception as e:
            self.logger.error(f"❌ Position size calculation error: {e}")
            return balance * DEFAULT_POSITION_SIZE_PERCENTAGE
    
    def update_daily_pnl(self, pnl: float) -> None:
        """Обновление дневного PnL"""
        self.daily_pnl += pnl
        
        # Проверяем необходимость остановки торговли
        if self.daily_pnl <= -self.max_daily_loss:
            self.logger.warning(f"⚠️ Daily loss limit reached: {self.daily_pnl:.2f}")
    
    def update_drawdown(self, current_balance: float) -> None:
        """Обновление просадки"""
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
            self.current_drawdown = 0.0
        else:
            self.current_drawdown = (self.peak_balance - current_balance) / self.peak_balance
    
    def should_stop_trading(self) -> Tuple[bool, str]:
        """Проверка необходимости остановки торговли"""
        if self.daily_pnl <= -self.max_daily_loss:
            return True, "Daily loss limit exceeded"
        
        if self.current_drawdown >= self.max_drawdown:
            return True, "Maximum drawdown exceeded"
        
        return False, ""
    
    def reset_daily_metrics(self) -> None:
        """Сброс дневных метрик"""
        self.daily_pnl = 0.0
        self.logger.info("📊 Daily risk metrics reset")


# ============================================================================
# POSITION MANAGER CLASS  
# ============================================================================

class PositionManager:
    """Менеджер управления позициями"""
    
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self.logger = setup_logger(f"{__name__}.PositionManager")
    
    def add_position(self, position: Position) -> None:
        """Добавление новой позиции"""
        position_key = f"{position.symbol}_{position.strategy_name}"
        self.positions[position_key] = position
        
        self.logger.info(f"📊 New position: {position.symbol} {position.side.value} "
                        f"${position.size:.2f} @ ${position.entry_price:.4f}")
    
    def close_position(self, symbol: str, strategy_name: str, exit_price: float) -> Optional[Position]:
        """Закрытие позиции"""
        position_key = f"{symbol}_{strategy_name}"
        
        if position_key in self.positions:
            position = self.positions[position_key]
            position.current_price = exit_price
            
            # Рассчитываем realized PnL
            if position.side == PositionSide.LONG:
                position.realized_pnl = (exit_price - position.entry_price) * position.size
            else:
                position.realized_pnl = (position.entry_price - exit_price) * position.size
            
            # Удаляем позицию
            del self.positions[position_key]
            
            self.logger.info(f"📊 Position closed: {symbol} PnL: ${position.realized_pnl:.2f}")
            return position
        
        return None
    
    def update_positions(self, price_data: Dict[str, float]) -> None:
        """Обновление всех позиций текущими ценами"""
        for position in self.positions.values():
            if position.symbol in price_data:
                position.update_price(price_data[position.symbol])
    
    def get_position(self, symbol: str, strategy_name: str) -> Optional[Position]:
        """Получение позиции"""
        position_key = f"{symbol}_{strategy_name}"
        return self.positions.get(position_key)
    
    def get_total_exposure(self) -> float:
        """Общая экспозиция по всем позициям"""
        return sum(abs(pos.size * pos.current_price) for pos in self.positions.values())
    
    def get_total_pnl(self) -> float:
        """Общий нереализованный PnL"""
        return sum(pos.unrealized_pnl for pos in self.positions.values())
    
    def get_positions_for_symbol(self, symbol: str) -> List[Position]:
        """Все позиции по символу"""
        return [pos for pos in self.positions.values() if pos.symbol == symbol]
    
    def has_position(self, symbol: str, strategy_name: str) -> bool:
        """Есть ли открытая позиция"""
        position_key = f"{symbol}_{strategy_name}"
        return position_key in self.positions


# ============================================================================
# ORDER MANAGER CLASS
# ============================================================================

class OrderManager:
    """Менеджер управления ордерами"""
    
    def __init__(self):
        self.active_orders: Dict[str, TradingOrder] = {}
        self.order_history: deque = deque(maxlen=1000)
        self.logger = setup_logger(f"{__name__}.OrderManager")
    
    def add_order(self, order: TradingOrder) -> None:
        """Добавление ордера"""
        self.active_orders[order.id] = order
        self.logger.info(f"📝 Order created: {order.symbol} {order.side} ${order.quantity:.2f}")
    
    def update_order_status(self, order_id: str, status: OrderStatus, 
                           filled_qty: float = 0, filled_price: float = None) -> Optional[TradingOrder]:
        """Обновление статуса ордера"""
        if order_id in self.active_orders:
            order = self.active_orders[order_id]
            order.status = status
            order.filled_quantity = filled_qty
            order.filled_price = filled_price
            
            if status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED]:
                order.filled_at = get_current_timestamp()
                
                # Перемещаем в историю
                self.order_history.append(order)
                del self.active_orders[order_id]
            
            self.logger.info(f"📝 Order updated: {order_id} status: {status.value}")
            return order
        
        return None
    
    def cancel_order(self, order_id: str) -> bool:
        """Отмена ордера"""
        if order_id in self.active_orders:
            self.update_order_status(order_id, OrderStatus.CANCELLED)
            self.logger.info(f"📝 Order cancelled: {order_id}")
            return True
        return False
    
    def get_active_orders_for_symbol(self, symbol: str) -> List[TradingOrder]:
        """Активные ордера по символу"""
        return [order for order in self.active_orders.values() if order.symbol == symbol]
    
    def cleanup_old_orders(self, max_age_hours: int = 24) -> None:
        """Очистка старых ордеров"""
        cutoff_time = get_current_timestamp() - (max_age_hours * 3600 * 1000)
        
        orders_to_remove = []
        for order_id, order in self.active_orders.items():
            if order.created_at < cutoff_time:
                orders_to_remove.append(order_id)
        
        for order_id in orders_to_remove:
            self.cancel_order(order_id)
            
        if orders_to_remove:
            self.logger.info(f"🧹 Cleaned up {len(orders_to_remove)} old orders")


# ============================================================================
# STRATEGY MANAGER CLASS
# ============================================================================

class StrategyManager:
    """Менеджер управления стратегиями"""
    
    def __init__(self, strategy_registry: StrategyRegistry):
        self.strategy_registry = strategy_registry
        self.active_strategies: Dict[str, Dict[str, List[BaseStrategy]]] = defaultdict(lambda: defaultdict(list))
        self.strategy_stats: Dict[str, TradingStats] = {}
        self.logger = setup_logger(f"{__name__}.StrategyManager")
    
    async def add_strategy(self, strategy_name: str, symbol: str, timeframe: str, 
                          params: Dict[str, Any]) -> bool:
        """Добавление стратегии"""
        try:
            strategy = self.strategy_registry.create_strategy(strategy_name, params)
            if not strategy:
                self.logger.error(f"❌ Failed to create strategy: {strategy_name}")
                return False
            
            self.active_strategies[symbol][timeframe].append(strategy)
            
            # Инициализируем статистику
            strategy_key = f"{strategy_name}_{symbol}_{timeframe}"
            if strategy_key not in self.strategy_stats:
                self.strategy_stats[strategy_key] = TradingStats()
            
            self.logger.info(f"📊 Strategy added: {strategy_name} for {symbol} {timeframe}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to add strategy {strategy_name}: {e}")
            return False
    
    async def remove_strategy(self, strategy_name: str, symbol: str, timeframe: str) -> bool:
        """Удаление стратегии"""
        try:
            strategies = self.active_strategies[symbol][timeframe]
            for i, strategy in enumerate(strategies):
                if strategy.name == strategy_name:
                    strategies.pop(i)
                    self.logger.info(f"📊 Strategy removed: {strategy_name} from {symbol} {timeframe}")
                    return True
            
            self.logger.warning(f"⚠️ Strategy not found: {strategy_name}")
            return False
            
        except Exception as e:
            self.logger.error(f"❌ Failed to remove strategy {strategy_name}: {e}")
            return False
    
    async def run_strategies(self, symbol: str, timeframe: str, 
                           candles: List[CandleData]) -> List[StrategyResult]:
        """Запуск стратегий для символа и таймфрейма"""
        signals = []
        strategies = self.active_strategies[symbol][timeframe]
        
        for strategy in strategies:
            try:
                with Timer(f"strategy_{strategy.name}_{symbol}"):
                    signal = await asyncio.wait_for(
                        strategy.analyze(candles, symbol, timeframe),
                        timeout=STRATEGY_EXECUTION_TIMEOUT
                    )
                    
                    if signal and signal.signal_type != SignalType.HOLD:
                        signal.metadata['strategy_name'] = strategy.name
                        signal.metadata['symbol'] = symbol
                        signal.metadata['timeframe'] = timeframe
                        signals.append(signal)
                        
                        self.logger.debug(f"🎯 Signal: {strategy.name} -> {signal.signal_type.value}")
                
            except asyncio.TimeoutError:
                self.logger.warning(f"⏰ Strategy timeout: {strategy.name}")
            except Exception as e:
                self.logger.error(f"❌ Strategy error {strategy.name}: {e}")
        
        return signals
    
    def update_strategy_stats(self, strategy_name: str, symbol: str, timeframe: str, 
                             pnl: float, is_win: bool) -> None:
        """Обновление статистики стратегии"""
        strategy_key = f"{strategy_name}_{symbol}_{timeframe}"
        
        if strategy_key not in self.strategy_stats:
            self.strategy_stats[strategy_key] = TradingStats()
        
        stats = self.strategy_stats[strategy_key]
        stats.total_trades += 1
        stats.total_pnl += pnl
        
        if is_win:
            stats.winning_trades += 1
            stats.largest_win = max(stats.largest_win, pnl)
            stats.current_streak = max(0, stats.current_streak + 1)
        else:
            stats.losing_trades += 1
            stats.largest_loss = min(stats.largest_loss, pnl)
            stats.current_streak = min(0, stats.current_streak - 1)
        
        # Обновляем максимальные серии
        stats.max_winning_streak = max(stats.max_winning_streak, stats.current_streak)
        stats.max_losing_streak = min(stats.max_losing_streak, stats.current_streak)
    
    def get_strategy_performance(self, strategy_name: str, symbol: str, timeframe: str) -> Optional[TradingStats]:
        """Получение статистики стратегии"""
        strategy_key = f"{strategy_name}_{symbol}_{timeframe}"
        return self.strategy_stats.get(strategy_key)
    
    def get_all_strategies(self) -> Dict[str, Dict[str, List[str]]]:
        """Получение всех активных стратегий"""
        result = {}
        for symbol, timeframes in self.active_strategies.items():
            result[symbol] = {}
            for timeframe, strategies in timeframes.items():
                result[symbol][timeframe] = [s.name for s in strategies]
        return result


# ============================================================================
# MAIN TRADING ENGINE CLASS
# ============================================================================

class TradingEngine:
    """
    Главный торговый движок
    
    Координирует все компоненты торговой системы:
    - Управление стратегиями
    - Обработка торговых сигналов  
    - Управление позициями и рисками
    - Исполнение ордеров
    - Мониторинг системы
    """
    
    def __init__(
        self,
        settings: Optional[Settings] = None,
        database: Optional[Database] = None
    ):
        self.settings = settings or get_settings()
        self.database = database
        self.logger = setup_logger(f"{__name__}.TradingEngine")
        
        # Статус движка
        self.status = EngineStatus.STOPPED
        self.trading_mode = TradingMode.PAPER if self.settings.BYBIT_TESTNET else TradingMode.LIVE
        
        # Компоненты (будут инжектированы)
        self.bybit_client = None
        self.data_manager = None
        self.signal_processor = None
        self.websocket_manager = None
        
        # Внутренние менеджеры
        self.strategy_registry = StrategyRegistry()
        self.strategy_manager = StrategyManager(self.strategy_registry)
        self.position_manager = PositionManager()
        self.order_manager = OrderManager()
        self.risk_manager = RiskManager(self.settings)
        
        # Настройки торговли
        self.trading_cycle_interval = DEFAULT_TRADING_CYCLE_INTERVAL
        self.max_positions = DEFAULT_MAX_POSITIONS
        self.max_daily_trades = DEFAULT_MAX_DAILY_TRADES
        
        # Состояние
        self._is_running = False
        self._trading_tasks: Set[asyncio.Task] = set()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="TradingEngine")
        
        # Метрики
        self.current_balance = 0.0
        self.daily_trades_count = 0
        self.engine_stats = TradingStats()
        
        # Callbacks
        self._signal_callbacks: List[Callable] = []
        self._position_callbacks: List[Callable] = []
        self._error_callbacks: List[Callable] = []
        
        self.logger.info(f"🤖 Trading Engine initialized (mode: {self.trading_mode.value})")
    
    # ========================================================================
    # LIFECYCLE MANAGEMENT
    # ========================================================================
    
    async def start(self) -> None:
        """Запуск торгового движка"""
        if self.status != EngineStatus.STOPPED:
            self.logger.warning("⚠️ Engine is already running")
            return
        
        try:
            self.status = EngineStatus.STARTING
            self.logger.info("🚀 Starting Trading Engine...")
            
            # Проверяем зависимости
            await self._validate_dependencies()
            
            # Инициализируем компоненты
            await self._initialize_components()
            
            # Загружаем стратегии
            await self._load_strategies()
            
            # Получаем текущий баланс
            await self._update_balance()
            
            # Запускаем фоновые задачи
            await self._start_background_tasks()
            
            self.status = EngineStatus.RUNNING
            self._is_running = True
            
            log_trading_event(
                event_type="ENGINE_STARTED",
                message="🚀 Trading Engine started successfully",
                trading_mode=self.trading_mode.value,
                balance=self.current_balance
            )
            
            self.logger.info("✅ Trading Engine started successfully")
            
        except Exception as e:
            self.status = EngineStatus.ERROR
            self.logger.error(f"❌ Failed to start Trading Engine: {e}")
            await self.stop()
            raise
    
    async def stop(self) -> None:
        """Остановка торгового движка"""
        if self.status == EngineStatus.STOPPED:
            return
        
        try:
            self.status = EngineStatus.STOPPING
            self.logger.info("🔄 Stopping Trading Engine...")
            self._is_running = False
            
            # Отменяем все задачи
            for task in self._trading_tasks:
                if not task.done():
                    task.cancel()
            
            if self._trading_tasks:
                await asyncio.gather(*self._trading_tasks, return_exceptions=True)
            
            # Закрываем все открытые позиции (опционально)
            if self.settings.ENVIRONMENT.value == "production":
                await self._close_all_positions()
            
            # Отменяем активные ордера
            await self._cancel_active_orders()
            
            # Закрываем executor
            self._executor.shutdown(wait=True)
            
            self.status = EngineStatus.STOPPED
            
            log_trading_event(
                event_type="ENGINE_STOPPED", 
                message="🔄 Trading Engine stopped",
                final_balance=self.current_balance,
                total_trades=self.engine_stats.total_trades
            )
            
            self.logger.info("✅ Trading Engine stopped successfully")
            
        except Exception as e:
            self.status = EngineStatus.ERROR
            self.logger.error(f"❌ Error stopping Trading Engine: {e}")
    
    async def pause(self) -> None:
        """Приостановка торговли"""
        if self.status == EngineStatus.RUNNING:
            self.status = EngineStatus.PAUSED
            self.logger.info("⏸️ Trading Engine paused")
    
    async def resume(self) -> None:
        """Возобновление торговли"""
        if self.status == EngineStatus.PAUSED:
            self.status = EngineStatus.RUNNING
            self.logger.info("▶️ Trading Engine resumed")
    
    async def _validate_dependencies(self) -> None:
        """Валидация зависимостей"""
        missing = []
        
        if not self.bybit_client:
            missing.append("bybit_client")
        if not self.data_manager:
            missing.append("data_manager")
        if not self.signal_processor:
            missing.append("signal_processor")
        
        if missing:
            raise ValueError(f"Missing dependencies: {', '.join(missing)}")
        
        # Тестируем подключения
        if not await self.bybit_client.test_connection():
            raise ConnectionError("Failed to connect to Bybit API")
    
    async def _initialize_components(self) -> None:
        """Инициализация компонентов"""
        # Устанавливаем связи между компонентами
        if self.signal_processor and self.data_manager:
            self.signal_processor.set_market_data_provider(self.data_manager)
        
        # Добавляем callbacks
        if self.signal_processor:
            self.signal_processor.add_signal_callback(self._on_processed_signal)
    
    async def _load_strategies(self) -> None:
        """Загрузка стратегий из конфигурации"""
        try:
            strategy_config = self.settings.DEFAULT_ACTIVE_STRATEGIES
            
            for symbol, strategies in strategy_config.items():
                for strategy_config in strategies:
                    if strategy_config.get('enabled', True):
                        await self.strategy_manager.add_strategy(
                            strategy_name=strategy_config['name'],
                            symbol=symbol,
                            timeframe=strategy_config['timeframe'],
                            params=strategy_config['params']
                        )
            
            self.logger.info("📊 Strategies loaded from configuration")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to load strategies: {e}")
    
    async def _start_background_tasks(self) -> None:
        """Запуск фоновых задач"""
        # Главный торговый цикл
        task = asyncio.create_task(
            self._main_trading_loop(),
            name="main_trading_loop"
        )
        self._trading_tasks.add(task)
        
        # Мониторинг позиций
        task = asyncio.create_task(
            self._position_monitoring_loop(),
            name="position_monitoring"
        )
        self._trading_tasks.add(task)
        
        # Мониторинг ордеров
        task = asyncio.create_task(
            self._order_monitoring_loop(),
            name="order_monitoring"
        )
        self._trading_tasks.add(task)
        
        # Health check
        task = asyncio.create_task(
            self._health_check_loop(),
            name="health_check"
        )
        self._trading_tasks.add(task)
        
        # Очистка старых данных
        task = asyncio.create_task(
            self._cleanup_loop(),
            name="cleanup"
        )
        self._trading_tasks.add(task)
    
    # ========================================================================
    # MAIN TRADING LOOP
    # ========================================================================
    
    async def _main_trading_loop(self) -> None:
        """Главный торговый цикл"""
        self.logger.info("🔄 Main trading loop started")
        
        while self._is_running:
            try:
                if self.status != EngineStatus.RUNNING:
                    await asyncio.sleep(5)
                    continue
                
                # Проверяем лимиты торговли
                should_stop, reason = self.risk_manager.should_stop_trading()
                if should_stop:
                    self.logger.warning(f"🛑 Trading stopped: {reason}")
                    await self.pause()
                    await asyncio.sleep(60)
                    continue
                
                # Проверяем дневной лимит сделок
                if self.daily_trades_count >= self.max_daily_trades:
                    self.logger.warning(f"📊 Daily trades limit reached: {self.daily_trades_count}")
                    await asyncio.sleep(300)  # Ждем 5 минут
                    continue
                
                # Основной торговый цикл
                await self._execute_trading_cycle()
                
                # Обновляем баланс
                await self._update_balance()
                
                # Обновляем метрики риска
                self.risk_manager.update_drawdown(self.current_balance)
                
                # Ждем до следующего цикла
                await asyncio.sleep(self.trading_cycle_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Main trading loop error: {e}")
                await self._handle_error(e)
                await asyncio.sleep(60)  # Ждем минуту при ошибке
        
        self.logger.info("🔄 Main trading loop stopped")
    
    async def _execute_trading_cycle(self) -> None:
        """Выполнение одного торгового цикла"""
        try:
            with Timer("trading_cycle"):
                # Получаем список активных символов
                active_symbols = set()
                for symbol in self.strategy_manager.active_strategies.keys():
                    active_symbols.add(symbol)
                
                if not active_symbols:
                    return
                
                # Обрабатываем каждый символ
                for symbol in active_symbols:
                    try:
                        await self._process_symbol(symbol)
                    except Exception as e:
                        self.logger.error(f"❌ Error processing {symbol}: {e}")
                        continue
                
        except Exception as e:
            self.logger.error(f"❌ Trading cycle error: {e}")
    
    async def _process_symbol(self, symbol: str) -> None:
        """Обработка торгового символа"""
        try:
            # Получаем активные таймфреймы для символа
            timeframes = list(self.strategy_manager.active_strategies[symbol].keys())
            
            for timeframe in timeframes:
                try:
                    # Получаем рыночные данные
                    candles = await self.data_manager.get_market_data(symbol, timeframe, 200)
                    
                    if not candles or len(candles) < 20:
                        self.logger.warning(f"⚠️ Insufficient data for {symbol} {timeframe}")
                        continue
                    
                    # Запускаем стратегии
                    signals = await self.strategy_manager.run_strategies(symbol, timeframe, candles)
                    
                    # Обрабатываем сигналы
                    for signal in signals:
                        await self._process_trading_signal(signal)
                        
                except Exception as e:
                    self.logger.error(f"❌ Error processing {symbol} {timeframe}: {e}")
                    continue
                    
        except Exception as e:
            self.logger.error(f"❌ Symbol processing error {symbol}: {e}")
    
    async def _process_trading_signal(self, signal: StrategyResult) -> None:
        """Обработка торгового сигнала"""
        try:
            symbol = signal.metadata.get('symbol')
            strategy_name = signal.metadata.get('strategy_name')
            
            # Проверяем нет ли уже позиции
            if self.position_manager.has_position(symbol, strategy_name):
                self.logger.debug(f"⚠️ Position already exists: {symbol} {strategy_name}")
                return
            
            # Проверяем лимит позиций
            if len(self.position_manager.positions) >= self.max_positions:
                self.logger.debug(f"⚠️ Max positions limit reached: {len(self.position_manager.positions)}")
                return
            
            # Валидация риска
            risk_ok, risk_reason = self.risk_manager.validate_signal(signal, self.current_balance)
            if not risk_ok:
                self.logger.debug(f"⚠️ Signal rejected by risk manager: {risk_reason}")
                return
            
            # Отправляем сигнал на дальнейшую обработку
            if self.signal_processor:
                processed = await self.signal_processor.process_signal(
                    signal=signal,
                    strategy_name=strategy_name,
                    symbol=symbol,
                    timeframe=signal.metadata.get('timeframe')
                )
                
                if processed and processed.status.value == "completed":
                    # Исполняем торговлю
                    await self._execute_signal(processed)
                    
        except Exception as e:
            self.logger.error(f"❌ Signal processing error: {e}")
    
    async def _execute_signal(self, processed_signal) -> None:
        """Исполнение торгового сигнала"""
        try:
            signal = processed_signal.original_signal
            symbol = signal.metadata.get('symbol')
            strategy_name = signal.metadata.get('strategy_name')
            
            # Рассчитываем размер позиции
            position_size = self.risk_manager.calculate_position_size(signal, self.current_balance)
            
            # Определяем сторону ордера
            side = "Buy" if signal.signal_type in [SignalType.BUY, SignalType.STRONG_BUY] else "Sell"
            
            # Создаем ордер
            order = TradingOrder(
                id=str(uuid.uuid4()),
                symbol=symbol,
                side=side,
                order_type="Market",  # Пока только рыночные ордера
                quantity=position_size,
                price=None,
                status=OrderStatus.PENDING,
                strategy_name=strategy_name,
                created_at=get_current_timestamp()
            )
            
            # Размещаем ордер
            if self.trading_mode == TradingMode.LIVE:
                success = await self._place_real_order(order, signal)
            else:
                success = await self._place_paper_order(order, signal)
            
            if success:
                self.order_manager.add_order(order)
                self.daily_trades_count += 1
                
                log_trading_event(
                    event_type="ORDER_PLACED",
                    symbol=symbol,
                    strategy=strategy_name,
                    side=side,
                    quantity=position_size,
                    trading_mode=self.trading_mode.value
                )
                
        except Exception as e:
            self.logger.error(f"❌ Signal execution error: {e}")
    
    # ========================================================================
    # ORDER EXECUTION
    # ========================================================================
    
    async def _place_real_order(self, order: TradingOrder, signal: StrategyResult) -> bool:
        """Размещение реального ордера"""
        try:
            response = await self.bybit_client.place_order(
                symbol=order.symbol,
                side=order.side,
                order_type="Market",
                qty=str(order.quantity)
            )
            
            if response and response.get('retCode') == 0:
                result = response.get('result', {})
                order.id = result.get('orderId', order.id)
                
                self.logger.info(f"✅ Real order placed: {order.symbol} {order.side} ${order.quantity:.2f}")
                return True
            else:
                self.logger.error(f"❌ Failed to place real order: {response}")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ Real order placement error: {e}")
            return False
    
    async def _place_paper_order(self, order: TradingOrder, signal: StrategyResult) -> bool:
        """Размещение бумажного ордера (симуляция)"""
        try:
            # Получаем текущую цену
            current_price = await self.data_manager.get_latest_price(order.symbol)
            
            if not current_price:
                self.logger.error(f"❌ Failed to get current price for {order.symbol}")
                return False
            
            # Симулируем исполнение
            order.filled_price = current_price
            order.filled_quantity = order.quantity
            order.status = OrderStatus.FILLED
            order.filled_at = get_current_timestamp()
            
            # Создаем позицию
            position = Position(
                symbol=order.symbol,
                side=PositionSide.LONG if order.side == "Buy" else PositionSide.SHORT,
                size=order.quantity,
                entry_price=current_price,
                current_price=current_price,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                entry_time=get_current_timestamp(),
                strategy_name=order.strategy_name,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit
            )
            
            self.position_manager.add_position(position)
            
            self.logger.info(f"✅ Paper order executed: {order.symbol} {order.side} ${order.quantity:.2f} @ ${current_price:.4f}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Paper order execution error: {e}")
            return False
    
    # ========================================================================
    # POSITION AND ORDER MONITORING
    # ========================================================================
    
    async def _position_monitoring_loop(self) -> None:
        """Мониторинг позиций"""
        while self._is_running:
            try:
                if self.status != EngineStatus.RUNNING:
                    await asyncio.sleep(10)
                    continue
                
                # Получаем текущие цены
                symbols = list(set(pos.symbol for pos in self.position_manager.positions.values()))
                price_data = {}
                
                for symbol in symbols:
                    price = await self.data_manager.get_latest_price(symbol)
                    if price:
                        price_data[symbol] = price
                
                # Обновляем позиции
                self.position_manager.update_positions(price_data)
                
                # Проверяем стоп-лоссы и тейк-профиты
                await self._check_position_exits()
                
                await asyncio.sleep(30)  # Проверка каждые 30 секунд
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Position monitoring error: {e}")
                await asyncio.sleep(60)
    
    async def _check_position_exits(self) -> None:
        """Проверка условий выхода из позиций"""
        positions_to_close = []
        
        for pos_key, position in self.position_manager.positions.items():
            try:
                should_close, exit_reason = self._should_close_position(position)
                
                if should_close:
                    positions_to_close.append((pos_key, position, exit_reason))
                    
            except Exception as e:
                self.logger.error(f"❌ Position exit check error: {e}")
        
        # Закрываем позиции
        for pos_key, position, reason in positions_to_close:
            await self._close_position(position, reason)
    
    def _should_close_position(self, position: Position) -> Tuple[bool, str]:
        """Проверка необходимости закрытия позиции"""
        # Проверка стоп-лосса
        if position.stop_loss:
            if position.side == PositionSide.LONG:
                if position.current_price <= position.stop_loss:
                    return True, f"Stop Loss hit: {position.current_price:.4f} <= {position.stop_loss:.4f}"
            else:
                if position.current_price >= position.stop_loss:
                    return True, f"Stop Loss hit: {position.current_price:.4f} >= {position.stop_loss:.4f}"
        
        # Проверка тейк-профита
        if position.take_profit:
            if position.side == PositionSide.LONG:
                if position.current_price >= position.take_profit:
                    return True, f"Take Profit hit: {position.current_price:.4f} >= {position.take_profit:.4f}"
            else:
                if position.current_price <= position.take_profit:
                    return True, f"Take Profit hit: {position.current_price:.4f} <= {position.take_profit:.4f}"
        
        return False, ""
    
    async def _close_position(self, position: Position, reason: str) -> None:
        """Закрытие позиции"""
        try:
            # Создаем ордер на закрытие
            close_side = "Sell" if position.side == PositionSide.LONG else "Buy"
            
            order = TradingOrder(
                id=str(uuid.uuid4()),
                symbol=position.symbol,
                side=close_side,
                order_type="Market",
                quantity=position.size,
                price=None,
                status=OrderStatus.PENDING,
                strategy_name=position.strategy_name,
                created_at=get_current_timestamp()
            )
            
            # Исполняем закрытие
            if self.trading_mode == TradingMode.LIVE:
                success = await self._close_real_position(order, position)
            else:
                success = await self._close_paper_position(order, position)
            
            if success:
                # Обновляем статистику
                is_win = position.realized_pnl > 0
                
                self.strategy_manager.update_strategy_stats(
                    position.strategy_name,
                    position.symbol,
                    "5m",  # TODO: get actual timeframe
                    position.realized_pnl,
                    is_win
                )
                
                # Обновляем общую статистику
                self._update_engine_stats(position.realized_pnl, is_win)
                
                # Обновляем риск-метрики
                self.risk_manager.update_daily_pnl(position.realized_pnl)
                
                log_trading_event(
                    event_type="POSITION_CLOSED",
                    symbol=position.symbol,
                    strategy=position.strategy_name,
                    pnl=position.realized_pnl,
                    reason=reason,
                    duration_minutes=(get_current_timestamp() - position.entry_time) // 60000
                )
                
        except Exception as e:
            self.logger.error(f"❌ Position closing error: {e}")
    
    async def _close_real_position(self, order: TradingOrder, position: Position) -> bool:
        """Закрытие реальной позиции"""
        # TODO: Implement real position closing via Bybit API
        return False
    
    async def _close_paper_position(self, order: TradingOrder, position: Position) -> bool:
        """Закрытие бумажной позиции"""
        try:
            closed_position = self.position_manager.close_position(
                position.symbol,
                position.strategy_name, 
                position.current_price
            )
            
            if closed_position:
                self.logger.info(f"✅ Paper position closed: {position.symbol} "
                               f"PnL: ${closed_position.realized_pnl:.2f}")
                return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"❌ Paper position closing error: {e}")
            return False
    
    async def _order_monitoring_loop(self) -> None:
        """Мониторинг ордеров"""
        while self._is_running:
            try:
                if self.status != EngineStatus.RUNNING:
                    await asyncio.sleep(30)
                    continue
                
                # Очистка старых ордеров
                self.order_manager.cleanup_old_orders(24)
                
                await asyncio.sleep(300)  # Проверка каждые 5 минут
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Order monitoring error: {e}")
                await asyncio.sleep(60)
    
    # ========================================================================
    # BACKGROUND TASKS
    # ========================================================================
    
    async def _health_check_loop(self) -> None:
        """Мониторинг здоровья системы"""
        while self._is_running:
            try:
                # Проверяем здоровье компонентов
                await self._perform_health_checks()
                
                await asyncio.sleep(self.settings.HEALTH_CHECK_INTERVAL)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Health check error: {e}")
                await asyncio.sleep(60)
    
    async def _perform_health_checks(self) -> None:
        """Выполнение проверок здоровья"""
        try:
            issues = []
            
            # Проверка API подключения
            if not await self.bybit_client.test_connection():
                issues.append("Bybit API connection failed")
            
            # Проверка WebSocket
            if self.websocket_manager and not self.websocket_manager.is_connected():
                issues.append("WebSocket not connected")
            
            # Проверка данных
            if self.data_manager:
                health = await self.data_manager.health_check()
                if health.get('status') != 'healthy':
                    issues.append(f"Data manager unhealthy: {health.get('status')}")
            
            # Логируем проблемы
            if issues:
                self.logger.warning(f"⚠️ Health check issues: {'; '.join(issues)}")
            else:
                self.logger.debug("✅ All health checks passed")
                
        except Exception as e:
            self.logger.error(f"❌ Health check execution error: {e}")
    
    async def _cleanup_loop(self) -> None:
        """Очистка старых данных"""
        while self._is_running:
            try:
                # Сброс дневных метрик в полночь UTC
                current_hour = datetime.now(timezone.utc).hour
                if current_hour == 0:  # Полночь UTC
                    self.risk_manager.reset_daily_metrics()
                    self.daily_trades_count = 0
                    self.logger.info("📊 Daily metrics reset")
                
                await asyncio.sleep(3600)  # Проверка каждый час
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Cleanup error: {e}")
                await asyncio.sleep(3600)
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    async def _update_balance(self) -> None:
        """Обновление текущего баланса"""
        try:
            if self.trading_mode == TradingMode.LIVE:
                # Получаем реальный баланс
                response = await self.bybit_client.get_wallet_balance()
                if response and response.get('retCode') == 0:
                    result = response.get('result', {})
                    accounts = result.get('list', [])
                    if accounts:
                        coins = accounts[0].get('coin', [])
                        usdt_balance = next((c for c in coins if c.get('coin') == 'USDT'), {})
                        self.current_balance = safe_float(usdt_balance.get('walletBalance', 0))
            else:
                # Симулируем баланс
                total_pnl = self.position_manager.get_total_pnl()
                self.current_balance = 10000.0 + self.engine_stats.total_pnl + total_pnl
                
        except Exception as e:
            self.logger.error(f"❌ Balance update error: {e}")
    
    def _update_engine_stats(self, pnl: float, is_win: bool) -> None:
        """Обновление статистики движка"""
        self.engine_stats.total_trades += 1
        self.engine_stats.total_pnl += pnl
        
        if is_win:
            self.engine_stats.winning_trades += 1
            self.engine_stats.largest_win = max(self.engine_stats.largest_win, pnl)
        else:
            self.engine_stats.losing_trades += 1
            self.engine_stats.largest_loss = min(self.engine_stats.largest_loss, pnl)
    
    async def _close_all_positions(self) -> None:
        """Закрытие всех позиций"""
        try:
            positions = list(self.position_manager.positions.values())
            for position in positions:
                await self._close_position(position, "Engine shutdown")
                
        except Exception as e:
            self.logger.error(f"❌ Close all positions error: {e}")
    
    async def _cancel_active_orders(self) -> None:
        """Отмена всех активных ордеров"""
        try:
            active_orders = list(self.order_manager.active_orders.keys())
            for order_id in active_orders:
                self.order_manager.cancel_order(order_id)
                
        except Exception as e:
            self.logger.error(f"❌ Cancel active orders error: {e}")
    
    async def _handle_error(self, error: Exception) -> None:
        """Обработка ошибок"""
        try:
            # Вызываем error callbacks
            for callback in self._error_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(error, self)
                    else:
                        callback(error, self)
                except Exception as e:
                    self.logger.error(f"❌ Error callback failed: {e}")
                    
        except Exception as e:
            self.logger.error(f"❌ Error handling error: {e}")
    
    # ========================================================================
    # CALLBACK MANAGEMENT
    # ========================================================================
    
    async def _on_processed_signal(self, processed_signal) -> None:
        """Callback для обработанных сигналов"""
        try:
            # Вызываем signal callbacks
            for callback in self._signal_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(processed_signal)
                    else:
                        callback(processed_signal)
                except Exception as e:
                    self.logger.error(f"❌ Signal callback error: {e}")
                    
        except Exception as e:
            self.logger.error(f"❌ Signal callback handling error: {e}")
    
    def add_signal_callback(self, callback: Callable) -> None:
        """Добавление callback для сигналов"""
        self._signal_callbacks.append(callback)
        
    def add_position_callback(self, callback: Callable) -> None:
        """Добавление callback для позиций"""
        self._position_callbacks.append(callback)
        
    def add_error_callback(self, callback: Callable) -> None:
        """Добавление callback для ошибок"""
        self._error_callbacks.append(callback)
    
    # ========================================================================
    # PUBLIC API METHODS
    # ========================================================================
    
    async def add_strategy(self, strategy_name: str, symbol: str, timeframe: str, 
                          params: Dict[str, Any]) -> bool:
        """Добавление новой стратегии"""
        return await self.strategy_manager.add_strategy(strategy_name, symbol, timeframe, params)
    
    async def remove_strategy(self, strategy_name: str, symbol: str, timeframe: str) -> bool:
        """Удаление стратегии"""
        return await self.strategy_manager.remove_strategy(strategy_name, symbol, timeframe)
    
    def get_active_strategies(self) -> Dict[str, Any]:
        """Получение активных стратегий"""
        return self.strategy_manager.get_all_strategies()
    
    def get_positions(self) -> List[Dict[str, Any]]:
        """Получение текущих позиций"""
        return [
            {
                'symbol': pos.symbol,
                'side': pos.side.value,
                'size': pos.size,
                'entry_price': pos.entry_price,
                'current_price': pos.current_price,
                'unrealized_pnl': pos.unrealized_pnl,
                'pnl_percentage': pos.pnl_percentage,
                'strategy_name': pos.strategy_name,
                'entry_time': pos.entry_time
            }
            for pos in self.position_manager.positions.values()
        ]
    
    def get_trading_stats(self) -> Dict[str, Any]:
        """Получение статистики торговли"""
        return {
            'engine_status': self.status.value,
            'trading_mode': self.trading_mode.value,
            'current_balance': self.current_balance,
            'daily_trades': self.daily_trades_count,
            'active_positions': len(self.position_manager.positions),
            'total_exposure': self.position_manager.get_total_exposure(),
            'unrealized_pnl': self.position_manager.get_total_pnl(),
            'daily_pnl': self.risk_manager.daily_pnl,
            'engine_stats': {
                'total_trades': self.engine_stats.total_trades,
                'win_rate': self.engine_stats.win_rate,
                'total_pnl': self.engine_stats.total_pnl,
                'profit_factor': self.engine_stats.profit_factor,
                'max_drawdown': self.engine_stats.max_drawdown
            }
        }
    
    async def get_performance_report(self) -> Dict[str, Any]:
        """Получение отчета о производительности"""
        return {
            'engine': self.get_trading_stats(),
            'strategies': {
                f"{name}_{symbol}_{timeframe}": {
                    'total_trades': stats.total_trades,
                    'win_rate': stats.win_rate,
                    'total_pnl': stats.total_pnl,
                    'avg_win': stats.avg_win,
                    'avg_loss': stats.avg_loss,
                    'profit_factor': stats.profit_factor
                }
                for name, stats in self.strategy_manager.strategy_stats.items()
                for symbol, timeframes in self.strategy_manager.active_strategies.items()
                for timeframe in timeframes.keys()
            },
            'risk_metrics': {
                'daily_pnl': self.risk_manager.daily_pnl,
                'current_drawdown': self.risk_manager.current_drawdown,
                'max_daily_loss': self.risk_manager.max_daily_loss,
                'max_drawdown': self.risk_manager.max_drawdown
            }
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Полная проверка здоровья"""
        try:
            component_health = {}
            
            # Проверяем компоненты
            if self.bybit_client:
                component_health['bybit_client'] = await self.bybit_client.test_connection()
            
            if self.data_manager:
                data_health = await self.data_manager.health_check()
                component_health['data_manager'] = data_health.get('status') == 'healthy'
            
            if self.signal_processor:
                signal_health = await self.signal_processor.health_check()
                component_health['signal_processor'] = signal_health.get('status') == 'healthy'
            
            # Общее здоровье
            all_healthy = all(component_health.values())
            
            return {
                'status': 'healthy' if all_healthy and self._is_running else 'unhealthy',
                'engine_status': self.status.value,
                'is_running': self._is_running,
                'components': component_health,
                'active_tasks': len([t for t in self._trading_tasks if not t.done()]),
                'positions': len(self.position_manager.positions),
                'daily_trades': self.daily_trades_count,
                'current_balance': self.current_balance
            }
            
        except Exception as e:
            return {
                'status': 'error',
                'error': str(e)
            }
    
    # ========================================================================
    # DEPENDENCY INJECTION
    # ========================================================================
    
    def set_bybit_client(self, client) -> None:
        """Установка Bybit клиента"""
        self.bybit_client = client
        self.logger.info("📡 Bybit client injected")
    
    def set_data_manager(self, manager) -> None:
        """Установка менеджера данных"""
        self.data_manager = manager
        self.logger.info("📊 Data manager injected")
    
    def set_signal_processor(self, processor) -> None:
        """Установка процессора сигналов"""
        self.signal_processor = processor
        self.logger.info("🎯 Signal processor injected")
    
    def set_websocket_manager(self, manager) -> None:
        """Установка WebSocket менеджера"""
        self.websocket_manager = manager
        self.logger.info("🌐 WebSocket manager injected")
    
    # ========================================================================
    # MAGIC METHODS
    # ========================================================================
    
    def __str__(self) -> str:
        return f"TradingEngine(status={self.status.value}, mode={self.trading_mode.value})"
    
    def __repr__(self) -> str:
        return (f"TradingEngine(status='{self.status.value}', "
                f"mode='{self.trading_mode.value}', "
                f"balance={self.current_balance:.2f}, "
                f"positions={len(self.position_manager.positions)}, "
                f"running={self._is_running})")


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def create_trading_engine(
    settings: Optional[Settings] = None,
    database: Optional[Database] = None
) -> TradingEngine:
    """Создание экземпляра Trading Engine"""
    return TradingEngine(settings, database)


async def create_configured_engine(
    bybit_client=None,
    data_manager=None, 
    signal_processor=None,
    websocket_manager=None,
    settings: Optional[Settings] = None,
    database: Optional[Database] = None
) -> TradingEngine:
    """Создание полностью настроенного Trading Engine"""
    engine = create_trading_engine(settings, database)
    
    if bybit_client:
        engine.set_bybit_client(bybit_client)
    if data_manager:
        engine.set_data_manager(data_manager)
    if signal_processor:
        engine.set_signal_processor(signal_processor)
    if websocket_manager:
        engine.set_websocket_manager(websocket_manager)
    
    return engine


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    async def test_trading_engine():
        """Тестирование Trading Engine"""
        print("🧪 Testing Trading Engine")
        print("=" * 50)
        
        try:
            # Создаем движок
            engine = create_trading_engine()
            
            # Mock компоненты для тестирования
            # В реальности они будут инжектированы
            
            # Тестируем статус
            print(f"✅ Engine status: {engine.status.value}")
            
            # Health check
            health = await engine.health_check()
            print(f"🏥 Health: {health['status']}")
            
            # Статистика
            stats = engine.get_trading_stats()
            print(f"📊 Stats: {stats}")
            
            print("✅ Trading Engine test completed")
            
        except Exception as e:
            print(f"❌ Test failed: {e}")
            import traceback
            traceback.print_exc()
    
    # Запуск теста
    asyncio.run(test_trading_engine())
