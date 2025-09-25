"""
Trading Bot Strategy Registry
Центральный реестр для регистрации и управления торговыми стратегиями
"""

from typing import Dict, List, Type, Optional, Any, Callable
from dataclasses import dataclass
import importlib
import inspect
from pathlib import Path
import sys

from utils.logger import setup_logger
from .base_strategy import BaseStrategy, StrategyConfig


# ============================================================================
# TYPES AND DATACLASSES
# ============================================================================

@dataclass
class StrategyInfo:
    """Информация о зарегистрированной стратегии"""
    name: str
    strategy_class: Type[BaseStrategy]
    description: str
    version: str
    author: str
    category: str
    parameters_schema: Dict[str, Any]
    min_history_length: int
    supported_timeframes: List[str]
    is_active: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'class_name': self.strategy_class.__name__,
            'module': self.strategy_class.__module__,
            'description': self.description,
            'version': self.version,
            'author': self.author,
            'category': self.category,
            'parameters_schema': self.parameters_schema,
            'min_history_length': self.min_history_length,
            'supported_timeframes': self.supported_timeframes,
            'is_active': self.is_active
        }


# ============================================================================
# STRATEGY REGISTRY CLASS
# ============================================================================

class StrategyRegistry:
    """
    Центральный реестр торговых стратегий
    
    Позволяет:
    - Регистрировать новые стратегии
    - Получать список доступных стратегий  
    - Создавать экземпляры стратегий по имени
    - Автоматически загружать стратегии из директории
    """
    
    _strategies: Dict[str, StrategyInfo] = {}
    _logger = setup_logger(__name__ + ".StrategyRegistry")
    
    @classmethod
    def register(
        cls,
        name: str,
        strategy_class: Type[BaseStrategy],
        description: str = "",
        version: str = "1.0.0",
        author: str = "Unknown",
        category: str = "General",
        parameters_schema: Optional[Dict[str, Any]] = None,
        supported_timeframes: Optional[List[str]] = None,
        override: bool = False
    ) -> None:
        """
        Регистрация стратегии в реестре
        
        Args:
            name: Уникальное имя стратегии
            strategy_class: Класс стратегии (наследник BaseStrategy)
            description: Описание стратегии
            version: Версия стратегии  
            author: Автор стратегии
            category: Категория стратегии
            parameters_schema: Схема параметров
            supported_timeframes: Поддерживаемые таймфреймы
            override: Перезаписать существующую стратегию
        """
        try:
            # Валидация
            if not name:
                raise ValueError("Strategy name cannot be empty")
            
            if not inspect.isclass(strategy_class):
                raise ValueError("strategy_class must be a class")
            
            if not issubclass(strategy_class, BaseStrategy):
                raise ValueError("strategy_class must inherit from BaseStrategy")
            
            # Проверка на дубликаты
            if name in cls._strategies and not override:
                raise ValueError(f"Strategy '{name}' already registered. Use override=True to replace.")
            
            # Получаем информацию о стратегии
            min_history_length = 20  # По умолчанию
            try:
                # Пытаемся создать временный экземпляр для получения информации
                temp_config = StrategyConfig(
                    name=name,
                    symbol="BTCUSDT", 
                    timeframe="5m",
                    parameters={}
                )
                temp_instance = strategy_class(temp_config)
                min_history_length = temp_instance.get_required_history_length()
            except Exception as e:
                cls._logger.warning(f"⚠️ Could not get min_history_length from {strategy_class.__name__}: {e}")
            
            # Создаем информацию о стратегии
            strategy_info = StrategyInfo(
                name=name,
                strategy_class=strategy_class,
                description=description or f"Strategy {name}",
                version=version,
                author=author,
                category=category,
                parameters_schema=parameters_schema or {},
                min_history_length=min_history_length,
                supported_timeframes=supported_timeframes or ['1m', '5m', '15m', '30m', '1h', '4h', '1d']
            )
            
            # Регистрируем
            cls._strategies[name] = strategy_info
            
            cls._logger.info(f"✅ Registered strategy: {name} ({strategy_class.__name__})")
            
        except Exception as e:
            cls._logger.error(f"❌ Failed to register strategy '{name}': {e}")
            raise
    
    @classmethod
    def unregister(cls, name: str) -> bool:
        """
        Отмена регистрации стратегии
        
        Args:
            name: Имя стратегии
            
        Returns:
            True если стратегия была удалена
        """
        if name in cls._strategies:
            del cls._strategies[name]
            cls._logger.info(f"🗑️ Unregistered strategy: {name}")
            return True
        else:
            cls._logger.warning(f"⚠️ Strategy '{name}' not found for unregistration")
            return False
    
    @classmethod
    def get_strategy_class(cls, name: str) -> Optional[Type[BaseStrategy]]:
        """
        Получение класса стратегии по имени
        
        Args:
            name: Имя стратегии
            
        Returns:
            Класс стратегии или None если не найдена
        """
        strategy_info = cls._strategies.get(name)
        return strategy_info.strategy_class if strategy_info else None
    
    @classmethod
    def create_strategy(cls, name: str, config: StrategyConfig) -> Optional[BaseStrategy]:
        """
        Создание экземпляра стратегии
        
        Args:
            name: Имя стратегии
            config: Конфигурация стратегии
            
        Returns:
            Экземпляр стратегии или None если не удалось создать
        """
        try:
            strategy_class = cls.get_strategy_class(name)
            
            if not strategy_class:
                cls._logger.error(f"❌ Strategy '{name}' not found in registry")
                return None
            
            # Валидируем конфигурацию
            strategy_info = cls._strategies[name]
            if config.timeframe not in strategy_info.supported_timeframes:
                cls._logger.warning(f"⚠️ Timeframe '{config.timeframe}' not officially supported by '{name}'")
            
            # Создаем экземпляр
            strategy_instance = strategy_class(config)
            
            cls._logger.info(f"🎯 Created strategy instance: {name} for {config.symbol}")
            return strategy_instance
            
        except Exception as e:
            cls._logger.error(f"❌ Failed to create strategy '{name}': {e}")
            return None
    
    @classmethod
    def list_strategies(cls, category: Optional[str] = None, active_only: bool = True) -> List[str]:
        """
        Получение списка зарегистрированных стратегий
        
        Args:
            category: Фильтр по категории
            active_only: Только активные стратегии
            
        Returns:
            Список имен стратегий
        """
        strategies = []
        
        for name, info in cls._strategies.items():
            # Фильтр по активности
            if active_only and not info.is_active:
                continue
            
            # Фильтр по категории
            if category and info.category != category:
                continue
            
            strategies.append(name)
        
        return sorted(strategies)
    
    @classmethod
    def get_strategy_info(cls, name: str) -> Optional[StrategyInfo]:
        """
        Получение информации о стратегии
        
        Args:
            name: Имя стратегии
            
        Returns:
            StrategyInfo или None
        """
        return cls._strategies.get(name)
    
    @classmethod
    def get_all_strategies_info(cls) -> Dict[str, Dict[str, Any]]:
        """
        Получение информации о всех стратегиях
        
        Returns:
            Словарь с информацией о стратегиях
        """
        return {name: info.to_dict() for name, info in cls._strategies.items()}
    
    @classmethod
    def get_categories(cls) -> List[str]:
        """
        Получение списка категорий стратегий
        
        Returns:
            Список уникальных категорий
        """
        categories = set()
        for info in cls._strategies.values():
            categories.add(info.category)
        return sorted(list(categories))
    
    @classmethod
    def search_strategies(cls, query: str) -> List[str]:
        """
        Поиск стратегий по названию или описанию
        
        Args:
            query: Поисковый запрос
            
        Returns:
            Список найденных стратегий
        """
        query = query.lower()
        found_strategies = []
        
        for name, info in cls._strategies.items():
            # Поиск в названии
            if query in name.lower():
                found_strategies.append(name)
                continue
            
            # Поиск в описании
            if query in info.description.lower():
                found_strategies.append(name)
                continue
            
            # Поиск в категории
            if query in info.category.lower():
                found_strategies.append(name)
                continue
        
        return sorted(found_strategies)
    
    @classmethod
    def validate_strategy_class(cls, strategy_class: Type[BaseStrategy]) -> Tuple[bool, List[str]]:
        """
        Валидация класса стратегии перед регистрацией
        
        Args:
            strategy_class: Класс стратегии
            
        Returns:
            (is_valid, errors_list)
        """
        errors = []
        
        try:
            # Проверяем что это класс
            if not inspect.isclass(strategy_class):
                errors.append("Must be a class")
                return False, errors
            
            # Проверяем наследование от BaseStrategy
            if not issubclass(strategy_class, BaseStrategy):
                errors.append("Must inherit from BaseStrategy")
            
            # Проверяем обязательные методы
            required_methods = ['analyze', 'get_required_history_length', 'validate_parameters']
            
            for method_name in required_methods:
                if not hasattr(strategy_class, method_name):
                    errors.append(f"Missing required method: {method_name}")
                else:
                    method = getattr(strategy_class, method_name)
                    if not callable(method):
                        errors.append(f"'{method_name}' must be callable")
            
            # Проверяем что analyze - async метод
            if hasattr(strategy_class, 'analyze'):
                analyze_method = getattr(strategy_class, 'analyze')
                if not inspect.iscoroutinefunction(analyze_method):
                    errors.append("'analyze' method must be async")
            
            return len(errors) == 0, errors
            
        except Exception as e:
            errors.append(f"Validation error: {str(e)}")
            return False, errors
    
    @classmethod
    def auto_discover_strategies(cls, strategies_path: Optional[str] = None) -> int:
        """
        Автоматическое обнаружение и регистрация стратегий из директории
        
        Args:
            strategies_path: Путь к директории со стратегиями
            
        Returns:
            Количество найденных стратегий
        """
        discovered = 0
        
        try:
            # Определяем путь к стратегиям
            if strategies_path is None:
                current_file = Path(__file__)
                strategies_path = current_file.parent
            else:
                strategies_path = Path(strategies_path)
            
            if not strategies_path.exists():
                cls._logger.warning(f"⚠️ Strategies path does not exist: {strategies_path}")
                return 0
            
            cls._logger.info(f"🔍 Auto-discovering strategies in: {strategies_path}")
            
            # Ищем Python файлы
            for strategy_file in strategies_path.glob("*.py"):
                # Пропускаем системные файлы
                if strategy_file.name.startswith(('__', 'base_', 'strategy_registry')):
                    continue
                
                try:
                    # Импортируем модуль
                    module_name = f"strategies.{strategy_file.stem}"
                    
                    if module_name in sys.modules:
                        # Перезагружаем если уже импортирован
                        module = importlib.reload(sys.modules[module_name])
                    else:
                        module = importlib.import_module(module_name)
                    
                    # Ищем классы стратегий в модуле
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        
                        # Проверяем что это класс стратегии
                        if (inspect.isclass(attr) and 
                            issubclass(attr, BaseStrategy) and 
                            attr != BaseStrategy):
                            
                            # Валидируем класс
                            is_valid, errors = cls.validate_strategy_class(attr)
                            
                            if is_valid:
                                # Автоматически регистрируем
                                strategy_name = attr_name.lower().replace('strategy', '')
                                if strategy_name.endswith('_'):
                                    strategy_name = strategy_name[:-1]
                                
                                # Проверяем что не зарегистрирована уже
                                if strategy_name not in cls._strategies:
                                    cls.register(
                                        name=strategy_name,
                                        strategy_class=attr,
                                        description=f"Auto-discovered {attr.__name__}",
                                        category="Auto-discovered"
                                    )
                                    discovered += 1
                            else:
                                cls._logger.warning(f"⚠️ Invalid strategy class {attr_name}: {errors}")
                
                except Exception as e:
                    cls._logger.error(f"❌ Failed to process {strategy_file.name}: {e}")
                    continue
            
            cls._logger.info(f"🎯 Auto-discovered {discovered} strategies")
            return discovered
            
        except Exception as e:
            cls._logger.error(f"❌ Auto-discovery failed: {e}")
            return 0
    
    @classmethod
    def activate_strategy(cls, name: str) -> bool:
        """Активация стратегии"""
        if name in cls._strategies:
            cls._strategies[name].is_active = True
            cls._logger.info(f"✅ Activated strategy: {name}")
            return True
        return False
    
    @classmethod
    def deactivate_strategy(cls, name: str) -> bool:
        """Деактивация стратегии"""
        if name in cls._strategies:
            cls._strategies[name].is_active = False
            cls._logger.info(f"⏸️ Deactivated strategy: {name}")
            return True
        return False
    
    @classmethod
    def clear_registry(cls) -> None:
        """Очистка реестра (для тестирования)"""
        cls._strategies.clear()
        cls._logger.info("🧹 Strategy registry cleared")
    
    @classmethod
    def get_registry_stats(cls) -> Dict[str, Any]:
        """Статистика реестра"""
        total_strategies = len(cls._strategies)
        active_strategies = sum(1 for info in cls._strategies.values() if info.is_active)
        categories = cls.get_categories()
        
        category_counts = {}
        for info in cls._strategies.values():
            category_counts[info.category] = category_counts.get(info.category, 0) + 1
        
        return {
            'total_strategies': total_strategies,
            'active_strategies': active_strategies,
            'inactive_strategies': total_strategies - active_strategies,
            'categories': categories,
            'strategies_by_category': category_counts
        }


