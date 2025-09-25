"""
Trading Bot Technical Indicators
Набор технических индикаторов для анализа рыночных данных
"""

import numpy as np
import pandas as pd
from typing import Union, List, Dict, Tuple, Optional, Any
from dataclasses import dataclass
from enum import Enum
import warnings
warnings.filterwarnings('ignore')

from utils.logger import setup_logger
from utils.helpers import safe_float, validate_price


# ============================================================================
# TYPES AND CONSTANTS
# ============================================================================

logger = setup_logger(__name__)

# Типы данных
PriceData = Union[List[float], np.ndarray, pd.Series]
OHLCData = Union[pd.DataFrame, Dict[str, List[float]]]


class IndicatorType(str, Enum):
    """Типы индикаторов"""
    TREND = "trend"
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    VOLUME = "volume"
    SUPPORT_RESISTANCE = "support_resistance"


@dataclass
class IndicatorResult:
    """Результат вычисления индикатора"""
    name: str
    type: IndicatorType
    values: Union[float, List[float], Dict[str, float]]
    timestamp: Optional[int] = None
    parameters: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'type': self.type.value,
            'values': self.values,
            'timestamp': self.timestamp,
            'parameters': self.parameters or {}
        }


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def _validate_data(data: PriceData, min_length: int = 1) -> np.ndarray:
    """Валидация и конвертация входных данных"""
    if data is None:
        raise ValueError("Data cannot be None")
    
    if isinstance(data, (list, tuple)):
        arr = np.array([safe_float(x) for x in data])
    elif isinstance(data, pd.Series):
        arr = data.values
    elif isinstance(data, np.ndarray):
        arr = data.copy()
    else:
        raise ValueError(f"Unsupported data type: {type(data)}")
    
    if len(arr) < min_length:
        raise ValueError(f"Not enough data points. Need at least {min_length}, got {len(arr)}")
    
    # Удаляем NaN и inf
    arr = arr[~np.isnan(arr)]
    arr = arr[~np.isinf(arr)]
    
    if len(arr) < min_length:
        raise ValueError(f"Not enough valid data points after cleaning")
    
    return arr


def _prepare_ohlc_data(data: OHLCData) -> Dict[str, np.ndarray]:
    """Подготовка OHLC данных"""
    if isinstance(data, pd.DataFrame):
        # Пробуем стандартные имена колонок
        column_mapping = {
            'open': ['open', 'Open', 'OPEN', 'o'],
            'high': ['high', 'High', 'HIGH', 'h'],
            'low': ['low', 'Low', 'LOW', 'l'],
            'close': ['close', 'Close', 'CLOSE', 'c'],
            'volume': ['volume', 'Volume', 'VOLUME', 'vol', 'v']
        }
        
        ohlc = {}
        for key, possible_names in column_mapping.items():
            for name in possible_names:
                if name in data.columns:
                    ohlc[key] = _validate_data(data[name])
                    break
            if key not in ohlc and key != 'volume':  # volume опциональный
                raise ValueError(f"Column '{key}' not found in DataFrame")
        
        return ohlc
        
    elif isinstance(data, dict):
        ohlc = {}
        required_keys = ['open', 'high', 'low', 'close']
        
        for key in required_keys:
            if key not in data:
                raise ValueError(f"Key '{key}' not found in data dictionary")
            ohlc[key] = _validate_data(data[key])
        
        if 'volume' in data:
            ohlc['volume'] = _validate_data(data['volume'])
        
        return ohlc
    
    else:
        raise ValueError("OHLC data must be DataFrame or dictionary")


# ============================================================================
# TREND INDICATORS
# ============================================================================

def sma(data: PriceData, period: int = 20) -> IndicatorResult:
    """
    Simple Moving Average (SMA)
    Простая скользящая средняя
    """
    try:
        arr = _validate_data(data, period)
        
        # Используем pandas для эффективного вычисления
        series = pd.Series(arr)
        sma_values = series.rolling(window=period).mean().dropna()
        
        result = IndicatorResult(
            name='SMA',
            type=IndicatorType.TREND,
            values=sma_values.tolist(),
            parameters={'period': period}
        )
        
        logger.debug(f"📊 SMA calculated with period={period}, got {len(sma_values)} values")
        return result
        
    except Exception as e:
        logger.error(f"❌ SMA calculation failed: {e}")
        raise


