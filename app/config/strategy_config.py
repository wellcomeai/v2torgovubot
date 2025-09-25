"""
Trading Bot Strategy Configuration
Центральная конфигурация всех торговых стратегий
"""

from typing import Dict, List, Any, Optional, Union
from dataclasses import dataclass
from enum import Enum
import logging

from app.config.settings import get_settings
from utils.logger import setup_logger


# ============================================================================
# ENUMS AND TYPES
# ============================================================================

class StrategyType(str, Enum):
    """Типы стратегий"""
    TREND_FOLLOWING = "trend_following"
    MEAN_REVERSION = "mean_reversion" 
    MOMENTUM = "momentum"
    ARBITRAGE = "arbitrage"
    SCALPING = "scalping"
    SWING = "swing"


class TimeframeType(str, Enum):
    """Поддерживаемые таймфреймы"""
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


# ============================================================================
# DATACLASSES
# ============================================================================

@dataclass
class StrategyParameters:
    """Параметры стратегии"""
    # Базовые параметры
    enabled: bool = True
    max_position_size: float = 1000.0
    stop_loss_pct: float = 0.02  # 2%
    take_profit_pct: float = 0.04  # 4%
    
    # Risk management
    max_daily_trades: int = 10
    cooldown_minutes: int = 5
    confidence_threshold: float = 0.7
    
    # Дополнительные параметры (специфичные для каждой стратегии)
    custom_params: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.custom_params is None:
            self.custom_params = {}


