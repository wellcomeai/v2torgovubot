"""
Trading Bot Base Strategy
Базовый класс для всех торговых стратегий
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional, Tuple, Union
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone
import pandas as pd
import numpy as np

from utils.logger import setup_logger
from utils.helpers import safe_float, get_current_timestamp, validate_symbol, validate_timeframe
from utils.indicators import IndicatorResult, IndicatorType
from data.historical_data import CandleData
from app.config.settings import get_settings


# ============================================================================
# ENUMS AND TYPES
# ============================================================================

class SignalType(str, Enum):
    """Типы торговых сигналов"""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    STRONG_BUY = "STRONG_BUY"
    STRONG_SELL = "STRONG_SELL"


class SignalStrength(str, Enum):
    """Сила сигнала"""
    WEAK = "WEAK"           # 0.5-0.6
    MODERATE = "MODERATE"   # 0.6-0.75
    STRONG = "STRONG"       # 0.75-0.9
    VERY_STRONG = "VERY_STRONG"  # 0.9+


class StrategyState(str, Enum):
    """Состояния стратегии"""
    INACTIVE = "INACTIVE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


# ============================================================================
# DATACLASSES
# ============================================================================

@dataclass
class StrategyResult:
    """Результат работы стратегии"""
    signal_type: SignalType
    confidence: float  # 0.0 - 1.0
    strength: SignalStrength
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reasoning: str = ""
    indicators_data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: int = field(default_factory=get_current_timestamp)
    
    def __post_init__(self):
        # Автоматически определяем силу сигнала по confidence
        if self.strength is None:
            if self.confidence >= 0.9:
                self.strength = SignalStrength.VERY_STRONG
            elif self.confidence >= 0.75:
                self.strength = SignalStrength.STRONG
            elif self.confidence >= 0.6:
                self.strength = SignalStrength.MODERATE
            else:
                self.strength = SignalStrength.WEAK
    
    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь для сохранения в БД"""
        return {
            'signal_type': self.signal_type.value,
            'confidence': self.confidence,
            'strength': self.strength.value,
            'entry_price': self.entry_price,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'reasoning': self.reasoning,
            'indicators_data': self.indicators_data,
            'metadata': self.metadata,
            'timestamp': self.timestamp
        }


@dataclass
class StrategyConfig:
    """Конфигурация стратегии"""
    name: str
    symbol: str
    timeframe: str
    parameters: Dict[str, Any]
    enabled: bool = True
    
    # Risk management
    max_position_size: float = 1000.0
    stop_loss_pct: float = 0.02  # 2%
    take_profit_pct: float = 0.04  # 4%
    
    # Signal filtering
    min_confidence: float = 0.7
    cooldown_minutes: int = 5
    max_signals_per_hour: int = 10
    
    def __post_init__(self):
        # Валидация
        if not validate_symbol(self.symbol):
            raise ValueError(f"Invalid symbol: {self.symbol}")
        
        if not validate_timeframe(self.timeframe):
            raise ValueError(f"Invalid timeframe: {self.timeframe}")
        
        if not (0 < self.min_confidence <= 1):
            raise ValueError("min_confidence must be between 0 and 1")


@dataclass  
class StrategyPerformance:
    """Метрики производительности стратегии"""
    total_signals: int = 0
    profitable_signals: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_profit_per_trade: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    last_signal_time: Optional[int] = None
    
    def update_metrics(self, pnl: float) -> None:
        """Обновление метрик после закрытия позиции"""
        self.total_signals += 1
        self.total_pnl += pnl
        
        if pnl > 0:
            self.profitable_signals += 1
        
        # Пересчитываем метрики
        if self.total_signals > 0:
            self.win_rate = self.profitable_signals / self.total_signals
            self.avg_profit_per_trade = self.total_pnl / self.total_signals


# ============================================================================
# BASE STRATEGY CLASS
# ============================================================================