def ema(data: PriceData, period: int = 20, alpha: float = None) -> IndicatorResult:
    """
    Exponential Moving Average (EMA)
    Экспоненциальная скользящая средняя
    """
    try:
        arr = _validate_data(data, period)
        
        if alpha is None:
            alpha = 2.0 / (period + 1)
        
        # Используем pandas ewm для EMA
        series = pd.Series(arr)
        ema_values = series.ewm(alpha=alpha, adjust=False).mean()
        
        result = IndicatorResult(
            name='EMA',
            type=IndicatorType.TREND,
            values=ema_values.tolist(),
            parameters={'period': period, 'alpha': alpha}
        )
        
        logger.debug(f"📊 EMA calculated with period={period}, alpha={alpha}")
        return result
        
    except Exception as e:
        logger.error(f"❌ EMA calculation failed: {e}")
        raise


def wma(data: PriceData, period: int = 20) -> IndicatorResult:
    """
    Weighted Moving Average (WMA)
    Взвешенная скользящая средняя
    """
    try:
        arr = _validate_data(data, period)
        
        weights = np.arange(1, period + 1)
        wma_values = []
        
        for i in range(period - 1, len(arr)):
            window = arr[i - period + 1:i + 1]
            wma_value = np.dot(window, weights) / weights.sum()
            wma_values.append(wma_value)
        
        result = IndicatorResult(
            name='WMA',
            type=IndicatorType.TREND,
            values=wma_values,
            parameters={'period': period}
        )
        
        logger.debug(f"📊 WMA calculated with period={period}")
        return result
        
    except Exception as e:
        logger.error(f"❌ WMA calculation failed: {e}")
        raise


def macd(
    data: PriceData,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9
) -> IndicatorResult:
    """
    Moving Average Convergence Divergence (MACD)
    """
    try:
        arr = _validate_data(data, slow_period + signal_period)
        
        # Вычисляем EMA для быстрой и медленной линий
        fast_ema = ema(arr, fast_period).values
        slow_ema = ema(arr, slow_period).values
        
        # Выравниваем длины массивов
        min_len = min(len(fast_ema), len(slow_ema))
        fast_ema = fast_ema[-min_len:]
        slow_ema = slow_ema[-min_len:]
        
        # MACD линия
        macd_line = np.array(fast_ema) - np.array(slow_ema)
        
        # Сигнальная линия
        signal_line = ema(macd_line, signal_period).values
        
        # Выравниваем для гистограммы
        min_len = min(len(macd_line), len(signal_line))
        macd_line = macd_line[-min_len:]
        signal_line = signal_line[-min_len:]
        
        # Гистограмма
        histogram = macd_line - signal_line
        
        result = IndicatorResult(
            name='MACD',
            type=IndicatorType.MOMENTUM,
            values={
                'macd': macd_line.tolist(),
                'signal': signal_line,
                'histogram': histogram.tolist()
            },
            parameters={
                'fast_period': fast_period,
                'slow_period': slow_period,
                'signal_period': signal_period
            }
        )
        
        logger.debug(f"📊 MACD calculated with periods {fast_period}/{slow_period}/{signal_period}")
        return result
        
    except Exception as e:
        logger.error(f"❌ MACD calculation failed: {e}")
        raise


# ============================================================================
# MOMENTUM INDICATORS
# ============================================================================

def rsi(data: PriceData, period: int = 14) -> IndicatorResult:
    """
    Relative Strength Index (RSI)
    Индекс относительной силы
    """
    try:
        arr = _validate_data(data, period + 1)
        
        # Вычисляем изменения цен
        deltas = np.diff(arr)
        
        # Разделяем на прибыли и убытки
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        # Используем pandas для скользящих средних
        gains_series = pd.Series(gains)
        losses_series = pd.Series(losses)
        
        avg_gains = gains_series.rolling(window=period).mean()
        avg_losses = losses_series.rolling(window=period).mean()
        
        # Вычисляем RSI
        rs = avg_gains / avg_losses
        rsi_values = 100 - (100 / (1 + rs))
        
        # Удаляем NaN значения
        rsi_values = rsi_values.dropna().tolist()
        
        result = IndicatorResult(
            name='RSI',
            type=IndicatorType.MOMENTUM,
            values=rsi_values,
            parameters={'period': period}
        )
        
        logger.debug(f"📊 RSI calculated with period={period}")
        return result
        
    except Exception as e:
        logger.error(f"❌ RSI calculation failed: {e}")
        raise


