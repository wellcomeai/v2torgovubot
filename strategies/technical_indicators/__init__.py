"""
Trading Bot Technical Indicators for Strategies
Модуль технических индикаторов специально адаптированных для торговых стратегий
"""

# Импорты будут добавляться по мере создания модулей
try:
    from .ma_indicators import (
        MovingAverageIndicators,
        calculate_sma_crossover,
        calculate_ema_crossover,
        calculate_multiple_ma,
        ma_trend_direction
    )
except ImportError:
    pass

try:
    from .momentum_indicators import (
        MomentumIndicators, 
        rsi_signals,
        stochastic_signals,
        macd_signals,
        williams_r_signals
    )
except ImportError:
    pass

try:
    from .volume_indicators import (
        VolumeIndicators,
        obv_trend,
        volume_confirmation,
        vwap_signals
    )
except ImportError:
    pass

# Версия модуля
__version__ = "1.0.0"

# Экспортируемые классы и функции
__all__ = [
    # Moving Average indicators
    "MovingAverageIndicators",
    "calculate_sma_crossover", 
    "calculate_ema_crossover",
    "calculate_multiple_ma",
    "ma_trend_direction",
    
    # Momentum indicators
    "MomentumIndicators",
    "rsi_signals",
    "stochastic_signals", 
    "macd_signals",
    "williams_r_signals",
    
    # Volume indicators
    "VolumeIndicators",
    "obv_trend",
    "volume_confirmation",
    "vwap_signals",
]

# Константы для индикаторов
DEFAULT_PERIODS = {
    'sma_fast': 10,
    'sma_slow': 20,
    'ema_fast': 12,
    'ema_slow': 26,
    'rsi': 14,
    'stochastic_k': 14,
    'stochastic_d': 3,
    'macd_fast': 12,
    'macd_slow': 26,
    'macd_signal': 9,
    'williams_r': 14,
    'bollinger_bands': 20,
    'atr': 14
}

# Уровни для индикаторов
INDICATOR_LEVELS = {
    'rsi_oversold': 30,
    'rsi_overbought': 70,
    'stochastic_oversold': 20,
    'stochastic_overbought': 80,
    'williams_oversold': -80,
    'williams_overbought': -20
}

def get_default_period(indicator_name: str) -> int:
    """Получение периода по умолчанию для индикатора"""
    return DEFAULT_PERIODS.get(indicator_name, 14)

def get_indicator_levels(indicator_name: str) -> dict:
    """Получение уровней для индикатора"""
    levels = {}
    for key, value in INDICATOR_LEVELS.items():
        if key.startswith(indicator_name):
            level_type = key.split('_', 1)[1] 
            levels[level_type] = value
    return levels
