"""
Trading Bot Backtest Engine
Движок для бэктестинга торговых стратегий с детальной аналитикой
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple, Union
from dataclasses import dataclass, field
from enum import Enum
import math
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid

from utils.logger import setup_logger
from utils.helpers import (
    get_current_timestamp, safe_float, timestamp_to_datetime,
    parse_timeframe_to_milliseconds, Timer
)
from app.config.settings import Settings, get_settings
from data.database import Database
from data.models import BacktestResult, BacktestTrade, BacktestStatus
from data.historical_data import CandleData
from strategies.base_strategy import BaseStrategy, StrategyResult, SignalType, StrategyConfig
from strategies.strategy_registry import StrategyRegistry


# ============================================================================
# CONSTANTS AND ENUMS
# ============================================================================

logger = setup_logger(__name__)

# Константы бэктестинга
DEFAULT_INITIAL_BALANCE = 10000.0
DEFAULT_COMMISSION = 0.001  # 0.1%
DEFAULT_SLIPPAGE = 0.0001  # 0.01%
MIN_TRADE_SIZE = 0.001  # Минимальный размер сделки

# Ограничения
MAX_CONCURRENT_BACKTESTS = 3
MAX_BACKTEST_DURATION_DAYS = 365 * 2  # 2 года
MAX_CANDLES_PER_BACKTEST = 100000

# Периоды для расчета метрик
TRADING_DAYS_PER_YEAR = 252


class BacktestError(Exception):
    """Ошибки бэктестинга"""
    pass


class OrderType(str, Enum):
    """Типы ордеров в бэктесте"""
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


@dataclass
class BacktestPosition:
    """Позиция в бэктесте"""
    symbol: str
    side: str  # "long" / "short"
    size: float
    entry_price: float
    entry_time: int
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    unrealized_pnl: float = 0.0
    
    def update_pnl(self, current_price: float) -> None:
        """Обновление нереализованного PnL"""
        if self.side == "long":
            self.unrealized_pnl = (current_price - self.entry_price) * self.size
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.size


@dataclass
class BacktestTrade:
    """Сделка в бэктесте"""
    symbol: str
    side: str  # "buy" / "sell"
    size: float
    price: float
    timestamp: int
    commission: float
    signal_type: Optional[str] = None
    strategy_confidence: Optional[float] = None
    
    @property
    def notional(self) -> float:
        """Номинал сделки"""
        return self.size * self.price


@dataclass
class BacktestOrder:
    """Ордер в бэктесте"""
    id: str
    symbol: str
    side: str
    size: float
    order_type: OrderType
    price: Optional[float] = None  # None для market orders
    stop_price: Optional[float] = None
    timestamp: int = field(default_factory=get_current_timestamp)
    filled: bool = False
    fill_price: Optional[float] = None
    fill_time: Optional[int] = None


@dataclass
class BacktestPortfolio:
    """Портфель в бэктесте"""
    initial_balance: float
    current_balance: float
    positions: Dict[str, BacktestPosition] = field(default_factory=dict)
    open_orders: Dict[str, BacktestOrder] = field(default_factory=dict)
    
    # История
    trades: List[BacktestTrade] = field(default_factory=list)
    balance_history: List[Tuple[int, float]] = field(default_factory=list)
    
    @property
    def total_exposure(self) -> float:
        """Общая экспозиция"""
        return sum(abs(pos.size * pos.entry_price) for pos in self.positions.values())
    
    @property
    def unrealized_pnl(self) -> float:
        """Общий нереализованный PnL"""
        return sum(pos.unrealized_pnl for pos in self.positions.values())
    
    @property
    def total_equity(self) -> float:
        """Общий капитал"""
        return self.current_balance + self.unrealized_pnl
    
    def add_balance_snapshot(self, timestamp: int) -> None:
        """Добавление снимка баланса"""
        self.balance_history.append((timestamp, self.total_equity))


@dataclass
class BacktestMetrics:
    """Метрики результата бэктеста"""
    # Основные метрики
    total_return: float = 0.0
    annualized_return: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    
    # PnL метрики
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_profit: float = 0.0
    profit_factor: float = 0.0
    
    # Риск-метрики
    max_drawdown: float = 0.0
    max_drawdown_duration: int = 0  # дни
    volatility: float = 0.0
    sharpe_ratio: float = 0.0
    calmar_ratio: float = 0.0
    
    # Торговые метрики
    avg_trade: float = 0.0
    avg_winning_trade: float = 0.0
    avg_losing_trade: float = 0.0
    largest_winning_trade: float = 0.0
    largest_losing_trade: float = 0.0
    
    # Дополнительные метрики
    total_fees: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_trade_duration: float = 0.0  # часы
    
    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь"""
        return {
            'total_return': round(self.total_return, 4),
            'annualized_return': round(self.annualized_return, 4),
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': round(self.win_rate, 4),
            'gross_profit': round(self.gross_profit, 2),
            'gross_loss': round(self.gross_loss, 2),
            'net_profit': round(self.net_profit, 2),
            'profit_factor': round(self.profit_factor, 4),
            'max_drawdown': round(self.max_drawdown, 4),
            'max_drawdown_duration': self.max_drawdown_duration,
            'volatility': round(self.volatility, 4),
            'sharpe_ratio': round(self.sharpe_ratio, 4),
            'calmar_ratio': round(self.calmar_ratio, 4),
            'avg_trade': round(self.avg_trade, 2),
            'avg_winning_trade': round(self.avg_winning_trade, 2),
            'avg_losing_trade': round(self.avg_losing_trade, 2),
            'largest_winning_trade': round(self.largest_winning_trade, 2),
            'largest_losing_trade': round(self.largest_losing_trade, 2),
            'total_fees': round(self.total_fees, 2),
            'max_consecutive_wins': self.max_consecutive_wins,
            'max_consecutive_losses': self.max_consecutive_losses,
            'avg_trade_duration': round(self.avg_trade_duration, 2)
        }