def stochastic(
    ohlc_data: OHLCData,
    k_period: int = 14,
    d_period: int = 3
) -> IndicatorResult:
    """
    Stochastic Oscillator
    Стохастический осциллятор
    """
    try:
        ohlc = _prepare_ohlc_data(ohlc_data)
        min_len = max(k_period, len(ohlc['high']))
        
        high = ohlc['high']
        low = ohlc['low'] 
        close = ohlc['close']
        
        # Убеждаемся что все массивы одной длины
        min_length = min(len(high), len(low), len(close))
        high = high[:min_length]
        low = low[:min_length]
        close = close[:min_length]
        
        k_values = []
        
        for i in range(k_period - 1, len(close)):
            # Находим максимум и минимум за период
            period_high = np.max(high[i - k_period + 1:i + 1])
            period_low = np.min(low[i - k_period + 1:i + 1])
            
            if period_high == period_low:
                k_percent = 50  # Избегаем деления на ноль
            else:
                k_percent = 100 * (close[i] - period_low) / (period_high - period_low)
            
            k_values.append(k_percent)
        
        # %D - скользящая средняя от %K
        if len(k_values) >= d_period:
            k_series = pd.Series(k_values)
            d_values = k_series.rolling(window=d_period).mean().dropna().tolist()
        else:
            d_values = []
        
        result = IndicatorResult(
            name='STOCHASTIC',
            type=IndicatorType.MOMENTUM,
            values={
                'k': k_values,
                'd': d_values
            },
            parameters={
                'k_period': k_period,
                'd_period': d_period
            }
        )
        
        logger.debug(f"📊 Stochastic calculated with periods K={k_period}, D={d_period}")
        return result
        
    except Exception as e:
        logger.error(f"❌ Stochastic calculation failed: {e}")
        raise


def williams_r(ohlc_data: OHLCData, period: int = 14) -> IndicatorResult:
    """
    Williams %R
    Индикатор Вильямса
    """
    try:
        ohlc = _prepare_ohlc_data(ohlc_data)
        
        high = ohlc['high']
        low = ohlc['low']
        close = ohlc['close']
        
        # Убеждаемся что все массивы одной длины
        min_length = min(len(high), len(low), len(close))
        high = high[:min_length]
        low = low[:min_length]
        close = close[:min_length]
        
        williams_values = []
        
        for i in range(period - 1, len(close)):
            period_high = np.max(high[i - period + 1:i + 1])
            period_low = np.min(low[i - period + 1:i + 1])
            
            if period_high == period_low:
                williams_value = -50  # Избегаем деления на ноль
            else:
                williams_value = -100 * (period_high - close[i]) / (period_high - period_low)
            
            williams_values.append(williams_value)
        
        result = IndicatorResult(
            name='WILLIAMS_R',
            type=IndicatorType.MOMENTUM,
            values=williams_values,
            parameters={'period': period}
        )
        
        logger.debug(f"📊 Williams %R calculated with period={period}")
        return result
        
    except Exception as e:
        logger.error(f"❌ Williams %R calculation failed: {e}")
        raise


# ============================================================================
# VOLATILITY INDICATORS
# ============================================================================

def bollinger_bands(
    data: PriceData,
    period: int = 20,
    std_multiplier: float = 2.0
) -> IndicatorResult:
    """
    Bollinger Bands
    Полосы Боллинджера
    """
    try:
        arr = _validate_data(data, period)
        
        series = pd.Series(arr)
        
        # Средняя линия (SMA)
        middle = series.rolling(window=period).mean()
        
        # Стандартное отклонение
        std = series.rolling(window=period).std()
        
        # Верхняя и нижняя полосы
        upper = middle + (std * std_multiplier)
        lower = middle - (std * std_multiplier)
        
        # Удаляем NaN
        valid_indices = ~pd.isna(middle)
        
        result = IndicatorResult(
            name='BOLLINGER_BANDS',
            type=IndicatorType.VOLATILITY,
            values={
                'upper': upper[valid_indices].tolist(),
                'middle': middle[valid_indices].tolist(),
                'lower': lower[valid_indices].tolist()
            },
            parameters={
                'period': period,
                'std_multiplier': std_multiplier
            }
        )
        
        logger.debug(f"📊 Bollinger Bands calculated with period={period}, multiplier={std_multiplier}")
        return result
        
    except Exception as e:
        logger.error(f"❌ Bollinger Bands calculation failed: {e}")
        raise