# ============================================================================
# DECORATORS
# ============================================================================

def register_strategy(
    name: Optional[str] = None,
    description: str = "",
    version: str = "1.0.0",
    author: str = "Unknown",
    category: str = "General",
    parameters_schema: Optional[Dict[str, Any]] = None,
    supported_timeframes: Optional[List[str]] = None
):
    """
    Декоратор для автоматической регистрации стратегий
    
    Usage:
        @register_strategy(name="my_strategy", category="Trend")
        class MyStrategy(BaseStrategy):
            ...
    """
    def decorator(strategy_class: Type[BaseStrategy]) -> Type[BaseStrategy]:
        strategy_name = name or strategy_class.__name__.lower().replace('strategy', '')
        if strategy_name.endswith('_'):
            strategy_name = strategy_name[:-1]
        
        StrategyRegistry.register(
            name=strategy_name,
            strategy_class=strategy_class,
            description=description,
            version=version,
            author=author,
            category=category,
            parameters_schema=parameters_schema,
            supported_timeframes=supported_timeframes
        )
        
        return strategy_class
    
    return decorator


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def get_strategy_class(name: str) -> Optional[Type[BaseStrategy]]:
    """Быстрое получение класса стратегии"""
    return StrategyRegistry.get_strategy_class(name)


def create_strategy(name: str, symbol: str, timeframe: str, parameters: Dict[str, Any] = None) -> Optional[BaseStrategy]:
    """Быстрое создание стратегии"""
    config = StrategyConfig(
        name=name,
        symbol=symbol,
        timeframe=timeframe,
        parameters=parameters or {}
    )
    return StrategyRegistry.create_strategy(name, config)