class BaseStrategy(ABC):
    """
    Базовый абстрактный класс для всех торговых стратегий
    
    Все стратегии должны наследовать от этого класса и реализовывать
    метод analyze() для генерации торговых сигналов
    """
    
    def __init__(self, config: StrategyConfig):
        self.config = config
        self.settings = get_settings()
        self.logger = setup_logger(f"strategies.{self.__class__.__name__}")
        
        # Состояние стратегии
        self.state = StrategyState.INACTIVE
        self.last_signal_time = 0
        self.signals_this_hour = 0
        self.hour_start = 0
        
        # Метрики производительности
        self.performance = StrategyPerformance()
        
        # Кеш данных и индикаторов
        self._data_cache: List[CandleData] = []
        self._indicators_cache: Dict[str, IndicatorResult] = {}
        self._cache_timestamp = 0
        self._cache_ttl = 60000  # 1 минута
        
        # История сигналов (для анализа)
        self.signal_history: List[StrategyResult] = []
        self.max_history_size = 1000
        
        self.logger.info(f"🎯 Strategy initialized: {self.name} for {config.symbol}")
    
    # ========================================================================
    # PROPERTIES
    # ========================================================================
    
    @property 
    def name(self) -> str:
        """Имя стратегии"""
        return self.__class__.__name__
    
    @property
    def symbol(self) -> str:
        """Торговый символ"""
        return self.config.symbol
    
    @property
    def timeframe(self) -> str:
        """Таймфрейм"""
        return self.config.timeframe
    
    @property
    def is_active(self) -> bool:
        """Активна ли стратегия"""
        return self.state == StrategyState.ACTIVE and self.config.enabled
    
    @property
    def parameters(self) -> Dict[str, Any]:
        """Параметры стратегии"""
        return self.config.parameters
    
    # ========================================================================
    # ABSTRACT METHODS
    # ========================================================================
    
    @abstractmethod
    async def analyze(self, data: List[CandleData]) -> StrategyResult:
        """
        Основной метод анализа данных и генерации сигналов
        
        Args:
            data: Список исторических свечей (от старых к новым)
            
        Returns:
            StrategyResult с сигналом и метаданными
        """
        pass
    
    @abstractmethod
    def get_required_history_length(self) -> int:
        """
        Минимальное количество исторических свечей для работы стратегии
        
        Returns:
            Количество свечей
        """
        pass
    
    @abstractmethod
    def validate_parameters(self) -> Tuple[bool, List[str]]:
        """
        Валидация параметров стратегии
        
        Returns:
            (is_valid, errors_list)
        """
        pass
    
    # ========================================================================
    # PUBLIC METHODS
    # ========================================================================
    
    async def process_market_data(self, data: List[CandleData]) -> Optional[StrategyResult]:
        """
        Обработка новых рыночных данных и генерация сигнала
        
        Args:
            data: Список свечей
            
        Returns:
            StrategyResult если есть сигнал, иначе None
        """
        try:
            if not self.is_active:
                return None
            
            # Проверяем достаточно ли данных
            if len(data) < self.get_required_history_length():
                self.logger.debug(f"📊 Not enough data: {len(data)} < {self.get_required_history_length()}")
                return None
            
            # Проверяем cooldown период
            if not self._check_cooldown():
                return None
            
            # Проверяем лимит сигналов в час
            if not self._check_hourly_limit():
                return None
            
            # Кешируем данные
            self._update_data_cache(data)
            
            # Анализируем и получаем результат
            with self._performance_timer():
                result = await self.analyze(data)
            
            # Валидируем результат
            if not self._validate_result(result):
                return None
            
            # Фильтруем по confidence
            if result.confidence < self.config.min_confidence:
                self.logger.debug(f"📊 Signal confidence too low: {result.confidence} < {self.config.min_confidence}")
                return None
            
            # Обновляем метаданные
            result.metadata.update({
                'strategy_name': self.name,
                'symbol': self.symbol,
                'timeframe': self.timeframe,
                'data_length': len(data),
                'last_price': data[-1].close_price if data else None
            })
            
            # Обновляем статистику
            self._update_signal_stats(result)
            
            # Добавляем в историю
            self._add_to_history(result)
            
            self.logger.info(
                f"🎯 Signal generated: {result.signal_type} "
                f"(confidence: {result.confidence:.2f}, price: {result.entry_price})"
            )
            
            return result
            
        except Exception as e:
            self.state = StrategyState.ERROR
            self.logger.error(f"❌ Strategy processing error: {e}")
            raise
    
    def start(self) -> None:
        """Запуск стратегии"""
        try:
            # Валидируем параметры
            is_valid, errors = self.validate_parameters()
            if not is_valid:
                raise ValueError(f"Invalid parameters: {errors}")
            
            self.state = StrategyState.ACTIVE
            self.logger.info(f"▶️ Strategy started: {self.name}")
            
        except Exception as e:
            self.state = StrategyState.ERROR
            self.logger.error(f"❌ Failed to start strategy: {e}")
            raise
    
    def stop(self) -> None:
        """Остановка стратегии"""
        self.state = StrategyState.INACTIVE
        self.logger.info(f"⏹️ Strategy stopped: {self.name}")
    
    def pause(self) -> None:
        """Пауза стратегии"""
        self.state = StrategyState.PAUSED
        self.logger.info(f"⏸️ Strategy paused: {self.name}")
    
    def resume(self) -> None:
        """Возобновление стратегии"""
        self.state = StrategyState.ACTIVE
        self.logger.info(f"▶️ Strategy resumed: {self.name}")
    
    def update_parameters(self, new_params: Dict[str, Any]) -> None:
        """Обновление параметров стратегии"""
        try:
            old_params = self.config.parameters.copy()
            self.config.parameters.update(new_params)
            
            # Валидируем новые параметры
            is_valid, errors = self.validate_parameters()
            if not is_valid:
                # Возвращаем старые параметры
                self.config.parameters = old_params
                raise ValueError(f"Invalid parameters: {errors}")
            
            # Очищаем кеш при изменении параметров
            self.clear_cache()
            
            self.logger.info(f"🔄 Parameters updated: {new_params}")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to update parameters: {e}")
            raise
    
    def clear_cache(self) -> None:
        """Очистка кешей стратегии"""
        self._data_cache.clear()
        self._indicators_cache.clear()
        self._cache_timestamp = 0
        self.logger.debug("🧹 Strategy cache cleared")
    
    def get_status(self) -> Dict[str, Any]:
        """Получение статуса стратегии"""
        return {
            'name': self.name,
            'symbol': self.symbol,
            'timeframe': self.timeframe,
            'state': self.state.value,
            'enabled': self.config.enabled,
            'last_signal_time': self.last_signal_time,
            'signals_this_hour': self.signals_this_hour,
            'performance': {
                'total_signals': self.performance.total_signals,
                'win_rate': round(self.performance.win_rate * 100, 2),
                'total_pnl': round(self.performance.total_pnl, 2),
                'avg_profit_per_trade': round(self.performance.avg_profit_per_trade, 2)
            },
            'parameters': self.parameters
        }
    
    def get_recent_signals(self, count: int = 10) -> List[Dict[str, Any]]:
        """Получение последних сигналов"""
        recent_signals = self.signal_history[-count:] if self.signal_history else []
        return [signal.to_dict() for signal in recent_signals]
    
    # ========================================================================
    # HELPER METHODS
    # ========================================================================
    
    def calculate_stop_loss(self, entry_price: float, signal_type: SignalType) -> float:
        """Расчет стоп-лосса"""
        if signal_type in [SignalType.BUY, SignalType.STRONG_BUY]:
            return entry_price * (1 - self.config.stop_loss_pct)
        else:  # SELL signals
            return entry_price * (1 + self.config.stop_loss_pct)
    
    def calculate_take_profit(self, entry_price: float, signal_type: SignalType) -> float:
        """Расчет тейк-профита"""
        if signal_type in [SignalType.BUY, SignalType.STRONG_BUY]:
            return entry_price * (1 + self.config.take_profit_pct)
        else:  # SELL signals
            return entry_price * (1 - self.config.take_profit_pct)
    
    def get_latest_price(self, data: List[CandleData]) -> float:
        """Получение последней цены"""
        if not data:
            raise ValueError("No data available")
        return data[-1].close_price
    
    def calculate_price_change_pct(self, data: List[CandleData], periods: int = 1) -> float:
        """Расчет изменения цены в процентах"""
        if len(data) <= periods:
            return 0.0
        
        current_price = data[-1].close_price
        old_price = data[-periods - 1].close_price
        
        if old_price == 0:
            return 0.0
        
        return ((current_price - old_price) / old_price) * 100
    
    def data_to_dataframe(self, data: List[CandleData]) -> pd.DataFrame:
        """Конвертация данных в DataFrame для анализа"""
        if not data:
            return pd.DataFrame()
        
        df_data = []
        for candle in data:
            df_data.append({
                'timestamp': candle.open_time,
                'open': candle.open_price,
                'high': candle.high_price,
                'low': candle.low_price,
                'close': candle.close_price,
                'volume': candle.volume
            })
        
        df = pd.DataFrame(df_data)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)
        
        return df
    
    # ========================================================================
    # PRIVATE METHODS
    # ========================================================================
    
    def _check_cooldown(self) -> bool:
        """Проверка cooldown периода между сигналами"""
        if self.last_signal_time == 0:
            return True
        
        current_time = get_current_timestamp()
        cooldown_ms = self.config.cooldown_minutes * 60 * 1000
        
        return (current_time - self.last_signal_time) >= cooldown_ms
    
    def _check_hourly_limit(self) -> bool:
        """Проверка лимита сигналов в час"""
        current_time = get_current_timestamp()
        current_hour = current_time // (60 * 60 * 1000)
        
        if current_hour != self.hour_start:
            # Новый час - сбрасываем счетчик
            self.hour_start = current_hour
            self.signals_this_hour = 0
        
        return self.signals_this_hour < self.config.max_signals_per_hour
    
    def _update_data_cache(self, data: List[CandleData]) -> None:
        """Обновление кеша данных"""
        self._data_cache = data[-self.get_required_history_length():]
        self._cache_timestamp = get_current_timestamp()
    
    def _validate_result(self, result: StrategyResult) -> bool:
        """Валидация результата стратегии"""
        if not isinstance(result, StrategyResult):
            self.logger.error("❌ Result must be StrategyResult instance")
            return False
        
        if not (0 <= result.confidence <= 1):
            self.logger.error(f"❌ Invalid confidence: {result.confidence}")
            return False
        
        if result.signal_type not in SignalType:
            self.logger.error(f"❌ Invalid signal type: {result.signal_type}")
            return False
        
        return True
    
    def _update_signal_stats(self, result: StrategyResult) -> None:
        """Обновление статистики сигналов"""
        current_time = get_current_timestamp()
        self.last_signal_time = current_time
        self.signals_this_hour += 1
        self.performance.last_signal_time = current_time
    
    def _add_to_history(self, result: StrategyResult) -> None:
        """Добавление результата в историю"""
        self.signal_history.append(result)
        
        # Ограничиваем размер истории
        if len(self.signal_history) > self.max_history_size:
            self.signal_history = self.signal_history[-self.max_history_size:]
    
    def _performance_timer(self):
        """Контекстный менеджер для измерения производительности"""
        from contextlib import contextmanager
        import time
        
        @contextmanager
        def timer():
            start = time.perf_counter()
            try:
                yield
            finally:
                duration = time.perf_counter() - start
                self.logger.debug(f"⏱️ Analysis took {duration:.4f}s")
        
        return timer()
    
    def __str__(self) -> str:
        return f"{self.name}({self.symbol}, {self.timeframe})"
    
    def __repr__(self) -> str:
        return (f"{self.__class__.__name__}(symbol='{self.symbol}', "
                f"timeframe='{self.timeframe}', state='{self.state.value}')")