def atr(ohlc_data: OHLCData, period: int = 14) -> IndicatorResult:
    """
    Average True Range (ATR)
    Средний истинный диапазон
    """
    try:
        ohlc = _prepare_ohlc_data(ohlc_data)
        
        high = ohlc['high']
        low = ohlc['low']
        close = ohlc['close']
        
        # Убеждаемся что все массивы одной длины
        min_length = min(len(high), len(low), len(close))
        high = high[:min_length]
        low = low[:min_length] 
        close = close[:min_length]
        
        true_ranges = []
        
        for i in range(1, len(close)):
            tr1 = high[i] - low[i]
            tr2 = abs(high[i] - close[i-1])
            tr3 = abs(low[i] - close[i-1])
            
            true_range = max(tr1, tr2, tr3)
            true_ranges.append(true_range)
        
        # Вычисляем ATR как EMA от True Range
        tr_series = pd.Series(true_ranges)
        atr_values = tr_series.rolling(window=period).mean().dropna().tolist()
        
        result = IndicatorResult(
            name='ATR',
            type=IndicatorType.VOLATILITY,
            values=atr_values,
            parameters={'period': period}
        )
        
        logger.debug(f"📊 ATR calculated with period={period}")
        return result
        
    except Exception as e:
        logger.error(f"❌ ATR calculation failed: {e}")
        raise


def keltner_channels(
    ohlc_data: OHLCData,
    period: int = 20,
    atr_period: int = 10,
    multiplier: float = 2.0
) -> IndicatorResult:
    """
    Keltner Channels
    Каналы Кельтнера
    """
    try:
        ohlc = _prepare_ohlc_data(ohlc_data)
        
        # Типичная цена
        typical_price = (ohlc['high'] + ohlc['low'] + ohlc['close']) / 3
        
        # Средняя линия
        middle_line = ema(typical_price, period).values
        
        # ATR
        atr_values = atr(ohlc_data, atr_period).values
        
        # Выравниваем длины
        min_len = min(len(middle_line), len(atr_values))
        middle_line = middle_line[-min_len:]
        atr_values = atr_values[-min_len:]
        
        # Верхний и нижний каналы
        upper_channel = np.array(middle_line) + (np.array(atr_values) * multiplier)
        lower_channel = np.array(middle_line) - (np.array(atr_values) * multiplier)
        
        result = IndicatorResult(
            name='KELTNER_CHANNELS',
            type=IndicatorType.VOLATILITY,
            values={
                'upper': upper_channel.tolist(),
                'middle': middle_line,
                'lower': lower_channel.tolist()
            },
            parameters={
                'period': period,
                'atr_period': atr_period,
                'multiplier': multiplier
            }
        )
        
        logger.debug(f"📊 Keltner Channels calculated")
        return result
        
    except Exception as e:
        logger.error(f"❌ Keltner Channels calculation failed: {e}")
        raise


# ============================================================================
# VOLUME INDICATORS
# ============================================================================

def volume_sma(volume_data: PriceData, period: int = 20) -> IndicatorResult:
    """
    Volume Simple Moving Average
    Простая скользящая средняя объемов
    """
    try:
        vol_arr = _validate_data(volume_data, period)
        
        series = pd.Series(vol_arr)
        vol_sma = series.rolling(window=period).mean().dropna()
        
        result = IndicatorResult(
            name='VOLUME_SMA',
            type=IndicatorType.VOLUME,
            values=vol_sma.tolist(),
            parameters={'period': period}
        )
        
        logger.debug(f"📊 Volume SMA calculated with period={period}")
        return result
        
    except Exception as e:
        logger.error(f"❌ Volume SMA calculation failed: {e}")
        raise


def obv(ohlc_data: OHLCData) -> IndicatorResult:
    """
    On-Balance Volume (OBV)
    Балансовый объем
    """
    try:
        ohlc = _prepare_ohlc_data(ohlc_data)
        
        if 'volume' not in ohlc:
            raise ValueError("Volume data is required for OBV calculation")
        
        close = ohlc['close']
        volume = ohlc['volume']
        
        # Убеждаемся что массивы одной длины
        min_length = min(len(close), len(volume))
        close = close[:min_length]
        volume = volume[:min_length]
        
        obv_values = [volume[0]]  # Начальное значение
        
        for i in range(1, len(close)):
            if close[i] > close[i-1]:
                # Цена выросла - добавляем объем
                obv_values.append(obv_values[-1] + volume[i])
            elif close[i] < close[i-1]:
                # Цена упала - вычитаем объем
                obv_values.append(obv_values[-1] - volume[i])
            else:
                # Цена не изменилась
                obv_values.append(obv_values[-1])
        
        result = IndicatorResult(
            name='OBV',
            type=IndicatorType.VOLUME,
            values=obv_values,
            parameters={}
        )
        
        logger.debug(f"📊 OBV calculated")
        return result
        
    except Exception as e:
        logger.error(f"❌ OBV calculation failed: {e}")
        raise


