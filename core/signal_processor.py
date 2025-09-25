"""
Trading Bot Signal Processor
Обработка торговых сигналов от стратегий с фильтрацией, валидацией и отправкой
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Callable, Set, Union, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import deque, defaultdict
import threading
from concurrent.futures import ThreadPoolExecutor
import uuid

from utils.logger import setup_logger, log_trading_event
from utils.helpers import (
    get_current_timestamp, safe_float, validate_symbol, Timer,
    round_price, round_quantity, calculate_percentage_change
)
from app.config.settings import Settings, get_settings
from data.database import Database
from data.models import SignalType as DBSignalType
from strategies.base_strategy import BaseStrategy, StrategyResult, SignalType, SignalStrength
from data.historical_data import CandleData


# ============================================================================
# TYPES AND CONSTANTS
# ============================================================================

logger = setup_logger(__name__)

# Конфигурация процессора
DEFAULT_MAX_SIGNALS_PER_HOUR = 10
DEFAULT_MIN_CONFIDENCE = 0.7
DEFAULT_COOLDOWN_MINUTES = 5
DEFAULT_MAX_QUEUE_SIZE = 1000

# Таймауты
SIGNAL_PROCESSING_TIMEOUT = 10  # секунды
AI_ANALYSIS_TIMEOUT = 30  # секунды
NOTIFICATION_TIMEOUT = 5  # секунды


class ProcessingStatus(str, Enum):
    """Статусы обработки сигналов"""
    PENDING = "pending"
    PROCESSING = "processing"
    VALIDATED = "validated"
    FILTERED = "filtered"
    ENRICHED = "enriched"
    STORED = "stored"
    NOTIFIED = "notified"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class FilterReason(str, Enum):
    """Причины фильтрации сигналов"""
    LOW_CONFIDENCE = "low_confidence"
    COOLDOWN_ACTIVE = "cooldown_active"
    HOURLY_LIMIT = "hourly_limit"
    DUPLICATE_SIGNAL = "duplicate_signal"
    INVALID_PRICE = "invalid_price"
    INSUFFICIENT_VOLUME = "insufficient_volume"
    RISK_LIMIT = "risk_limit"
    MARKET_CLOSED = "market_closed"


@dataclass
class ProcessedSignal:
    """Обработанный торговый сигнал"""
    id: str
    original_signal: StrategyResult
    status: ProcessingStatus
    
    # Обогащенные данные
    market_price: Optional[float] = None
    spread: Optional[float] = None
    volume_24h: Optional[float] = None
    price_change_24h: Optional[float] = None
    
    # AI анализ
    ai_analysis: Optional[str] = None
    ai_confidence: Optional[float] = None
    ai_processed_at: Optional[int] = None
    
    # Метаданные обработки
    processed_at: int = field(default_factory=get_current_timestamp)
    processing_time_ms: Optional[float] = None
    filter_reason: Optional[FilterReason] = None
    notifications_sent: List[str] = field(default_factory=list)
    
    # Дополнительная информация
    risk_score: Optional[float] = None
    execution_priority: int = 1  # 1=низкий, 5=высокий
    
    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь"""
        return {
            'id': self.id,
            'status': self.status.value,
            'signal_type': self.original_signal.signal_type.value,
            'confidence': self.original_signal.confidence,
            'entry_price': self.original_signal.entry_price,
            'stop_loss': self.original_signal.stop_loss,
            'take_profit': self.original_signal.take_profit,
            'reasoning': self.original_signal.reasoning,
            'market_price': self.market_price,
            'spread': self.spread,
            'volume_24h': self.volume_24h,
            'price_change_24h': self.price_change_24h,
            'ai_analysis': self.ai_analysis,
            'ai_confidence': self.ai_confidence,
            'processed_at': self.processed_at,
            'processing_time_ms': self.processing_time_ms,
            'risk_score': self.risk_score,
            'execution_priority': self.execution_priority,
            'filter_reason': self.filter_reason.value if self.filter_reason else None
        }


@dataclass
class SignalProcessingConfig:
    """Конфигурация обработки сигналов"""
    max_signals_per_hour: int = DEFAULT_MAX_SIGNALS_PER_HOUR
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES
    max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE
    
    # Фильтры
    enable_confidence_filter: bool = True
    enable_cooldown_filter: bool = True
    enable_hourly_limit_filter: bool = True
    enable_duplicate_filter: bool = True
    enable_market_data_enrichment: bool = True
    enable_ai_analysis: bool = True
    enable_risk_scoring: bool = True
    
    # Уведомления
    enable_telegram_notifications: bool = True
    enable_email_notifications: bool = False
    enable_webhook_notifications: bool = False
    
    # Производительность
    max_concurrent_processing: int = 5
    processing_timeout: int = SIGNAL_PROCESSING_TIMEOUT


# ============================================================================
# SIGNAL PROCESSOR CLASS
# ============================================================================

