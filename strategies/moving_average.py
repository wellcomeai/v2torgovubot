"""
Moving Average Crossover Strategy
Стратегия пересечения скользящих средних
"""

from typing import List, Dict, Any, Tuple
import numpy as np

from .base_strategy import BaseStrategy, StrategyResult, SignalType, create_strategy_result
from .strategy_registry import register_strategy
from data.historical_data import CandleData
from utils.indicators import sma, ema, detect_crossover, IndicatorResult
from utils.helpers import safe_float


# ============================================================================
# MOVING AVERAGE STRATEGY
# ============================================================================

@register_strategy(
    name="moving_average",
    description="Classic moving average crossover strategy with configurable periods and MA types",
    version="1.2.0", 
    author="Trading Bot Team",
    category="Trend Following",
    parameters_schema={
        "fast_period": {
            "type": "integer",
            "description": "Period for fast moving average",
            "default": 10,
            "min": 2,
            "max": 100
        },
        "slow_period": {
            "type": "integer", 
            "description": "Period for slow moving average",
            "default": 20,
            "min": 5,
            "max": 200
        },
        "ma_type": {
            "type": "string",
            "description": "Type of moving average (sma, ema)",
            "default": "sma",
            "choices": ["sma", "ema"]
        },
        "source": {
            "type": "string",
            "description": "Price source (close, open, high, low)",
            "default": "close",
            "choices": ["close", "open", "high", "low"]
        },
        "signal_smoothing": {
            "type": "boolean",
            "description": "Enable signal smoothing to reduce noise",
            "default": True
        },
        "trend_filter": {
            "type": "boolean",
            "description": "Only trade in direction of longer trend",
            "default": False
        },
        "trend_period": {
            "type": "integer",
            "description": "Period for trend filter MA",
            "default": 50,
            "min": 10,
            "max": 500
        }
    },
    supported_timeframes=["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
)
class MovingAverageStrategy(BaseStrategy):
    """
    Стратегия пересечения скользящих средних
    
    Логика:
    - BUY сигнал: когда быстрая MA пересекает медленную MA снизу вверх
    - SELL сигнал: когда быстрая MA пересекает медленную MA сверху вниз
    
    Дополнительные фильтры:
    - Сглаживание сигналов для уменьшения шума
    - Фильтр по направлению тренда (опционально)
    - Подтверждение объемом (опционально)
    """
    
    def __init__(self, config):
        super().__init__(config)
        
        # Параметры стратегии
        self.fast_period = self.parameters.get("fast_period", 10)
        self.slow_period = self.parameters.get("slow_period", 20)
        self.ma_type = self.parameters.get("ma_type", "sma").lower()
        self.source = self.parameters.get("source", "close").lower()
        self.signal_smoothing = self.parameters.get("signal_smoothing", True)
        self.trend_filter = self.parameters.get("trend_filter", False)
        self.trend_period = self.parameters.get("trend_period", 50)
        
        # Внутреннее состояние
        self.last_crossover_bar = -1  # Индекс последнего пересечения
        self.previous_fast_ma = None
        self.previous_slow_ma = None
        
        # Кеш индикаторов
        self._ma_cache = {}
        
        self.logger.info(
            f"🎯 MA Strategy initialized: {self.fast_period}/{self.slow_period} {self.ma_type.upper()} "
            f"on {self.source}, smoothing: {self.signal_smoothing}"
        )
    
    async def analyze(self, data: List[CandleData]) -> StrategyResult:
        """
        Основной анализ данных для генерации сигналов
        
        Args:
            data: Список исторических свечей
            
        Returns:
            StrategyResult с торговым сигналом
        """
        try:
            # Проверяем достаточность данных
            if len(data) < self.get_required_history_length():
                return create_strategy_result(
                    SignalType.HOLD,
                    0.0,
                    reasoning=f"Not enough data: {len(data)} < {self.get_required_history_length()}"
                )
            
            # Извлекаем цены
            prices = self._extract_price_data(data)
            current_price = prices[-1]
            
            # Вычисляем скользящие средние
            fast_ma_result = self._calculate_moving_average(prices, self.fast_period)
            slow_ma_result = self._calculate_moving_average(prices, self.slow_period)
            
            if not fast_ma_result.values or not slow_ma_result.values:
                return create_strategy_result(
                    SignalType.HOLD,
                    0.0,
                    reasoning="Failed to calculate moving averages"
                )
            
            fast_ma_values = fast_ma_result.values
            slow_ma_values = slow_ma_result.values
            
            # Выравниваем длины массивов
            min_length = min(len(fast_ma_values), len(slow_ma_values))
            fast_ma_values = fast_ma_values[-min_length:]
            slow_ma_values = slow_ma_values[-min_length:]
            
            if min_length < 2:
                return create_strategy_result(
                    SignalType.HOLD,
                    0.0,
                    reasoning="Not enough MA values for crossover detection"
                )
            
            # Текущие значения MA
            current_fast_ma = fast_ma_values[-1]
            current_slow_ma = slow_ma_values[-1]
            
            # Предыдущие значения MA
            prev_fast_ma = fast_ma_values[-2]
            prev_slow_ma = slow_ma_values[-2]
            
            # Обнаружение пересечений
            crossover_info = self._detect_ma_crossover(
                fast_ma_values, slow_ma_values, 
                prev_fast_ma, prev_slow_ma,
                current_fast_ma, current_slow_ma
            )
            
            # Определяем базовый сигнал
            base_signal = crossover_info['signal_type']
            base_confidence = crossover_info['confidence']
            
            if base_signal == SignalType.HOLD:
                return create_strategy_result(
                    SignalType.HOLD,
                    base_confidence,
                    entry_price=current_price,
                    reasoning=crossover_info['reasoning']
                )
            
            # Применяем фильтры
            filtered_result = await self._apply_filters(
                data, base_signal, base_confidence, 
                current_price, current_fast_ma, current_slow_ma
            )
            
            # Добавляем stop loss и take profit
            if filtered_result.signal_type != SignalType.HOLD:
                filtered_result.stop_loss = self.calculate_stop_loss(current_price, filtered_result.signal_type)
                filtered_result.take_profit = self.calculate_take_profit(current_price, filtered_result.signal_type)
            
            # Добавляем данные индикаторов
            filtered_result.indicators_data = {
                'fast_ma': current_fast_ma,
                'slow_ma': current_slow_ma,
                'fast_period': self.fast_period,
                'slow_period': self.slow_period,
                'ma_type': self.ma_type,
                'crossover_strength': crossover_info['strength'],
                'trend_alignment': crossover_info.get('trend_aligned', True)
            }
            
            # Сохраняем состояние для следующего анализа
            self.previous_fast_ma = current_fast_ma
            self.previous_slow_ma = current_slow_ma
            
            self.logger.debug(
                f"📊 MA Analysis: Fast MA={current_fast_ma:.2f}, Slow MA={current_slow_ma:.2f}, "
                f"Signal={filtered_result.signal_type}, Confidence={filtered_result.confidence:.2f}"
            )
            
            return filtered_result
            
        except Exception as e:
            self.logger.error(f"❌ MA Strategy analysis failed: {e}")
            return create_strategy_result(
                SignalType.HOLD,
                0.0,
                reasoning=f"Analysis error: {str(e)}"
            )
    
    def _extract_price_data(self, data: List[CandleData]) -> List[float]:
        """Извлечение ценовых данных по заданному источнику"""
        if self.source == "close":
            return [candle.close_price for candle in data]
        elif self.source == "open":
            return [candle.open_price for candle in data]
        elif self.source == "high":
            return [candle.high_price for candle in data]
        elif self.source == "low":
            return [candle.low_price for candle in data]
        else:
            # По умолчанию close
            return [candle.close_price for candle in data]
    
    def _calculate_moving_average(self, prices: List[float], period: int) -> IndicatorResult:
        """Вычисление скользящей средней"""
        cache_key = f"{self.ma_type}_{period}_{len(prices)}"
        
        # Проверяем кеш
        if cache_key in self._ma_cache:
            return self._ma_cache[cache_key]
        
        try:
            if self.ma_type == "sma":
                result = sma(prices, period)
            elif self.ma_type == "ema":
                result = ema(prices, period)
            else:
                # По умолчанию SMA
                result = sma(prices, period)
            
            # Кешируем результат
            self._ma_cache[cache_key] = result
            return result
            
        except Exception as e:
            self.logger.error(f"❌ Failed to calculate {self.ma_type.upper()} {period}: {e}")
            return IndicatorResult(
                name=f"{self.ma_type.upper()}_{period}",
                type="trend",
                values=[]
            )
    
    def _detect_ma_crossover(
        self, 
        fast_values: List[float], 
        slow_values: List[float],
        prev_fast: float,
        prev_slow: float, 
        curr_fast: float,
        curr_slow: float
    ) -> Dict[str, Any]:
        """Обнаружение пересечения скользящих средних"""
        
        crossover_info = {
            'signal_type': SignalType.HOLD,
            'confidence': 0.5,
            'strength': 0.0,
            'reasoning': "No crossover detected"
        }
        
        try:
            # Используем функцию обнаружения пересечений
            crossovers = detect_crossover(fast_values, slow_values)
            
            # Проверяем последние пересечения
            recent_bullish = crossovers['bullish'][-5:] if crossovers['bullish'] else []
            recent_bearish = crossovers['bearish'][-5:] if crossovers['bearish'] else []
            
            # Определяем текущее пересечение
            bullish_crossover = (prev_fast <= prev_slow and curr_fast > curr_slow)
            bearish_crossover = (prev_fast >= prev_slow and curr_fast < curr_slow)
            
            if bullish_crossover:
                # Бычье пересечение (быстрая пересекает медленную снизу вверх)
                strength = self._calculate_crossover_strength(curr_fast, curr_slow, "bullish")
                confidence = min(0.9, 0.6 + strength * 0.3)
                
                crossover_info.update({
                    'signal_type': SignalType.BUY,
                    'confidence': confidence,
                    'strength': strength,
                    'reasoning': f"Bullish MA crossover: Fast MA ({curr_fast:.2f}) > Slow MA ({curr_slow:.2f})"
                })
                
            elif bearish_crossover:
                # Медвежье пересечение (быстрая пересекает медленную сверху вниз)
                strength = self._calculate_crossover_strength(curr_fast, curr_slow, "bearish")
                confidence = min(0.9, 0.6 + strength * 0.3)
                
                crossover_info.update({
                    'signal_type': SignalType.SELL,
                    'confidence': confidence,
                    'strength': strength,
                    'reasoning': f"Bearish MA crossover: Fast MA ({curr_fast:.2f}) < Slow MA ({curr_slow:.2f})"
                })
            
            # Дополнительная логика для определения силы тренда
            if crossover_info['signal_type'] != SignalType.HOLD:
                # Чем больше расстояние между MA, тем сильнее сигнал
                ma_distance = abs(curr_fast - curr_slow) / curr_slow
                if ma_distance > 0.01:  # Больше 1% разницы
                    crossover_info['confidence'] *= 1.2  # Увеличиваем confidence
                    crossover_info['confidence'] = min(crossover_info['confidence'], 0.95)
            
            return crossover_info
            
        except Exception as e:
            self.logger.error(f"❌ Crossover detection failed: {e}")
            return crossover_info
    
    def _calculate_crossover_strength(self, fast_ma: float, slow_ma: float, direction: str) -> float:
        """Расчет силы пересечения"""
        try:
            # Расстояние между MA как процент от цены
            distance_pct = abs(fast_ma - slow_ma) / max(fast_ma, slow_ma)
            
            # Нормализуем силу от 0 до 1
            strength = min(1.0, distance_pct * 50)  # 2% разницы = сила 1.0
            
            return strength
            
        except Exception:
            return 0.5
    
    async def _apply_filters(
        self,
        data: List[CandleData],
        signal: SignalType,
        confidence: float,
        current_price: float,
        fast_ma: float,
        slow_ma: float
    ) -> StrategyResult:
        """Применение дополнительных фильтров к сигналу"""
        
        filtered_confidence = confidence
        reasoning_parts = []
        
        try:
            # Фильтр сглаживания сигналов
            if self.signal_smoothing:
                smoothing_factor = self._apply_signal_smoothing(data)
                filtered_confidence *= smoothing_factor
                reasoning_parts.append(f"smoothing_factor={smoothing_factor:.2f}")
            
            # Фильтр направления тренда
            if self.trend_filter:
                trend_alignment = await self._check_trend_alignment(data, signal)
                if not trend_alignment['aligned']:
                    filtered_confidence *= 0.5  # Снижаем уверенность
                    reasoning_parts.append("against_trend")
                else:
                    reasoning_parts.append("with_trend")
            
            # Проверка волатильности
            volatility_factor = self._check_volatility(data)
            filtered_confidence *= volatility_factor
            reasoning_parts.append(f"volatility_factor={volatility_factor:.2f}")
            
            # Формируем итоговый reasoning
            base_reasoning = f"MA crossover: fast={fast_ma:.2f}, slow={slow_ma:.2f}"
            if reasoning_parts:
                full_reasoning = f"{base_reasoning}, filters: {', '.join(reasoning_parts)}"
            else:
                full_reasoning = base_reasoning
            
            # Определяем финальный сигнал
            if filtered_confidence < self.config.min_confidence:
                final_signal = SignalType.HOLD
                full_reasoning += f" (confidence {filtered_confidence:.2f} < threshold {self.config.min_confidence})"
            else:
                final_signal = signal
            
            return create_strategy_result(
                signal_type=final_signal,
                confidence=min(filtered_confidence, 0.95),  # Максимум 95%
                entry_price=current_price,
                reasoning=full_reasoning
            )
            
        except Exception as e:
            self.logger.error(f"❌ Filter application failed: {e}")
            return create_strategy_result(
                SignalType.HOLD,
                0.0,
                reasoning=f"Filter error: {str(e)}"
            )
    
    def _apply_signal_smoothing(self, data: List[CandleData]) -> float:
        """Сглаживание сигналов для уменьшения шума"""
        try:
            if len(data) < 5:
                return 1.0
            
            # Анализируем последние несколько свечей на консистентность движения
            recent_prices = [candle.close_price for candle in data[-5:]]
            
            # Считаем направление движения
            up_moves = 0
            down_moves = 0
            
            for i in range(1, len(recent_prices)):
                if recent_prices[i] > recent_prices[i-1]:
                    up_moves += 1
                elif recent_prices[i] < recent_prices[i-1]:
                    down_moves += 1
            
            # Если движение консистентное, увеличиваем коэффициент
            total_moves = up_moves + down_moves
            if total_moves > 0:
                consistency = max(up_moves, down_moves) / total_moves
                return 0.8 + (consistency * 0.4)  # От 0.8 до 1.2
            
            return 1.0
            
        except Exception:
            return 1.0
    
    async def _check_trend_alignment(self, data: List[CandleData], signal: SignalType) -> Dict[str, Any]:
        """Проверка выравнивания сигнала с общим трендом"""
        try:
            if len(data) < self.trend_period:
                return {'aligned': True, 'trend_direction': 'unknown'}
            
            # Вычисляем долгосрочную MA для определения тренда
            prices = self._extract_price_data(data)
            trend_ma_result = self._calculate_moving_average(prices, self.trend_period)
            
            if not trend_ma_result.values or len(trend_ma_result.values) < 2:
                return {'aligned': True, 'trend_direction': 'unknown'}
            
            trend_ma_values = trend_ma_result.values
            current_trend_ma = trend_ma_values[-1]
            prev_trend_ma = trend_ma_values[-2]
            current_price = prices[-1]
            
            # Определяем направление тренда
            if current_trend_ma > prev_trend_ma and current_price > current_trend_ma:
                trend_direction = 'up'
            elif current_trend_ma < prev_trend_ma and current_price < current_trend_ma:
                trend_direction = 'down'
            else:
                trend_direction = 'sideways'
            
            # Проверяем выравнивание
            aligned = False
            if signal in [SignalType.BUY, SignalType.STRONG_BUY] and trend_direction == 'up':
                aligned = True
            elif signal in [SignalType.SELL, SignalType.STRONG_SELL] and trend_direction == 'down':
                aligned = True
            elif trend_direction == 'sideways':
                aligned = True  # В боковом тренде разрешаем любые сигналы
            
            return {
                'aligned': aligned,
                'trend_direction': trend_direction,
                'trend_ma': current_trend_ma
            }
            
        except Exception as e:
            self.logger.error(f"❌ Trend alignment check failed: {e}")
            return {'aligned': True, 'trend_direction': 'unknown'}
    
    def _check_volatility(self, data: List[CandleData]) -> float:
        """Проверка волатильности рынка"""
        try:
            if len(data) < 10:
                return 1.0
            
            # Берем последние 10 свечей
            recent_data = data[-10:]
            price_changes = []
            
            for i in range(1, len(recent_data)):
                change = abs(recent_data[i].close_price - recent_data[i-1].close_price) / recent_data[i-1].close_price
                price_changes.append(change)
            
            if not price_changes:
                return 1.0
            
            # Средняя волатильность
            avg_volatility = np.mean(price_changes)
            
            # Адаптируем коэффициент в зависимости от волатильности
            if avg_volatility > 0.03:  # Высокая волатильность (>3%)
                return 0.8  # Снижаем уверенность
            elif avg_volatility < 0.005:  # Низкая волатильность (<0.5%)
                return 0.9  # Небольшое снижение уверенности
            else:
                return 1.0  # Нормальная волатильность
                
        except Exception:
            return 1.0
    
    def get_required_history_length(self) -> int:
        """Минимальное количество свечей для работы стратегии"""
        # Нужно достаточно данных для самой медленной MA плюс запас для фильтров
        base_requirement = max(self.slow_period, 20)
        
        if self.trend_filter:
            base_requirement = max(base_requirement, self.trend_period)
        
        # Добавляем запас для расчетов
        return base_requirement + 10
    
    def validate_parameters(self) -> Tuple[bool, List[str]]:
        """Валидация параметров стратегии"""
        errors = []
        
        try:
            # Проверка периодов
            if not isinstance(self.fast_period, int) or self.fast_period < 1:
                errors.append("fast_period must be a positive integer")
            
            if not isinstance(self.slow_period, int) or self.slow_period < 1:
                errors.append("slow_period must be a positive integer")
            
            if self.fast_period >= self.slow_period:
                errors.append("fast_period must be less than slow_period")
            
            # Проверка типа MA
            if self.ma_type not in ["sma", "ema"]:
                errors.append("ma_type must be 'sma' or 'ema'")
            
            # Проверка источника цены
            if self.source not in ["close", "open", "high", "low"]:
                errors.append("source must be one of: close, open, high, low")
            
            # Проверка тренд фильтра
            if self.trend_filter:
                if not isinstance(self.trend_period, int) or self.trend_period < 1:
                    errors.append("trend_period must be a positive integer")
                
                if self.trend_period <= self.slow_period:
                    errors.append("trend_period should be greater than slow_period")
            
            return len(errors) == 0, errors
            
        except Exception as e:
            errors.append(f"Parameter validation error: {str(e)}")
            return False, errors
    
    def get_parameter_info(self) -> Dict[str, Any]:
        """Получение информации о текущих параметрах"""
        return {
            'fast_period': self.fast_period,
            'slow_period': self.slow_period,
            'ma_type': self.ma_type,
            'source': self.source,
            'signal_smoothing': self.signal_smoothing,
            'trend_filter': self.trend_filter,
            'trend_period': self.trend_period if self.trend_filter else None,
            'required_history': self.get_required_history_length()
        }
    
    def __str__(self) -> str:
        return f"MovingAverageStrategy({self.fast_period}/{self.slow_period} {self.ma_type.upper()})"


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    import asyncio
    from .base_strategy import StrategyConfig
    from data.historical_data import CandleData
    from utils.helpers import get_current_timestamp
    
    async def test_moving_average_strategy():
        """Тестирование стратегии скользящих средних"""
        print("🧪 Testing Moving Average Strategy")
        print("=" * 50)
        
        try:
            # Создаем конфигурацию
            config = StrategyConfig(
                name="moving_average",
                symbol="BTCUSDT",
                timeframe="5m",
                parameters={
                    "fast_period": 10,
                    "slow_period": 20,
                    "ma_type": "sma",
                    "source": "close",
                    "signal_smoothing": True,
                    "trend_filter": False
                }
            )
            
            # Создаем стратегию
            strategy = MovingAverageStrategy(config)
            print(f"✅ Strategy created: {strategy}")
            
            # Валидируем параметры
            is_valid, errors = strategy.validate_parameters()
            if is_valid:
                print("✅ Parameters are valid")
            else:
                print(f"❌ Parameter errors: {errors}")
                return
            
            # Создаем тестовые данные
            test_data = []
            base_price = 50000.0
            timestamp = get_current_timestamp() - (100 * 5 * 60 * 1000)  # 100 свечей по 5 минут
            
            for i in range(100):
                # Имитируем тренд с шумом
                trend = i * 50  # Восходящий тренд
                noise = (i % 10 - 5) * 100  # Случайный шум
                price = base_price + trend + noise
                
                candle = CandleData(
                    symbol="BTCUSDT",
                    timeframe="5m",
                    open_time=timestamp + i * 5 * 60 * 1000,
                    close_time=timestamp + (i + 1) * 5 * 60 * 1000 - 1,
                    open_price=price,
                    high_price=price * 1.01,
                    low_price=price * 0.99,
                    close_price=price,
                    volume=1000.0
                )
                test_data.append(candle)
            
            print(f"📊 Created {len(test_data)} test candles")
            
            # Запускаем стратегию
            strategy.start()
            print("▶️ Strategy started")
            
            # Анализируем данные
            result = await strategy.process_market_data(test_data)
            
            if result:
                print(f"🎯 Signal generated:")
                print(f"   Type: {result.signal_type}")
                print(f"   Confidence: {result.confidence:.2f}")
                print(f"   Entry Price: {result.entry_price}")
                print(f"   Stop Loss: {result.stop_loss}")
                print(f"   Take Profit: {result.take_profit}")
                print(f"   Reasoning: {result.reasoning}")
                print(f"   Indicators: {result.indicators_data}")
            else:
                print("📊 No signal generated")
            
            # Получаем статус стратегии
            status = strategy.get_status()
            print(f"\n📈 Strategy Status:")
            print(f"   State: {status['state']}")
            print(f"   Total Signals: {status['performance']['total_signals']}")
            
            # Тестируем с разными параметрами
            print(f"\n🔄 Testing parameter update...")
            strategy.update_parameters({"fast_period": 5, "slow_period": 15})
            print(f"✅ Parameters updated: {strategy.get_parameter_info()}")
            
            print("\n✅ Moving Average Strategy test completed!")
            
        except Exception as e:
            print(f"❌ Test failed: {e}")
            import traceback
            traceback.print_exc()
    
    # Запуск тестирования
    asyncio.run(test_moving_average_strategy())