def vwap(ohlc_data: OHLCData) -> IndicatorResult:
    """
    Volume Weighted Average Price (VWAP)
    Средневзвешенная по объему цена
    """
    try:
        ohlc = _prepare_ohlc_data(ohlc_data)
        
        if 'volume' not in ohlc:
            raise ValueError("Volume data is required for VWAP calculation")
        
        # Типичная цена
        typical_price = (ohlc['high'] + ohlc['low'] + ohlc['close']) / 3
        volume = ohlc['volume']
        
        # Убеждаемся что массивы одной длины
        min_length = min(len(typical_price), len(volume))
        typical_price = typical_price[:min_length]
        volume = volume[:min_length]
        
        # Кумулятивные значения
        cum_volume = np.cumsum(volume)
        cum_price_volume = np.cumsum(typical_price * volume)
        
        # VWAP
        vwap_values = cum_price_volume / cum_volume
        
        result = IndicatorResult(
            name='VWAP',
            type=IndicatorType.VOLUME,
            values=vwap_values.tolist(),
            parameters={}
        )
        
        logger.debug(f"📊 VWAP calculated")
        return result
        
    except Exception as e:
        logger.error(f"❌ VWAP calculation failed: {e}")
        raise


# ============================================================================
# SUPPORT & RESISTANCE
# ============================================================================

def pivot_points(ohlc_data: OHLCData) -> IndicatorResult:
    """
    Pivot Points
    Опорные точки
    """
    try:
        ohlc = _prepare_ohlc_data(ohlc_data)
        
        high = ohlc['high'][-1]  # Последние значения
        low = ohlc['low'][-1]
        close = ohlc['close'][-1]
        
        # Центральная опорная точка
        pivot = (high + low + close) / 3
        
        # Уровни сопротивления
        r1 = 2 * pivot - low
        r2 = pivot + (high - low)
        r3 = high + 2 * (pivot - low)
        
        # Уровни поддержки
        s1 = 2 * pivot - high
        s2 = pivot - (high - low)
        s3 = low - 2 * (high - pivot)
        
        result = IndicatorResult(
            name='PIVOT_POINTS',
            type=IndicatorType.SUPPORT_RESISTANCE,
            values={
                'pivot': pivot,
                'r1': r1,
                'r2': r2,
                'r3': r3,
                's1': s1,
                's2': s2,
                's3': s3
            },
            parameters={}
        )
        
        logger.debug(f"📊 Pivot Points calculated: P={pivot:.2f}")
        return result
        
    except Exception as e:
        logger.error(f"❌ Pivot Points calculation failed: {e}")
        raise


def fibonacci_retracement(high_price: float, low_price: float) -> IndicatorResult:
    """
    Fibonacci Retracement Levels
    Уровни коррекции Фибоначчи
    """
    try:
        if not validate_price(high_price) or not validate_price(low_price):
            raise ValueError("Invalid high or low price")
        
        if high_price <= low_price:
            raise ValueError("High price must be greater than low price")
        
        # Стандартные уровни Фибоначчи
        fib_levels = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
        
        price_diff = high_price - low_price
        levels = {}
        
        for level in fib_levels:
            retracement_price = high_price - (price_diff * level)
            levels[f"fib_{level}"] = retracement_price
        
        result = IndicatorResult(
            name='FIBONACCI_RETRACEMENT',
            type=IndicatorType.SUPPORT_RESISTANCE,
            values=levels,
            parameters={
                'high_price': high_price,
                'low_price': low_price
            }
        )
        
        logger.debug(f"📊 Fibonacci retracement calculated for range {low_price}-{high_price}")
        return result
        
    except Exception as e:
        logger.error(f"❌ Fibonacci retracement calculation failed: {e}")
        raise


# ============================================================================
# COMPOSITE INDICATORS
# ============================================================================