def list_available_strategies() -> List[str]:
    """Список доступных стратегий"""
    return StrategyRegistry.list_strategies()


# ============================================================================
# EXAMPLE USAGE AND TESTING
# ============================================================================

if __name__ == "__main__":
    # Пример использования реестра стратегий
    
    print("🧪 Testing Strategy Registry")
    print("=" * 50)
    
    # Тестируем базовую функциональность
    from .base_strategy import BaseStrategy, StrategyConfig, StrategyResult, SignalType
    from data.historical_data import CandleData
    from typing import Tuple
    
    # Создаем тестовую стратегию
    class TestStrategy(BaseStrategy):
        """Тестовая стратегия для демонстрации"""
        
        async def analyze(self, data: List[CandleData]) -> StrategyResult:
            # Простая логика
            current_price = self.get_latest_price(data)
            return StrategyResult(
                signal_type=SignalType.BUY,
                confidence=0.8,
                entry_price=current_price,
                reasoning="Test strategy signal"
            )
        
        def get_required_history_length(self) -> int:
            return 10
        
        def validate_parameters(self) -> Tuple[bool, List[str]]:
            return True, []
    
    try:
        # Тест 1: Регистрация стратегии
        print("📝 Test 1: Registering strategy...")
        StrategyRegistry.register(
            name="test_strategy",
            strategy_class=TestStrategy,
            description="Simple test strategy",
            category="Test"
        )
        print("✅ Strategy registered")
        
        # Тест 2: Список стратегий
        print("\n📝 Test 2: Listing strategies...")
        strategies = StrategyRegistry.list_strategies()
        print(f"✅ Found strategies: {strategies}")
        
        # Тест 3: Создание экземпляра
        print("\n📝 Test 3: Creating strategy instance...")
        config = StrategyConfig(
            name="test_strategy",
            symbol="BTCUSDT",
            timeframe="5m",
            parameters={}
        )
        strategy_instance = StrategyRegistry.create_strategy("test_strategy", config)
        print(f"✅ Created instance: {strategy_instance}")
        
        # Тест 4: Информация о стратегии
        print("\n📝 Test 4: Getting strategy info...")
        info = StrategyRegistry.get_strategy_info("test_strategy")
        print(f"✅ Strategy info: {info.name}, category: {info.category}")
        
        # Тест 5: Статистика реестра
        print("\n📝 Test 5: Registry stats...")
        stats = StrategyRegistry.get_registry_stats()
        print(f"✅ Registry stats: {stats}")
        
        # Тест 6: Декоратор регистрации
        print("\n📝 Test 6: Testing decorator...")
        
        @register_strategy(name="decorated_strategy", category="Decorated")
        class DecoratedStrategy(BaseStrategy):
            async def analyze(self, data):
                return StrategyResult(SignalType.HOLD, 0.5)
            
            def get_required_history_length(self):
                return 5
            
            def validate_parameters(self):
                return True, []
        
        decorated_strategies = StrategyRegistry.list_strategies()
        print(f"✅ Strategies after decoration: {decorated_strategies}")
        
        print("\n✅ All strategy registry tests completed!")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