# ============================================================================
# BACKTEST ENGINE CLASS
# ============================================================================

class BacktestEngine:
    """
    Движок для бэктестинга торговых стратегий
    
    Особенности:
    - Реалистичная симуляция исполнения ордеров
    - Учет комиссий и проскальзывания
    - Детальная аналитика результатов
    - Поддержка различных типов стратегий
    - Параллельное выполнение бэктестов
    """
    
    def __init__(
        self,
        database: Optional[Database] = None,
        bybit_client=None,
        data_manager=None,
        settings: Optional[Settings] = None
    ):
        self.database = database
        self.bybit_client = bybit_client
        self.data_manager = data_manager
        self.settings = settings or get_settings()
        self.logger = setup_logger(f"{__name__}.BacktestEngine")
        
        # Настройки бэктестинга
        self.max_concurrent = min(
            self.settings.BACKTEST_MAX_CONCURRENT,
            MAX_CONCURRENT_BACKTESTS
        )
        self.timeout = self.settings.BACKTEST_TIMEOUT
        
        # Executor для параллельных вычислений
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrent,
            thread_name_prefix="BacktestEngine"
        )
        
        # Активные бэктесты
        self._active_backtests: Dict[str, asyncio.Task] = {}
        
        # Статистика
        self.stats = {
            'total_backtests': 0,
            'successful_backtests': 0,
            'failed_backtests': 0,
            'avg_execution_time': 0.0
        }
        
        self.logger.info("🧪 Backtest Engine initialized")
    
    # ========================================================================
    # PUBLIC API METHODS
    # ========================================================================
    
    async def run_backtest(
        self,
        strategy_name: str,
        symbol: str,
        start_date: Union[str, datetime],
        end_date: Union[str, datetime],
        timeframe: str = "5m",
        strategy_params: Optional[Dict[str, Any]] = None,
        initial_balance: float = DEFAULT_INITIAL_BALANCE,
        commission: float = DEFAULT_COMMISSION,
        slippage: float = DEFAULT_SLIPPAGE,
        save_to_db: bool = True
    ) -> Dict[str, Any]:
        """
        Запуск бэктеста стратегии
        
        Args:
            strategy_name: Имя стратегии из реестра
            symbol: Торговая пара
            start_date: Дата начала
            end_date: Дата окончания
            timeframe: Таймфрейм
            strategy_params: Параметры стратегии
            initial_balance: Начальный баланс
            commission: Комиссия за сделку
            slippage: Проскальзывание
            save_to_db: Сохранять результаты в БД
            
        Returns:
            Результаты бэктеста
        """
        backtest_id = str(uuid.uuid4())
        
        try:
            self.logger.info(f"🧪 Starting backtest: {strategy_name} on {symbol} ({start_date} - {end_date})")
            
            with Timer(f"backtest_{strategy_name}_{symbol}"):
                # Валидация параметров
                self._validate_backtest_params(
                    strategy_name, symbol, start_date, end_date, timeframe, initial_balance
                )
                
                # Нормализуем даты
                start_ts, end_ts = self._normalize_dates(start_date, end_date)
                
                # Получаем исторические данные
                historical_data = await self._get_historical_data(symbol, timeframe, start_ts, end_ts)
                
                if len(historical_data) < 20:
                    raise BacktestError(f"Insufficient historical data: {len(historical_data)} candles")
                
                # Создаем стратегию
                strategy = self._create_strategy(strategy_name, symbol, timeframe, strategy_params or {})
                
                # Запускаем бэктест
                result = await self._execute_backtest(
                    backtest_id=backtest_id,
                    strategy=strategy,
                    symbol=symbol,
                    timeframe=timeframe,
                    historical_data=historical_data,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    initial_balance=initial_balance,
                    commission=commission,
                    slippage=slippage
                )
                
                # Сохраняем результаты
                if save_to_db and self.database:
                    await self._save_backtest_results(result)
                
                self.stats['total_backtests'] += 1
                self.stats['successful_backtests'] += 1
                
                self.logger.info(f"✅ Backtest completed: return={result['total_return']:.2%}, trades={result['total_trades']}")
                
                return result
                
        except Exception as e:
            self.stats['failed_backtests'] += 1
            self.logger.error(f"❌ Backtest failed: {e}")
            
            # Сохраняем ошибку в БД
            if save_to_db and self.database:
                await self._save_failed_backtest(backtest_id, strategy_name, symbol, str(e))
            
            raise BacktestError(f"Backtest failed: {e}") from e
        
        finally:
            # Очищаем активные бэктесты
            if backtest_id in self._active_backtests:
                del self._active_backtests[backtest_id]
    
    async def run_multiple_backtests(
        self,
        backtest_configs: List[Dict[str, Any]],
        max_concurrent: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Запуск нескольких бэктестов параллельно
        
        Args:
            backtest_configs: Список конфигураций бэктестов
            max_concurrent: Максимум параллельных бэктестов
            
        Returns:
            Список результатов
        """
        max_concurrent = min(max_concurrent or self.max_concurrent, len(backtest_configs))
        
        self.logger.info(f"🧪 Running {len(backtest_configs)} backtests (max_concurrent={max_concurrent})")
        
        results = []
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def run_single_backtest(config: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                try:
                    return await self.run_backtest(**config)
                except Exception as e:
                    self.logger.error(f"❌ Parallel backtest failed: {e}")
                    return {'error': str(e), 'config': config}
        
        # Создаем задачи
        tasks = [run_single_backtest(config) for config in backtest_configs]
        
        # Выполняем с таймаутом
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self.timeout * len(backtest_configs)
            )
        except asyncio.TimeoutError:
            self.logger.error(f"❌ Multiple backtests timeout: {self.timeout * len(backtest_configs)}s")
            results = [{'error': 'Timeout'} for _ in backtest_configs]
        
        successful = sum(1 for r in results if isinstance(r, dict) and 'error' not in r)
        self.logger.info(f"✅ Multiple backtests completed: {successful}/{len(backtest_configs)} successful")
        
        return results
    
    async def optimize_strategy(
        self,
        strategy_name: str,
        symbol: str,
        start_date: Union[str, datetime],
        end_date: Union[str, datetime],
        parameter_ranges: Dict[str, List[Any]],
        timeframe: str = "5m",
        optimization_metric: str = "total_return",
        max_combinations: int = 100
    ) -> Dict[str, Any]:
        """
        Оптимизация параметров стратегии
        
        Args:
            strategy_name: Имя стратегии
            symbol: Торговая пара
            start_date: Дата начала
            end_date: Дата окончания
            parameter_ranges: Диапазоны параметров для оптимизации
            timeframe: Таймфрейм
            optimization_metric: Метрика для оптимизации
            max_combinations: Максимум комбинаций параметров
            
        Returns:
            Результаты оптимизации
        """
        self.logger.info(f"🔧 Starting strategy optimization: {strategy_name}")
        
        try:
            # Генерируем комбинации параметров
            param_combinations = self._generate_parameter_combinations(
                parameter_ranges, max_combinations
            )
            
            self.logger.info(f"🔧 Generated {len(param_combinations)} parameter combinations")
            
            # Создаем конфигурации бэктестов
            backtest_configs = []
            for i, params in enumerate(param_combinations):
                config = {
                    'strategy_name': strategy_name,
                    'symbol': symbol,
                    'start_date': start_date,
                    'end_date': end_date,
                    'timeframe': timeframe,
                    'strategy_params': params,
                    'save_to_db': False  # Не сохраняем промежуточные результаты
                }
                backtest_configs.append(config)
            
            # Запускаем параллельные бэктесты
            results = await self.run_multiple_backtests(backtest_configs)
            
            # Анализируем результаты
            optimization_results = self._analyze_optimization_results(
                results, param_combinations, optimization_metric
            )
            
            # Сохраняем лучший результат
            if optimization_results['best_result'] and self.database:
                best_result = optimization_results['best_result']
                best_result['strategy_parameters']['_optimized'] = True
                await self._save_backtest_results(best_result)
            
            self.logger.info(f"✅ Optimization completed: best {optimization_metric}={optimization_results['best_metric']:.4f}")
            
            return optimization_results
            
        except Exception as e:
            self.logger.error(f"❌ Strategy optimization failed: {e}")
            raise BacktestError(f"Optimization failed: {e}") from e
    
    # ========================================================================
    # CORE BACKTEST EXECUTION
    # ========================================================================
    
    async def _execute_backtest(
        self,
        backtest_id: str,
        strategy: BaseStrategy,
        symbol: str,
        timeframe: str,
        historical_data: List[CandleData],
        start_ts: int,
        end_ts: int,
        initial_balance: float,
        commission: float,
        slippage: float
    ) -> Dict[str, Any]:
        """Выполнение основной логики бэктеста"""
        
        # Инициализация портфеля
        portfolio = BacktestPortfolio(
            initial_balance=initial_balance,
            current_balance=initial_balance
        )
        
        # Минимальная длина истории для стратегии
        required_history = strategy.get_required_history_length()
        
        # Метрики для отслеживания
        equity_curve = []
        drawdown_periods = []
        trade_results = []
        
        self.logger.debug(f"🧪 Processing {len(historical_data)} candles with strategy {strategy.name}")
        
        # Основной цикл бэктестинга
        for i in range(required_history, len(historical_data)):
            current_candle = historical_data[i]
            current_time = current_candle.open_time
            current_price = current_candle.close_price
            
            # История данных для стратегии
            data_slice = historical_data[max(0, i - required_history + 1):i + 1]
            
            try:
                # Обновляем позиции текущими ценами
                self._update_positions(portfolio, {symbol: current_price})
                
                # Проверяем стоп-лоссы и тейк-профиты
                self._check_position_exits(portfolio, symbol, current_price, current_time, trade_results, commission)
                
                # Получаем сигнал от стратегии
                signal = await strategy.process_market_data(data_slice)
                
                if signal and signal.signal_type != SignalType.HOLD:
                    # Обрабатываем торговый сигнал
                    await self._process_trading_signal(
                        portfolio, signal, symbol, current_price, current_time,
                        trade_results, commission, slippage
                    )
                
                # Записываем состояние портфеля
                portfolio.add_balance_snapshot(current_time)
                equity_curve.append((current_time, portfolio.total_equity))
                
            except Exception as e:
                self.logger.warning(f"⚠️ Error processing candle at {current_time}: {e}")
                continue
        
        # Закрываем все открытые позиции в конце
        await self._close_all_positions(portfolio, symbol, historical_data[-1].close_price, 
                                       historical_data[-1].open_time, trade_results, commission)
        
        # Рассчитываем финальные метрики
        metrics = self._calculate_metrics(
            portfolio, equity_curve, trade_results, 
            start_ts, end_ts, initial_balance
        )
        
        # Формируем результат
        result = {
            'backtest_id': backtest_id,
            'strategy_name': strategy.name,
            'symbol': symbol,
            'timeframe': timeframe,
            'start_date': timestamp_to_datetime(start_ts),
            'end_date': timestamp_to_datetime(end_ts),
            'strategy_parameters': strategy.parameters,
            'initial_balance': initial_balance,
            'final_balance': portfolio.current_balance,
            'commission': commission,
            'slippage': slippage,
            'total_trades': len(trade_results),
            'equity_curve': equity_curve,
            'trade_history': trade_results,
            'execution_time': None,  # Будет заполнено в Timer
            **metrics.to_dict()
        }
        
        return result
    
    async def _process_trading_signal(
        self,
        portfolio: BacktestPortfolio,
        signal: StrategyResult,
        symbol: str,
        current_price: float,
        current_time: int,
        trade_results: List[Dict[str, Any]],
        commission: float,
        slippage: float
    ) -> None:
        """Обработка торгового сигнала"""
        
        # Проверяем есть ли уже позиция
        if symbol in portfolio.positions:
            # Логика закрытия/разворота позиции
            existing_position = portfolio.positions[symbol]
            
            # Если сигнал противоположный - закрываем позицию
            if ((existing_position.side == "long" and signal.signal_type in [SignalType.SELL, SignalType.STRONG_SELL]) or
                (existing_position.side == "short" and signal.signal_type in [SignalType.BUY, SignalType.STRONG_BUY])):
                
                await self._close_position(portfolio, symbol, current_price, current_time, 
                                         trade_results, commission, slippage, "signal_reversal")
        
        # Открываем новую позицию
        if signal.signal_type in [SignalType.BUY, SignalType.STRONG_BUY, SignalType.SELL, SignalType.STRONG_SELL]:
            await self._open_position(portfolio, signal, symbol, current_price, current_time, 
                                    trade_results, commission, slippage)
    
    async def _open_position(
        self,
        portfolio: BacktestPortfolio,
        signal: StrategyResult,
        symbol: str,
        current_price: float,
        current_time: int,
        trade_results: List[Dict[str, Any]],
        commission: float,
        slippage: float
    ) -> None:
        """Открытие новой позиции"""
        
        # Определяем сторону позиции
        is_long = signal.signal_type in [SignalType.BUY, SignalType.STRONG_BUY]
        side = "long" if is_long else "short"
        
        # Рассчитываем размер позиции
        position_size = self._calculate_position_size(portfolio, signal, current_price)
        
        if position_size < MIN_TRADE_SIZE:
            return
        
        # Применяем slippage
        execution_price = self._apply_slippage(current_price, is_long, slippage)
        
        # Рассчитываем комиссию
        trade_commission = position_size * execution_price * commission
        
        # Проверяем достаточность баланса
        required_balance = trade_commission
        if not is_long:  # Для шорта нужен дополнительный залог
            required_balance += position_size * execution_price * 0.1  # 10% маржа
        
        if portfolio.current_balance < required_balance:
            self.logger.debug(f"⚠️ Insufficient balance for position: {portfolio.current_balance} < {required_balance}")
            return
        
        # Создаем позицию
        position = BacktestPosition(
            symbol=symbol,
            side=side,
            size=position_size,
            entry_price=execution_price,
            entry_time=current_time,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit
        )
        
        # Обновляем портфель
        portfolio.positions[symbol] = position
        portfolio.current_balance -= trade_commission
        
        # Записываем сделку
        trade = {
            'symbol': symbol,
            'side': 'buy' if is_long else 'sell',
            'size': position_size,
            'price': execution_price,
            'timestamp': current_time,
            'commission': trade_commission,
            'signal_type': signal.signal_type.value,
            'confidence': signal.confidence,
            'trade_type': 'open_position'
        }
        
        trade_results.append(trade)
        
        self.logger.debug(f"📊 Opened {side} position: {symbol} size={position_size:.6f} @ {execution_price:.4f}")
    
    async def _close_position(
        self,
        portfolio: BacktestPortfolio,
        symbol: str,
        current_price: float,
        current_time: int,
        trade_results: List[Dict[str, Any]],
        commission: float,
        slippage: float,
        reason: str = "manual"
    ) -> None:
        """Закрытие позиции"""
        
        if symbol not in portfolio.positions:
            return
        
        position = portfolio.positions[symbol]
        
        # Применяем slippage
        is_closing_long = position.side == "long"
        execution_price = self._apply_slippage(current_price, not is_closing_long, slippage)
        
        # Рассчитываем PnL
        if position.side == "long":
            pnl = (execution_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - execution_price) * position.size
        
        # Рассчитываем комиссию
        trade_commission = position.size * execution_price * commission
        
        # Обновляем баланс
        portfolio.current_balance += pnl - trade_commission
        
        # Записываем сделку
        trade = {
            'symbol': symbol,
            'side': 'sell' if is_closing_long else 'buy',
            'size': position.size,
            'price': execution_price,
            'timestamp': current_time,
            'commission': trade_commission,
            'pnl': pnl,
            'duration_minutes': (current_time - position.entry_time) // (60 * 1000),
            'reason': reason,
            'trade_type': 'close_position'
        }
        
        trade_results.append(trade)
        
        # Удаляем позицию
        del portfolio.positions[symbol]
        
        self.logger.debug(f"📊 Closed {position.side} position: {symbol} PnL={pnl:.2f} reason={reason}")
    
    async def _close_all_positions(
        self,
        portfolio: BacktestPortfolio,
        symbol: str,
        final_price: float,
        final_time: int,
        trade_results: List[Dict[str, Any]],
        commission: float
    ) -> None:
        """Закрытие всех открытых позиций в конце бэктеста"""
        
        for symbol in list(portfolio.positions.keys()):
            await self._close_position(
                portfolio, symbol, final_price, final_time,
                trade_results, commission, 0.0, "backtest_end"
            )
    
    # ========================================================================
    # HELPER METHODS
    # ========================================================================
    
    def _validate_backtest_params(
        self,
        strategy_name: str,
        symbol: str,
        start_date: Union[str, datetime],
        end_date: Union[str, datetime],
        timeframe: str,
        initial_balance: float
    ) -> None:
        """Валидация параметров бэктеста"""
        
        # Проверка стратегии
        if not StrategyRegistry.get_strategy_class(strategy_name):
            raise BacktestError(f"Strategy '{strategy_name}' not found in registry")
        
        # Проверка символа
        if not symbol or len(symbol) < 3:
            raise BacktestError(f"Invalid symbol: {symbol}")
        
        # Проверка дат
        start_ts, end_ts = self._normalize_dates(start_date, end_date)
        
        if start_ts >= end_ts:
            raise BacktestError("Start date must be before end date")
        
        duration_days = (end_ts - start_ts) // (24 * 60 * 60 * 1000)
        if duration_days > MAX_BACKTEST_DURATION_DAYS:
            raise BacktestError(f"Backtest duration too long: {duration_days} > {MAX_BACKTEST_DURATION_DAYS} days")
        
        # Проверка баланса
        if initial_balance <= 0:
            raise BacktestError(f"Initial balance must be positive: {initial_balance}")
        
        # Проверка таймфрейма
        if timeframe not in ['1m', '5m', '15m', '30m', '1h', '4h', '1d']:
            raise BacktestError(f"Unsupported timeframe: {timeframe}")
    
    def _normalize_dates(self, start_date: Union[str, datetime], end_date: Union[str, datetime]) -> Tuple[int, int]:
        """Нормализация дат в миллисекундные timestamps"""
        
        def to_timestamp(date_val: Union[str, datetime]) -> int:
            if isinstance(date_val, str):
                try:
                    if 'T' in date_val:
                        dt = datetime.fromisoformat(date_val.replace('Z', '+00:00'))
                    else:
                        dt = datetime.strptime(date_val, '%Y-%m-%d')
                        dt = dt.replace(tzinfo=timezone.utc)
                except ValueError as e:
                    raise BacktestError(f"Invalid date format: {date_val}") from e
            else:
                dt = date_val
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            
            return int(dt.timestamp() * 1000)
        
        return to_timestamp(start_date), to_timestamp(end_date)
    
    async def _get_historical_data(
        self, 
        symbol: str, 
        timeframe: str, 
        start_ts: int, 
        end_ts: int
    ) -> List[CandleData]:
        """Получение исторических данных"""
        
        try:
            # Сначала пытаемся получить из data_manager
            if self.data_manager:
                # Рассчитываем количество свечей
                timeframe_ms = parse_timeframe_to_milliseconds(timeframe)
                estimated_candles = int((end_ts - start_ts) / timeframe_ms) + 100  # +100 для запаса
                
                data = await self.data_manager.get_market_data(
                    symbol=symbol,
                    timeframe=timeframe,
                    count=min(estimated_candles, MAX_CANDLES_PER_BACKTEST),
                    force_refresh=True
                )
                
                if data:
                    # Фильтруем по времени
                    filtered_data = [
                        candle for candle in data 
                        if start_ts <= candle.open_time <= end_ts
                    ]
                    if len(filtered_data) >= 20:
                        return filtered_data
            
            # Если data_manager недоступен, используем bybit_client напрямую
            if self.bybit_client:
                return await self._fetch_historical_data_from_api(symbol, timeframe, start_ts, end_ts)
            
            raise BacktestError("No data sources available for historical data")
            
        except Exception as e:
            raise BacktestError(f"Failed to fetch historical data: {e}") from e
    
    async def _fetch_historical_data_from_api(
        self, 
        symbol: str, 
        timeframe: str, 
        start_ts: int, 
        end_ts: int
    ) -> List[CandleData]:
        """Получение данных напрямую из API"""
        
        all_data = []
        current_end = end_ts
        timeframe_ms = parse_timeframe_to_milliseconds(timeframe)
        
        while current_end > start_ts and len(all_data) < MAX_CANDLES_PER_BACKTEST:
            try:
                response = await self.bybit_client.get_kline(
                    symbol=symbol,
                    interval=timeframe,
                    start=max(start_ts, current_end - (200 * timeframe_ms)),
                    end=current_end,
                    limit=200
                )
                
                if not response or 'result' not in response or not response['result']['list']:
                    break
                
                # Конвертируем в CandleData
                batch_data = []
                for kline in response['result']['list']:
                    try:
                        candle = CandleData.from_bybit_response(kline, symbol, timeframe)
                        if start_ts <= candle.open_time <= end_ts:
                            batch_data.append(candle)
                    except Exception as e:
                        self.logger.warning(f"⚠️ Failed to parse candle: {e}")
                        continue
                
                if not batch_data:
                    break
                
                all_data.extend(batch_data)
                current_end = min(candle.open_time for candle in batch_data) - timeframe_ms
                
                # Небольшая задержка чтобы не превысить rate limit
                await asyncio.sleep(0.1)
                
            except Exception as e:
                self.logger.warning(f"⚠️ API fetch error: {e}")
                break
        
        # Сортируем по времени
        all_data.sort(key=lambda x: x.open_time)
        
        return all_data
    
    def _create_strategy(
        self, 
        strategy_name: str, 
        symbol: str, 
        timeframe: str, 
        parameters: Dict[str, Any]
    ) -> BaseStrategy:
        """Создание экземпляра стратегии"""
        
        config = StrategyConfig(
            name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            parameters=parameters
        )
        
        strategy = StrategyRegistry.create_strategy(strategy_name, config)
        if not strategy:
            raise BacktestError(f"Failed to create strategy '{strategy_name}'")
        
        # Запускаем стратегию
        strategy.start()
        
        return strategy
    
    def _calculate_position_size(
        self, 
        portfolio: BacktestPortfolio, 
        signal: StrategyResult, 
        current_price: float
    ) -> float:
        """Расчет размера позиции"""
        
        # Базовый расчет - фиксированный процент от баланса
        base_size_usd = portfolio.current_balance * 0.02  # 2% от баланса
        
        # Корректировка по уверенности сигнала
        confidence_multiplier = signal.confidence
        
        # Корректировка по силе сигнала
        strength_multiplier = 1.0
        if signal.signal_type in [SignalType.STRONG_BUY, SignalType.STRONG_SELL]:
            strength_multiplier = 1.5
        
        # Финальный размер
        position_size_usd = base_size_usd * confidence_multiplier * strength_multiplier
        position_size = position_size_usd / current_price
        
        # Ограничиваем максимальным размером (50% баланса)
        max_size_usd = portfolio.current_balance * 0.5
        max_size = max_size_usd / current_price
        
        return min(position_size, max_size)
    
    def _apply_slippage(self, price: float, is_buy: bool, slippage: float) -> float:
        """Применение проскальзывания"""
        if is_buy:
            return price * (1 + slippage)  # Покупаем дороже
        else:
            return price * (1 - slippage)  # Продаем дешевле
    
    def _update_positions(self, portfolio: BacktestPortfolio, prices: Dict[str, float]) -> None:
        """Обновление текущих цен в позициях"""
        for symbol, position in portfolio.positions.items():
            if symbol in prices:
                position.update_pnl(prices[symbol])
    
    def _check_position_exits(
        self,
        portfolio: BacktestPortfolio,
        symbol: str,
        current_price: float,
        current_time: int,
        trade_results: List[Dict[str, Any]],
        commission: float
    ) -> None:
        """Проверка условий выхода из позиций (стоп-лосс, тейк-профит)"""
        
        if symbol not in portfolio.positions:
            return
        
        position = portfolio.positions[symbol]
        should_close = False
        reason = ""
        
        if position.side == "long":
            # Для лонга: стоп-лосс ниже цены входа, тейк-профит выше
            if position.stop_loss and current_price <= position.stop_loss:
                should_close = True
                reason = "stop_loss"
            elif position.take_profit and current_price >= position.take_profit:
                should_close = True
                reason = "take_profit"
        
        else:  # short
            # Для шорта: стоп-лосс выше цены входа, тейк-профит ниже
            if position.stop_loss and current_price >= position.stop_loss:
                should_close = True
                reason = "stop_loss"
            elif position.take_profit and current_price <= position.take_profit:
                should_close = True
                reason = "take_profit"
        
        if should_close:
            asyncio.create_task(
                self._close_position(
                    portfolio, symbol, current_price, current_time,
                    trade_results, commission, 0.0, reason
                )
            )
    
    def _calculate_metrics(
        self,
        portfolio: BacktestPortfolio,
        equity_curve: List[Tuple[int, float]],
        trade_results: List[Dict[str, Any]],
        start_ts: int,
        end_ts: int,
        initial_balance: float
    ) -> BacktestMetrics:
        """Расчет метрик бэктеста"""
        
        metrics = BacktestMetrics()
        
        if not trade_results:
            return metrics
        
        # Базовые метрики
        final_balance = portfolio.current_balance
        metrics.total_return = (final_balance - initial_balance) / initial_balance
        
        # Аннуализированная доходность
        duration_days = (end_ts - start_ts) / (24 * 60 * 60 * 1000)
        if duration_days > 0:
            metrics.annualized_return = (final_balance / initial_balance) ** (365.25 / duration_days) - 1
        
        # Торговые метрики
        trades = [t for t in trade_results if t.get('trade_type') == 'close_position']
        metrics.total_trades = len(trades)
        
        if trades:
            pnls = [t.get('pnl', 0) for t in trades]
            profits = [pnl for pnl in pnls if pnl > 0]
            losses = [pnl for pnl in pnls if pnl < 0]
            
            metrics.winning_trades = len(profits)
            metrics.losing_trades = len(losses)
            metrics.win_rate = metrics.winning_trades / metrics.total_trades if metrics.total_trades > 0 else 0
            
            # PnL метрики
            metrics.gross_profit = sum(profits)
            metrics.gross_loss = sum(losses)
            metrics.net_profit = sum(pnls)
            metrics.profit_factor = abs(metrics.gross_profit / metrics.gross_loss) if metrics.gross_loss < 0 else float('inf')
            
            # Средние значения
            metrics.avg_trade = sum(pnls) / len(pnls)
            metrics.avg_winning_trade = sum(profits) / len(profits) if profits else 0
            metrics.avg_losing_trade = sum(losses) / len(losses) if losses else 0
            metrics.largest_winning_trade = max(profits) if profits else 0
            metrics.largest_losing_trade = min(losses) if losses else 0
            
            # Комиссии
            metrics.total_fees = sum(t.get('commission', 0) for t in trade_results)
            
            # Длительность сделок
            durations = [t.get('duration_minutes', 0) for t in trades if t.get('duration_minutes')]
            metrics.avg_trade_duration = sum(durations) / len(durations) / 60 if durations else 0  # в часах
        
        # Риск-метрики на основе equity curve
        if equity_curve:
            self._calculate_risk_metrics(metrics, equity_curve, initial_balance)
        
        return metrics
    
    def _calculate_risk_metrics(
        self,
        metrics: BacktestMetrics,
        equity_curve: List[Tuple[int, float]],
        initial_balance: float
    ) -> None:
        """Расчет риск-метрик"""
        
        if len(equity_curve) < 2:
            return
        
        equity_values = [equity for _, equity in equity_curve]
        
        # Максимальная просадка
        peak = equity_values[0]
        max_drawdown = 0.0
        
        for equity in equity_values:
            if equity > peak:
                peak = equity
            drawdown = (peak - equity) / peak if peak > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)
        
        metrics.max_drawdown = max_drawdown
        
        # Волатильность
        if len(equity_values) > 1:
            returns = []
            for i in range(1, len(equity_values)):
                if equity_values[i-1] != 0:
                    daily_return = (equity_values[i] - equity_values[i-1]) / equity_values[i-1]
                    returns.append(daily_return)
            
            if returns:
                metrics.volatility = np.std(returns) * np.sqrt(252) if len(returns) > 1 else 0
                
                # Sharpe Ratio (предполагаем risk-free rate = 0)
                avg_return = np.mean(returns) * 252  # Аннуализированный
                metrics.sharpe_ratio = avg_return / metrics.volatility if metrics.volatility > 0 else 0
                
                # Calmar Ratio
                metrics.calmar_ratio = metrics.annualized_return / max_drawdown if max_drawdown > 0 else 0
    
    def _generate_parameter_combinations(
        self, 
        parameter_ranges: Dict[str, List[Any]], 
        max_combinations: int
    ) -> List[Dict[str, Any]]:
        """Генерация комбинаций параметров для оптимизации"""
        
        import itertools
        
        param_names = list(parameter_ranges.keys())
        param_values = list(parameter_ranges.values())
        
        # Генерируем все возможные комбинации
        all_combinations = list(itertools.product(*param_values))
        
        # Ограничиваем количество комбинаций
        if len(all_combinations) > max_combinations:
            # Берем равномерно распределенные комбинации
            step = len(all_combinations) // max_combinations
            all_combinations = all_combinations[::step][:max_combinations]
        
        # Конвертируем в список словарей
        combinations = []
        for combo in all_combinations:
            param_dict = dict(zip(param_names, combo))
            combinations.append(param_dict)
        
        return combinations
    
    def _analyze_optimization_results(
        self,
        results: List[Dict[str, Any]],
        param_combinations: List[Dict[str, Any]],
        optimization_metric: str
    ) -> Dict[str, Any]:
        """Анализ результатов оптимизации"""
        
        valid_results = []
        
        for i, result in enumerate(results):
            if isinstance(result, dict) and 'error' not in result:
                result['parameters'] = param_combinations[i]
                valid_results.append(result)
        
        if not valid_results:
            return {
                'best_result': None,
                'best_metric': None,
                'best_parameters': None,
                'total_combinations': len(param_combinations),
                'successful_combinations': 0,
                'results_summary': []
            }
        
        # Сортируем по метрике оптимизации
        valid_results.sort(key=lambda x: x.get(optimization_metric, 0), reverse=True)
        
        best_result = valid_results[0]
        
        # Создаем сводку результатов
        results_summary = []
        for result in valid_results[:10]:  # Топ 10 результатов
            results_summary.append({
                'parameters': result['parameters'],
                'metric_value': result.get(optimization_metric, 0),
                'total_trades': result.get('total_trades', 0),
                'win_rate': result.get('win_rate', 0),
                'total_return': result.get('total_return', 0)
            })
        
        return {
            'best_result': best_result,
            'best_metric': best_result.get(optimization_metric, 0),
            'best_parameters': best_result['parameters'],
            'total_combinations': len(param_combinations),
            'successful_combinations': len(valid_results),
            'results_summary': results_summary
        }
    
    async def _save_backtest_results(self, result: Dict[str, Any]) -> None:
        """Сохранение результатов бэктеста в БД"""
        
        if not self.database:
            return
        
        try:
            backtest_data = {
                'strategy_name': result['strategy_name'],
                'symbol': result['symbol'],
                'timeframe': result['timeframe'],
                'start_date': result['start_date'],
                'end_date': result['end_date'],
                'strategy_parameters': result['strategy_parameters'],
                'initial_balance': result['initial_balance'],
                'commission': result['commission'],
                'slippage': result['slippage'],
                'final_balance': result['final_balance'],
                'total_return': result['total_return'],
                'annualized_return': result.get('annualized_return'),
                'sharpe_ratio': result.get('sharpe_ratio'),
                'max_drawdown': result.get('max_drawdown'),
                'total_trades': result['total_trades'],
                'winning_trades': result.get('winning_trades', 0),
                'win_rate': result.get('win_rate'),
                'status': BacktestStatus.COMPLETED,
                'execution_time': result.get('execution_time'),
                'trades_data': result.get('trade_history', [])
            }
            
            await self.database.save_backtest_result(backtest_data)
            self.logger.debug(f"💾 Backtest results saved to database")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to save backtest results: {e}")
    
    async def _save_failed_backtest(
        self, 
        backtest_id: str, 
        strategy_name: str, 
        symbol: str, 
        error_message: str
    ) -> None:
        """Сохранение информации о неудачном бэктесте"""
        
        if not self.database:
            return
        
        try:
            backtest_data = {
                'strategy_name': strategy_name,
                'symbol': symbol,
                'timeframe': '5m',  # default
                'start_date': datetime.now(timezone.utc),
                'end_date': datetime.now(timezone.utc),
                'strategy_parameters': {},
                'initial_balance': 0.0,
                'status': BacktestStatus.FAILED,
                'error_message': error_message[:500]  # Ограничиваем длину
            }
            
            await self.database.save_backtest_result(backtest_data)
            
        except Exception as e:
            self.logger.error(f"❌ Failed to save failed backtest info: {e}")
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    def get_active_backtests(self) -> List[str]:
        """Получение списка активных бэктестов"""
        return list(self._active_backtests.keys())
    
    async def cancel_backtest(self, backtest_id: str) -> bool:
        """Отмена активного бэктеста"""
        if backtest_id in self._active_backtests:
            task = self._active_backtests[backtest_id]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            del self._active_backtests[backtest_id]
            self.logger.info(f"🚫 Backtest cancelled: {backtest_id}")
            return True
        return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики движка"""
        return {
            **self.stats,
            'active_backtests': len(self._active_backtests),
            'max_concurrent': self.max_concurrent
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Проверка здоровья движка"""
        return {
            'status': 'healthy',
            'active_backtests': len(self._active_backtests),
            'executor_running': not self._executor._shutdown,
            'database_available': self.database is not None,
            'data_source_available': (self.data_manager is not None) or (self.bybit_client is not None)
        }
    
    async def cleanup(self) -> None:
        """Очистка ресурсов"""
        try:
            # Отменяем активные бэктесты
            for backtest_id in list(self._active_backtests.keys()):
                await self.cancel_backtest(backtest_id)
            
            # Закрываем executor
            self._executor.shutdown(wait=True)
            
            self.logger.info("🧹 Backtest Engine cleanup completed")
            
        except Exception as e:
            self.logger.error(f"❌ Cleanup error: {e}")


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    import asyncio
    
    async def test_backtest_engine():
        """Тестирование Backtest Engine"""
        print("🧪 Testing Backtest Engine")
        print("=" * 50)
        
        try:
            # Создаем движок
            engine = BacktestEngine()
            
            # Health check
            health = await engine.health_check()
            print(f"🏥 Health: {health['status']}")
            
            # Статистика
            stats = engine.get_stats()
            print(f"📊 Stats: {stats}")
            
            print("✅ Backtest Engine test completed")
            
        except Exception as e:
            print(f"❌ Test failed: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            await engine.cleanup()
    
    # Запуск теста
    asyncio.run(test_backtest_engine())