def ichimoku_cloud(
    ohlc_data: OHLCData,
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_span_b_period: int = 52,
    displacement: int = 26
) -> IndicatorResult:
    """
    Ichimoku Kinko Hyo (Ichimoku Cloud)
    Индикатор Ишимоку
    """
    try:
        ohlc = _prepare_ohlc_data(ohlc_data)
        
        high = ohlc['high']
        low = ohlc['low']
        close = ohlc['close']
        
        def calculate_line(period):
            """Вспомогательная функция для расчета линий"""
            line_values = []
            for i in range(period - 1, len(high)):
                period_high = np.max(high[i - period + 1:i + 1])
                period_low = np.min(low[i - period + 1:i + 1])
                line_values.append((period_high + period_low) / 2)
            return line_values
        
        # Tenkan-sen (Conversion Line)
        tenkan_sen = calculate_line(tenkan_period)
        
        # Kijun-sen (Base Line)
        kijun_sen = calculate_line(kijun_period)
        
        # Senkou Span A (Leading Span A)
        min_len = min(len(tenkan_sen), len(kijun_sen))
        tenkan_aligned = tenkan_sen[-min_len:]
        kijun_aligned = kijun_sen[-min_len:]
        
        senkou_span_a = [(t + k) / 2 for t, k in zip(tenkan_aligned, kijun_aligned)]
        
        # Senkou Span B (Leading Span B)
        senkou_span_b = calculate_line(senkou_span_b_period)
        
        # Chikou Span (Lagging Span) - просто смещенная цена закрытия
        chikou_span = close[:-displacement].tolist() if len(close) > displacement else []
        
        result = IndicatorResult(
            name='ICHIMOKU_CLOUD',
            type=IndicatorType.TREND,
            values={
                'tenkan_sen': tenkan_sen,
                'kijun_sen': kijun_sen,
                'senkou_span_a': senkou_span_a,
                'senkou_span_b': senkou_span_b,
                'chikou_span': chikou_span
            },
            parameters={
                'tenkan_period': tenkan_period,
                'kijun_period': kijun_period,
                'senkou_span_b_period': senkou_span_b_period,
                'displacement': displacement
            }
        )
        
        logger.debug(f"📊 Ichimoku Cloud calculated")
        return result
        
    except Exception as e:
        logger.error(f"❌ Ichimoku Cloud calculation failed: {e}")
        raise


# ============================================================================
# HELPER FUNCTIONS FOR STRATEGIES
# ============================================================================

def detect_crossover(fast_line: List[float], slow_line: List[float]) -> Dict[str, List[int]]:
    """
    Обнаружение пересечений двух линий
    Возвращает индексы где происходят пересечения
    """
    try:
        if len(fast_line) != len(slow_line):
            min_len = min(len(fast_line), len(slow_line))
            fast_line = fast_line[-min_len:]
            slow_line = slow_line[-min_len:]
        
        crossovers = {'bullish': [], 'bearish': []}
        
        for i in range(1, len(fast_line)):
            # Бычье пересечение: быстрая пересекает медленную снизу вверх
            if fast_line[i-1] <= slow_line[i-1] and fast_line[i] > slow_line[i]:
                crossovers['bullish'].append(i)
            
            # Медвежье пересечение: быстрая пересекает медленную сверху вниз
            elif fast_line[i-1] >= slow_line[i-1] and fast_line[i] < slow_line[i]:
                crossovers['bearish'].append(i)
        
        logger.debug(f"📊 Detected {len(crossovers['bullish'])} bullish and {len(crossovers['bearish'])} bearish crossovers")
        return crossovers
        
    except Exception as e:
        logger.error(f"❌ Crossover detection failed: {e}")
        return {'bullish': [], 'bearish': []}


def detect_divergence(
    price_data: PriceData,
    indicator_data: PriceData,
    lookback: int = 5
) -> Dict[str, List[int]]:
    """
    Обнаружение дивергенции между ценой и индикатором
    """
    try:
        prices = _validate_data(price_data)
        indicator = _validate_data(indicator_data)
        
        if len(prices) != len(indicator):
            min_len = min(len(prices), len(indicator))
            prices = prices[-min_len:]
            indicator = indicator[-min_len:]
        
        divergences = {'bullish': [], 'bearish': []}
        
        for i in range(lookback, len(prices) - lookback):
            # Ищем локальные максимумы и минимумы
            price_window = prices[i-lookback:i+lookback+1]
            indicator_window = indicator[i-lookback:i+lookback+1]
            
            # Проверяем является ли текущая точка экстремумом
            if prices[i] == np.max(price_window):  # Локальный максимум цены
                if indicator[i] < np.max(indicator_window):  # Индикатор не на максимуме
                    divergences['bearish'].append(i)
            
            elif prices[i] == np.min(price_window):  # Локальный минимум цены
                if indicator[i] > np.min(indicator_window):  # Индикатор не на минимуме
                    divergences['bullish'].append(i)
        
        logger.debug(f"📊 Detected {len(divergences['bullish'])} bullish and {len(divergences['bearish'])} bearish divergences")
        return divergences
        
    except Exception as e:
        logger.error(f"❌ Divergence detection failed: {e}")
        return {'bullish': [], 'bearish': []}