@dataclass  
class StrategyConfig:
    """Полная конфигурация стратегии"""
    name: str
    type: StrategyType
    symbol: str
    timeframe: TimeframeType
    parameters: StrategyParameters
    description: str = ""
    version: str = "1.0.0"
    
    # Метаданные
    created_by: str = "system"
    is_active: bool = True
    priority: int = 1  # 1=низкий, 5=высокий
    
    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь для сохранения в БД"""
        return {
            'name': self.name,
            'symbol': self.symbol,
            'timeframe': self.timeframe.value,
            'parameters': {
                'enabled': self.parameters.enabled,
                'max_position_size': self.parameters.max_position_size,
                'stop_loss_pct': self.parameters.stop_loss_pct,
                'take_profit_pct': self.parameters.take_profit_pct,
                'max_daily_trades': self.parameters.max_daily_trades,
                'cooldown_minutes': self.parameters.cooldown_minutes,
                'confidence_threshold': self.parameters.confidence_threshold,
                **self.parameters.custom_params
            },
            'type': self.type.value,
            'description': self.description,
            'version': self.version,
            'created_by': self.created_by,
            'is_active': self.is_active,
            'priority': self.priority
        }


# ============================================================================
# ПРЕДУСТАНОВЛЕННЫЕ КОНФИГУРАЦИИ СТРАТЕГИЙ
# ============================================================================

class StrategyTemplates:
    """Шаблоны конфигураций для популярных стратегий"""
    
    @staticmethod
    def moving_average_crossover(
        symbol: str = "BTCUSDT",
        timeframe: TimeframeType = TimeframeType.M5,
        fast_period: int = 10,
        slow_period: int = 20
    ) -> StrategyConfig:
        """Стратегия пересечения скользящих средних"""
        
        params = StrategyParameters(
            enabled=True,
            max_position_size=1000.0,
            stop_loss_pct=0.015,  # 1.5%
            take_profit_pct=0.03,  # 3%
            max_daily_trades=8,
            cooldown_minutes=3,
            confidence_threshold=0.75,
            custom_params={
                'fast_period': fast_period,
                'slow_period': slow_period,
                'source': 'close',
                'signal_smoothing': True
            }
        )
        
        return StrategyConfig(
            name='moving_average',
            type=StrategyType.TREND_FOLLOWING,
            symbol=symbol,
            timeframe=timeframe,
            parameters=params,
            description="Стратегия на основе пересечения быстрой и медленной скользящих средних",
            version="1.0.0"
        )
    
    @staticmethod
    def rsi_oversold_overbought(
        symbol: str = "ETHUSDT", 
        timeframe: TimeframeType = TimeframeType.M15,
        rsi_period: int = 14,
        oversold_level: int = 30,
        overbought_level: int = 70
    ) -> StrategyConfig:
        """RSI стратегия на перекупленности/перепроданности"""
        
        params = StrategyParameters(
            enabled=True,
            max_position_size=800.0,
            stop_loss_pct=0.02,  # 2%
            take_profit_pct=0.04,  # 4%
            max_daily_trades=6,
            cooldown_minutes=10,
            confidence_threshold=0.8,
            custom_params={
                'rsi_period': rsi_period,
                'oversold_level': oversold_level,
                'overbought_level': overbought_level,
                'divergence_check': True,
                'volume_confirmation': True
            }
        )
        
        return StrategyConfig(
            name='rsi_strategy',
            type=StrategyType.MEAN_REVERSION,
            symbol=symbol,
            timeframe=timeframe,
            parameters=params,
            description="RSI стратегия для определения зон перекупленности/перепроданности",
            version="1.0.0"
        )
    
    @staticmethod
    def bollinger_bands_breakout(
        symbol: str = "ADAUSDT",
        timeframe: TimeframeType = TimeframeType.M30,
        bb_period: int = 20,
        bb_std: float = 2.0
    ) -> StrategyConfig:
        """Стратегия пробоя полос Боллинджера"""
        
        params = StrategyParameters(
            enabled=True,
            max_position_size=1200.0,
            stop_loss_pct=0.025,  # 2.5%
            take_profit_pct=0.05,  # 5%
            max_daily_trades=5,
            cooldown_minutes=15,
            confidence_threshold=0.85,
            custom_params={
                'bb_period': bb_period,
                'bb_std': bb_std,
                'breakout_threshold': 0.001,  # 0.1% пробой
                'volume_spike_required': True,
                'trend_filter': True
            }
        )
        
        return StrategyConfig(
            name='bollinger_breakout',
            type=StrategyType.MOMENTUM,
            symbol=symbol,
            timeframe=timeframe,
            parameters=params,
            description="Стратегия пробоя верхней/нижней полосы Боллинджера",
            version="1.0.0"
        )
    
    @staticmethod
    def macd_signal_line(
        symbol: str = "SOLUSDT",
        timeframe: TimeframeType = TimeframeType.H1,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> StrategyConfig:
        """MACD стратегия на пересечение с сигнальной линией"""
        
        params = StrategyParameters(
            enabled=True,
            max_position_size=900.0,
            stop_loss_pct=0.03,  # 3%
            take_profit_pct=0.06,  # 6%
            max_daily_trades=4,
            cooldown_minutes=30,
            confidence_threshold=0.8,
            custom_params={
                'fast_period': fast_period,
                'slow_period': slow_period,
                'signal_period': signal_period,
                'histogram_confirmation': True,
                'zero_line_cross': True
            }
        )
        
        return StrategyConfig(
            name='macd_strategy',
            type=StrategyType.TREND_FOLLOWING,
            symbol=symbol,
            timeframe=timeframe,
            parameters=params,
            description="MACD стратегия на основе пересечения MACD с сигнальной линией",
            version="1.0.0"
        )


# ============================================================================
# MANAGER КЛАСС ДЛЯ УПРАВЛЕНИЯ КОНФИГУРАЦИЯМИ
# ============================================================================

class StrategyConfigManager:
    """
    Менеджер для управления конфигурациями стратегий
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.logger = setup_logger(f"{__name__}.StrategyConfigManager")
        
        # Кеш активных конфигураций
        self._active_configs: Dict[str, StrategyConfig] = {}
        self._initialized = False
    
    def initialize(self) -> None:
        """Инициализация менеджера"""
        if self._initialized:
            return
            
        try:
            self.logger.info("🔧 Initializing Strategy Config Manager...")
            
            # Загружаем дефолтные стратегии из настроек
            default_strategies = self.settings.DEFAULT_ACTIVE_STRATEGIES
            
            for symbol, strategies_list in default_strategies.items():
                for strategy_data in strategies_list:
                    if strategy_data.get('enabled', True):
                        config = self._create_config_from_dict(symbol, strategy_data)
                        if config:
                            self._active_configs[f"{config.name}_{config.symbol}"] = config
            
            self._initialized = True
            self.logger.info(f"✅ Strategy Config Manager initialized with {len(self._active_configs)} active strategies")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize Strategy Config Manager: {e}")
            raise
    
    def get_active_strategies(self) -> List[StrategyConfig]:
        """Получение всех активных стратегий"""
        if not self._initialized:
            self.initialize()
        
        return [config for config in self._active_configs.values() if config.is_active]
    
    def get_strategy_config(self, name: str, symbol: str) -> Optional[StrategyConfig]:
        """Получение конфигурации конкретной стратегии"""
        key = f"{name}_{symbol}"
        return self._active_configs.get(key)
    
    def add_strategy_config(self, config: StrategyConfig) -> bool:
        """Добавление новой конфигурации стратегии"""
        try:
            key = f"{config.name}_{config.symbol}"
            
            if key in self._active_configs:
                self.logger.warning(f"⚠️ Strategy config {key} already exists, updating...")
            
            self._active_configs[key] = config
            self.logger.info(f"✅ Added strategy config: {key}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to add strategy config: {e}")
            return False
    
    def remove_strategy_config(self, name: str, symbol: str) -> bool:
        """Удаление конфигурации стратегии"""
        try:
            key = f"{name}_{symbol}"
            
            if key in self._active_configs:
                del self._active_configs[key]
                self.logger.info(f"🗑️ Removed strategy config: {key}")
                return True
            else:
                self.logger.warning(f"⚠️ Strategy config {key} not found")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ Failed to remove strategy config: {e}")
            return False
    
    def update_strategy_parameters(
        self, 
        name: str, 
        symbol: str, 
        parameters: Dict[str, Any]
    ) -> bool:
        """Обновление параметров стратегии"""
        try:
            config = self.get_strategy_config(name, symbol)
            if not config:
                self.logger.error(f"❌ Strategy config {name}_{symbol} not found")
                return False
            
            # Обновляем параметры
            for key, value in parameters.items():
                if hasattr(config.parameters, key):
                    setattr(config.parameters, key, value)
                else:
                    config.parameters.custom_params[key] = value
            
            self.logger.info(f"🔄 Updated parameters for strategy {name}_{symbol}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to update strategy parameters: {e}")
            return False
    
    def get_strategies_by_symbol(self, symbol: str) -> List[StrategyConfig]:
        """Получение всех стратегий для символа"""
        return [
            config for config in self._active_configs.values() 
            if config.symbol == symbol and config.is_active
        ]
    
    def get_strategies_by_type(self, strategy_type: StrategyType) -> List[StrategyConfig]:
        """Получение всех стратегий определенного типа"""
        return [
            config for config in self._active_configs.values()
            if config.type == strategy_type and config.is_active
        ]
    
    def validate_config(self, config: StrategyConfig) -> tuple[bool, List[str]]:
        """Валидация конфигурации стратегии"""
        errors = []
        
        try:
            # Проверка обязательных полей
            if not config.name:
                errors.append("Strategy name is required")
            
            if not config.symbol:
                errors.append("Symbol is required")
            
            if config.symbol not in self.settings.SUPPORTED_SYMBOLS:
                errors.append(f"Symbol {config.symbol} not supported")
            
            if config.timeframe not in [tf.value for tf in TimeframeType]:
                errors.append(f"Timeframe {config.timeframe} not supported")
            
            # Проверка параметров
            params = config.parameters
            
            if params.max_position_size <= 0:
                errors.append("Max position size must be positive")
            
            if params.max_position_size > self.settings.MAX_POSITION_SIZE:
                errors.append(f"Max position size exceeds limit ({self.settings.MAX_POSITION_SIZE})")
            
            if not (0 < params.stop_loss_pct < 1):
                errors.append("Stop loss percentage must be between 0 and 1")
            
            if not (0 < params.take_profit_pct < 1):
                errors.append("Take profit percentage must be between 0 and 1")
            
            if not (0 < params.confidence_threshold <= 1):
                errors.append("Confidence threshold must be between 0 and 1")
            
            if params.max_daily_trades <= 0:
                errors.append("Max daily trades must be positive")
            
            if params.cooldown_minutes < 0:
                errors.append("Cooldown minutes cannot be negative")
            
            return len(errors) == 0, errors
            
        except Exception as e:
            errors.append(f"Validation error: {str(e)}")
            return False, errors
    
    def create_from_template(
        self, 
        template_name: str, 
        symbol: str = "BTCUSDT",
        timeframe: TimeframeType = TimeframeType.M5,
        **custom_params
    ) -> Optional[StrategyConfig]:
        """Создание конфигурации из шаблона"""
        try:
            templates = {
                'moving_average': StrategyTemplates.moving_average_crossover,
                'rsi_strategy': StrategyTemplates.rsi_oversold_overbought,
                'bollinger_breakout': StrategyTemplates.bollinger_bands_breakout,
                'macd_strategy': StrategyTemplates.macd_signal_line
            }
            
            if template_name not in templates:
                self.logger.error(f"❌ Template {template_name} not found")
                return None
            
            # Создаем конфигурацию из шаблона
            config = templates[template_name](symbol=symbol, timeframe=timeframe, **custom_params)
            
            # Валидируем
            is_valid, errors = self.validate_config(config)
            if not is_valid:
                self.logger.error(f"❌ Template config validation failed: {errors}")
                return None
            
            self.logger.info(f"✅ Created config from template: {template_name}")
            return config
            
        except Exception as e:
            self.logger.error(f"❌ Failed to create config from template: {e}")
            return None
    
    def _create_config_from_dict(self, symbol: str, data: Dict[str, Any]) -> Optional[StrategyConfig]:
        """Создание конфигурации из словаря (для загрузки из настроек)"""
        try:
            # Определяем тип стратегии (по умолчанию trend_following)
            strategy_type_map = {
                'moving_average': StrategyType.TREND_FOLLOWING,
                'rsi_strategy': StrategyType.MEAN_REVERSION,
                'bollinger_breakout': StrategyType.MOMENTUM,
                'macd_strategy': StrategyType.TREND_FOLLOWING
            }
            
            name = data['name']
            strategy_type = strategy_type_map.get(name, StrategyType.TREND_FOLLOWING)
            timeframe = TimeframeType(data.get('timeframe', '5m'))
            
            # Создаем параметры
            params_data = data.get('params', {})
            
            parameters = StrategyParameters(
                enabled=data.get('enabled', True),
                max_position_size=params_data.get('max_position_size', 1000.0),
                stop_loss_pct=params_data.get('stop_loss_pct', 0.02),
                take_profit_pct=params_data.get('take_profit_pct', 0.04),
                max_daily_trades=params_data.get('max_daily_trades', 10),
                cooldown_minutes=params_data.get('cooldown_minutes', 5),
                confidence_threshold=params_data.get('confidence_threshold', 0.7),
                custom_params={k: v for k, v in params_data.items() 
                              if k not in ['max_position_size', 'stop_loss_pct', 
                                          'take_profit_pct', 'max_daily_trades', 
                                          'cooldown_minutes', 'confidence_threshold']}
            )
            
            config = StrategyConfig(
                name=name,
                type=strategy_type,
                symbol=symbol,
                timeframe=timeframe,
                parameters=parameters,
                description=data.get('description', f"Auto-generated {name} strategy"),
                is_active=data.get('enabled', True)
            )
            
            return config
            
        except Exception as e:
            self.logger.error(f"❌ Failed to create config from dict: {e}")
            return None
    
    def export_configs(self) -> Dict[str, Any]:
        """Экспорт всех конфигураций в словарь"""
        try:
            exported = {}
            
            for key, config in self._active_configs.items():
                exported[key] = config.to_dict()
            
            self.logger.info(f"📤 Exported {len(exported)} strategy configurations")
            return exported
            
        except Exception as e:
            self.logger.error(f"❌ Failed to export configs: {e}")
            return {}
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики конфигураций"""
        try:
            total_configs = len(self._active_configs)
            active_configs = len([c for c in self._active_configs.values() if c.is_active])
            
            # Группируем по символам
            by_symbol = {}
            for config in self._active_configs.values():
                if config.symbol not in by_symbol:
                    by_symbol[config.symbol] = 0
                by_symbol[config.symbol] += 1
            
            # Группируем по типам
            by_type = {}
            for config in self._active_configs.values():
                type_name = config.type.value
                if type_name not in by_type:
                    by_type[type_name] = 0
                by_type[type_name] += 1
            
            stats = {
                'total_configurations': total_configs,
                'active_configurations': active_configs,
                'inactive_configurations': total_configs - active_configs,
                'configurations_by_symbol': by_symbol,
                'configurations_by_type': by_type,
                'supported_symbols': list(self.settings.SUPPORTED_SYMBOLS),
                'supported_timeframes': [tf.value for tf in TimeframeType]
            }
            
            return stats
            
        except Exception as e:
            self.logger.error(f"❌ Failed to get config stats: {e}")
            return {}


# ============================================================================
# SINGLETON INSTANCE
# ============================================================================

_config_manager_instance: Optional[StrategyConfigManager] = None


def get_strategy_config_manager() -> StrategyConfigManager:
    """Получение singleton экземпляра менеджера конфигураций"""
    global _config_manager_instance
    
    if _config_manager_instance is None:
        _config_manager_instance = StrategyConfigManager()
        _config_manager_instance.initialize()
    
    return _config_manager_instance


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def get_active_strategies() -> List[StrategyConfig]:
    """Быстрое получение активных стратегий"""
    manager = get_strategy_config_manager()
    return manager.get_active_strategies()


def get_strategies_for_symbol(symbol: str) -> List[StrategyConfig]:
    """Получение всех стратегий для символа"""
    manager = get_strategy_config_manager()
    return manager.get_strategies_by_symbol(symbol)


def create_strategy_from_template(
    template_name: str,
    symbol: str,
    timeframe: str = "5m",
    **params
) -> Optional[StrategyConfig]:
    """Создание стратегии из шаблона"""
    manager = get_strategy_config_manager()
    timeframe_enum = TimeframeType(timeframe)
    return manager.create_from_template(template_name, symbol, timeframe_enum, **params)


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Пример использования
    
    # Инициализация менеджера
    manager = get_strategy_config_manager()
    
    # Создание стратегии из шаблона
    config = create_strategy_from_template(
        'moving_average',
        'BTCUSDT',
        '5m',
        fast_period=10,
        slow_period=20
    )
    
    if config:
        print(f"✅ Created strategy: {config.name} for {config.symbol}")
        print(f"Parameters: {config.parameters.custom_params}")
    
    # Добавление стратегии
    if config:
        manager.add_strategy_config(config)
    
    # Получение активных стратегий
    active_strategies = get_active_strategies()
    print(f"📊 Active strategies: {len(active_strategies)}")
    
    # Статистика
    stats = manager.get_stats()
    print(f"📈 Statistics: {stats}")