class SignalProcessor:
    """
    Процессор торговых сигналов
    
    Функции:
    - Валидация входящих сигналов
    - Фильтрация по различным критериям
    - Обогащение рыночными данными
    - AI анализ сигналов
    - Расчет риск-скора
    - Отправка уведомлений
    - Сохранение в базу данных
    """
    
    def __init__(
        self,
        config: Optional[SignalProcessingConfig] = None,
        settings: Optional[Settings] = None,
        database: Optional[Database] = None
    ):
        self.config = config or SignalProcessingConfig()
        self.settings = settings or get_settings()
        self.database = database
        self.logger = setup_logger(f"{__name__}.SignalProcessor")
        
        # Очереди сигналов
        self._incoming_queue: asyncio.Queue = asyncio.Queue(maxsize=self.config.max_queue_size)
        self._processing_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._completed_signals: deque = deque(maxlen=1000)
        
        # Кеши для фильтрации
        self._recent_signals: Dict[str, List[int]] = defaultdict(list)  # symbol -> timestamps
        self._signal_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self._cooldown_tracker: Dict[str, int] = {}  # strategy_symbol -> last_signal_time
        
        # Состояние процессора
        self._is_running = False
        self._processing_tasks: Set[asyncio.Task] = set()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="SignalProcessor")
        
        # Callbacks
        self._signal_callbacks: List[Callable[[ProcessedSignal], None]] = []
        self._error_callbacks: List[Callable[[Exception, ProcessedSignal], None]] = []
        
        # Статистика
        self.stats = {
            'total_received': 0,
            'total_processed': 0,
            'total_rejected': 0,
            'total_filtered': 0,
            'processing_errors': 0,
            'avg_processing_time_ms': 0.0,
            'filters_applied': defaultdict(int),
            'signals_by_type': defaultdict(int),
            'signals_by_strategy': defaultdict(int)
        }
        
        # Внешние сервисы (будут инжектированы)
        self.market_data_provider = None
        self.ai_service = None
        self.notification_service = None
        
        self.logger.info("🎯 Signal Processor initialized")
    
    # ========================================================================
    # LIFECYCLE MANAGEMENT
    # ========================================================================
    
    async def start(self) -> None:
        """Запуск процессора сигналов"""
        if self._is_running:
            return
        
        try:
            self.logger.info("🚀 Starting Signal Processor...")
            self._is_running = True
            
            # Запускаем фоновые задачи
            self._start_background_tasks()
            
            self.logger.info("✅ Signal Processor started successfully")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to start Signal Processor: {e}")
            await self.stop()
            raise
    
    async def stop(self) -> None:
        """Остановка процессора"""
        if not self._is_running:
            return
        
        try:
            self.logger.info("🔄 Stopping Signal Processor...")
            self._is_running = False
            
            # Отменяем все задачи
            for task in self._processing_tasks:
                if not task.done():
                    task.cancel()
            
            if self._processing_tasks:
                await asyncio.gather(*self._processing_tasks, return_exceptions=True)
            
            # Закрываем executor
            self._executor.shutdown(wait=True)
            
            self.logger.info("✅ Signal Processor stopped")
            
        except Exception as e:
            self.logger.error(f"❌ Error stopping Signal Processor: {e}")
    
    def _start_background_tasks(self) -> None:
        """Запуск фоновых задач"""
        # Основная задача обработки
        for i in range(self.config.max_concurrent_processing):
            task = asyncio.create_task(
                self._signal_processing_worker(f"worker-{i}"),
                name=f"signal_processor_worker_{i}"
            )
            self._processing_tasks.add(task)
        
        # Задача мониторинга очередей
        task = asyncio.create_task(
            self._queue_monitor_task(),
            name="signal_processor_monitor"
        )
        self._processing_tasks.add(task)
        
        # Задача очистки старых данных
        task = asyncio.create_task(
            self._cleanup_task(),
            name="signal_processor_cleanup"
        )
        self._processing_tasks.add(task)
    
    # ========================================================================
    # SIGNAL PROCESSING PIPELINE
    # ========================================================================
    
    async def process_signal(
        self,
        signal: StrategyResult,
        strategy_name: str,
        symbol: str,
        timeframe: str
    ) -> Optional[ProcessedSignal]:
        """
        Обработка одного сигнала
        
        Args:
            signal: Результат стратегии
            strategy_name: Имя стратегии
            symbol: Торговая пара
            timeframe: Таймфрейм
            
        Returns:
            Обработанный сигнал или None если отфильтрован
        """
        try:
            if not self._is_running:
                self.logger.warning("⚠️ Signal processor is not running")
                return None
            
            # Добавляем в очередь
            processed_signal = ProcessedSignal(
                id=str(uuid.uuid4()),
                original_signal=signal,
                status=ProcessingStatus.PENDING
            )
            
            # Добавляем метаданные
            signal.metadata.update({
                'strategy_name': strategy_name,
                'symbol': symbol,
                'timeframe': timeframe,
                'received_at': get_current_timestamp()
            })
            
            # Отправляем в очередь для обработки
            try:
                await asyncio.wait_for(
                    self._incoming_queue.put(processed_signal),
                    timeout=1.0
                )
                
                self.stats['total_received'] += 1
                self.stats['signals_by_strategy'][strategy_name] += 1
                self.stats['signals_by_type'][signal.signal_type.value] += 1
                
                log_trading_event(
                    event_type="SIGNAL_RECEIVED",
                    symbol=symbol,
                    strategy=strategy_name,
                    message=f"🎯 {signal.signal_type.value} signal received (confidence: {signal.confidence:.2f})",
                    confidence=signal.confidence,
                    entry_price=signal.entry_price
                )
                
                return processed_signal
                
            except asyncio.TimeoutError:
                self.logger.warning(f"⚠️ Signal queue is full, dropping signal for {symbol}")
                return None
                
        except Exception as e:
            self.logger.error(f"❌ Failed to process signal: {e}")
            self.stats['processing_errors'] += 1
            return None
    
    async def _signal_processing_worker(self, worker_name: str) -> None:
        """Воркер для обработки сигналов"""
        while self._is_running:
            try:
                # Получаем сигнал из очереди
                try:
                    processed_signal = await asyncio.wait_for(
                        self._incoming_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                # Обрабатываем сигнал
                start_time = asyncio.get_event_loop().time()
                
                try:
                    await self._process_single_signal(processed_signal)
                    processing_time = (asyncio.get_event_loop().time() - start_time) * 1000
                    processed_signal.processing_time_ms = processing_time
                    
                    # Обновляем статистику
                    self.stats['total_processed'] += 1
                    self._update_avg_processing_time(processing_time)
                    
                    # Добавляем в историю
                    self._completed_signals.append(processed_signal)
                    
                    # Вызываем callbacks
                    await self._call_signal_callbacks(processed_signal)
                    
                    self.logger.debug(f"✅ Signal processed by {worker_name}: {processed_signal.id}")
                    
                except Exception as e:
                    processed_signal.status = ProcessingStatus.FAILED
                    self.stats['processing_errors'] += 1
                    
                    await self._call_error_callbacks(e, processed_signal)
                    self.logger.error(f"❌ Signal processing failed in {worker_name}: {e}")
                
                finally:
                    self._incoming_queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Worker {worker_name} error: {e}")
                await asyncio.sleep(1)
    
    async def _process_single_signal(self, processed_signal: ProcessedSignal) -> None:
        """Обработка одного сигнала через pipeline"""
        signal = processed_signal.original_signal
        symbol = signal.metadata.get('symbol')
        strategy_name = signal.metadata.get('strategy_name')
        
        try:
            with Timer(f"process_signal_{symbol}"):
                processed_signal.status = ProcessingStatus.PROCESSING
                
                # 1. Валидация
                if not await self._validate_signal(processed_signal):
                    processed_signal.status = ProcessingStatus.REJECTED
                    self.stats['total_rejected'] += 1
                    return
                
                processed_signal.status = ProcessingStatus.VALIDATED
                
                # 2. Фильтрация
                filter_result = await self._apply_filters(processed_signal)
                if filter_result:
                    processed_signal.status = ProcessingStatus.FILTERED
                    processed_signal.filter_reason = filter_result
                    self.stats['total_filtered'] += 1
                    self.stats['filters_applied'][filter_result.value] += 1
                    
                    self.logger.debug(f"🚫 Signal filtered: {filter_result.value} for {symbol}")
                    return
                
                # 3. Обогащение рыночными данными
                if self.config.enable_market_data_enrichment:
                    await self._enrich_with_market_data(processed_signal)
                
                processed_signal.status = ProcessingStatus.ENRICHED
                
                # 4. AI анализ
                if self.config.enable_ai_analysis and self.ai_service:
                    await self._perform_ai_analysis(processed_signal)
                
                # 5. Расчет риск-скора
                if self.config.enable_risk_scoring:
                    await self._calculate_risk_score(processed_signal)
                
                # 6. Сохранение в БД
                if self.database:
                    await self._save_to_database(processed_signal)
                
                processed_signal.status = ProcessingStatus.STORED
                
                # 7. Отправка уведомлений
                await self._send_notifications(processed_signal)
                
                processed_signal.status = ProcessingStatus.COMPLETED
                
                log_trading_event(
                    event_type="SIGNAL_PROCESSED",
                    symbol=symbol,
                    strategy=strategy_name,
                    message=f"✅ {signal.signal_type.value} signal processed successfully",
                    processing_time_ms=processed_signal.processing_time_ms,
                    risk_score=processed_signal.risk_score
                )
                
        except Exception as e:
            processed_signal.status = ProcessingStatus.FAILED
            self.logger.error(f"❌ Signal processing pipeline failed: {e}")
            raise
    
    # ========================================================================
    # VALIDATION
    # ========================================================================
    
    async def _validate_signal(self, processed_signal: ProcessedSignal) -> bool:
        """Валидация сигнала"""
        try:
            signal = processed_signal.original_signal
            
            # Проверяем обязательные поля
            if not signal.signal_type or not isinstance(signal.signal_type, SignalType):
                self.logger.error("❌ Invalid signal type")
                return False
            
            if not (0 <= signal.confidence <= 1):
                self.logger.error(f"❌ Invalid confidence: {signal.confidence}")
                return False
            
            # Проверяем цены
            if signal.entry_price is not None:
                if not isinstance(signal.entry_price, (int, float)) or signal.entry_price <= 0:
                    self.logger.error(f"❌ Invalid entry price: {signal.entry_price}")
                    return False
            
            if signal.stop_loss is not None:
                if not isinstance(signal.stop_loss, (int, float)) or signal.stop_loss <= 0:
                    self.logger.error(f"❌ Invalid stop loss: {signal.stop_loss}")
                    return False
            
            if signal.take_profit is not None:
                if not isinstance(signal.take_profit, (int, float)) or signal.take_profit <= 0:
                    self.logger.error(f"❌ Invalid take profit: {signal.take_profit}")
                    return False
            
            # Проверяем логику цен
            if (signal.signal_type in [SignalType.BUY, SignalType.STRONG_BUY] and 
                signal.entry_price and signal.stop_loss):
                if signal.stop_loss >= signal.entry_price:
                    self.logger.error("❌ Stop loss should be below entry price for BUY signals")
                    return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Signal validation error: {e}")
            return False
    
    # ========================================================================
    # FILTERING
    # ========================================================================
    
    async def _apply_filters(self, processed_signal: ProcessedSignal) -> Optional[FilterReason]:
        """Применение фильтров к сигналу"""
        try:
            signal = processed_signal.original_signal
            symbol = signal.metadata.get('symbol')
            strategy_name = signal.metadata.get('strategy_name')
            
            # Фильтр по confidence
            if (self.config.enable_confidence_filter and 
                signal.confidence < self.config.min_confidence):
                return FilterReason.LOW_CONFIDENCE
            
            # Фильтр cooldown
            if self.config.enable_cooldown_filter:
                cooldown_key = f"{strategy_name}_{symbol}"
                last_signal = self._cooldown_tracker.get(cooldown_key, 0)
                cooldown_ms = self.config.cooldown_minutes * 60 * 1000
                
                if get_current_timestamp() - last_signal < cooldown_ms:
                    return FilterReason.COOLDOWN_ACTIVE
                
                # Обновляем время последнего сигнала
                self._cooldown_tracker[cooldown_key] = get_current_timestamp()
            
            # Фильтр лимита в час
            if self.config.enable_hourly_limit_filter:
                if not self._check_hourly_limit(symbol):
                    return FilterReason.HOURLY_LIMIT
            
            # Фильтр дубликатов
            if self.config.enable_duplicate_filter:
                if self._is_duplicate_signal(processed_signal):
                    return FilterReason.DUPLICATE_SIGNAL
            
            # Добавляем сигнал в историю
            self._add_to_signal_history(processed_signal)
            
            return None  # Не отфильтрован
            
        except Exception as e:
            self.logger.error(f"❌ Filter application error: {e}")
            return FilterReason.LOW_CONFIDENCE  # Безопасная фильтрация при ошибке
    
    def _check_hourly_limit(self, symbol: str) -> bool:
        """Проверка лимита сигналов в час"""
        try:
            current_time = get_current_timestamp()
            hour_ago = current_time - (60 * 60 * 1000)  # час назад
            
            # Очищаем старые записи
            recent_signals = self._recent_signals[symbol]
            self._recent_signals[symbol] = [
                ts for ts in recent_signals if ts > hour_ago
            ]
            
            # Проверяем лимит
            if len(self._recent_signals[symbol]) >= self.config.max_signals_per_hour:
                return False
            
            # Добавляем текущий сигнал
            self._recent_signals[symbol].append(current_time)
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Hourly limit check error: {e}")
            return True  # При ошибке разрешаем сигнал
    
    def _is_duplicate_signal(self, processed_signal: ProcessedSignal) -> bool:
        """Проверка на дубликат сигнала"""
        try:
            signal = processed_signal.original_signal
            symbol = signal.metadata.get('symbol')
            
            # Получаем недавние сигналы для символа
            recent_signals = list(self._signal_history[symbol])
            
            # Проверяем последние 5 сигналов
            for recent_signal in recent_signals[-5:]:
                if (recent_signal.original_signal.signal_type == signal.signal_type and
                    abs(recent_signal.original_signal.confidence - signal.confidence) < 0.05 and
                    recent_signal.original_signal.entry_price and signal.entry_price and
                    abs(recent_signal.original_signal.entry_price - signal.entry_price) / signal.entry_price < 0.001):  # 0.1% разница
                    return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"❌ Duplicate check error: {e}")
            return False
    
    def _add_to_signal_history(self, processed_signal: ProcessedSignal) -> None:
        """Добавление сигнала в историю"""
        try:
            symbol = processed_signal.original_signal.metadata.get('symbol')
            self._signal_history[symbol].append(processed_signal)
        except Exception as e:
            self.logger.error(f"❌ Add to history error: {e}")
    
    # ========================================================================
    # MARKET DATA ENRICHMENT
    # ========================================================================
    
    async def _enrich_with_market_data(self, processed_signal: ProcessedSignal) -> None:
        """Обогащение сигнала рыночными данными"""
        try:
            if not self.market_data_provider:
                return
            
            symbol = processed_signal.original_signal.metadata.get('symbol')
            
            # Получаем текущую рыночную цену
            current_price = await self._get_current_price(symbol)
            if current_price:
                processed_signal.market_price = current_price
                
                # Рассчитываем спред если есть entry_price
                if processed_signal.original_signal.entry_price:
                    spread = abs(current_price - processed_signal.original_signal.entry_price)
                    processed_signal.spread = spread
            
            # Получаем объем за 24 часа
            volume_24h = await self._get_24h_volume(symbol)
            if volume_24h:
                processed_signal.volume_24h = volume_24h
            
            # Получаем изменение цены за 24 часа
            price_change = await self._get_24h_price_change(symbol)
            if price_change is not None:
                processed_signal.price_change_24h = price_change
            
            self.logger.debug(f"📊 Enriched signal for {symbol}: price={current_price}, volume={volume_24h}")
            
        except Exception as e:
            self.logger.error(f"❌ Market data enrichment error: {e}")
    
    async def _get_current_price(self, symbol: str) -> Optional[float]:
        """Получение текущей цены"""
        try:
            if hasattr(self.market_data_provider, 'get_latest_price'):
                return await self.market_data_provider.get_latest_price(symbol)
        except Exception as e:
            self.logger.error(f"❌ Get current price error: {e}")
        return None
    
    async def _get_24h_volume(self, symbol: str) -> Optional[float]:
        """Получение объема за 24 часа"""
        try:
            if hasattr(self.market_data_provider, 'get_volume_24h'):
                return await self.market_data_provider.get_volume_24h(symbol)
        except Exception as e:
            self.logger.error(f"❌ Get 24h volume error: {e}")
        return None
    
    async def _get_24h_price_change(self, symbol: str) -> Optional[float]:
        """Получение изменения цены за 24 часа"""
        try:
            if hasattr(self.market_data_provider, 'get_price_change_24h'):
                change_data = await self.market_data_provider.get_price_change_24h(symbol)
                return change_data.get('price_change_pct', 0.0)
        except Exception as e:
            self.logger.error(f"❌ Get 24h price change error: {e}")
        return None
    
    # ========================================================================
    # AI ANALYSIS
    # ========================================================================
    
    async def _perform_ai_analysis(self, processed_signal: ProcessedSignal) -> None:
        """AI анализ сигнала"""
        try:
            if not self.ai_service:
                return
            
            signal = processed_signal.original_signal
            symbol = signal.metadata.get('symbol')
            
            # Подготавливаем данные для AI
            ai_prompt = self._prepare_ai_prompt(processed_signal)
            
            # Отправляем на анализ с таймаутом
            try:
                ai_result = await asyncio.wait_for(
                    self.ai_service.analyze_signal(ai_prompt),
                    timeout=AI_ANALYSIS_TIMEOUT
                )
                
                if ai_result:
                    processed_signal.ai_analysis = ai_result.get('analysis', '')
                    processed_signal.ai_confidence = ai_result.get('confidence', 0.5)
                    processed_signal.ai_processed_at = get_current_timestamp()
                    
                    self.logger.debug(f"🧠 AI analysis completed for {symbol}")
                
            except asyncio.TimeoutError:
                self.logger.warning(f"⏰ AI analysis timeout for {symbol}")
            
        except Exception as e:
            self.logger.error(f"❌ AI analysis error: {e}")
    
    def _prepare_ai_prompt(self, processed_signal: ProcessedSignal) -> str:
        """Подготовка промпта для AI анализа"""
        try:
            signal = processed_signal.original_signal
            
            prompt_parts = [
                f"Signal Type: {signal.signal_type.value}",
                f"Confidence: {signal.confidence:.2f}",
                f"Entry Price: {signal.entry_price}",
                f"Stop Loss: {signal.stop_loss}",
                f"Take Profit: {signal.take_profit}",
                f"Strategy Reasoning: {signal.reasoning}",
            ]
            
            if processed_signal.market_price:
                prompt_parts.append(f"Current Market Price: {processed_signal.market_price}")
            
            if processed_signal.volume_24h:
                prompt_parts.append(f"24h Volume: {processed_signal.volume_24h}")
            
            if processed_signal.price_change_24h is not None:
                prompt_parts.append(f"24h Price Change: {processed_signal.price_change_24h:.2f}%")
            
            prompt_parts.append("Please analyze this trading signal and provide your assessment.")
            
            return "\n".join(prompt_parts)
            
        except Exception as e:
            self.logger.error(f"❌ AI prompt preparation error: {e}")
            return "Signal analysis requested"
    
    # ========================================================================
    # RISK SCORING
    # ========================================================================
    
    async def _calculate_risk_score(self, processed_signal: ProcessedSignal) -> None:
        """Расчет риск-скора"""
        try:
            signal = processed_signal.original_signal
            risk_factors = []
            
            # Базовый риск по confidence
            confidence_risk = 1.0 - signal.confidence  # Чем меньше уверенность, тем выше риск
            risk_factors.append(confidence_risk * 0.4)  # 40% веса
            
            # Риск по волатильности (на основе 24h изменения)
            if processed_signal.price_change_24h is not None:
                volatility_risk = min(abs(processed_signal.price_change_24h) / 10.0, 1.0)  # Нормализуем к 10%
                risk_factors.append(volatility_risk * 0.3)  # 30% веса
            
            # Риск по ликвидности (на основе объема)
            if processed_signal.volume_24h is not None:
                # Простая логика: меньше объем = больше риск
                volume_risk = max(0, 1.0 - (processed_signal.volume_24h / 1000000))  # Нормализуем к 1M
                risk_factors.append(volume_risk * 0.2)  # 20% веса
            
            # Риск по размеру стоп-лосса
            if signal.entry_price and signal.stop_loss:
                stop_loss_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
                stop_loss_risk = min(stop_loss_pct / 0.05, 1.0)  # Нормализуем к 5%
                risk_factors.append(stop_loss_risk * 0.1)  # 10% веса
            
            # Расчитываем финальный риск-скор
            if risk_factors:
                processed_signal.risk_score = sum(risk_factors) / len(risk_factors)
                
                # Устанавливаем приоритет выполнения на основе риска
                if processed_signal.risk_score < 0.3:
                    processed_signal.execution_priority = 5  # Высокий приоритет
                elif processed_signal.risk_score < 0.6:
                    processed_signal.execution_priority = 3  # Средний приоритет
                else:
                    processed_signal.execution_priority = 1  # Низкий приоритет
            
            self.logger.debug(f"⚖️ Risk score calculated: {processed_signal.risk_score:.3f}")
            
        except Exception as e:
            self.logger.error(f"❌ Risk scoring error: {e}")
            processed_signal.risk_score = 0.5  # Средний риск по умолчанию
    
    # ========================================================================
    # DATABASE OPERATIONS
    # ========================================================================
    
    async def _save_to_database(self, processed_signal: ProcessedSignal) -> None:
        """Сохранение сигнала в базу данных"""
        try:
            if not self.database:
                return
            
            signal = processed_signal.original_signal
            
            # Подготавливаем данные для сохранения
            signal_data = {
                'symbol': signal.metadata.get('symbol'),
                'strategy_name': signal.metadata.get('strategy_name'),
                'signal_type': signal.signal_type.value,
                'confidence': signal.confidence,
                'entry_price': signal.entry_price,
                'stop_loss': signal.stop_loss,
                'take_profit': signal.take_profit,
                'timeframe': signal.metadata.get('timeframe'),
                'indicators_data': signal.indicators_data,
                'ai_analysis': processed_signal.ai_analysis,
                'ai_confidence': processed_signal.ai_confidence
            }
            
            # Сохраняем в БД
            signal_id = await self.database.save_signal(signal_data)
            
            # Обновляем AI анализ если есть
            if processed_signal.ai_analysis and processed_signal.ai_confidence:
                await self.database.update_signal_ai_analysis(
                    signal_id,
                    processed_signal.ai_analysis,
                    processed_signal.ai_confidence
                )
            
            # Сохраняем ID сигнала в метаданных
            signal.metadata['database_id'] = signal_id
            
            self.logger.debug(f"💾 Signal saved to database: ID {signal_id}")
            
        except Exception as e:
            self.logger.error(f"❌ Database save error: {e}")
    
    # ========================================================================
    # NOTIFICATIONS
    # ========================================================================
    
    async def _send_notifications(self, processed_signal: ProcessedSignal) -> None:
        """Отправка уведомлений о сигнале"""
        try:
            if not self.notification_service:
                return
            
            signal = processed_signal.original_signal
            symbol = signal.metadata.get('symbol')
            strategy = signal.metadata.get('strategy_name')
            
            # Формируем сообщение уведомления
            message = self._format_notification_message(processed_signal)
            
            # Отправляем уведомления
            notifications_sent = []
            
            # Telegram
            if self.config.enable_telegram_notifications:
                try:
                    success = await asyncio.wait_for(
                        self.notification_service.send_telegram(message),
                        timeout=NOTIFICATION_TIMEOUT
                    )
                    if success:
                        notifications_sent.append('telegram')
                except Exception as e:
                    self.logger.error(f"❌ Telegram notification failed: {e}")
            
            # Email (если включен)
            if self.config.enable_email_notifications:
                try:
                    success = await asyncio.wait_for(
                        self.notification_service.send_email(
                            subject=f"Trading Signal: {signal.signal_type.value} {symbol}",
                            message=message
                        ),
                        timeout=NOTIFICATION_TIMEOUT
                    )
                    if success:
                        notifications_sent.append('email')
                except Exception as e:
                    self.logger.error(f"❌ Email notification failed: {e}")
            
            processed_signal.notifications_sent = notifications_sent
            processed_signal.status = ProcessingStatus.NOTIFIED
            
            if notifications_sent:
                self.logger.info(f"📢 Notifications sent for {symbol}: {', '.join(notifications_sent)}")
            
        except Exception as e:
            self.logger.error(f"❌ Notification error: {e}")
    
    def _format_notification_message(self, processed_signal: ProcessedSignal) -> str:
        """Форматирование сообщения уведомления"""
        try:
            signal = processed_signal.original_signal
            symbol = signal.metadata.get('symbol', 'Unknown')
            strategy = signal.metadata.get('strategy_name', 'Unknown')
            
            # Эмодзи для типов сигналов
            signal_emoji = {
                'BUY': '🟢',
                'SELL': '🔴', 
                'STRONG_BUY': '💚',
                'STRONG_SELL': '❤️',
                'HOLD': '🟡'
            }
            
            emoji = signal_emoji.get(signal.signal_type.value, '🎯')
            
            lines = [
                f"{emoji} **{signal.signal_type.value} SIGNAL**",
                f"Symbol: **{symbol}**",
                f"Strategy: {strategy}",
                f"Confidence: **{signal.confidence:.2f}** ({signal.strength.value})",
                ""
            ]
            
            if signal.entry_price:
                lines.append(f"Entry Price: **${signal.entry_price:.4f}**")
            
            if processed_signal.market_price:
                lines.append(f"Current Price: ${processed_signal.market_price:.4f}")
            
            if signal.stop_loss:
                lines.append(f"Stop Loss: ${signal.stop_loss:.4f}")
            
            if signal.take_profit:
                lines.append(f"Take Profit: ${signal.take_profit:.4f}")
            
            if processed_signal.risk_score is not None:
                risk_level = "Low" if processed_signal.risk_score < 0.3 else "Medium" if processed_signal.risk_score < 0.7 else "High"
                lines.append(f"Risk: **{risk_level}** ({processed_signal.risk_score:.2f})")
            
            if signal.reasoning:
                lines.extend(["", f"Reasoning: _{signal.reasoning}_"])
            
            if processed_signal.ai_analysis:
                lines.extend(["", f"AI Analysis: _{processed_signal.ai_analysis[:200]}..._"])
            
            return "\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"❌ Message formatting error: {e}")
            return f"Trading signal for {signal.metadata.get('symbol', 'Unknown')}"
    
    # ========================================================================
    # BACKGROUND TASKS
    # ========================================================================
    
    async def _queue_monitor_task(self) -> None:
        """Мониторинг очередей"""
        while self._is_running:
            try:
                incoming_size = self._incoming_queue.qsize()
                processing_size = self._processing_queue.qsize()
                
                # Логируем если очереди переполняются
                if incoming_size > self.config.max_queue_size * 0.8:
                    self.logger.warning(f"⚠️ Incoming queue nearly full: {incoming_size}")
                
                # Статистика каждые 60 секунд
                await asyncio.sleep(60)
                
                if self.stats['total_received'] > 0:
                    self.logger.info(f"📊 Signal Processor Stats: {self.get_stats()}")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Queue monitor error: {e}")
                await asyncio.sleep(30)
    
    async def _cleanup_task(self) -> None:
        """Очистка старых данных"""
        while self._is_running:
            try:
                await asyncio.sleep(300)  # Каждые 5 минут
                
                current_time = get_current_timestamp()
                hour_ago = current_time - (60 * 60 * 1000)
                
                # Очищаем старые записи о недавних сигналах
                for symbol in list(self._recent_signals.keys()):
                    self._recent_signals[symbol] = [
                        ts for ts in self._recent_signals[symbol] if ts > hour_ago
                    ]
                    if not self._recent_signals[symbol]:
                        del self._recent_signals[symbol]
                
                # Очищаем старые cooldown записи
                minute_ago = current_time - (60 * 1000)
                keys_to_remove = [
                    key for key, timestamp in self._cooldown_tracker.items()
                    if timestamp < minute_ago
                ]
                for key in keys_to_remove:
                    del self._cooldown_tracker[key]
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Cleanup task error: {e}")
                await asyncio.sleep(60)
    
    # ========================================================================
    # CALLBACKS AND EVENTS
    # ========================================================================
    
    def add_signal_callback(self, callback: Callable[[ProcessedSignal], None]) -> None:
        """Добавление callback для обработанных сигналов"""
        self._signal_callbacks.append(callback)
        self.logger.debug("📡 Signal callback added")
    
    def remove_signal_callback(self, callback: Callable[[ProcessedSignal], None]) -> None:
        """Удаление callback"""
        if callback in self._signal_callbacks:
            self._signal_callbacks.remove(callback)
            self.logger.debug("📡 Signal callback removed")
    
    def add_error_callback(self, callback: Callable[[Exception, ProcessedSignal], None]) -> None:
        """Добавление callback для ошибок"""
        self._error_callbacks.append(callback)
        self.logger.debug("📡 Error callback added")
    
    async def _call_signal_callbacks(self, processed_signal: ProcessedSignal) -> None:
        """Вызов callbacks для сигналов"""
        for callback in self._signal_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(processed_signal)
                else:
                    callback(processed_signal)
            except Exception as e:
                self.logger.error(f"❌ Signal callback error: {e}")
    
    async def _call_error_callbacks(self, error: Exception, processed_signal: ProcessedSignal) -> None:
        """Вызов callbacks для ошибок"""
        for callback in self._error_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(error, processed_signal)
                else:
                    callback(error, processed_signal)
            except Exception as e:
                self.logger.error(f"❌ Error callback error: {e}")
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    def _update_avg_processing_time(self, processing_time_ms: float) -> None:
        """Обновление среднего времени обработки"""
        current_avg = self.stats['avg_processing_time_ms']
        total_processed = self.stats['total_processed']
        
        if total_processed == 1:
            self.stats['avg_processing_time_ms'] = processing_time_ms
        else:
            # Экспоненциальное сглаживание
            alpha = 0.1
            self.stats['avg_processing_time_ms'] = (1 - alpha) * current_avg + alpha * processing_time_ms
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики процессора"""
        return {
            **self.stats,
            'incoming_queue_size': self._incoming_queue.qsize(),
            'completed_signals_count': len(self._completed_signals),
            'active_workers': len([t for t in self._processing_tasks if not t.done()]),
            'is_running': self._is_running
        }
    
    def get_recent_signals(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Получение последних обработанных сигналов"""
        recent = list(self._completed_signals)[-limit:]
        return [signal.to_dict() for signal in recent]
    
    async def health_check(self) -> Dict[str, Any]:
        """Проверка здоровья процессора"""
        try:
            health = {
                'status': 'healthy' if self._is_running else 'stopped',
                'is_running': self._is_running,
                'queue_size': self._incoming_queue.qsize(),
                'active_workers': len([t for t in self._processing_tasks if not t.done()]),
                'total_processed': self.stats['total_processed'],
                'error_rate': self.stats['processing_errors'] / max(self.stats['total_received'], 1),
                'avg_processing_time_ms': self.stats['avg_processing_time_ms']
            }
            
            # Определяем статус здоровья
            if not self._is_running:
                health['status'] = 'stopped'
            elif health['error_rate'] > 0.1:  # Более 10% ошибок
                health['status'] = 'unhealthy'
            elif health['queue_size'] > self.config.max_queue_size * 0.9:
                health['status'] = 'overloaded'
            else:
                health['status'] = 'healthy'
            
            return health
            
        except Exception as e:
            return {'status': 'error', 'error': str(e)}
    
    # Dependency injection
    def set_market_data_provider(self, provider) -> None:
        """Установка провайдера рыночных данных"""
        self.market_data_provider = provider
        self.logger.info("📊 Market data provider set")
    
    def set_ai_service(self, service) -> None:
        """Установка AI сервиса"""
        self.ai_service = service
        self.logger.info("🧠 AI service set")
    
    def set_notification_service(self, service) -> None:
        """Установка сервиса уведомлений"""
        self.notification_service = service
        self.logger.info("📢 Notification service set")


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def create_signal_processor(
    config: Optional[SignalProcessingConfig] = None,
    settings: Optional[Settings] = None,
    database: Optional[Database] = None
) -> SignalProcessor:
    """Создание экземпляра Signal Processor"""
    return SignalProcessor(config, settings, database)


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    async def test_signal_processor():
        """Тестирование Signal Processor"""
        print("🧪 Testing Signal Processor")
        print("=" * 50)
        
        try:
            # Создаем процессор
            processor = create_signal_processor()
            
            # Callback для обработанных сигналов
            def signal_callback(processed_signal: ProcessedSignal):
                print(f"✅ Signal processed: {processed_signal.original_signal.signal_type.value} "
                      f"(confidence: {processed_signal.original_signal.confidence:.2f})")
            
            processor.add_signal_callback(signal_callback)
            
            # Запускаем процессор
            await processor.start()
            print("✅ Signal Processor started")
            
            # Создаем тестовый сигнал
            from strategies.base_strategy import StrategyResult, SignalType
            
            test_signal = StrategyResult(
                signal_type=SignalType.BUY,
                confidence=0.85,
                entry_price=50000.0,
                stop_loss=49000.0,
                take_profit=52000.0,
                reasoning="Test signal for demonstration"
            )
            
            # Обрабатываем сигнал
            processed = await processor.process_signal(
                signal=test_signal,
                strategy_name="test_strategy",
                symbol="BTCUSDT",
                timeframe="5m"
            )
            
            if processed:
                print(f"✅ Signal submitted for processing: {processed.id}")
            
            # Ждем обработки
            await asyncio.sleep(2)
            
            # Статистика
            stats = processor.get_stats()
            print(f"📊 Stats: {stats}")
            
            # Health check
            health = await processor.health_check()
            print(f"🏥 Health: {health['status']}")
            
            # Последние сигналы
            recent = processor.get_recent_signals(5)
            print(f"📊 Recent signals: {len(recent)}")
            
        except Exception as e:
            print(f"❌ Test failed: {e}")
        finally:
            if 'processor' in locals():
                await processor.stop()
                print("✅ Signal Processor stopped")
    
    # Запуск теста
    asyncio.run(test_signal_processor())