def calculate_indicator_signals(
    indicator_result: IndicatorResult,
    overbought_level: float = 70,
    oversold_level: float = 30
) -> Dict[str, List[int]]:
    """
    Генерация сигналов на основе индикатора
    """
    try:
        signals = {'buy': [], 'sell': []}
        
        if indicator_result.name == 'RSI':
            rsi_values = indicator_result.values
            
            for i, rsi in enumerate(rsi_values):
                if rsi < oversold_level:
                    signals['buy'].append(i)
                elif rsi > overbought_level:
                    signals['sell'].append(i)
        
        elif indicator_result.name == 'STOCHASTIC':
            k_values = indicator_result.values.get('k', [])
            d_values = indicator_result.values.get('d', [])
            
            if k_values and d_values:
                crossovers = detect_crossover(k_values, d_values)
                
                # Фильтруем по уровням перекупленности/перепроданности
                for idx in crossovers['bullish']:
                    if idx < len(k_values) and k_values[idx] < oversold_level:
                        signals['buy'].append(idx)
                
                for idx in crossovers['bearish']:
                    if idx < len(k_values) and k_values[idx] > overbought_level:
                        signals['sell'].append(idx)
        
        elif indicator_result.name == 'MACD':
            macd_line = indicator_result.values.get('macd', [])
            signal_line = indicator_result.values.get('signal', [])
            
            if macd_line and signal_line:
                crossovers = detect_crossover(macd_line, signal_line)
                signals['buy'] = crossovers['bullish']
                signals['sell'] = crossovers['bearish']
        
        logger.debug(f"📊 Generated {len(signals['buy'])} buy and {len(signals['sell'])} sell signals for {indicator_result.name}")
        return signals
        
    except Exception as e:
        logger.error(f"❌ Signal generation failed: {e}")
        return {'buy': [], 'sell': []}


# ============================================================================
# MULTI-INDICATOR ANALYSIS
# ============================================================================

def analyze_multiple_indicators(
    ohlc_data: OHLCData,
    indicators_config: Dict[str, Dict[str, Any]]
) -> Dict[str, IndicatorResult]:
    """
    Анализ нескольких индикаторов одновременно
    
    indicators_config example:
    {
        'rsi': {'period': 14},
        'macd': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
        'bollinger': {'period': 20, 'std_multiplier': 2.0}
    }
    """
    try:
        ohlc = _prepare_ohlc_data(ohlc_data)
        close_prices = ohlc['close']
        
        results = {}
        
        for indicator_name, params in indicators_config.items():
            try:
                if indicator_name.lower() == 'rsi':
                    result = rsi(close_prices, **params)
                
                elif indicator_name.lower() == 'macd':
                    result = macd(close_prices, **params)
                
                elif indicator_name.lower() in ['bollinger', 'bollinger_bands']:
                    result = bollinger_bands(close_prices, **params)
                
                elif indicator_name.lower() == 'sma':
                    result = sma(close_prices, **params)
                
                elif indicator_name.lower() == 'ema':
                    result = ema(close_prices, **params)
                
                elif indicator_name.lower() == 'atr':
                    result = atr(ohlc_data, **params)
                
                elif indicator_name.lower() == 'stochastic':
                    result = stochastic(ohlc_data, **params)
                
                else:
                    logger.warning(f"⚠️ Unknown indicator: {indicator_name}")
                    continue
                
                results[indicator_name] = result
                
            except Exception as e:
                logger.error(f"❌ Failed to calculate {indicator_name}: {e}")
                continue
        
        logger.info(f"📊 Calculated {len(results)} indicators successfully")
        return results
        
    except Exception as e:
        logger.error(f"❌ Multi-indicator analysis failed: {e}")
        return {}


