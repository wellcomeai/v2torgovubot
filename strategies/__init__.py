"""
Trading Bot Strategies Module
Модуль торговых стратегий и технических индикаторов
"""

# Импорты будут добавлены по мере создания файлов
from .base_strategy import BaseStrategy, StrategyResult, SignalStrength
from .strategy_registry import StrategyRegistry, register_strategy, get_strategy_class
from .moving_average import MovingAverageStrategy

# Версия модуля
__version__ = "1.0.0"

# Экспортируемые классы и функции
__all__ = [
    # Base Strategy
    "BaseStrategy",
    "StrategyResult", 
    "SignalStrength",
    
    # Strategy Registry
    "StrategyRegistry",
    "register_strategy",
    "get_strategy_class",
    
    # Concrete Strategies
    "MovingAverageStrategy",
]

# Автоматическая регистрация стратегий при импорте модуля
def _register_built_in_strategies():
    """Регистрация встроенных стратегий"""
    try:
        from .moving_average import MovingAverageStrategy
        StrategyRegistry.register("moving_average", MovingAverageStrategy)
        
        # Добавим другие стратегии когда создадим
        # from .rsi_strategy import RSIStrategy 
        # StrategyRegistry.register("rsi_strategy", RSIStrategy)
        
    except ImportError as e:
        # Логируем ошибку но не прерываем инициализацию
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"⚠️ Failed to register some strategies: {e}")

# Выполняем автоматическую регистрацию
_register_built_in_strategies()