# ============================================================================
# UTILITY FUNCTIONS FOR STRATEGIES
# ============================================================================

def create_strategy_result(
    signal_type: Union[str, SignalType],
    confidence: float,
    entry_price: float = None,
    reasoning: str = "",
    **kwargs
) -> StrategyResult:
    """Вспомогательная функция для создания StrategyResult"""
    
    if isinstance(signal_type, str):
        signal_type = SignalType(signal_type.upper())
    
    return StrategyResult(
        signal_type=signal_type,
        confidence=confidence,
        entry_price=entry_price,
        reasoning=reasoning,
        **kwargs
    )


def combine_signals(signals: List[StrategyResult]) -> StrategyResult:
    """
    Комбинирование нескольких сигналов в один
    Используется для мульти-индикаторных стратегий
    """
    if not signals:
        return create_strategy_result(SignalType.HOLD, 0.0, reasoning="No signals to combine")
    
    if len(signals) == 1:
        return signals[0]
    
    # Подсчитываем сигналы по типам
    signal_counts = {signal_type: 0 for signal_type in SignalType}
    total_confidence = 0.0
    
    for signal in signals:
        signal_counts[signal.signal_type] += 1
        total_confidence += signal.confidence
    
    # Определяем доминирующий сигнал
    max_count = max(signal_counts.values())
    dominant_signals = [st for st, count in signal_counts.items() if count == max_count]
    
    # Если несколько равных по количеству сигналов, берем по приоритету
    signal_priority = [SignalType.STRONG_BUY, SignalType.BUY, SignalType.STRONG_SELL, 
                      SignalType.SELL, SignalType.HOLD]
    
    for signal_type in signal_priority:
        if signal_type in dominant_signals:
            combined_signal = signal_type
            break
    else:
        combined_signal = SignalType.HOLD
    
    # Средняя confidence
    avg_confidence = total_confidence / len(signals)
    
    # Берем цену от первого сигнала с entry_price
    entry_price = None
    for signal in signals:
        if signal.entry_price is not None:
            entry_price = signal.entry_price
            break
    
    return create_strategy_result(
        signal_type=combined_signal,
        confidence=avg_confidence,
        entry_price=entry_price,
        reasoning=f"Combined {len(signals)} signals: {[s.signal_type.value for s in signals]}",
        metadata={'combined_signals': [s.to_dict() for s in signals]}
    )


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Пример простой стратегии для демонстрации
    
    class ExampleStrategy(BaseStrategy):
        """Пример простой стратегии"""
        
        async def analyze(self, data: List[CandleData]) -> StrategyResult:
            """Простой анализ на основе изменения цены"""
            if len(data) < 2:
                return create_strategy_result(SignalType.HOLD, 0.0)
            
            # Простая логика: если цена выросла более чем на 1%, сигнал SELL
            # если упала более чем на 1%, сигнал BUY
            price_change = self.calculate_price_change_pct(data, 1)
            current_price = self.get_latest_price(data)
            
            if price_change > 1.0:
                return create_strategy_result(
                    SignalType.SELL,
                    confidence=min(0.8, abs(price_change) / 5.0),
                    entry_price=current_price,
                    reasoning=f"Price increased by {price_change:.2f}%"
                )
            elif price_change < -1.0:
                return create_strategy_result(
                    SignalType.BUY,
                    confidence=min(0.8, abs(price_change) / 5.0),
                    entry_price=current_price,
                    reasoning=f"Price decreased by {price_change:.2f}%"
                )
            else:
                return create_strategy_result(
                    SignalType.HOLD,
                    confidence=0.5,
                    reasoning=f"Price change too small: {price_change:.2f}%"
                )
        
        def get_required_history_length(self) -> int:
            return 2  # Нужны минимум 2 свечи
        
        def validate_parameters(self) -> Tuple[bool, List[str]]:
            return True, []  # Простая стратегия без параметров
    
    # Тестирование
    print("🧪 Testing Base Strategy")
    print("=" * 50)
    
    # Создаем конфигурацию
    config = StrategyConfig(
        name="example_strategy",
        symbol="BTCUSDT",
        timeframe="5m",
        parameters={}
    )
    
    # Создаем стратегию
    strategy = ExampleStrategy(config)
    print(f"✅ Strategy created: {strategy}")
    
    # Запускаем
    strategy.start()
    print(f"✅ Strategy started, state: {strategy.state}")
    
    # Получаем статус
    status = strategy.get_status()
    print(f"✅ Strategy status: {status}")
    
    print("\n✅ Base strategy test completed!")