def create_trading_summary(
    indicators_results: Dict[str, IndicatorResult],
    current_price: float
) -> Dict[str, Any]:
    """
    Создание торгового саммари на основе множественных индикаторов
    """
    try:
        summary = {
            'timestamp': int(pd.Timestamp.now().timestamp() * 1000),
            'current_price': current_price,
            'indicators': {},
            'signals': {'buy': 0, 'sell': 0, 'neutral': 0},
            'overall_sentiment': 'NEUTRAL',
            'confidence': 0.0
        }
        
        total_signals = 0
        buy_signals = 0
        sell_signals = 0
        
        for name, result in indicators_results.items():
            indicator_info = {
                'type': result.type.value,
                'current_value': None,
                'signal': 'NEUTRAL'
            }
            
            # Получаем текущее значение индикатора
            if isinstance(result.values, list) and result.values:
                indicator_info['current_value'] = result.values[-1]
            elif isinstance(result.values, dict):
                # Для сложных индикаторов берем основное значение
                if name.upper() == 'MACD':
                    indicator_info['current_value'] = result.values.get('macd', [])[-1] if result.values.get('macd') else None
                elif name.upper() == 'BOLLINGER_BANDS':
                    indicator_info['current_value'] = {
                        'upper': result.values.get('upper', [])[-1] if result.values.get('upper') else None,
                        'middle': result.values.get('middle', [])[-1] if result.values.get('middle') else None,
                        'lower': result.values.get('lower', [])[-1] if result.values.get('lower') else None
                    }
            
            # Генерируем сигналы
            signals = calculate_indicator_signals(result)
            
            if signals['buy']:
                indicator_info['signal'] = 'BUY'
                buy_signals += 1
            elif signals['sell']:
                indicator_info['signal'] = 'SELL'
                sell_signals += 1
            
            summary['indicators'][name] = indicator_info
            total_signals += 1
        
        # Определяем общий сентимент
        if total_signals > 0:
            buy_ratio = buy_signals / total_signals
            sell_ratio = sell_signals / total_signals
            
            if buy_ratio > 0.6:
                summary['overall_sentiment'] = 'BULLISH'
                summary['confidence'] = buy_ratio
            elif sell_ratio > 0.6:
                summary['overall_sentiment'] = 'BEARISH'
                summary['confidence'] = sell_ratio
            else:
                summary['overall_sentiment'] = 'NEUTRAL'
                summary['confidence'] = max(buy_ratio, sell_ratio)
        
        summary['signals'] = {
            'buy': buy_signals,
            'sell': sell_signals,
            'neutral': total_signals - buy_signals - sell_signals
        }
        
        logger.info(f"📊 Trading summary created: {summary['overall_sentiment']} (confidence: {summary['confidence']:.2f})")
        return summary
        
    except Exception as e:
        logger.error(f"❌ Trading summary creation failed: {e}")
        return {}


# ============================================================================
# EXAMPLE USAGE AND TESTING
# ============================================================================

if __name__ == "__main__":
    # Пример использования индикаторов
    
    print("🧪 Testing Technical Indicators")
    print("=" * 50)
    
    # Создаем тестовые данные
    np.random.seed(42)
    n_points = 100
    
    # Генерируем реалистичные OHLC данные
    base_price = 50000
    price_changes = np.random.normal(0, 0.02, n_points)  # 2% волатильность
    close_prices = [base_price]
    
    for change in price_changes[1:]:
        new_price = close_prices[-1] * (1 + change)
        close_prices.append(new_price)
    
    # Создаем OHLC данные
    ohlc_data = {
        'open': [p * (1 + np.random.uniform(-0.01, 0.01)) for p in close_prices],
        'high': [p * (1 + np.random.uniform(0.001, 0.02)) for p in close_prices],
        'low': [p * (1 + np.random.uniform(-0.02, -0.001)) for p in close_prices],
        'close': close_prices,
        'volume': [np.random.uniform(100, 1000) for _ in range(n_points)]
    }
    
    try:
        # Тестируем различные индикаторы
        print("📊 Testing SMA...")
        sma_result = sma(close_prices, 20)
        print(f"✅ SMA: {len(sma_result.values)} values calculated")
        
        print("📊 Testing EMA...")
        ema_result = ema(close_prices, 20)
        print(f"✅ EMA: {len(ema_result.values)} values calculated")
        
        print("📊 Testing RSI...")
        rsi_result = rsi(close_prices, 14)
        print(f"✅ RSI: {len(rsi_result.values)} values, last value: {rsi_result.values[-1]:.2f}")
        
        print("📊 Testing MACD...")
        macd_result = macd(close_prices)
        print(f"✅ MACD: MACD line has {len(macd_result.values['macd'])} values")
        
        print("📊 Testing Bollinger Bands...")
        bb_result = bollinger_bands(close_prices)
        print(f"✅ Bollinger Bands: {len(bb_result.values['middle'])} values calculated")
        
        print("📊 Testing ATR...")
        atr_result = atr(ohlc_data)
        print(f"✅ ATR: {len(atr_result.values)} values, last ATR: {atr_result.values[-1]:.2f}")
        
        print("📊 Testing multiple indicators...")
        indicators_config = {
            'rsi': {'period': 14},
            'macd': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
            'bollinger': {'period': 20, 'std_multiplier': 2.0}
        }
        
        multi_results = analyze_multiple_indicators(ohlc_data, indicators_config)
        print(f"✅ Multi-indicator analysis: {len(multi_results)} indicators calculated")
        
        print("📊 Creating trading summary...")
        summary = create_trading_summary(multi_results, close_prices[-1])
        print(f"✅ Trading Summary: {summary['overall_sentiment']} (confidence: {summary['confidence']:.2f})")
        
        print("\n✅ All indicator tests completed successfully!")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
